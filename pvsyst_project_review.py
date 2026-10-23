"""Readonly comparison of producer-declared inputs and saved native project fields.

This compares stored configuration, not effective runtime physics or source authenticity.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from pvsyst_component_editor import objects
from pvsyst_paths import project_name, safe_filename, variant_id, workspace_file


def _baseline_bytes(path):
    with path.open("rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("Preparation baseline exceeds 32 MiB")
    return data


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Expected a finite saved/declared number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Expected a finite saved/declared number") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite saved/declared number")
    return result


def review_project_inputs(store, *, project, variant, preparation_manifest_name):
    result = {"status": "blocked", "readonly": True, "issues": [], "comparisons": [],
              "declared_not_native": [], "native_consumption_verified": False,
              "runtime_state_observed": False, "repairs_applied": False}
    with store.lock:
        try:
            project = project_name(project)
            variant = variant_id(variant)
            path = workspace_file(store.workspace, "Results", preparation_manifest_name, ".json")
            baseline_bytes = _baseline_bytes(path)
            baseline = json.loads(baseline_bytes)
            if (not isinstance(baseline, dict) or baseline.get("operation") != "prepare_project"
                    or baseline.get("status") not in ("prepared", "needs_review") or baseline.get("prepared") is not True
                    or baseline.get("simulated") is not False or baseline.get("metrics") is not None
                    or not isinstance(baseline.get("request"), dict)
                    or not isinstance(baseline.get("preparation_context"), dict)):
                raise ValueError("Require a completed source-preparation baseline, not an annual/job result")
            request = baseline["request"]
            source = project_name(baseline["working_project"])
            member = variant_id(baseline["working_variant"])
            year = request["year"]
            if type(year) is not int or not 1969 <= year <= 2068:
                raise ValueError("Baseline year is outside the supported request range")
            expected_name = f"{Path(source).stem}_prepare_{year}.json"
            if (path.name.casefold() != expected_name.casefold() or request.get("new_project") != source
                    or request.get("variant") != member):
                raise ValueError("Baseline filename/project/variant declarations disagree")
            for key, extension in (("pan_name", ".PAN"), ("ond_name", ".OND"), ("site_name", ".SIT"), ("met_name", ".MET")):
                safe_filename(request[key], extension)
            _number(request["tilt"])
            if request.get("azimuth") is not None:
                _number(request["azimuth"])
            before = store.inspect_project(project)
            parameters = store.inspect_parameters(project, variant)
            references = store.inspect(project, variant)
            if (parameters["system_parameters"].get("SystemType") != "Grid"
                    or len(parameters["subarrays"]) != 1 or len(parameters["orientations"]) != 1
                    or len(references["subarrays"]) != 1):
                raise ValueError("Initial review supports one Grid subarray/orientation; no automatic topology mapping")
            array = parameters["subarrays"][0]
            orient = parameters["orientations"][0]
            values = orient["values"]
            if (values.get("FieldType") != "FixedPlane"
                    or any(values.get(key, "False") != "False" for key in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"))
                    or array["values"].get("NoOrientation") != str(orient["id"])):
                raise ValueError("Require a single assigned fixed non-seasonal plane for angle comparison")

            def compare(field, expected, actual, owner, unit=None, integer=False, text=False):
                if text:
                    same = isinstance(actual, str) and actual.casefold() == expected.casefold()
                    saved = expected
                else:
                    declared = _number(expected)
                    if integer:
                        if declared != declared.to_integral_value() or declared <= 0:
                            raise ValueError("Invalid baseline integer: " + field)
                        saved = str(int(declared))
                    else:
                        saved = format(float(declared), ".3f")
                    same = actual is not None and _number(actual) == _number(saved)
                result["comparisons"].append({"field": field, "owner": owner, "unit": unit,
                    "declared": expected, "expected_saved": saved, "actual_saved": actual,
                    "status": "missing" if actual is None else "matches" if same else "differs",
                    "scope": "stored_value_only"})

            refs = references["subarrays"][0]["references"]
            for kind, key in (("PAN", "pan_name"), ("OND", "ond_name")):
                entries = refs.get(kind, [])
                if len(entries) > 1:
                    raise ValueError("Ambiguous owning component reference: " + kind)
                compare(key, request[key], entries[0] if entries else None, f"subarray:{array['id']}", text=True)
            actual_sources = before["sources"][store._path(project, variant).name]
            for key, kind in (("site_name", "site"), ("met_name", "meteo")):
                compare(key, request[key], actual_sources[kind]["name"], "variant_sources", text=True)
                compare("project_" + key, request[key], before["sources"][store._path(project).name][kind]["name"],
                        "project_sources", text=True)
            compare("tilt", request["tilt"], values.get("FieldTilt"), f"orientation:{orient['id']}", "deg")
            if request.get("azimuth") is not None:
                compare("azimuth", request["azimuth"], values.get("FieldAzim"), f"orientation:{orient['id']}", "PVsyst_deg")
            plan = baseline.get("capacity_plan")
            if not isinstance(plan, dict) or plan.get("status") != "ready":
                raise ValueError("Baseline lacks its accepted capacity plan; counts are not inferred")
            for key, field in (("series_modules", "NModSerie"), ("string_count", "NStringCh"), ("inverter_count", "NInverter")):
                compare(field, plan[key], array["values"].get(field), f"subarray:{array['id']}", "count", integer=True)
            losses = request.get("losses") or {}
            if not isinstance(losses, dict) or set(losses) - {"LIDLoss", "PMismMPP", "ModuleQualityLoss", "UFactor"}:
                raise ValueError("Baseline contains an unsupported loss declaration")
            for key, value in losses.items():
                fields = ("PQualityModU", "PQualityModI") if key == "ModuleQualityLoss" else (key,)
                owner = "main_array" if key == "UFactor" else f"subarray:{array['id']}"
                actual = parameters["main_array"]["values"] if key == "UFactor" else array["values"]
                for field in fields:
                    compare(field, value, actual.get(field), owner, "W/m2/K" if key == "UFactor" else "%")
            project_roots = [block for block in objects(store._read(store._path(project)).decode("utf-8-sig"))
                             if not block.path and block.object_type == "pvProject"]
            if len(project_roots) != 1:
                raise ValueError("Expected one owning project object")
            raw_project = {key: project_roots[0].one(key)[1] for key in
                ("DesignTempMin", "TempOper", "TempOperMin", "TempOperMax", "TranspositionAlgorithm", "ACLossesRef")
                if key in project_roots[0].fields}
            context = baseline["preparation_context"]
            declared = context.get("request", {})
            if not isinstance(declared, dict):
                raise ValueError("Baseline preparation request must be an object")
            for key in ("electrical_design", "capacity"):
                if key in declared:
                    result["declared_not_native"].append({"field": key, "value": declared[key],
                        "status": "not_directly_comparable",
                        "reason": "Declared envelope/goal is not a native project setting; no temperature/coefficient/current owner is guessed"})
            result["declared_not_native"].append({"field": "year", "value": year, "status": "execution_option_not_compared"})
            binding = context.get("source_binding") or {}
            if not isinstance(binding, dict):
                raise ValueError("Baseline source binding must be an object")
            if before["files"] != store.inspect_project(project)["files"] or _baseline_bytes(path) != baseline_bytes:
                raise ValueError("Baseline or target changed during review; no mixed-state result accepted")
            differences = [row for row in result["comparisons"] if row["status"] != "matches"]
            result.update(status="needs_review" if differences or losses or before["warnings"] or baseline["status"] == "needs_review"
                          or context.get("review_required") or binding.get("needs_review") else "reviewed",
                source_reference_warnings=before["warnings"],
                project=project, variant=variant, baseline_source={"project": source, "variant": member, "manifest": str(path)},
                differences=differences, compared_fields=len(result["comparisons"]),
                inherited_project_saved=raw_project,
                native_mode_flags={"subarray": array["values"].get("Flags"),
                                   "main_array": parameters["main_array"]["values"].get("Flags"),
                                   "main_array_flags2": parameters["main_array"]["values"].get("Flags2")},
                loss_mode_interpretation_performed=False,
                unchecked_checks=["Saved disk state only; unsaved GUI/runtime values are not observed",
                    "Baseline declarations are not authenticated or certified; model/meteorology file contents are not compared",
                    "Loss values do not prove native default/manual mode or physical effectiveness",
                    "No automatic repair, geometry/cache invalidation or annual simulation was performed"])
            return result
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result.update(status="blocked", issues=[str(exc)], comparisons=[], declared_not_native=[])
            return result

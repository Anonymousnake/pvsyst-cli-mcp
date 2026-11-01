"""Compose source binding, project preparation and the existing native house task.

No independent job journal: preparation and house manifests own their existing stages.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from pvsyst_auto.native.cli import PVsystError
from pvsyst_auto.workflows.design import _native_preflight, _prune_empty, prepare_design_project
from pvsyst_auto.workflows.house import run_house_task
from pvsyst_auto.native.project import _processes
from pvsyst_auto.project.paths import project_name, variant_id, workspace_file
from pvsyst_auto.results import fingerprint
from pvsyst_auto.weather.preparation import load_weather_manifest
import pvsyst_auto.native.scene as native


PRESERVE_SOURCE_HARDWARE = "preserve_source_hardware"


def is_preserve_request(request) -> bool:
    """Shared branch predicate for the one explicit hardware-preserving mode."""
    return isinstance(request, dict) and request.get("prepare_mode") == PRESERVE_SOURCE_HARDWARE


def _preserve_child_request(request):
    """Build the child house request from the preserved source and year-only design.

    Only existing HouseTaskRequest members are forwarded, each with the caller's own value,
    so an explicit native/report_pdf=False/confirm flow is carried unchanged, never re-derived.
    """
    design = request["design"]
    child = {key: request[key] for key in ("new_project", "variant_name", "house", "layout")}
    child.update(source_project=request["source_project"],
                 source_variant=request["source_variant"], year=design["year"])
    if design.get("report_pages") is not None:
        child["report_pages"] = design["report_pages"]
    for key in ("module_geometry", "string_layout", "report_pdf", "shading_mode",
                "scene_import_method", "allow_window_creation"):
        if request.get(key) is not None:
            child[key] = request[key]
    return child


def _preserve_source_summary(store, project, variant, plan):
    """Read-only per-subarray inventory of the preserved source; no wiring or capacity is derived.

    Reads the same declared native subarray fields and per-group module geometry the house task
    already resolved (plan["source"]/plan["modules"]). A missing/unreadable variant yields the
    empty shape rather than a fabricated row; kWp is declared nominal only, not a sizing target.
    """
    from pvsyst_auto.components.editor import objects
    normalized = variant_id(variant)
    summary = {"source_project": project, "source_variant": normalized, "subarrays": [], "subarray_count": 0}
    groups, default = {}, {}
    if isinstance(plan, dict):
        for group in (plan.get("source") or {}).get("orientations") or []:
            # Key by the owning source subarray when present (several owners may share one
            # orientation), else by the orientation id for the legacy unique-owner shape.
            groups[group.get("subarray_id", group.get("id"))] = group.get("module_geometry") or {}
        default = plan.get("modules") or {}
    try:
        text = store._read(store._path(project, normalized)).decode("utf-8-sig")
    except (OSError, KeyError):
        return summary

    def _int(block, key):
        if key not in block.fields:
            return None
        try:
            return int(block.one(key)[1])
        except (TypeError, ValueError):
            return None

    for block in objects(text):
        if block.object_type != "pvSubArray":
            continue
        orientation = _int(block, "NoOrientation")
        subarray_id = _int(block, "SubArrayId")
        series, strings = _int(block, "NModSerie"), _int(block, "NStringCh")
        geometry = groups.get(subarray_id) or groups.get(orientation) or {}
        nom = geometry.get("nom_power", default.get("nom_power"))
        metadata = geometry.get("metadata_source") or default.get("metadata_source") or "declared-source"
        kwp = (round(nom * series * strings / 1000.0, 6)
               if isinstance(nom, (int, float)) and series and strings else None)
        summary["subarrays"].append({
            "subarray_id": _int(block, "SubArrayId"),
            "pv_module": block.one("PVModule")[1] if "PVModule" in block.fields else None,
            "inverter": block.one("GInverter")[1] if "GInverter" in block.fields else None,
            "no_orientation": orientation,
            "n_mod_serie": series,
            "n_string_ch": strings,
            "n_inv_mppt": _int(block, "NInvMPPT"),
            "n_inverter": _int(block, "NInverter"),
            "power_sharing_inverter_id": _int(block, "PowerSharingInverterId"),
            "nominal_power_w": nom,
            "nominal_kwp": kwp,
            "module_metadata_source": metadata,
        })
    summary["subarray_count"] = len(summary["subarrays"])
    return summary


def _run_preserve_source_hardware(store, components, cli, *, request, dry_run=True,
                                  confirm=False, resume=False, retry_simulation=False):
    """Preserve the existing source hardware and deliver it through the existing house task.

    Skips prepare_template/global resize/setIndependentMPPT entirely; the source variant is
    cloned verbatim by run_house_task. Native version/licence is still read through the existing
    _native_preflight (per-call window policy), and every house guard stays in force.
    """
    result = {"status": "blocked", "stage": "source_probe", "issues": [],
              "prepare_mode": PRESERVE_SOURCE_HARDWARE, "source_preparation": None,
              "source_summary": None, "house": None, "house_preflight_complete": False,
              "native_consumption_verified": False, "weather_native_comparison_performed": False}
    try:
        if any(type(flag) is not bool for flag in (dry_run, confirm, resume, retry_simulation)):
            raise ValueError("Use strict booleans")
        child = _preserve_child_request(request)
        preflight, error = _native_preflight(
            cli, "existing", prepare_only=True,
            allow_window_creation=request.get("allow_window_creation"))
        result["native_preflight"] = preflight
        if error is not None:
            result.update(status="blocked", stage="native_preflight", issues=[error])
            return result
        result["stage"] = "house_delivery"
        delivered = run_house_task(store, components, cli, request=child, dry_run=dry_run,
            confirm=confirm, resume=resume, retry_simulation=retry_simulation)
        result["house"] = delivered
        result["stage"] = delivered.get("stage") or "house_delivery"
        result["source_summary"] = _preserve_source_summary(
            store, request["source_project"], request["source_variant"], delivered.get("plan"))
        result["house_preflight_complete"] = delivered["status"] in (
            "ready", "completed", "needs_review", "confirmation_required")
        result.update(status=delivered["status"],
                      native_consumption_verified=delivered.get("native_consumption_verified", False))
        if delivered["status"] in ("completed", "needs_review"):
            result["stage"] = "completed"
        return result
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, PVsystError, subprocess.TimeoutExpired) as exc:
        result.update(status="blocked", stage="stopped", issues=[str(exc)],
                      native_consumption_verified=False)
        return result


def _source_checkpoint(store, design, binding):
    """Recheck canonical files, never execute or fingerprint arbitrary checkpoint paths."""
    name = project_name(design["new_project"])
    stem = Path(name).stem
    path = workspace_file(store.workspace, "Results", f"{stem}_prepare_{design['year']}.json", ".json")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Source preparation checkpoint exceeds 32 MiB")
    record = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("operation") != "prepare_project"
            or record.get("status") not in ("prepared", "needs_review") or record.get("prepared") is not True
            or record.get("simulated") is not False or record.get("native_consumption_verified") is not False
            or record.get("metrics") is not None or not isinstance(record.get("preparation_context"), dict)):
        raise ValueError("Source preparation is incomplete; inspect retained transactions before continuing")
    context = record["preparation_context"]
    if context.get("request") != _prune_empty(design) or context.get("source_binding") != binding:
        raise ValueError("Declared source/design differs from the prepared checkpoint")
    member = variant_id(record["working_variant"])
    if (record.get("working_project") != name or record["request"]["variant"] != member
            or store.inspect_project(name)["files"] != record.get("working_files")):
        raise ValueError("Prepared source project changed after preparation")
    weather = design["weather"]
    sit = weather.get("sit_name") or stem + ".SIT"
    met = weather.get("met_name") or stem + ".MET"
    expected = {}
    for kind, key in (("PAN", "module"), ("OND", "inverter")):
        filename = design[key]["file_name"]
        library = "workspace"
        file = store.components._path(kind, filename, library)
        if not file.is_file() and store.components.builtin:
            library = "builtin"
            file = store.components._path(kind, filename, library)
        expected[kind] = {"path": str(file), "sha256": fingerprint(file), "library": library}
    for label, folder, filename, ext in (("site_name", "Sites", sit, ".SIT"), ("met_name", "Meteo", met, ".MET")):
        file = workspace_file(store.workspace, folder, filename, ext)
        expected[label] = {"path": str(file), "sha256": fingerprint(file)}
    weather_manifest = weather.get("weather_manifest_name")
    if weather["mode"] != "existing":
        weather_manifest = weather_manifest or Path(met).stem + "_weather.json"
    warnings = []
    if weather_manifest:
        try:
            assets, _, warnings = load_weather_manifest(store.workspace, weather_manifest, design["year"], sit, met)
        except (KeyError, TypeError, AttributeError) as exc:
            # A manifest missing or mistyping recorded fields is an incomplete checkpoint, not a crash.
            raise ValueError(f"Incomplete weather preparation manifest: {weather_manifest}") from exc
        expected.update(assets)
    if record.get("inputs") != expected:
        raise ValueError("Prepared component/weather assets changed; no source or checkpoint was rewritten")
    review = (record["status"] == "needs_review" or context.get("review_required")
              or binding.get("needs_review") or bool(warnings))
    return {"project": name, "variant": member, "manifest": str(path), "needs_review": bool(review),
            "warnings": warnings, "configuration_summary": record.get("configuration_summary")}


def run_sourced_house_task(store, components, cli, *, request, binding=None, dry_run=True,
                           confirm=False, resume=False, retry_simulation=False, accept_source_review=False):
    # One shared workflow: the preserve branch reuses the existing house task verbatim and never
    # builds a loose-component binding; the legacy flow below is untouched.
    if is_preserve_request(request):
        return _run_preserve_source_hardware(store, components, cli, request=request,
            dry_run=dry_run, confirm=confirm, resume=resume, retry_simulation=retry_simulation)
    result = {"status": "blocked", "stage": "source_inputs", "issues": [], "source_binding": binding,
              "source_preparation": None, "house": None, "native_consumption_verified": False,
              "house_preflight_complete": False, "weather_native_comparison_performed": False}
    with store.lock:
        try:
            if any(type(flag) is not bool for flag in (dry_run, confirm, resume, retry_simulation, accept_source_review)):
                raise ValueError("Use strict booleans")
            # Reject an unusable explicit binding before any source-project write, rather than
            # preparing the source and only failing at the child house stage.
            if request.get("string_layout") is not None and not (
                    (request.get("scene_import_method") or "gui") == "native"
                    and (request.get("shading_mode") or "linear") == "module_layout"):
                result.update(status="blocked", stage="house_inputs",
                              issues=["An explicit string_layout requires scene_import_method='native' "
                                      "and shading_mode='module_layout'; nothing was prepared."])
                return result
            # A strict no-window request cannot be delivered by the GUI import method; reject
            # before any source preparation so a windowless request never prepares a project.
            if request.get("allow_window_creation") is False and (
                    (request.get("scene_import_method") or "gui") != "native"):
                result.update(status="blocked", stage="house_inputs",
                              issues=["allow_window_creation=false requires scene_import_method='native'; "
                                      "the GUI import method opens the native window, so nothing was prepared."])
                return result
            design = _prune_empty(request["design"])
            if not isinstance(design, dict) or not isinstance(binding, dict):
                raise ValueError("Provide the design request and the cited-source binding as objects")
            if design.get("capacity") is not None or design.get("electrical_design") is not None:
                raise ValueError("Provide cited source inputs, not a second capacity/electrical_design override")
            source_name = project_name(design["new_project"])
            final_name = project_name(request["new_project"])
            if source_name.casefold() == final_name.casefold():
                raise ValueError("Source preparation and final house require distinct new project names")
            for key in ("module", "inverter"):
                if not isinstance(design.get(key), dict) or not design[key].get("file_name"):
                    raise ValueError("Sourced house requires exact loose module/inverter filenames")
            if binding.get("status") not in ("ready", "needs_review") or not isinstance(binding.get("electrical_design"), dict):
                result.update(status="needs_context", issues=list(binding.get("issues") or []),
                              missing_fields=binding.get("missing_fields", []))
                return result
            draft = binding["electrical_design"]
            target = draft.get("target_dc_kw")
            if design.get("target_dc_kw") is not None and design["target_dc_kw"] != target:
                raise ValueError("Design target differs from the explicitly bound source/goal target")
            design.update(target_dc_kw=target, electrical_design=draft)
            if binding.get("needs_review") and not accept_source_review:
                result.update(status="needs_review", issues=["Set accept_source_review=true to continue; review stays in final status"])
                return result
            source_manifest = workspace_file(store.workspace, "Results",
                f"{Path(source_name).stem}_prepare_{design['year']}.json", ".json")
            house_manifest = workspace_file(store.workspace, "Results", Path(final_name).stem + "_house_task.json", ".json")
            source_exists = store._path(source_name).exists() or source_manifest.exists()
            if source_exists:
                if not resume:
                    raise ValueError("Source preparation already exists; explicit resume is required")
                prepared = _source_checkpoint(store, design, binding)
                result["source_preparation"] = prepared
            else:
                if resume:
                    raise ValueError("No completed source preparation checkpoint exists to resume")
                stem = Path(final_name).stem
                outputs = [("UserData", stem + "_house.pvc2", ".pvc2"),
                           ("Models", stem + "_house.SFI", ".SFI"),
                           ("Results", stem + "_house_task.json", ".json"),
                           ("Results", stem + "_house_run01.csv", ".csv"),
                           ("Results", stem + "_house_run01.pdf", ".pdf")]
                root = store._root()
                if ((root.exists() and any(store._belongs_to_project(p, final_name) for p in root.iterdir()))
                        or any(workspace_file(store.workspace, folder, name, ext).exists() for folder, name, ext in outputs)):
                    raise FileExistsError("Final house namespace already exists; source preparation was not started")
                preview = prepare_design_project(cli, store, spec=design, dry_run=True,
                    source_context=binding, allow_window_creation=request.get("allow_window_creation"))
                result.update(stage="source_preparation", source_preparation=preview)
                if preview["status"] not in ("ready", "ready_for_weather"):
                    result.update(status=preview["status"], issues=preview.get("issues", []))
                    return result
                if dry_run:
                    result.update(status="ready_for_preparation" if preview["status"] == "ready" else "ready_for_weather")
                    return result
                if not confirm:
                    result["status"] = "confirmation_required"
                    return result
                if _processes(native, (Path(cli.cli).parent / "PVsyst.exe").resolve()):
                    raise ValueError("Existing PVsyst GUI remains untouched; no source preparation was started")
                made = prepare_design_project(cli, store, spec=design, dry_run=False,
                    source_context=binding, allow_window_creation=request.get("allow_window_creation"))
                result["source_preparation"] = made
                if not made.get("prepared") or made["status"] not in ("prepared", "needs_review"):
                    result.update(status="needs_attention", issues=made.get("issues", []))
                    return result
                prepared = _source_checkpoint(store, design, binding)
                result["source_preparation"] = prepared
            if prepared["needs_review"] and not accept_source_review:
                result.update(status="needs_review", issues=["Source/weather review retained; explicit accept_source_review is required"])
                return result
            child = {key: request[key] for key in ("new_project", "variant_name", "house", "layout")}
            child.update(source_project=source_name, source_variant=prepared["variant"], year=design["year"])
            if request.get("module_geometry") is not None:
                child["module_geometry"] = request["module_geometry"]
            if design.get("report_pages") is not None:
                child["report_pages"] = design["report_pages"]
            # Forward the CSV-only selector whenever the caller set it: False must reach the
            # child house run instead of being dropped, while an omitted/None value adds no key
            # and so preserves the historical request/checkpoint shape.
            if request.get("report_pdf") is not None:
                child["report_pdf"] = request["report_pdf"]
            # Carry the native routing/selector, explicit per-cell binding and the per-request
            # window policy into the child house request; these stay absent for the historical
            # GUI/linear shape (is-not-None, so an explicit False is forwarded, never dropped).
            for key in ("shading_mode", "scene_import_method", "string_layout", "allow_window_creation"):
                if request.get(key) is not None:
                    child[key] = request[key]
            child_resume = house_manifest.exists()
            if child_resume and not resume:
                raise ValueError("House checkpoint already exists; explicit resume is required")
            result["stage"] = "house_delivery"
            delivered = run_house_task(store, components, cli, request=child, dry_run=dry_run,
                confirm=confirm, resume=child_resume, retry_simulation=retry_simulation)
            result["house"] = delivered
            result["house_preflight_complete"] = delivered["status"] in ("ready", "completed", "needs_review", "confirmation_required")
            latest = _source_checkpoint(store, design, binding)
            latest["needs_review"] |= prepared["needs_review"]
            prepared = latest
            result["source_preparation"] = prepared
            result.update(status=delivered["status"], native_consumption_verified=delivered.get("native_consumption_verified", False))
            if delivered["status"] in ("completed", "needs_review"):
                result["stage"] = "completed"
                if prepared["needs_review"]:
                    result["status"] = "needs_review"
            return result
        except (ValueError, OSError, KeyError, TypeError, RuntimeError, PVsystError, subprocess.TimeoutExpired) as exc:
            result.update(status="needs_attention" if result["source_preparation"] is not None else "blocked",
                          issues=[str(exc)], native_consumption_verified=False)
            return result

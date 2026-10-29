"""Compose source binding, project preparation and the existing native house task.

No independent job journal: preparation and house manifests own their existing stages.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from pvsyst_auto.native.cli import PVsystError
from pvsyst_auto.workflows.design import _prune_empty, prepare_design_project
from pvsyst_auto.workflows.house import run_house_task
from pvsyst_auto.native.project import _processes
from pvsyst_auto.project.paths import project_name, variant_id, workspace_file
from pvsyst_auto.results import fingerprint
from pvsyst_auto.weather.preparation import load_weather_manifest
import pvsyst_auto.native.scene as native


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


def run_sourced_house_task(store, components, cli, *, request, binding, dry_run=True,
                           confirm=False, resume=False, retry_simulation=False, accept_source_review=False):
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
                preview = prepare_design_project(cli, store, spec=design, dry_run=True, source_context=binding)
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
                made = prepare_design_project(cli, store, spec=design, dry_run=False, source_context=binding)
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
            # Carry the native routing/selector and explicit per-cell binding into the child
            # house request; these stay absent for the historical GUI/linear shape.
            for key in ("shading_mode", "scene_import_method", "string_layout"):
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

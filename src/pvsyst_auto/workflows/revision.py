"""Revise an existing native design and bind its annual outputs to the run inputs."""
from __future__ import annotations
import calendar
import csv
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import html
import json
import math
from pathlib import Path
import re
import subprocess
import zipfile
from pvsyst_auto.native.cli import PVsystError, VAR_GROUPS, build_sfi, iter_result_csv, result_units, validate_report_pages
from pvsyst_auto.components.editor import objects
from pvsyst_auto.energy.profiles import hourly_load_block, load_snapshot
from pvsyst_auto.energy.storage import create_grid_storage_variant, storage_state, validate_storage
from pvsyst_auto.engineering.comparison import build_project_handoff
from pvsyst_auto.project.paths import project_name, safe_filename, variant_id, workspace_file, write_json
from pvsyst_auto.results import POWER_TO_KW, annual_summary, fingerprint, verify_native_csv_context, verify_pdf_header
from pvsyst_auto.project.tracking import create_tracking_variant
from pvsyst_auto.weather.preparation import inspect_met_calendar
from pvsyst_auto.validation import CLI_TRACKING_BUILDS
ASSET_FOLDERS = {"Sites": (".SIT",), "Meteo": (".MET",), "ComposPV/PVmodules": (".PAN",),
                 "ComposPV/Inverters": (".OND",), "ComposPV/Batteries": (".BTR",),
                 "UserData": (".csv", ".pvc2"), "UserHourlyParams": (".csv",),
                 "Models": (".SFI",), "Results": (".json",)}
COMPONENT_KINDS = {"ComposPV/PVmodules": "PAN", "ComposPV/Inverters": "OND", "ComposPV/Batteries": "BTR"}
def _asset_extension(folder, name):
    extensions = ASSET_FOLDERS.get(folder)
    if extensions is None: raise ValueError("Unsupported run input location")
    if not isinstance(name, str): raise ValueError("Run input asset name must be a filename")
    extension = Path(name).suffix
    if extension.casefold() not in {item.casefold() for item in extensions}: raise ValueError(f"Unsupported {folder} asset extension: {name}")
    return next(item for item in extensions if item.casefold() == extension.casefold())
def _asset_path(store, asset):
    folder, name, library = asset["folder"], asset["name"], asset["library"]
    if folder not in ASSET_FOLDERS or library not in ("workspace", "builtin"): raise ValueError("Unsupported run input location")
    extension = _asset_extension(folder, name)
    safe_filename(name, extension)
    if folder in COMPONENT_KINDS:
        kind = COMPONENT_KINDS[folder]
        if library == "builtin" and store.components._path(kind, name).exists():
            raise ValueError("A workspace component now shadows the run input: " + name)
        return store.components._path(kind, name, library)
    if library != "workspace": raise ValueError("Non-component run inputs must be workspace assets")
    return workspace_file(store.workspace, folder, name, extension)
def _check_assets(store, record):
    assets = record.get("input_assets")
    if not isinstance(assets, dict) or not {"site", "meteo"} <= set(assets): raise ValueError("Run input asset record is incomplete")
    for asset in assets.values():
        if fingerprint(_asset_path(store, asset)) != asset["sha256"]: raise ValueError("A bound run input has changed: " + asset["name"])
    for reference in record.get("database_references", []):
        kind, name = reference["kind"], reference["name"]
        if kind not in ("PAN", "OND", "BTR"): raise ValueError("Unsupported database reference")
        safe_filename(name, "." + kind)
        libraries = ("workspace", "builtin") if store.components.builtin else ("workspace",)
        if any(store.components._path(kind, name, library).exists() for library in libraries):
            raise ValueError("A loose component now shadows an uninspected database reference: " + name)
def _result_path(store, record, kind):
    extension = {"csv": ".csv", "pdf": ".pdf"}[kind]
    value = record["artifacts"][kind]
    if not isinstance(value, str): raise ValueError("Run artifact path must be a string")
    path = workspace_file(store.workspace, "Results", Path(value).name, extension)
    if Path(value).resolve() != path: raise ValueError("Run artifact is outside the workspace Results directory")
    return path
def run_output_kinds(record):
    if record["operation"] != "engineering_house": return ("csv", "pdf")
    return ("csv",) if record["request"].get("report_pdf") is False else ("csv", "pdf")
def _check_house_run(store, record, path, project, variant):
    request, plan, runs = record["request"], record["plan"], record["runs"]
    year = record["year"]
    if (type(record.get("schema")) is not int or record["schema"] != 1
            or record.get("phase") != "completed" or not isinstance(request, dict)
            or type(year) is not int or not 1969 <= year <= 2068
            or request.get("new_project") != project or request.get("year") != year
            or variant_id(record["variant"]) != variant
            or not isinstance(runs, list) or not 1 <= len(runs) <= 99
            or any(not isinstance(run, dict) for run in runs)):
        raise ValueError("Require a completed house checkpoint for this project, variant and year")
    kinds = run_output_kinds(record)
    attempt = runs[-1]
    if (attempt.get("status") != "returned" or type(attempt.get("number")) is not int
            or attempt["number"] != len(runs) or not isinstance(attempt.get("native"), dict)
            or not isinstance(attempt["native"].get("warnings"), list)
            or not isinstance(record.get("output_guards"), dict)
            or set(record["output_guards"]) != set(kinds)
            or record.get("result_files") != record["output_guards"]):
        raise ValueError("House run lacks a returned native attempt and matching output guards")
    stem = Path(project).stem
    expected = {"manifest": workspace_file(store.workspace, "Results", stem + "_house_task.json", ".json"),
                "scene": workspace_file(store.workspace, "UserData", stem + "_house.pvc2", ".pvc2"),
                "sfi": workspace_file(store.workspace, "Models", stem + "_house.SFI", ".SFI"),
                "native_variant": store._path(project, variant)}
    if expected["manifest"] != path: raise ValueError("House manifest name does not identify its project task")
    for kind, target in expected.items():
        if Path(record["artifacts"][kind]).resolve() != target: raise ValueError("House artifact does not match its controlled project path: " + kind)
    for kind in kinds:
        name = stem + f"_house_run{len(runs):02d}." + kind
        if attempt.get(kind) != name or _result_path(store, record, kind).name != name:
            raise ValueError("House outputs do not identify the last recorded native attempt")
    assets, guards = record["input_assets"], record["guards"]
    for label, key, folder in (("site", "site", "Sites"), ("meteo", "met", "Meteo")):
        if (assets[label].get("folder") != folder or assets[label].get("library") != "workspace"
                or str(assets[label].get("name", "")).casefold() != plan[key].casefold()
                or assets[label].get("sha256") != guards[key]):
            raise ValueError("House weather input binding differs from its prepared native context")
    for kind in ("scene", "sfi"):
        if (kind not in assets or assets[kind].get("library") != "workspace"
                or _asset_path(store, assets[kind]) != expected[kind]
                or assets[kind]["sha256"] != guards[kind]):
            raise ValueError("House scene/SFI input binding is incomplete or misidentified")
    selected = Path(project).stem + "." + variant
    handoff = record["input_handoff"]
    if (guards["native_variant"] != record["working_files"][selected]
            or guards["working_project"] != record["working_files"][project]
            or handoff["input_snapshot"].get("project_file") != project
            or handoff["input_snapshot"].get("variant_file") != selected
            or handoff["input_snapshot"].get("variant_sha256") != guards["native_variant"]):
        raise ValueError("House runtime handoff does not match its guarded native saved variant")
    imported = record["native_import"]
    if (not isinstance(imported, dict) or imported.get("status") != "native_variant_created"
            or imported.get("stored_geometry_verified") is not True
            or not isinstance(imported.get("verification"), dict)
            or variant_id(imported["variant"]) != variant
            or Path(imported["file"]).resolve() != expected["native_variant"]):
        raise ValueError("House checkpoint lacks a verified native save for this variant")
    expected_rows = 8784 if calendar.isleap(year) else 8760
    if (record.get("hourly_rows") != expected_rows or record["metrics"].get("rows") != expected_rows
            or record["metrics"].get("year") != year or record["metrics"].get("complete_year") is not True
            or record["metrics"].get("sha256") != record["result_files"]["csv"]):
        raise ValueError("House annual result evidence is incomplete or differs from the recorded CSV")
    expected_context = {"Project": project, "Simulation variant": selected, "Geographical Site": plan["site"], "Weather data": plan["met"]}
    context = record.get("native_context")
    if (not isinstance(context, dict) or set(context) != set(expected_context)
            or any(not isinstance(context[key], str) or context[key].casefold() != value.casefold() for key, value in expected_context.items())):
        raise ValueError("House native context does not identify its bound project, variant and weather")
    if not isinstance(record.get("changes"), list) or any(not isinstance(item, dict) for item in record["changes"]):
        raise ValueError("House engineering changes must be a recorded change list")
def load_engineering_run(store, manifest_name, *, project=None, variant=None, csv_name=None):
    """Read a completed run binding, refusing changed or misidentified inputs/outputs."""
    with store.lock:
        path = workspace_file(store.workspace, "Results", manifest_name, ".json")
        with path.open("rb") as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024: raise ValueError("Engineering run manifest exceeds 32 MiB")
        record = json.loads(raw)
        try:
            if (not isinstance(record, dict) or record.get("operation") not in ("engineering_revision", "engineering_house")
                    or type(record.get("schema_version")) is not int or record["schema_version"] != 1
                    or record.get("stage") != "completed"
                    or record.get("status") not in ("completed", "needs_review")
                    or record.get("native_consumption_verified") is not True
                    or (record.get("operation") == "engineering_house" and record.get("phase") != "completed")):
                raise ValueError("Require a completed native engineering run binding")
            actual_project = project_name(record["working_project"])
            actual_variant = variant_id(record["working_variant"])
            if (project is not None and project_name(project).casefold() != actual_project.casefold()
                    or variant is not None and variant_id(variant) != actual_variant):
                raise ValueError("Run binding identifies a different project or variant")
            handoff = record["input_handoff"]
            if (handoff.get("project", "").casefold() != actual_project.casefold()
                    or handoff.get("variant") != actual_variant or handoff.get("status") == "blocked"):
                raise ValueError("Run input handoff identifies a different or unreadable design")
            if store.inspect_project(actual_project)["files"] != record["working_files"]:
                raise ValueError("The saved project has changed since this native run")
            if record.get("operation") == "engineering_house": _check_house_run(store, record, path, actual_project, actual_variant)
            _check_assets(store, record)
            for kind in run_output_kinds(record):
                target = _result_path(store, record, kind)
                if kind == "csv" and csv_name is not None and target.name.casefold() != csv_name.casefold():
                    raise ValueError("Run binding identifies a different result CSV")
                if fingerprint(target) != record["result_files"][kind]: raise ValueError("A bound native output has changed: " + target.name)
            manifest_path = workspace_file(store.workspace, "Results", Path(record["artifacts"]["manifest"]).name, ".json")
            if manifest_path != path or Path(record["artifacts"]["manifest"]).resolve() != path:
                raise ValueError("Run manifest identifies a different manifest file")
            if path.read_bytes() != raw: raise ValueError("Run binding changed while it was read")
        except (KeyError, TypeError, AttributeError) as exc: raise ValueError("Incomplete engineering run binding") from exc
        return record
def _context(path, project, variant, site, meteo):
    expected = {"Project": project, "Simulation variant": Path(project).stem + "." + variant, "Geographical Site": site, "Weather data": meteo}
    return verify_native_csv_context(path, expected, mismatch_message="Native output identifies a different project/variant/site/weather")
def run_engineering_task(cli, store, *, source_project, source_variant, new_project, new_variant,
                         objective, year, nominal_dc_power_kw, power_source, changes,
                         declarations=None, report_pages=None, dry_run=True,
                         load_manifest_name="", availability=None):
    """One isolated revision and one native annual execution; no automatic retry."""
    record = {"operation": "engineering_revision", "schema_version": 1, "status": "blocked",
              "stage": "preflight", "dry_run": dry_run, "native_consumption_verified": False,
              "issues": [], "warnings": [], "changes": [], "metrics": None, "artifacts": {}}
    with store.lock:
        try:
            if type(dry_run) is not bool: raise ValueError("Use a strict boolean dry_run")
            source_project, new_project = project_name(source_project), project_name(new_project)
            source_variant, new_variant = variant_id(source_variant), variant_id(new_variant)
            if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
                raise ValueError("State the engineering objective in 1..4000 characters")
            if not isinstance(power_source, str) or not power_source.strip() or len(power_source) > 4000:
                raise ValueError("Declare the source of the nominal DC power")
            if type(year) is not int or not 1969 <= year <= 2068: raise ValueError("Annual year must be an integer in 1969..2068")
            if (type(nominal_dc_power_kw) not in (int, float) or not math.isfinite(nominal_dc_power_kw) or nominal_dc_power_kw <= 0):
                raise ValueError("Declare a positive finite nominal DC power in kWp")
            if not isinstance(changes, dict) or set(changes) - {"tilt", "azimuth", "losses", "tracking", "storage"}:
                raise ValueError("Use supported explicit orientation, loss or tracking changes")
            if report_pages is not None: validate_report_pages(report_pages)
            original = store.inspect_project(source_project)
            if source_variant not in original["variants"]: raise ValueError("Source variant is missing")
            if new_variant in original["variants"]: raise ValueError("Choose a new variant absent from the source project")
            if any(store._belongs_to_project(p, new_project) for p in store._root().iterdir()):
                raise FileExistsError("Choose a new isolated project name")
            source_handoff = build_project_handoff(store, project=source_project, variant=source_variant, declarations=declarations)
            if source_handoff["status"] == "blocked": raise ValueError("Source handoff is blocked: " + str(source_handoff["issues"]))
            parameters = store.inspect_parameters(source_project, source_variant)
            structure = store.validate_structure(source_project, source_variant)
            if (structure["valid"] is not True or structure["system_type"] != "Grid"
                    or len(parameters["subarrays"]) != 1 or len(parameters["orientations"]) != 1):
                raise ValueError("Revision supports one completely checked Grid subarray and orientation")
            data = store._read(store._path(source_project, source_variant))
            _, lines = store._text(data)
            if set(changes) & {"tilt", "azimuth", "tracking"}: store._require_no_geometry(lines)
            blocks = objects("".join(lines))
            storage_mode = any(block.object_type == "pvGridStorage" for block in blocks)
            if not storage_mode and (any(block.object_type == "pvLoad" for block in blocks)
                                     or load_manifest_name or availability is not None or "storage" in changes):
                raise ValueError("Load/profile declarations require an existing native AC storage system")
            root = next(block for block in blocks if not block.path)
            if not storage_mode and root.one("PRCorrTemp")[1] != "False": raise ValueError("Standard annual grid PR requires PRCorrTemp=False")
            array = parameters["subarrays"][0]
            orientation = parameters["orientations"][0]
            tracking_mode = orientation["values"].get("FieldType") == "UnlimTracking"
            storage_preparation = None
            if storage_mode:
                if set(changes) != {"storage"} or orientation["values"].get("FieldType") != "FixedPlane":
                    raise ValueError("Existing AC storage revisions accept explicit storage updates only on a fixed plane")
                storage_preparation = prepare_storage_revision(store, source_project, source_variant,
                    new_variant, changes["storage"], load_manifest_name, year, availability=availability)
                if storage_preparation["status"] != "ready": raise ValueError("Storage preparation: " + str(storage_preparation["issues"]))
                preview = storage_preparation["preview"]
                before, after = storage_preparation["before"], storage_preparation["after"]
                record["changes"] = [{"field": "storage." + key, "before": before.get(key), "after": value}
                                     for key, value in after.items() if before.get(key) != value]
                record["scope"] = "existing_native_ac_storage"
                record["load_binding"] = {"manifest_name": load_manifest_name, "year": year,
                    "time_basis": storage_preparation["time_basis"], "utc_offset_hours": storage_preparation["utc_offset_hours"],
                    "rows": len(storage_preparation["load_values"]), "embedded_snapshot_matches": True,
                    "normalized_csv_name": Path(storage_preparation["load_record"]["artifacts"]["csv"]).name,
                    "source_csv_name": storage_preparation["load_record"]["source"]["name"]}
                record["warnings"].extend(storage_preparation["warnings"])
            elif tracking_mode:
                if set(changes) != {"tracking"}: raise ValueError("UnlimTracking revisions require explicit tracking controls only")
                preview = create_tracking_variant(store, project=source_project, source_variant=source_variant,
                                                   new_variant=new_variant, tracking=changes["tracking"], dry_run=True)
                if preview["status"] != "ready" or preview.get("field_type") != "UnlimTracking":
                    raise ValueError("Tracking revision is outside the verified template: " + str(preview["issues"]))
                record["changes"] = [{"field": "orientations." + field, "before": orientation["values"].get(field), "after": value}
                    for field, value in preview["tracking"].items()
                    if orientation["values"].get(field) != value]
            else:
                if orientation["values"].get("FieldType") != "FixedPlane" or "tracking" in changes:
                    raise ValueError("Require a fixed plane or the supported existing UnlimTracking template")
                angles = {native: changes[key] for key, native in (("tilt", "FieldTilt"), ("azimuth", "FieldAzim")) if key in changes}
                losses = changes.get("losses", {})
                if not isinstance(losses, dict) or set(losses) - {"LIDLoss", "PMismMPP", "ModuleQualityLoss", "UFactor"}:
                    raise ValueError("Unsupported loss changes")
                subarray_losses = {key: value for key, value in losses.items() if key != "UFactor"}
                main_losses = {"UFactor": losses["UFactor"]} if "UFactor" in losses else {}
                preview = (store.update_parameters(source_project, source_variant, parameters["sha256"],
                    array["id"], orientation["id"], orientation_updates=angles or None,
                    subarray_updates=subarray_losses or None, main_array_updates=main_losses or None, dry_run=True)
                    if angles or losses else {"status": "ready", "mode": "preserve_saved_design"})
                for field, value in angles.items():
                    if orientation["values"].get(field) != f"{value:.3f}":
                        record["changes"].append({"field": "orientations." + field,
                            "before": orientation["values"].get(field), "after": f"{value:.3f}"})
                for field, value in losses.items():
                    owner = parameters["main_array"]["values"] if field == "UFactor" else array["values"]
                    before = ({key: owner.get(key) for key in ("PQualityModU", "PQualityModI")} if field == "ModuleQualityLoss" else owner.get(field))
                    record["changes"].append({"field": ("main_array." if field == "UFactor" else "subarrays.") + field,
                                              "before": before, "after": f"{value:.3f}"})
            references = original["sources"]
            project_refs, variant_refs = references[source_project], references[Path(source_project).stem + "." + source_variant]
            assets = {}
            for label, folder in (("site", "Sites"), ("meteo", "Meteo")):
                name = variant_refs[label]["name"]
                if not name or name.casefold() != (project_refs[label]["name"] or "").casefold():
                    raise ValueError("Project and variant must bind the same explicit " + label)
                asset = {"folder": folder, "name": name, "library": "workspace"}
                asset["sha256"] = fingerprint(_asset_path(store, asset))
                assets[label] = asset
            record["input_assets"], record["database_references"] = assets, []
            if storage_preparation is not None:
                assets.update(storage_preparation["input_assets"])
                record["database_references"].extend(storage_preparation["database_references"])
            component_refs = store.inspect(source_project, source_variant)["subarrays"][0]["references"]
            for kind, folder in (("PAN", "ComposPV/PVmodules"), ("OND", "ComposPV/Inverters")):
                if len(component_refs[kind]) != 1: raise ValueError("Require one explicit " + kind + " reference")
                name = component_refs[kind][0]
                libraries = ("workspace", "builtin") if store.components.builtin else ("workspace",)
                library = next((item for item in libraries if store.components._path(kind, name, item).is_file()), None)
                if library is None: record["database_references"].append({"kind": kind, "name": name, "resolution": "native_database_uninspected"})
                else:
                    asset = {"folder": folder, "name": name, "library": library}
                    asset["sha256"] = fingerprint(_asset_path(store, asset))
                    assets[kind] = asset
            calendar = inspect_met_calendar(_asset_path(store, assets["meteo"]), year)
            if calendar["issues"]: raise ValueError("Weather calendar: " + "; ".join(calendar["issues"]))
            record["warnings"].extend(calendar["warnings"])
            if storage_preparation is not None:
                site_blocks = objects(_asset_path(store, assets["site"]).read_text(encoding="utf-8-sig"))
                sites = [block for block in site_blocks if block.object_type == "pvSite" and not block.path]
                if len(sites) != 1 or float(sites[0].one("TimeZone")[1]) != storage_preparation["utc_offset_hours"]:
                    raise ValueError("Storage site standard UTC offset differs from the declared hourly load")
            prefix = f"{Path(new_project).stem}_engineering_{year}"
            paths = {kind: workspace_file(store.workspace, folder, prefix + extension, extension)
                     for kind, folder, extension in (("manifest", "Results", ".json"), ("csv", "Results", ".csv"),
                                                     ("pdf", "Results", ".pdf"), ("sfi", "Models", ".SFI"))}
            if any(path.exists() for path in paths.values()): raise FileExistsError("An engineering output already exists; choose a new project name")
            for option in ("input-sfi-file", "start-date", "end-date", "time-step", "site", "input-met-file"): cli._require("run-simulation", option)
            license_state = cli.license_info()
            if license_state.get("status") == "TRIAL" and license_state.get("remaining_executions") == 0:
                raise ValueError("PVsystCLI reports no remaining trial executions")
            version = cli.version()
            if tracking_mode and version not in CLI_TRACKING_BUILDS:
                raise ValueError("Tracking revision requires a verified PVsystCLI build: " + ", ".join(CLI_TRACKING_BUILDS))
            record.update(status="ready", source_project=source_project, source_variant=source_variant,
                          working_project=new_project, working_variant=new_variant, year=year,
                          objective=objective, nominal_dc_power_kw=nominal_dc_power_kw, power_source=power_source,
                          requested_changes=changes, source_handoff=source_handoff,
                          source_files=original["files"], configuration_preview=preview,
                          artifacts={kind: str(path) for kind, path in paths.items()},
                          cli_version=version, license=license_state, report_pages=report_pages)
            if storage_mode: record.update(load_manifest_name=load_manifest_name, availability=availability)
            if record["database_references"]: record["warnings"].append("Inherited native database model contents are uninspected.")
        except (ValueError, OSError, KeyError, TypeError, PVsystError, subprocess.TimeoutExpired) as exc:
            record["issues"].append(str(exc))
            return record
        if dry_run: return record
        def save(stage=None, first=False):
            if stage: record.update(status="running", stage=stage)
            write_json(paths["manifest"], record, first=first)
        manifest_owned = False
        try:
            save("clone", first=True)
            manifest_owned = True
            if store.inspect_project(source_project)["files"] != original["files"]: raise ValueError("Source changed after preflight")
            _check_assets(store, record)
            store.clone_project(source_project, new_project)
            save("revision")
            if storage_mode:
                if changes["storage"] or availability is not None:
                    edited = create_grid_storage_variant(store, project=new_project, source_variant=source_variant,
                        new_variant=new_variant, updates=changes["storage"], availability=availability, dry_run=False)
                    if edited["status"] != "created": raise ValueError("Storage revision failed: " + str(edited["issues"]))
                else: edited = store.clone(new_project, source_variant, new_variant)
            elif tracking_mode:
                edited = create_tracking_variant(store, project=new_project, source_variant=source_variant,
                    new_variant=new_variant, tracking=changes["tracking"], dry_run=False)
                if edited["status"] != "created": raise ValueError("Tracking revision failed: " + str(edited["issues"]))
            else:
                cloned = store.clone(new_project, source_variant, new_variant)
                edited = (store.update_parameters(new_project, new_variant, cloned["sha256"], array["id"], orientation["id"],
                    orientation_updates=angles or None, subarray_updates=subarray_losses or None,
                    main_array_updates=main_losses or None) if angles or losses else cloned)
            record["revision"] = edited
            if store.validate_structure(new_project, new_variant)["valid"] is not True:
                raise ValueError("Revised design failed native structure checks")
            record["working_files"] = store.inspect_project(new_project)["files"]
            record["input_handoff"] = build_project_handoff(store, project=new_project, variant=new_variant, declarations=declarations)
            if record["input_handoff"]["status"] == "blocked": raise ValueError("Revised input handoff is blocked")
            save("simulation")
            _check_assets(store, record)
            variables = list(VAR_GROUPS["grid_storage" if storage_mode else "all_common"])
            if tracking_mode: variables.append("viGlobShd")
            build_sfi(paths["sfi"], variables)
            run = cli.run_simulation(new_project, new_variant, sfi=paths["sfi"], out_csv=paths["csv"],
                report_pdf=paths["pdf"], site=_asset_path(store, assets["site"]), met=_asset_path(store, assets["meteo"]),
                time_step="hour", start_date=f"{year}.01.01", end_date=f"{year}.12.31",
                report_language="en", report_pages=report_pages, log_level=3)
            record["run"] = run
            save("results")
            record["native_context"] = _context(paths["csv"], new_project, new_variant, assets["site"]["name"], assets["meteo"]["name"])
            if storage_mode:
                record.update(read_storage_year(paths["csv"], paths["pdf"], year,
                    storage_preparation["load_record"], storage_preparation["load_values"], storage_preparation["after"]))
                record["metrics"]["sha256"] = fingerprint(paths["csv"])
            else: record["metrics"] = annual_summary(paths["csv"], year, nominal_dc_power_kw)
            count = 0
            for headers, row in iter_result_csv(paths["csv"], strict=True):
                if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in row[1:]):
                    raise ValueError("Native output contains missing or nonfinite numeric data")
                count += 1
            if tracking_mode and result_units(paths["csv"], strict=True).get("GlobShd") != "W/m\u00b2":
                raise ValueError("Tracking output lacks native GlobShd in W/m2")
            verify_pdf_header(paths["pdf"])
            record["result_files"] = {"csv": record["metrics"]["sha256"], "pdf": fingerprint(paths["pdf"])}
            record["warnings"].extend(run.get("warnings", []))
            record["warnings"].extend(record["metrics"].get("diagnostic_flags", []))
            record["warnings"].extend(record.get("storage_output", {}).get("review_items", []))
            record.update(stage="completed", native_consumption_verified=True, hourly_rows=count, numeric_columns=headers[1:])
            review = (record["database_references"] or run.get("warnings") or calendar["warnings"]
                      or record["metrics"].get("diagnostic_flags") or record.get("storage_output", {}).get("review_items")
                      or source_handoff.get("review_items")
                      or source_handoff.get("missing_items"))
            record["status"] = "needs_review" if review else "completed"
        except (ValueError, OSError, KeyError, TypeError, PVsystError, subprocess.TimeoutExpired) as exc:
            record.update(status="failed", native_consumption_verified=False, error=cli._redact(str(exc))[-12000:])
        except BaseException as exc:
            # An unanticipated error still leaves a terminal manifest, never "running"; it then propagates.
            record.update(status="failed", native_consumption_verified=False, error=cli._redact(f"{type(exc).__name__}: {exc}")[-12000:])
            if manifest_owned:
                try:
                    save()
                except Exception:
                    pass  # a failed save must not replace the original error
            raise
        try:
            record["source_preserved"] = store.inspect_project(source_project)["files"] == original["files"]
            if not record["source_preserved"]:
                record.update(status="failed", native_consumption_verified=False, error="Source changed during the engineering task")
        except (ValueError, OSError, KeyError, TypeError) as exc: record.update(status="failed", native_consumption_verified=False, error=str(exc))
        record["recovery"] = "Retain the isolated project and outputs for review; no automatic retry, overwrite, deletion or rollback. Use a new project name for a new execution."
        if manifest_owned: save()
        return record
def _load_signature(text):
    """Keep cache structure/tokens; display text and historical paths are metadata."""
    signature = []
    for line in text.splitlines():
        if not (line := line.strip()): continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if separator and name in ("Comment", "PathName"): continue
        if separator and name == "FileName": value = value.casefold()
        if separator and name in ("WRFactor", "OriginalScale", "RenormVal", "NColDate", "DateFormat"):
            if not (number := Decimal(value)).is_finite(): raise ValueError("Nonfinite native load field: " + name)
            value = number
        signature.append((name, value) if separator else line)
    return signature
def prepare_storage_revision(store, project, variant, new_variant, updates, load_manifest_name, year, availability=None):
    """Preview an existing native storage edit and bind its actual embedded load."""
    result = {"status": "blocked", "issues": [], "warnings": [], "input_assets": {}, "database_references": []}
    with store.lock:
        try:
            if type(year) is not int or not 1969 <= year <= 2068: raise ValueError("Storage year must be an integer in 1969..2068")
            source = store._path(project, variant)
            original = store._read(source)
            _, lines = store._text(original)
            _, system = store._sections(lines)
            before, _, _ = storage_state(lines, system)
            blocks = objects("".join(lines))
            root = next(block for block in blocks if not block.path)
            flags = root.one("Flags")[1]
            if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flags) or not int(flags[1:], 16) & 0x00080000:
                raise ValueError("Storage delivery requires the enabled native user-load bit")
            loads = [block for block in blocks if block.object_type == "pvLoad"]
            if len(loads) != 1 or loads[0].path != "PVObject_UserNeeds" or not loads[0].end_line:
                raise ValueError("Require one complete directly owned native hourly load")
            record, profile, values = load_snapshot(store.workspace, load_manifest_name)
            manifest_path = workspace_file(store.workspace, "Results", load_manifest_name, ".json")
            raw_source_path = workspace_file(store.workspace, "UserData", record["source"]["name"], ".csv")
            snapshots = {path: path.read_bytes() for path in (manifest_path, raw_source_path, profile)}
            if record["year"] != year: raise ValueError("Prepared load and native annual execution must use the same year")
            expected, encoding = hourly_load_block(profile, values, year, "\n")
            stored = "\n".join(lines[loads[0].line - 1:loads[0].end_line])
            if _load_signature(stored) != _load_signature(expected):
                raise ValueError("Native hourly load cache/time settings differ from the prepared load; rebind using the existing load tool")
            if availability is not None:
                if not isinstance(availability, dict): raise ValueError("Grid availability must be an explicit declaration")
                if any(availability.get(key) != record[key] for key in ("year", "utc_offset_hours", "time_basis")):
                    raise ValueError("Grid availability and prepared load must use the same year and standard time basis")
                path = workspace_file(store.workspace, "UserData", availability["csv_name"], ".csv")
                snapshots[path] = path.read_bytes()
            if updates == {} and availability is None:
                validate_storage(before)
                if store._path(project, new_variant).exists(): raise FileExistsError("Choose a new grid storage variant")
                preview = {"status": "ready", "before": before, "after": before, "warnings": []}
            else:
                preview = create_grid_storage_variant(store, project=project, source_variant=variant,
                    new_variant=new_variant, updates=updates, availability=availability, dry_run=True)
            result["preview"] = preview
            if preview["status"] != "ready": raise ValueError("Storage revision is blocked: " + "; ".join(preview["issues"]))
            assets = {}
            for label, folder, name in (("load_manifest", "Results", load_manifest_name),
                                        ("load_source", "UserData", record["source"]["name"]),
                                        ("load_profile", "UserHourlyParams", profile.name)):
                path = workspace_file(store.workspace, folder, name, ".json" if label == "load_manifest" else ".csv")
                assets[label] = {"folder": folder, "name": name, "library": "workspace", "sha256": fingerprint(path)}
            if availability is not None:
                path = workspace_file(store.workspace, "UserData", availability["csv_name"], ".csv")
                assets["grid_availability"] = {"folder": "UserData", "name": availability["csv_name"], "library": "workspace", "sha256": fingerprint(path)}
            libraries = ("workspace", "builtin") if store.components.builtin else ("workspace",)
            battery = before["battery_file"]
            library = next((item for item in libraries if store.components._path("BTR", battery, item).is_file()), None)
            if library is not None:
                assets["BTR"] = {"folder": "ComposPV/Batteries", "name": battery, "library": library, "sha256": fingerprint(store.components._path("BTR", battery, library))}
            else: result["database_references"].append({"kind": "BTR", "name": battery, "resolution": "native_database_uninspected"})
            if store._read(source) != original or any(path.read_bytes() != data for path, data in snapshots.items()):
                raise ValueError("A source variant or prepared load/availability input changed during preflight")
            result.update(status="ready", before=before, after=preview["after"], load_record={**record, "native_encoding": encoding},
                          load_values=values, input_assets=assets, utc_offset_hours=record["utc_offset_hours"], time_basis=record["time_basis"], load_encoding=encoding)
            result["warnings"].extend(preview["warnings"])
        except (ValueError, OSError, KeyError, TypeError, StopIteration, ArithmeticError, csv.Error) as exc: result["issues"].append(str(exc))
    return result
def _power_error(value):
    return max(0.001, 0.5 * 10 ** (math.floor(math.log10(abs(value))) - 5)) if value else 0.001
def read_storage_year(csv_path, pdf_path, year, load_record, load_values, state):
    """Interpret native hourly storage flows without the grid-only PR formula."""
    if type(year) is not int or not 1969 <= year <= 2068 or load_record["year"] != year:
        raise ValueError("Storage output and prepared load require the same supported year")
    units = result_units(csv_path, strict=True)
    required = {"E_User", "EFrGrid", "E_Grid", "SOC_Beg", "SOC_End", "EBatCh", "EBatDis", "CL_Chrg", "CL_InvB"}
    if required - set(units):
        raise ValueError("Storage output is missing required load/SOC/AC power columns: " + ", ".join(sorted(required - set(units))))
    if any(not unit for unit in units.values()): raise ValueError("Every actual storage result column requires an explicit unit")
    power_names = {"E_User", "EFrGrid", "E_Grid", "E_Miss", "E_Avail", "EBatCh", "EBatDis", "CL_Chrg", "CL_InvB", "CL_Over", "EUnused", "EArray", "IL_Pmax"}
    power_scales = {name: POWER_TO_KW.get(units[name]) for name in power_names & set(units)}
    soc_scales = {name: {"ratio": 1, "%": 0.01}.get(units[name]) for name in ("SOC_Beg", "SOC_End")}
    if any(scale is None for scale in (*power_scales.values(), *soc_scales.values())):
        raise ValueError("Storage power columns require W/kW and SOC requires ratio or percent")
    balance_names = {"E_Avail", "EFrGrid", "EBatDis", "E_User", "E_Miss", "E_Grid", "EBatCh", "CL_Chrg", "CL_InvB", "CL_Over", "EUnused"}
    missing_balance = sorted(balance_names - set(power_scales))
    totals = dict.fromkeys(power_scales, 0.0)
    soc_min, soc_max = dict.fromkeys(soc_scales, math.inf), dict.fromkeys(soc_scales, -math.inf)
    first = expected = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    indices = initial_soc = None
    count = 0
    load_error = charge_max = discharge_max = balance_max = balance_bound_max = 0.0
    encoding_error = load_record.get("native_encoding", {}).get("encoding_error_bound_kw", 0.0)
    for headers, row in iter_result_csv(csv_path, strict=True):
        if indices is None:
            if len(set(headers)) != len(headers) or set(headers[1:]) != set(units):
                raise ValueError("Storage output requires unique columns matching its units row")
            indices = {name: headers.index(name) for name in units}
        if row[0] != expected or expected >= end or count >= len(load_values):
            raise ValueError("Storage annual hourly coverage differs from the declared year/load")
        if any(not math.isfinite(value) for value in row[1:]): raise ValueError("Storage output contains missing or nonfinite numeric data")
        load_stamp, load_power = load_values[count]
        if load_stamp != expected: raise ValueError("Prepared load timestamps differ from the native output")
        power = {name: row[indices[name]] * scale for name, scale in power_scales.items()}
        soc = {name: row[indices[name]] * scale for name, scale in soc_scales.items()}
        if any(not math.isfinite(value) for value in (*power.values(), *soc.values())):
            raise ValueError("Storage unit conversion exceeds finite numeric range")
        difference = abs(power["E_User"] - load_power)
        load_error = max(load_error, difference)
        if difference > encoding_error + _power_error(power["E_User"]): raise ValueError("Native E_User differs from the bound prepared hourly load")
        for name, value in soc.items():
            if not state["soc_min"] - 0.0001 <= value <= state["soc_max"] + 0.0001: raise ValueError("Native SOC is outside the declared bounds")
            soc_min[name], soc_max[name] = min(soc_min[name], value), max(soc_max[name], value)
        if initial_soc is None:
            initial_soc = soc["SOC_Beg"]
            if abs(initial_soc - state["initial_soc"]) > 0.0001: raise ValueError("Native initial SOC differs from the declared initial SOC")
        charge, discharge = power["EBatCh"] + power["CL_Chrg"], power["EBatDis"] - power["CL_InvB"]
        if charge > state["max_charge_ac_kw"] + _power_error(power["EBatCh"]) + _power_error(power["CL_Chrg"]):
            raise ValueError("Native charging AC input exceeds the declared limit")
        if discharge > state["max_discharge_ac_kw"] + _power_error(power["EBatDis"]) + _power_error(power["CL_InvB"]):
            raise ValueError("Native battery-inverter AC output exceeds the declared limit")
        charge_max, discharge_max = max(charge_max, charge), max(discharge_max, discharge)
        if not missing_balance:
            try:
                residual = math.fsum(power[name] for name in ("E_Avail", "EFrGrid", "EBatDis", "E_Miss")) - math.fsum(
                    power[name] for name in ("E_User", "E_Grid", "EBatCh", "CL_Chrg", "CL_InvB", "CL_Over", "EUnused"))
            except OverflowError: raise ValueError("Storage AC balance exceeds finite numeric range") from None
            bound = math.fsum(_power_error(power[name]) for name in balance_names)
            if not math.isfinite(residual) or not math.isfinite(bound): raise ValueError("Storage AC balance exceeds finite numeric range")
            balance_max, balance_bound_max = max(balance_max, abs(residual)), max(balance_bound_max, bound)
            if abs(residual) > bound: raise ValueError("Native storage AC energy-flow residual exceeds its column export precision bound")
        for name, value in power.items(): totals[name] += value
        expected += timedelta(hours=1)
        count += 1
    if expected != end or count != len(load_values): raise ValueError("Storage output does not cover the entire prepared hourly year")
    if any(not math.isfinite(value) for value in totals.values()): raise ValueError("Storage annual totals exceed finite numeric range")
    verify_pdf_header(Path(pdf_path), message="Native storage report is not a PDF")
    names = {"grid_import_kwh": "EFrGrid", "grid_export_kwh": "E_Grid", "load_energy_kwh": "E_User",
             "unmet_load_kwh": "E_Miss", "battery_charge_kwh": "EBatCh", "battery_discharge_kwh": "EBatDis",
             "unused_energy_kwh": "EUnused", "array_energy_kwh": "EArray", "clipping_loss_kwh": "IL_Pmax"}
    flags = ["Complete AC balance was not checked; native output lacks: " + ", ".join(missing_balance)] if missing_balance else []
    metrics = {
        "year": year, "rows": count, "first": str(first), "last": str(end - timedelta(hours=1)),
        "complete_year": True, "step_minutes": 60, "diagnostic_flags": flags,
        **{name: totals.get(column) for name, column in names.items()},
        "missing_metrics": [name for name, column in names.items() if column not in totals],
        "soc_begin_min": soc_min["SOC_Beg"], "soc_begin_max": soc_max["SOC_Beg"],
        "soc_end_min": soc_min["SOC_End"], "soc_end_max": soc_max["SOC_End"], "initial_soc": initial_soc,
        "pr_method": "Not applied: storage/self-consumption E_Grid is exported energy, not grid-only PV yield",
    }
    return {
        "metrics": metrics, "hourly_rows": count, "numeric_columns": headers[1:],
        "storage_output": {
            "units": units, "utc_offset_hours": load_record["utc_offset_hours"], "time_basis": load_record["time_basis"],
            "max_load_error_kw": load_error, "max_charge_ac_kw": charge_max, "max_discharge_ac_kw": discharge_max,
            "soc_export_tolerance_ratio": 0.0001, "power_export_precision": "Six significant figures with a 0.001 kW per-column floor",
            "ac_balance_verified": not missing_balance, "ac_balance_missing_columns": missing_balance,
            "max_ac_balance_residual_kw": balance_max if not missing_balance else None,
            "max_ac_balance_export_bound_kw": balance_bound_max if not missing_balance else None,
        },
    }
def _cell(value):
    if value is None: return "未记录"
    text = json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (dict, list)) else str(value)
    return html.escape(text, quote=False).replace("|", "\\|").replace("\r", "").replace("\n", " / ")
def _table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
                     + ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows])
def _json_block(value):
    return "```json\n" + json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n```"
def _house_evidence(task):
    imported = task.get("native_import") or {}
    verification, reload = imported.get("verification") or {}, imported.get("native_reload") or {}
    evidence = {
        "regions": (task.get("geometry") or {}).get("tables"), "native_geometry_verified": imported.get("native_geometry_verified"),
        "house_mapping": verification.get("house_mapping"), "pv_mapping": verification.get("pv_mapping"),
        "native_reload_verified": imported.get("native_reload_verified"), "native_reload": reload if reload else None,
    }
    if imported.get("method") == "native":
        evidence.update(import_method="native", stored_geometry_verified=imported.get("stored_geometry_verified"),
                        stored_module_layout_verified=imported.get("stored_module_layout_verified"),
                        native_consumption_verified=task.get("native_consumption_verified"))
    return evidence
def _summary(task, handoff, manifest_name, comparison=None, delivery_review_required=False, house_runs=()):
    metrics = task.get("metrics") or {}
    storage = any(key in metrics for key in ("grid_import_kwh", "load_energy_kwh", "battery_charge_kwh", "initial_soc"))
    names = (("grid_import_kwh", "年电网购电", "kWh"), ("grid_export_kwh", "年上网电量", "kWh"), ("load_energy_kwh", "年用户负荷", "kWh"),
             ("unmet_load_kwh", "年未供负荷", "kWh"), ("battery_charge_kwh", "年电池侧充电", "kWh"),
             ("battery_discharge_kwh", "年电池侧放电", "kWh"), ("unused_energy_kwh", "年未用电量", "kWh")) if storage else (
             ("grid_energy_kwh", "年并网电量", "kWh"), ("array_energy_kwh", "年阵列有效电量", "kWh"),
             ("clipping_loss_kwh", "逆变器超额功率损失 IL_Pmax", "kWh"), ("pr_percent", "标准 PR", "%"),
             ("nominal_dc_power_kw", "铭牌直流容量", "kWp"), ("incident_irradiation_kwh_m2", "阵列面年辐照", "kWh/m²"))
    metric_rows = [(label, metrics[key], unit) for key, label, unit in names if key in metrics]
    if storage:
        metric_rows.extend((label, metrics[key], "ratio") for key, label in
                           (("initial_soc", "首小时SOC"), ("soc_begin_min", "小时开始SOC最小值"), ("soc_begin_max", "小时开始SOC最大值"),
                            ("soc_end_min", "小时结束SOC最小值"), ("soc_end_max", "小时结束SOC最大值")) if key in metrics)
    changes = task.get("changes") or []
    warnings = list(task.get("warnings") or []) + list(handoff.get("review_items") or []) + (list(comparison.get("review_items") or []) if comparison else [])
    status = "原生年度任务完成，仍需复核" if task["status"] == "needs_review" else ("原生年度任务完成；交付内容仍需复核" if delivery_review_required else "原生年度任务完成")
    lines = ["# 工程交付摘要", "", "**" + status + "**", "",
             _table(("项目", "记录"), [("工程 / 方案", task["working_project"] + " / " + task["working_variant"]), ("运行年份", task.get("year")),
                    ("运行清单", manifest_name), ("任务状态 / 阶段", task["status"] + " / " + task["stage"]),
                    ("运行清单记录的原生消费验证", "已记录为通过" if task.get("native_consumption_verified") is True else "未记录为通过")]),
             "", "## 实际变更", "",
             _table(("项目", "修改前", "修改后", "原因"),
                    [(row.get("field"), row.get("before"), row.get("after"), row.get("reason", "调用方明确要求")) for row in changes[:6]])
             if changes else "本任务没有记录参数变更；以 inputs.md 中的实际保存输入为准。",
             "", "## 年度结果", "", _table(("指标", "原生结果汇总", "单位"), metric_rows) if metric_rows else "运行清单未提供上述年度指标。"]
    if storage:
        lines.extend(["", "电池充放电按原生电池侧导出量展示，不能直接当作AC侧功率限值；SOC采用0..1分数。"])
        if metrics.get("missing_metrics"): lines.append("未导出的指标保留未记录：" + _cell(metrics["missing_metrics"]) + "。")
    for house in house_runs or ([task] if task.get("operation") == "engineering_house" else []):
        evidence = _house_evidence(house)
        lines.extend(["", "## 住宅保存与重载记录", "", "工程/方案：" + _cell(house["working_project"] + "/" + house["working_variant"]), "",
            _table(("记录项", "本次运行清单记录"), [("PV区域", evidence["regions"]), ("原生保存几何已验证", evidence["native_geometry_verified"]),
                ("住宅物体映射", evidence["house_mapping"]), ("PV表映射", evidence["pv_mapping"]), ("原生GUI重载已验证", evidence["native_reload_verified"])]),
            "", "以上仅转述该次运行记录；未记录的映射或重载项保持未记录。实际证据和重载有效性见 inputs.md。",
            "包内所选原生VC保留其内嵌几何，另附绑定的场景、SFI和原始任务清单；接收方仍需准备合法的组件及天气依赖。"])
    if comparison:
        lines.extend(["", "## 方案比较", "", "比较记录状态：" + _cell(comparison["status"]), ""])
        lines.extend("- " + _cell(item) for item in (comparison.get("summary") or [])[:4])
        order = (("EFrGrid", "电网购电"), ("E_Miss", "未供负荷"), ("EBatCh", "电池侧充电"), ("EBatDis", "电池侧放电"),
                 ("E_Grid", "上网电量"), ("E_User", "用户负荷"), ("EUnused", "未用电量")) if storage else (("E_Grid", "并网电量"),)
        deltas = [(row.get("baseline_label"), row.get("target_label"), label, energy.get("delta_energy_kwh"))
                  for row in comparison.get("metrics") or [] for column, label in order
                  if isinstance(energy := row.get("summary", {}).get(column), dict)]
        if deltas: lines.extend(["", _table(("基准", "候选", "指标", "候选减基准的完整电量差(kWh)"), deltas)])
        lines.extend(["", "各方案的明确变更、关键时段和限制见 comparison.md；逐项数据见 comparison.csv，工程与原生结果已一并收录。"])
    lines.extend(["", "## 交接事项", "", *(("- " + _cell(item)) for item in warnings[:5])])
    if not warnings: lines.append("- 本次任务未记录额外警告。")
    if handoff.get("missing_items"): lines.append("- 输入交接仍有 " + str(len(handoff["missing_items"])) + " 项补件/依据事项，详见 inputs.md。")
    lines.extend(["- 包内含各收录方案的原生 PRJ、选中 VC 和运行清单绑定的结果文件；设备模型、气象、负荷等外部依赖按 inputs.md 另行准备。",
                  "- 打包成功表示成果已整理；工程依据和任务复核状态沿用运行清单。完整变更、警告及输入说明见 inputs.md。", ""])
    return "\n".join(lines)
def _inputs(handoff, task):
    saved, declarations = handoff["saved"], (handoff.get("declarations") or {})
    lines = ["# 工程输入交接", "", "工程：" + _cell(handoff["project"]) + "；方案：" + _cell(handoff["variant"]),
             "", "以下为该次运行记录的保存输入。声明资料单独列示；软件默认值与继承值不自动成为现场或厂家依据。", "", "## 设备、数量与朝向分配", ""]
    rows = [(row["id"], (refs := row.get("references", {})).get("PAN"), refs.get("OND"), (values := row.get("values", {})).get("NModSerie"),
             values.get("NStringCh"), row.get("declared_module_count"), values.get("NInverter"), values.get("NInvMPPT"), values.get("NoOrientation"))
            for row in saved.get("subarrays", [])]
    lines.extend([
        _table(("子阵", "保存组件引用", "保存逆变器引用", "串联组件数", "并联串数", "声明组件数(串联×并联)", "逆变器数", "保存NInvMPPT", "朝向ID"), rows),
        "", "## 保存朝向与跟踪参数", "", _table(("朝向ID", "保存值"), [(row["id"], row.get("values")) for row in saved.get("orientations", [])]),
        "", "## 声明资料", "", _table(("声明项", "调用方提供的值、单位与来源"), declarations.items()) if declarations else "本次未提供额外工程声明。",
        "", "## 项目继承值及原生系统保存值", "", _json_block({"project": saved.get("project"), "system": saved.get("system"), "main_array": saved.get("main_array")}),
        "", "## 站点、天气、负荷与储能", "", _json_block({key: saved.get(key) for key in ("sources", "load", "storage")}),
        "", "## 保存的MPPT与电路对应", "", _json_block(saved.get("grid_circuit")),
        "", "## 未打包的外部依赖", "",
        _table(("类别", "引用", "解析状态", "需要准备或确认"),
               [(row.get("kind"), row.get("name"), row.get("resolution"), row.get("required_action", "在接收方PVsyst环境中准备合法可用的相同依赖"))
                for row in handoff.get("external_dependencies", [])]),
        "", "## 缺件与补件事项", "",
        _table(("项目", "原因", "下一步"), [(row.get("field"), row.get("reason"), row.get("action")) for row in handoff.get("missing_items", [])]),
        "", "## 完整变更记录", "", _json_block(task.get("changes") or []), "", "## 保留的复核事项", "",
        *(("- " + _cell(item)) for item in list(task.get("warnings") or []) + list(handoff.get("review_items") or [])),
    ])
    if task.get("operation") == "engineering_house":
        lines.extend(["", "## 住宅屋面映射、保存与重载证据", "", _json_block(_house_evidence(task)),
            "", "## 随包提供的受控住宅复演资料", "",
            _table(("类别", "目录", "文件名"), [(kind, task["input_assets"][kind]["folder"], task["input_assets"][kind]["name"]) for kind in ("scene", "sfi")]
                + [("原始任务清单", "Results", Path(task["artifacts"]["manifest"]).name)]),
            "", "这些文件是该次运行绑定的原文件。清单中的本机绝对路径作为历史记录保留；在接收方建立工作区时需按包内目录映射，"
            "不据此读取包外路径。商业组件、站点及气象原件不随包分发；缺少这些依赖时无法在本地重新执行仿真。"])
    lines.extend(["", "本交接包汇总已保存的工程文件和运行清单，不包含未保存的 GUI 状态或外部商业组件原件。", ""])
    return "\n".join(lines)
def _delivery_result_path(store, value, extension):
    if not isinstance(value, str): raise ValueError("Run artifact must be a controlled workspace filename or absolute Results path")
    given = Path(value)
    if not given.is_absolute() and given.name != value: raise ValueError("Relative run artifacts must be filenames without directories")
    name = safe_filename(given.name, extension)
    expected = workspace_file(store.workspace, "Results", name, extension)
    if given.is_absolute() and given.resolve() != expected: raise ValueError("Run artifact is outside the controlled Results folder")
    if not expected.is_file(): raise FileNotFoundError(name)
    return expected
def _stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino
def _saved_inputs(saved):
    saved = deepcopy(saved)
    if isinstance(saved, dict):
        for row in saved.get("load", {}).get("objects", []):
            if isinstance(row, dict) and isinstance(row.get("hourly_source"), dict): row["hourly_source"].pop("path_matches_workspace", None)
    return saved
def _saved_inputs_match(current, recorded):
    current, recorded = _saved_inputs(current), _saved_inputs(recorded)
    if isinstance(current, dict) and isinstance(recorded, dict):
        before, after = recorded.get("subarrays"), current.get("subarrays")
        if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
            for old, new in zip(before, after):
                if isinstance(old, dict) and isinstance(new, dict) and old.get("id") == new.get("id") and "inverter_state" not in old:
                    new.pop("inverter_state", None)
    return current == recorded
def _comparison_report(store, comparison_name, load_run):
    safe_filename(comparison_name, ".json")
    path = _delivery_result_path(store, comparison_name, ".json")
    with path.open("rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024: raise ValueError("Comparison report exceeds 32 MiB")
    report = json.loads(data)
    if (not isinstance(report, dict) or report.get("operation") != "engineering_scenario_comparison"
            or report.get("dry_run") is not False or report.get("status") not in ("created", "needs_review")
            or report.get("historical_input_binding_verified") is not True
            or not isinstance(report.get("written_artifacts"), list) or set(report["written_artifacts"]) != {"csv", "markdown", "json"} or len(report["written_artifacts"]) != 3
            or not isinstance(report.get("scenarios"), list) or not 2 <= len(report["scenarios"]) <= 3
            or any(not isinstance(report.get(key), list) for key in ("summary", "metrics", "review_items"))
            or any(not isinstance(item, str) for item in report["summary"])
            or any(not isinstance(item, dict) or not isinstance(item.get("summary"), dict) for item in report["metrics"])
            or not isinstance(report.get("report_files"), dict) or set(report["report_files"]) != {"csv", "markdown"}):
        raise ValueError("Require a written comparison with complete reports and bound native-run inputs")
    files = {key: _delivery_result_path(store, report["artifacts"][key], extension)
             for key, extension in (("json", ".json"), ("csv", ".csv"), ("markdown", ".md"))}
    if files["json"] != path or files["csv"] != path.with_suffix(".csv") or files["markdown"] != path.with_suffix(".md"):
        raise ValueError("Comparison artifacts must identify this report and its matching CSV/Markdown files")
    if any(fingerprint(files[key]) != report["report_files"][key] for key in ("csv", "markdown")):
        raise ValueError("Comparison CSV or Markdown changed after it was recorded")
    bindings, manifests = [], set()
    for scenario in report["scenarios"]:
        if not isinstance(scenario, dict): raise ValueError("Comparison scenario must contain a native-run binding")
        manifest = safe_filename(scenario["run_manifest_name"], ".json")
        if manifest.casefold() in manifests: raise ValueError("Comparison scenarios must refer to distinct native runs")
        manifests.add(manifest.casefold())
        project, variant = project_name(scenario["project"]), variant_id(scenario["variant"])
        csv_name = safe_filename(scenario["csv_name"], ".csv")
        task = load_run(store, manifest, project=project, variant=variant, csv_name=csv_name)
        if not isinstance(scenario.get("input_handoff"), dict) or not _saved_inputs_match(scenario["input_handoff"].get("saved"), task["input_handoff"].get("saved")):
            raise ValueError("Comparison scenario inputs differ from its recorded native run")
        bindings.append({"run_manifest_name": manifest, "project": project, "variant": variant,
                         "csv_name": csv_name, "label": scenario.get("label", project + "/" + variant), "task": task})
    return report, files, data, bindings
def export_engineering_delivery(store, *, manifest_name, output_name, dry_run=True, comparison_name=""):
    """Export a bound native run and an optional recorded comparison without changing their review states."""
    result = {"status": "blocked", "dry_run": dry_run, "issues": [], "included_files": [], "external_dependencies": [],
              "native_consumption_verified": False, "task_native_consumption_verified": False, "artifact": None}
    writing = False
    with store.lock:
        try:
            if type(dry_run) is not bool: raise ValueError("Use a strict dry_run boolean")
            if not isinstance(comparison_name, str): raise ValueError("comparison_name must be an optional Results JSON filename")
            safe_filename(manifest_name, ".json")
            output = workspace_file(store.workspace, "Results", output_name, ".zip")
            if output.name.casefold() != output_name.casefold(): raise ValueError("Delivery filename resolves to a different file")
            if output.exists(): raise FileExistsError("Delivery already exists; use a new output_name")
            comparison, comparison_files, comparison_bytes = None, {}, None
            if comparison_name:
                comparison, comparison_files, comparison_bytes, bindings = _comparison_report(store, comparison_name, load_engineering_run)
                primary = next((item for item in bindings if item["run_manifest_name"].casefold() == manifest_name.casefold()), None)
                if primary is None: raise ValueError("The explicit comparison must include the requested primary native-run manifest")
                task = primary["task"]
            else:
                task = load_engineering_run(store, manifest_name)
                bindings = [{"run_manifest_name": manifest_name, "project": task["working_project"], "variant": task["working_variant"],
                             "csv_name": Path(task["artifacts"]["csv"]).name, "label": task["working_project"] + "/" + task["working_variant"], "task": task}]
            sources, payloads, external_dependencies = {}, {}, []
            for binding in bindings:
                run = binding["task"]
                project, variant = project_name(run["working_project"]), variant_id(run["working_variant"])
                if run.get("status") not in ("completed", "needs_review") or run.get("stage") != "completed":
                    raise ValueError("Delivery requires completed engineering runs; their review states are retained")
                handoff = run["input_handoff"]
                if not isinstance(handoff, dict) or handoff.get("project", "").casefold() != project.casefold() or handoff.get("variant", "").upper() != variant:
                    raise ValueError("Runtime input handoff does not identify this working project and variant")
                current = build_project_handoff(store, project=project, variant=variant, declarations=handoff.get("declarations"))
                if current["status"] == "blocked" or not _saved_inputs_match(current["saved"], handoff.get("saved")):
                    raise ValueError("Saved inputs differ from the run-time handoff; no mismatched delivery was created")
                files = [("Projects", store._path(project)), ("Projects", store._path(project, variant)),
                         *(("Results", _delivery_result_path(store, run["artifacts"][kind], "." + kind)) for kind in run_output_kinds(run))]
                if run.get("operation") == "engineering_house":
                    files.append(("Results", _delivery_result_path(store, run["artifacts"]["manifest"], ".json")))
                    files.extend((run["input_assets"][kind]["folder"], _asset_path(store, run["input_assets"][kind])) for kind in ("scene", "sfi"))
                for folder, path in files:
                    name = folder + "/" + path.name
                    if (existing := next((key for key in sources if key.casefold() == name.casefold()), None)) is not None:
                        if sources[existing] != path: raise ValueError("Different scenario files have conflicting delivery names")
                        continue
                    sources[name] = path
                    if folder == "Projects": payloads[name] = store._read(path)
                    elif run.get("operation") == "engineering_house" and name.casefold() == ("Results/" + binding["run_manifest_name"]).casefold():
                        raw_manifest = path.read_bytes()
                        if json.loads(raw_manifest) != run: raise ValueError("House task manifest changed before it was packaged")
                        payloads[name] = raw_manifest
                for dependency in handoff.get("external_dependencies", []):
                    if dependency not in external_dependencies: external_dependencies.append(dependency)
            if comparison:
                sources.update({"comparison.json": comparison_files["json"], "comparison.csv": comparison_files["csv"], "comparison.md": comparison_files["markdown"]})
                payloads["comparison.json"] = comparison_bytes
            before = {name: _stamp(path) for name, path in sources.items()}
            handoff = task["input_handoff"]
            review = (any(item["task"]["status"] == "needs_review" or item["task"]["input_handoff"].get("status") == "needs_review" for item in bindings)
                      or bool(comparison and (comparison["status"] == "needs_review" or comparison.get("review_items"))))
            summary = _summary(task, handoff, manifest_name, comparison, review,
                               house_runs=[b["task"] for b in bindings if b["task"].get("operation") == "engineering_house"])
            inputs = _inputs(handoff, task)
            documented = {manifest_name.casefold()}
            for binding in bindings:
                if binding["run_manifest_name"].casefold() not in documented:
                    inputs += "\n---\n\n" + _inputs(binding["task"]["input_handoff"], binding["task"])
                    documented.add(binding["run_manifest_name"].casefold())
            result.update(
                status="ready", task_status=task["status"], task_stage=task["stage"],
                task_native_consumption_verified=task.get("native_consumption_verified") is True,
                included_files=["summary.md", "inputs.md", *sources], external_dependencies=external_dependencies,
                review_required=review, comparison_status=comparison["status"] if comparison else None,
                comparison_name=comparison_name,
                scenarios=[{k: v for k, v in b.items() if k != "task"} | {"task_status": b["task"]["status"]} for b in bindings],
                artifact={"zip": str(output)}, summary_markdown=summary, inputs_markdown=inputs, source_manifest=manifest_name,
                scope="Recorded engineering runs and an optional bound comparison; selected variants retain embedded geometry. "
                      "House runs also include their bound scene, SFI and original manifest; model, weather and load files are external dependencies",
            )
            if dry_run: return result
            output.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                writing = True
                archive.writestr("summary.md", summary)
                archive.writestr("inputs.md", inputs)
                for name, path in sources.items():
                    if name in payloads: archive.writestr(name, payloads[name])
                    else: archive.write(path, name)
            if (any(_stamp(path) != before[name] for name, path in sources.items())
                    or any(path.read_bytes() != payloads[name] for name, path in sources.items() if name in payloads)):
                raise ValueError("Delivery inputs changed while packaging; retained ZIP is not accepted for delivery")
            result["status"] = "exported"
            return result
        except (ValueError, OSError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile) as exc:
            result.update(status="failed" if writing else "blocked", issues=[str(exc)])
            if writing:
                result["retained_artifact"] = result["artifact"]
                result["recovery"] = "保留本次ZIP供查看；解决输入变化或写入问题后，使用新的output_name重新导出。"
            return result

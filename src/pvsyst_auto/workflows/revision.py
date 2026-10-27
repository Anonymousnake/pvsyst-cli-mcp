"""Revise an existing native design and bind its annual outputs to the run inputs."""
from __future__ import annotations

import csv
import calendar
import json
import math
from pathlib import Path
import subprocess

from pvsyst_auto.native.cli import PVsystError, VAR_GROUPS, build_sfi, iter_result_csv, result_units, validate_report_pages
from pvsyst_auto.components.editor import objects
from pvsyst_auto.project.paths import project_name, safe_filename, variant_id, workspace_file, write_json
from pvsyst_auto.results import annual_summary, fingerprint
from pvsyst_auto.project.tracking import create_tracking_variant, _view
from pvsyst_auto.weather.preparation import inspect_met_calendar


ASSET_FOLDERS = {"Sites": (".SIT",), "Meteo": (".MET",), "ComposPV/PVmodules": (".PAN",),
                 "ComposPV/Inverters": (".OND",), "ComposPV/Batteries": (".BTR",),
                 "UserData": (".csv", ".pvc2"), "UserHourlyParams": (".csv",),
                 "Models": (".SFI",), "Results": (".json",)}
COMPONENT_KINDS = {"ComposPV/PVmodules": "PAN", "ComposPV/Inverters": "OND", "ComposPV/Batteries": "BTR"}


def _asset_extension(folder, name):
    extensions = ASSET_FOLDERS.get(folder)
    if extensions is None:
        raise ValueError("Unsupported run input location")
    if not isinstance(name, str):
        raise ValueError("Run input asset name must be a filename")
    extension = Path(name).suffix
    if extension.casefold() not in {item.casefold() for item in extensions}:
        raise ValueError(f"Unsupported {folder} asset extension: {name}")
    return next(item for item in extensions if item.casefold() == extension.casefold())


def _asset_path(store, asset):
    folder, name, library = asset["folder"], asset["name"], asset["library"]
    if folder not in ASSET_FOLDERS or library not in ("workspace", "builtin"):
        raise ValueError("Unsupported run input location")
    extension = _asset_extension(folder, name)
    safe_filename(name, extension)
    if folder in COMPONENT_KINDS:
        kind = COMPONENT_KINDS[folder]
        if library == "builtin" and store.components._path(kind, name).exists():
            raise ValueError("A workspace component now shadows the run input: " + name)
        return store.components._path(kind, name, library)
    if library != "workspace":
        raise ValueError("Non-component run inputs must be workspace assets")
    return workspace_file(store.workspace, folder, name, extension)


def _check_assets(store, record):
    assets = record.get("input_assets")
    if not isinstance(assets, dict) or not {"site", "meteo"} <= set(assets):
        raise ValueError("Run input asset record is incomplete")
    for asset in assets.values():
        if fingerprint(_asset_path(store, asset)) != asset["sha256"]:
            raise ValueError("A bound run input has changed: " + asset["name"])
    for reference in record.get("database_references", []):
        kind, name = reference["kind"], reference["name"]
        if kind not in ("PAN", "OND", "BTR"):
            raise ValueError("Unsupported database reference")
        safe_filename(name, "." + kind)
        libraries = ("workspace", "builtin") if store.components.builtin else ("workspace",)
        if any(store.components._path(kind, name, library).exists() for library in libraries):
            raise ValueError("A loose component now shadows an uninspected database reference: " + name)


def _result_path(store, record, kind):
    extension = {"csv": ".csv", "pdf": ".pdf"}[kind]
    value = record["artifacts"][kind]
    if not isinstance(value, str):
        raise ValueError("Run artifact path must be a string")
    path = workspace_file(store.workspace, "Results", Path(value).name, extension)
    if Path(value).resolve() != path:
        raise ValueError("Run artifact is outside the workspace Results directory")
    return path


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
    attempt = runs[-1]
    if (attempt.get("status") != "returned" or type(attempt.get("number")) is not int
            or attempt["number"] != len(runs) or not isinstance(attempt.get("native"), dict)
            or not isinstance(attempt["native"].get("warnings"), list)
            or not isinstance(record.get("output_guards"), dict)
            or set(record["output_guards"]) != {"csv", "pdf"}
            or record.get("result_files") != record["output_guards"]):
        raise ValueError("House run lacks a returned native attempt and matching output guards")
    stem = Path(project).stem
    expected = {"manifest": workspace_file(store.workspace, "Results", stem + "_house_task.json", ".json"),
                "scene": workspace_file(store.workspace, "UserData", stem + "_house.pvc2", ".pvc2"),
                "sfi": workspace_file(store.workspace, "Models", stem + "_house.SFI", ".SFI"),
                "native_variant": store._path(project, variant)}
    if expected["manifest"] != path:
        raise ValueError("House manifest name does not identify its project task")
    for kind, target in expected.items():
        if Path(record["artifacts"][kind]).resolve() != target:
            raise ValueError("House artifact does not match its controlled project path: " + kind)
    for kind in ("csv", "pdf"):
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
    if (not isinstance(imported, dict) or imported.get("status") != "imported"
            or imported.get("native_geometry_verified") is not True
            or not isinstance(imported.get("verification"), dict)
            or variant_id(imported["variant"]) != variant
            or Path(imported["file"]).resolve() != expected["native_variant"]):
        raise ValueError("House checkpoint lacks a verified native save for this variant")
    grouped = bool(plan["source"].get("orientations")) or len(handoff["saved"].get("orientations", [])) > 1
    if grouped:
        reload = imported.get("native_reload")
        if (imported.get("native_reload_verified") is not True or not isinstance(reload, dict)
                or variant_id(reload["source_variant"]) != "VC0" or variant_id(reload["saved_variant"]) != variant
                or not isinstance(reload.get("verification"), dict) or not reload["verification"]
                or not isinstance(reload.get("native_validity"), dict)
                or reload["native_validity"].get("valid") is not True):
            raise ValueError("Grouped house run lacks verified native reload evidence")
    expected_rows = 8784 if calendar.isleap(year) else 8760
    if (record.get("hourly_rows") != expected_rows or record["metrics"].get("rows") != expected_rows
            or record["metrics"].get("year") != year or record["metrics"].get("complete_year") is not True
            or record["metrics"].get("sha256") != record["result_files"]["csv"]):
        raise ValueError("House annual result evidence is incomplete or differs from the recorded CSV")
    expected_context = {"Project": project, "Simulation variant": selected,
                        "Geographical Site": plan["site"], "Weather data": plan["met"]}
    context = record.get("native_context")
    if (not isinstance(context, dict) or set(context) != set(expected_context)
            or any(not isinstance(context[key], str) or context[key].casefold() != value.casefold()
                   for key, value in expected_context.items())):
        raise ValueError("House native context does not identify its bound project, variant and weather")
    if not isinstance(record.get("changes"), list) or any(not isinstance(item, dict) for item in record["changes"]):
        raise ValueError("House engineering changes must be a recorded change list")


def load_engineering_run(store, manifest_name, *, project=None, variant=None, csv_name=None):
    """Read a completed run binding, refusing changed or misidentified inputs/outputs."""
    with store.lock:
        path = workspace_file(store.workspace, "Results", manifest_name, ".json")
        with path.open("rb") as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("Engineering run manifest exceeds 32 MiB")
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
            if record.get("operation") == "engineering_house":
                _check_house_run(store, record, path, actual_project, actual_variant)
            _check_assets(store, record)
            for kind in ("csv", "pdf"):
                target = _result_path(store, record, kind)
                if kind == "csv" and csv_name is not None and target.name.casefold() != csv_name.casefold():
                    raise ValueError("Run binding identifies a different result CSV")
                if fingerprint(target) != record["result_files"][kind]:
                    raise ValueError("A bound native output has changed: " + target.name)
            manifest_path = workspace_file(store.workspace, "Results", Path(record["artifacts"]["manifest"]).name, ".json")
            if manifest_path != path or Path(record["artifacts"]["manifest"]).resolve() != path:
                raise ValueError("Run manifest identifies a different manifest file")
            if path.read_bytes() != raw:
                raise ValueError("Run binding changed while it was read")
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("Incomplete engineering run binding") from exc
        return record


def _context(path, project, variant, site, meteo):
    expected = {"Project": project, "Simulation variant": Path(project).stem + "." + variant,
                "Geographical Site": site, "Weather data": meteo}
    context = {}
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        for row in csv.reader(stream, delimiter=";"):
            if row and row[0].strip().lower() == "date":
                break
            if len(row) > 1 and row[0].strip() in expected:
                key = row[0].strip()
                if key in context:
                    raise ValueError("Duplicate native result context: " + key)
                context[key] = row[1].strip()
    if any(context.get(key, "").casefold() != value.casefold() for key, value in expected.items()):
        raise ValueError("Native output identifies a different project/variant/site/weather")
    return context


def run_engineering_task(cli, store, *, source_project, source_variant, new_project, new_variant,
                         objective, year, nominal_dc_power_kw, power_source, changes,
                         declarations=None, report_pages=None, dry_run=True,
                         load_manifest_name="", availability=None):
    """One isolated revision and one native annual execution; no automatic retry."""
    from pvsyst_auto.engineering.handoff import build_project_handoff

    record = {"operation": "engineering_revision", "schema_version": 1, "status": "blocked",
              "stage": "preflight", "dry_run": dry_run, "native_consumption_verified": False,
              "issues": [], "warnings": [], "changes": [], "metrics": None, "artifacts": {}}
    with store.lock:
        try:
            if type(dry_run) is not bool:
                raise ValueError("Use a strict boolean dry_run")
            source_project, new_project = project_name(source_project), project_name(new_project)
            source_variant, new_variant = variant_id(source_variant), variant_id(new_variant)
            if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
                raise ValueError("State the engineering objective in 1..4000 characters")
            if not isinstance(power_source, str) or not power_source.strip() or len(power_source) > 4000:
                raise ValueError("Declare the source of the nominal DC power")
            if type(year) is not int or not 1969 <= year <= 2068:
                raise ValueError("Annual year must be an integer in 1969..2068")
            if (type(nominal_dc_power_kw) not in (int, float)
                    or not math.isfinite(nominal_dc_power_kw) or nominal_dc_power_kw <= 0):
                raise ValueError("Declare a positive finite nominal DC power in kWp")
            if not isinstance(changes, dict) or set(changes) - {"tilt", "azimuth", "losses", "tracking", "storage"}:
                raise ValueError("Use supported explicit orientation, loss or tracking changes")
            if report_pages is not None:
                validate_report_pages(report_pages)
            original = store.inspect_project(source_project)
            if source_variant not in original["variants"]:
                raise ValueError("Source variant is missing")
            if new_variant in original["variants"]:
                raise ValueError("Choose a new variant absent from the source project")
            if any(store._belongs_to_project(p, new_project) for p in store._root().iterdir()):
                raise FileExistsError("Choose a new isolated project name")
            source_handoff = build_project_handoff(store, project=source_project, variant=source_variant,
                                                   declarations=declarations)
            if source_handoff["status"] == "blocked":
                raise ValueError("Source handoff is blocked: " + str(source_handoff["issues"]))
            parameters = store.inspect_parameters(source_project, source_variant)
            structure = store.validate_structure(source_project, source_variant)
            if (structure["valid"] is not True or structure["system_type"] != "Grid"
                    or len(parameters["subarrays"]) != 1 or len(parameters["orientations"]) != 1):
                raise ValueError("Revision supports one completely checked Grid subarray and orientation")
            data = store._read(store._path(source_project, source_variant))
            _, lines = store._text(data)
            store._require_no_geometry(lines)
            blocks = objects(_view(lines))
            storage_mode = any(block.object_type == "pvGridStorage" for block in blocks)
            if not storage_mode and (any(block.object_type == "pvLoad" for block in blocks)
                                     or load_manifest_name or availability is not None or "storage" in changes):
                raise ValueError("Load/profile declarations require an existing native AC storage system")
            root = next(block for block in blocks if not block.path)
            if not storage_mode and root.one("PRCorrTemp")[1] != "False":
                raise ValueError("Standard annual grid PR requires PRCorrTemp=False")
            array = parameters["subarrays"][0]
            orientation = parameters["orientations"][0]
            tracking_mode = orientation["values"].get("FieldType") == "UnlimTracking"
            storage_preparation = None
            if storage_mode:
                from pvsyst_auto.workflows.storage import prepare_storage_revision
                if set(changes) != {"storage"} or orientation["values"].get("FieldType") != "FixedPlane":
                    raise ValueError("Existing AC storage revisions accept explicit storage updates only on a fixed plane")
                storage_preparation = prepare_storage_revision(store, source_project, source_variant,
                    new_variant, changes["storage"], load_manifest_name, year, availability=availability)
                if storage_preparation["status"] != "ready":
                    raise ValueError("Storage preparation: " + str(storage_preparation["issues"]))
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
                if set(changes) != {"tracking"}:
                    raise ValueError("UnlimTracking revisions require explicit tracking controls only")
                preview = create_tracking_variant(store, project=source_project, source_variant=source_variant,
                                                   new_variant=new_variant, tracking=changes["tracking"], dry_run=True)
                if preview["status"] != "ready" or preview.get("field_type") != "UnlimTracking":
                    raise ValueError("Tracking revision is outside the verified template: " + str(preview["issues"]))
                record["changes"] = [{"field": "orientations." + field,
                    "before": orientation["values"].get(field), "after": value}
                    for field, value in preview["tracking"].items()
                    if orientation["values"].get(field) != value]
            else:
                if orientation["values"].get("FieldType") != "FixedPlane" or "tracking" in changes:
                    raise ValueError("Require a fixed plane or the supported existing UnlimTracking template")
                angles = {native: changes[key] for key, native in (("tilt", "FieldTilt"), ("azimuth", "FieldAzim"))
                          if key in changes}
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
                    before = ({key: owner.get(key) for key in ("PQualityModU", "PQualityModI")}
                              if field == "ModuleQualityLoss" else owner.get(field))
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
                if len(component_refs[kind]) != 1:
                    raise ValueError("Require one explicit " + kind + " reference")
                name = component_refs[kind][0]
                libraries = ("workspace", "builtin") if store.components.builtin else ("workspace",)
                library = next((item for item in libraries if store.components._path(kind, name, item).is_file()), None)
                if library is None:
                    record["database_references"].append({"kind": kind, "name": name,
                                                          "resolution": "native_database_uninspected"})
                else:
                    asset = {"folder": folder, "name": name, "library": library}
                    asset["sha256"] = fingerprint(_asset_path(store, asset))
                    assets[kind] = asset
            calendar = inspect_met_calendar(_asset_path(store, assets["meteo"]), year)
            if calendar["issues"]:
                raise ValueError("Weather calendar: " + "; ".join(calendar["issues"]))
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
            if any(path.exists() for path in paths.values()):
                raise FileExistsError("An engineering output already exists; choose a new project name")
            for option in ("input-sfi-file", "start-date", "end-date", "time-step", "site", "input-met-file"):
                cli._require("run-simulation", option)
            license_state = cli.license_info()
            if license_state.get("status") == "TRIAL" and license_state.get("remaining_executions") == 0:
                raise ValueError("PVsystCLI reports no remaining trial executions")
            version = cli.version()
            if tracking_mode and version != "PVsystCLI 8.1.6 (rev. 49278)":
                raise ValueError("Tracking revision requires the verified native8.1.6rev49278 executable")
            record.update(status="ready", source_project=source_project, source_variant=source_variant,
                          working_project=new_project, working_variant=new_variant, year=year,
                          objective=objective, nominal_dc_power_kw=nominal_dc_power_kw, power_source=power_source,
                          requested_changes=changes, source_handoff=source_handoff,
                          source_files=original["files"], configuration_preview=preview,
                          artifacts={kind: str(path) for kind, path in paths.items()},
                          cli_version=version, license=license_state, report_pages=report_pages)
            if storage_mode:
                record.update(load_manifest_name=load_manifest_name, availability=availability)
            record["warnings"].append("Nameplate/source are declared; equipment limits, fitted models and site climate are not certified.")
            if record["database_references"]:
                record["warnings"].append("Inherited native database model contents are uninspected; engineering review remains required.")
        except (ValueError, OSError, KeyError, TypeError, PVsystError, subprocess.TimeoutExpired) as exc:
            record["issues"].append(str(exc))
            return record
        if dry_run:
            return record

        def save(stage=None, first=False):
            if stage:
                record.update(status="running", stage=stage)
            write_json(paths["manifest"], record, first=first)

        manifest_owned = False
        try:
            save("clone", first=True)
            manifest_owned = True
            if store.inspect_project(source_project)["files"] != original["files"]:
                raise ValueError("Source changed after preflight")
            _check_assets(store, record)
            store.clone_project(source_project, new_project)
            save("revision")
            if storage_mode:
                if changes["storage"] or availability is not None:
                    from pvsyst_auto.energy.storage import create_grid_storage_variant
                    edited = create_grid_storage_variant(store, project=new_project, source_variant=source_variant,
                        new_variant=new_variant, updates=changes["storage"], availability=availability, dry_run=False)
                    if edited["status"] != "created":
                        raise ValueError("Storage revision failed: " + str(edited["issues"]))
                else:
                    edited = store.clone(new_project, source_variant, new_variant)
            elif tracking_mode:
                edited = create_tracking_variant(store, project=new_project, source_variant=source_variant,
                    new_variant=new_variant, tracking=changes["tracking"], dry_run=False)
                if edited["status"] != "created":
                    raise ValueError("Tracking revision failed: " + str(edited["issues"]))
            else:
                cloned = store.clone(new_project, source_variant, new_variant)
                edited = (store.update_parameters(new_project, new_variant, cloned["sha256"], array["id"], orientation["id"],
                    orientation_updates=angles or None, subarray_updates=subarray_losses or None,
                    main_array_updates=main_losses or None) if angles or losses else cloned)
            record["revision"] = edited
            if store.validate_structure(new_project, new_variant)["valid"] is not True:
                raise ValueError("Revised design failed native structure checks")
            record["working_files"] = store.inspect_project(new_project)["files"]
            record["input_handoff"] = build_project_handoff(store, project=new_project, variant=new_variant,
                                                           declarations=declarations)
            if record["input_handoff"]["status"] == "blocked":
                raise ValueError("Revised input handoff is blocked")
            save("simulation")
            _check_assets(store, record)
            variables = list(VAR_GROUPS["grid_storage" if storage_mode else "all_common"])
            if tracking_mode:
                variables.append("viGlobShd")
            build_sfi(paths["sfi"], variables)
            run = cli.run_simulation(new_project, new_variant, sfi=paths["sfi"], out_csv=paths["csv"],
                report_pdf=paths["pdf"], site=_asset_path(store, assets["site"]), met=_asset_path(store, assets["meteo"]),
                time_step="hour", start_date=f"{year}.01.01", end_date=f"{year}.12.31",
                report_language="en", report_pages=report_pages, log_level=3)
            record["run"] = run
            save("results")
            output_snapshot = fingerprint(paths["csv"])
            record["native_context"] = _context(paths["csv"], new_project, new_variant,
                                                assets["site"]["name"], assets["meteo"]["name"])
            if storage_mode:
                from pvsyst_auto.workflows.storage import read_storage_year
                record.update(read_storage_year(paths["csv"], paths["pdf"], year,
                    storage_preparation["load_record"], storage_preparation["load_values"], storage_preparation["after"]))
                record["metrics"]["sha256"] = output_snapshot
            else:
                record["metrics"] = annual_summary(paths["csv"], year, nominal_dc_power_kw)
            if record["metrics"]["sha256"] != output_snapshot:
                raise ValueError("Native CSV changed between context and annual interpretation")
            count = 0
            for headers, row in iter_result_csv(paths["csv"], strict=True):
                if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in row[1:]):
                    raise ValueError("Native output contains missing or nonfinite numeric data")
                count += 1
            if tracking_mode and result_units(paths["csv"], strict=True).get("GlobShd") != "W/m\u00b2":
                raise ValueError("Tracking output lacks native GlobShd in W/m2")
            with paths["pdf"].open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise ValueError("Native report is not a PDF")
            if store.inspect_project(new_project)["files"] != record["working_files"]:
                raise ValueError("Working design changed during native execution")
            _check_assets(store, record)
            record["result_files"] = {kind: fingerprint(paths[kind]) for kind in ("csv", "pdf")}
            if record["result_files"]["csv"] != record["metrics"]["sha256"]:
                raise ValueError("Native CSV changed during result interpretation")
            record["warnings"].extend(run.get("warnings", []))
            record["warnings"].extend(record["metrics"].get("diagnostic_flags", []))
            record["warnings"].extend(record.get("storage_output", {}).get("review_items", []))
            record.update(stage="completed", native_consumption_verified=True,
                          hourly_rows=count, numeric_columns=headers[1:])
            review = (record["database_references"] or run.get("warnings") or calendar["warnings"]
                      or record["metrics"].get("diagnostic_flags") or record.get("storage_output", {}).get("review_items")
                      or source_handoff.get("review_items")
                      or source_handoff.get("missing_items"))
            record["status"] = "needs_review" if review else "completed"
        except (ValueError, OSError, KeyError, TypeError, PVsystError, subprocess.TimeoutExpired) as exc:
            record.update(status="failed", native_consumption_verified=False, error=cli._redact(str(exc))[-12000:])
        except BaseException as exc:
            # An unanticipated error still leaves a terminal manifest, never "running"; it then propagates.
            record.update(status="failed", native_consumption_verified=False,
                          error=cli._redact(f"{type(exc).__name__}: {exc}")[-12000:])
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
        except (ValueError, OSError, KeyError, TypeError) as exc:
            record.update(status="failed", native_consumption_verified=False, error=str(exc))
        record["recovery"] = "Retain the isolated project and outputs for review; no automatic retry, overwrite, deletion or rollback. Use a new project name for a new execution."
        if manifest_owned:
            save()
        return record

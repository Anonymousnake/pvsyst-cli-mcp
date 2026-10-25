"""Revise an existing native design and bind its annual outputs to the run inputs."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import subprocess

from pvsyst_cli import PVsystError, VAR_GROUPS, build_sfi, iter_result_csv, result_units, validate_report_pages
from pvsyst_component_editor import objects
from pvsyst_paths import project_name, safe_filename, variant_id, workspace_file, write_json
from pvsyst_results import annual_summary, fingerprint
from pvsyst_tracking import create_tracking_variant, _view
from pvsyst_weather_flow import inspect_met_calendar


ASSET_FOLDERS = {"Sites": ".SIT", "Meteo": ".MET", "ComposPV/PVmodules": ".PAN",
                 "ComposPV/Inverters": ".OND"}
COMPONENT_KINDS = {"ComposPV/PVmodules": "PAN", "ComposPV/Inverters": "OND"}


def _asset_path(store, asset):
    folder, name, library = asset["folder"], asset["name"], asset["library"]
    if folder not in ASSET_FOLDERS or library not in ("workspace", "builtin"):
        raise ValueError("Unsupported run input location")
    safe_filename(name, ASSET_FOLDERS[folder])
    if folder in COMPONENT_KINDS:
        kind = COMPONENT_KINDS[folder]
        if library == "builtin" and store.components._path(kind, name).exists():
            raise ValueError("A workspace component now shadows the run input: " + name)
        return store.components._path(kind, name, library)
    if library != "workspace":
        raise ValueError("Site and weather run inputs must be workspace assets")
    return workspace_file(store.workspace, folder, name, ASSET_FOLDERS[folder])


def _check_assets(store, record):
    assets = record.get("input_assets")
    if not isinstance(assets, dict) or not {"site", "meteo"} <= set(assets):
        raise ValueError("Run input asset record is incomplete")
    for asset in assets.values():
        if fingerprint(_asset_path(store, asset)) != asset["sha256"]:
            raise ValueError("A bound run input has changed: " + asset["name"])
    for reference in record.get("database_references", []):
        kind, name = reference["kind"], reference["name"]
        if kind not in ("PAN", "OND"):
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
            if (not isinstance(record, dict) or record.get("operation") != "engineering_revision"
                    or record.get("schema_version") != 1 or record.get("stage") != "completed"
                    or record.get("status") not in ("completed", "needs_review")
                    or record.get("native_consumption_verified") is not True):
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
                         declarations=None, report_pages=None, dry_run=True):
    """One isolated revision and one native annual execution; no automatic retry."""
    from pvsyst_engineering_handoff import build_project_handoff

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
            if not isinstance(changes, dict) or set(changes) - {"tilt", "azimuth", "losses", "tracking"}:
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
            if any(block.object_type in ("pvLoad", "pvGridStorage") for block in blocks):
                raise ValueError("Load/storage revisions use their existing native tools; this task is PV-only")
            root = next(block for block in blocks if not block.path)
            if root.one("PRCorrTemp")[1] != "False":
                raise ValueError("Standard annual grid PR requires PRCorrTemp=False")
            array = parameters["subarrays"][0]
            orientation = parameters["orientations"][0]
            tracking_mode = orientation["values"].get("FieldType") == "UnlimTracking"
            if tracking_mode:
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
            if tracking_mode:
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
            variables = list(VAR_GROUPS["all_common"])
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
            record["warnings"].extend(record["metrics"]["diagnostic_flags"])
            record.update(stage="completed", native_consumption_verified=True,
                          hourly_rows=count, numeric_columns=headers[1:])
            review = (record["database_references"] or run.get("warnings") or calendar["warnings"]
                      or record["metrics"]["diagnostic_flags"] or source_handoff.get("review_items")
                      or source_handoff.get("missing_items"))
            record["status"] = "needs_review" if review else "completed"
        except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc:
            record.update(status="failed", native_consumption_verified=False, error=cli._redact(str(exc))[-12000:])
        try:
            record["source_preserved"] = store.inspect_project(source_project)["files"] == original["files"]
            if not record["source_preserved"]:
                record.update(status="failed", native_consumption_verified=False, error="Source changed during the engineering task")
        except (ValueError, OSError) as exc:
            record.update(status="failed", native_consumption_verified=False, error=str(exc))
        record["recovery"] = "Retain the isolated project and outputs for review; no automatic retry, overwrite, deletion or rollback. Use a new project name for a new execution."
        if manifest_owned:
            save()
        return record

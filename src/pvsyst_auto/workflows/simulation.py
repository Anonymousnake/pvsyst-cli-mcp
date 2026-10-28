"""One bounded template-to-annual-results workflow, reusing guarded file editors."""
from __future__ import annotations

import csv
import math
import subprocess
from pathlib import Path

from pvsyst_auto.native.cli import PVsystError, VAR_GROUPS, build_sfi, validate_report_pages, iter_result_csv, result_units
from pvsyst_auto.components.store import electrical_inventory
from pvsyst_auto.engineering.sizing import plan_capacity
from pvsyst_auto.project.paths import project_path, variant_id, workspace_file, write_json
from pvsyst_auto.components.pan import validation_fields as pan_validation_fields
from pvsyst_auto.weather.preparation import load_weather_manifest, compare_native_weather, inspect_met_calendar
from pvsyst_auto.results import annual_summary, fingerprint
from pvsyst_auto.project.tracking import create_tracking_variant


def simulate_template(cli, store, *, template_project="", variant="", new_project="",
                      pan_name="", ond_name="", site_name="", met_name="",
                      tilt=None, year=None, dry_run=True, capacity=None, weather_manifest_name="", azimuth=None, losses=None,
                      report_pages=None, tracking=None, tracking_variant="",
                      nominal_dc_power_kw=None, tracking_power_source=""):
    # ponytail: one workspace lock; separate workspaces for parallel native runs.
    with store.lock:
        return _simulate_template(cli, store, template_project, variant, new_project,
                                  pan_name, ond_name, site_name, met_name, tilt, year, dry_run, capacity, weather_manifest_name, azimuth, losses,
                                  report_pages=report_pages, tracking=tracking, tracking_variant=tracking_variant,
                                  nominal_dc_power_kw=nominal_dc_power_kw, tracking_power_source=tracking_power_source)


def prepare_template(cli, store, **kwargs):
    """Reuse the same guarded configuration, stopping before SFI/annual execution."""
    with store.lock:
        return _simulate_template(cli, store, **kwargs, prepare_only=True)


def _simulate_template(cli, store, template_project="", variant="", new_project="",
                       pan_name="", ond_name="", site_name="", met_name="", tilt=None, year=None,
                       dry_run=True, capacity=None, weather_manifest_name="", azimuth=None, losses=None,
                       *, report_pages=None, prepare_only=False, preparation_context=None,
                       tracking=None, tracking_variant="", nominal_dc_power_kw=None, tracking_power_source=""):
    tracking_mode = tracking is not None
    request = dict(template_project=template_project, variant=variant, new_project=new_project,
                   pan_name=pan_name, ond_name=ond_name, site_name=site_name, met_name=met_name,
                   tilt=tilt, year=year)
    if weather_manifest_name:
        request["weather_manifest_name"] = weather_manifest_name
    if capacity is not None:
        request["capacity"] = ({k: str(v) if isinstance(v, float) and not math.isfinite(v) else v
                                for k, v in capacity.items()} if isinstance(capacity, dict) else capacity)
    if isinstance(tilt, float) and not math.isfinite(tilt):
        request["tilt"] = str(tilt)
    if azimuth is not None:
        request["azimuth"] = azimuth if not isinstance(azimuth, float) or math.isfinite(azimuth) else str(azimuth)
    if losses is not None:
        request["losses"] = ({key: str(value) if isinstance(value, float) and not math.isfinite(value) else value
                              for key, value in losses.items()} if isinstance(losses, dict) else losses)
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run,
              "request": request, "issues": [], "warnings": [], "transactions": [], "recovery": [],
              "metrics": None, "artifacts": {}, "template_preserved": None}

    if prepare_only:
        result.update(operation="prepare_project", prepared=False, simulated=False,
                      native_consumption_verified=False, preparation_context=preparation_context or {})

    def issue(field, message):
        result["issues"].append({"field": field, "message": str(message)})

    if tracking_mode:
        request.pop("tilt")
        request.update(tracking=({key: str(value) if isinstance(value, float) and not math.isfinite(value) else value
                                  for key, value in tracking.items()} if isinstance(tracking, dict) else tracking),
                       tracking_variant=tracking_variant,
                       nominal_dc_power_kw=(str(nominal_dc_power_kw) if isinstance(nominal_dc_power_kw, float)
                                            and not math.isfinite(nominal_dc_power_kw) else nominal_dc_power_kw),
                       tracking_power_source=tracking_power_source)
        result.update(scope="existing_unlim_tracking_grid", native_consumption_verified=False,
                      component_resolution="native_database_uninspected", review_required=True)
        if (isinstance(nominal_dc_power_kw, bool) or not isinstance(nominal_dc_power_kw, (int, float))
                or not math.isfinite(nominal_dc_power_kw) or nominal_dc_power_kw <= 0):
            issue("nominal_dc_power_kw", "Tracking flow requires explicit positive finite nameplate kWp, cross-checked with native PR after the run")
        if not isinstance(tracking_power_source, str) or not tracking_power_source.strip() or not tracking_power_source.isprintable():
            issue("tracking_power_source", "Declare the nameplate power source; no database model or nameplate is inferred")
        if prepare_only or any(value is not None for value in (tilt, azimuth, capacity, losses)):
            issue("tracking", "Tracking flow preserves source equipment/counts/losses and forbids fixed angles, capacity/loss edits or preparation-only mode")
    elif tracking_variant or nominal_dc_power_kw is not None or tracking_power_source:
        issue("tracking", "Tracking-only variant/power/source inputs require explicit tracking controls")

    for field, value in request.items():
        if value is None or value == "":
            issue(field, "Required input is missing")
    if report_pages is not None:
        request["report_pages"] = report_pages
        try:
            validate_report_pages(report_pages)
        except ValueError as exc:
            issue("report_pages", exc)
    if type(dry_run) is not bool:
        issue("dry_run", "Use a strict boolean")
    if year is not None and (type(year) is not int or not 1969 <= year <= 2068):
        issue("year", "Use an integer year in 1969..2068; native SFI dates contain two-digit years")
    if tilt is not None and (isinstance(tilt, bool) or not isinstance(tilt, (int, float))
                             or not math.isfinite(tilt) or not 0 <= tilt <= 90):
        issue("tilt", "Use a finite numeric tilt in 0..90 degrees")
    if azimuth is not None and (isinstance(azimuth, bool) or not isinstance(azimuth, (int, float))
                                or not math.isfinite(azimuth) or not -180 <= azimuth <= 180):
        issue("azimuth", "Use finite PVsyst azimuth in -180..180 degrees, zero south, west positive")
    subarray_losses, main_losses = {}, {}
    if losses is not None:
        allowed_losses = {"LIDLoss", "PMismMPP", "ModuleQualityLoss", "UFactor"}
        if not isinstance(losses, dict):
            issue("losses", "Use an explicit supported loss object")
        else:
            for key, value in losses.items():
                if (key not in allowed_losses or isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value)):
                    issue("losses." + key, "Require a supported finite numeric loss parameter")
                elif key == "UFactor":
                    main_losses[key] = value
                else:
                    subarray_losses[key] = value
    if result["issues"]:
        return result
    angles = {"FieldTilt": tilt}
    if azimuth is not None:
        angles["FieldAzim"] = azimuth

    assets = {}
    components = {}

    def require_database_references():
        for kind, name in (("PAN", pan_name), ("OND", ond_name)):
            libraries = ("workspace", "builtin") if store.components.builtin else ("workspace",)
            if any(store.components._path(kind, name, library).exists() for library in libraries):
                raise ValueError("Tracking database-reference profile blocks a loose model/shadow: " + name)

    original = parameters = structure = None
    paths = {}
    try:
        project_path(cli.workspace, new_project)
        variant = variant_id(variant)
        original = store.inspect_project(template_project)
        if variant not in original["variants"]:
            raise ValueError(f"Template variant {variant} is missing")
        if any(store._belongs_to_project(p, new_project) for p in store._root().iterdir()):
            raise FileExistsError("Working project already exists; choose a new project filename")
        parameters = store.inspect_parameters(template_project, variant)
        structure = store.validate_structure(template_project, variant)
        if (structure["valid"] is not True or structure["system_type"] != "Grid"
                or len(parameters["subarrays"]) != 1 or len(parameters["orientations"]) != 1):
            raise ValueError("Require a completely checked single-subarray, single-plane grid template: "
                             + "; ".join(structure["issues"] + structure["unchecked_checks"]))
        data = store._read(store._path(template_project, variant))
        _, lines = store._text(data)
        child = next(i for i in range(1, len(lines)) if lines[i].strip().startswith("PVObject_"))
        corrected = store._field_indices(lines, (0, child), "PRCorrTemp")
        if len(corrected) != 1 or lines[corrected[0]].split("=", 1)[1].strip() != "False":
            raise ValueError("Template must explicitly declare PRCorrTemp=False for standard annual PR")
        array_id = parameters["subarrays"][0]["id"]
        orientation_id = parameters["orientations"][0]["id"]
        if tracking_mode:
            tracking_variant = variant_id(tracking_variant)
            preview = create_tracking_variant(store, project=template_project, source_variant=variant,
                                              new_variant=tracking_variant, tracking=tracking, dry_run=True)
            if preview["status"] != "ready" or preview.get("field_type") != "UnlimTracking":
                raise ValueError("Require the existing verified UnlimTracking template profile: " + "; ".join(preview["issues"]))
            references = store.inspect(template_project, variant)["subarrays"][0]["references"]
            if any(len(references[kind]) != 1 or references[kind][0].casefold() != name.casefold()
                   for kind, name in (("PAN", pan_name), ("OND", ond_name))):
                raise ValueError("Tracking flow must retain the exact source PAN/OND references")
            require_database_references()
            result["inherited_component_references"] = references
            effective_angles = preview["tracking"]
        else:
            preview = store.update_parameters(template_project, variant, parameters["sha256"],
                                              array_id, orientation_id, orientation_updates=angles,
                                              subarray_updates=subarray_losses or None,
                                              main_array_updates=main_losses or None, dry_run=True)
            effective_angles = angles
        result["configuration_preview"] = preview
        result["configuration_summary"] = {
            "orientation": {**parameters["orientations"][0]["values"], **effective_angles},
            "requested_losses": losses or {},
            "template_subarray_fields": {key: value for key, value in parameters["subarrays"][0]["values"].items()
                                         if key in ("LIDLoss", "PMismMPP", "PQualityModU", "PQualityModI", "PQualityModP", "Flags")},
            "template_main_array_fields": parameters.get("main_array", {}).get("values", {}),
            "scope": "Unspecified fields retain template settings and native default modes; no installation/loss fitting"}
        result["template_files"] = original["files"]
        result["template_preserved"] = True
        result["structure"] = structure
    except (ValueError, OSError) as exc:
        issue("template", exc)

    for kind, name in (("PAN", pan_name), ("OND", ond_name)):
        if tracking_mode:
            continue
        try:
            library = "workspace"
            path = store.components._path(kind, name, library)
            if not path.is_file() and store.components.builtin:
                library = "builtin"
                path = store.components._path(kind, name, library)
            raw = store.components._read(path)
            info = store.components._inspect(kind, raw)
            store.components._require_valid(info, complete=True)
            components[kind] = info
            components[kind]["electrical"] = electrical_inventory(kind, raw.decode("utf-8-sig"))
            if kind == "PAN":
                components[kind]["physical_fields"] = pan_validation_fields(raw.decode("utf-8-sig"))
            assets[kind] = {"path": str(path), "sha256": info["sha256"], "library": library}
        except (ValueError, OSError) as exc:
            issue("pan_name" if kind == "PAN" else "ond_name", exc)
    for field, folder, name, extension in (("site_name", "Sites", site_name, ".SIT"),
                                           ("met_name", "Meteo", met_name, ".MET")):
        try:
            path = workspace_file(cli.workspace, folder, name, extension)
            if not path.is_file():
                raise FileNotFoundError(f"Required {folder} file is missing: {name}")
            assets[field] = {"path": str(path), "sha256": fingerprint(path)}
            if field == "met_name":
                calendar = inspect_met_calendar(path, year)
                result["meteo_calendar"] = calendar
                for message in calendar["issues"]:
                    issue(field, message)
                result["warnings"].extend(calendar["warnings"])
        except (ValueError, OSError) as exc:
            issue(field, exc)
    weather_report = None
    weather_warnings = []
    if weather_manifest_name:
        try:
            weather_assets, weather_report, weather_warnings = load_weather_manifest(
                cli.workspace, weather_manifest_name, year, site_name, met_name)
            assets.update(weather_assets)
            result["warnings"].extend(weather_warnings)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            issue("weather_manifest_name", exc)
    try:
        prefix = f"{Path(new_project).stem}_{'prepare' if prepare_only else 'annual'}_{year}"
        paths = {"manifest": workspace_file(cli.workspace, "Results", prefix + ".json", ".json")}
        if not prepare_only:
            paths.update(csv=workspace_file(cli.workspace, "Results", prefix + ".csv", ".csv"),
                         pdf=workspace_file(cli.workspace, "Results", prefix + ".pdf", ".pdf"),
                         sfi=workspace_file(cli.workspace, "Models", prefix + ".SFI", ".SFI"))
        if tracking_mode:
            paths.update(project=store._path(new_project), variant=store._path(new_project, tracking_variant))
        if any(path.exists() for path in paths.values()):
            raise FileExistsError("An output/SFI/manifest already exists; choose a new working project")
        result["artifacts"] = {key: str(path) for key, path in paths.items()}
        if tracking_mode:
            result["recovery"] = [{"action": "archive_isolated_tracking_task", "automatic": False,
                "project": new_project, "artifacts": dict(result["artifacts"]),
                "reason": "Original template is retained. Review/archive the new project, all its cloned variants and outputs before retrying under a new name; no automatic rollback/deletion or reuse of stale restore commands."}]
    except (ValueError, OSError) as exc:
        issue("outputs", exc)
    if not result["issues"]:
        try:
            if tracking_mode:
                nominal = nominal_dc_power_kw
            else:
                store._replace(data, {"PAN": pan_name, "OND": ond_name}, array_id)
                pan = components["PAN"]["physical_fields"]
                if float(pan["GRef"][0]) != 1000:
                    raise ValueError("Standard annual PR requires PAN GRef=1000 W/m2")
                count = structure["grid_circuit"]["subarrays"][0]["declared_module_count"]
                if capacity is not None:
                    plan = plan_capacity(store, lines, array_id, pan, components["OND"]["electrical"], capacity)
                    result["capacity_plan"] = plan
                    if plan["status"] != "ready":
                        result["issues"].extend(plan["issues"])
                        result["inputs"] = assets
                        return result
                    count = plan["module_count"]
                nominal = count * float(pan["PNom"][0]) / 1000
            if not math.isfinite(nominal) or nominal <= 0:
                raise ValueError("Template/module combination has invalid nominal DC power")
            result["nominal_dc_power_kw"] = nominal
            if not prepare_only:
                for option in ("input-sfi-file", "start-date", "end-date", "time-step"):
                    cli._require("run-simulation", option)
            license_state = cli.license_info()
            if (not prepare_only and license_state.get("status") == "TRIAL"
                    and license_state.get("remaining_executions") == 0):
                raise ValueError("PVsystCLI reports no remaining trial executions")
            result["license"] = license_state
            result["cli_version"] = cli.version()
            if tracking_mode and result["cli_version"] != "PVsystCLI 8.1.6 (rev. 49278)":
                raise ValueError("Tracking flow requires the verified native8.1.6rev49278 executable")
        except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc:
            issue("simulation", cli._redact(str(exc)))
    result["inputs"] = assets
    result["warnings"].extend([
        ("Template string/inverter counts are retained; voltage/current sizing and optimal design are not verified."
         if capacity is None else "Counts satisfy the supplied design envelope; MPPT topology/mode are retained. Envelope accuracy, native model sizing and energy/cost optimality are not certified."),
        "Requested SIT/MET filenames and simulation year are checked in native output; geographic/time-reference compatibility is not certified.",
        ("Tracking database PAN/OND are inherited references, not loose-file parameter inspection. Nameplate kWp/source are caller-declared and native-PR cross-checked, not inferred from filenames or cached sizing. Native database contents are not snapshotted or engineering-certified; review remains required."
         if tracking_mode else "PAN/OND checks cover static fields, not internal model fitting; native results retain a conservative anomaly screen."),
        ("Tracking recovery retains the isolated clone/new VC/outputs for review and manual archiving; no automatic rollback or resume."
         if tracking_mode else "Recovery is reverse-order whole-file restoration with fresh-state guards; not a cross-process lock or crash-atomic job."),
    ])
    if result["issues"]:
        return result
    result["status"] = "ready"
    result["stage"] = "preflight"
    if dry_run:
        return result

    manifest = paths["manifest"]

    def save(first=False):
        write_json(manifest, result, first=first)

    def stage(name):
        result.update(status="running", stage=name)
        save()

    def transaction(tool, response, recovery=None):
        result["transactions"].append({"tool": tool, "result": response})
        if recovery and not tracking_mode:
            result["recovery"].insert(0, recovery)
        save()

    def check_inputs():
        if tracking_mode:
            require_database_references()
        for label, asset in assets.items():
            if label in ("PAN", "OND") and asset["library"] == "builtin":
                name = pan_name if label == "PAN" else ond_name
                if store.components._path(label, name).is_file():
                    raise ValueError("Workspace now shadows selected builtin component: " + name)
            if fingerprint(asset["path"]) != asset["sha256"]:
                raise ValueError("Input changed during job: " + asset["path"])

    save(first=True)
    try:
        stage("clone")
        if store.inspect_project(template_project)["files"] != original["files"]:
            raise ValueError("Template changed after preflight")
        check_inputs()
        cloned = store.clone_project(template_project, new_project)
        transaction("pvsyst_clone_project", cloned)
        stage("sources")
        sources = store.update_project_sources(new_project, site_name, met_name, cloned["files"])
        transaction("pvsyst_update_project_sources", sources, {
            "tool": "pvsyst_restore_project_sources", "arguments": {
                "project": new_project, "backups": sources["backups"],
                "expected_files": sources["files"], "confirm": True}})
        if not tracking_mode:
            stage("components")
            edited = store.update(new_project, variant, {"PAN": pan_name, "OND": ond_name},
                                  store.inspect(new_project, variant)["sha256"], array_id)
            transaction("pvsyst_update_variant_components", edited, {
                "tool": "pvsyst_restore_variant", "arguments": {
                    "project": new_project, "variant": variant, "backup_name": edited["backup_name"],
                    "expected_sha256": edited["sha256"], "confirm": True}})
        if capacity is not None:
            stage("capacity")
            for updates in result["capacity_plan"]["steps"]:
                edited = store.update_parameters(new_project, variant, edited["sha256"], array_id, orientation_id,
                                                 subarray_updates=updates)
                transaction("pvsyst_update_variant_parameters", edited, {
                    "tool": "pvsyst_restore_variant", "arguments": {
                        "project": new_project, "variant": variant, "backup_name": edited["backup_name"],
                        "expected_sha256": edited["sha256"], "confirm": True}} if edited.get("backup_name") else None)
            actual = store.inspect_parameters(new_project, variant)["subarrays"][0]["values"]
            planned = result["capacity_plan"]
            if any(int(actual[key]) != planned[value] for key, value in (
                    ("NModSerie", "series_modules"), ("NStringCh", "string_count"), ("NInverter", "inverter_count"))):
                raise ValueError("Configured counts do not match the accepted capacity plan")
        if tracking_mode:
            stage("tracking")
            created = create_tracking_variant(store, project=new_project, source_variant=variant,
                                               new_variant=tracking_variant, tracking=tracking, dry_run=False)
            transaction("pvsyst_create_tracking_variant", created)
            if created["status"] != "created":
                raise ValueError("Tracking variant was not created: " + "; ".join(created["issues"]))
            variant = tracking_variant
            result["working_variant"] = variant
            result["warnings"].extend(created["warnings"])
        else:
            stage("parameters")
            edited = store.update_parameters(new_project, variant, edited["sha256"], array_id, orientation_id,
                                             orientation_updates=angles,
                                              subarray_updates=subarray_losses or None,
                                              main_array_updates=main_losses or None)
            transaction("pvsyst_update_variant_parameters", edited, {
                "tool": "pvsyst_restore_variant", "arguments": {
                    "project": new_project, "variant": variant, "backup_name": edited["backup_name"],
                    "expected_sha256": edited["sha256"], "confirm": True}} if edited.get("backup_name") else None)
        result["working_files"] = store.inspect_project(new_project)["files"]
        if store.validate_structure(new_project, variant)["valid"] is not True:
            raise ValueError("Configured working variant failed structural validation")
        if prepare_only:
            check_inputs()
            review = weather_warnings or result["preparation_context"].get("review_required", False)
            result.update(status="needs_review" if review else "prepared", stage="prepared",
                          prepared=True, working_project=new_project, working_variant=variant)
            result["artifacts"].update(project=str(store._path(new_project)),
                                       variant=str(store._path(new_project, variant)))
        else:
            stage("simulation")
            check_inputs()
            variables = list(VAR_GROUPS["all_common"])
            if tracking_mode:
                variables.append("viGlobShd")
            if weather_report is not None:
                variables.extend(["viGlobHor", "viDiffHor", "viT_Amb"])
            build_sfi(paths["sfi"], variables)
            native_inputs = ({"site": Path(assets["site_name"]["path"]), "met": Path(assets["met_name"]["path"])}
                             if weather_report is not None else {})
            run = cli.run_simulation(new_project, variant, sfi=paths["sfi"], out_csv=paths["csv"],
                                     report_pdf=paths["pdf"], time_step="hour", start_date=f"{year}.01.01",
                                     end_date=f"{year}.12.31", report_language="en", log_level=3,
                                     report_pages=report_pages, **native_inputs)
            result["run"] = run
            result["warnings"].extend(run.get("warnings", []))
            stage("results")
            output_snapshot = fingerprint(paths["csv"])
            expected_context = {"Project": new_project, "Geographical Site": site_name, "Weather data": met_name,
                                "Simulation variant": f"{Path(new_project).stem}.{variant}"}
            context = {}
            with paths["csv"].open(encoding="utf-8-sig", errors="replace", newline="") as stream:
                for row in csv.reader(stream, delimiter=";"):
                    if row and row[0].strip().lower() == "date":
                        break
                    if len(row) >= 2 and row[0].strip() in expected_context:
                        key = row[0].strip()
                        if key in context:
                            raise ValueError("Duplicate native result context: " + key)
                        context[key] = row[1].strip()
            if any(context.get(key, "").casefold() != value.casefold() for key, value in expected_context.items()):
                raise ValueError("Native CSV did not consume the requested project/variant/site/weather: " + str(context))
            result["native_context"] = context
            result["metrics"] = annual_summary(paths["csv"], year, nominal)
            if tracking_mode:
                rows = 0
                for headers, row in iter_result_csv(paths["csv"], strict=True):
                    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in row[1:]):
                        raise ValueError("Native tracking output contains missing/nonfinite numeric data")
                    rows += 1
                units = result_units(paths["csv"], strict=True)
                if units.get("GlobShd") != "W/m\u00b2":
                    raise ValueError("Native UnlimTracking output lacks GlobShd in W/m2")
                result["tracking_output"] = {"hourly_rows": rows, "numeric_columns": headers[1:], "units": units}
            if result["metrics"]["sha256"] != output_snapshot:
                raise ValueError("Native output changed between context and annual validation")
            if weather_report is not None:
                result["weather_comparison"] = compare_native_weather(
                    paths["csv"], Path(assets["weather_source"]["path"]), weather_report)
            with paths["pdf"].open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise ValueError("Native report is not a PDF")
            if store.inspect_project(new_project)["files"] != result["working_files"]:
                raise ValueError("Working files changed during native simulation")
            check_inputs()
            if fingerprint(paths["csv"]) != output_snapshot:
                raise ValueError("Native output changed across result validation stages")
            flags = result["metrics"]["diagnostic_flags"]
            result["warnings"].extend(flags)
            review = (tracking_mode or run.get("warnings") or flags or weather_warnings
                      or result.get("weather_comparison", {}).get("needs_review", False))
            result.update(status="needs_review" if review else "completed", stage="completed")
    except (ValueError, OSError, KeyError, TypeError, PVsystError, subprocess.TimeoutExpired) as exc:
        result.update(status="failed", error=cli._redact(str(exc))[-12000:])
    except BaseException as exc:
        # An unanticipated error still leaves a terminal manifest, never "running"; it then propagates.
        result.update(status="failed", error=cli._redact(f"{type(exc).__name__}: {exc}")[-12000:])
        try:
            save()
        except Exception:
            pass  # a failed save must not replace the original error
        raise
    try:
        result["template_preserved"] = store.inspect_project(template_project)["files"] == original["files"]
        if not result["template_preserved"]:
            result.update(status="failed", error="Template changed externally during the job")
        if store._path(new_project).is_file():
            result["working_files"] = store.inspect_project(new_project)["files"]
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result.update(status="failed", error=cli._redact(str(exc)))
    if prepare_only and result["status"] == "failed":
        result["prepared"] = False
    if tracking_mode:
        result["native_consumption_verified"] = result["status"] in ("completed", "needs_review") and result["stage"] == "completed"
    save()
    return result

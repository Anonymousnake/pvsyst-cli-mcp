"""One bounded template-to-annual-results workflow, reusing guarded file editors."""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

from pvsyst_auto.native.cli import PVsystError, VAR_GROUPS, build_sfi, validate_report_pages, iter_result_csv, result_units
from pvsyst_auto.components.store import electrical_inventory
from pvsyst_auto.engineering.diagnostics import compact_diagnostics_summary, diagnose_simulation_csv
from pvsyst_auto.engineering.sizing import plan_capacity
from pvsyst_auto.project.circuit import circuit_nodes, inventory as grid_inventory
from pvsyst_auto.project.paths import project_path, variant_id, workspace_file, write_json
from pvsyst_auto.components.editor import ASSIGNMENT, direct_fields, one, pan_validation_fields, root_block
from pvsyst_auto.weather.preparation import load_weather_manifest, compare_native_weather, inspect_met_calendar
from pvsyst_auto.results import annual_summary, fingerprint, verify_native_csv_context, verify_pdf_header
from pvsyst_auto.project.tracking import create_tracking_variant
from pvsyst_auto.validation import CLI_TRACKING_BUILDS, NATIVE_TEXT_OR_SEED, SEED_LABEL


def collect_simulation_weather_assets(
    workspace: Path,
    *,
    site_name: str,
    met_name: str,
    year: int,
    synthetic_weather: bool,
    weather_manifest_name: str,
    assets: dict,
    result: dict,
    issue,
    inspect_calendar=None,
) -> tuple[dict | None, list]:
    """Resolve and fingerprint workspace SIT/MET inputs and optional weather manifest."""
    calendar_fn = inspect_met_calendar if inspect_calendar is None else inspect_calendar
    weather_targets = (
        (("site_name", "Sites", site_name, ".SIT"),)
        if synthetic_weather
        else (("site_name", "Sites", site_name, ".SIT"), ("met_name", "Meteo", met_name, ".MET"))
    )
    for field, folder, name, extension in weather_targets:
        try:
            path = workspace_file(workspace, folder, name, extension)
            if not path.is_file():
                raise FileNotFoundError(f"Required {folder} file is missing: {name}")
            assets[field] = {"path": str(path), "sha256": fingerprint(path)}
            if field == "met_name":
                calendar = calendar_fn(path, year)
                result["meteo_calendar"] = calendar
                for message in calendar["issues"]:
                    issue(field, message)
                result["warnings"].extend(calendar["warnings"])
        except (ValueError, OSError) as exc:
            issue(field, exc)
    weather_report = None
    weather_warnings: list = []
    if weather_manifest_name:
        try:
            weather_assets, weather_report, weather_warnings = load_weather_manifest(
                workspace, weather_manifest_name, year, site_name, met_name)
            assets.update(weather_assets)
            result["warnings"].extend(weather_warnings)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            issue("weather_manifest_name", exc)
    return weather_report, weather_warnings


def native_cli_weather_inputs(assets: dict, *, synthetic_weather: bool, weather_report: object) -> dict:
    """Build native CLI site/meteo kwargs matching the prepared simulation assets."""
    if synthetic_weather:
        return {"site": Path(assets["site_name"]["path"]), "synthetic_weather": True}
    if weather_report is not None:
        return {"site": Path(assets["site_name"]["path"]), "met": Path(assets["met_name"]["path"])}
    return {}


def verify_and_read_simulation_results(
    result: dict,
    *,
    store,
    project: str,
    variant: str,
    site_name: str,
    met_name: str,
    year: int,
    nominal_dc_kw: float,
    csv_path: Path,
    pdf_path: Path,
    assets: dict,
    synthetic_weather: bool,
    weather_report: object,
    validate_output=None,
) -> bool:
    """Run context, annual, optional output-shape, weather, PDF, and diagnostic verification."""
    output_snapshot = fingerprint(csv_path)
    expected_context = {
        "Project": project,
        "Geographical Site": site_name,
        "Simulation variant": f"{Path(project).stem}.{variant}",
    }
    if not synthetic_weather:
        expected_context["Weather data"] = met_name
    result["native_context"] = verify_native_csv_context(csv_path, expected_context)
    result["metrics"] = annual_summary(csv_path, year, nominal_dc_kw)
    if validate_output is not None:
        validate_output()
    if result["metrics"]["sha256"] != output_snapshot:
        raise ValueError("Native output changed between context and annual validation")
    if weather_report is not None:
        result["weather_comparison"] = compare_native_weather(
            csv_path, Path(assets["weather_source"]["path"]), weather_report,
        )
    verify_pdf_header(pdf_path)
    if fingerprint(csv_path) != output_snapshot:
        raise ValueError("Native output changed across result validation stages")
    flags = result["metrics"]["diagnostic_flags"]
    result["warnings"].extend(flags)
    diag_full = diagnose_simulation_csv(
        csv_path, store=store, project=project, variant=variant,
    )
    result["diagnostics"] = compact_diagnostics_summary(diag_full)
    return bool(diag_full["needs_review"])


def record_workflow_failure(result: dict, cli, exc: BaseException, save) -> bool:
    """Record a workflow failure onto ``result``; return True when the exception must propagate."""
    if issubclass(type(exc), (ValueError, OSError, KeyError, TypeError, PVsystError, subprocess.TimeoutExpired)):
        result.update(status="failed", error=cli._redact(str(exc))[-12000:])
        return False
    # An unanticipated error still leaves a terminal manifest, never "running"; it then propagates.
    result.update(status="failed", error=cli._redact(f"{type(exc).__name__}: {exc}")[-12000:])
    try:
        save()
    except Exception:
        pass  # a failed save must not replace the original error
    return True


def simulate_template(cli, store, *, template_project="", variant="", new_project="",
                      pan_name="", ond_name="", site_name="", met_name="",
                      tilt=None, year=None, dry_run=True, capacity=None, weather_manifest_name="", azimuth=None, losses=None,
                      report_pages=None, report_language="en", tracking=None, tracking_variant="",
                      nominal_dc_power_kw=None, tracking_power_source="",
                      synthetic_weather=False):
    # Hold one workspace lock; use separate workspaces for parallel native runs.
    with store.lock:
        return _simulate_template(cli, store, template_project, variant, new_project,
                                  pan_name, ond_name, site_name, met_name, tilt, year, dry_run, capacity, weather_manifest_name, azimuth, losses,
                                  report_pages=report_pages, report_language=report_language,
                                  tracking=tracking, tracking_variant=tracking_variant,
                                  nominal_dc_power_kw=nominal_dc_power_kw, tracking_power_source=tracking_power_source,
                                  synthetic_weather=synthetic_weather)


def prepare_template(cli, store, **kwargs):
    """Reuse the same guarded configuration, stopping before SFI/annual execution."""
    with store.lock:
        return _simulate_template(cli, store, **kwargs, prepare_only=True)


def _simulate_template(cli, store, template_project="", variant="", new_project="",
                       pan_name="", ond_name="", site_name="", met_name="", tilt=None, year=None,
                       dry_run=True, capacity=None, weather_manifest_name="", azimuth=None, losses=None,
                       *, report_pages=None, report_language="en", prepare_only=False, preparation_context=None,
                       tracking=None, tracking_variant="", nominal_dc_power_kw=None, tracking_power_source="",
                       allow_window_creation=None, synthetic_weather=False):
    tracking_mode = tracking is not None
    request = dict(template_project=template_project, variant=variant, new_project=new_project,
                   pan_name=pan_name, ond_name=ond_name, site_name=site_name, met_name=met_name,
                   tilt=tilt, year=year)
    if synthetic_weather:
        request.pop("met_name", None)
        request["synthetic_weather"] = True
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
            "template_main_array_fields": parameters.get("main_array", {}).get("values", {})}
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
    weather_report, weather_warnings = collect_simulation_weather_assets(
        cli.workspace,
        site_name=site_name,
        met_name=met_name,
        year=year,
        synthetic_weather=synthetic_weather,
        weather_manifest_name=weather_manifest_name,
        assets=assets,
        result=result,
        issue=issue,
    )
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
                    cli._require("run-simulation", option, allow_window_creation=allow_window_creation)
            license_state = cli.license_info(allow_window_creation=allow_window_creation)
            if (not prepare_only and license_state.get("status") == "TRIAL"
                    and license_state.get("remaining_executions") == 0):
                raise ValueError("PVsystCLI reports no remaining trial executions")
            result["license"] = license_state
            result["cli_version"] = cli.version(allow_window_creation=allow_window_creation)
            if tracking_mode and result["cli_version"] not in CLI_TRACKING_BUILDS:
                raise ValueError("Tracking flow requires a verified PVsystCLI build: "
                                 + ", ".join(CLI_TRACKING_BUILDS))
        except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc:
            issue("simulation", cli._redact(str(exc)))
    result["inputs"] = assets
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

    def transaction(tool, response):
        result["transactions"].append({"tool": tool, "result": response})
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
        transaction("clone_project", cloned)
        if not tracking_mode:
            result["recovery"] = [{
                "action": "retain_isolated_project_for_review",
                "automatic": False,
                "project": new_project,
                "artifacts": dict(result["artifacts"]),
                "reason": (
                    "Original template is retained untouched. Review/archive the isolated project, "
                    "its .mcp-variant-backups snapshots, and any outputs before retrying under a new project name."
                ),
            }]
        stage("sources")
        sources = store.update_project_sources(
            new_project, site_name, "" if synthetic_weather else met_name, cloned["files"],
            allow_synthetic_met=synthetic_weather,
        )
        transaction("update_project_sources", sources)
        if not tracking_mode:
            stage("components")
            edited = store.update(new_project, variant, {"PAN": pan_name, "OND": ond_name},
                                  store.inspect(new_project, variant)["sha256"], array_id)
            transaction("update_variant_components", edited)
        if capacity is not None:
            stage("capacity")
            for updates in result["capacity_plan"]["steps"]:
                edited = store.update_parameters(new_project, variant, edited["sha256"], array_id, orientation_id,
                                                 subarray_updates=updates)
                transaction("update_variant_parameters", edited)
            actual = store.inspect_parameters(new_project, variant)["subarrays"][0]["values"]
            planned = result["capacity_plan"]
            if any(int(actual[key]) != planned[value] for key, value in (
                    ("NModSerie", "series_modules"), ("NStringCh", "string_count"), ("NInverter", "inverter_count"))):
                raise ValueError("Configured counts do not match the accepted capacity plan")
        if tracking_mode:
            stage("tracking")
            created = create_tracking_variant(store, project=new_project, source_variant=variant,
                                               new_variant=tracking_variant, tracking=tracking, dry_run=False)
            transaction("create_tracking_variant", created)
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
            transaction("update_variant_parameters", edited)
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
            native_inputs = native_cli_weather_inputs(
                assets, synthetic_weather=synthetic_weather, weather_report=weather_report,
            )
            run = cli.run_simulation(new_project, variant, sfi=paths["sfi"], out_csv=paths["csv"],
                                     report_pdf=paths["pdf"], time_step="hour", start_date=f"{year}.01.01",
                                     end_date=f"{year}.12.31", report_language=report_language, log_level=3,
                                     report_pages=report_pages, **native_inputs)
            result["run"] = run
            result["warnings"].extend(run.get("warnings", []))
            stage("results")

            def _validate_tracking():
                if not tracking_mode:
                    return
                rows = 0
                headers = []
                for headers, row in iter_result_csv(paths["csv"], strict=True):
                    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in row[1:]):
                        raise ValueError("Native tracking output contains missing/nonfinite numeric data")
                    rows += 1
                units = result_units(paths["csv"], strict=True)
                if units.get("GlobShd") != "W/m\u00b2":
                    raise ValueError("Native UnlimTracking output lacks GlobShd in W/m2")
                result["tracking_output"] = {"hourly_rows": rows, "numeric_columns": headers[1:], "units": units}

            diag_needs_review = verify_and_read_simulation_results(
                result,
                store=store,
                project=new_project,
                variant=variant,
                site_name=site_name,
                met_name=met_name,
                year=year,
                nominal_dc_kw=nominal,
                csv_path=paths["csv"],
                pdf_path=paths["pdf"],
                assets=assets,
                synthetic_weather=synthetic_weather,
                weather_report=weather_report,
                validate_output=_validate_tracking,
            )
            flags = result["metrics"]["diagnostic_flags"]
            review = (tracking_mode or run.get("warnings") or flags or weather_warnings
                      or result.get("weather_comparison", {}).get("needs_review", False)
                      or diag_needs_review)
            result.update(status="needs_review" if review else "completed", stage="completed")
    except BaseException as exc:
        if record_workflow_failure(result, cli, exc, save):
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


def _blocks_request(blocks):
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 100:
        raise ValueError("blocks must be a nonempty list of at most 100 block types")
    result, ids, sources = [], set(), set()
    for block in blocks:
        if not isinstance(block, dict) or set(block) != {"id", "source_subarray_id", "count"}:
            raise ValueError("Each block requires only id, source_subarray_id and count")
        identifier, source, count = (block[key] for key in ("id", "source_subarray_id", "count"))
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 100
                or any(ord(char) < 32 for char in identifier) or identifier in ids):
            raise ValueError("Block id must be unique, nonblank printable text of at most 100 characters")
        if type(source) is not int or not 1 <= source <= 2147483647 or type(count) is not int or not 1 <= count <= 100:
            raise ValueError("source_subarray_id and count must be positive strict integers; count is at most 100")
        if source in sources:
            raise ValueError("Each source_subarray_id must occur exactly once")
        ids.add(identifier)
        sources.add(source)
        result.append(dict(block))
    if sum(block["count"] for block in result) > 100:
        raise ValueError("The total block count must not exceed 100")
    return result


def _owner_values(root):
    return {key: [value for _, value in entries] for key, entries in root.fields.items()
            if key not in {"SubArrayId", "Comment"}}


def _inspect_blocks(store, data):
    _, lines = store._text(data)
    arrays, system = store._sections(lines)
    main = store._main_array(lines)
    if system is None or main is None:
        raise ValueError("Require a complete native main array and Grid system")
    first_child = next((index for index in range(1, len(lines))
                        if (match := ASSIGNMENT.fullmatch(lines[index].rstrip("\r\n")))
                        and match[2].startswith("PVObject_")), len(lines) - 1)
    if one(direct_fields(lines, (0, first_child)), "Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Block preparation requires native variant Version={SEED_LABEL}")
    main_fields = root_block(lines, main).fields
    for key, expected in (("Flags", 0x00100400), ("Flags2", 0x082A)):
        raw = one(main_fields, key)[1]
        if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw) or int(raw[1:], 16) != expected:
            raise ValueError("Block preparation requires the verified native main-array Flags=$00100400/Flags2=$082A profile")
    fields = direct_fields(lines, system)
    if one(fields, "SystemType")[1] != "Grid":
        raise ValueError("Only PV-only Grid block projects are supported")
    flags = one(fields, "Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) or int(flags[1:], 16) != 0:
        raise ValueError("System grid limits, auxiliary powers, generator or other active system modes are unsupported")
    if set(fields) - {"Comment", "Flags", "SystemType"}:
        raise ValueError("Additional owning Grid system controls require separate review")
    for key in fields:
        one(fields, key)
    unsupported = {"pvLoad", "pvGridStorage", "pvBattery", "pvGenerator"}
    for line in lines:
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if match and match[2].startswith("PVObject_") and match[4] in unsupported:
            raise ValueError("Load, storage and generator objects are outside PV-only block preparation")
    if any((match := ASSIGNMENT.fullmatch(line.rstrip("\r\n")))
           and match[2].startswith("PVObject_") for line in lines[system[0] + 1:system[1]]):
        raise ValueError("Nested native Grid system objects require separate review")
    store._require_no_geometry(lines, require_shading_section=True)
    _, orientations = store._orientation_list(lines)
    planes = {}
    for first, last, identifier in orientations:
        store._ordinary_fixed_plane(lines, (first, last))
        planes[identifier] = "".join(lines[first:last + 1])

    def inspect_node(node):
        if node.kind == "TransformerNode":
            raise ValueError("Shared upstream native transformer configuration is not replicated by this operation")
        for child in node.children:
            inspect_node(child)

    for node in circuit_nodes(lines):
        inspect_node(node)
    inventory = grid_inventory(lines, arrays)
    if inventory["counts_valid"] is not True:
        raise ValueError("Require complete consistent grid counts: " + "; ".join(
            inventory["issues"] + inventory["unchecked_checks"]))
    rows = {row["id"]: row for row in inventory["subarrays"]}
    snapshots = {}
    for identifier in arrays:
        branches, root = store._subarray_branches(lines, arrays, identifier)
        store._require_plane_branch_mode(root, branches)
        if root.one("Version")[1] not in NATIVE_TEXT_OR_SEED:
            raise ValueError(f"Block preparation requires native subarray Version={SEED_LABEL}")
        orientation = root.one("NoOrientation")[1]
        if not orientation.isdigit() or int(orientation) not in planes:
            raise ValueError("Each source block requires one declared ordinary fixed plane")
        if root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(
                key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0"
                for key in root.fields):
            raise ValueError("Each block must allocate all strings to its one existing orientation")
        snapshots[identifier] = {
            "orientation_id": int(orientation), "owner_values": _owner_values(root),
            "counts": rows[identifier], "inverter_group_count": len(branches),
            "component_refs": {"PAN": root.one("PVModule")[1], "OND": root.one("GInverter")[1]},
        }
    return inventory, snapshots, planes


def prepare_block_project(store, source_project, source_variant, new_project, blocks, dry_run=True):
    """Copy the selected PRJ/VC only and repeat every complete source block in memory.

    count includes the retained original block. All source subarrays must be covered
    exactly once. Weather, losses, component models and existing orientations remain
    inherited; no physical sizing, energy calculation, native call or model fitting.
    """
    result = {
        "operation": "block_project_preparation", "schema_version": 1,
        "status": "blocked", "dry_run": dry_run, "prepared": False, "simulated": False,
        "native_consumption_verified": False,
        "review_required": True, "source": {}, "working": {}, "plan": [], "mapping": [],
        "serialized_counts": None, "issues": [], "warnings": [], "artifacts": {},
    }
    publishing = False
    try:
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a strict boolean")
        plan = _blocks_request(blocks)
        result["plan"] = plan
        with store.lock:
            source_prj = store._path(source_project)
            source_vc = store._path(source_project, source_variant)
            target_prj = store._path(new_project)
            target_vc = store._path(new_project, source_vc.suffix[1:])
            result["source"] = {"project": source_prj.name, "variant": source_vc.suffix[1:]}
            result["working"] = {"project": target_prj.name, "variant": target_vc.suffix[1:]}
            if any(store._belongs_to_project(path, target_prj.name) for path in store._root().iterdir()):
                raise FileExistsError("Destination project or variant already exists")
            project_data = store._read(source_prj)
            store._project_text(project_data)
            original = store._read(source_vc)
            source_counts, source_blocks, source_planes = _inspect_blocks(store, original)
            if {block["source_subarray_id"] for block in plan} != set(source_blocks):
                raise ValueError("blocks must cover every source subarray exactly once")
            candidate = original
            mapping = []
            for block in plan:
                source_id = block["source_subarray_id"]
                identifiers = [source_id]
                for _ in range(block["count"] - 1):
                    candidate, details = store._clone_subarray_data(candidate, source_id)
                    identifiers.append(details["subarray_id"])
                saved = source_blocks[source_id]
                mapping.append({
                    "id": block["id"], "source_subarray_id": source_id,
                    "subarray_ids": identifiers, "orientation_id": saved["orientation_id"],
                    "source_counts": saved["counts"], "component_refs": saved["component_refs"],
                })
            final_counts, final_blocks, final_planes = _inspect_blocks(store, candidate)
            expected_ids = [identifier for row in mapping for identifier in row["subarray_ids"]]
            if (len(expected_ids) != len(set(expected_ids)) or set(final_blocks) != set(expected_ids)
                    or final_planes != source_planes):
                raise ValueError("Prepared block identities or source orientation records do not match the plan")
            for row in mapping:
                source = source_blocks[row["source_subarray_id"]]
                for identifier in row["subarray_ids"]:
                    actual = final_blocks[identifier]
                    if (actual["owner_values"] != source["owner_values"]
                            or actual["orientation_id"] != source["orientation_id"]
                            or actual["component_refs"] != source["component_refs"]
                            or actual["inverter_group_count"] != source["inverter_group_count"]
                            or actual["counts"]["circuit"] != source["counts"]["circuit"]
                            or actual["counts"]["declared_module_count"] != source["counts"]["declared_module_count"]):
                        raise ValueError(f"Prepared block {identifier} does not preserve its complete native source")
            result.update(mapping=mapping, serialized_counts=final_counts,
                          source_serialized_counts=source_counts,
                          totals={"blocks": len(final_blocks),
                                  "modules": sum(row["declared_module_count"] for row in final_counts["subarrays"]),
                                  **final_counts["totals"]})
            if store._read(source_prj) != project_data or store._read(source_vc) != original:
                raise ValueError("Source PRJ/VC changed while preparing the block project")
            if dry_run:
                result["status"] = "ready"
                return result
            publishing = True
            created = store._create_project_files(target_prj.name,
                [(target_prj, project_data), (target_vc, candidate)])
            result.update(status="prepared", prepared=True,
                          artifacts={"project": str(target_prj), "variant": str(target_vc)},
                          created_files=list(created["files"]))
    except (ValueError, OSError) as exc:
        result["status"] = "failed" if publishing else "blocked"
        result["issues"].append(str(exc))
    return result

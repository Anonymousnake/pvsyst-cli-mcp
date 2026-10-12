"""One bounded template-to-annual-results workflow, reusing guarded file editors."""
from __future__ import annotations

import csv
import json
import math
import subprocess
from pathlib import Path

from pvsyst_cli import PVsystError, build_sfi
from pvsyst_paths import project_path, variant_id, workspace_file
from pvsyst_pan import validation_fields as pan_validation_fields
from pvsyst_results import annual_summary, fingerprint


def simulate_template(cli, store, *, template_project="", variant="", new_project="",
                      pan_name="", ond_name="", site_name="", met_name="",
                      tilt=None, year=None, dry_run=True):
    # ponytail: one workspace lock; separate workspaces for parallel native runs.
    with store.lock:
        return _simulate_template(cli, store, template_project, variant, new_project,
                                  pan_name, ond_name, site_name, met_name, tilt, year, dry_run)


def _simulate_template(cli, store, template_project, variant, new_project,
                       pan_name, ond_name, site_name, met_name, tilt, year, dry_run):
    request = dict(template_project=template_project, variant=variant, new_project=new_project,
                   pan_name=pan_name, ond_name=ond_name, site_name=site_name, met_name=met_name,
                   tilt=tilt, year=year)
    if isinstance(tilt, float) and not math.isfinite(tilt):
        request["tilt"] = str(tilt)
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run,
              "request": request, "issues": [], "warnings": [], "transactions": [], "recovery": [],
              "metrics": None, "artifacts": {}, "template_preserved": None}

    def issue(field, message):
        result["issues"].append({"field": field, "message": str(message)})

    for field, value in request.items():
        if value is None or value == "":
            issue(field, "Required input is missing")
    if type(dry_run) is not bool:
        issue("dry_run", "Use a strict boolean")
    if year is not None and (type(year) is not int or not 1969 <= year <= 2068):
        issue("year", "Use an integer year in 1969..2068; native SFI dates contain two-digit years")
    if tilt is not None and (isinstance(tilt, bool) or not isinstance(tilt, (int, float))
                             or not math.isfinite(tilt) or not 0 <= tilt <= 90):
        issue("tilt", "Use a finite numeric tilt in 0..90 degrees")
    if result["issues"]:
        return result

    assets = {}
    components = {}
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
        preview = store.update_parameters(template_project, variant, parameters["sha256"],
                                          array_id, orientation_id, orientation_updates={"FieldTilt": tilt},
                                          dry_run=True)
        result["configuration_preview"] = preview
        result["template_files"] = original["files"]
        result["template_preserved"] = True
        result["structure"] = structure
    except (ValueError, OSError) as exc:
        issue("template", exc)

    for kind, name in (("PAN", pan_name), ("OND", ond_name)):
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
        except (ValueError, OSError) as exc:
            issue(field, exc)
    try:
        prefix = f"{Path(new_project).stem}_annual_{year}"
        paths = {"csv": workspace_file(cli.workspace, "Results", prefix + ".csv", ".csv"),
                 "pdf": workspace_file(cli.workspace, "Results", prefix + ".pdf", ".pdf"),
                 "manifest": workspace_file(cli.workspace, "Results", prefix + ".json", ".json"),
                 "sfi": workspace_file(cli.workspace, "Models", prefix + ".SFI", ".SFI")}
        if any(path.exists() for path in paths.values()):
            raise FileExistsError("An output/SFI/manifest already exists; choose a new working project")
        result["artifacts"] = {key: str(path) for key, path in paths.items()}
    except (ValueError, OSError) as exc:
        issue("outputs", exc)
    if not result["issues"]:
        try:
            store._replace(data, {"PAN": pan_name, "OND": ond_name}, array_id)
            pan = components["PAN"]["physical_fields"]
            if float(pan["GRef"][0]) != 1000:
                raise ValueError("Standard annual PR requires PAN GRef=1000 W/m2")
            count = structure["grid_circuit"]["subarrays"][0]["declared_module_count"]
            nominal = count * float(pan["PNom"][0]) / 1000
            if not math.isfinite(nominal) or nominal <= 0:
                raise ValueError("Template/module combination has invalid nominal DC power")
            result["nominal_dc_power_kw"] = nominal
            for option in ("input-sfi-file", "start-date", "end-date", "time-step"):
                cli._require("run-simulation", option)
            license_state = cli.license_info()
            if license_state.get("status") == "TRIAL" and license_state.get("remaining_executions") == 0:
                raise ValueError("PVsystCLI reports no remaining trial executions")
            result["license"] = license_state
            result["cli_version"] = cli.version()
        except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc:
            issue("simulation", cli._redact(str(exc)))
    result["inputs"] = assets
    result["warnings"].extend([
        "Template string/inverter counts and azimuth are retained; voltage/current sizing and optimal design are not verified.",
        "Requested SIT/MET filenames and simulation year are checked in native output; geographic/time-reference compatibility is not certified.",
        "PAN/OND checks cover static fields, not internal model fitting; native results retain a conservative anomaly screen.",
        "Recovery is reverse-order whole-file restoration with fresh-state guards; not a cross-process lock or crash-atomic job.",
    ])
    if result["issues"]:
        return result
    result["status"] = "ready"
    result["stage"] = "preflight"
    if dry_run:
        return result

    manifest = paths["manifest"]
    manifest.parent.mkdir(parents=True, exist_ok=True)

    def save(first=False):
        with manifest.open("x" if first else "w", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.write("\n")

    def stage(name):
        result.update(status="running", stage=name)
        save()

    def transaction(tool, response, recovery=None):
        result["transactions"].append({"tool": tool, "result": response})
        if recovery:
            result["recovery"].insert(0, recovery)
        save()

    def check_inputs():
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
        stage("components")
        edited = store.update(new_project, variant, {"PAN": pan_name, "OND": ond_name},
                              store.inspect(new_project, variant)["sha256"], array_id)
        transaction("pvsyst_update_variant_components", edited, {
            "tool": "pvsyst_restore_variant", "arguments": {
                "project": new_project, "variant": variant, "backup_name": edited["backup_name"],
                "expected_sha256": edited["sha256"], "confirm": True}})
        stage("parameters")
        edited = store.update_parameters(new_project, variant, edited["sha256"], array_id, orientation_id,
                                         orientation_updates={"FieldTilt": tilt})
        transaction("pvsyst_update_variant_parameters", edited, {
            "tool": "pvsyst_restore_variant", "arguments": {
                "project": new_project, "variant": variant, "backup_name": edited["backup_name"],
                "expected_sha256": edited["sha256"], "confirm": True}} if edited.get("backup_name") else None)
        result["working_files"] = store.inspect_project(new_project)["files"]
        if store.validate_structure(new_project, variant)["valid"] is not True:
            raise ValueError("Configured working variant failed structural validation")
        stage("simulation")
        check_inputs()
        build_sfi(paths["sfi"], "all_common")
        run = cli.run_simulation(new_project, variant, sfi=paths["sfi"], out_csv=paths["csv"],
                                 report_pdf=paths["pdf"], time_step="hour", start_date=f"{year}.01.01",
                                 end_date=f"{year}.12.31", report_language="en", log_level=3)
        result["run"] = run
        result["warnings"].extend(run.get("warnings", []))
        stage("results")
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
        with paths["pdf"].open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("Native report is not a PDF")
        if store.inspect_project(new_project)["files"] != result["working_files"]:
            raise ValueError("Working files changed during native simulation")
        check_inputs()
        flags = result["metrics"]["diagnostic_flags"]
        result["warnings"].extend(flags)
        result.update(status="needs_review" if flags else "completed", stage="completed")
    except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc:
        result.update(status="failed", error=cli._redact(str(exc))[-12000:])
    try:
        result["template_preserved"] = store.inspect_project(template_project)["files"] == original["files"]
        if not result["template_preserved"]:
            result.update(status="failed", error="Template changed externally during the job")
        if store._path(new_project).is_file():
            result["working_files"] = store.inspect_project(new_project)["files"]
    except (ValueError, OSError) as exc:
        result.update(status="failed", error=cli._redact(str(exc)))
    save()
    return result

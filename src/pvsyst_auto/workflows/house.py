"""A bounded house delivery task, composing existing serializers and native entry points."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import subprocess

from pvsyst_auto.native.cli import VAR_GROUPS, build_sfi, iter_result_csv, result_units, validate_report_pages
from pvsyst_auto.components.editor import objects
from pvsyst_auto.engineering.diagnostics import compact_diagnostics_summary, diagnose_simulation_csv
from pvsyst_auto.project.paths import safe_filename, variant_id, workspace_file, write_json as _save
from pvsyst_auto.results import annual_summary, fingerprint, verify_native_csv_context, verify_pdf_header
from pvsyst_auto.scene.author import create_house_pv_scene, house_profile, obstacle_profiles, table_profiles, check_tables
from pvsyst_auto.scene.reader import read_house_pv_scene
from pvsyst_auto.native._prep import _source_snapshot, _source_snapshot_bytes, _verify_new
from pvsyst_auto.project.synthesize import synthesize_house_source

PHASES = ("initialized", "project_created", "scene_created", "prepared", "native_saved", "simulated", "completed")


def _normalize_house_request(req):
    """Strip legacy default scene_import_method='native' / None so v0.7.0 and current requests compare equal."""
    if isinstance(req, dict) and "scene_import_method" in req and req.get("scene_import_method") in ("native", None):
        return {k: v for k, v in req.items() if k != "scene_import_method"}
    return req


def _public(record):
    public = {key: value for key, value in record.items() if key not in ("request", "guards")}
    request = record.get("request", {})
    public["request_summary"] = {key: value for key, value in request.items()
                                 if key not in ("house", "layout", "module_geometry", "obstacles")}
    return public


def _annual(csv_path, pdf_path, request, plan, variant, *, require_pdf=True):
    expected = {"Project": request["new_project"], "Simulation variant": Path(request["new_project"]).stem + "." + variant,
                "Geographical Site": plan["site"], "Weather data": plan["met"]}
    context = verify_native_csv_context(
        csv_path,
        expected,
        mismatch_message="Native output does not identify the requested house project/variant/site/weather",
    )
    metrics = annual_summary(csv_path, request["year"], plan["nominal_dc_kw"])
    units = result_units(csv_path, strict=True)
    if not units.get("GlobShd"):
        raise ValueError("Native house output lacks the requested GlobShd column/unit")
    rows = 0
    for headers, row in iter_result_csv(csv_path, strict=True):
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in row[1:]):
            raise ValueError("Native house output contains missing/nonfinite numeric data")
        rows += 1
    if require_pdf:
        verify_pdf_header(pdf_path, message="Native house report is not a PDF")
    electrical = {}
    if request.get("shading_mode") == "module_layout":
        if units.get("ShdElec") != "kW":
            raise ValueError("Detailed native house output lacks the requested ShdElec column/unit")
        electrical = {"electrical_shading_output": {"column": "ShdElec", "unit": units["ShdElec"]}}
    diagnostics = compact_diagnostics_summary(diagnose_simulation_csv(csv_path))
    return {"metrics": metrics, "diagnostics": diagnostics, "native_context": context,
            "numeric_columns": len(headers) - 1, **electrical,
            "hourly_rows": rows, "shading_output": {"column": "GlobShd", "unit": units["GlobShd"]}}


def _prepare(store, components, request, *, resume=False):
    has_source_project = request.get("source_project") is not None
    has_source_variant = request.get("source_variant") is not None
    if has_source_project != has_source_variant:
        raise ValueError("Provide both source_project and source_variant, or omit both for zero-template synthesis")
    zero_template = not has_source_project
    target = request["new_project"]
    store._validate_display_name(request["variant_name"], "variant_name")
    year = request["year"]
    if type(year) is not int or not 1969 <= year <= 2068:
        raise ValueError("Use an explicit year in 1969..2068")
    validate_report_pages(request.get("report_pages"))
    prefix = Path(target).stem + "_house"
    scene_name = safe_filename(prefix + ".pvc2", ".pvc2")
    paths = {"working_project": store._path(target), "working_variant": store._path(target, "VC0"),
             "scene": workspace_file(store.workspace, "UserData", scene_name, ".pvc2"),
             "sfi": workspace_file(store.workspace, "Models", prefix + ".SFI", ".sfi"),
             "manifest": workspace_file(store.workspace, "Results", prefix + "_task.json", ".json")}

    entries = request["layout"].get("tables") if isinstance(request["layout"], dict) else None
    orientation_ids = ([item.get("orientation_id") for item in entries]
                       if isinstance(entries, list)
                       and all(isinstance(item, dict) and type(item.get("orientation_id")) is int
                               for item in entries) else [])
    grouped = (bool(orientation_ids) and all(identifier >= 1 for identifier in orientation_ids)
               and (len(set(orientation_ids)) >= 2
                    or any(item.get("source_subarray_id") is not None for item in entries)))

    synth_payload = None
    if zero_template:
        for index, orient in enumerate(request.get("orientations") or ()):
            if isinstance(orient, dict) and orient.get("base_slope_deg") is not None:
                slope = orient["base_slope_deg"]
                if not isinstance(slope, bool) and isinstance(slope, (int, float)) and slope != 0:
                    raise ValueError(
                        f"orientations[{index}].base_slope_deg must be 0 for headless house tasks "
                        "(headless 3D PV tables emit BaseSlope=0.0)"
                    )
        synth = synthesize_house_source(store, components, request)
        synth_payload = {"prj_text": synth["prj_text"], "vc0_text": synth["vc0_text"]}
        source_bytes = synth["vc0_text"].encode("utf-8")
        _, lines = store._text(source_bytes)
        project_text = synth["prj_text"].lstrip("\ufeff")
        snap = _source_snapshot_bytes(
            store, components, source_bytes, request.get("module_geometry"),
            allow_groups=grouped or len(synth["subarrays"]) > 1, layout=request.get("layout"),
        )
        inherited_parameters = {"synthesized": True, "orientations": synth["orientations"], "subarrays": synth["subarrays"]}
    else:
        project, variant = request["source_project"], variant_id(request["source_variant"])
        store._check_project(project)
        if project.casefold() == target.casefold():
            raise ValueError("The working project must be new, not the source project")
        paths.update(source_project=store._path(project), source_variant=store._path(project, variant))
        source_bytes = store._read(paths["source_variant"])
        _, lines = store._text(source_bytes)
        project_text = store._read(paths["source_project"]).decode("utf-8-sig")
        snap = _source_snapshot(store, components, project, variant, request.get("module_geometry"),
                                **({"allow_groups": True} if grouped else {}), layout=request.get("layout"))
        inherited_parameters = store.inspect_parameters(project, variant)
    # A heterogeneous per-source PAN set is carried by the typed per-table geometry forwarded
    # through ``layout`` into resolve_source_geometry; the headless builder consumes it per owner
    # instead of collapsing to the first PAN.
    source_groups = snap["source"].get("orientations") or []
    distinct_pans = {group["pan_name"].casefold() for group in source_groups}
    source_site = store._embedded_reference(lines, "PVObject_SiteSimul=pvSite", "End of PVObject pvSite")
    source_met = store._embedded_reference(lines, "PVObject_MeteoSimul=pvMeteo", "End of PVObject pvMeteo")
    project_root = objects(project_text)[0]
    project_site = store._embedded_reference(project_text.splitlines(), "PVObject_SitePrj=pvSite", "End of PVObject pvSite")
    met = re.findall(r"(?m)^[ \t]*MeteoFileName=([^\r\n]+)", project_text)
    if not source_site or not source_met or source_site != project_site or met != [source_met]:
        raise ValueError("Source PRJ and selected VC must already bind the same explicit workspace SIT/MET")
    paths.update(site=workspace_file(store.workspace, "Sites", source_site, ".sit"),
                 met=workspace_file(store.workspace, "Meteo", source_met, ".met"))
    if not paths["site"].is_file() or not paths["met"].is_file():
        raise ValueError("Source SIT/MET must exist in the workspace")
    component_libraries = {}
    if snap["pan_snapshot"] is not None:
        paths["pan"] = snap["pan_snapshot"][0]
        for library in ("workspace", "builtin"):
            if library == "builtin" and components.builtin is None:
                continue
            if components._path("PAN", snap["source"]["pan_name"], library).resolve() == paths["pan"].resolve():
                component_libraries["PAN"] = library
                break
        if "PAN" not in component_libraries:
            raise ValueError("Resolved PAN is outside the supported component libraries")
    sub_blocks = [block for block in objects("".join(lines)) if block.object_type == "pvSubArray"]
    sub = sub_blocks[0]
    # component_references.OND mirrors component_references.PAN: expose every DISTINCT actual
    # per-subarray GInverter reference (not only the first), while a homogeneous single-OND
    # source keeps its exact original one-string shape. Value-independent declared metadata --
    # no sizing/curve/MPPT derivation and no electronics curve is read here.
    ond_names = sorted({block.one("GInverter")[1] for block in sub_blocks if "GInverter" in block.fields})
    ond_reference = ond_names[0] if len(ond_names) == 1 else (ond_names or sub.one("GInverter")[1])
    for library in ("workspace", "builtin"):
        if library == "builtin" and components.builtin is None:
            continue
        ond = components._path("OND", sub.one("GInverter")[1], library)
        if ond.is_file():
            paths["ond"] = ond
            component_libraries["OND"] = library
            break
    pocket = house_profile(request["house"])
    if pocket["kind"] != "mesh":
        raise ValueError("The house task requires a complete AI/caller-authored mesh house")
    obstacles = obstacle_profiles(request.get("obstacles"), existing_names=[m["name"] for m in pocket["meshes"]])
    tables = table_profiles(request["layout"], pocket, snap["modules"], snap["source"])
    if zero_template and source_groups:
        for entry, table in zip(entries, tables):
            geometry = table.get("module_geometry", snap["modules"])
            if entry.get("module_geometry") is None and any(
                    geometry[axis] != snap["modules"][axis] for axis in ("width_m", "length_m")):
                raise ValueError(f"Table {table['name']} uses a different PAN size. Set layout.tables[].module_geometry.")
    check_tables(pocket, tables)
    mode = request.get("shading_mode", "linear")
    if mode not in ("linear", "module_layout"):
        raise ValueError("shading_mode must be linear or module_layout")
    binding = request.get("string_layout")
    if binding is not None and mode != "module_layout":
        raise ValueError("An explicit string_layout requires shading_mode='module_layout'")
    from pvsyst_auto.native.headless import _support_gate
    _support_gate(snap["source"], tables, source_bytes.decode("utf-8-sig"), mode,
                  string_layout=binding, check_spacing=not resume)
    site_blocks = objects(store._read(paths["site"]).decode("utf-8-sig"))
    if len(site_blocks) != 1 or site_blocks[0].object_type != "pvSite" or any(
            float(site_blocks[0].one(field)[1]) != snap["source"]["site"][field.lower()]
            for field in ("Latitude", "Longitude", "Altitude")):
        raise ValueError("Bound SIT coordinates differ from the source scene site")
    if source_groups:
        # Declaration-level nameplate inventory: each source group's own module count times its
        # own resolved nominal power. This is not an electrical/MPPT allocation and never reuses
        # the first PAN; resolve_source_geometry already refused any group without geometry.
        nominal = 0.0
        for group in source_groups:
            geometry = group.get("module_geometry")
            if not isinstance(geometry, dict) or "nom_power" not in geometry:
                raise ValueError("Source orientation %d has no resolved nominal module power; "
                                 "declare its per-source module geometry" % group["id"])
            nominal += geometry["nom_power"] * group["module_total"] / 1000
    else:
        nominal = snap["modules"]["nom_power"] * snap["source"]["module_total"] / 1000
    if not math.isfinite(nominal) or nominal <= 0:
        raise ValueError("Source nameplate capacity is not finite and positive")
    if len(distinct_pans) <= 1:
        pan_reference = snap["source"]["pan_name"]
    else:
        # Never silently bind a single PAN name to a heterogeneous set: expose each distinct
        # source PAN reference (per group) for the handoff declaration instead of the first one.
        pan_reference = sorted({group["pan_name"] for group in source_groups})
    plan = {"site": source_site, "met": source_met, "scene_name": scene_name,
            "synthesized": zero_template,
            "nominal_dc_kw": nominal, "shading_mode": mode,
            "house_parts": len(pocket["meshes"]), "pv_tables": len(tables),
            "obstacles": obstacles,
            "obstacle_count": len(obstacles),
            "source": snap["source"], "modules": snap["modules"],
            "tables": tables, "component_libraries": component_libraries,
            "component_references": {"PAN": pan_reference, "OND": ond_reference},
            "inherited_parameters": inherited_parameters,
            "inherited_project_parameters": {key: project_root.one(key)[1]
                for key in ("DesignTempMin", "TempOper", "TempOperMin", "TempOperMax", "TranspositionAlgorithm", "ACLossesRef")
                if key in project_root.fields}}
    if synth_payload is not None:
        plan["_synthesized_files"] = synth_payload
    return paths, plan


def _engineering_assets(paths, plan, guards):
    assets = {}
    for label, key, folder, library in (
            ("site", "site", "Sites", "workspace"), ("meteo", "met", "Meteo", "workspace"),
            ("scene", "scene", "UserData", "workspace"), ("sfi", "sfi", "Models", "workspace"),
            ("PAN", "pan", "ComposPV/PVmodules", plan["component_libraries"].get("PAN")),
            ("OND", "ond", "ComposPV/Inverters", plan["component_libraries"].get("OND"))):
        if key in paths:
            assets[label] = {"folder": folder, "name": paths[key].name,
                             "library": library, "sha256": guards[key]}
    return assets


def run_house_task(store, components, cli, *, request, dry_run=True, confirm=False,
                   resume=False, retry_simulation=False, simulation_preflight=None):
    """Deliver the house task, calling optional preflight only before a new attempt.

    The preflight callback returns an error message or None.
    """
    result = {"status": "blocked", "stage": "preflight", "issues": [], "warnings": [],
              "artifacts": {}, "native_consumption_verified": False}
    paths = None
    record = None
    attempt = None
    write_enabled = False
    with store.lock:
        try:
            if any(type(flag) is not bool for flag in (dry_run, confirm, resume, retry_simulation)):
                raise ValueError("Use strict booleans")
            import_method = request.get("scene_import_method") if isinstance(request, dict) else None
            if import_method is not None and import_method != "native":
                raise ValueError(
                    "Legacy Win32 GUI scene import ('gui') was removed in v0.6.0; "
                    "use scene_import_method='native' or omit it"
                )
            request = _normalize_house_request(request)
            if not resume and request.get("allow_window_creation") is False:
                raise ValueError("House shading recomputation requires allow_window_creation=true or unset")
            # Any value except an explicit False keeps the original CSV+PDF contract
            # (absent/None/True). CSV-only runs still recompute shading.
            require_pdf = request.get("report_pdf") is not False
            guard_keys = {"csv", "pdf"} if require_pdf else {"csv"}
            paths, plan = _prepare(store, components, request, resume=resume)
            synth_payload = plan.pop("_synthesized_files", None)
            zero_template = synth_payload is not None
            sources = {key: path for key, path in paths.items() if key in ("source_project", "source_variant", "site", "met", "pan", "ond")}
            if resume:
                if paths["manifest"].stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("House checkpoint exceeds 32MB")
                record = json.loads(paths["manifest"].read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("House checkpoint must contain an object")
                if record.get("schema") == 1 and record.get("phase") == "gui_closed":
                    record["phase"] = "native_saved"
                if (record.get("schema") != 1 or _normalize_house_request(record.get("request")) != request
                        or record.get("phase") not in PHASES
                        or not isinstance(record.get("guards"), dict) or not isinstance(record.get("issues"), list)
                        or not isinstance(record.get("artifacts"), dict) or not isinstance(record.get("runs"), list)
                        or len(record["runs"]) > 99 or any(not isinstance(run, dict) for run in record["runs"])):
                    raise ValueError("Checkpoint schema/request/phase differs; it is not a resumable task")
                record["request"] = request
                simulated = PHASES.index(record["phase"]) >= PHASES.index("simulated")
                if not simulated and request.get("allow_window_creation") is False:
                    raise ValueError("House shading recomputation requires allow_window_creation=true or unset; start a new task with that policy")
                if simulated and (
                        not record["runs"] or not isinstance(record.get("output_guards"), dict)
                        or set(record["output_guards"]) != guard_keys
                        or record["runs"][-1].get("status") != "returned"
                        or not isinstance(record["runs"][-1].get("native"), dict)
                        or not isinstance(record["runs"][-1]["native"].get("warnings"), list)):
                    raise ValueError("Completed native checkpoint lacks its recorded run/output evidence")
                if "variant" in record:
                    paths["native_variant"] = store._path(request["new_project"], variant_id(record["variant"]))
            else:
                if retry_simulation:
                    raise ValueError("retry_simulation requires resume=true")
                if paths["manifest"].exists() or any(store._belongs_to_project(p, request["new_project"]) for p in store._root().iterdir()):
                    raise FileExistsError("Working project/checkpoint namespace already exists; use a new project or explicit resume")
                preview = create_house_pv_scene(store, components,
                    project=(request["new_project"] if zero_template else request["source_project"]),
                    variant=("VC0" if zero_template else request["source_variant"]),
                    output_name=plan["scene_name"], house=request["house"],
                    layout=request["layout"], module_geometry=request.get("module_geometry"), dry_run=True,
                    string_layout=request.get("string_layout"), obstacles=request.get("obstacles"),
                    source_variant_bytes=(synth_payload["vc0_text"].encode("utf-8") if zero_template else None))
                if preview["status"] != "ready":
                    return {**result, "scene_preflight": preview, "issues": preview["issues"]}
                if paths["sfi"].exists():
                    raise FileExistsError("House SFI already exists; choose a new working project")
                run_stem = Path(request["new_project"]).stem + "_house_run01"
                if any(workspace_file(store.workspace, "Results", run_stem + "." + ext, "." + ext).exists()
                       for ext in (("csv", "pdf") if require_pdf else ("csv",))):
                    raise FileExistsError("House first-run outputs already exist; choose a new working project")
                record = {"schema": 1, "request": request, "phase": "initialized", "status": "ready",
                          "issues": [], "warnings": preview["warnings"], "runs": [], "guards": {},
                          "native_consumption_verified": False,
                          "artifacts": {key: str(paths[key]) for key in ("manifest", "working_project", "working_variant", "scene", "sfi")},
                          "plan": plan, "geometry": preview["geometry"]}
                record["guards"] = {key: fingerprint(path) for key, path in sources.items()}
            record["plan"] = plan
            result = record
            position = lambda: PHASES.index(record["phase"])
            bindings = dict(sources)
            for phase, keys in (("project_created", ("working_project", "working_variant")),
                                ("scene_created", ("scene",)), ("prepared", ("sfi",)), ("native_saved", ("native_variant",))):
                if position() >= PHASES.index(phase):
                    bindings.update({key: paths[key] for key in keys})
            def unchanged():
                if set(bindings) != set(record["guards"]):
                    raise ValueError("Checkpoint input/artifact guards are incomplete or unexpected")
                if any(fingerprint(path) != record["guards"][key] for key, path in bindings.items()):
                    raise ValueError("A protected source or prepared artifact changed; no resume action was taken")
                if record.get("operation") == "engineering_house":
                    handoff = record.get("input_handoff")
                    if (type(record.get("schema_version")) is not int or record["schema_version"] != 1 or not isinstance(handoff, dict)
                            or not isinstance(handoff.get("saved"), dict) or not handoff["saved"]
                            or record.get("working_project") != request["new_project"]
                            or record.get("working_variant") != record.get("variant")
                            or record.get("year") != request["year"]
                            or handoff.get("input_snapshot", {}).get("variant_sha256") != record["guards"].get("native_variant")
                            or record.get("input_assets") != _engineering_assets(paths, plan, record["guards"])
                            or store.inspect_project(request["new_project"])["files"] != record.get("working_files")):
                        raise ValueError("Bound native-house inputs or complete working file set changed")
            def bind_engineering_inputs():
                if record.get("operation") == "engineering_house":
                    return
                # A legacy completed/attempted run cannot acquire a retrospective input handoff.
                if record["phase"] != "native_saved" or record["runs"]:
                    return
                if "input_handoff" in record or record.get("operation") is not None:
                    raise ValueError("Incomplete native-house binding cannot be replaced")
                from pvsyst_auto.engineering.handoff import build_project_handoff
                handoff = build_project_handoff(store, project=request["new_project"], variant=record["variant"])
                if handoff["status"] == "blocked" or not handoff.get("saved"):
                    raise ValueError("Saved native-house engineering handoff is blocked: " + str(handoff["issues"]))
                assets = _engineering_assets(paths, plan, record["guards"])
                # ``component_references.PAN`` may be a list for a heterogeneous source set
                # (one distinct PAN per group). ``database_references`` is consumed by
                # revision._check_assets, which requires one STRING name per entry
                # (safe_filename(name)); expand the list into one entry per PAN, never store a
                # list in ``name``. A single-PAN str keeps its exact original one-entry shape.
                database = [{"kind": kind, "name": entry, "resolution": "native_database_or_unavailable"}
                            for kind, name in plan["component_references"].items() if kind not in assets
                            for entry in (name if isinstance(name, (list, tuple)) else (name,))]
                record.update(operation="engineering_house", schema_version=1,
                    working_project=request["new_project"], working_variant=record["variant"], year=request["year"],
                    objective="原生住宅交付：" + request["variant_name"], nominal_dc_power_kw=plan["nominal_dc_kw"],
                    input_handoff=handoff, working_files=store.inspect_project(request["new_project"])["files"],
                    input_assets=assets, database_references=database,
                    changes=[{"field": "geometry", "before": None,
                        "after": {"scene_name": plan["scene_name"], "house_parts": plan["house_parts"],
                        "geometry": record["geometry"],
                        "pv_regions": record["geometry"].get("tables", [
                            {"name": item.get("name", "PV table"), "orientation_id": item.get("orientation_id", 1),
                             "module_total": item["module_rows"] * item["module_columns"]}
                            for item in plan["tables"]]),
                        "native_saved_verification": record.get("native_import", {}).get("verification", {})},
                        "reason": "将已声明住宅网格和PV区域原生导入、关联、校验并另存为新变体；保存验证单独保留。"}])
                version = record.get("native_import", {}).get("cli_version")
                if isinstance(version, str) and version:
                    record["cli_version"] = version
                _save(paths["manifest"], record)
            def stage(phase, keys=()):
                for key in keys:
                    bindings[key] = paths[key]
                    record["guards"][key] = fingerprint(paths[key])
                record.update(phase=phase, status="running", issues=[])
                _save(paths["manifest"], record)
            unchanged()
            if position() >= PHASES.index("scene_created"):
                recovered = read_house_pv_scene(components, plan["scene_name"])
                arguments = recovered.get("create_arguments", {})
                if (recovered.get("status") != "ready" or arguments.get("project") != request["new_project"]
                        or arguments.get("variant") != "VC0" or arguments.get("house") != house_profile(request["house"])
                        or arguments.get("layout") != request["layout"]
                        or arguments.get("string_layout") != request.get("string_layout")
                        or obstacle_profiles(arguments.get("obstacles")) != plan["obstacles"]):
                    raise ValueError("Stored scene does not recover the declared house/layout/source")
            if position() >= PHASES.index("native_saved"):
                _verify_new(paths["native_variant"], store._read(paths["working_variant"]),
                    plan["house_parts"], plan["pv_tables"], plan["source"]["module_total"],
                    **({"scene_context": {"house": house_profile(request["house"]),
                        "obstacles": plan["obstacles"], "tables": plan["tables"],
                        "modules": plan["modules"], "source": plan["source"]}}
                       if plan["source"].get("orientations") else {}),
                    shading_mode=plan["shading_mode"],
                    obstacle_gen_count=sum(1 for obs in plan["obstacles"] if obs["kind"] != "tree"),
                    obstacle_tree_count=sum(1 for obs in plan["obstacles"] if obs["kind"] == "tree"))
            if record["phase"] == "prepared":
                # This task creates only VC0; another VC may be an unrecorded native SaveAs.
                candidates = [variant_id(member) for member in cli.list_variants(request["new_project"])
                              if member.upper() != "VC0"]
                if candidates:
                    return {**_public(record), "status": "needs_attention", "stage": "native_save_reconciliation",
                            "native_consumption_verified": False,
                            "unrecorded_native_variants": [
                                {"variant": member, "file": str(store._path(request["new_project"], member))}
                                for member in candidates],
                            "issues": ["Unrecorded native variants exist; inspect them before resuming. "
                                       "The bridge did not reopen, reimport or modify the checkpoint."]}
            if dry_run or not confirm:
                return {**_public(record), "status": "ready" if dry_run else "confirmation_required",
                        "stage": "preflight", "resume": resume, "native_consumption_verified": False}
            if not resume:
                _save(paths["manifest"], record, first=True)
            write_enabled = record["phase"] != "completed"
            if record["phase"] == "initialized":
                unchanged()
                if zero_template:
                    store.create_project(request["new_project"], synth_payload["prj_text"],
                                         {"VC0": synth_payload["vc0_text"]})
                else:
                    store.create_project(request["new_project"], store._read(paths["source_project"]).decode("utf-8-sig"),
                                         {"VC0": store._read(paths["source_variant"]).decode("utf-8-sig")})
                stage("project_created", ("working_project", "working_variant"))
            if record["phase"] == "project_created":
                created = create_house_pv_scene(store, components, project=request["new_project"], variant="VC0",
                    output_name=plan["scene_name"], house=request["house"], layout=request["layout"],
                    module_geometry=request.get("module_geometry"), dry_run=False,
                    string_layout=request.get("string_layout"),
                    obstacles=request.get("obstacles"))
                record["scene_creation"] = created
                if created["status"] != "created":
                    raise ValueError("House scene creation stopped: " + str(created["issues"]))
                stage("scene_created", ("scene",))
            if record["phase"] == "scene_created":
                variables = (list(VAR_GROUPS["shading"]) if plan["shading_mode"] == "module_layout"
                             else list(VAR_GROUPS["all_common"]) + ["viGlobShd"])
                build_sfi(paths["sfi"], variables, "House native hourly results")
                stage("prepared", ("sfi",))
            if record["phase"] == "prepared":
                if record.get("native_import", {}).get("file"):
                    raise ValueError("An unverified native save artifact exists; inspect it instead of reimporting")
                unchanged()
                from pvsyst_auto.native.headless import import_house_pv_scene_native
                imported = import_house_pv_scene_native(store, components, plan["scene_name"],
                                                        request["variant_name"], dry_run=False, confirm=True,
                                                        shading_mode=plan["shading_mode"])
                record["native_import"] = imported
                _save(paths["manifest"], record)
                if (imported["status"] != "native_variant_created"
                        or not imported.get("stored_geometry_verified")):
                    raise ValueError("Native headless house import stopped before a verified stored variant: "
                                     + str(imported.get("issues")))
                if (plan["shading_mode"] == "module_layout"
                        and imported.get("stored_module_layout_verified") is not True):
                    raise ValueError("Native headless module-layout import lacks stored layout verification")
                # STORED ONLY: PVsyst has not loaded or consumed this variant. imported /
                # native_geometry_verified / native_reload_verified stay false; consumption is
                # proven only by the later explicit CLI simulation on this new variant.
                record["variant"] = variant_id(imported["variant"])
                paths["native_variant"] = store._path(request["new_project"], record["variant"])
                record["artifacts"]["native_variant"] = str(paths["native_variant"])
                stage("native_saved", ("native_variant",))
            ran_simulation = False
            if record["phase"] == "native_saved":
                unchanged()
                bind_engineering_inputs()
                if record["runs"] and not retry_simulation:
                    raise ValueError("Previous simulation attempt is retained; explicit retry_simulation=true creates new outputs")
                number = len(record["runs"]) + 1
                stem = Path(request["new_project"]).stem + f"_house_run{number:02d}"
                csv_path = workspace_file(store.workspace, "Results", stem + ".csv", ".csv")
                pdf_path = workspace_file(store.workspace, "Results", stem + ".pdf", ".pdf")
                if csv_path.exists() or (require_pdf and pdf_path.exists()):
                    raise FileExistsError("Simulation outputs already exist; no native overwrite/retry")
                if simulation_preflight is not None and (error := simulation_preflight()) is not None:
                    raise ValueError(error)
                attempt = {"number": number, "status": "running", "csv": csv_path.name}
                if require_pdf:
                    attempt["pdf"] = pdf_path.name
                record["runs"].append(attempt)
                _save(paths["manifest"], record)
                run = cli.run_simulation(request["new_project"], record["variant"], sfi=paths["sfi"],
                    out_csv=csv_path, report_pdf=pdf_path if require_pdf else None, site=paths["site"],
                    met=paths["met"], time_step="hour",
                    start_date=f"{request['year']}.01.01", end_date=f"{request['year']}.12.31",
                    report_language="en" if require_pdf else None,
                    report_pages=request.get("report_pages") if require_pdf else None,
                    recompute_shading=True, log_level=3,
                    allow_window_creation=request.get("allow_window_creation"))
                attempt.update(status="returned", native=run)
                record["output_guards"] = {"csv": fingerprint(csv_path)}
                if require_pdf:
                    record["output_guards"]["pdf"] = fingerprint(pdf_path)
                ran_simulation = True
                stage("simulated")
            if position() >= PHASES.index("simulated"):
                attempt = record["runs"][-1]
                number = len(record["runs"])
                stem = Path(request["new_project"]).stem + f"_house_run{number:02d}"
                csv_path = workspace_file(store.workspace, "Results", stem + ".csv", ".csv")
                pdf_path = workspace_file(store.workspace, "Results", stem + ".pdf", ".pdf")
                if not ran_simulation:
                    unchanged()
                    current_guards = {"csv": fingerprint(csv_path)}
                    if require_pdf:
                        current_guards["pdf"] = fingerprint(pdf_path)
                    if (current_guards != record["output_guards"]
                            or attempt["csv"] != csv_path.name or (require_pdf and attempt.get("pdf") != pdf_path.name) or attempt["status"] != "returned"):
                        raise ValueError("Recorded native outputs changed or do not match this task attempt")
                verified = _annual(csv_path, pdf_path, request, plan, record["variant"], require_pdf=require_pdf)
                diag_needs_review = bool((verified.get("diagnostics") or {}).get("needs_review"))
                record.update(verified, native_consumption_verified=True,
                               status="needs_review" if (attempt["native"]["warnings"] or verified["metrics"]["diagnostic_flags"]
                                   or record.get("input_handoff", {}).get("status") == "needs_review"
                                   or diag_needs_review) else "completed",
                              phase="completed", stage="completed")
                record["artifacts"].update(csv=str(csv_path))
                if require_pdf:
                    record["artifacts"]["pdf"] = str(pdf_path)
                if record.get("operation") == "engineering_house":
                    record["result_files"] = dict(record["output_guards"])
                if write_enabled:
                    _save(paths["manifest"], record)
            return _public(record)
        except (OSError, ValueError, RuntimeError, KeyError, StopIteration, TypeError, subprocess.TimeoutExpired) as exc:
            if attempt is not None and attempt.get("status") == "running":
                attempt["status"] = "failed"
            result.update(status="needs_attention" if write_enabled else "blocked", stage="stopped",
                          native_consumption_verified=False)
            result.setdefault("issues", []).append(str(exc))
            result["recovery"] = "Keep all artifacts. Resume identical request after resolving the reported input/window issue; never reopen/reimport an unverified partial native save. Native retry needs explicit consent and new output names."
            if write_enabled and paths is not None and paths["manifest"].is_file() and record is not None:
                _save(paths["manifest"], record)
            return _public(result)
        except BaseException as exc:
            # An unanticipated error must not leave a written checkpoint "running"; it then propagates.
            if attempt is not None and attempt.get("status") == "running":
                attempt["status"] = "failed"
            if write_enabled and paths is not None and record is not None:
                record.update(status="needs_attention", stage="stopped", native_consumption_verified=False)
                try:
                    record.setdefault("issues", []).append(f"{type(exc).__name__}: {exc}")
                    if paths["manifest"].is_file():
                        _save(paths["manifest"], record)
                except Exception:
                    pass  # a failed save must not replace the original error
            raise

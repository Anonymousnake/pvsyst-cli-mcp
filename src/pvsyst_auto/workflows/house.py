"""A bounded house delivery task, composing existing serializers and native entry points."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import re
import subprocess
import time

from pvsyst_auto.native.cli import VAR_GROUPS, build_sfi, iter_result_csv, result_units, validate_report_pages
from pvsyst_auto.components.editor import objects
from pvsyst_auto.project.paths import safe_filename, variant_id, workspace_file, write_json as _save
from pvsyst_auto.results import annual_summary, fingerprint
from pvsyst_auto.scene.author import create_house_pv_scene, house_profile, table_profiles, check_tables
from pvsyst_auto.scene.reader import read_house_pv_scene
import pvsyst_auto.native.scene as native
from pvsyst_auto.native.project import _open, _processes, open_native_project

PHASES = ("initialized", "project_created", "scene_created", "prepared", "native_saved", "gui_closed", "simulated", "completed")


def _public(record):
    public = {key: value for key, value in record.items() if key not in ("request", "guards")}
    request = record.get("request", {})
    public["request_summary"] = {key: value for key, value in request.items()
                                 if key not in ("house", "layout", "module_geometry")}
    return public


def _close_owned(proc, cli):
    """Only an actual Popen held by this call is ownership; a checkpoint PID is not."""
    if proc.poll() is not None:
        return {"exited": True, "pid": proc.pid}
    expected = (Path(cli.cli).parent / "PVsyst.exe").resolve()
    if Path(native._exe_of(proc.pid)).resolve() != expected:
        raise ValueError("Owned native executable identity changed")
    end = time.monotonic() + 20
    posted, observed = set(), set()
    unexpected = []
    while proc.poll() is None and time.monotonic() < end:
        windows = [native._describe(h) for h in native._tops() if native._pid_of(h) == proc.pid]
        visible = [row for row in windows if row["visible"] and row["rect"][2] > row["rect"][0]
                   and row["rect"][3] > row["rect"][1]]
        observed.update(row["class"] for row in visible)
        unexpected = [row for row in visible if row["class"] not in ("TProjectForm", "THomeForm")]
        if unexpected:
            # Native shutdown can show a splash/progress form. Wait, never dismiss it.
            time.sleep(0.1)
            continue
        project = [row for row in visible if row["class"] == "TProjectForm"]
        targets = project or [row for row in visible if row["class"] == "THomeForm"]
        for row in targets:
            if row["enabled"] and row["hwnd"] not in posted and proc.poll() is None:
                native._post_close(row["hwnd"])
                posted.add(row["hwnd"])
        time.sleep(0.1)
    return {"exited": proc.poll() is not None, "pid": proc.pid, "windows": unexpected,
            "observed_window_classes": sorted(observed), "posted_close_handles": sorted(posted),
            "reason": "Only owned project/home received native close; no dialog dismissal or forced termination"}


def _annual(csv_path, pdf_path, request, plan, variant, *, require_pdf=True):
    expected = {"Project": request["new_project"], "Simulation variant": Path(request["new_project"]).stem + "." + variant,
                "Geographical Site": plan["site"], "Weather data": plan["met"]}
    context = {}
    with csv_path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        for row in csv.reader(stream, delimiter=";"):
            if row and row[0].strip().lower() == "date":
                break
            if len(row) > 1 and row[0].strip() in expected:
                key = row[0].strip()
                if key in context:
                    raise ValueError("Duplicate native result context: " + key)
                context[key] = row[1].strip()
    if any(context.get(key, "").casefold() != value.casefold() for key, value in expected.items()):
        raise ValueError("Native output does not identify the requested house project/variant/site/weather")
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
        with pdf_path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("Native house report is not a PDF")
    electrical = {}
    if request.get("shading_mode") == "module_layout":
        if units.get("ShdElec") != "kW":
            raise ValueError("Detailed native house output lacks the requested ShdElec column/unit")
        electrical = {"electrical_shading_output": {"column": "ShdElec", "unit": units["ShdElec"]}}
    return {"metrics": metrics, "native_context": context, "numeric_columns": len(headers) - 1, **electrical,
            "hourly_rows": rows, "shading_output": {"column": "GlobShd", "unit": units["GlobShd"]}}


def _prepare(store, components, request):
    project, variant, target = request["source_project"], variant_id(request["source_variant"]), request["new_project"]
    store._check_project(project)
    if project.casefold() == target.casefold():
        raise ValueError("The working project must be new, not the source project")
    store._validate_display_name(request["variant_name"], "variant_name")
    year = request["year"]
    if type(year) is not int or not 1969 <= year <= 2068:
        raise ValueError("Use an explicit year in 1969..2068")
    validate_report_pages(request.get("report_pages"))
    prefix = Path(target).stem + "_house"
    scene_name = safe_filename(prefix + ".pvc2", ".pvc2")
    paths = {"source_project": store._path(project), "source_variant": store._path(project, variant),
             "working_project": store._path(target), "working_variant": store._path(target, "VC0"),
             "scene": workspace_file(store.workspace, "UserData", scene_name, ".pvc2"),
             "sfi": workspace_file(store.workspace, "Models", prefix + ".SFI", ".sfi"),
             "manifest": workspace_file(store.workspace, "Results", prefix + "_task.json", ".json")}
    source_bytes = store._read(paths["source_variant"])
    _, lines = store._text(source_bytes)
    entries = request["layout"].get("tables") if isinstance(request["layout"], dict) else None
    grouped = (isinstance(entries, list) and len(entries) == 2
               and all(isinstance(item, dict) and type(item.get("orientation_id")) is int for item in entries)
               and {item["orientation_id"] for item in entries} == {1, 2})
    snap = native._source_snapshot(store, components, project, variant, request.get("module_geometry"),
                                   **({"allow_groups": True} if grouped else {}))
    source_site = store._embedded_reference(lines, "PVObject_SiteSimul=pvSite", "End of PVObject pvSite")
    source_met = store._embedded_reference(lines, "PVObject_MeteoSimul=pvMeteo", "End of PVObject pvMeteo")
    project_text = store._read(paths["source_project"]).decode("utf-8-sig")
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
    sub = next(block for block in objects("".join(lines))
               if block.object_type == "pvSubArray")
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
    tables = table_profiles(request["layout"], pocket, snap["modules"], snap["source"])
    check_tables(pocket, tables)
    mode = request.get("shading_mode", "linear")
    if mode not in ("linear", "module_layout"):
        raise ValueError("shading_mode must be linear or module_layout")
    method = request.get("scene_import_method") or "gui"
    binding = request.get("string_layout")
    # An explicit per-cell string_layout is consumed only by the headless native emitter in
    # module_layout mode. The GUI importer auto-allocates strings and the linear path has no
    # per-cell layout, so reject before any project/scene/SFI write instead of silently
    # dropping the declared wiring.
    if binding is not None:
        if method != "native":
            raise ValueError(
                "An explicit string_layout requires scene_import_method='native'; the GUI import "
                "path auto-allocates strings and cannot consume declared per-cell wiring")
        if mode != "module_layout":
            raise ValueError("An explicit string_layout requires shading_mode='module_layout'")
    if mode == "module_layout" and method != "native":
        # GUI automatic wiring keeps its proven dual profile; the headless path is gated below.
        native._module_layout_preflight(snap, tables)
    if method == "native":
        # The real evidence gate. With no binding it keeps the released single 1..3 linear /
        # 2 fine / dual recipes; an explicit binding is validated against native source
        # capacity and scene identity inside the gate (never inferred from table count), and
        # there is no caller-side allocator. Decide from the REAL source snapshot, not a
        # caller-supplied orientation_id.
        from pvsyst_auto.native.headless import _support_gate
        _support_gate(snap["source"], tables, source_bytes.decode("utf-8-sig"), mode,
                      string_layout=binding)
    elif not 1 <= len(tables) <= native.MAX_TABLES_BRIDGED:
        raise ValueError("The house task supports only one or two PV regions")
    site_blocks = objects(store._read(paths["site"]).decode("utf-8-sig"))
    if len(site_blocks) != 1 or site_blocks[0].object_type != "pvSite" or any(
            float(site_blocks[0].one(field)[1]) != snap["source"]["site"][field.lower()]
            for field in ("Latitude", "Longitude", "Altitude")):
        raise ValueError("Bound SIT coordinates differ from the source scene site")
    nominal = snap["modules"]["nom_power"] * snap["source"]["module_total"] / 1000
    if not math.isfinite(nominal) or nominal <= 0:
        raise ValueError("Source nameplate capacity is not finite and positive")
    plan = {"site": source_site, "met": source_met, "scene_name": scene_name,
            "nominal_dc_kw": nominal, "shading_mode": mode,
            "house_parts": len(pocket["meshes"]), "pv_tables": len(tables),
            "source": snap["source"], "modules": snap["modules"],
            "tables": tables, "component_libraries": component_libraries,
            "component_references": {"PAN": snap["source"]["pan_name"], "OND": sub.one("GInverter")[1]},
            "inherited_parameters": store.inspect_parameters(project, variant),
            "inherited_project_parameters": {key: project_root.one(key)[1]
                for key in ("DesignTempMin", "TempOper", "TempOperMin", "TempOperMax", "TranspositionAlgorithm", "ACLossesRef")
                if key in project_root.fields},
            "inherited_scope": "Hardware/counts/angles/losses and bound SIT/MET remain from the selected source; no sizing/climate/design certification."}
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
                   resume=False, retry_simulation=False):
    result = {"status": "blocked", "stage": "preflight", "issues": [], "warnings": [],
              "artifacts": {}, "native_consumption_verified": False}
    paths = None
    record = None
    proc = None
    write_enabled = False
    with store.lock:
        try:
            if any(type(flag) is not bool for flag in (dry_run, confirm, resume, retry_simulation)):
                raise ValueError("Use strict booleans")
            method = request.get("scene_import_method") or "gui"
            if method not in ("gui", "native"):
                raise ValueError("scene_import_method must be gui or native")
            # Any value except an explicit False keeps the original CSV+PDF contract
            # (absent/None/True). An explicit False requests the CSV-only native path,
            # so no PDF output is expected and the CLI runs as a plain CSV (windowless).
            require_pdf = request.get("report_pdf") is not False
            guard_keys = {"csv", "pdf"} if require_pdf else {"csv"}
            paths, plan = _prepare(store, components, request)
            sources = {key: path for key, path in paths.items() if key in ("source_project", "source_variant", "site", "met", "pan", "ond")}
            if resume:
                if paths["manifest"].stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("House checkpoint exceeds 32MB")
                record = json.loads(paths["manifest"].read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("House checkpoint must contain an object")
                if (record.get("schema") != 1 or record.get("request") != request or record.get("phase") not in PHASES
                        or not isinstance(record.get("guards"), dict) or not isinstance(record.get("issues"), list)
                        or not isinstance(record.get("artifacts"), dict) or not isinstance(record.get("runs"), list)
                        or len(record["runs"]) > 99 or any(not isinstance(run, dict) for run in record["runs"])):
                    raise ValueError("Checkpoint schema/request/phase differs; it is not a resumable task")
                if PHASES.index(record["phase"]) >= PHASES.index("simulated") and (
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
                preview = create_house_pv_scene(store, components, project=request["source_project"],
                    variant=request["source_variant"], output_name=plan["scene_name"], house=request["house"],
                    layout=request["layout"], module_geometry=request.get("module_geometry"), dry_run=True,
                    string_layout=request.get("string_layout"))
                if preview["status"] != "ready":
                    return {**result, "scene_preflight": preview, "issues": preview["issues"]}
                if method == "gui" and not 1 <= preview["geometry"]["pv_table_count"] <= native.MAX_TABLES_BRIDGED:
                    raise ValueError("The house task supports only one or two PV regions")
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
                if record["phase"] not in ("native_saved", "gui_closed") or record["runs"]:
                    return
                if "input_handoff" in record or record.get("operation") is not None:
                    raise ValueError("Incomplete native-house binding cannot be replaced")
                from pvsyst_auto.engineering.handoff import build_project_handoff
                handoff = build_project_handoff(store, project=request["new_project"], variant=record["variant"])
                if handoff["status"] == "blocked" or not handoff.get("saved"):
                    raise ValueError("Saved native-house engineering handoff is blocked: " + str(handoff["issues"]))
                assets = _engineering_assets(paths, plan, record["guards"])
                database = [{"kind": kind, "name": name, "resolution": "native_database_or_unavailable"}
                            for kind, name in plan["component_references"].items() if kind not in assets]
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
                unchanged()
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
                        or arguments.get("string_layout") != request.get("string_layout")):
                    raise ValueError("Stored scene does not recover the declared house/layout/source")
            if position() >= PHASES.index("native_saved"):
                if method == "gui":
                    if (plan["source"].get("orientations")
                            and record.get("native_import", {}).get("native_reload_verified") is not True):
                        raise ValueError("Grouped house checkpoint lacks verified native reload evidence")
                    if (plan["shading_mode"] == "module_layout"
                            and record.get("native_import", {}).get("native_shading_mode_reload_verified") is not True):
                        raise ValueError("Detailed house checkpoint lacks verified native calculation-mode reload evidence")
                native._verify_new(paths["native_variant"], store._read(paths["working_variant"]),
                    plan["house_parts"], plan["pv_tables"], plan["source"]["module_total"],
                    **({"scene_context": {"house": house_profile(request["house"]), "tables": plan["tables"],
                        "modules": plan["modules"], "source": plan["source"]}}
                       if plan["source"].get("orientations") else {}),
                    shading_mode=plan["shading_mode"])
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
            if method == "gui" and position() < PHASES.index("gui_closed"):
                ready = open_native_project(store, cli, project=request["source_project"], dry_run=True)
                if ready["status"] != "ready_to_open":
                    raise ValueError("A new owned GUI session/configured workspace is required: " + str(ready))
            if dry_run or not confirm:
                return {**_public(record), "status": "ready" if dry_run else "confirmation_required",
                        "stage": "preflight", "resume": resume, "native_consumption_verified": False}
            if not resume:
                _save(paths["manifest"], record, first=True)
            write_enabled = True
            if record["phase"] == "initialized":
                unchanged()
                store.create_project(request["new_project"], store._read(paths["source_project"]).decode("utf-8-sig"),
                                     {"VC0": store._read(paths["source_variant"]).decode("utf-8-sig")})
                stage("project_created", ("working_project", "working_variant"))
            if record["phase"] == "project_created":
                created = create_house_pv_scene(store, components, project=request["new_project"], variant="VC0",
                    output_name=plan["scene_name"], house=request["house"], layout=request["layout"],
                    module_geometry=request.get("module_geometry"), dry_run=False,
                    string_layout=request.get("string_layout"))
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
                if method == "native":
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
                else:
                    opening = {"status": "blocked", "stage": "preflight", "native_actions": []}
                    record["native_open"] = opening
                    proc = _open(store, cli, request["new_project"], False, True, opening)
                    if proc is None:
                        raise ValueError("The task will not take over an existing unverified user GUI session")
                    _save(paths["manifest"], record)
                    imported = native.import_house_pv_scene(store, components, cli, plan["scene_name"],
                                                            request["variant_name"], dry_run=False, confirm=True,
                                                            shading_mode=plan["shading_mode"])
                    record["native_import"] = imported
                    _save(paths["manifest"], record)
                    if imported["status"] != "imported" or not imported["native_geometry_verified"]:
                        raise ValueError("Native house import stopped; current owned window/artifacts are retained")
                    if plan["source"].get("orientations") and imported.get("native_reload_verified") is not True:
                        raise ValueError("Grouped house import requires verified native reload before input binding")
                    record["variant"] = variant_id(imported["variant"])
                    paths["native_variant"] = store._path(request["new_project"], record["variant"])
                    record["artifacts"]["native_variant"] = str(paths["native_variant"])
                    stage("native_saved", ("native_variant",))
            if record["phase"] == "native_saved":
                unchanged()
                bind_engineering_inputs()
                if proc is not None:
                    record["native_cleanup"] = _close_owned(proc, cli)
                    _save(paths["manifest"], record)
                    if not record["native_cleanup"]["exited"]:
                        raise ValueError("Owned GUI remains for inspection; simulation was not started")
                else:
                    if _processes(native, (Path(cli.cli).parent / "PVsyst.exe").resolve()):
                        raise ValueError("A native GUI remains; this resume will not close a checkpoint PID")
                    record["native_cleanup_recovery"] = {"native_processes_absent": True,
                        "checkpoint_pid_not_used_for_close": True, "close_performed_by_resume": False}
                stage("gui_closed")
            if record["phase"] == "gui_closed":
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
                    recompute_shading=True, log_level=3)
                attempt.update(status="returned", native=run)
                record["output_guards"] = {"csv": fingerprint(csv_path)}
                if require_pdf:
                    record["output_guards"]["pdf"] = fingerprint(pdf_path)
                stage("simulated")
            if position() >= PHASES.index("simulated"):
                unchanged()
                attempt = record["runs"][-1]
                number = len(record["runs"])
                stem = Path(request["new_project"]).stem + f"_house_run{number:02d}"
                csv_path = workspace_file(store.workspace, "Results", stem + ".csv", ".csv")
                pdf_path = workspace_file(store.workspace, "Results", stem + ".pdf", ".pdf")
                current_guards = {"csv": fingerprint(csv_path)}
                if require_pdf:
                    current_guards["pdf"] = fingerprint(pdf_path)
                if (current_guards != record["output_guards"]
                        or attempt["csv"] != csv_path.name or (require_pdf and attempt.get("pdf") != pdf_path.name) or attempt["status"] != "returned"):
                    raise ValueError("Recorded native outputs changed or do not match this task attempt")
                verified = _annual(csv_path, pdf_path, request, plan, record["variant"], require_pdf=require_pdf)
                unchanged()
                if current_guards != record["output_guards"]:
                    raise ValueError("Native outputs changed during validation")
                record.update(verified, native_consumption_verified=True,
                               status="needs_review" if (attempt["native"]["warnings"] or verified["metrics"]["diagnostic_flags"]
                                   or record.get("input_handoff", {}).get("status") == "needs_review") else "completed",
                              phase="completed", stage="completed")
                record["artifacts"].update(csv=str(csv_path))
                if require_pdf:
                    record["artifacts"]["pdf"] = str(pdf_path)
                if record.get("operation") == "engineering_house":
                    record["result_files"] = dict(record["output_guards"])
                    # Retire a capability notice emitted by the earlier scene-author
                    # stage, only after genuine native save/reload/year verification.
                    obsolete = "Two-orientation authoring is candidate-only; native automatic import remains needs_context."
                    record["warnings"] = [
                        "Two-orientation authoring and native consumption are separate stages; this task's saved/reloaded geometry and annual evidence are recorded below."
                        if warning == obsolete and record.get("native_import", {}).get("native_reload_verified") is True
                        else warning for warning in record.get("warnings", [])]
                _save(paths["manifest"], record)
            return _public(record)
        except (OSError, ValueError, RuntimeError, KeyError, StopIteration, TypeError, subprocess.TimeoutExpired) as exc:
            result.update(status="needs_attention" if write_enabled else "blocked", stage="stopped",
                          native_consumption_verified=False)
            result.setdefault("issues", []).append(str(exc))
            result["recovery"] = "Keep all artifacts. Resume identical request after resolving the reported input/window issue; never reopen/reimport an unverified partial native save. Native retry needs explicit consent and new output names."
            if write_enabled and paths is not None and paths["manifest"].is_file() and record is not None:
                _save(paths["manifest"], record)
            return _public(result)
        except BaseException as exc:
            # An unanticipated error must not leave a written checkpoint "running"; it then propagates.
            if write_enabled and paths is not None and record is not None:
                record.update(status="needs_attention", stage="stopped", native_consumption_verified=False)
                try:
                    record.setdefault("issues", []).append(f"{type(exc).__name__}: {exc}")
                    if paths["manifest"].is_file():
                        _save(paths["manifest"], record)
                except Exception:
                    pass  # a failed save must not replace the original error
            raise

"""A Chinese engineering delivery package bound to one recorded native annual run."""
from __future__ import annotations

import html
import json
from copy import deepcopy
from pathlib import Path
import zipfile

from pvsyst_auto.engineering.handoff import build_project_handoff
from pvsyst_auto.project.paths import project_name, safe_filename, variant_id, workspace_file
from pvsyst_auto.results import fingerprint


def _cell(value):
    if value is None:
        return "未记录"
    text = json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (dict, list)) else str(value)
    return html.escape(text, quote=False).replace("|", "\\|").replace("\r", "").replace("\n", " / ")


def _table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
                     + ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows])


def _json_block(value):
    return "```json\n" + json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n```"


def _house_evidence(task):
    imported = task.get("native_import") or {}
    verification = imported.get("verification") or {}
    reload = imported.get("native_reload") or {}
    return {"regions": (task.get("geometry") or {}).get("tables"),
            "native_geometry_verified": imported.get("native_geometry_verified"),
            "house_mapping": verification.get("house_mapping"), "pv_mapping": verification.get("pv_mapping"),
            "native_reload_verified": imported.get("native_reload_verified"),
            "native_reload": reload if reload else None}


def _summary(task, handoff, manifest_name, comparison=None, delivery_review_required=False, house_runs=()):
    metrics = task.get("metrics") or {}
    storage = any(key in metrics for key in ("grid_import_kwh", "load_energy_kwh", "battery_charge_kwh", "initial_soc"))
    if storage:
        names = (("grid_import_kwh", "年电网购电", "kWh"), ("grid_export_kwh", "年上网电量", "kWh"),
                 ("load_energy_kwh", "年用户负荷", "kWh"), ("unmet_load_kwh", "年未供负荷", "kWh"),
                 ("battery_charge_kwh", "年电池侧充电", "kWh"), ("battery_discharge_kwh", "年电池侧放电", "kWh"),
                 ("unused_energy_kwh", "年未用电量", "kWh"))
    else:
        names = (("grid_energy_kwh", "年并网电量", "kWh"), ("array_energy_kwh", "年阵列有效电量", "kWh"),
                 ("clipping_loss_kwh", "逆变器超额功率损失 IL_Pmax", "kWh"),
                 ("pr_percent", "标准 PR", "%"), ("nominal_dc_power_kw", "铭牌直流容量", "kWp"),
                 ("incident_irradiation_kwh_m2", "阵列面年辐照", "kWh/m²"))
    metric_rows = [(label, metrics[key], unit) for key, label, unit in names if key in metrics]
    if storage:
        metric_rows.extend([(label, metrics[key], "ratio") for key, label in
                            (("initial_soc", "首小时SOC"), ("soc_begin_min", "小时开始SOC最小值"),
                             ("soc_begin_max", "小时开始SOC最大值"), ("soc_end_min", "小时结束SOC最小值"),
                             ("soc_end_max", "小时结束SOC最大值")) if key in metrics])
    changes = task.get("changes") or []
    warnings = list(task.get("warnings") or []) + list(handoff.get("review_items") or [])
    if comparison:
        warnings.extend(comparison.get("review_items") or [])
    status = "原生年度任务完成，仍需复核" if task["status"] == "needs_review" else "原生年度任务完成"
    if delivery_review_required and task["status"] != "needs_review":
        status += "；交付内容仍需复核"
    lines = ["# 工程交付摘要", "", "**" + status + "**", "",
             _table(("项目", "记录"), [("工程 / 方案", task["working_project"] + " / " + task["working_variant"]),
                    ("运行年份", task.get("year")), ("运行清单", manifest_name),
                    ("任务状态 / 阶段", task["status"] + " / " + task["stage"]),
                    ("运行清单记录的原生消费验证", "已记录为通过" if task.get("native_consumption_verified") is True else "未记录为通过")]),
             "", "## 实际变更", ""]
    lines.append(_table(("项目", "修改前", "修改后", "原因"),
                        [(row.get("field"), row.get("before"), row.get("after"), row.get("reason", "调用方明确要求"))
                         for row in changes[:6]]) if changes else "本任务没有记录参数变更；以 inputs.md 中的实际保存输入为准。")
    lines.extend(["", "## 年度结果", "",
                  _table(("指标", "原生结果汇总", "单位"), metric_rows) if metric_rows else "运行清单未提供上述年度指标。"])
    if storage:
        lines.extend(["", "电池充放电按原生电池侧导出量展示，不能直接当作AC侧功率限值；SOC采用0..1分数。"])
        if metrics.get("missing_metrics"):
            lines.append("未导出的指标保留未记录：" + _cell(metrics["missing_metrics"]) + "。")
    for house in house_runs or ([task] if task.get("operation") == "engineering_house" else []):
        evidence = _house_evidence(house)
        lines.extend(["", "## 住宅保存与重载记录", "",
            "工程/方案：" + _cell(house["working_project"] + "/" + house["working_variant"]), "",
            _table(("记录项", "本次运行清单记录"), [
                ("PV区域", evidence["regions"]), ("原生保存几何已验证", evidence["native_geometry_verified"]),
                ("住宅物体映射", evidence["house_mapping"]), ("PV表映射", evidence["pv_mapping"]),
                ("原生GUI重载已验证", evidence["native_reload_verified"])]),
            "", "以上仅转述该次运行记录；未记录的映射或重载项保持未记录。实际证据和重载有效性见 inputs.md。",
            "包内所选原生VC保留其内嵌几何，另附绑定的场景、SFI和原始任务清单；接收方仍需准备合法的组件及天气依赖。"])
    if comparison:
        lines.extend(["", "## 方案比较", "", "比较记录状态：" + _cell(comparison["status"]), ""])
        lines.extend("- " + _cell(item) for item in (comparison.get("summary") or [])[:4])
        deltas = []
        for row in comparison.get("metrics") or []:
            order = (("EFrGrid", "电网购电"), ("E_Miss", "未供负荷"), ("EBatCh", "电池侧充电"),
                     ("EBatDis", "电池侧放电"), ("E_Grid", "上网电量"), ("E_User", "用户负荷"),
                     ("EUnused", "未用电量")) if storage else (("E_Grid", "并网电量"),)
            for column, label in order:
                energy = row.get("summary", {}).get(column)
                if isinstance(energy, dict):
                    deltas.append((row.get("baseline_label"), row.get("target_label"), label, energy.get("delta_energy_kwh")))
        if deltas:
            lines.extend(["", _table(("基准", "候选", "指标", "候选减基准的完整电量差(kWh)"), deltas)])
        lines.extend(["", "各方案的明确变更、关键时段和限制见 comparison.md；逐项数据见 comparison.csv，工程与原生结果已一并收录。"])
    lines.extend(["", "## 交接事项", ""])
    lines.extend("- " + _cell(item) for item in warnings[:5])
    if not warnings:
        lines.append("- 本次任务未记录额外警告。")
    if handoff.get("missing_items"):
        lines.append("- 输入交接仍有 " + str(len(handoff["missing_items"])) + " 项补件/依据事项，详见 inputs.md。")
    lines.extend(["- 包内含各收录方案的原生 PRJ、选中 VC、CSV 和 PDF；设备模型、气象、负荷等外部依赖按 inputs.md 另行准备。",
                  "- 打包成功表示成果已整理；工程依据和任务复核状态沿用运行清单。完整变更、警告及输入说明见 inputs.md。", ""])
    return "\n".join(lines)


def _inputs(handoff, task):
    saved = handoff["saved"]
    lines = ["# 工程输入交接", "", "工程：" + _cell(handoff["project"]) + "；方案：" + _cell(handoff["variant"]),
             "", "以下为该次运行记录的保存输入。声明资料单独列示；软件默认值与继承值不自动成为现场或厂家依据。",
             "", "## 设备、数量与朝向分配", ""]
    rows = []
    for row in saved.get("subarrays", []):
        values, refs = row.get("values", {}), row.get("references", {})
        rows.append((row["id"], refs.get("PAN"), refs.get("OND"), values.get("NModSerie"),
                     values.get("NStringCh"), row.get("declared_module_count"), values.get("NInverter"), values.get("NInvMPPT"), values.get("NoOrientation")))
    lines.extend([_table(("子阵", "保存组件引用", "保存逆变器引用", "串联组件数", "并联串数", "声明组件数(串联×并联)", "逆变器数", "保存NInvMPPT", "朝向ID"), rows),
                  "", "## 保存朝向与跟踪参数", "",
                  _table(("朝向ID", "保存值"), [(row["id"], row.get("values")) for row in saved.get("orientations", [])]),
                  "", "## 声明资料", ""])
    declarations = handoff.get("declarations") or {}
    lines.append(_table(("声明项", "调用方提供的值、单位与来源"), declarations.items()) if declarations else "本次未提供额外工程声明。")
    lines.extend(["", "## 项目继承值及原生系统保存值", "", _json_block({"project": saved.get("project"),
                  "system": saved.get("system"), "main_array": saved.get("main_array")}),
                  "", "## 站点、天气、负荷与储能", "", _json_block({key: saved.get(key) for key in ("sources", "load", "storage")}),
                  "", "## 保存的MPPT与电路对应", "", _json_block(saved.get("grid_circuit")),
                  "", "## 未打包的外部依赖", "",
                  _table(("类别", "引用", "解析状态", "需要准备或确认"),
                         [(row.get("kind"), row.get("name"), row.get("resolution"), row.get("required_action", "在接收方PVsyst环境中准备合法可用的相同依赖"))
                          for row in handoff.get("external_dependencies", [])]),
                  "", "## 缺件与补件事项", "",
                  _table(("项目", "原因", "下一步"), [(row.get("field"), row.get("reason"), row.get("action"))
                         for row in handoff.get("missing_items", [])]),
                  "", "## 完整变更记录", "", _json_block(task.get("changes") or []),
                  "", "## 保留的复核事项", ""])
    lines.extend("- " + _cell(item) for item in list(task.get("warnings") or []) + list(handoff.get("review_items") or []))
    if task.get("operation") == "engineering_house":
        lines.extend(["", "## 住宅屋面映射、保存与重载证据", "", _json_block(_house_evidence(task)),
            "", "## 随包提供的受控住宅复演资料", "",
            _table(("类别", "目录", "文件名"), [(kind, task["input_assets"][kind]["folder"],
                task["input_assets"][kind]["name"]) for kind in ("scene", "sfi")]
                + [("原始任务清单", "Results", Path(task["artifacts"]["manifest"]).name)]),
            "", "这些文件是该次运行绑定的原文件。清单中的本机绝对路径作为历史记录保留；在接收方建立工作区时需按包内目录映射，"
            "不据此读取包外路径。商业组件、站点及气象原件不随包分发；缺少这些依赖时不能声称已复演。"])
    lines.extend(["", "本交接整理保存状态与运行清单，不观察未保存GUI状态，不认证资料真实性、设备拟合或全工况工程设计。", ""])
    return "\n".join(lines)


def _result_path(store, value, extension):
    if not isinstance(value, str):
        raise ValueError("Run artifact must be a controlled workspace filename or absolute Results path")
    given = Path(value)
    if not given.is_absolute() and given.name != value:
        raise ValueError("Relative run artifacts must be filenames without directories")
    name = safe_filename(given.name, extension)
    expected = workspace_file(store.workspace, "Results", name, extension)
    if given.is_absolute() and given.resolve() != expected:
        raise ValueError("Run artifact is outside the controlled Results folder")
    if not expected.is_file():
        raise FileNotFoundError(name)
    return expected


def _stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _saved_inputs(saved):
    saved = deepcopy(saved)
    if isinstance(saved, dict):
        for row in saved.get("load", {}).get("objects", []):
            if isinstance(row, dict) and isinstance(row.get("hourly_source"), dict):
                # Older run records included this workspace-location observation among saved inputs.
                row["hourly_source"].pop("path_matches_workspace", None)
    return saved


def _comparison_report(store, comparison_name, load_run):
    safe_filename(comparison_name, ".json")
    path = _result_path(store, comparison_name, ".json")
    with path.open("rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("Comparison report exceeds 32 MiB")
    report = json.loads(data)
    if (not isinstance(report, dict) or report.get("operation") != "engineering_scenario_comparison"
            or report.get("dry_run") is not False or report.get("status") not in ("created", "needs_review")
            or report.get("historical_input_binding_verified") is not True
            or not isinstance(report.get("written_artifacts"), list)
            or len(report["written_artifacts"]) != 3
            or set(report["written_artifacts"]) != {"csv", "markdown", "json"}
            or not isinstance(report.get("scenarios"), list) or not 2 <= len(report["scenarios"]) <= 3
            or any(not isinstance(report.get(key), list) for key in ("summary", "metrics", "review_items"))
            or any(not isinstance(item, str) for item in report["summary"])
            or any(not isinstance(item, dict) or not isinstance(item.get("summary"), dict) for item in report["metrics"])
            or not isinstance(report.get("report_files"), dict)
            or set(report["report_files"]) != {"csv", "markdown"}):
        raise ValueError("Require a written comparison with complete reports and bound native-run inputs")
    files = {key: _result_path(store, report["artifacts"][key], extension)
             for key, extension in (("json", ".json"), ("csv", ".csv"), ("markdown", ".md"))}
    if (files["json"] != path or files["csv"] != path.with_suffix(".csv")
            or files["markdown"] != path.with_suffix(".md")):
        raise ValueError("Comparison artifacts must identify this report and its matching CSV/Markdown files")
    if any(fingerprint(files[key]) != report["report_files"][key] for key in ("csv", "markdown")):
        raise ValueError("Comparison CSV or Markdown changed after it was recorded")
    bindings, manifests = [], set()
    for scenario in report["scenarios"]:
        if not isinstance(scenario, dict):
            raise ValueError("Comparison scenario must contain a native-run binding")
        manifest = safe_filename(scenario["run_manifest_name"], ".json")
        if manifest.casefold() in manifests:
            raise ValueError("Comparison scenarios must refer to distinct native runs")
        manifests.add(manifest.casefold())
        project, variant = project_name(scenario["project"]), variant_id(scenario["variant"])
        csv_name = safe_filename(scenario["csv_name"], ".csv")
        task = load_run(store, manifest, project=project, variant=variant, csv_name=csv_name)
        if (not isinstance(scenario.get("input_handoff"), dict)
                or _saved_inputs(scenario["input_handoff"].get("saved")) != _saved_inputs(task["input_handoff"].get("saved"))):
            raise ValueError("Comparison scenario inputs differ from its recorded native run")
        bindings.append({"run_manifest_name": manifest, "project": project, "variant": variant,
                         "csv_name": csv_name, "label": scenario.get("label", project + "/" + variant), "task": task})
    if (path.read_bytes() != data
            or any(fingerprint(files[key]) != report["report_files"][key] for key in ("csv", "markdown"))):
        raise ValueError("Comparison report changed while its native-run bindings were checked")
    return report, files, data, bindings


def export_engineering_delivery(store, *, manifest_name, output_name, dry_run=True, comparison_name=""):
    """Export a bound native run and an optional recorded comparison without changing their review states."""
    result = {"status": "blocked", "dry_run": dry_run, "issues": [], "included_files": [],
              "external_dependencies": [], "native_consumption_verified": False,
              "task_native_consumption_verified": False, "artifact": None}
    writing = False
    with store.lock:
        try:
            if type(dry_run) is not bool:
                raise ValueError("Use a strict dry_run boolean")
            if not isinstance(comparison_name, str):
                raise ValueError("comparison_name must be an optional Results JSON filename")
            safe_filename(manifest_name, ".json")
            output = workspace_file(store.workspace, "Results", output_name, ".zip")
            if output.name.casefold() != output_name.casefold():
                raise ValueError("Delivery filename resolves to a different file")
            if output.exists():
                raise FileExistsError("Delivery already exists; use a new output_name")
            # Local import keeps the run producer independent of its delivery consumer.
            from pvsyst_auto.workflows.revision import _asset_path, load_engineering_run
            comparison, comparison_files, comparison_bytes = None, {}, None
            if comparison_name:
                comparison, comparison_files, comparison_bytes, bindings = _comparison_report(
                    store, comparison_name, load_engineering_run)
                primary = next((item for item in bindings
                                if item["run_manifest_name"].casefold() == manifest_name.casefold()), None)
                if primary is None:
                    raise ValueError("The explicit comparison must include the requested primary native-run manifest")
                task = primary["task"]
            else:
                task = load_engineering_run(store, manifest_name)
                bindings = [{"run_manifest_name": manifest_name, "project": task["working_project"],
                             "variant": task["working_variant"], "csv_name": Path(task["artifacts"]["csv"]).name,
                             "label": task["working_project"] + "/" + task["working_variant"], "task": task}]
            sources, payloads, external_dependencies = {}, {}, []
            for binding in bindings:
                run = binding["task"]
                project, variant = project_name(run["working_project"]), variant_id(run["working_variant"])
                if run.get("status") not in ("completed", "needs_review") or run.get("stage") != "completed":
                    raise ValueError("Delivery requires completed engineering runs; their review states are retained")
                handoff = run["input_handoff"]
                if (not isinstance(handoff, dict) or handoff.get("project", "").casefold() != project.casefold()
                        or handoff.get("variant", "").upper() != variant):
                    raise ValueError("Runtime input handoff does not identify this working project and variant")
                current = build_project_handoff(store, project=project, variant=variant, declarations=handoff.get("declarations"))
                if current["status"] == "blocked" or _saved_inputs(current["saved"]) != _saved_inputs(handoff.get("saved")):
                    raise ValueError("Saved inputs differ from the run-time handoff; no mismatched delivery was created")
                project_path, variant_path = store._path(project), store._path(project, variant)
                csv_path = _result_path(store, run["artifacts"]["csv"], ".csv")
                pdf_path = _result_path(store, run["artifacts"]["pdf"], ".pdf")
                files = [("Projects", project_path), ("Projects", variant_path), ("Results", csv_path), ("Results", pdf_path)]
                if run.get("operation") == "engineering_house":
                    files.append(("Results", _result_path(store, run["artifacts"]["manifest"], ".json")))
                    for kind in ("scene", "sfi"):
                        asset = run["input_assets"][kind]
                        files.append((asset["folder"], _asset_path(store, asset)))
                for folder, path in files:
                    name = folder + "/" + path.name
                    existing = next((key for key in sources if key.casefold() == name.casefold()), None)
                    if existing is not None:
                        if sources[existing] != path:
                            raise ValueError("Different scenario files have conflicting delivery names")
                        continue
                    sources[name] = path
                    if folder == "Projects":
                        payloads[name] = store._read(path)
                    elif (run.get("operation") == "engineering_house"
                          and name.casefold() == ("Results/" + binding["run_manifest_name"]).casefold()):
                        raw_manifest = path.read_bytes()
                        if json.loads(raw_manifest) != run:
                            raise ValueError("House task manifest changed before it was packaged")
                        payloads[name] = raw_manifest
                for dependency in handoff.get("external_dependencies", []):
                    if dependency not in external_dependencies:
                        external_dependencies.append(dependency)
            if comparison:
                sources.update({"comparison.json": comparison_files["json"], "comparison.csv": comparison_files["csv"],
                                "comparison.md": comparison_files["markdown"]})
                payloads["comparison.json"] = comparison_bytes
            before = {name: _stamp(path) for name, path in sources.items()}
            handoff = task["input_handoff"]
            review = (any(item["task"]["status"] == "needs_review" or item["task"]["input_handoff"].get("status") == "needs_review"
                          for item in bindings)
                      or bool(comparison and (comparison["status"] == "needs_review" or comparison.get("review_items"))))
            summary = _summary(task, handoff, manifest_name, comparison, review,
                house_runs=[binding["task"] for binding in bindings if binding["task"].get("operation") == "engineering_house"])
            inputs = _inputs(handoff, task)
            documented = {manifest_name.casefold()}
            for binding in bindings:
                if binding["run_manifest_name"].casefold() not in documented:
                    inputs += "\n---\n\n" + _inputs(binding["task"]["input_handoff"], binding["task"])
                    documented.add(binding["run_manifest_name"].casefold())
            result.update(status="ready", task_status=task["status"], task_stage=task["stage"],
                          task_native_consumption_verified=task.get("native_consumption_verified") is True,
                          included_files=["summary.md", "inputs.md", *sources],
                          external_dependencies=external_dependencies, review_required=review,
                          comparison_status=comparison["status"] if comparison else None,
                          comparison_name=comparison_name,
                          scenarios=[{key: value for key, value in binding.items() if key != "task"} | {"task_status": binding["task"]["status"]}
                                     for binding in bindings],
                          artifact={"zip": str(output)}, summary_markdown=summary, inputs_markdown=inputs,
                          source_manifest=manifest_name,
                          scope="Recorded engineering runs and an optional bound comparison; selected variants retain embedded geometry. "
                                "House runs also include their bound scene, SFI and original manifest; model, weather and load files are external dependencies")
            if dry_run:
                return result
            output.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                writing = True
                archive.writestr("summary.md", summary)
                archive.writestr("inputs.md", inputs)
                for name, path in sources.items():
                    if name in payloads:
                        archive.writestr(name, payloads[name])
                    else:
                        archive.write(path, name)
            if (any(_stamp(path) != before[name] for name, path in sources.items())
                    or any(path.read_bytes() != payloads[name] for name, path in sources.items() if name in payloads)):
                raise ValueError("Delivery inputs changed while packaging; retained ZIP is not accepted for delivery")
            if comparison and any(fingerprint(comparison_files[key]) != comparison["report_files"][key] for key in ("csv", "markdown")):
                raise ValueError("Comparison report changed while packaging; retained ZIP is not accepted for delivery")
            for binding in bindings:
                checked = load_engineering_run(store, binding["run_manifest_name"], project=binding["project"],
                                               variant=binding["variant"], csv_name=binding["csv_name"])
                if checked != binding["task"]:
                    raise ValueError("Native-run metadata changed while packaging; retained ZIP is not accepted for delivery")
            result["status"] = "exported"
            return result
        except (ValueError, OSError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile) as exc:
            result.update(status="failed" if writing else "blocked", issues=[str(exc)])
            if writing:
                result["retained_artifact"] = result["artifact"]
                result["recovery"] = "保留本次ZIP供查看；解决输入变化或写入问题后，使用新的output_name重新导出。"
            return result

"""Compare declared engineering scenarios using existing native result files."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
import io
import json
import math
from pathlib import Path, PureWindowsPath
import re

from pvsyst_auto.native.cli import iter_result_csv, result_units
from pvsyst_auto.project.fileio import write_json
from pvsyst_auto.project.paths import project_name, safe_filename, variant_id, workspace_file
from pvsyst_auto.results import compare_results, fingerprint


GROUPS = ("project", "system", "main_array", "orientations", "subarrays", "grid_circuit",
          "sources", "load", "storage", "shading_scene")
VARIABLES = {
    "E_Grid": "原生上网功率", "EToGrid": "原生上网功率（EToGrid）",
    "EFrGrid": "电网购电功率", "E_User": "用户负荷", "E_Miss": "未供负荷",
    "EArray": "阵列有效功率", "E_Avail": "原生可用功率",
    "EBatCh": "电池DC侧充电功率（EBatCh）", "EBatDis": "电池DC侧放电功率（EBatDis）",
    "EUseful": "原生有效供能（EUseful）", "EDirUse": "原生直接能量通道（EDirUse）",
    "E_Solar": "原生太阳能供能（E_Solar）", "SolFrac": "原生太阳能供给比例（SolFrac）",
    "EUnused": "原生未利用功率", "CL_Chrg": "充电器损失", "CL_InvB": "电池逆变器损失",
    "CL_Over": "原生过载损失", "IL_Pmax": "逆变器超功率损失",
    "SOC": "原生SOC", "SOC_Beg": "时段初SOC", "SOC_End": "时段末SOC",
    "GlobInc": "组件面辐照", "GlobHor": "水平总辐照", "DiffHor": "水平散射辐照",
    "T_Amb": "环境温度", "PR": "原生PR",
}
RUN_FIELDS = {"working_project", "working_variant", "year", "input_handoff", "working_files",
              "input_assets", "artifacts", "native_consumption_verified", "status", "stage", "result_files"}
CONTEXT_FIELDS = ("Project", "Simulation variant", "Geographical Site", "Weather data")


def _text(value, field, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{field} must be nonempty text of at most {limit} characters")
    return value.strip()


def _native_context(path):
    context = {}
    consumed = 0
    with path.open(encoding="utf-8-sig", errors="strict", newline="") as stream:
        for line in stream:
            consumed += len(line)
            if consumed > 65536:
                raise ValueError("Native CSV context exceeds 64 KiB")
            if re.match(r"^\s*date[;,]", line, re.I):
                break
            match = re.match(r"^\s*(Project|Simulation variant|Geographical Site|Weather data)\s*([;,])", line)
            if match:
                row = next(csv.reader([line], delimiter=match[2]))
                key = row[0].strip()
                if len(row) < 2 or key in context or not row[1].strip():
                    raise ValueError("Missing or duplicate native CSV context: " + key)
                context[key] = row[1].strip()
    if set(context) != set(CONTEXT_FIELDS):
        raise ValueError("Native CSV must identify project, variant, SIT and MET")
    safe_filename(context["Geographical Site"], ".SIT")
    safe_filename(context["Weather data"], ".MET")
    return context


def _conditions(handoff):
    saved = handoff.get("saved")
    if not isinstance(saved, dict):
        raise ValueError("Project handoff has no saved input conditions")
    values = {key: saved.get(key) for key in GROUPS}
    # Display names change when a scenario is cloned; the saved project design fields still matter.
    project = values.get("project")
    values["project"] = project.get("values", {}) if isinstance(project, dict) else project
    return values


def _differences(before, after, prefix=""):
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in after:
                yield {"field": path, "baseline": before.get(key), "target": after.get(key),
                       "baseline_present": key in before, "target_present": key in after}
            else:
                yield from _differences(before[key], after[key], path)
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for index, (left, right) in enumerate(zip(before, after)):
            yield from _differences(left, right, f"{prefix}.{index}")
    elif before != after:
        yield {"field": prefix, "baseline": before, "target": after,
               "baseline_present": True, "target_present": True}


def _covers(declaration, field):
    return field == declaration or field.startswith(declaration + ".")


def _same_bound_load(before, after):
    if before is None or after is None:
        return False
    bindings = (before.get("load_binding"), after.get("load_binding"))
    if any(not isinstance(item, dict) or item.get("embedded_snapshot_matches") is not True for item in bindings):
        return False
    keys = ("year", "time_basis", "utc_offset_hours", "rows", "normalized_csv_name", "source_csv_name")
    if any(key not in bindings[0] or bindings[0][key] != bindings[1].get(key) for key in keys):
        return False
    for label in ("load_manifest", "load_source", "load_profile"):
        left, right = before["input_assets"].get(label), after["input_assets"].get(label)
        if (not isinstance(left, dict) or not isinstance(right, dict) or not left.get("sha256")
                or any(left.get(key) != right.get(key) for key in ("folder", "name", "library", "sha256"))):
            return False
    return True


def _load_location_difference(difference, before, after):
    match = re.fullmatch(r"load\.objects\.(\d+)\.(values\.PathName|hourly_source\.(?:saved_path|path_matches_workspace))",
                         difference["field"])
    if not match:
        return False
    objects = (before["load"]["objects"][int(match[1])], after["load"]["objects"][int(match[1])])
    names = [item.get("values", {}).get("FileName") for item in objects]
    if not isinstance(names[0], str) or not names[0] or names[0] != names[1]:
        return False
    for item in objects:
        value = item.get("values", {}).get("PathName")
        if not isinstance(value, str):
            return False
        path = PureWindowsPath(value)
        if not path.is_absolute() or path.name.casefold() != "userhourlyparams":
            return False
        source = item.get("hourly_source", {})
        if source and (source.get("saved_path") != value or source.get("file_name") != names[0]):
            return False
    return True


def _run_binding(store, scenario, reviews):
    name = scenario.get("run_manifest_name")
    if not name:
        reviews.append(f"{scenario['label']}：未提供运行清单，历史输入绑定未验证；当前工程状态不代表仿真时状态。")
        return None
    path = workspace_file(store.workspace, "Results", name, ".json")
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Run manifest exceeds 8 MiB")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("Run manifest must be a JSON object")
    if record.get("operation") not in ("engineering_revision", "engineering_house") or not RUN_FIELDS.issubset(record):
        reviews.append(f"{scenario['label']}：旧运行清单缺少完整输入/结果绑定，只提供数值对照。")
        return None
    from pvsyst_auto.workflows.revision import load_engineering_run
    return load_engineering_run(store, name, project=scenario["project"],
                                variant=scenario["variant"], csv_name=scenario["csv_name"])


def _scan_result(path, units, year):
    selected = [name for name in VARIABLES if name in units]
    if not any(name.startswith("E") for name in selected):
        raise ValueError("Native CSV contains no supported energy/power result column")
    for name in selected:
        if ((name.startswith(("E", "CL_", "IL_")) and units[name] not in ("W", "kW"))
                or ((name.startswith("SOC") or name == "SolFrac") and units[name] not in ("ratio", "%"))):
            raise ValueError(f"{name}: unsupported native unit {units[name]!r}")
    stats = {name: {"unit": units[name], "samples": 0, "sum": 0.0, "min": None, "max": None,
                    "min_at": None, "max_at": None} for name in selected}
    first = last = previous = step = None
    regular = True
    indices = None
    count = 0
    for headers, row in iter_result_csv(path, strict=True):
        if indices is None:
            if len(headers) != len(set(headers)) or any(not units[name] for name in selected):
                raise ValueError("Native CSV requires unique columns and explicit units")
            indices = {name: headers.index(name) for name in selected}
            first = row[0]
        stamp = row[0]
        if year is not None and stamp.year != year:
            raise ValueError("Native CSV contains timestamps outside the declared scenario year")
        if previous is not None:
            delta = (stamp - previous).total_seconds() / 60
            if delta <= 0:
                raise ValueError("Scenario timestamps must be strictly increasing and unique")
            if step is None:
                step = delta
            elif step != delta:
                regular = False
        previous = last = stamp
        count += 1
        for name, index in indices.items():
            value = row[index]
            if count == 1:
                stats[name]["first_value"] = value if math.isfinite(value) else None
            if not math.isfinite(value):
                continue
            state = stats[name]
            state["samples"] += 1
            state["sum"] += value
            if not math.isfinite(state["sum"]):
                raise ValueError("Scenario result arithmetic exceeds finite range")
            for extremum, direction in (("min", -1), ("max", 1)):
                if state[extremum] is None or direction * value > direction * state[extremum]:
                    state[extremum], state[extremum + "_at"] = value, str(stamp)
    step = step if regular else None
    for state in stats.values():
        samples = state["samples"]
        scale = {"W": 0.001, "kW": 1}.get(state["unit"])
        observed = state["sum"] * step / 60 * scale if samples and step is not None and scale else None
        if observed is not None and not math.isfinite(observed):
            raise ValueError("Scenario energy integral exceeds finite range")
        state.update(missing_samples=count - samples, coverage=samples / count, complete=samples == count,
                     mean=state["sum"] / samples if samples else None,
                     observed_energy_kwh=observed, energy_kwh=observed if samples == count else None)
        if not samples:
            state["sum"] = None
        if state["unit"] in ("ratio", "%"):
            state.pop("sum")
    end = datetime(first.year + 1, 1, 1)
    complete_year = (first == datetime(first.year, 1, 1) and step == 60
                     and last == end - timedelta(hours=1)
                     and count == int((end - first).total_seconds() / 3600))
    return {"rows": count, "first": str(first), "last": str(last), "step_minutes": step,
            "complete_year": complete_year, "observed_year": first.year, "columns": stats}


def _explanations(result):
    lines = [f"比较目的：{result['objective']}。结果只描述给定原生输出的差异，不自动判断因果或最优方案。"]
    for index, item in enumerate(result["metrics"], start=1):
        baseline = result["scenarios"][0]["result_summary"]["columns"]
        target = result["scenarios"][index]["result_summary"]["columns"]
        lines.append(f"{item['target_label']} 相对 {item['baseline_label']}：{item['rows']}个配对时段，"
                     f"{item['first']} 至 {item['last']}。")
        for name in ("E_Grid", "EToGrid", "EFrGrid", "E_User", "E_Miss", "EArray", "E_Avail",
                     "EUseful", "EDirUse", "E_Solar", "EBatCh", "EBatDis", "EUnused", "IL_Pmax"):
            state = item["summary"].get(name)
            if state is None:
                continue
            energy = state["delta_energy_kwh"]
            if energy is not None:
                direction = "增加" if energy > 0 else "减少" if energy < 0 else "不变"
                lines.append(f"{VARIABLES[name]}对应时段电量：基准{baseline[name]['energy_kwh']:.6g} kWh，"
                             f"候选{target[name]['energy_kwh']:.6g} kWh，{direction}，"
                             f"差值{energy:.6g} kWh（候选减基准）。")
            else:
                lines.append(f"{VARIABLES[name]}：完整差值电量不可用；配对覆盖率{state['paired_coverage']:.2%}，"
                             f"原生单位{state['unit']}。缺测不按0处理。")
        for name, state in item["soc_changes"].items():
            label = {"SOC_Beg": "首时段开始SOC", "SOC_End": "首时段结束SOC（不是开始SOC）",
                     "SOC": "首个原生SOC样本"}[name]
            for field, description in (("first_value", label), ("min", "各自时段最低SOC"), ("max", "各自时段最高SOC")):
                values = state[field]
                if values["delta"] is None:
                    lines.append(f"{name} {description}：至少一方缺失，不推断数值。")
                else:
                    lines.append(f"{name} {description}：基准{values['baseline']:.6g}、候选{values['target']:.6g}、"
                                 f"差值{values['delta']:.6g}（原生单位{state['unit']}）。")
        fraction = item["summary"].get("SolFrac")
        if fraction is not None and fraction["mean_delta"] is not None:
            lines.append(f"原生SolFrac配对样本平均差{fraction['mean_delta']:.6g} {fraction['unit']}；"
                         "这是所给样本比例的平均差，不是按年电量重算的太阳能供给比例。")
        unmet = target.get("E_Miss")
        if unmet is not None and unmet["energy_kwh"] is not None:
            if unmet["energy_kwh"] > 0:
                lines.append(f"候选仍有{unmet['energy_kwh']:.6g} kWh原生未供负荷；"
                             "不能因上网或电池放电增加就认定供电需求已满足。")
            elif unmet["energy_kwh"] == 0:
                lines.append("候选在所给时段的原生未供负荷积分为0；该结果的范围限于本次输入和时段。")
        if any(name in item["summary"] for name in ("EUseful", "EDirUse", "E_Solar")):
            lines.append("阵列输出、直接利用、有效供能和太阳能供能是不同原生出口；"
                         "不把它们或电池DC放电量相加作为负荷供给，也不从上网变化推算自用变化。")
    if not result["historical_input_binding_verified"]:
        lines.append("至少一项未验证历史输入绑定；保存条件差异只作当前状态提示，不能解释为该次原生运行的因果。")
    lines.append("EDirUse及EUseful沿用原生通道定义，不能自动等同直接供用户负荷或用户自用率。")
    lines.append("购电、上网、未供负荷及原生供能列分别报告，不互相抵消；EBatCh/EBatDis为电池DC侧输出，"
                 "不代表AC充电器输入或电池逆变器输出额定功率。SOC为原生观测状态量，不换算为可用电量；"
                 "最低/最高值不证明任意时段触及配置限值。")
    return lines


def _markdown(result):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = ["# 工程方案对比", "", *[cell(line) for line in result["summary"]], "",
             "| 方案 | 工程/变体 | 原生CSV | 历史绑定 | 行数 | 全年时间轴 |",
             "|---|---|---|---|---:|---|"]
    for item in result["scenarios"]:
        period = item["result_summary"]
        lines.append("| " + " | ".join(cell(value) for value in (item["label"],
            item["project"] + "/" + item["variant"], item["csv_name"],
            item["historical_input_binding_verified"], period["rows"], period["complete_year"])) + " |")
    lines.extend(["", "## 条件与声明改动", ""])
    for item in result["changes"]:
        lines.append(f"- {cell(item['target_label'])}：声明改动 {cell(', '.join(item['declared_changes']))}；"
                     f"共同条件 {cell(', '.join(item['common_groups']))}；"
                     f"未声明差异 {cell(', '.join(item['undeclared_fields']) or '无')}。")
        if item["normalized_location_differences"]:
            lines.append("- 已核对相同负荷资料、逐时profile与原生内嵌快照；仅将以下保存路径位置差异单列："
                         + cell(", ".join(value["field"] for value in item["normalized_location_differences"])) + "。")
    lines.extend(["", "## 原生指标", "", "| 方案 | 变量 | 单位 | 有限样本/行数 | 时段电量kWh | 首时段值 | 最小值 | 最大值 |",
                  "|---|---|---|---:|---:|---:|---:|---:|"])
    for item in result["scenarios"]:
        for name, state in item["result_summary"]["columns"].items():
            values = (item["label"], name, state["unit"], f"{state['samples']}/{item['result_summary']['rows']}",
                      state["energy_kwh"], state["first_value"], state["min"], state["max"])
            lines.append("| " + " | ".join("缺失/不可积分" if value is None else cell(value) for value in values) + " |")
    lines.extend(["", "## 最大逐时差异", "", "| 候选 | 变量 | 时间 | 基准 | 候选 | 差值 | 单位 |",
                  "|---|---|---|---:|---:|---:|---|"])
    for item in result["metrics"]:
        for point in item["key_periods"]:
            lines.append("| " + " | ".join(cell(value) for value in (item["target_label"], point["column"],
                point["timestamp"], point["baseline"], point["target"], point["delta"], point["unit"])) + " |")
    lines.extend(["", "## 假设与保留复核", ""])
    for item in result["scenarios"]:
        lines.extend(f"- {cell(item['label'])} 假设：{cell(text)}" for text in item["assumptions"])
    lines.extend("- " + cell(text) for text in result["review_items"])
    lines.extend(["", "本报告不执行仿真，不认证工程、来源或调度模型；完整保存条件、引用和缺失列见同名JSON。", ""])
    return "\n".join(lines)


def _comparison_csv(result):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["kind", "scenario", "baseline", "column", "unit", "rows", "finite_pairs_or_samples",
                     "coverage", "energy_kwh", "observed_energy_kwh", "min_or_min_delta", "max_or_max_delta", "first_value"])

    def safe(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    for item in result["scenarios"]:
        for name, state in item["result_summary"]["columns"].items():
            writer.writerow([safe(value) for value in ("scenario", item["label"], "", name, state["unit"],
                item["result_summary"]["rows"], state["samples"], state["coverage"], state["energy_kwh"],
                state["observed_energy_kwh"], state["min"], state["max"], state["first_value"])])
    for item in result["metrics"]:
        for name, state in item["summary"].items():
            writer.writerow([safe(value) for value in ("target_minus_baseline", item["target_label"],
                item["baseline_label"], name, state["unit"], item["rows"], state["paired_samples"],
                state["paired_coverage"], state["delta_energy_kwh"], state["observed_delta_energy_kwh"],
                state["min_delta"], state["max_delta"], "")])
    return stream.getvalue()


def compare_engineering_scenarios(store, *, objective, scenarios, output_name, dry_run=True):
    """Preview or export two/three explicit scenarios; never run or alter a native model."""
    result = {"operation": "engineering_scenario_comparison", "status": "blocked", "dry_run": dry_run,
              "objective": objective, "summary": [], "scenarios": [], "changes": [], "metrics": [],
              "review_items": [], "issues": [], "artifacts": {}, "report_files": {}, "written_artifacts": [],
              "historical_input_binding_verified": False, "native_consumption_verified": False,
              "created": False}
    active_output = None
    with store.lock:
        try:
            from pvsyst_auto.engineering.handoff import build_project_handoff
            objective = result["objective"] = _text(objective, "objective")
            if type(dry_run) is not bool:
                raise ValueError("dry_run must be a strict boolean")
            if not isinstance(scenarios, list) or not 2 <= len(scenarios) <= 3:
                raise ValueError("Provide exactly two or three scenarios")
            safe_filename(output_name, ".json")
            stem = Path(output_name).stem
            paths = {kind: workspace_file(store.workspace, "Results", name, extension)
                     for kind, name, extension in (("json", output_name, ".json"),
                         ("csv", stem + ".csv", ".csv"), ("markdown", stem + ".md", ".md"))}
            if any(path.exists() for path in paths.values()):
                raise ValueError("Comparison outputs must all be new filenames")
            result["artifacts"] = {kind: str(path) for kind, path in paths.items()}
            inputs, bindings, snapshots, conditions, labels, filenames = [], [], [], [], set(), set()
            required = {"label", "project", "variant", "csv_name", "declared_changes"}
            allowed = required | {"year", "nominal_dc_power_kw", "assumptions", "run_manifest_name"}
            for index, item in enumerate(scenarios):
                if not isinstance(item, dict) or not required.issubset(item) or set(item) - allowed:
                    raise ValueError("Each scenario requires explicit label/project/variant/csv_name/declared_changes")
                scenario = dict(item)
                scenario["label"] = label = _text(item["label"], "label", 120)
                if label.casefold() in labels:
                    raise ValueError("Scenario labels must be distinct")
                labels.add(label.casefold())
                project_name(item["project"])
                scenario["variant"] = variant_id(item["variant"])
                path = workspace_file(store.workspace, "Results", item["csv_name"], ".csv")
                if not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
                    raise ValueError("Scenario CSV must exist and be at most 128 MiB")
                if path in filenames:
                    raise ValueError("Each scenario must identify a distinct native CSV")
                filenames.add(path)
                changes = item["declared_changes"]
                if (not isinstance(changes, list) or len(changes) > 32
                        or any(not isinstance(field, str) or len(field) > 160
                               or field.split(".")[0] not in GROUPS
                               or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", field)
                               for field in changes) or len(set(changes)) != len(changes)
                        or (index == 0 and changes)):
                    raise ValueError("declared_changes must name saved condition groups/paths; baseline must be empty")
                year = item.get("year")
                if year is not None and (type(year) is not int or not 1969 <= year <= 2068):
                    raise ValueError("year must be an integer in 1969..2068")
                nominal = item.get("nominal_dc_power_kw")
                if nominal is not None and (isinstance(nominal, bool) or not isinstance(nominal, (int, float))
                        or not math.isfinite(nominal) or nominal <= 0):
                    raise ValueError("nominal_dc_power_kw must be positive and finite")
                assumptions = item.get("assumptions", [])
                if not isinstance(assumptions, list) or len(assumptions) > 20:
                    raise ValueError("assumptions must be a list of at most twenty text declarations")
                scenario["assumptions"] = [_text(value, "assumption") for value in assumptions]
                if item.get("run_manifest_name"):
                    safe_filename(item["run_manifest_name"], ".json")
                snapshots.append(fingerprint(path))
                context = _native_context(path)
                expected = {"Project": item["project"],
                            "Simulation variant": f"{Path(item['project']).stem}.{scenario['variant']}"}
                if any(context[key].casefold() != value.casefold() for key, value in expected.items()):
                    raise ValueError(f"{label}: native CSV project/variant differs from the declared scenario")
                binding = _run_binding(store, scenario, result["review_items"])
                current = build_project_handoff(store, project=item["project"], variant=scenario["variant"])
                historical = binding["input_handoff"] if binding is not None else None
                handoff = historical if historical is not None else current
                saved = _conditions(handoff)
                if binding is not None:
                    source = saved["sources"]["variant"]
                    for key, kind in (("Geographical Site", "site"), ("Weather data", "meteo")):
                        if context[key].casefold() != str(source[kind]["name"]).casefold():
                            raise ValueError(f"{label}: native CSV weather context differs from the bound inputs")
                    if year is not None and binding["year"] != year:
                        raise ValueError(f"{label}: declared year differs from the bound run")
                    year = binding["year"]
                for review in handoff.get("review_items", []):
                    result["review_items"].append(f"{label}：{review}")
                if handoff.get("status") != "ready":
                    result["review_items"].append(f"{label}：工程交接状态为 {handoff.get('status')}，保留其复核/缺项。")
                if binding is not None and binding["status"] == "needs_review":
                    result["review_items"].append(f"{label}：已绑定原生任务保留 needs_review，不升级为正常工程验收。")
                units = result_units(path, strict=True)
                period = _scan_result(path, units, year)
                if not period["complete_year"]:
                    result["review_items"].append(f"{label}：时间轴不是完整小时年，只解释所给时段。")
                if any(not state["complete"] for state in period["columns"].values()):
                    result["review_items"].append(f"{label}：存在缺测；完整电量不可用的列保留观测部分。")
                if nominal is not None:
                    grid = next((name for name in ("E_Grid", "EToGrid") if name in period["columns"]), None)
                    energy = period["columns"][grid]["energy_kwh"] if grid else None
                    specific = energy / nominal if energy is not None else None
                    if specific is not None and not math.isfinite(specific):
                        raise ValueError("Specific period yield exceeds finite numeric range")
                    period["specific_grid_yield"] = {"source_column": grid, "nominal_dc_power_kw": nominal,
                        "kwh_per_kwp": specific,
                        "scope": "Observed period only; caller-declared nameplate capacity, not a sizing certification"}
                scenario.update(native_context=context, historical_input_binding_verified=binding is not None,
                                condition_basis="bound_run_handoff" if binding is not None else "current_saved_state_only",
                                input_handoff=historical, current_handoff=current, result_summary=period)
                if binding is not None:
                    scenario["bound_run"] = {key: binding[key] for key in ("operation", "status", "stage", "year")}
                    if "load_binding" in binding:
                        scenario["bound_run"]["load_binding"] = binding["load_binding"]
                    if binding["operation"] == "engineering_house":
                        imported = binding["native_import"]
                        scenario["bound_run"]["house"] = {
                            "geometry": binding.get("geometry"),
                            "native_geometry_verified": imported.get("native_geometry_verified"),
                            "native_reload_verified": imported.get("native_reload_verified"),
                            "verification": imported.get("verification"), "native_reload": imported.get("native_reload")}
                result["scenarios"].append(scenario)
                inputs.append(path)
                bindings.append(binding)
                conditions.append(saved)
            union = set().union(*(item["result_summary"]["columns"] for item in result["scenarios"]))
            for item in result["scenarios"]:
                item["missing_columns"] = sorted(union - set(item["result_summary"]["columns"]))
                if item["missing_columns"]:
                    result["review_items"].append(f"{item['label']}：原生未提供列 {', '.join(item['missing_columns'])}；不补0。")
            for index in range(1, len(inputs)):
                candidate = result["scenarios"][index]
                same_load = _same_bound_load(bindings[0], bindings[index])
                differences, locations = [], []
                for difference in _differences(conditions[0], conditions[index]):
                    if same_load and _load_location_difference(difference, conditions[0], conditions[index]):
                        locations.append(difference)
                        continue
                    if len(differences) >= 200:
                        raise ValueError("More than 200 saved-condition differences; narrow the scenario before comparison")
                    difference["declared"] = any(_covers(field, difference["field"])
                                                 for field in candidate["declared_changes"])
                    differences.append(difference)
                undeclared = [item["field"] for item in differences if not item["declared"]]
                if undeclared:
                    result["review_items"].append(f"{candidate['label']}：存在未声明保存条件差异，不能将结果变化归于单一改动。")
                for field in candidate["declared_changes"]:
                    if not any(_covers(field, item["field"]) for item in differences):
                        result["review_items"].append(f"{candidate['label']}：声明改动 {field} 未在所用保存条件中显示差异。")
                result["changes"].append({"target_label": candidate["label"],
                    "declared_changes": candidate["declared_changes"], "differences": differences,
                    "undeclared_fields": undeclared,
                    "normalized_location_differences": locations, "load_input_equivalence_verified": same_load,
                    "historically_bound": bindings[0] is not None and bindings[index] is not None,
                    "common_groups": [key for key in GROUPS if conditions[0][key] is not None
                                      and not any(_covers(key, item["field"]) for item in differences)]})
                load_present = any(isinstance(item["load"], dict) and item["load"].get("present")
                                   for item in (conditions[0], conditions[index]))
                if load_present and not same_load:
                    if "load" in result["changes"][-1]["common_groups"]:
                        result["changes"][-1]["common_groups"].remove("load")
                    result["review_items"].append(f"{candidate['label']}：相同逐时负荷输入/缓存未由双方绑定记录确认，"
                                                  "保存字段相同不代表完整负荷相同。")
                if bindings[0] is not None and bindings[index] is not None:
                    assets = list(_differences(bindings[0]["input_assets"], bindings[index]["input_assets"], "input_assets"))
                    result["changes"][-1]["input_asset_differences"] = assets
                    if assets:
                        result["review_items"].append(f"{candidate['label']}：绑定输入资产存在差异，不能将相同文件名当作相同模型/天气。")
                baseline = result["scenarios"][0]
                common = [name for name in VARIABLES if name in baseline["result_summary"]["columns"]
                          and name in candidate["result_summary"]["columns"]]
                if not common:
                    raise ValueError("Scenarios have no common supported native result columns")
                comparison = compare_results(inputs[0], inputs[index], common, include_key_periods=True)
                if (comparison["baseline"]["sha256"] != snapshots[0]
                        or comparison["target"]["sha256"] != snapshots[index]):
                    raise ValueError("Native result changed between context inspection and comparison")
                comparison.update(baseline_label=baseline["label"], target_label=candidate["label"])
                comparison["soc_changes"] = {}
                for name in ("SOC", "SOC_Beg", "SOC_End"):
                    if name not in comparison["summary"]:
                        continue
                    left, right = (item["result_summary"]["columns"][name] for item in (baseline, candidate))
                    values = {"unit": left["unit"]}
                    for field in ("first_value", "min", "max"):
                        a, b = left[field], right[field]
                        values[field] = {"baseline": a, "target": b,
                                         "delta": b - a if a is not None and b is not None else None,
                                         "baseline_at": baseline["result_summary"]["first"] if field == "first_value" else left[field + "_at"],
                                         "target_at": candidate["result_summary"]["first"] if field == "first_value" else right[field + "_at"]}
                    comparison["soc_changes"][name] = values
                result["metrics"].append(comparison)
            for index, path in enumerate(inputs):
                if fingerprint(path) != snapshots[index]:
                    raise ValueError("Native result changed during engineering comparison")
                if bindings[index] is not None:
                    rebound = _run_binding(store, result["scenarios"][index], [])
                    if rebound != bindings[index]:
                        raise ValueError("Bound run record changed during engineering comparison")
            result["historical_input_binding_verified"] = all(binding is not None for binding in bindings)
            result["review_items"] = list(dict.fromkeys(result["review_items"]))
            result["summary"] = _explanations(result)
            result["status"] = "needs_review" if result["review_items"] else "ready" if dry_run else "created"
            markdown, table = _markdown(result), _comparison_csv(result)
            json.dumps(result, allow_nan=False)
            if not dry_run:
                paths["json"].parent.mkdir(parents=True, exist_ok=True)
                for kind, payload in (("csv", table), ("markdown", markdown)):
                    path = workspace_file(store.workspace, "Results", paths[kind].name, paths[kind].suffix)
                    if path != paths[kind]:
                        raise ValueError("Comparison output folder changed during the operation")
                    with path.open("x", encoding="utf-8-sig" if kind == "csv" else "utf-8", newline="") as stream:
                        active_output = kind
                        stream.write(payload)
                    result["written_artifacts"].append(kind)
                    active_output = None
                result["report_files"] = {kind: fingerprint(paths[kind]) for kind in ("csv", "markdown")}
                path = workspace_file(store.workspace, "Results", output_name, ".json")
                if path != paths["json"]:
                    raise ValueError("Comparison output folder changed during the operation")
                active_output = "json"
                # status keeps needs_review for a written report; created records that all three exist.
                write_json(path, {**result, "created": True,
                                  "written_artifacts": [*result["written_artifacts"], "json"]}, first=True)
                result["written_artifacts"].append("json")
                result["created"] = True
                active_output = None
            return result
        except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
            result["status"] = "blocked"
            result["issues"].append(str(exc))
            if active_output is not None:
                result["incomplete_artifact"] = active_output
            return result

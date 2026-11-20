"""Interactive Plotly HTML engineering report generator (single-scenario and multi-scenario).

Combines streaming SFI CSV monthly/hourly analytics, deterministic physics diagnostics
(pvsyst_auto.engineering.diagnostics), and financial/TOU cashflow evaluation
(pvsyst_auto.engineering.economics) into a modern bilingual (zh/en) HTML report with
interactive Plotly charts (hover, zoom, pan, legend toggle, PNG export) and print-PDF CSS.
"""
from __future__ import annotations

from datetime import datetime, timezone
import html
import math
from pathlib import Path
from typing import Any

from pvsyst_auto.engineering.diagnostics import diagnose_simulation_csv
from pvsyst_auto.engineering.economics import evaluate_economics
from pvsyst_auto.native.cli import iter_result_csv, result_units
from pvsyst_auto.project.paths import safe_filename, workspace_file

_POWER_TO_KW = {"W": 0.001, "kW": 1.0}
_ENERGY_TO_KWH = {"Wh": 0.001, "kWh": 1.0}
_PR_SCALE = {"ratio": 1.0, "%": 0.01}

_I18N = {
    "zh": {
        "subtitle": "光伏系统工程仿真与经济性交互报告",
        "generated_at": "生成时间",
        "scenarios_count": "方案数量",
        "primary_label": "主展示方案",
        "kpi_yield": "首年发电量",
        "kpi_specific": "等效利用小时",
        "kpi_pr": "加权 PR",
        "kpi_lcoe": "度电成本 LCOE",
        "kpi_npv": "净现值 NPV",
        "kpi_irr": "内部收益率 IRR",
        "kpi_payback": "静态回收期",
        "kpi_demand_savings": "年需量电费节省",
        "kpi_diag": "物理诊断",
        "status_ok": "诊断通过",
        "status_review": "需工程复核",
        "sec_monthly": "月度发电量与性能比 (PR)",
        "sec_losses": "光伏系统能量损失瀑布图",
        "sec_typical_day": "典型 24 小时功率与分时电价曲线",
        "sec_demand": "两部制月度最大需量削峰对比",
        "sec_cashflow": "全生命周期累计现金流与财务对比",
        "sec_sensitivity": "财务敏感性分析龙卷风图 (NPV)",
        "sec_bess_opt": "光储容量寻优收益热力图与 Pareto 前沿",
        "sec_ranking": "多方案综合排名对比表",
        "sec_diag": "物理与时序诊断明细",
        "demand_base": "基准最大需量 (kW)",
        "demand_actual": "加装光储后最大需量 (kW)",
        "sens_factor_labels": {
            "tariff": "电价水平",
            "capex": "初始投资 CAPEX",
            "yield": "年发电量 / 辐照",
            "degradation": "衰减率",
        },
        "sens_low": "下调",
        "sens_high": "上调",
        "bess_cap_axis": "储能容量 (kWh)",
        "bess_pcs_axis": "PCS 功率 (kW)",
        "bess_pareto_capex": "总投资 CAPEX",
        "bess_pareto_metric": "净现值 NPV",
        "no_findings": "未检出异常告警，所有物理与时序指标均处于正常阈值内。",
        "col_rank": "排名",
        "col_scenario": "方案",
        "col_yield": "首年发电 (kWh)",
        "col_pr": "加权 PR (%)",
        "col_capex": "总投资 CAPEX",
        "col_lcoe": "LCOE",
        "col_npv": "NPV",
        "col_irr": "IRR (%)",
        "col_payback": "回收期 (年)",
        "col_sev": "严重级",
        "col_code": "诊断码",
        "col_metric": "指标",
        "col_obs": "实测 / 阈值",
        "col_msg": "说明与建议",
        "months": ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"],
        "wf_array": "阵列直流输出 (EArray)",
        "wf_clip": "逆变器超功率限发 (-IL_Pmax)",
        "wf_vlim": "电压/电流限制损失 (-IL_V/I)",
        "wf_shd": "电学阴影失配 (-ShdElec)",
        "wf_curt": "电网限发 (-EGrdLim)",
        "wf_other": "逆变转换及其他损益",
        "wf_grid": "上网/可用交流电量 (E_Grid)",
        "yr_unit": "年",
        "hour_axis": "小时 (0..23)",
        "power_axis": "平均功率 (kW)",
        "tariff_axis": "电价",
        "cum_nom": "累计名义现金流",
        "cum_disc": "累计折现现金流",
    },
    "en": {
        "subtitle": "PV Engineering Simulation & Financial Interactive Report",
        "generated_at": "Generated",
        "scenarios_count": "Scenarios",
        "primary_label": "Primary scenario",
        "kpi_yield": "Year-1 Yield",
        "kpi_specific": "Specific Yield",
        "kpi_pr": "Weighted PR",
        "kpi_lcoe": "LCOE",
        "kpi_npv": "NPV",
        "kpi_irr": "IRR",
        "kpi_payback": "Simple Payback",
        "kpi_demand_savings": "Annual Demand Savings",
        "kpi_diag": "Diagnostics",
        "status_ok": "Healthy",
        "status_review": "Needs Review",
        "sec_monthly": "Monthly Generation & Performance Ratio (PR)",
        "sec_losses": "PV Energy Loss Waterfall",
        "sec_typical_day": "Typical 24-Hour Power & TOU Tariff Profile",
        "sec_demand": "Monthly Peak Demand Shaving Comparison",
        "sec_cashflow": "Lifetime Cumulative Cashflow & Financial Comparison",
        "sec_sensitivity": "Financial Sensitivity Tornado Chart (NPV)",
        "sec_bess_opt": "PV+BESS Sizing Heatmap & Pareto Frontier",
        "sec_ranking": "Multi-Scenario Ranking Summary",
        "sec_diag": "Physics & Time-Series Diagnostics",
        "demand_base": "Baseline Peak Demand (kW)",
        "demand_actual": "Post-PV/BESS Peak Demand (kW)",
        "sens_factor_labels": {
            "tariff": "Tariff Rate",
            "capex": "Initial CAPEX",
            "yield": "Annual Yield / Irradiance",
            "degradation": "Degradation Rate",
        },
        "sens_low": "Downside",
        "sens_high": "Upside",
        "bess_cap_axis": "BESS Energy (kWh)",
        "bess_pcs_axis": "PCS Power (kW)",
        "bess_pareto_capex": "Total CAPEX",
        "bess_pareto_metric": "NPV",
        "no_findings": "No anomalies detected; all physics and time-series checks are within thresholds.",
        "col_rank": "Rank",
        "col_scenario": "Scenario",
        "col_yield": "Year-1 Yield (kWh)",
        "col_pr": "Weighted PR (%)",
        "col_capex": "CAPEX",
        "col_lcoe": "LCOE",
        "col_npv": "NPV",
        "col_irr": "IRR (%)",
        "col_payback": "Payback (yr)",
        "col_sev": "Severity",
        "col_code": "Code",
        "col_metric": "Metric",
        "col_obs": "Observed / Threshold",
        "col_msg": "Details & Recommendations",
        "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        "wf_array": "DC Array Energy (EArray)",
        "wf_clip": "Inverter Clipping (-IL_Pmax)",
        "wf_vlim": "Voltage/Current Limits (-IL_V/I)",
        "wf_shd": "Electrical Shading (-ShdElec)",
        "wf_curt": "Grid Curtailment (-EGrdLim)",
        "wf_other": "Inverter Conversion & Balance",
        "wf_grid": "Net AC Grid Energy (E_Grid)",
        "yr_unit": "yr",
        "hour_axis": "Hour of Day (0..23)",
        "power_axis": "Average Power (kW)",
        "tariff_axis": "Tariff",
        "cum_nom": "Cumulative Nominal Cashflow",
        "cum_disc": "Cumulative Discounted Cashflow",
    },
}


def _resolve_csv(workspace: Path | None, csv_name: str) -> Path:
    if workspace is not None:
        safe = safe_filename(csv_name, ".csv")
        for folder in ("Results", "UserHourly"):
            cand = workspace_file(workspace, folder, safe, ".csv")
            if cand.is_file():
                return cand
        raise FileNotFoundError(f"Result CSV not found in Results/ or UserHourly/: {safe}")
    p = Path(csv_name)
    if p.suffix.lower() != ".csv":
        raise ValueError("Expected a .csv file")
    if not p.is_file():
        raise FileNotFoundError(f"Result CSV not found: {p}")
    return p


def _summarize_csv_for_report(csv_path: Path) -> dict[str, Any]:
    """Single streaming pass to extract monthly PV yield/PR and loss-chain totals for charts.

    Aligns monthly and annual PV yield with economics settlement: prefers EToGrid over E_Grid
    and includes self-consumed PV energy (max(0, E_User - EFrGrid)) when E_User is present so
    self-consumption and storage reports agree with economics tables.
    """
    units = result_units(csv_path, strict=True)
    idx: dict[str, int] = {}
    rows = 0
    prev_stamp = None
    step_minutes: float | None = None
    regular = True
    first_row_data: tuple[Any, list[Any]] | None = None

    export_col: str | None = None
    has_load = False
    has_import = False
    col_scale: dict[str, tuple[float, bool]] = {}

    monthly_pv_kwh = {m: 0.0 for m in range(1, 13)}
    monthly_pr_num = {m: 0.0 for m in range(1, 13)}
    monthly_pr_den = {m: 0.0 for m in range(1, 13)}
    col_sums: dict[str, float] = {}

    def _unit_kind(col: str) -> tuple[float, bool]:
        u = units.get(col, "")
        if u in _POWER_TO_KW:
            return _POWER_TO_KW[u], True
        if u in _ENERGY_TO_KWH:
            return _ENERGY_TO_KWH[u], False
        return 1.0, False

    def _val(row_data: list[Any], col: str) -> float:
        i = idx.get(col)
        if i is None:
            return 0.0
        v = row_data[i]
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else 0.0

    def _process_row(stamp: Any, row_data: list[Any], dt_hours: float) -> None:
        def kwh(col: str) -> float:
            if col not in col_scale:
                return 0.0
            factor, needs_dt = col_scale[col]
            return _val(row_data, col) * factor * (dt_hours if needs_dt else 1.0)

        for name, col_i in idx.items():
            v = row_data[col_i]
            if isinstance(v, (int, float)) and math.isfinite(v):
                col_sums[name] += float(v)

        raw_eg = kwh(export_col) if export_col else 0.0
        e_exp = max(0.0, raw_eg)
        if has_load:
            e_user = max(0.0, kwh("E_User"))
            if has_import:
                e_imp = max(0.0, kwh("EFrGrid"))
            elif "E_Solar" in idx:
                e_imp = max(0.0, e_user - max(0.0, kwh("E_Solar")))
            elif raw_eg < 0:
                e_imp = -raw_eg
            else:
                e_imp = 0.0
            self_cons = max(0.0, e_user - e_imp)
            monthly_pv_kwh[stamp.month] += e_exp + self_cons
        else:
            monthly_pv_kwh[stamp.month] += e_exp

        if "PR" in idx and "GlobInc" in idx:
            gi = row_data[idx["GlobInc"]]
            pr = row_data[idx["PR"]]
            pr_scale = _PR_SCALE.get(units.get("PR", ""), 1.0)
            if (isinstance(gi, (int, float)) and math.isfinite(gi) and gi > 0
                    and isinstance(pr, (int, float)) and math.isfinite(pr)):
                monthly_pr_num[stamp.month] += float(gi) * float(pr) * pr_scale
                monthly_pr_den[stamp.month] += float(gi)

    for headers, row in iter_result_csv(csv_path, strict=True):
        if rows == 0:
            idx = {name: i for i, name in enumerate(headers) if i > 0}
            col_sums = {name: 0.0 for name in idx}
            export_col = "EToGrid" if "EToGrid" in idx else ("E_Grid" if "E_Grid" in idx else None)
            has_load = "E_User" in idx
            has_import = "EFrGrid" in idx
            for col in idx:
                col_scale[col] = _unit_kind(col)
            first_row_data = (row[0], list(row))
            prev_stamp = row[0]
            rows = 1
            continue

        stamp = row[0]
        delta = (stamp - prev_stamp).total_seconds() / 60.0
        if delta <= 0:
            regular = False
            dt_hours = (step_minutes or 60.0) / 60.0
        else:
            if step_minutes is None:
                step_minutes = delta
                _process_row(first_row_data[0], first_row_data[1], step_minutes / 60.0)
                first_row_data = None
            elif abs(delta - step_minutes) > 1e-6:
                regular = False
            dt_hours = delta / 60.0
        _process_row(stamp, row, dt_hours)
        prev_stamp = stamp
        rows += 1

    if rows == 0:
        raise ValueError("Result CSV has no data rows")
    if export_col is None:
        raise ValueError("Result CSV must include E_Grid or EToGrid for report generation")
    if not regular:
        raise ValueError(
            "Result CSV timestamps do not have a regular cadence (missing, duplicate, or out-of-order rows); "
            "cannot compute monthly energy or losses reliably"
        )
    if first_row_data is not None:
        step_minutes = 60.0
        _process_row(first_row_data[0], first_row_data[1], 1.0)
    step_hours = (step_minutes if step_minutes else 60.0) / 60.0

    def to_kwh(col: str, raw: float) -> float:
        factor, needs_dt = col_scale.get(col, (1.0, False))
        return raw * factor * (step_hours if needs_dt else 1.0)

    monthly_kwh = [round(monthly_pv_kwh[m], 2) for m in range(1, 13)]
    monthly_pr_pct = [
        round((monthly_pr_num[m] / monthly_pr_den[m]) * 100.0, 2) if monthly_pr_den[m] > 0 else None
        for m in range(1, 13)
    ]
    losses = {
        col: round(to_kwh(col, col_sums[col]), 2)
        for col in ("EArray", "E_Grid", "EToGrid", "IL_Pmax", "IL_Vmax", "IL_Vmin", "IL_Imax", "ShdElec", "EGrdLim", "EUnused")
        if col in col_sums
    }
    return {
        "rows": rows,
        "step_minutes": step_hours * 60.0,
        "has_load": has_load,
        "monthly_kwh": monthly_kwh,
        "monthly_pr_pct": monthly_pr_pct,
        "losses_kwh": losses,
        "annual_e_grid_kwh": round(sum(monthly_kwh), 2),
    }


def _plotly_div(fig, *, include_js: bool | str) -> str:
    return fig.to_html(full_html=False, include_plotlyjs=include_js, config={"responsive": True, "displaylogo": False})


def _fmt_num(val: float | None, decimals: int = 2, suffix: str = "") -> str:
    if val is None or not math.isfinite(val):
        return "—"
    return f"{val:,.{decimals}f}{suffix}"


def generate_report(
    *,
    workspace: Path | None = None,
    store=None,
    title: str,
    output_name: str,
    scenarios: list[dict[str, Any]],
    financial: dict[str, Any] | None = None,
    tariff: dict[str, Any] | None = None,
    sensitivity: dict[str, Any] | None = None,
    bess_optimization: dict[str, Any] | None = None,
    language: str = "zh",
    plotlyjs: str = "cdn",
    thresholds: dict[str, float] | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Generate an interactive Plotly HTML engineering report under workspace/Results."""
    if language not in ("zh", "en"):
        raise ValueError("language must be 'zh' or 'en'")
    if plotlyjs not in ("cdn", "embed"):
        raise ValueError("plotlyjs must be 'cdn' or 'embed'")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("title must be 1..200 characters")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 20:
        raise ValueError("scenarios must contain 1..20 scenario entries")
    if type(overwrite) is not bool or type(dry_run) is not bool:
        raise ValueError("overwrite and dry_run must be strict booleans")

    safe_out = safe_filename(output_name, ".html")
    out_path = workspace_file(workspace, "Results", safe_out, ".html") if workspace is not None else Path(output_name)
    if out_path.exists() and not overwrite:
        raise FileExistsError(f"Report already exists: {out_path.name}; choose a new output_name or set overwrite=true")

    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    t = _I18N[language]
    parsed_scenarios: list[dict[str, Any]] = []
    econ_inputs: list[dict[str, Any]] = []

    seen_labels: set[str] = set()
    for idx_s, sc in enumerate(scenarios):
        if not isinstance(sc, dict):
            raise ValueError(f"scenarios[{idx_s}] must be an object")
        label = str(sc.get("label") or f"Scenario_{idx_s + 1}").strip()
        if label in seen_labels:
            raise ValueError(f"Duplicate scenario label {label!r}; scenario labels must be unique")
        seen_labels.add(label)
        csv_name = sc.get("csv_name")
        if not csv_name:
            raise ValueError(f"scenarios[{idx_s}] ({label}) requires csv_name")
        csv_path = _resolve_csv(workspace, csv_name)
        proj = sc.get("project")
        var = sc.get("variant")
        diag = diagnose_simulation_csv(
            csv_path,
            store=store if (proj and var) else None,
            project=proj,
            variant=var,
            thresholds=thresholds,
        )
        csv_stats = _summarize_csv_for_report(csv_path)
        parsed_scenarios.append({
            "label": label,
            "csv_name": csv_path.name,
            "project": proj,
            "variant": var,
            "installed_dc_kw": sc.get("installed_dc_kw"),
            "battery_kwh": sc.get("battery_kwh"),
            "stats": csv_stats,
            "diagnostics": diag,
        })
        econ_entry = {"label": label, "csv_path": csv_path}
        for k in ("installed_dc_kw", "battery_kwh", "capex_total", "annual_opex", "replacement_events"):
            if sc.get(k) is not None:
                econ_entry[k] = sc[k]
        econ_inputs.append(econ_entry)

    econ_result = None
    if financial is not None or tariff is not None:
        econ_result = evaluate_economics(
            workspace=workspace,
            scenarios=econ_inputs,
            financial=financial,
            tariff=tariff,
            sensitivity=sensitivity,
            include_cashflow_schedule=True,
        )

    econ_by_label = {
        s["label"]: s for s in (econ_result["scenarios"] if econ_result else ())
    }

    if dry_run:
        return {
            "status": "ready",
            "dry_run": True,
            "report_path": str(out_path),
            "scenario_count": len(parsed_scenarios),
            "language": language,
            "plotlyjs": plotlyjs,
            "has_economics": econ_result is not None,
        }

    # --- Build Interactive Plotly Figures ---
    js_first = True if plotlyjs == "embed" else "cdn"
    js_used = False

    def next_js():
        nonlocal js_used
        if not js_used:
            js_used = True
            return js_first
        return False

    palette = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#dc2626", "#0891b2"]
    chart_sections: list[tuple[str, str]] = []

    # 1. Monthly Generation & PR Combo Chart
    fig_monthly = make_subplots(specs=[[{"secondary_y": True}]])
    months = t["months"]
    for i, sc in enumerate(parsed_scenarios):
        color = palette[i % len(palette)]
        fig_monthly.add_trace(
            go.Bar(name=f"{sc['label']} (kWh)", x=months, y=sc["stats"]["monthly_kwh"], marker_color=color, opacity=0.85),
            secondary_y=False,
        )
        if any(v is not None for v in sc["stats"]["monthly_pr_pct"]):
            fig_monthly.add_trace(
                go.Scatter(
                    name=f"{sc['label']} PR (%)", x=months, y=sc["stats"]["monthly_pr_pct"],
                    mode="lines+markers", line=dict(color=color, width=2.5),
                ),
                secondary_y=True,
            )
    fig_monthly.update_layout(
        barmode="group", template="plotly_white", height=420,
        margin=dict(l=55, r=55, t=30, b=45),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
    )
    fig_monthly.update_yaxes(title_text="kWh", secondary_y=False)
    fig_monthly.update_yaxes(title_text="PR (%)", range=[0, 115], secondary_y=True)
    chart_sections.append((t["sec_monthly"], _plotly_div(fig_monthly, include_js=next_js())))

    # Select best scenario as primary for single-scenario KPI cards & charts (fallback to first if None)
    best_label = (
        econ_result["best_scenario"]
        if econ_result is not None
        else max(parsed_scenarios, key=lambda sc: sc["stats"]["annual_e_grid_kwh"])["label"]
    )
    primary = next((sc for sc in parsed_scenarios if sc["label"] == best_label), parsed_scenarios[0])
    primary_suffix = f" ({primary['label']})" if len(parsed_scenarios) > 1 else ""

    # 2. Loss Waterfall (Primary Scenario)
    losses = primary["stats"]["losses_kwh"]
    if "EArray" in losses and ("E_Grid" in losses or "EToGrid" in losses):
        e_arr = losses["EArray"]
        e_grd = (
            primary["stats"]["annual_e_grid_kwh"]
            if primary["stats"].get("has_load")
            else losses.get("E_Grid", losses.get("EToGrid", 0.0))
        )
        il_pmax = abs(losses.get("IL_Pmax", 0.0))
        il_vi = abs(losses.get("IL_Vmax", 0.0)) + abs(losses.get("IL_Vmin", 0.0)) + abs(losses.get("IL_Imax", 0.0))
        shd_el = abs(losses.get("ShdElec", 0.0))
        curt = abs(losses.get("EGrdLim", 0.0))
        other = e_grd - (e_arr - il_pmax - il_vi - shd_el - curt)

        wf_x = [t["wf_array"]]
        wf_y = [e_arr]
        wf_m = ["absolute"]
        for lbl, val in (
            (t["wf_clip"], -il_pmax),
            (t["wf_vlim"], -il_vi),
            (t["wf_shd"], -shd_el),
            (t["wf_curt"], -curt),
        ):
            if abs(val) > 1e-3:
                wf_x.append(lbl)
                wf_y.append(round(val, 2))
                wf_m.append("relative")
        wf_x.extend([t["wf_other"], t["wf_grid"]])
        wf_y.extend([round(other, 2), round(e_grd, 2)])
        wf_m.extend(["relative", "total"])

        fig_wf = go.Figure(go.Waterfall(
            orientation="v", measure=wf_m, x=wf_x, y=wf_y,
            textposition="outside",
            text=[f"{v:+,.0f}" if m == "relative" else f"{v:,.0f}" for v, m in zip(wf_y, wf_m)],
            decreasing={"marker": {"color": "#ef4444"}},
            increasing={"marker": {"color": "#10b981"}},
            totals={"marker": {"color": "#2563eb"}},
        ))
        fig_wf.update_layout(template="plotly_white", height=400, margin=dict(l=55, r=30, t=30, b=70), yaxis_title="kWh")
        chart_sections.append((f"{t['sec_losses']}{primary_suffix}", _plotly_div(fig_wf, include_js=next_js())))

    # 3. Typical 24-Hour Power & TOU Profile (when economics settlement has typical_day_profile)
    primary_econ = econ_by_label.get(primary["label"])
    if primary_econ and (primary_econ.get("settlement") or {}).get("typical_day_profile"):
        prof = primary_econ["settlement"]["typical_day_profile"]
        hours_x = [p["hour"] for p in prof]
        fig_day = make_subplots(specs=[[{"secondary_y": True}]])
        fig_day.add_trace(go.Scatter(name="PV (kW)", x=hours_x, y=[p["avg_pv_kw"] for p in prof], fill="tozeroy", line=dict(color="#f59e0b")), secondary_y=False)
        if any(p["avg_load_kw"] > 0 for p in prof):
            fig_day.add_trace(go.Scatter(name="Load (kW)", x=hours_x, y=[p["avg_load_kw"] for p in prof], line=dict(color="#1f2937", width=2.5)), secondary_y=False)
        if any(p["avg_battery_discharge_kw"] > 0 or p["avg_battery_charge_kw"] > 0 for p in prof):
            fig_day.add_trace(go.Bar(name="Battery Charge (kW)", x=hours_x, y=[-p["avg_battery_charge_kw"] for p in prof], marker_color="#3b82f6", opacity=0.65), secondary_y=False)
            fig_day.add_trace(go.Bar(name="Battery Discharge (kW)", x=hours_x, y=[p["avg_battery_discharge_kw"] for p in prof], marker_color="#10b981", opacity=0.75), secondary_y=False)
        if any(p["avg_import_rate"] > 0 for p in prof):
            fig_day.add_trace(go.Scatter(name=f"{t['tariff_axis']} ({econ_result['currency']}/kWh)", x=hours_x, y=[p["avg_import_rate"] for p in prof], line=dict(color="#dc2626", dash="dot", width=2)), secondary_y=True)
        fig_day.update_layout(
            barmode="relative", template="plotly_white", height=400,
            margin=dict(l=55, r=55, t=30, b=45),
            xaxis_title=t["hour_axis"],
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
        )
        fig_day.update_yaxes(title_text=t["power_axis"], secondary_y=False)
        fig_day.update_yaxes(title_text=f"{t['tariff_axis']} ({econ_result['currency']}/kWh)", secondary_y=True)
        chart_sections.append((f"{t['sec_typical_day']}{primary_suffix}", _plotly_div(fig_day, include_js=next_js())))

    # 4. Lifetime Cumulative Cashflow Chart (when economics evaluated)
    if econ_result:
        fig_cf = go.Figure()
        for i, sc_e in enumerate(econ_result["scenarios"]):
            sched = sc_e["financials"].get("cashflow_schedule") or []
            if not sched:
                continue
            years = [0] + [r["year"] for r in sched]
            capex = sc_e["financials"]["capex_total"]
            cum_nom = [-capex] + [r["cumulative_cashflow"] for r in sched]
            cum_disc = [-capex] + [r["cumulative_discounted_cashflow"] for r in sched]
            color = palette[i % len(palette)]
            fig_cf.add_trace(go.Scatter(
                name=f"{sc_e['label']} ({t['cum_nom']})", x=years, y=cum_nom,
                mode="lines+markers", line=dict(color=color, width=2.5),
            ))
            fig_cf.add_trace(go.Scatter(
                name=f"{sc_e['label']} ({t['cum_disc']})", x=years, y=cum_disc,
                mode="lines", line=dict(color=color, width=2, dash="dash"),
            ))
        fig_cf.add_hline(y=0, line_width=1, line_dash="dot", line_color="#6b7280")
        fig_cf.update_layout(
            template="plotly_white", height=400, margin=dict(l=65, r=30, t=30, b=45),
            xaxis_title=t["yr_unit"], yaxis_title=econ_result["currency"],
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
        )
        chart_sections.append((t["sec_cashflow"], _plotly_div(fig_cf, include_js=next_js())))

    # 5. Monthly Peak Demand Shaving Comparison (two-part demand tariff)
    primary_dc_summary = ((primary_econ.get("settlement") or {}).get("demand_charge_summary") if primary_econ else None)
    if primary_dc_summary and primary_dc_summary.get("monthly_peaks"):
        peaks = primary_dc_summary["monthly_peaks"]
        fig_dc = go.Figure()
        fig_dc.add_trace(go.Bar(
            name=t["demand_base"], x=months, y=[p["baseline_peak_kw"] for p in peaks],
            marker_color="#94a3b8", opacity=0.85,
        ))
        fig_dc.add_trace(go.Bar(
            name=t["demand_actual"], x=months, y=[p["actual_peak_kw"] for p in peaks],
            marker_color="#059669", opacity=0.9,
        ))
        fig_dc.update_layout(
            barmode="group", template="plotly_white", height=380,
            margin=dict(l=55, r=30, t=30, b=45), yaxis_title="kW",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
        )
        chart_sections.append((t["sec_demand"], _plotly_div(fig_dc, include_js=next_js())))

    # 6. Financial Sensitivity Tornado Chart (NPV impact around base)
    primary_sens = primary_econ.get("sensitivity") if primary_econ else None
    if primary_sens and primary_sens.get("factors"):
        base_npv = float(primary_sens["base"]["npv"])
        # Reverse order so the highest-impact factor appears at the top of the horizontal bar chart
        s_rows = list(reversed(primary_sens["factors"]))
        lbl_map = t["sens_factor_labels"]
        y_labels = [lbl_map.get(r["factor"], r["factor"]) for r in s_rows]
        low_deltas = [round(float(r["low"]["npv"]) - base_npv, 2) for r in s_rows]
        high_deltas = [round(float(r["high"]["npv"]) - base_npv, 2) for r in s_rows]
        var_pct = primary_sens.get("variation_pct", 15)
        fig_tor = go.Figure()
        fig_tor.add_trace(go.Bar(
            name=f"{t['sens_low']} (-{var_pct:g}%)",
            y=y_labels, x=low_deltas, base=base_npv, orientation="h",
            marker_color="#ef4444", opacity=0.85,
            hovertemplate="%{y} (-" + f"{var_pct:g}%): NPV=%{{x:,.0f}} ({econ_result['currency']})<extra></extra>",
        ))
        fig_tor.add_trace(go.Bar(
            name=f"{t['sens_high']} (+{var_pct:g}%)",
            y=y_labels, x=high_deltas, base=base_npv, orientation="h",
            marker_color="#2563eb", opacity=0.85,
            hovertemplate="%{y} (+" + f"{var_pct:g}%): NPV=%{{x:,.0f}} ({econ_result['currency']})<extra></extra>",
        ))
        fig_tor.add_vline(x=base_npv, line_width=2, line_dash="dash", line_color="#0f172a")
        fig_tor.update_layout(
            barmode="overlay", template="plotly_white", height=360,
            margin=dict(l=140, r=30, t=30, b=45),
            xaxis_title=f"NPV ({econ_result['currency']})",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
        )
        chart_sections.append((t["sec_sensitivity"], _plotly_div(fig_tor, include_js=next_js())))

    # 7. Optional BESS Sizing 2D Heatmap & Pareto Frontier (from pvsyst_optimize_bess_sizing)
    if isinstance(bess_optimization, dict):
        hm = bess_optimization.get("heatmap")
        pareto = bess_optimization.get("pareto_frontier") or []
        if hm and hm.get("bess_kwh") and hm.get("pcs_kw") and hm.get("z"):
            fig_bess = make_subplots(
                rows=1, cols=2 if pareto else 1,
                subplot_titles=(
                    [f"{hm.get('metric', 'npv').upper()} Heatmap", "Pareto Frontier"]
                    if pareto else [f"{hm.get('metric', 'npv').upper()} Heatmap"]
                ),
            )
            fig_bess.add_trace(
                go.Heatmap(
                    x=[str(v) for v in hm["bess_kwh"]],
                    y=[str(v) for v in hm["pcs_kw"]],
                    z=hm["z"],
                    colorscale="Viridis",
                    colorbar=dict(title=str(hm.get("metric", "npv")).upper(), x=0.45 if pareto else 1.02),
                ),
                row=1, col=1,
            )
            fig_bess.update_xaxes(title_text=t["bess_cap_axis"], row=1, col=1)
            fig_bess.update_yaxes(title_text=t["bess_pcs_axis"], row=1, col=1)
            if pareto:
                fig_bess.add_trace(
                    go.Scatter(
                        name="Pareto",
                        x=[p["capex_total"] for p in pareto],
                        y=[p["npv"] for p in pareto],
                        text=[f"{p.get('bess_kwh')} kWh / {p.get('pcs_kw')} kW (IRR {p.get('irr_pct')}%)" for p in pareto],
                        mode="lines+markers",
                        line=dict(color="#2563eb", width=2.5),
                        marker=dict(size=9, color="#10b981"),
                    ),
                    row=1, col=2,
                )
                fig_bess.update_xaxes(title_text=t["bess_pareto_capex"], row=1, col=2)
                fig_bess.update_yaxes(title_text=t["bess_pareto_metric"], row=1, col=2)
            fig_bess.update_layout(template="plotly_white", height=420, margin=dict(l=60, r=40, t=45, b=50), showlegend=False)
            chart_sections.append((t["sec_bess_opt"], _plotly_div(fig_bess, include_js=next_js())))

    # --- Assemble Clean Responsive HTML Document ---
    primary_diag = primary["diagnostics"]
    primary_yield = primary["stats"]["annual_e_grid_kwh"]
    primary_pr = (primary_diag["weighted_pr"] * 100.0) if primary_diag.get("weighted_pr") is not None else None
    primary_dc = primary.get("installed_dc_kw")
    specific_yield = (primary_yield / primary_dc) if (primary_dc and primary_dc > 0) else None
    curr = econ_result["currency"] if econ_result else ""

    primary_needs_review = bool(primary_diag["needs_review"] or primary_diag.get("status") == "blocked")
    kpis = [
        (t["kpi_yield"], _fmt_num(primary_yield, 1, " kWh")),
        (t["kpi_specific"], _fmt_num(specific_yield, 1, " kWh/kWp")),
        (t["kpi_pr"], _fmt_num(primary_pr, 2, " %")),
        (t["kpi_diag"], f"{t['status_review']} ({primary_diag['finding_count']})" if primary_needs_review else f"{t['status_ok']} ({primary_diag['finding_count']})"),
    ]
    if primary_econ:
        pf = primary_econ["financials"]
        kpis.extend([
            (t["kpi_lcoe"], _fmt_num(pf.get("lcoe"), 4, f" {curr}/kWh")),
            (t["kpi_npv"], _fmt_num(pf.get("npv"), 0, f" {curr}")),
            (t["kpi_irr"], _fmt_num(pf.get("irr_pct"), 2, " %")),
            (t["kpi_payback"], _fmt_num(pf.get("simple_payback_years"), 2, f" {t['yr_unit']}")),
        ])
        if primary_dc_summary is not None:
            kpis.append((t["kpi_demand_savings"], _fmt_num(primary_dc_summary.get("annual_demand_savings"), 0, f" {curr}")))

    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-label">{html.escape(lbl)}</div><div class="kpi-val">{html.escape(val)}</div></div>'
        for lbl, val in kpis
    )

    # Ranking / comparison table
    ranking_rows_html = []
    if econ_result:
        for row in econ_result["ranking"]:
            sc_match = next((s for s in parsed_scenarios if s["label"] == row["label"]), None)
            pr_val = (sc_match["diagnostics"]["weighted_pr"] * 100.0) if (sc_match and sc_match["diagnostics"].get("weighted_pr") is not None) else None
            ranking_rows_html.append(
                f"<tr><td>#{row['rank']}</td><td><strong>{html.escape(row['label'])}</strong></td>"
                f"<td>{_fmt_num(row['year1_energy_kwh'], 1)}</td><td>{_fmt_num(pr_val, 2)}</td>"
                f"<td>{_fmt_num(row['capex_total'], 0)}</td><td>{_fmt_num(row['lcoe'], 4)}</td>"
                f"<td>{_fmt_num(row['npv'], 0)}</td><td>{_fmt_num(row['irr_pct'], 2)}</td>"
                f"<td>{_fmt_num(row['simple_payback_years'], 2)}</td></tr>"
            )
        ranking_table = (
            f"<table><thead><tr><th>{t['col_rank']}</th><th>{t['col_scenario']}</th><th>{t['col_yield']}</th>"
            f"<th>{t['col_pr']}</th><th>{t['col_capex']} ({html.escape(curr)})</th><th>{t['col_lcoe']} ({html.escape(curr)}/kWh)</th>"
            f"<th>{t['col_npv']} ({html.escape(curr)})</th><th>{t['col_irr']}</th><th>{t['col_payback']}</th></tr></thead>"
            f"<tbody>{''.join(ranking_rows_html)}</tbody></table>"
        )
    else:
        for i, sc in enumerate(sorted(parsed_scenarios, key=lambda s: -s["stats"]["annual_e_grid_kwh"]), start=1):
            pr_val = (sc["diagnostics"]["weighted_pr"] * 100.0) if sc["diagnostics"].get("weighted_pr") is not None else None
            ranking_rows_html.append(
                f"<tr><td>#{i}</td><td><strong>{html.escape(sc['label'])}</strong></td>"
                f"<td>{_fmt_num(sc['stats']['annual_e_grid_kwh'], 1)}</td><td>{_fmt_num(pr_val, 2)}</td></tr>"
            )
        ranking_table = (
            f"<table><thead><tr><th>{t['col_rank']}</th><th>{t['col_scenario']}</th><th>{t['col_yield']}</th>"
            f"<th>{t['col_pr']}</th></tr></thead><tbody>{''.join(ranking_rows_html)}</tbody></table>"
        )

    # Diagnostics table (all scenarios)
    diag_rows = []
    for sc in parsed_scenarios:
        for f in sc["diagnostics"].get("findings") or ():
            sev = str(f.get("severity", "info"))
            actions_txt = " | ".join(f.get("recommended_actions") or ())
            diag_rows.append(
                f"<tr><td>{html.escape(sc['label'])}</td>"
                f"<td><span class='badge badge-{html.escape(sev)}'>{html.escape(sev.upper())}</span></td>"
                f"<td><code>{html.escape(str(f.get('code')))}</code></td>"
                f"<td>{html.escape(str(f.get('metric')))}</td>"
                f"<td>{html.escape(str(f.get('observed')))} / {html.escape(str(f.get('threshold')))} {html.escape(str(f.get('unit')))}</td>"
                f"<td>{html.escape(str(f.get('message')))}<br/><small>{html.escape(actions_txt)}</small></td></tr>"
            )
    diag_html = (
        f"<p class='ok-note'>{html.escape(t['no_findings'])}</p>"
        if not diag_rows else
        f"<table><thead><tr><th>{t['col_scenario']}</th><th>{t['col_sev']}</th><th>{t['col_code']}</th>"
        f"<th>{t['col_metric']}</th><th>{t['col_obs']}</th><th>{t['col_msg']}</th></tr></thead>"
        f"<tbody>{''.join(diag_rows)}</tbody></table>"
    )

    charts_html = "".join(
        f"<section class='card'><h2>{html.escape(sec_title)}</h2>{div_html}</section>"
        for sec_title, div_html in chart_sections
    )
    badge_cls = "badge-high" if primary_needs_review else "badge-ok"
    badge_txt = t["status_review"] if primary_needs_review else t["status_ok"]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    primary_meta = (
        f" · {html.escape(t['primary_label'])}: {html.escape(primary['label'])}"
        if len(parsed_scenarios) > 1
        else ""
    )

    doc = f"""<!DOCTYPE html>
<html lang="{html.escape(language)}">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)}</title>
<style>
  :root {{ --bg:#f8fafc; --card:#ffffff; --text:#0f172a; --muted:#64748b; --border:#e2e8f0; --accent:#2563eb; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); line-height:1.5; }}
  .wrap {{ max-width:1200px; margin:0 auto; padding:24px; }}
  header {{ background:linear-gradient(135deg,#0f172a,#1e3a8a); color:#fff; padding:28px 32px; border-radius:14px; margin-bottom:20px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; }}
  header h1 {{ margin:0 0 4px 0; font-size:24px; }}
  header .sub {{ color:#cbd5e1; font-size:13px; }}
  .kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; margin-bottom:20px; }}
  .kpi {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; box-shadow:0 1px 2px rgba(15,23,42,.04); }}
  .kpi-label {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; }}
  .kpi-val {{ font-size:20px; font-weight:700; margin-top:4px; color:var(--text); }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px 24px; margin-bottom:20px; box-shadow:0 1px 3px rgba(15,23,42,.04); }}
  .card h2 {{ margin:0 0 14px 0; font-size:17px; border-left:4px solid var(--accent); padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ border-bottom:1px solid var(--border); padding:9px 10px; text-align:left; vertical-align:top; }}
  th {{ background:#f1f5f9; font-weight:600; color:#334155; }}
  .badge {{ display:inline-block; padding:3px 9px; border-radius:999px; font-size:11px; font-weight:700; }}
  .badge-ok {{ background:#dcfce7; color:#166534; }}
  .badge-info {{ background:#dbeafe; color:#1e40af; }}
  .badge-medium {{ background:#fef3c7; color:#92400e; }}
  .badge-high, .badge-critical {{ background:#fee2e2; color:#991b1b; }}
  .ok-note {{ color:#166534; background:#f0fdf4; border:1px solid #bbf7d0; padding:10px 14px; border-radius:8px; margin:0; }}
  @media print {{ body {{ background:#fff; }} .card {{ break-inside:avoid; box-shadow:none; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>{html.escape(title)}</h1>
      <div class="sub">{html.escape(t["subtitle"])} · {html.escape(t["generated_at"])}: {generated} · {html.escape(t["scenarios_count"])}: {len(parsed_scenarios)}{primary_meta}</div>
    </div>
    <span class="badge {badge_cls}">{html.escape(badge_txt)}</span>
  </header>
  <div class="kpi-grid">{kpi_html}</div>
  {charts_html}
  <section class="card"><h2>{html.escape(t["sec_ranking"])}</h2>{ranking_table}</section>
  <section class="card"><h2>{html.escape(t["sec_diag"])}</h2>{diag_html}</section>
</div>
</body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return {
        "status": "generated",
        "dry_run": False,
        "report_path": str(out_path),
        "report_name": out_path.name,
        "bytes": out_path.stat().st_size,
        "language": language,
        "plotlyjs": plotlyjs,
        "scenario_count": len(parsed_scenarios),
        "chart_count": len(chart_sections),
        "has_economics": econ_result is not None,
        "best_scenario": best_label,
    }

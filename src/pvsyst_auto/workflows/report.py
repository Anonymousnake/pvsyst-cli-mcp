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
        "kpi_yield": "首年发电量",
        "kpi_specific": "等效利用小时",
        "kpi_pr": "加权 PR",
        "kpi_lcoe": "度电成本 LCOE",
        "kpi_npv": "净现值 NPV",
        "kpi_irr": "内部收益率 IRR",
        "kpi_payback": "静态回收期",
        "kpi_diag": "物理诊断",
        "status_ok": "诊断通过",
        "status_review": "需工程复核",
        "sec_monthly": "月度发电量与性能比 (PR)",
        "sec_losses": "光伏系统能量损失瀑布图",
        "sec_typical_day": "典型 24 小时功率与分时电价曲线",
        "sec_cashflow": "全生命周期累计现金流与财务对比",
        "sec_ranking": "多方案综合排名对比表",
        "sec_diag": "物理与时序诊断明细",
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
        "kpi_yield": "Year-1 Yield",
        "kpi_specific": "Specific Yield",
        "kpi_pr": "Weighted PR",
        "kpi_lcoe": "LCOE",
        "kpi_npv": "NPV",
        "kpi_irr": "IRR",
        "kpi_payback": "Simple Payback",
        "kpi_diag": "Diagnostics",
        "status_ok": "Healthy",
        "status_review": "Needs Review",
        "sec_monthly": "Monthly Generation & Performance Ratio (PR)",
        "sec_losses": "PV Energy Loss Waterfall",
        "sec_typical_day": "Typical 24-Hour Power & TOU Tariff Profile",
        "sec_cashflow": "Lifetime Cumulative Cashflow & Financial Comparison",
        "sec_ranking": "Multi-Scenario Ranking Summary",
        "sec_diag": "Physics & Time-Series Diagnostics",
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
    safe = safe_filename(csv_name, ".csv")
    if workspace is not None:
        for folder in ("Results", "UserHourly"):
            cand = workspace_file(workspace, folder, safe, ".csv")
            if cand.is_file():
                return cand
        raise FileNotFoundError(f"Result CSV not found in Results/ or UserHourly/: {safe}")
    p = Path(csv_name)
    if not p.is_file():
        raise FileNotFoundError(f"Result CSV not found: {p}")
    return p


def _summarize_csv_for_report(csv_path: Path) -> dict[str, Any]:
    """Single streaming pass to extract monthly E_Grid/PR and loss-chain totals for charts."""
    units = result_units(csv_path, strict=True)
    idx: dict[str, int] = {}
    rows = 0
    prev_stamp = None
    step_minutes: float | None = None
    regular = True

    monthly_egrid = {m: 0.0 for m in range(1, 13)}
    monthly_pr_num = {m: 0.0 for m in range(1, 13)}
    monthly_pr_den = {m: 0.0 for m in range(1, 13)}
    col_sums: dict[str, float] = {}

    for headers, row in iter_result_csv(csv_path, strict=True):
        if rows == 0:
            idx = {name: i for i, name in enumerate(headers) if i > 0}
            col_sums = {name: 0.0 for name in idx}
        stamp = row[0]
        if prev_stamp is not None:
            delta = (stamp - prev_stamp).total_seconds() / 60.0
            if delta <= 0:
                regular = False
            elif step_minutes is None:
                step_minutes = delta
            elif abs(delta - step_minutes) > 1e-6:
                regular = False
        prev_stamp = stamp
        rows += 1

        for name, col_i in idx.items():
            v = row[col_i]
            if isinstance(v, (int, float)) and math.isfinite(v):
                col_sums[name] += float(v)

        eg_col = "E_Grid" if "E_Grid" in idx else ("EToGrid" if "EToGrid" in idx else None)
        if eg_col is not None:
            v = row[idx[eg_col]]
            if isinstance(v, (int, float)) and math.isfinite(v):
                monthly_egrid[stamp.month] += float(v)
        if "PR" in idx and "GlobInc" in idx:
            gi = row[idx["GlobInc"]]
            pr = row[idx["PR"]]
            pr_scale = _PR_SCALE.get(units.get("PR", ""), 1.0)
            if (isinstance(gi, (int, float)) and math.isfinite(gi) and gi > 0
                    and isinstance(pr, (int, float)) and math.isfinite(pr)):
                monthly_pr_num[stamp.month] += float(gi) * float(pr) * pr_scale
                monthly_pr_den[stamp.month] += float(gi)

    if rows == 0:
        raise ValueError("Result CSV has no data rows")
    if not regular:
        raise ValueError(
            "Result CSV timestamps do not have a regular cadence (missing, duplicate, or out-of-order rows); "
            "cannot compute monthly energy or losses reliably"
        )
    step_hours = (step_minutes if step_minutes else 60.0) / 60.0

    def to_kwh(col: str, raw: float) -> float:
        u = units.get(col, "")
        if u in _POWER_TO_KW:
            return raw * _POWER_TO_KW[u] * step_hours
        if u in _ENERGY_TO_KWH:
            return raw * _ENERGY_TO_KWH[u]
        return raw

    eg_col = "E_Grid" if "E_Grid" in idx else ("EToGrid" if "EToGrid" in idx else None)
    monthly_kwh = [round(to_kwh(eg_col, monthly_egrid[m]), 2) if eg_col else 0.0 for m in range(1, 13)]
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

    for idx_s, sc in enumerate(scenarios):
        if not isinstance(sc, dict):
            raise ValueError(f"scenarios[{idx_s}] must be an object")
        label = str(sc.get("label") or f"Scenario_{idx_s + 1}").strip()
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
        econ_entry = {"label": label, "csv_name": csv_path.name}
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

    # 2. Loss Waterfall (Primary Scenario)
    primary = parsed_scenarios[0]
    losses = primary["stats"]["losses_kwh"]
    if "EArray" in losses and ("E_Grid" in losses or "EToGrid" in losses):
        e_arr = losses["EArray"]
        e_grd = losses.get("E_Grid", losses.get("EToGrid", 0.0))
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
        chart_sections.append((t["sec_losses"], _plotly_div(fig_wf, include_js=next_js())))

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
        chart_sections.append((t["sec_typical_day"], _plotly_div(fig_day, include_js=next_js())))

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

    # --- Assemble Clean Responsive HTML Document ---
    primary_diag = primary["diagnostics"]
    primary_yield = primary["stats"]["annual_e_grid_kwh"]
    primary_pr = (primary_diag["weighted_pr"] * 100.0) if primary_diag.get("weighted_pr") is not None else None
    primary_dc = primary.get("installed_dc_kw")
    specific_yield = (primary_yield / primary_dc) if (primary_dc and primary_dc > 0) else None
    curr = econ_result["currency"] if econ_result else ""

    kpis = [
        (t["kpi_yield"], _fmt_num(primary_yield, 1, " kWh")),
        (t["kpi_specific"], _fmt_num(specific_yield, 1, " kWh/kWp")),
        (t["kpi_pr"], _fmt_num(primary_pr, 2, " %")),
        (t["kpi_diag"], f"{t['status_review']} ({primary_diag['finding_count']})" if primary_diag["needs_review"] else f"{t['status_ok']} ({primary_diag['finding_count']})"),
    ]
    if primary_econ:
        pf = primary_econ["financials"]
        kpis.extend([
            (t["kpi_lcoe"], _fmt_num(pf.get("lcoe"), 4, f" {curr}/kWh")),
            (t["kpi_npv"], _fmt_num(pf.get("npv"), 0, f" {curr}")),
            (t["kpi_irr"], _fmt_num(pf.get("irr_pct"), 2, " %")),
            (t["kpi_payback"], _fmt_num(pf.get("simple_payback_years"), 2, f" {t['yr_unit']}")),
        ])

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
    badge_cls = "badge-high" if primary_diag["needs_review"] else "badge-ok"
    badge_txt = t["status_review"] if primary_diag["needs_review"] else t["status_ok"]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

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
      <div class="sub">{html.escape(t["subtitle"])} · {html.escape(t["generated_at"])}: {generated} · {html.escape(t["scenarios_count"])}: {len(parsed_scenarios)}</div>
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
        "best_scenario": econ_result["best_scenario"] if econ_result else max(
            parsed_scenarios, key=lambda sc: sc["stats"]["annual_e_grid_kwh"]
        )["label"],
    }

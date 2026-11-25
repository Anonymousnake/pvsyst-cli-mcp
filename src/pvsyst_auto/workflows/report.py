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
from pvsyst_auto.native.cli import parse_batch_results
from pvsyst_auto.project.paths import safe_filename, workspace_file
from pvsyst_auto.results import IRRAD_UNIT_KIND, RegularResultCsv, iter_aligned_result_rows
from pvsyst_auto.validation import finite_number as _finite

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
        "kpi_equity_irr": "资本金 IRR",
        "kpi_min_dscr": "最低偿债备付率 DSCR",
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
        "sec_batch_sweep": "参数扫描寻优曲线 (批处理结果)",
        "batch_opt_label": "最优点",
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
        "kpi_equity_irr": "Equity IRR",
        "kpi_min_dscr": "Min DSCR",
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
        "sec_batch_sweep": "Parametric Batch Sweep Curves",
        "batch_opt_label": "Optimum",
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
    """Single streaming pass to extract monthly PV yield/PR and loss-chain totals for charts."""
    return _summarize_multi_csv_for_report([csv_path], [1.0])


def _summarize_multi_csv_for_report(csv_paths: list[Path], weights: list[float]) -> dict[str, Any]:
    """Single streaming pass over one or more aligned SFI CSVs for merged monthly yield/PR and losses."""
    if not csv_paths or len(csv_paths) != len(weights):
        raise ValueError("csv_paths and weights must be non-empty lists of identical length")
    readers = [
        RegularResultCsv(
            p,
            empty_message=f"Result CSV {p.name} has no data rows",
            irregular_message=(
                f"Result CSV {p.name} timestamps do not have a regular cadence (missing, duplicate, or out-of-order rows); "
                "cannot compute monthly energy or losses reliably"
            ),
            required_any_of=("EToGrid", "E_Grid"),
            missing_required_message=f"Result CSV {p.name} must include E_Grid or EToGrid for report generation",
        )
        for p in csv_paths
    ]

    per_meta: list[dict[str, Any]] = []
    has_load = False
    monthly_pv_kwh = {m: 0.0 for m in range(1, 13)}
    monthly_pr_num = {m: 0.0 for m in range(1, 13)}
    monthly_pr_den = {m: 0.0 for m in range(1, 13)}
    loss_cols = ("EArray", "E_Grid", "EToGrid", "IL_Pmax", "IL_Vmax", "IL_Vmin", "IL_Imax", "ShdElec", "EGrdLim", "EUnused")
    loss_kwh_sums: dict[str, float] = {}
    loss_seen: set[str] = set()

    for stamp, _, items in iter_aligned_result_rows(readers):
        if not per_meta:
            for r in readers:
                idx = r.idx
                per_meta.append({
                    "reader": r,
                    "idx": idx,
                    "export_col": "EToGrid" if "EToGrid" in idx else "E_Grid",
                    "has_load": "E_User" in idx,
                    "has_import": "EFrGrid" in idx,
                    "pr_scale": _PR_SCALE.get(r.units.get("PR", ""), 1.0),
                })
                for lc in loss_cols:
                    if lc in idx:
                        loss_seen.add(lc)
                        loss_kwh_sums.setdefault(lc, 0.0)
            has_load = any(m["has_load"] for m in per_meta)

        for meta, w, (_, row_data, dt_k) in zip(per_meta, weights, items):
            idx = meta["idx"]
            reader_k = meta["reader"]

            for lc in loss_cols:
                if lc in idx:
                    loss_kwh_sums[lc] += w * reader_k.cell_kwh(row_data, lc, dt_k)

            raw_eg = reader_k.cell_kwh(row_data, meta["export_col"], dt_k)
            e_exp = max(0.0, raw_eg)
            if meta["has_load"]:
                e_user = max(0.0, reader_k.cell_kwh(row_data, "E_User", dt_k))
                if meta["has_import"]:
                    e_imp = max(0.0, reader_k.cell_kwh(row_data, "EFrGrid", dt_k))
                elif "E_Solar" in idx:
                    e_imp = max(0.0, e_user - max(0.0, reader_k.cell_kwh(row_data, "E_Solar", dt_k)))
                elif raw_eg < 0:
                    e_imp = -raw_eg
                else:
                    e_imp = 0.0
                self_cons = max(0.0, e_user - e_imp)
                monthly_pv_kwh[stamp.month] += w * (e_exp + self_cons)
            else:
                monthly_pv_kwh[stamp.month] += w * e_exp

            if "PR" in idx and "GlobInc" in idx:
                gi = row_data[idx["GlobInc"]]
                pr = row_data[idx["PR"]]
                if (isinstance(gi, (int, float)) and math.isfinite(gi) and gi > 0
                        and isinstance(pr, (int, float)) and math.isfinite(pr)):
                    unit = reader_k.units.get("GlobInc", "")
                    info = IRRAD_UNIT_KIND.get(unit)
                    if info is None:
                        raise ValueError(f"GlobInc requires an irradiance or irradiation unit, got {unit!r}")
                    scale, is_power = info
                    weight = w * gi * scale * (dt_k if is_power else 1.0)
                    monthly_pr_num[stamp.month] += weight * float(pr) * meta["pr_scale"]
                    monthly_pr_den[stamp.month] += weight

    rows = readers[0].rows_count
    step_hours = (readers[0].step_minutes if readers[0].step_minutes else 60.0) / 60.0
    monthly_kwh = [round(monthly_pv_kwh[m], 2) for m in range(1, 13)]
    monthly_pr_pct = [
        round((monthly_pr_num[m] / monthly_pr_den[m]) * 100.0, 2) if monthly_pr_den[m] > 0 else None
        for m in range(1, 13)
    ]
    total_pr_den = sum(monthly_pr_den.values())
    overall_weighted_pr = (sum(monthly_pr_num.values()) / total_pr_den) if total_pr_den > 0 else None
    losses = {col: round(loss_kwh_sums[col], 2) for col in loss_cols if col in loss_seen}
    return {
        "rows": rows,
        "step_minutes": step_hours * 60.0,
        "has_load": has_load,
        "monthly_kwh": monthly_kwh,
        "monthly_pr_pct": monthly_pr_pct,
        "overall_weighted_pr": round(overall_weighted_pr, 4) if overall_weighted_pr is not None else None,
        "losses_kwh": losses,
        "annual_e_grid_kwh": round(sum(monthly_pv_kwh.values()), 2),
    }


def _plotly_div(fig, *, include_js: bool | str) -> str:
    return fig.to_html(full_html=False, include_plotlyjs=include_js, config={"responsive": True, "displaylogo": False})


def _fmt_num(val: float | None, decimals: int = 2, suffix: str = "") -> str:
    if val is None or not math.isfinite(val):
        return "—"
    return f"{val:,.{decimals}f}{suffix}"


def _resolve_batch_csv(workspace: Path | None, batch_summary_csv: str) -> Path:
    if workspace is not None:
        safe = safe_filename(batch_summary_csv, ".csv")
        cand = workspace_file(workspace, "UserBatch", safe, ".csv")
        if cand.is_file():
            return cand
        raise FileNotFoundError(f"Batch summary CSV not found in UserBatch/: {safe}")
    p = Path(batch_summary_csv)
    if p.suffix.lower() != ".csv":
        raise ValueError("Expected a .csv file for batch_summary_csv")
    if not p.is_file():
        raise FileNotFoundError(f"Batch summary CSV not found: {p}")
    return p


def _try_float(val: Any) -> float | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)) and math.isfinite(val):
        return float(val)
    if isinstance(val, str):
        try:
            num = float(val.strip().replace(",", "."))
            return num if math.isfinite(num) else None
        except ValueError:
            return None
    return None


def _build_batch_sweep_figure(parsed_batch: dict[str, Any], *, palette: list[str], opt_label: str):
    """Build a Plotly dual-axis parametric curve figure from parse_batch_results()."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    rows = [
        r for r in (parsed_batch.get("scenarios") or ())
        if not r.get("error") and isinstance(r.get("values"), dict)
    ]
    if not rows:
        return None

    units = parsed_batch.get("units") or {}
    # Find output metric (prefer E_Grid, else first numeric output)
    out_metric = "E_Grid" if any(_try_float(r["values"].get("E_Grid")) is not None for r in rows) else None
    if out_metric is None:
        for k in rows[0]["values"]:
            if any(_try_float(r["values"].get(k)) is not None for r in rows):
                out_metric = k
                break
    if out_metric is None:
        return None
    has_pr = out_metric != "PR" and any(_try_float(r["values"].get("PR")) is not None for r in rows)
    pr_unit = units.get("PR", "")

    def pr_pct(value):
        v = _try_float(value)
        return v * 100 if v is not None and (pr_unit == "ratio" or (not pr_unit and v <= 1.5)) else v

    # Identify varied parameter columns
    param_keys: list[str] = []
    for r in rows:
        for k in (r.get("parameters") or ()):
            if k not in param_keys:
                param_keys.append(k)
    varied: list[str] = []
    for k in param_keys:
        distinct = {str((r.get("parameters") or {}).get(k, "")).strip() for r in rows}
        if len(distinct) > 1:
            varied.append(k)

    fig = make_subplots(specs=[[{"secondary_y": has_pr}]])
    y_unit = units.get(out_metric, "")
    y_title = f"{out_metric} ({y_unit})" if y_unit else out_metric

    if not varied:
        # Fallback: plot by scenario ID
        xs = [str(r.get("id") or f"SIM_{i + 1}") for i, r in enumerate(rows)]
        ys = [_try_float(r["values"].get(out_metric)) for r in rows]
        fig.add_trace(
            go.Scatter(name=y_title, x=xs, y=ys, mode="lines+markers", line=dict(color=palette[0], width=2.5)),
            secondary_y=False,
        )
        if has_pr:
            prs = [pr_pct(r["values"].get("PR")) for r in rows]
            fig.add_trace(
                go.Scatter(name="PR (%)", x=xs, y=prs, mode="lines+markers",
                           line=dict(color="#059669", width=2, dash="dash")),
                secondary_y=True,
            )
        x_title = "Scenario"
    elif len(varied) == 1:
        x_key = varied[0]
        points = []
        for idx_r, r in enumerate(rows):
            raw_x = (r.get("parameters") or {}).get(x_key)
            num_x = _try_float(raw_x)
            y_val = _try_float(r["values"].get(out_metric))
            pr_val = pr_pct(r["values"].get("PR"))
            if y_val is not None:
                points.append((num_x if num_x is not None else idx_r, raw_x if num_x is None else num_x, y_val, pr_val))
        points.sort(key=lambda item: item[0])
        xs = [p[1] for p in points]
        ys = [p[2] for p in points]
        fig.add_trace(
            go.Scatter(name=y_title, x=xs, y=ys, mode="lines+markers",
                       line=dict(color=palette[0], width=2.5), marker=dict(size=8)),
            secondary_y=False,
        )
        if points:
            best = max(points, key=lambda p: p[2])
            fig.add_trace(
                go.Scatter(
                    name=f"{opt_label} ({best[1]})",
                    x=[best[1]], y=[best[2]], mode="markers",
                    marker=dict(symbol="star", size=14, color="#dc2626"),
                ),
                secondary_y=False,
            )
        if has_pr and any(p[3] is not None for p in points):
            fig.add_trace(
                go.Scatter(name="PR (%)", x=xs, y=[p[3] for p in points], mode="lines+markers",
                           line=dict(color="#059669", width=2, dash="dash")),
                secondary_y=True,
            )
        x_title = x_key
    else:
        x_key = varied[0]
        groups: dict[tuple[str, ...], list[tuple[float, Any, float, float | None]]] = {}
        for idx_r, r in enumerate(rows):
            params = r.get("parameters") or {}
            group = tuple(str(params.get(k, "")) for k in varied[1:])
            raw_x = params.get(x_key)
            num_x = _try_float(raw_x)
            y_val = _try_float(r["values"].get(out_metric))
            pr_val = pr_pct(r["values"].get("PR"))
            if y_val is not None:
                groups.setdefault(group, []).append(
                    (num_x if num_x is not None else idx_r, raw_x if num_x is None else num_x, y_val, pr_val)
                )
        for g_idx, (group, pts) in enumerate(sorted(groups.items())):
            g_lbl = ", ".join(f"{k}={v}" for k, v in zip(varied[1:], group))
            pts.sort(key=lambda item: item[0])
            color = palette[g_idx % len(palette)]
            fig.add_trace(
                go.Scatter(
                    name=f"{g_lbl} ({out_metric})",
                    x=[p[1] for p in pts], y=[p[2] for p in pts],
                    mode="lines+markers", line=dict(color=color, width=2.5),
                ),
                secondary_y=False,
            )
            if has_pr and any(p[3] is not None for p in pts):
                fig.add_trace(
                    go.Scatter(
                        name=f"{g_lbl} PR (%)",
                        x=[p[1] for p in pts], y=[p[3] for p in pts],
                        mode="lines", line=dict(color=color, width=1.8, dash="dot"),
                    ),
                    secondary_y=True,
                )
        x_title = x_key

    fig.update_layout(
        template="plotly_white", height=420,
        margin=dict(l=60, r=55, t=35, b=50),
        xaxis_title=x_title,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
    )
    fig.update_yaxes(title_text=y_title, secondary_y=False)
    if has_pr:
        fig.update_yaxes(title_text="PR (%)", secondary_y=True)
    return fig


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
    batch_summary_csv: str | None = None,
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
        csv_names = sc.get("csv_names")
        if csv_name and csv_names:
            raise ValueError(f"scenarios[{idx_s}] ({label}): specify either csv_name or csv_names, not both")
        proj = sc.get("project")
        var = sc.get("variant")
        if csv_names:
            if not isinstance(csv_names, list) or not 1 <= len(csv_names) <= 20:
                raise ValueError(f"scenarios[{idx_s}] ({label}): csv_names must contain 1..20 entries")
            csv_paths = [_resolve_csv(workspace, name) for name in csv_names]
            raw_weights = sc.get("csv_weights")
            if raw_weights is None:
                weights = [1.0] * len(csv_paths)
            else:
                if not isinstance(raw_weights, list) or len(raw_weights) != len(csv_paths):
                    raise ValueError(f"scenarios[{idx_s}] ({label}): csv_weights length must match csv_names")
                weights = [
                    _finite(w, f"scenarios[{idx_s}].csv_weights[{wi}]", minimum=1e-9)
                    for wi, w in enumerate(raw_weights)
                ]
            csv_stats = _summarize_multi_csv_for_report(csv_paths, weights)
            member_diags = [
                diagnose_simulation_csv(p, store=None, project=None, variant=None, thresholds=thresholds)
                for p in csv_paths
            ]
            merged_findings: list[dict[str, Any]] = []
            for p, d in zip(csv_paths, member_diags):
                for f in d.get("findings") or ():
                    item = dict(f)
                    if len(csv_paths) > 1:
                        item["message"] = f"[{p.name}] {item.get('message', '')}"
                    merged_findings.append(item)
            diag = {
                "status": "blocked" if any(d.get("status") == "blocked" for d in member_diags) else "ok",
                "needs_review": any(bool(d.get("needs_review")) for d in member_diags),
                "finding_count": len(merged_findings),
                "findings": merged_findings,
                "weighted_pr": csv_stats.get("overall_weighted_pr"),
            }
            csv_label = ", ".join(p.name for p in csv_paths)
            econ_entry: dict[str, Any] = {"label": label, "csv_paths": csv_paths, "csv_weights": weights}
        else:
            if not csv_name:
                raise ValueError(f"scenarios[{idx_s}] ({label}) requires csv_name or csv_names")
            if sc.get("csv_weights") is not None:
                raise ValueError(f"scenarios[{idx_s}] ({label}): csv_weights requires csv_names")
            csv_path = _resolve_csv(workspace, csv_name)
            diag = diagnose_simulation_csv(
                csv_path,
                store=store if (proj and var) else None,
                project=proj,
                variant=var,
                thresholds=thresholds,
            )
            csv_stats = _summarize_csv_for_report(csv_path)
            csv_label = csv_path.name
            econ_entry = {"label": label, "csv_path": csv_path}
        parsed_scenarios.append({
            "label": label,
            "csv_name": csv_label,
            "project": proj,
            "variant": var,
            "installed_dc_kw": sc.get("installed_dc_kw"),
            "battery_kwh": sc.get("battery_kwh"),
            "stats": csv_stats,
            "diagnostics": diag,
        })
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

    palette = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#dc2626", "#0891b2"]
    fig_batch = None
    if batch_summary_csv:
        batch_path = _resolve_batch_csv(workspace, batch_summary_csv)
        fig_batch = _build_batch_sweep_figure(parse_batch_results(batch_path, columns=None), palette=palette, opt_label=t["batch_opt_label"])

    if dry_run:
        return {
            "status": "ready",
            "dry_run": True,
            "report_path": str(out_path),
            "scenario_count": len(parsed_scenarios),
            "language": language,
            "plotlyjs": plotlyjs,
            "has_economics": econ_result is not None,
            "has_batch_sweep": fig_batch is not None,
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
            else losses.get("EToGrid", losses.get("E_Grid", 0.0))
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
        low_npvs = [round(float(r["low"]["npv"]), 2) for r in s_rows]
        high_npvs = [round(float(r["high"]["npv"]), 2) for r in s_rows]
        low_deltas = [round(v - base_npv, 2) for v in low_npvs]
        high_deltas = [round(v - base_npv, 2) for v in high_npvs]
        var_pct = primary_sens.get("variation_pct", 15)
        fig_tor = go.Figure()
        fig_tor.add_trace(go.Bar(
            name=f"{t['sens_low']} (-{var_pct:g}%)",
            y=y_labels, x=low_deltas, base=base_npv, customdata=low_npvs, orientation="h",
            marker_color="#ef4444", opacity=0.85,
            hovertemplate="%{y} (-" + f"{var_pct:g}%): NPV=%{{customdata:,.0f}} ({econ_result['currency']})<extra></extra>",
        ))
        fig_tor.add_trace(go.Bar(
            name=f"{t['sens_high']} (+{var_pct:g}%)",
            y=y_labels, x=high_deltas, base=base_npv, customdata=high_npvs, orientation="h",
            marker_color="#2563eb", opacity=0.85,
            hovertemplate="%{y} (+" + f"{var_pct:g}%): NPV=%{{customdata:,.0f}} ({econ_result['currency']})<extra></extra>",
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

    # 8. Optional Parametric Batch Sweep Curve Chart (from UserBatch *Results.CSV)
    if fig_batch is not None:
        chart_sections.append((t["sec_batch_sweep"], _plotly_div(fig_batch, include_js=next_js())))

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
        fin_sum = pf.get("financing_summary")
        if isinstance(fin_sum, dict):
            kpis.append((t["kpi_equity_irr"], _fmt_num(fin_sum.get("equity_irr_pct"), 2, " %")))
            if fin_sum.get("min_dscr") is not None:
                kpis.append((t["kpi_min_dscr"], _fmt_num(fin_sum.get("min_dscr"), 2, "x")))

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
        "has_batch_sweep": fig_batch is not None,
        "best_scenario": best_label,
    }

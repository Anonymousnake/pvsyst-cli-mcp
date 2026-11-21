"""Two-stage hybrid PV + BESS sizing optimizer (fast pure-Python grid sweep + optional PVsystCLI verification).

Stage 1 loads a baseline hourly or sub-hourly PV CSV once into memory and evaluates up to 200
(bess_kwh x pcs_kw x strategy) candidates against TOU + two-part demand-charge tariffs and 25-year
degraded cashflows (including mid-life battery replacement) in milliseconds, producing full rankings,
a CAPEX-vs-NPV Pareto frontier, and a 2D heatmap ready for ``pvsyst_generate_report``.

Stage 2 (optional, ``verify_top_k`` in 1..5 with ``dry_run=False`` and ``confirm=True``) clones the
baseline variant for the top-K candidates, installs real AC-coupled storage hardware and schedules via
``create_grid_storage_system``, executes ``PVsystCLI``, and reconciles fast-screen vs PVsyst-exact KPIs.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pvsyst_auto.energy.tou_planner import plan_tou_storage_schedule
from pvsyst_auto.engineering.economics import (
    _battery_replacement_event,
    _compute_cashflow_metrics,
    _lookup_rate,
    _resolve_result_csv,
    _resolve_system_capex,
    _validate_financial,
    _validate_tariff,
    evaluate_economics,
)
from pvsyst_auto.native.cli import build_sfi
from pvsyst_auto.results import RegularResultCsv, energy_unit_scale
from pvsyst_auto.validation import finite_number as _finite

_VALID_STRATEGIES = ("self_consumption", "tou_arbitrage", "peak_shaving")
_VALID_RANK_BY = ("npv", "irr", "lcoe", "simple_payback_years", "discounted_payback_years")


def _load_baseline_series(
    csv_path: Path,
    *,
    tariff: dict,
    load_profile: dict | None,
) -> dict[str, Any]:
    """Stream baseline PV (+ optional E_User load) CSV once into flat arrays for fast BESS sweeps."""
    # Validate optional external load profile
    ext_mode = None
    ext_const_kw = 0.0
    ext_daily_kw: list[float] = []
    if load_profile is not None:
        if not isinstance(load_profile, dict):
            raise ValueError("load_profile must be an object")
        ext_mode = load_profile.get("mode")
        if ext_mode == "constant":
            ext_const_kw = _finite(load_profile.get("power_kw"), "load_profile.power_kw", minimum=0.0)
        elif ext_mode == "daily":
            arr = load_profile.get("daily_power_kw")
            if not isinstance(arr, list) or len(arr) != 24:
                raise ValueError("load_profile.daily_power_kw must contain exactly 24 numbers")
            ext_daily_kw = [_finite(v, f"load_profile.daily_power_kw[{i}]", minimum=0.0) for i, v in enumerate(arr)]
        else:
            raise ValueError("load_profile.mode must be 'constant' or 'daily'")

    reader = RegularResultCsv(
        csv_path,
        empty_message="Baseline CSV contains no rows",
        irregular_message=(
            "Baseline CSV timestamps do not have a regular cadence (missing, duplicate, or out-of-order rows); "
            "cannot simulate BESS dispatch reliably"
        ),
        required_any_of=("E_Grid", "EToGrid"),
        missing_required_message="Baseline CSV must contain E_Grid or EToGrid",
    )

    idx: dict[str, int] = {}
    pv_col: str | None = None
    has_load_col = False
    has_etogrid = False
    has_esolar = False
    has_efrgrid = False
    col_scale: dict[str, tuple[float, bool]] = {}

    months: list[int] = []
    hours: list[int] = []
    pv_kw: list[float] = []
    load_kw: list[float] = []
    imp_rates: list[float] = []
    exp_rates: list[float] = []

    for stamp, row, dt_hours in reader.iter_rows():
        if not col_scale:
            idx = reader.idx
            pv_col = "E_Grid" if "E_Grid" in idx else "EToGrid"
            has_load_col = "E_User" in idx
            has_etogrid = "EToGrid" in idx
            has_esolar = "E_Solar" in idx
            has_efrgrid = "EFrGrid" in idx
            col_scale = {col: energy_unit_scale(reader.units.get(col, "")) for col in idx}

        def kw(col_name: str) -> float:
            col_i = idx.get(col_name)
            if col_i is None:
                return 0.0
            factor, is_power = col_scale[col_name]
            if factor == 0.0:
                raise ValueError(f"{col_name} requires a power or energy unit, got {reader.units.get(col_name, '')!r}")
            raw = row[col_i]
            val = float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else 0.0
            return val * factor if is_power else (val * factor / dt_hours)

        csv_l_kw = max(0.0, kw("E_User")) if has_load_col else 0.0
        if has_etogrid and (has_esolar or has_load_col):
            # True PVsyst self-consumption/storage export: reconstruct total AC PV = export + self-consumed PV
            exp_kw = max(0.0, kw("EToGrid"))
            if has_esolar:
                sc_kw = max(0.0, kw("E_Solar"))
            elif has_efrgrid:
                sc_kw = max(0.0, csv_l_kw - max(0.0, kw("EFrGrid")))
            else:
                sc_kw = 0.0
            p_kw = exp_kw + sc_kw
        elif has_esolar:
            p_kw = max(0.0, kw(pv_col)) + max(0.0, kw("E_Solar"))
        else:
            p_kw = max(0.0, kw(pv_col))

        if ext_mode == "constant":
            l_kw = max(0.0, ext_const_kw)
        elif ext_mode == "daily":
            l_kw = max(0.0, ext_daily_kw[stamp.hour])
        elif has_load_col:
            l_kw = csv_l_kw
        else:
            l_kw = 0.0
        _, imp_r, exp_r = _lookup_rate(stamp, tariff)
        months.append(stamp.month)
        hours.append(stamp.hour)
        pv_kw.append(p_kw)
        load_kw.append(l_kw)
        imp_rates.append(imp_r)
        exp_rates.append(exp_r)

    return {
        "rows": reader.rows_count,
        "dt_hours": (reader.step_minutes if reader.step_minutes is not None else 60.0) / 60.0,
        "has_load": has_load_col or (ext_mode is not None),
        "months": months,
        "hours": hours,
        "pv_kw": pv_kw,
        "load_kw": load_kw,
        "imp_rates": imp_rates,
        "exp_rates": exp_rates,
    }


def _simulate_candidate_fast(
    *,
    series: dict[str, Any],
    bess_kwh: float,
    pcs_kw: float,
    strategy: str,
    tariff: dict,
    bess_params: dict[str, Any],
    charge_hours: frozenset[int],
    discharge_hours: frozenset[int],
) -> dict[str, Any]:
    """Deterministic hourly/sub-hourly AC-coupled BESS dispatch model with demand-charge tracking."""
    dt = series["dt_hours"]
    months = series["months"]
    hours = series["hours"]
    pv_list = series["pv_kw"]
    ld_list = series["load_kw"]
    imp_list = series["imp_rates"]
    exp_list = series["exp_rates"]
    has_load = series["has_load"]

    soc_min_f = bess_params["soc_min"]
    soc_max_f = bess_params["soc_max"]
    soc_init_f = bess_params["initial_soc"]
    rte = bess_params["round_trip_efficiency"]
    allow_grid_ch = bess_params["allow_grid_charging"]
    peak_limit_cfg = bess_params.get("peak_shaving_limit_kw")

    eta_ch = math.sqrt(rte)
    eta_dis = math.sqrt(rte)
    e_min = bess_kwh * soc_min_f
    e_max = bess_kwh * soc_max_f
    usable_kwh = max(0.0, e_max - e_min)
    soc_kwh = min(e_max, max(e_min, bess_kwh * soc_init_f)) if bess_kwh > 0 else 0.0

    # Derive sensible peak limit for peak_shaving if omitted
    max_load = max(ld_list) if ld_list else 0.0
    if peak_limit_cfg is not None:
        peak_limit = peak_limit_cfg
    else:
        peak_limit = max(0.0, max_load - 0.85 * pcs_kw)

    # Demand ceiling for grid-charging in tou_arbitrage when demand_charge is active
    # so overnight grid charging never exceeds the natural baseline peak demand!
    dc_cfg = tariff.get("demand_charge")
    base_month_peaks = [0.0] * 13
    for m, l_kw in zip(months, ld_list):
        if l_kw > base_month_peaks[m]:
            base_month_peaks[m] = l_kw
    act_month_peaks = [0.0] * 13

    pv_export_kwh = 0.0
    grid_import_kwh = 0.0
    load_kwh = 0.0
    self_cons_kwh = 0.0
    bat_ch_ac_kwh = 0.0
    bat_dis_ac_kwh = 0.0
    export_rev = 0.0
    baseline_bill = 0.0
    actual_bill = 0.0

    eff_pcs = pcs_kw if bess_kwh > 0 else 0.0

    for m, h, p_kw, l_kw, imp_r, exp_r in zip(months, hours, pv_list, ld_list, imp_list, exp_list):
        # Step 1: Direct PV -> Load
        direct_pv = min(p_kw, l_kw) if has_load else 0.0
        excess_pv = max(0.0, p_kw - direct_pv)
        net_load = max(0.0, l_kw - direct_pv) if has_load else 0.0

        ch_ac_kw = 0.0
        dis_ac_kw = 0.0

        if eff_pcs > 0.0 and usable_kwh > 0.0:
            max_ch_by_soc = max(0.0, (e_max - soc_kwh) / (eta_ch * dt))
            max_dis_by_soc = max(0.0, (soc_kwh - e_min) * eta_dis / dt)

            if strategy == "self_consumption":
                if excess_pv > 0.0:
                    ch_ac_kw = min(excess_pv, eff_pcs, max_ch_by_soc)
                elif net_load > 0.0:
                    dis_ac_kw = min(net_load, eff_pcs, max_dis_by_soc)

            elif strategy == "peak_shaving":
                if net_load > peak_limit:
                    need_shave = net_load - peak_limit
                    dis_ac_kw = min(need_shave, eff_pcs, max_dis_by_soc)
                elif excess_pv > 0.0:
                    ch_ac_kw = min(excess_pv, eff_pcs, max_ch_by_soc)
                elif allow_grid_ch and has_load and net_load < peak_limit * 0.85 and (h in charge_hours or not charge_hours):
                    headroom = max(0.0, peak_limit - net_load)
                    ch_ac_kw = min(headroom, eff_pcs, max_ch_by_soc)

            else:  # tou_arbitrage
                if h in discharge_hours and net_load > 0.0:
                    dis_ac_kw = min(net_load, eff_pcs, max_dis_by_soc)
                elif h in charge_hours:
                    if allow_grid_ch and has_load:
                        # Respect baseline peak ceiling if demand charge is active
                        grid_cap = (
                            max(0.0, base_month_peaks[m] * 0.98 - net_load)
                            if (dc_cfg is not None and base_month_peaks[m] > 0)
                            else eff_pcs
                        )
                        desired_ch = min(eff_pcs, excess_pv + grid_cap)
                        ch_ac_kw = min(desired_ch, eff_pcs, max_ch_by_soc)
                    elif excess_pv > 0.0:
                        ch_ac_kw = min(excess_pv, eff_pcs, max_ch_by_soc)
                elif excess_pv > 0.0:
                    # Outside explicit windows, still capture surplus PV if export tariff is lower than import
                    ch_ac_kw = min(excess_pv, eff_pcs, max_ch_by_soc)

            soc_kwh += (ch_ac_kw * eta_ch - dis_ac_kw / eta_dis) * dt
            soc_kwh = min(e_max, max(e_min, soc_kwh))

        # Power balance at grid meter
        pv_to_bat = min(excess_pv, ch_ac_kw)
        grid_to_bat = max(0.0, ch_ac_kw - pv_to_bat)
        export_kw = max(0.0, excess_pv - pv_to_bat)
        import_kw = max(0.0, net_load - dis_ac_kw) + grid_to_bat

        if import_kw > act_month_peaks[m]:
            act_month_peaks[m] = import_kw

        e_exp = export_kw * dt
        e_imp = import_kw * dt
        e_ld = l_kw * dt

        if has_load:
            e_sc = (direct_pv + pv_to_bat) * dt
            b_bill = e_ld * imp_r
            a_bill = e_imp * imp_r
            e_rev = e_exp * exp_r
        else:
            sc_ratio = (
                tariff["self_consumption_ratio"]
                if (tariff["mode"] != "feed_in" and tariff["self_consumption_ratio"] is not None)
                else 0.0
            )
            e_sc = e_exp * sc_ratio
            e_exp = e_exp - e_sc
            b_bill = e_sc * imp_r
            a_bill = e_imp * imp_r
            e_rev = e_exp * exp_r

        pv_export_kwh += e_exp
        grid_import_kwh += e_imp
        load_kwh += e_ld
        self_cons_kwh += e_sc
        bat_ch_ac_kwh += ch_ac_kw * dt
        bat_dis_ac_kwh += dis_ac_kw * dt

        baseline_bill += b_bill
        actual_bill += a_bill
        export_rev += e_rev

    # Demand charge savings
    y1_demand_savings = 0.0
    avg_peak_red = 0.0
    if dc_cfg is not None and has_load:
        rate = dc_cfg["demand_rate_per_kw_month"]
        active_months = dc_cfg["months"]
        base_dc = sum(base_month_peaks[m] * rate for m in active_months)
        act_dc = sum(act_month_peaks[m] * rate for m in active_months)
        avg_peak_red = sum(base_month_peaks[m] - act_month_peaks[m] for m in active_months) / max(1, len(active_months))
        if dc_cfg["billing_mode"] == "demand":
            y1_demand_savings = base_dc - act_dc
        elif dc_cfg["billing_mode"] == "capacity":
            y1_demand_savings = 0.0
        else:
            cap_charge = len(active_months) * dc_cfg["contract_capacity_kva"] * dc_cfg["capacity_rate_per_kva_month"]
            y1_demand_savings = min(base_dc, cap_charge) - min(act_dc, cap_charge)

    y1_energy_kwh = sum(pv_list) * dt
    y1_energy_benefit = export_rev + (baseline_bill - actual_bill)
    y1_gross_benefit = y1_energy_benefit + y1_demand_savings
    eq_cycles = (bat_dis_ac_kwh / usable_kwh) if usable_kwh > 0 else 0.0
    sc_rate_pct = (self_cons_kwh / y1_energy_kwh * 100.0) if y1_energy_kwh > 0 else 0.0

    return {
        "year1_energy_kwh": y1_energy_kwh,
        "year1_energy_benefit": y1_energy_benefit,
        "year1_demand_savings": y1_demand_savings,
        "year1_gross_benefit": y1_gross_benefit,
        "pv_export_kwh": round(pv_export_kwh, 2),
        "grid_import_kwh": round(grid_import_kwh, 2),
        "self_consumed_kwh": round(self_cons_kwh, 2),
        "self_consumption_pct": round(sc_rate_pct, 2),
        "battery_charge_kwh": round(bat_ch_ac_kwh, 2),
        "battery_discharge_kwh": round(bat_dis_ac_kwh, 2),
        "equivalent_full_cycles": round(eq_cycles, 1),
        "avg_monthly_peak_reduction_kw": round(avg_peak_red, 2),
    }


def _compute_pareto_frontier(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Non-dominated Pareto frontier minimizing CAPEX and maximizing NPV."""
    ordered = sorted(candidates, key=lambda c: (c["capex_total"], -c["npv"]))
    frontier: list[dict[str, Any]] = []
    best_npv = -math.inf
    for item in ordered:
        if item["npv"] > best_npv + 1e-6:
            best_npv = item["npv"]
            frontier.append({
                "label": item["label"],
                "strategy": item["strategy"],
                "bess_kwh": item["bess_kwh"],
                "pcs_kw": item["pcs_kw"],
                "capex_total": item["capex_total"],
                "npv": item["npv"],
                "irr_pct": item["irr_pct"],
                "simple_payback_years": item["simple_payback_years"],
            })
    return frontier


def _run_stage2_verify(
    *,
    cli,
    store,
    top_candidates: list[dict[str, Any]],
    verify_config: dict[str, Any],
    tariff: dict,
    financial: dict,
    fin_cfg: dict,
    installed_dc_kw: float,
    pcs_capex_per_kw: float,
    bess_params: dict[str, Any],
    tou_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Clone baseline variant for top-K candidates, install real AC storage, simulate via PVsystCLI, and reconcile."""
    from pvsyst_auto.energy.system import create_grid_storage_system

    project = str(verify_config["project"])
    source_variant = str(verify_config["source_variant"])
    bat_file = str(verify_config["battery_file_name"])
    bat_lib = str(verify_config.get("battery_library", "workspace"))
    bat_unit_kwh = _finite(verify_config["battery_unit_kwh"], "verify_config.battery_unit_kwh", minimum=0.01)
    series_count = int(verify_config.get("series_count", 1))
    monthly_temp = list(verify_config.get("monthly_temperature_c") or [20.0] * 12)
    cycle_sow = float(verify_config.get("cycle_sow", 0.8))
    static_sow = float(verify_config.get("static_sow", 0.8))
    load_manifest = verify_config.get("load_manifest_name")
    prefix = str(verify_config.get("variant_prefix") or "VB").strip()

    ws = cli.workspace
    sfi_path = ws / "Models" / f"{prefix}_bess_verify.SFI"
    if not sfi_path.is_file():
        build_sfi(sfi_path, "grid_storage", time_step="hour")

    comparisons: list[dict[str, Any]] = []
    for idx, cand in enumerate(top_candidates, start=1):
        new_var = f"{prefix}{idx}"
        parallel = max(1, int(round(cand["bess_kwh"] / (bat_unit_kwh * series_count))))
        actual_kwh = round(parallel * series_count * bat_unit_kwh, 2)
        pcs_kw = max(0.1, float(cand["pcs_kw"]))

        # Map optimizer strategy -> PVsyst native storage strategy
        strat = cand["strategy"]
        if strat == "tou_arbitrage" and tou_plan and tou_plan.get("planned_cycles_count", 0) > 0:
            storage_cfg = {
                "coupling": "ac_coupled",
                "strategy": "power_shift",
                "charging_strategy": "spec_charg_power",
                "discharging_strategy": "specified_hours",
                "charging_schedule_48": [min(v, pcs_kw) for v in tou_plan["charging_schedule_48"]],
                "discharging_schedule_48": [min(v, pcs_kw) for v in tou_plan["discharging_schedule_48"]],
            }
        elif strat == "peak_shaving":
            storage_cfg = {
                "coupling": "ac_coupled",
                "strategy": "peak_shaving",
                "charging_strategy": "pv_to_grid_min",
                "discharging_strategy": "after_sunset",
                "grid_limit_kw": float(bess_params.get("peak_shaving_limit_kw") or max(1.0, installed_dc_kw * 0.5)),
            }
        else:
            storage_cfg = {
                "coupling": "ac_coupled",
                "strategy": "self_consumption",
            }

        eta_one_pct = round(math.sqrt(bess_params["round_trip_efficiency"]) * 100.0, 2)
        storage_cfg.update({
            "soc_min": bess_params["soc_min"],
            "soc_max": bess_params["soc_max"],
            "initial_soc": bess_params["initial_soc"],
            "reserve_soc": bess_params["soc_min"],
            "max_charge_ac_kw": pcs_kw,
            "max_discharge_ac_kw": pcs_kw,
            "charger_max_efficiency_pct": min(99.5, eta_one_pct + 1.0),
            "charger_euro_efficiency_pct": eta_one_pct,
            "battery_inverter_max_efficiency_pct": min(99.5, eta_one_pct + 1.0),
            "battery_inverter_euro_efficiency_pct": eta_one_pct,
        })

        battery_cfg = {
            "file_name": bat_file,
            "library": bat_lib,
            "series_count": series_count,
            "parallel_count": parallel,
            "monthly_temperature_c": monthly_temp,
            "cycle_sow": cycle_sow,
            "static_sow": static_sow,
        }
        create_grid_storage_system(
            store,
            project=project,
            source_variant=source_variant,
            new_variant=new_var,
            battery=battery_cfg,
            storage=storage_cfg,
            load_manifest_name=None if strat == "peak_shaving" else load_manifest,
            dry_run=False,
        )

        out_csv = ws / "Results" / f"{prefix}_{idx}_{int(round(actual_kwh))}kWh_{int(round(pcs_kw))}kW.csv"
        sim_res = cli.run_simulation(project, new_var, sfi=sfi_path, out_csv=out_csv, log_level=3)
        stage2_sc: dict[str, Any] = {
            "label": cand["label"],
            "csv_name": out_csv.name,
            "installed_dc_kw": installed_dc_kw,
            "battery_kwh": actual_kwh,
        }
        if pcs_capex_per_kw > 0:
            capex = _resolve_system_capex(
                fin_cfg,
                dc_kw=installed_dc_kw,
                battery_kwh=actual_kwh,
                pcs_kw=pcs_kw,
                pcs_capex_per_kw=pcs_capex_per_kw,
            )
            stage2_sc["capex_total"] = round(capex, 2)
        exact_econ = evaluate_economics(
            workspace=ws,
            scenarios=[stage2_sc],
            financial=financial,
            tariff=tariff,
            include_cashflow_schedule=False,
        )
        sc_exact = exact_econ["scenarios"][0]
        ef = sc_exact["financials"]
        comparisons.append({
            "rank": idx,
            "label": cand["label"],
            "variant": new_var,
            "csv_name": out_csv.name,
            "actual_battery_kwh": actual_kwh,
            "parallel_count": parallel,
            "pcs_kw": pcs_kw,
            "capex_total": ef["capex_total"],
            "warnings": sim_res.get("warnings") or [],
            "fast_model": {
                "npv": cand["npv"],
                "irr_pct": cand["irr_pct"],
                "year1_gross_benefit": cand["year1_gross_benefit"],
            },
            "pvsyst_exact": {
                "npv": ef["npv"],
                "irr_pct": ef["irr_pct"],
                "lcoe": ef["lcoe"],
                "simple_payback_years": ef["simple_payback_years"],
                "year1_gross_benefit": ef["year1_gross_benefit"],
                "year1_energy_kwh": sc_exact["year1_energy_kwh"],
            },
            "deltas": {
                "npv_diff": round(ef["npv"] - cand["npv"], 2),
                "irr_diff_pct": (
                    round(ef["irr_pct"] - cand["irr_pct"], 3)
                    if (ef["irr_pct"] is not None and cand["irr_pct"] is not None) else None
                ),
            },
        })
    return comparisons


def optimize_bess_sizing(
    *,
    workspace: Path | None = None,
    cli=None,
    store=None,
    baseline_csv: str | Path,
    installed_dc_kw: float,
    bess_kwh_values: list[float],
    pcs_kw_values: list[float],
    tariff: dict,
    financial: dict,
    strategies: list[str] | None = None,
    load_profile: dict | None = None,
    bess_params: dict[str, Any] | None = None,
    pcs_capex_per_kw: float = 0.0,
    rank_by: str = "npv",
    verify_top_k: int = 0,
    verify_config: dict[str, Any] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Run Stage 1 fast PV+BESS grid optimization and optional Stage 2 PVsystCLI verification."""
    dc_kw = _finite(installed_dc_kw, "installed_dc_kw", minimum=0.01)
    pcs_capex_kw = _finite(pcs_capex_per_kw, "pcs_capex_per_kw", minimum=0.0)
    if rank_by not in _VALID_RANK_BY:
        raise ValueError(f"rank_by must be one of {_VALID_RANK_BY}")
    if type(verify_top_k) is not int or not 0 <= verify_top_k <= 5:
        raise ValueError("verify_top_k must be an integer in 0..5")
    if type(dry_run) is not bool or type(confirm) is not bool:
        raise ValueError("dry_run and confirm must be strict booleans")

    if not isinstance(bess_kwh_values, list) or not 1 <= len(bess_kwh_values) <= 20:
        raise ValueError("bess_kwh_values must contain 1..20 numbers")
    if not isinstance(pcs_kw_values, list) or not 1 <= len(pcs_kw_values) <= 15:
        raise ValueError("pcs_kw_values must contain 1..15 numbers")
    bess_grid = sorted({_finite(v, "bess_kwh_values[]", minimum=0.0) for v in bess_kwh_values})
    pcs_grid = sorted({_finite(v, "pcs_kw_values[]", minimum=0.01) for v in pcs_kw_values})

    fin_cfg = _validate_financial(financial)
    tar_cfg = _validate_tariff(tariff)

    if strategies is None:
        strat_list = ["tou_arbitrage"] if tar_cfg["mode"] == "tou" else ["self_consumption"]
    else:
        if not isinstance(strategies, list) or not strategies:
            raise ValueError("strategies must be a non-empty list")
        strat_list = []
        for s in strategies:
            if s not in _VALID_STRATEGIES:
                raise ValueError(f"strategies entries must be in {_VALID_STRATEGIES}")
            if s not in strat_list:
                strat_list.append(s)

    total_combos = len(bess_grid) * len(pcs_grid) * len(strat_list)
    if total_combos > 200:
        raise ValueError(f"Grid size {total_combos} exceeds maximum 200 combinations")

    bp_raw = dict(bess_params or {})
    bp = {
        "soc_min": _finite(bp_raw.get("soc_min", 0.10), "bess_params.soc_min", minimum=0.0, maximum=0.9),
        "soc_max": _finite(bp_raw.get("soc_max", 0.95), "bess_params.soc_max", minimum=0.1, maximum=1.0),
        "initial_soc": _finite(bp_raw.get("initial_soc", 0.20), "bess_params.initial_soc", minimum=0.0, maximum=1.0),
        "round_trip_efficiency": _finite(bp_raw.get("round_trip_efficiency", 0.88), "bess_params.round_trip_efficiency", minimum=0.5, maximum=1.0),
        "allow_grid_charging": bool(bp_raw.get("allow_grid_charging", True)),
        "cycles_per_day": int(bp_raw.get("cycles_per_day", 2)),
        "min_spread": _finite(bp_raw.get("min_spread", 0.05), "bess_params.min_spread", minimum=0.0),
        "peak_shaving_limit_kw": (
            _finite(bp_raw["peak_shaving_limit_kw"], "bess_params.peak_shaving_limit_kw", minimum=0.0)
            if bp_raw.get("peak_shaving_limit_kw") is not None else None
        ),
    }
    if bp["soc_min"] >= bp["soc_max"]:
        raise ValueError("bess_params.soc_min must be < bess_params.soc_max")

    csv_path = _resolve_result_csv(workspace, baseline_csv)
    series = _load_baseline_series(csv_path, tariff=tar_cfg, load_profile=load_profile)

    # Derive TOU charge/discharge windows once if TOU tariff is present
    tou_plan: dict[str, Any] | None = None
    charge_hours: frozenset[int] = frozenset()
    discharge_hours: frozenset[int] = frozenset()
    if tar_cfg["mode"] == "tou" and tariff.get("tou_schedule"):
        tou_plan = plan_tou_storage_schedule(
            tou_schedule=tariff["tou_schedule"],
            charge_power_kw=max(pcs_grid),
            discharge_power_kw=max(pcs_grid),
            cycles_per_day=bp["cycles_per_day"],
            round_trip_efficiency=bp["round_trip_efficiency"],
            min_spread=bp["min_spread"],
            default_import_rate=tar_cfg["import_tariff"],
            default_export_rate=tar_cfg["feed_in_tariff"],
        )
        ch_h: set[int] = set()
        dis_h: set[int] = set()
        for cyc in tou_plan["cycles"]:
            ch_h.update(cyc["charge_hours"])
            dis_h.update(cyc["discharge_hours"])
        charge_hours = frozenset(ch_h)
        discharge_hours = frozenset(dis_h)

    candidates: list[dict[str, Any]] = []
    seen_zero = False
    for strat in strat_list:
        for b_kwh in bess_grid:
            for p_kw in pcs_grid:
                if b_kwh == 0.0:
                    if seen_zero:
                        continue
                    seen_zero = True
                    eff_pcs = 0.0
                    lbl = "PV_Only_0kWh"
                else:
                    eff_pcs = p_kw
                    lbl = f"{strat}_{int(round(b_kwh))}kWh_{int(round(eff_pcs))}kW"

                sim = _simulate_candidate_fast(
                    series=series,
                    bess_kwh=b_kwh,
                    pcs_kw=eff_pcs,
                    strategy=strat,
                    tariff=tar_cfg,
                    bess_params=bp,
                    charge_hours=charge_hours,
                    discharge_hours=discharge_hours,
                )

                # Compute total CAPEX & OPEX & battery replacement
                capex = _resolve_system_capex(
                    fin_cfg,
                    dc_kw=dc_kw,
                    battery_kwh=b_kwh,
                    pcs_kw=eff_pcs,
                    pcs_capex_per_kw=pcs_capex_kw,
                )
                opex = capex * (fin_cfg["opex_pct_capex"] / 100.0) + dc_kw * fin_cfg["opex_per_kw_year"]

                reps = list(fin_cfg["replacement_events"])
                bat_rep_ev = _battery_replacement_event(
                    fin_cfg,
                    battery_kwh=b_kwh,
                    is_pv_only=(b_kwh <= 0.0),
                    label="battery_replacement",
                )
                if bat_rep_ev is not None:
                    reps.append(bat_rep_ev)

                fin_m = _compute_cashflow_metrics(
                    year1_energy_kwh=sim["year1_energy_kwh"],
                    year1_gross_benefit=sim["year1_gross_benefit"],
                    year1_demand_savings=sim["year1_demand_savings"],
                    capex_total=capex,
                    base_annual_opex=opex,
                    replacement_events=reps,
                    financial=fin_cfg,
                )
                candidates.append({
                    "label": lbl,
                    "strategy": "pv_only" if b_kwh == 0.0 else strat,
                    "bess_kwh": b_kwh,
                    "pcs_kw": eff_pcs,
                    "capex_total": fin_m["capex_total"],
                    "year1_gross_benefit": fin_m["year1_gross_benefit"],
                    "year1_demand_savings": fin_m["year1_demand_savings"],
                    "npv": fin_m["npv"],
                    "irr_pct": fin_m["irr_pct"],
                    "lcoe": fin_m["lcoe"],
                    "simple_payback_years": fin_m["simple_payback_years"],
                    "discounted_payback_years": fin_m["discounted_payback_years"],
                    "self_consumption_pct": sim["self_consumption_pct"],
                    "battery_charge_kwh": sim["battery_charge_kwh"],
                    "battery_discharge_kwh": sim["battery_discharge_kwh"],
                    "equivalent_full_cycles": sim["equivalent_full_cycles"],
                    "avg_monthly_peak_reduction_kw": sim["avg_monthly_peak_reduction_kw"],
                })

    lower_is_better = rank_by in ("lcoe", "simple_payback_years", "discounted_payback_years")
    metric_key = "irr_pct" if rank_by == "irr" else rank_by

    def _sort_key(item: dict) -> tuple[int, float]:
        v = item.get(metric_key)
        if v is None or not math.isfinite(v):
            return (1, 0.0)
        return (0, float(v) if lower_is_better else -float(v))

    ranked = sorted(candidates, key=_sort_key)
    for r_i, item in enumerate(ranked, start=1):
        item["rank"] = r_i

    best = ranked[0]
    pareto = _compute_pareto_frontier(ranked)

    # Build 2D heatmap for the winning non-zero strategy (or first requested strategy)
    hm_strategy = best["strategy"] if best["strategy"] in _VALID_STRATEGIES else strat_list[0]
    nonzero_bess = [b for b in bess_grid if b > 0]
    lookup = {(c["strategy"], c["bess_kwh"], c["pcs_kw"]): c.get(metric_key) for c in ranked}
    z_matrix: list[list[float | None]] = []
    for p_kw in pcs_grid:
        row_z: list[float | None] = []
        for b_kwh in nonzero_bess:
            val = lookup.get((hm_strategy, b_kwh, p_kw))
            row_z.append(val)
        z_matrix.append(row_z)

    heatmap = {
        "metric": rank_by,
        "strategy": hm_strategy,
        "bess_kwh": nonzero_bess,
        "pcs_kw": pcs_grid,
        "z": z_matrix,
    }

    # Stage 2: optional PVsystCLI verification
    stage2_status = "skipped"
    stage2_plan: list[dict[str, Any]] = []
    verified_comparisons: list[dict[str, Any]] = []
    top_nonzero = [c for c in ranked if c["bess_kwh"] > 0][:verify_top_k]

    if verify_top_k > 0:
        if not verify_config or not isinstance(verify_config, dict):
            raise ValueError("verify_config is required when verify_top_k > 0")
        for req_k in ("project", "source_variant", "battery_file_name", "battery_unit_kwh"):
            if not verify_config.get(req_k):
                raise ValueError(f"verify_config.{req_k} is required when verify_top_k > 0")
        prefix = str(verify_config.get("variant_prefix") or "VB").strip()
        for idx, c in enumerate(top_nonzero, start=1):
            stage2_plan.append({
                "rank": idx,
                "label": c["label"],
                "new_variant": f"{prefix}{idx}",
                "bess_kwh": c["bess_kwh"],
                "pcs_kw": c["pcs_kw"],
                "strategy": c["strategy"],
            })
        if dry_run:
            stage2_status = "ready_for_cli_verification"
        else:
            if not confirm:
                raise ValueError("confirm=True is required when dry_run=False and verify_top_k > 0")
            if cli is None or store is None:
                raise ValueError("cli and store are required for Stage 2 PVsystCLI verification")
            verified_comparisons = _run_stage2_verify(
                cli=cli,
                store=store,
                top_candidates=top_nonzero,
                verify_config=verify_config,
                tariff=tariff,
                financial=financial,
                fin_cfg=fin_cfg,
                installed_dc_kw=dc_kw,
                pcs_capex_per_kw=pcs_capex_kw,
                bess_params=bp,
                tou_plan=tou_plan,
            )
            stage2_status = "verified"

    return {
        "status": "verified" if stage2_status == "verified" else ("ready" if dry_run and verify_top_k > 0 else "completed"),
        "dry_run": dry_run,
        "baseline_csv": csv_path.name,
        "installed_dc_kw": dc_kw,
        "currency": fin_cfg["currency"],
        "rank_by": rank_by,
        "evaluated_count": len(ranked),
        "best": best,
        "pareto_frontier": pareto,
        "heatmap": heatmap,
        "tou_schedule_plan": tou_plan,
        "candidates": ranked,
        "stage2_verification": {
            "status": stage2_status,
            "verify_top_k": verify_top_k,
            "planned_variants": stage2_plan,
            "verified_comparisons": verified_comparisons,
        },
    }

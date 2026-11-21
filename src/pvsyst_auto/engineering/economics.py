"""Deterministic PV & PV+BESS financial, TOU tariff, LCOE, NPV, and IRR evaluation engine.

Pure Python standard library implementation. Streams hourly or sub-hourly PVsyst SFI CSVs
(5/10/15/30/60 min) or batch summary CSVs to compute auditable year-1 energy/TOU settlements,
25-year degraded cashflows, LCOE, NPV, IRR, simple/discounted payback, and multi-scenario rankings.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pvsyst_auto.native.cli import iter_result_csv, parse_batch_results, result_units
from pvsyst_auto.project.paths import safe_filename, workspace_file

_POWER_TO_KW = {"W": 0.001, "kW": 1.0}
_ENERGY_TO_KWH = {"Wh": 0.001, "kWh": 1.0}
_VALID_RANK_KEYS = ("npv", "irr", "lcoe", "simple_payback_years", "discounted_payback_years", "year1_net_benefit")
_VALID_SENSITIVITY_FACTORS = ("tariff", "capex", "yield", "degradation")


def _validate_demand_charge(raw_dc: dict | None) -> dict | None:
    if raw_dc is None:
        return None
    if not isinstance(raw_dc, dict):
        raise ValueError("tariff.demand_charge must be an object")
    billing_mode = raw_dc.get("billing_mode", "demand")
    if billing_mode not in ("demand", "capacity", "compare"):
        raise ValueError("tariff.demand_charge.billing_mode must be 'demand', 'capacity', or 'compare'")
    demand_rate = _finite(
        raw_dc.get("demand_rate_per_kw_month", 0.0),
        "tariff.demand_charge.demand_rate_per_kw_month",
        minimum=0.0,
    )
    cap_rate = (
        _finite(
            raw_dc["capacity_rate_per_kva_month"],
            "tariff.demand_charge.capacity_rate_per_kva_month",
            minimum=0.0,
        )
        if raw_dc.get("capacity_rate_per_kva_month") is not None else None
    )
    contract_kva = (
        _finite(
            raw_dc["contract_capacity_kva"],
            "tariff.demand_charge.contract_capacity_kva",
            minimum=0.0,
        )
        if raw_dc.get("contract_capacity_kva") is not None else None
    )
    if billing_mode in ("capacity", "compare") and (cap_rate is None or contract_kva is None):
        raise ValueError(
            "tariff.demand_charge requires capacity_rate_per_kva_month and contract_capacity_kva "
            "when billing_mode is 'capacity' or 'compare'"
        )
    pf = _finite(raw_dc.get("power_factor", 0.95), "tariff.demand_charge.power_factor", minimum=0.5, maximum=1.0)
    months_raw = raw_dc.get("months")
    if months_raw is not None:
        if (not isinstance(months_raw, list) or not months_raw
                or any(type(m) is not int or not 1 <= m <= 12 for m in months_raw)):
            raise ValueError("tariff.demand_charge.months must be integers in 1..12")
        active_months = frozenset(months_raw)
    else:
        active_months = frozenset(range(1, 13))
    return {
        "billing_mode": billing_mode,
        "demand_rate_per_kw_month": demand_rate,
        "capacity_rate_per_kva_month": cap_rate,
        "contract_capacity_kva": contract_kva,
        "power_factor": pf,
        "months": active_months,
    }


def _validate_sensitivity(raw_sens: dict | None) -> dict | None:
    if raw_sens is None:
        return None
    if not isinstance(raw_sens, dict):
        raise ValueError("sensitivity must be an object")
    enabled = raw_sens.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("sensitivity.enabled must be a strict boolean")
    if not enabled:
        return None
    raw_factors = raw_sens.get("factors")
    if raw_factors is None:
        factors = list(_VALID_SENSITIVITY_FACTORS)
    else:
        if not isinstance(raw_factors, list) or not raw_factors:
            raise ValueError("sensitivity.factors must be a non-empty list")
        factors = []
        for f in raw_factors:
            if f not in _VALID_SENSITIVITY_FACTORS:
                raise ValueError(f"sensitivity.factors entries must be in {_VALID_SENSITIVITY_FACTORS}")
            if f not in factors:
                factors.append(f)
    variation = _finite(raw_sens.get("variation_pct", 15.0), "sensitivity.variation_pct", minimum=0.1, maximum=80.0)
    return {
        "enabled": True,
        "factors": factors,
        "variation_pct": variation,
    }


def _finite(value: Any, name: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    num = float(value)
    if not math.isfinite(num):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and num < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and num > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return num


def _to_kwh(value: float, unit: str, step_hours: float) -> float | None:
    if unit in _POWER_TO_KW:
        return value * _POWER_TO_KW[unit] * step_hours
    if unit in _ENERGY_TO_KWH:
        return value * _ENERGY_TO_KWH[unit]
    return None


def _validate_tariff(tariff: dict | None) -> dict:
    raw = dict(tariff or {})
    mode = raw.get("mode", "feed_in")
    if mode not in ("feed_in", "self_consumption_flat", "tou"):
        raise ValueError("tariff.mode must be 'feed_in', 'self_consumption_flat', or 'tou'")
    feed_in = _finite(raw.get("feed_in_tariff", 0.0), "tariff.feed_in_tariff", minimum=0.0)
    import_rate = _finite(raw.get("import_tariff", 0.0), "tariff.import_tariff", minimum=0.0)
    sc_ratio = raw.get("self_consumption_ratio")
    if sc_ratio is not None:
        sc_ratio = _finite(sc_ratio, "tariff.self_consumption_ratio", minimum=0.0, maximum=1.0)

    periods: list[dict] = []
    if mode == "tou":
        raw_periods = raw.get("tou_schedule")
        if not isinstance(raw_periods, list) or not raw_periods:
            raise ValueError("tariff.tou_schedule must be a non-empty list when tariff.mode='tou'")
        rates_by_name = {"default": (import_rate, feed_in)}
        for idx, item in enumerate(raw_periods):
            if not isinstance(item, dict):
                raise ValueError(f"tariff.tou_schedule[{idx}] must be an object")
            name = str(item.get("name") or f"period_{idx + 1}").strip()
            hours = item.get("hours")
            if (not isinstance(hours, list) or not hours
                    or any(type(h) is not int or not 0 <= h <= 23 for h in hours)):
                raise ValueError(f"tariff.tou_schedule[{idx}].hours must be a non-empty list of integers in 0..23")
            months = item.get("months")
            if months is not None:
                if (not isinstance(months, list) or not months
                        or any(type(m) is not int or not 1 <= m <= 12 for m in months)):
                    raise ValueError(f"tariff.tou_schedule[{idx}].months must be integers in 1..12")
            day_type = item.get("day_type", "all")
            if day_type not in ("all", "weekday", "weekend"):
                raise ValueError(f"tariff.tou_schedule[{idx}].day_type must be 'all', 'weekday', or 'weekend'")
            p_import = _finite(item.get("import_rate", import_rate), f"tariff.tou_schedule[{idx}].import_rate", minimum=0.0)
            p_export = _finite(
                item["export_rate"] if item.get("export_rate") is not None else feed_in,
                f"tariff.tou_schedule[{idx}].export_rate",
                minimum=0.0,
            )
            rates = (p_import, p_export)
            if rates_by_name.setdefault(name, rates) != rates:
                raise ValueError(f"tariff.tou_schedule uses period name {name!r} with different rates")
            periods.append({
                "name": name,
                "hours": frozenset(hours),
                "months": frozenset(months) if months is not None else None,
                "day_type": day_type,
                "import_rate": p_import,
                "export_rate": p_export,
            })
    return {
        "mode": mode,
        "feed_in_tariff": feed_in,
        "import_tariff": import_rate,
        "self_consumption_ratio": sc_ratio,
        "tou_schedule": periods,
        "demand_charge": _validate_demand_charge(raw.get("demand_charge")),
    }


def _validate_financial(financial: dict | None) -> dict:
    raw = dict(financial or {})
    currency = str(raw.get("currency") or "CNY").strip()
    if not currency or len(currency) > 12:
        raise ValueError("financial.currency must be 1..12 characters")
    life = raw.get("project_life_years", 25)
    if type(life) is not int or not 1 <= life <= 50:
        raise ValueError("financial.project_life_years must be an integer in 1..50")
    discount = _finite(raw.get("discount_rate_pct", 6.0), "financial.discount_rate_pct", minimum=0.0, maximum=50.0)
    deg1 = _finite(raw.get("first_year_degradation_pct", 1.0), "financial.first_year_degradation_pct", minimum=0.0, maximum=25.0)
    deg_ann = _finite(raw.get("annual_degradation_pct", 0.4), "financial.annual_degradation_pct", minimum=0.0, maximum=10.0)
    opex_esc = _finite(raw.get("opex_escalation_pct", 2.0), "financial.opex_escalation_pct", minimum=-10.0, maximum=20.0)
    tariff_esc = _finite(raw.get("tariff_escalation_pct", 0.0), "financial.tariff_escalation_pct", minimum=-10.0, maximum=20.0)
    capex_per_kw = (
        _finite(raw["capex_per_kw"], "financial.capex_per_kw", minimum=0.0)
        if raw.get("capex_per_kw") is not None else None
    )
    bat_capex_per_kwh = (
        _finite(raw["battery_capex_per_kwh"], "financial.battery_capex_per_kwh", minimum=0.0)
        if raw.get("battery_capex_per_kwh") is not None else None
    )
    fixed_capex = _finite(raw.get("fixed_capex", 0.0), "financial.fixed_capex", minimum=0.0)
    opex_per_kw_yr = _finite(raw.get("opex_per_kw_year", 0.0), "financial.opex_per_kw_year", minimum=0.0)
    opex_pct_capex = _finite(raw.get("opex_pct_capex", 0.0), "financial.opex_pct_capex", minimum=0.0, maximum=50.0)
    salvage_pct = _finite(raw.get("salvage_value_pct", 0.0), "financial.salvage_value_pct", minimum=0.0, maximum=100.0)

    bat_rep_year = raw.get("battery_replacement_year")
    if bat_rep_year is not None:
        if type(bat_rep_year) is not int or not 1 <= bat_rep_year <= life:
            raise ValueError(f"financial.battery_replacement_year must be an integer in 1..{life}")
    bat_rep_ratio = (
        _finite(raw["battery_replacement_cost_ratio"], "financial.battery_replacement_cost_ratio", minimum=0.0, maximum=2.0)
        if raw.get("battery_replacement_cost_ratio") is not None else None
    )
    bat_rep_cost = (
        _finite(raw["battery_replacement_cost"], "financial.battery_replacement_cost", minimum=0.0)
        if raw.get("battery_replacement_cost") is not None else None
    )

    replacements: list[dict] = []
    for idx, ev in enumerate(raw.get("replacement_events") or ()):
        if not isinstance(ev, dict):
            raise ValueError(f"financial.replacement_events[{idx}] must be an object")
        yr = ev.get("year")
        if type(yr) is not int or not 1 <= yr <= life:
            raise ValueError(f"financial.replacement_events[{idx}].year must be an integer in 1..{life}")
        cost = _finite(ev.get("cost"), f"financial.replacement_events[{idx}].cost", minimum=0.0)
        replacements.append({
            "year": yr,
            "cost": cost,
            "label": str(ev.get("label") or f"replacement_year_{yr}").strip(),
        })
    return {
        "currency": currency,
        "project_life_years": life,
        "discount_rate_pct": discount,
        "first_year_degradation_pct": deg1,
        "annual_degradation_pct": deg_ann,
        "opex_escalation_pct": opex_esc,
        "tariff_escalation_pct": tariff_esc,
        "capex_per_kw": capex_per_kw,
        "battery_capex_per_kwh": bat_capex_per_kwh,
        "fixed_capex": fixed_capex,
        "opex_per_kw_year": opex_per_kw_yr,
        "opex_pct_capex": opex_pct_capex,
        "salvage_value_pct": salvage_pct,
        "battery_replacement_year": bat_rep_year,
        "battery_replacement_cost_ratio": bat_rep_ratio,
        "battery_replacement_cost": bat_rep_cost,
        "replacement_events": replacements,
    }


def _lookup_rate(stamp, tariff: dict) -> tuple[str, float, float]:
    """Return (period_name, import_rate, export_rate) for a given timestamp."""
    if tariff["mode"] == "feed_in":
        return "flat_export", tariff["import_tariff"], tariff["feed_in_tariff"]
    if tariff["mode"] == "self_consumption_flat":
        return "flat", tariff["import_tariff"], tariff["feed_in_tariff"]
    is_weekend = stamp.weekday() >= 5
    for period in tariff["tou_schedule"]:
        if period["months"] is not None and stamp.month not in period["months"]:
            continue
        if period["day_type"] == "weekday" and is_weekend:
            continue
        if period["day_type"] == "weekend" and not is_weekend:
            continue
        if stamp.hour in period["hours"]:
            return period["name"], period["import_rate"], period["export_rate"]
    return "default", tariff["import_tariff"], tariff["feed_in_tariff"]


def _resolve_result_csv(workspace: Path | None, csv_ref: str | Path) -> Path:
    if isinstance(csv_ref, Path) and csv_ref.is_file():
        return csv_ref
    text = str(csv_ref)
    if workspace is not None:
        safe = safe_filename(text, ".csv")
        for folder in ("Results", "UserHourly"):
            candidate = workspace_file(workspace, folder, safe, ".csv")
            if candidate.is_file():
                return candidate
        raise FileNotFoundError(f"Result CSV not found in workspace Results/ or UserHourly/: {safe}")
    path = Path(text)
    if not path.is_file():
        raise FileNotFoundError(f"Result CSV not found: {path}")
    return path


def _settle_csv_year1(csv_path: Path, tariff: dict) -> dict:
    """Single-pass streaming settlement over an hourly or sub-hourly PVsyst SFI CSV."""
    units = result_units(csv_path, strict=True)

    # First detect step_hours accurately by streaming once into lightweight accumulators.
    # For energy-unit columns (kWh/Wh) we do not need step_hours until totals; for power-unit
    # columns (kW/W) multiplying by step_hours is linear, so we accumulate raw power-steps and
    # scale by step_hours after the loop!
    idx: dict[str, int] = {}
    rows = 0
    prev_stamp = None
    step_minutes: float | None = None
    regular = True

    # Accumulate raw (unscaled-by-step-hours for power units, or already-energy for energy units)
    # Wait: since units are uniform per column, we scale per-column cleanly after the loop for totals,
    # and for tariff-weighted sums (where every row is multiplied by rate_t), linearity STILL holds
    # because step_hours is constant across a regular time series!
    export_col = None
    has_load = False
    has_import_col = False
    has_storage = False

    # Monthly (1..12) and 24-hour (0..23) accumulators
    monthly = {
        m: {"e_grid": 0.0, "e_user": 0.0, "e_from_grid": 0.0, "e_bat_ch": 0.0, "e_bat_dis": 0.0,
            "export_rev": 0.0, "baseline_bill": 0.0, "import_cost": 0.0, "self_cons": 0.0,
            "baseline_peak_kw": 0.0, "actual_peak_kw": 0.0}
        for m in range(1, 13)
    }
    hourly_prof = {
        h: {"count": 0, "e_grid": 0.0, "e_user": 0.0, "e_from_grid": 0.0,
            "e_bat_ch": 0.0, "e_bat_dis": 0.0, "import_rate_sum": 0.0, "export_rate_sum": 0.0}
        for h in range(24)
    }
    tou_breakdown: dict[str, dict[str, float]] = {}

    def _unit_kind(col: str) -> tuple[float, bool]:
        """Return (scale_to_kw_or_kwh, needs_step_hours)."""
        u = units.get(col, "")
        if u in _POWER_TO_KW:
            return _POWER_TO_KW[u], True
        if u in _ENERGY_TO_KWH:
            return _ENERGY_TO_KWH[u], False
        return 0.0, False

    col_scale: dict[str, tuple[float, bool]] = {}

    def _val(row, col: str) -> float:
        if col not in idx:
            return 0.0
        raw = row[idx[col]]
        return float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else 0.0

    # We collect raw rows only if non-regular timestamps occur; for regular series (99.99% of PVsyst CSVs),
    # compute delta on the fly using the first observed delta (default 1.0 h for the very first row, reconciled after).
    first_row_data: tuple | None = None
    totals = {
        "pv_export_kwh": 0.0,
        "grid_import_kwh": 0.0,
        "load_kwh": 0.0,
        "self_consumed_kwh": 0.0,
        "battery_charge_kwh": 0.0,
        "battery_discharge_kwh": 0.0,
        "export_revenue": 0.0,
        "baseline_grid_bill": 0.0,
        "actual_grid_bill": 0.0,
        "avoided_import_savings": 0.0,
    }

    def _process_row(stamp, row, dt_hours: float) -> None:
        def kwh(col: str) -> float:
            if col not in col_scale:
                return 0.0
            factor, needs_dt = col_scale[col]
            if factor == 0.0:
                raise ValueError(f"{col} requires a power or energy unit, got {units.get(col, '')!r}")
            return _val(row, col) * factor * (dt_hours if needs_dt else 1.0)

        p_name, imp_rate, exp_rate = _lookup_rate(stamp, tariff)

        raw_eg = kwh(export_col) if export_col else 0.0
        e_exp = max(0.0, raw_eg)
        e_user = max(0.0, kwh("E_User")) if has_load else 0.0
        if has_import_col:
            e_imp = max(0.0, kwh("EFrGrid"))
        elif has_load and "E_Solar" in idx:
            e_imp = max(0.0, e_user - max(0.0, kwh("E_Solar")))
        elif raw_eg < 0:
            e_imp = -raw_eg
        else:
            e_imp = 0.0

        e_bat_ch = max(0.0, kwh("EBatCh")) if "EBatCh" in idx else 0.0
        e_bat_dis = max(0.0, kwh("EBatDis")) if "EBatDis" in idx else 0.0

        if has_load:
            self_cons = max(0.0, e_user - e_imp)
            baseline_bill = e_user * imp_rate
            import_cost = e_imp * imp_rate
            export_rev = e_exp * exp_rate
            avoided = baseline_bill - import_cost
        else:
            sc_ratio = (
                tariff["self_consumption_ratio"]
                if (tariff["mode"] != "feed_in" and tariff["self_consumption_ratio"] is not None)
                else 0.0
            )
            self_cons = e_exp * sc_ratio
            net_exp = e_exp - self_cons
            baseline_bill = self_cons * imp_rate
            import_cost = e_imp * imp_rate
            export_rev = net_exp * exp_rate
            avoided = baseline_bill - import_cost
            e_exp = net_exp

        totals["pv_export_kwh"] += e_exp
        totals["grid_import_kwh"] += e_imp
        totals["load_kwh"] += e_user
        totals["self_consumed_kwh"] += self_cons
        totals["battery_charge_kwh"] += e_bat_ch
        totals["battery_discharge_kwh"] += e_bat_dis
        totals["export_revenue"] += export_rev
        totals["baseline_grid_bill"] += baseline_bill
        totals["actual_grid_bill"] += import_cost
        totals["avoided_import_savings"] += avoided

        tb = tou_breakdown.setdefault(p_name, {
            "import_rate": imp_rate,
            "export_rate": exp_rate,
            "hours": 0.0,
            "export_kwh": 0.0,
            "import_kwh": 0.0,
            "self_consumed_kwh": 0.0,
            "battery_charge_kwh": 0.0,
            "battery_discharge_kwh": 0.0,
            "export_revenue": 0.0,
            "avoided_import_savings": 0.0,
        })
        tb["hours"] += dt_hours
        tb["export_kwh"] += e_exp
        tb["import_kwh"] += e_imp
        tb["self_consumed_kwh"] += self_cons
        tb["battery_charge_kwh"] += e_bat_ch
        tb["battery_discharge_kwh"] += e_bat_dis
        tb["export_revenue"] += export_rev
        tb["avoided_import_savings"] += avoided

        m_rec = monthly[stamp.month]
        m_rec["e_grid"] += e_exp + self_cons
        m_rec["e_user"] += e_user
        m_rec["e_from_grid"] += e_imp
        m_rec["e_bat_ch"] += e_bat_ch
        m_rec["e_bat_dis"] += e_bat_dis
        m_rec["export_rev"] += export_rev
        m_rec["baseline_bill"] += baseline_bill
        m_rec["import_cost"] += import_cost
        m_rec["self_cons"] += self_cons
        if dt_hours > 0:
            load_kw = e_user / dt_hours
            imp_kw = e_imp / dt_hours
            if load_kw > m_rec["baseline_peak_kw"]:
                m_rec["baseline_peak_kw"] = load_kw
            if imp_kw > m_rec["actual_peak_kw"]:
                m_rec["actual_peak_kw"] = imp_kw

        h_rec = hourly_prof[stamp.hour]
        h_rec["count"] += 1
        h_rec["e_grid"] += (e_exp + self_cons) / dt_hours if dt_hours > 0 else 0.0
        h_rec["e_user"] += e_user / dt_hours if dt_hours > 0 else 0.0
        h_rec["e_from_grid"] += e_imp / dt_hours if dt_hours > 0 else 0.0
        h_rec["e_bat_ch"] += e_bat_ch / dt_hours if dt_hours > 0 else 0.0
        h_rec["e_bat_dis"] += e_bat_dis / dt_hours if dt_hours > 0 else 0.0
        h_rec["import_rate_sum"] += imp_rate
        h_rec["export_rate_sum"] += exp_rate

    for headers, row in iter_result_csv(csv_path, strict=True):
        if rows == 0:
            idx = {name: i for i, name in enumerate(headers) if i > 0}
            export_col = "EToGrid" if "EToGrid" in idx else ("E_Grid" if "E_Grid" in idx else None)
            if export_col is None:
                raise ValueError("Result CSV must include E_Grid or EToGrid for financial evaluation")
            has_load = "E_User" in idx
            has_import_col = "EFrGrid" in idx
            has_storage = "EBatCh" in idx or "EBatDis" in idx
            for col in idx:
                col_scale[col] = _unit_kind(col)
            first_row_data = (row[0], list(row))
            prev_stamp = row[0]
            rows = 1
            continue

        stamp = row[0]
        delta_min = (stamp - prev_stamp).total_seconds() / 60.0
        if delta_min <= 0:
            regular = False
            dt_hours = (step_minutes or 60.0) / 60.0
        else:
            if step_minutes is None:
                step_minutes = delta_min
                # Flush buffered first row with true detected step_hours!
                _process_row(first_row_data[0], first_row_data[1], step_minutes / 60.0)
                first_row_data = None
            elif abs(delta_min - step_minutes) > 1e-6:
                regular = False
            dt_hours = delta_min / 60.0
        _process_row(stamp, row, dt_hours)
        prev_stamp = stamp
        rows += 1

    if rows == 0:
        raise ValueError("Result CSV contains no simulation rows")
    if not regular:
        raise ValueError(
            "Result CSV timestamps do not have a regular cadence (missing, duplicate, or out-of-order rows); "
            "cannot settle energy or tariffs reliably"
        )
    if first_row_data is not None:
        step_minutes = 60.0
        _process_row(first_row_data[0], first_row_data[1], 1.0)

    eff_step_min = step_minutes if (regular and step_minutes is not None) else 60.0
    total_pv_utilized_kwh = totals["pv_export_kwh"] + totals["self_consumed_kwh"]
    year1_energy_benefit = totals["export_revenue"] + totals["avoided_import_savings"]

    warnings: list[str] = []
    if not has_load and tariff["mode"] != "feed_in" and tariff["self_consumption_ratio"] is None:
        warnings.append(
            "CSV has no E_User column and tariff.self_consumption_ratio was omitted; "
            "all PV generation was valued at export_rate."
        )

    demand_summary: dict[str, Any] | None = None
    year1_demand_savings = 0.0
    dc_cfg = tariff.get("demand_charge")
    if dc_cfg is not None:
        if not has_load:
            warnings.append(
                "tariff.demand_charge was configured, but the CSV has no E_User load column; "
                "monthly demand charges and peak shaving savings were skipped."
            )
        else:
            rate = dc_cfg["demand_rate_per_kw_month"]
            active_months = dc_cfg["months"]
            monthly_demand: list[dict[str, Any]] = []
            base_dc_total = 0.0
            act_dc_total = 0.0
            for m in range(1, 13):
                rec = monthly[m]
                b_kw = rec["baseline_peak_kw"]
                a_kw = rec["actual_peak_kw"]
                red_kw = b_kw - a_kw
                if m in active_months:
                    b_cost = b_kw * rate
                    a_cost = a_kw * rate
                else:
                    b_cost = 0.0
                    a_cost = 0.0
                base_dc_total += b_cost
                act_dc_total += a_cost
                monthly_demand.append({
                    "month": m,
                    "active": m in active_months,
                    "baseline_peak_kw": round(b_kw, 3),
                    "actual_peak_kw": round(a_kw, 3),
                    "peak_reduction_kw": round(red_kw, 3),
                    "baseline_demand_charge": round(b_cost, 2),
                    "actual_demand_charge": round(a_cost, 2),
                    "demand_savings": round(b_cost - a_cost, 2),
                })

            cap_charge: float | None = None
            recommended_mode = dc_cfg["billing_mode"]
            if dc_cfg["capacity_rate_per_kva_month"] is not None and dc_cfg["contract_capacity_kva"] is not None:
                cap_charge = len(active_months) * dc_cfg["contract_capacity_kva"] * dc_cfg["capacity_rate_per_kva_month"]
                recommended_mode = "demand" if act_dc_total <= cap_charge else "capacity"

            if dc_cfg["billing_mode"] == "demand":
                year1_demand_savings = base_dc_total - act_dc_total
            elif dc_cfg["billing_mode"] == "capacity":
                year1_demand_savings = 0.0
            else:  # compare: optimal post-system vs optimal baseline billing
                assert cap_charge is not None
                year1_demand_savings = min(base_dc_total, cap_charge) - min(act_dc_total, cap_charge)

            demand_summary = {
                "billing_mode": dc_cfg["billing_mode"],
                "recommended_billing_mode": recommended_mode,
                "demand_rate_per_kw_month": rate,
                "annual_baseline_peak_max_kw": round(max(r["baseline_peak_kw"] for r in monthly_demand), 3),
                "annual_actual_peak_max_kw": round(max(r["actual_peak_kw"] for r in monthly_demand), 3),
                "avg_monthly_peak_reduction_kw": round(
                    sum(r["peak_reduction_kw"] for r in monthly_demand if r["active"]) / max(1, len(active_months)), 3
                ),
                "baseline_annual_demand_charge": round(base_dc_total, 2),
                "actual_annual_demand_charge": round(act_dc_total, 2),
                "annual_capacity_charge": round(cap_charge, 2) if cap_charge is not None else None,
                "annual_demand_savings": round(year1_demand_savings, 2),
                "monthly_peaks": monthly_demand,
            }

    year1_gross_benefit = year1_energy_benefit + year1_demand_savings

    monthly_summary = [
        {
            "month": m,
            "pv_energy_kwh": round(rec["e_grid"], 2),
            "self_consumed_kwh": round(rec["self_cons"], 2),
            "load_kwh": round(rec["e_user"], 2),
            "grid_import_kwh": round(rec["e_from_grid"], 2),
            "battery_charge_kwh": round(rec["e_bat_ch"], 2),
            "battery_discharge_kwh": round(rec["e_bat_dis"], 2),
            "export_revenue": round(rec["export_rev"], 2),
            "avoided_import_savings": round(rec["baseline_bill"] - rec["import_cost"], 2),
            "gross_benefit": round(rec["export_rev"] + (rec["baseline_bill"] - rec["import_cost"]), 2),
            "baseline_peak_kw": round(rec["baseline_peak_kw"], 3),
            "actual_peak_kw": round(rec["actual_peak_kw"], 3),
        }
        for m, rec in sorted(monthly.items())
    ]

    typical_day = [
        {
            "hour": h,
            "avg_pv_kw": round(rec["e_grid"] / rec["count"], 3) if rec["count"] else 0.0,
            "avg_load_kw": round(rec["e_user"] / rec["count"], 3) if rec["count"] else 0.0,
            "avg_grid_import_kw": round(rec["e_from_grid"] / rec["count"], 3) if rec["count"] else 0.0,
            "avg_battery_charge_kw": round(rec["e_bat_ch"] / rec["count"], 3) if rec["count"] else 0.0,
            "avg_battery_discharge_kw": round(rec["e_bat_dis"] / rec["count"], 3) if rec["count"] else 0.0,
            "avg_import_rate": round(rec["import_rate_sum"] / rec["count"], 4) if rec["count"] else 0.0,
            "avg_export_rate": round(rec["export_rate_sum"] / rec["count"], 4) if rec["count"] else 0.0,
        }
        for h, rec in sorted(hourly_prof.items())
    ]

    tou_summary = {
        name: {k: round(v, 4) for k, v in rec.items()}
        for name, rec in tou_breakdown.items()
    }

    return {
        "rows": rows,
        "step_minutes": eff_step_min if regular else None,
        "has_load": has_load,
        "has_storage": has_storage,
        "year1_energy_kwh": total_pv_utilized_kwh,
        "year1_energy_benefit": year1_energy_benefit,
        "year1_demand_savings": year1_demand_savings,
        "year1_gross_benefit": year1_gross_benefit,
        "energy_summary": {k: round(v, 2) for k, v in totals.items()},
        "demand_charge_summary": demand_summary,
        "tou_breakdown": tou_summary,
        "monthly_summary": monthly_summary,
        "typical_day_profile": typical_day,
        "warnings": warnings,
    }


def _compute_irr(cashflows: list[float]) -> float | None:
    """Compute exact Internal Rate of Return (as decimal, e.g. 0.12 = 12%) or None if unsolvable."""
    if not cashflows or len(cashflows) < 2:
        return None
    if not (any(c < 0 for c in cashflows) and any(c > 0 for c in cashflows)):
        return None

    def npv_at(rate: float) -> float:
        return sum(cf / ((1.0 + rate) ** t) for t, cf in enumerate(cashflows))

    # Scan candidate grid to locate the first positive-to-negative sign change (standard project IRR)
    grid = [-0.95, -0.75, -0.5, -0.25, -0.1, 0.0, 0.02, 0.05, 0.08, 0.12, 0.18, 0.25, 0.4, 0.7, 1.0, 2.0, 5.0, 10.0]
    bracket = None
    prev_r = grid[0]
    prev_v = npv_at(prev_r)
    for r in grid[1:]:
        v = npv_at(r)
        if abs(v) < 1e-9:
            return r
        if (prev_v > 0 and v < 0) or (prev_v < 0 and v > 0):
            bracket = (prev_r, r, prev_v, v)
            break
        prev_r, prev_v = r, v
    if bracket is None:
        return None

    lo, hi, f_lo, f_hi = bracket
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = npv_at(mid)
        if abs(f_mid) < 1e-9 or abs(hi - lo) < 1e-10:
            return mid
        if (f_lo > 0 and f_mid > 0) or (f_lo < 0 and f_mid < 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    return 0.5 * (lo + hi)


def _compute_cashflow_metrics(
    *,
    year1_energy_kwh: float,
    year1_gross_benefit: float,
    capex_total: float,
    base_annual_opex: float,
    replacement_events: list[dict],
    financial: dict,
    year1_demand_savings: float = 0.0,
) -> dict:
    life = financial["project_life_years"]
    r = financial["discount_rate_pct"] / 100.0
    deg1 = financial["first_year_degradation_pct"] / 100.0
    deg_ann = financial["annual_degradation_pct"] / 100.0
    opex_esc = financial["opex_escalation_pct"] / 100.0
    tariff_esc = financial["tariff_escalation_pct"] / 100.0
    salvage_pct = financial["salvage_value_pct"] / 100.0
    year1_energy_benefit = year1_gross_benefit - year1_demand_savings

    rep_by_year: dict[int, float] = {}
    for ev in replacement_events:
        rep_by_year[ev["year"]] = rep_by_year.get(ev["year"], 0.0) + ev["cost"]

    cashflows: list[float] = [-capex_total]
    schedule: list[dict] = []
    cum_nom = -capex_total
    cum_disc = -capex_total
    simple_payback: float | None = 0.0 if capex_total == 0 else None
    discounted_payback: float | None = 0.0 if capex_total == 0 else None

    pv_costs = capex_total
    pv_energy = 0.0
    lifetime_energy = 0.0
    lifetime_revenue = 0.0
    lifetime_opex = 0.0

    for year in range(1, life + 1):
        deg_factor = (1.0 - deg1) * ((1.0 - deg_ann) ** (year - 1))
        energy_y = year1_energy_kwh * deg_factor
        gross_y = (year1_energy_benefit * deg_factor + year1_demand_savings) * ((1.0 + tariff_esc) ** (year - 1))
        opex_y = base_annual_opex * ((1.0 + opex_esc) ** (year - 1))
        rep_y = rep_by_year.get(year, 0.0)
        salvage_y = capex_total * salvage_pct if year == life else 0.0
        net_y = gross_y - opex_y - rep_y + salvage_y
        disc_factor = 1.0 / ((1.0 + r) ** year)
        disc_net_y = net_y * disc_factor

        prev_nom = cum_nom
        prev_disc = cum_disc
        cum_nom += net_y
        cum_disc += disc_net_y
        if simple_payback is None and prev_nom < 0 <= cum_nom and net_y > 0:
            simple_payback = (year - 1) + (-prev_nom / net_y)
        if discounted_payback is None and prev_disc < 0 <= cum_disc and disc_net_y > 0:
            discounted_payback = (year - 1) + (-prev_disc / disc_net_y)

        pv_costs += (opex_y + rep_y - salvage_y) * disc_factor
        pv_energy += energy_y * disc_factor
        lifetime_energy += energy_y
        lifetime_revenue += gross_y
        lifetime_opex += opex_y
        cashflows.append(net_y)
        schedule.append({
            "year": year,
            "degradation_factor": round(deg_factor, 5),
            "energy_kwh": round(energy_y, 2),
            "gross_benefit": round(gross_y, 2),
            "opex": round(opex_y, 2),
            "replacement_cost": round(rep_y, 2),
            "salvage_value": round(salvage_y, 2),
            "net_cashflow": round(net_y, 2),
            "discounted_cashflow": round(disc_net_y, 2),
            "cumulative_cashflow": round(cum_nom, 2),
            "cumulative_discounted_cashflow": round(cum_disc, 2),
        })

    npv = sum(cf / ((1.0 + r) ** t) for t, cf in enumerate(cashflows))
    irr = _compute_irr(cashflows)
    lcoe = (pv_costs / pv_energy) if pv_energy > 0 else None

    year1_energy_benefit_deg = year1_energy_benefit * (1.0 - deg1)
    year1_gross_deg = year1_energy_benefit_deg + year1_demand_savings
    return {
        "currency": financial["currency"],
        "capex_total": round(capex_total, 2),
        "year1_opex": round(base_annual_opex, 2),
        "year1_energy_benefit": round(year1_energy_benefit_deg, 2),
        "year1_demand_savings": round(year1_demand_savings, 2),
        "year1_gross_benefit": round(year1_gross_deg, 2),
        "year1_net_benefit": round(year1_gross_deg - base_annual_opex, 2),
        "lifetime_energy_kwh": round(lifetime_energy, 2),
        "lifetime_gross_benefit": round(lifetime_revenue, 2),
        "lifetime_opex": round(lifetime_opex, 2),
        "lcoe": round(lcoe, 5) if lcoe is not None else None,
        "npv": round(npv, 2),
        "irr_pct": round(irr * 100.0, 3) if irr is not None else None,
        "simple_payback_years": round(simple_payback, 2) if simple_payback is not None else None,
        "discounted_payback_years": round(discounted_payback, 2) if discounted_payback is not None else None,
        "cashflow_schedule": schedule,
    }


def _compute_sensitivity(
    *,
    sens_cfg: dict,
    base_metrics: dict,
    year1_energy_kwh: float,
    year1_energy_benefit: float,
    year1_demand_savings: float,
    capex_total: float,
    base_annual_opex: float,
    replacement_events: list[dict],
    financial: dict,
) -> dict:
    """Deterministic +/- variation_pct one-at-a-time sensitivity sweep sorted for tornado plotting."""
    var_pct = sens_cfg["variation_pct"]
    frac = var_pct / 100.0

    def _run_case(factor: str, sign: float) -> dict:
        scale = 1.0 + sign * frac
        e_kwh = year1_energy_kwh
        e_ben = year1_energy_benefit
        d_sav = year1_demand_savings
        capex = capex_total
        fin = financial
        if factor == "tariff":
            e_ben = year1_energy_benefit * scale
            d_sav = year1_demand_savings * scale
        elif factor == "capex":
            capex = capex_total * scale
        elif factor == "yield":
            e_kwh = year1_energy_kwh * scale
            e_ben = year1_energy_benefit * scale
        elif factor == "degradation":
            fin = dict(financial)
            fin["first_year_degradation_pct"] = max(0.0, min(25.0, financial["first_year_degradation_pct"] * scale))
            fin["annual_degradation_pct"] = max(0.0, min(10.0, financial["annual_degradation_pct"] * scale))
        m = _compute_cashflow_metrics(
            year1_energy_kwh=e_kwh,
            year1_gross_benefit=e_ben + d_sav,
            year1_demand_savings=d_sav,
            capex_total=capex,
            base_annual_opex=base_annual_opex,
            replacement_events=replacement_events,
            financial=fin,
        )
        return {
            "npv": m["npv"],
            "irr_pct": m["irr_pct"],
            "lcoe": m["lcoe"],
            "simple_payback_years": m["simple_payback_years"],
        }

    rows: list[dict] = []
    for factor in sens_cfg["factors"]:
        low = _run_case(factor, -1.0)
        high = _run_case(factor, +1.0)
        npv_swing = round(abs(high["npv"] - low["npv"]), 2)
        irr_swing = (
            round(abs(high["irr_pct"] - low["irr_pct"]), 3)
            if (low["irr_pct"] is not None and high["irr_pct"] is not None) else None
        )
        rows.append({
            "factor": factor,
            "low_label": f"-{var_pct:g}%",
            "high_label": f"+{var_pct:g}%",
            "low": low,
            "high": high,
            "npv_swing": npv_swing,
            "irr_swing_pct": irr_swing,
        })

    rows.sort(key=lambda item: -item["npv_swing"])
    return {
        "variation_pct": var_pct,
        "base": {
            "npv": base_metrics["npv"],
            "irr_pct": base_metrics["irr_pct"],
            "lcoe": base_metrics["lcoe"],
            "simple_payback_years": base_metrics["simple_payback_years"],
        },
        "factors": rows,
    }


def evaluate_economics(
    *,
    workspace: Path | None = None,
    scenarios: list[dict] | None = None,
    batch_summary_csv: str | None = None,
    financial: dict | None = None,
    tariff: dict | None = None,
    sensitivity: dict | None = None,
    rank_by: str = "npv",
    include_cashflow_schedule: bool = True,
) -> dict:
    """Evaluate single or multi-scenario PV/PV+BESS financial KPIs, TOU/demand settlement, sensitivity, and rankings."""
    if rank_by not in _VALID_RANK_KEYS:
        raise ValueError(f"rank_by must be one of: {', '.join(_VALID_RANK_KEYS)}")
    if type(include_cashflow_schedule) is not bool:
        raise ValueError("include_cashflow_schedule must be a strict boolean")

    fin_cfg = _validate_financial(financial)
    tar_cfg = _validate_tariff(tariff)
    sens_cfg = _validate_sensitivity(sensitivity)

    resolved_scenarios: list[dict] = list(scenarios or [])
    if batch_summary_csv:
        if workspace is None:
            raise ValueError("workspace is required when loading batch_summary_csv")
        safe_batch = safe_filename(batch_summary_csv, ".csv")
        batch_path = workspace_file(workspace, "UserBatch", safe_batch, ".csv")
        parsed = parse_batch_results(batch_path, columns=("E_Grid",))
        for row in parsed.get("scenarios") or ():
            if row.get("error"):
                continue
            ident = str(row.get("id") or f"SIM_{len(resolved_scenarios) + 1}")
            metrics = row.get("values") or {}
            hourly_name = row.get("hourly_file")
            item: dict[str, Any] = {"label": ident}
            if row.get("parameters"):
                item["batch_parameters"] = dict(row["parameters"])
            if hourly_name and hourly_name.lower().endswith(".csv"):
                hourly_path = workspace_file(workspace, "UserHourly", hourly_name, ".csv")
                if hourly_path.is_file():
                    item["csv_path"] = hourly_path
            if "csv_path" not in item:
                e_grid = metrics.get("E_Grid")
                if not isinstance(e_grid, (int, float)) or not math.isfinite(e_grid):
                    continue
                item["annual_yield_kwh"] = float(e_grid)
            resolved_scenarios.append(item)

    if not resolved_scenarios:
        raise ValueError("Provide at least one scenario in scenarios or a valid batch_summary_csv")
    if len(resolved_scenarios) > 100:
        raise ValueError("At most 100 scenarios can be evaluated in one call")

    any_has_battery = any(
        isinstance(item, dict)
        and isinstance(item.get("battery_kwh"), (int, float))
        and not isinstance(item.get("battery_kwh"), bool)
        and float(item["battery_kwh"]) > 0.0
        for item in resolved_scenarios
    )
    results: list[dict] = []
    for index, sc in enumerate(resolved_scenarios):
        if not isinstance(sc, dict):
            raise ValueError(f"scenarios[{index}] must be an object")
        label = str(sc.get("label") or f"scenario_{index + 1}").strip()
        dc_kw = (
            _finite(sc["installed_dc_kw"], f"scenarios[{index}].installed_dc_kw", minimum=0.0)
            if sc.get("installed_dc_kw") is not None else None
        )
        bat_kwh = (
            _finite(sc["battery_kwh"], f"scenarios[{index}].battery_kwh", minimum=0.0)
            if sc.get("battery_kwh") is not None else 0.0
        )

        # Resolve CAPEX
        if sc.get("capex_total") is not None:
            capex = _finite(sc["capex_total"], f"scenarios[{index}].capex_total", minimum=0.0)
        else:
            capex = fin_cfg["fixed_capex"]
            if fin_cfg["capex_per_kw"] is not None:
                if dc_kw is None:
                    raise ValueError(
                        f"scenarios[{index}] ({label}): installed_dc_kw is required when financial.capex_per_kw is set"
                    )
                capex += dc_kw * fin_cfg["capex_per_kw"]
            if fin_cfg["battery_capex_per_kwh"] is not None:
                capex += bat_kwh * fin_cfg["battery_capex_per_kwh"]

        # Resolve annual OPEX
        if sc.get("annual_opex") is not None:
            opex = _finite(sc["annual_opex"], f"scenarios[{index}].annual_opex", minimum=0.0)
        else:
            opex = capex * (fin_cfg["opex_pct_capex"] / 100.0)
            if fin_cfg["opex_per_kw_year"] > 0:
                if dc_kw is None:
                    raise ValueError(
                        f"scenarios[{index}] ({label}): installed_dc_kw is required when financial.opex_per_kw_year > 0"
                    )
                opex += dc_kw * fin_cfg["opex_per_kw_year"]

        # Scenario-specific or shared replacement events (plus optional mid-life battery replacement)
        sc_replacements = list(fin_cfg["replacement_events"])
        if fin_cfg["battery_replacement_year"] is not None:
            is_pv_only = (bat_kwh == 0.0) and (sc.get("battery_kwh") is not None or any_has_battery)
            if is_pv_only:
                rep_cost = 0.0
            elif fin_cfg["battery_replacement_cost"] is not None:
                rep_cost = fin_cfg["battery_replacement_cost"]
            elif bat_kwh > 0:
                if fin_cfg["battery_capex_per_kwh"] is None:
                    raise ValueError(
                        f"scenarios[{index}] ({label}): financial.battery_capex_per_kwh or "
                        "financial.battery_replacement_cost is required when financial.battery_replacement_year is set"
                    )
                ratio = (
                    fin_cfg["battery_replacement_cost_ratio"]
                    if fin_cfg["battery_replacement_cost_ratio"] is not None else 0.7
                )
                rep_cost = bat_kwh * fin_cfg["battery_capex_per_kwh"] * ratio
            else:
                rep_cost = 0.0
            if rep_cost > 0:
                sc_replacements.append({
                    "year": fin_cfg["battery_replacement_year"],
                    "cost": rep_cost,
                    "label": f"battery_replacement_year_{fin_cfg['battery_replacement_year']}",
                })
        for ev_i, ev in enumerate(sc.get("replacement_events") or ()):
            if not isinstance(ev, dict):
                raise ValueError(f"scenarios[{index}].replacement_events[{ev_i}] must be an object")
            yr = ev.get("year")
            if type(yr) is not int or not 1 <= yr <= fin_cfg["project_life_years"]:
                raise ValueError(f"scenarios[{index}].replacement_events[{ev_i}].year out of project life")
            sc_replacements.append({
                "year": yr,
                "cost": _finite(ev.get("cost"), f"scenarios[{index}].replacement_events[{ev_i}].cost", minimum=0.0),
                "label": str(ev.get("label") or f"replacement_year_{yr}").strip(),
            })

        settlement: dict | None = None
        y1_demand_savings = 0.0
        if sc.get("csv_name") or sc.get("csv_path"):
            csv_path = _resolve_result_csv(workspace, sc.get("csv_path") or sc["csv_name"])
            settlement = _settle_csv_year1(csv_path, tar_cfg)
            y1_energy = settlement["year1_energy_kwh"]
            y1_demand_savings = settlement["year1_demand_savings"]
            y1_benefit = settlement["year1_gross_benefit"]
            sc_warnings = list(settlement["warnings"])
        elif sc.get("annual_yield_kwh") is not None:
            y1_energy = _finite(sc["annual_yield_kwh"], f"scenarios[{index}].annual_yield_kwh", minimum=0.0)
            sc_ratio = (
                tar_cfg["self_consumption_ratio"]
                if (tar_cfg["mode"] != "feed_in" and tar_cfg["self_consumption_ratio"] is not None)
                else 0.0
            )
            y1_benefit = y1_energy * (
                sc_ratio * tar_cfg["import_tariff"] + (1.0 - sc_ratio) * tar_cfg["feed_in_tariff"]
            )
            sc_warnings = []
            if tar_cfg["mode"] == "tou":
                sc_warnings.append(
                    "annual_yield_kwh summary input has no hourly profile; valued using flat tariff fallback."
                )
            if tar_cfg.get("demand_charge") is not None:
                sc_warnings.append(
                    "annual_yield_kwh summary input has no hourly profile; demand_charge was skipped."
                )
        else:
            raise ValueError(f"scenarios[{index}] ({label}) must provide csv_name, csv_path, or annual_yield_kwh")

        metrics = _compute_cashflow_metrics(
            year1_energy_kwh=y1_energy,
            year1_gross_benefit=y1_benefit,
            year1_demand_savings=y1_demand_savings,
            capex_total=capex,
            base_annual_opex=opex,
            replacement_events=sc_replacements,
            financial=fin_cfg,
        )
        sens_result = (
            _compute_sensitivity(
                sens_cfg=sens_cfg,
                base_metrics=metrics,
                year1_energy_kwh=y1_energy,
                year1_energy_benefit=y1_benefit - y1_demand_savings,
                year1_demand_savings=y1_demand_savings,
                capex_total=capex,
                base_annual_opex=opex,
                replacement_events=sc_replacements,
                financial=fin_cfg,
            )
            if sens_cfg is not None else None
        )
        if not include_cashflow_schedule:
            metrics.pop("cashflow_schedule", None)

        entry: dict[str, Any] = {
            "label": label,
            "installed_dc_kw": dc_kw,
            "battery_kwh": bat_kwh if bat_kwh else None,
            "specific_yield_kwh_per_kwp": round(y1_energy / dc_kw, 2) if (dc_kw and dc_kw > 0) else None,
            "year1_energy_kwh": round(y1_energy, 2),
            "financials": metrics,
            "warnings": sc_warnings,
        }
        if sens_result is not None:
            entry["sensitivity"] = sens_result
        if sc.get("batch_parameters"):
            entry["batch_parameters"] = sc["batch_parameters"]
        if settlement is not None:
            entry["settlement"] = {
                "rows": settlement["rows"],
                "step_minutes": settlement["step_minutes"],
                "has_load": settlement["has_load"],
                "has_storage": settlement["has_storage"],
                "energy_summary": settlement["energy_summary"],
                "demand_charge_summary": settlement["demand_charge_summary"],
                "tou_breakdown": settlement["tou_breakdown"],
                "monthly_summary": settlement["monthly_summary"],
                "typical_day_profile": settlement["typical_day_profile"],
            }
        results.append(entry)

    # Ranking: lower is better for LCOE and payback; higher is better for NPV, IRR, year1_net_benefit
    lower_is_better = rank_by in ("lcoe", "simple_payback_years", "discounted_payback_years")
    metric_key = "irr_pct" if rank_by == "irr" else rank_by

    def _sort_key(item: dict) -> tuple[int, float]:
        val = item["financials"].get(metric_key)
        if val is None or not math.isfinite(val):
            return (1, 0.0)
        return (0, float(val) if lower_is_better else -float(val))

    ranked = sorted(results, key=_sort_key)
    ranking_table = [
        {
            "rank": rank_idx,
            "label": item["label"],
            "year1_energy_kwh": item["year1_energy_kwh"],
            "capex_total": item["financials"]["capex_total"],
            "lcoe": item["financials"]["lcoe"],
            "npv": item["financials"]["npv"],
            "irr_pct": item["financials"]["irr_pct"],
            "simple_payback_years": item["financials"]["simple_payback_years"],
            "discounted_payback_years": item["financials"]["discounted_payback_years"],
            "year1_net_benefit": item["financials"]["year1_net_benefit"],
        }
        for rank_idx, item in enumerate(ranked, start=1)
    ]

    return {
        "status": "ok",
        "currency": fin_cfg["currency"],
        "project_life_years": fin_cfg["project_life_years"],
        "discount_rate_pct": fin_cfg["discount_rate_pct"],
        "tariff_mode": tar_cfg["mode"],
        "rank_by": rank_by,
        "best_scenario": ranked[0]["label"] if _sort_key(ranked[0])[0] == 0 else None,
        "scenario_count": len(results),
        "ranking": ranking_table,
        "scenarios": results,
    }

"""Deterministic PV & PV+BESS financial, TOU tariff, LCOE, NPV, and IRR evaluation engine.

Pure Python standard library implementation. Streams hourly or sub-hourly PVsyst SFI CSVs
(5/10/15/30/60 min) or batch summary CSVs to compute auditable year-1 energy/TOU settlements,
25-year degraded cashflows, LCOE, NPV, IRR, simple/discounted payback, and multi-scenario rankings.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pvsyst_auto.native.cli import parse_batch_results
from pvsyst_auto.project.paths import safe_filename, workspace_file
from pvsyst_auto.results import BATCH_ENERGY_TO_KWH, RegularResultCsv, iter_aligned_result_rows
from pvsyst_auto.validation import finite_number as _finite

_VALID_RANK_KEYS = ("npv", "irr", "equity_irr", "lcoe", "simple_payback_years", "discounted_payback_years", "year1_net_benefit")
_VALID_SENSITIVITY_FACTORS = ("tariff", "capex", "yield", "degradation")


def _int_range(val: Any, label: str, lo: int, hi: int) -> int:
    if type(val) is not int or not lo <= val <= hi:
        raise ValueError(f"{label} must be an integer in {lo}..{hi}")
    return val


def _strict_bool(raw: dict, key: str, label: str, default: bool) -> bool:
    val = raw.get(key, default)
    if type(val) is not bool:
        raise ValueError(f"{label} must be a strict boolean")
    return val


def _opt_fin(raw: dict, key: str, label: str, **kw) -> float | None:
    return _finite(raw[key], label, **kw) if raw.get(key) is not None else None


def _validate_vat(raw_vat: dict | None) -> dict | None:
    if raw_vat is None:
        return None
    if not isinstance(raw_vat, dict):
        raise ValueError("financial.vat must be an object")
    rate_pct = _finite(raw_vat.get("rate_pct", 0.0), "financial.vat.rate_pct", minimum=0.0, maximum=50.0)
    includes_vat = _strict_bool(raw_vat, "tariff_includes_vat", "financial.vat.tariff_includes_vat", True)
    input_credit = _opt_fin(raw_vat, "input_vat_credit", "financial.vat.input_vat_credit", minimum=0.0)
    ded_share = _opt_fin(raw_vat, "deductible_capex_share", "financial.vat.deductible_capex_share", minimum=0.0, maximum=1.0)
    if input_credit is not None and ded_share is not None:
        raise ValueError("financial.vat: specify either input_vat_credit or deductible_capex_share, not both")
    return {
        "rate_pct": rate_pct,
        "tariff_includes_vat": includes_vat,
        "input_vat_credit": input_credit,
        "deductible_capex_share": ded_share,
        "surtax_pct": _finite(raw_vat.get("surtax_pct", 0.0), "financial.vat.surtax_pct", minimum=0.0, maximum=50.0),
        "opex_input_vat_pct": _finite(raw_vat.get("opex_input_vat_pct", 0.0), "financial.vat.opex_input_vat_pct", minimum=0.0, maximum=50.0),
    }


def _validate_cit(raw_cit: dict | None, life: int) -> dict | None:
    if raw_cit is None:
        return None
    if not isinstance(raw_cit, dict):
        raise ValueError("financial.cit must be an object")
    dep_raw = raw_cit.get("depreciation_years")
    dep_years = life if dep_raw is None else _int_range(dep_raw, "financial.cit.depreciation_years", 1, 50)
    return {
        "rate_pct": _finite(raw_cit.get("rate_pct", 0.0), "financial.cit.rate_pct", minimum=0.0, maximum=60.0),
        "exempt_years": _int_range(raw_cit.get("exempt_years", 0), "financial.cit.exempt_years", 0, 30),
        "half_rate_years": _int_range(raw_cit.get("half_rate_years", 0), "financial.cit.half_rate_years", 0, 30),
        "depreciation_years": dep_years,
        "residual_value_pct": _finite(raw_cit.get("residual_value_pct", 0.0), "financial.cit.residual_value_pct", minimum=0.0, maximum=50.0),
        "loss_carryforward_years": _int_range(raw_cit.get("loss_carryforward_years", 5), "financial.cit.loss_carryforward_years", 0, 50),
    }


def _validate_financing(raw_fin: dict | None) -> dict | None:
    if raw_fin is None:
        return None
    if not isinstance(raw_fin, dict):
        raise ValueError("financial.financing must be an object")
    tenor = _int_range(raw_fin.get("loan_tenor_years", 15), "financial.financing.loan_tenor_years", 1, 50)
    grace = _int_range(raw_fin.get("loan_grace_years", 0), "financial.financing.loan_grace_years", 0, 10)
    if grace >= tenor:
        raise ValueError("financial.financing.loan_grace_years must be strictly less than loan_tenor_years")
    method = raw_fin.get("repayment_method", "equal_principal")
    if method not in ("equal_principal", "equal_payment"):
        raise ValueError("financial.financing.repayment_method must be 'equal_principal' or 'equal_payment'")
    return {
        "equity_ratio_pct": _finite(raw_fin.get("equity_ratio_pct", 100.0), "financial.financing.equity_ratio_pct", minimum=1.0, maximum=100.0),
        "loan_tenor_years": tenor,
        "loan_grace_years": grace,
        "loan_interest_rate_pct": _finite(raw_fin.get("loan_interest_rate_pct", 0.0), "financial.financing.loan_interest_rate_pct", minimum=0.0, maximum=30.0),
        "repayment_method": method,
        "equity_discount_rate_pct": _opt_fin(raw_fin, "equity_discount_rate_pct", "financial.financing.equity_discount_rate_pct", minimum=0.0, maximum=50.0),
    }


def _validate_opex_tiers(raw_tiers: list | None, life: int) -> list[dict]:
    if not raw_tiers:
        return []
    if not isinstance(raw_tiers, list):
        raise ValueError("financial.opex_tiers must be a list of tier objects")
    tiers: list[dict] = []
    override_years: set[int] = set()
    for idx, raw in enumerate(raw_tiers):
        if not isinstance(raw, dict):
            raise ValueError(f"financial.opex_tiers[{idx}] must be an object")
        prefix = f"financial.opex_tiers[{idx}]"
        sy = _int_range(raw.get("start_year"), f"{prefix}.start_year", 1, life)
        ey = _int_range(raw.get("end_year"), f"{prefix}.end_year", sy, life)
        ann = _opt_fin(raw, "annual_opex", f"{prefix}.annual_opex", minimum=0.0)
        per_kw = _opt_fin(raw, "opex_per_kw_year", f"{prefix}.opex_per_kw_year", minimum=0.0)
        pct_cap = _opt_fin(raw, "opex_pct_capex", f"{prefix}.opex_pct_capex", minimum=0.0, maximum=50.0)
        extra = _opt_fin(raw, "extra_annual_opex", f"{prefix}.extra_annual_opex", minimum=0.0)
        if ann is None and per_kw is None and pct_cap is None and extra is None:
            raise ValueError(f"{prefix} must set annual_opex, opex_per_kw_year, opex_pct_capex, or extra_annual_opex")
        escalate = _strict_bool(raw, "escalate", f"{prefix}.escalate", False)
        if any(v is not None for v in (ann, per_kw, pct_cap)):
            span = set(range(sy, ey + 1))
            if span & override_years:
                raise ValueError(f"{prefix} base-OPEX year span {sy}..{ey} overlaps an earlier base-OPEX tier")
            override_years |= span
        tiers.append({
            "start_year": sy, "end_year": ey, "annual_opex": ann, "opex_per_kw_year": per_kw,
            "opex_pct_capex": pct_cap, "extra_annual_opex": extra, "escalate": escalate,
            "label": str(raw.get("label") or f"tier_{idx + 1}").strip(),
        })
    return tiers


def _validate_demand_charge(raw_dc: dict | None) -> dict | None:
    if raw_dc is None:
        return None
    if not isinstance(raw_dc, dict):
        raise ValueError("tariff.demand_charge must be an object")
    mode = raw_dc.get("billing_mode", "demand")
    if mode not in ("demand", "capacity", "compare"):
        raise ValueError("tariff.demand_charge.billing_mode must be 'demand', 'capacity', or 'compare'")
    demand_rate = _finite(raw_dc.get("demand_rate_per_kw_month", 0.0), "tariff.demand_charge.demand_rate_per_kw_month", minimum=0.0)
    cap_rate = _opt_fin(raw_dc, "capacity_rate_per_kva_month", "tariff.demand_charge.capacity_rate_per_kva_month", minimum=0.0)
    contract_kva = _opt_fin(raw_dc, "contract_capacity_kva", "tariff.demand_charge.contract_capacity_kva", minimum=0.0)
    if mode in ("capacity", "compare") and (cap_rate is None or contract_kva is None):
        raise ValueError("tariff.demand_charge requires capacity_rate_per_kva_month and contract_capacity_kva when billing_mode is 'capacity' or 'compare'")
    pf = _finite(raw_dc.get("power_factor", 0.95), "tariff.demand_charge.power_factor", minimum=0.5, maximum=1.0)
    months_raw = raw_dc.get("months")
    if months_raw is not None:
        if not isinstance(months_raw, list) or not months_raw or any(type(m) is not int or not 1 <= m <= 12 for m in months_raw):
            raise ValueError("tariff.demand_charge.months must be integers in 1..12")
        active_months = frozenset(months_raw)
    else:
        active_months = frozenset(range(1, 13))
    return {
        "billing_mode": mode, "demand_rate_per_kw_month": demand_rate,
        "capacity_rate_per_kva_month": cap_rate, "contract_capacity_kva": contract_kva,
        "power_factor": pf, "months": active_months,
    }


def _validate_sensitivity(raw_sens: dict | None) -> dict | None:
    if raw_sens is None:
        return None
    if not isinstance(raw_sens, dict):
        raise ValueError("sensitivity must be an object")
    if not _strict_bool(raw_sens, "enabled", "sensitivity.enabled", True):
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
    return {
        "enabled": True, "factors": factors,
        "variation_pct": _finite(raw_sens.get("variation_pct", 15.0), "sensitivity.variation_pct", minimum=0.1, maximum=80.0),
    }


def _validate_tariff(tariff: dict | None) -> dict:
    raw = dict(tariff or {})
    mode = raw.get("mode", "feed_in")
    if mode not in ("feed_in", "self_consumption_flat", "tou"):
        raise ValueError("tariff.mode must be 'feed_in', 'self_consumption_flat', or 'tou'")
    feed_in = _finite(raw.get("feed_in_tariff", 0.0), "tariff.feed_in_tariff", minimum=0.0)
    import_rate = _finite(raw.get("import_tariff", 0.0), "tariff.import_tariff", minimum=0.0)
    sc_ratio = _opt_fin(raw, "self_consumption_ratio", "tariff.self_consumption_ratio", minimum=0.0, maximum=1.0)

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
            if not isinstance(hours, list) or not hours or any(type(h) is not int or not 0 <= h <= 23 for h in hours):
                raise ValueError(f"tariff.tou_schedule[{idx}].hours must be a non-empty list of integers in 0..23")
            months = item.get("months")
            if months is not None and (not isinstance(months, list) or not months or any(type(m) is not int or not 1 <= m <= 12 for m in months)):
                raise ValueError(f"tariff.tou_schedule[{idx}].months must be integers in 1..12")
            day_type = item.get("day_type", "all")
            if day_type not in ("all", "weekday", "weekend"):
                raise ValueError(f"tariff.tou_schedule[{idx}].day_type must be 'all', 'weekday', or 'weekend'")
            p_import = _finite(item.get("import_rate", import_rate), f"tariff.tou_schedule[{idx}].import_rate", minimum=0.0)
            p_export = _finite(item["export_rate"] if item.get("export_rate") is not None else feed_in, f"tariff.tou_schedule[{idx}].export_rate", minimum=0.0)
            if rates_by_name.setdefault(name, (p_import, p_export)) != (p_import, p_export):
                raise ValueError(f"tariff.tou_schedule uses period name {name!r} with different rates")
            periods.append({
                "name": name, "hours": frozenset(hours), "months": frozenset(months) if months is not None else None,
                "day_type": day_type, "import_rate": p_import, "export_rate": p_export,
            })
    return {
        "mode": mode, "feed_in_tariff": feed_in, "import_tariff": import_rate,
        "self_consumption_ratio": sc_ratio, "tou_schedule": periods,
        "demand_charge": _validate_demand_charge(raw.get("demand_charge")),
    }


def _parse_replacement_events(raw_events: Any, life: int, prefix: str, *, scenario_mode: bool = False) -> list[dict]:
    events: list[dict] = []
    for idx, ev in enumerate(raw_events or ()):
        if not isinstance(ev, dict):
            raise ValueError(f"{prefix}.replacement_events[{idx}] must be an object")
        yr = ev.get("year")
        if scenario_mode:
            if type(yr) is not int or not 1 <= yr <= life:
                raise ValueError(f"{prefix}.replacement_events[{idx}].year out of project life")
        else:
            yr = _int_range(yr, f"{prefix}.replacement_events[{idx}].year", 1, life)
        events.append({
            "year": yr,
            "cost": _finite(ev.get("cost"), f"{prefix}.replacement_events[{idx}].cost", minimum=0.0),
            "label": str(ev.get("label") or f"replacement_year_{yr}").strip(),
        })
    return events


def _validate_financial(financial: dict | None) -> dict:
    raw = dict(financial or {})
    currency = str(raw.get("currency") or "CNY").strip()
    if not currency or len(currency) > 12:
        raise ValueError("financial.currency must be 1..12 characters")
    life = _int_range(raw.get("project_life_years", 25), "financial.project_life_years", 1, 50)
    bat_rep_year = raw.get("battery_replacement_year")
    if bat_rep_year is not None:
        bat_rep_year = _int_range(bat_rep_year, "financial.battery_replacement_year", 1, life)
    return {
        "currency": currency, "project_life_years": life,
        "discount_rate_pct": _finite(raw.get("discount_rate_pct", 6.0), "financial.discount_rate_pct", minimum=0.0, maximum=50.0),
        "first_year_degradation_pct": _finite(raw.get("first_year_degradation_pct", 1.0), "financial.first_year_degradation_pct", minimum=0.0, maximum=25.0),
        "annual_degradation_pct": _finite(raw.get("annual_degradation_pct", 0.4), "financial.annual_degradation_pct", minimum=0.0, maximum=10.0),
        "opex_escalation_pct": _finite(raw.get("opex_escalation_pct", 2.0), "financial.opex_escalation_pct", minimum=-10.0, maximum=20.0),
        "tariff_escalation_pct": _finite(raw.get("tariff_escalation_pct", 0.0), "financial.tariff_escalation_pct", minimum=-10.0, maximum=20.0),
        "capex_per_kw": _opt_fin(raw, "capex_per_kw", "financial.capex_per_kw", minimum=0.0),
        "battery_capex_per_kwh": _opt_fin(raw, "battery_capex_per_kwh", "financial.battery_capex_per_kwh", minimum=0.0),
        "fixed_capex": _finite(raw.get("fixed_capex", 0.0), "financial.fixed_capex", minimum=0.0),
        "opex_per_kw_year": _finite(raw.get("opex_per_kw_year", 0.0), "financial.opex_per_kw_year", minimum=0.0),
        "opex_pct_capex": _finite(raw.get("opex_pct_capex", 0.0), "financial.opex_pct_capex", minimum=0.0, maximum=50.0),
        "salvage_value_pct": _finite(raw.get("salvage_value_pct", 0.0), "financial.salvage_value_pct", minimum=0.0, maximum=100.0),
        "upfront_opex": _finite(raw.get("upfront_opex", 0.0), "financial.upfront_opex", minimum=0.0),
        "opex_tiers": _validate_opex_tiers(raw.get("opex_tiers"), life),
        "vat": _validate_vat(raw.get("vat")), "cit": _validate_cit(raw.get("cit"), life),
        "financing": _validate_financing(raw.get("financing")),
        "battery_replacement_year": bat_rep_year,
        "battery_replacement_cost_ratio": _opt_fin(raw, "battery_replacement_cost_ratio", "financial.battery_replacement_cost_ratio", minimum=0.0, maximum=2.0),
        "battery_replacement_cost": _opt_fin(raw, "battery_replacement_cost", "financial.battery_replacement_cost", minimum=0.0),
        "replacement_events": _parse_replacement_events(raw.get("replacement_events"), life, "financial"),
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
        if (period["day_type"] == "weekday" and is_weekend) or (period["day_type"] == "weekend" and not is_weekend):
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
            if (candidate := workspace_file(workspace, folder, safe, ".csv")).is_file():
                return candidate
        raise FileNotFoundError(f"Result CSV not found in workspace Results/ or UserHourly/: {safe}")
    path = Path(text)
    if not path.is_file():
        raise FileNotFoundError(f"Result CSV not found: {path}")
    return path


def _resolve_system_capex(
    fin_cfg: dict, *, dc_kw: float | None, battery_kwh: float = 0.0,
    pcs_kw: float = 0.0, pcs_capex_per_kw: float = 0.0, context: str = "",
) -> float:
    capex = fin_cfg["fixed_capex"]
    if fin_cfg["capex_per_kw"] is not None:
        if dc_kw is None:
            raise ValueError(f"{context}: installed_dc_kw is required when financial.capex_per_kw is set")
        capex += dc_kw * fin_cfg["capex_per_kw"]
    if fin_cfg["battery_capex_per_kwh"] is not None and battery_kwh > 0:
        capex += battery_kwh * fin_cfg["battery_capex_per_kwh"]
    if pcs_capex_per_kw > 0 and pcs_kw > 0:
        capex += pcs_kw * pcs_capex_per_kw
    return capex


def _battery_replacement_event(
    fin_cfg: dict, *, battery_kwh: float, is_pv_only: bool = False,
    require_cost_basis: bool = False, context: str = "", label: str | None = None,
) -> dict | None:
    year = fin_cfg["battery_replacement_year"]
    if year is None or is_pv_only:
        return None
    if fin_cfg["battery_replacement_cost"] is not None:
        cost = fin_cfg["battery_replacement_cost"]
    elif battery_kwh > 0:
        if fin_cfg["battery_capex_per_kwh"] is None:
            if require_cost_basis:
                raise ValueError(
                    f"{context}: financial.battery_capex_per_kwh or "
                    "financial.battery_replacement_cost is required when financial.battery_replacement_year is set"
                )
            return None
        ratio = fin_cfg["battery_replacement_cost_ratio"] if fin_cfg["battery_replacement_cost_ratio"] is not None else 0.7
        cost = battery_kwh * fin_cfg["battery_capex_per_kwh"] * ratio
    else:
        return None
    return None if cost <= 0 else {"year": year, "cost": cost, "label": label or f"battery_replacement_year_{year}"}


def _settle_csv_year1(csv_path: Path, tariff: dict) -> dict:
    """Single-pass streaming settlement over an hourly or sub-hourly PVsyst SFI CSV."""
    return _settle_multi_csv_year1([csv_path], [1.0], tariff)


def _settle_multi_csv_year1(csv_paths: list[Path], weights: list[float], tariff: dict) -> dict:
    """Single-pass streaming settlement over one or more timestamp-aligned SFI CSVs."""
    if not csv_paths or len(csv_paths) != len(weights):
        raise ValueError("csv_paths and weights must be non-empty lists of identical length")
    readers = [
        RegularResultCsv(
            p, empty_message=f"Result CSV {p.name} contains no simulation rows",
            irregular_message=f"Result CSV {p.name} timestamps do not have a regular cadence (missing, duplicate, or out-of-order rows); cannot settle energy or tariffs reliably",
            required_any_of=("EToGrid", "E_Grid"),
            missing_required_message=f"Result CSV {p.name} must include E_Grid or EToGrid for financial evaluation",
        )
        for p in csv_paths
    ]

    _m_fields = ("e_grid", "e_user", "e_from_grid", "e_bat_ch", "e_bat_dis", "export_rev", "baseline_bill", "import_cost", "self_cons", "baseline_peak_kw", "actual_peak_kw")
    monthly = {m: dict.fromkeys(_m_fields, 0.0) for m in range(1, 13)}
    _h_fields = ("e_grid", "e_user", "e_from_grid", "e_bat_ch", "e_bat_dis", "import_rate_sum", "export_rate_sum")
    hourly_prof = {h: {"count": 0, **dict.fromkeys(_h_fields, 0.0)} for h in range(24)}
    tou_breakdown: dict[str, dict[str, float]] = {}
    _tot_fields = ("pv_export_kwh", "grid_import_kwh", "load_kwh", "self_consumed_kwh", "battery_charge_kwh", "battery_discharge_kwh", "export_revenue", "baseline_grid_bill", "actual_grid_bill", "avoided_import_savings")
    totals = dict.fromkeys(_tot_fields, 0.0)

    per_meta: list[dict[str, Any]] = []
    has_load = has_storage = False

    for stamp, dt_hours, items in iter_aligned_result_rows(
        readers,
        count_mismatch_message="Merged result CSVs have different row counts; all portfolio CSVs must span identical timestamps",
        detailed_timestamp_error=True,
    ):
        if not per_meta:
            per_meta = [{
                "reader": r, "idx": r.idx, "export_col": "EToGrid" if "EToGrid" in r.idx else "E_Grid",
                "has_load": "E_User" in r.idx, "has_import": "EFrGrid" in r.idx,
                "has_storage": ("EBatCh" in r.idx or "EBatDis" in r.idx),
            } for r in readers]
            has_load = any(m["has_load"] for m in per_meta)
            has_storage = any(m["has_storage"] for m in per_meta)

        self_cons = inferred_self_cons = e_exp = e_user = e_imp = e_bat_ch = e_bat_dis = 0.0
        for meta, w, (_, row, dt_k) in zip(per_meta, weights, items):
            idx, reader_k = meta["idx"], meta["reader"]
            raw_eg = reader_k.cell_kwh(row, meta["export_col"], dt_k)
            e_exp += w * max(0.0, raw_eg)
            e_user_k = max(0.0, reader_k.cell_kwh(row, "E_User", dt_k)) if meta["has_load"] else 0.0
            e_user += w * e_user_k
            if meta["has_import"]:
                e_imp_k = max(0.0, reader_k.cell_kwh(row, "EFrGrid", dt_k))
            elif meta["has_load"] and "E_Solar" in idx:
                e_imp_k = max(0.0, e_user_k - max(0.0, reader_k.cell_kwh(row, "E_Solar", dt_k)))
            elif raw_eg < 0:
                e_imp_k = -raw_eg
            else:
                e_imp_k = 0.0
            if meta["has_load"]:
                self_cons += w * max(0.0, e_user_k - e_imp_k)
            elif tariff["mode"] != "feed_in" and tariff["self_consumption_ratio"]:
                inferred = w * max(0.0, raw_eg) * tariff["self_consumption_ratio"]
                inferred_self_cons += inferred
                self_cons += inferred
                e_exp -= inferred
            e_imp += w * e_imp_k
            if "EBatCh" in idx:
                e_bat_ch += w * max(0.0, reader_k.cell_kwh(row, "EBatCh", dt_k))
            if "EBatDis" in idx:
                e_bat_dis += w * max(0.0, reader_k.cell_kwh(row, "EBatDis", dt_k))

        p_name, imp_rate, exp_rate = _lookup_rate(stamp, tariff)
        baseline_bill = (e_user + inferred_self_cons) * imp_rate
        import_cost = e_imp * imp_rate
        export_rev = e_exp * exp_rate
        avoided = baseline_bill - import_cost

        for k, delta in (
            ("pv_export_kwh", e_exp), ("grid_import_kwh", e_imp), ("load_kwh", e_user),
            ("self_consumed_kwh", self_cons), ("battery_charge_kwh", e_bat_ch),
            ("battery_discharge_kwh", e_bat_dis), ("export_revenue", export_rev),
            ("baseline_grid_bill", baseline_bill), ("actual_grid_bill", import_cost), ("avoided_import_savings", avoided),
        ):
            totals[k] += delta

        tb = tou_breakdown.setdefault(p_name, {
            "import_rate": imp_rate, "export_rate": exp_rate, "hours": 0.0, "export_kwh": 0.0,
            "import_kwh": 0.0, "self_consumed_kwh": 0.0, "battery_charge_kwh": 0.0,
            "battery_discharge_kwh": 0.0, "export_revenue": 0.0, "avoided_import_savings": 0.0,
        })
        for k, delta in (
            ("hours", dt_hours), ("export_kwh", e_exp), ("import_kwh", e_imp), ("self_consumed_kwh", self_cons),
            ("battery_charge_kwh", e_bat_ch), ("battery_discharge_kwh", e_bat_dis),
            ("export_revenue", export_rev), ("avoided_import_savings", avoided),
        ):
            tb[k] += delta

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
            m_rec["baseline_peak_kw"] = max(m_rec["baseline_peak_kw"], e_user / dt_hours)
            m_rec["actual_peak_kw"] = max(m_rec["actual_peak_kw"], e_imp / dt_hours)

        h_rec = hourly_prof[stamp.hour]
        h_rec["count"] += 1
        inv_dt = (1.0 / dt_hours) if dt_hours > 0 else 0.0
        h_rec["e_grid"] += (e_exp + self_cons) * inv_dt
        h_rec["e_user"] += e_user * inv_dt
        h_rec["e_from_grid"] += e_imp * inv_dt
        h_rec["e_bat_ch"] += e_bat_ch * inv_dt
        h_rec["e_bat_dis"] += e_bat_dis * inv_dt
        h_rec["import_rate_sum"] += imp_rate
        h_rec["export_rate_sum"] += exp_rate

    rows = readers[0].rows_count
    eff_step_min = readers[0].step_minutes if readers[0].step_minutes is not None else 60.0
    total_pv_utilized_kwh = totals["pv_export_kwh"] + totals["self_consumed_kwh"]
    year1_energy_benefit = totals["export_revenue"] + totals["avoided_import_savings"]

    warnings: list[str] = []
    if not has_load and tariff["mode"] != "feed_in" and tariff["self_consumption_ratio"] is None:
        warnings.append("CSV has no E_User column and tariff.self_consumption_ratio was omitted; all PV generation was valued at export_rate.")

    demand_summary: dict[str, Any] | None = None
    year1_demand_savings = 0.0
    dc_cfg = tariff.get("demand_charge")
    if dc_cfg is not None:
        if not has_load:
            warnings.append("tariff.demand_charge was configured, but the CSV has no E_User load column; monthly demand charges and peak shaving savings were skipped.")
        else:
            rate, active_months = dc_cfg["demand_rate_per_kw_month"], dc_cfg["months"]
            monthly_demand: list[dict[str, Any]] = []
            base_dc_total = act_dc_total = 0.0
            for m in range(1, 13):
                b_kw, a_kw = monthly[m]["baseline_peak_kw"], monthly[m]["actual_peak_kw"]
                b_cost, a_cost = (b_kw * rate, a_kw * rate) if m in active_months else (0.0, 0.0)
                base_dc_total += b_cost
                act_dc_total += a_cost
                monthly_demand.append({
                    "month": m, "active": m in active_months,
                    "baseline_peak_kw": round(b_kw, 3), "actual_peak_kw": round(a_kw, 3),
                    "peak_reduction_kw": round(b_kw - a_kw, 3),
                    "baseline_demand_charge": round(b_cost, 2), "actual_demand_charge": round(a_cost, 2),
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
            else:
                assert cap_charge is not None
                year1_demand_savings = min(base_dc_total, cap_charge) - min(act_dc_total, cap_charge)
            demand_summary = {
                "billing_mode": dc_cfg["billing_mode"], "recommended_billing_mode": recommended_mode,
                "demand_rate_per_kw_month": rate,
                "annual_baseline_peak_max_kw": round(max(r["baseline_peak_kw"] for r in monthly_demand), 3),
                "annual_actual_peak_max_kw": round(max(r["actual_peak_kw"] for r in monthly_demand), 3),
                "avg_monthly_peak_reduction_kw": round(sum(r["peak_reduction_kw"] for r in monthly_demand if r["active"]) / max(1, len(active_months)), 3),
                "baseline_annual_demand_charge": round(base_dc_total, 2),
                "actual_annual_demand_charge": round(act_dc_total, 2),
                "annual_capacity_charge": round(cap_charge, 2) if cap_charge is not None else None,
                "annual_demand_savings": round(year1_demand_savings, 2),
                "monthly_peaks": monthly_demand,
            }

    monthly_summary = [
        {
            "month": m, "pv_energy_kwh": round(r["e_grid"], 2), "self_consumed_kwh": round(r["self_cons"], 2),
            "load_kwh": round(r["e_user"], 2), "grid_import_kwh": round(r["e_from_grid"], 2),
            "battery_charge_kwh": round(r["e_bat_ch"], 2), "battery_discharge_kwh": round(r["e_bat_dis"], 2),
            "export_revenue": round(r["export_rev"], 2),
            "avoided_import_savings": round(r["baseline_bill"] - r["import_cost"], 2),
            "gross_benefit": round(r["export_rev"] + (r["baseline_bill"] - r["import_cost"]), 2),
            "baseline_peak_kw": round(r["baseline_peak_kw"], 3), "actual_peak_kw": round(r["actual_peak_kw"], 3),
        }
        for m, r in sorted(monthly.items())
    ]
    typical_day = [
        {
            "hour": h,
            "avg_pv_kw": round(r["e_grid"] / r["count"], 3) if r["count"] else 0.0,
            "avg_load_kw": round(r["e_user"] / r["count"], 3) if r["count"] else 0.0,
            "avg_grid_import_kw": round(r["e_from_grid"] / r["count"], 3) if r["count"] else 0.0,
            "avg_battery_charge_kw": round(r["e_bat_ch"] / r["count"], 3) if r["count"] else 0.0,
            "avg_battery_discharge_kw": round(r["e_bat_dis"] / r["count"], 3) if r["count"] else 0.0,
            "avg_import_rate": round(r["import_rate_sum"] / r["count"], 4) if r["count"] else 0.0,
            "avg_export_rate": round(r["export_rate_sum"] / r["count"], 4) if r["count"] else 0.0,
        }
        for h, r in sorted(hourly_prof.items())
    ]
    merged_sources = [{"csv_name": p.name, "weight": round(w, 6)} for p, w in zip(csv_paths, weights)] if len(csv_paths) > 1 else None
    return {
        "rows": rows, "step_minutes": eff_step_min, "has_load": has_load, "has_storage": has_storage,
        "year1_energy_kwh": total_pv_utilized_kwh, "year1_energy_benefit": year1_energy_benefit,
        "year1_demand_savings": year1_demand_savings, "year1_gross_benefit": year1_energy_benefit + year1_demand_savings,
        "energy_summary": {k: round(v, 2) for k, v in totals.items()},
        "demand_charge_summary": demand_summary,
        "tou_breakdown": {name: {k: round(v, 4) for k, v in rec.items()} for name, rec in tou_breakdown.items()},
        "monthly_summary": monthly_summary, "typical_day_profile": typical_day, "warnings": warnings,
        **({"merged_sources": merged_sources} if merged_sources is not None else {}),
    }


def _compute_irr(cashflows: list[float]) -> float | None:
    """Compute exact Internal Rate of Return (as decimal, e.g. 0.12 = 12%) or None if unsolvable."""
    if not cashflows or len(cashflows) < 2 or not (any(c < 0 for c in cashflows) and any(c > 0 for c in cashflows)):
        return None
    npv_at = lambda rate: sum(cf / ((1.0 + rate) ** t) for t, cf in enumerate(cashflows))
    grid = [-0.95, -0.75, -0.5, -0.25, -0.1, 0.0, 0.02, 0.05, 0.08, 0.12, 0.18, 0.25, 0.4, 0.7, 1.0, 2.0, 5.0, 10.0]
    bracket = None
    prev_r, prev_v = grid[0], npv_at(grid[0])
    for r in grid[1:]:
        v = npv_at(r)
        if v == 0:
            return r
        if (prev_v > 0 and v < 0) or (prev_v < 0 and v > 0):
            bracket = (prev_r, r, prev_v, v)
            break
        prev_r, prev_v = r, v
    if bracket is None:
        return None
    lo, hi, f_lo, _ = bracket
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = npv_at(mid)
        if f_mid == 0 or abs(hi - lo) < 1e-10:
            return mid
        if (f_lo > 0 and f_mid > 0) or (f_lo < 0 and f_mid < 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _apply_loss_carryforward(ebt: float, year: int, carry_years: int, losses: list[list[float]]) -> float:
    """Apply FIFO operating tax-loss carryforward and return taxable income (>= 0)."""
    if carry_years <= 0:
        losses.clear()
        return max(0.0, ebt)
    while losses and (year - int(losses[0][0])) > carry_years:
        losses.pop(0)
    if ebt < 0.0:
        losses.append([float(year), -ebt])
        return 0.0
    remaining = ebt
    while remaining > 0.0 and losses:
        used = min(losses[0][1], remaining)
        remaining -= used
        losses[0][1] -= used
        if losses[0][1] <= 1e-9:
            losses.pop(0)
    return max(0.0, remaining)


def _resolve_year_opex(
    year: int, *, base_annual_opex: float, opex_esc: float,
    opex_tiers: list[dict], capex_total: float, dc_kw: float | None,
) -> float:
    """Resolve annual OPEX for a given operating year (1..life) with optional staged tiers."""
    esc_factor = (1.0 + opex_esc) ** (year - 1)
    base_override: float | None = None
    extra_sum = 0.0
    for tier in opex_tiers:
        if not (tier["start_year"] <= year <= tier["end_year"]):
            continue
        tier_esc = esc_factor if tier["escalate"] else 1.0
        if any(tier[k] is not None for k in ("annual_opex", "opex_per_kw_year", "opex_pct_capex")):
            amt = float(tier["annual_opex"] or 0.0)
            if tier["opex_per_kw_year"] is not None and tier["opex_per_kw_year"] > 0:
                if dc_kw is None:
                    raise ValueError(f"installed_dc_kw is required when financial.opex_tiers ({tier['label']}) sets opex_per_kw_year > 0")
                amt += dc_kw * tier["opex_per_kw_year"]
            if tier["opex_pct_capex"] is not None and tier["opex_pct_capex"] > 0:
                amt += capex_total * (tier["opex_pct_capex"] / 100.0)
            base_override = amt * tier_esc
        if tier["extra_annual_opex"] is not None:
            extra_sum += tier["extra_annual_opex"] * tier_esc
    return (base_override if base_override is not None else base_annual_opex * esc_factor) + extra_sum


def _compute_cashflow_metrics(
    *, year1_energy_kwh: float, year1_gross_benefit: float, capex_total: float,
    base_annual_opex: float, replacement_events: list[dict], financial: dict,
    year1_demand_savings: float = 0.0, dc_kw: float | None = None,
) -> dict:
    life, r = financial["project_life_years"], financial["discount_rate_pct"] / 100.0
    deg1, deg_ann = financial["first_year_degradation_pct"] / 100.0, financial["annual_degradation_pct"] / 100.0
    opex_esc, tariff_esc = financial["opex_escalation_pct"] / 100.0, financial["tariff_escalation_pct"] / 100.0
    salvage_pct, upfront_opex = financial["salvage_value_pct"] / 100.0, float(financial.get("upfront_opex") or 0.0)
    opex_tiers = financial.get("opex_tiers") or []
    vat_cfg, cit_cfg, fin_loan = financial.get("vat"), financial.get("cit"), financial.get("financing")
    year1_energy_benefit = year1_gross_benefit - year1_demand_savings
    rep_by_year: dict[int, float] = {}
    for ev in replacement_events:
        rep_by_year[ev["year"]] = rep_by_year.get(ev["year"], 0.0) + ev["cost"]

    has_vat = vat_cfg is not None
    vat_r = (vat_cfg["rate_pct"] / 100.0) if has_vat else 0.0
    vat_surtax_r = (vat_cfg["surtax_pct"] / 100.0) if has_vat else 0.0
    opex_vat_r = (vat_cfg["opex_input_vat_pct"] / 100.0) if has_vat else 0.0
    if not has_vat:
        initial_input_vat_credit = 0.0
    elif vat_cfg["input_vat_credit"] is not None:
        initial_input_vat_credit = float(vat_cfg["input_vat_credit"])
    elif vat_cfg["deductible_capex_share"] is not None and vat_r > 0.0:
        initial_input_vat_credit = capex_total * vat_cfg["deductible_capex_share"] * (vat_r / (1.0 + vat_r))
    else:
        initial_input_vat_credit = 0.0
    vat_credit_rem, vat_credit_exhausted_year = initial_input_vat_credit, None

    has_cit = cit_cfg is not None
    cit_r = (cit_cfg["rate_pct"] / 100.0) if has_cit else 0.0
    cit_exempt = cit_cfg["exempt_years"] if has_cit else 0
    cit_half = cit_cfg["half_rate_years"] if has_cit else 0
    dep_years = cit_cfg["depreciation_years"] if has_cit else life
    residual_pct = (cit_cfg["residual_value_pct"] / 100.0) if has_cit else 0.0
    carry_years = cit_cfg["loss_carryforward_years"] if has_cit else 0
    depreciable_base = max(0.0, capex_total - initial_input_vat_credit)
    annual_dep = (depreciable_base * (1.0 - residual_pct) / dep_years) if (has_cit and dep_years > 0) else 0.0
    proj_losses: list[list[float]] = []
    eq_losses: list[list[float]] = []

    has_financing = fin_loan is not None
    if has_financing:
        eq_ratio = fin_loan["equity_ratio_pct"] / 100.0
        debt_principal_init = capex_total * (1.0 - eq_ratio)
        equity_investment = capex_total * eq_ratio + upfront_opex
        loan_tenor, loan_grace, loan_r = fin_loan["loan_tenor_years"], fin_loan["loan_grace_years"], fin_loan["loan_interest_rate_pct"] / 100.0
        repay_years, repay_method = loan_tenor - loan_grace, fin_loan["repayment_method"]
        eq_disc_r = (fin_loan["equity_discount_rate_pct"] / 100.0) if fin_loan["equity_discount_rate_pct"] is not None else r
        if repay_method == "equal_payment" and debt_principal_init > 0:
            level_annuity = (debt_principal_init * loan_r / (1.0 - ((1.0 + loan_r) ** (-repay_years)))) if loan_r > 0 else (debt_principal_init / repay_years)
        else:
            level_annuity = 0.0
        debt_balance = debt_principal_init
    else:
        debt_principal_init = equity_investment = loan_r = level_annuity = debt_balance = 0.0
        loan_tenor, loan_grace, repay_years, repay_method, eq_disc_r = 0, 0, 1, "equal_principal", r

    initial_outflow = capex_total + upfront_opex
    pre_tax_cfs, post_tax_cfs = [-initial_outflow], [-initial_outflow]
    equity_cfs = [-equity_investment] if has_financing else []
    schedule: list[dict] = []
    cum_nom = cum_disc = -initial_outflow
    simple_payback: float | None = 0.0 if initial_outflow == 0 else None
    discounted_payback: float | None = 0.0 if initial_outflow == 0 else None
    eq_cum_nom = -equity_investment
    eq_simple_payback: float | None = (0.0 if equity_investment == 0 else None) if has_financing else None
    pv_costs = initial_outflow
    pv_energy = lifetime_energy = lifetime_revenue = lifetime_opex = 0.0
    lifetime_vat_payable = lifetime_vat_surtax = lifetime_cit_project = lifetime_cit_equity = lifetime_interest = 0.0
    dscr_values: list[float] = []

    for year in range(1, life + 1):
        deg_factor = (1.0 - deg1) * ((1.0 - deg_ann) ** (year - 1))
        energy_y = year1_energy_kwh * deg_factor
        gross_y = (year1_energy_benefit * deg_factor + year1_demand_savings) * ((1.0 + tariff_esc) ** (year - 1))
        opex_y = _resolve_year_opex(year, base_annual_opex=base_annual_opex, opex_esc=opex_esc, opex_tiers=opex_tiers, capex_total=capex_total, dc_kw=dc_kw)
        rep_y = rep_by_year.get(year, 0.0)
        salvage_y = capex_total * salvage_pct if year == life else 0.0

        if has_vat:
            if vat_cfg["tariff_includes_vat"]:
                rev_ex_vat = gross_y / (1.0 + vat_r)
                output_vat = gross_y - rev_ex_vat
            else:
                rev_ex_vat, output_vat = gross_y, gross_y * vat_r
            opex_in_vat = opex_y * (opex_vat_r / (1.0 + opex_vat_r)) if opex_vat_r > 0.0 else 0.0
            opex_ex_vat, net_vat_before = opex_y - opex_in_vat, output_vat - opex_in_vat
            if net_vat_before > 0.0:
                credit_used = min(vat_credit_rem, net_vat_before)
                prev_credit = vat_credit_rem
                vat_credit_rem = max(0.0, vat_credit_rem - credit_used)
                if vat_credit_exhausted_year is None and prev_credit > 1e-6 and vat_credit_rem <= 1e-6:
                    vat_credit_exhausted_year = year
                vat_payable = net_vat_before - credit_used
            else:
                vat_credit_rem += -net_vat_before
                vat_payable = 0.0
            vat_surtax, cash_in_y = vat_payable * vat_surtax_r, rev_ex_vat + output_vat
        else:
            rev_ex_vat, opex_ex_vat, vat_payable, vat_surtax, cash_in_y = gross_y, opex_y, 0.0, 0.0, gross_y

        if has_financing and year <= loan_tenor and debt_balance > 1e-9:
            interest_y = debt_balance * loan_r
            if year == life:
                principal_y = debt_balance
            elif year <= loan_grace:
                principal_y = 0.0
            elif repay_method == "equal_principal":
                principal_y = min(debt_balance, debt_principal_init / repay_years)
            else:
                principal_y = min(debt_balance, max(0.0, level_annuity - interest_y))
            debt_balance = max(0.0, debt_balance - principal_y)
            debt_service_y = principal_y + interest_y
        else:
            interest_y = principal_y = debt_service_y = 0.0

        dep_y = annual_dep if (has_cit and year <= dep_years) else 0.0
        if has_cit:
            eff_cit_r = 0.0 if year <= cit_exempt else (0.5 * cit_r if year <= cit_exempt + cit_half else cit_r)
            ebit_proj = rev_ex_vat - opex_ex_vat - vat_surtax - dep_y
            cit_proj_y = _apply_loss_carryforward(ebit_proj, year, carry_years, proj_losses) * eff_cit_r
            cit_eq_y = _apply_loss_carryforward(ebit_proj - interest_y, year, carry_years, eq_losses) * eff_cit_r if has_financing else cit_proj_y
        else:
            cit_proj_y = cit_eq_y = 0.0

        pre_tax_y = cash_in_y - opex_y - vat_payable - vat_surtax - rep_y + salvage_y
        post_tax_y = pre_tax_y - cit_proj_y
        net_y = post_tax_y if has_cit else pre_tax_y
        disc_factor = 1.0 / ((1.0 + r) ** year)
        disc_net_y = net_y * disc_factor

        prev_nom, prev_disc = cum_nom, cum_disc
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
        lifetime_vat_payable += vat_payable
        lifetime_vat_surtax += vat_surtax
        lifetime_cit_project += cit_proj_y
        lifetime_cit_equity += cit_eq_y
        lifetime_interest += interest_y
        pre_tax_cfs.append(pre_tax_y)
        post_tax_cfs.append(post_tax_y)

        row_item: dict[str, Any] = {
            "year": year, "degradation_factor": round(deg_factor, 5), "energy_kwh": round(energy_y, 2),
            "gross_benefit": round(gross_y, 2), "opex": round(opex_y, 2), "replacement_cost": round(rep_y, 2),
            "salvage_value": round(salvage_y, 2), "net_cashflow": round(net_y, 2),
            "discounted_cashflow": round(disc_net_y, 2), "cumulative_cashflow": round(cum_nom, 2),
            "cumulative_discounted_cashflow": round(cum_disc, 2),
        }
        if has_vat:
            row_item.update({
                "revenue_ex_vat": round(rev_ex_vat, 2), "vat_payable": round(vat_payable, 2),
                "vat_surtax": round(vat_surtax, 2), "vat_credit_remaining": round(vat_credit_rem, 2),
            })
        if has_cit:
            row_item.update({
                "depreciation": round(dep_y, 2), "cit_project": round(cit_proj_y, 2),
                "pre_tax_project_cashflow": round(pre_tax_y, 2), "post_tax_project_cashflow": round(post_tax_y, 2),
            })
        if has_financing:
            cfads_y = pre_tax_y - cit_eq_y
            eq_cf_y = cfads_y - debt_service_y
            equity_cfs.append(eq_cf_y)
            prev_eq = eq_cum_nom
            eq_cum_nom += eq_cf_y
            if eq_simple_payback is None and prev_eq < 0 <= eq_cum_nom and eq_cf_y > 0:
                eq_simple_payback = (year - 1) + (-prev_eq / eq_cf_y)
            dscr_y = (cfads_y / debt_service_y) if debt_service_y > 1e-9 else None
            if dscr_y is not None:
                dscr_values.append(dscr_y)
            row_item.update({
                "debt_interest": round(interest_y, 2), "debt_principal": round(principal_y, 2),
                "debt_service": round(debt_service_y, 2), "dscr": round(dscr_y, 3) if dscr_y is not None else None,
                "equity_cashflow": round(eq_cf_y, 2),
            })
        schedule.append(row_item)

    primary_cfs = post_tax_cfs if has_cit else pre_tax_cfs
    npv = sum(cf / ((1.0 + r) ** t) for t, cf in enumerate(primary_cfs))
    irr = _compute_irr(primary_cfs)
    lcoe = (pv_costs / pv_energy) if pv_energy > 0 else None
    y1_e_deg = year1_energy_benefit * (1.0 - deg1)
    y1_gross_deg = y1_e_deg + year1_demand_savings
    y1_opex_act = schedule[0]["opex"] if schedule else round(base_annual_opex, 2)
    out: dict[str, Any] = {
        "currency": financial["currency"], "capex_total": round(capex_total, 2),
        "year1_opex": round(y1_opex_act, 2), "year1_energy_benefit": round(y1_e_deg, 2),
        "year1_demand_savings": round(year1_demand_savings, 2), "year1_gross_benefit": round(y1_gross_deg, 2),
        "year1_net_benefit": round(y1_gross_deg - y1_opex_act, 2),
        "lifetime_energy_kwh": round(lifetime_energy, 2), "lifetime_gross_benefit": round(lifetime_revenue, 2),
        "lifetime_opex": round(lifetime_opex, 2), "lcoe": round(lcoe, 5) if lcoe is not None else None,
        "npv": round(npv, 2), "irr_pct": round(irr * 100.0, 3) if irr is not None else None,
        "simple_payback_years": round(simple_payback, 2) if simple_payback is not None else None,
        "discounted_payback_years": round(discounted_payback, 2) if discounted_payback is not None else None,
        "cashflow_schedule": schedule,
    }
    if upfront_opex > 0:
        out["upfront_opex"] = round(upfront_opex, 2)
    if has_vat or has_cit:
        pre_npv, pre_irr = sum(cf / ((1.0 + r) ** t) for t, cf in enumerate(pre_tax_cfs)), _compute_irr(pre_tax_cfs)
        post_npv, post_irr = sum(cf / ((1.0 + r) ** t) for t, cf in enumerate(post_tax_cfs)), _compute_irr(post_tax_cfs)
        out.update({
            "pre_tax_project_npv": round(pre_npv, 2),
            "pre_tax_project_irr_pct": round(pre_irr * 100.0, 3) if pre_irr is not None else None,
            "post_tax_project_npv": round(post_npv, 2),
            "post_tax_project_irr_pct": round(post_irr * 100.0, 3) if post_irr is not None else None,
            "tax_summary": {
                "initial_input_vat_credit": round(initial_input_vat_credit, 2),
                "vat_credit_exhausted_year": vat_credit_exhausted_year,
                "vat_credit_remaining_end": round(vat_credit_rem, 2),
                "lifetime_vat_payable": round(lifetime_vat_payable, 2),
                "lifetime_vat_surtax": round(lifetime_vat_surtax, 2),
                "depreciable_base": round(depreciable_base, 2),
                "lifetime_cit_project": round(lifetime_cit_project, 2),
            },
        })
    if has_financing:
        eq_npv = sum(cf / ((1.0 + eq_disc_r) ** t) for t, cf in enumerate(equity_cfs))
        eq_irr = _compute_irr(equity_cfs)
        eq_irr_pct = round(eq_irr * 100.0, 3) if eq_irr is not None else None
        out["equity_irr_pct"] = eq_irr_pct
        out["financing_summary"] = {
            "equity_ratio_pct": fin_loan["equity_ratio_pct"], "equity_investment": round(equity_investment, 2),
            "debt_principal": round(debt_principal_init, 2), "loan_tenor_years": loan_tenor,
            "loan_grace_years": loan_grace, "loan_interest_rate_pct": fin_loan["loan_interest_rate_pct"],
            "repayment_method": repay_method, "equity_discount_rate_pct": round(eq_disc_r * 100.0, 3),
            "equity_npv": round(eq_npv, 2), "equity_irr_pct": eq_irr_pct,
            "equity_simple_payback_years": round(eq_simple_payback, 2) if eq_simple_payback is not None else None,
            "min_dscr": round(min(dscr_values), 3) if dscr_values else None,
            "avg_dscr": round(sum(dscr_values) / len(dscr_values), 3) if dscr_values else None,
            "lifetime_interest": round(lifetime_interest, 2), "lifetime_cit_equity": round(lifetime_cit_equity, 2),
        }
    return out


def _compute_sensitivity(
    *, sens_cfg: dict, base_metrics: dict, year1_energy_kwh: float, year1_energy_benefit: float,
    year1_demand_savings: float, capex_total: float, base_annual_opex: float,
    replacement_events: list[dict], financial: dict, dc_kw: float | None = None,
) -> dict:
    """Deterministic +/- variation_pct one-at-a-time sensitivity sweep sorted for tornado plotting."""
    var_pct = sens_cfg["variation_pct"]
    frac = var_pct / 100.0

    def _run_case(factor: str, sign: float) -> dict:
        scale = 1.0 + sign * frac
        e_kwh, e_ben, d_sav, capex, fin = year1_energy_kwh, year1_energy_benefit, year1_demand_savings, capex_total, financial
        if factor == "tariff":
            e_ben, d_sav = year1_energy_benefit * scale, year1_demand_savings * scale
        elif factor == "capex":
            capex = capex_total * scale
        elif factor == "yield":
            e_kwh, e_ben = year1_energy_kwh * scale, year1_energy_benefit * scale
        elif factor == "degradation":
            fin = dict(financial)
            fin["first_year_degradation_pct"] = max(0.0, min(25.0, financial["first_year_degradation_pct"] * scale))
            fin["annual_degradation_pct"] = max(0.0, min(10.0, financial["annual_degradation_pct"] * scale))
        m = _compute_cashflow_metrics(
            year1_energy_kwh=e_kwh, year1_gross_benefit=e_ben + d_sav, year1_demand_savings=d_sav,
            capex_total=capex, base_annual_opex=base_annual_opex, replacement_events=replacement_events,
            financial=fin, dc_kw=dc_kw,
        )
        return {k: m[k] for k in ("npv", "irr_pct", "lcoe", "simple_payback_years")}

    rows: list[dict] = []
    for factor in sens_cfg["factors"]:
        low, high = _run_case(factor, -1.0), _run_case(factor, +1.0)
        irr_swing = round(abs(high["irr_pct"] - low["irr_pct"]), 3) if (low["irr_pct"] is not None and high["irr_pct"] is not None) else None
        rows.append({
            "factor": factor, "low_label": f"-{var_pct:g}%", "high_label": f"+{var_pct:g}%",
            "low": low, "high": high, "npv_swing": round(abs(high["npv"] - low["npv"]), 2), "irr_swing_pct": irr_swing,
        })
    rows.sort(key=lambda item: -item["npv_swing"])
    return {
        "variation_pct": var_pct,
        "base": {k: base_metrics[k] for k in ("npv", "irr_pct", "lcoe", "simple_payback_years")},
        "factors": rows,
    }


def evaluate_economics(
    *, workspace: Path | None = None, scenarios: list[dict] | None = None,
    batch_summary_csv: str | None = None, financial: dict | None = None,
    tariff: dict | None = None, sensitivity: dict | None = None,
    rank_by: str = "npv", include_cashflow_schedule: bool = True,
) -> dict:
    """Evaluate single or multi-scenario PV/PV+BESS financial KPIs, TOU/demand settlement, sensitivity, and rankings."""
    if rank_by not in _VALID_RANK_KEYS:
        raise ValueError(f"rank_by must be one of: {', '.join(_VALID_RANK_KEYS)}")
    if type(include_cashflow_schedule) is not bool:
        raise ValueError("include_cashflow_schedule must be a strict boolean")
    fin_cfg, tar_cfg, sens_cfg = _validate_financial(financial), _validate_tariff(tariff), _validate_sensitivity(sensitivity)

    resolved_scenarios: list[dict] = list(scenarios or [])
    if batch_summary_csv:
        if workspace is None:
            raise ValueError("workspace is required when loading batch_summary_csv")
        batch_path = workspace_file(workspace, "UserBatch", safe_filename(batch_summary_csv, ".csv"), ".csv")
        parsed = parse_batch_results(batch_path, columns=("E_Grid",))
        for row in parsed.get("scenarios") or ():
            if row.get("error"):
                continue
            ident = str(row.get("id") or f"SIM_{len(resolved_scenarios) + 1}")
            metrics, hourly_name = row.get("values") or {}, row.get("hourly_file")
            item: dict[str, Any] = {"label": ident}
            if row.get("parameters"):
                item["batch_parameters"] = dict(row["parameters"])
            if hourly_name and hourly_name.lower().endswith(".csv"):
                if (hourly_path := workspace_file(workspace, "UserHourly", hourly_name, ".csv")).is_file():
                    item["csv_path"] = hourly_path
            if "csv_path" not in item:
                e_grid = metrics.get("E_Grid")
                if not isinstance(e_grid, (int, float)) or not math.isfinite(e_grid):
                    continue
                unit = parsed.get("units", {}).get("E_Grid", "kWh")
                if unit not in BATCH_ENERGY_TO_KWH:
                    raise ValueError(f"Expected energy unit for batch E_Grid, got {unit!r}")
                item["annual_yield_kwh"] = e_grid * BATCH_ENERGY_TO_KWH[unit]
            resolved_scenarios.append(item)

    if not resolved_scenarios:
        raise ValueError("Provide at least one scenario in scenarios or a valid batch_summary_csv")
    if len(resolved_scenarios) > 100:
        raise ValueError("At most 100 scenarios can be evaluated in one call")

    any_has_battery = any(
        isinstance(item, dict) and isinstance(item.get("battery_kwh"), (int, float))
        and not isinstance(item.get("battery_kwh"), bool) and float(item["battery_kwh"]) > 0.0
        for item in resolved_scenarios
    )
    results: list[dict] = []
    for index, sc in enumerate(resolved_scenarios):
        if not isinstance(sc, dict):
            raise ValueError(f"scenarios[{index}] must be an object")
        label = str(sc.get("label") or f"scenario_{index + 1}").strip()
        dc_kw = _opt_fin(sc, "installed_dc_kw", f"scenarios[{index}].installed_dc_kw", minimum=0.0)
        bat_kwh = _finite(sc["battery_kwh"], f"scenarios[{index}].battery_kwh", minimum=0.0) if sc.get("battery_kwh") is not None else 0.0

        capex = (
            _finite(sc["capex_total"], f"scenarios[{index}].capex_total", minimum=0.0)
            if sc.get("capex_total") is not None else
            _resolve_system_capex(fin_cfg, dc_kw=dc_kw, battery_kwh=bat_kwh, context=f"scenarios[{index}] ({label})")
        )
        if sc.get("annual_opex") is not None:
            opex = _finite(sc["annual_opex"], f"scenarios[{index}].annual_opex", minimum=0.0)
        else:
            opex = capex * (fin_cfg["opex_pct_capex"] / 100.0)
            if fin_cfg["opex_per_kw_year"] > 0:
                if dc_kw is None:
                    raise ValueError(f"scenarios[{index}] ({label}): installed_dc_kw is required when financial.opex_per_kw_year > 0")
                opex += dc_kw * fin_cfg["opex_per_kw_year"]

        sc_replacements = list(fin_cfg["replacement_events"])
        bat_rep_ev = _battery_replacement_event(
            fin_cfg, battery_kwh=bat_kwh,
            is_pv_only=(bat_kwh == 0.0) and (sc.get("battery_kwh") is not None or any_has_battery),
            require_cost_basis=True, context=f"scenarios[{index}] ({label})",
        )
        if bat_rep_ev is not None:
            sc_replacements.append(bat_rep_ev)
        sc_replacements.extend(_parse_replacement_events(sc.get("replacement_events"), fin_cfg["project_life_years"], f"scenarios[{index}]", scenario_mode=True))

        settlement: dict | None = None
        y1_demand_savings = 0.0
        has_single_csv = bool(sc.get("csv_name") or sc.get("csv_path"))
        has_multi_csv = bool(sc.get("csv_names") or sc.get("csv_paths"))
        if has_single_csv and has_multi_csv:
            raise ValueError(f"scenarios[{index}] ({label}): specify either csv_name/csv_path or csv_names/csv_paths, not both")
        if has_multi_csv or has_single_csv:
            if has_multi_csv:
                raw_list = sc.get("csv_paths") or sc["csv_names"]
                if not isinstance(raw_list, list) or not 1 <= len(raw_list) <= 20:
                    raise ValueError(f"scenarios[{index}].csv_names must contain 1..20 entries")
                resolved_paths = [_resolve_result_csv(workspace, ref) for ref in raw_list]
                raw_weights = sc.get("csv_weights")
                if raw_weights is None:
                    weights = [1.0] * len(resolved_paths)
                else:
                    if not isinstance(raw_weights, list) or len(raw_weights) != len(resolved_paths):
                        raise ValueError(f"scenarios[{index}].csv_weights length must match csv_names ({len(resolved_paths)})")
                    weights = [_finite(w, f"scenarios[{index}].csv_weights[{wi}]", minimum=1e-9) for wi, w in enumerate(raw_weights)]
                settlement = _settle_multi_csv_year1(resolved_paths, weights, tar_cfg)
            else:
                if sc.get("csv_weights") is not None:
                    raise ValueError(f"scenarios[{index}] ({label}): csv_weights requires csv_names")
                settlement = _settle_csv_year1(_resolve_result_csv(workspace, sc.get("csv_path") or sc["csv_name"]), tar_cfg)
            y1_energy, y1_demand_savings, y1_benefit = settlement["year1_energy_kwh"], settlement["year1_demand_savings"], settlement["year1_gross_benefit"]
            sc_warnings = list(settlement["warnings"])
        elif sc.get("annual_yield_kwh") is not None:
            y1_energy = _finite(sc["annual_yield_kwh"], f"scenarios[{index}].annual_yield_kwh", minimum=0.0)
            sc_ratio = tar_cfg["self_consumption_ratio"] if (tar_cfg["mode"] != "feed_in" and tar_cfg["self_consumption_ratio"] is not None) else 0.0
            y1_benefit = y1_energy * (sc_ratio * tar_cfg["import_tariff"] + (1.0 - sc_ratio) * tar_cfg["feed_in_tariff"])
            sc_warnings = []
            if tar_cfg["mode"] == "tou":
                sc_warnings.append("annual_yield_kwh summary input has no hourly profile; valued using flat tariff fallback.")
            if tar_cfg.get("demand_charge") is not None:
                sc_warnings.append("annual_yield_kwh summary input has no hourly profile; demand_charge was skipped.")
        else:
            raise ValueError(f"scenarios[{index}] ({label}) must provide csv_name, csv_names, csv_path, or annual_yield_kwh")

        metrics = _compute_cashflow_metrics(
            year1_energy_kwh=y1_energy, year1_gross_benefit=y1_benefit, year1_demand_savings=y1_demand_savings,
            capex_total=capex, base_annual_opex=opex, replacement_events=sc_replacements, financial=fin_cfg, dc_kw=dc_kw,
        )
        sens_result = _compute_sensitivity(
            sens_cfg=sens_cfg, base_metrics=metrics, year1_energy_kwh=y1_energy,
            year1_energy_benefit=y1_benefit - y1_demand_savings, year1_demand_savings=y1_demand_savings,
            capex_total=capex, base_annual_opex=opex, replacement_events=sc_replacements, financial=fin_cfg, dc_kw=dc_kw,
        ) if sens_cfg is not None else None
        if not include_cashflow_schedule:
            metrics.pop("cashflow_schedule", None)

        entry: dict[str, Any] = {
            "label": label, "installed_dc_kw": dc_kw, "battery_kwh": bat_kwh if bat_kwh else None,
            "specific_yield_kwh_per_kwp": round(y1_energy / dc_kw, 2) if (dc_kw and dc_kw > 0) else None,
            "year1_energy_kwh": round(y1_energy, 2), "financials": metrics, "warnings": sc_warnings,
        }
        if sens_result is not None:
            entry["sensitivity"] = sens_result
        if sc.get("batch_parameters"):
            entry["batch_parameters"] = sc["batch_parameters"]
        if settlement is not None:
            entry["settlement"] = {
                **{k: settlement[k] for k in ("rows", "step_minutes", "has_load", "has_storage", "energy_summary", "demand_charge_summary", "tou_breakdown", "monthly_summary", "typical_day_profile")},
                **({"merged_sources": settlement["merged_sources"]} if settlement.get("merged_sources") else {}),
            }
        results.append(entry)

    lower_is_better = rank_by in ("lcoe", "simple_payback_years", "discounted_payback_years")
    metric_key = {"irr": "irr_pct", "equity_irr": "equity_irr_pct"}.get(rank_by, rank_by)

    def _sort_key(item: dict) -> tuple[int, float]:
        val = item["financials"].get(metric_key)
        return (1, 0.0) if (val is None or not math.isfinite(val)) else (0, float(val) if lower_is_better else -float(val))

    ranked = sorted(results, key=_sort_key)
    ranking_table = [
        {"rank": i, "label": item["label"], "year1_energy_kwh": item["year1_energy_kwh"],
         **{k: item["financials"][k] for k in ("capex_total", "lcoe", "npv", "irr_pct", "simple_payback_years", "discounted_payback_years", "year1_net_benefit")}}
        for i, item in enumerate(ranked, start=1)
    ]
    return {
        "status": "ok", "currency": fin_cfg["currency"], "project_life_years": fin_cfg["project_life_years"],
        "discount_rate_pct": fin_cfg["discount_rate_pct"], "tariff_mode": tar_cfg["mode"],
        "rank_by": rank_by, "best_scenario": ranked[0]["label"] if _sort_key(ranked[0])[0] == 0 else None,
        "scenario_count": len(results), "ranking": ranking_table, "scenarios": results,
    }


def evaluate_standalone_economics(
    energy_balance: dict[str, float], *, capex_total: float, annual_om_opex: float = 0.0,
    fuel_price_per_liter: float = 1.50, genset_opex_per_hour: float = 0.0,
    genset_fuel_rate_l_per_kwh: float | None = None, avoided_tariff_per_kwh: float = 0.0,
    financial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate lifetime LCOE, diesel fuel OPEX, and cashflows for Stand-Alone PV+BESS(+Genset) systems."""
    fin_cfg = _validate_financial(financial)
    replacement_events = list(fin_cfg["replacement_events"])
    if fin_cfg["battery_replacement_year"] is not None and fin_cfg["battery_replacement_cost"] is None:
        raise ValueError("financial.battery_replacement_cost is required for a stand-alone battery replacement year")
    if (rep := _battery_replacement_event(fin_cfg, battery_kwh=0.0)) is not None:
        replacement_events.append(rep)
    capex = _finite(capex_total, "capex_total", minimum=0.0)
    om_opex = _finite(annual_om_opex, "annual_om_opex", minimum=0.0)
    fuel_price = _finite(fuel_price_per_liter, "fuel_price_per_liter", minimum=0.0)
    gen_opex_h = _finite(genset_opex_per_hour, "genset_opex_per_hour", minimum=0.0)
    avoided_rate = _finite(avoided_tariff_per_kwh, "avoided_tariff_per_kwh", minimum=0.0)

    e_user = max(0.0, float(energy_balance.get("e_user_kwh", 0.0)))
    e_bkup = max(0.0, float(energy_balance.get("e_backup_kwh", 0.0)))
    bkup_h = max(0.0, float(energy_balance.get("backup_runtime_hours", 0.0)))
    fuel_l = max(0.0, float(energy_balance.get("backup_fuel_liters", 0.0)))
    if fuel_l <= 0.0 and e_bkup > 0.0 and genset_fuel_rate_l_per_kwh is not None:
        fuel_l = e_bkup * _finite(genset_fuel_rate_l_per_kwh, "genset_fuel_rate_l_per_kwh", minimum=0.0)

    annual_fuel_cost, annual_genset_om = fuel_l * fuel_price, bkup_h * gen_opex_h
    base_annual_opex = om_opex + annual_fuel_cost + annual_genset_om
    metrics = _compute_cashflow_metrics(
        year1_energy_kwh=e_user, year1_gross_benefit=e_user * avoided_rate,
        capex_total=capex, base_annual_opex=base_annual_opex,
        replacement_events=replacement_events, financial=fin_cfg,
    )
    return {
        "currency": fin_cfg["currency"], "project_life_years": fin_cfg["project_life_years"],
        "capex_total": round(capex, 2), "annual_om_opex": round(om_opex, 2),
        "annual_fuel_liters": round(fuel_l, 2), "annual_fuel_cost": round(annual_fuel_cost, 2),
        "annual_genset_maintenance_cost": round(annual_genset_om, 2), "total_year1_opex": round(base_annual_opex, 2),
        **{k: metrics[k] for k in ("lcoe", "npv", "irr_pct", "simple_payback_years", "discounted_payback_years", "lifetime_energy_kwh", "lifetime_opex")},
    }

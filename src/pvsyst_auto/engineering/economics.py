"""Deterministic PV & PV+BESS financial, TOU tariff, LCOE, NPV, and IRR evaluation engine.

Pure Python standard library implementation. Streams hourly or sub-hourly PVsyst SFI CSVs
(5/10/15/30/60 min) or batch summary CSVs to compute auditable year-1 energy/TOU settlements,
25-year degraded cashflows, LCOE, NPV, IRR, simple/discounted payback, and multi-scenario rankings.
"""
from __future__ import annotations

import itertools
import math
from pathlib import Path
from typing import Any

from pvsyst_auto.native.cli import parse_batch_results
from pvsyst_auto.project.paths import safe_filename, workspace_file
from pvsyst_auto.results import BATCH_ENERGY_TO_KWH, RegularResultCsv, energy_unit_scale
from pvsyst_auto.validation import finite_number as _finite

_VALID_RANK_KEYS = (
    "npv", "irr", "equity_irr", "lcoe",
    "simple_payback_years", "discounted_payback_years", "year1_net_benefit",
)
_VALID_SENSITIVITY_FACTORS = ("tariff", "capex", "yield", "degradation")


def _validate_vat(raw_vat: dict | None) -> dict | None:
    if raw_vat is None:
        return None
    if not isinstance(raw_vat, dict):
        raise ValueError("financial.vat must be an object")
    rate_pct = _finite(raw_vat.get("rate_pct", 0.0), "financial.vat.rate_pct", minimum=0.0, maximum=50.0)
    includes_vat = raw_vat.get("tariff_includes_vat", True)
    if type(includes_vat) is not bool:
        raise ValueError("financial.vat.tariff_includes_vat must be a strict boolean")
    input_credit = (
        _finite(raw_vat["input_vat_credit"], "financial.vat.input_vat_credit", minimum=0.0)
        if raw_vat.get("input_vat_credit") is not None else None
    )
    ded_share = (
        _finite(raw_vat["deductible_capex_share"], "financial.vat.deductible_capex_share", minimum=0.0, maximum=1.0)
        if raw_vat.get("deductible_capex_share") is not None else None
    )
    if input_credit is not None and ded_share is not None:
        raise ValueError("financial.vat: specify either input_vat_credit or deductible_capex_share, not both")
    surtax_pct = _finite(raw_vat.get("surtax_pct", 0.0), "financial.vat.surtax_pct", minimum=0.0, maximum=50.0)
    opex_vat_pct = _finite(raw_vat.get("opex_input_vat_pct", 0.0), "financial.vat.opex_input_vat_pct", minimum=0.0, maximum=50.0)
    return {
        "rate_pct": rate_pct,
        "tariff_includes_vat": includes_vat,
        "input_vat_credit": input_credit,
        "deductible_capex_share": ded_share,
        "surtax_pct": surtax_pct,
        "opex_input_vat_pct": opex_vat_pct,
    }


def _validate_cit(raw_cit: dict | None, life: int) -> dict | None:
    if raw_cit is None:
        return None
    if not isinstance(raw_cit, dict):
        raise ValueError("financial.cit must be an object")
    rate_pct = _finite(raw_cit.get("rate_pct", 0.0), "financial.cit.rate_pct", minimum=0.0, maximum=60.0)
    exempt_years = raw_cit.get("exempt_years", 0)
    if type(exempt_years) is not int or not 0 <= exempt_years <= 30:
        raise ValueError("financial.cit.exempt_years must be an integer in 0..30")
    half_rate_years = raw_cit.get("half_rate_years", 0)
    if type(half_rate_years) is not int or not 0 <= half_rate_years <= 30:
        raise ValueError("financial.cit.half_rate_years must be an integer in 0..30")
    dep_years = raw_cit.get("depreciation_years")
    if dep_years is None:
        dep_years = life
    elif type(dep_years) is not int or not 1 <= dep_years <= 50:
        raise ValueError("financial.cit.depreciation_years must be an integer in 1..50")
    residual_pct = _finite(raw_cit.get("residual_value_pct", 0.0), "financial.cit.residual_value_pct", minimum=0.0, maximum=50.0)
    carry_years = raw_cit.get("loss_carryforward_years", 5)
    if type(carry_years) is not int or not 0 <= carry_years <= 50:
        raise ValueError("financial.cit.loss_carryforward_years must be an integer in 0..50")
    return {
        "rate_pct": rate_pct,
        "exempt_years": exempt_years,
        "half_rate_years": half_rate_years,
        "depreciation_years": dep_years,
        "residual_value_pct": residual_pct,
        "loss_carryforward_years": carry_years,
    }


def _validate_financing(raw_fin: dict | None) -> dict | None:
    if raw_fin is None:
        return None
    if not isinstance(raw_fin, dict):
        raise ValueError("financial.financing must be an object")
    equity_ratio = _finite(raw_fin.get("equity_ratio_pct", 100.0), "financial.financing.equity_ratio_pct", minimum=1.0, maximum=100.0)
    tenor = raw_fin.get("loan_tenor_years", 15)
    if type(tenor) is not int or not 1 <= tenor <= 50:
        raise ValueError("financial.financing.loan_tenor_years must be an integer in 1..50")
    grace = raw_fin.get("loan_grace_years", 0)
    if type(grace) is not int or not 0 <= grace <= 10:
        raise ValueError("financial.financing.loan_grace_years must be an integer in 0..10")
    if grace >= tenor:
        raise ValueError("financial.financing.loan_grace_years must be strictly less than loan_tenor_years")
    interest = _finite(raw_fin.get("loan_interest_rate_pct", 0.0), "financial.financing.loan_interest_rate_pct", minimum=0.0, maximum=30.0)
    method = raw_fin.get("repayment_method", "equal_principal")
    if method not in ("equal_principal", "equal_payment"):
        raise ValueError("financial.financing.repayment_method must be 'equal_principal' or 'equal_payment'")
    eq_disc = (
        _finite(raw_fin["equity_discount_rate_pct"], "financial.financing.equity_discount_rate_pct", minimum=0.0, maximum=50.0)
        if raw_fin.get("equity_discount_rate_pct") is not None else None
    )
    return {
        "equity_ratio_pct": equity_ratio,
        "loan_tenor_years": tenor,
        "loan_grace_years": grace,
        "loan_interest_rate_pct": interest,
        "repayment_method": method,
        "equity_discount_rate_pct": eq_disc,
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
        sy = raw.get("start_year")
        ey = raw.get("end_year")
        if type(sy) is not int or not 1 <= sy <= life:
            raise ValueError(f"financial.opex_tiers[{idx}].start_year must be an integer in 1..{life}")
        if type(ey) is not int or not sy <= ey <= life:
            raise ValueError(f"financial.opex_tiers[{idx}].end_year must be an integer in {sy}..{life}")
        ann = _finite(raw["annual_opex"], f"financial.opex_tiers[{idx}].annual_opex", minimum=0.0) if raw.get("annual_opex") is not None else None
        per_kw = _finite(raw["opex_per_kw_year"], f"financial.opex_tiers[{idx}].opex_per_kw_year", minimum=0.0) if raw.get("opex_per_kw_year") is not None else None
        pct_cap = _finite(raw["opex_pct_capex"], f"financial.opex_tiers[{idx}].opex_pct_capex", minimum=0.0, maximum=50.0) if raw.get("opex_pct_capex") is not None else None
        extra = _finite(raw["extra_annual_opex"], f"financial.opex_tiers[{idx}].extra_annual_opex", minimum=0.0) if raw.get("extra_annual_opex") is not None else None
        if ann is None and per_kw is None and pct_cap is None and extra is None:
            raise ValueError(f"financial.opex_tiers[{idx}] must set annual_opex, opex_per_kw_year, opex_pct_capex, or extra_annual_opex")
        escalate = raw.get("escalate", False)
        if type(escalate) is not bool:
            raise ValueError(f"financial.opex_tiers[{idx}].escalate must be a strict boolean")
        is_override = any(v is not None for v in (ann, per_kw, pct_cap))
        if is_override:
            span = set(range(sy, ey + 1))
            if span & override_years:
                raise ValueError(f"financial.opex_tiers[{idx}] base-OPEX year span {sy}..{ey} overlaps an earlier base-OPEX tier")
            override_years |= span
        tiers.append({
            "start_year": sy,
            "end_year": ey,
            "annual_opex": ann,
            "opex_per_kw_year": per_kw,
            "opex_pct_capex": pct_cap,
            "extra_annual_opex": extra,
            "escalate": escalate,
            "label": str(raw.get("label") or f"tier_{idx + 1}").strip(),
        })
    return tiers


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
    upfront_opex = _finite(raw.get("upfront_opex", 0.0), "financial.upfront_opex", minimum=0.0)
    opex_tiers = _validate_opex_tiers(raw.get("opex_tiers"), life)
    vat_cfg = _validate_vat(raw.get("vat"))
    cit_cfg = _validate_cit(raw.get("cit"), life)
    financing_cfg = _validate_financing(raw.get("financing"))

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
        "upfront_opex": upfront_opex,
        "opex_tiers": opex_tiers,
        "vat": vat_cfg,
        "cit": cit_cfg,
        "financing": financing_cfg,
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


def _resolve_system_capex(
    fin_cfg: dict,
    *,
    dc_kw: float | None,
    battery_kwh: float = 0.0,
    pcs_kw: float = 0.0,
    pcs_capex_per_kw: float = 0.0,
    context: str = "",
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
    fin_cfg: dict,
    *,
    battery_kwh: float,
    is_pv_only: bool = False,
    require_cost_basis: bool = False,
    context: str = "",
    label: str | None = None,
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
        ratio = (
            fin_cfg["battery_replacement_cost_ratio"]
            if fin_cfg["battery_replacement_cost_ratio"] is not None else 0.7
        )
        cost = battery_kwh * fin_cfg["battery_capex_per_kwh"] * ratio
    else:
        return None
    if cost <= 0:
        return None
    return {
        "year": year,
        "cost": cost,
        "label": label or f"battery_replacement_year_{year}",
    }


def _settle_csv_year1(csv_path: Path, tariff: dict) -> dict:
    """Single-pass streaming settlement over an hourly or sub-hourly PVsyst SFI CSV."""
    return _settle_multi_csv_year1([csv_path], [1.0], tariff)


def _settle_multi_csv_year1(csv_paths: list[Path], weights: list[float], tariff: dict) -> dict:
    """Single-pass streaming settlement over one or more timestamp-aligned SFI CSVs."""
    if not csv_paths or len(csv_paths) != len(weights):
        raise ValueError("csv_paths and weights must be non-empty lists of identical length")
    readers = [
        RegularResultCsv(
            p,
            empty_message=f"Result CSV {p.name} contains no simulation rows",
            irregular_message=(
                f"Result CSV {p.name} timestamps do not have a regular cadence (missing, duplicate, or out-of-order rows); "
                "cannot settle energy or tariffs reliably"
            ),
            required_any_of=("EToGrid", "E_Grid"),
            missing_required_message=f"Result CSV {p.name} must include E_Grid or EToGrid for financial evaluation",
        )
        for p in csv_paths
    ]

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

    per_meta: list[dict[str, Any]] = []
    has_load = False
    has_storage = False
    sentinel = object()
    iterators = [r.iter_rows() for r in readers]

    for row_idx, items in enumerate(itertools.zip_longest(*iterators, fillvalue=sentinel)):
        if any(item is sentinel for item in items):
            raise ValueError(
                "Merged result CSVs have different row counts; all portfolio CSVs must span identical timestamps"
            )
        stamp, _, dt_hours = items[0]
        for k, (st_k, _, dt_k) in enumerate(items[1:], start=1):
            if st_k != stamp or abs(dt_k - dt_hours) > 1e-6:
                raise ValueError(
                    f"Merged result CSV {csv_paths[k].name} timestamp mismatch at row {row_idx + 1}: "
                    f"expected {stamp.isoformat()} (dt={dt_hours:g}h), got {st_k.isoformat()} (dt={dt_k:g}h)"
                )

        if not per_meta:
            for r in readers:
                idx = r.idx
                per_meta.append({
                    "reader": r,
                    "idx": idx,
                    "export_col": "EToGrid" if "EToGrid" in idx else "E_Grid",
                    "has_load": "E_User" in idx,
                    "has_import": "EFrGrid" in idx,
                    "has_storage": ("EBatCh" in idx or "EBatDis" in idx),
                    "col_scale": {col: energy_unit_scale(r.units.get(col, "")) for col in idx},
                })
            has_load = any(m["has_load"] for m in per_meta)
            has_storage = any(m["has_storage"] for m in per_meta)

        self_cons = inferred_self_cons = 0.0
        e_exp_sum = 0.0
        e_user_sum = 0.0
        e_imp_sum = 0.0
        e_bat_ch_sum = 0.0
        e_bat_dis_sum = 0.0

        for meta, w, (_, row, dt_k) in zip(per_meta, weights, items):
            idx = meta["idx"]
            col_scale = meta["col_scale"]
            reader_k = meta["reader"]

            def kwh(col: str) -> float:
                col_i = idx.get(col)
                if col_i is None:
                    return 0.0
                raw = row[col_i]
                val = float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else 0.0
                factor, needs_dt = col_scale[col]
                if factor == 0.0:
                    raise ValueError(f"{col} requires a power or energy unit, got {reader_k.units.get(col, '')!r}")
                return val * factor * (dt_k if needs_dt else 1.0)

            raw_eg = kwh(meta["export_col"])
            e_exp_sum += w * max(0.0, raw_eg)
            e_user_k = max(0.0, kwh("E_User")) if meta["has_load"] else 0.0
            e_user_sum += w * e_user_k
            if meta["has_import"]:
                e_imp_k = max(0.0, kwh("EFrGrid"))
            elif meta["has_load"] and "E_Solar" in idx:
                e_imp_k = max(0.0, e_user_k - max(0.0, kwh("E_Solar")))
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
                e_exp_sum -= inferred
            e_imp_sum += w * e_imp_k
            if "EBatCh" in idx:
                e_bat_ch_sum += w * max(0.0, kwh("EBatCh"))
            if "EBatDis" in idx:
                e_bat_dis_sum += w * max(0.0, kwh("EBatDis"))

        p_name, imp_rate, exp_rate = _lookup_rate(stamp, tariff)
        e_exp = e_exp_sum
        e_user = e_user_sum
        e_imp = e_imp_sum
        e_bat_ch = e_bat_ch_sum
        e_bat_dis = e_bat_dis_sum

        baseline_bill = (e_user + inferred_self_cons) * imp_rate
        import_cost = e_imp * imp_rate
        export_rev = e_exp * exp_rate
        avoided = baseline_bill - import_cost

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

    rows = readers[0].rows_count
    eff_step_min = readers[0].step_minutes if readers[0].step_minutes is not None else 60.0
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

    merged_sources = (
        [{"csv_name": p.name, "weight": round(w, 6)} for p, w in zip(csv_paths, weights)]
        if len(csv_paths) > 1 else None
    )
    return {
        "rows": rows,
        "step_minutes": eff_step_min,
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
        **({"merged_sources": merged_sources} if merged_sources is not None else {}),
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
        if v == 0:
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
        if f_mid == 0 or abs(hi - lo) < 1e-10:
            return mid
        if (f_lo > 0 and f_mid > 0) or (f_lo < 0 and f_mid < 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    return 0.5 * (lo + hi)


def _apply_loss_carryforward(ebt: float, year: int, carry_years: int, losses: list[list[float]]) -> float:
    """Apply FIFO operating tax-loss carryforward and return taxable income (>= 0)."""
    if carry_years <= 0:
        losses.clear()
        return max(0.0, ebt)
    # Expire losses older than carry_years
    while losses and (year - int(losses[0][0])) > carry_years:
        losses.pop(0)
    if ebt < 0.0:
        losses.append([float(year), -ebt])
        return 0.0
    remaining = ebt
    while remaining > 0.0 and losses:
        avail = losses[0][1]
        used = min(avail, remaining)
        remaining -= used
        losses[0][1] -= used
        if losses[0][1] <= 1e-9:
            losses.pop(0)
    return max(0.0, remaining)


def _resolve_year_opex(
    year: int,
    *,
    base_annual_opex: float,
    opex_esc: float,
    opex_tiers: list[dict],
    capex_total: float,
    dc_kw: float | None,
) -> float:
    """Resolve annual OPEX for a given operating year (1..life) with optional staged tiers."""
    esc_factor = (1.0 + opex_esc) ** (year - 1)
    base_override: float | None = None
    extra_sum = 0.0
    for tier in opex_tiers:
        if not (tier["start_year"] <= year <= tier["end_year"]):
            continue
        tier_esc = esc_factor if tier["escalate"] else 1.0
        if (tier["annual_opex"] is not None or tier["opex_per_kw_year"] is not None
                or tier["opex_pct_capex"] is not None):
            amt = float(tier["annual_opex"] or 0.0)
            if tier["opex_per_kw_year"] is not None and tier["opex_per_kw_year"] > 0:
                if dc_kw is None:
                    raise ValueError(
                        f"installed_dc_kw is required when financial.opex_tiers ({tier['label']}) sets opex_per_kw_year > 0"
                    )
                amt += dc_kw * tier["opex_per_kw_year"]
            if tier["opex_pct_capex"] is not None and tier["opex_pct_capex"] > 0:
                amt += capex_total * (tier["opex_pct_capex"] / 100.0)
            base_override = amt * tier_esc
        if tier["extra_annual_opex"] is not None:
            extra_sum += tier["extra_annual_opex"] * tier_esc
    base_y = base_override if base_override is not None else (base_annual_opex * esc_factor)
    return base_y + extra_sum


def _compute_cashflow_metrics(
    *,
    year1_energy_kwh: float,
    year1_gross_benefit: float,
    capex_total: float,
    base_annual_opex: float,
    replacement_events: list[dict],
    financial: dict,
    year1_demand_savings: float = 0.0,
    dc_kw: float | None = None,
) -> dict:
    life = financial["project_life_years"]
    r = financial["discount_rate_pct"] / 100.0
    deg1 = financial["first_year_degradation_pct"] / 100.0
    deg_ann = financial["annual_degradation_pct"] / 100.0
    opex_esc = financial["opex_escalation_pct"] / 100.0
    tariff_esc = financial["tariff_escalation_pct"] / 100.0
    salvage_pct = financial["salvage_value_pct"] / 100.0
    upfront_opex = float(financial.get("upfront_opex") or 0.0)
    opex_tiers = financial.get("opex_tiers") or []
    vat_cfg = financial.get("vat")
    cit_cfg = financial.get("cit")
    fin_loan = financial.get("financing")
    year1_energy_benefit = year1_gross_benefit - year1_demand_savings

    rep_by_year: dict[int, float] = {}
    for ev in replacement_events:
        rep_by_year[ev["year"]] = rep_by_year.get(ev["year"], 0.0) + ev["cost"]

    # VAT setup
    has_vat = vat_cfg is not None
    vat_r = (vat_cfg["rate_pct"] / 100.0) if has_vat else 0.0
    vat_surtax_r = (vat_cfg["surtax_pct"] / 100.0) if has_vat else 0.0
    opex_vat_r = (vat_cfg["opex_input_vat_pct"] / 100.0) if has_vat else 0.0
    if has_vat:
        if vat_cfg["input_vat_credit"] is not None:
            initial_input_vat_credit = float(vat_cfg["input_vat_credit"])
        elif vat_cfg["deductible_capex_share"] is not None and vat_r > 0.0:
            initial_input_vat_credit = capex_total * vat_cfg["deductible_capex_share"] * (vat_r / (1.0 + vat_r))
        else:
            initial_input_vat_credit = 0.0
    else:
        initial_input_vat_credit = 0.0
    vat_credit_rem = initial_input_vat_credit
    vat_credit_exhausted_year: int | None = None

    # CIT setup
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

    # Financing (debt / equity) setup
    has_financing = fin_loan is not None
    if has_financing:
        eq_ratio = fin_loan["equity_ratio_pct"] / 100.0
        debt_principal_init = capex_total * (1.0 - eq_ratio)
        equity_investment = capex_total * eq_ratio + upfront_opex
        loan_tenor = fin_loan["loan_tenor_years"]
        loan_grace = fin_loan["loan_grace_years"]
        loan_r = fin_loan["loan_interest_rate_pct"] / 100.0
        repay_years = loan_tenor - loan_grace
        repay_method = fin_loan["repayment_method"]
        eq_disc_r = (
            (fin_loan["equity_discount_rate_pct"] / 100.0)
            if fin_loan["equity_discount_rate_pct"] is not None else r
        )
        if repay_method == "equal_payment" and debt_principal_init > 0:
            if loan_r > 0:
                level_annuity = debt_principal_init * loan_r / (1.0 - ((1.0 + loan_r) ** (-repay_years)))
            else:
                level_annuity = debt_principal_init / repay_years
        else:
            level_annuity = 0.0
        debt_balance = debt_principal_init
    else:
        debt_principal_init = 0.0
        equity_investment = 0.0
        loan_tenor = 0
        loan_grace = 0
        loan_r = 0.0
        repay_years = 1
        repay_method = "equal_principal"
        eq_disc_r = r
        level_annuity = 0.0
        debt_balance = 0.0

    initial_outflow = capex_total + upfront_opex
    pre_tax_cfs: list[float] = [-initial_outflow]
    post_tax_cfs: list[float] = [-initial_outflow]
    equity_cfs: list[float] = [-equity_investment] if has_financing else []

    schedule: list[dict] = []
    cum_nom = -initial_outflow
    cum_disc = -initial_outflow
    simple_payback: float | None = 0.0 if initial_outflow == 0 else None
    discounted_payback: float | None = 0.0 if initial_outflow == 0 else None
    eq_cum_nom = -equity_investment
    eq_simple_payback: float | None = (0.0 if equity_investment == 0 else None) if has_financing else None

    pv_costs = initial_outflow
    pv_energy = 0.0
    lifetime_energy = 0.0
    lifetime_revenue = 0.0
    lifetime_opex = 0.0
    lifetime_vat_payable = 0.0
    lifetime_vat_surtax = 0.0
    lifetime_cit_project = 0.0
    lifetime_cit_equity = 0.0
    lifetime_interest = 0.0
    dscr_values: list[float] = []

    for year in range(1, life + 1):
        deg_factor = (1.0 - deg1) * ((1.0 - deg_ann) ** (year - 1))
        energy_y = year1_energy_kwh * deg_factor
        gross_y = (year1_energy_benefit * deg_factor + year1_demand_savings) * ((1.0 + tariff_esc) ** (year - 1))
        opex_y = _resolve_year_opex(
            year,
            base_annual_opex=base_annual_opex,
            opex_esc=opex_esc,
            opex_tiers=opex_tiers,
            capex_total=capex_total,
            dc_kw=dc_kw,
        )
        rep_y = rep_by_year.get(year, 0.0)
        salvage_y = capex_total * salvage_pct if year == life else 0.0

        # VAT layer
        if has_vat:
            if vat_cfg["tariff_includes_vat"]:
                rev_ex_vat = gross_y / (1.0 + vat_r)
                output_vat = gross_y - rev_ex_vat
            else:
                rev_ex_vat = gross_y
                output_vat = gross_y * vat_r
            opex_input_vat = opex_y * (opex_vat_r / (1.0 + opex_vat_r)) if opex_vat_r > 0.0 else 0.0
            opex_ex_vat = opex_y - opex_input_vat
            net_vat_before = output_vat - opex_input_vat
            if net_vat_before > 0.0:
                credit_used = min(vat_credit_rem, net_vat_before)
                prev_credit = vat_credit_rem
                vat_credit_rem = max(0.0, vat_credit_rem - credit_used)
                if vat_credit_exhausted_year is None and prev_credit > 1e-6 and vat_credit_rem <= 1e-6:
                    vat_credit_exhausted_year = year
                vat_payable = net_vat_before - credit_used
            else:
                vat_credit_rem += (-net_vat_before)
                vat_payable = 0.0
            vat_surtax = vat_payable * vat_surtax_r
            cash_in_y = rev_ex_vat + output_vat
        else:
            rev_ex_vat = gross_y
            opex_ex_vat = opex_y
            vat_payable = 0.0
            vat_surtax = 0.0
            cash_in_y = gross_y

        # Debt service layer
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
            interest_y = 0.0
            principal_y = 0.0
            debt_service_y = 0.0

        # CIT layer
        dep_y = annual_dep if (has_cit and year <= dep_years) else 0.0
        if has_cit:
            if year <= cit_exempt:
                eff_cit_r = 0.0
            elif year <= cit_exempt + cit_half:
                eff_cit_r = 0.5 * cit_r
            else:
                eff_cit_r = cit_r
            ebit_proj = rev_ex_vat - opex_ex_vat - vat_surtax - dep_y
            taxable_proj = _apply_loss_carryforward(ebit_proj, year, carry_years, proj_losses)
            cit_proj_y = taxable_proj * eff_cit_r
            if has_financing:
                ebt_eq = ebit_proj - interest_y
                taxable_eq = _apply_loss_carryforward(ebt_eq, year, carry_years, eq_losses)
                cit_eq_y = taxable_eq * eff_cit_r
            else:
                cit_eq_y = cit_proj_y
        else:
            cit_proj_y = 0.0
            cit_eq_y = 0.0

        pre_tax_y = cash_in_y - opex_y - vat_payable - vat_surtax - rep_y + salvage_y
        post_tax_y = pre_tax_y - cit_proj_y
        net_y = post_tax_y if has_cit else pre_tax_y
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
        lifetime_vat_payable += vat_payable
        lifetime_vat_surtax += vat_surtax
        lifetime_cit_project += cit_proj_y
        lifetime_cit_equity += cit_eq_y
        lifetime_interest += interest_y

        pre_tax_cfs.append(pre_tax_y)
        post_tax_cfs.append(post_tax_y)

        row_item: dict[str, Any] = {
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
        }
        if has_vat:
            row_item.update({
                "revenue_ex_vat": round(rev_ex_vat, 2),
                "vat_payable": round(vat_payable, 2),
                "vat_surtax": round(vat_surtax, 2),
                "vat_credit_remaining": round(vat_credit_rem, 2),
            })
        if has_cit:
            row_item.update({
                "depreciation": round(dep_y, 2),
                "cit_project": round(cit_proj_y, 2),
                "pre_tax_project_cashflow": round(pre_tax_y, 2),
                "post_tax_project_cashflow": round(post_tax_y, 2),
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
                "debt_interest": round(interest_y, 2),
                "debt_principal": round(principal_y, 2),
                "debt_service": round(debt_service_y, 2),
                "dscr": round(dscr_y, 3) if dscr_y is not None else None,
                "equity_cashflow": round(eq_cf_y, 2),
            })
        schedule.append(row_item)

    primary_cfs = post_tax_cfs if has_cit else pre_tax_cfs
    npv = sum(cf / ((1.0 + r) ** t) for t, cf in enumerate(primary_cfs))
    irr = _compute_irr(primary_cfs)
    lcoe = (pv_costs / pv_energy) if pv_energy > 0 else None

    year1_energy_benefit_deg = year1_energy_benefit * (1.0 - deg1)
    year1_gross_deg = year1_energy_benefit_deg + year1_demand_savings
    year1_opex_actual = schedule[0]["opex"] if schedule else round(base_annual_opex, 2)
    out: dict[str, Any] = {
        "currency": financial["currency"],
        "capex_total": round(capex_total, 2),
        "year1_opex": round(year1_opex_actual, 2),
        "year1_energy_benefit": round(year1_energy_benefit_deg, 2),
        "year1_demand_savings": round(year1_demand_savings, 2),
        "year1_gross_benefit": round(year1_gross_deg, 2),
        "year1_net_benefit": round(year1_gross_deg - year1_opex_actual, 2),
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
    if upfront_opex > 0:
        out["upfront_opex"] = round(upfront_opex, 2)
    if has_vat or has_cit:
        pre_npv = sum(cf / ((1.0 + r) ** t) for t, cf in enumerate(pre_tax_cfs))
        pre_irr = _compute_irr(pre_tax_cfs)
        post_npv = sum(cf / ((1.0 + r) ** t) for t, cf in enumerate(post_tax_cfs))
        post_irr = _compute_irr(post_tax_cfs)
        out["pre_tax_project_npv"] = round(pre_npv, 2)
        out["pre_tax_project_irr_pct"] = round(pre_irr * 100.0, 3) if pre_irr is not None else None
        out["post_tax_project_npv"] = round(post_npv, 2)
        out["post_tax_project_irr_pct"] = round(post_irr * 100.0, 3) if post_irr is not None else None
        out["tax_summary"] = {
            "initial_input_vat_credit": round(initial_input_vat_credit, 2),
            "vat_credit_exhausted_year": vat_credit_exhausted_year,
            "vat_credit_remaining_end": round(vat_credit_rem, 2),
            "lifetime_vat_payable": round(lifetime_vat_payable, 2),
            "lifetime_vat_surtax": round(lifetime_vat_surtax, 2),
            "depreciable_base": round(depreciable_base, 2),
            "lifetime_cit_project": round(lifetime_cit_project, 2),
        }
    if has_financing:
        eq_npv = sum(cf / ((1.0 + eq_disc_r) ** t) for t, cf in enumerate(equity_cfs))
        eq_irr = _compute_irr(equity_cfs)
        out["equity_irr_pct"] = round(eq_irr * 100.0, 3) if eq_irr is not None else None
        out["financing_summary"] = {
            "equity_ratio_pct": fin_loan["equity_ratio_pct"],
            "equity_investment": round(equity_investment, 2),
            "debt_principal": round(debt_principal_init, 2),
            "loan_tenor_years": loan_tenor,
            "loan_grace_years": loan_grace,
            "loan_interest_rate_pct": fin_loan["loan_interest_rate_pct"],
            "repayment_method": repay_method,
            "equity_discount_rate_pct": round(eq_disc_r * 100.0, 3),
            "equity_npv": round(eq_npv, 2),
            "equity_irr_pct": round(eq_irr * 100.0, 3) if eq_irr is not None else None,
            "equity_simple_payback_years": round(eq_simple_payback, 2) if eq_simple_payback is not None else None,
            "min_dscr": round(min(dscr_values), 3) if dscr_values else None,
            "avg_dscr": round(sum(dscr_values) / len(dscr_values), 3) if dscr_values else None,
            "lifetime_interest": round(lifetime_interest, 2),
            "lifetime_cit_equity": round(lifetime_cit_equity, 2),
        }
    return out


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
    dc_kw: float | None = None,
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
            dc_kw=dc_kw,
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
            capex = _resolve_system_capex(
                fin_cfg,
                dc_kw=dc_kw,
                battery_kwh=bat_kwh,
                context=f"scenarios[{index}] ({label})",
            )

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
        bat_rep_ev = _battery_replacement_event(
            fin_cfg,
            battery_kwh=bat_kwh,
            is_pv_only=(bat_kwh == 0.0) and (sc.get("battery_kwh") is not None or any_has_battery),
            require_cost_basis=True,
            context=f"scenarios[{index}] ({label})",
        )
        if bat_rep_ev is not None:
            sc_replacements.append(bat_rep_ev)
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
        has_single_csv = bool(sc.get("csv_name") or sc.get("csv_path"))
        has_multi_csv = bool(sc.get("csv_names") or sc.get("csv_paths"))
        if has_single_csv and has_multi_csv:
            raise ValueError(
                f"scenarios[{index}] ({label}): specify either csv_name/csv_path or csv_names/csv_paths, not both"
            )
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
                    raise ValueError(
                        f"scenarios[{index}].csv_weights length must match csv_names ({len(resolved_paths)})"
                    )
                weights = [
                    _finite(w, f"scenarios[{index}].csv_weights[{wi}]", minimum=1e-9)
                    for wi, w in enumerate(raw_weights)
                ]
            settlement = _settle_multi_csv_year1(resolved_paths, weights, tar_cfg)
            y1_energy = settlement["year1_energy_kwh"]
            y1_demand_savings = settlement["year1_demand_savings"]
            y1_benefit = settlement["year1_gross_benefit"]
            sc_warnings = list(settlement["warnings"])
        elif has_single_csv:
            if sc.get("csv_weights") is not None:
                raise ValueError(f"scenarios[{index}] ({label}): csv_weights requires csv_names")
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
            raise ValueError(f"scenarios[{index}] ({label}) must provide csv_name, csv_names, csv_path, or annual_yield_kwh")

        metrics = _compute_cashflow_metrics(
            year1_energy_kwh=y1_energy,
            year1_gross_benefit=y1_benefit,
            year1_demand_savings=y1_demand_savings,
            capex_total=capex,
            base_annual_opex=opex,
            replacement_events=sc_replacements,
            financial=fin_cfg,
            dc_kw=dc_kw,
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
                dc_kw=dc_kw,
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
                **({"merged_sources": settlement["merged_sources"]} if settlement.get("merged_sources") else {}),
            }
        results.append(entry)

    # Ranking: lower is better for LCOE and payback; higher is better for NPV, IRR, year1_net_benefit
    lower_is_better = rank_by in ("lcoe", "simple_payback_years", "discounted_payback_years")
    metric_key = {"irr": "irr_pct", "equity_irr": "equity_irr_pct"}.get(rank_by, rank_by)

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


def evaluate_standalone_economics(
    energy_balance: dict[str, float],
    *,
    capex_total: float,
    annual_om_opex: float = 0.0,
    fuel_price_per_liter: float = 1.50,
    genset_opex_per_hour: float = 0.0,
    genset_fuel_rate_l_per_kwh: float | None = None,
    avoided_tariff_per_kwh: float = 0.0,
    financial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate lifetime LCOE, diesel fuel OPEX, and cashflows for Stand-Alone PV+BESS(+Genset) systems."""
    fin_cfg = _validate_financial(financial)
    replacement_events = list(fin_cfg["replacement_events"])
    if fin_cfg["battery_replacement_year"] is not None and fin_cfg["battery_replacement_cost"] is None:
        raise ValueError("financial.battery_replacement_cost is required for a stand-alone battery replacement year")
    replacement = _battery_replacement_event(fin_cfg, battery_kwh=0.0)
    if replacement is not None:
        replacement_events.append(replacement)
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
        rate = _finite(genset_fuel_rate_l_per_kwh, "genset_fuel_rate_l_per_kwh", minimum=0.0)
        fuel_l = e_bkup * rate

    annual_fuel_cost = fuel_l * fuel_price
    annual_genset_om = bkup_h * gen_opex_h
    base_annual_opex = om_opex + annual_fuel_cost + annual_genset_om
    year1_gross_benefit = e_user * avoided_rate

    metrics = _compute_cashflow_metrics(
        year1_energy_kwh=e_user,
        year1_gross_benefit=year1_gross_benefit,
        capex_total=capex,
        base_annual_opex=base_annual_opex,
        replacement_events=replacement_events,
        financial=fin_cfg,
    )
    return {
        "currency": fin_cfg["currency"],
        "project_life_years": fin_cfg["project_life_years"],
        "capex_total": round(capex, 2),
        "annual_om_opex": round(om_opex, 2),
        "annual_fuel_liters": round(fuel_l, 2),
        "annual_fuel_cost": round(annual_fuel_cost, 2),
        "annual_genset_maintenance_cost": round(annual_genset_om, 2),
        "total_year1_opex": round(base_annual_opex, 2),
        "lcoe": metrics["lcoe"],
        "npv": metrics["npv"],
        "irr_pct": metrics["irr_pct"],
        "simple_payback_years": metrics["simple_payback_years"],
        "discounted_payback_years": metrics["discounted_payback_years"],
        "lifetime_energy_kwh": metrics["lifetime_energy_kwh"],
        "lifetime_opex": metrics["lifetime_opex"],
    }

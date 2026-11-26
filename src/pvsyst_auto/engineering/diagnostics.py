"""Deterministic physics loss-chain and time-series anomaly diagnostics for PVsyst SFI CSV results.

Pure Python standard library implementation (no external ML dependencies). Operates in a single
streaming pass over any hourly or sub-hourly SFI CSV, respecting explicit units and actual
step_minutes, and optionally attributes root causes back to saved .VC0 parameters.
"""
from __future__ import annotations

import math
from pathlib import Path
import re

from pvsyst_auto.components.editor import root_block
from pvsyst_auto.results import ENERGY_TO_KWH, IRRAD_UNIT_KIND, POWER_TO_KW, RegularResultCsv

SEVERITY_ORDER = {"none": 0, "info": 1, "medium": 2, "high": 3, "critical": 4}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "pr_min": 0.65, "pr_max": 0.94, "bifacial_pr_min": 0.70, "bifacial_pr_max": 1.12,
    "clipping_loss_max_pct": 3.0, "overvoltage_loss_max_kwh": 0.0,
    "undervoltage_loss_max_pct": 1.5, "current_limit_loss_max_pct": 1.0,
    "threshold_loss_max_pct": 2.0, "shading_loss_max_pct": 12.0,
    "electrical_shading_max_pct": 8.0, "soiling_loss_max_pct": 5.0,
    "curtailment_max_pct": 5.0, "unmet_load_max_pct": 10.0, "soc_pinned_max_pct": 35.0,
    "zero_output_irrad_min_w_m2": 100.0, "zero_output_max_hours": 24.0,
    "night_export_max_kwh": 1.0, "phase_lag_max_hours": 2.0,
}

_APPARENT_POWER_SCALE = {"VA": 0.001, "kVA": 1.0}
_APPARENT_ENERGY_SCALE = {"VAh": 0.001, "kVAh": 1.0}
_PR_SCALE = {"ratio": 1.0, "%": 0.01}
_HEX_FLAG = re.compile(r"\$[0-9a-fA-F]{1,8}")
_LOAD_FLAG_BIT = 0x00080000
_GRID_LIMIT_ENABLED_BIT = 0x01000000
_GRID_LIMIT_APPARENT_BIT = 0x02000000
_LOCAL_USE_COLUMNS = (
    "E_User", "E_Solar", "EFrGrid", "E_Miss", "EUseful", "EDirUse",
    "EBatCh", "EBatDis", "SOC", "SOC_End", "SOC_Beg", "SOCmean",
)
_GENERATION_COLUMNS = ("EArray", "EOutInv", "E_Avail")

_INV_LIMIT_RULES = (
    ("IL_Pmax", "INVERTER_CLIPPING_HIGH", "clipping_loss_max_pct", "medium",
     "Inverter overpower clipping loss (IL_Pmax)",
     "Review DC/AC ratio (NInverter / NStringCh / GInverter) or evaluate PeakShaving storage."),
    ("IL_Vmin", "INVERTER_UNDERVOLTAGE", "undervoltage_loss_max_pct", "high",
     "Inverter under-voltage loss (IL_Vmin)",
     "Increase NModSerie so hot string Vmp stays above inverter VMppMin."),
    ("IL_Imax", "INVERTER_CURRENT_LIMIT", "current_limit_loss_max_pct", "high",
     "Inverter current-limit loss (IL_Imax)",
     "Reduce parallel strings per MPPT or select an inverter with higher IDCMax."),
    ("IL_Pmin", "INVERTER_THRESHOLD_LOSS", "threshold_loss_max_pct", "medium",
     "Inverter power-threshold loss (IL_Pmin)",
     "Verify inverter is not oversized relative to installed DC capacity."),
)


def _validate_thresholds(overrides: dict | None) -> dict[str, float]:
    merged = dict(DEFAULT_THRESHOLDS)
    if overrides is None:
        return merged
    if not isinstance(overrides, dict):
        raise ValueError("thresholds must be a mapping of threshold names to finite numbers")
    unknown = sorted(set(overrides) - set(DEFAULT_THRESHOLDS))
    if unknown:
        raise ValueError("Unknown diagnostic threshold keys: " + ", ".join(unknown))
    for key, val in overrides.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0:
            raise ValueError(f"thresholds.{key} must be a finite nonnegative number")
        merged[key] = float(val)
    if merged["pr_min"] >= merged["pr_max"]:
        raise ValueError("thresholds.pr_min must be less than thresholds.pr_max")
    if merged["bifacial_pr_min"] >= merged["bifacial_pr_max"]:
        raise ValueError("thresholds.bifacial_pr_min must be less than thresholds.bifacial_pr_max")
    return merged


def _to_kwh(raw_sum: float, unit: str, step_hours: float) -> float | None:
    if unit in POWER_TO_KW:
        return raw_sum * POWER_TO_KW[unit] * step_hours
    if unit in ENERGY_TO_KWH:
        return raw_sum * ENERGY_TO_KWH[unit]
    return None


def _scale_to_kw(
    value: float, unit: str, step_hours: float, power_scale: dict[str, float], energy_scale: dict[str, float]
) -> float | None:
    if unit in power_scale:
        return value * power_scale[unit]
    if unit in energy_scale and step_hours > 0:
        return value * energy_scale[unit] / step_hours
    return None


def _to_kw(value: float, unit: str, step_hours: float) -> float | None:
    return _scale_to_kw(value, unit, step_hours, POWER_TO_KW, ENERGY_TO_KWH)


def _to_kva(value: float, unit: str, step_hours: float) -> float | None:
    return _scale_to_kw(value, unit, step_hours, _APPARENT_POWER_SCALE, _APPARENT_ENERGY_SCALE)


def _to_kwh_m2(raw_sum: float, unit: str, step_hours: float) -> float | None:
    info = IRRAD_UNIT_KIND.get(unit)
    return None if info is None else raw_sum * info[0] * (step_hours if info[1] else 1.0)


def _to_w_m2(value: float, unit: str, step_hours: float = 1.0) -> float | None:
    info = IRRAD_UNIT_KIND.get(unit)
    if info is None:
        return None
    scale, is_power = info
    if not is_power and step_hours <= 0:
        return None
    return value * (scale * 1000.0) / (1.0 if is_power else step_hours)


def _variant_context(store, project: str | None, variant: str | None) -> tuple[dict | None, dict | None]:
    if not project and not variant:
        return None, None
    if bool(project) != bool(variant):
        raise ValueError("Provide both project and variant for .VC0 root-cause attribution, or omit both")
    if store is None:
        raise ValueError("VariantStore is required for .VC0 root-cause attribution")
    vc_info = store.inspect_parameters(project, variant)
    load_ctx = None
    if hasattr(store, "_read") and hasattr(store, "_path"):
        try:
            with store.lock:
                data = store._read(store._path(project, variant))
            lines = data.decode("utf-8-sig", errors="replace").splitlines(keepends=True)
            has_pvload = any(line.strip() == "PVObject_UserNeeds=pvLoad" for line in lines)
            raw_flags = None
            load_flag_enabled = None
            try:
                _, raw_flags = root_block(lines).one("Flags")
                if _HEX_FLAG.fullmatch(raw_flags):
                    load_flag_enabled = bool(int(raw_flags[1:], 16) & _LOAD_FLAG_BIT)
            except (ValueError, KeyError):
                pass
            load_ctx = {"has_pvload": has_pvload, "root_flags": raw_flags, "load_flag_enabled": load_flag_enabled}
        except OSError:
            load_ctx = None
    return vc_info, load_ctx


def _parse_monthly_tan_phi(raw) -> list[float] | None:
    if isinstance(raw, list) and len(raw) == 12:
        items = raw
    elif isinstance(raw, str):
        items = [part.strip() for part in raw.removesuffix(",").split(",") if part.strip()]
        if len(items) != 12:
            return None
    else:
        return None
    vals: list[float] = []
    for item in items:
        if isinstance(item, bool):
            return None
        try:
            v = float(item)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(v):
            return None
        vals.append(v)
    return vals


def _pf_from_dict(pf: dict | None) -> tuple[float | None, list[float] | None]:
    """Extract (scalar tan_phi, monthly tan_phi list) from a normalized power_factor dict."""
    if not isinstance(pf, dict):
        return None, None
    mode = pf.get("mode")
    if mode == "constant":
        raw_tp = pf.get("tan_phi")
        if isinstance(raw_tp, (int, float)) and math.isfinite(raw_tp):
            return float(raw_tp), None
    elif mode == "monthly":
        return None, _parse_monthly_tan_phi(pf.get("monthly_tan_phi"))
    return None, None


def _extract_grid_limit_context(
    vc_info: dict | None,
    *,
    peak_egrid_kw: float | None,
    peak_eap_kva: float | None,
    peak_egrid_kw_by_month: dict[int, float] | None = None,
) -> dict | None:
    """Resolve active dispatch cap for kW or kVA (apparent) PLimGrid variants."""
    if not vc_info:
        return None
    limit = None
    quantity = None
    tan_phi = None
    monthly_tan_phi: list[float] | None = None

    pc = vc_info.get("power_controls")
    if isinstance(pc, dict):
        gl = pc.get("grid_limit")
        if isinstance(gl, dict) and gl.get("mode") == "constant" and isinstance(gl.get("limit"), (int, float)):
            limit = float(gl["limit"])
            quantity = gl.get("quantity") or "active"
        tan_phi, monthly_tan_phi = _pf_from_dict(pc.get("power_factor"))

    sys_params = vc_info.get("system_parameters") or {}
    raw_flags = sys_params.get("Flags")
    sys_flags = int(raw_flags[1:], 16) if isinstance(raw_flags, str) and _HEX_FLAG.fullmatch(raw_flags) else 0

    if limit is None and (sys_flags & _GRID_LIMIT_ENABLED_BIT) and sys_params.get("PLimGrid") is not None:
        try:
            parsed_lim = float(sys_params["PLimGrid"])
        except (TypeError, ValueError):
            parsed_lim = None
        if parsed_lim is not None and math.isfinite(parsed_lim) and parsed_lim > 0:
            limit = parsed_lim
            quantity = "apparent" if (sys_flags & _GRID_LIMIT_APPARENT_BIT) else "active"

    if tan_phi is None and monthly_tan_phi is None:
        tan_phi, monthly_tan_phi = _pf_from_dict(vc_info.get("power_factor"))
    if tan_phi is None and monthly_tan_phi is None:
        if (sys_flags & 0x00300000) == 0x00300000 and sys_params.get("TanPhiM") is not None:
            monthly_tan_phi = _parse_monthly_tan_phi(sys_params.get("TanPhiM"))
        elif sys_params.get("TanPhi") is not None:
            try:
                parsed_tp = float(sys_params["TanPhi"])
                if math.isfinite(parsed_tp):
                    tan_phi = parsed_tp
            except (TypeError, ValueError):
                pass

    if limit is None or not math.isfinite(limit) or limit <= 0:
        return None

    base = {
        "enabled": True,
        "quantity": quantity,
        "configured_limit": round(limit, 4),
        "configured_unit": "kVA" if quantity == "apparent" else "kW",
        "peak_e_grid_kw": round(peak_egrid_kw, 4) if peak_egrid_kw is not None else None,
        "peak_eap_grid_kva": round(peak_eap_kva, 4) if peak_eap_kva is not None else None,
        "apparent_exceeds_configured_is_expected": quantity == "apparent",
    }
    if monthly_tan_phi is not None:
        monthly_cos = [1.0 / math.sqrt(1.0 + tp * tp) for tp in monthly_tan_phi]
        monthly_caps = [(limit * cp if quantity == "apparent" else limit) for cp in monthly_cos]
        uniform = all(math.isclose(tp, monthly_tan_phi[0], rel_tol=1e-9, abs_tol=1e-9) for tp in monthly_tan_phi)
        scalar_cap = round(monthly_caps[0], 4) if uniform else None
        if peak_egrid_kw_by_month:
            binding = any(
                peak_m >= monthly_caps[m - 1] * 0.995
                for m, peak_m in peak_egrid_kw_by_month.items()
                if 1 <= m <= 12 and monthly_caps[m - 1] > 0
            )
        else:
            binding = bool(peak_egrid_kw is not None and scalar_cap is not None and scalar_cap > 0
                           and peak_egrid_kw >= scalar_cap * 0.995)
        return {
            **base,
            "power_factor_mode": "monthly",
            "tan_phi": round(monthly_tan_phi[0], 6) if uniform else None,
            "cos_phi": round(monthly_cos[0], 6) if uniform else None,
            "active_dispatch_cap_kw": scalar_cap,
            "monthly_tan_phi": [round(tp, 6) for tp in monthly_tan_phi],
            "monthly_cos_phi": [round(cp, 6) for cp in monthly_cos],
            "monthly_active_dispatch_cap_kw": [round(cap, 4) for cap in monthly_caps],
            "active_cap_binding": binding,
        }

    eff_tp = tan_phi if tan_phi is not None else 0.0
    cos_phi = 1.0 / math.sqrt(1.0 + eff_tp * eff_tp)
    active_cap_kw = limit * cos_phi if quantity == "apparent" else limit
    return {
        **base,
        "tan_phi": round(eff_tp, 6),
        "cos_phi": round(cos_phi, 6),
        "active_dispatch_cap_kw": round(active_cap_kw, 4),
        "active_cap_binding": bool(peak_egrid_kw is not None and active_cap_kw > 0 and peak_egrid_kw >= active_cap_kw * 0.995),
    }


def _finite_float(val) -> float | None:
    return float(val) if isinstance(val, (int, float)) and math.isfinite(val) else None


def diagnose_simulation_csv(
    csv_path: str | Path,
    *,
    store=None,
    project: str | None = None,
    variant: str | None = None,
    thresholds: dict[str, float] | None = None,
) -> dict:
    """Run deterministic physics and time-series anomaly diagnostics on a PVsyst SFI CSV."""
    path = Path(csv_path)
    cfg = _validate_thresholds(thresholds)
    vc_info, load_ctx = _variant_context(store, project, variant)
    reader = RegularResultCsv(path, empty_message="Result CSV contains no simulation rows")
    units = reader.units
    has_grid_storage = bool(vc_info and vc_info.get("grid_storage") is not None)
    system_type = ((vc_info or {}).get("system_parameters") or {}).get("SystemType")
    has_local_use = bool(
        has_grid_storage or system_type == "Battery"
        or (load_ctx and (load_ctx.get("has_pvload") or load_ctx.get("load_flag_enabled")))
    )

    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    weighted_pr_num = weighted_pr_den = 0.0
    peak_egrid_raw: float | None = None
    peak_egrid_raw_by_month: dict[int, float] = {}
    peak_eap_raw: float | None = None
    egrid_exceeds_earray_count = 0
    egrid_exceeds_stamps: list[str] = []
    night_export_kwh_raw = 0.0
    night_export_count = 0
    night_export_stamps: list[str] = []
    zero_out_count = 0
    zero_out_stamps: list[str] = []
    soc_low_count = soc_high_count = soc_total = 0
    irrad_hour_num = irrad_hour_den = power_hour_num = power_hour_den = 0.0
    headers: list[str] = []
    idx: dict[str, int] = {}
    rows = 0
    step_hours = 1.0

    try:
        for stamp, row, dt_hours in reader.iter_rows(validate_all_rows=True):
            if rows == 0:
                headers, idx, step_hours = reader.headers, reader.idx, dt_hours
                sums = {name: 0.0 for name in idx}
                counts = {name: 0 for name in idx}
            rows += 1
            for name, col_i in idx.items():
                val = _finite_float(row[col_i])
                if val is not None:
                    sums[name] += val
                    counts[name] += 1

            gi_val = _finite_float(row[idx["GlobInc"]]) if "GlobInc" in idx else None
            gi_w_m2 = _to_w_m2(gi_val, units.get("GlobInc", ""), step_hours) if gi_val is not None else None
            eg_val = _finite_float(row[idx["E_Grid"]]) if "E_Grid" in idx else None
            ea_val = _finite_float(row[idx["EArray"]]) if "EArray" in idx else None
            eap_val = _finite_float(row[idx["EApGrid"]]) if "EApGrid" in idx else None
            if eg_val is not None:
                if peak_egrid_raw is None or eg_val > peak_egrid_raw:
                    peak_egrid_raw = eg_val
                if stamp.month not in peak_egrid_raw_by_month or eg_val > peak_egrid_raw_by_month[stamp.month]:
                    peak_egrid_raw_by_month[stamp.month] = eg_val
            if eap_val is not None and (peak_eap_raw is None or eap_val > peak_eap_raw):
                peak_eap_raw = eap_val
            pr_val = _finite_float(row[idx["PR"]]) if "PR" in idx else None
            pr_scale = _PR_SCALE.get(units.get("PR", ""))
            hour_of_day = stamp.hour + stamp.minute / 60.0

            if gi_w_m2 is not None and gi_w_m2 > 0:
                if pr_val is not None and pr_scale is not None:
                    weighted_pr_num += gi_w_m2 * pr_val * pr_scale
                    weighted_pr_den += gi_w_m2
                irrad_hour_num += gi_w_m2 * hour_of_day
                irrad_hour_den += gi_w_m2

            phase_p = ea_val if ea_val is not None else eg_val
            if phase_p is not None and phase_p > 0:
                power_hour_num += phase_p * hour_of_day
                power_hour_den += phase_p

            bat_dis = _finite_float(row[idx["EBatDis"]]) if "EBatDis" in idx else None
            skip_no_discharge = (bat_dis is not None and bat_dis > 1e-6) or (has_grid_storage and bat_dis is None)
            if eg_val is not None and ea_val is not None and not skip_no_discharge:
                if units.get("E_Grid", "") == units.get("EArray", "") and eg_val > ea_val + 1e-3 and eg_val > 0.01:
                    egrid_exceeds_earray_count += 1
                    if len(egrid_exceeds_stamps) < 5:
                        egrid_exceeds_stamps.append(str(stamp))

            if gi_w_m2 is not None and gi_w_m2 <= 0.1 and eg_val is not None and eg_val > 1e-4 and not skip_no_discharge:
                night_export_kwh_raw += eg_val
                night_export_count += 1
                if len(night_export_stamps) < 5:
                    night_export_stamps.append(str(stamp))

            if gi_w_m2 is not None and gi_w_m2 >= cfg["zero_output_irrad_min_w_m2"]:
                out_val = next((v for col in _GENERATION_COLUMNS if col in idx and (v := _finite_float(row[idx[col]])) is not None), None)
                if out_val is None and not has_local_use and not any(col in idx for col in _LOCAL_USE_COLUMNS):
                    out_val = eg_val
                if out_val is not None and out_val <= 1e-6:
                    zero_out_count += 1
                    if len(zero_out_stamps) < 5:
                        zero_out_stamps.append(str(stamp))

            soc_col = "SOC_End" if "SOC_End" in idx else ("SOC" if "SOC" in idx else None)
            if soc_col is not None:
                soc_val = _finite_float(row[idx[soc_col]])
                if soc_val is not None:
                    soc_norm = soc_val * (0.01 if units.get(soc_col) == "%" else 1.0)
                    soc_total += 1
                    if soc_norm <= 0.05:
                        soc_low_count += 1
                    elif soc_norm >= 0.95:
                        soc_high_count += 1
    except ValueError as exc:
        if str(exc) != reader._irregular_message:
            raise
        return {
            "status": "blocked",
            "issues": ["Result CSV timestamps do not have a regular cadence (missing, duplicate, or out-of-order rows); cannot establish interval duration for diagnostics"],
            "max_severity": "none", "finding_count": 0,
            "counts_by_severity": {"critical": 0, "high": 0, "medium": 0, "info": 0},
            "needs_review": False, "rows": reader.rows_count, "step_minutes": None, "is_bifacial": False,
            "weighted_pr": None, "analyzed_columns": list(reader.headers[1:]),
            "variant_attribution_used": vc_info is not None, "grid_limit_context": None,
            "coverage": [], "evaluated_rule_count": 0, "skipped_rule_count": 0, "findings": [],
        }

    eff_step_min = reader.step_minutes if reader.step_minutes is not None else 60.0
    vc_bifacial = bool(vc_info and any((o.get("bifacial") or {}).get("enabled") for o in (vc_info.get("orientations") or [])))
    is_bifacial = vc_bifacial or ("GlobBak" in idx and sums.get("GlobBak", 0.0) > 0)
    sub0 = (vc_info.get("subarrays") or [{}])[0].get("values", {}) if vc_info else {}
    orient0 = (vc_info.get("orientations") or [{}])[0].get("values", {}) if vc_info else {}
    has_planned_unavail = bool(vc_info and (
        (vc_info.get("system_unavailability") or {}).get("configured")
        or (vc_info.get("detailed_losses") or {}).get("system_unavailability", {}).get("enabled")
    ))
    peak_egrid_kw = _to_kw(peak_egrid_raw, units.get("E_Grid", ""), step_hours) if peak_egrid_raw is not None and "E_Grid" in idx else None
    peak_egrid_kw_by_month = {}
    if "E_Grid" in idx:
        eg_unit = units.get("E_Grid", "")
        for m, raw_peak in peak_egrid_raw_by_month.items():
            if (conv := _to_kw(raw_peak, eg_unit, step_hours)) is not None:
                peak_egrid_kw_by_month[m] = conv
    peak_eap_kva = _to_kva(peak_eap_raw, units.get("EApGrid", ""), step_hours) if peak_eap_raw is not None and "EApGrid" in idx else None
    grid_limit_context = _extract_grid_limit_context(
        vc_info, peak_egrid_kw=peak_egrid_kw, peak_eap_kva=peak_eap_kva, peak_egrid_kw_by_month=peak_egrid_kw_by_month or None,
    )

    findings: list[dict] = []
    coverage: list[dict] = []

    def record_coverage(code: str, evaluated: bool, columns: list[str], reason: str | None = None) -> None:
        coverage.append({"code": code, "evaluated": evaluated, "reason": None if evaluated else reason, "columns": list(columns)})

    def add(
        code: str, severity: str, category: str, metric: str, observed: float, threshold: float, unit: str,
        message: str, root_cause_hint: str, actions: list[str],
        *, affected_samples: int = 0, sample_timestamps: list[str] | None = None,
    ) -> None:
        findings.append({
            "code": code, "severity": severity, "category": category, "metric": metric,
            "observed": round(float(observed), 4), "threshold": round(float(threshold), 4), "unit": unit,
            "affected_samples": affected_samples, "sample_timestamps": list(sample_timestamps or []),
            "message": message, "root_cause_hint": root_cause_hint, "recommended_actions": actions,
        })

    earray_kwh = _to_kwh(sums["EArray"], units.get("EArray", ""), step_hours) if "EArray" in idx else None
    egrid_kwh = _to_kwh(sums["E_Grid"], units.get("E_Grid", ""), step_hours) if "E_Grid" in idx else None
    globinc_kwh_m2 = _to_kwh_m2(sums["GlobInc"], units.get("GlobInc", ""), step_hours) if "GlobInc" in idx else None
    base_kwh = earray_kwh if (earray_kwh is not None and earray_kwh > 0) else egrid_kwh
    base_denom_col = "EArray" if (earray_kwh is not None and earray_kwh > 0) else "E_Grid"

    def _base_denom_skip() -> tuple[str, list[str]]:
        bad = [c for c in ("EArray", "E_Grid") if c in idx and _to_kwh(sums[c], units.get(c, ""), step_hours) is None]
        return ("unrecognized_unit", bad) if (bad and base_kwh is None) else ("no_denominator", ["EArray", "E_Grid"])

    # --- 1. PR & Energy Integrity ---
    weighted_pr = (weighted_pr_num / weighted_pr_den) if weighted_pr_den > 0 else None
    if weighted_pr is not None:
        record_coverage("PR_TOO_LOW", True, ["PR", "GlobInc"])
        record_coverage("PR_TOO_HIGH", True, ["PR", "GlobInc"])
        pr_min = cfg["bifacial_pr_min"] if is_bifacial else cfg["pr_min"]
        pr_max = cfg["bifacial_pr_max"] if is_bifacial else cfg["pr_max"]
        if weighted_pr < pr_min:
            ctx = f" (NModSerie={sub0.get('NModSerie')}, NStringCh={sub0.get('NStringCh')})" if sub0 else ""
            add("PR_TOO_LOW", "critical" if weighted_pr < 0.50 else "high", "pr_integrity", "PR", weighted_pr, pr_min, "ratio",
                f"Irradiance-weighted PR ({weighted_pr:.4f}) is below expected minimum ({pr_min:.2f}){ctx}.",
                "Severe inverter clipping/overvoltage, heavy shading, curtailment, or mis-sized strings/MPPT.",
                ["Inspect IL_Vmax, IL_Pmax, GlobShd/ShdElec, and EGrdLim columns.",
                 "Check string length (NModSerie), DC/AC ratio, and shading geometry in .VC0."])
        elif weighted_pr > pr_max:
            add("PR_TOO_HIGH", "high", "pr_integrity", "PR", weighted_pr, pr_max, "ratio",
                f"Irradiance-weighted PR ({weighted_pr:.4f}) exceeds physical upper bound ({pr_max:.2f}).",
                "Check module nameplate PNom, bifacial setting, or unexpected storage discharge/weather mismatch.",
                ["Verify PAN PNom and array module count match nominal DC power.",
                 "Confirm whether bifaciality or temperature-corrected PR (PRCorrTemp) is enabled."])
    else:
        missing_pr = [c for c in ("PR", "GlobInc") if c not in idx]
        pr_ok = _PR_SCALE.get(units.get("PR", "")) is not None
        gi_ok = _to_w_m2(1.0, units.get("GlobInc", ""), step_hours) is not None
        if missing_pr:
            pr_reason, pr_cols = "missing_column", missing_pr
        elif not pr_ok or not gi_ok:
            pr_reason = "unrecognized_unit"
            pr_cols = [c for c, ok in (("PR", pr_ok), ("GlobInc", gi_ok)) if not ok]
        else:
            pr_reason, pr_cols = "no_denominator", ["PR", "GlobInc"]
        record_coverage("PR_TOO_LOW", False, pr_cols, reason=pr_reason)
        record_coverage("PR_TOO_HIGH", False, pr_cols, reason=pr_reason)

    storage_missing_ebatdis = has_grid_storage and counts.get("EBatDis", 0) == 0
    if "E_Grid" in idx and "EArray" in idx:
        eg_u, ea_u = units.get("E_Grid", ""), units.get("EArray", "")
        if _to_kwh(1.0, eg_u, step_hours) is None or _to_kwh(1.0, ea_u, step_hours) is None or eg_u != ea_u:
            record_coverage("E_GRID_EXCEEDS_EARRAY", False, ["E_Grid", "EArray"], reason="unrecognized_unit")
        elif storage_missing_ebatdis:
            record_coverage("E_GRID_EXCEEDS_EARRAY", False, ["EBatDis"], reason="missing_column")
        else:
            record_coverage("E_GRID_EXCEEDS_EARRAY", True, ["E_Grid", "EArray"])
    else:
        record_coverage("E_GRID_EXCEEDS_EARRAY", False, [c for c in ("E_Grid", "EArray") if c not in idx], reason="missing_column")
    if egrid_exceeds_earray_count > 0:
        add("E_GRID_EXCEEDS_EARRAY", "high", "pr_integrity", "E_Grid - EArray", float(egrid_exceeds_earray_count), 0.0, "samples",
            f"E_Grid exceeded EArray on {egrid_exceeds_earray_count} samples without battery discharge.",
            "Possible unit mismatch or non-PV AC generation source active in the system.",
            ["Verify SFI column units and inspect pvSystem for backup generator or storage."],
            affected_samples=egrid_exceeds_earray_count, sample_timestamps=egrid_exceeds_stamps)

    if "GlobInc" in idx and "E_Grid" in idx:
        gi_unit_ok = _to_w_m2(1.0, units.get("GlobInc", ""), step_hours) is not None
        eg_unit_ok = _to_kwh(1.0, units.get("E_Grid", ""), step_hours) is not None
        if not gi_unit_ok or not eg_unit_ok:
            record_coverage("NIGHTTIME_EXPORT", False, [c for c, ok in (("GlobInc", gi_unit_ok), ("E_Grid", eg_unit_ok)) if not ok], reason="unrecognized_unit")
        elif storage_missing_ebatdis:
            record_coverage("NIGHTTIME_EXPORT", False, ["EBatDis"], reason="missing_column")
        else:
            record_coverage("NIGHTTIME_EXPORT", True, ["GlobInc", "E_Grid"])
    else:
        record_coverage("NIGHTTIME_EXPORT", False, [c for c in ("GlobInc", "E_Grid") if c not in idx], reason="missing_column")
    if night_export_count > 0 and "E_Grid" in idx:
        night_kwh = _to_kwh(night_export_kwh_raw, units.get("E_Grid", ""), step_hours) or 0.0
        if night_kwh > cfg["night_export_max_kwh"]:
            add("NIGHTTIME_EXPORT", "high", "pr_integrity", "E_Grid(night)", night_kwh, cfg["night_export_max_kwh"], "kWh",
                f"Positive grid export ({night_kwh:.2f} kWh across {night_export_count} samples) occurred at zero irradiance.",
                "Weather file timezone/timestamp misalignment or unlogged battery/generator discharge.",
                ["Run pvsyst_review_weather to verify MET timezone/LegalTime alignment."],
                affected_samples=night_export_count, sample_timestamps=night_export_stamps)

    # --- 2. Inverter Limit Losses ---
    if "IL_Vmax" in idx:
        il_vmax_kwh = _to_kwh(sums["IL_Vmax"], units.get("IL_Vmax", ""), step_hours)
        if il_vmax_kwh is None:
            record_coverage("INVERTER_OVERVOLTAGE", False, ["IL_Vmax"], reason="unrecognized_unit")
        else:
            record_coverage("INVERTER_OVERVOLTAGE", True, ["IL_Vmax"])
            if il_vmax_kwh > cfg["overvoltage_loss_max_kwh"]:
                n_ser = sub0.get("NModSerie")
                hint = (f"String voltage exceeded inverter VMPPMax/VAbsMax (current NModSerie={n_ser})."
                        if n_ser else "String open-circuit or MPP voltage exceeded inverter upper voltage limit.")
                add("INVERTER_OVERVOLTAGE", "critical" if (base_kwh and il_vmax_kwh / base_kwh > 0.01) else "high",
                    "inverter_limits", "IL_Vmax", il_vmax_kwh, cfg["overvoltage_loss_max_kwh"], "kWh",
                    f"Inverter over-voltage loss IL_Vmax = {il_vmax_kwh:.2f} kWh detected.", hint,
                    ["Reduce NModSerie via pvsyst_update_variant_parameters (or re-run pvsyst_suggest_sizing with colder min_ambient_c)."])
    else:
        record_coverage("INVERTER_OVERVOLTAGE", False, ["IL_Vmax"], reason="missing_column")

    for col, code, thresh_key, sev, desc, action in _INV_LIMIT_RULES:
        if col not in idx:
            record_coverage(code, False, [col], reason="missing_column")
            continue
        loss_kwh = _to_kwh(sums[col], units.get(col, ""), step_hours)
        if loss_kwh is None:
            record_coverage(code, False, [col], reason="unrecognized_unit")
            continue
        if not (base_kwh and base_kwh > 0):
            skip_reason, denom_cols = _base_denom_skip()
            record_coverage(code, False, [col, *denom_cols], reason=skip_reason)
            continue
        record_coverage(code, True, [col, base_denom_col])
        pct = (loss_kwh / base_kwh) * 100.0
        if pct > cfg[thresh_key]:
            final_sev = "high" if (col == "IL_Pmax" and pct > cfg[thresh_key] * 2.5) else sev
            extra = f" (NInverter={sub0.get('NInverter')}, NModSerie={sub0.get('NModSerie')})" if sub0 else ""
            add(code, final_sev, "inverter_limits", f"{col} / EArray", pct, cfg[thresh_key], "%",
                f"{desc} is {pct:.2f}% ({loss_kwh:.1f} kWh), above threshold {cfg[thresh_key]:.2f}%{extra}.",
                f"{col} accounts for {pct:.2f}% of array energy.", [action])

    # --- 3. Shading, Soiling & Grid Curtailment ---
    before = "GlobHrz" if counts.get("GlobHrz") == rows else "GlobInc"
    if all(counts.get(col) == rows for col in (before, "GlobShd")):
        before_shd = _to_kwh_m2(sums[before], units.get(before, ""), step_hours)
        after_shd = _to_kwh_m2(sums["GlobShd"], units.get("GlobShd", ""), step_hours)
        if before_shd is None or after_shd is None:
            record_coverage("SHADING_LOSS_HIGH", False, [c for c, v in ((before, before_shd), ("GlobShd", after_shd)) if v is None], reason="unrecognized_unit")
        elif not (before_shd and before_shd > 0):
            record_coverage("SHADING_LOSS_HIGH", False, [before, "GlobShd"], reason="no_denominator")
        else:
            record_coverage("SHADING_LOSS_HIGH", True, [before, "GlobShd"])
            shd_kwh_m2 = before_shd - after_shd
            pct = shd_kwh_m2 / before_shd * 100.0
            if pct > cfg["shading_loss_max_pct"]:
                pitch_note = f" (FieldType={orient0.get('FieldType')}, Pitch={orient0.get('Pitch')})" if orient0 else ""
                add("SHADING_LOSS_HIGH", "high" if pct > cfg["shading_loss_max_pct"] * 2 else "medium",
                    "shading_soiling_curtailment", f"({before} - GlobShd) / {before}", pct, cfg["shading_loss_max_pct"], "%",
                    f"Near-shading irradiance loss is {pct:.2f}% ({abs(shd_kwh_m2):.1f} kWh/m2){pitch_note}.",
                    "Row pitch too tight (high GCR), steep tilt, or nearby 3D obstacles casting shadows.",
                    ["Increase row pitch (lower GCR), enable backtracking on trackers, or adjust 3D obstacle clearance."])
    else:
        record_coverage("SHADING_LOSS_HIGH", False, [col for col in (before, "GlobShd") if counts.get(col) != rows], reason="missing_column")

    if "ShdElec" not in idx:
        record_coverage("ELECTRICAL_SHADING_HIGH", False, ["ShdElec"], reason="missing_column")
    elif (shd_elec_kwh := _to_kwh(sums["ShdElec"], units.get("ShdElec", ""), step_hours)) is None:
        record_coverage("ELECTRICAL_SHADING_HIGH", False, ["ShdElec"], reason="unrecognized_unit")
    elif not (base_kwh and base_kwh > 0):
        skip_reason, denom_cols = _base_denom_skip()
        record_coverage("ELECTRICAL_SHADING_HIGH", False, ["ShdElec", *denom_cols], reason=skip_reason)
    else:
        record_coverage("ELECTRICAL_SHADING_HIGH", True, ["ShdElec", base_denom_col])
        pct = (abs(shd_elec_kwh) / base_kwh) * 100.0
        if pct > cfg["electrical_shading_max_pct"]:
            add("ELECTRICAL_SHADING_HIGH", "high", "shading_soiling_curtailment", "ShdElec / EArray", pct, cfg["electrical_shading_max_pct"], "%",
                f"Electrical shading mismatch loss ShdElec is {pct:.2f}% ({abs(shd_elec_kwh):.1f} kWh).",
                "Partial shading hits across module strings in module_layout / string wiring.",
                ["Re-group shaded modules onto dedicated strings/MPPTs or switch table orientation (portrait vs landscape)."])

    if "SlgLoss" not in idx:
        record_coverage("SOILING_LOSS_HIGH", False, ["SlgLoss"], reason="missing_column")
    elif (slg_kwh_m2 := _to_kwh_m2(sums["SlgLoss"], units.get("SlgLoss", ""), step_hours)) is None:
        record_coverage("SOILING_LOSS_HIGH", False, ["SlgLoss"], reason="unrecognized_unit")
    elif "GlobInc" in idx and globinc_kwh_m2 is None:
        record_coverage("SOILING_LOSS_HIGH", False, ["SlgLoss", "GlobInc"], reason="unrecognized_unit")
    elif not (globinc_kwh_m2 and globinc_kwh_m2 > 0):
        record_coverage("SOILING_LOSS_HIGH", False, ["SlgLoss", "GlobInc"], reason="no_denominator")
    else:
        record_coverage("SOILING_LOSS_HIGH", True, ["SlgLoss", "GlobInc"])
        pct = (abs(slg_kwh_m2) / globinc_kwh_m2) * 100.0
        if pct > cfg["soiling_loss_max_pct"]:
            add("SOILING_LOSS_HIGH", "medium", "shading_soiling_curtailment", "SlgLoss / GlobInc", pct, cfg["soiling_loss_max_pct"], "%",
                f"Soiling loss is {pct:.2f}%, exceeding {cfg['soiling_loss_max_pct']:.1f}%.",
                "High constant or monthly soiling loss factors configured in .VC0.",
                ["Review soiling_updates in pvsyst_update_variant_parameters."])

    present_curt = [c for c in ("EGrdLim", "EUnused") if c in idx]
    if not present_curt:
        record_coverage("GRID_CURTAILMENT_HIGH", False, ["EGrdLim", "EUnused"], reason="missing_column")
    else:
        curt_col = next((c for c in present_curt if _to_kwh(sums[c], units.get(c, ""), step_hours) is not None), present_curt[0])
        curt_kwh = _to_kwh(sums[curt_col], units.get(curt_col, ""), step_hours)
        if curt_kwh is None:
            record_coverage("GRID_CURTAILMENT_HIGH", False, present_curt, reason="unrecognized_unit")
        elif not (base_kwh and base_kwh > 0):
            skip_reason, denom_cols = _base_denom_skip()
            record_coverage("GRID_CURTAILMENT_HIGH", False, [curt_col, *denom_cols], reason=skip_reason)
        else:
            record_coverage("GRID_CURTAILMENT_HIGH", True, [curt_col, base_denom_col])
            pct = (abs(curt_kwh) / base_kwh) * 100.0
            if pct > cfg["curtailment_max_pct"]:
                if grid_limit_context and grid_limit_context["quantity"] == "apparent":
                    hint = (f"Apparent grid limit {grid_limit_context['configured_limit']:g} kVA "
                            f"with cos(phi)={grid_limit_context['cos_phi']:.4f} caps active dispatch at "
                            f"{grid_limit_context['active_dispatch_cap_kw']:.2f} kW "
                            f"(not {grid_limit_context['configured_limit']:g} kW); native EApGrid can "
                            f"legitimately exceed {grid_limit_context['configured_limit']:g} kVA.")
                else:
                    hint = "Active grid injection limit (PLimGrid) or full battery preventing PV export."
                add("GRID_CURTAILMENT_HIGH", "medium", "shading_soiling_curtailment", f"{curt_col} / EArray", pct, cfg["curtailment_max_pct"], "%",
                    f"Grid limit / unused energy ({curt_col}) is {pct:.2f}% ({abs(curt_kwh):.1f} kWh).", hint,
                    ["Evaluate PeakShaving or larger battery capacity via pvsyst_create_grid_storage_system."])

    # --- 4. Storage & Load Balance ---
    if vc_info is None:
        record_coverage("LOAD_NOT_ENABLED", False, [], reason="missing_vc0")
    else:
        record_coverage("LOAD_NOT_ENABLED", True, [c for c in ("E_User", "E_Solar", "EFrGrid") if c in idx])
    if load_ctx and load_ctx.get("has_pvload") and load_ctx.get("load_flag_enabled") is False:
        e_user_total = _to_kwh(sums["E_User"], units.get("E_User", ""), step_hours) if "E_User" in idx else None
        if not any(c in idx for c in ("E_User", "E_Solar", "EFrGrid")) or (e_user_total is not None and abs(e_user_total) <= 1e-6):
            add("LOAD_NOT_ENABLED", "high", "storage_balance", "root_flags_bit19", 0.0, 1.0, "flag",
                f"Variant embeds PVObject_UserNeeds=pvLoad, but root pvVCalcul.Flags ({load_ctx.get('root_flags')}) has bit 19 ($00080000) clear and no load energy was consumed.",
                "PVsystCLI ignores PVObject_UserNeeds=pvLoad unless root pvVCalcul.Flags sets bit 19 ($00080000).",
                ["Rebind the hourly load via pvsyst_create_native_load_variant (sets bit 19 $00080000) and re-run."])

    if "E_Miss" not in idx:
        record_coverage("UNMET_LOAD_HIGH", False, ["E_Miss"], reason="missing_column")
    else:
        miss_kwh = _to_kwh(sums["E_Miss"], units.get("E_Miss", ""), step_hours)
        user_kwh = _to_kwh(sums["E_User"], units.get("E_User", ""), step_hours) if "E_User" in idx else None
        if miss_kwh is None or ("E_User" in idx and user_kwh is None):
            bad_cols = [c for c, ok in (("E_Miss", miss_kwh is not None), ("E_User", user_kwh is not None or "E_User" not in idx)) if not ok]
            record_coverage("UNMET_LOAD_HIGH", False, bad_cols, reason="unrecognized_unit")
        elif not (user_kwh and user_kwh > 0):
            record_coverage("UNMET_LOAD_HIGH", False, ["E_Miss", "E_User"], reason="no_denominator")
        else:
            record_coverage("UNMET_LOAD_HIGH", True, ["E_Miss", "E_User"])
            pct = (miss_kwh / user_kwh) * 100.0
            if pct > cfg["unmet_load_max_pct"]:
                add("UNMET_LOAD_HIGH", "high" if pct > 25.0 else "medium", "storage_balance", "E_Miss / E_User", pct, cfg["unmet_load_max_pct"], "%",
                    f"Unmet load E_Miss is {pct:.2f}% ({miss_kwh:.1f} / {user_kwh:.1f} kWh).",
                    "Battery capacity, discharge power limit, or PV sizing insufficient for load.",
                    ["Review battery parallel_count, max_discharge_ac_kw, or WeakGrid/PowerShift schedule."])

    active_soc_col = "SOC_End" if "SOC_End" in idx else ("SOC" if "SOC" in idx else None)
    if soc_total > 0 and active_soc_col is not None:
        record_coverage("SOC_PINNED_LOW", True, [active_soc_col])
        record_coverage("SOC_PINNED_HIGH", True, [active_soc_col])
        low_pct, high_pct = (soc_low_count / soc_total) * 100.0, (soc_high_count / soc_total) * 100.0
        if low_pct > cfg["soc_pinned_max_pct"]:
            add("SOC_PINNED_LOW", "medium", "storage_balance", "SOC <= 5%", low_pct, cfg["soc_pinned_max_pct"], "%",
                f"Battery SOC stayed <= 5% for {low_pct:.1f}% of the simulation ({soc_low_count} samples).",
                "Storage is under-charged relative to load or charging strategy is too restrictive.",
                ["Check charging_strategy, max_charge_ac_kw, or PV-to-load ratio."], affected_samples=soc_low_count)
        if high_pct > cfg["soc_pinned_max_pct"]:
            add("SOC_PINNED_HIGH", "medium", "storage_balance", "SOC >= 95%", high_pct, cfg["soc_pinned_max_pct"], "%",
                f"Battery SOC stayed >= 95% for {high_pct:.1f}% of the simulation ({soc_high_count} samples).",
                "Battery rarely discharges (e.g., restrictive discharge window or oversized battery).",
                ["Check discharging_strategy, discharge_start_hour/end_hour, or battery sizing."], affected_samples=soc_high_count)
    else:
        record_coverage("SOC_PINNED_LOW", False, ["SOC_End", "SOC"], reason="missing_column")
        record_coverage("SOC_PINNED_HIGH", False, ["SOC_End", "SOC"], reason="missing_column")

    # --- 5. Time-Series Anomalies ---
    local_use_in_csv = any(col in idx for col in _LOCAL_USE_COLUMNS)
    usable_gen_col = next((col for col in _GENERATION_COLUMNS if counts.get(col, 0) > 0), None)
    if usable_gen_col is None and not has_local_use and not local_use_in_csv and counts.get("E_Grid", 0) > 0:
        usable_gen_col = "E_Grid"
    if "GlobInc" not in idx:
        record_coverage("HIGH_IRRADIANCE_ZERO_OUTPUT", False, ["GlobInc"], reason="missing_column")
    elif _to_w_m2(1.0, units.get("GlobInc", ""), step_hours) is None:
        record_coverage("HIGH_IRRADIANCE_ZERO_OUTPUT", False, ["GlobInc"], reason="unrecognized_unit")
    elif usable_gen_col is None:
        missing_out = list(_GENERATION_COLUMNS) + ([] if (has_local_use or local_use_in_csv) else ["E_Grid"])
        record_coverage("HIGH_IRRADIANCE_ZERO_OUTPUT", False, missing_out, reason="missing_column")
    else:
        record_coverage("HIGH_IRRADIANCE_ZERO_OUTPUT", True, ["GlobInc", usable_gen_col])
    zero_out_hours = zero_out_count * step_hours
    if zero_out_hours > cfg["zero_output_max_hours"]:
        sev = "info" if has_planned_unavail else ("critical" if zero_out_hours > 200 else "high")
        hint = (
            "Planned SystemUnavail periods are active in .VC0; verify timestamps match scheduled outages."
            if has_planned_unavail else
            "Prolonged zero output during high irradiance (>= 100 W/m2) indicates inverter shutdown (Voc/Vmp out of range) or unscheduled outage."
        )
        add("HIGH_IRRADIANCE_ZERO_OUTPUT", sev, "time_series", "zero_output_hours", zero_out_hours, cfg["zero_output_max_hours"], "hours",
            f"{zero_out_hours:.1f} hours had GlobInc >= {cfg['zero_output_irrad_min_w_m2']:g} W/m2 with zero output.", hint,
            ["Inspect sample_timestamps via pvsyst_read_rows alongside IL_Vmax, IL_Vmin, and SystemUnavail."],
            affected_samples=zero_out_count, sample_timestamps=zero_out_stamps)

    phase_out_col = "EArray" if "EArray" in idx else ("E_Grid" if "E_Grid" in idx else None)
    if "GlobInc" not in idx or phase_out_col is None:
        missing_phase = ([c for c in ("GlobInc",) if c not in idx]
                         + (["EArray", "E_Grid"] if phase_out_col is None else []))
        record_coverage("IRRADIANCE_OUTPUT_PHASE_MISMATCH", False, missing_phase, reason="missing_column")
    elif _to_w_m2(1.0, units.get("GlobInc", ""), step_hours) is None:
        record_coverage("IRRADIANCE_OUTPUT_PHASE_MISMATCH", False, ["GlobInc"], reason="unrecognized_unit")
    elif irrad_hour_den <= 0 or power_hour_den <= 0:
        record_coverage("IRRADIANCE_OUTPUT_PHASE_MISMATCH", False, ["GlobInc", phase_out_col], reason="no_denominator")
    else:
        record_coverage("IRRADIANCE_OUTPUT_PHASE_MISMATCH", True, ["GlobInc", phase_out_col])
    if irrad_hour_den > 0 and power_hour_den > 0:
        irrad_center, power_center = irrad_hour_num / irrad_hour_den, power_hour_num / power_hour_den
        lag = abs(power_center - irrad_center)
        if lag > cfg["phase_lag_max_hours"]:
            add("IRRADIANCE_OUTPUT_PHASE_MISMATCH", "high", "time_series", "solar_centroid_lag", lag, cfg["phase_lag_max_hours"], "hours",
                f"Output solar centroid ({power_center:.2f}h) differs from GlobInc centroid ({irrad_center:.2f}h) by {lag:.2f} hours.",
                "Possible extreme asymmetric shading, tracker stall, or weather/load timestamp shift.",
                ["Inspect hourly profile with pvsyst_read_rows and run pvsyst_review_weather."])

    counts_by_sev = {"critical": 0, "high": 0, "medium": 0, "info": 0}
    max_sev = "none"
    for item in findings:
        s = item["severity"]
        counts_by_sev[s] += 1
        if SEVERITY_ORDER[s] > SEVERITY_ORDER[max_sev]:
            max_sev = s
    needs_review = SEVERITY_ORDER[max_sev] >= SEVERITY_ORDER["high"]
    return {
        "status": "anomalies_detected" if needs_review else "ok",
        "max_severity": max_sev, "finding_count": len(findings), "counts_by_severity": counts_by_sev,
        "needs_review": needs_review, "rows": rows, "step_minutes": eff_step_min, "is_bifacial": is_bifacial,
        "weighted_pr": round(weighted_pr, 4) if weighted_pr is not None else None,
        "analyzed_columns": list(headers[1:]), "variant_attribution_used": vc_info is not None,
        "grid_limit_context": grid_limit_context,
        "evaluated_rule_count": sum(1 for item in coverage if item["evaluated"]),
        "skipped_rule_count": sum(1 for item in coverage if not item["evaluated"]),
        "coverage": coverage, "findings": findings,
    }


def compact_diagnostics_summary(diag: dict, *, max_findings: int = 5) -> dict:
    """Return a concise diagnostic summary suitable for embedding in high-level workflow results."""
    sorted_findings = sorted(diag.get("findings") or [], key=lambda f: -SEVERITY_ORDER.get(f.get("severity", "info"), 0))
    coverage = diag.get("coverage") or []
    summary = {
        "status": diag["status"], "max_severity": diag["max_severity"],
        "finding_count": diag["finding_count"], "needs_review": diag["needs_review"],
        "evaluated_rule_count": diag.get("evaluated_rule_count", sum(1 for item in coverage if item.get("evaluated"))),
        "skipped_rule_count": diag.get("skipped_rule_count", sum(1 for item in coverage if not item.get("evaluated"))),
        "top_findings": [
            {k: f[k] for k in ("code", "severity", "metric", "observed", "threshold", "unit", "message", "recommended_actions")}
            for f in sorted_findings[:max_findings]
        ],
    }
    if diag.get("issues"):
        summary["issues"] = list(diag["issues"])
    return summary


def diagnose_standalone(balance: dict[str, float], *, target_lol_pct: float = 5.0, has_genset: bool = False) -> dict:
    """Evaluate off-grid Stand-Alone PV+BESS (+ optional Genset) health & sizing diagnostics."""
    findings: list[dict] = []

    def add(code: str, severity: str, metric: str, observed: float, threshold: float, unit: str, message: str, action: str) -> None:
        findings.append({
            "code": code, "severity": severity, "category": "stand_alone_offgrid", "metric": metric,
            "observed": round(observed, 4), "threshold": round(threshold, 4), "unit": unit,
            "message": message, "recommended_actions": [action],
        })

    e_user, e_miss = float(balance.get("e_user_kwh", 0.0)), float(balance.get("e_missing_kwh", 0.0))
    e_avail, e_unused = float(balance.get("e_avail_kwh", 0.0)), float(balance.get("e_unused_kwh", 0.0))
    e_bkup, t_lol = float(balance.get("e_backup_kwh", 0.0)), float(balance.get("t_lol_hours", 0.0))
    lol_pct, soc_mean = float(balance.get("lol_pct", 0.0)), float(balance.get("soc_mean", 0.0))
    bkup_h = float(balance.get("backup_runtime_hours", 0.0))

    if lol_pct > target_lol_pct or e_miss > 0.0:
        sev = "high" if lol_pct > max(target_lol_pct, 2.0) else "medium"
        add("STANDALONE_LOL_HIGH", sev, "lol_pct", lol_pct, target_lol_pct, "%",
            f"Off-grid Loss of Load is {lol_pct:.2f}% ({t_lol:.1f} h/yr, missing {e_miss:.1f} kWh vs target {target_lol_pct:.1f}%).",
            "Increase PV array strings, expand battery parallel strings (n_batt_parall), or enable a backup .GEN generator.")
    if e_avail > 0.0 and (unused_pct := (e_unused / e_avail) * 100.0) > 25.0:
        add("STANDALONE_UNUSED_ENERGY_HIGH", "medium", "EUnused / E_Avail", unused_pct, 25.0, "%",
            f"Unused/curtailed PV energy is {unused_pct:.1f}% ({e_unused:.1f} kWh) due to full battery.",
            "Increase battery bank storage capacity or add daytime deferrable loads.")
    if 0.0 < soc_mean < 0.45:
        add("STANDALONE_SOC_LOW", "medium", "SOCmean", soc_mean * 100.0, 45.0, "%",
            f"Mean annual battery SOC is low ({soc_mean * 100.0:.1f}%), accelerating battery degradation.",
            "Increase PV nominal power or raise backup generator start threshold (v_bkup_encl).")
    if has_genset and e_user > 0.0:
        bkup_share_pct = (e_bkup / e_user) * 100.0
        if bkup_share_pct > 25.0 or bkup_h > 1500.0:
            add("STANDALONE_GENSET_RELIANCE_HIGH", "medium", "E_BkUp / E_User", bkup_share_pct, 25.0, "%",
                f"Backup generator supplies {bkup_share_pct:.1f}% of load ({e_bkup:.1f} kWh, {bkup_h:.1f} h/yr), increasing fuel OPEX.",
                "Upsize PV array and battery bank to improve solar fraction and cut diesel consumption.")

    counts = {sev: 0 for sev in SEVERITY_ORDER}
    for f in findings:
        counts[f["severity"]] += 1
    max_sev = max((f["severity"] for f in findings), key=lambda s: SEVERITY_ORDER[s], default="none")
    needs_review = any(f["severity"] in ("critical", "high", "medium") for f in findings)
    return {
        "status": "anomalies_detected" if needs_review else "ok",
        "max_severity": max_sev, "finding_count": len(findings),
        "counts_by_severity": counts, "needs_review": needs_review, "findings": findings,
    }

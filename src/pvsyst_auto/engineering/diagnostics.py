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
from pvsyst_auto.native.cli import iter_result_csv, result_units

SEVERITY_ORDER = {"none": 0, "info": 1, "medium": 2, "high": 3, "critical": 4}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "pr_min": 0.65,
    "pr_max": 0.94,
    "bifacial_pr_min": 0.70,
    "bifacial_pr_max": 1.12,
    "clipping_loss_max_pct": 3.0,
    "overvoltage_loss_max_kwh": 0.0,
    "undervoltage_loss_max_pct": 1.5,
    "current_limit_loss_max_pct": 1.0,
    "threshold_loss_max_pct": 2.0,
    "shading_loss_max_pct": 12.0,
    "electrical_shading_max_pct": 8.0,
    "soiling_loss_max_pct": 5.0,
    "curtailment_max_pct": 5.0,
    "unmet_load_max_pct": 10.0,
    "soc_pinned_max_pct": 35.0,
    "zero_output_irrad_min_w_m2": 100.0,
    "zero_output_max_hours": 24.0,
    "night_export_max_kwh": 1.0,
    "phase_lag_max_hours": 2.0,
}

_POWER_SCALE = {"W": 0.001, "kW": 1.0}
_ENERGY_SCALE = {"Wh": 0.001, "kWh": 1.0}
_APPARENT_POWER_SCALE = {"VA": 0.001, "kVA": 1.0}
_APPARENT_ENERGY_SCALE = {"VAh": 0.001, "kVAh": 1.0}
_IRRAD_SCALE = {
    "W/m2": 0.001, "W/m\u00b2": 0.001,
    "Wh/m2": 0.001, "Wh/m\u00b2": 0.001,
    "kW/m2": 1.0, "kW/m\u00b2": 1.0,
    "kWh/m2": 1.0, "kWh/m\u00b2": 1.0,
}
_PR_SCALE = {"ratio": 1.0, "%": 0.01}
_HEX_FLAG = re.compile(r"\$[0-9a-fA-F]{1,8}")
_LOAD_FLAG_BIT = 0x00080000
_GRID_LIMIT_ENABLED_BIT = 0x01000000
_GRID_LIMIT_APPARENT_BIT = 0x02000000


def detect_native_refusal(text: str) -> str | None:
    """Extract a native PVsystCLI refusal reason (e.g., 'Simulation cannot be run.')."""
    if not isinstance(text, str) or not text:
        return None
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if "simulation cannot be run." in line.casefold():
            collected = [line]
            for follow in lines[index + 1:]:
                if not follow or follow.startswith(("PVsystCLI", "Exiting", "Exit code")):
                    break
                collected.append(follow)
                if len(collected) >= 5:
                    break
            return " ".join(collected)
    return None


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
    if unit in _POWER_SCALE:
        return raw_sum * _POWER_SCALE[unit] * step_hours
    if unit in _ENERGY_SCALE:
        return raw_sum * _ENERGY_SCALE[unit]
    return None


def _to_kw(value: float, unit: str, step_hours: float) -> float | None:
    if unit in _POWER_SCALE:
        return value * _POWER_SCALE[unit]
    if unit in _ENERGY_SCALE and step_hours > 0:
        return value * _ENERGY_SCALE[unit] / step_hours
    return None


def _to_kva(value: float, unit: str, step_hours: float) -> float | None:
    if unit in _APPARENT_POWER_SCALE:
        return value * _APPARENT_POWER_SCALE[unit]
    if unit in _APPARENT_ENERGY_SCALE and step_hours > 0:
        return value * _APPARENT_ENERGY_SCALE[unit] / step_hours
    return None


def _to_kwh_m2(raw_sum: float, unit: str, step_hours: float) -> float | None:
    if unit in ("W/m2", "W/m\u00b2"):
        return raw_sum * 0.001 * step_hours
    if unit in ("kW/m2", "kW/m\u00b2"):
        return raw_sum * step_hours
    if unit in ("Wh/m2", "Wh/m\u00b2"):
        return raw_sum * 0.001
    if unit in ("kWh/m2", "kWh/m\u00b2"):
        return raw_sum
    return None


def _to_w_m2(value: float, unit: str) -> float | None:
    if unit in ("W/m2", "W/m\u00b2", "Wh/m2", "Wh/m\u00b2"):
        return value
    if unit in ("kW/m2", "kW/m\u00b2", "kWh/m2", "kWh/m\u00b2"):
        return value * 1000.0
    return None


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
            text = data.decode("utf-8-sig", errors="replace")
            lines = text.splitlines(keepends=True)
            has_pvload = any(line.strip() == "PVObject_UserNeeds=pvLoad" for line in lines)
            raw_flags = None
            load_flag_enabled = None
            try:
                _, raw_flags = root_block(lines).one("Flags")
                if _HEX_FLAG.fullmatch(raw_flags):
                    load_flag_enabled = bool(int(raw_flags[1:], 16) & _LOAD_FLAG_BIT)
            except (ValueError, KeyError):
                pass
            load_ctx = {
                "has_pvload": has_pvload,
                "root_flags": raw_flags,
                "load_flag_enabled": load_flag_enabled,
            }
        except OSError:
            load_ctx = None
    return vc_info, load_ctx


def _extract_grid_limit_context(
    vc_info: dict | None,
    *,
    peak_egrid_kw: float | None,
    peak_eap_kva: float | None,
) -> dict | None:
    """Resolve active dispatch cap for kW or kVA (apparent) PLimGrid variants."""
    if not vc_info:
        return None
    limit = None
    quantity = None
    tan_phi = None

    pc = vc_info.get("power_controls")
    if isinstance(pc, dict):
        gl = pc.get("grid_limit")
        pf = pc.get("power_factor")
        if isinstance(gl, dict) and gl.get("mode") == "constant" and isinstance(gl.get("limit"), (int, float)):
            limit = float(gl["limit"])
            quantity = gl.get("quantity") or "active"
        if isinstance(pf, dict) and pf.get("mode") == "constant":
            raw_tp = pf.get("tan_phi")
            if isinstance(raw_tp, (int, float)) and math.isfinite(raw_tp):
                tan_phi = float(raw_tp)

    if limit is None:
        sys_params = vc_info.get("system_parameters") or {}
        raw_flags = sys_params.get("Flags")
        raw_lim = sys_params.get("PLimGrid")
        if isinstance(raw_flags, str) and _HEX_FLAG.fullmatch(raw_flags) and raw_lim is not None:
            sys_flags = int(raw_flags[1:], 16)
            if sys_flags & _GRID_LIMIT_ENABLED_BIT:
                try:
                    parsed_lim = float(raw_lim)
                except (TypeError, ValueError):
                    parsed_lim = None
                if parsed_lim is not None and math.isfinite(parsed_lim) and parsed_lim > 0:
                    limit = parsed_lim
                    quantity = "apparent" if (sys_flags & _GRID_LIMIT_APPARENT_BIT) else "active"
        if tan_phi is None and sys_params.get("TanPhi") is not None:
            try:
                parsed_tp = float(sys_params["TanPhi"])
                if math.isfinite(parsed_tp):
                    tan_phi = parsed_tp
            except (TypeError, ValueError):
                pass
        if tan_phi is None:
            pf_state = vc_info.get("power_factor")
            if isinstance(pf_state, dict) and pf_state.get("mode") == "constant":
                raw_tp = pf_state.get("tan_phi")
                if isinstance(raw_tp, (int, float)) and math.isfinite(raw_tp):
                    tan_phi = float(raw_tp)

    if limit is None or not math.isfinite(limit) or limit <= 0:
        return None
    eff_tan_phi = tan_phi if tan_phi is not None else 0.0
    cos_phi = 1.0 / math.sqrt(1.0 + eff_tan_phi * eff_tan_phi)
    active_cap_kw = limit * cos_phi if quantity == "apparent" else limit
    return {
        "enabled": True,
        "quantity": quantity,
        "configured_limit": round(limit, 4),
        "configured_unit": "kVA" if quantity == "apparent" else "kW",
        "tan_phi": round(eff_tan_phi, 6),
        "cos_phi": round(cos_phi, 6),
        "active_dispatch_cap_kw": round(active_cap_kw, 4),
        "peak_e_grid_kw": round(peak_egrid_kw, 4) if peak_egrid_kw is not None else None,
        "peak_eap_grid_kva": round(peak_eap_kva, 4) if peak_eap_kva is not None else None,
        "active_cap_binding": bool(
            peak_egrid_kw is not None and active_cap_kw > 0 and peak_egrid_kw >= active_cap_kw * 0.995
        ),
        "apparent_exceeds_configured_is_expected": quantity == "apparent",
    }


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
    units = result_units(path, strict=True)

    # Accumulators
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    weighted_pr_num = 0.0
    weighted_pr_den = 0.0
    peak_egrid_raw: float | None = None
    peak_eap_raw: float | None = None
    egrid_exceeds_earray_count = 0
    egrid_exceeds_stamps: list[str] = []
    night_export_kwh_raw = 0.0
    night_export_count = 0
    night_export_stamps: list[str] = []
    zero_out_count = 0
    zero_out_stamps: list[str] = []
    soc_low_count = 0
    soc_high_count = 0
    soc_total = 0
    # Phase alignment (solar-weighted mean hour of day for GlobInc vs positive E_Grid/EArray)
    irrad_hour_num = 0.0
    irrad_hour_den = 0.0
    power_hour_num = 0.0
    power_hour_den = 0.0

    headers: list[str] = []
    idx: dict[str, int] = {}
    rows = 0
    prev_stamp = None
    step_minutes: float | None = None
    regular = True

    for headers, row in iter_result_csv(path, strict=True):
        if rows == 0:
            idx = {name: i for i, name in enumerate(headers) if i > 0}
            for name in idx:
                sums[name] = 0.0
                counts[name] = 0
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
            val = row[col_i]
            if isinstance(val, (int, float)) and math.isfinite(val):
                sums[name] += float(val)
                counts[name] += 1

        gi_raw = row[idx["GlobInc"]] if "GlobInc" in idx else None
        gi_w_m2 = (
            _to_w_m2(float(gi_raw), units.get("GlobInc", ""))
            if isinstance(gi_raw, (int, float)) and math.isfinite(gi_raw) else None
        )
        eg_raw = row[idx["E_Grid"]] if "E_Grid" in idx else None
        ea_raw = row[idx["EArray"]] if "EArray" in idx else None
        eap_raw = row[idx["EApGrid"]] if "EApGrid" in idx else None
        eg_ok = isinstance(eg_raw, (int, float)) and math.isfinite(eg_raw)
        ea_ok = isinstance(ea_raw, (int, float)) and math.isfinite(ea_raw)
        eap_ok = isinstance(eap_raw, (int, float)) and math.isfinite(eap_raw)
        if eg_ok and (peak_egrid_raw is None or float(eg_raw) > peak_egrid_raw):
            peak_egrid_raw = float(eg_raw)
        if eap_ok and (peak_eap_raw is None or float(eap_raw) > peak_eap_raw):
            peak_eap_raw = float(eap_raw)
        pr_raw = row[idx["PR"]] if "PR" in idx else None
        pr_scale = _PR_SCALE.get(units.get("PR", ""))

        if gi_w_m2 is not None and gi_w_m2 > 0:
            if isinstance(pr_raw, (int, float)) and math.isfinite(pr_raw) and pr_scale is not None:
                weighted_pr_num += gi_w_m2 * float(pr_raw) * pr_scale
                weighted_pr_den += gi_w_m2
            hour_of_day = stamp.hour + stamp.minute / 60.0
            irrad_hour_num += gi_w_m2 * hour_of_day
            irrad_hour_den += gi_w_m2

        # Output power for phase check (prefer EArray when present so storage discharge doesn't skew phase)
        phase_p = float(ea_raw) if ea_ok else (float(eg_raw) if eg_ok else None)
        if phase_p is not None and phase_p > 0:
            hour_of_day = stamp.hour + stamp.minute / 60.0
            power_hour_num += phase_p * hour_of_day
            power_hour_den += phase_p

        # E_Grid > EArray (skip if storage discharge is active)
        bat_dis = row[idx["EBatDis"]] if "EBatDis" in idx else 0.0
        has_bat_dis = isinstance(bat_dis, (int, float)) and math.isfinite(bat_dis) and bat_dis > 1e-6
        if eg_ok and ea_ok and not has_bat_dis:
            eg_u, ea_u = units.get("E_Grid", ""), units.get("EArray", "")
            if eg_u == ea_u and float(eg_raw) > float(ea_raw) + 1e-3 and float(eg_raw) > 0.01:
                egrid_exceeds_earray_count += 1
                if len(egrid_exceeds_stamps) < 5:
                    egrid_exceeds_stamps.append(str(stamp))

        # Nighttime positive export (without battery discharge)
        if gi_w_m2 is not None and gi_w_m2 <= 0.1 and eg_ok and float(eg_raw) > 1e-4 and not has_bat_dis:
            night_export_kwh_raw += float(eg_raw)
            night_export_count += 1
            if len(night_export_stamps) < 5:
                night_export_stamps.append(str(stamp))

        # High irradiance with zero/negative output
        if gi_w_m2 is not None and gi_w_m2 >= cfg["zero_output_irrad_min_w_m2"]:
            out_val = float(ea_raw) if ea_ok else (float(eg_raw) if eg_ok else None)
            if out_val is not None and out_val <= 1e-6:
                zero_out_count += 1
                if len(zero_out_stamps) < 5:
                    zero_out_stamps.append(str(stamp))

        # Storage SOC bounds
        soc_col = "SOC_End" if "SOC_End" in idx else ("SOC" if "SOC" in idx else None)
        if soc_col is not None:
            soc_val = row[idx[soc_col]]
            if isinstance(soc_val, (int, float)) and math.isfinite(soc_val):
                soc_norm = float(soc_val) * (0.01 if units.get(soc_col) == "%" else 1.0)
                soc_total += 1
                if soc_norm <= 0.05:
                    soc_low_count += 1
                elif soc_norm >= 0.95:
                    soc_high_count += 1

    if rows == 0:
        raise ValueError("Result CSV contains no simulation rows")

    eff_step_min = step_minutes if (regular and step_minutes is not None) else 60.0
    step_hours = eff_step_min / 60.0

    # Detect bifacial presence from CSV columns or .VC0
    vc_bifacial = False
    if vc_info is not None:
        vc_bifacial = any((o.get("bifacial") or {}).get("enabled") for o in (vc_info.get("orientations") or []))
    is_bifacial = vc_bifacial or ("GlobBak" in idx and sums.get("GlobBak", 0.0) > 0)

    # Extract key .VC0 parameters for root-cause attribution
    sub0 = (vc_info.get("subarrays") or [{}])[0].get("values", {}) if vc_info else {}
    orient0 = (vc_info.get("orientations") or [{}])[0].get("values", {}) if vc_info else {}
    has_planned_unavail = bool(
        vc_info and (
            (vc_info.get("system_unavailability") or {}).get("configured")
            or (vc_info.get("detailed_losses") or {}).get("system_unavailability", {}).get("enabled")
        )
    )
    peak_egrid_kw = (
        _to_kw(peak_egrid_raw, units.get("E_Grid", ""), step_hours)
        if peak_egrid_raw is not None and "E_Grid" in idx else None
    )
    peak_eap_kva = (
        _to_kva(peak_eap_raw, units.get("EApGrid", ""), step_hours)
        if peak_eap_raw is not None and "EApGrid" in idx else None
    )
    grid_limit_context = _extract_grid_limit_context(
        vc_info, peak_egrid_kw=peak_egrid_kw, peak_eap_kva=peak_eap_kva,
    )

    findings: list[dict] = []

    def add(
        *,
        code: str,
        severity: str,
        category: str,
        metric: str,
        observed: float,
        threshold: float,
        unit: str,
        message: str,
        root_cause_hint: str,
        recommended_actions: list[str],
        affected_samples: int = 0,
        sample_timestamps: list[str] | None = None,
    ) -> None:
        findings.append({
            "code": code,
            "severity": severity,
            "category": category,
            "metric": metric,
            "observed": round(float(observed), 4),
            "threshold": round(float(threshold), 4),
            "unit": unit,
            "affected_samples": affected_samples,
            "sample_timestamps": list(sample_timestamps or []),
            "message": message,
            "root_cause_hint": root_cause_hint,
            "recommended_actions": recommended_actions,
        })

    earray_kwh = _to_kwh(sums["EArray"], units.get("EArray", ""), step_hours) if "EArray" in idx else None
    egrid_kwh = _to_kwh(sums["E_Grid"], units.get("E_Grid", ""), step_hours) if "E_Grid" in idx else None
    globinc_kwh_m2 = _to_kwh_m2(sums["GlobInc"], units.get("GlobInc", ""), step_hours) if "GlobInc" in idx else None

    # --- 1. PR & Energy Integrity ---
    weighted_pr = (weighted_pr_num / weighted_pr_den) if weighted_pr_den > 0 else None
    if weighted_pr is not None:
        pr_min = cfg["bifacial_pr_min"] if is_bifacial else cfg["pr_min"]
        pr_max = cfg["bifacial_pr_max"] if is_bifacial else cfg["pr_max"]
        if weighted_pr < pr_min:
            sev = "critical" if weighted_pr < 0.50 else "high"
            ctx = f" (NModSerie={sub0.get('NModSerie')}, NStringCh={sub0.get('NStringCh')})" if sub0 else ""
            add(
                code="PR_TOO_LOW",
                severity=sev,
                category="pr_integrity",
                metric="PR",
                observed=weighted_pr,
                threshold=pr_min,
                unit="ratio",
                message=f"Irradiance-weighted PR ({weighted_pr:.4f}) is below expected minimum ({pr_min:.2f}){ctx}.",
                root_cause_hint="Severe inverter clipping/overvoltage, heavy shading, curtailment, or mis-sized strings/MPPT.",
                recommended_actions=[
                    "Inspect IL_Vmax, IL_Pmax, GlobShd/ShdElec, and EGrdLim columns.",
                    "Check string length (NModSerie), DC/AC ratio, and shading geometry in .VC0.",
                ],
            )
        elif weighted_pr > pr_max:
            add(
                code="PR_TOO_HIGH",
                severity="high",
                category="pr_integrity",
                metric="PR",
                observed=weighted_pr,
                threshold=pr_max,
                unit="ratio",
                message=f"Irradiance-weighted PR ({weighted_pr:.4f}) exceeds physical upper bound ({pr_max:.2f}).",
                root_cause_hint="Check module nameplate PNom, bifacial setting, or unexpected storage discharge/weather mismatch.",
                recommended_actions=[
                    "Verify PAN PNom and array module count match nominal DC power.",
                    "Confirm whether bifaciality or temperature-corrected PR (PRCorrTemp) is enabled.",
                ],
            )

    if egrid_exceeds_earray_count > 0:
        add(
            code="E_GRID_EXCEEDS_EARRAY",
            severity="high",
            category="pr_integrity",
            metric="E_Grid - EArray",
            observed=float(egrid_exceeds_earray_count),
            threshold=0.0,
            unit="samples",
            affected_samples=egrid_exceeds_earray_count,
            sample_timestamps=egrid_exceeds_stamps,
            message=f"E_Grid exceeded EArray on {egrid_exceeds_earray_count} samples without battery discharge.",
            root_cause_hint="Possible unit mismatch or non-PV AC generation source active in the system.",
            recommended_actions=["Verify SFI column units and inspect pvSystem for backup generator or storage."],
        )

    if night_export_count > 0 and "E_Grid" in idx:
        night_kwh = _to_kwh(night_export_kwh_raw, units.get("E_Grid", ""), step_hours) or 0.0
        if night_kwh > cfg["night_export_max_kwh"]:
            add(
                code="NIGHTTIME_EXPORT",
                severity="high",
                category="pr_integrity",
                metric="E_Grid(night)",
                observed=night_kwh,
                threshold=cfg["night_export_max_kwh"],
                unit="kWh",
                affected_samples=night_export_count,
                sample_timestamps=night_export_stamps,
                message=f"Positive grid export ({night_kwh:.2f} kWh across {night_export_count} samples) occurred at zero irradiance.",
                root_cause_hint="Weather file timezone/timestamp misalignment or unlogged battery/generator discharge.",
                recommended_actions=["Run pvsyst_review_weather to verify MET timezone/LegalTime alignment."],
            )

    # --- 2. Inverter Limit Losses ---
    base_kwh = earray_kwh if (earray_kwh is not None and earray_kwh > 0) else egrid_kwh
    if "IL_Vmax" in idx:
        il_vmax_kwh = _to_kwh(sums["IL_Vmax"], units.get("IL_Vmax", ""), step_hours) or 0.0
        if il_vmax_kwh > cfg["overvoltage_loss_max_kwh"]:
            n_ser = sub0.get("NModSerie")
            hint = (f"String voltage exceeded inverter VMPPMax/VAbsMax (current NModSerie={n_ser})."
                    if n_ser else "String open-circuit or MPP voltage exceeded inverter upper voltage limit.")
            add(
                code="INVERTER_OVERVOLTAGE",
                severity="critical" if (base_kwh and il_vmax_kwh / base_kwh > 0.01) else "high",
                category="inverter_limits",
                metric="IL_Vmax",
                observed=il_vmax_kwh,
                threshold=cfg["overvoltage_loss_max_kwh"],
                unit="kWh",
                message=f"Inverter over-voltage loss IL_Vmax = {il_vmax_kwh:.2f} kWh detected.",
                root_cause_hint=hint,
                recommended_actions=[
                    "Reduce NModSerie via pvsyst_update_variant_parameters (or re-run pvsyst_suggest_sizing with colder min_ambient_c).",
                ],
            )

    if base_kwh and base_kwh > 0:
        for col, code, thresh_key, sev, desc, action in (
            (
                "IL_Pmax", "INVERTER_CLIPPING_HIGH", "clipping_loss_max_pct", "medium",
                "Inverter overpower clipping loss (IL_Pmax)",
                "Review DC/AC ratio (NInverter / NStringCh / GInverter) or evaluate PeakShaving storage.",
            ),
            (
                "IL_Vmin", "INVERTER_UNDERVOLTAGE", "undervoltage_loss_max_pct", "high",
                "Inverter under-voltage loss (IL_Vmin)",
                "Increase NModSerie so hot string Vmp stays above inverter VMppMin.",
            ),
            (
                "IL_Imax", "INVERTER_CURRENT_LIMIT", "current_limit_loss_max_pct", "high",
                "Inverter current-limit loss (IL_Imax)",
                "Reduce parallel strings per MPPT or select an inverter with higher IDCMax.",
            ),
            (
                "IL_Pmin", "INVERTER_THRESHOLD_LOSS", "threshold_loss_max_pct", "medium",
                "Inverter power-threshold loss (IL_Pmin)",
                "Verify inverter is not oversized relative to installed DC capacity.",
            ),
        ):
            if col in idx:
                loss_kwh = _to_kwh(sums[col], units.get(col, ""), step_hours)
                if loss_kwh is not None:
                    pct = (loss_kwh / base_kwh) * 100.0
                    if pct > cfg[thresh_key]:
                        final_sev = "high" if (col == "IL_Pmax" and pct > cfg[thresh_key] * 2.5) else sev
                        extra = f" (NInverter={sub0.get('NInverter')}, NModSerie={sub0.get('NModSerie')})" if sub0 else ""
                        add(
                            code=code,
                            severity=final_sev,
                            category="inverter_limits",
                            metric=f"{col} / EArray",
                            observed=pct,
                            threshold=cfg[thresh_key],
                            unit="%",
                            message=f"{desc} is {pct:.2f}% ({loss_kwh:.1f} kWh), above threshold {cfg[thresh_key]:.2f}%{extra}.",
                            root_cause_hint=f"{col} accounts for {pct:.2f}% of array energy.",
                            recommended_actions=[action],
                        )

    # --- 3. Shading, Soiling & Grid Curtailment ---
    before = "GlobHrz" if counts.get("GlobHrz") == rows else "GlobInc"
    if all(counts.get(col) == rows for col in (before, "GlobShd")):
        before_shd = _to_kwh_m2(sums[before], units.get(before, ""), step_hours)
        after_shd = _to_kwh_m2(sums["GlobShd"], units.get("GlobShd", ""), step_hours)
        if before_shd and after_shd is not None:
            shd_kwh_m2 = before_shd - after_shd
            pct = shd_kwh_m2 / before_shd * 100.0
            if pct > cfg["shading_loss_max_pct"]:
                pitch_note = f" (FieldType={orient0.get('FieldType')}, Pitch={orient0.get('Pitch')})" if orient0 else ""
                add(
                    code="SHADING_LOSS_HIGH",
                    severity="high" if pct > cfg["shading_loss_max_pct"] * 2 else "medium",
                    category="shading_soiling_curtailment",
                    metric=f"({before} - GlobShd) / {before}",
                    observed=pct,
                    threshold=cfg["shading_loss_max_pct"],
                    unit="%",
                    message=f"Near-shading irradiance loss is {pct:.2f}% ({abs(shd_kwh_m2):.1f} kWh/m2){pitch_note}.",
                    root_cause_hint="Row pitch too tight (high GCR), steep tilt, or nearby 3D obstacles casting shadows.",
                    recommended_actions=[
                        "Increase row pitch (lower GCR), enable backtracking on trackers, or adjust 3D obstacle clearance.",
                    ],
                )

    if "ShdElec" in idx and base_kwh and base_kwh > 0:
        shd_elec_kwh = _to_kwh(sums["ShdElec"], units.get("ShdElec", ""), step_hours)
        if shd_elec_kwh is not None:
            pct = (abs(shd_elec_kwh) / base_kwh) * 100.0
            if pct > cfg["electrical_shading_max_pct"]:
                add(
                    code="ELECTRICAL_SHADING_HIGH",
                    severity="high",
                    category="shading_soiling_curtailment",
                    metric="ShdElec / EArray",
                    observed=pct,
                    threshold=cfg["electrical_shading_max_pct"],
                    unit="%",
                    message=f"Electrical shading mismatch loss ShdElec is {pct:.2f}% ({abs(shd_elec_kwh):.1f} kWh).",
                    root_cause_hint="Partial shading hits across module strings in module_layout / string wiring.",
                    recommended_actions=[
                        "Re-group shaded modules onto dedicated strings/MPPTs or switch table orientation (portrait vs landscape).",
                    ],
                )

    if "SlgLoss" in idx and globinc_kwh_m2 and globinc_kwh_m2 > 0:
        slg_kwh_m2 = _to_kwh_m2(sums["SlgLoss"], units.get("SlgLoss", ""), step_hours)
        if slg_kwh_m2 is not None:
            pct = (abs(slg_kwh_m2) / globinc_kwh_m2) * 100.0
            if pct > cfg["soiling_loss_max_pct"]:
                add(
                    code="SOILING_LOSS_HIGH",
                    severity="medium",
                    category="shading_soiling_curtailment",
                    metric="SlgLoss / GlobInc",
                    observed=pct,
                    threshold=cfg["soiling_loss_max_pct"],
                    unit="%",
                    message=f"Soiling loss is {pct:.2f}%, exceeding {cfg['soiling_loss_max_pct']:.1f}%.",
                    root_cause_hint="High constant or monthly soiling loss factors configured in .VC0.",
                    recommended_actions=["Review soiling_updates in pvsyst_update_variant_parameters."],
                )

    for curt_col in ("EGrdLim", "EUnused"):
        if curt_col in idx and base_kwh and base_kwh > 0:
            curt_kwh = _to_kwh(sums[curt_col], units.get(curt_col, ""), step_hours)
            if curt_kwh is not None:
                pct = (abs(curt_kwh) / base_kwh) * 100.0
                if pct > cfg["curtailment_max_pct"]:
                    if grid_limit_context and grid_limit_context["quantity"] == "apparent":
                        hint = (
                            f"Apparent grid limit {grid_limit_context['configured_limit']:g} kVA "
                            f"with cos(phi)={grid_limit_context['cos_phi']:.4f} caps active dispatch at "
                            f"{grid_limit_context['active_dispatch_cap_kw']:.2f} kW "
                            f"(not {grid_limit_context['configured_limit']:g} kW); native EApGrid can "
                            f"legitimately exceed {grid_limit_context['configured_limit']:g} kVA."
                        )
                    else:
                        hint = "Active grid injection limit (PLimGrid) or full battery preventing PV export."
                    add(
                        code="GRID_CURTAILMENT_HIGH",
                        severity="medium",
                        category="shading_soiling_curtailment",
                        metric=f"{curt_col} / EArray",
                        observed=pct,
                        threshold=cfg["curtailment_max_pct"],
                        unit="%",
                        message=f"Grid limit / unused energy ({curt_col}) is {pct:.2f}% ({abs(curt_kwh):.1f} kWh).",
                        root_cause_hint=hint,
                        recommended_actions=[
                            "Evaluate PeakShaving or larger battery capacity via pvsyst_create_grid_storage_system.",
                        ],
                    )
                    break

    # --- 4. Storage & Load Balance ---
    if load_ctx and load_ctx.get("has_pvload") and load_ctx.get("load_flag_enabled") is False:
        load_cols_present = any(col in idx for col in ("E_User", "E_Solar", "EFrGrid"))
        e_user_total = (
            _to_kwh(sums["E_User"], units.get("E_User", ""), step_hours)
            if "E_User" in idx else None
        )
        if (not load_cols_present) or (e_user_total is not None and abs(e_user_total) <= 1e-6):
            add(
                code="LOAD_NOT_ENABLED",
                severity="high",
                category="storage_balance",
                metric="root_flags_bit19",
                observed=0.0,
                threshold=1.0,
                unit="flag",
                message=(
                    "Variant embeds PVObject_UserNeeds=pvLoad, but root pvVCalcul.Flags "
                    f"({load_ctx.get('root_flags')}) has bit 19 ($00080000) clear and no load energy was consumed."
                ),
                root_cause_hint=(
                    "PVsystCLI ignores PVObject_UserNeeds=pvLoad unless root pvVCalcul.Flags sets bit 19 ($00080000)."
                ),
                recommended_actions=[
                    "Rebind the hourly load via pvsyst_create_native_load_variant (sets bit 19 $00080000) and re-run.",
                ],
            )
    if "E_Miss" in idx and "E_User" in idx:
        miss_kwh = _to_kwh(sums["E_Miss"], units.get("E_Miss", ""), step_hours)
        user_kwh = _to_kwh(sums["E_User"], units.get("E_User", ""), step_hours)
        if miss_kwh is not None and user_kwh and user_kwh > 0:
            pct = (miss_kwh / user_kwh) * 100.0
            if pct > cfg["unmet_load_max_pct"]:
                add(
                    code="UNMET_LOAD_HIGH",
                    severity="high" if pct > 25.0 else "medium",
                    category="storage_balance",
                    metric="E_Miss / E_User",
                    observed=pct,
                    threshold=cfg["unmet_load_max_pct"],
                    unit="%",
                    message=f"Unmet load E_Miss is {pct:.2f}% ({miss_kwh:.1f} / {user_kwh:.1f} kWh).",
                    root_cause_hint="Battery capacity, discharge power limit, or PV sizing insufficient for load.",
                    recommended_actions=["Review battery parallel_count, max_discharge_ac_kw, or WeakGrid/PowerShift schedule."],
                )

    if soc_total > 0:
        low_pct = (soc_low_count / soc_total) * 100.0
        high_pct = (soc_high_count / soc_total) * 100.0
        if low_pct > cfg["soc_pinned_max_pct"]:
            add(
                code="SOC_PINNED_LOW",
                severity="medium",
                category="storage_balance",
                metric="SOC <= 5%",
                observed=low_pct,
                threshold=cfg["soc_pinned_max_pct"],
                unit="%",
                affected_samples=soc_low_count,
                message=f"Battery SOC stayed <= 5% for {low_pct:.1f}% of the simulation ({soc_low_count} samples).",
                root_cause_hint="Storage is under-charged relative to load or charging strategy is too restrictive.",
                recommended_actions=["Check charging_strategy, max_charge_ac_kw, or PV-to-load ratio."],
            )
        if high_pct > cfg["soc_pinned_max_pct"]:
            add(
                code="SOC_PINNED_HIGH",
                severity="medium",
                category="storage_balance",
                metric="SOC >= 95%",
                observed=high_pct,
                threshold=cfg["soc_pinned_max_pct"],
                unit="%",
                affected_samples=soc_high_count,
                message=f"Battery SOC stayed >= 95% for {high_pct:.1f}% of the simulation ({soc_high_count} samples).",
                root_cause_hint="Battery rarely discharges (e.g., restrictive discharge window or oversized battery).",
                recommended_actions=["Check discharging_strategy, discharge_start_hour/end_hour, or battery sizing."],
            )

    # --- 5. Time-Series Anomalies ---
    zero_out_hours = zero_out_count * step_hours
    if zero_out_hours > cfg["zero_output_max_hours"]:
        sev = "info" if has_planned_unavail else ("critical" if zero_out_hours > 200 else "high")
        hint = (
            "Planned SystemUnavail periods are active in .VC0; verify timestamps match scheduled outages."
            if has_planned_unavail else
            "Prolonged zero output during high irradiance (>= 100 W/m2) indicates inverter shutdown (Voc/Vmp out of range) or unscheduled outage."
        )
        add(
            code="HIGH_IRRADIANCE_ZERO_OUTPUT",
            severity=sev,
            category="time_series",
            metric="zero_output_hours",
            observed=zero_out_hours,
            threshold=cfg["zero_output_max_hours"],
            unit="hours",
            affected_samples=zero_out_count,
            sample_timestamps=zero_out_stamps,
            message=f"{zero_out_hours:.1f} hours had GlobInc >= {cfg['zero_output_irrad_min_w_m2']:g} W/m2 with zero output.",
            root_cause_hint=hint,
            recommended_actions=[
                "Inspect sample_timestamps via pvsyst_read_rows alongside IL_Vmax, IL_Vmin, and SystemUnavail.",
            ],
        )

    if irrad_hour_den > 0 and power_hour_den > 0:
        irrad_center = irrad_hour_num / irrad_hour_den
        power_center = power_hour_num / power_hour_den
        lag = abs(power_center - irrad_center)
        if lag > cfg["phase_lag_max_hours"]:
            add(
                code="IRRADIANCE_OUTPUT_PHASE_MISMATCH",
                severity="high",
                category="time_series",
                metric="solar_centroid_lag",
                observed=lag,
                threshold=cfg["phase_lag_max_hours"],
                unit="hours",
                message=(
                    f"Output solar centroid ({power_center:.2f}h) differs from GlobInc centroid "
                    f"({irrad_center:.2f}h) by {lag:.2f} hours."
                ),
                root_cause_hint="Possible extreme asymmetric shading, tracker stall, or weather/load timestamp shift.",
                recommended_actions=["Inspect hourly profile with pvsyst_read_rows and run pvsyst_review_weather."],
            )

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
        "max_severity": max_sev,
        "finding_count": len(findings),
        "counts_by_severity": counts_by_sev,
        "needs_review": needs_review,
        "rows": rows,
        "step_minutes": eff_step_min if regular else None,
        "is_bifacial": is_bifacial,
        "weighted_pr": round(weighted_pr, 4) if weighted_pr is not None else None,
        "analyzed_columns": [h for h in headers[1:]],
        "variant_attribution_used": vc_info is not None,
        "grid_limit_context": grid_limit_context,
        "findings": findings,
    }


def compact_diagnostics_summary(diag: dict, *, max_findings: int = 5) -> dict:
    """Return a concise diagnostic summary suitable for embedding in high-level workflow results."""
    sorted_findings = sorted(
        diag.get("findings") or [],
        key=lambda f: -SEVERITY_ORDER.get(f.get("severity", "info"), 0),
    )
    return {
        "status": diag["status"],
        "max_severity": diag["max_severity"],
        "finding_count": diag["finding_count"],
        "needs_review": diag["needs_review"],
        "top_findings": [
            {
                "code": f["code"],
                "severity": f["severity"],
                "metric": f["metric"],
                "observed": f["observed"],
                "threshold": f["threshold"],
                "unit": f["unit"],
                "message": f["message"],
                "recommended_actions": f["recommended_actions"],
            }
            for f in sorted_findings[:max_findings]
        ],
    }

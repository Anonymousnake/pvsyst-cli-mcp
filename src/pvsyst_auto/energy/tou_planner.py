"""Deterministic TOU tariff -> PVsyst PowerShift 48-slot charge/discharge schedule planner.

Generates two-charge/two-discharge (or single-cycle) half-hourly 48-slot schedules ready to pass
directly into ``pvsyst_create_grid_storage_system`` or ``pvsyst_create_grid_storage_variant``.
"""
from __future__ import annotations

import math
from typing import Any

from pvsyst_auto.engineering.economics import _validate_tariff
from pvsyst_auto.validation import pos_finite as _pos_finite


def _resolve_hourly_rates(tariff: dict, *, month: int, day_type: str) -> list[dict[str, Any]]:
    is_weekend = day_type == "weekend"
    resolved: list[dict[str, Any]] = []
    for hour in range(24):
        matched_name = "default"
        matched_imp = tariff["import_tariff"]
        matched_exp = tariff["feed_in_tariff"]
        for period in tariff["tou_schedule"]:
            if period["months"] is not None and month not in period["months"]:
                continue
            if period["day_type"] == "weekday" and is_weekend:
                continue
            if period["day_type"] == "weekend" and not is_weekend:
                continue
            if hour in period["hours"]:
                matched_name = period["name"]
                matched_imp = period["import_rate"]
                matched_exp = period["export_rate"]
                break
        resolved.append({
            "hour": hour,
            "period": matched_name,
            "import_rate": round(matched_imp, 6),
            "export_rate": round(matched_exp, 6),
        })
    return resolved


def _segment_day(hourly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive hours with identical import_rate into contiguous segments."""
    segments: list[dict[str, Any]] = []
    for rec in hourly:
        if segments and abs(segments[-1]["rate"] - rec["import_rate"]) < 1e-9:
            segments[-1]["hours"].append(rec["hour"])
        else:
            segments.append({
                "period": rec["period"],
                "rate": rec["import_rate"],
                "hours": [rec["hour"]],
            })
    return segments


def plan_tou_storage_schedule(
    *,
    tou_schedule: list[dict[str, Any]],
    charge_power_kw: float,
    discharge_power_kw: float | None = None,
    usable_energy_kwh: float | None = None,
    cycles_per_day: int = 2,
    round_trip_efficiency: float = 0.88,
    min_spread: float = 0.05,
    month: int = 7,
    day_type: str = "weekday",
    charging_strategy: str = "spec_charg_power",
    default_import_rate: float = 0.0,
    default_export_rate: float = 0.0,
) -> dict[str, Any]:
    """Derive a PVsyst PowerShift 48-slot (half-hour) schedule from a TOU tariff schedule."""
    ch_kw = _pos_finite(charge_power_kw, "charge_power_kw")
    dis_kw = _pos_finite(discharge_power_kw, "discharge_power_kw") if discharge_power_kw is not None else ch_kw
    usable_kwh = _pos_finite(usable_energy_kwh, "usable_energy_kwh") if usable_energy_kwh is not None else None
    if type(cycles_per_day) is not int or cycles_per_day not in (1, 2):
        raise ValueError("cycles_per_day must be 1 or 2")
    rte = _pos_finite(round_trip_efficiency, "round_trip_efficiency")
    if not 0.5 <= rte <= 1.0:
        raise ValueError("round_trip_efficiency must be in [0.5, 1.0]")
    min_sp = _pos_finite(min_spread, "min_spread", allow_zero=True)
    if type(month) is not int or not 1 <= month <= 12:
        raise ValueError("month must be an integer in 1..12")
    if day_type not in ("all", "weekday", "weekend"):
        raise ValueError("day_type must be 'all', 'weekday', or 'weekend'")
    if charging_strategy not in ("spec_charg_power", "pv_prod_fraction"):
        raise ValueError("charging_strategy must be 'spec_charg_power' or 'pv_prod_fraction'")

    tar = _validate_tariff({
        "mode": "tou",
        "import_tariff": default_import_rate,
        "feed_in_tariff": default_export_rate,
        "tou_schedule": tou_schedule,
    })
    hourly = _resolve_hourly_rates(tar, month=month, day_type=day_type)
    rates = [h["import_rate"] for h in hourly]
    segments = _segment_day(hourly)

    min_rate = min(rates)
    max_rate = max(rates)
    warnings: list[str] = []

    raw_cycles: list[dict[str, Any]] = []
    if max_rate - (min_rate / rte) < min_sp:
        warnings.append(
            f"Maximum effective spread ({max_rate - min_rate / rte:.4f}) is below min_spread ({min_sp:.4f}); "
            "returning idle 48-slot schedules to avoid unprofitable cycling."
        )
    else:
        n_seg = len(segments)
        peak_indices = [
            i for i, seg in enumerate(segments)
            if seg["rate"] >= min_rate / rte + min_sp
            and (i == 0 or seg["rate"] >= segments[i - 1]["rate"])
            and (i == n_seg - 1 or seg["rate"] >= segments[i + 1]["rate"])
        ]
        if not peak_indices:
            peak_indices = [i for i, seg in enumerate(segments) if abs(seg["rate"] - max_rate) < 1e-9]

        chosen_peaks = sorted(
            sorted(peak_indices, key=lambda i: (-segments[i]["rate"], segments[i]["hours"][0]))[:cycles_per_day]
        )

        used_hours: set[int] = set()
        for peak_idx in chosen_peaks:
            used_hours.update(segments[peak_idx]["hours"])

        prev_peak_idx = -1
        for peak_idx in chosen_peaks:
            peak_seg = segments[peak_idx]
            # Search preceding segments strictly between prev_peak_idx and peak_idx for cheapest charging window
            cand_indices = [
                j for j in range(prev_peak_idx + 1, peak_idx)
                if not (used_hours & set(segments[j]["hours"]))
            ]
            # If the first peak is at segment 0 (hour 0), only allow trailing late-night wrap-around
            # segments strictly after the last chosen peak (never daytime segments between peaks).
            if not cand_indices and prev_peak_idx == -1:
                wrap_start = (chosen_peaks[-1] + 1) if chosen_peaks else (peak_idx + 1)
                cand_indices = [
                    j for j in range(wrap_start, n_seg)
                    if not (used_hours & set(segments[j]["hours"]))
                ]
            best_ch_idx = None
            best_spread = -1e9
            for j in cand_indices:
                spread = peak_seg["rate"] - (segments[j]["rate"] / rte)
                if spread >= min_sp and spread > best_spread:
                    best_spread = spread
                    best_ch_idx = j
            if best_ch_idx is not None:
                ch_seg = segments[best_ch_idx]
                used_hours.update(ch_seg["hours"])
                raw_cycles.append({
                    "charge_hours": list(ch_seg["hours"]),
                    "discharge_hours": list(peak_seg["hours"]),
                    "charge_period": ch_seg["period"],
                    "discharge_period": peak_seg["period"],
                    "avg_charge_rate": ch_seg["rate"],
                    "avg_discharge_rate": peak_seg["rate"],
                    "effective_spread": round(best_spread, 5),
                })
                prev_peak_idx = peak_idx

        if not raw_cycles:
            warnings.append(
                "No chronological charge-then-discharge window pair satisfied min_spread; returning idle schedules."
            )

    ch_48 = [0.0] * 48
    dis_48 = [0.0] * 48
    eta_one = math.sqrt(rte)
    planned_cycles: list[dict[str, Any]] = []

    for c_idx, cyc in enumerate(raw_cycles, start=1):
        ch_hours = cyc["charge_hours"]
        dis_hours = cyc["discharge_hours"]
        ch_slots = [s for h in ch_hours for s in (2 * h, 2 * h + 1)]
        dis_slots = [s for h in dis_hours for s in (2 * h, 2 * h + 1)]

        # Cap power smoothly if usable_energy_kwh is specified so energy per cycle fits the battery
        eff_ch_kw = ch_kw
        eff_dis_kw = dis_kw
        if usable_kwh is not None:
            # AC energy needed to fill usable_kwh = usable_kwh / eta_one
            max_ac_ch_kwh = usable_kwh / eta_one
            max_ac_dis_kwh = usable_kwh * eta_one
            ch_dur_h = len(ch_slots) * 0.5
            dis_dur_h = len(dis_slots) * 0.5
            if ch_dur_h > 0:
                eff_ch_kw = min(ch_kw, max_ac_ch_kwh / ch_dur_h)
            if dis_dur_h > 0:
                eff_dis_kw = min(dis_kw, max_ac_dis_kwh / dis_dur_h)

        eff_ch_kw = round(eff_ch_kw, 3)
        eff_dis_kw = round(eff_dis_kw, 3)
        for s in ch_slots:
            ch_48[s] = eff_ch_kw
        for s in dis_slots:
            dis_48[s] = eff_dis_kw

        planned_cycles.append({
            "cycle": c_idx,
            "charge_period": cyc["charge_period"],
            "discharge_period": cyc["discharge_period"],
            "charge_hours": ch_hours,
            "discharge_hours": dis_hours,
            "charge_power_kw": eff_ch_kw,
            "discharge_power_kw": eff_dis_kw,
            "avg_charge_rate": round(cyc["avg_charge_rate"], 4),
            "avg_discharge_rate": round(cyc["avg_discharge_rate"], 4),
            "effective_spread": cyc["effective_spread"],
            "planned_charge_kwh": round(eff_ch_kw * len(ch_slots) * 0.5, 2),
            "planned_discharge_kwh": round(eff_dis_kw * len(dis_slots) * 0.5, 2),
        })

    storage_updates: dict[str, Any] = {
        "coupling": "ac_coupled",
        "strategy": "power_shift",
        "charging_strategy": charging_strategy,
        "discharging_strategy": "specified_hours",
        "max_charge_ac_kw": round(ch_kw, 3),
        "max_discharge_ac_kw": round(dis_kw, 3),
        "discharging_schedule_48": dis_48,
    }
    if charging_strategy == "spec_charg_power":
        storage_updates["charging_schedule_48"] = ch_48
    else:
        storage_updates["charging_pv_fraction"] = 1.0

    return {
        "status": "ok" if planned_cycles else "idle",
        "month": month,
        "day_type": day_type,
        "requested_cycles_per_day": cycles_per_day,
        "planned_cycles_count": len(planned_cycles),
        "round_trip_efficiency": rte,
        "min_spread": min_sp,
        "cycles": planned_cycles,
        "charging_schedule_48": ch_48,
        "discharging_schedule_48": dis_48,
        "storage_updates": storage_updates,
        "hourly_rates": hourly,
        "warnings": warnings,
    }

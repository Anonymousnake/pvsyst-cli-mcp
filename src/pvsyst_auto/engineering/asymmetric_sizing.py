"""Asymmetric MPPT remainder sizing fallback across multi-MPPT string inverters."""
from __future__ import annotations

import math


def build_asymmetric_subarrays(
    series: int, ports: int, ac_kw: float, count: int, s_full: int | None, s_high: int, s_low: int, k: int,
) -> list[dict]:
    """Build 2 (single shared inverter) or 3 (full + shared remainder) subarray allocations."""
    s_shared = k * s_high + (ports - k) * s_low
    p_high = round(ac_kw * (s_high / s_shared), 2)
    p_low = round(ac_kw * (s_low / s_shared), 2)
    if ports - k == 1:
        p_low = round(ac_kw - k * p_high, 2)
    elif k == 1:
        p_high = round(ac_kw - (ports - k) * p_low, 2)
    if p_high <= 0 or p_low <= 0:
        raise ValueError("Shared MPPT nominal AC allocation must remain positive")
    allocs: list[dict] = []
    sid = 1
    if count >= 2:
        assert s_full is not None
        allocs.append({
            "subarray_id": sid, "comment": "PV Array (Full Inv)", "n_mod_serie": series, "n_inverter": count - 1,
            "n_inv_mppt": (count - 1) * ports, "n_string_ch": (count - 1) * ports * s_full,
            "power_sharing_inverter_id": -1, "pnom_ac_in_mppt": round(ac_kw / ports, 2),
        })
        sid += 1
    allocs.extend([
        {"subarray_id": sid, "comment": "PV Array (Shared High MPPT)", "n_mod_serie": series, "n_inverter": 1,
         "n_inv_mppt": k, "n_string_ch": k * s_high, "power_sharing_inverter_id": 0, "pnom_ac_in_mppt": p_high},
        {"subarray_id": sid + 1, "comment": "PV Array (Shared Low MPPT)", "n_mod_serie": series, "n_inverter": 1,
         "n_inv_mppt": ports - k, "n_string_ch": (ports - k) * s_low, "power_sharing_inverter_id": 0, "pnom_ac_in_mppt": p_low},
    ])
    return allocs


def search_asymmetric_mppt(
    *, target_dc_kw: float, tol: float, module_kw: float, ac_kw: float, ports: int,
    feasible_series: list[int], max_parallel: int, min_ratio: float | None, max_ratio: float,
    preferred_ratio: float | None = None, ref_series: int | None = None,
) -> dict | None:
    """Fallback search allowing 1 power-sharing inverter with k*s_high + (M-k)*s_low strings."""
    if ports < 2 or max_parallel < 2 or not feasible_series:
        return None
    ref_s = (0.5 * (feasible_series[0] + feasible_series[-1])) if ref_series is None else ref_series
    best_key = best_cfg = None
    ratio_ok = lambda r: (r <= max_ratio + 1e-9) and (min_ratio is None or r >= min_ratio - 1e-9)

    def consider(err_pct: float, ratio: float, count: int, series: int, max_s: int,
                 s_full: int | None, s_high: int, s_low: int, k: int, total_str: int, inst_dc: float) -> None:
        nonlocal best_key, best_cfg
        key = (
            (round(err_pct, 4), round(abs(ratio - preferred_ratio), 4), count, abs(series - ref_s), series, max_s)
            if preferred_ratio is not None
            else (round(err_pct, 9), count, abs(series - ref_s), series, max_s)
        )
        if best_key is None or key < best_key:
            try:
                allocs = build_asymmetric_subarrays(series, ports, ac_kw, count, s_full, s_high, s_low, k)
            except ValueError:
                return
            best_key = key
            best_cfg = (series, count, s_full, s_high, s_low, k, total_str, inst_dc, ratio, max_s, allocs)

    for series in feasible_series:
        str_kw = series * module_kw
        if not (math.isfinite(str_kw) and str_kw > 0):
            continue
        for s_high in range(2, max_parallel + 1):
            s_low = s_high - 1
            for k in range(1, ports):
                s_shared = k * s_high + (ports - k) * s_low
                inst_dc = s_shared * str_kw
                ratio = inst_dc / ac_kw
                if ratio_ok(ratio) and (err_pct := abs(inst_dc - target_dc_kw) / target_dc_kw * 100.0) <= tol + 1e-9:
                    consider(err_pct, ratio, 1, series, s_high, None, s_high, s_low, k, s_shared, inst_dc)
        for s_full in range(1, max_parallel + 1):
            if not ratio_ok((ports * s_full * str_kw) / ac_kw):
                continue
            pairs = ([(s_full, s_full - 1)] if s_full >= 2 else []) + ([(s_full + 1, s_full)] if s_full < max_parallel else [])
            for s_high, s_low in pairs:
                for k in range(1, ports):
                    s_shared = k * s_high + (ports - k) * s_low
                    if not ratio_ok((s_shared * str_kw) / ac_kw):
                        continue
                    ideal_n = 1.0 + (target_dc_kw / str_kw - s_shared) / (ports * s_full)
                    for count in {max(2, math.floor(ideal_n)), max(2, math.ceil(ideal_n))}:
                        if not 2 <= count <= 10000:
                            continue
                        tot_str = (count - 1) * ports * s_full + s_shared
                        inst_dc = tot_str * str_kw
                        ratio = inst_dc / (count * ac_kw)
                        if ratio_ok(ratio) and (err_pct := abs(inst_dc - target_dc_kw) / target_dc_kw * 100.0) <= tol + 1e-9:
                            consider(err_pct, ratio, count, series, max(s_full, s_high), s_full, s_high, s_low, k, tot_str, inst_dc)

    if best_cfg is None:
        return None
    series, count, s_full, s_high, s_low, k, tot_str, inst_dc, ratio, max_s, allocs = best_cfg
    desc = (
        f"Asymmetric MPPT fallback ({count} inverter, {ports} MPPT/inv): {k} MPPTs @ {s_high} str + {ports - k} MPPTs @ {s_low} str ({tot_str}x{series} mods)"
        if count == 1 else
        f"Asymmetric MPPT fallback ({count} inverters, {ports} MPPT/inv): {count - 1} uniform inv @ {s_full} str/MPPT + 1 shared inv ({k}@{s_high} + {ports - k}@{s_low}) ({tot_str}x{series} mods)"
    )
    return {
        "series": series, "count": count, "total_strings": tot_str, "max_strings_per_mppt": max_s,
        "installed_dc": inst_dc, "installed_ac": count * ac_kw, "ratio": ratio, "allocations": allocs, "description": desc,
    }

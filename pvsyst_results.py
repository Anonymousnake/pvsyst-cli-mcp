"""Streaming comparisons of aligned native SFI results, without rerunning PVsyst."""
from __future__ import annotations

import hashlib
import math
from contextlib import closing
from datetime import datetime, timedelta
from itertools import zip_longest
from pathlib import Path

from pvsyst_cli import iter_result_csv, result_units


def fingerprint(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def annual_summary(path, year, nominal_dc_power_kw):
    """Accept one complete hourly calendar year and compute standard grid PR."""
    if type(year) is not int or not 1969 <= year <= 2068:
        raise ValueError("Annual SFI validation requires a year in 1969..2068 (two-digit CSV dates)")
    if (isinstance(nominal_dc_power_kw, bool) or not isinstance(nominal_dc_power_kw, (int, float))
            or not math.isfinite(nominal_dc_power_kw) or nominal_dc_power_kw <= 0):
        raise ValueError("Nominal DC power must be a positive finite kWp value")
    before = fingerprint(path)
    units = result_units(path, strict=True)
    power = {name: {"W": 0.001, "kW": 1}.get(units.get(name))
             for name in ("E_Grid", "EArray", "IL_Pmax")}
    irradiation_scale = {"W/m2": 0.001, "W/m\u00b2": 0.001,
                         "kW/m2": 1, "kW/m\u00b2": 1}.get(units.get("GlobInc"))
    pr_scale = {"ratio": 1, "%": 0.01}.get(units.get("PR"))
    if any(scale is None for scale in power.values()) or irradiation_scale is None or pr_scale is None:
        raise ValueError("Annual output requires explicit power, irradiance and PR units")
    columns = (*power, "GlobInc", "PR")
    totals = dict.fromkeys(power, 0.0)
    irradiation = weighted_pr = 0.0
    first = expected = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    indices = None
    count = 0
    for headers, row in iter_result_csv(path, strict=True):
        if indices is None:
            if len(set(headers)) != len(headers) or any(name not in headers for name in columns):
                raise ValueError("Annual output requires unique E_Grid, EArray, IL_Pmax, GlobInc and PR columns")
            indices = {name: headers.index(name) for name in columns}
        if row[0] != expected or expected >= end:
            raise ValueError(f"Annual hourly coverage mismatch at row {count + 1}: expected {expected}, got {row[0]}")
        values = {name: row[index] for name, index in indices.items()}
        if any(not math.isfinite(values[name]) for name in (*power, "GlobInc")):
            raise ValueError(f"Annual output has missing or nonfinite energy/irradiance at {expected}")
        if values["GlobInc"] < 0 or values["IL_Pmax"] < 0:
            raise ValueError("Annual output contains negative irradiation or clipping loss")
        for name, scale in power.items():
            totals[name] += values[name] * scale  # Fixed one-hour samples integrate kW to kWh.
        light = values["GlobInc"] * irradiation_scale
        irradiation += light
        if light:
            if not math.isfinite(values["PR"]):
                raise ValueError(f"Annual output has missing PR during irradiation at {expected}")
            weighted_pr += light * values["PR"] * pr_scale
        expected += timedelta(hours=1)
        count += 1
    if expected != end:
        raise ValueError(f"Annual output is incomplete: {count} hourly rows; ended at {expected}")
    if irradiation <= 0 or any(not math.isfinite(v) for v in (*totals.values(), irradiation, weighted_pr)):
        raise ValueError("Annual arithmetic requires positive irradiation and finite totals")
    reference_energy = irradiation * nominal_dc_power_kw
    if not math.isfinite(reference_energy) or reference_energy <= 0:
        raise ValueError("Annual STC reference energy exceeds finite numeric range")
    ratio = totals["E_Grid"] / reference_energy
    native_ratio = weighted_pr / irradiation
    if not math.isfinite(ratio) or not math.isclose(ratio, native_ratio, abs_tol=0.002, rel_tol=0):
        raise ValueError("Native PR disagrees with standard grid-only PR; check capacity, self-consumption or PR correction")
    if before != fingerprint(path):
        raise ValueError("Annual output changed during validation")
    diagnostics = []
    if totals["IL_Pmax"] > 3 * reference_energy:
        diagnostics.append("IL_Pmax exceeds three times STC reference energy; review PV model and sizing. "
                           "This is a conservative plausibility screen, not a native physical limit.")
    return {"year": year, "rows": count, "first": str(first), "last": str(end - timedelta(hours=1)),
            "diagnostic_flags": diagnostics,
            "step_minutes": 60, "complete_year": True, "sha256": before,
            "grid_energy_kwh": totals["E_Grid"], "array_energy_kwh": totals["EArray"],
            "clipping_loss_kwh": totals["IL_Pmax"], "incident_irradiation_kwh_m2": irradiation,
            "nominal_dc_power_kw": nominal_dc_power_kw, "pr_ratio": ratio, "pr_percent": ratio * 100,
            "native_pr_irradiance_weighted_ratio": native_ratio,
            "pr_method": "E_Grid_kWh / (GlobInc_kWh_m2 * nameplate_DC_kWp); not mean(hourly PR)",
            "clipping_scope": "IL_Pmax inverter overpower loss, not all curtailment or inverter losses"}


def compare_results(baseline, target, columns):
    """Compare target minus baseline on identical, strictly increasing timestamps."""
    if not columns or len(columns) > 64 or len(set(columns)) != len(columns):
        raise ValueError("Select 1..64 distinct numeric columns")
    paths = (Path(baseline), Path(target))
    hashes = [fingerprint(path) for path in paths]
    units = [result_units(path, strict=True) for path in paths]
    for name in columns:
        if not units[0].get(name) or units[0].get(name) != units[1].get(name):
            raise ValueError(f"{name}: comparison requires identical explicit units in both files")
    stats = {name: {"baseline_samples": 0, "target_samples": 0, "paired_samples": 0,
                   "paired_baseline_sum": 0.0, "paired_target_sum": 0.0,
                   "paired_delta_sum": 0.0, "min_delta": None, "max_delta": None,
                   "max_abs_delta": None, "max_abs_delta_at": None} for name in columns}
    indices = None
    first = last = previous = step = None
    regular = True
    count = 0
    with closing(iter_result_csv(paths[0], strict=True)) as left, closing(iter_result_csv(paths[1], strict=True)) as right:
        for pair in zip_longest(left, right):
            if any(item is None for item in pair):
                raise ValueError("Results have different row counts")
            (ha, a), (hb, b) = pair
            if indices is None:
                for headers in (ha, hb):
                    if len(set(headers)) != len(headers) or any(name not in headers[1:] for name in columns):
                        raise ValueError("Comparison requires unambiguous numeric columns in both files")
                indices = [(ha.index(name), hb.index(name)) for name in columns]
                first = str(a[0])
            if a[0] != b[0]:
                raise ValueError(f"Results have different timestamps at row {count + 1}")
            if previous is not None:
                delta = (a[0] - previous).total_seconds() / 60
                if delta <= 0:
                    raise ValueError("Comparison timestamps must be strictly increasing and unique")
                if step is None:
                    step = delta
                elif delta != step:
                    regular = False
            previous, last = a[0], str(a[0])
            count += 1
            for name, (ia, ib) in zip(columns, indices):
                state = stats[name]
                va, vb = a[ia], b[ib]
                good_a, good_b = math.isfinite(va), math.isfinite(vb)
                state["baseline_samples"] += good_a
                state["target_samples"] += good_b
                if not (good_a and good_b):
                    continue
                difference = vb - va
                state["paired_samples"] += 1
                for key, value in (("paired_baseline_sum", va), ("paired_target_sum", vb), ("paired_delta_sum", difference)):
                    state[key] += value
                    if not math.isfinite(state[key]):
                        raise ValueError(f"{name}: comparison arithmetic exceeds finite numeric range")
                state["min_delta"] = difference if state["min_delta"] is None else min(state["min_delta"], difference)
                state["max_delta"] = difference if state["max_delta"] is None else max(state["max_delta"], difference)
                if state["max_abs_delta"] is None or abs(difference) > state["max_abs_delta"]:
                    state["max_abs_delta"], state["max_abs_delta_at"] = abs(difference), last
    step = step if regular else None
    for name, state in stats.items():
        samples = state["paired_samples"]
        complete = samples == count
        scale = {"W": 0.001, "kW": 1}.get(units[0][name])
        observed = state["paired_delta_sum"] * step / 60 * scale if samples and step is not None and scale else None
        relative = (state["paired_delta_sum"] / state["paired_baseline_sum"] * 100
                    if samples and state["paired_baseline_sum"] else None)
        if any(value is not None and not math.isfinite(value) for value in (observed, relative)):
            raise ValueError(f"{name}: comparison arithmetic exceeds finite numeric range")
        state.update(unit=units[0][name], missing_pairs=count - samples,
                     paired_coverage=samples / count, complete=complete,
                     mean_delta=state["paired_delta_sum"] / samples if samples else None,
                     paired_relative_change_pct=relative,
                     observed_delta_energy_kwh=observed,
                     delta_energy_kwh=observed if complete else None)
        if not samples:
            for key in ("paired_baseline_sum", "paired_target_sum", "paired_delta_sum"):
                state[key] = None
    if hashes != [fingerprint(path) for path in paths]:
        raise ValueError("Result file changed during comparison; retry with stable files")
    return {"mode": "comparison", "direction": "target minus baseline",
            "baseline": {"filename": paths[0].name, "sha256": hashes[0]},
            "target": {"filename": paths[1].name, "sha256": hashes[1]},
            "columns": list(columns), "rows": count, "first": first, "last": last,
            "step_minutes": step, "summary": stats,
            "scope": "Identical parsed timestamps and units; paired finite samples only. "
                     "Completeness covers rows present, not the intended simulation period or input equivalence."}

"""Streaming comparisons of aligned native SFI results, without rerunning PVsyst."""
from __future__ import annotations

import csv
import hashlib
import math
from contextlib import closing
from datetime import datetime, timedelta
from itertools import zip_longest
from pathlib import Path

from pvsyst_auto.native.cli import iter_result_csv, result_units


def fingerprint(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_native_csv_context(
    csv_path: Path,
    expected: dict[str, str],
    *,
    mismatch_message: str | None = None,
) -> dict[str, str]:
    """Verify that the native result CSV lead-in rows match the expected project/variant/site/weather."""
    context: dict[str, str] = {}
    with Path(csv_path).open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        for row in csv.reader(stream, delimiter=";"):
            if row and row[0].strip().lower() == "date":
                break
            if len(row) >= 2 and row[0].strip() in expected:
                key = row[0].strip()
                if key in context:
                    raise ValueError("Duplicate native result context: " + key)
                context[key] = row[1].strip()
    if any(context.get(key, "").casefold() != value.casefold() for key, value in expected.items()):
        if mismatch_message is not None:
            raise ValueError(mismatch_message)
        raise ValueError("Native CSV did not consume the requested project/variant/site/weather: " + str(context))
    return context


def verify_pdf_header(pdf_path: Path, *, message: str = "Native report is not a PDF") -> None:
    """Ensure a generated native PDF report begins with the %PDF- magic header."""
    with Path(pdf_path).open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError(message)


SUPPORTED_ANNUAL_STEPS = (5, 10, 15, 30, 60)

POWER_TO_KW = {"W": 0.001, "kW": 1.0}
ENERGY_TO_KWH = {"Wh": 0.001, "kWh": 1.0}
BATCH_ENERGY_TO_KWH = {**ENERGY_TO_KWH, "MWh": 1000.0, "GWh": 1_000_000.0}
POWER_UNIT_KIND = {
    "W": (0.001, True), "kW": (1.0, True),
    "Wh": (0.001, False), "kWh": (1.0, False),
}
IRRAD_UNIT_KIND = {
    "W/m2": (0.001, True), "W/m\u00b2": (0.001, True),
    "kW/m2": (1.0, True), "kW/m\u00b2": (1.0, True),
    "Wh/m2": (0.001, False), "Wh/m\u00b2": (0.001, False),
    "kWh/m2": (1.0, False), "kWh/m\u00b2": (1.0, False),
}


def energy_unit_scale(unit: str) -> tuple[float, bool]:
    """Return (factor_to_kw_or_kwh, needs_step_hours) for a PVsyst CSV power/energy column unit."""
    if unit in POWER_TO_KW:
        return POWER_TO_KW[unit], True
    if unit in ENERGY_TO_KWH:
        return ENERGY_TO_KWH[unit], False
    return 0.0, False


class RegularResultCsv:
    """Single-pass streaming reader for regular-cadence hourly/sub-hourly PVsyst SFI CSVs."""

    def __init__(
        self,
        csv_path: Path,
        *,
        empty_message: str = "Result CSV has no data rows",
        irregular_message: str = (
            "Result CSV timestamps do not have a regular cadence (missing, duplicate, or out-of-order rows)"
        ),
        required_any_of: tuple[str, ...] = (),
        missing_required_message: str = "",
    ) -> None:
        self.csv_path = csv_path
        self.units: dict[str, str] = result_units(csv_path, strict=True)
        self.headers: list[str] = []
        self.idx: dict[str, int] = {}
        self.col_scale: dict[str, tuple[float, bool]] = {}
        self.rows_count: int = 0
        self.step_minutes: float | None = None
        self._empty_message = empty_message
        self._irregular_message = irregular_message
        self._required_any_of = required_any_of
        self._missing_required_message = missing_required_message

    def cell_kwh(self, row: list, col: str, dt_hours: float) -> float:
        """Convert one CSV cell to kWh, integrating power units over ``dt_hours``."""
        col_i = self.idx.get(col)
        if col_i is None:
            return 0.0
        factor, needs_dt = self.col_scale[col]
        if factor == 0.0:
            raise ValueError(f"{col} requires a power or energy unit, got {self.units.get(col, '')!r}")
        raw = row[col_i]
        val = float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else 0.0
        return val * factor * (dt_hours if needs_dt else 1.0)

    def cell_kw(self, row: list, col: str, dt_hours: float) -> float:
        """Convert one CSV cell to kW, dividing energy units by ``dt_hours``."""
        col_i = self.idx.get(col)
        if col_i is None:
            return 0.0
        factor, is_power = self.col_scale[col]
        if factor == 0.0:
            raise ValueError(f"{col} requires a power or energy unit, got {self.units.get(col, '')!r}")
        raw = row[col_i]
        val = float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else 0.0
        return val * factor if is_power else (val * factor / dt_hours)

    def iter_rows(self):
        first_row = None
        prev_stamp = None
        step_min: float | None = None
        for headers, row in iter_result_csv(self.csv_path, strict=True):
            if self.rows_count == 0:
                self.headers = list(headers)
                self.idx = {name: i for i, name in enumerate(headers) if i > 0}
                self.col_scale = {col: energy_unit_scale(self.units.get(col, "")) for col in self.idx}
                if self._required_any_of and not any(c in self.idx for c in self._required_any_of):
                    raise ValueError(
                        self._missing_required_message
                        or f"Result CSV must include one of: {', '.join(self._required_any_of)}"
                    )
                first_row = list(row)
                prev_stamp = row[0]
                self.rows_count = 1
                continue
            stamp = row[0]
            delta = (stamp - prev_stamp).total_seconds() / 60.0
            if delta <= 0:
                raise ValueError(self._irregular_message)
            if step_min is None:
                step_min = delta
                self.step_minutes = step_min
                dt_hours = step_min / 60.0
                yield first_row[0], first_row, dt_hours
                first_row = None
            elif abs(delta - step_min) > 1e-6:
                raise ValueError(self._irregular_message)
            dt_hours = step_min / 60.0
            prev_stamp = stamp
            self.rows_count += 1
            yield stamp, row, dt_hours
        if self.rows_count == 0:
            raise ValueError(self._empty_message)
        if first_row is not None:
            self.step_minutes = 60.0
            yield first_row[0], first_row, 1.0


def iter_aligned_result_rows(
    readers: list[RegularResultCsv],
    *,
    count_mismatch_message: str = "Merged result CSVs have different row counts; all portfolio CSVs must align",
    detailed_timestamp_error: bool = False,
):
    """Yield ``(timestamp, dt_hours, per_reader_items)`` across aligned ``RegularResultCsv`` readers."""
    sentinel = object()
    for row_idx, items in enumerate(zip_longest(*(r.iter_rows() for r in readers), fillvalue=sentinel)):
        if any(item is sentinel for item in items):
            raise ValueError(count_mismatch_message)
        stamp, _, dt_hours = items[0]
        for k, (st_k, _, dt_k) in enumerate(items[1:], start=1):
            if st_k != stamp or abs(dt_k - dt_hours) > 1e-6:
                msg = f"Merged result CSV {readers[k].csv_path.name} timestamp mismatch at row {row_idx + 1}"
                if detailed_timestamp_error:
                    msg += f": expected {stamp.isoformat()} (dt={dt_hours:g}h), got {st_k.isoformat()} (dt={dt_k:g}h)"
                raise ValueError(msg)
        yield stamp, dt_hours, items


def annual_summary(path, year, nominal_dc_power_kw):
    """Accept one complete hourly or sub-hourly (5/10/15/30/60 min) calendar year and compute standard grid PR."""
    if type(year) is not int or not 1969 <= year <= 2068:
        raise ValueError("Annual SFI validation requires a year in 1969..2068 (two-digit CSV dates)")
    if (isinstance(nominal_dc_power_kw, bool) or not isinstance(nominal_dc_power_kw, (int, float))
            or not math.isfinite(nominal_dc_power_kw) or nominal_dc_power_kw <= 0):
        raise ValueError("Nominal DC power must be a positive finite kWp value")
    before = fingerprint(path)
    units = result_units(path, strict=True)
    power_info = {name: POWER_UNIT_KIND.get(units.get(name)) for name in ("E_Grid", "EArray", "IL_Pmax")}
    irrad_info = IRRAD_UNIT_KIND.get(units.get("GlobInc"))
    pr_scale = {"ratio": 1, "%": 0.01}.get(units.get("PR"))
    if any(info is None for info in power_info.values()) or irrad_info is None or pr_scale is None:
        raise ValueError("Annual output requires explicit power, irradiance and PR units")

    columns = (*power_info, "GlobInc", "PR")
    first = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)

    # Accumulate raw column sums first, inferring step_minutes from rows 1 and 2 on the fly.
    raw_totals = dict.fromkeys(power_info, 0.0)
    raw_irradiation = raw_weighted_pr = raw_dark_tare = 0.0
    indices = None
    step_minutes: int | None = None
    step_delta: timedelta | None = None
    expected = first
    count = 0
    for headers, row in iter_result_csv(path, strict=True):
        if indices is None:
            if len(set(headers)) != len(headers) or any(name not in headers for name in columns):
                raise ValueError("Annual output requires unique E_Grid, EArray, IL_Pmax, GlobInc and PR columns")
            indices = {name: headers.index(name) for name in columns}
        if count == 0:
            if row[0] != first:
                raise ValueError(f"Annual coverage mismatch at row 1: expected {first}, got {row[0]}")
        elif count == 1:
            delta_sec = (row[0] - first).total_seconds()
            delta_min = delta_sec / 60.0
            if delta_sec <= 0 or delta_min != int(delta_min) or int(delta_min) not in SUPPORTED_ANNUAL_STEPS:
                raise ValueError(
                    f"Annual coverage mismatch at row 2: expected step in {SUPPORTED_ANNUAL_STEPS} min from {first}, got {row[0]}"
                )
            step_minutes = int(delta_min)
            step_delta = timedelta(minutes=step_minutes)
            expected = first + step_delta
        if count >= 1:
            if row[0] != expected or expected >= end:
                raise ValueError(f"Annual coverage mismatch at row {count + 1}: expected {expected}, got {row[0]}")
        values = {name: row[index] for name, index in indices.items()}
        if any(not math.isfinite(values[name]) for name in (*power_info, "GlobInc")):
            raise ValueError(f"Annual output has missing or nonfinite energy/irradiance at {row[0]}")
        if values["GlobInc"] < 0 or values["IL_Pmax"] < 0:
            raise ValueError("Annual output contains negative irradiation or clipping loss")
        for name in power_info:
            raw_totals[name] += values[name]
        g = values["GlobInc"]
        raw_irradiation += g
        if g:
            if not math.isfinite(values["PR"]):
                raise ValueError(f"Annual output has missing PR during irradiation at {row[0]}")
            raw_weighted_pr += g * values["PR"] * pr_scale
        elif values["E_Grid"] < 0:
            raw_dark_tare += values["E_Grid"]
        if step_delta is not None:
            expected += step_delta
        count += 1
    if step_minutes is None or step_delta is None or expected != end:
        raise ValueError(f"Annual output is incomplete: {count} rows; ended at {expected}")

    step_hours = step_minutes / 60.0
    totals = {
        name: raw_totals[name] * base * (step_hours if is_power else 1.0)
        for name, (base, is_power) in power_info.items()
    }
    irrad_base, irrad_is_power = irrad_info
    irrad_factor = irrad_base * (step_hours if irrad_is_power else 1.0)
    irradiation = raw_irradiation * irrad_factor
    weighted_pr = raw_weighted_pr * irrad_factor
    egrid_base, egrid_is_power = power_info["E_Grid"]
    dark_tare_kwh = raw_dark_tare * egrid_base * (step_hours if egrid_is_power else 1.0)

    if irradiation <= 0 or any(not math.isfinite(v) for v in (*totals.values(), irradiation, weighted_pr, dark_tare_kwh)):
        raise ValueError("Annual arithmetic requires positive irradiation and finite totals")
    reference_energy = irradiation * nominal_dc_power_kw
    if not math.isfinite(reference_energy) or reference_energy <= 0:
        raise ValueError("Annual STC reference energy exceeds finite numeric range")
    ratio = totals["E_Grid"] / reference_energy
    native_ratio = weighted_pr / irradiation
    expected_ratio = native_ratio + dark_tare_kwh / reference_energy
    if (not math.isfinite(ratio) or not math.isfinite(expected_ratio)
            or not math.isclose(ratio, expected_ratio, abs_tol=0.002, rel_tol=0)):
        raise ValueError("Native PR disagrees with standard grid-only PR; check capacity, self-consumption or PR correction")
    if before != fingerprint(path):
        raise ValueError("Annual output changed during validation")
    diagnostics = []
    if totals["IL_Pmax"] > 3 * reference_energy:
        diagnostics.append("IL_Pmax exceeds three times STC reference energy; review PV model and sizing.")
    return {"year": year, "rows": count, "first": str(first), "last": str(end - step_delta),
            "diagnostic_flags": diagnostics,
            "step_minutes": step_minutes, "complete_year": True, "sha256": before,
            "grid_energy_kwh": totals["E_Grid"], "array_energy_kwh": totals["EArray"],
            "clipping_loss_kwh": totals["IL_Pmax"], "incident_irradiation_kwh_m2": irradiation,
            "nominal_dc_power_kw": nominal_dc_power_kw, "pr_ratio": ratio, "pr_percent": ratio * 100,
            "native_pr_irradiance_weighted_ratio": native_ratio}


def compare_results(baseline, target, columns, *, include_key_periods=False):
    """Compare target minus baseline on identical, strictly increasing timestamps."""
    # A bare string would otherwise be compared character by character as column names.
    if (isinstance(columns, str) or not columns or len(columns) > 64
            or len(set(columns)) != len(columns)):
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
    key_periods = {}
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
                    if include_key_periods:
                        key_periods[name] = {"column": name, "timestamp": last, "unit": units[0][name],
                                             "baseline": va, "target": vb, "delta": difference}
    if count == 0:
        raise ValueError("Result CSV contains no simulation rows")
    step = step if regular else None
    for name, state in stats.items():
        samples = state["paired_samples"]
        complete = samples == count
        unit = units[0][name]
        power_scale = POWER_TO_KW.get(unit)
        energy_scale = ENERGY_TO_KWH.get(unit)
        if samples and step is not None and power_scale is not None:
            observed = state["paired_delta_sum"] * step / 60 * power_scale
        elif samples and step is not None and energy_scale is not None:
            observed = state["paired_delta_sum"] * energy_scale
        else:
            observed = None
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
    result = {"mode": "comparison", "direction": "target minus baseline",
            "baseline": {"filename": paths[0].name, "sha256": hashes[0]},
            "target": {"filename": paths[1].name, "sha256": hashes[1]},
            "columns": list(columns), "rows": count, "first": first, "last": last,
            "step_minutes": step, "summary": stats}
    if include_key_periods:
        result["key_periods"] = [key_periods[name] for name in columns if name in key_periods]
    return result

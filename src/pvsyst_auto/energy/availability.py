"""Bind an explicit or deterministically generated grid-availability profile to native pvUnavail."""
from __future__ import annotations

import calendar
import csv
import io
import random
from datetime import datetime, timedelta

from pvsyst_auto.energy.profiles import _read, TIME_BASIS
from pvsyst_auto.project.paths import workspace_file


def _render_unavail_snapshot(year: int, days: dict[str, list[str]], comment: str, newline: str) -> str:
    lines = [
        "    PVObject_GridUnavail=pvUnavail",
        f"      Comment={comment}",
        "      Flags=$00",
        "      DefMode=Periods",
        "      HourlyVal=GrdUnav",
        "        Varident=GrdUnav",
        "        VarAcc=Hour",
        "        VarType=Factor",
        "        VarStore=Simul",
        "        VarModeSS=Aver",
        f"        DateBeg=01/01/{year % 100:02d} 00:00",
        f"        DateEnd=31/12/{year % 100:02d} 23:00",
        "        WRFactor=1000.0",
    ]
    lines.extend("          Day_" + day + "=" + "".join(tokens) for day, tokens in days.items())
    lines.extend(["        End of Data", "      End of VarSimul=viGrdUnav", "    End of PVObject pvUnavail"])
    return newline.join(lines) + newline


def generate_outage_availability(declaration: dict, newline: str) -> tuple[str, dict]:
    """Deterministically generate a full-year WeakGrid outage schedule into native pvUnavail."""
    year = declaration.get("year")
    if type(year) is not int or not 1969 <= year <= 2068:
        raise ValueError("Declare an integer availability year in 1969..2068")
    outage_count = declaration.get("outage_count")
    if type(outage_count) is not int or not 1 <= outage_count <= 365:
        raise ValueError("outage_count must be an integer in 1..365")
    min_dur = declaration.get("min_duration_hours", 2)
    max_dur = declaration.get("max_duration_hours", 6)
    if type(min_dur) is not int or type(max_dur) is not int or not 1 <= min_dur <= max_dur <= 168:
        raise ValueError("min_duration_hours and max_duration_hours must be integers with 1 <= min <= max <= 168")
    seed = declaration.get("seed", 42)
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    peak_hours = declaration.get("peak_hours")
    if peak_hours is not None:
        if not isinstance(peak_hours, list) or not peak_hours or any(type(h) is not int or not 0 <= h <= 23 for h in peak_hours):
            raise ValueError("peak_hours must be a non-empty list of integers in 0..23")

    total_hours = 24 * (366 if calendar.isleap(year) else 365)
    avail = [True] * total_hours
    rng = random.Random(seed)
    beginning = datetime(year, 1, 1)

    events: list[dict] = []
    # Spread outage starts across the year so events don't pile into one day
    stride = max(1, total_hours // outage_count)
    for idx in range(outage_count):
        base = (idx * stride + rng.randint(0, max(0, stride - 1))) % total_hours
        if peak_hours:
            day_start = (base // 24) * 24
            chosen_h = rng.choice(peak_hours)
            start_h = min(total_hours - 1, day_start + chosen_h)
        else:
            start_h = base
        dur = rng.randint(min_dur, max_dur)
        end_h = min(total_hours, start_h + dur)
        for h in range(start_h, end_h):
            avail[h] = False
        events.append({
            "start": (beginning + timedelta(hours=start_h)).strftime("%Y-%m-%d %H:%M"),
            "duration_hours": end_h - start_h,
        })

    days: dict[str, list[str]] = {}
    unavailable = 0
    for h_idx, is_up in enumerate(avail):
        stamp = beginning + timedelta(hours=h_idx)
        if not is_up:
            unavailable += 1
        days.setdefault(stamp.strftime("%d%m"), []).append("+G8" if is_up else "+0")

    rendered = _render_unavail_snapshot(year, days, f"MCP generated grid outages (seed={seed})", newline)
    metadata = {
        "mode": "generated",
        "year": year,
        "rows": total_hours,
        "outage_count": outage_count,
        "unavailable_hours": unavailable,
        "unavailability_pct": round(100.0 * unavailable / total_hours, 3),
        "seed": seed,
        "peak_hours": peak_hours,
        "sample_events": events[:10],
        "binding": "native pvUnavail/HourlyVal GrdUnav Factor/Aver snapshot",
        "WRFactor": 1000,
    }
    return rendered, metadata


def native_availability(workspace, declaration, newline):
    if isinstance(declaration, dict) and (declaration.get("mode") == "generated" or (
        "csv_name" not in declaration and "outage_count" in declaration
    )):
        return generate_outage_availability(declaration, newline)

    year = declaration["year"]
    if type(year) is not int or not 1969 <= year <= 2068:
        raise ValueError("Declare an integer availability year in 1969..2068")
    if (declaration["time_basis"] != TIME_BASIS or type(declaration["utc_offset_hours"]) is not int
            or not -12 <= declaration["utc_offset_hours"] <= 14):
        raise ValueError("Declare a fixed whole-hour standard offset and local_standard_interval_start")
    source = workspace_file(workspace, "UserData", declaration["csv_name"], ".csv")
    original = _read(source)
    reader = csv.reader(io.StringIO(original.decode("utf-8-sig")), delimiter=";", strict=True)
    if next(reader, None) != ["Date", "Grid Available"] or next(reader, None) != ["", "ratio"]:
        raise ValueError("Expected Date;Grid Available / ;ratio availability CSV")
    count = 24 * (366 if calendar.isleap(year) else 365)
    beginning = datetime(year, 1, 1)
    days = {}
    unavailable = 0
    rows = 0
    for row in reader:
        if len(row) != 2 or rows >= count:
            raise ValueError("Availability must contain exactly one two-column interval-start row per hour")
        stamp = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
        if stamp != beginning + timedelta(hours=rows):
            raise ValueError("Availability timestamps must exactly cover the requested increasing full year; no filling or shifting")
        value = float(row[1])
        if value not in (0.0, 1.0):
            raise ValueError("Only explicit 0/1 grid availability is accepted; no threshold or interpolation is inferred")
        unavailable += value == 0
        days.setdefault(stamp.strftime("%d%m"), []).append("+G8" if value else "+0")
        rows += 1
    if rows != count:
        raise ValueError("Availability must include every requested hour, including leap day")
    if unavailable == 0:
        raise ValueError("Native WeakGrid requires a nonempty unavailable profile; an all-available source failed native validation. Use SelfConsumption instead")
    rendered = _render_unavail_snapshot(year, days, "MCP explicit grid availability snapshot", newline)
    metadata = {"year": year, "rows": rows, "unavailable_hours": unavailable,
                "utc_offset_hours": declaration["utc_offset_hours"], "time_basis": TIME_BASIS,
                "source": str(source), "binding": "native pvUnavail/HourlyVal GrdUnav Factor/Aver snapshot",
                "WRFactor": 1000, "encoded_value_error": 0}
    return rendered, metadata

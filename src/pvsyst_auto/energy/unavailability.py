"""WeakGrid availability generation/parsing and system unavailability periods."""
from __future__ import annotations

import calendar
import csv
import io
import math
import random
import re
from datetime import datetime, timedelta

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value, root_block
from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.validation import HEADLESS_RECORD_VERSION, NATIVE_TEXT_OR_SEED, SEED_LABEL

MAX_BYTES = 8 * 1024 * 1024
TIME_BASIS = "local_standard_interval_start"

START = ("SystemUnavail", "TUnavailability")
SETTINGS_KEYS = ("TimeFraction", "NbPeriods", "MinDuration", "MaxDuration", "RandomInit")
LEGACY_KEYS = ("UnavTotDuration", "UnavNbPeriods", "UnavDuration", "UnavBegDate")
MAX_PERIODS = 1000
_HEX_FLAG = re.compile(r"\$[0-9A-Fa-f]{1,8}")
_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{2} \d{2}:\d{2}")


def _read(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Load CSV exceeds 8 MiB")
    return data


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
    if peak_hours is not None and (
        not isinstance(peak_hours, list) or not peak_hours or any(type(h) is not int or not 0 <= h <= 23 for h in peak_hours)
    ):
        raise ValueError("peak_hours must be a non-empty list of integers in 0..23")
    utc_offset = declaration.get("utc_offset_hours")
    if utc_offset is not None and (type(utc_offset) is not int or not -12 <= utc_offset <= 14):
        raise ValueError("utc_offset_hours must be an integer in -12..14")
    declared_basis = declaration.get("time_basis")
    if declared_basis is not None and declared_basis != TIME_BASIS:
        raise ValueError(f"time_basis must be {TIME_BASIS!r}")

    total_hours = 24 * (366 if calendar.isleap(year) else 365)
    avail = [True] * total_hours
    rng = random.Random(seed)
    beginning = datetime(year, 1, 1)
    events: list[dict] = []
    stride = max(1, total_hours // outage_count)
    for idx in range(outage_count):
        base = (idx * stride + rng.randint(0, max(0, stride - 1))) % total_hours
        if peak_hours:
            day_start = (base // 24) * 24
            start_h = min(total_hours - 1, day_start + rng.choice(peak_hours))
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
        "mode": "generated", "year": year, "utc_offset_hours": declaration.get("utc_offset_hours"),
        "time_basis": TIME_BASIS, "rows": total_hours, "outage_count": outage_count,
        "unavailable_hours": unavailable, "unavailability_pct": round(100.0 * unavailable / total_hours, 3),
        "seed": seed, "peak_hours": peak_hours, "sample_events": events[:10],
        "binding": "native pvUnavail/HourlyVal GrdUnav Factor/Aver snapshot", "WRFactor": 1000,
    }
    return rendered, metadata


def native_availability(workspace, declaration, newline):
    """Build native pvUnavail block from CSV or deterministic outage declaration."""
    if not isinstance(declaration, dict):
        raise ValueError("Grid availability declaration must be an object")
    mode = declaration.get("mode")
    if mode == "generated" or (mode is None and "csv_name" not in declaration and "outage_count" in declaration):
        return generate_outage_availability(declaration, newline)

    year = declaration.get("year")
    if type(year) is not int or not 1969 <= year <= 2068:
        raise ValueError("Declare an integer availability year in 1969..2068")
    if (
        declaration.get("time_basis") != TIME_BASIS
        or type(declaration.get("utc_offset_hours")) is not int
        or not -12 <= declaration["utc_offset_hours"] <= 14
    ):
        raise ValueError("Declare a fixed whole-hour standard offset and local_standard_interval_start")
    csv_name = declaration.get("csv_name")
    if not isinstance(csv_name, str) or not csv_name.strip():
        raise ValueError("CSV grid availability requires a non-empty csv_name")
    source = workspace_file(workspace, "UserData", csv_name, ".csv")
    reader = csv.reader(io.StringIO(_read(source).decode("utf-8-sig")), delimiter=";", strict=True)
    if next(reader, None) != ["Date", "Grid Available"] or next(reader, None) != ["", "ratio"]:
        raise ValueError("Expected Date;Grid Available / ;ratio availability CSV")
    count = 24 * (366 if calendar.isleap(year) else 365)
    beginning = datetime(year, 1, 1)
    days, unavailable, rows = {}, 0, 0
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
    metadata = {
        "year": year, "rows": rows, "unavailable_hours": unavailable,
        "utc_offset_hours": declaration["utc_offset_hours"], "time_basis": TIME_BASIS,
        "source": str(source), "binding": "native pvUnavail/HourlyVal GrdUnav Factor/Aver snapshot",
        "WRFactor": 1000, "encoded_value_error": 0,
    }
    return rendered, metadata


def _value(line):
    match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
    return None if match is None else (match[2], match[4])


def system_unavail_state(lines, system):
    if system is None:
        return {"configured": False, "reason": "no pvSystem section"}
    starts, may_have_legacy = [], False
    for index in range(system[0] + 1, system[1]):
        pair = _value(lines[index])
        if pair == START:
            starts.append(index)
        elif pair and pair[0] in LEGACY_KEYS:
            may_have_legacy = True
    legacy = {}
    if may_have_legacy:
        try:
            fields = root_block(lines, system).fields
        except ValueError as exc:
            return {"configured": True, "issue": str(exc)}
        if legacy_fields := {key: fields[key][0][1] for key in LEGACY_KEYS if key in fields}:
            legacy["legacy_fields"] = legacy_fields
    if not starts:
        return {"configured": True, "mode": "legacy", **legacy} if legacy else {"configured": False, "reason": None}
    if len(starts) != 1:
        return {"configured": True, "issue": "Requires exactly one SystemUnavail record", **legacy}
    start = starts[0]
    end = next((i for i in range(start + 1, system[1]) if lines[i].strip() == "End of PVObject pvUnavail"), None)
    if end is None:
        return {"configured": True, "issue": "SystemUnavail block is not closed", **legacy}
    settings, periods, mode = {}, [], None
    issues = ["Legacy outage fields coexist with SystemUnavail"] if legacy else []
    for index in range(start + 1, end):
        pair = _value(lines[index])
        if pair is None:
            continue
        key, value = pair
        if key == "DefMode":
            if mode is not None:
                issues.append("Repeated DefMode in SystemUnavail")
            mode = value
        elif key in SETTINGS_KEYS:
            if key in settings:
                issues.append(f"Repeated {key} in SystemUnavail")
            settings[key] = value
        elif key == "DateBeg":
            if len(periods) >= MAX_PERIODS:
                return {"configured": True, "issue": f"SystemUnavail period list exceeds {MAX_PERIODS} entries", **legacy}
            duration = None
            for probe in range(index + 1, min(index + 4, end)):
                inner = _value(lines[probe])
                if inner is not None and inner[0] == "DateBeg":
                    break
                if inner is not None and inner[0] == "Duration":
                    duration = inner[1]
                    break
            periods.append({"date_beg": value, "duration": duration})
    declared = settings.get("NbPeriods")
    if (
        declared is None
        or not declared.isascii()
        or not declared.isdigit()
        or ((mode != "Random" or len(periods) > 0) and int(declared) != len(periods))
    ):
        issues.append("NbPeriods does not match the stored period list")
    if undated := sum(period["duration"] is None for period in periods):
        issues.append(f"{undated} SystemUnavail period(s) lack a Duration within three lines of DateBeg")
    result = {
        "configured": True, "mode": mode, "settings": settings,
        "periods": periods, "period_count": len(periods), **legacy,
    }
    if issues:
        result["issues"] = issues
    return result


def _validate_periods(raw_periods) -> list[tuple[str, float]]:
    if not isinstance(raw_periods, list) or not 1 <= len(raw_periods) <= MAX_PERIODS:
        raise ValueError(f"System unavailability periods mode requires 1..{MAX_PERIODS} period entries")
    normalized = []
    for item in raw_periods:
        if not isinstance(item, dict) or set(item) != {"date_beg", "duration_hours"}:
            raise ValueError("Each unavailability period requires date_beg and duration_hours")
        date_beg, duration = item["date_beg"], item["duration_hours"]
        if not isinstance(date_beg, str) or not _DATE_RE.fullmatch(date_beg):
            raise ValueError("Period date_beg must match DD/MM/YY HH:MM")
        try:
            datetime.strptime(date_beg, "%d/%m/%y %H:%M")
        except ValueError as exc:
            raise ValueError(f"Invalid calendar date_beg {date_beg!r}") from exc
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ValueError("Period duration_hours must be a finite positive number")
        dur = float(duration)
        if not math.isfinite(dur) or dur <= 0.0 or dur > 8784.0:
            raise ValueError("Period duration_hours must be within (0, 8784]")
        if f"{dur:.3f}" == "0.000":
            raise ValueError("Period duration_hours rounds to 0.000 hours at native precision")
        normalized.append((date_beg, dur))
    return normalized


def _render_unavail_lines(
    periods: list[tuple[str, float]], *, time_fraction_str: str, min_d: int, max_d: int, random_seed: int, ending: str,
) -> list[str]:
    block = [
        f"    SystemUnavail=TUnavailability{ending}",
        f"      PVObject_=pvUnavail{ending}",
        f"        Version={HEADLESS_RECORD_VERSION}{ending}",
        f"        Flags=$00{ending}",
        f"      DefMode=Periods{ending}",
        f"      RandomUnavSettings=TRandomUnavSettings{ending}",
        f"        TimeFraction={time_fraction_str}{ending}",
        f"        NbPeriods={len(periods)}{ending}",
        f"        MinDuration={min_d}{ending}",
        f"        MaxDuration={max_d}{ending}",
        f"        RandomInit ={random_seed}{ending}",
        f"      EndTags{ending}",
        f"      UnavailPeriods=Start{ending}",
    ]
    for date_beg, dur in periods:
        block.extend([f"        DateBeg={date_beg}{ending}", f"        Duration={dur:.3f}{ending}", f"      EndTags{ending}"])
    block.extend([f"      EndTags{ending}", f"      End of PVObject pvUnavail{ending}"])
    return block


def _format_unavail_block(periods: list[tuple[str, float]], ending: str) -> list[str]:
    fraction = min(1.0, sum(dur for _, dur in periods) / 8760.0)
    min_d = max(1, int(round(min(dur for _, dur in periods))))
    max_d = max(min_d, int(round(max(dur for _, dur in periods))))
    return _render_unavail_lines(periods, time_fraction_str=f"{fraction:.3f}", min_d=min_d, max_d=max_d, random_seed=7, ending=ending)


def _materialize_random_periods(cfg: dict) -> list[tuple[str, float]]:
    """Deterministically materialize random outage periods matching time_fraction, count, and duration bounds."""
    rng = random.Random(cfg["random_seed"])
    n, min_d, max_d = cfg["period_count"], cfg["min_duration_hours"], cfg["max_duration_hours"]
    target_total = cfg["time_fraction"] * 8760.0
    min_total, max_total = float(n * min_d), float(n * max_d)
    if not (min_total - 1e-9 <= target_total <= max_total + 1e-9):
        raise ValueError(
            f"time_fraction_pct ({target_total:.2f} h/yr) is incompatible with period_count={n} "
            f"and duration bounds [{min_d}, {max_d}] h (feasible range [{min_total:g}, {max_total:g}] h/yr)"
        )
    target_total = min(max(target_total, min_total), max_total)
    durs = [float(min_d)] * n
    rem = max(0.0, target_total - sum(durs))
    indices = list(range(n))
    rng.shuffle(indices)
    cap = float(max_d - min_d)
    if cap > 0.0:
        for idx in indices:
            if rem <= 1e-6:
                break
            take = min(cap, round(rng.uniform(0.0, min(cap, rem)), 1), rem) if n > 1 else min(cap, rem)
            durs[idx] = round(durs[idx] + take, 3)
            rem = max(0.0, target_total - sum(durs))
        for idx in indices:
            if rem <= 1e-6:
                break
            room = float(max_d) - durs[idx]
            if room > 0.0:
                add = min(room, rem)
                durs[idx] = round(durs[idx] + add, 3)
                rem = max(0.0, target_total - sum(durs))
    start_year, slot, periods, last_end_h = datetime(1990, 1, 1, 0, 0), 8760.0 / n, [], 0.0
    for idx, dur in enumerate(durs):
        dur_rounded = round(dur, 3)
        remaining_after = sum(durs[idx + 1:])
        earliest_f = 0.0 if idx == 0 else last_end_h
        latest_f = max(earliest_f, 8760.0 - dur_rounded - remaining_after)
        earliest_h = int(math.ceil(earliest_f - 1e-9))
        latest_h = int(math.floor(latest_f + 1e-9))
        if earliest_h <= latest_h:
            win_start = max(earliest_h, min(latest_h, int(round(idx * slot))))
            win_end = max(win_start, min(latest_h, int(round((idx + 1) * slot - dur_rounded))))
            start_min = int(round(rng.uniform(win_start, win_end))) * 60 if win_end > win_start else win_start * 60
        else:
            min_start_min = int(math.ceil(earliest_f * 60.0 - 1e-9))
            max_start_min = int(math.floor(latest_f * 60.0 + 1e-9))
            start_min = min(max_start_min, max(0, min_start_min))
        stamp = start_year + timedelta(minutes=start_min)
        actual_start_h = start_min / 60.0
        periods.append((stamp.strftime("%d/%m/%y %H:%M"), dur_rounded))
        last_end_h = actual_start_h + dur_rounded
    total_dur = sum(d for _, d in periods)
    if len(periods) != n or any(d < min_d - 1e-6 or d > max_d + 1e-6 for _, d in periods):
        raise ValueError("Unable to materialize random outage periods within requested duration bounds")
    if abs(total_dur - target_total) > 0.1 or last_end_h > 8760.0 + 1e-6:
        raise ValueError(f"Random outage placement could not fit requested total ({total_dur:.2f} h vs target {target_total:.2f} h)")
    return periods


def _validate_random_settings(updates: dict) -> dict:
    allowed = {"mode", "time_fraction_pct", "period_count", "min_duration_hours", "max_duration_hours", "random_seed"}
    if not set(updates) <= allowed:
        raise ValueError("Unknown field in random system_unavailability_updates")
    tf_pct = updates.get("time_fraction_pct")
    if isinstance(tf_pct, bool) or not isinstance(tf_pct, (int, float)) or not math.isfinite(tf_pct) or not 0.0 < float(tf_pct) <= 100.0:
        raise ValueError("time_fraction_pct must be within (0, 100]")
    n_per = updates.get("period_count")
    if type(n_per) is not int or not 1 <= n_per <= MAX_PERIODS:
        raise ValueError(f"period_count must be an integer in 1..{MAX_PERIODS}")
    min_d, max_d = updates.get("min_duration_hours", 1), updates.get("max_duration_hours", 24)
    if type(min_d) is not int or type(max_d) is not int or not 1 <= min_d <= max_d <= 8760:
        raise ValueError("min_duration_hours and max_duration_hours must be integers with 1 <= min <= max <= 8760")
    seed = updates.get("random_seed", 7)
    if type(seed) is not int or not 0 <= seed <= 999999:
        raise ValueError("random_seed must be an integer in 0..999999")
    return {"time_fraction": float(tf_pct) / 100.0, "period_count": n_per, "min_duration_hours": min_d,
            "max_duration_hours": max_d, "random_seed": seed}


def replace_system_unavailability(lines, system, updates):
    """Add/replace explicit outage periods, random outage settings, or remove existing SystemUnavail."""
    if not isinstance(updates, dict) or "mode" not in updates:
        raise ValueError("System unavailability updates require mode='none', 'periods', or 'random'")
    mode = updates["mode"]
    periods = random_cfg = None
    if mode == "none":
        if set(updates) - {"mode"} != set() and updates.get("periods") is not None:
            raise ValueError("System unavailability mode=none does not accept periods")
        if not set(updates) <= {"mode", "periods"}:
            raise ValueError("Unknown field in system_unavailability_updates")
    elif mode == "periods":
        if not set(updates) <= {"mode", "periods"}:
            raise ValueError("Unknown field in system_unavailability_updates")
        periods = _validate_periods(updates.get("periods"))
    elif mode == "random":
        random_cfg = _validate_random_settings(updates)
    else:
        raise ValueError("System unavailability mode must be 'none', 'periods', or 'random'")

    if system is None:
        raise ValueError("System unavailability requires one Grid system")
    blocks = objects("".join(lines[system[0]:system[1] + 1]))
    root = blocks[0]
    if root.object_type != "pvSystem" or root.one("SystemType")[1] != "Grid":
        raise ValueError("System unavailability requires the owning Grid system")
    flag_index, raw = root.one("Flags")
    if not _HEX_FLAG.fullmatch(raw):
        raise ValueError("System unavailability requires hexadecimal Grid system Flags")
    flags_val = int(raw[1:], 16)
    before = system_unavail_state(lines, system)
    if "legacy_fields" in before:
        raise ValueError("Legacy outage fields are read-only; native migration is not verified")
    if "issue" in before or before.get("issues"):
        raise ValueError("Existing SystemUnavail block has structural issues and cannot be rewritten safely")
    if before["configured"]:
        if root.one("SystemUnavail")[1] != "TUnavailability" or len(blocks) != 2:
            raise ValueError("Require exactly one directly owned SystemUnavail object")
        child = blocks[1]
        if child.path != "PVObject_" or child.object_type != "pvUnavail" or child.one("Version")[1] not in NATIVE_TEXT_OR_SEED or child.one("Flags")[1] != "$00":
            raise ValueError(f"Require a valid {SEED_LABEL} pvUnavail record")
        first, last = root.one("SystemUnavail")[0], child.end_line
        if not 0 < first < child.line - 1 < last < root.end_line:
            raise ValueError("SystemUnavail lies outside its owning system")
    elif len(blocks) != 1:
        raise ValueError("Unexpected nested object inside pvSystem without SystemUnavail")

    candidate = list(lines)
    ending = "\r\n" if lines[system[0]].endswith("\r\n") else "\n"
    if mode == "none":
        if flags_val & 0x00010000:
            candidate[system[0] + flag_index] = replace_value(candidate[system[0] + flag_index], f"${flags_val & ~0x00010000:08X}")
        if before["configured"]:
            del candidate[system[0] + first:system[0] + last]
    else:
        if mode == "random":
            random_periods = _materialize_random_periods(random_cfg)
            new_block = _render_unavail_lines(random_periods, time_fraction_str=f"{random_cfg['time_fraction']:.4f}", min_d=random_cfg["min_duration_hours"],
                                              max_d=random_cfg["max_duration_hours"], random_seed=random_cfg["random_seed"], ending=ending)
        else:
            new_block = _format_unavail_block(periods, ending)
        candidate[system[0] + flag_index] = replace_value(candidate[system[0] + flag_index], f"${flags_val | 0x00010000:08X}")
        target_slice = slice(system[0] + first, system[0] + last) if before["configured"] else slice(system[1], system[1])
        candidate[target_slice] = new_block

    after = system_unavail_state(candidate, (system[0], system[1] + len(candidate) - len(lines)))
    if mode == "none" and after["configured"]:
        raise ValueError("Native outage removal did not clear the owning declaration")
    if mode == "periods" and (not after["configured"] or after.get("issues") or after["period_count"] != len(periods)):
        raise ValueError("Native outage creation failed structural verification")
    if mode == "random" and (not after["configured"] or after.get("issues") or after["period_count"] != random_cfg["period_count"]):
        raise ValueError("Native random outage creation failed structural verification")
    lines[:] = candidate
    return {"before": before, "after": after, "native_consumption_verified": False}

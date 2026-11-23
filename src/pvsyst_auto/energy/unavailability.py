"""Inspect, configure explicit periods, or remove native PV-system outages."""
from __future__ import annotations

from datetime import datetime, timedelta
import math
import random
import re

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value, root_block
from pvsyst_auto.versions import HEADLESS_RECORD_VERSION, NATIVE_TEXT_OR_SEED, SEED_LABEL

START = ("SystemUnavail", "TUnavailability")
SETTINGS_KEYS = ("TimeFraction", "NbPeriods", "MinDuration", "MaxDuration", "RandomInit")
LEGACY_KEYS = ("UnavTotDuration", "UnavNbPeriods", "UnavDuration", "UnavBegDate")
MAX_PERIODS = 1000
_HEX_FLAG = re.compile(r"\$[0-9A-Fa-f]{1,8}")
_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{2} \d{2}:\d{2}")


def _value(line):
    match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
    return None if match is None else (match[2], match[4])


def system_unavail_state(lines, system):
    if system is None:
        return {"configured": False, "reason": "no pvSystem section"}
    # Look for declarations using the same assignment grammar as the other readers.
    starts = []
    may_have_legacy = False
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
    end = next((i for i in range(start + 1, system[1])
                if lines[i].strip() == "End of PVObject pvUnavail"), None)
    if end is None:
        return {"configured": True, "issue": "SystemUnavail block is not closed", **legacy}
    settings, periods = {}, []
    issues = ["Legacy outage fields coexist with SystemUnavail"] if legacy else []
    mode = None
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
                    break  # the next period starts: never borrow its Duration
                if inner is not None and inner[0] == "Duration":
                    duration = inner[1]
                    break
            periods.append({"date_beg": value, "duration": duration})
    declared = settings.get("NbPeriods")
    # ASCII digits only: str.isdigit() also accepts e.g. "²", which int() then rejects.
    # In DefMode=Random without materialized DateBeg rows, NbPeriods belongs to RandomUnavSettings.
    if (declared is None or not declared.isascii() or not declared.isdigit()
            or ((mode != "Random" or len(periods) > 0) and int(declared) != len(periods))):
        issues.append("NbPeriods does not match the stored period list")
    undated = sum(period["duration"] is None for period in periods)
    if undated:
        issues.append(f"{undated} SystemUnavail period(s) lack a Duration within three lines of DateBeg")
    result = {"configured": True, "mode": mode, "settings": settings,
              "periods": periods, "period_count": len(periods), **legacy}
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
        date_beg = item["date_beg"]
        duration = item["duration_hours"]
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


def _format_unavail_block(periods: list[tuple[str, float]], ending: str) -> list[str]:
    total_hours = sum(dur for _, dur in periods)
    fraction = min(1.0, total_hours / 8760.0)
    min_d = max(1, int(round(min(dur for _, dur in periods))))
    max_d = max(min_d, int(round(max(dur for _, dur in periods))))
    block = [
        f"    SystemUnavail=TUnavailability{ending}",
        f"      PVObject_=pvUnavail{ending}",
        f"        Version={HEADLESS_RECORD_VERSION}{ending}",
        f"        Flags=$00{ending}",
        f"      DefMode=Periods{ending}",
        f"      RandomUnavSettings=TRandomUnavSettings{ending}",
        f"        TimeFraction={fraction:.3f}{ending}",
        f"        NbPeriods={len(periods)}{ending}",
        f"        MinDuration={min_d}{ending}",
        f"        MaxDuration={max_d}{ending}",
        f"        RandomInit =7{ending}",
        f"      EndTags{ending}",
        f"      UnavailPeriods=Start{ending}",
    ]
    for date_beg, dur in periods:
        block.extend([
            f"        DateBeg={date_beg}{ending}",
            f"        Duration={dur:.3f}{ending}",
            f"      EndTags{ending}",
        ])
    block.extend([
        f"      EndTags{ending}",
        f"      End of PVObject pvUnavail{ending}",
    ])
    return block


def _materialize_random_periods(cfg: dict) -> list[tuple[str, float]]:
    """Deterministically materialize random outage periods matching time_fraction, count, and duration bounds."""
    rng = random.Random(cfg["random_seed"])
    n = cfg["period_count"]
    min_d = cfg["min_duration_hours"]
    max_d = cfg["max_duration_hours"]
    target_total = cfg["time_fraction"] * 8760.0
    if not n * min_d <= target_total <= n * max_d:
        raise ValueError("time_fraction_pct is incompatible with period_count and duration bounds")
    # Allocate durations within [min_d, max_d] whose sum closely matches target_total
    durs = [float(min_d)] * n
    rem = max(0.0, target_total - sum(durs))
    indices = list(range(n))
    rng.shuffle(indices)
    for idx in indices:
        if rem <= 0.0:
            break
        cap = float(max_d - min_d)
        if cap <= 0.0:
            break
        take = min(cap, rng.uniform(0.0, min(cap, rem))) if n > 1 else min(cap, rem)
        durs[idx] += round(take, 1)
        rem = max(0.0, target_total - sum(durs))
    # Top up any remaining deficit evenly up to max_d
    for idx in indices:
        if rem <= 0.05:
            break
        room = float(max_d) - durs[idx]
        if room > 0.0:
            add = min(room, rem)
            durs[idx] = round(durs[idx] + add, 1)
            rem -= add

    # Spread non-overlapping start hours across a 365-day year (1990 synthetic year convention)
    start_year = datetime(1990, 1, 1, 0, 0)
    slot = 8760.0 / n
    periods: list[tuple[str, float]] = []
    last_end_h = 0.0
    for idx, dur in enumerate(durs):
        win_start = max(last_end_h + 1.0, idx * slot)
        win_end = max(win_start, (idx + 1) * slot - dur - 1.0)
        start_h = int(round(rng.uniform(win_start, min(8759.0 - dur, win_end)))) if win_end > win_start else int(round(win_start))
        start_h = max(int(math.ceil(last_end_h)), min(8759, start_h))
        dur_clamped = max(0.5, min(dur, float(8760 - start_h)))
        dt = start_year + timedelta(hours=start_h)
        periods.append((dt.strftime("%d/%m/%y %H:%M"), round(dur_clamped, 3)))
        last_end_h = start_h + dur_clamped
    return periods


def _format_random_unavail_block(cfg: dict, ending: str) -> list[str]:
    periods = _materialize_random_periods(cfg)
    block = [
        f"    SystemUnavail=TUnavailability{ending}",
        f"      PVObject_=pvUnavail{ending}",
        f"        Version={HEADLESS_RECORD_VERSION}{ending}",
        f"        Flags=$00{ending}",
        f"      DefMode=Periods{ending}",
        f"      RandomUnavSettings=TRandomUnavSettings{ending}",
        f"        TimeFraction={cfg['time_fraction']:.4f}{ending}",
        f"        NbPeriods={len(periods)}{ending}",
        f"        MinDuration={cfg['min_duration_hours']}{ending}",
        f"        MaxDuration={cfg['max_duration_hours']}{ending}",
        f"        RandomInit ={cfg['random_seed']}{ending}",
        f"      EndTags{ending}",
        f"      UnavailPeriods=Start{ending}",
    ]
    for date_beg, dur in periods:
        block.extend([
            f"        DateBeg={date_beg}{ending}",
            f"        Duration={dur:.3f}{ending}",
            f"      EndTags{ending}",
        ])
    block.extend([
        f"      EndTags{ending}",
        f"      End of PVObject pvUnavail{ending}",
    ])
    return block


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
    min_d = updates.get("min_duration_hours", 1)
    max_d = updates.get("max_duration_hours", 24)
    if type(min_d) is not int or type(max_d) is not int or not 1 <= min_d <= max_d <= 8760:
        raise ValueError("min_duration_hours and max_duration_hours must be integers with 1 <= min <= max <= 8760")
    seed = updates.get("random_seed", 7)
    if type(seed) is not int or not 0 <= seed <= 999999:
        raise ValueError("random_seed must be an integer in 0..999999")
    return {
        "time_fraction": float(tf_pct) / 100.0,
        "period_count": n_per,
        "min_duration_hours": min_d,
        "max_duration_hours": max_d,
        "random_seed": seed,
    }


def replace_system_unavailability(lines, system, updates):
    """Add/replace explicit outage periods, random outage settings, or remove existing SystemUnavail."""
    if not isinstance(updates, dict) or "mode" not in updates:
        raise ValueError("System unavailability updates require mode='none', 'periods', or 'random'")
    mode = updates["mode"]
    periods = None
    random_cfg = None
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
        if (child.path != "PVObject_" or child.object_type != "pvUnavail"
                or child.one("Version")[1] not in NATIVE_TEXT_OR_SEED
                or child.one("Flags")[1] != "$00"):
            raise ValueError(f"Require a valid {SEED_LABEL} pvUnavail record")
        first = root.one("SystemUnavail")[0]
        last = child.end_line
        if not 0 < first < child.line - 1 < last < root.end_line:
            raise ValueError("SystemUnavail lies outside its owning system")
    elif len(blocks) != 1:
        raise ValueError("Unexpected nested object inside pvSystem without SystemUnavail")

    candidate = list(lines)
    ending = "\r\n" if lines[system[0]].endswith("\r\n") else "\n"
    if mode == "none":
        if before["configured"]:
            if flags_val & 0x00010000:
                candidate[system[0] + flag_index] = replace_value(
                    candidate[system[0] + flag_index], f"${flags_val & ~0x00010000:08X}"
                )
            del candidate[system[0] + first:system[0] + last]
        elif flags_val & 0x00010000:
            candidate[system[0] + flag_index] = replace_value(
                candidate[system[0] + flag_index], f"${flags_val & ~0x00010000:08X}"
            )
    else:
        new_block = _format_random_unavail_block(random_cfg, ending) if mode == "random" else _format_unavail_block(periods, ending)
        candidate[system[0] + flag_index] = replace_value(
            candidate[system[0] + flag_index], f"${flags_val | 0x00010000:08X}"
        )
        if before["configured"]:
            candidate[system[0] + first:system[0] + last] = new_block
        else:
            candidate[system[1]:system[1]] = new_block

    delta = len(candidate) - len(lines)
    after = system_unavail_state(candidate, (system[0], system[1] + delta))
    if mode == "none" and after["configured"]:
        raise ValueError("Native outage removal did not clear the owning declaration")
    if mode == "periods" and (not after["configured"] or after.get("issues") or after["period_count"] != len(periods)):
        raise ValueError("Native outage creation failed structural verification")
    if mode == "random" and (not after["configured"] or after.get("issues") or after["period_count"] != random_cfg["period_count"]):
        raise ValueError("Native random outage creation failed structural verification")
    lines[:] = candidate
    return {"before": before, "after": after, "native_consumption_verified": False}

"""Inspect, configure explicit periods, or remove native PV-system outages."""
from __future__ import annotations

from datetime import datetime
import math
import re

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value
from pvsyst_auto.versions import HEADLESS_RECORD_VERSION, NATIVE_TEXT_OR_SEED, SEED_LABEL

START = "SystemUnavail=TUnavailability"
SETTINGS_KEYS = ("TimeFraction", "NbPeriods", "MinDuration", "MaxDuration", "RandomInit")
MAX_PERIODS = 1000
_HEX_FLAG = re.compile(r"\$[0-9A-Fa-f]{1,8}")
_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{2} \d{2}:\d{2}")


def _value(line):
    match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
    return None if match is None else (match[2], match[4])


def system_unavail_state(lines, system):
    if system is None:
        return {"configured": False, "reason": "no pvSystem section"}
    # Same assignment grammar as the other readers, so spacing around "=" is not "absent".
    starts = [i for i in range(system[0] + 1, system[1]) if _value(lines[i]) == tuple(START.split("="))]
    if not starts:
        return {"configured": False, "reason": None}
    if len(starts) != 1:
        return {"configured": True, "issue": "Requires exactly one SystemUnavail record"}
    start = starts[0]
    end = next((i for i in range(start + 1, system[1])
                if lines[i].strip() == "End of PVObject pvUnavail"), None)
    if end is None:
        return {"configured": True, "issue": "SystemUnavail block is not closed"}
    settings, periods, issues = {}, [], []
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
                return {"configured": True, "issue": f"SystemUnavail period list exceeds {MAX_PERIODS} entries"}
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
    if declared is None or not declared.isascii() or not declared.isdigit() or int(declared) != len(periods):
        issues.append("NbPeriods does not match the stored period list")
    undated = sum(period["duration"] is None for period in periods)
    if undated:
        issues.append(f"{undated} SystemUnavail period(s) lack a Duration within three lines of DateBeg")
    result = {"configured": True, "mode": mode, "settings": settings,
              "periods": periods, "period_count": len(periods)}
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


def replace_system_unavailability(lines, system, updates):
    """Add/replace explicit outage periods or remove an existing SystemUnavail child object."""
    if not isinstance(updates, dict) or "mode" not in updates:
        raise ValueError("System unavailability updates require mode='none' or mode='periods'")
    mode = updates["mode"]
    if mode == "none":
        if set(updates) - {"mode"} != set() and updates.get("periods") is not None:
            raise ValueError("System unavailability mode=none does not accept periods")
        if not set(updates) <= {"mode", "periods"}:
            raise ValueError("Unknown field in system_unavailability_updates")
    elif mode == "periods":
        if not set(updates) <= {"mode", "periods"}:
            raise ValueError("Unknown field in system_unavailability_updates")
        periods = _validate_periods(updates.get("periods"))
    else:
        raise ValueError("System unavailability mode must be 'none' or 'periods'")

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
    if before["configured"] and "issue" in before or before.get("issues"):
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
        new_block = _format_unavail_block(periods, ending)
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
    lines[:] = candidate
    return {"before": before, "after": after, "native_consumption_verified": False}

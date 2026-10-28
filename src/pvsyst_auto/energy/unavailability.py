"""Inspect or remove native PV-system outages; never generate replacement schedules."""
from __future__ import annotations

import re

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value

START = "SystemUnavail=TUnavailability"
SETTINGS_KEYS = ("TimeFraction", "NbPeriods", "MinDuration", "MaxDuration", "RandomInit")
MAX_PERIODS = 1000
SCOPE = ("stored random-outage settings and periods of the owning pvSystem; not a grid "
         "availability snapshot and not a regenerated schedule")


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
                return {"configured": True, "issue": "SystemUnavail period list exceeds the adapter ceiling"}
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
              "periods": periods, "period_count": len(periods), "scope": SCOPE}
    if issues:
        result["issues"] = issues
    return result


def replace_system_unavailability(lines, system, updates):
    """Remove the native child object, not just a flag that the loader can restore."""
    if not isinstance(updates, dict) or updates != {"mode": "none"}:
        raise ValueError("System unavailability supports only mode=none")
    if system is None:
        raise ValueError("System unavailability requires one Grid system")
    blocks = objects("".join(lines[system[0]:system[1] + 1]))
    root = blocks[0]
    if root.object_type != "pvSystem" or root.one("SystemType")[1] != "Grid":
        raise ValueError("System unavailability requires the owning Grid system")
    flag_index, raw = root.one("Flags")
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw) or int(raw[1:], 16) not in (0, 0x10000):
        raise ValueError("System unavailability requires plain Grid flags or its native outage tag")
    before = system_unavail_state(lines, system)
    expected = {"Flags", "SystemType"} | ({"SystemUnavail"} if before["configured"] else set())
    if set(root.fields) != expected or any(len(entries) != 1 for entries in root.fields.values()):
        raise ValueError("Unknown or repeated owning system fields")
    if before["configured"]:
        if root.one("SystemUnavail")[1] != "TUnavailability" or len(blocks) != 2:
            raise ValueError("Require exactly one directly owned SystemUnavail object")
        child = blocks[1]
        expected = {"Version", "Flags", "DefMode", "RandomUnavSettings", "UnavailPeriods",
                    "DateBeg", "Duration", *SETTINGS_KEYS}
        if (child.path != "PVObject_" or child.object_type != "pvUnavail" or set(child.fields) != expected
                or child.one("Version")[1] != "8.1.6" or child.one("Flags")[1] != "$00"
                or child.one("DefMode")[1] != "Periods"
                or child.one("RandomUnavSettings")[1] != "TRandomUnavSettings"
                or child.one("UnavailPeriods")[1] != "Start"
                or not 1 <= len(child.fields["DateBeg"]) <= MAX_PERIODS
                or len(child.fields["DateBeg"]) != len(child.fields["Duration"])):
            raise ValueError("Require a complete native 8.1.6 Periods outage template")
        for key in SETTINGS_KEYS:
            child.one(key)
        first = root.one("SystemUnavail")[0]
        last = child.end_line
        if not 0 < first < child.line - 1 < last < root.end_line:
            raise ValueError("SystemUnavail lies outside its owning system")
    elif len(blocks) != 1 or int(raw[1:], 16) != 0:
        raise ValueError("An absent outage object requires plain Grid flags")
    candidate = list(lines)
    if before["configured"]:
        if int(raw[1:], 16):
            candidate[system[0] + flag_index] = replace_value(candidate[system[0] + flag_index], "$" + "0" * (len(raw) - 1))
        del candidate[system[0] + first:system[0] + last]
    after = system_unavail_state(candidate, (system[0], system[1] - (len(lines) - len(candidate))))
    if after["configured"]:
        raise ValueError("Native outage removal did not clear the owning declaration")
    lines[:] = candidate
    return {"before": before, "after": after, "native_consumption_verified": False,
            "scope": "Remove existing PV-system outages only; restore the transaction backup to recover the original schedule"}

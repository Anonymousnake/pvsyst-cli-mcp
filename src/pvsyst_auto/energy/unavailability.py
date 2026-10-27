"""Read stored PV-system outages, never generate a schedule or edit its declaration."""
from __future__ import annotations

from pvsyst_auto.components.editor import ASSIGNMENT

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
    starts = [i for i in range(system[0] + 1, system[1]) if lines[i].strip() == START]
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

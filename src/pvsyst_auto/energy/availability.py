"""Bind a complete explicit grid-availability CSV to native pvUnavail hourly data."""
from __future__ import annotations

import calendar
import csv
import io
from datetime import datetime, timedelta

from pvsyst_auto.energy.profiles import _read, TIME_BASIS
from pvsyst_auto.project.paths import workspace_file


def native_availability(workspace, declaration, newline):
    year = declaration["year"]
    if type(year) is not int or not 1969 <= year <= 2068:
        raise ValueError("Declare an integer availability year in 1969..2068")
    if (declaration["time_basis"] != TIME_BASIS or type(declaration["utc_offset_hours"]) is not int
            or not -12 <= declaration["utc_offset_hours"] <= 14):
        raise ValueError("Declare a fixed whole-hour standard offset and local_standard_interval_start")
    source = workspace_file(workspace, "UserData", declaration["csv_name"], ".csv")
    original = _read(source)
    reader = csv.reader(io.StringIO(original.decode("utf-8-sig")), delimiter=";", strict=True)
    if next(reader) != ["Date", "Grid Available"] or next(reader) != ["", "ratio"]:
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
    lines = ["    PVObject_GridUnavail=pvUnavail", "      Comment=MCP explicit grid availability snapshot",
             "      Flags=$00", "      DefMode=Periods", "      HourlyVal=GrdUnav", "        Varident=GrdUnav",
             "        VarAcc=Hour", "        VarType=Factor", "        VarStore=Simul", "        VarModeSS=Aver",
             f"        DateBeg=01/01/{year % 100:02d} 00:00", f"        DateEnd=31/12/{year % 100:02d} 23:00",
             "        WRFactor=1000.0"]
    lines.extend("          Day_" + day + "=" + "".join(tokens) for day, tokens in days.items())
    lines.extend(["        End of Data", "      End of VarSimul=viGrdUnav", "    End of PVObject pvUnavail"])
    if _read(source) != original:
        raise ValueError("Availability source changed while preparing its native snapshot")
    metadata = {"year": year, "rows": rows, "unavailable_hours": unavailable,
                "utc_offset_hours": declaration["utc_offset_hours"], "time_basis": TIME_BASIS,
                "source": str(source), "binding": "native pvUnavail/HourlyVal GrdUnav Factor/Aver snapshot",
                "WRFactor": 1000, "encoded_value_error": 0}
    return newline.join(lines) + newline, metadata, source, original

"""Weather CSV inspection, standard-hourly/sub-hourly conversion to native SIT/MET, and comparison."""
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import math
import re
import subprocess
from datetime import datetime, timedelta
from itertools import zip_longest
from pathlib import Path

from pvsyst_auto.components.editor import objects
from pvsyst_auto.native.cli import PVsystError, build_monthly_weather_csv, iter_result_csv, result_units
from pvsyst_auto.project.paths import workspace_file, write_json
from pvsyst_auto.results import fingerprint

# --------------------------------------------------------------------- weather CSV inspection
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 110000
SUPPORTED_STEP_MINUTES = (5, 10, 15, 30, 60)
IDENTIFIERS = {
    "#Weather data hourly data", "#TMY hourly data", "#Meteo hourly data",
    "#Weather data sub-hourly data", "#Meteo sub-hourly data",
    "#Weather data subhourly data", "#Meteo subhourly data",
}
_TIME_STEP_MINUTES = {
    "hour": 60, "hourly": 60, "60": 60, "60 min": 60, "60min": 60, "1 h": 60, "1h": 60,
    "30": 30, "30 min": 30, "30min": 30, "15": 15, "15 min": 15, "15min": 15,
    "10": 10, "10 min": 10, "10min": 10, "5": 5, "5 min": 5, "5min": 5,
}
META = {"#Site", "#Country", "#Data Source", "#Time step", "#Year", "#Latitude", "#Longitude", "#Altitude",
        "#Time Zone", "#Time reference", "#Hour Shift", "#Time Shift", "#Plane tilt", "#Plane Azimuth",
        "#Albedo", "#Summarization period"}
IRRADIANCE = {"GHI", "DHI", "DNI", "GPI"}
OTHER_UNITS = {"Tamb": {"deg.C", "degC"}, "WindVel": {"m/s", "m/sec"},
               "RH": {"%"}, "Pw": {"cm"}, "Linke": {"", "-"}, "Aod": {"", "-"}}
DATE_COLUMNS = {"Year", "Month", "Day", "Hour", "Minute"}

def parse_time_step_minutes(value: str | None) -> int | None:
    """Parse PVsyst #Time step metadata into a supported step in minutes (5, 10, 15, 30, 60)."""
    return _TIME_STEP_MINUTES.get(" ".join(value.strip().lower().split())) if isinstance(value, str) else None

def number(value):
    if not value or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        raise ValueError("expected a finite decimal number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("expected a finite decimal number")
    return result

def integer(value):
    if not re.fullmatch(r"[0-9]{1,4}", value):
        raise ValueError("expected an integer date field")
    return int(value)

def read_bytes(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Weather CSV exceeds the 32 MiB reader limit")
    return data

def _inspect_weather(path: Path, expected_year, *, fingerprint_result: bool, step_minutes: int | None = None):
    if expected_year is not None and (type(expected_year) is not int or not 1 <= expected_year <= 9998):
        raise ValueError("expected_year must be an integer in 1..9998")
    if step_minutes is not None and (type(step_minutes) is not int or step_minutes not in SUPPORTED_STEP_MINUTES):
        raise ValueError(f"step_minutes must be one of {SUPPORTED_STEP_MINUTES}")
    data = read_bytes(path)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("Weather CSV must be UTF-8 text") from None
    if "\x00" in text:
        raise ValueError("Weather CSV must not contain binary NUL bytes")
    separator = None
    for candidate in (";", ","):
        try:
            for index, row in enumerate(csv.reader(io.StringIO(text), delimiter=candidate, strict=True)):
                if index >= 200:
                    break
                row = [v.strip() for v in row]
                if row and not row[0].startswith("#") and {"Month", "Day", "Hour"} <= set(row):
                    separator = candidate
                    break
        except csv.Error:
            continue
        if separator:
            break
    if separator is None:
        raise ValueError("Expected a PVsyst standard hourly CSV with Month, Day and Hour columns")
    issues, warnings, unchecked, issue_count = [], [], [], 0

    def issue(message):
        nonlocal issue_count
        issue_count += 1
        if len(issues) < 50:
            issues.append(message)

    try:
        reader = csv.reader(io.StringIO(text), delimiter=separator, strict=True)
        first_row = next(reader, [])
        if not first_row or first_row[0].strip() not in IDENTIFIERS:
            issue("First line must identify the PVsyst standard hourly format")
        metadata, headers, units = {}, None, None
        for row in reader:
            row = [v.strip() for v in row]
            if not any(row):
                continue
            if row[0].startswith("#"):
                if row[0] in META:
                    if row[0] in metadata:
                        issue(f"Duplicate metadata tag {row[0]}")
                    else:
                        metadata[row[0]] = row[1] if len(row) > 1 else ""
                continue
            headers, units = row, [v.strip() for v in next(reader, [])]
            break
        if headers is not None:
            while headers and not headers[-1]:
                headers.pop()
            if len(units) > len(headers) and not any(units[len(headers):]):
                del units[len(headers):]
        if headers is None or len(headers) > 64 or len(headers) != len(set(headers)) or "" in headers:
            raise ValueError("Expected 1..64 distinct nonempty column names")
        if len(units) != len(headers):
            raise ValueError("Unit row must have one cell per data column")
        unit_map = dict(zip(headers, units))
        missing = {"Month", "Day", "Hour", "Tamb"} - set(headers)
        if "Year" not in headers and not metadata.get("#Year"):
            missing.add("Year (or #Year metadata)")
        if missing:
            raise ValueError("Missing columns: " + ", ".join(sorted(missing)))
        if "GHI" not in headers:
            if "GPI" in headers:
                unchecked.append("POA-only weather: inverse transposition is outside this GHI validation profile")
            else:
                issue("Missing GHI irradiance column")
        for key in ("#Site", "#Country", "#Data Source", "#Time step", "#Latitude", "#Longitude", "#Altitude", "#Time Zone"):
            if not metadata.get(key):
                issue(f"Missing metadata {key}")
        declared_step = parse_time_step_minutes(metadata.get("#Time step"))
        resolved_step = step_minutes if step_minutes is not None else declared_step
        if declared_step is None:
            issue("#Time step must be Hour or a supported sub-hourly step (5/10/15/30 min)")
        elif step_minutes is not None and declared_step != step_minutes:
            issue(f"#Time step ({metadata.get('#Time step')}) differs from requested step_minutes={step_minutes}")
        if resolved_step is None:
            resolved_step = 60
        if resolved_step < 60 and "Minute" not in headers:
            issue(f"Sub-hourly ({resolved_step} min) weather CSV requires a Minute column")
        step_hours, numeric_meta = resolved_step / 60.0, {}
        for key, lower, upper in (("#Latitude", -90, 90), ("#Longitude", -180, 180), ("#Time Zone", -12, 14),
                                  ("#Altitude", None, None), ("#Albedo", 0, 1), ("#Hour Shift", None, None), ("#Time Shift", None, None)):
            if key not in metadata:
                continue
            try:
                value = number(metadata[key])
                if lower is not None and not lower <= value <= upper:
                    raise ValueError(f"expected {lower}..{upper}")
                numeric_meta[key] = value
            except ValueError as exc:
                issue(f"{key}: {exc}")
        if any(numeric_meta.get(k, 0) for k in ("#Hour Shift", "#Time Shift")):
            unchecked.append("Nonzero time shifts: coverage is reported on raw labels, not shifted MET records")
        if metadata.get("#Time reference"):
            unchecked.append("Explicit #Time reference: conversion to site legal winter time is not verified")
        if "GPI" in headers:
            unchecked.append("POA plane geometry and reverse transposition are not checked")
        if metadata.get("#Summarization period"):
            unchecked.append("Summarization periods and multi-year/TMY assembly are not checked")
        meta_year = None
        if "#Year" in metadata:
            try:
                cy = integer(metadata["#Year"])
                if not 1 <= cy <= 9998:
                    raise ValueError("year outside 1..9998")
                meta_year = cy
            except ValueError as exc:
                issue(f"#Year: {exc}")
        scales = {}
        for name in headers:
            if name in IRRADIANCE:
                scale = {"W/m2": 0.001 * step_hours, "MJ/m2": 1 / 3.6}.get(unit_map[name])
                if scale is None:
                    issue(f"{name}: expected W/m2 or MJ/m2 units")
                elif declared_step is not None:
                    scales[name] = scale
            elif name in OTHER_UNITS and unit_map[name] not in OTHER_UNITS[name]:
                issue(f"{name}: unsupported or missing unit {unit_map[name]!r}")
        timestamps, previous, years, rows = set(), None, set(), 0
        duplicates = out_of_order = off_grid = invalid_dates = 0
        first = last = None
        month_stats = {m: {"rows": 0, "GHI": [], "DHI": [], "Tamb": []} for m in range(1, 13)}
        numeric_columns = set(headers) & (IRRADIANCE | set(OTHER_UNITS))
        for row in reader:
            if not any(v.strip() for v in row):
                continue
            rows += 1
            if rows > MAX_ROWS:
                raise ValueError(f"Weather CSV exceeds the {MAX_ROWS}-row single-year reader limit")
            line = reader.line_num
            if len(row) > len(headers) and not any(v.strip() for v in row[len(headers):]):
                del row[len(headers):]
            if len(row) != len(headers):
                issue(f"Line {line}: data width differs from column header")
                invalid_dates += 1
                continue
            values, stamp = dict(zip(headers, (v.strip() for v in row))), None
            try:
                year = integer(values["Year"]) if "Year" in values else meta_year
                if year is None or not 1 <= year <= 9998:
                    raise ValueError("year outside 1..9998")
                if meta_year is not None and year != meta_year:
                    issue(f"Line {line}: data year differs from #Year")
                stamp = datetime(year, integer(values["Month"]), integer(values["Day"]),
                                 integer(values["Hour"]), integer(values.get("Minute", "0")))
                years.add(year)
                if stamp in timestamps:
                    duplicates += 1
                if previous is not None and stamp <= previous:
                    out_of_order += 1
                if stamp.minute % resolved_step:
                    off_grid += 1
                timestamps.add(stamp)
                first = stamp if first is None else min(first, stamp)
                last = stamp if last is None else max(last, stamp)
                previous = stamp
                month_stats[stamp.month]["rows"] += 1
            except ValueError as exc:
                invalid_dates += 1
                issue(f"Line {line}: invalid date ({exc})")
            numbers = {}
            for name in sorted(numeric_columns):
                try:
                    value = number(values[name])
                    if name in IRRADIANCE | {"WindVel", "RH", "Pw", "Linke", "Aod"} and value < 0:
                        raise ValueError("must not be negative")
                    if name == "RH" and value > 100:
                        raise ValueError("must not exceed 100 percent")
                    if name == "Tamb" and value <= -273.15:
                        raise ValueError("must be above absolute zero")
                    numbers[name] = value
                except ValueError as exc:
                    issue(f"Line {line}: {name} {exc}")
            if {"GHI", "DHI"} <= numbers.keys() and {"GHI", "DHI"} <= scales.keys():
                if numbers["DHI"] * scales["DHI"] > numbers["GHI"] * scales["GHI"] + 1e-9:
                    issue(f"Line {line}: DHI exceeds GHI after unit conversion")
            if stamp is not None:
                stats = month_stats[stamp.month]
                for name in ("GHI", "DHI", "Tamb"):
                    if name in numbers and (name in scales or (name == "Tamb" and unit_map[name] in OTHER_UNITS[name])):
                        stats[name].append(numbers[name] * scales.get(name, 1))
    except csv.Error as exc:
        raise ValueError(f"Invalid weather CSV: {exc}") from None
    year = expected_year if expected_year is not None else (next(iter(years)) if meta_year is None and len(years) == 1 else meta_year)
    if year is None:
        issue("Cannot determine one expected year; supply expected_year")
    expected = set()
    if year is not None:
        start, days = datetime(year, 1, 1), 366 if calendar.isleap(year) else 365
        expected = {start + timedelta(minutes=resolved_step * i) for i in range(days * (1440 // resolved_step))}
    missing, unexpected = expected - timestamps, timestamps - expected
    coverage = bool(expected) and not (missing or unexpected or duplicates or out_of_order or invalid_dates)
    if not coverage:
        cadence_label = "hourly" if resolved_step == 60 else f"{resolved_step}-minute sub-hourly"
        issue(f"Incomplete raw full-year {cadence_label} coverage: missing={len(missing)}, unexpected={len(unexpected)}, "
              f"duplicates={duplicates}, nonincreasing={out_of_order}, invalid_dates={invalid_dates}")
    if off_grid:
        warnings.append(f"{off_grid} records are not aligned to the {resolved_step}-minute grid; verify the provider's interval label convention")
    if "DHI" not in headers and "DNI" not in headers:
        warnings.append("No measured diffuse/direct column; native diffuse estimation needs separate review")
    if any(v < -60 or v > 60 for stats in month_stats.values() for v in stats["Tamb"]):
        warnings.append("Ambient temperatures outside -60..60 deg.C need source/units review")
    months = []
    for month, stats in month_stats.items():
        monthly = {"month": month, "rows": stats["rows"]}
        for name in ("GHI", "DHI", "Tamb"):
            values = stats[name]
            try:
                value = math.fsum(values) if values else None
            except OverflowError:
                raise ValueError("Monthly arithmetic exceeds finite numeric range") from None
            if value is not None and not math.isfinite(value):
                raise ValueError("Monthly arithmetic exceeds finite numeric range")
            if name == "Tamb" and value is not None:
                value /= len(values)
            monthly[name] = {"finite_samples": len(values), "observed_mean_degC" if name == "Tamb" else "observed_kwh_m2": value}
        months.append(monthly)
    if read_bytes(path) != data:
        raise ValueError("Weather CSV changed during inspection; retry with a stable file")
    return {
        "filename": path.name, **({"sha256": hashlib.sha256(data).hexdigest()} if fingerprint_result else {}),
        "format": "pvsyst_standard_hourly" if resolved_step == 60 else "pvsyst_standard_subhourly",
        "step_minutes": resolved_step, "delimiter": separator, "metadata": metadata, "units": unit_map,
        "ignored_columns": [n for n in headers if n not in numeric_columns | DATE_COLUMNS],
        "valid": False if issue_count else None if unchecked else True,
        "issues": issues, "issue_count": issue_count, "issues_truncated": issue_count > len(issues),
        "warnings": warnings, "checks_complete": not unchecked, "unchecked_checks": unchecked, "rows": rows,
        "expected_year": year, "expected_rows": len(expected), "raw_hourly_coverage_complete": coverage,
        "first": str(first) if first else None, "last": str(last) if last else None,
        "missing_hours": len(missing), "unexpected_timestamps": len(unexpected),
        "missing_examples": [str(s) for s in sorted(missing)[:12]],
        "duplicate_timestamps": duplicates, "nonincreasing_timestamps": out_of_order,
        "invalid_dates": invalid_dates, "nonzero_minute_records": off_grid, "months": months,
    }

def inspect_weather_csv(path: Path, expected_year: int | None = None, *, step_minutes: int | None = None):
    """Inspect a raw full-year hourly or sub-hourly (5/10/15/30/60 min) CSV and return its SHA-256."""
    return _inspect_weather(path, expected_year, fingerprint_result=True, step_minutes=step_minutes)

def inspect_weather_values(path: Path, expected_year: int | None = None, *, step_minutes: int | None = None):
    """Value-only twin of inspect_weather_csv (identical checks and key order, no SHA-256 hash)."""
    return _inspect_weather(path, expected_year, fingerprint_result=False, step_minutes=step_minutes)

# --------------------------------------------------------------------- native SIT/MET preparation & comparison
MEF_TEMPLATE = """PVObject_=pvMeteoFormat
  Comment=Standard hourly import, fixed local standard time
  Version=8.0.0
  Flags=$0043
  DateMode=ReadOnFile
  DateFmtType=YYxMMxDDxhhxmm
  FileType=SingleFile
  Separ=$002C
  NLigDeb=1
  MeasureStep=60
  TypeInterv=1
  UsesDST=False
  DateHEte=01/01/24
  DateHHiver=01/01/24
  RefTimeS=LegalTime
  TypeRE=1
  InclSolMF=0.0
  AzimSolMF=0.0
  AlbedoSolMF=0.2
  AlbedoMonMF=0.20,0.20,0.20,0.20,0.20,0.20,0.20,0.20,0.20,0.20,0.20,0.20,
  IsAlbedoMon=False
  TypeJourFich=1
  TypeMoisFich=1
  NCarFMask=8
  NoFieldDate=1,0,0,0,0,
  VarASCIIList, list of=3 tVarASCII
    VarASCII=1
      VarIdent=GlobHor
      VarType=Irrad
      NoField=2
      FieldMult=1.00000
      UnitId=W_m2
    End of TVarASCII
    VarASCII=2
      VarIdent=DiffHor
      VarType=Irrad
      NoField=3
      FieldMult=1.00000
      UnitId=W_m2
    End of TVarASCII
    VarASCII=3
      VarIdent=T_Amb
      VarType=Temper
      NoField=4
      FieldMult=1.00000
      UnitId=DegC
    End of TVarASCII
  End of List VarASCIIList
  NChampsAff=6
  LargChamps=17,19,19,17,17,12,
End of PVObject pvMeteoFormat
"""

MET_HEADER_LIMIT = 256 * 1024

def render_mef(step_minutes: int = 60) -> str:
    """Render a pvMeteoFormat (.MEF) definition for hourly or sub-hourly (5/10/15/30/60 min) CSVs."""
    if type(step_minutes) is not int or step_minutes not in SUPPORTED_STEP_MINUTES:
        raise ValueError(f"step_minutes must be one of {SUPPORTED_STEP_MINUTES}")
    if step_minutes == 60:
        return MEF_TEMPLATE
    return (MEF_TEMPLATE.replace("Comment=Standard hourly import, fixed local standard time",
                                 f"Comment=Standard {step_minutes}-minute sub-hourly import, fixed local standard time")
                        .replace("MeasureStep=60", f"MeasureStep={step_minutes}"))

def read_object_prefix(path, limit=MET_HEADER_LIMIT):
    """Parse owning Key=Value declarations from the leading prefix of a PVsyst object file."""
    with path.open("rb") as stream:
        data = stream.read(limit)
    segments, section, stop = {"root": {}, "SiteMet": {}}, "root", None
    for index, raw_line in enumerate(data.split(b"\n"), start=1):
        line = raw_line[3:].rstrip(b"\r") if index == 1 and raw_line.startswith(b"\xef\xbb\xbf") else raw_line.rstrip(b"\r")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = line.decode("cp1252")
            except UnicodeDecodeError:
                stop = {"line": index, "reason": "undecodable data"}
                break
        if any(ord(c) < 32 and c != "\t" for c in text):
            stop = {"line": index, "reason": "binary or control data"}
            break
        text = text.strip()
        if not text:
            continue
        if text.startswith("End of PVObject"):
            if text.split()[-1] == "pvMeteo":
                stop = {"line": index, "reason": "end of the pvMeteo object"}
                break
            section = "root"
            continue
        if "=" not in text:
            stop = {"line": index, "reason": "first non owning-field line: " + text[:60]}
            break
        key, _, value = text.partition("=")
        key, value = key.strip(), value.strip()
        if key == "PVObject_":
            continue
        if key.startswith("PVObject_"):
            if key.startswith("PVObject_SiteMet"):
                section = "SiteMet"
                continue
            stop = {"line": index, "reason": "unknown nested object, not treated as an owning field: " + key[:60]}
            break
        if key.startswith("VarMeteo, list of"):
            stop = {"line": index, "reason": "start of the measured value lists, after the declarations"}
            break
        segments[section].setdefault(key, []).append({"line": index, "value": value})
    return segments, stop

def declared_fields(segments, section, wanted):
    return {k: entries for k, entries in segments.get(section, {}).items() if k in wanted}

def first_value(fields, key):
    entries = fields.get(key)
    return entries[0]["value"] if entries else None

def inspect_met_calendar(path, year):
    """Check annual range declarations only without decoding native weather arrays."""
    segments, _ = read_object_prefix(path)
    fields = declared_fields(segments, "root", ("DateDebF", "DateFinF"))
    report = {"requested_year": year, "coverage": "unverified", "declarations": fields, "issues": [], "warnings": []}
    dates = {}
    for key in ("DateDebF", "DateFinF"):
        entries = fields.get(key, ())
        if len(entries) > 1:
            report["issues"].append(f"Native MET declares {key} more than once")
            continue
        if not entries:
            report["warnings"].append(f"Native MET {key} is missing; annual coverage remains unverified")
            continue
        for fmt in ("%d/%m/%y %H:%M", "%d/%m/%Y %H:%M"):
            try:
                dates[key] = datetime.strptime(entries[0]["value"], fmt)
                break
            except ValueError:
                continue
        if key not in dates:
            report["warnings"].append(f"Native MET {key} format is unreadable; annual coverage remains unverified")
    if report["issues"] or len(dates) != 2:
        return report
    begin, end = dates["DateDebF"], dates["DateFinF"]
    report.update(declared_start=begin.isoformat(" "), declared_end=end.isoformat(" "))
    if end < begin:
        report["issues"].append("Native MET DateFinF precedes DateDebF")
    elif begin <= datetime(year, 1, 1) and end >= datetime(year, 12, 31, 23):
        report["coverage"] = "declared"
    elif begin == datetime(1990, 1, 1) and datetime(1990, 12, 31, 23) <= end < datetime(1991, 1, 1):
        report["coverage"] = "generic_reference"
        report["warnings"].append("Native MET declares generic reference year 1990; compatibility with the requested year remains for the native run to check")
    else:
        report["coverage"] = "insufficient"
        report["issues"].append(f"Requested full year {year} is outside native MET declarations {begin:%Y-%m-%d %H:%M} through {end:%Y-%m-%d %H:%M}; "
                                "use a matching weather file/year, not the year in the filename")
    return report

def check_generated_met(path, *, time_zone, expected_step_minutes: int = 60):
    """Verify a freshly converted .MET header against the requested legal standard time basis."""
    if type(expected_step_minutes) is not int or expected_step_minutes not in SUPPORTED_STEP_MINUTES:
        raise ValueError(f"expected_step_minutes must be one of {SUPPORTED_STEP_MINUTES}")
    segments, stop = read_object_prefix(path)
    root_keys = ("IsSourceUt", "HourShift", "TimeShift", "TimeShiftTotalMin", "PasAccum", "VarMeteoStepDuration", "VarMeteoVarAcc")
    root, site = declared_fields(segments, "root", root_keys), declared_fields(segments, "SiteMet", ("HLeg", "TimeZone"))
    issues, unchecked = [], []
    for section, fields, keys in (("root", root, root_keys), ("SiteMet", site, ("HLeg", "TimeZone"))):
        for key in keys:
            if len(fields.get(key, ())) > 1:
                issues.append(f"Generated MET declares {section} {key} more than once; an ambiguous time declaration is refused")
    if (source_ut := first_value(root, "IsSourceUt")) is None:
        unchecked.append("Generated MET declares no root IsSourceUt")
    elif source_ut != "False":
        issues.append(f"Generated MET root declares IsSourceUt={source_ut}, but this local standard import profile requires a non-UTC source declaration (False)")
    if (legal := first_value(site, "HLeg")) is None:
        issues.append("Generated MET declares no SiteMet HLeg, so the legal-time basis is unverified")
    elif legal != "True":
        issues.append(f"Generated MET SiteMet declares HLeg={legal}, not legal time; the requested local standard CSV basis would not be reproduced")
    if (offset := first_value(site, "TimeZone")) is None:
        issues.append("Generated MET declares no SiteMet TimeZone")
    else:
        try:
            if float(offset) != float(time_zone):
                issues.append(f"Generated MET SiteMet TimeZone={offset} differs from the requested {time_zone}")
        except (TypeError, ValueError):
            issues.append(f"Generated MET SiteMet TimeZone={offset!r} is not a number")
    for key in ("HourShift", "TimeShift", "TimeShiftTotalMin"):
        if (val := first_value(root, key)) is None:
            unchecked.append(f"Generated MET declares no root {key}")
        elif val != "0":
            issues.append(f"Generated MET root declares {key}={val}, not 0")
    allowed_steps = (str(expected_step_minutes), f"{expected_step_minutes}.0")
    for key in ("PasAccum", "VarMeteoStepDuration"):
        if (val := first_value(root, key)) is None:
            unchecked.append(f"Generated MET declares no root {key}")
        elif val not in allowed_steps:
            issues.append(f"Generated MET root declares {key}={val}, not a {expected_step_minutes} minute step")
    if expected_step_minutes < 60:
        if (var_acc := first_value(root, "VarMeteoVarAcc")) is None:
            unchecked.append("Generated MET declares no root VarMeteoVarAcc for sub-hourly step")
        elif var_acc != "SubHour":
            issues.append(f"Generated MET root declares VarMeteoVarAcc={var_acc}, expected SubHour for {expected_step_minutes} minute step")
    return {"valid": not issues, "issues": issues, "unchecked_checks": unchecked,
            "step_minutes": expected_step_minutes, "header_read_stop": stop, "root": root, "site": site}

def normalized_rows(path, report):
    """Yield (timestamp, GHI, DHI, Tamb) converted to W/m2 and deg.C."""
    step_hours = report.get("step_minutes", 60) / 60.0
    reader = csv.reader(io.StringIO(read_bytes(path).decode("utf-8-sig")), delimiter=report["delimiter"], strict=True)
    for header in reader:
        header = [cell.strip() for cell in header]
        if header and header[0] and not header[0].startswith("#"):
            break
    next(reader)
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        values = dict(zip(header, (cell.strip() for cell in row)))
        stamp = datetime(int(values.get("Year", report["expected_year"])), int(values["Month"]),
                         int(values["Day"]), int(values["Hour"]), int(values.get("Minute", "0")))
        irrad = [number(values[n]) * (1 if report["units"][n] == "W/m2" else (1000 / 3.6) / step_hours) for n in ("GHI", "DHI")]
        yield stamp, *irrad, number(values["Tamb"])

def check_site(path, report, country_code):
    root, = [obj for obj in objects(read_bytes(path).decode("utf-8-sig")) if not obj.path]

    def value(key):
        entries = root.fields.get(key, [])
        if len(entries) != 1:
            raise ValueError("SIT requires one owning " + key)
        return entries[0][1]

    precision = {"Latitude": 0.0000501, "Longitude": 0.0000501, "Altitude": 0.500001, "TimeZone": 1e-6}
    for key in ("Latitude", "Longitude", "Altitude", "TimeZone"):
        tag = "#Time Zone" if key == "TimeZone" else "#" + key
        if not math.isclose(number(value(key)), number(report["metadata"][tag]), rel_tol=0, abs_tol=precision[key]):
            raise ValueError("Native SIT differs from weather CSV " + tag + " beyond its serialized precision")
    if value("HLeg") != "True" or value("Site") != report["metadata"]["#Site"]:
        raise ValueError("Native SIT name or legal-standard time differs from the requested CSV")
    if report["metadata"]["#Country"].casefold() not in (value("Country").casefold(), country_code.casefold()):
        raise ValueError("Native country label for the supplied ISO code differs from CSV #Country")
    for key, col, stat, tol in (("GlobalH", "GHI", "observed_kwh_m2", 0.051),
                                ("DiffuseH", "DHI", "observed_kwh_m2", 0.051),
                                ("TAmb", "Tamb", "observed_mean_degC", 0.0051)):
        vals = [number(c) for c in value(key).split(",") if c]
        if len(vals) != 13 or any(abs(vals[i] - month[col][stat]) > tol for i, month in enumerate(report["months"])):
            raise ValueError("Native SIT monthly " + key + " differs beyond its printed precision")
    return {"coordinates_timezone_months_match": True, "country": value("Country"),
            "coordinate_precision_deg": precision["Latitude"], "altitude_precision_m": precision["Altitude"],
            "monthly_precision_kwh_m2": 0.051, "monthly_temperature_precision_degC": 0.0051}

def _check_generated_met_for_step(met_path, met_tz, step_minutes):
    return (check_generated_met(met_path, time_zone=met_tz) if step_minutes == 60
            else check_generated_met(met_path, time_zone=met_tz, expected_step_minutes=step_minutes))

def prepare_weather(cli, *, csv_name="", year=None, sit_name="", met_name="", country_code="",
                    time_basis="", dry_run=True, step_minutes: int | None = None, allow_window_creation=None):
    with cli._lock:
        result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [], "warnings": [],
                  "artifacts": {}, "created_files": {}, "native_met_verified": False, "source_preserved": None}
        try:
            if type(dry_run) is not bool or type(year) is not int or not 1969 <= year <= 2068:
                raise ValueError("Use a strict boolean and integer year in 1969..2068")
            if step_minutes is not None and (type(step_minutes) is not int or step_minutes not in SUPPORTED_STEP_MINUTES):
                raise ValueError(f"step_minutes must be one of {SUPPORTED_STEP_MINUTES}")
            if time_basis != "local_standard_interval_start":
                raise ValueError("Declare local_standard_interval_start: fixed winter timezone, no DST, interval-start labels")
            if not isinstance(country_code, str) or not re.fullmatch(r"[A-Z]{2}", country_code):
                raise ValueError("country_code must be an explicit uppercase ISO alpha-2 code")
            source = workspace_file(cli.workspace, "Meteo", csv_name, ".csv")
            report = inspect_weather_csv(source, year, step_minutes=step_minutes)
            resolved_step, meta = report["step_minutes"], report["metadata"]
            result["inspection"] = report
            if report["valid"] is not True:
                raise ValueError("Weather CSV preflight: " + "; ".join(report["issues"] + report["unchecked_checks"]))
            for key, low, high, label in (("#Latitude", -90, 90, "Latitude"), ("#Longitude", -180, 180, "Longitude"), ("#Time Zone", -12, 12, "Timezone")):
                raw_val = meta.get(key)
                val = number(raw_val) if isinstance(raw_val, str) else raw_val
                if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not low <= val <= high:
                    raise ValueError(f"{label} outside [{low}, {high}]")
            raw_alt = meta.get("#Altitude")
            alt_val = number(raw_alt) if isinstance(raw_alt, str) else raw_alt
            if isinstance(alt_val, bool) or not isinstance(alt_val, (int, float)) or not math.isfinite(alt_val):
                raise ValueError("Altitude must be finite")
            extra = set(report["units"]) - DATE_COLUMNS - {"GHI", "DHI", "Tamb"}
            if "DHI" not in report["units"] or extra:
                raise ValueError("This verified import profile requires GHI, DHI, Tamb only; unmapped columns: " + ", ".join(sorted(extra)))
            for key in ("#Site", "#Country", "#Data Source"):
                t = meta[key]
                if not t or len(t.encode("utf-8")) > 200 or any(not c.isprintable() for c in t):
                    raise ValueError(key + " must be printable one-line text, at most 200 UTF-8 bytes")
            sit, met = workspace_file(cli.workspace, "Sites", sit_name, ".sit"), workspace_file(cli.workspace, "Meteo", met_name, ".met")
            stem = met.stem
            paths = {
                "sit": sit, "met": met,
                "normalized_csv": workspace_file(cli.workspace, "Meteo", stem + "_import.csv", ".csv"),
                "monthly_csv": workspace_file(cli.workspace, "Meteo", stem + "_monthly.csv", ".csv"),
                "mef": workspace_file(cli.workspace, "Meteo", stem + ".MEF", ".mef"),
                "manifest": workspace_file(cli.workspace, "Results", stem + "_weather.json", ".json"),
            }
            if any(p.exists() for p in paths.values()):
                raise FileExistsError("A destination already exists; choose new SIT/MET names")
            for option in ("site-name", "altitude", "timezone", "country-code"):
                cli._require("create-site", option, allow_window_creation=allow_window_creation)
            cli.command_help("convert-meteo", allow_window_creation=allow_window_creation)
            result.update(status="ready", source={"name": source.name, "sha256": report["sha256"]}, year=year,
                          step_minutes=resolved_step, country_code=country_code, time_basis=time_basis,
                          artifacts={k: str(p) for k, p in paths.items()},
                          mapping={"GHI": "GlobHor (W/m2)", "DHI": "DiffHor (W/m2)", "Tamb": "T_Amb (degC)"})
            result["warnings"].extend(report["warnings"])
            if dry_run:
                return result
        except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc:
            result["issues"].append(str(exc))
            return result
        manifest = paths["manifest"]
        manifest.parent.mkdir(parents=True, exist_ok=True)
        save = lambda first=False: write_json(manifest, result, first=first)
        save(first=True)
        try:
            result.update(status="running", stage="normalized_inputs")
            save()
            with paths["normalized_csv"].open("x", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(["Date", "GHI", "DHI", "Tamb"])
                writer.writerows([[s.strftime("%Y-%m-%d %H:%M"), g, d, t] for s, g, d, t in normalized_rows(source, report)])
            with paths["mef"].open("xb") as stream:
                stream.write(b"\xef\xbb\xbf" + render_mef(resolved_step).encode("ascii"))
            build_monthly_weather_csv(paths["monthly_csv"],
                                      [m["GHI"]["observed_kwh_m2"] for m in report["months"]],
                                      [m["Tamb"]["observed_mean_degC"] for m in report["months"]],
                                      [m["DHI"]["observed_kwh_m2"] for m in report["months"]])
            result["stage"] = "site"
            save()
            result["site_creation"] = cli.create_site_details(
                meta["#Site"], number(meta["#Latitude"]), number(meta["#Longitude"]), paths["monthly_csv"], sit,
                altitude=number(meta["#Altitude"]), timezone=number(meta["#Time Zone"]), country_code=country_code,
                source=meta["#Data Source"], log_level=3, allow_window_creation=allow_window_creation)
            result["warnings"].extend(result["site_creation"]["warnings"])
            result["site_checks"] = check_site(sit, report, country_code)
            result["stage"] = "conversion"
            save()
            result["conversion"] = cli.convert_meteo_details(paths["normalized_csv"], paths["mef"], sit, met,
                                                             timeshift=0, log_level=3, allow_window_creation=allow_window_creation)
            result["warnings"].extend(result["conversion"]["warnings"])
            result["stage"] = "met_declarations"
            save()
            result["met_declarations"] = _check_generated_met_for_step(met, number(meta["#Time Zone"]), resolved_step)
            result["warnings"].extend(result["met_declarations"]["unchecked_checks"])
            if not result["met_declarations"]["valid"]:
                raise ValueError("Generated MET time basis: " + "; ".join(result["met_declarations"]["issues"]))
            review = result["conversion"]["warnings"] or result["site_creation"]["warnings"] or result["met_declarations"]["unchecked_checks"]
            result.update(status="needs_review" if review else "prepared", stage="prepared")
        except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc:
            result.update(status="failed", error=cli._redact(str(exc)))
            if (diagnostics := getattr(exc, "diagnostics", None)) is not None:
                result["failure_diagnostics"] = diagnostics
        finally:
            try:
                result["source_preserved"] = fingerprint(source) == report["sha256"]
            except OSError:
                result["source_preserved"] = False
            if not result["source_preserved"]:
                result.update(status="failed", error="Source CSV changed during preparation")
            for key, path in paths.items():
                if key == "manifest":
                    continue
                try:
                    if path.is_file():
                        result["created_files"][key] = {"name": path.name, "sha256": fingerprint(path)}
                except OSError as exc:
                    result["created_files"][key] = {"name": path.name, "hash_error": str(exc)}
                    result["issues"].append("Artifact hash collection failed: " + key)
                    result["status"] = "failed"
            try:
                save()
            except (OSError, TypeError, ValueError) as exc:
                result["status"] = "failed"
                result["issues"].append("Weather manifest write failed: " + str(exc))
        return result

def load_weather_manifest(workspace, name, year, sit_name, met_name):
    path = workspace_file(workspace, "Results", name, ".json")
    if path.stat().st_size > 256 * 1024:
        raise ValueError("Weather manifest exceeds 256 KiB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Weather manifest must be a JSON object")
    conversion = data.get("conversion")
    if (not isinstance(conversion, dict) or not isinstance(conversion.get("diagnostics"), str)
            or not isinstance(conversion.get("warnings"), list) or any(not isinstance(i, str) for i in conversion["warnings"])):
        raise ValueError("Weather manifest requires a conversion record with diagnostics and string warnings")
    if (data.get("status") not in ("prepared", "needs_review") or data.get("year") != year
            or data.get("time_basis") != "local_standard_interval_start" or data.get("source_preserved") is not True):
        raise ValueError("Weather manifest must describe a prepared unchanged source and the requested year/time basis")
    assets = {"weather_manifest": {"path": str(path), "sha256": fingerprint(path)}}
    for key, folder, ext in (("sit", "Sites", ".sit"), ("met", "Meteo", ".met"), ("mef", "Meteo", ".mef"),
                             ("normalized_csv", "Meteo", ".csv"), ("monthly_csv", "Meteo", ".csv"), ("source", "Meteo", ".csv")):
        asset = data["source"] if key == "source" else data["created_files"][key]
        file = workspace_file(workspace, folder, asset["name"], ext)
        if fingerprint(file) != asset["sha256"]:
            raise ValueError("Weather preparation input/output changed: " + key)
        assets["weather_" + key] = {"path": str(file), "sha256": asset["sha256"]}
    if (data["created_files"]["sit"]["name"].casefold() != sit_name.casefold()
            or data["created_files"]["met"]["name"].casefold() != met_name.casefold()):
        raise ValueError("Weather manifest SIT/MET names differ from simulation inputs")
    manifest_step = data.get("step_minutes", 60)
    report = inspect_weather_csv(Path(assets["weather_source"]["path"]), year, step_minutes=manifest_step)
    if report["valid"] is not True:
        raise ValueError("Prepared source CSV no longer passes full-year inspection")
    check_site(Path(assets["weather_sit"]["path"]), report, data["country_code"])
    declarations = _check_generated_met_for_step(Path(assets["weather_met"]["path"]), number(report["metadata"]["#Time Zone"]), manifest_step)
    if not declarations["valid"]:
        raise ValueError("Prepared MET time basis: " + "; ".join(declarations["issues"]) + "; prepare new SIT/MET files from the original CSV with LegalTime")
    warnings = [*conversion["warnings"], *declarations["unchecked_checks"]]
    if data["status"] == "needs_review":
        warnings.append("Weather preparation remains needs_review; original review state is retained")
    return assets, report, warnings

def compare_native_weather(csv_path, source, report):
    """Compare native hourly/sub-hourly weather against the prepared CSV row by row."""
    units = result_units(csv_path, strict=True)
    for key in ("GlobHor", "DiffHor"):
        if units.get(key) not in ("W/m2", "W/m\u00b2"):
            raise ValueError("Native weather comparison requires " + key + " in W/m2")
    if units.get("T_Amb") not in ("degC", "deg.C", "\u00b0C"):
        raise ValueError("Native weather comparison requires T_Amb in degrees C")
    names = ("GlobHor", "DiffHor", "T_Amb")
    stats = {k: {"max_abs_delta": 0.0, "signed_delta_sum": 0.0, "outside_review_threshold": 0,
                 "samples": 0, "max_abs": 0.0, "abs_sum": 0.0, "squared_sum": 0.0} for k in names}
    months = {m: {k: {"samples": 0, "signed_delta_sum": 0.0, "max_abs_delta": 0.0} for k in names} for m in range(1, 13)}
    count, previous, steps = 0, None, set()
    for native, expected in zip_longest(iter_result_csv(csv_path, strict=True), normalized_rows(source, report)):
        if native is None or expected is None or native[1][0] != expected[0]:
            raise ValueError("Native weather labels/row count differ from the prepared CSV")
        header, row = native
        stamp = expected[0]
        if previous is not None:
            steps.add(round((stamp - previous).total_seconds() / 60.0, 6))
        previous = stamp
        for key, ref, thresh in zip(names, expected[1:], (0.1, 0.1, 0.01)):
            if header.count(key) != 1 or not math.isfinite(row[header.index(key)]):
                raise ValueError("Missing/ambiguous/nonfinite native weather: " + key)
            delta = row[header.index(key)] - ref
            s, m = stats[key], months[stamp.month][key]
            s["max_abs_delta"] = s["max_abs"] = max(s["max_abs_delta"], abs(delta))
            s["signed_delta_sum"] += delta
            s["outside_review_threshold"] += abs(delta) > thresh
            s["samples"] += 1
            s["abs_sum"] += abs(delta)
            s["squared_sum"] += delta * delta
            m["samples"] += 1
            m["signed_delta_sum"] += delta
            m["max_abs_delta"] = max(m["max_abs_delta"], abs(delta))
        count += 1
    for s in stats.values():
        n = s["samples"]
        s["mean_signed"] = s["signed_delta_sum"] / n if n else None
        s["mae"] = s["abs_sum"] / n if n else None
        s["rmse"] = math.sqrt(s["squared_sum"] / n) if n else None
        del s["abs_sum"], s["squared_sum"]
    monthly = [
        {"month": m, "samples": months[m]["GlobHor"]["samples"],
         **{k: {"samples": months[m][k]["samples"], "signed_delta_sum": months[m][k]["signed_delta_sum"],
                "mean_signed": (months[m][k]["signed_delta_sum"] / months[m][k]["samples"] if months[m][k]["samples"] else None),
                "max_abs_delta": months[m][k]["max_abs_delta"]} for k in names}}
        for m in range(1, 13)
    ]
    step_minutes = sorted(steps)
    regular_hourly = step_minutes == [60.0]
    regular_step = step_minutes[0] if len(step_minutes) == 1 and step_minutes[0] in tuple(float(s) for s in SUPPORTED_STEP_MINUTES) else None
    return {
        "rows": count, "timestamps_match": True, "columns": stats,
        "needs_review": any(s["outside_review_threshold"] for s in stats.values()),
        "review_thresholds": {"irradiance_w_m2": 0.1, "temperature_degC": 0.01},
        "step_minutes": step_minutes, "regular_hourly_step": regular_hourly, "regular_step_minutes": regular_step,
        "signed_energy_delta_kwh_m2": {k: (stats[k]["signed_delta_sum"] * (regular_step / 60.0) / 1000.0 if regular_step is not None else None) for k in ("GlobHor", "DiffHor")},
        "temperature_mean_delta_degC": stats["T_Amb"]["mean_signed"],
        "monthly": monthly, "reported_delta_measures": ["MAE", "RMSE", "mean_signed", "max_abs"],
    }

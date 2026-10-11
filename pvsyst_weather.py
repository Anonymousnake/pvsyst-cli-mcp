"""Read-only checks for the public PVsyst standard hourly weather CSV format."""
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 20000
IDENTIFIERS = {"#Weather data hourly data", "#TMY hourly data", "#Meteo hourly data"}
META = {"#Site", "#Country", "#Data Source", "#Time step", "#Year", "#Latitude",
        "#Longitude", "#Altitude", "#Time Zone", "#Time reference", "#Hour Shift",
        "#Time Shift", "#Plane tilt", "#Plane Azimuth", "#Albedo", "#Summarization period"}
IRRADIANCE = {"GHI", "DHI", "DNI", "GPI"}
OTHER_UNITS = {"Tamb": {"deg.C", "degC"}, "WindVel": {"m/s", "m/sec"},
               "RH": {"%"}, "Pw": {"cm"}, "Linke": {"", "-"}, "Aod": {"", "-"}}
DATE_COLUMNS = {"Year", "Month", "Day", "Hour", "Minute"}


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


def inspect_weather_csv(path: Path, expected_year: int | None = None):
    """Inspect one raw hourly year; never fill gaps, relabel time or alter units."""
    if expected_year is not None and (type(expected_year) is not int or not 1 <= expected_year <= 9998):
        raise ValueError("expected_year must be an integer in 1..9998")
    data = read_bytes(path)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("Weather CSV must be UTF-8 text") from None
    if "\x00" in text:
        raise ValueError("Weather CSV must not contain binary NUL bytes")
    # Choose the separator from the date header, not punctuation in free text.
    separator = None
    for candidate in (";", ","):
        reader = csv.reader(io.StringIO(text), delimiter=candidate, strict=True)
        try:
            for index, row in enumerate(reader):
                if index >= 200:
                    break
                row = [value.strip() for value in row]
                if row and not row[0].startswith("#") and {"Month", "Day", "Hour"} <= set(row):
                    separator = candidate
                    break
        except csv.Error:
            continue
        if separator:
            break
    if separator is None:
        raise ValueError("Expected a PVsyst standard hourly CSV with Month, Day and Hour columns")

    issues, warnings, unchecked = [], [], []
    issue_count = 0

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
            row = [value.strip() for value in row]
            if not any(row):
                continue
            if row[0].startswith("#"):
                if row[0] in META:
                    if row[0] in metadata:
                        issue(f"Duplicate metadata tag {row[0]}")
                    else:
                        metadata[row[0]] = row[1] if len(row) > 1 else ""
                continue
            headers = row
            units = [value.strip() for value in next(reader, [])]
            break
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
        for key in ("#Site", "#Country", "#Data Source", "#Time step", "#Latitude",
                    "#Longitude", "#Altitude", "#Time Zone"):
            if not metadata.get(key):
                issue(f"Missing metadata {key}")
        if metadata.get("#Time step") != "Hour":
            issue("#Time step must be Hour")
        numeric_meta = {}
        for key, lower, upper in (("#Latitude", -90, 90), ("#Longitude", -180, 180),
                                  ("#Time Zone", -12, 14), ("#Altitude", None, None),
                                  ("#Albedo", 0, 1), ("#Hour Shift", None, None),
                                  ("#Time Shift", None, None)):
            if key not in metadata:
                continue
            try:
                value = number(metadata[key])
                if lower is not None and not lower <= value <= upper:
                    raise ValueError(f"expected {lower}..{upper}")
                numeric_meta[key] = value
            except ValueError as exc:
                issue(f"{key}: {exc}")
        if any(numeric_meta.get(key, 0) for key in ("#Hour Shift", "#Time Shift")):
            unchecked.append("Nonzero time shifts: coverage is reported on raw labels, not shifted MET records")
        reference = metadata.get("#Time reference")
        if reference:
            unchecked.append("Explicit #Time reference: conversion to site legal winter time is not verified")
        if "GPI" in headers:
            unchecked.append("POA plane geometry and reverse transposition are not checked")
        if metadata.get("#Summarization period"):
            unchecked.append("Summarization periods and multi-year/TMY assembly are not checked")
        meta_year = None
        if "#Year" in metadata:
            try:
                candidate_year = integer(metadata["#Year"])
                if not 1 <= candidate_year <= 9998:
                    raise ValueError("year outside 1..9998")
                meta_year = candidate_year
            except ValueError as exc:
                issue(f"#Year: {exc}")
        scales = {}
        for name in headers:
            if name in IRRADIANCE:
                scale = {"W/m2": 0.001, "MJ/m2": 1 / 3.6}.get(unit_map[name])
                if scale is None:
                    issue(f"{name}: expected W/m2 or MJ/m2 hourly units")
                elif metadata.get("#Time step") == "Hour":
                    scales[name] = scale
            elif name in OTHER_UNITS and unit_map[name] not in OTHER_UNITS[name]:
                issue(f"{name}: unsupported or missing unit {unit_map[name]!r}")

        timestamps, previous, years = set(), None, set()
        rows = duplicates = out_of_order = off_grid = invalid_dates = 0
        first = last = None
        month_stats = {m: {"rows": 0, "GHI": [], "DHI": [], "Tamb": []} for m in range(1, 13)}
        numeric_columns = set(headers) & (IRRADIANCE | set(OTHER_UNITS))
        for row in reader:
            if not any(value.strip() for value in row):
                continue
            rows += 1
            if rows > MAX_ROWS:
                raise ValueError("Weather CSV exceeds the 20000-row single-year reader limit")
            line = reader.line_num
            if len(row) != len(headers):
                issue(f"Line {line}: data width differs from column header")
                invalid_dates += 1
                continue
            values = dict(zip(headers, (value.strip() for value in row)))
            stamp = None
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
                if stamp.minute:
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
                    if name in numbers and (name in scales or
                                            (name == "Tamb" and unit_map[name] in OTHER_UNITS[name])):
                        stats[name].append(numbers[name] * scales.get(name, 1))
    except csv.Error as exc:
        raise ValueError(f"Invalid weather CSV: {exc}") from None

    year = expected_year if expected_year is not None else meta_year
    if year is None and len(years) == 1:
        year = next(iter(years))
    if year is None:
        issue("Cannot determine one expected year; supply expected_year")
    expected = set()
    if year is not None:
        start = datetime(year, 1, 1)
        expected = {start + timedelta(hours=i) for i in range(8784 if calendar.isleap(year) else 8760)}
    missing, unexpected = expected - timestamps, timestamps - expected
    coverage = bool(expected) and not (missing or unexpected or duplicates or out_of_order or invalid_dates)
    if not coverage:
        issue(f"Incomplete raw full-year hourly coverage: missing={len(missing)}, unexpected={len(unexpected)}, "
              f"duplicates={duplicates}, nonincreasing={out_of_order}, invalid_dates={invalid_dates}")
    if off_grid:
        warnings.append(f"{off_grid} records have nonzero minutes; verify the provider's interval label convention")
    if "DHI" not in headers and "DNI" not in headers:
        warnings.append("No measured diffuse/direct column; native diffuse estimation needs separate review")
    if any(value < -60 or value > 60 for stats in month_stats.values() for value in stats["Tamb"]):
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
    return {"filename": path.name, "sha256": hashlib.sha256(data).hexdigest(),
            "format": "pvsyst_standard_hourly", "delimiter": separator, "metadata": metadata,
            "units": unit_map, "ignored_columns": [name for name in headers if name not in numeric_columns | DATE_COLUMNS],
            "valid": False if issue_count else None if unchecked else True,
            "issues": issues, "issue_count": issue_count, "issues_truncated": issue_count > len(issues),
            "warnings": warnings, "checks_complete": not unchecked, "unchecked_checks": unchecked, "rows": rows,
            "expected_year": year, "expected_rows": len(expected), "raw_hourly_coverage_complete": coverage,
            "first": str(first) if first else None, "last": str(last) if last else None,
            "missing_hours": len(missing), "unexpected_timestamps": len(unexpected),
            "missing_examples": [str(stamp) for stamp in sorted(missing)[:12]],
            "duplicate_timestamps": duplicates, "nonincreasing_timestamps": out_of_order,
            "invalid_dates": invalid_dates, "nonzero_minute_records": off_grid, "months": months,
            "scope": "Raw standard CSV, explicit units and one full hourly year. No gap filling, time shifting, "
                     "MET parsing, MEF/SIT matching, clear-sky quality check or native conversion certification."}

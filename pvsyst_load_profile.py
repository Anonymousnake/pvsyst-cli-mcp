"""Strict full-year hourly load import for the native CSV reader; no resampling."""
from __future__ import annotations

import calendar
import csv
import io
import json
import math
from datetime import datetime, timedelta

from pvsyst_paths import workspace_file
from pvsyst_weather import number

MAX_BYTES = 8 * 1024 * 1024
TIME_BASIS = "local_standard_interval_start"
DATE_FORMATS = {"iso": "%Y-%m-%d %H:%M", "pvsyst_dot": "%d.%m.%Y %H:%M",
                "pvsyst_slash": "%d/%m/%Y %H:%M"}
UNITS = {"W": 0.001, "[W]": 0.001, "kW": 1.0, "[kW]": 1.0}


def _read(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Load CSV exceeds 8 MiB")
    return data


def _inspect_load(raw, *, year, date_format="iso", date_column="Date", load_column="P Load"):
    """Check one coherent byte snapshot of the full year's interval-start powers."""
    if type(year) is not int or not 1969 <= year <= 2068:
        raise ValueError("Use a strict integer year in 1969..2068")
    if date_format not in DATE_FORMATS:
        raise ValueError("Use iso, pvsyst_dot or pvsyst_slash date_format")
    if (not isinstance(date_column, str) or not isinstance(load_column, str)
            or not date_column or not load_column or date_column == load_column):
        raise ValueError("Declare distinct date and load column names")
    text = raw.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("Load CSV must not contain NUL bytes")
    header = rows = None
    separator = None
    for delimiter in (";", ","):
        parsed = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
        for index, cells in enumerate(parsed):
            if index > 100:
                break
            cells = [cell.strip() for cell in cells]
            if not any(cells) or cells[0].startswith("#"):
                continue
            if len(cells) == 2 and set(cells) == {date_column, load_column}:
                header, rows, separator = cells, parsed, delimiter
            break
        if header is not None:
            break
    if rows is None:
        raise ValueError("Expected exactly the declared date/load columns and a following units row")
    units = next(rows, None)
    if units is None or len(units) != 2:
        raise ValueError("Load CSV needs an explicit two-column units row")
    load_index, date_index = header.index(load_column), header.index(date_column)
    unit = units[load_index].strip()
    if unit not in UNITS:
        raise ValueError("Load power unit must be W or kW (bracketed aliases allowed); no unit is inferred")
    expected = 24 * (366 if calendar.isleap(year) else 365)
    beginning = datetime(year, 1, 1)
    values, months, issues = [], [[] for _ in range(12)], []
    seen, duplicate_hours, missing_hours = set(), 0, 0
    for cells in rows:
        if not any(cell.strip() for cell in cells) or cells[0].lstrip().startswith("#"):
            continue
        index = len(values)
        if index >= expected:
            raise ValueError("Load CSV has more than one full year's hourly rows")
        if len(cells) != 2:
            raise ValueError("Each load row must contain exactly two columns")
        stamp = datetime.strptime(cells[date_index].strip(), DATE_FORMATS[date_format])
        power = number(cells[load_index].strip()) * UNITS[unit]
        if power < 0:
            raise ValueError("Load is nonnegative consumption, not a net import/export meter")
        wanted = beginning + timedelta(hours=index)
        if stamp in seen:
            duplicate_hours += 1
        seen.add(stamp)
        if stamp != wanted and len(issues) < 20:
            issues.append(f"Row {index + 1}: expected {wanted.isoformat()}, found {stamp.isoformat()}")
        if stamp.year == year:
            months[stamp.month - 1].append(power)
        values.append((stamp, power))
    if len(values) != expected:
        issues.append(f"Expected {expected} hourly rows for {year}, found {len(values)}")
    valid_hours = {stamp for stamp in seen if stamp.year == year and stamp.minute == stamp.second == 0}
    missing_hours = expected - len(valid_hours)
    try:
        annual = math.fsum(power for _, power in values)
        monthly = [math.fsum(month) for month in months]
    except OverflowError:
        raise ValueError("Annual and monthly load totals must remain finite") from None
    report = {"valid": not issues, "year": year, "rows": len(values), "expected_rows": expected,
              "missing_hours": missing_hours, "duplicate_hours": duplicate_hours,
              "first": values[0][0].isoformat() if values else None,
              "last": values[-1][0].isoformat() if values else None,
              "source_unit": unit, "output_unit": "kW", "delimiter": separator,
              "annual_load_kwh": annual,
              "max_load_kw": max((power for _, power in values), default=0),
              "monthly_load_kwh": monthly, "issues": issues}
    return report


def inspect_load_profile(path, *, year, date_format="iso", date_column="Date", load_column="P Load"):
    """Inspect a concrete year's hourly powers; return values, not fingerprints."""
    raw = _read(path)
    report = _inspect_load(raw, year=year, date_format=date_format,
                           date_column=date_column, load_column=load_column)
    if _read(path) != raw:
        raise ValueError("Load CSV changed while it was being inspected")
    return report


def prepare_load_profile(cli, *, csv_name, year, output_name, utc_offset_hours,
                         time_basis, date_format="iso", date_column="Date",
                         load_column="P Load", dry_run=True):
    """Normalize verified hourly loads into a NEW ASCII native CSV and an import record."""
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run,
              "issues": [], "warnings": [], "artifacts": {}, "source_preserved": None,
              "native_consumption_verified": False}
    with cli._lock:
        try:
            if type(dry_run) is not bool or type(utc_offset_hours) is not int or not -12 <= utc_offset_hours <= 14:
                raise ValueError("Use a strict boolean and an explicit whole-hour UTC offset in -12..14")
            if time_basis != TIME_BASIS:
                raise ValueError("Declare local_standard_interval_start: fixed standard offset, no DST")
            source = workspace_file(cli.workspace, "UserData", csv_name, ".csv")
            target = workspace_file(cli.workspace, "UserHourlyParams", output_name, ".csv")
            record = workspace_file(cli.workspace, "Results", target.stem + "_load.json", ".json")
            for path in (target, record):
                if path.exists() or (path.parent.exists() and any(
                        item.name.casefold() == path.name.casefold() for item in path.parent.iterdir())):
                    raise FileExistsError("Choose new load CSV/import record names: " + path.name)
            original = _read(source)
            report = _inspect_load(original, year=year, date_format=date_format,
                                   date_column=date_column, load_column=load_column)
            result["inspection"] = report
            if not report["valid"]:
                result["issues"].extend(report["issues"])
                return result
            # ponytail: reuse the same two-column format, with only explicit power-unit conversion.
            reader = csv.reader(io.StringIO(original.decode("utf-8-sig")), delimiter=report["delimiter"], strict=True)
            for header in reader:
                header = [cell.strip() for cell in header]
                if header and header[0] and not header[0].startswith("#"):
                    break
            units = next(reader)
            column_date, column_load = header.index(date_column), header.index(load_column)
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, delimiter=";", lineterminator="\r\n")
            writer.writerow(["Date", "P Load"])
            writer.writerow(["", "kW"])
            for cells in reader:
                if not any(cell.strip() for cell in cells) or cells[0].lstrip().startswith("#"):
                    continue
                stamp = datetime.strptime(cells[column_date].strip(), DATE_FORMATS[date_format])
                power = number(cells[column_load].strip()) * UNITS[units[column_load].strip()]
                writer.writerow([stamp.strftime(DATE_FORMATS["pvsyst_dot"]), format(power, ".12g")])
            output = stream.getvalue().encode("ascii")
            result.update(status="ready", load_record_version=1,
                          source={"name": source.name, "folder": "UserData", "date_format": date_format,
                                  "date_column": date_column, "load_column": load_column},
                          year=year, time_basis=time_basis, utc_offset_hours=utc_offset_hours,
                          artifacts={"csv": str(target), "manifest": str(record)},
                          normalized_format={"header": ["Date", "P Load"], "units": ["", "kW"],
                                             "date_format": "pvsyst_dot", "encoding": "ASCII", "separator": ";"})
            result["warnings"].append("Native calendar lookup ignores the input year; this importer verifies the requested year and leap day explicitly. No filling, resampling, annual renormalization or DST shift is performed.")
            result["warnings"].append("A prepared CSV alone does not prove the variant reads it; bind and verify the native hourly load output before claiming consumption.")
            if _read(source) != original:
                raise ValueError("Source load CSV changed during preparation")
            result["source_preserved"] = True
            if dry_run:
                return result
            result["stage"] = "write"
            target.parent.mkdir(parents=True, exist_ok=True)
            record.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as destination:
                destination.write(output)
            normalized = inspect_load_profile(target, year=year, date_format="pvsyst_dot")
            if not normalized["valid"] or normalized["rows"] != report["rows"]:
                raise ValueError("Normalized hourly load coverage differs from input")
            if not math.isclose(normalized["annual_load_kwh"], report["annual_load_kwh"], rel_tol=1e-11, abs_tol=1e-9):
                raise ValueError("Normalized annual load changed beyond numeric serialization precision")
            if _read(source) != original:
                raise ValueError("Source load CSV changed while writing outputs")
            result["normalized_inspection"] = normalized
            payload = {**result, "status": "prepared", "stage": "prepared"}
            with record.open("x", encoding="utf-8") as destination:
                json.dump(payload, destination, ensure_ascii=True, indent=2, allow_nan=False)
            result.update(status="prepared", stage="prepared")
        except (ValueError, OSError, UnicodeError, csv.Error) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append("Retained output paths may contain partial artifacts; inspect them before retrying with new names.")
    return result

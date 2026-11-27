"""Hourly load profiles, grid availability snapshots, and system unavailability periods."""
from __future__ import annotations

import calendar
import csv
import io
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath

from pvsyst_auto.components.editor import objects, replace_value, root_block
from pvsyst_auto.energy.unavailability import (
    _DATE_RE,
    _HEX_FLAG,
    LEGACY_KEYS,
    MAX_BYTES,
    MAX_PERIODS,
    SETTINGS_KEYS,
    START,
    TIME_BASIS,
    _format_unavail_block,
    _materialize_random_periods,
    _read,
    _render_unavail_lines,
    _render_unavail_snapshot,
    _validate_periods,
    _validate_random_settings,
    _value,
    generate_outage_availability,
    native_availability,
    replace_system_unavailability,
    system_unavail_state,
)
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.project.variant_updates import run_new_variant_transaction
from pvsyst_auto.results import POWER_TO_KW
from pvsyst_auto.validation import HEADLESS_RECORD_VERSION, NATIVE_TEXT_OR_SEED, SEED_LABEL
from pvsyst_auto.weather.preparation import number

DATE_FORMATS = {
    "iso": "%Y-%m-%d %H:%M",
    "pvsyst_dot": "%d.%m.%Y %H:%M",
    "pvsyst_slash": "%d/%m/%Y %H:%M",
}
UNITS = {key: scale for unit, scale in POWER_TO_KW.items() for key in (unit, f"[{unit}]")}
DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

PEAK_SHAVING_LOAD_ERROR = (
    "PVsyst cannot run PeakShaving storage with a self-consumption load (pvLoad); "
    "use a source variant without a self-consumption load"
)


def has_peak_shaving_storage(lines) -> bool:
    """Return True when the variant declares a pvGridStorage object with Strategy=PeakShaving."""
    return any(
        block.object_type == "pvGridStorage"
        and any(value == "PeakShaving" for _, value in block.fields.get("Strategy", ()))
        for block in objects("".join(lines))
    )


def _read_header_and_units(text: str, delimiter: str, date_column: str, load_column: str):
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
    header = None
    for index, cells in enumerate(reader):
        if index > 100:
            break
        cells = [cell.strip() for cell in cells]
        if not any(cells) or cells[0].startswith("#"):
            continue
        if len(cells) == 2 and set(cells) == {date_column, load_column}:
            header = cells
        break
    if header is None:
        return None
    units = next(reader, None)
    return header, units, reader


def _inspect_load(raw, *, year, date_format="iso", date_column="Date", load_column="P Load"):
    """Check one coherent byte snapshot of the full year's interval-start powers."""
    if type(year) is not int or not 1969 <= year <= 2068:
        raise ValueError("Use a strict integer year in 1969..2068")
    if date_format not in DATE_FORMATS:
        raise ValueError("Use iso, pvsyst_dot or pvsyst_slash date_format")
    if (
        not isinstance(date_column, str)
        or not isinstance(load_column, str)
        or not date_column
        or not load_column
        or date_column == load_column
    ):
        raise ValueError("Declare distinct date and load column names")
    text = raw.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("Load CSV must not contain NUL bytes")
    header = units = rows = separator = None
    for delimiter in (";", ","):
        found = _read_header_and_units(text, delimiter, date_column, load_column)
        if found is not None:
            header, units, rows = found
            separator = delimiter
            break
    if rows is None:
        raise ValueError("Expected exactly the declared date/load columns and a following units row")
    if units is None or len(units) != 2:
        raise ValueError("Load CSV needs an explicit two-column units row")
    load_index, date_index = header.index(load_column), header.index(date_column)
    unit = units[load_index].strip()
    if unit not in UNITS:
        raise ValueError("Load power unit must be W or kW (bracketed aliases allowed); no unit is inferred")
    expected = 24 * (366 if calendar.isleap(year) else 365)
    beginning = datetime(year, 1, 1)
    values, months, issues = [], [[] for _ in range(12)], []
    seen, duplicate_hours = set(), 0
    for cells in rows:
        if not any(cell.strip() for cell in cells) or cells[0].lstrip().startswith("#"):
            continue
        index = len(values)
        if index >= expected:
            raise ValueError(
                "Load CSV has more than one full year's hourly rows "
                "(PVsyst 8.1.7 native pvLoad THourlyParams snapshots are hourly 60-min profiles)"
            )
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
    return {
        "valid": not issues,
        "year": year,
        "rows": len(values),
        "expected_rows": expected,
        "missing_hours": missing_hours,
        "duplicate_hours": duplicate_hours,
        "first": values[0][0].isoformat() if values else None,
        "last": values[-1][0].isoformat() if values else None,
        "source_unit": unit,
        "output_unit": "kW",
        "delimiter": separator,
        "annual_load_kwh": annual,
        "max_load_kw": max((power for _, power in values), default=0),
        "monthly_load_kwh": monthly,
        "issues": issues,
    }


def inspect_load_profile(path, *, year, date_format="iso", date_column="Date", load_column="P Load"):
    """Inspect a concrete year's hourly powers; return values, not fingerprints."""
    raw = _read(path)
    report = _inspect_load(raw, year=year, date_format=date_format, date_column=date_column, load_column=load_column)
    if _read(path) != raw:
        raise ValueError("Load CSV changed while it was being inspected")
    return report


def prepare_load_profile(
    cli, *, csv_name, year, output_name, utc_offset_hours, time_basis,
    date_format="iso", date_column="Date", load_column="P Load", dry_run=True,
):
    """Normalize verified hourly loads into a NEW ASCII native CSV and an import record."""
    result = {
        "status": "blocked", "stage": "preflight", "dry_run": dry_run,
        "issues": [], "warnings": [], "artifacts": {},
        "source_preserved": None, "native_consumption_verified": False,
    }
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
                if path.exists() or (
                    path.parent.exists()
                    and any(item.name.casefold() == path.name.casefold() for item in path.parent.iterdir())
                ):
                    raise FileExistsError("Choose new load CSV/import record names: " + path.name)
            original = _read(source)
            report = _inspect_load(
                original, year=year, date_format=date_format, date_column=date_column, load_column=load_column
            )
            result["inspection"] = report
            if not report["valid"]:
                result["issues"].extend(report["issues"])
                return result
            header, units, reader = _read_header_and_units(
                original.decode("utf-8-sig"), report["delimiter"], date_column, load_column
            )
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
            result.update(
                status="ready",
                load_record_version=1,
                source={
                    "name": source.name, "folder": "UserData", "date_format": date_format,
                    "date_column": date_column, "load_column": load_column,
                },
                year=year,
                time_basis=time_basis,
                utc_offset_hours=utc_offset_hours,
                artifacts={"csv": str(target), "manifest": str(record)},
                normalized_format={
                    "header": ["Date", "P Load"], "units": ["", "kW"],
                    "date_format": "pvsyst_dot", "encoding": "ASCII", "separator": ";",
                },
            )
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
            if not math.isclose(
                normalized["annual_load_kwh"], report["annual_load_kwh"], rel_tol=1e-11, abs_tol=1e-9
            ):
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
                result["warnings"].append(
                    "Retained output paths may contain partial artifacts; inspect them before retrying with new names."
                )
    return result


def load_snapshot(workspace, manifest_name):
    """Validate current CSVs and reconstruct exactly the import record's source conversion."""
    manifest = workspace_file(workspace, "Results", manifest_name, ".json")
    record = json.loads(_read(manifest).decode("utf-8"))
    if (
        not isinstance(record, dict)
        or record.get("load_record_version") != 1
        or record.get("status") != "prepared"
        or record.get("time_basis") != TIME_BASIS
        or type(record.get("utc_offset_hours")) is not int
        or not -12 <= record["utc_offset_hours"] <= 14
    ):
        raise ValueError("Use a prepared v1 hourly load record with an explicit standard time basis")
    year = record["year"]
    source_info = record["source"]
    source = workspace_file(workspace, "UserData", source_info["name"], ".csv")
    original = _read(source)
    report = _inspect_load(
        original, year=year, date_format=source_info["date_format"],
        date_column=source_info["date_column"], load_column=source_info["load_column"],
    )
    name = record["artifacts"]["csv"]
    name = PureWindowsPath(name).name if "\\" in name else Path(name).name
    profile = workspace_file(workspace, "UserHourlyParams", name, ".csv")
    normalized = _read(profile)
    result = _inspect_load(normalized, year=year, date_format="pvsyst_dot")
    if not report["valid"] or not result["valid"]:
        raise ValueError("Current raw/normalized hourly loads no longer have complete valid coverage")
    header, units, raw_reader = _read_header_and_units(
        original.decode("utf-8-sig"), report["delimiter"], source_info["date_column"], source_info["load_column"]
    )
    col_date, col_load = header.index(source_info["date_column"]), header.index(source_info["load_column"])
    expected = [
        (
            datetime.strptime(row[col_date].strip(), DATE_FORMATS[source_info["date_format"]]),
            number(row[col_load].strip()) * UNITS[units[col_load].strip()],
        )
        for row in raw_reader
        if any(item.strip() for item in row) and not row[0].lstrip().startswith("#")
    ]
    reader = csv.reader(io.StringIO(normalized.decode("ascii")), delimiter=";", strict=True)
    if next(reader) != ["Date", "P Load"] or next(reader) != ["", "kW"]:
        raise ValueError("Expected the prepared native ASCII Date;P Load / ;kW CSV")
    rows = list(reader)
    if any(len(row) != 2 for row in rows):
        raise ValueError("Every prepared load row must be one Date;P Load pair")
    values = [(datetime.strptime(row[0], DATE_FORMATS["pvsyst_dot"]), number(row[1])) for row in rows]
    if any(
        stamp != original_stamp or not math.isclose(power, original_power, rel_tol=1e-11, abs_tol=1e-9)
        for (stamp, power), (original_stamp, original_power) in zip(values, expected, strict=True)
    ):
        raise ValueError("Normalized load differs from its current declared raw input")
    return record, profile, values


def hourly_load_block(profile, values, year, newline):
    """Native Energy/MJ accumulation: signed base62 integers divided by WRFactor."""
    maximum = max((power for _, power in values), default=0)
    factor = min(1000.0, (62**3 - 2) / (3.6 * maximum)) if maximum else 1000.0
    if not math.isfinite(factor) or factor <= 0 or not math.isfinite(math.fsum(power for _, power in values) * 3.6):
        raise ValueError("Load energy must remain finite in native MJ representation")
    days, error = {}, 0.0
    for stamp, power in values:
        raw = round(power * 3.6 * factor)
        if not 0 <= raw < 62**3:
            raise ValueError("Hourly energy cannot be represented by the checked native token range")
        encoded, current = "", raw
        while current:
            current, remainder = divmod(current, 62)
            encoded = DIGITS[remainder] + encoded
        days.setdefault(stamp.strftime("%d%m"), []).append("+" + (encoded or "0"))
        error = max(error, abs(raw / factor / 3.6 - power))
    if not math.isfinite(factor) or factor <= 0 or any(len(tokens) != 24 for tokens in days.values()):
        raise ValueError("Invalid full-day load token representation")
    lines = [
        "  PVObject_UserNeeds=pvLoad",
        "    Comment=MCP hourly load: explicit embedded snapshot, no renormalization",
        "    Flags=$4000", "    LoadType=HourlyFile", "    UsesModul=Yearly", "    Energy Units=kW",
        "    HourlyLoad=THourlyParams", "      ParIdentH=ELoadIN", "      VarIdentH=ELoadIN",
        "      VarAccFile=Hour", "      ParUnits=kW", "      PathName=" + str(profile.parent),
        "      FileName=" + profile.name, "      ParSimulH=TVarSimul", "        Varident=ELoadIN",
        "        VarAcc=Hour", "        VarType=Energy", "        VarStore=Simul", "        VarModeSS=Accum",
        f"        DateBeg=01/01/{year % 100:02d} 00:00",
        f"        DateEnd=31/12/{year % 100:02d} 23:00",
        "        WRFactor=" + repr(factor),
    ]
    lines.extend("          Day_" + day + "=" + "".join(tokens) for day, tokens in days.items())
    lines.extend([
        "        End of Data", "      End of VarSimul=viELoadIN", "      NColDate=0",
        "      DateTitle=Date", "      DateFormat=0", "      DSTShiftReq=False",
        "      WinterDST=01/01/80 00:00", "      SummerDST=01/01/80 00:00",
        "      IsRenormalized=False", "      RenormMonth=Year", "      RenormVal=0",
        "      OriginalScale=" + repr(math.fsum(power for _, power in values) * 3.6),
        "      StoredUnit=MJ", "    End of THourlyParams", "  End of PVObject pvLoad",
    ])
    return newline.join(lines) + newline, {
        "mode": "embedded_snapshot",
        "day_count": len(days),
        "rows": len(values),
        "WRFactor": factor,
        "stored_unit": "MJ",
        "max_encoding_error_kw": error,
        "encoding_error_bound_kw": 0.5 / factor / 3.6,
    }


def upsert_load_block(text: str, block: str) -> tuple[str, bool]:
    """Replace an existing ``pvLoad`` section in ``text``, returning ``(updated, replaced)``."""
    starts = list(re.finditer(r"(?m)^[ \t]*PVObject_UserNeeds=pvLoad[ \t]*\r?$", text))
    ends = list(re.finditer(r"(?m)^[ \t]*End of PVObject pvLoad[ \t]*\r?$", text))
    if len(starts) > 1 or len(ends) > 1 or bool(starts) != bool(ends):
        raise ValueError("Ambiguous owning load block")
    if not starts:
        return text, False
    begin, finish = starts[0].start(), ends[0].end()
    if begin >= finish:
        raise ValueError("Unmatched owning load block")
    return text[:begin] + block + text[finish + text.startswith("\n", finish):], True


def create_load_variant(
    store, *, project, source_variant, new_variant, load_manifest_name,
    dry_run=True, variant_name=None, add_utf8_bom=False,
):
    """Preview or create a NEW VC only; keep its original system/strategy/site and sources intact."""
    def build(*, text, lines, **_):
        _, system = store._sections(lines)
        if system is None:
            raise ValueError("One owning pvSystem is required")
        if has_peak_shaving_storage(lines):
            raise ValueError(PEAK_SHAVING_LOAD_ERROR)
        record, profile, values = load_snapshot(store.workspace, load_manifest_name)
        newline = "\r\n" if "\r\n" in text else "\n"
        block, encoding = hourly_load_block(profile, values, record["year"], newline)
        candidate_text, replaced = upsert_load_block(text, block)
        if not replaced:
            closing = text.rfind("\n", 0, text.rindex("End of PVObject pvVCalcul")) + 1
            candidate_text = text[:closing] + block + text[closing:]
        candidate_lines = candidate_text.splitlines(keepends=True)
        index, raw = root_block(candidate_lines).one("Flags")
        flags = int(raw[1:], 16) | 0x00080000
        candidate_lines[index] = replace_value(candidate_lines[index], f"${flags:08X}")
        if candidate_lines != lines:
            candidate_lines, _ = strip_result_cache(candidate_lines)
        candidate_text = "".join(candidate_lines)

        def verify(checked, _, res):
            store._sections(checked)
            res.update(
                artifacts={"load_csv": str(profile)},
                year=record["year"],
                utc_offset_hours=record["utc_offset_hours"],
                time_basis=record["time_basis"],
                load_encoding=encoding,
                annual_load_kwh=math.fsum(power for _, power in values),
            )
            res["warnings"].append(
                "The load is an explicit embedded hourly snapshot. Later CSV changes require a new binding; "
                "file-reading progress logs do not prove CSV consumption."
            )
            res["warnings"].append(
                "Storage strategy, site/weather and topology stay inherited. Verify native E_User and matching "
                "simulation year/timezone before claiming consumption."
            )

        return {"candidate_text": candidate_text, "verify": verify}

    return run_new_variant_transaction(
        store, project=project, source_variant=source_variant, new_variant=new_variant,
        dry_run=dry_run, add_utf8_bom=add_utf8_bom, variant_name=variant_name,
        bom_display_target=new_variant if variant_name is None else None,
        build=build, source_changed_error="A template/load input changed before variant creation",
        extra_exceptions=(csv.Error,), uppercase_variant=False,
    )




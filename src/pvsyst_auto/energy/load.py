"""Create a new VC with a verified hourly load snapshot, not a cached template load."""
from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import datetime
from pathlib import Path, PureWindowsPath

from pvsyst_auto.components.editor import replace_value, root_block
from pvsyst_auto.energy.profiles import DATE_FORMATS, TIME_BASIS, UNITS, _inspect_load, _read
from pvsyst_auto.energy.storage import PEAK_SHAVING_LOAD_ERROR, has_peak_shaving_storage
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.project.variant_writer import run_new_variant_transaction
from pvsyst_auto.weather.csv import number

DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def load_snapshot(workspace, manifest_name):
    """Validate current CSVs and reconstruct exactly the import record's source conversion."""
    manifest = workspace_file(workspace, "Results", manifest_name, ".json")
    saved = _read(manifest)
    record = json.loads(saved.decode("utf-8"))
    if (not isinstance(record, dict) or record.get("load_record_version") != 1
            or record.get("status") != "prepared" or record.get("time_basis") != TIME_BASIS
            or type(record.get("utc_offset_hours")) is not int
            or not -12 <= record["utc_offset_hours"] <= 14):
        raise ValueError("Use a prepared v1 hourly load record with an explicit standard time basis")
    year = record["year"]
    source_info = record["source"]
    source = workspace_file(workspace, "UserData", source_info["name"], ".csv")
    original = _read(source)
    report = _inspect_load(original, year=year, date_format=source_info["date_format"],
                           date_column=source_info["date_column"], load_column=source_info["load_column"])
    name = record["artifacts"]["csv"]
    # Historical absolute paths are metadata, never paths to open outside this workspace.
    name = PureWindowsPath(name).name if "\\" in name else Path(name).name
    profile = workspace_file(workspace, "UserHourlyParams", name, ".csv")
    normalized = _read(profile)
    result = _inspect_load(normalized, year=year, date_format="pvsyst_dot")
    if not report["valid"] or not result["valid"]:
        raise ValueError("Current raw/normalized hourly loads no longer have complete valid coverage")
    raw_reader = csv.reader(io.StringIO(original.decode("utf-8-sig")), delimiter=report["delimiter"], strict=True)
    for header in raw_reader:
        header = [item.strip() for item in header]
        if any(header) and not header[0].startswith("#"):
            break
    units = next(raw_reader)
    col_date, col_load = header.index(source_info["date_column"]), header.index(source_info["load_column"])
    expected = []
    for row in raw_reader:
        if not any(item.strip() for item in row) or row[0].lstrip().startswith("#"):
            continue
        expected.append((datetime.strptime(row[col_date].strip(), DATE_FORMATS[source_info["date_format"]]),
                         number(row[col_load].strip()) * UNITS[units[col_load].strip()]))
    reader = csv.reader(io.StringIO(normalized.decode("ascii")), delimiter=";", strict=True)
    if next(reader) != ["Date", "P Load"] or next(reader) != ["", "kW"]:
        raise ValueError("Expected the prepared native ASCII Date;P Load / ;kW CSV")
    rows = list(reader)
    # _inspect_load skips blank rows, which would otherwise reach row[0] as an empty list.
    if any(len(row) != 2 for row in rows):
        raise ValueError("Every prepared load row must be one Date;P Load pair")
    values = [(datetime.strptime(row[0], DATE_FORMATS["pvsyst_dot"]), number(row[1])) for row in rows]
    if any(stamp != original_stamp or not math.isclose(power, original_power, rel_tol=1e-11, abs_tol=1e-9)
           for (stamp, power), (original_stamp, original_power) in zip(values, expected, strict=True)):
        raise ValueError("Normalized load differs from its current declared raw input")
    return record, profile, values


def hourly_load_block(profile, values, year, newline):
    """Native Energy/MJ accumulation: signed base62 integers divided by WRFactor."""
    maximum = max((power for _, power in values), default=0)
    factor = min(1000.0, (62**3 - 2) / (3.6 * maximum)) if maximum else 1000.0
    if not math.isfinite(factor) or factor <= 0 or not math.isfinite(math.fsum(power for _, power in values) * 3.6):
        raise ValueError("Load energy must remain finite in native MJ representation")
    days = {}
    error = 0.0
    for stamp, power in values:
        raw = round(power * 3.6 * factor)
        if not 0 <= raw < 62**3:
            raise ValueError("Hourly energy cannot be represented by the checked native token range")
        encoded = ""
        current = raw
        while current:
            current, remainder = divmod(current, 62)
            encoded = DIGITS[remainder] + encoded
        days.setdefault(stamp.strftime("%d%m"), []).append("+" + (encoded or "0"))
        error = max(error, abs(raw / factor / 3.6 - power))
    if not math.isfinite(factor) or factor <= 0 or any(len(tokens) != 24 for tokens in days.values()):
        raise ValueError("Invalid full-day load token representation")
    lines = ["  PVObject_UserNeeds=pvLoad", "    Comment=MCP hourly load: explicit embedded snapshot, no renormalization",
             "    Flags=$4000", "    LoadType=HourlyFile", "    UsesModul=Yearly", "    Energy Units=kW",
             "    HourlyLoad=THourlyParams", "      ParIdentH=ELoadIN", "      VarIdentH=ELoadIN",
             "      VarAccFile=Hour", "      ParUnits=kW", "      PathName=" + str(profile.parent),
             "      FileName=" + profile.name, "      ParSimulH=TVarSimul", "        Varident=ELoadIN",
             "        VarAcc=Hour", "        VarType=Energy", "        VarStore=Simul", "        VarModeSS=Accum",
             f"        DateBeg=01/01/{year % 100:02d} 00:00", f"        DateEnd=31/12/{year % 100:02d} 23:00",
             "        WRFactor=" + repr(factor)]
    lines.extend("          Day_" + day + "=" + "".join(tokens) for day, tokens in days.items())
    lines.extend(["        End of Data", "      End of VarSimul=viELoadIN", "      NColDate=0",
                  "      DateTitle=Date", "      DateFormat=0", "      DSTShiftReq=False",
                  "      WinterDST=01/01/80 00:00", "      SummerDST=01/01/80 00:00",
                  "      IsRenormalized=False", "      RenormMonth=Year", "      RenormVal=0",
                  "      OriginalScale=" + repr(math.fsum(power for _, power in values) * 3.6),
                  "      StoredUnit=MJ", "    End of THourlyParams", "  End of PVObject pvLoad"])
    return newline.join(lines) + newline, {"mode": "embedded_snapshot", "day_count": len(days),
        "rows": len(values), "WRFactor": factor, "stored_unit": "MJ", "max_encoding_error_kw": error,
        "encoding_error_bound_kw": 0.5 / factor / 3.6}


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


def create_load_variant(store, *, project, source_variant, new_variant, load_manifest_name, dry_run=True,
                        variant_name=None, add_utf8_bom=False):
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
            res["warnings"].append("The load is an explicit embedded hourly snapshot. Later CSV changes require a new binding; file-reading progress logs do not prove CSV consumption.")
            res["warnings"].append("Storage strategy, site/weather and topology stay inherited. Verify native E_User and matching simulation year/timezone before claiming consumption.")

        return {"candidate_text": candidate_text, "verify": verify}

    return run_new_variant_transaction(
        store,
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        dry_run=dry_run,
        add_utf8_bom=add_utf8_bom,
        variant_name=variant_name,
        bom_display_target=new_variant if variant_name is None else None,
        build=build,
        source_changed_error="A template/load input changed before variant creation",
        extra_exceptions=(csv.Error,),
        uppercase_variant=False,
    )

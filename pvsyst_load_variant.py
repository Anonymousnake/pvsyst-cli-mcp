"""Create a new VC with a verified hourly load snapshot, not a cached template load."""
from __future__ import annotations

import csv
import difflib
import io
import json
import math
import re
from datetime import datetime
from pathlib import Path, PureWindowsPath

from pvsyst_load_profile import DATE_FORMATS, TIME_BASIS, UNITS, _inspect_load, _read
from pvsyst_paths import workspace_file
from pvsyst_weather import number

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
    values = [(datetime.strptime(row[0], DATE_FORMATS["pvsyst_dot"]), number(row[1])) for row in reader]
    if any(stamp != original_stamp or not math.isclose(power, original_power, rel_tol=1e-11, abs_tol=1e-9)
           for (stamp, power), (original_stamp, original_power) in zip(values, expected, strict=True)):
        raise ValueError("Normalized load differs from its current declared raw input")
    snapshots = {manifest: saved, source: original, profile: normalized}
    if any(_read(path) != data for path, data in snapshots.items()):
        raise ValueError("A load input changed while preparing the binding")
    return record, profile, values, snapshots


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


def create_load_variant(store, *, project, source_variant, new_variant, load_manifest_name, dry_run=True):
    """Preview or create a NEW VC only; keep its original system/strategy/site and sources intact."""
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [],
              "warnings": [], "artifacts": {}, "variant_created": False, "native_consumption_verified": False}
    with store.lock:
        try:
            if type(dry_run) is not bool:
                raise ValueError("Use a strict boolean")
            store._check_project(project)
            source, target = store._path(project, source_variant), store._path(project, new_variant)
            if any(path.name.casefold() == target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError("Choose a new variant: " + target.name)
            original = store._read(source)
            text, lines = store._text(original)
            _, system = store._sections(lines)
            if system is None:
                raise ValueError("One owning pvSystem is required")
            record, profile, values, snapshots = load_snapshot(store.workspace, load_manifest_name)
            newline = "\r\n" if "\r\n" in text else "\n"
            block, encoding = hourly_load_block(profile, values, record["year"], newline)
            starts = list(re.finditer(r"(?m)^\s*PVObject_UserNeeds=pvLoad\s*\r?$", text))
            ends = list(re.finditer(r"(?m)^\s*End of PVObject pvLoad\s*\r?$", text))
            if len(starts) > 1 or len(ends) > 1 or bool(starts) != bool(ends):
                raise ValueError("Ambiguous owning load block")
            if starts:
                begin, finish = starts[0].start(), ends[0].end()
                if begin >= finish:
                    raise ValueError("Unmatched owning load block")
                candidate_text = text[:begin] + block + text[finish:]
            else:
                candidate_text = text[:text.rindex("End of PVObject pvVCalcul")] + block + text[text.rindex("End of PVObject pvVCalcul"):]
            bom = original.startswith(b"\xef\xbb\xbf") or not block.isascii()
            candidate = (b"\xef\xbb\xbf" if bom else b"") + candidate_text.encode("utf-8")
            _, checked = store._text(candidate)
            store._sections(checked)
            result.update(status="ready", project=project, source_variant=source_variant, variant=new_variant,
                          artifacts={"variant": str(target), "load_csv": str(profile)}, year=record["year"],
                          utc_offset_hours=record["utc_offset_hours"], time_basis=record["time_basis"],
                          load_encoding=encoding, annual_load_kwh=math.fsum(power for _, power in values),
                          bom_added=bom and not original.startswith(b"\xef\xbb\xbf"))
            result["warnings"].append("The load is an explicit embedded hourly snapshot. Later CSV changes require a new binding; file-reading progress logs do not prove CSV consumption.")
            result["warnings"].append("Storage strategy, system flags, site/weather, topology and all non-load fields stay inherited. Verify native E_User and matching simulation year/timezone before claiming consumption.")
            diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), candidate_text.splitlines(keepends=True)))
            result.update(diff=diff[:32000], diff_truncated=len(diff) > 32000)
            if store._read(source) != original or any(_read(path) != data for path, data in snapshots.items()):
                raise ValueError("A template/load input changed before variant creation")
            if dry_run:
                return result
            result["stage"] = "write"
            with target.open("xb") as stream:
                stream.write(candidate)
            result.update(status="created", stage="created", variant_created=True)
        except (ValueError, OSError, UnicodeError, KeyError, TypeError, csv.Error) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append("The new variant may contain a partial artifact; inspect the disclosed path before retrying. Original template is retained.")
    return result

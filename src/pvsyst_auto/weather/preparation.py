"""Bounded standard-hourly CSV to native SIT/MET, retaining source and provenance."""
from __future__ import annotations

import csv
import io
import json
import math
import re
import subprocess
from datetime import datetime
from itertools import zip_longest
from pathlib import Path

from pvsyst_auto.native.cli import PVsystError, build_monthly_weather_csv, iter_result_csv, result_units
from pvsyst_auto.components.editor import objects
from pvsyst_auto.project.fileio import write_json
from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.results import fingerprint
from pvsyst_auto.weather.csv import DATE_COLUMNS, inspect_weather_csv, number, read_bytes

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


def read_object_prefix(path, limit=MET_HEADER_LIMIT):
    """Parse the bounded leading text of a PVsyst object file into named owning-field segments.

    Shared, read-only helper for the prepare guard and the weather review. Only ``Key=Value``
    declarations are read. Nesting is tracked explicitly, so a key that exists both on the root
    object and inside ``PVObject_SiteMet`` (``Version``, ``Flags``, ``TimeZone``) never
    contaminates the other. A measured-value list, an unknown child object, a closing marker or a
    non-declaration line ends the read; an encoded hour array is never decoded or rewritten.

    An international site name is decoded as UTF-8, else as cp1252, so a legal non-ASCII site
    label cannot truncate the declaration block and hide the declarations after it.
    """
    with path.open("rb") as stream:
        data = stream.read(limit)
    segments = {"root": {}, "SiteMet": {}}
    section, stop = "root", None
    for index, raw_line in enumerate(data.split(b"\n"), start=1):
        line = raw_line.rstrip(b"\r")
        if index == 1 and line.startswith(b"\xef\xbb\xbf"):
            line = line[3:]
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = line.decode("cp1252")
            except UnicodeDecodeError:
                stop = {"line": index, "reason": "undecodable data"}
                break
        if any(ord(character) < 32 and character != "\t" for character in text):
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
            stop = {"line": index, "reason": "unknown nested object, not treated as an owning field: "
                                             + key[:60]}
            break
        if key.startswith("VarMeteo, list of"):
            stop = {"line": index, "reason": "start of the measured value lists, after the declarations"}
            break
        segments[section].setdefault(key, []).append({"line": index, "value": value})
    return segments, stop


def declared_fields(segments, section, wanted):
    """Owned fields of one section; a nested value is never returned as a root value."""
    return {key: entries for key, entries in segments.get(section, {}).items() if key in wanted}


def first_value(fields, key):
    entries = fields.get(key)
    return entries[0]["value"] if entries else None


def inspect_met_calendar(path, year):
    """Check annual range declarations only; do not decode or re-calendar native weather."""
    segments, _ = read_object_prefix(path)
    fields = declared_fields(segments, "root", ("DateDebF", "DateFinF"))
    report = {"requested_year": year, "coverage": "unverified", "declarations": fields,
              "issues": [], "warnings": [],
              "scope": "Native MET root dates only; not hourly completeness or time-basis certification"}
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
    elif begin == datetime(1990, 1, 1) and end == datetime(1990, 12, 31, 23):
        report["coverage"] = "generic_reference"
        report["warnings"].append("Native MET declares generic reference year 1990; compatibility with "
                                  "the requested year remains for the native run to check")
    else:
        report["coverage"] = "insufficient"
        report["issues"].append(f"Requested full year {year} is outside native MET declarations "
                                f"{begin:%Y-%m-%d %H:%M} through {end:%Y-%m-%d %H:%M}; "
                                "use a matching weather file/year, not the year in the filename")
    return report


def check_generated_met(path, *, time_zone):
    """Verify a freshly converted .MET against the requested local legal standard basis.

    Only the bounded owning-field prefix of the generated file is read; the encoded hour array is
    never parsed or rewritten. Its purpose is to stop a wrong ``RefTimeS`` from being published as
    a prepared weather basis: the native site snapshot must declare legal time and the requested
    standard offset. A field that exists but contradicts the request is a hard failure; a field
    this profile does not require is reported as unchecked rather than invented.
    """
    segments, stop = read_object_prefix(path)
    root = declared_fields(segments, "root", ("IsSourceUt", "HourShift", "TimeShift",
                                              "TimeShiftTotalMin", "PasAccum",
                                              "VarMeteoStepDuration"))
    site = declared_fields(segments, "SiteMet", ("HLeg", "TimeZone"))
    issues, unchecked = [], []
    # A duplicated time declaration is ambiguous even when the values agree, so it is refused
    # before any value is read.
    for section, fields, keys in (("root", root, ("IsSourceUt", "HourShift", "TimeShift",
                                                  "TimeShiftTotalMin", "PasAccum",
                                                  "VarMeteoStepDuration")),
                                  ("SiteMet", site, ("HLeg", "TimeZone"))):
        for key in keys:
            if len(fields.get(key, ())) > 1:
                issues.append(f"Generated MET declares {section} {key} more than once; an ambiguous "
                              "time declaration is refused")
    source_ut = first_value(root, "IsSourceUt")
    if source_ut is None:
        unchecked.append("Generated MET declares no root IsSourceUt")
    elif source_ut != "False":
        issues.append(f"Generated MET root declares IsSourceUt={source_ut}, but this local standard "
                      "import profile requires a non-UTC source declaration (False)")
    legal = first_value(site, "HLeg")
    if legal is None:
        issues.append("Generated MET declares no SiteMet HLeg, so the legal-time basis is unverified")
    elif legal != "True":
        issues.append(f"Generated MET SiteMet declares HLeg={legal}, not legal time; the requested "
                      "local standard CSV basis would not be reproduced")
    offset = first_value(site, "TimeZone")
    if offset is None:
        issues.append("Generated MET declares no SiteMet TimeZone")
    else:
        try:
            if float(offset) != float(time_zone):
                issues.append(f"Generated MET SiteMet TimeZone={offset} differs from the requested "
                              f"{time_zone}")
        except (TypeError, ValueError):
            issues.append(f"Generated MET SiteMet TimeZone={offset!r} is not a number")
    for key in ("HourShift", "TimeShift", "TimeShiftTotalMin"):
        value = first_value(root, key)
        if value is None:
            unchecked.append(f"Generated MET declares no root {key}")
        elif value != "0":
            issues.append(f"Generated MET root declares {key}={value}, not 0")
    for key in ("PasAccum", "VarMeteoStepDuration"):
        value = first_value(root, key)
        if value is None:
            unchecked.append(f"Generated MET declares no root {key}")
        elif value not in ("60", "60.0"):
            issues.append(f"Generated MET root declares {key}={value}, not a 60 minute step")
    return {"valid": not issues, "issues": issues, "unchecked_checks": unchecked,
            "header_read_stop": stop, "root": root, "site": site,
            "scope": "Declared owning fields of the generated MET only, read from a bounded text "
                     "prefix; the encoded hour array is not parsed, and a declared legal-time basis "
                     "is not a certification of the simulation time reference."}


def normalized_rows(path, report):
    """Reserialize verified date columns and units; never change the interval labels."""
    reader = csv.reader(io.StringIO(read_bytes(path).decode("utf-8-sig")),
                        delimiter=report["delimiter"], strict=True)
    for header in reader:
        header = [cell.strip() for cell in header]
        if header and header[0] and not header[0].startswith("#"):
            break
    next(reader)  # Explicit units were checked by the public inspector.
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        values = dict(zip(header, (cell.strip() for cell in row)))
        stamp = datetime(int(values.get("Year", report["expected_year"])), int(values["Month"]),
                         int(values["Day"]), int(values["Hour"]), int(values.get("Minute", "0")))
        irradiance = [number(values[name]) * (1 if report["units"][name] == "W/m2" else 1000 / 3.6)
                      for name in ("GHI", "DHI")]
        yield stamp, *irradiance, number(values["Tamb"])


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
    for key, column, stat, tolerance in (("GlobalH", "GHI", "observed_kwh_m2", 0.051),
                                        ("DiffuseH", "DHI", "observed_kwh_m2", 0.051),
                                        ("TAmb", "Tamb", "observed_mean_degC", 0.0051)):
        values = [number(cell) for cell in value(key).split(",") if cell]
        if len(values) != 13 or any(abs(values[i] - month[column][stat]) > tolerance
                                    for i, month in enumerate(report["months"])):
            raise ValueError("Native SIT monthly " + key + " differs beyond its printed precision")
    return {"coordinates_timezone_months_match": True, "country": value("Country"),
            "coordinate_precision_deg": precision["Latitude"], "altitude_precision_m": precision["Altitude"],
            "monthly_precision_kwh_m2": 0.051, "monthly_temperature_precision_degC": 0.0051,
            "scope": "Serialized site fields and observed monthly inputs, not geocoding or climate certification"}


def prepare_weather(cli, *, csv_name="", year=None, sit_name="", met_name="",
                    country_code="", time_basis="", dry_run=True, allow_window_creation=None):
    # ponytail: reuse the workspace CLI lock, not a second job/concurrency framework.
    with cli._lock:
        result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run,
                  "issues": [], "warnings": [], "artifacts": {}, "created_files": {},
                  "native_met_verified": False, "source_preserved": None}
        try:
            if type(dry_run) is not bool or type(year) is not int or not 1969 <= year <= 2068:
                raise ValueError("Use a strict boolean and integer year in 1969..2068")
            if time_basis != "local_standard_interval_start":
                raise ValueError("Declare local_standard_interval_start: fixed winter timezone, no DST, hourly interval-start labels")
            if not isinstance(country_code, str) or not re.fullmatch(r"[A-Z]{2}", country_code):
                raise ValueError("country_code must be an explicit uppercase ISO alpha-2 code")
            source = workspace_file(cli.workspace, "Meteo", csv_name, ".csv")
            report = inspect_weather_csv(source, year)
            result["inspection"] = report
            if report["valid"] is not True:
                raise ValueError("Weather CSV preflight: " + "; ".join(report["issues"] + report["unchecked_checks"]))
            extra = set(report["units"]) - DATE_COLUMNS - {"GHI", "DHI", "Tamb"}
            if "DHI" not in report["units"] or extra:
                raise ValueError("This verified import profile requires GHI, DHI, Tamb only; unmapped columns: " + ", ".join(sorted(extra)))
            for key in ("#Site", "#Country", "#Data Source"):
                text = report["metadata"][key]
                if not text or len(text.encode("utf-8")) > 200 or any(not c.isprintable() for c in text):
                    raise ValueError(key + " must be printable one-line text, at most 200 UTF-8 bytes")
            sit = workspace_file(cli.workspace, "Sites", sit_name, ".sit")
            met = workspace_file(cli.workspace, "Meteo", met_name, ".met")
            stem = met.stem
            paths = {"sit": sit, "met": met,
                     "normalized_csv": workspace_file(cli.workspace, "Meteo", stem + "_import.csv", ".csv"),
                     "monthly_csv": workspace_file(cli.workspace, "Meteo", stem + "_monthly.csv", ".csv"),
                     "mef": workspace_file(cli.workspace, "Meteo", stem + ".MEF", ".mef"),
                     "manifest": workspace_file(cli.workspace, "Results", stem + "_weather.json", ".json")}
            if any(path.exists() for path in paths.values()):
                raise FileExistsError("A destination already exists; choose new SIT/MET names")
            for option in ("site-name", "altitude", "timezone", "country-code"):
                cli._require("create-site", option, allow_window_creation=allow_window_creation)
            cli.command_help("convert-meteo", allow_window_creation=allow_window_creation)
            result.update(status="ready", source={"name": source.name, "sha256": report["sha256"]},
                          year=year, country_code=country_code, time_basis=time_basis,
                          artifacts={key: str(path) for key, path in paths.items()},
                          mapping={"GHI": "GlobHor (W/m2)", "DHI": "DiffHor (W/m2)", "Tamb": "T_Amb (degC)"})
            result["warnings"].extend(report["warnings"])
            result["warnings"].append("Prepared MET still requires a native full-year simulation and source-weather comparison; no binary MET parsing or clear-sky certification")
            if dry_run:
                return result
        except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc:
            result["issues"].append(str(exc))
            return result

        manifest = paths["manifest"]
        manifest.parent.mkdir(parents=True, exist_ok=True)
        def save(first=False):
            # The shared atomic writer serializes first and replaces later states only with a complete
            # temporary copy, so a failed write never leaves a truncated manifest behind.
            write_json(manifest, result, first=first)
        def unchanged():
            if fingerprint(source) != report["sha256"]:
                raise ValueError("Source CSV changed after preflight")
        save(first=True)
        try:
            unchanged()
            result.update(status="running", stage="normalized_inputs")
            save()
            with paths["normalized_csv"].open("x", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(["Date", "GHI", "DHI", "Tamb"])
                writer.writerows([[stamp.strftime("%Y-%m-%d %H:%M"), ghi, dhi, temp]
                                  for stamp, ghi, dhi, temp in normalized_rows(source, report)])
            unchanged()
            with paths["mef"].open("xb") as stream:
                stream.write(b"\xef\xbb\xbf" + MEF_TEMPLATE.encode("ascii"))
            build_monthly_weather_csv(paths["monthly_csv"],
                [month["GHI"]["observed_kwh_m2"] for month in report["months"]],
                [month["Tamb"]["observed_mean_degC"] for month in report["months"]],
                [month["DHI"]["observed_kwh_m2"] for month in report["months"]])
            result["stage"] = "site"
            save()
            meta = report["metadata"]
            result["site_creation"] = cli.create_site_details(meta["#Site"], number(meta["#Latitude"]), number(meta["#Longitude"]),
                            paths["monthly_csv"], sit, altitude=number(meta["#Altitude"]),
                            timezone=number(meta["#Time Zone"]), country_code=country_code,
                            source=meta["#Data Source"], log_level=3,
                            allow_window_creation=allow_window_creation)
            result["warnings"].extend(result["site_creation"]["warnings"])
            result["site_checks"] = check_site(sit, report, country_code)
            result["stage"] = "conversion"
            save()
            result["conversion"] = cli.convert_meteo_details(paths["normalized_csv"], paths["mef"], sit, met,
                                                              timeshift=0, log_level=3,
                                                              allow_window_creation=allow_window_creation)
            result["warnings"].extend(result["conversion"]["warnings"])
            unchanged()
            result["stage"] = "met_declarations"
            save()
            # A wrong fixed plane time reference must never be published as a prepared basis.
            result["met_declarations"] = check_generated_met(
                met, time_zone=number(report["metadata"]["#Time Zone"]))
            result["warnings"].extend(result["met_declarations"]["unchecked_checks"])
            if not result["met_declarations"]["valid"]:
                raise ValueError("Generated MET time basis: "
                                 + "; ".join(result["met_declarations"]["issues"]))
            unchanged()
            review = (result["conversion"]["warnings"] or result["site_creation"]["warnings"]
                      or result["met_declarations"]["unchecked_checks"])
            result.update(status="needs_review" if review else "prepared", stage="prepared")
        except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc:
            result.update(status="failed", error=cli._redact(str(exc)))
            diagnostics = getattr(exc, "diagnostics", None)
            if diagnostics is not None:
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
                # Never replace an exception already in flight, and never report success while the
                # manifest on disk still holds an earlier stage.
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
            or not isinstance(conversion.get("warnings"), list)
            or any(not isinstance(item, str) for item in conversion["warnings"])):
        raise ValueError("Weather manifest requires a conversion record with diagnostics and string warnings")
    if (data.get("status") not in ("prepared", "needs_review") or data.get("year") != year
            or data.get("time_basis") != "local_standard_interval_start" or data.get("source_preserved") is not True):
        raise ValueError("Weather manifest must describe a prepared unchanged source and the requested year/time basis")
    assets = {"weather_manifest": {"path": str(path), "sha256": fingerprint(path)}}
    for key, folder in (("sit", "Sites"), ("met", "Meteo"), ("mef", "Meteo"),
                         ("normalized_csv", "Meteo"), ("monthly_csv", "Meteo"), ("source", "Meteo")):
        asset = data["source"] if key == "source" else data["created_files"][key]
        file = workspace_file(workspace, folder, asset["name"], Path(asset["name"]).suffix)
        if fingerprint(file) != asset["sha256"]:
            raise ValueError("Weather preparation input/output changed: " + key)
        assets["weather_" + key] = {"path": str(file), "sha256": asset["sha256"]}
    if data["created_files"]["sit"]["name"] != sit_name or data["created_files"]["met"]["name"] != met_name:
        raise ValueError("Weather manifest SIT/MET names differ from simulation inputs")
    report = inspect_weather_csv(Path(assets["weather_source"]["path"]), year)
    if report["valid"] is not True:
        raise ValueError("Prepared source CSV no longer passes full-year inspection")
    check_site(Path(assets["weather_sit"]["path"]), report, data["country_code"])
    declarations = check_generated_met(
        Path(assets["weather_met"]["path"]), time_zone=number(report["metadata"]["#Time Zone"]))
    if not declarations["valid"]:
        raise ValueError("Prepared MET time basis: " + "; ".join(declarations["issues"])
                         + "; prepare new SIT/MET files from the original CSV with LegalTime")
    warnings = list(conversion["warnings"])
    warnings.extend(declarations["unchecked_checks"])
    if data["status"] == "needs_review":
        warnings.append("Weather preparation remains needs_review; original review state is retained")
    return assets, report, warnings


def compare_native_weather(csv_path, source, report):
    """Compare native hourly weather against the prepared CSV, row by row.

    Rounding/conversion review is unchanged: the original ``max_abs_delta``,
    ``signed_delta_sum`` and ``outside_review_threshold`` keys and the ``needs_review``
    decision based on them are retained. MAE, RMSE, mean signed bias, per-month deltas and
    a signed annual irradiance difference are added alongside them. Nothing here is a
    physical or clear-sky certification.
    """
    units = result_units(csv_path, strict=True)
    for key in ("GlobHor", "DiffHor"):
        if units.get(key) not in ("W/m2", "W/m\u00b2"):
            raise ValueError("Native weather comparison requires " + key + " in W/m2")
    if units.get("T_Amb") not in ("degC", "deg.C", "\u00b0C"):
        raise ValueError("Native weather comparison requires T_Amb in degrees C")
    names = ("GlobHor", "DiffHor", "T_Amb")
    stats = {key: {"max_abs_delta": 0.0, "signed_delta_sum": 0.0, "outside_review_threshold": 0,
                   "samples": 0, "max_abs": 0.0, "abs_sum": 0.0, "squared_sum": 0.0}
             for key in names}
    months = {month: {key: {"samples": 0, "signed_delta_sum": 0.0, "max_abs_delta": 0.0}
                      for key in names} for month in range(1, 13)}
    count, previous, steps = 0, None, set()
    for native, expected in zip_longest(iter_result_csv(csv_path, strict=True), normalized_rows(source, report)):
        if native is None or expected is None or native[1][0] != expected[0]:
            raise ValueError("Native weather labels/row count differ from the prepared CSV")
        header, row = native
        stamp = expected[0]
        if previous is not None:
            steps.add(round((stamp - previous).total_seconds() / 60.0, 6))
        previous = stamp
        for key, reference, threshold in zip(names, expected[1:], (0.1, 0.1, 0.01)):
            if header.count(key) != 1 or not math.isfinite(row[header.index(key)]):
                raise ValueError("Missing/ambiguous/nonfinite native weather: " + key)
            delta = row[header.index(key)] - reference
            state = stats[key]
            state["max_abs_delta"] = max(state["max_abs_delta"], abs(delta))
            state["signed_delta_sum"] += delta
            state["outside_review_threshold"] += abs(delta) > threshold
            state["samples"] += 1
            state["max_abs"] = state["max_abs_delta"]
            state["abs_sum"] += abs(delta)
            state["squared_sum"] += delta * delta
            month = months[stamp.month][key]
            month["samples"] += 1
            month["signed_delta_sum"] += delta
            month["max_abs_delta"] = max(month["max_abs_delta"], abs(delta))
        count += 1
    for state in stats.values():
        samples = state["samples"]
        state["mean_signed"] = state["signed_delta_sum"] / samples if samples else None
        state["mae"] = state["abs_sum"] / samples if samples else None
        state["rmse"] = math.sqrt(state["squared_sum"] / samples) if samples else None
        del state["abs_sum"], state["squared_sum"]
    monthly = []
    for month in range(1, 13):
        entry = {"month": month, "samples": months[month]["GlobHor"]["samples"]}
        for key in names:
            state = months[month][key]
            entry[key] = {"samples": state["samples"], "signed_delta_sum": state["signed_delta_sum"],
                          "mean_signed": (state["signed_delta_sum"] / state["samples"]
                                          if state["samples"] else None),
                          "max_abs_delta": state["max_abs_delta"]}
        monthly.append(entry)
    step_minutes = sorted(steps)
    # W/m2 deltas integrate to kWh/m2 only across a regular hourly cadence.
    regular_hourly = step_minutes == [60.0]
    return {"rows": count, "timestamps_match": True, "columns": stats,
            "needs_review": any(state["outside_review_threshold"] for state in stats.values()),
            "review_thresholds": {"irradiance_w_m2": 0.1, "temperature_degC": 0.01},
            "step_minutes": step_minutes, "regular_hourly_step": regular_hourly,
            "signed_energy_delta_kwh_m2": {key: (stats[key]["signed_delta_sum"] / 1000.0
                                                 if regular_hourly else None)
                                           for key in ("GlobHor", "DiffHor")},
            "temperature_mean_delta_degC": stats["T_Amb"]["mean_signed"],
            "monthly": monthly,
            "reported_delta_measures": ["MAE", "RMSE", "mean_signed", "max_abs"],
            "scope": "Disclosed rounding/conversion review heuristic, not native storage precision or "
                     "physical certification. Deltas are native minus prepared CSV; irradiance deltas "
                     "integrate to kWh/m2 only on a regular hourly step."}

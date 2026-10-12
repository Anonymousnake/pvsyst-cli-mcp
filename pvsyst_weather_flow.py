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

from pvsyst_cli import PVsystError, build_monthly_weather_csv, iter_result_csv, result_units
from pvsyst_component_editor import objects
from pvsyst_paths import workspace_file
from pvsyst_results import fingerprint
from pvsyst_weather import DATE_COLUMNS, inspect_weather_csv, number, read_bytes

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
  RefTimeS=SolarTime
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
    for key in ("Latitude", "Longitude", "Altitude", "TimeZone"):
        tag = "#Time Zone" if key == "TimeZone" else "#" + key
        if not math.isclose(number(value(key)), number(report["metadata"][tag]), rel_tol=0, abs_tol=1e-6):
            raise ValueError("Native SIT differs from weather CSV " + tag)
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
            "monthly_precision_kwh_m2": 0.051, "monthly_temperature_precision_degC": 0.0051,
            "scope": "Serialized site fields and observed monthly inputs, not geocoding or climate certification"}


def prepare_weather(cli, *, csv_name="", year=None, sit_name="", met_name="",
                    country_code="", time_basis="", dry_run=True):
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
                cli._require("create-site", option)
            cli.command_help("convert-meteo")
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
            with manifest.open("x" if first else "w", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2, ensure_ascii=True, allow_nan=False)
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
                            source=meta["#Data Source"], log_level=3)
            result["warnings"].extend(result["site_creation"]["warnings"])
            result["site_checks"] = check_site(sit, report, country_code)
            result["stage"] = "conversion"
            save()
            result["conversion"] = cli.convert_meteo_details(paths["normalized_csv"], paths["mef"], sit, met,
                                                              timeshift=0, log_level=3)
            result["warnings"].extend(result["conversion"]["warnings"])
            unchanged()
            result.update(status="needs_review" if result["conversion"]["warnings"] or result["site_creation"]["warnings"]
                          else "prepared", stage="prepared")
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
            save()
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
    warnings = list(conversion["warnings"])
    if data["status"] == "needs_review":
        warnings.append("Weather preparation remains needs_review; original review state is retained")
    return assets, report, warnings


def compare_native_weather(csv_path, source, report):
    units = result_units(csv_path, strict=True)
    for key in ("GlobHor", "DiffHor"):
        if units.get(key) not in ("W/m2", "W/m\u00b2"):
            raise ValueError("Native weather comparison requires " + key + " in W/m2")
    if units.get("T_Amb") not in ("degC", "deg.C", "\u00b0C"):
        raise ValueError("Native weather comparison requires T_Amb in degrees C")
    stats = {key: {"max_abs_delta": 0.0, "signed_delta_sum": 0.0, "outside_review_threshold": 0}
             for key in ("GlobHor", "DiffHor", "T_Amb")}
    count = 0
    for native, expected in zip_longest(iter_result_csv(csv_path, strict=True), normalized_rows(source, report)):
        if native is None or expected is None or native[1][0] != expected[0]:
            raise ValueError("Native weather labels/row count differ from the prepared CSV")
        header, row = native
        for key, reference, threshold in zip(stats, expected[1:], (0.1, 0.1, 0.01)):
            if header.count(key) != 1 or not math.isfinite(row[header.index(key)]):
                raise ValueError("Missing/ambiguous/nonfinite native weather: " + key)
            delta = row[header.index(key)] - reference
            stats[key]["max_abs_delta"] = max(stats[key]["max_abs_delta"], abs(delta))
            stats[key]["signed_delta_sum"] += delta
            stats[key]["outside_review_threshold"] += abs(delta) > threshold
        count += 1
    return {"rows": count, "timestamps_match": True, "columns": stats,
            "needs_review": any(state["outside_review_threshold"] for state in stats.values()),
            "review_thresholds": {"irradiance_w_m2": 0.1, "temperature_degC": 0.01},
            "scope": "Disclosed rounding/conversion review heuristic, not native storage precision or physical certification"}

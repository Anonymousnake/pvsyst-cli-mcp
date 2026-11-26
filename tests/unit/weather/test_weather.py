"""Consolidated weather validation, acquisition, provider review, and sub-hourly pipeline tests."""
from __future__ import annotations
import calendar
import csv
import functools
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode
from zoneinfo import ZoneInfoNotFoundError
from mcp.server.mcpserver.exceptions import ToolError
import pvsyst_auto.api.simulation as api_weather
import pvsyst_auto.project.paths as fileio_module
import pvsyst_auto.weather.acquisition as acquisition
import pvsyst_auto.weather.preparation as pvsyst_weather
import pvsyst_auto.weather.preparation as preparation
from pvsyst_auto.api import context as server
from pvsyst_auto.api.simulation import pvsyst_create_site
from pvsyst_auto.native.cli import PVsystCLI, PVsystError, build_monthly_weather_csv, build_sfi, summarize_results
from pvsyst_auto.results import annual_summary
from pvsyst_auto.weather.acquisition import WeatherAcquisitionError, download_weather, search_locations
from pvsyst_auto.weather.preparation import (MEF_TEMPLATE, check_generated_met, compare_native_weather, inspect_weather_csv, inspect_weather_values, load_weather_manifest,
    normalized_rows, prepare_weather, render_mef,)
from pvsyst_auto.weather.review import review_weather
@functools.lru_cache(maxsize=32)
def _weather_text_cached(year=2023, separator=";", skip=None, diffuse=True, unit="W/m2", extra=()):
    extra_cols = list(extra)
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=separator, lineterminator="\n")
    writer.writerows([["#Weather data hourly data"], ["#Site", "Test site"], ["#Country", "Test"],
                      ["#Data Source", "Synthetic fixture"], ["#Time step", "Hour"],
                      ["#Latitude", 30], ["#Longitude", 110], ["#Altitude", 100], ["#Time Zone", 8]])
    headers = ["Year", "Month", "Day", "Hour", "GHI", "Tamb"] + (["DHI"] if diffuse else []) + extra_cols
    units = ["", "", "", "", unit, "deg.C"] + ([unit] if diffuse else []) + ["m/s"] * len(extra_cols)
    writer.writerow(headers)
    writer.writerow(units)
    start = datetime(year, 1, 1)
    for hour in range(8784 if calendar.isleap(year) else 8760):
        if hour == skip: continue
        stamp = start + timedelta(hours=hour)
        power = 100 if 6 <= stamp.hour < 18 else 0
        factor = 1.0 if unit == "W/m2" else 0.0036
        writer.writerow([stamp.year, stamp.month, stamp.day, stamp.hour, power * factor, 20] + ([power / 2 * factor] if diffuse else []) + [2] * len(extra_cols))
    return stream.getvalue()
def weather_text(year=2023, separator=";", skip=None, diffuse=True, unit="W/m2", extra=None):
    """One complete hourly year; `extra` adds validated columns for profile counterexamples."""
    return _weather_text_cached(year, separator, skip, diffuse, unit, tuple(extra or ()))
def native_weather_commands(site="Test site", country="China", conversion="Conversion ended successfully...", log=None):
    """Fake create-site/convert-meteo boundary: builds the SIT from the monthly CSV it is given.

    Coordinates, altitude and timezone mirror the fixture metadata in `weather_text`, so a
    generated SIT only passes `check_site` for sources built by that helper.
    """
    def run(*args):
        def argument(flag):
            return next(value[len(flag) + 2:] for value in args if value.startswith("-" + flag + ":"))
        if log is not None: log.append(list(args))
        if args[0] == "create-site":
            with Path(argument("wdf")).open(newline="") as stream: rows = list(csv.reader(stream, delimiter=";"))
            series = list(zip(*[[float(value) for value in row] for row in rows[2:]]))
            monthly = [field + "=" + ",".join(map(str, (*values, sum(values)))) + "," for field, values in zip(("GlobalH", "TAmb", "DiffuseH"), series)]
            target = Path(argument("osf"))
            target.write_text("PVObject_=pvSite\nNomF=" + target.name + "\nSite=" + site + "\nCountry=" + country + "\n"
                "Latitude=30\nLongitude=110\nAltitude=100\nTimeZone=8\nHLeg=True\n" + "\n".join(monthly) + "\nEnd of PVObject pvSite\n", encoding="utf-8-sig")
            return subprocess.CompletedProcess([], 0, "Site created", "")
        Path(argument("omf")).write_bytes(FAKE_MET_HEAD)
        return subprocess.CompletedProcess([], 0, conversion, "")
    return run
# A generated .MET starts with its ASCII owning fields; the prepare guard reads only these,
# so the fake native runner must return this shape rather than an opaque placeholder.
FAKE_MET_HEAD = ("PVObject_=pvMeteo\r\n  Version=8.1.6\r\n  Flags=$0143\r\n  IsSourceUt=False\r\n"
    "  HourShift=0\r\n  TimeShift=0\r\n  TimeShiftTotalMin=0\r\n  PasAccum=60\r\n" "  VarMeteoStepDuration=60\r\n  PVObject_SiteMet=pvSite\r\n    TimeZone=8.0\r\n"
    "    HLeg=True\r\n  End of PVObject pvSite\r\n  End of PVObject pvMeteo\r\n").encode("utf-8")
def _configure_cli(root):
    cli = PVsystCLI(sys.executable, root)
    cli._help_cache["create-site"] = "\n".join("--" + name + ":" for name in ("site-name", "altitude", "timezone", "country-code"))
    cli._help_cache["convert-meteo"] = "--input-csv-file"
    return cli
class WeatherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain_2023.csv"
            path.write_text(weather_text(2023), encoding="utf-8-sig", newline="")
            cls._plain_2023 = inspect_weather_csv(path)
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolve like the workspace code does: %TEMP% can be an 8.3 short path.
        self.root = Path(temporary.name).resolve()
        (self.root / "Meteo").mkdir()
        self.path = self.root / "Meteo" / "weather.csv"
    def inspect(self, value, expected_year=None):
        self.path.write_text(value, encoding="utf-8-sig", newline="")
        before = self.path.read_bytes()
        report = inspect_weather_csv(self.path, expected_year)
        self.assertEqual(before, self.path.read_bytes())
        json.dumps(report, allow_nan=False)
        return report
    @staticmethod
    def _head_slice(text):
        return "\n".join(text.splitlines()[:12]) + "\n"
    def test_normal_leap_and_mj_energy_equivalence(self):
        report_2023 = self._plain_2023
        self.assertTrue(report_2023["valid"])
        self.assertTrue(report_2023["raw_hourly_coverage_complete"])
        self.assertEqual(report_2023["rows"], 8760)
        self.assertEqual(len(report_2023["sha256"]), 64)
        self.assertAlmostEqual(report_2023["months"][0]["GHI"]["observed_kwh_m2"], 31 * 12 * 0.1)
        self.assertEqual(report_2023["months"][1]["Tamb"]["finite_samples"], 28 * 24)
        self.assertEqual(report_2023["months"][0]["Tamb"]["observed_mean_degC"], 20)
        report_2024 = self.inspect(weather_text(2024, ","))
        self.assertTrue(report_2024["valid"])
        self.assertTrue(report_2024["raw_hourly_coverage_complete"])
        self.assertEqual(report_2024["rows"], 8784)
        self.assertEqual(len(report_2024["sha256"]), 64)
        self.assertAlmostEqual(report_2024["months"][0]["GHI"]["observed_kwh_m2"], 31 * 12 * 0.1)
        self.assertEqual(report_2024["months"][1]["Tamb"]["finite_samples"], 29 * 24)
        self.assertEqual(report_2024["months"][0]["Tamb"]["observed_mean_degC"], 20)
        mj = self.inspect(weather_text(unit="MJ/m2"))
        self.assertTrue(mj["valid"])
        self.assertEqual(report_2023["months"], mj["months"])
    def test_coverage_gaps_duplicates_order_and_wrong_year(self):
        for year, skip in ((2023, 0), (2024, 24 * 59)):
            with self.subTest(year=year, skip=skip):
                r = self.inspect(weather_text(year=year, skip=skip))
                self.assertFalse(r["valid"])
                self.assertEqual(r["missing_hours"], 1)
                self.assertEqual(len(r["missing_examples"]), 1)
        source = weather_text()
        lines = source.splitlines()
        dup = self.inspect(source + lines[-1] + "\n")
        self.assertEqual(dup["duplicate_timestamps"], 1)
        self.assertFalse(dup["valid"])
        rev = self.inspect("\n".join(lines[:-2] + lines[-2:][::-1]) + "\n")
        self.assertEqual(rev["nonincreasing_timestamps"], 1)
        self.assertFalse(rev["raw_hourly_coverage_complete"])
        wrong_year = self.inspect(source, 2024)
        self.assertEqual((wrong_year["missing_hours"], wrong_year["unexpected_timestamps"]), (8784, 8760))
    def test_missing_nonnumeric_and_physical_errors_are_not_zeros(self):
        bad_tokens = ("", "NaN", "Infinity", "1e999", "-1", "true")
        lines = weather_text().splitlines()
        row6 = next(i for i, line in enumerate(lines) if line.startswith("2023;1;1;6;"))
        for idx, value in enumerate(bad_tokens): lines[row6 + idx] = lines[row6 + idx].replace(f"2023;1;1;{6 + idx};100.0", f"2023;1;1;{6 + idx};" + value)
        lines[row6 + 6] = lines[row6 + 6].replace("100.0;20;50.0", "100.0;20;101.0")
        r = self.inspect("\n".join(lines) + "\n")
        self.assertFalse(r["valid"])
        self.assertEqual(r["months"][0]["GHI"]["finite_samples"], 744 - len(bad_tokens))
        for idx, value in enumerate(bad_tokens):
            with self.subTest(value=value):
                line_num = row6 + idx + 1
                self.assertTrue(any(f"Line {line_num}: GHI" in item for item in r["issues"]), r["issues"])
        self.assertIn("DHI exceeds GHI", " ".join(r["issues"]))
    def test_metadata_missing_duplicates_ranges_and_identifier(self):
        head = self._head_slice(weather_text())
        cases = (
            ("missing_site", head.replace("#Site;Test site\n", ""), "Missing metadata #Site"),
            ("bad_latitude", head.replace("#Latitude;30", "#Latitude;91"), "#Latitude: expected -90..90"),
            ("dup_timezone", head.replace("#Time Zone;8", "#Time Zone;8\n#Time Zone;0"), "Duplicate metadata tag #Time Zone"),
            ("bad_year", head.replace("#Site;", "#Year;0\n#Site;"), "#Year: year outside 1..9998"),
            ("bad_identifier", head.replace("#Weather data hourly data", "#unknown"), "First line must identify"),
            ("leading_blank", "\n" + head, "First line must identify"),)
        for label, text, expected in cases:
            with self.subTest(case=label):
                r = self.inspect(text)
                self.assertFalse(r["valid"])
                self.assertIn(expected, " ".join(r["issues"]))
    def test_metadata_year_and_reordered_columns_with_optional_weather(self):
        original = list(csv.reader(io.StringIO(weather_text()), delimiter=";"))
        output = io.StringIO()
        writer = csv.writer(output, delimiter=",", lineterminator="\n")
        header_index = next(i for i, row in enumerate(original) if row[0] == "Year")
        writer.writerows(original[:header_index])
        writer.writerow(["#Year", "2023"])
        writer.writerow(["Hour", "Day", "Month", "Tamb", "GHI", "DHI", "WindVel", "RH"])
        writer.writerow(["", "", "", "deg.C", "W/m2", "W/m2", "m/s", "%"])
        writer.writerows([[row[3], row[2], row[1], row[5], row[4], row[6], 2, 50] for row in original[header_index + 2:]])
        r = self.inspect(output.getvalue())
        self.assertTrue(r["valid"])
        self.assertEqual(r["expected_year"], 2023)
        bad_rh = self.inspect(output.getvalue().replace(",2,50\n", ",2,101\n"))
        self.assertFalse(bad_rh["valid"])
        self.assertEqual((bad_rh["issue_count"], len(bad_rh["issues"]), bad_rh["issues_truncated"]), (8760, 50, True))
    def test_unknown_units_do_not_produce_mislabeled_temperature_or_energy(self):
        source = weather_text()
        r = self.inspect(source.replace("W/m2;deg.C;W/m2", "kWh/m2;degF;W/m2"))
        self.assertFalse(r["valid"])
        self.assertIsNone(r["months"][0]["GHI"]["observed_kwh_m2"])
        self.assertIsNone(r["months"][0]["Tamb"]["observed_mean_degC"])
        bad_step = self.inspect(self._head_slice(source).replace("#Time step;Hour", "#Time step;Day"))
        self.assertFalse(bad_step["valid"])
        self.assertIn("#Time step must be Hour", " ".join(bad_step["issues"]))
        self.assertIsNone(bad_step["months"][0]["GHI"]["observed_kwh_m2"])
    def test_explicit_time_shift_reference_poa_and_missing_diffuse(self):
        source = weather_text()
        for tag in ("#Hour Shift;1", "#Time Shift;30", "#Time reference;UT"):
            with self.subTest(tag=tag):
                r = self.inspect(source.replace("#Site;", tag + "\n#Site;"))
                self.assertIsNone(r["valid"])
                self.assertTrue(r["raw_hourly_coverage_complete"])
                self.assertTrue(r["unchecked_checks"])
        poa = self.inspect(source.replace("GHI", "GPI"))
        self.assertIsNone(poa["valid"])
        self.assertIn("POA", " ".join(poa["unchecked_checks"]))
        nodiff = self.inspect(weather_text(diffuse=False))
        self.assertTrue(nodiff["valid"])
        self.assertIn("diffuse", " ".join(nodiff["warnings"]))
    def test_off_hour_labels_and_invalid_dates_are_not_normalized(self):
        source = weather_text()
        shifted = source.replace("Year;Month;Day;Hour;", "Year;Month;Day;Hour;Minute;")
        shifted = shifted.replace(";;;;W/m2", ";;;;;W/m2")
        shifted = "\n".join(line.replace(";100.0;20", ";30;100.0;20").replace(";0.0;20", ";30;0.0;20") if line.startswith("2023;") else line for line in shifted.splitlines())
        r = self.inspect(shifted)
        self.assertEqual(r["nonzero_minute_records"], 8760)
        self.assertFalse(r["raw_hourly_coverage_complete"])
        invalid = self.inspect(source.replace("2023;1;1;0", "2023;2;29;0", 1))
        self.assertEqual(invalid["invalid_dates"], 1)
    def test_change_during_inspection_large_input_mcp_and_expected_year(self):
        head = self._head_slice(weather_text())
        self.path.write_text(head, encoding="utf-8")
        raw = self.path.read_bytes()
        with patch.object(pvsyst_weather, "read_bytes", side_effect=[raw, raw + b"\n"]):
            with self.assertRaisesRegex(ValueError, "changed during"): inspect_weather_csv(self.path)
        with patch.object(pvsyst_weather, "MAX_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "reader limit"): inspect_weather_csv(self.path)
        self.path.write_text(weather_text(), encoding="utf-8")
        with patch.object(server, "_cli", PVsystCLI(sys.executable, self.root)):
            self.assertTrue(api_weather.pvsyst_inspect_weather_csv("weather.csv", 2023)["valid"])
            for name in ("../weather.csv", "weather.MET", "C:\\outside.csv"):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ToolError, "filename"): api_weather.pvsyst_inspect_weather_csv(name)
        for year in (True, 0, 9999, "2023", 2023.0):
            with self.subTest(expected_year=year):
                with self.assertRaises(ValueError): inspect_weather_csv(self.path, year)
    def test_trailing_delimiters_and_nonempty_trailing_cells(self):
        plain = self._plain_2023
        for sep in (";", ","):
            with self.subTest(separator=sep):
                trailed = "".join(line + sep + "\n" for line in weather_text(2023, sep).splitlines())
                report = self.inspect(trailed)
                self.assertIs(report["valid"], True, report["issues"])
                self.assertEqual((report["units"], report["ignored_columns"], report["months"], report["metadata"]), (plain["units"], [], plain["months"], plain["metadata"]))
                if sep == ",":
                    rows = list(normalized_rows(self.path, report))
                    self.assertEqual((len(rows), rows[6]), (8760, (datetime(2023, 1, 1, 6), 100.0, 50.0, 20.0)))
        data_only = "".join(line + (";" if line[:1].isdigit() else "") + "\n" for line in weather_text(2023).splitlines())
        data_report = self.inspect(data_only)
        self.assertIs(data_report["valid"], True, data_report["issues"])
        self.assertEqual(data_report["rows"], 8760)
        head = self._head_slice(weather_text(2023))
        with self.assertRaisesRegex(ValueError, "distinct nonempty column names"): self.inspect(head.replace("Year;Month;", "Year;;Month;"))
        with self.assertRaisesRegex(ValueError, "Unit row must have one cell"):
            self.inspect(head.replace(";deg.C;W/m2\n", ";deg.C;W/m2;m/s\n").replace("GHI;Tamb;DHI\n", "GHI;Tamb;DHI;\n"))
        extra = self.inspect(weather_text(2023).replace("2023;1;1;6;100.0;20;50.0\n", "2023;1;1;6;100.0;20;50.0;7\n"))
        self.assertIs(extra["valid"], False)
        self.assertIn("data width differs", " ".join(extra["issues"]))
    def test_prepare_weather_provenance_review_and_failure(self):
        self.path.write_text(weather_text(year=2020).replace("#Country;Test", "#Country;China"), encoding="utf-8-sig")
        raw = self.path.read_bytes()
        for name, text in (("gap.csv", weather_text(year=2020, skip=0)), ("extra.csv", weather_text(year=2020, extra=("WindVel",))),
                           ("nodhi.csv", weather_text(year=2020, diffuse=False))):
            (self.root / "Meteo" / name).write_text(text, encoding="utf-8-sig")
        untouched = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        cli = _configure_cli(self.root)
        request = dict(csv_name="weather.csv", year=2020, sit_name="New.SIT", met_name="New.MET", country_code="CN", time_basis="local_standard_interval_start")
        def native(*args):
            def arg(flag):
                return next(value[len(flag) + 2:] for value in args if value.startswith("-" + flag + ":"))
            if args[0] == "create-site":
                self.assertEqual((arg("tz"), arg("alt"), arg("cc")), ("8.0", "100.0", "CN"))
                with Path(arg("wdf")).open(newline="") as stream: rows = list(csv.reader(stream, delimiter=";"))
                series = list(zip(*[[float(value) for value in row] for row in rows[2:]]))
                monthly = [field + "=" + ",".join(map(str, (*values, sum(values)))) + "," for field, values in zip(("GlobalH", "TAmb", "DiffuseH"), series)]
                Path(arg("osf")).write_text("PVObject_=pvSite\nSite=Test site\nCountry=China\n" "Latitude=30\nLongitude=110\nAltitude=100\nTimeZone=8\nHLeg=True\n"
                    + "\n".join(monthly) + "\nEnd of PVObject pvSite\n", encoding="utf-8-sig")
                return subprocess.CompletedProcess([], 0, "Site created", "")
            self.assertEqual((args[0], arg("imt")), ("convert-meteo", "0"))
            Path(arg("omf")).write_bytes(FAKE_MET_HEAD)
            return subprocess.CompletedProcess([], 0, "The imported weather data are shifted by 25 minutes\nConversion ended successfully...", "")
        with patch.object(server, "_cli", cli):
            preview = api_weather.pvsyst_prepare_weather(**request)
            self.assertEqual(preview["status"], "ready", preview)
            self.assertEqual(untouched, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
            for changes in ({"time_basis": "UTC"}, {"year": 2021}, {"country_code": ""}, {"csv_name": "../weather.csv"}, {"csv_name": "gap.csv"},
                            {"csv_name": "extra.csv"}, {"csv_name": "nodhi.csv"}):
                with self.subTest(changes=changes):
                    blocked = api_weather.pvsyst_prepare_weather(**{**request, **changes})
                    self.assertEqual(blocked["status"], "blocked", blocked)
                    self.assertTrue(blocked["issues"], blocked)
                    self.assertEqual(untouched, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
            with patch.object(cli, "_run", side_effect=native): prepared = api_weather.pvsyst_prepare_weather(**request, dry_run=False)
            self.assertEqual(prepared["status"], "needs_review", prepared)
            self.assertFalse(prepared["native_met_verified"])
            self.assertTrue(prepared["source_preserved"])
            self.assertEqual(self.path.read_bytes(), raw)
            self.assertEqual(json.loads(Path(prepared["artifacts"]["manifest"]).read_text()), prepared)
            assets, report, warnings = load_weather_manifest(self.root, "New_weather.json", 2020, "New.SIT", "New.MET")
            self.assertEqual(report["rows"], 8784)
            self.assertIn("25 minutes", warnings[0])
            with Path(assets["weather_normalized_csv"]["path"]).open(newline="") as stream: normalized = list(csv.reader(stream))
            self.assertEqual((len(normalized), normalized[1][0], normalized[-1][0]), (8785, "2020-01-01 00:00", "2020-12-31 23:00"))
            output = self.root / "Results/weather-output.csv"
            with output.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, delimiter=";")
                writer.writerow(["date", "GlobHor", "DiffHor", "T_Amb"])
                writer.writerow(["", "W/m2", "W/m2", "degC"])
                for date, ghi, dhi, temp in normalized[1:]:
                    writer.writerow([datetime.strptime(date, "%Y-%m-%d %H:%M").strftime("%d/%m/%y %H:%M"), ghi, dhi, temp])
            comparison = compare_native_weather(output, self.path, report)
            self.assertFalse(comparison["needs_review"])
            self.assertEqual(comparison["rows"], 8784)
            before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
            self.assertEqual(api_weather.pvsyst_prepare_weather(**request, dry_run=False)["status"], "blocked")
            self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
            self.path.write_bytes(raw + b"\n")
            with self.assertRaisesRegex(ValueError, "changed"): load_weather_manifest(self.root, "New_weather.json", 2020, "New.SIT", "New.MET")
            self.path.write_bytes(raw)
            with patch.object(cli, "_run", side_effect=PVsystError("Native site failure")):
                failed = api_weather.pvsyst_prepare_weather(**{**request, "sit_name": "Fail.SIT", "met_name": "Fail.MET"}, dry_run=False)
            self.assertEqual((failed["status"], failed["stage"]), ("failed", "site"))
            self.assertTrue(failed["source_preserved"])
            self.assertIn("normalized_csv", failed["created_files"])
            self.assertEqual(json.loads(Path(failed["artifacts"]["manifest"]).read_text()), failed)
    def test_leap_year_mj_units_import_equivalence_and_review_inheritance(self):
        cli = _configure_cli(self.root)
        for name, unit in (("watts.csv", "W/m2"), ("joules.csv", "MJ/m2")):
            (self.root / "Meteo" / name).write_text(weather_text(year=2020, unit=unit).replace("#Country;Test", "#Country;China"), encoding="utf-8-sig")
        request = dict(year=2020, country_code="CN", time_basis="local_standard_interval_start", dry_run=False)
        native = native_weather_commands(conversion="The imported weather data are shifted by 25 minutes\nConversion ended successfully...")
        with patch.object(server, "_cli", cli), patch.object(cli, "_run", side_effect=native):
            watts = api_weather.pvsyst_prepare_weather(csv_name="watts.csv", sit_name="W.SIT", met_name="W.MET", **request)
            joules = api_weather.pvsyst_prepare_weather(csv_name="joules.csv", sit_name="J.SIT", met_name="J.MET", **request)
        for prepared in (watts, joules):
            self.assertEqual(prepared["status"], "needs_review", prepared)
            self.assertTrue(prepared["source_preserved"])
        with Path(watts["artifacts"]["normalized_csv"]).open(newline="") as s1, \
                Path(joules["artifacts"]["normalized_csv"]).open(newline="") as s2:
            watt_rows, joule_rows = list(csv.reader(s1)), list(csv.reader(s2))
        self.assertEqual((len(watt_rows), watt_rows[0], watt_rows[1][0], watt_rows[-1][0]), (8785, ["Date", "GHI", "DHI", "Tamb"], "2020-01-01 00:00", "2020-12-31 23:00"))
        self.assertEqual(len(joule_rows), len(watt_rows))
        for index, (left, right) in enumerate(zip(watt_rows[1:], joule_rows[1:])):
            self.assertEqual((left[0], len(left)), (right[0], len(right)))
            for watt, joule in zip(left[1:], right[1:]): self.assertAlmostEqual(float(watt), float(joule), places=9, msg=f"row {index}")
        manifest = Path(watts["artifacts"]["manifest"])
        data = json.loads(manifest.read_text())
        self.assertEqual((manifest.name, data["status"], len(data["conversion"]["warnings"])), ("W_weather.json", "needs_review", 1))
        self.assertIn("shifted by 25 minutes", data["conversion"]["diagnostics"])
        data["conversion"]["warnings"] = []
        manifest.write_text(json.dumps(data), encoding="utf-8")
        assets, report, warnings = load_weather_manifest(self.root, "W_weather.json", 2020, "W.SIT", "W.MET")
        self.assertEqual(warnings, ["Weather preparation remains needs_review; original review state is retained"])
        self.assertEqual(report["rows"], 8784)
        self.assertTrue(assets["weather_source"]["path"].endswith("watts.csv"))
    def test_native_weather_details_and_diagnostics(self):
        """Public native interface the workflow relies on: details dicts and redacted diagnostics."""
        cli = _configure_cli(self.root)
        source = self.root / "Meteo/source.csv"
        source.write_text(self._head_slice(weather_text(year=2020)), encoding="utf-8-sig")
        (self.root / "Sites").mkdir(exist_ok=True)
        monthly = build_monthly_weather_csv(self.root / "Meteo/monthly.csv", [120.0] * 12, [20.0] * 12, [60.0] * 12)
        mef = self.root / "Meteo/source.MEF"
        mef.write_bytes(b"\xef\xbb\xbfPVObject_=pvMeteoFormat\nEnd of PVObject pvMeteoFormat\n")
        sit = self.root / "Sites/source.SIT"
        sit.write_bytes(b"PVObject_=pvSite\nEnd of PVObject pvSite\n")
        conversion = "The imported weather data are shifted by 25 minutes\nConversion ended successfully..."
        log = []
        with patch.object(cli, "_run", side_effect=native_weather_commands(site="Test site", country="Test", conversion=conversion, log=log)):
            created = cli.create_site_details("Test site", 30, 110, monthly, self.root / "Sites/new.SIT", altitude=100, timezone=8, country_code="TT",
                                              source="Synthetic fixture", log_level=3)
            converted = cli.convert_meteo_details(source, mef, sit, self.root / "Meteo/new.MET", timeshift=0, log_level=3)
            wrapped = cli.create_site("Test site", 30, 110, monthly, self.root / "Sites/plain.SIT", country_code="TT")
        self.assertEqual([entry[0] for entry in log], ["create-site", "convert-meteo", "create-site"])
        self.assertIn("-cc:TT", log[0])
        self.assertIn("-imt:0", log[1])
        self.assertEqual((created["sit"], created["warnings"]), (str(self.root / "Sites/new.SIT"), []))
        self.assertIn("Site created", created["diagnostics"])
        self.assertEqual((converted["met"], converted["warnings"]), (str(self.root / "Meteo/new.MET"), ["The imported weather data are shifted by 25 minutes"]))
        self.assertIn("Conversion ended successfully", converted["diagnostics"])
        self.assertEqual(wrapped, Path(self.root / "Sites/plain.SIT"))
        rejection = subprocess.CompletedProcess([], 3, "Error: synthetic native rejection\nHost ID: fixture-secret", "")
        with patch.object(cli, "_run", return_value=rejection):
            for call in (lambda: cli.convert_meteo_details(source, mef, sit, self.root / "Meteo/rejected.MET", timeshift=0, log_level=3),
                         lambda: cli.create_site_details("Test site", 30, 110, monthly, self.root / "Sites/rejected.SIT", country_code="TT")):
                with self.assertRaises(PVsystError) as caught: call()
                self.assertIsInstance(caught.exception.diagnostics, str)
                self.assertIn("synthetic native rejection", caught.exception.diagnostics)
                self.assertIn("[redacted]", caught.exception.diagnostics)
                self.assertNotIn("fixture-secret", str(caught.exception) + caught.exception.diagnostics)
        self.assertFalse((self.root / "Meteo/rejected.MET").exists())
        self.assertFalse((self.root / "Sites/rejected.SIT").exists())
    def test_prepare_manifest_writes_and_artifact_hash_error(self):
        csv_2020 = weather_text(year=2020).replace("#Country;Test", "#Country;China")
        req = dict(csv_name="weather.csv", year=2020, country_code="CN", time_basis="local_standard_interval_start", dry_run=False)
        def new_workspace():
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            root = Path(tmp.name).resolve()
            (root / "Meteo").mkdir()
            (root / "Meteo" / "weather.csv").write_text(csv_2020, encoding="utf-8-sig")
            return root, _configure_cli(root)
        def assert_only_manifest(root, stem):
            self.assertEqual([path.name for path in (root / "Results").iterdir()], [stem + "_weather.json"])
        with self.subTest(case="nan_save_keeps_running_site_manifest"):
            root, cli = new_workspace()
            real_site = cli.create_site_details
            with patch.object(cli, "_run", side_effect=native_weather_commands()), \
                    patch.object(cli, "create_site_details", side_effect=lambda *a, **k: dict(real_site(*a, **k), fixture_ratio=float("nan"))):
                res = prepare_weather(cli, sit_name="Nan.SIT", met_name="Nan.MET", **req)
            self.assertEqual(res["status"], "failed", res)
            self.assertIn("not JSON compliant", res["error"])
            self.assertTrue(any(i.startswith("Weather manifest write failed") for i in res["issues"]), res["issues"])
            disk = json.loads((root / "Results/Nan_weather.json").read_text(encoding="utf-8"))
            self.assertEqual((disk["status"], disk["stage"]), ("running", "site"))
            assert_only_manifest(root, "Nan")
        with self.subTest(case="locked_replace_keeps_preflight_manifest"):
            root, cli = new_workspace()
            class LockedManifest:
                def __getattr__(self, name):
                    return getattr(os, name)
                @staticmethod
                def replace(*_args):
                    raise PermissionError("fixture: manifest is locked")
            with patch.object(cli, "_run", side_effect=native_weather_commands()), \
                    patch.object(fileio_module, "os", LockedManifest()):
                res = prepare_weather(cli, sit_name="Locked.SIT", met_name="Locked.MET", **req)
            self.assertEqual((res["status"], res["stage"]), ("failed", "normalized_inputs"), res)
            self.assertIn("Weather manifest write failed: fixture: manifest is locked", res["issues"])
            disk = json.loads((root / "Results/Locked_weather.json").read_text(encoding="utf-8"))
            self.assertEqual(disk["stage"], "preflight")
            assert_only_manifest(root, "Locked")
        with self.subTest(case="final_save_error_never_masks_exception"):
            root, cli = new_workspace()
            manifest = root / "Results/Masked_weather.json"
            def unexpected(_path, *, time_zone):
                manifest.unlink()
                manifest.mkdir()
                raise RuntimeError("fixture: unexpected native failure")
            with patch.object(cli, "_run", side_effect=native_weather_commands()), \
                    patch.object(preparation, "check_generated_met", side_effect=unexpected):
                with self.assertRaisesRegex(RuntimeError, "fixture: unexpected native failure"):
                    prepare_weather(cli, sit_name="Masked.SIT", met_name="Masked.MET", **req)
            assert_only_manifest(root, "Masked")
        with self.subTest(case="artifact_hash_error_retains_failed_manifest"):
            root, cli = new_workspace()
            raw = (root / "Meteo/weather.csv").read_bytes()
            real_fp = preparation.fingerprint
            def unreadable(path):
                if Path(path).suffix.lower() == ".mef": raise OSError("fixture: MEF unreadable at hash collection")
                return real_fp(path)
            with patch.object(server, "_cli", cli), \
                    patch.object(cli, "_run", side_effect=native_weather_commands()), \
                    patch.object(preparation, "fingerprint", side_effect=unreadable):
                failed = api_weather.pvsyst_prepare_weather(csv_name="weather.csv", year=2020, sit_name="K.SIT", met_name="K.MET",
                    country_code="CN", time_basis="local_standard_interval_start", dry_run=False)
            self.assertEqual(failed["status"], "failed", failed)
            self.assertEqual(failed["created_files"]["mef"], {"name": "K.MEF", "hash_error": "fixture: MEF unreadable at hash collection"})
            self.assertIn("Artifact hash collection failed: mef", failed["issues"])
            self.assertTrue(failed["source_preserved"])
            self.assertTrue((root / "Meteo/K.MET").is_file())
            self.assertEqual((root / "Meteo/weather.csv").read_bytes(), raw)
            self.assertEqual(json.loads(Path(failed["artifacts"]["manifest"]).read_text()), failed)
HASH_KEYS = ("sha256", "hash", "fingerprint", "md5", "checksum")
@functools.lru_cache(maxsize=16)
def _base_archive(start_iso, end_iso, unit, grid):
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    first = datetime(start.year, start.month, start.day)
    last = datetime(end.year, end.month, end.day, 23, 0)
    labels = tuple(first + timedelta(hours=index) for index in range(int((last - first).total_seconds() // 3600) + 1))
    ghi = tuple(100.0 if 8 <= stamp.hour < 16 else 0.0 for stamp in labels)
    dhi = tuple(value / 2 for value in ghi)
    temperature = tuple(float(stamp.hour) for stamp in labels)
    times = tuple(stamp.isoformat() for stamp in labels)
    return labels, times, ghi, dhi, temperature, grid, unit
def archive_payload(params, *, missing=(), dhi_above_ghi=False, unit="W/m\u00b2", grid=(40.0, 116.5, 47.0)):
    """A provider-shaped payload whose values make the time mapping checkable."""
    labels, times, base_ghi, base_dhi, base_temp, grid, unit = _base_archive(params["start_date"], params["end_date"], unit, tuple(grid))
    ghi, dhi, temperature = list(base_ghi), list(base_dhi), list(base_temp)
    if dhi_above_ghi:
        middle = len(labels) // 2
        dhi[middle] = ghi[middle] + 10.0
    for stamp in missing:
        index = labels.index(stamp)
        ghi[index] = dhi[index] = temperature[index] = None
    return {"latitude": grid[0], "longitude": grid[1], "elevation": grid[2], "timezone": "GMT", "utc_offset_seconds": 0, "timezone_abbreviation": "GMT",
            "generationtime_ms": 1.0, "hourly_units": {"time": "iso8601", "shortwave_radiation": unit, "diffuse_radiation": unit, "temperature_2m": "\u00b0C"},
            "hourly": {"time": list(times), "shortwave_radiation": ghi, "diffuse_radiation": dhi, "temperature_2m": temperature}}
def geocoding_search_payload():
    return {"results": [{"id": 1816670, "name": "Beijing", "latitude": 39.9075, "longitude": 116.39723,
         "elevation": 49.0, "feature_code": "PPLC", "country_code": "CN", "country": "China", "admin1": "Beijing", "timezone": "Asia/Shanghai", "population": 11716620},
        {"id": 8404324, "name": "Beijing", "latitude": 30.72608, "longitude": 108.67483, "elevation": 583.0, "feature_code": "PPL", "country_code": "CN", "country": "China",
         "admin1": "Chongqing", "timezone": "Asia/Shanghai"}]}
def fake_provider(*, search=None, single=None, archive_kwargs=None, error=None):
    """Return a stand-in for the module's single HTTP entry point."""
    archive_kwargs = dict(archive_kwargs or {})
    def call(url, params, **_kwargs):
        if error is not None: raise error
        if url == acquisition.GEOCODING_SEARCH_URL: payload = geocoding_search_payload() if search is None else search
        elif url == acquisition.GEOCODING_GET_URL: payload = geocoding_search_payload()["results"][0] if single is None else single
        else: payload = archive_payload(params, **archive_kwargs)
        return payload, json.dumps(payload), url + "?" + urlencode(params)
    return call
def read_standard_csv(path):
    text = path.read_text(encoding="utf-8")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    metadata = {row[0]: row[1] for row in rows if row and row[0].startswith("#") and len(row) > 1}
    header_index = next(index for index, row in enumerate(rows) if row and row[0] == "Year")
    return metadata, rows[header_index], rows[header_index + 1], rows[header_index + 2:]
def collected_keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            collected_keys(item, found)
    elif isinstance(value, list):
        for item in value: collected_keys(item, found)
    return found
class FakeResponse:
    """A provider body delivered in fixed chunks; not an http.client.HTTPResponse."""
    headers = {}
    def __init__(self, chunks):
        self.chunks = list(chunks)
    def read(self, _size=-1):
        return self.chunks.pop(0) if self.chunks else b""
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
class SteppingClock:
    def __init__(self, step):
        self.now, self.step = 0.0, step
    def monotonic(self):
        value = self.now
        self.now += self.step
        return value
class FakeSocket:
    def __init__(self, data):
        self.data = data
    def makefile(self, *_args, **_kwargs):
        return io.BytesIO(self.data)
def frozen_datetime(moment):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is not None else moment.replace(tzinfo=None)
    return Frozen
class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolve like the workspace code does: %TEMP% can be an 8.3 short path.
        self.workspace = Path(temporary.name).resolve()
        (self.workspace / "Meteo").mkdir()
        (self.workspace / "Results").mkdir()
    def run_download(self, provider_callable, *, workspace=None, **overrides):
        options = dict(csv_name="beijing_2024.csv", year=2024, latitude=39.9075, longitude=116.39723, site_name="Beijing", country_code="CN",
                       timezone_name="Asia/Shanghai", altitude=49.0, dry_run=False)
        options.update(overrides)
        with patch.object(acquisition, "_provider_get", provider_callable): return download_weather(workspace or self.workspace, **options)
    def test_provider_read_and_decode_failures_become_acquisition_errors(self):
        def response(read_effect):
            stream = MagicMock()
            stream.__enter__.return_value.read.side_effect = read_effect
            return stream
        for effect, message in ((http.client.IncompleteRead(b"{"), "transport failure"), (ConnectionResetError("reset by peer"), "transport failure"),
                                ([b"\xff\xfe", b""], "invalid UTF-8")):
            with self.subTest(message=message, effect=type(effect).__name__):
                with patch.object(acquisition.urllib.request, "urlopen", return_value=response(effect)):
                    with self.assertRaisesRegex(WeatherAcquisitionError, message): acquisition._provider_get(acquisition.ARCHIVE_URL, {"x": 1})
    def test_provider_body_deadline_prompt_read_and_bounded_sockets(self):
        slow = FakeResponse([b"{"] + [b" "] * 9 + [b"}"])
        with patch.object(acquisition.urllib.request, "urlopen", return_value=slow), \
                patch.object(acquisition, "time", SteppingClock(100.0), create=True):
            with self.assertRaisesRegex(WeatherAcquisitionError, "request deadline"): acquisition._provider_get(acquisition.ARCHIVE_URL, {"x": 1})
        self.assertTrue(slow.chunks, "the read must stop before the body is exhausted")
        prompt = FakeResponse([b'{"ok": ', b"true}"])
        with patch.object(acquisition.urllib.request, "urlopen", return_value=prompt), \
                patch.object(acquisition, "time", SteppingClock(0.0), create=True):
            payload, raw, _url = acquisition._provider_get(acquisition.ARCHIVE_URL, {"x": 1})
        self.assertEqual((payload, raw), ({"ok": True}, '{"ok": true}'))
        body = json.dumps({"hourly": list(range(5000))}).encode()
        framed = (b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body)) + body
        chunked = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + b"".join(b"%x\r\n%s\r\n" % (len(part), part) for part in (body[:7000], body[7000:]))
                   + b"0\r\n\r\n")
        for framing, raw in (("Content-Length", framed), ("chunked", chunked)):
            with self.subTest(framing=framing):
                response = http.client.HTTPResponse(FakeSocket(raw))
                response.begin()
                self.assertEqual(acquisition._read_bounded(response, deadline=float("inf")), body)
    def test_search_locations_returns_candidates_without_hashes(self):
        with patch.object(acquisition, "_provider_get", fake_provider()): result = search_locations("Beijing", country_code="CN")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["candidate_count"], 2)
        self.assertTrue(result["ambiguous"])
        self.assertTrue(result["selection_required"])
        self.assertEqual([item["location_id"] for item in result["candidates"]], [1816670, 8404324])
        self.assertEqual(result["candidates"][0]["timezone_name"], "Asia/Shanghai")
        self.assertEqual(result["candidates"][0]["country_code"], "CN")
        self.assertEqual(collected_keys(result) & set(HASH_KEYS), set())
    def test_search_locations_empty_and_http_error(self):
        with patch.object(acquisition, "_provider_get", fake_provider(search={})): empty = search_locations("Nowhere")
        self.assertEqual((empty["status"], empty["candidates"], empty["selection_required"]), ("blocked", [], True))
        self.assertIn("no geocoding candidate", empty["issues"][0])
        for bad_results in ({}, False, 0, ""):
            with self.subTest(bad_results=bad_results), \
                    patch.object(acquisition, "_provider_get", fake_provider(search={"results": bad_results})):
                malformed = search_locations("Nowhere")
            self.assertEqual(malformed["status"], "blocked")
            self.assertEqual(malformed["issues"], ["provider returned a non-list geocoding result set"])
        failure = WeatherAcquisitionError("HTTP 429 from geocoding-api.open-meteo.com: slow down")
        with patch.object(acquisition, "_provider_get", fake_provider(error=failure)): failed = search_locations("Beijing")
        self.assertEqual(failed["status"], "blocked")
        self.assertIn("HTTP 429", failed["issues"][0])
    def test_dry_run_plan_standard_offsets_ignore_daylight_saving(self):
        cases = (("Asia/Shanghai", "CN", 2024, 8, 8784, True, ["2023-12-31T16:00:00Z", "2024-12-31T16:00:00Z"]),
            ("Pacific/Honolulu", "US", 2023, -10, 8760, False, ["2023-01-01T10:00:00Z", "2024-01-01T10:00:00Z"]),
            ("Europe/Berlin", "DE", 2024, 1, 8784, True, ["2023-12-31T23:00:00Z", "2024-12-31T23:00:00Z"]),
            ("Australia/Sydney", "AU", 2024, 10, 8784, True, ["2023-12-31T14:00:00Z", "2024-12-31T14:00:00Z"]),
            ("Etc/GMT+12", "UM", 2024, -12, 8784, True, ["2024-01-01T12:00:00Z", "2025-01-01T12:00:00Z"]),)
        for zone, country, year, offset, rows, leap, utc_range in cases:
            with self.subTest(zone=zone):
                csv_name = f"{zone.split('/')[-1].lower()}.csv"
                plan = self.run_download(fake_provider(), dry_run=True, year=year, csv_name=csv_name, country_code=country, timezone_name=zone)
                self.assertEqual(plan["status"], "ready", plan["issues"])
                self.assertEqual(plan["stage"], "plan")
                self.assertEqual(plan["standard_utc_offset_hours"], offset)
                self.assertEqual(plan["plan"]["target_rows"], rows)
                self.assertEqual(plan["plan"]["leap_year"], leap)
                self.assertEqual(plan["plan"]["utc_interval_range"], utc_range)
                self.assertIn("models=era5", plan["plan"]["request_url"])
                self.assertIn("timezone=GMT", plan["plan"]["request_url"])
                self.assertEqual(plan["artifacts"]["csv"], str(self.workspace / "Meteo" / csv_name))
                self.assertTrue(any("instantaneous" in w for w in plan["warnings"]))
        shanghai = self.run_download(fake_provider(), dry_run=True)
        self.assertEqual((shanghai["plan"]["request_start_date"], shanghai["plan"]["request_end_date"]), ("2023-12-31", "2024-12-31"))
        self.assertFalse(list((self.workspace / "Meteo").iterdir()))
    def test_blocked_timezones_non_constant_half_hour_dst_and_bounds(self):
        cases = (({"timezone_name": "Asia/Kolkata", "country_code": "IN"}, ("not a whole hour", "does not do")),
            ({"year": 2018, "timezone_name": "Africa/Casablanca", "country_code": "MA"}, ("not constant during 2018", "no single local standard time basis")),
            ({"timezone_name": "Europe/Dublin", "country_code": "IE"}, ("negative daylight-saving offset", "Europe/Dublin")),
            ({"timezone_name": "Pacific/Tongatapu", "country_code": "TO"}, ("installed PVsyst CLI profile -12..12", "not a data error")),
            ({"timezone_name": "Pacific/Kiritimati", "country_code": "KI"}, ("installed PVsyst CLI profile -12..12", "not a data error")),)
        for overrides, snippets in cases:
            with self.subTest(timezone=overrides["timezone_name"]):
                result = self.run_download(fake_provider(), dry_run=True, **overrides)
                self.assertEqual(result["status"], "blocked")
                for snippet in snippets: self.assertIn(snippet, result["issues"][0])
    def test_unknown_iana_zone_and_missing_tzdata(self):
        unknown = self.run_download(fake_provider(), dry_run=True, timezone_name="Mars/Olympus")
        self.assertEqual(unknown["status"], "blocked")
        self.assertIn("unknown IANA zone", unknown["issues"][0])
        with patch.object(acquisition, "_have_timezone_database", lambda: False), \
                patch.object(acquisition, "ZoneInfo", side_effect=ZoneInfoNotFoundError("no zone data")):
            missing = self.run_download(fake_provider(), dry_run=True, timezone_name="Asia/Shanghai")
        self.assertEqual(missing["status"], "blocked")
        self.assertIn("no IANA time zone database", missing["issues"][0])
    def test_geocoding_location_id_resolution_and_preflight(self):
        resolved = self.run_download(fake_provider(), dry_run=True, location_id=1816670, latitude=None, longitude=None, site_name="",
                                     country_code="", timezone_name="", altitude=None)
        self.assertEqual((resolved["status"], resolved["location"]["location_id"], resolved["location"]["elevation_m"]), ("ready", 1816670, 49.0))
        self.assertIn("geocoding", resolved["location"]["evidence"]["coordinates"])
        conflict = self.run_download(fake_provider(), dry_run=True, location_id=1816670, latitude=None, longitude=None, site_name="",
                                     country_code="US", timezone_name="", altitude=None)
        self.assertEqual(conflict["status"], "blocked")
        self.assertIn("contradicts the geocoded ISO code", conflict["issues"][0])
        base = geocoding_search_payload()["results"][0]
        for overrides, msg in (({"id": 999999}, "refusing to use a different place"), ({"latitude": 95.0}, "is outside -90..90"),
            ({"country_code": "USA"}, "is not an uppercase ISO alpha-2 code"),):
            with self.subTest(overrides=overrides):
                res = self.run_download(fake_provider(single=dict(base, **overrides)), dry_run=True, location_id=1816670, latitude=None, longitude=None,
                                        site_name="", country_code="", timezone_name="", altitude=None)
                self.assertEqual(res["status"], "blocked")
                self.assertIn(msg, res["issues"][0])
    def test_explicit_location_validation_and_coordinate_bounds(self):
        for overrides, snippets in (({"location_id": 1816670}, ("not both",)), ({"timezone_name": ""}, ("timezone_name", "nothing is inferred")),
            ({"site_name": "x" * 201}, ("1..200 printable UTF-8 bytes",)), ({"site_name": "two\nlines"}, ("1..200 printable UTF-8 bytes",)),
            ({"site_name": ""}, ("explicit coordinates require",)), ({"latitude": 91.0}, ("latitude must lie",)), ({"longitude": 181.0}, ("longitude must lie",)),
            ({"temperature_method": "instant"}, ("temperature_method",)),):
            with self.subTest(overrides=overrides):
                result = self.run_download(fake_provider(), dry_run=True, **overrides)
                self.assertEqual(result["status"], "blocked")
                for snippet in snippets: self.assertIn(snippet, result["issues"][0])
    def test_era5_and_prepared_year_bounds_block_before_request(self):
        provider = MagicMock(side_effect=fake_provider())
        for year, dry_run in ((1940, True), (1950, True), (1968, False)):
            with self.subTest(year=year, dry_run=dry_run):
                result = self.run_download(provider, year=year, dry_run=dry_run)
                self.assertEqual(result["status"], "blocked", result)
                self.assertIn("1969..2068", result["issues"][0])
                self.assertIn("two-digit", result["issues"][0])
        too_old = self.run_download(provider, year=1939, dry_run=True)
        self.assertEqual(too_old["status"], "blocked")
        self.assertIn("1940..2068", too_old["issues"][0])
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(list((self.workspace / "Meteo").iterdir()), [])
        self.assertEqual(list((self.workspace / "Results").iterdir()), [])
        self.assertEqual(self.run_download(provider, year=1969, dry_run=True)["status"], "ready")
    def test_unpublished_year_and_era5_lag_block_before_request(self):
        provider = MagicMock(side_effect=fake_provider())
        future = datetime.now(timezone.utc).year + 1
        res_future = self.run_download(provider, year=future, csv_name="future.csv", dry_run=True)
        self.assertEqual(res_future["status"], "blocked")
        self.assertIn("not fully published", res_future["issues"][0])
        early = frozen_datetime(datetime(2026, 1, 3, 12, tzinfo=timezone.utc))
        with patch.object(acquisition, "datetime", early):
            for dry_run in (True, False):
                with self.subTest(dry_run=dry_run):
                    res = self.run_download(provider, year=2025, dry_run=dry_run)
                    self.assertEqual(res["status"], "blocked", res)
                    self.assertIn("not fully published", res["issues"][0])
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(list((self.workspace / "Meteo").iterdir()), [])
        later = frozen_datetime(datetime(2026, 1, 7, 12, tzinfo=timezone.utc))
        with patch.object(acquisition, "datetime", later): self.assertEqual(self.run_download(provider, year=2025, dry_run=True)["status"], "ready")
    def test_number_text_never_writes_negative_zero(self):
        for value in (-1e-7, -4e-7, -0.0, 0, 1e-7):
            with self.subTest(value=value): self.assertEqual(acquisition._number_text(value), "0")
        self.assertEqual(acquisition._number_text(-1.5), "-1.5")
        self.assertEqual(acquisition._number_text(2.000001), "2.000001")
        site = {"name": "Equator", "country_code": "EC", "latitude": -1e-7, "longitude": -78.5, "elevation_m": 2850.0}
        text = acquisition._standard_csv_text([(datetime(2024, 1, 1), 0.0, 0.0, -2e-7)], site=site, year=2024, offset_hours=-5)
        self.assertIn("#Latitude;0\n", text)
        self.assertTrue(text.endswith("2024;1;1;0;0;0;0\n"), text)
    def test_execution_writes_csv_endpoint_mean_and_manifest_without_hashes(self):
        result = self.run_download(fake_provider())
        self.assertEqual((result["status"], result["stage"], result["raw_preserved"]), ("prepared", "done", True))
        raw_path = self.workspace / "Meteo" / "beijing_2024_openmeteo_raw.json"
        csv_path = self.workspace / "Meteo" / "beijing_2024.csv"
        manifest_path = self.workspace / "Results" / "beijing_2024_acquisition.json"
        for path in (raw_path, csv_path, manifest_path): self.assertTrue(path.is_file(), path)
        self.assertEqual(json.loads(raw_path.read_text(encoding="utf-8"))["timezone"], "GMT")
        metadata, header, units, rows = read_standard_csv(csv_path)
        self.assertEqual(header, ["Year", "Month", "Day", "Hour", "GHI", "DHI", "Tamb"])
        self.assertEqual(units, ["", "", "", "", "W/m2", "W/m2", "deg.C"])
        self.assertEqual((metadata["#Time step"], metadata["#Time Zone"], metadata["#Country"]), ("Hour", "8", "CN"))
        self.assertEqual((len(rows), rows[0][:4], rows[-1][:4]), (8784, ["2024", "1", "1", "0"], ["2024", "12", "31", "23"]))
        self.assertIn(["2024", "2", "29", "12"], [row[:4] for row in rows])
        self.assertEqual([row[:4] for row in rows].count(["2024", "3", "1", "0"]), 1)
        # local 2024-01-01T00:00 at UTC+8 bounds UTC 16:00 and 17:00; 01:00 bounds 17:00 and 18:00.
        self.assertEqual((rows[0][6], rows[1][6]), ("16.5", "17.5"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for text in (json.dumps(result), json.dumps(manifest), csv_path.read_text(encoding="utf-8")):
            lowered = text.lower()
            for key in HASH_KEYS: self.assertNotIn(key, lowered)
        self.assertEqual(result["provider_echo"]["grid_latitude"], 40.0)
        self.assertEqual(manifest["created_files"]["csv"]["rows"], 8784)
        self.assertEqual(manifest["created_files"]["manifest"], {"name": "beijing_2024_acquisition.json"})
        self.assertEqual(manifest, result)
    def test_execution_creates_folders_while_plan_never_does(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            plan = self.run_download(fake_provider(), workspace=workspace, dry_run=True)
            self.assertEqual((plan["status"], list(workspace.iterdir())), ("ready", []))
            executed = self.run_download(fake_provider(), workspace=workspace, dry_run=False)
            self.assertEqual(executed["status"], "prepared")
            self.assertTrue((workspace / "Meteo" / "beijing_2024.csv").is_file())
            self.assertTrue((workspace / "Results" / "beijing_2024_acquisition.json").is_file())
            empty = self.run_download(fake_provider(), workspace=workspace, csv_name="", dry_run=True)
            self.assertEqual((empty["status"], empty["created_files"]), ("ready", {}))
    def test_missing_provider_hours_block_and_keep_raw_response(self):
        missing = (datetime(2024, 6, 1, 3, 0), datetime(2024, 6, 1, 4, 0))
        result = self.run_download(fake_provider(archive_kwargs={"missing": missing}))
        self.assertEqual((result["status"], result["stage"], result["raw_preserved"]), ("blocked", "validation", True))
        self.assertTrue((self.workspace / "Meteo" / "beijing_2024_openmeteo_raw.json").is_file())
        self.assertFalse((self.workspace / "Meteo" / "beijing_2024.csv").exists())
        self.assertIn("nothing is filled", result["issues"][0])
        manifest_path = self.workspace / "Results" / "beijing_2024_acquisition.json"
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), result)
    def test_http_failure_is_reported_as_failed(self):
        failure = WeatherAcquisitionError("HTTP 429 from archive-api.open-meteo.com: rate limited")
        result = self.run_download(fake_provider(error=failure))
        self.assertEqual((result["status"], result["stage"]), ("failed", "request"))
        self.assertIn("HTTP 429", result["issues"][0])
        self.assertFalse(list((self.workspace / "Meteo").iterdir()))
        manifest_path = self.workspace / "Results" / "beijing_2024_acquisition.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual((manifest["status"], manifest["issues"], manifest["raw_preserved"]), ("failed", result["issues"], False))
    def test_existing_csv_or_manifest_is_never_overwritten(self):
        for target_rel, content, forbid_raw in (("Results/beijing_2024_acquisition.json", '{"status": "existing"}\n', False),
            ("Meteo/beijing_2024.csv", "existing content\n", True),):
            with self.subTest(target=target_rel), tempfile.TemporaryDirectory() as raw:
                ws = Path(raw)
                (ws / "Meteo").mkdir()
                (ws / "Results").mkdir()
                target = ws / target_rel
                target.write_text(content, encoding="utf-8")
                result = self.run_download(fake_provider(), workspace=ws)
                self.assertEqual(result["status"], "blocked")
                self.assertIn("refusing to overwrite", result["issues"][0])
                self.assertEqual(target.read_text(encoding="utf-8"), content)
                if forbid_raw: self.assertFalse((ws / "Meteo" / "beijing_2024_openmeteo_raw.json").exists())
    def test_provider_validation_rejects_dhi_gt_ghi_bad_unit_and_non_gmt_echo(self):
        def bad_echo(url, params, **_kwargs):
            payload = archive_payload(params)
            payload["utc_offset_seconds"] = 3600
            return payload, json.dumps(payload), url + "?" + urlencode(params)
        cases = (("dhi_above_ghi", fake_provider(archive_kwargs={"dhi_above_ghi": True}), "exceeds global horizontal irradiance"),
            ("bad_unit", fake_provider(archive_kwargs={"unit": "kJ/m\u00b2"}), "is not one of"), ("non_gmt", bad_echo, "did not echo the requested GMT context"),)
        for label, provider, expected in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as raw:
                ws = Path(raw)
                (ws / "Meteo").mkdir()
                (ws / "Results").mkdir()
                result = self.run_download(provider, workspace=ws)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(expected, result["issues"][0])
                self.assertFalse((ws / "Meteo" / "beijing_2024.csv").exists())
OFFSET = 8
def year_hours(year):
    return (366 if calendar.isleap(year) else 365) * 24
@lru_cache(maxsize=8)
def _cached_provider_labels(first_day, last_day):
    first = datetime(first_day.year, first_day.month, first_day.day)
    last = datetime(last_day.year, last_day.month, last_day.day, 23, 0)
    return tuple(first + timedelta(hours=index) for index in range(int((last - first).total_seconds() // 3600) + 1))
def provider_labels(first_day, last_day):
    return list(_cached_provider_labels(first_day, last_day))
@lru_cache(maxsize=8)
def _cached_provider_values(labels, tam_offset):
    ghi, dhi, tamb = [], [], []
    for stamp in labels:
        day = 100.0 if 8 <= stamp.hour < 16 else 0.0
        ghi.append(day)
        dhi.append(day / 2.0)
        tamb.append(round(-5.0 + stamp.hour * 0.5 + tam_offset, 1))
    return tuple(ghi), tuple(dhi), tuple(tamb)
def provider_values(labels, *, null_labels=(), tam_offset=0.0):
    base_ghi, base_dhi, base_tamb = _cached_provider_values(tuple(labels), tam_offset)
    ghi, dhi, tamb = list(base_ghi), list(base_dhi), list(base_tamb)
    if null_labels:
        nulls = set(null_labels)
        for index, stamp in enumerate(labels):
            if stamp in nulls: ghi[index] = dhi[index] = tamb[index] = None
    return ghi, dhi, tamb
def provider_payload(year, *, offset=OFFSET, null_labels=(), timezone="GMT", utc_offset=0, ghi_unit="W/m\u00b2", tam_unit="\u00b0C"):
    utc_start = datetime(year, 1, 1) - timedelta(hours=offset)
    utc_end = utc_start + timedelta(hours=year_hours(year))
    labels = provider_labels(utc_start.date(), utc_end.date())
    ghi, dhi, tamb = provider_values(labels, null_labels=null_labels)
    return {"latitude": 40.0, "longitude": 116.5, "elevation": 47.0, "timezone": timezone, "utc_offset_seconds": utc_offset,
        "hourly_units": {"time": "iso8601", "shortwave_radiation": ghi_unit, "diffuse_radiation": ghi_unit, "temperature_2m": tam_unit},
        "hourly": {"time": [stamp.isoformat() for stamp in labels], "shortwave_radiation": ghi, "diffuse_radiation": dhi, "temperature_2m": tamb},}
def csv_rows(year, *, offset=OFFSET, drop=None, tam_offset=0.0, negative_at=None):
    hours = year_hours(year)
    utc_start = datetime(year, 1, 1) - timedelta(hours=offset)
    utc_end = utc_start + timedelta(hours=hours)
    labels = provider_labels(utc_start.date(), utc_end.date())
    index = {stamp: position for position, stamp in enumerate(labels)}
    ghi, dhi, tamb = provider_values(labels, tam_offset=tam_offset)
    rows = []
    for position in range(hours):
        local = datetime(year, 1, 1) + timedelta(hours=position)
        if local == drop: continue
        radiation = utc_start + timedelta(hours=position + 1)
        start = utc_start + timedelta(hours=position)
        mean = (tamb[index[start]] + tamb[index[radiation]]) / 2.0
        value = ghi[index[radiation]]
        if negative_at is not None and position == negative_at: value = -3.0
        rows.append((local, value, dhi[index[radiation]], mean))
    return rows
def _text(value):
    if isinstance(value, str): return value
    return str(int(value)) if float(value) == int(value) else f"{value:.6f}".rstrip("0").rstrip(".")
def weather_csv_text(year, rows, *, extra_metadata=(), units=None):
    units = units or {"GHI": "W/m2", "DHI": "W/m2", "Tamb": "deg.C"}
    lines = ["#Weather data hourly data", "#Site;Fixture Site", "#Country;CN",
             "#Data Source;Independent offline fixture", "#Time step;Hour", f"#Year;{year}",
             "#Latitude;39.9075", "#Longitude;116.39723", "#Altitude;47", f"#Time Zone;{OFFSET}"]
    lines.extend(extra_metadata)
    lines.append("Year;Month;Day;Hour;GHI;DHI;Tamb")
    lines.append(";;;;%s;%s;%s" % (units["GHI"], units["DHI"], units["Tamb"]))
    for stamp, ghi, dhi, tamb in rows:
        lines.append("%d;%d;%d;%d;%s;%s;%s" % (stamp.year, stamp.month, stamp.day, stamp.hour, _text(ghi), _text(dhi), _text(tamb)))
    return "\n".join(lines) + "\n"
def native_csv_text(rows, *, units=("W/m\u00b2", "W/m\u00b2", "\u00b0C"), shift_hours=0, glob_delta=0.0, sit="FIXTURE.SIT", met="FIXTURE.MET", context=True):
    lines = ["PVSYST 8.1.6", ";File;File date;Description", "Project;FIXTURE.PRJ;01/01/24 00h00;offline fixture;"]
    if context:
        lines.append(f"Geographical Site;{sit};01/01/24 00h00;Fixture Site;CN;Asia;;")
        lines.append(f"Weather data;{met};01/01/24 00h00;Fixture Site;Custom file;Imported;")
    lines += ["", "Simulation:;Hourly values; from 01/01/24; to 31/12/24", "", "date;GlobInc;GlobHor;DiffHor;T_Amb",
              "    ;W/m\u00b2;%s;%s;%s" % (units[0], units[1], units[2])]
    for stamp, ghi, dhi, tamb in rows:
        label = (stamp + timedelta(hours=shift_hours)).strftime("%d/%m/%y %H:%M")
        lines.append("%s;0;%s;%s;%s" % (label, _text(ghi + glob_delta), _text(dhi), _text(tamb)))
    return "\n".join(lines) + "\n"
def acquisition_manifest(year, *, raw_name="fixture_raw.json", status="prepared", timezone_name="Asia/Shanghai", method="endpoint_mean",
                         time_basis="local_standard_interval_start", manifest_year=None, csv_name="fixture_weather.csv"):
    return {"status": status, "year": manifest_year if manifest_year is not None else year, "method": method, "time_basis": time_basis,
        "location": {"name": "Fixture Site", "country_code": "CN", "latitude": 39.9075, "longitude": 116.39723, "timezone_name": timezone_name},
        "created_files": {"raw_json": {"name": raw_name}, "csv": {"name": csv_name}}, "artifacts": {"csv": "/definitely/not/trusted/fixture.csv"},}
def weather_manifest(*, met_name="FIXTURE.MET", sit_name="FIXTURE.SIT", source_name="fixture_weather.csv",
                     status="needs_review", time_basis="local_standard_interval_start", year=2024):
    return {"status": status, "year": year, "time_basis": time_basis, "source": {"name": source_name}, "created_files": {"met": {"name": met_name}, "sit": {"name": sit_name}}}
def met_text(hleg="False", *, site="Fixture Site", binary_after=True, var_meteo=True, end_of_data=True, unknown_object=False, date_deb="01/01/24 00:00"):
    text = ("PVObject_=pvMeteo\r\n  Comment=fixture\r\n  Version=8.1.6\r\n  Flags=$0143\r\n"
            "  IsSourceUt=False\r\n  HourShift=0\r\n  TimeShift=0\r\n  TimeShiftTotalMin=0\r\n" "  PasAccum=60\r\n  VarMeteoStepDuration=60\r\n  DateDebF=%s\r\n"
            "  PVObject_SiteMet=pvSite\r\n" "    Version=8.1.6\r\n    Flags=$37\r\n    Site=%s\r\n    TimeZone=8.0\r\n"
            "    HLeg=%s\r\n  End of PVObject pvSite\r\n" % (date_deb, site, hleg))
    if unknown_object: text += "  PVObject_SomeChild=pvOther\r\n    HourShift=7\r\n  End of PVObject pvOther\r\n"
    if var_meteo: text += "  VarMeteo, list of=1 TVarSimul\r\n    VarSimul_1=GlobHor\r\n      Day_0101=+0+GW+5zb\r\n"
    if end_of_data: text += "  End of Data\r\n"
    text += "  End of PVObject pvMeteo\r\n"
    if binary_after: text += "\x00\x01\x02binary-hour-array-not-read\r\n"
    return text
def sit_text(hleg="True"):
    return ("PVObject_=pvSite\r\n  Version=8.1.6\r\n  Site=Fixture Site\r\n" "  TimeZone=8.0\r\n  HLeg=%s\r\n  End of PVObject pvSite\r\n" % hleg)
class ReviewFixture(unittest.TestCase):
    year = 2024
    csv_name = "fixture_weather.csv"
    native_name = "fixture_native.csv"
    @classmethod
    def setUpClass(cls):
        cls._base_rows = csv_rows(cls.year)
        cls._base_csv = weather_csv_text(cls.year, cls._base_rows).encode("utf-8-sig")
        cls._base_native = native_csv_text(cls._base_rows).encode("utf-8-sig")
        cls._base_provider = json.dumps(provider_payload(cls.year)).encode("utf-8")
        cls._base_manifest = json.dumps(acquisition_manifest(cls.year)).encode("utf-8")
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for folder in ("Meteo", "Results", "Sites"): (self.root / folder).mkdir()
        self.rows = self._base_rows
        self._reset_baseline()
    def _reset_baseline(self):
        (self.root / "Meteo" / self.csv_name).write_bytes(self._base_csv)
        (self.root / "Results" / self.native_name).write_bytes(self._base_native)
        (self.root / "Meteo" / "fixture_raw.json").write_bytes(self._base_provider)
        (self.root / "Results" / "fixture_acquisition.json").write_bytes(self._base_manifest)
    def write_csv(self, *, rows=None, extra_metadata=(), units=None):
        rows = self.rows if rows is None else rows
        (self.root / "Meteo" / self.csv_name).write_text(weather_csv_text(self.year, rows, extra_metadata=extra_metadata, units=units), encoding="utf-8-sig")
    def write_native(self, *, rows=None, units=("W/m\u00b2", "W/m\u00b2", "\u00b0C"), shift_hours=0, glob_delta=0.0, sit="FIXTURE.SIT", met="FIXTURE.MET", context=True):
        rows = self.rows if rows is None else rows
        (self.root / "Results" / self.native_name).write_text(
            native_csv_text(rows, units=units, shift_hours=shift_hours, glob_delta=glob_delta, sit=sit, met=met, context=context), encoding="utf-8-sig")
    def write_provider(self, *, payload=None, manifest=None):
        if payload is not None: (self.root / "Meteo" / "fixture_raw.json").write_text(json.dumps(payload), encoding="utf-8")
        if manifest is not None: (self.root / "Results" / "fixture_acquisition.json").write_text(json.dumps(manifest), encoding="utf-8")
    def review(self, **overrides):
        arguments = {"csv_name": self.csv_name, "native_csv_name": self.native_name, "year": self.year}
        arguments.update(overrides)
        return review_weather(self.root, **arguments)
class WeatherReviewTests(ReviewFixture):
    def test_value_inspector_complete_year_and_gap(self):
        report = inspect_weather_values(self.root / "Meteo" / self.csv_name, self.year)
        self.assertIs(report["valid"], True)
        self.assertEqual((report["rows"], report["missing_hours"], report["delimiter"]), (8784, 0, ";"))
        self.assertEqual((report["units"]["GHI"], report["metadata"]["#Time Zone"]), ("W/m2", "8"))
        self.assertAlmostEqual(report["months"][1]["GHI"]["observed_kwh_m2"], 23.2, places=6)
        self.assertAlmostEqual(sum(m["GHI"]["observed_kwh_m2"] for m in report["months"]), 366 * 0.8, places=6)
        self.write_csv(rows=csv_rows(self.year, drop=datetime(self.year, 6, 1, 12)))
        gapped = inspect_weather_values(self.root / "Meteo" / self.csv_name, self.year)
        self.assertIs(gapped["valid"], False)
        self.assertEqual(gapped["missing_hours"], 1)
        self.assertIn("2024-06-01 12:00:00", gapped["missing_examples"])
    def test_complete_year_trailing_csv_native_delta_and_compare_stats(self):
        report = self.review(acquisition_manifest_name="fixture_acquisition.json")
        self.assertEqual((report["status"], report["issues"]), ("needs_review", []))
        self.assertNotIn("scope", report)
        self.assertTrue((report["csv_to_native"]["timestamps_match"], report["provider_to_csv"]["matches"]) == (True, True))
        self.assertEqual(report["provider_to_csv"]["rows_compared"], year_hours(self.year))
        trailed = "".join(line + ";\n" for line in self._base_csv.decode("utf-8-sig").splitlines())
        (self.root / "Meteo" / self.csv_name).write_text(trailed, encoding="utf-8-sig")
        trailed_report = self.review(acquisition_manifest_name="fixture_acquisition.json")
        self.assertTrue(trailed_report["provider_to_csv"]["matches"])
        self.assertEqual(trailed_report["csv_to_native"]["rows"], year_hours(self.year))
        self._reset_baseline()
        self.write_native(glob_delta=0.5)
        delta_report = self.review()
        self.assertGreater(delta_report["csv_to_native"]["columns"]["GlobHor"]["outside_review_threshold"], 0)
        self.assertTrue(any("csv_to_native" in item for item in delta_report["review_items"]))
        source = self.root / "Meteo" / self.csv_name
        rep = inspect_weather_values(source, self.year)
        comp = compare_native_weather(self.root / "Results" / self.native_name, source, rep)
        self.assertTrue(comp["needs_review"])
        self.assertEqual(len(comp["monthly"]), 12)
        for col in comp["columns"].values():
            self.assertAlmostEqual(col["max_abs_delta"], col["max_abs"])
            self.assertAlmostEqual(col["mean_signed"], col["signed_delta_sum"] / col["samples"])
    def test_blocked_sources_units_and_arguments(self):
        for mutator, expected in ((lambda: self.write_native(shift_hours=1), "Native weather labels"),
            (lambda: self.write_native(units=("kW/m\u00b2", "W/m\u00b2", "\u00b0C")), "W/m2"), (lambda: self.write_native(units=("W/m\u00b2", "W/m\u00b2", "K")), "degrees C"),
            (lambda: self.write_csv(rows=csv_rows(self.year, negative_at=200)), "negative"),
            (lambda: self.write_csv(extra_metadata=("#Time Shift;15",)), "#Time Shift"),
        ):
            self._reset_baseline()
            mutator()
            rep = self.review()
            self.assertEqual(rep["status"], "blocked")
            self.assertTrue(any(expected in issue for issue in rep["issues"]), rep["issues"])
        for kwargs in ({"csv_name": "../escape.csv"}, {"csv_name": "escape.txt"}, {"native_csv_name": "absent.csv"},
                       {"year": True}, {"year": 2024.0}, {"year": 1900}, {"year": None}):
            self.assertEqual(self.review(**kwargs)["status"], "blocked")
    def test_provider_evidence_validation_and_mismatch(self):
        self.assertIsNone(self.review()["provider_to_csv"])
        self.write_provider(payload=provider_payload(self.year, null_labels=(datetime(self.year, 3, 1, 9),)))
        null_rep = self.review(acquisition_manifest_name="fixture_acquisition.json")
        self.assertGreater(null_rep["provider_to_csv"]["rows_without_a_provider_row"], 0)
        for change, msg in ((lambda: self.write_provider(payload=provider_payload(self.year, timezone="Europe/Paris", utc_offset=3600)), "GMT"),
            (lambda: self.write_provider(manifest=acquisition_manifest(self.year, manifest_year=self.year - 1)), "year"),
            (lambda: self.write_provider(manifest=acquisition_manifest(self.year, method="instantaneous")), "method"),
            (lambda: (self.root / "Meteo" / "fixture_raw.json").write_text("[1,2]"), "not a JSON object"),):
            self._reset_baseline()
            change()
            rep = self.review(acquisition_manifest_name="fixture_acquisition.json")
            self.assertIsNone(rep["provider_to_csv"])
            self.assertTrue(any(msg in item for item in rep["review_items"]), rep["review_items"])
        self._reset_baseline()
        broken = list(self.rows)
        stamp, ghi, dhi, tamb = broken[5000]
        broken[5000] = (stamp, ghi + 40.0, dhi, tamb)
        self.write_csv(rows=broken)
        self.write_native(rows=broken)
        mismatch = self.review(acquisition_manifest_name="fixture_acquisition.json")
        self.assertFalse(mismatch["provider_to_csv"]["matches"])
        self.assertGreater(mismatch["provider_to_csv"]["columns"]["GHI"]["max_abs_delta"], 30.0)
class TimeBasisAndMetTests(ReviewFixture):
    def write_time_basis(self, *, met_hleg="False", sit_hleg="True", site="Fixture Site", var_meteo=True, end_of_data=True, unknown_object=False,
                         date_deb="01/01/24 00:00", native_met="FIXTURE.MET", native_context=True):
        (self.root / "Meteo" / "FIXTURE.MET").write_text(
            met_text(met_hleg, site=site, var_meteo=var_meteo, end_of_data=end_of_data, unknown_object=unknown_object, date_deb=date_deb), encoding="utf-8-sig")
        (self.root / "Sites" / "FIXTURE.SIT").write_text(sit_text(sit_hleg), encoding="utf-8-sig")
        (self.root / "Results" / "fixture_weather.json").write_text(json.dumps(weather_manifest()), encoding="utf-8")
        self.write_native(context=native_context, met=native_met)
    def test_time_basis_binding_header_markers_and_conflicts(self):
        self.assertIsNone(self.review()["time_basis"]["context_matches"])
        self.write_time_basis(site="\u5317\u4eac\u6d4b\u8bd5\u7ad9")
        rep = self.review(weather_manifest_name="fixture_weather.json")
        basis = rep["time_basis"]
        self.assertTrue((basis["reviewed"], basis["context_matches"]) == (True, True))
        self.assertEqual((basis["consumed_met"], basis["declared_source_year"]), ("FIXTURE.MET", 2024))
        self.assertEqual(basis["met_site_fields"]["Site"][0]["value"], "\u5317\u4eac\u6d4b\u8bd5\u7ad9")
        self.assertNotIn("HLeg", basis["met_fields"])
        self.assertNotIn("VarSimul_1", basis["met_fields"])
        self.assertTrue(any("HLeg" in item for item in rep["review_items"]))
        for kwargs, reason in (({"var_meteo": False, "end_of_data": False}, "end of the pvMeteo object"), ({"var_meteo": False, "end_of_data": True}, "End of Data"),
            ({"var_meteo": False, "end_of_data": False, "unknown_object": True}, "unknown nested object"),):
            self.write_time_basis(**kwargs)
            stop = self.review(weather_manifest_name="fixture_weather.json")["time_basis"]["met_header_read_stop"]
            self.assertIn(reason, stop["reason"])
        for kwargs, msg in (({"native_met": "OTHER.MET"}, "does not name the manifest"), ({"native_context": False}, "does not name the manifest"),
            ({"date_deb": "01/01/19 00:00"}, "declares its own data year"),):
            self.write_time_basis(**kwargs)
            conflicts = self.review(weather_manifest_name="fixture_weather.json")["time_basis"]["conflicts"]
            self.assertTrue(any(msg in item for item in conflicts), conflicts)
    def test_generated_met_declarations_and_mef_template(self):
        met = self.root / "Meteo" / "GEN.MET"
        met.write_text(met_text("True"), encoding="utf-8-sig")
        self.assertTrue(check_generated_met(met, time_zone=8)["valid"])
        met.write_text(met_text("True").replace("  HourShift=0\r\n", ""), encoding="utf-8-sig")
        self.assertTrue(check_generated_met(met, time_zone=8)["unchecked_checks"])
        bad_cases = ((met_text("False"), 8, "HLeg"), (met_text("True"), 9, "TimeZone"),
            (met_text("True").replace("  TimeShift=0\r\n", "  TimeShift=15\r\n"), 8, "TimeShift=15"),
            (met_text("True").replace("    HLeg=True\r\n", "    HLeg=True\r\n    HLeg=True\r\n"), 8, "more than once"),
            (met_text("True").replace("  TimeShift=0\r\n", "  TimeShift=0\r\n  TimeShift=0\r\n"), 8, "more than once"),
            (met_text("True").replace("  IsSourceUt=False\r\n", "  IsSourceUt=True\r\n"), 8, "IsSourceUt=True"),
            (met_text("True").replace("    HLeg=True\r\n", ""), 8, "no SiteMet HLeg"),)
        for text, tz, msg in bad_cases:
            met.write_text(text, encoding="utf-8-sig")
            res = check_generated_met(met, time_zone=tz)
            self.assertFalse(res["valid"])
            self.assertTrue(any(msg in issue for issue in res["issues"]), res["issues"])
        self.assertIn("RefTimeS=LegalTime", MEF_TEMPLATE)
        self.assertNotIn("SolarTime", MEF_TEMPLATE)
@lru_cache(maxsize=8)
def _subhourly_weather_text(year: int, step_minutes: int, unit: str) -> str:
    lines = [
        "#Weather data sub-hourly data",
        "#Site;TestSite",
        "#Country;Switzerland",
        "#Data Source;Synthetic",
        f"#Time step;{step_minutes} min",
        f"#Year;{year}",
        "#Latitude;46.24",
        "#Longitude;6.11",
        "#Altitude;425",
        "#Time Zone;1",
        "Year;Month;Day;Hour;Minute;GHI;DHI;Tamb", f";;;;;{unit};{unit};deg.C",]
    start = datetime(year, 1, 1, 0, 0)
    end = datetime(year + 1, 1, 1, 0, 0)
    step = timedelta(minutes=step_minutes)
    cur = start
    while cur < end:
        ghi_w = 400.0 if 8 <= cur.hour < 16 else 0.0
        dhi_w = 120.0 if 8 <= cur.hour < 16 else 0.0
        if unit == "MJ/m2":
            # Energy per step in MJ/m2: W/m2 * step_hours * 3.6 / 1000
            factor = (step_minutes / 60.0) * 3.6 / 1000.0
            ghi_val = f"{ghi_w * factor:.6f}"
            dhi_val = f"{dhi_w * factor:.6f}"
        else:
            ghi_val = f"{ghi_w:.1f}"
            dhi_val = f"{dhi_w:.1f}"
        lines.append(f"{cur.year};{cur.month};{cur.day};{cur.hour};{cur.minute};{ghi_val};{dhi_val};20.0")
        cur += step
    return "\n".join(lines) + "\n"
def _write_subhourly_weather_csv(path: Path, *, year: int = 2023, step_minutes: int = 15, unit: str = "W/m2"):
    path.write_text(_subhourly_weather_text(year, step_minutes, unit), encoding="utf-8")
@lru_cache(maxsize=8)
def _subhourly_result_text(year: int, step_minutes: int, dc_kw: float) -> str:
    lines = ["PVSYST 8.1.7", "date;GlobHor;DiffHor;T_Amb;GlobInc;EArray;E_Grid;IL_Pmax;PR", ";W/m2;W/m2;degC;W/m2;kW;kW;kW;ratio"]
    start = datetime(year, 1, 1, 0, 0)
    end = datetime(year + 1, 1, 1, 0, 0)
    step = timedelta(minutes=step_minutes)
    cur = start
    while cur < end:
        if 8 <= cur.hour < 16:
            # GlobInc = 500 W/m2 -> reference power = 0.5 * 10 kWp = 5 kW -> E_Grid = 4.0 kW => PR = 0.80
            lines.append(f"{cur:%d/%m/%y %H:%M};400.0;120.0;20.0;500.0;4.2;4.0;0.1;0.800")
        else: lines.append(f"{cur:%d/%m/%y %H:%M};0.0;0.0;20.0;0.0;0.0;0.0;0.0;0.0")
        cur += step
    return "\n".join(lines) + "\n"
def _write_subhourly_result_csv(path: Path, *, year: int = 2023, step_minutes: int = 15, dc_kw: float = 10.0):
    path.write_text(_subhourly_result_text(year, step_minutes, dc_kw), encoding="utf-8")
class SubHourlyPipelineUnitTests(unittest.TestCase):
    def test_inspect_15min_weather_csv_w_m2_and_mj_m2_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            p_w = Path(tmp) / "w15.csv"
            p_mj = Path(tmp) / "mj15.csv"
            _write_subhourly_weather_csv(p_w, year=2023, step_minutes=15, unit="W/m2")
            _write_subhourly_weather_csv(p_mj, year=2023, step_minutes=15, unit="MJ/m2")
            rw = inspect_weather_csv(p_w, 2023)
            rmj = inspect_weather_csv(p_mj, 2023)
            self.assertTrue(rw["valid"], rw["issues"])
            self.assertTrue(rmj["valid"], rmj["issues"])
            self.assertEqual(rw["step_minutes"], 15)
            self.assertEqual(rw["format"], "pvsyst_standard_subhourly")
            self.assertEqual(rw["rows"], 35040)
            self.assertEqual(rw["expected_rows"], 35040)
            # Jan has 31 days * 8 sunlit hours * 0.4 kW/m2 = 99.2 kWh/m2
            self.assertAlmostEqual(rw["months"][0]["GHI"]["observed_kwh_m2"], 99.2, places=3)
            self.assertAlmostEqual(rmj["months"][0]["GHI"]["observed_kwh_m2"], 99.2, places=3)
            # Requesting mismatched step_minutes must flag an issue
            bad = inspect_weather_csv(p_w, 2023, step_minutes=30)
            self.assertFalse(bad["valid"])
    def test_render_mef_and_check_generated_met_for_subhourly(self):
        mef15 = render_mef(15)
        self.assertIn("MeasureStep=15", mef15)
        self.assertIn("15-minute sub-hourly", mef15)
        with self.assertRaises(ValueError): render_mef(7)
        with tempfile.TemporaryDirectory() as tmp:
            met = Path(tmp) / "Site_15m.MET"
            met.write_text(
                'PVObject_=pvMeteo\n  IsSourceUt=False\n  HourShift=0\n  TimeShift=0\n  TimeShiftTotalMin=0\n  PasAccum=15\n  VarMeteoVarAcc=SubHour\n  VarMeteoStepDuration=15\n  PVObject_SiteMet=pvSite\n    HLeg=True\n    TimeZone=1.0\n  End of PVObject pvSite\nEnd of PVObject pvMeteo' + "\n",
                encoding="utf-8",)
            ok = check_generated_met(met, time_zone=1.0, expected_step_minutes=15)
            self.assertTrue(ok["valid"], ok["issues"])
            self.assertEqual(ok["step_minutes"], 15)
            wrong = check_generated_met(met, time_zone=1.0, expected_step_minutes=60)
            self.assertFalse(wrong["valid"])
    def test_build_sfi_time_step_subhour(self):
        with tempfile.TemporaryDirectory() as tmp:
            sfi = Path(tmp) / "Sub15.SFI"
            build_sfi(sfi, "all_common", time_step="subhour")
            text = sfi.read_text(encoding="utf-8")
            self.assertIn("TypeValW=SubHour", text)
            with self.assertRaises(ValueError): build_sfi(Path(tmp) / "Bad.SFI", "energy", time_step="15min")
    def test_annual_summary_and_compare_native_weather_15min(self):
        with tempfile.TemporaryDirectory() as tmp:
            wcsv = Path(tmp) / "w15.csv"
            rcsv = Path(tmp) / "r15.csv"
            _write_subhourly_weather_csv(wcsv, year=2023, step_minutes=15, unit="W/m2")
            _write_subhourly_result_csv(rcsv, year=2023, step_minutes=15, dc_kw=10.0)
            summary = annual_summary(rcsv, 2023, nominal_dc_power_kw=10.0)
            self.assertEqual(summary["step_minutes"], 15)
            self.assertEqual(summary["rows"], 35040)
            self.assertEqual(summary["last"], "2023-12-31 23:45:00")
            # 365 days * 8 sunlit hours * 4.0 kW = 11680 kWh
            self.assertAlmostEqual(summary["grid_energy_kwh"], 11680.0, places=3)
            # 365 days * 8 sunlit hours * 0.5 kW/m2 = 1460 kWh/m2
            self.assertAlmostEqual(summary["incident_irradiation_kwh_m2"], 1460.0, places=3)
            self.assertAlmostEqual(summary["pr_ratio"], 0.80, places=4)
            sres = summarize_results(rcsv, ("E_Grid", "EArray"))
            self.assertEqual(sres["step_minutes"], 15.0)
            self.assertAlmostEqual(sres["summary"]["E_Grid"]["energy_kwh"], 11680.0, places=3)
            wrep = inspect_weather_csv(wcsv, 2023)
            cmp = compare_native_weather(rcsv, wcsv, wrep)
            self.assertTrue(cmp["timestamps_match"])
            self.assertEqual(cmp["regular_step_minutes"], 15.0)
            self.assertAlmostEqual(cmp["signed_energy_delta_kwh_m2"]["GlobHor"], 0.0, places=6)
    def test_summarize_results_sums_wh_and_kwh_interval_energy_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamps = ("01/06/23 08:00", "01/06/23 08:15", "01/06/23 08:30", "01/06/23 08:45")
            for unit, val in (("kWh", "1.0"), ("Wh", "1000.0")):
                with self.subTest(unit=unit):
                    csv_path = Path(tmp) / f"energy_{unit}.csv"
                    lines = ["PVSYST 8.1.7", "date;E_Grid", f";{unit}"] + [f"{s};{val}" for s in stamps]
                    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    res = summarize_results(csv_path, ("E_Grid",))
                    self.assertEqual(res["step_minutes"], 15.0)
                    self.assertAlmostEqual(res["summary"]["E_Grid"]["energy_kwh"], 4.0, places=6)
                    self.assertAlmostEqual(res["summary"]["E_Grid"]["observed_energy_kwh"], 4.0, places=6)
    def test_prepare_weather_blocks_utc_plus_13_in_preflight_before_writing(self):
        from pvsyst_auto.native.cli import PVsystCLI
        from pvsyst_auto.weather.preparation import prepare_weather
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "PVsyst8.1_Data"
            for sub in ("Meteo", "Sites", "Results"): (ws / sub).mkdir(parents=True, exist_ok=True)
            tonga_csv = ws / "Meteo" / "tonga_15min.csv"
            _write_subhourly_weather_csv(tonga_csv, year=2023, step_minutes=15, unit="W/m2")
            tonga_csv.write_text(tonga_csv.read_text(encoding="utf-8")
                .replace("#Site;TestSite", "#Site;Nukualofa")
                .replace("#Country;Switzerland", "#Country;Tonga")
                .replace("#Latitude;46.24", "#Latitude;-21.14")
                .replace("#Longitude;6.11", "#Longitude;-175.20")
                .replace("#Time Zone;1", "#Time Zone;13"),
                encoding="utf-8",)
            fake_cli_exe = Path(tmp) / "PVsystCLI.exe"
            fake_cli_exe.write_bytes(b"stub")
            cli = PVsystCLI(fake_cli_exe, ws)
            before_files = set(ws.rglob("*"))
            for dry_run in (True, False):
                with self.subTest(dry_run=dry_run):
                    res = prepare_weather(cli, csv_name="tonga_15min.csv", year=2023, sit_name="Tonga.SIT", met_name="Tonga.MET", country_code="TO",
                        time_basis="local_standard_interval_start", step_minutes=15, dry_run=dry_run,)
                    self.assertEqual(res["status"], "blocked", res)
                    self.assertTrue(any("Timezone outside [-12, 12]" in msg for msg in res["issues"]), res["issues"])
                    self.assertEqual(set(ws.rglob("*")), before_files)
def _fake_pvgis_payload(year_label: int = 2015) -> tuple[dict, str]:
    start = datetime(year_label, 1, 1, 0, 0)
    rows = []
    for h in range(8760):
        dt = start + timedelta(hours=h)
        ghi = 600.0 if 6 <= dt.hour <= 16 else 0.0
        dhi = 180.0 if ghi > 0 else 0.0
        rows.append({"time(UTC)": dt.strftime("%Y%m%d:%H%M"), "T2m": 18.5, "G(h)": ghi, "Gd(h)": dhi})
    payload = {"inputs": {"location": {"latitude": 31.32, "longitude": 120.72, "elevation": 12.0}, "meteo_data": {"radiation_db": "PVGIS-ERA5"}},
        "outputs": {"tmy_hourly": rows},}
    return payload, json.dumps(payload)
def _fake_nasa_payload(year: int = 2023, offset_hours: int = 8, sentinel_at: str | None = None) -> tuple[dict, str]:
    utc_start = datetime(year, 1, 1, 0, 0) - timedelta(hours=offset_hours)
    ghi_map, dhi_map, t2m_map = {}, {}, {}
    for step in range(8760):
        utc_dt = utc_start + timedelta(hours=step)
        key = utc_dt.strftime("%Y%m%d%H")
        local_h = (utc_dt.hour + offset_hours) % 24
        ghi = 550.0 if 8 <= local_h <= 16 else 0.0
        dhi = 150.0 if ghi > 0 else 0.0
        if key == sentinel_at: ghi = -999.0
        ghi_map[key], dhi_map[key], t2m_map[key] = ghi, dhi, 16.0
    payload = {"geometry": {"coordinates": [120.72, 31.32, 10.0]}, "properties": {"parameter": {"ALLSKY_SFC_SW_DWN": ghi_map, "ALLSKY_SFC_SW_DIFF": dhi_map, "T2M": t2m_map}},}
    return payload, json.dumps(payload)
class MultiProviderWeatherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Meteo").mkdir()
        (self.root / "Results").mkdir()
    def _dl(self, csv_name, year, provider, *, ws=None, lat=31.32, lon=120.72, site="Suzhou", **kw):
        return acquisition.download_weather(ws or self.root, csv_name=csv_name, year=year, latitude=lat, longitude=lon,
            site_name=site, country_code="CN", timezone_name="Asia/Shanghai", provider=provider, **kw,)
    def test_pvgis_tmy_plan_execution_and_csv_validation(self):
        bad = self._dl("suzhou_tmy.csv", 2024, "pvgis_tmy", dry_run=True)
        self.assertEqual(bad["status"], "blocked")
        self.assertTrue(any("non-leap" in msg for msg in bad["issues"]))
        plan = self._dl("suzhou_tmy.csv", 2021, "pvgis_tmy", dry_run=True)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["standard_utc_offset_hours"], 8)
        self.assertIn("re.jrc.ec.europa.eu", plan["plan"]["request_url"])
        payload, raw_text = _fake_pvgis_payload()
        with patch.object(acquisition, "_provider_get", return_value=(payload, raw_text, plan["plan"]["request_url"])):
            done = self._dl("suzhou_tmy.csv", 2021, "pvgis_tmy", dry_run=False)
        self.assertEqual(done["status"], "prepared", done["issues"])
        self.assertEqual(done["created_files"]["csv"]["rows"], 8760)
        report = inspect_weather_csv(self.root / "Meteo" / "suzhou_tmy.csv", expected_year=2021)
        self.assertTrue(report["valid"], report.get("issues"))
        self.assertEqual(report["rows"], 8760)
    def test_nasa_power_execution_and_sentinel_rejection(self):
        payload, raw_text = _fake_nasa_payload(2023, offset_hours=8)
        with patch.object(acquisition, "_provider_get", return_value=(payload, raw_text, "https://power.larc.nasa.gov/api/temporal/hourly/point?ok=1")):
            done = self._dl("suzhou_nasa_2023.csv", 2023, "nasa_power", dry_run=False)
        self.assertEqual(done["status"], "prepared", done["issues"])
        report = inspect_weather_csv(self.root / "Meteo" / "suzhou_nasa_2023.csv", expected_year=2023)
        self.assertTrue(report["valid"], report.get("issues"))
        bad_payload, bad_raw = _fake_nasa_payload(2023, offset_hours=8, sentinel_at="2023061504")
        with patch.object(acquisition, "_provider_get", return_value=(bad_payload, bad_raw, "https://power.larc.nasa.gov/api/temporal/hourly/point?bad=1")):
            blocked = self._dl("suzhou_nasa_bad.csv", 2023, "nasa_power", dry_run=False)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(any("sentinel" in msg for msg in blocked["issues"]))
    def test_execution_records_blocked_manifest_on_weather_output_collision(self):
        for provider, raw_suffix in (("open_meteo_era5", "openmeteo"), ("pvgis_tmy", "pvgis_tmy"), ("nasa_power", "nasapower")):
            for dry_run in (False, True):
                for existing_name in ("collision.csv", f"collision_{raw_suffix}_raw.json"):
                    with self.subTest(provider=provider, dry_run=dry_run, existing=existing_name):
                        workspace = self.root / provider / str(dry_run) / existing_name
                        (workspace / "Meteo").mkdir(parents=True)
                        (workspace / "Results").mkdir()
                        existing = workspace / "Meteo" / existing_name
                        existing.write_bytes(b"existing output\n")
                        with patch.object(acquisition, "_provider_get") as get:
                            result = self._dl("collision.csv", 2023, provider, ws=workspace, altitude=12.0, dry_run=dry_run)
                        get.assert_not_called()
                        self.assertEqual((result["status"], result["stage"]), ("blocked", "plan"))
                        self.assertIn("refusing to overwrite", result["issues"][0])
                        self.assertEqual(existing.read_bytes(), b"existing output\n")
                        self.assertEqual(list((workspace / "Meteo").iterdir()), [existing])
                        manifest = workspace / "Results" / "collision_acquisition.json"
                        if dry_run:
                            self.assertFalse(manifest.exists())
                            self.assertEqual(result["created_files"], {})
                        else:
                            self.assertEqual(result["created_files"], {"manifest": {"name": manifest.name}})
                            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), result)
    def test_mcp_acquire_weather_one_step_prepare_chain(self):
        fake_cli = self.root / "PVsystCLI.exe"
        fake_cli.write_bytes(b"stub")
        payload, raw_text = _fake_pvgis_payload()
        args = dict(year=2021, latitude=31.32, longitude=120.72, site_name="Suzhou", country_code="CN", timezone_name="Asia/Shanghai", provider="pvgis_tmy", prepare=True)
        with patch.dict(os.environ, {"PVSYST_WORKSPACE": str(self.root), "PVSYST_CLI": str(fake_cli)}):
            plan = api_weather.pvsyst_acquire_weather(csv_name="chain_tmy.csv", dry_run=True, **args)
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["preparation_plan"]["sit_name"], "chain_tmy.SIT")
            self.assertEqual(plan["preparation_plan"]["met_name"], "chain_tmy.MET")
            with patch.object(acquisition, "_provider_get", return_value=(payload, raw_text, "https://re.jrc.ec.europa.eu/api/v5_3/tmy?ok=1")), \
                    patch.object(api_weather, "prepare_weather", return_value={"status": "prepared", "sit": "chain_tmy.SIT", "met": "chain_tmy.MET"}) as prep_mock:
                done = api_weather.pvsyst_acquire_weather(csv_name="chain_tmy.csv", dry_run=False, **args)
            self.assertEqual(done["status"], "prepared")
            self.assertEqual(done["preparation"]["status"], "prepared")
            prep_mock.assert_called_once()
            with patch.object(acquisition, "_provider_get", return_value=(payload, raw_text, "https://re.jrc.ec.europa.eu/api/v5_3/tmy?ok=1")), \
                    patch.object(api_weather, "prepare_weather", return_value={"status": "blocked", "issues": ["SIT exists"]}):
                failed = api_weather.pvsyst_acquire_weather(csv_name="chain_tmy2.csv", dry_run=False, **args)
            self.assertEqual((failed["status"], failed["stage"]), ("blocked", "preparation"))
            self.assertEqual(failed["preparation"]["issues"], ["SIT exists"])
    def test_nasa_power_blocks_future_year_in_preflight_with_zero_requests(self):
        future_year = datetime.now(timezone.utc).year + 1
        with patch.object(acquisition, "_provider_get") as mock_get:
            for dry_run in (True, False):
                with self.subTest(dry_run=dry_run):
                    res = self._dl("future.csv", future_year, "nasa_power", dry_run=dry_run)
                    self.assertEqual((res["status"], res["stage"]), ("blocked", "preflight"), res)
                    self.assertTrue(res["issues"])
        mock_get.assert_not_called()
    def test_region_error_and_schema_expose_allowed_values(self):
        fake_exe = self.root / "PVsystCLI.exe"
        fake_exe.write_bytes(b"fake")
        cli = PVsystCLI(cli_path=fake_exe, workspace=self.root)
        with patch.object(cli, "_require", return_value=None):
            with self.assertRaisesRegex(ValueError, "Unknown region.*asia.*europe"):
                cli.create_site_details("S", 30.0, 120.0, self.root / "Meteo" / "m.csv", self.root / "s.sit", region="Asia")
        ann = pvsyst_create_site.__annotations__["region"]
        self.assertIn("asia", str(ann))
        self.assertIn("north_america", str(ann))
    def test_nasa_power_twilight_dhi_tolerance_and_strict_opt_out(self):
        year, offset_hours = 2022, 8
        utc_start = datetime(year, 1, 1, 0, 0) - timedelta(hours=offset_hours)
        ghi_map, dhi_map, t2m_map, twilight_key = {}, {}, {}, None
        for step in range(8760):
            utc_dt = utc_start + timedelta(hours=step)
            key = utc_dt.strftime("%Y%m%d%H")
            local_h = (utc_dt.hour + offset_hours) % 24
            ghi = 500.0 if 8 <= local_h <= 16 else 0.0
            dhi = 150.0 if ghi > 0 else 0.0
            if step == 100: ghi, dhi, twilight_key = 14.48, 17.26, key
            ghi_map[key], dhi_map[key], t2m_map[key] = ghi, dhi, 15.0
        payload = {"geometry": {"coordinates": [110.0, 35.0, 400.0]}, "properties": {"parameter": {"ALLSKY_SFC_SW_DWN": ghi_map, "ALLSKY_SFC_SW_DIFF": dhi_map, "T2M": t2m_map}}}
        raw = json.dumps(payload)
        with patch.object(acquisition, "_provider_get", return_value=(payload, raw, "https://power.larc.nasa.gov/mock")):
            ok = self._dl("nasa_twilight_ok.csv", year, "nasa_power", lat=35.0, lon=110.0, site="TwSite", dry_run=False)
            self.assertEqual(ok["status"], "prepared", ok["issues"])
            self.assertTrue(any("twilight" in w.lower() and twilight_key in w for w in ok["warnings"]))
            strict = self._dl("nasa_twilight_strict.csv", year, "nasa_power", lat=35.0, lon=110.0, site="TwSite", strict_dhi=True, dry_run=False)
            self.assertEqual(strict["status"], "blocked")
            self.assertTrue(any("DHI (17.26) > GHI (14.48)" in i for i in strict["issues"]))
    def test_pvgis_and_nasa_power_reject_sub_absolute_zero_temperature(self):
        """Issue #275: sub-absolute-zero temperatures from PVGIS / NASA POWER fail closed."""
        pvgis_payload, raw_pvgis = _fake_pvgis_payload()
        pvgis_payload["outputs"]["tmy_hourly"][0]["T2m"] = -300.0
        with patch.object(acquisition, "_provider_get", return_value=(pvgis_payload, raw_pvgis, "https://re.jrc.ec.europa.eu/api/v5_3/tmy?bad=1")):
            res_pvgis = self._dl("pvgis_abszero.csv", 2021, "pvgis_tmy", dry_run=False)
        self.assertEqual(res_pvgis["status"], "blocked")
        self.assertIn("absolute zero", " ".join(res_pvgis["issues"]).lower())
        nasa_payload, raw_nasa = _fake_nasa_payload()
        nasa_keys = sorted(nasa_payload["properties"]["parameter"]["T2M"])
        nasa_year = int(nasa_keys[24][:4])
        nasa_payload["properties"]["parameter"]["T2M"][nasa_keys[24]] = -300.0
        with patch.object(acquisition, "_provider_get", return_value=(nasa_payload, raw_nasa, "https://power.larc.nasa.gov/api/temporal/hourly/point?bad=1")):
            res_nasa = self._dl("nasa_abszero.csv", nasa_year, "nasa_power", dry_run=False)
        self.assertEqual(res_nasa["status"], "blocked")
        self.assertIn("absolute zero", " ".join(res_nasa["issues"]).lower())
if __name__ == "__main__": unittest.main()

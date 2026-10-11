"""Public weather-format validation with known hourly energy/coverage oracles."""
import calendar
import csv
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from mcp.server.mcpserver.exceptions import ToolError

from pvsyst_cli import PVsystCLI
from pvsyst_weather import inspect_weather_csv
import pvsyst_mcp_server as server


def weather_text(year=2023, separator=";", skip=None, diffuse=True, unit="W/m2"):
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=separator, lineterminator="\n")
    writer.writerows([["#Weather data hourly data"], ["#Site", "Test site"], ["#Country", "Test"],
                      ["#Data Source", "Synthetic fixture"], ["#Time step", "Hour"],
                      ["#Latitude", 30], ["#Longitude", 110], ["#Altitude", 100], ["#Time Zone", 8]])
    headers = ["Year", "Month", "Day", "Hour", "GHI", "Tamb"] + (["DHI"] if diffuse else [])
    units = ["", "", "", "", unit, "deg.C"] + ([unit] if diffuse else [])
    writer.writerow(headers)
    writer.writerow(units)
    start = datetime(year, 1, 1)
    for hour in range(8784 if calendar.isleap(year) else 8760):
        if hour == skip:
            continue
        stamp = start + timedelta(hours=hour)
        power = 100 if 6 <= stamp.hour < 18 else 0
        factor = 1.0 if unit == "W/m2" else 0.0036
        writer.writerow([stamp.year, stamp.month, stamp.day, stamp.hour, power * factor, 20] +
                        ([power / 2 * factor] if diffuse else []))
    return stream.getvalue()


class WeatherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "Meteo").mkdir()
        self.path = self.root / "Meteo" / "weather.csv"

    def inspect(self, value, expected_year=None):
        self.path.write_text(value, encoding="utf-8-sig", newline="")
        before = self.path.read_bytes()
        report = inspect_weather_csv(self.path, expected_year)
        self.assertEqual(before, self.path.read_bytes())
        json.dumps(report, allow_nan=False)
        return report

    def test_normal_and_leap_year_energy_and_hash(self):
        for year, delimiter, expected in ((2023, ";", 8760), (2024, ",", 8784)):
            report = self.inspect(weather_text(year, delimiter))
            self.assertTrue(report["valid"])
            self.assertTrue(report["raw_hourly_coverage_complete"])
            self.assertEqual(report["rows"], expected)
            self.assertEqual(len(report["sha256"]), 64)
            self.assertAlmostEqual(report["months"][0]["GHI"]["observed_kwh_m2"], 31 * 12 * 0.1)
            self.assertEqual(report["months"][1]["Tamb"]["finite_samples"], calendar.monthrange(year, 2)[1] * 24)
            self.assertEqual(report["months"][0]["Tamb"]["observed_mean_degC"], 20)

    def test_mj_energy_conversion_matches_hourly_watts(self):
        a = self.inspect(weather_text())
        b = self.inspect(weather_text(unit="MJ/m2"))
        self.assertTrue(b["valid"])
        self.assertEqual(a["months"], b["months"])

    def test_missing_night_or_leap_day_is_not_complete(self):
        for year, skip in ((2023, 0), (2024, 24 * 59)):
            r = self.inspect(weather_text(year=year, skip=skip))
            self.assertFalse(r["valid"])
            self.assertEqual(r["missing_hours"], 1)
            self.assertEqual(len(r["missing_examples"]), 1)

    def test_duplicate_reversed_and_wrong_year_are_diagnosed(self):
        source = weather_text()
        lines = source.splitlines()
        r = self.inspect(source + lines[-1] + "\n")
        self.assertEqual(r["duplicate_timestamps"], 1)
        self.assertFalse(r["valid"])
        r = self.inspect("\n".join(lines[:-2] + lines[-2:][::-1]) + "\n")
        self.assertEqual(r["nonincreasing_timestamps"], 1)
        self.assertFalse(r["raw_hourly_coverage_complete"])
        r = self.inspect(source, 2024)
        self.assertEqual((r["missing_hours"], r["unexpected_timestamps"]), (8784, 8760))

    def test_missing_nonnumeric_and_physical_errors_are_not_zeros(self):
        source = weather_text()
        for value in ("", "NaN", "Infinity", "1e999", "-1", "true"):
            with self.subTest(value=value):
                r = self.inspect(source.replace("2023;1;1;6;100.0", "2023;1;1;6;" + value))
                self.assertFalse(r["valid"])
                self.assertIn("GHI", " ".join(r["issues"]))
                self.assertEqual(r["months"][0]["GHI"]["finite_samples"], 743)
        r = self.inspect(source.replace("100.0;20;50.0", "100.0;20;101.0", 1))
        self.assertIn("DHI exceeds GHI", " ".join(r["issues"]))

    def test_metadata_missing_duplicates_ranges_and_identifier(self):
        source = weather_text()
        for text in (source.replace("#Site;Test site\n", ""),
                     source.replace("#Latitude;30", "#Latitude;91"),
                     source.replace("#Time Zone;8", "#Time Zone;8\n#Time Zone;0"),
                     source.replace("#Site;", "#Year;0\n#Site;"),
                     source.replace("#Weather data hourly data", "#unknown"),
                     "\n" + source):
            self.assertFalse(self.inspect(text)["valid"])

    def test_metadata_year_and_reordered_columns_with_optional_weather(self):
        original = list(csv.reader(io.StringIO(weather_text()), delimiter=";"))
        output = io.StringIO()
        writer = csv.writer(output, delimiter=",", lineterminator="\n")
        header_index = next(i for i, row in enumerate(original) if row[0] == "Year")
        writer.writerows(original[:header_index])
        writer.writerow(["#Year", "2023"])
        writer.writerow(["Hour", "Day", "Month", "Tamb", "GHI", "DHI", "WindVel", "RH"])
        writer.writerow(["", "", "", "deg.C", "W/m2", "W/m2", "m/s", "%"])
        writer.writerows([[row[3], row[2], row[1], row[5], row[4], row[6], 2, 50]
                          for row in original[header_index + 2:]])
        r = self.inspect(output.getvalue())
        self.assertTrue(r["valid"])
        self.assertEqual(r["expected_year"], 2023)
        r = self.inspect(output.getvalue().replace(",2,50\n", ",2,101\n"))
        self.assertFalse(r["valid"])
        self.assertEqual(r["issue_count"], 8760)
        self.assertEqual(len(r["issues"]), 50)
        self.assertTrue(r["issues_truncated"])

    def test_unknown_units_do_not_produce_mislabeled_temperature_or_energy(self):
        source = weather_text()
        r = self.inspect(source.replace("W/m2;deg.C;W/m2", "kWh/m2;degF;W/m2"))
        self.assertFalse(r["valid"])
        self.assertIsNone(r["months"][0]["GHI"]["observed_kwh_m2"])
        self.assertIsNone(r["months"][0]["Tamb"]["observed_mean_degC"])
        r = self.inspect(source.replace("#Time step;Hour", "#Time step;Day"))
        self.assertFalse(r["valid"])
        self.assertIsNone(r["months"][0]["GHI"]["observed_kwh_m2"])

    def test_explicit_time_shift_and_reference_are_unchecked(self):
        source = weather_text()
        for tag in ("#Hour Shift;1", "#Time Shift;30", "#Time reference;UT"):
            r = self.inspect(source.replace("#Site;", tag + "\n#Site;"))
            self.assertIsNone(r["valid"])
            self.assertTrue(r["raw_hourly_coverage_complete"])
            self.assertTrue(r["unchecked_checks"])

    def test_off_hour_labels_and_invalid_dates_are_not_normalized(self):
        source = weather_text()
        shifted = source.replace("Year;Month;Day;Hour;", "Year;Month;Day;Hour;Minute;")
        shifted = shifted.replace(";;;;W/m2", ";;;;;W/m2")
        shifted = "\n".join(line.replace(";100.0;20", ";30;100.0;20").replace(";0.0;20", ";30;0.0;20")
                            if line.startswith("2023;") else line for line in shifted.splitlines())
        r = self.inspect(shifted)
        self.assertEqual(r["nonzero_minute_records"], 8760)
        self.assertFalse(r["raw_hourly_coverage_complete"])
        r = self.inspect(source.replace("2023;1;1;0", "2023;2;29;0", 1))
        self.assertEqual(r["invalid_dates"], 1)

    def test_incomplete_poa_and_missing_diffuse_are_explicit(self):
        r = self.inspect(weather_text().replace("GHI", "GPI"))
        self.assertIsNone(r["valid"])
        self.assertIn("POA", " ".join(r["unchecked_checks"]))
        r = self.inspect(weather_text(diffuse=False))
        self.assertTrue(r["valid"])
        self.assertIn("diffuse", " ".join(r["warnings"]))

    def test_change_during_inspection_and_large_input_fail(self):
        import pvsyst_weather
        self.path.write_text(weather_text(), encoding="utf-8")
        raw = self.path.read_bytes()
        with patch.object(pvsyst_weather, "read_bytes", side_effect=[raw, raw + b"\n"]):
            with self.assertRaisesRegex(ValueError, "changed during"):
                inspect_weather_csv(self.path)
        with patch.object(pvsyst_weather, "MAX_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "reader limit"):
                inspect_weather_csv(self.path)

    def test_mcp_paths_and_expected_year(self):
        self.path.write_text(weather_text(), encoding="utf-8")
        with patch.object(server, "_cli", PVsystCLI(sys.executable, self.root)):
            self.assertTrue(server.pvsyst_inspect_weather_csv("weather.csv", 2023)["valid"])
            for name in ("../weather.csv", "weather.MET", "C:\\outside.csv"):
                with self.assertRaisesRegex(ToolError, "filename"):
                    server.pvsyst_inspect_weather_csv(name)
        for year in (True, 0, 9999, "2023", 2023.0):
            with self.assertRaises(ValueError):
                inspect_weather_csv(self.path, year)


if __name__ == "__main__":
    unittest.main()

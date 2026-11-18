"""Result comparison regression cases; no vendor CLI execution."""
import asyncio
from datetime import datetime, timedelta
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.api import app as _registered_app, context as server
import pvsyst_auto.api.results as api_results
from pvsyst_auto.engineering import comparison as eng_comparison
import pvsyst_auto.results as results
from pvsyst_auto.native.cli import PVsystCLI, summarize


class ResultComparisonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.a = self.root / "baseline.csv"
        self.b = self.root / "target.csv"

    def write(self, path, rows, unit="kW", header="E_Grid", separator=";", times=None):
        times = times or [f"{i:02d}:00" for i in range(len(rows))]
        text = f"date;{header}\n;{unit}\n" + "".join(
            f"01/01/90 {stamp};{value}\n" for stamp, value in zip(times, rows))
        path.write_text(text.replace(";", separator), encoding="utf-8-sig")

    def compare(self, columns=("E_Grid",)):
        result = results.compare_results(self.a, self.b, columns)
        json.dumps(result, allow_nan=False)
        return result

    def test_aligned_hourly_delta_direction_and_known_energy(self):
        self.write(self.a, [10, 20, 30])
        self.write(self.b, [15, 18, 37], separator=",")
        result = self.compare()
        s = result["summary"]["E_Grid"]
        self.assertEqual((s["paired_baseline_sum"], s["paired_target_sum"], s["paired_delta_sum"]), (60, 70, 10))
        self.assertEqual((s["min_delta"], s["max_delta"], s["max_abs_delta_at"]), (-2, 7, "1990-01-01 02:00:00"))
        self.assertEqual(s["delta_energy_kwh"], 10)
        self.assertAlmostEqual(s["paired_relative_change_pct"], 100 / 6)

    def test_missing_values_use_common_pairs_not_independent_totals(self):
        self.write(self.a, [10, "", 30, 40, "NaN"])
        self.write(self.b, [15, 100, "inf", 42, "text"])
        s = self.compare()["summary"]["E_Grid"]
        self.assertEqual((s["baseline_samples"], s["target_samples"], s["paired_samples"]), (3, 3, 2))
        self.assertEqual((s["paired_baseline_sum"], s["paired_target_sum"], s["paired_delta_sum"]), (50, 57, 7))
        self.assertEqual(s["observed_delta_energy_kwh"], 7)
        self.assertEqual(s["missing_pairs"], 3)
        self.assertEqual(s["paired_coverage"], 0.4)
        self.assertIsNone(s["delta_energy_kwh"])

    def test_all_missing_and_zero_baseline_have_no_relative_gain(self):
        for a, b in ((["", "inf"], [4, ""]), ([0, 0], [1, 2])):
            self.write(self.a, a)
            self.write(self.b, b)
            s = self.compare()["summary"]["E_Grid"]
            self.assertIsNone(s["paired_relative_change_pct"])
            if not s["paired_samples"]:
                self.assertIsNone(s["paired_delta_sum"])
                self.assertIsNone(s["mean_delta"])
                self.assertIsNone(s["max_abs_delta_at"])

    def test_subhour_watts_and_non_power_units(self):
        for unit, expected in (("W", 0.05), ("kW", 50), ("ratio", None)):
            self.write(self.a, [100, 200, 300], unit=unit, times=["00:00", "00:01", "00:02"])
            self.write(self.b, [1100, 1200, 1300], unit=unit, times=["00:00", "00:01", "00:02"])
            s = self.compare()["summary"]["E_Grid"]
            self.assertEqual(s["delta_energy_kwh"], expected)

    def test_single_and_identical_irregular_axes_compare_without_energy(self):
        for times in (["00:00"], ["00:00", "01:00", "03:00"]):
            self.write(self.a, [1] * len(times), times=times)
            self.write(self.b, [2] * len(times), times=times)
            report = self.compare()
            self.assertIsNone(report["step_minutes"])
            self.assertIsNone(report["summary"]["E_Grid"]["delta_energy_kwh"])

    def test_reject_mismatched_duplicate_or_descending_timestamps(self):
        for ta, tb in ((["00:00", "01:00"], ["00:00", "02:00"]),
                       (["00:00", "00:00"], ["00:00", "00:00"]),
                       (["01:00", "00:00"], ["01:00", "00:00"]),
                       (["00:00"], ["00:00", "01:00"])):
            self.write(self.a, [1] * len(ta), times=ta)
            self.write(self.b, [2] * len(tb), times=tb)
            with self.assertRaises(ValueError):
                self.compare()

    def test_reject_unknown_or_mismatched_units_and_ambiguous_columns(self):
        self.write(self.a, [1, 2])
        for kwargs in ({"unit": "W"}, {"unit": ""}, {"header": "Other"},
                       {"header": "E_Grid;E_Grid", "unit": "kW;kW"}):
            self.write(self.b, [1, 2], **kwargs)
            with self.assertRaises(ValueError):
                self.compare()
        self.write(self.b, [1, 2])
        for columns in ((), ("E_Grid", "E_Grid"), ("date",)):
            with self.assertRaises(ValueError):
                self.compare(columns)

    def test_reject_malformed_timestamps_extra_values_missing_units_and_empty_data(self):
        self.write(self.a, [1, 2])
        for text in ("date;E_Grid\n;kW\n32/01/90 00:00;1\n",
                     "date;E_Grid\n;kW\n01/01/90 00:00;1;2\n",
                     "date;E_Grid\n01/01/90 00:00;1\n01/01/90 01:00;2\n",
                     "date;E_Grid\n;kW\n"):
            self.b.write_text(text)
            with self.assertRaises(ValueError):
                self.compare()

    def test_reordered_columns_compare_by_name(self):
        self.write(self.a, ["1;0.1", "2;0.2"], unit="kW;ratio", header="E_Grid;PR")
        self.write(self.b, ["0.15;3", "0.3;4"], unit="ratio;kW", header="PR;E_Grid")
        s = self.compare(("PR", "E_Grid"))["summary"]
        self.assertEqual(s["E_Grid"]["paired_delta_sum"], 4)
        self.assertAlmostEqual(s["PR"]["mean_delta"], 0.075)

    def test_changed_file_during_read_is_rejected(self):
        self.write(self.a, [1, 2])
        self.write(self.b, [3, 4])
        original = results.iter_result_csv
        def change(path, **kwargs):
            yield from original(path, **kwargs)
            if path == self.b:
                self.b.write_text(self.b.read_text(encoding="utf-8-sig") + "\n")
        with patch.object(results, "iter_result_csv", side_effect=change), self.assertRaisesRegex(ValueError, "changed during"):
            self.compare()

    def test_extreme_finite_values_never_produce_infinite_json(self):
        self.write(self.a, [-1e308, -1e308])
        self.write(self.b, [1e308, 1e308])
        with self.assertRaisesRegex(ValueError, "finite numeric range"):
            self.compare()

    def test_server_cross_folder_paths_and_existing_summary(self):
        (self.root / "Results").mkdir()
        (self.root / "UserHourly").mkdir()
        self.write(self.root / "UserHourly" / "base.csv", [1, 2])
        self.write(self.root / "Results" / "target.csv", [3, 4])
        with patch.object(server, "_cli", PVsystCLI(sys.executable, self.root)):
            report = api_results.pvsyst_read_results("target.csv", "E_Grid", compare_to="base.csv", compare_folder="UserHourly")
            self.assertEqual(report["summary"]["E_Grid"]["paired_delta_sum"], 4)
            self.assertEqual(report["baseline"]["folder"], "UserHourly")
            self.assertEqual(api_results.pvsyst_read_results("target.csv", "E_Grid")["summary"]["E_Grid"]["sum"], 7)
            for kwargs in ({"compare_to": "../base.csv"}, {"compare_to": "base.csv", "compare_folder": "Projects"},
                           {"compare_folder": "UserHourly"}):
                with self.assertRaises((ValueError, server.ToolError)):
                    api_results.pvsyst_read_results("target.csv", "E_Grid", **kwargs)

    def test_compare_rejects_bare_string_columns_and_reports_missing_coverage(self):
        self.write(self.a, ["1;2", "3;4"], unit="kW;kW", header="P;R")
        self.write(self.b, ["1;2", "3;4"], unit="kW;kW", header="P;R")
        with self.assertRaisesRegex(ValueError, "distinct numeric columns"):
            results.compare_results(self.a, self.b, "PR")
        self.assertEqual(results.compare_results(self.a, self.b, ("P", "R"))["columns"], ["P", "R"])

        (self.root / "Results").mkdir(exist_ok=True)
        path = self.root / "Results" / "missing.csv"
        with patch.object(server, "_cli", PVsystCLI(sys.executable, self.root)):
            for missing in ("", "NaN", "inf", "text"):
                path.write_text(f"date;E_Grid\n;kW\n01/01/90 00:00;10\n01/01/90 01:00;{missing}\n01/01/90 02:00;10\n",
                                encoding="utf-8")
                direct = summarize(path)
                report = api_results.pvsyst_read_results(path.name, "E_Grid")
                self.assertEqual(direct, {"column": "E_Grid", "rows": report["rows"],
                                          "step_minutes": report["step_minutes"], **report["summary"]["E_Grid"]})
                self.assertAlmostEqual(direct["coverage"], 2 / 3)
                self.assertIsNone(direct["energy_kwh"])
            for body, complete, step in (("00:00;\n01:00;NaN\n02:00;inf", False, 60),
                                         ("00:00;10\n01:00;10\n03:00;10", True, None),
                                         ("00:00;10", True, None)):
                path.write_text("date;E_Grid\n;kW\n" + "\n".join("01/01/90 " + r for r in body.splitlines()) + "\n",
                                encoding="utf-8")
                direct = summarize(path)
                self.assertEqual((direct["complete"], direct["step_minutes"], direct["energy_kwh"]), (complete, step, None))

    def test_zero_row_iterators_raise_value_error_and_block_comparison(self):
        header_only = (
            "Project;Alpha.PRJ\nSimulation variant;Alpha.VC0\n"
            "Geographical Site;Site.SIT\nWeather data;Weather.MET\n"
            "date;E_Grid;GlobInc\n;kW;W/m2\n"
        )
        self.a.write_text(header_only, encoding="utf-8")
        self.b.write_text(header_only.replace("Alpha", "Beta"), encoding="utf-8")
        with patch.object(results, "iter_result_csv", side_effect=lambda *_a, **_k: (_ for _ in ())), \
                self.assertRaisesRegex(ValueError, "no simulation rows"):
            self.compare()
        with patch.object(eng_comparison, "iter_result_csv", side_effect=lambda *_a, **_k: (_ for _ in ())), \
                self.assertRaisesRegex(ValueError, "no simulation rows"):
            eng_comparison._scan_result(self.a, {"E_Grid": "kW", "GlobInc": "W/m2"}, 2024)

        (self.root / "Results").mkdir(exist_ok=True)
        for name, proj in (("base0.csv", "Alpha"), ("cand0.csv", "Beta")):
            (self.root / "Results" / name).write_text(header_only.replace("Alpha", proj), encoding="utf-8")
        store = type("Store", (), {"lock": threading.RLock(), "workspace": self.root})()
        handoff = {"status": "ready", "review_items": [],
                   "saved": {"project": {"values": {}}, "system": {"SystemType": "Grid"},
                             "sources": {"variant": {"site": {"name": "Site.SIT"}, "meteo": {"name": "Weather.MET"}}}}}
        scenarios = [
            {"label": "baseline", "project": "Alpha.PRJ", "variant": "VC0", "csv_name": "base0.csv", "declared_changes": []},
            {"label": "candidate", "project": "Beta.PRJ", "variant": "VC0", "csv_name": "cand0.csv", "declared_changes": ["system"]},
        ]
        with patch("pvsyst_auto.engineering.handoff.build_project_handoff", return_value=handoff):
            blocked = eng_comparison.compare_engineering_scenarios(
                store, objective="Header-only CSV guard", scenarios=scenarios, output_name="zero.json", dry_run=True)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(any("no simulation rows" in issue for issue in blocked["issues"]), blocked["issues"])

    def test_energy_and_irradiation_units_in_compare_and_annual_summary(self):
        for unit, expected in (("Wh", 3.0), ("kWh", 3000.0), ("Wh/m2", None),
                               ("Wh/m\u00b2", None), ("kWh/m2", None), ("kWh/m\u00b2", None)):
            self.write(self.a, [100, 200, 300], unit=unit, times=["00:00", "00:01", "00:02"])
            self.write(self.b, [1100, 1200, 1300], unit=unit, times=["00:00", "00:01", "00:02"])
            summary = self.compare()["summary"]["E_Grid"]
            self.assertEqual(summary["delta_energy_kwh"], expected, unit)

        nominal = 10.0
        start = datetime(1990, 1, 1)
        annual_path = self.root / "annual_units.csv"
        for e_unit, e_factor, g_unit, g_factor in (
            ("Wh", 1000.0, "Wh/m2", 1.0),
            ("Wh", 1000.0, "Wh/m\u00b2", 1.0),
            ("kWh", 1.0, "kWh/m2", 0.001),
            ("kWh", 1.0, "kWh/m\u00b2", 0.001),
        ):
            lines = ["date;E_Grid;EArray;IL_Pmax;GlobInc;PR\n",
                     f";{e_unit};{e_unit};{e_unit};{g_unit};%\n"]
            for h in range(8760):
                stamp = (start + timedelta(hours=h)).strftime("%d/%m/%y %H:%M")
                if h % 2:
                    g = 500.0 * g_factor
                    e_grid = nominal * 0.5 * 0.8 * e_factor
                    lines.append(f"{stamp};{e_grid};{e_grid * 1.05};{0.1 * e_factor};{g};80\n")
                else:
                    lines.append(f"{stamp};0;0;0;0;\n")
            annual_path.write_text("".join(lines), encoding="utf-8")
            summary = results.annual_summary(annual_path, 1990, nominal)
            self.assertAlmostEqual(summary["incident_irradiation_kwh_m2"], 4380 * 0.5)
            self.assertAlmostEqual(summary["grid_energy_kwh"], 4380 * nominal * 0.5 * 0.8)
            self.assertAlmostEqual(summary["pr_ratio"], 0.8)

    def test_annual_summary_dark_hour_tare_and_pr_mismatch_detection(self):
        nominal = 10.0
        start = datetime(1990, 1, 1)
        annual_path = self.root / "annual_tare.csv"

        def write_annual(dark_e_grid=-0.1, day_pr=0.8):
            lines = ["date;E_Grid;EArray;IL_Pmax;GlobInc;PR\n", ";kW;kW;kW;W/m2;ratio\n"]
            for h in range(8760):
                stamp = (start + timedelta(hours=h)).strftime("%d/%m/%y %H:%M")
                if h % 2:
                    e_grid = nominal * 0.5 * 0.8
                    lines.append(f"{stamp};{e_grid};{e_grid + 0.5};0;500;{day_pr}\n")
                else:
                    lines.append(f"{stamp};{dark_e_grid};0;0;0;\n")
            annual_path.write_text("".join(lines), encoding="utf-8")

        write_annual(dark_e_grid=-0.1, day_pr=0.8)
        summary = results.annual_summary(annual_path, 1990, nominal)
        expected_grid = 4380 * (nominal * 0.5 * 0.8) + 4380 * (-0.1)
        expected_ref = 4380 * 0.5 * nominal
        self.assertAlmostEqual(summary["grid_energy_kwh"], expected_grid)
        self.assertAlmostEqual(summary["native_pr_irradiance_weighted_ratio"], 0.8)
        self.assertAlmostEqual(summary["pr_ratio"], expected_grid / expected_ref)
        self.assertLess(summary["pr_ratio"], 0.785)

        with self.assertRaisesRegex(ValueError, "Native PR disagrees"):
            results.annual_summary(annual_path, 1990, nominal * 2)
        write_annual(dark_e_grid=-0.1, day_pr=0.85)
        with self.assertRaisesRegex(ValueError, "Native PR disagrees"):
            results.annual_summary(annual_path, 1990, nominal)
        write_annual(dark_e_grid=0.2, day_pr=0.8)
        with self.assertRaisesRegex(ValueError, "Native PR disagrees"):
            results.annual_summary(annual_path, 1990, nominal)

    def test_read_results_column_bounds_and_permission_error_prefix(self):
        (self.root / "Results").mkdir(exist_ok=True)
        self.write(self.root / "Results" / "base.csv", [1, 2])
        self.write(self.root / "Results" / "target.csv", [3, 4])
        with patch.object(server, "_cli", PVsystCLI(sys.executable, self.root)):
            for bad_columns in ("", " , ", "E_Grid,E_Grid", ",".join(f"C{i}" for i in range(65))):
                with self.subTest(columns=bad_columns), self.assertRaisesRegex(ValueError, "1..64 distinct"):
                    api_results.pvsyst_read_results("target.csv", bad_columns)
            with patch.object(api_results, "compare_results",
                              side_effect=PermissionError(13, "Permission denied", "base.csv")), \
                    self.assertRaises(server.ToolError) as raised:
                asyncio.run(server.mcp.call_tool("pvsyst_read_results", {
                    "csv_name": "target.csv", "columns": "E_Grid", "compare_to": "base.csv"}))
            self.assertIn("PermissionError:", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

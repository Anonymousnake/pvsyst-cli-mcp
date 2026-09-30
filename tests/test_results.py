"""Result comparison regression cases; no vendor CLI execution."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pvsyst_mcp_server as server
import pvsyst_results as results
from pvsyst_cli import PVsystCLI


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
        self.assertEqual(result["baseline"]["sha256"], results.fingerprint(self.a))

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
            report = server.pvsyst_read_results("target.csv", "E_Grid", compare_to="base.csv", compare_folder="UserHourly")
            self.assertEqual(report["summary"]["E_Grid"]["paired_delta_sum"], 4)
            self.assertEqual(report["baseline"]["folder"], "UserHourly")
            self.assertEqual(server.pvsyst_read_results("target.csv", "E_Grid")["summary"]["E_Grid"]["sum"], 7)
            for kwargs in ({"compare_to": "../base.csv"}, {"compare_to": "base.csv", "compare_folder": "Projects"},
                           {"compare_folder": "UserHourly"}):
                with self.assertRaises((ValueError, server.ToolError)):
                    server.pvsyst_read_results("target.csv", "E_Grid", **kwargs)

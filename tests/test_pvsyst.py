"""Offline tests: no PVsyst executable is started and no quota is consumed."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_cli import PVsystCLI, PVsystError, build_sfi, parse_result_csv, summarize
import pvsyst_mcp_server as server

CSV_TEXT = """PVSYST 8.0.6
;File;File date;Description
Simulation:;Hourly values

date;E_Grid;PR
    ;kW;ratio

01/01/90 00:00;0;0
01/01/90 01:00;4.5;0.8
"""


class PVsystTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "workspace"
        for folder in ("Projects", "Models", "Results", "Meteo", "Sites"):
            (self.workspace / folder).mkdir(parents=True)
        (self.workspace / "Projects" / "Example.PRJ").write_text("example", encoding="utf-8")
        (self.workspace / "Projects" / "Example.VC0").write_text("variant", encoding="utf-8")
        self.sfi = build_sfi(self.workspace / "Models" / "demo.sfi", "energy")
        self.client = PVsystCLI(sys.executable, self.workspace)

    def tearDown(self):
        server._cli = None

    def test_build_sfi_validation_and_no_overwrite(self):
        self.assertIn("VarIdentWr=viGlobInc,viGlobEff,viEArray,viE_Grid,viPR,",
                      self.sfi.read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            build_sfi(self.sfi, "energy")
        with self.assertRaises(ValueError):
            build_sfi(self.workspace / "Models" / "invalid.sfi", ["viE_Grid\nBad=1"])
        with self.assertRaises(ValueError):
            build_sfi(self.workspace / "Models" / "bad.sfi", "energy", "line1\nline2")

    def test_project_and_variant_names(self):
        self.assertEqual(self.client.list_projects(), ["Example.PRJ"])
        self.assertEqual(self.client.list_variants("Example.PRJ"), ["VC0"])
        with self.assertRaises(ValueError):
            self.client.list_variants("../Example.PRJ")

    def test_license_info_redacts_host(self):
        output = ("Status: TRIAL\nRemaining days: 60\nRemaining executions: 248\n"
                  "Host ID: HARDDISK=fixture-secret\n")
        with patch.object(self.client, "_run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            info = self.client.license_info()
        self.assertEqual(info, {"status": "TRIAL", "remaining_days": 60,
                                "remaining_executions": 248})
        self.assertNotIn("fixture-secret", str(info))

    def test_simulation_requires_actual_csv_not_just_success_message(self):
        target = self.workspace / "Results" / "new.csv"
        with patch.object(self.client, "_run", return_value=subprocess.CompletedProcess(
                [], 0, 'File "new.csv" created with success.', "")):
            with self.assertRaises(PVsystError):
                self.client.run_simulation("Example.PRJ", "VC0", sfi=self.sfi, out_csv=target)

    def test_simulation_args_and_existing_output_protection(self):
        target = self.workspace / "Results" / "new.csv"

        def run(*args):
            self.assertIn("-p:Example.PRJ", args)
            self.assertIn("-v:VC0", args)
            self.assertIn(f"-isf:{self.sfi}", args)
            self.assertIn(f"-ocf:{target}", args)
            self.assertFalse(any(arg.startswith(('-bpf:', '-ts:')) for arg in args))
            target.write_text(CSV_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 8sec", "")

        with patch.object(self.client, "_run", side_effect=run) as command:
            result = self.client.run_simulation("Example.PRJ", "VC0", sfi=self.sfi, out_csv=target)
            self.assertEqual(result["seconds"], 8)
            with self.assertRaises(ValueError):
                self.client.run_simulation("Example.PRJ", "VC0", sfi=self.sfi, out_csv=target)
            command.assert_called_once()

    def test_weather_conversion_flags_and_output(self):
        inputs = [self.workspace / "Meteo" / "sample.csv",
                  self.workspace / "Meteo" / "format.mef",
                  self.workspace / "Sites" / "place.sit"]
        for item in inputs:
            item.write_text("fixture", encoding="utf-8")
        target = self.workspace / "Meteo" / "result.met"

        def run(*args):
            self.assertEqual(args[0], "convert-meteo")
            self.assertIn("-imt:0", args)
            target.write_text("MET", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Conversion ended successfully...", "")

        with patch.object(self.client, "_run", side_effect=run):
            self.assertEqual(self.client.convert_meteo(*inputs, target), target)
        with self.assertRaises(ValueError):
            self.client.convert_meteo(*inputs, target, timeshift=31)

    def test_result_parser(self):
        target = self.workspace / "Results" / "result.csv"
        target.write_text(CSV_TEXT, encoding="utf-8")
        headers, rows = parse_result_csv(target)
        self.assertEqual(headers, ["date", "E_Grid", "PR"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(summarize(target)["sum"], 4.5)
        with self.assertRaises(ValueError):
            summarize(target, "not_a_column")
        target.write_text("date\n01/01/90 00:00\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            parse_result_csv(target)

    def test_mcp_tools_and_path_confinement(self):
        server._cli = self.client
        tools = asyncio.run(server.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(len(names), 7)
        self.assertNotIn("pvsyst_raw", names)
        self.assertEqual(server.pvsyst_list_projects(), ["Example.PRJ"])
        with self.assertRaises(ValueError):
            server.workspace_path("Results", "..\\secret.csv", ".csv")
        with self.assertRaises(ValueError):
            server.workspace_path("Results", "../secret.csv", ".csv")
        with self.assertRaises(ValueError):
            server.workspace_path("Results", "C:\\secret.csv", ".csv")


if __name__ == "__main__":
    unittest.main()

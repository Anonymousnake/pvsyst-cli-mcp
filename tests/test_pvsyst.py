"""Offline tests: no PVsyst executable is started and no quota is consumed."""
from __future__ import annotations

import asyncio
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_cli import (PVsystCLI, PVsystError, build_monthly_weather_csv,
                        build_sfi, parse_result_csv, summarize)
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
        self.client._help_cache["run-simulation"] = (
            "--input-sfi-file|-isf: --input-rvt-file|-irf: "
            "--input-met-file|-imf: --input-param-file|-ipf: "
            "--start-date|-sd: --end-date|-ed: --report-pdf-file|-rpf:"
        ).replace(" --", "\n --")

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
        target.write_text(CSV_TEXT + "01/01/90 02:00;;NaN\n", encoding="utf-8")
        _, rows_with_missing = parse_result_csv(target)
        self.assertEqual(len(rows_with_missing), 3)
        self.assertTrue(math.isnan(rows_with_missing[-1][1]))
        self.assertEqual(summarize(target)["samples"], 2)
        target.write_text("date\n01/01/90 00:00\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            parse_result_csv(target)

    def test_mcp_tools_and_path_confinement(self):
        server._cli = self.client
        tools = asyncio.run(server.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(len(names), 15)
        self.assertIn("pvsyst_create_site", names)
        self.assertIn("pvsyst_read_rows", names)
        self.assertNotIn("pvsyst_raw", names)
        self.assertEqual(server.pvsyst_list_projects(), ["Example.PRJ"])
        with self.assertRaises(ValueError):
            server.workspace_path("Results", "..\\secret.csv", ".csv")
        with self.assertRaises(ValueError):
            server.workspace_path("Results", "../secret.csv", ".csv")
        with self.assertRaises(ValueError):
            server.workspace_path("Results", "C:\\secret.csv", ".csv")

    def test_old_cli_rejects_new_options_before_launch(self):
        with patch.object(self.client, "_run") as command:
            with self.assertRaisesRegex(PVsystError, "lacks run-simulation --time-step"):
                self.client.run_simulation("Example.PRJ", "VC0", sfi=self.sfi,
                                           out_csv=self.workspace / "Results" / "new.csv",
                                           time_step="subhour")
            command.assert_not_called()
            self.assertFalse((self.workspace / "Results" / "new.csv").exists())

    def test_invalid_date_rejected_before_invocation(self):
        with patch.object(self.client, "_run") as command:
            with self.assertRaisesRegex(ValueError, "Invalid calendar date"):
                self.client.run_simulation("Example.PRJ", "VC0", sfi=self.sfi,
                                           out_csv=self.workspace / "Results" / "new.csv",
                                           start_date="1990.13.40")
            command.assert_not_called()

    def test_license_timeout_does_not_expose_secret(self):
        with patch.object(self.client, "_run", side_effect=subprocess.TimeoutExpired(
                ["PVsystCLI", "lic-activate", "-k:private-key"], 5)):
            with self.assertRaises(PVsystError) as raised:
                self.client.license_activate("private-key", confirm=True)
        self.assertNotIn("private-key", str(raised.exception))

    def test_new_cli_optional_simulation_flags(self):
        self.client._help_cache["run-simulation"] += (
            " --time-step|-ts: --site|-s: --synthetic-weatherdata-generation|-swg "
            "--batch-params-file|-bpf: --batch-rvt-file|-brf: "
            "--recompute-shadingfactors-tables|-rst:"
        ).replace(" --", "\n --")
        site = self.workspace / "Sites" / "place.sit"
        site.write_text("site", encoding="utf-8")
        batch = self.workspace / "UserBatch" / "variants.csv"
        batch.parent.mkdir()
        batch.write_text("batch", encoding="utf-8")
        target = self.workspace / "Results" / "new.csv"

        def run(*args):
            self.assertIn(f"-s:{site}", args)
            self.assertIn(f"-bpf:{batch}", args)
            self.assertIn("-swg", args)
            self.assertIn("-ts:hour", args)
            self.assertIn("-rst:0", args)
            self.assertIn("-sd:1990.01.01", args)
            self.assertIn("-ed:1990.01.02", args)
            target.write_text(CSV_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 2sec", "")

        with patch.object(self.client, "_run", side_effect=run):
            response = self.client.run_simulation(
                "Example.PRJ", "VC0", sfi=self.sfi, out_csv=target,
                site=site, synthetic_weather=True, time_step="hour",
                batch_params=batch, recompute_shading=False,
                start_date="1990.01.01", end_date="1990.01.02")
        self.assertEqual(response["seconds"], 2)

    def test_monthly_weather_and_create_site(self):
        self.client._help_cache["create-site"] = "--site-name|-sn:\n --weather-data-file|-wdf:"
        monthly = build_monthly_weather_csv(self.workspace / "Meteo" / "monthly.csv",
                                            [100.0] * 12, [20.0] * 12)
        self.assertEqual(len(monthly.read_text(encoding="utf-8").splitlines()), 14)
        target = self.workspace / "Sites" / "site.sit"

        def run(*args):
            self.assertIn("-lat:19.7", args)
            self.assertIn("-lon:-155.0", args)
            self.assertIn("-tz:-10", args)
            target.write_text("PVObject_=pvSite", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "File created successfully", "")

        with patch.object(self.client, "_run", side_effect=run):
            self.assertEqual(self.client.create_site("Test Site", 19.7, -155.0,
                                                      monthly, target, timezone=-10), target)
        with self.assertRaises(ValueError):
            build_monthly_weather_csv(self.workspace / "Meteo" / "bad.csv",
                                      [100.0] * 11, [20.0] * 12)
        with self.assertRaises(ValueError):
            self.client.create_site("Bad", 100.0, 0, monthly,
                                    self.workspace / "Sites" / "bad.sit")

    def test_license_unknown_is_not_misreported_as_licensed(self):
        result = subprocess.CompletedProcess([], 0, "License file: present\nExpiration date: \n", "")
        with patch.object(self.client, "_run", return_value=result):
            self.assertEqual(self.client.license_info(),
                             {"status": "UNSPECIFIED", "expiration_date": None})

    def test_license_mutations_require_confirmation_and_redact_key(self):
        with patch.object(self.client, "_run") as command:
            with self.assertRaises(ValueError):
                self.client.license_activate("private-key")
            command.assert_not_called()

        def run(*args):
            if args[0] == "lic-info":
                return subprocess.CompletedProcess([], 0, "Status: TRIAL\nRemaining days: 5", "")
            return subprocess.CompletedProcess([], 0, "License key: private-key", "")

        with patch.object(self.client, "_run", side_effect=run):
            info = self.client.license_activate("private-key", confirm=True)
        self.assertNotIn("private-key", str(info))
        self.assertEqual(info["command"], "lic-activate")

    def test_export_logs_and_bounded_rows(self):
        folder = self.workspace / "Results"

        def run(*args):
            self.assertEqual(args[0], "export-logs")
            (folder / "logs.zip").write_bytes(b"PK")
            return subprocess.CompletedProcess([], 0, "exported", "")

        with patch.object(self.client, "_run", side_effect=run):
            self.assertEqual(self.client.export_logs(folder).name, "logs.zip")
        result = folder / "result.csv"
        result.write_text(CSV_TEXT, encoding="utf-8")
        server._cli = self.client
        page = server.pvsyst_read_rows("result.csv", offset=1, limit=1)
        self.assertEqual(page["rows"][0][1:], [4.5, 0.8])
        self.assertEqual(page["total"], 2)
        with self.assertRaises(ValueError):
            server.pvsyst_read_rows("result.csv", limit=501)


if __name__ == "__main__":
    unittest.main()

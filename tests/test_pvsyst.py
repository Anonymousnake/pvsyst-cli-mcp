"""Offline tests: no PVsyst executable is started and no quota is consumed."""
from __future__ import annotations

import asyncio
import math
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_cli import (PVsystCLI, PVsystError, build_monthly_weather_csv,
                        build_sfi, parse_batch_results, parse_result_csv,
                        result_units, summarize)
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
        self.assertEqual(len(names), 45)
        self.assertEqual(names, set(server.TOOL_CATEGORY))
        self.assertEqual({group: len(group_names) for group, group_names in server.TOOL_GROUPS.items()},
                         {"Setup & License": 6, "Projects & Variants": 18, "Components": 13,
                          "Sites & Weather": 3, "Simulation": 2, "Results": 3})
        for tool in tools:
            self.assertTrue(tool.description.startswith(f"[{server.TOOL_CATEGORY[tool.name]}] "))
        self.assertIn("pvsyst_read_batch_results", names)
        self.assertIn("pvsyst_create_site", names)
        self.assertIn("pvsyst_read_rows", names)
        simulation = next(tool for tool in tools if tool.name == "pvsyst_run_simulation")
        self.assertIn("bare filename", simulation.description)
        self.assertNotIn("pvsyst_raw", names)
        self.assertEqual(server.pvsyst_list_projects(), ["Example.PRJ"])
        with self.assertRaises(ValueError):
            server.workspace_path("Results", "..\\secret.csv", ".csv")
        with self.assertRaises(ValueError):
            server.workspace_path("Results", "../secret.csv", ".csv")
        with self.assertRaises(ValueError):
            server.workspace_path("Results", "C:\\secret.csv", ".csv")

    def test_mcp_single_run_bare_csv_name_and_no_overwrite(self):
        server._cli = self.client
        target = self.workspace / "Results" / "ans_btr_curve1.csv"

        def run(*args):
            self.assertIn(f"-ocf:{target}", args)
            target.write_text(CSV_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 2sec", "")

        with patch.object(self.client, "license_info", return_value={"status": "UNSPECIFIED"}):
            with patch.object(self.client, "_run", side_effect=run) as command:
                result = server.pvsyst_run_simulation(
                    "Example.PRJ", "VC0", sfi_name="demo.sfi",
                    csv_name="ans_btr_curve1")
                self.assertEqual(result["csv"], str(target))
                with self.assertRaisesRegex(ValueError, "Output must be a new"):
                    server.pvsyst_run_simulation(
                        "Example.PRJ", "VC0", sfi_name="demo.sfi",
                        csv_name="ans_btr_curve1.csv")
                command.assert_called_once()

    def test_mcp_single_run_csv_name_validation(self):
        server._cli = self.client
        target = self.workspace / "Results"
        self.assertEqual(server.simulation_csv_path("run01"), target / "run01.csv")
        self.assertEqual(server.simulation_csv_path("run01.CSV"), target / "run01.CSV")
        self.assertIsNone(server.simulation_csv_path(""))
        for name in ("run01.pdf", "run01.", ".", "..", "../escape", "..\\escape",
                     "C:\\escape", "/escape", "../escape.csv", "..\\escape.csv"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    server.simulation_csv_path(name)
        self.assertFalse((self.workspace / "escape.csv").exists())

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
        target = self.workspace / "Results" / "new.csv"

        def run(*args):
            self.assertIn(f"-s:{site}", args)
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
                recompute_shading=False,
                start_date="1990.01.01", end_date="1990.01.02")
        self.assertEqual(response["seconds"], 2)

    def test_batch_auto_outputs_and_summary_parsing(self):
        self.client._help_cache["run-simulation"] += "\n --batch-params-file|-bpf:\n --batch-rvt-file|-brf:"
        batch_dir = self.workspace / "UserBatch"
        hourly_dir = self.workspace / "UserHourly"
        batch_dir.mkdir()
        hourly_dir.mkdir()
        batch = batch_dir / "batch_params.CSV"
        batch.write_text("Ident;Create hourly\nSIM_1;one.CSV\nSIM_2;two.CSV\n",
                         encoding="utf-8")
        rvt = batch_dir / "vars.RVT"
        rvt.write_text("ResultVars=E_Grid;PR", encoding="utf-8")
        summary = batch_dir / "batch_Results.CSV"

        def run(*args):
            self.assertIn(f"-bpf:{batch}", args)
            self.assertIn(f"-brf:{rvt}", args)
            self.assertFalse(any(arg.startswith("-ocf:") for arg in args))
            summary.write_bytes(("Ident;Create hourly;Error;E_Grid;PR\n"
                                 "SIM_1;one.CSV;;12;0.7\nSIM_2;two.CSV;;13;0.8\n"
                                 "Note;Caf\xe9\n").encode("cp1252"))
            for name in ("one.CSV", "two.CSV"):
                (hourly_dir / name).write_text(CSV_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 2sec", "")

        with patch.object(self.client, "_run", side_effect=run) as command:
            result = self.client.run_simulation("Example.PRJ", "VC0",
                                                batch_params=batch, batch_rvt=rvt)
            self.assertEqual(result["batch_runs"], 2)
            self.assertEqual(result["batch_summary"], str(summary))
            self.assertEqual(len(result["batch_hourly"]), 2)
            with self.assertRaisesRegex(ValueError, "already exists"):
                self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch,
                                           batch_rvt=rvt)
            command.assert_called_once()
        self.assertEqual(parse_batch_results(summary)["scenarios"][1]["values"]["PR"], 0.8)
        server._cli = self.client
        self.assertEqual(len(server.pvsyst_read_batch_results(summary.name)["scenarios"]), 2)

    def test_batch_custom_result_variables_need_no_grid_or_pr(self):
        self.client._help_cache["run-simulation"] += "\n --batch-params-file|-bpf:"
        batch_dir = self.workspace / "UserBatch"
        hourly_dir = self.workspace / "UserHourly"
        batch_dir.mkdir()
        hourly_dir.mkdir()
        batch = batch_dir / "custom_params.CSV"
        batch.write_text("SIM_1;custom.CSV\n", encoding="utf-8")
        summary = batch_dir / "custom_Results.CSV"

        def run(*args):
            summary.write_text("Ident;Create hourly;Error;GlobInc\n"
                               "SIM_1;custom.CSV;;33\n", encoding="utf-8")
            (hourly_dir / "custom.CSV").write_text(CSV_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 2sec", "")

        with patch.object(self.client, "_run", side_effect=run):
            result = self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
        self.assertEqual(result["batch_runs"], 1)
        self.assertEqual(parse_batch_results(summary, ("GlobInc",))["scenarios"][0]["values"],
                         {"GlobInc": 33.0})

    def test_concurrent_runs_do_not_reuse_output(self):
        target = self.workspace / "Results" / "same.csv"

        def run(*args):
            target.write_text(CSV_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 2sec", "")

        with patch.object(self.client, "_run", side_effect=run) as command:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(self.client.run_simulation, "Example.PRJ", "VC0",
                                           sfi=self.sfi, out_csv=target) for _ in range(2)]
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except ValueError:
                        outcomes.append("existing output")
            self.assertEqual(len([value for value in outcomes if isinstance(value, dict)]), 1)
            self.assertIn("existing output", outcomes)
            command.assert_called_once()

    def test_batch_rejects_output_traversal_before_running(self):
        self.client._help_cache["run-simulation"] += "\n --batch-params-file|-bpf:"
        batch = self.workspace / "Models" / "bad_params.CSV"
        batch.write_text("SIM_1;../outside.csv\n", encoding="utf-8")
        with patch.object(self.client, "_run") as command:
            with self.assertRaisesRegex(ValueError, "unique CSV filename"):
                self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
            batch.write_text("SIM_1;One.CSV\nSIM_2;one.csv\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unique CSV filename"):
                self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
            command.assert_not_called()

    def test_batch_rejects_zero_exit_without_success(self):
        self.client._help_cache["run-simulation"] += "\n --batch-params-file|-bpf:"
        batch = self.workspace / "Models" / "empty_params.CSV"
        batch.write_text("SIM_1;hourly.csv\n", encoding="utf-8")
        with patch.object(self.client, "_run", return_value=subprocess.CompletedProcess(
                [], 0, "Loading workspace failed", "")):
            with self.assertRaises(PVsystError):
                self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)

    def test_comma_batch_hourly_and_minute_energy(self):
        batch_file = self.workspace / "Results" / "batch_hourly.csv"
        batch_file.write_text(CSV_TEXT.replace(";", ",").replace("    ,kW,ratio", "    ,W,ratio"),
                              encoding="utf-8")
        headers, rows = parse_result_csv(batch_file)
        self.assertEqual(len(rows), 2)
        self.assertEqual(headers[1], "E_Grid")
        self.assertEqual(result_units(batch_file)["E_Grid"], "W")
        self.assertAlmostEqual(summarize(batch_file)["energy_kwh"], 0.0045)

        minute_file = self.workspace / "Results" / "minute.csv"
        minute_file.write_text(CSV_TEXT + "01/01/90 01:01;60;0.8\n", encoding="utf-8")
        self.assertIsNone(summarize(minute_file)["energy_kwh"])
        minute_file.write_text(CSV_TEXT.split("01/01/90 00:00")[0] +
                               "01/01/90 00:00;60;0.8\n"
                               "01/01/90 00:01;60;0.8\n"
                               "01/01/90 00:02;60;0.8\n", encoding="utf-8")
        self.assertEqual(summarize(minute_file)["step_minutes"], 1)
        self.assertAlmostEqual(summarize(minute_file)["energy_kwh"], 3)
        server._cli = self.client
        report = server.pvsyst_read_results("minute.csv")
        self.assertEqual(report["step_minutes"], 1)
        self.assertAlmostEqual(report["summary"]["E_Grid"]["energy_kwh"], 3)
        with self.assertRaises(ValueError):
            server.pvsyst_read_results("batch_hourly.csv", folder="../outside")

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

"""Offline tests for PVsystCLI, file I/O transactions, workspace.ini, and retry handling."""
from __future__ import annotations
import asyncio
import contextlib
import os
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import tempfile
import unittest
from threading import Barrier
from time import sleep
from pathlib import Path
from unittest.mock import Mock, patch
from pvsyst_auto.native.cli import PVsystCLI, PVsystError, _is_transient_simulation_abort, build_monthly_weather_csv, build_sfi, iter_result_csv, parse_batch_results, result_units, summarize_results
from pvsyst_auto.native import _winlaunch as ini, load as native_load
from pvsyst_auto.api import context as server
import pvsyst_auto.api.projects as api_projects
import pvsyst_auto.api.simulation as api_results
import pvsyst_auto.api.simulation as api_simulation
import json
from pvsyst_auto.project import paths
import codecs
from contextlib import nullcontext
CSV_TEXT = 'PVSYST 8.0.6\n;File;File date;Description\nSimulation:;Hourly values\n\ndate;E_Grid;PR\n    ;kW;ratio\n\n01/01/90 00:00;0;0\n01/01/90 01:00;4.5;0.8\n'
class PVsystTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Resolve like PVsystCLI does: %TEMP% can be an 8.3 short path (e.g. a user name with a space).
        self.workspace = Path(self.tmp.name).resolve() / "workspace"
        for folder in ("Projects", "Models", "Results", "Meteo", "Sites"): (self.workspace / folder).mkdir(parents=True)
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
        with self.assertRaises(FileExistsError): build_sfi(self.sfi, "energy")
        with self.assertRaises(ValueError): build_sfi(self.workspace / "Models" / "invalid.sfi", ["viE_Grid\nBad=1"])
        with self.assertRaises(ValueError): build_sfi(self.workspace / "Models" / "bad.sfi", "energy", "line1\nline2")
    def test_project_and_variant_names(self):
        self.assertEqual(self.client.list_projects(), ["Example.PRJ"])
        self.assertEqual(self.client.list_variants("Example.PRJ"), ["VC0"])
        with self.assertRaises(ValueError): self.client.list_variants("../Example.PRJ")
    def test_license_status_confirmation_and_key_redaction(self):
        with self.subTest(case="info_redacts_host"):
            output = ("Status: TRIAL\nRemaining days: 60\nRemaining executions: 248\n"
                      "Host ID: HARDDISK=fixture-secret\n")
            with patch.object(self.client, "_run", return_value=subprocess.CompletedProcess([], 0, output, "")): info = self.client.license_info()
            self.assertEqual(info, {"status": "TRIAL", "remaining_days": 60,
                                    "remaining_executions": 248})
            self.assertNotIn("fixture-secret", str(info))
        with self.subTest(case="unknown_status"):
            result = subprocess.CompletedProcess([], 0, "License file: present\nExpiration date: \n", "")
            with patch.object(self.client, "_run", return_value=result):
                self.assertEqual(self.client.license_info(),
                                 {"status": "UNSPECIFIED", "expiration_date": None})
        with self.subTest(case="timeout_redacts_key"):
            with patch.object(self.client, "_run", side_effect=subprocess.TimeoutExpired(
                    ["PVsystCLI", "lic-activate", "-k:private-key"], 5)):
                with self.assertRaises(PVsystError) as raised: self.client.license_activate("private-key", confirm=True)
            self.assertNotIn("private-key", str(raised.exception))
        with self.subTest(case="mutation_confirmation_and_redaction"):
            with patch.object(self.client, "_run") as command:
                with self.assertRaises(ValueError): self.client.license_activate("private-key")
                command.assert_not_called()
            def run(*args):
                if args[0] == "lic-info": return subprocess.CompletedProcess([], 0, "Status: TRIAL\nRemaining days: 5", "")
                return subprocess.CompletedProcess([], 0, "License key: private-key", "")
            with patch.object(self.client, "_run", side_effect=run): info = self.client.license_activate("private-key", confirm=True)
            self.assertNotIn("private-key", str(info))
            self.assertEqual(info["command"], "lic-activate")
    def test_simulation_requires_actual_csv_not_just_success_message(self):
        target = self.workspace / "Results" / "new.csv"
        with patch.object(self.client, "_run", return_value=subprocess.CompletedProcess(
                [], 0, 'File "new.csv" created with success.', "")):
            with self.assertRaises(PVsystError): self.client.run_simulation("Example.PRJ", "VC0", sfi=self.sfi, out_csv=target)
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
            with self.assertRaises(ValueError): self.client.run_simulation("Example.PRJ", "VC0", sfi=self.sfi, out_csv=target)
            command.assert_called_once()
    def test_weather_conversion_flags_and_output(self):
        inputs = [self.workspace / "Meteo" / "sample.csv",
                  self.workspace / "Meteo" / "format.mef",
                  self.workspace / "Sites" / "place.sit"]
        for item in inputs: item.write_text("fixture", encoding="utf-8")
        target = self.workspace / "Meteo" / "result.met"
        def run(*args):
            self.assertEqual(args[0], "convert-meteo")
            self.assertIn("-imt:0", args)
            target.write_text("MET", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Conversion ended successfully...", "")
        with patch.object(self.client, "_run", side_effect=run): self.assertEqual(self.client.convert_meteo(*inputs, target), target)
        with self.assertRaises(ValueError): self.client.convert_meteo(*inputs, target, timeshift=31)
    def test_mcp_project_listing_and_path_confinement(self):
        server._cli = self.client
        self.assertEqual(api_projects.pvsyst_list_projects(), ["Example.PRJ"])
        with self.assertRaises(ValueError): server.workspace_path("Results", "..\\secret.csv", ".csv")
        with self.assertRaises(ValueError): server.workspace_path("Results", "../secret.csv", ".csv")
        with self.assertRaises(ValueError): server.workspace_path("Results", "C:\\secret.csv", ".csv")
    def test_mcp_single_run_bare_csv_name_and_no_overwrite(self):
        server._cli = self.client
        target = self.workspace / "Results" / "single_run.csv"
        def run(*args):
            self.assertIn(f"-ocf:{target}", args)
            target.write_text(CSV_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 2sec", "")
        with patch.object(self.client, "license_info", return_value={"status": "UNSPECIFIED"}):
            with patch.object(self.client, "_run", side_effect=run) as command:
                result = api_simulation.pvsyst_run_simulation(
                    "Example.PRJ", "VC0", sfi_name="demo.sfi",
                    csv_name="single_run")
                self.assertEqual(result["csv"], str(target))
                with self.assertRaisesRegex(ValueError, "Output must be a new"):
                    api_simulation.pvsyst_run_simulation(
                        "Example.PRJ", "VC0", sfi_name="demo.sfi",
                        csv_name="single_run.csv")
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
                with self.assertRaises(ValueError): server.simulation_csv_path(name)
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
        server._cli = self.client
        target = self.workspace / "Results" / "new.csv"
        with patch.object(self.client, "license_info", return_value={"status": "UNSPECIFIED"}), patch.object(self.client, "_run") as command:
            for updates, message in (({"start_date": "1990-01-01"}, "Date must use"),
                                     ({"start_date": "1990.13.40"}, "Invalid calendar date"),
                                     ({"time_step": "subhour"}, "lacks run-simulation --time-step"),
                                     ({"csv_name": "../escape.csv"}, "filename")):
                with self.assertRaisesRegex(server.ToolError, message):
                    asyncio.run(server.mcp.call_tool("pvsyst_run_simulation", {
                        "project": "Example.PRJ", "variant": "VC0", "sfi_name": "demo.sfi",
                        "csv_name": "new.csv", **updates}))
                self.assertFalse(target.exists())
            command.assert_not_called()
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
            for name in ("one.CSV", "two.CSV"): (hourly_dir / name).write_text(CSV_TEXT, encoding="utf-8")
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
        self.assertEqual(len(api_results.pvsyst_read_batch_results(summary.name)["scenarios"]), 2)
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
        with patch.object(self.client, "_run", side_effect=run): result = self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
        self.assertEqual(result["batch_runs"], 1)
        self.assertEqual(parse_batch_results(summary, ("GlobInc",))["scenarios"][0]["values"],
                         {"GlobInc": 33.0})
    def test_concurrent_runs_do_not_reuse_output(self):
        server._cli = None
        ready = Barrier(8)
        def construct(*args):
            sleep(0.02)
            return self.client
        def request():
            ready.wait(timeout=5)
            return server.cli()
        with patch.object(server, "PVsystCLI", side_effect=construct) as factory:
            with ThreadPoolExecutor(max_workers=8) as executor:
                clients = [future.result(timeout=5) for future in [executor.submit(request) for _ in range(8)]]
            factory.assert_called_once()
            self.assertTrue(all(client is self.client for client in clients))
        self.assertIs(server.components().lock, self.client._lock)
        target = self.workspace / "Results" / "same.csv"
        def run(*args):
            target.write_text(CSV_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 2sec", "")
        with patch.object(self.client, "license_info", return_value={"status": "UNSPECIFIED"}), patch.object(self.client, "_run", side_effect=run) as command:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(api_simulation.pvsyst_run_simulation, "Example.PRJ", "VC0",
                                           sfi_name="demo.sfi", csv_name="same.csv") for _ in range(2)]
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except ValueError: outcomes.append("existing output")
            self.assertEqual(len([value for value in outcomes if isinstance(value, dict)]), 1)
            self.assertIn("existing output", outcomes)
            command.assert_called_once()
    def test_batch_rejects_output_traversal_and_missing_success(self):
        self.client._help_cache["run-simulation"] += "\n --batch-params-file|-bpf:"
        with self.subTest(case="output_traversal_and_duplicate"):
            batch = self.workspace / "Models" / "bad_params.CSV"
            batch.write_text("SIM_1;../outside.csv\n", encoding="utf-8")
            with patch.object(self.client, "_run") as command:
                with self.assertRaisesRegex(ValueError, "unique CSV filename"): self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
                batch.write_text("SIM_1;One.CSV\nSIM_2;one.csv\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "unique CSV filename"): self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
                command.assert_not_called()
        with self.subTest(case="zero_exit_without_success"):
            batch = self.workspace / "Models" / "empty_params.CSV"
            batch.write_text("SIM_1;hourly.csv\n", encoding="utf-8")
            with patch.object(self.client, "_run", return_value=subprocess.CompletedProcess(
                    [], 0, "Loading workspace failed", "")):
                with self.assertRaises(PVsystError): self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
    def test_comma_batch_hourly_and_minute_energy(self):
        batch_file = self.workspace / "Results" / "batch_hourly.csv"
        batch_file.write_text(CSV_TEXT.replace(";", ",").replace("    ,kW,ratio", "    ,W,ratio"),
                              encoding="utf-8")
        parsed = list(iter_result_csv(batch_file))
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0][0][1], "E_Grid")
        self.assertEqual(result_units(batch_file)["E_Grid"], "W")
        self.assertAlmostEqual(summarize_results(batch_file, ("E_Grid",))["summary"]["E_Grid"]["energy_kwh"], 0.0045)
        minute_file = self.workspace / "Results" / "minute.csv"
        minute_file.write_text(CSV_TEXT + "01/01/90 01:01;60;0.8\n", encoding="utf-8")
        self.assertIsNone(summarize_results(minute_file, ("E_Grid",))["summary"]["E_Grid"]["energy_kwh"])
        minute_file.write_text(CSV_TEXT.split("01/01/90 00:00")[0] +
                               "01/01/90 00:00;60;0.8\n"
                               "01/01/90 00:01;60;0.8\n"
                               "01/01/90 00:02;60;0.8\n", encoding="utf-8")
        minute_summary = summarize_results(minute_file, ("E_Grid",))
        self.assertEqual(minute_summary["step_minutes"], 1)
        self.assertAlmostEqual(minute_summary["summary"]["E_Grid"]["energy_kwh"], 3)
        server._cli = self.client
        report = api_results.pvsyst_read_results("minute.csv")
        self.assertEqual(report["step_minutes"], 1)
        self.assertAlmostEqual(report["summary"]["E_Grid"]["energy_kwh"], 3)
        with self.assertRaisesRegex(ValueError, "Choose columns from"): summarize_results(minute_file, ("not_a_column",))
        bad_header = self.workspace / "Results" / "missing_sep.csv"
        bad_header.write_text("date\n01/01/90 00:00\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Missing SFI result header"): list(iter_result_csv(bad_header))
        with self.assertRaises(ValueError): api_results.pvsyst_read_results("batch_hourly.csv", folder="../outside")
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
    def test_export_logs_and_bounded_rows(self):
        folder = self.workspace / "Results"
        def run(*args):
            self.assertEqual(args[0], "export-logs")
            (folder / "logs.zip").write_bytes(b"PK")
            return subprocess.CompletedProcess([], 0, "exported", "")
        with patch.object(self.client, "_run", side_effect=run): self.assertEqual(self.client.export_logs(folder).name, "logs.zip")
        result = folder / "result.csv"
        result.write_text(CSV_TEXT, encoding="utf-8")
        server._cli = self.client
        page = api_results.pvsyst_read_rows("result.csv", offset=1, limit=1)
        self.assertEqual(page["rows"][0][1:], [4.5, 0.8])
        self.assertEqual(page["total"], 2)
        with self.assertRaises(ValueError): api_results.pvsyst_read_rows("result.csv", limit=501)
    def test_timeout_redaction_empty_version_and_case_insensitive_variants(self):
        self.client._help_cache["run-simulation"] += "\n --batch-params-file|-bpf:"
        timeout = subprocess.TimeoutExpired(
            ["PVsystCLI", "-w:C:/secret-workspace", "-k:private-key"], 5,
            output="partial line\nHost ID: fixture-secret")
        target = self.workspace / "Results" / "new.csv"
        with patch.object(self.client, "_run", side_effect=timeout):
            with self.assertRaises(PVsystError) as caught: self.client.run_simulation("Example.PRJ", "VC0", sfi=self.sfi, out_csv=target)
        text = str(caught.exception) + caught.exception.diagnostics
        for secret in ("private-key", "secret-workspace", "fixture-secret"): self.assertNotIn(secret, text)
        self.assertIn("partial line", caught.exception.diagnostics)
        with patch.object(self.client, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            with self.assertRaisesRegex(PVsystError, "no output"): self.client.version()
        def run(*args):
            self.assertIn("-v:VC0", args)
            target.write_text(CSV_TEXT, encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 1sec", "")
        for disk, req in (("VC0", "vc0"), ("vc0", "VC0")):
            for old in (self.workspace / "Projects").glob("Example.[Vv][Cc]0"): old.unlink()
            (self.workspace / "Projects" / f"Example.{disk}").write_text("variant", encoding="utf-8")
            target.unlink(missing_ok=True)
            self.assertEqual(self.client.list_variants("Example.PRJ"), ["VC0"])
            with patch.object(self.client, "_run", side_effect=run): self.client.run_simulation("Example.PRJ", req, sfi=self.sfi, out_csv=target)
    def test_batch_summary_matching_and_symlink_guards(self):
        self.client._help_cache["run-simulation"] += "\n --batch-params-file|-bpf:"
        batch_dir, hourly_dir = self.workspace / "UserBatch", self.workspace / "UserHourly"
        batch_dir.mkdir()
        hourly_dir.mkdir()
        batch = batch_dir / "batch_params.CSV"
        batch.write_text("Ident;Create hourly\nSIM_1;one.CSV\n", encoding="utf-8")
        def writes(summary):
            def run(*args):
                (batch_dir / summary).write_text("Ident;Create hourly;Error;E_Grid;PR\nSIM_1;one.CSV;;12;0.7\n", encoding="utf-8")
                (hourly_dir / "one.CSV").write_text(CSV_TEXT, encoding="utf-8")
                return subprocess.CompletedProcess([], 0, "Simulation done in 0min 1sec", "")
            return run
        with patch.object(self.client, "_run", side_effect=writes("other_Results.CSV")):
            with self.assertRaisesRegex(PVsystError, "batch_Results.CSV"): self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
        (hourly_dir / "one.CSV").unlink()
        with patch.object(self.client, "_run", side_effect=writes("batch_Results.CSV")):
            result = self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
        self.assertEqual(Path(result["batch_summary"]).name, "batch_Results.CSV")
        # Quiet batch runs must still forward native refusal text
        (hourly_dir / "one.CSV").unlink(missing_ok=True)
        (batch_dir / "batch_Results.CSV").unlink(missing_ok=True)
        refusal_out = (
            "Simulation cannot be run.\n"
            "The hourly load cannot be used with the Peak Shaving strategy.\n"
        )
        for level in (0, 2):
            with self.subTest(log_level=level):
                with patch.object(
                    self.client, "_run",
                    return_value=subprocess.CompletedProcess([], 0, refusal_out, ""),
                ):
                    with self.assertRaises(PVsystError) as caught:
                        self.client.run_simulation(
                            "Example.PRJ", "VC0", batch_params=batch, log_level=level,
                        )
                self.assertIn("batch_Results.CSV", str(caught.exception))
                self.assertIn("Simulation cannot be run.", str(caught.exception))
                self.assertIn("Peak Shaving", str(caught.exception))
        external = self.workspace.parent / "external"
        external.mkdir()
        link = hourly_dir / "escape.csv"
        try:
            link.symlink_to(external / "escape.csv")
        except OSError: return
        (self.workspace / "Models" / "DemoParams.CSV").write_text("SIM_1;escape.csv\n", encoding="utf-8")
        with patch.object(self.client, "_run") as cmd:
            with self.assertRaisesRegex(ValueError, "outside"):
                self.client.run_simulation("Example.PRJ", "VC0", batch_params=self.workspace / "Models" / "DemoParams.CSV")
            cmd.assert_not_called()
    def test_native_load_missing_root_and_winlaunch_stdio_guard(self):
        from threading import RLock
        from types import SimpleNamespace
        fake_store = SimpleNamespace(
            lock=RLock(), _check_project=lambda p: None,
            _path=lambda p, v=None: self.workspace / (f"Example.{v}" if v else p),
            _read=lambda p: b"  Comment=no root object\r\n",
            _text=lambda d: (d.decode(), d.decode().splitlines(keepends=True)),
            _sections=lambda l: ({}, None),
        )
        blocked = native_load.create_native_load_variant(
            fake_store, project="Example.PRJ", source_variant="VC0", new_variant="VC1",
            load={"mode": "constant", "constant_kw": 1.0})
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["issues"], ["Project file has no root PVObject block"])
        if os.name == "nt":
            from pvsyst_auto.native import _winlaunch
            invalid = _winlaunch.ctypes.c_void_p(-1).value
            create = Mock(return_value=0)
            with patch.object(_winlaunch, "_current_station_name", return_value="WinSta0"), \
                    patch.object(_winlaunch, "_CreateDesktopW", return_value=101), \
                    patch.object(_winlaunch, "_CloseDesktop", return_value=1), \
                    patch.object(_winlaunch, "_CreateJobObjectW", return_value=102), \
                    patch.object(_winlaunch, "_SetInformationJobObject", return_value=1), \
                    patch.object(_winlaunch, "_TerminateJobObject", return_value=1), \
                    patch.object(_winlaunch, "_CloseHandle", return_value=1), \
                    patch.object(_winlaunch, "_CreateFileW", return_value=invalid), \
                    patch.object(_winlaunch, "_CreateProcessW", create):
                with self.assertRaisesRegex(OSError, r"CreateFileW\(stdout\) failed"): _winlaunch.run([sys.executable, "-c", "pass"], Path.cwd(), 5)
            create.assert_not_called()
    def test_double_quotes_never_reach_cli_and_os_errors_preserve_message(self):
        injected = 'a" -osf:C:\\Temp\\evil.sit "'
        weather = self.workspace / "Meteo" / "weather.csv"
        weather.write_text("Year;Month;Day;Hour;GHI;Tamb\n", encoding="utf-8")
        site = self.workspace / "Sites" / "new.sit"
        self.client._help_cache["create-site"] = "--site-name|-sn:"
        calls = {
            "site name": lambda: self.client.create_site(injected, 0, 0, weather, site),
            "site source": lambda: self.client.create_site("Site", 0, 0, weather, site, source=injected),
            "license key": lambda: self.client.license_activate(injected, confirm=True),
            "customer id": lambda: self.client.license_deactivate(injected, confirm=True),
            "any command": lambda: self.client._run("create-site", f"-sn:{injected}"),
        }
        with contextlib.ExitStack() as stack:
            launchers = [stack.enter_context(patch("subprocess.run"))]
            if os.name == "nt":
                from pvsyst_auto.native import _winlaunch
                launchers.append(stack.enter_context(patch.object(_winlaunch, "run")))
            for name, call in calls.items():
                with self.subTest(name), self.assertRaisesRegex(ValueError, "double quotes") as caught: call()
                self.assertNotIn("evil", str(caught.exception))
        for launcher in launchers: launcher.assert_not_called()
        from mcp.server.mcpserver.exceptions import ToolError
        (self.workspace / "Results" / "locked.csv").write_text("date;E_Grid\n;kW\n01/01/90 00:00;10\n", encoding="utf-8")
        cases = (
            (PermissionError(13, "Permission denied", "locked.csv"), "PermissionError: [Errno 13] Permission denied: 'locked.csv'"),
            (FileNotFoundError("gone.csv"), "pvsyst_read_rows: gone.csv"),
        )
        with patch.object(server, "_cli", self.client):
            for error, message in cases:
                with self.subTest(error=type(error).__name__), \
                        patch.object(api_results, "read_result_page", side_effect=error), \
                        self.assertRaises(ToolError) as raised:
                    asyncio.run(server.mcp.call_tool("pvsyst_read_rows", {"csv_name": "locked.csv"}))
                self.assertIn(message, str(raised.exception))
    def test_centralized_version_allowlists_stay_explicit_and_reject_unverified(self):
        from pvsyst_auto import validation as versions
        for name in ("NATIVE_TEXT", "NATIVE_TEXT_OR_SEED", "CLI_TRACKING_BUILDS"):
            value = getattr(versions, name)
            self.assertIsInstance(value, tuple, name)
            self.assertTrue(value and len(set(value)) == len(value), name)
        self.assertTrue(set(versions.NATIVE_TEXT) < set(versions.NATIVE_TEXT_OR_SEED))
        self.assertEqual(versions.NATIVE_TEXT, ("8.1.6", "8.1.7"))
        self.assertIn("8.1.0", versions.NATIVE_TEXT_OR_SEED)
        self.assertIn("PVsystCLI 8.1.7 (rev. 49382)", versions.CLI_TRACKING_BUILDS)
        self.assertIn(versions.HEADLESS_RECORD_VERSION, versions.NATIVE_TEXT)
        for token in ("8.1.5", "8.1.8", "8.2.0", "9.0.0"): self.assertNotIn(token, versions.NATIVE_TEXT_OR_SEED)
    def test_cli_control_chars_bool_guards_csv_encoding_headers_and_leap_day(self):
        import warnings
        from pvsyst_auto.native.cli import (
            _cli_text, _csv_encoding, read_result_page, summarize_results,
        )
        # 1. Control-char injection rejected in _cli_text and _launch without echoing secrets
        for bad_ch in ('\x00', '\t', '\n', '\r', '\x1b', '\x1f', '\x7f', '"'): self.assertFalse(_cli_text(f"secret{bad_ch}value"))
        self.assertTrue(_cli_text("Normal Site 01"))
        with contextlib.ExitStack() as stack:
            launchers = [stack.enter_context(patch("subprocess.run"))]
            if os.name == "nt":
                from pvsyst_auto.native import _winlaunch
                launchers.append(stack.enter_context(patch.object(_winlaunch, "run")))
            for bad_ch in ('\x00', '\x07', '\x1b', '\x7f', '\n'):
                secret = f"secret-token{bad_ch}-osf:evil.sit"
                with self.assertRaises(ValueError) as caught: self.client._run("lic-activate", f"-k:{secret}")
                self.assertNotIn("secret-token", str(caught.exception))
                with self.assertRaises(ValueError) as caught2: self.client.license_activate(secret, confirm=True)
                self.assertNotIn("secret-token", str(caught2.exception))
            for launcher in launchers: launcher.assert_not_called()
        # 2. Bool-as-numeric guards
        with self.assertRaises(ValueError): build_monthly_weather_csv(self.workspace / "Meteo" / "bool_w.csv", [True] + [100.0] * 11, [20.0] * 12)
        with self.assertRaises(ValueError): build_monthly_weather_csv(self.workspace / "Meteo" / "bool_t.csv", [100.0] * 12, [False] + [20.0] * 11)
        inputs = [self.workspace / "Meteo" / "b.csv", self.workspace / "Meteo" / "b.mef", self.workspace / "Sites" / "b.sit"]
        for p in inputs: p.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError): self.client.convert_meteo_details(*inputs, self.workspace / "Meteo" / "b.met", timeshift=True)
        self.client._help_cache["create-site"] = "--site-name|-sn:"
        out_sit = self.workspace / "Sites" / "bool_out.sit"
        for kwargs in ({"latitude": True, "longitude": 0.0}, {"latitude": 0.0, "longitude": 0.0, "albedo": False}):
            lat = kwargs.pop("latitude")
            lon = kwargs.pop("longitude")
            with self.assertRaises(ValueError): self.client.create_site_details("Site", lat, lon, inputs[0], out_sit, **kwargs)
        # 3. CP1252 vs BOM-less UTF-8 vs UTF-8-sig CSV decoding without U+FFFD
        sfi_body = "date;GlobInc;E_Grid\n;W/m\u00b2;kW\n01/01/90 00:00;500;4.0\n"
        for label, raw_bytes, expected_enc in (
            ("utf8_bom", b"\xef\xbb\xbf" + sfi_body.encode("utf-8"), "utf-8-sig"),
            ("utf8_nobom", sfi_body.encode("utf-8"), "utf-8"),
            ("cp1252", sfi_body.encode("cp1252"), "cp1252"),
        ):
            csv_file = self.workspace / "Results" / f"enc_{label}.csv"
            csv_file.write_bytes(raw_bytes)
            self.assertEqual(_csv_encoding(csv_file), expected_enc)
            units = result_units(csv_file, strict=True)
            self.assertEqual(units["GlobInc"], "W/m\u00b2")
            self.assertNotIn("\ufffd", str(units))
            page = read_result_page(csv_file, ["GlobInc"], 0, 10)
            self.assertEqual(page["units"]["GlobInc"], "W/m\u00b2")
            summ = summarize_results(csv_file, ("GlobInc", "E_Grid"))
            self.assertEqual(summ["summary"]["GlobInc"]["unit"], "W/m\u00b2")
        batch_cp1252 = self.workspace / "Models" / "accentParams.CSV"
        batch_cp1252.write_bytes("Ident;Create hourly\nSIM_1;Caf\u00e9.CSV\n".encode("cp1252"))
        names, _, _ = self.client._batch_outputs(batch_cp1252)
        self.assertEqual(names, ["Caf\u00e9.CSV"])
        summary_utf8 = self.workspace / "Results" / "utf8_Results.CSV"
        summary_utf8.write_bytes("Ident;Create hourly;Error;E_Grid;PR\nSIM_1;Caf\u00e9.CSV;;10;0.8\n".encode("utf-8"))
        parsed_batch = parse_batch_results(summary_utf8)
        self.assertEqual(parsed_batch["scenarios"][0]["hourly_file"], "Caf\u00e9.CSV")
        # 4. Truncated units row returns {} cleanly or raises ValueError in strict mode (never StopIteration)
        trunc = self.workspace / "Results" / "truncated.csv"
        trunc.write_text("date;E_Grid\n", encoding="utf-8")
        self.assertEqual(result_units(trunc, strict=False), {})
        with self.assertRaises(ValueError): result_units(trunc, strict=True)
        # 5. Duplicate headers, 'date' column indexing, and sum overflow
        dup = self.workspace / "Results" / "dup.csv"
        dup.write_text("date;E_Grid;E_Grid\n;kW;kW\n01/01/90 00:00;1;2\n", encoding="utf-8")
        with self.assertRaises(ValueError): read_result_page(dup, ["E_Grid"], 0, 5)
        with self.assertRaises(ValueError): summarize_results(dup, ("E_Grid",))
        date_col = self.workspace / "Results" / "date_col.csv"
        date_col.write_text("Date;date;PR\n;kW;ratio\n01/01/90 00:00;12.5;0.85\n", encoding="utf-8")
        self.assertEqual(read_result_page(date_col, ["date"], 0, 5)["rows"][0][1:], [12.5])
        self.assertEqual(summarize_results(date_col, ("date",))["summary"]["date"]["sum"], 12.5)
        overflow_csv = self.workspace / "Results" / "overflow.csv"
        overflow_csv.write_text(
            "date;E_Grid\n;kW\n01/01/90 00:00;1e308\n01/01/90 01:00;1e308\n", encoding="utf-8"
        )
        with self.assertRaises(ValueError): summarize_results(overflow_csv, ("E_Grid",))
        # 6. Leap day "02.29" without DeprecationWarning + ASCII digit enforcement
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            pre = self.client.preflight_run_simulation(
                "Example.PRJ", "VC0", out_csv=self.workspace / "Results" / "leap.csv",
                start_date="02.29", end_date="2024.02.29",
            )
        self.assertEqual(pre["variant"], "VC0")
        for bad_date in ("02.30", "23.02.29", "\u0660\u0661.\u0660\u0661"):
            with self.assertRaises(ValueError):
                self.client.preflight_run_simulation(
                    "Example.PRJ", "VC0", out_csv=self.workspace / "Results" / "leap.csv",
                    start_date=bad_date,
                )
    def test_winlaunch_posix_import_attr_guard_cwd_share_and_timeout_clamp(self):
        import importlib.util
        from pvsyst_auto.native import _winlaunch
        # 1. Importing _winlaunch on non-Windows does not touch ctypes.WinDLL
        spec = importlib.util.spec_from_file_location(
            "_winlaunch_posix_probe", Path(_winlaunch.__file__)
        )
        probe = importlib.util.module_from_spec(spec)
        with patch.object(os, "name", "posix"), \
                patch.object(_winlaunch.ctypes, "WinDLL", side_effect=AttributeError("no WinDLL"), create=True):
            spec.loader.exec_module(probe)
            self.assertIsNone(probe._u)
            with self.assertRaises(OSError): probe.run(["echo"], None, 1)
        if os.name == "nt":
            # 2. Timeout overflow guard before creating handles
            with self.assertRaises(OverflowError): _winlaunch.run([sys.executable], None, _winlaunch._INFINITE / 1000)
            with self.assertRaises(OverflowError): _winlaunch.run([sys.executable], None, 5_000_000)
            # 3. Failed InitializeProcThreadAttributeList never calls DeleteProcThreadAttributeList
            delete_attr = Mock()
            def init_attr(buf, count, flags, size_ptr):
                if buf is None:
                    size_ptr._obj.value = 64
                    return 0
                return 0
            with patch.object(_winlaunch, "_current_station_name", return_value="WinSta0"), \
                    patch.object(_winlaunch, "_CreateDesktopW", return_value=101), \
                    patch.object(_winlaunch, "_CloseDesktop", return_value=1), \
                    patch.object(_winlaunch, "_CreateJobObjectW", return_value=102), \
                    patch.object(_winlaunch, "_SetInformationJobObject", return_value=1), \
                    patch.object(_winlaunch, "_TerminateJobObject", return_value=1), \
                    patch.object(_winlaunch, "_CloseHandle", return_value=1), \
                    patch.object(_winlaunch, "_CreateFileW", side_effect=[201, 202, 203]), \
                    patch.object(_winlaunch, "_InitializeProcThreadAttributeList", side_effect=init_attr), \
                    patch.object(_winlaunch, "_DeleteProcThreadAttributeList", delete_attr):
                with self.assertRaisesRegex(OSError, "InitializeProcThreadAttributeList failed"): _winlaunch.run([sys.executable], None, 5)
            delete_attr.assert_not_called()
            # 4. FILE_SHARE_DELETE on stdout/stderr, cwd=None -> None, WaitForSingleObject before _read_stream
            events = []
            share_modes = []
            def fake_create_file(path, access, share, sa, disp, attr, tpl):
                share_modes.append((path, share))
                return 300 + len(share_modes)
            def fake_init_ok(buf, count, flags, size_ptr):
                if buf is None:
                    size_ptr._obj.value = 64
                    return 0
                return 1
            captured_cwd = []
            def fake_create_proc(app, cmd, pa, ta, inh, flags, env, cwd_arg, si, pi):
                captured_cwd.append(cwd_arg)
                pi._obj.hProcess = 401
                pi._obj.hThread = 402
                return 1
            def fake_wait(handle, ms):
                events.append(("wait", handle, ms))
                return 0
            def fake_term_job(handle, code):
                events.append(("term_job", handle, code))
                return 1
            def fake_read(path):
                events.append(("read", os.path.basename(path)))
                return "ok"
            with patch.object(_winlaunch, "_current_station_name", return_value="WinSta0"), \
                    patch.object(_winlaunch, "_CreateDesktopW", return_value=101), \
                    patch.object(_winlaunch, "_CloseDesktop", return_value=1), \
                    patch.object(_winlaunch, "_CreateJobObjectW", return_value=102), \
                    patch.object(_winlaunch, "_SetInformationJobObject", return_value=1), \
                    patch.object(_winlaunch, "_CreateFileW", side_effect=fake_create_file), \
                    patch.object(_winlaunch, "_InitializeProcThreadAttributeList", side_effect=fake_init_ok), \
                    patch.object(_winlaunch, "_UpdateProcThreadAttribute", return_value=1), \
                    patch.object(_winlaunch, "_DeleteProcThreadAttributeList"), \
                    patch.object(_winlaunch, "_CreateProcessW", side_effect=fake_create_proc), \
                    patch.object(_winlaunch, "_AssignProcessToJobObject", return_value=1), \
                    patch.object(_winlaunch, "_ResumeThread", return_value=1), \
                    patch.object(_winlaunch, "_WaitForSingleObject", side_effect=fake_wait), \
                    patch.object(_winlaunch, "_GetExitCodeProcess", return_value=1), \
                    patch.object(_winlaunch, "_TerminateJobObject", side_effect=fake_term_job), \
                    patch.object(_winlaunch, "_TerminateProcess", return_value=1), \
                    patch.object(_winlaunch, "_CloseHandle", return_value=1), \
                    patch.object(_winlaunch, "_read_stream", side_effect=fake_read):
                completed = _winlaunch.run([sys.executable, "-V"], None, 5)
            self.assertEqual(completed.stdout, "ok")
            self.assertEqual(captured_cwd, [None])
            self.assertEqual(share_modes[0][1], _winlaunch._FILE_SHARE_RW | _winlaunch._FILE_SHARE_DELETE)
            self.assertEqual(share_modes[1][1], _winlaunch._FILE_SHARE_RW | _winlaunch._FILE_SHARE_DELETE)
            term_idx = events.index(("term_job", 102, 0))
            rundown_idx = events.index(("wait", 401, 2000))
            read_idx = events.index(("read", "stdout"))
            self.assertLess(term_idx, rundown_idx)
            self.assertLess(rundown_idx, read_idx)
    def test_simulation_error_includes_nonzero_exit_code(self):
        out_csv = self.workspace / "Results" / "nonzero_exit.csv"
        out_pdf = self.workspace / "Results" / "nonzero_exit.pdf"
        def _fake_nonzero(*args):
            out_csv.write_text(CSV_TEXT, encoding="utf-8")
            out_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            return subprocess.CompletedProcess(
                [], 3, "Simulation done in 0min 5sec\nReport PDF generated\nA ", ""
            )
        with patch.object(self.client, "_run", side_effect=_fake_nonzero):
            with self.assertRaisesRegex(
                PVsystError,
                r"Simulation did not complete or produce requested output \(exit code 3\):",
            ):
                self.client.run_simulation(
                    "Example.PRJ", "VC0", sfi=self.sfi,
                    out_csv=out_csv, report_pdf=out_pdf,
                )
    def test_e2e_ini_paths_and_plain_csv_helper(self):
        import importlib.util
        repo_root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location(
            "_e2e_full", repo_root / "tests" / "e2e" / "test_mcp_full_workflows_e2e.py"
        )
        e2e_full = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(e2e_full)
        with patch.dict(os.environ, {"LOCALAPPDATA": ""}, clear=False):
            expected_local = Path.home() / "AppData" / "Local"
            self.assertEqual(
                e2e_full._ini_paths()[0],
                expected_local / "PVsyst" / "8.1" / "Admin" / "PVsyst.ini",
            )
        case = e2e_full.RealMcpServerWorkflowsE2E("test_mcp_dc_cabling_unavail_and_storage_combo_e2e")
        case.ws = self.workspace
        ok_csv = self.workspace / "Results" / "e2e_ok.csv"
        ok_csv.write_text(CSV_TEXT, encoding="utf-8")
        case._assert_plain_csv_result({
            "csv": str(ok_csv), "pdf": None, "batch_summary": None,
            "batch_runs": None, "batch_hourly": [], "seconds": 1,
        }, "e2e_ok.csv")
        for bad in (
            {"csv": str(ok_csv), "pdf": "extra.pdf", "batch_hourly": []},
            {"csv": str(ok_csv), "batch_hourly": ["x.csv"]},
            {"csv": str(self.workspace / "Results" / "other.csv"), "batch_hourly": []},
        ):
            with self.assertRaises(AssertionError): case._assert_plain_csv_result(bad, "e2e_ok.csv")
class FileioTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "Example.VC0"
    def names(self):
        return sorted(path.name for path in self.root.iterdir())
    def temporaries(self):
        return [path.name for path in self.root.iterdir() if path.name.endswith(".tmp")]
    def test_stage_cleans_up_on_fsync_failure(self):
        staged = paths.stage(self.path, b"candidate")
        self.assertEqual(staged.parent, self.root)
        self.assertTrue(staged.name.startswith(".Example.VC0.") and staged.name.endswith(".tmp"))
        self.assertEqual(staged.read_bytes(), b"candidate")
        staged.unlink()
        with patch.object(paths.os, "fsync", side_effect=OSError("injected flush failure")):
            with self.assertRaisesRegex(OSError, "injected flush failure"): paths.stage(self.path, b"candidate")
        self.assertEqual(self.names(), [])
    def test_replace_checked_success_and_stale_abort(self):
        self.path.write_bytes(b"inspected")
        events = []
        def read(path):
            events.append(("read", len(self.temporaries())))
            return path.read_bytes()
        result = paths.replace_checked(
            self.path, b"inspected", b"candidate", read, "changed",
            prepare=lambda: events.append(("prepare", 0)) or "backup-name",
            verify=lambda: events.append(("verify", len(self.temporaries()))),
        )
        self.assertEqual(result, "backup-name")
        self.assertEqual(events, [("read", 0), ("prepare", 0), ("read", 1), ("verify", 1)])
        self.assertEqual(self.path.read_bytes(), b"candidate")
        self.assertEqual(self.names(), ["Example.VC0"])
        self.path.write_bytes(b"saved by another process")
        prepared = []
        with self.assertRaisesRegex(ValueError, "^Variant changed while preparing the edit$"):
            paths.replace_checked(self.path, b"inspected", b"candidate", Path.read_bytes,
                                   "Variant changed while preparing the edit",
                                   prepare=lambda: prepared.append(True))
        self.assertEqual(prepared, [])
        self.path.write_bytes(b"inspected")
        reads = []
        def racing_read(path):
            reads.append(self.temporaries())
            if len(reads) == 2: path.write_bytes(b"saved after staging")
            return path.read_bytes()
        with self.assertRaisesRegex(ValueError, "^Component changed while preparing the edit$"):
            paths.replace_checked(self.path, b"inspected", b"candidate", racing_read,
                                   "Component changed while preparing the edit")
        self.assertEqual(self.path.read_bytes(), b"saved after staging")
        self.path.write_bytes(b"inspected")
        with self.assertRaisesRegex(ValueError, "Generator component changed"):
            paths.replace_checked(self.path, b"inspected", b"candidate", Path.read_bytes, "changed",
                                   verify=lambda: (_ for _ in ()).throw(ValueError("Generator component changed")))
        self.assertEqual(self.path.read_bytes(), b"inspected")
        self.assertEqual(self.names(), ["Example.VC0"])
    def test_atomic_replace_and_lock_rollback(self):
        paths.atomic_replace(self.path, b"first")
        self.assertEqual(self.path.read_bytes(), b"first")
        paths.atomic_replace(self.path, b"second")
        self.assertEqual(self.path.read_bytes(), b"second")
        with patch.object(paths.os, "replace", side_effect=PermissionError("injected lock")):
            with self.assertRaisesRegex(PermissionError, "injected lock"): paths.atomic_replace(self.path, b"third")
        self.assertEqual(self.path.read_bytes(), b"second")
        self.assertEqual(self.names(), ["Example.VC0"])
    def test_publish_new_exclusive_and_race_safe(self):
        with patch.object(paths.os, "fsync", side_effect=OSError(28, "injected disk full")):
            with self.assertRaisesRegex(OSError, "injected disk full"): paths.publish_new(self.path, b"candidate")
        self.assertEqual(self.names(), [])
        paths.publish_new(self.path, b"created")
        self.assertEqual(self.path.read_bytes(), b"created")
        with self.assertRaises(FileExistsError): paths.publish_new(self.path, b"overwrite attempt")
        target = self.root / "Race.VC0"
        stage = paths.stage
        def stage_then_write(path, data):
            staged = stage(path, data)
            path.write_bytes(b"saved meanwhile")
            return staged
        with patch.object(paths, "stage", side_effect=stage_then_write):
            with self.assertRaises(FileExistsError): paths.publish_new(target, b"candidate")
        self.assertEqual(target.read_bytes(), b"saved meanwhile")
    def test_write_json_exclusive_atomic_and_failure_preserves_checkpoint(self):
        target = self.root / "Results" / "task.json"
        paths.write_json(target, {"phase": "initialized"}, first=True)
        with self.assertRaises(FileExistsError): paths.write_json(target, {"phase": "other task"}, first=True)
        events = []
        fsync, replace = os.fsync, os.replace
        with patch.object(paths.os, "fsync", side_effect=lambda fd: (events.append("fsync"), fsync(fd))[1]), \
                patch.object(paths.os, "replace", side_effect=lambda s, d: (events.append("replace"), replace(s, d))[1]):
            paths.write_json(target, {"phase": "completed", "note": "caf\u00e9"})
        self.assertEqual(events, ["fsync", "replace"])
        raw = target.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(json.loads(raw.decode("ascii")), {"phase": "completed", "note": "caf\u00e9"})
        with self.assertRaisesRegex(ValueError, "not JSON compliant"): paths.write_json(target, {"ratio": float("nan")})
        with patch.object(paths.os, "replace", side_effect=PermissionError("fixture: manifest is locked")):
            with self.assertRaisesRegex(PermissionError, "manifest is locked"): paths.write_json(target, {"phase": "failed"})
        self.assertEqual(target.read_bytes(), raw)
class WorkspaceIniTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "PVsystCLI" / "8.1" / "Admin" / "PVsystCLI.ini"
        self.path.parent.mkdir(parents=True)
        self.executable = Path(directory.name) / "PVsyst8.1.7" / "PVsystCLI.exe"
        for mock in (patch.object(ini, "_profile_ini", return_value=self.path),
                     patch.object(ini, "_is_supported", return_value=True)):
            mock.start()
            self.addCleanup(mock.stop)
        lock = patch.object(ini, "_profile_lock", side_effect=lambda _: nullcontext())
        self.lock = lock.start()
        self.addCleanup(lock.stop)
    def test_merge_utf16_fields_on_native_timeout(self):
        def raw(text):
            return codecs.BOM_UTF16_LE + text.encode("utf-16-le")
        self.path.write_bytes(raw("[System]\r\nDataPath=old-é\r\nNRun=1\r\n"
                                  "[RecentWorkspaces]\r\nWorkspace3=空\r\nWorkspace1=old"))
        native = (";新 setting\r\n[System]\r\nNRun=2\r\n  DATAPATH =new\r\nLastDate=today\n"
                  "[RecentWorkspaces]\nWorkspace1=new\r\n; native comment\r\n"
                  "Workspace2=temp\r\nWorkspace4=temp\r\nWorkspaceMax=9\r\n"
                  "[Other]\r\nDataPath=untouched\r\nWorkspace1=untouched")
        with self.assertRaises(subprocess.TimeoutExpired):
            with ini.preserve_workspace_settings(self.executable, restore=True):
                self.path.write_bytes(raw(native))
                raise subprocess.TimeoutExpired("fake native launch", 10)
        expected = (native.replace("  DATAPATH =new\r\n", "DataPath=old-é\r\n")
                    .replace("Workspace1=new\r\n", "Workspace3=空\r\n")
                    .replace("Workspace2=temp\r\nWorkspace4=temp\r\n", "Workspace1=old\r\n"))
        self.assertEqual(self.path.read_bytes(), raw(expected))
        self.lock.assert_called_once_with(self.path)
    def test_first_launch_removes_only_new_workspace_fields(self):
        before = b"[System]\r\nNRun=1\r\nDataPath=temp\r\nLastDate=today\r\n"
        recent = b"[RecentWorkspaces]\r\nWorkspace1=temp\r\nOpaque=\x81\r\n"
        self.assertFalse(self.path.exists())
        with ini.preserve_workspace_settings(self.executable, restore=True): self.path.write_bytes(codecs.BOM_UTF8 + before + recent)
        self.assertEqual(self.path.read_bytes(), codecs.BOM_UTF8
                         + before.replace(b"DataPath=temp\r\n", b"")
                         + recent.replace(b"Workspace1=temp\r\n", b""))
    def test_restore_target_fields_when_native_removes_file(self):
        self.path.write_bytes(b"[System]\r\nDataPath=old\r\nNRun=2\r\n"
                              b"[RecentWorkspaces]\r\nWorkspace1=old")
        with ini.preserve_workspace_settings(self.executable, restore=True): self.path.unlink()
        self.assertEqual(self.path.read_bytes(), b"[System]\r\nDataPath=old\r\n"
                                                 b"[RecentWorkspaces]\r\nWorkspace1=old")
    def test_opt_out_takes_same_lock_without_touching_ini(self):
        with patch.object(ini, "_read_ini", side_effect=AssertionError("unexpected INI access")):
            with ini.preserve_workspace_settings(self.executable, restore=False): self.assertFalse(self.path.exists())
        self.lock.assert_called_once_with(self.path)
    def test_failed_restore_keeps_current_file_and_removes_staging_file(self):
        self.path.write_bytes(b"[System]\r\nDataPath=old\r\n")
        native = b"[System]\r\nDataPath=temp\r\nNRun=2\r\n"
        with patch.object(ini.os, "replace", side_effect=PermissionError("fixture locked")):
            with self.assertRaisesRegex(ini.WorkspaceSettingsError, "Could not restore"):
                with ini.preserve_workspace_settings(self.executable, restore=True): self.path.write_bytes(native)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(self.path.read_bytes(), native)
    def test_recheck_does_not_overwrite_a_concurrent_external_edit(self):
        old = b"[System]\r\nDataPath=old\r\n"
        native, external = old.replace(b"old", b"temp"), old + b"NRun=3\r\n"
        self.path.write_bytes(old)
        replace = ini.atomic_replace
        def external_save(path, data, verify):
            path.write_bytes(external)
            replace(path, data, verify)
        with patch.object(ini, "atomic_replace", side_effect=external_save):
            with self.assertRaises(ini.WorkspaceSettingsError):
                with ini.preserve_workspace_settings(self.executable, restore=True): self.path.write_bytes(native)
        self.assertEqual(self.path.read_bytes(), external)
@unittest.skipUnless(os.name == "nt", "Windows profile coordination")
class WorkspaceIniWindowsTests(unittest.TestCase):
    def test_native_version_gate_and_profile_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "PVsystCLI.ini"
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"not a PE resource")
            with patch.object(ini, "_profile_ini", side_effect=AssertionError("unexpected profile access")):
                with ini.preserve_workspace_settings(fake, restore=True): pass
                with patch.object(ini, "_file_version", return_value=(8, 1, 7, 49383)):
                    with ini.preserve_workspace_settings(fake, restore=True): pass
            child = "\nimport sys\nfrom pathlib import Path\nfrom pvsyst_auto.native import _winlaunch as ini\nini._LOCK_TIMEOUT_MS = 40\npath = Path(sys.argv[1])\ntry:\n    with ini._profile_lock(path):\n        raise AssertionError('same-profile lock was not held')\nexcept ini.WorkspaceSettingsError as exc:\n    assert 'Timed out' in str(exc), exc\nwith ini._profile_lock(path.with_name('another-profile.ini')):\n    pass\n"
            env = dict(os.environ, PYTHONPATH=str(Path(ini.__file__).resolve().parents[2]))
            with patch.object(ini, "_profile_ini", return_value=profile):
                for version in ((8, 1, 6, 49278), (8, 1, 7, 49382)):
                    with self.subTest(version=version), patch.object(ini, "_file_version", return_value=version):
                        with ini.preserve_workspace_settings(fake, restore=False):
                            subprocess.run([sys.executable, "-c", child, str(profile)], check=True, timeout=10,
                                           env=env)
            with ini._profile_lock(profile): self.assertFalse(profile.exists())
class CliTransientRetryAndResourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        for sub in ("Projects", "Models", "Results", "UserHourly", "ComposPV/PVmodules", "ComposPV/Inverters", "ComposPV/Batteries"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.fake_cli = Path(self._tmp.name) / "PVsystCLI.exe"
        self.fake_cli.write_bytes(b"stub")
        (self.root / "Projects" / "Demo.PRJ").write_text("PVObject_=pvProjet\nEnd of PVObject pvProjet\n", encoding="utf-8")
        (self.root / "Projects" / "Demo.VC0").write_text("PVObject_=pvVCalcul\nEnd of PVObject pvVCalcul\n", encoding="utf-8")
        (self.root / "Models" / "sim.SFI").write_text("SFI", encoding="utf-8")
    def test_transient_abort_classifier_matrix(self) -> None:
        self.assertTrue(_is_transient_simulation_abort("Checking license...\nLoading project Demo.PRJ...\nChecking va"))
        self.assertTrue(_is_transient_simulation_abort(""))
        self.assertFalse(_is_transient_simulation_abort("Checking license...\nNo license or license expired"))
        self.assertFalse(_is_transient_simulation_abort("Checking variant ...\nSimulation cannot be run.\nGrid storage: The selected battery is in error."))
        self.assertFalse(_is_transient_simulation_abort("Error: Invalid command syntax"))
        self.assertFalse(_is_transient_simulation_abort("Simulation done in 0min 3sec"))
    def test_run_simulation_retries_transient_abort_and_never_retries_fatal(self) -> None:
        cli = PVsystCLI(self.fake_cli, self.root, max_transient_retries=1)
        cli._help_cache["run-simulation"] = (
            "  --workspace|-w\n"
            "  --project|-p\n"
            "  --variant|-v\n"
            "  --input-sfi-file|-isf\n"
            "  --output-csv-file|-ocf\n"
            "  --log-level|-ll\n"
        )
        out_csv = self.root / "Results" / "retry_ok.csv"
        calls = {"n": 0}
        def _fake_checked_run(label, *args, allow_window_creation=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(
                    args, 1,
                    stdout="Checking license...\nLoading project Demo.PRJ...\nChecking va",
                    stderr="",
                )
            out_csv.write_text("date;E_Grid\n;kW\n01/01/24 00:00;0\n", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="Simulation done in 0min 2sec\n", stderr="")
        with patch.object(cli, "_checked_run", side_effect=_fake_checked_run), patch("pvsyst_auto.native.cli.time.sleep"):
            res = cli.run_simulation("Demo.PRJ", "VC0", sfi=self.root / "Models" / "sim.SFI", out_csv=out_csv, log_level=3)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(res["transient_retries"], 1)
        self.assertEqual(len(res["transient_retry_evidence"]), 1)
        # Fatal license error must NEVER retry
        calls_lic = {"n": 0}
        def _fake_lic(label, *args, allow_window_creation=None):
            calls_lic["n"] += 1
            return subprocess.CompletedProcess(args, 1, stdout="Checking license...\nNo license or license expired\n", stderr="")
        with patch.object(cli, "_checked_run", side_effect=_fake_lic), patch("pvsyst_auto.native.cli.time.sleep"):
            with self.assertRaisesRegex(PVsystError, "No license"):
                cli.run_simulation("Demo.PRJ", "VC0", sfi=self.root / "Models" / "sim.SFI", out_csv=self.root / "Results" / "lic.csv", log_level=3)
        self.assertEqual(calls_lic["n"], 1)
        # Exhausted transient retries reports retry count clearly
        calls_exh = {"n": 0}
        def _fake_abort(label, *args, allow_window_creation=None):
            calls_exh["n"] += 1
            return subprocess.CompletedProcess(args, 1, stdout="Checking license...\nChecking va", stderr="")
        with patch.object(cli, "_checked_run", side_effect=_fake_abort), patch("pvsyst_auto.native.cli.time.sleep"):
            with self.assertRaisesRegex(PVsystError, r"after 1 transient retry"):
                cli.run_simulation("Demo.PRJ", "VC0", sfi=self.root / "Models" / "sim.SFI", out_csv=self.root / "Results" / "exh.csv", log_level=3)
        self.assertEqual(calls_exh["n"], 2)
    def test_query_commands_retry_cold_start_empty_output_and_respect_deterministic_errors(self) -> None:
        cli = PVsystCLI(self.fake_cli, self.root, max_transient_retries=1)
        # license_info retries exit-0 empty stdout and succeeds on 2nd attempt
        lic_calls = {"n": 0}
        def fake_lic(label, *args, allow_window_creation=None):
            lic_calls["n"] += 1
            if lic_calls["n"] == 1:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="Status: LICENSED\nRemaining days: 120\n", stderr="")
        with patch.object(cli, "_checked_run", side_effect=fake_lic), patch("pvsyst_auto.native.cli.time.sleep") as mock_sleep:
            info = cli.license_info()
        self.assertEqual(lic_calls["n"], 2)
        mock_sleep.assert_called_once()
        self.assertEqual(info, {"status": "LICENSED", "remaining_days": 120})
        # deterministic license error must never retry
        det_lic_calls = {"n": 0}
        def fake_det_lic(label, *args, allow_window_creation=None):
            det_lic_calls["n"] += 1
            return subprocess.CompletedProcess(args, 0, stdout="No license or license expired\n", stderr="")
        with patch.object(cli, "_checked_run", side_effect=fake_det_lic), patch("pvsyst_auto.native.cli.time.sleep"):
            with self.assertRaisesRegex(PVsystError, "no recognized fields"):
                cli.license_info()
        self.assertEqual(det_lic_calls["n"], 1)
        # version retries empty output once, then succeeds
        ver_calls = {"n": 0}
        def fake_ver(label, *args, allow_window_creation=None):
            ver_calls["n"] += 1
            if ver_calls["n"] == 1:
                return subprocess.CompletedProcess(args, 0, stdout="   \n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="PVsystCLI 8.1.7\n", stderr="")
        with patch.object(cli, "_checked_run", side_effect=fake_ver), patch("pvsyst_auto.native.cli.time.sleep"):
            self.assertEqual(cli.version(), "PVsystCLI 8.1.7")
        self.assertEqual(ver_calls["n"], 2)
        # help / capabilities retries empty stdout once and caches only valid text; Unknown command never retries
        help_calls = {"n": 0}
        def fake_help(label, *args, allow_window_creation=None):
            help_calls["n"] += 1
            if args == ("help", "bogus"):
                return subprocess.CompletedProcess(args, 0, stdout="Unknown command: bogus", stderr="")
            if help_calls["n"] == 1:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="  --output-csv-file|-ocf: CSV output\n", stderr="")
        with patch.object(cli, "_checked_run", side_effect=fake_help), patch("pvsyst_auto.native.cli.time.sleep"):
            caps = cli.capabilities("run-simulation")
            self.assertEqual(caps, {"run-simulation": ["output-csv-file"]})
            self.assertEqual(help_calls["n"], 2)
            # cached hit makes no extra call
            self.assertEqual(cli.capabilities("run-simulation"), {"run-simulation": ["output-csv-file"]})
            self.assertEqual(help_calls["n"], 2)
            with self.assertRaisesRegex(PVsystError, "CLI does not support bogus"):
                cli.command_help("bogus")
            self.assertEqual(help_calls["n"], 3)
            self.assertNotIn("bogus", cli._help_cache)
        # max_transient_retries=0 disables retries on empty stdout
        cli_zero = PVsystCLI(self.fake_cli, self.root, max_transient_retries=0)
        with patch.object(cli_zero, "_checked_run", return_value=subprocess.CompletedProcess([], 0, "", "")) as mock_run:
            with self.assertRaisesRegex(PVsystError, "no output"):
                cli_zero.version()
        self.assertEqual(mock_run.call_count, 1)
    def test_public_simulation_stops_if_partial_output_cannot_be_removed(self) -> None:
        from pvsyst_auto.api.simulation import pvsyst_run_simulation
        cli = PVsystCLI(self.fake_cli, self.root, max_transient_retries=1)
        out_csv = self.root / "Results" / "locked.csv"
        calls = []
        def aborted(label, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                out_csv.write_text("partial", encoding="utf-8")
                return subprocess.CompletedProcess(args, 1, stdout="Loading project", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="Simulation done in 0min 1sec", stderr="")
        with patch("pvsyst_auto.api.simulation.cli", return_value=cli), \
             patch("pvsyst_auto.api.simulation.simulation_csv_path", return_value=out_csv), \
             patch.object(cli, "license_info", return_value={"status": "OK"}), \
             patch.object(cli, "_checked_run", side_effect=aborted), \
             patch("pvsyst_auto.native.cli.time.sleep"), \
             patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            with self.assertRaisesRegex(PVsystError, "partial.*locked"):
                pvsyst_run_simulation(project="Demo.PRJ", variant="VC0", csv_name="locked.csv", log_level=3)
        self.assertEqual(len(calls), 1)
    def test_dynamic_mcp_workspace_resources_and_new_prompts(self) -> None:
        from pvsyst_auto.api.context import mcp
        (self.root / "Results" / "report_a.html").write_text("<html></html>", encoding="utf-8")
        (self.root / "Results" / "run_a.csv").write_text("date;E_Grid\n", encoding="utf-8")
        (self.root / "Projects" / "Demo.VB1").write_text("PVObject_=pvVCalcul\n", encoding="utf-8")
        async def _check():
            resources = await mcp.list_resources()
            uris = {str(r.uri) for r in resources}
            for expected in (
                "pvsyst://guide/workflows",
                "pvsyst://guide/variables",
                "pvsyst://workspace/projects",
                "pvsyst://workspace/components",
                "pvsyst://workspace/results",
            ):
                self.assertIn(expected, uris)
            prompts = await mcp.list_prompts()
            p_names = {p.name for p in prompts}
            self.assertIn("ci_bess_feasibility_study", p_names)
            self.assertIn("optimize_bess_sizing", p_names)
            # Unconfigured workspace returns graceful configured=False
            with patch.dict(os.environ, {"PVSYST_WORKSPACE": ""}):
                raw = await mcp.read_resource("pvsyst://workspace/projects")
                payload = json.loads(raw[0].content)
                self.assertFalse(payload["configured"])
            # Configured workspace returns projects and results cleanly
            with patch.dict(os.environ, {"PVSYST_WORKSPACE": str(self.root)}):
                raw_proj = await mcp.read_resource("pvsyst://workspace/projects")
                proj_data = json.loads(raw_proj[0].content)
                self.assertTrue(proj_data["configured"])
                demo = next(p for p in proj_data["projects"] if p["project"] == "Demo.PRJ")
                self.assertIn("VC0", demo["variants"])
                self.assertIn("VB1", demo["variants"])
                raw_res = await mcp.read_resource("pvsyst://workspace/results")
                res_data = json.loads(raw_res[0].content)
                self.assertTrue(res_data["configured"])
                names = {f["name"] for f in res_data["files"]}
                self.assertIn("report_a.html", names)
                self.assertIn("run_a.csv", names)
                raw_comp = await mcp.read_resource("pvsyst://workspace/components")
                comp_data = json.loads(raw_comp[0].content)
                self.assertTrue(comp_data["configured"])
                self.assertIn("PAN", comp_data["components"])
        asyncio.run(_check())
class CapabilitiesLookupTests(unittest.TestCase):
    """Issue #277: optional single-command pvsyst_capabilities avoids enumerating all subcommands."""
    def test_single_command_lookup_caches_and_rejects_invalid_command(self) -> None:
        from pvsyst_auto.api.projects import pvsyst_capabilities
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            ws.mkdir()
            fake_exe = Path(tmp) / "PVsystCLI.exe"
            fake_exe.write_bytes(b"stub")
            client = PVsystCLI(fake_exe, ws)
            calls: list[list[str]] = []
            def fake_run(label, *args, allow_window_creation=None):
                calls.append(list(args))
                if args == ("version",): return subprocess.CompletedProcess(args, 0, stdout="PVsystCLI 8.1.7\n", stderr="")
                if args == ("help", "run-simulation"):
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        stdout="  --input-sfi-file|-isf: input\n  --output-csv-file|-ocf: output\n",
                        stderr="",
                    )
                raise AssertionError(f"Unexpected CLI invocation: {args!r}")
            with patch.object(client, "_checked_run", side_effect=fake_run), \
                 patch("pvsyst_auto.api.projects.cli", return_value=client):
                cold = pvsyst_capabilities(command="run-simulation")
                self.assertEqual(calls, [["help", "run-simulation"], ["version"]])
                self.assertEqual(cold["version"], "PVsystCLI 8.1.7")
                self.assertEqual(list(cold["commands"].keys()), ["run-simulation"])
                self.assertIn("input-sfi-file", cold["commands"]["run-simulation"])
                warm = client.capabilities("run-simulation")
                self.assertEqual(calls, [["help", "run-simulation"], ["version"]])
                self.assertEqual(warm, cold["commands"])
                warm_tool = pvsyst_capabilities(command="run-simulation")
                self.assertEqual(warm_tool, cold)
                self.assertEqual(len(calls), 3)
                for bad_cmd in ("", "bad cmd", "--help", "run_simulation"):
                    with self.assertRaises(ValueError): pvsyst_capabilities(command=bad_cmd)
                self.assertEqual(len(calls), 3)
if __name__ == "__main__": unittest.main()

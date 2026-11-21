"""Unit tests for v0.5.0 Phase 4: PVsystCLI transient retry hardening and dynamic MCP resources/prompts."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.native.cli import PVsystCLI, PVsystError, _is_transient_simulation_abort


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


if __name__ == "__main__":
    unittest.main()

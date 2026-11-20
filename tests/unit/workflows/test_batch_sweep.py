"""Unit tests for parametric batch sweep workflow (UserBatch CSV/RVT generation & ranking)."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from pvsyst_auto.workflows.batch import (
    BATCH_PARAMETERS,
    MAX_BATCH_SCENARIOS,
    render_batch_params_csv,
    render_batch_rvt,
    run_batch_sweep,
)


def _make_fake_cli(ws: Path):
    for sub in ("Projects", "UserBatch", "UserHourly", "Results"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    (ws / "Projects" / "Demo.PRJ").write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
    (ws / "Projects" / "Demo.VC0").write_text("PVObject_=pvVCalcul\nEnd of PVObject pvVCalcul\n", encoding="utf-8")
    cli = MagicMock()
    cli.workspace = ws
    cli._lock = MagicMock()
    cli._lock.__enter__ = MagicMock(return_value=None)
    cli._lock.__exit__ = MagicMock(return_value=False)
    cli.list_projects.return_value = ["Demo.PRJ"]
    cli.list_variants.return_value = ["VC0"]
    cli.license_info.return_value = {"status": "VALID", "remaining_executions": None}
    return cli


class BatchSweepUnitTests(unittest.TestCase):
    def test_render_batch_params_uses_exact_utf8_bom_crlf_and_headers(self):
        raw = render_batch_params_csv(
            project="Demo.PRJ",
            variant="VC0",
            param_keys=["plane_tilt", "backtracking", "soiling_loss_pct"],
            scenarios=[
                {
                    "id": "SIM_1",
                    "hourly_file": "",
                    "formatted": {"plane_tilt": "25", "backtracking": "Y", "soiling_loss_pct": "2"},
                    "comment": "Case1",
                }
            ],
        )
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\r\n", raw)
        self.assertIn(b"\r\n", raw)
        text = raw.decode("utf-8-sig")
        lines = text.splitlines()
        self.assertEqual(lines[11], "Ident;Create hourly;Plane;Backtraking;Soiling;Simul")
        self.assertEqual(lines[12], ";file;tilt;;Loss;Comment")
        self.assertEqual(lines[13], ";File name;[deg];Y/N;[%];")
        self.assertEqual(lines[15], "SIM_1;;25;Y;2;Case1")

    def test_render_batch_rvt_has_no_trailing_semicolon(self):
        rvt = render_batch_rvt(["GlobInc", "E_Grid", "PR"]).decode("utf-8")
        self.assertEqual(rvt, "  ResultVars=GlobInc;E_Grid;PR\r\n")

    def test_dry_run_cartesian_grid_writes_nothing_and_previews_scenarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "PVsyst8.1_Data"
            cli = _make_fake_cli(ws)
            res = run_batch_sweep(
                cli,
                project="Demo.PRJ",
                variant="VC0",
                sweep_name="TiltSoil",
                grid={"plane_tilt": [20, 30], "soiling_loss_pct": [1.0, 2.5]},
                dry_run=True,
            )
            self.assertEqual(res["status"], "ready")
            self.assertEqual(res["scenario_count"], 4)
            self.assertEqual([s["id"] for s in res["planned_scenarios"]], ["SIM_1", "SIM_2", "SIM_3", "SIM_4"])
            self.assertFalse((ws / "UserBatch" / "TiltSoil_BatchParams.CSV").exists())
            self.assertFalse((ws / "UserBatch" / "TiltSoil_BatchVariables.RVT").exists())
            cli.run_simulation.assert_not_called()

    def test_validation_rejects_grid_over_limit_unknown_param_and_ambiguous_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "PVsyst8.1_Data"
            cli = _make_fake_cli(ws)
            # Over 100 scenarios
            over = run_batch_sweep(
                cli, project="Demo.PRJ", variant="VC0", sweep_name="TooBig",
                grid={"plane_tilt": list(range(1, 15)), "plane_azimuth": list(range(0, 10))},
                dry_run=True,
            )
            self.assertEqual(over["status"], "blocked")
            self.assertTrue(any(str(MAX_BATCH_SCENARIOS) in msg for msg in over["issues"]))

            # Unknown parameter
            bad_param = run_batch_sweep(
                cli, project="Demo.PRJ", variant="VC0", sweep_name="BadKey",
                grid={"nonexistent_key": [1, 2]},
                dry_run=True,
            )
            self.assertEqual(bad_param["status"], "blocked")
            self.assertTrue(any("Unsupported batch parameter" in msg for msg in bad_param["issues"]))

            # Both grid and scenarios
            both = run_batch_sweep(
                cli, project="Demo.PRJ", variant="VC0", sweep_name="Both",
                grid={"plane_tilt": [20]},
                scenarios=[{"plane_tilt": 20}],
                dry_run=True,
            )
            self.assertEqual(both["status"], "blocked")

            # sort_by not in result_variables
            bad_sort = run_batch_sweep(
                cli, project="Demo.PRJ", variant="VC0", sweep_name="BadSort",
                grid={"plane_tilt": [20, 30]},
                result_variables=["GlobInc", "PR"],
                sort_by="E_Grid",
                dry_run=True,
            )
            self.assertEqual(bad_sort["status"], "blocked")

            # Explicit hourly_file equal to another scenario's default name
            clash = run_batch_sweep(
                cli, project="Demo.PRJ", variant="VC0", sweep_name="Roof",
                scenarios=[{"plane_tilt": 20}, {"plane_tilt": 35, "hourly_file": "roof_SIM_1.csv"}],
                export_hourly=True, dry_run=True,
            )
            self.assertEqual(clash["status"], "blocked")
            self.assertTrue(any("Duplicate scenario hourly_file" in msg for msg in clash["issues"]))

    def test_execute_batch_sweep_ranks_scenarios_and_handles_partial_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "PVsyst8.1_Data"
            cli = _make_fake_cli(ws)

            def fake_run(project, variant, *, batch_params, batch_rvt, **_):
                self.assertTrue(Path(batch_params).is_file())
                self.assertTrue(Path(batch_rvt).is_file())
                summary = ws / "UserBatch" / "Sweep1_BatchResults.CSV"
                lines = [
                    "PVsyst simulations Batch mode;;;;;;;;;",
                    "Ident;Create hourly;Plane;Soiling;Simul;Error;GlobInc;E_Grid;PR",
                    ";file;tilt;Loss;Comment;;kWh/m2;kWh;ratio",
                    "SIM_1;;20;2;Tilt20;Warning: minor;1500;13500;0.850",
                    "SIM_2;;30;1;Tilt30;;1560;14250;0.870",
                    "SIM_3;;85;1;Tilt85;Error: geometry invalid;;;",
                ]
                summary.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8-sig"))
                return {
                    "batch_summary": str(summary),
                    "batch_hourly": [],
                    "batch_runs": 3,
                    "seconds": 5,
                    "warnings": [],
                }

            cli.run_simulation.side_effect = fake_run

            res = run_batch_sweep(
                cli,
                project="Demo.PRJ",
                variant="VC0",
                sweep_name="Sweep1",
                scenarios=[
                    {"plane_tilt": 20, "soiling_loss_pct": 2.0, "comment": "Tilt20"},
                    {"plane_tilt": 30, "soiling_loss_pct": 1.0, "comment": "Tilt30"},
                    {"plane_tilt": 85, "soiling_loss_pct": 1.0, "comment": "Tilt85"},
                ],
                result_variables=["viGlobInc", "viE_Grid", "viPR"],
                sort_by="E_Grid",
                dry_run=False,
            )
            self.assertEqual(res["status"], "needs_review")
            self.assertTrue(res["native_consumption_verified"])
            self.assertEqual(res["completed_count"], 2)
            self.assertEqual(res["failed_count"], 1)
            self.assertEqual(res["best_scenario"]["id"], "SIM_2")
            self.assertEqual(res["best_scenario"]["rank"], 1)
            self.assertAlmostEqual(res["best_scenario"]["values"]["E_Grid"], 14250.0)
            self.assertAlmostEqual(res["best_scenario"]["delta_vs_baseline"]["delta"], 750.0)
            self.assertEqual(res["failed_scenarios"][0]["id"], "SIM_3")
            self.assertIn("plane_tilt", BATCH_PARAMETERS)


if __name__ == "__main__":
    unittest.main()

"""Consolidated workflow tests for variant optimization, batch sweeps, BESS sizing, and feasibility studies."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from pvsyst_auto.api.schemas import GridStorageUpdates
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.energy.storage import plan_tou_storage_schedule
from pvsyst_auto.native.cli import PVsystCLI
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.workflows.batch import (
    BATCH_PARAMETERS,
    MAX_BATCH_SCENARIOS,
    poll_batch_sweep_job,
    render_batch_params_csv,
    render_batch_rvt,
    run_batch_sweep,
)
from pvsyst_auto.workflows.bess_optimize import optimize_bess_sizing
from pvsyst_auto.workflows.feasibility import run_feasibility_study
from pvsyst_auto.workflows.optimize import optimize_variant


def _write_csv(path: Path, *, pr: float = 0.84, il_vmax: float = 0.0, il_pmax: float = 0.0, glob_shd: float = 0.0) -> Path:
    lines = [
        "Project;OptTest.PRJ",
        "Geographical Site;OptSite.SIT",
        "Weather data;OptMet.MET",
        "Simulation variant;OptTest.VC0",
        "date;GlobInc;EArray;E_Grid;PR;IL_Vmax;IL_Pmax;GlobHrz;GlobShd",
        ";W/m2;kW;kW;ratio;kW;kW;W/m2;W/m2",
    ]
    start = datetime(2024, 6, 1, 0, 0)
    for h in range(48):
        dt = start + timedelta(hours=h)
        gi = 700.0 if 7 <= dt.hour < 17 else 0.0
        ea = 80.0 * (gi / 1000.0) * pr if gi > 0 else 0.0
        eg = ea * 0.98 if ea > 0 else 0.0
        lines.append(
            f"{dt.strftime('%d/%m/%y %H:%M')};{gi:.2f};{ea:.3f};{eg:.3f};"
            f"{pr if gi > 0 else ''};{il_vmax if gi > 0 else 0.0};"
            f"{il_pmax if gi > 0 else 0.0};{gi:.2f};{gi - glob_shd if gi > 0 else 0.0}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _FakeOptCLI:
    def __init__(self, workspace: Path, store: VariantStore):
        self.workspace = workspace
        self.store = store
        self.calls: list[tuple[str, str]] = []

    def run_simulation(self, project, variant, *, sfi=None, out_csv=None, **kwargs):
        self.calls.append((project, variant))
        params = self.store.inspect_parameters(project, variant)
        n_ser = int(params["subarrays"][0]["values"]["NModSerie"])
        tilt = float(params["orientations"][0]["values"]["FieldTilt"])
        # Once NModSerie <= 21 and tilt <= 25, anomalies clear!
        il_vmax = 0.0 if n_ser <= 21 else 3.0
        glob_shd = 0.0 if tilt <= 25.0 else 150.0
        _write_csv(Path(out_csv), pr=0.84 if il_vmax == 0 and glob_shd == 0 else 0.62,
                   il_vmax=il_vmax, glob_shd=glob_shd)
        return {"returncode": 0, "warnings": [], "csv": str(out_csv)}


class OptimizeVariantTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for sub in ("Projects", "Models", "Results", "ComposPV/PVmodules", "ComposPV/Inverters"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.components = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.components)
        synth = synthesize_project_and_variant(
            None,
            project_comment="OptTest",
            variant_comment="Base",
            pan_name="Test.PAN",
            ond_name="Test.OND",
            n_mod_serie=24,
            n_string_ch=8,
            n_inverter=2,
            n_inv_mppt=4,
            independent_mppt=True,
            pnom_ac_in_mppt=25.0,
            tilt_deg=30.0,
            azimuth_deg=0.0,
        )
        self.store.create_project("OptTest.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
        self.baseline_bytes = (self.root / "Projects" / "OptTest.VC0").read_bytes()
        self.cli = _FakeOptCLI(self.root, self.store)

    def test_dry_run_preview_confirm_guard_and_bounded_convergence(self):
        _write_csv(self.root / "Results" / "base_bad.csv", pr=0.60, il_vmax=3.0, glob_shd=150.0)

        # 1. Default dry_run=True proposes patches + diff without creating any variant or calling CLI
        prev = optimize_variant(
            self.cli, self.store, project="OptTest.PRJ", variant="VC0",
            csv_name="base_bad.csv", dry_run=True,
        )
        self.assertEqual(prev["status"], "preview")
        self.assertTrue(prev["proposed_remediation"]["has_executable_patch"])
        self.assertIn("NModSerie", prev["proposed_remediation"]["subarray_updates"])
        self.assertIn("FieldTilt", prev["proposed_remediation"]["orientation_updates"])
        self.assertIn("---", prev["proposed_remediation"]["diff_preview"])
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.store.inspect_project("OptTest.PRJ")["variants"], ["VC0"])

        # 2. dry_run=False without confirm=True blocks safely
        blocked = optimize_variant(
            self.cli, self.store, project="OptTest.PRJ", variant="VC0",
            csv_name="base_bad.csv", dry_run=False, confirm=False,
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self.cli.calls, [])

        # 3. dry_run=False with confirm=True clones new variants, never mutates VC0, converges cleanly
        done = optimize_variant(
            self.cli, self.store, project="OptTest.PRJ", variant="VC0",
            csv_name="base_bad.csv", max_iterations=3, dry_run=False, confirm=True,
        )
        self.assertEqual(done["status"], "converged")
        self.assertTrue(done["converged"])
        self.assertEqual(len(done["iterations"]), 2)  # 24 -> 22 -> 20 clears overvoltage & 30->25 clears shading
        self.assertEqual(done["recommended_variant"], "VC2")
        # Baseline VC0 must remain byte-for-byte untouched!
        self.assertEqual((self.root / "Projects" / "OptTest.VC0").read_bytes(), self.baseline_bytes)

    def test_optimization_uses_valid_slot_after_vc9(self):
        for digit in range(1, 10):
            self.store.clone("OptTest.PRJ", "VC0", f"VC{digit}")
        _write_csv(self.root / "Results" / "base_bad.csv", pr=0.60, il_vmax=3.0)
        result = optimize_variant(
            self.cli, self.store, project="OptTest.PRJ", variant="VC0",
            csv_name="base_bad.csv", max_iterations=1, dry_run=False, confirm=True,
        )
        self.assertEqual(result["iterations"][0]["new_variant"], "VCA")
        self.assertEqual(self.cli.calls, [("OptTest.PRJ", "VCA")])
        self.assertEqual((self.root / "Projects" / "OptTest.VC0").read_bytes(), self.baseline_bytes)

    def test_optimization_uses_distinct_sfi_for_hour_and_subhour(self):
        sfis: list[Path] = []
        orig_run = self.cli.run_simulation

        def recording_run(project, variant, *, sfi=None, out_csv=None, **kwargs):
            sfis.append(Path(sfi))
            return orig_run(project, variant, sfi=sfi, out_csv=out_csv, **kwargs)

        self.cli.run_simulation = recording_run
        _write_csv(self.root / "Results" / "base_step.csv", pr=0.60, il_vmax=3.0)
        optimize_variant(
            self.cli, self.store, project="OptTest.PRJ", variant="VC0",
            csv_name="base_step.csv", max_iterations=1, time_step="hour", dry_run=False, confirm=True,
        )
        optimize_variant(
            self.cli, self.store, project="OptTest.PRJ", variant="VC0",
            csv_name="base_step.csv", max_iterations=1, time_step="subhour", dry_run=False, confirm=True,
        )
        self.assertEqual(len(sfis), 2)
        self.assertNotEqual(sfis[0].name, sfis[1].name)
        self.assertIn("SubHour", sfis[1].read_text(encoding="utf-8"))

    def test_clean_baseline_returns_no_action_needed(self):
        _write_csv(self.root / "Results" / "base_clean.csv", pr=0.84)
        res = optimize_variant(
            self.cli, self.store, project="OptTest.PRJ", variant="VC0",
            csv_name="base_clean.csv", dry_run=False, confirm=True,
        )
        self.assertEqual(res["status"], "no_action_needed")
        self.assertTrue(res["converged"])
        self.assertEqual(self.cli.calls, [])

    def test_optimize_blocks_irregular_cadence_uses_feasible_series_range_and_subhourly_kwh(self):
        from unittest.mock import patch
        from pvsyst_auto.workflows.optimize import propose_remediations

        # 1. Irregular cadence returns status='blocked' (not 'no_action_needed')
        gap_csv = self.root / "Results" / "gap_opt.csv"
        gap_csv.write_text(
            "PVSYST 8.1.7\n"
            "date;GlobInc;EArray;E_Grid;PR\n"
            ";W/m2;kW;kW;ratio\n"
            "01/06/24 08:00;500;0;0;0\n"
            "01/06/24 08:15;500;0;0;0\n"
            "01/06/24 08:45;500;0;0;0\n",
            encoding="utf-8",
        )
        blocked = optimize_variant(
            self.cli, self.store, project="OptTest.PRJ", variant="VC0",
            csv_name="gap_opt.csv", dry_run=True,
        )
        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assertFalse(blocked["converged"])
        self.assertTrue(blocked["issues"])

        # 2. Sub-hourly kW E_Grid reports energy_kwh (4 * 15min * 40 kW = 40 kWh, not 160)
        sub_csv = self.root / "Results" / "sub15_clean.csv"
        sub_csv.write_text(
            "PVSYST 8.1.7\n"
            "date;GlobInc;EArray;E_Grid;PR\n"
            ";W/m2;kW;kW;ratio\n"
            "01/06/24 10:00;800;42;40;0.84\n"
            "01/06/24 10:15;800;42;40;0.84\n"
            "01/06/24 10:30;800;42;40;0.84\n"
            "01/06/24 10:45;800;42;40;0.84\n",
            encoding="utf-8",
        )
        clean = optimize_variant(
            self.cli, self.store, project="OptTest.PRJ", variant="VC0",
            csv_name="sub15_clean.csv", dry_run=True,
        )
        self.assertEqual(clean["status"], "no_action_needed")
        self.assertAlmostEqual(clean["baseline"]["e_grid_sum"], 40.0, places=2)

        # 3. propose_remediations respects suggest_sizing recommended.feasible_series_range[1]
        fake_hint = {"status": "ready", "recommended": {"series_modules": 18, "feasible_series_range": [10, 25]}}
        diag_uv = {"findings": [{"code": "INVERTER_UNDERVOLTAGE", "severity": "high"}]}
        with patch("pvsyst_auto.workflows.optimize.suggest_sizing", return_value=fake_hint), \
                patch.object(self.store.components, "list", return_value={"items": [{"name": "Test.PAN", "specs": {"pnom_w": 440.0}}]}):
            prop = propose_remediations(self.store, "OptTest.PRJ", "VC0", diag_uv)
        # Current NModSerie=24; +10% would be 26, capped to feasible_series_range[1]=25
        self.assertEqual(prop["subarray_updates"]["NModSerie"], 25)


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

    def test_mppt_scaling_albedo_fraction_and_background_job_polling(self):
        import time

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "PVsyst8.1_Data"
            cli = _make_fake_cli(ws)
            # Independent-MPPT subarray: 350 inverters x 6 MPPT/inv = 2100 MPPTs
            (ws / "Projects" / "Demo.VC0").write_text(
                "PVObject_=pvVCalcul\n"
                "  PVObject_PVMainArray=pvMainArray\n"
                "    PVObject_=pvSubArray\n"
                "      SubArrayId=1\n"
                "      NInverter=350\n"
                "      NInvMPPT=2100\n"
                "    End of PVObject pvSubArray\n"
                "  End of PVObject pvMainArray\n"
                "End of PVObject pvVCalcul\n",
                encoding="utf-8",
            )
            # 1. inverter_count scales by 6x -> 2100, fractional albedo 0.20 -> 20%, >8 scenarios warns about background
            prev = run_batch_sweep(
                cli,
                project="Demo.PRJ",
                variant="VC0",
                sweep_name="MpptAlb",
                grid={"inverter_count": [300, 350, 400], "albedo": [0.20, 0.25, 0.30]},
                dry_run=True,
            )
            self.assertEqual(prev["status"], "ready", prev)
            self.assertEqual(prev["mppt_scaling"]["mppt_per_inverter"], 6)
            self.assertEqual(prev["planned_scenarios"][0]["formatted"]["inverter_count"], "1800")
            self.assertEqual(prev["planned_scenarios"][0]["formatted"]["albedo"], "20")
            self.assertTrue(any("MPPT" in w for w in prev["warnings"]), prev["warnings"])
            self.assertTrue(any("albedo" in w.lower() for w in prev["warnings"]), prev["warnings"])
            self.assertTrue(any("background=True" in w for w in prev["warnings"]), prev["warnings"])

            # 2. mppt_count passes raw MPPT total without scaling; combining both is blocked
            raw_mppt = run_batch_sweep(
                cli, project="Demo.PRJ", variant="VC0", sweep_name="RawMppt",
                grid={"mppt_count": [1800, 2100]}, dry_run=True,
            )
            self.assertEqual(raw_mppt["status"], "ready")
            self.assertEqual(raw_mppt["planned_scenarios"][0]["formatted"]["mppt_count"], "1800")
            self.assertNotIn("mppt_scaling", raw_mppt)

            both_keys = run_batch_sweep(
                cli, project="Demo.PRJ", variant="VC0", sweep_name="BothKeys",
                grid={"inverter_count": [350], "mppt_count": [2100]}, dry_run=True,
            )
            self.assertEqual(both_keys["status"], "blocked")

            # 3. background=True runs asynchronously and poll_batch_sweep_job retrieves ranked results
            def fake_bg_run(project, variant, *, batch_params, batch_rvt, **_):
                time.sleep(0.05)
                summary = ws / "UserBatch" / "BgSweep_BatchResults.CSV"
                lines = [
                    "PVsyst simulations Batch mode;;;;;;;;;",
                    "Ident;Create hourly;Plane;Simul;Error;GlobInc;E_Grid;PR",
                    ";file;tilt;Comment;;kWh/m2;kWh;ratio",
                    "SIM_1;;20;T20;;1500;13500;0.850",
                    "SIM_2;;30;T30;;1560;14250;0.870",
                ]
                summary.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8-sig"))
                return {"batch_summary": str(summary), "batch_hourly": [], "batch_runs": 2, "seconds": 1, "warnings": []}

            cli.run_simulation.side_effect = fake_bg_run
            started = run_batch_sweep(
                cli, project="Demo.PRJ", variant="VC0", sweep_name="BgSweep",
                grid={"plane_tilt": [20, 30]}, result_variables=["GlobInc", "E_Grid", "PR"],
                dry_run=False, background=True,
            )
            self.assertEqual(started["status"], "running")
            self.assertEqual(started["job_id"], "BgSweep")

            other = _make_fake_cli(ws / "other")
            self.assertEqual(poll_batch_sweep_job(other, "BgSweep")["status"], "not_found")
            (other.workspace / "UserBatch" / "BgSweep_job.json").write_text(
                '{"artifacts": {"expected_batch_summary": "missing-summary.csv"}}', encoding="utf-8"
            )
            self.assertEqual(poll_batch_sweep_job(other, "BgSweep")["status"], "unknown")
            polled = None
            for _ in range(40):
                polled = poll_batch_sweep_job(cli, "BgSweep")
                if polled["status"] != "running":
                    break
                time.sleep(0.05)
            assert polled is not None
            self.assertEqual(polled["status"], "completed", polled)
            self.assertEqual(polled["best_scenario"]["id"], "SIM_2")


def _china_ci_tou() -> list[dict]:
    return [
        {"name": "valley_night", "hours": [0, 1, 2, 3, 4, 5, 6, 7], "import_rate": 0.30, "export_rate": 0.30},
        {"name": "peak_morning", "hours": [8, 9, 10, 11], "import_rate": 1.15, "export_rate": 0.35},
        {"name": "midday_flat", "hours": [12, 13, 14, 15], "import_rate": 0.60, "export_rate": 0.35},
        {"name": "flat_afternoon", "hours": [16, 17], "import_rate": 0.75, "export_rate": 0.35},
        {"name": "sharp_evening", "hours": [18, 19, 20, 21], "import_rate": 1.35, "export_rate": 0.35},
        {"name": "late_flat", "hours": [22, 23], "import_rate": 0.65, "export_rate": 0.35},
    ]


def _write_pv_load_csv(path: Path, *, days: int = 30) -> Path:
    cols = [("GlobInc", "W/m2"), ("EArray", "kW"), ("E_Grid", "kW"), ("PR", "ratio"), ("E_User", "kW"), ("EFrGrid", "kW")]
    lines = [
        "Project;BessOpt.PRJ",
        "Geographical Site;Site.SIT",
        "Weather data;Met.MET",
        "Simulation variant;BessOpt.VC0",
        "date;" + ";".join(c for c, _ in cols),
        ";" + ";".join(u for _, u in cols),
    ]
    # Contiguous hourly timestamps starting Jan 1 so regular-cadence checks always pass
    start = datetime(2024, 1, 1, 0, 0)
    for step in range(max(1, days) * 24):
        dt = start + timedelta(hours=step)
        h = dt.hour
        gi = 800.0 if 9 <= h < 16 else (300.0 if h in (7, 8, 16, 17) else 0.0)
        pv = 120.0 * (gi / 1000.0) * 0.85 if gi > 0 else 0.0
        # Commercial load with sharp evening peak (18-21) and morning peak (8-11)
        if 18 <= h <= 21:
            load = 140.0
        elif 8 <= h <= 11:
            load = 110.0
        elif 12 <= h <= 17:
            load = 85.0
        else:
            load = 25.0
        imp = max(0.0, load - pv)
        lines.append(
            f"{dt.strftime('%d/%m/%y %H:%M')};{gi:.1f};{pv * 1.03:.3f};{pv:.3f};"
            f"{'0.85' if gi > 0 else ''};{load:.2f};{imp:.2f}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TouPlannerAndBessOptimizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for sub in ("Results", "UserHourly", "Models", "Projects"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tou_arbitrage_rejects_seasonal_or_day_specific_windows(self) -> None:
        from pvsyst_auto.api.schemas import EconomicsTariffConfig, EconomicsFinancialConfig
        from pvsyst_auto.api.simulation import pvsyst_optimize_bess_sizing

        csv_path = _write_pv_load_csv(self.root / "Results" / "seasonal.csv", days=2)
        for restriction in ({"months": [1, 2, 3]}, {"day_type": "weekend"}):
            with self.subTest(restriction=restriction):
                tariff = EconomicsTariffConfig(mode="tou", import_tariff=0.5, tou_schedule=[
                    {"name": "valley", "hours": list(range(8)), "import_rate": 0.1, **restriction},
                    {"name": "peak", "hours": list(range(16, 24)), "import_rate": 1.0},
                ])
                with patch("pvsyst_auto.api.simulation.cli") as mock_cli, patch("pvsyst_auto.api.simulation.variant_store"):
                    mock_cli.return_value.workspace = self.root
                    with self.assertRaisesRegex(ValueError, "month.*day"):
                        pvsyst_optimize_bess_sizing(
                            baseline_csv=csv_path.name, installed_dc_kw=120, bess_kwh_values=[100],
                            pcs_kw_values=[50], tariff=tariff,
                            financial=EconomicsFinancialConfig(fixed_capex=1000),
                            strategies=["tou_arbitrage"],
                        )

    def test_plan_tou_two_charge_two_discharge_and_schema_compat(self) -> None:
        plan = plan_tou_storage_schedule(
            tou_schedule=_china_ci_tou(),
            charge_power_kw=100.0,
            discharge_power_kw=100.0,
            usable_energy_kwh=200.0,
            cycles_per_day=2,
            round_trip_efficiency=0.88,
            min_spread=0.10,
        )
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(plan["planned_cycles_count"], 2)
        self.assertEqual(len(plan["charging_schedule_48"]), 48)
        self.assertEqual(len(plan["discharging_schedule_48"]), 48)

        # Charge and discharge slots must never overlap
        for ch, dis in zip(plan["charging_schedule_48"], plan["discharging_schedule_48"]):
            self.assertFalse(ch > 0 and dis > 0)

        # Cycle 1 charges in valley_night (0..7) and discharges in peak_morning (8..11)
        # Cycle 2 charges in midday_flat (12..15) and discharges in sharp_evening (18..21)
        c1, c2 = plan["cycles"]
        self.assertEqual(c1["charge_period"], "valley_night")
        self.assertEqual(c1["discharge_period"], "peak_morning")
        self.assertEqual(c2["charge_period"], "midday_flat")
        self.assertEqual(c2["discharge_period"], "sharp_evening")

        # Power capped so planned_discharge_kwh <= usable_energy_kwh * sqrt(rte)
        self.assertLessEqual(c1["planned_discharge_kwh"], 200.0)
        self.assertLessEqual(c2["planned_discharge_kwh"], 200.0)

        # Must validate cleanly against GridStorageUpdates (drop-in for PVsyst native storage!)
        validated = GridStorageUpdates.model_validate(plan["storage_updates"])
        self.assertEqual(validated.strategy, "power_shift")
        self.assertEqual(validated.discharging_strategy, "specified_hours")
        self.assertEqual(len(validated.charging_schedule_48), 48)

    def test_short_charge_window_limits_scheduled_discharge(self) -> None:
        plan = plan_tou_storage_schedule(
            tou_schedule=[{"name": "valley", "hours": [0], "import_rate": 0.1}],
            default_import_rate=0.8, charge_power_kw=10, discharge_power_kw=20,
            usable_energy_kwh=100, round_trip_efficiency=0.81, cycles_per_day=1,
        )
        charged = sum(plan["charging_schedule_48"]) / 2
        discharged = sum(plan["discharging_schedule_48"]) / 2
        self.assertEqual(charged, 10)
        self.assertAlmostEqual(discharged, 8.1, delta=0.023)
        self.assertLessEqual(discharged, charged * 0.81)

    def test_exact_spread_and_previous_evening_charge(self) -> None:
        for rates, spread, window in [([0.2]*12 + [0.3]*12, 0.1, range(12)),
                                      ([0.9]*8 + [1.0]*4 + [0.2]*12, 0.25, range(12, 24))]:
            with self.subTest(rates=rates):
                schedule = [{"hours": [h for h in range(24) if rates[h] == rate], "import_rate": rate}
                            for rate in sorted(set(rates))]
                plan = plan_tou_storage_schedule(tou_schedule=schedule, charge_power_kw=10,
                                                 cycles_per_day=1, round_trip_efficiency=1, min_spread=spread)
                self.assertEqual([cycle["charge_hours"] for cycle in plan["cycles"]], [list(window)])

    def test_public_stage2_allocates_available_variants(self) -> None:
        from pvsyst_auto.api.schemas import BessVerifyConfig, EconomicsFinancialConfig, EconomicsTariffConfig
        from pvsyst_auto.api.simulation import pvsyst_optimize_bess_sizing

        csv_path = _write_pv_load_csv(self.root / "Results" / "variant.csv", days=2)
        projects = self.root / "Projects"
        for suffix in ("VC0", "VC1", "VC3", "vc4"):
            (projects / f"Demo.{suffix}").write_text("stub", encoding="utf-8")
        store = MagicMock()
        store._path.return_value = projects / "Demo.PRJ"
        fake_cli = MagicMock(workspace=self.root)
        def fake_sim(project, variant, *, sfi, out_csv, log_level):
            _write_pv_load_csv(out_csv, days=2)
            return {"warnings": []}
        fake_cli.run_simulation.side_effect = fake_sim
        kwargs = dict(baseline_csv=csv_path.name, installed_dc_kw=100, bess_kwh_values=[100, 200],
                      pcs_kw_values=[50], tariff=EconomicsTariffConfig(mode="self_consumption_flat", import_tariff=1),
                      financial=EconomicsFinancialConfig(fixed_capex=1000), verify_top_k=2,
                      verify_config=BessVerifyConfig(project="Demo.PRJ", source_variant="VC0",
                          battery_file_name="Cell.BTR", battery_unit_kwh=100))
        with patch("pvsyst_auto.api.simulation.cli", return_value=fake_cli), \
             patch("pvsyst_auto.api.simulation.variant_store", return_value=store), \
             patch("pvsyst_auto.energy.system.create_grid_storage_system", return_value={"status": "created"}) as create:
            preview = pvsyst_optimize_bess_sizing(**kwargs)
            result = pvsyst_optimize_bess_sizing(**kwargs, dry_run=False, confirm=True)
            names = [p["new_variant"] for p in preview["stage2_verification"]["planned_variants"]]
            self.assertEqual(names, ["VC2", "VC5"])
            self.assertEqual([p["new_variant"] for p in result["stage2_verification"]["planned_variants"]], names)
            self.assertEqual([c.kwargs["new_variant"] for c in create.call_args_list], names)
            self.assertEqual([c.args[1] for c in fake_cli.run_simulation.call_args_list], names)
            kwargs["verify_config"].variant_prefix = "VC1"
            with self.assertRaisesRegex(ValueError, "three characters"):
                pvsyst_optimize_bess_sizing(**kwargs)

    def test_plan_tou_idle_when_spread_too_small(self) -> None:
        flat_tou = [
            {"name": "low", "hours": list(range(0, 12)), "import_rate": 0.50},
            {"name": "high", "hours": list(range(12, 24)), "import_rate": 0.55},
        ]
        plan = plan_tou_storage_schedule(
            tou_schedule=flat_tou,
            charge_power_kw=50.0,
            round_trip_efficiency=0.88,
            min_spread=0.05,
        )
        self.assertEqual(plan["status"], "idle")
        self.assertEqual(plan["planned_cycles_count"], 0)
        self.assertEqual(sum(plan["charging_schedule_48"]), 0.0)
        self.assertEqual(sum(plan["discharging_schedule_48"]), 0.0)
        self.assertTrue(plan["warnings"])

    def test_stage1_hybrid_bess_grid_pareto_and_heatmap(self) -> None:
        csv_path = _write_pv_load_csv(self.root / "Results" / "base_pv.csv", days=36)
        tariff = {
            "mode": "tou",
            "import_tariff": 0.70,
            "feed_in_tariff": 0.35,
            "tou_schedule": _china_ci_tou(),
            "demand_charge": {
                "billing_mode": "demand",
                "demand_rate_per_kw_month": 42.0,
            },
        }
        financial = {
            "currency": "CNY",
            "project_life_years": 20,
            "discount_rate_pct": 6.0,
            "capex_per_kw": 3500.0,
            "battery_capex_per_kwh": 950.0,
            "opex_pct_capex": 1.0,
            "battery_replacement_year": 12,
            "battery_replacement_cost_ratio": 0.6,
        }
        res = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=csv_path.name,
            installed_dc_kw=120.0,
            bess_kwh_values=[0.0, 100.0, 200.0, 300.0],
            pcs_kw_values=[50.0, 100.0],
            strategies=["self_consumption", "tou_arbitrage", "peak_shaving"],
            tariff=tariff,
            financial=financial,
            rank_by="npv",
            verify_top_k=0,
            dry_run=True,
        )
        self.assertEqual(res["status"], "completed")
        # 1 PV-only + 3 non-zero bess * 2 pcs * 3 strategies = 19 candidates
        self.assertEqual(res["evaluated_count"], 19)
        self.assertEqual(res["best"]["rank"], 1)
        # Pareto frontier has non-decreasing capex and strictly increasing npv
        pareto = res["pareto_frontier"]
        self.assertGreaterEqual(len(pareto), 1)
        for a, b in zip(pareto, pareto[1:]):
            self.assertLessEqual(a["capex_total"], b["capex_total"])
            self.assertGreater(b["npv"], a["npv"])

        # Heatmap dimensions match pcs_kw x nonzero bess_kwh
        hm = res["heatmap"]
        self.assertEqual(hm["bess_kwh"], [100.0, 200.0, 300.0])
        self.assertEqual(hm["pcs_kw"], [50.0, 100.0])
        self.assertEqual(len(hm["z"]), 2)
        self.assertEqual(len(hm["z"][0]), 3)

        # Peak shaving strategy achieves positive peak demand reduction
        ps_cands = [c for c in res["candidates"] if c["strategy"] == "peak_shaving"]
        self.assertTrue(any(c["avg_monthly_peak_reduction_kw"] > 0 for c in ps_cands))

    def test_stage2_dry_run_guard_and_mocked_cli_reconciliation(self) -> None:
        csv_path = _write_pv_load_csv(self.root / "Results" / "base_s2.csv", days=24)
        tariff = {"mode": "tou", "import_tariff": 0.7, "feed_in_tariff": 0.3, "tou_schedule": _china_ci_tou()}
        financial = {"project_life_years": 15, "discount_rate_pct": 6.0, "capex_per_kw": 3500.0, "battery_capex_per_kwh": 900.0}
        v_cfg = {
            "project": "Demo.PRJ",
            "source_variant": "VC0",
            "battery_file_name": "LFP_100kWh.BTR",
            "battery_unit_kwh": 100.0,
            "variant_prefix": "VC",
        }

        base_opt = {
            "workspace": self.root,
            "baseline_csv": csv_path.name,
            "installed_dc_kw": 120.0,
            "bess_kwh_values": [100.0, 200.0],
            "pcs_kw_values": [50.0],
            "tariff": tariff,
            "financial": financial,
            "verify_config": v_cfg,
        }

        # dry_run=True plans Stage 2 without launching CLI
        plan = optimize_bess_sizing(**base_opt, verify_top_k=2, dry_run=True)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["stage2_verification"]["status"], "ready_for_cli_verification")
        self.assertEqual(len(plan["stage2_verification"]["planned_variants"]), 2)

        # dry_run=False without confirm=True must fail closed
        with self.assertRaisesRegex(ValueError, "confirm=True"):
            optimize_bess_sizing(**base_opt, verify_top_k=1, dry_run=False, confirm=False)

        # Mocked Stage 2 execution reconciles fast model vs PVsyst exact CSV
        fake_cli = MagicMock()
        fake_cli.workspace = self.root

        def _fake_sim(project, variant, *, sfi, out_csv, log_level=3):
            _write_pv_load_csv(Path(out_csv), days=24)
            return {"returncode": 0, "warnings": []}

        fake_cli.run_simulation.side_effect = _fake_sim
        fake_store = MagicMock()

        with patch("pvsyst_auto.energy.system.create_grid_storage_system", return_value={"status": "created"}) as mock_create:
            verified = optimize_bess_sizing(
                **base_opt, cli=fake_cli, store=fake_store, verify_top_k=1, dry_run=False, confirm=True,
            )
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["stage2_verification"]["status"], "verified")
        self.assertEqual(mock_create.call_count, 1)
        comps = verified["stage2_verification"]["verified_comparisons"]
        self.assertEqual(len(comps), 1)
        self.assertIn("npv_diff", comps[0]["deltas"])

        # Re-running Stage 2 with same prefix + peak_shaving + pcs_capex_per_kw must not crash
        # on existing SFI, must omit load_manifest for peak_shaving, and must include PCS CAPEX
        v_cfg_ps = dict(v_cfg, load_manifest_name="load_manifest.json")
        with patch("pvsyst_auto.energy.system.create_grid_storage_system", return_value={"status": "created"}) as mock_create_ps:
            verified_ps = optimize_bess_sizing(
                workspace=self.root,
                cli=fake_cli,
                store=fake_store,
                baseline_csv=csv_path.name,
                installed_dc_kw=120.0,
                bess_kwh_values=[100.0],
                pcs_kw_values=[50.0],
                strategies=["peak_shaving"],
                tariff=tariff,
                financial=financial,
                pcs_capex_per_kw=600.0,
                verify_top_k=1,
                verify_config=v_cfg_ps,
                dry_run=False,
                confirm=True,
            )
        self.assertEqual(verified_ps["status"], "verified")
        self.assertIsNone(mock_create_ps.call_args.kwargs["load_manifest_name"])
        # 120*3500 + 100*900 + 50*600 = 540,000
        self.assertEqual(verified_ps["stage2_verification"]["verified_comparisons"][0]["capex_total"], 540_000.0)

    def test_public_stage2_stops_on_blocked_storage_creation(self) -> None:
        from pvsyst_auto.api.schemas import BessVerifyConfig, EconomicsFinancialConfig, EconomicsTariffConfig
        from pvsyst_auto.api.simulation import pvsyst_optimize_bess_sizing

        csv_path = _write_pv_load_csv(self.root / "Results" / "blocked.csv", days=2)
        fake_cli = MagicMock(workspace=self.root)
        fake_cli.run_simulation.side_effect = RuntimeError("unexpected simulation")
        with patch("pvsyst_auto.api.simulation.cli", return_value=fake_cli), \
             patch("pvsyst_auto.api.simulation.variant_store"), \
             patch("pvsyst_auto.energy.system.create_grid_storage_system", return_value={
                 "status": "blocked", "issues": ["battery missing"]}):
            with self.assertRaisesRegex(ValueError, "battery missing"):
                pvsyst_optimize_bess_sizing(
                    baseline_csv=csv_path.name, installed_dc_kw=100, bess_kwh_values=[100], pcs_kw_values=[50],
                    tariff=EconomicsTariffConfig(mode="self_consumption_flat", import_tariff=1),
                    financial=EconomicsFinancialConfig(fixed_capex=1000), verify_top_k=1,
                    verify_config=BessVerifyConfig(project="Demo.PRJ", source_variant="VC0",
                        battery_file_name="Cell.BTR", battery_unit_kwh=100, variant_prefix="VC"),
                    dry_run=False, confirm=True,
                )
        fake_cli.run_simulation.assert_not_called()

    def test_tou_wraparound_no_load_sc_totals_self_cons_pv_reconstruction_and_irregular_cadence(self) -> None:
        # 1. TOU planner must not schedule daytime charge after segment-0 peak when a later peak exists
        wrap_tou = [
            {"name": "midnight_peak", "hours": [0, 1], "import_rate": 1.50, "export_rate": 0.30},
            {"name": "morning_valley", "hours": [2, 3, 4, 5, 6, 7], "import_rate": 0.30, "export_rate": 0.30},
            {"name": "day_flat", "hours": [8, 9, 10, 11, 12, 13, 14, 15, 16, 17], "import_rate": 0.70, "export_rate": 0.30},
            {"name": "evening_peak", "hours": [18, 19, 20, 21, 22, 23], "import_rate": 1.50, "export_rate": 0.30},
        ]
        plan = plan_tou_storage_schedule(tou_schedule=wrap_tou, charge_power_kw=50.0, cycles_per_day=2)
        for cyc in plan["cycles"]:
            # Segment-0 peak [0, 1] must never be paired with daytime hours [2..7] after it
            if cyc["discharge_hours"] == [0, 1]:
                self.assertTrue(all(h >= 24 for h in cyc["charge_hours"]))  # impossible -> not paired
        # Evening peak [18..23] should get the cheapest preceding window [2..7]
        ev_cycles = [c for c in plan["cycles"] if c["discharge_hours"] == [18, 19, 20, 21, 22, 23]]
        self.assertEqual(len(ev_cycles), 1)
        self.assertEqual(ev_cycles[0]["charge_hours"], [2, 3, 4, 5, 6, 7])

        # 2. No-load self_consumption_ratio must reflect in self_consumed_kwh, self_consumption_pct, and pv_export_kwh
        pv_only_csv = self.root / "Results" / "pv_only_sc.csv"
        pv_only_csv.write_text(
            "PVSYST 8.1.7\ndate;E_Grid\n;kWh\n"
            "01/06/24 12:00;100.0\n01/06/24 13:00;100.0\n",
            encoding="utf-8",
        )
        res_sc = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=pv_only_csv.name,
            installed_dc_kw=100.0,
            bess_kwh_values=[0.0],
            pcs_kw_values=[50.0],
            tariff={"mode": "self_consumption_flat", "import_tariff": 0.8, "feed_in_tariff": 0.3, "self_consumption_ratio": 0.6},
            financial={"fixed_capex": 1000.0},
        )
        self.assertAlmostEqual(res_sc["candidates"][0]["self_consumption_pct"], 60.0, places=2)

        # 3. True PVsyst self-consumption CSV (EToGrid + EFrGrid + E_User, negative net E_Grid) reconstructs total PV
        real_sc_csv = self.root / "Results" / "real_sc.csv"
        real_sc_csv.write_text(
            "PVSYST 8.1.7\ndate;E_Grid;EToGrid;EFrGrid;E_User\n;kWh;kWh;kWh;kWh\n"
            "01/06/24 12:00;20;20;0;60\n01/06/24 13:00;-80;0;80;80\n",
            encoding="utf-8",
        )
        res_real = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=real_sc_csv.name,
            installed_dc_kw=100.0,
            bess_kwh_values=[0.0],
            pcs_kw_values=[50.0],
            tariff={"mode": "self_consumption_flat", "import_tariff": 0.8, "feed_in_tariff": 0.3},
            financial={"fixed_capex": 1000.0},
        )
        # Total PV = 20 export + 60 direct self-consumption = 80 kWh -> self_consumption_pct = 75%
        self.assertAlmostEqual(res_real["candidates"][0]["self_consumption_pct"], 75.0, places=2)

        # 4. Irregular cadence baseline CSV is rejected instead of falling back to 60 minutes
        gap_csv = self.root / "Results" / "bess_gap.csv"
        gap_csv.write_text(
            "PVSYST 8.1.7\ndate;E_Grid\n;kW\n"
            "01/06/24 08:00;10\n01/06/24 08:15;10\n01/06/24 08:45;10\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "regular cadence"):
            optimize_bess_sizing(
                workspace=self.root,
                baseline_csv=gap_csv.name,
                installed_dc_kw=100.0,
                bess_kwh_values=[0.0],
                pcs_kw_values=[50.0],
                tariff={"mode": "feed_in", "feed_in_tariff": 0.4},
                financial={"fixed_capex": 1000.0},
            )

        # 5. Incompatible unit on E_Grid must raise ValueError instead of valuing PV at 0 kW
        bad_unit_csv = self.root / "Results" / "bess_bad_unit.csv"
        bad_unit_csv.write_text(
            "PVSYST 8.1.7\ndate;E_Grid\n;%\n01/06/24 08:00;10\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "E_Grid.*%"):
            optimize_bess_sizing(
                workspace=self.root,
                baseline_csv=bad_unit_csv.name,
                installed_dc_kw=100.0,
                bess_kwh_values=[0.0],
                pcs_kw_values=[50.0],
                tariff={"mode": "feed_in", "feed_in_tariff": 0.4},
                financial={"fixed_capex": 1000.0},
            )

        # 6. Grid-charged TOU arbitrage BESS must not inflate PV self_consumption_pct above 100%
        small_pv_csv = self.root / "Results" / "small_pv_large_load.csv"
        rows = ["PVSYST 8.1.7", "date;E_Grid;E_User", ";kW;kW"]
        start = datetime(2024, 1, 1, 0, 0)
        for step in range(48):
            dt = start + timedelta(hours=step)
            pv_val = 5.0 if 10 <= dt.hour <= 14 else 0.0
            rows.append(f"{dt.strftime('%d/%m/%y %H:%M')};{pv_val:.1f};80.0")
        small_pv_csv.write_text("\n".join(rows) + "\n", encoding="utf-8")
        res_grid_ch = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=small_pv_csv.name,
            installed_dc_kw=10.0,
            bess_kwh_values=[200.0],
            pcs_kw_values=[50.0],
            strategies=["tou_arbitrage"],
            tariff={"mode": "tou", "import_tariff": 0.7, "feed_in_tariff": 0.3, "tou_schedule": _china_ci_tou()},
            financial={"fixed_capex": 1000.0, "battery_capex_per_kwh": 900.0},
        )
        cand = res_grid_ch["candidates"][0]
        self.assertGreater(cand["battery_discharge_kwh"], 0.0)
        self.assertLessEqual(cand["self_consumption_pct"], 100.0)

    def test_full_year_months_allowed_and_stage2_unique_csv_and_prefix_preflight(self) -> None:
        from pvsyst_auto.api.schemas import BessVerifyConfig, EconomicsFinancialConfig, EconomicsTariffConfig
        from pvsyst_auto.api.simulation import pvsyst_optimize_bess_sizing

        # 1. Explicit full-year months=[1..12] is accepted by BESS TOU dispatch
        csv_path = _write_pv_load_csv(self.root / "Results" / "full_months.csv", days=2)
        full_year_tou = [dict(p, months=list(range(1, 13))) for p in _china_ci_tou()]
        res_full = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=csv_path.name,
            installed_dc_kw=100.0,
            bess_kwh_values=[100.0],
            pcs_kw_values=[50.0],
            strategies=["tou_arbitrage"],
            tariff={"mode": "tou", "import_tariff": 0.7, "feed_in_tariff": 0.3, "tou_schedule": full_year_tou},
            financial={"fixed_capex": 1000.0, "battery_capex_per_kwh": 900.0},
        )
        self.assertEqual(len(res_full["candidates"]), 1)

        # 2. Explicit variant_prefix detects occupied variant on disk during dry_run=True
        proj_dir = self.root / "Projects"
        proj_dir.mkdir(exist_ok=True)
        (proj_dir / "Demo.VC1").write_text("existing\n", encoding="utf-8")
        fake_cli = MagicMock(workspace=self.root)
        with patch("pvsyst_auto.api.simulation.cli", return_value=fake_cli), \
             patch("pvsyst_auto.api.simulation.variant_store"):
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                pvsyst_optimize_bess_sizing(
                    baseline_csv=csv_path.name, installed_dc_kw=100, bess_kwh_values=[100], pcs_kw_values=[50],
                    tariff=EconomicsTariffConfig(mode="self_consumption_flat", import_tariff=1),
                    financial=EconomicsFinancialConfig(fixed_capex=1000), verify_top_k=1,
                    verify_config=BessVerifyConfig(project="Demo.PRJ", source_variant="VC0",
                        battery_file_name="Cell.BTR", battery_unit_kwh=100, variant_prefix="VC"),
                    dry_run=True,
                )

        # 3. Auto-allocated Stage 2 variant names output CSV with new_var so reruns never collide
        from pvsyst_auto.components.store import ComponentStore
        from pvsyst_auto.project.variants import VariantStore
        (proj_dir / "Demo2.PRJ").write_text("PVObject_=pvProject\n", encoding="utf-8")
        (proj_dir / "Demo2.VC0").write_text("PVObject_=pvVariant\n", encoding="utf-8")
        (proj_dir / "Demo2.VC1").write_text("PVObject_=pvVariant\n", encoding="utf-8")
        recorded_csvs: list[str] = []

        def fake_run(project, variant, *, out_csv, **kwargs):
            recorded_csvs.append(Path(out_csv).name)
            _write_pv_load_csv(Path(out_csv), days=2)
            return {}

        fake_cli2 = MagicMock(workspace=self.root)
        fake_cli2.run_simulation.side_effect = fake_run
        with patch("pvsyst_auto.api.simulation.cli", return_value=fake_cli2), \
             patch("pvsyst_auto.api.simulation.variant_store", return_value=VariantStore(self.root, ComponentStore(self.root))), \
             patch("pvsyst_auto.energy.system.create_grid_storage_system", return_value={"status": "created"}):
            res2 = pvsyst_optimize_bess_sizing(
                baseline_csv=csv_path.name, installed_dc_kw=100, bess_kwh_values=[100], pcs_kw_values=[50],
                tariff=EconomicsTariffConfig(mode="self_consumption_flat", import_tariff=1),
                financial=EconomicsFinancialConfig(fixed_capex=1000), verify_top_k=1,
                verify_config=BessVerifyConfig(project="Demo2.PRJ", source_variant="VC0",
                    battery_file_name="Cell.BTR", battery_unit_kwh=100),
                dry_run=False, confirm=True,
            )
        self.assertEqual(res2["stage2_verification"]["verified_comparisons"][0]["variant"], "VC2")
        self.assertEqual(recorded_csvs, ["VC2_100kWh_50kW.csv"])


class FeasibilityStudyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        for sub in ("Projects", "Models", "Results", "UserHourly", "ComposPV/PVmodules", "ComposPV/Inverters", "ComposPV/Batteries"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.fake_cli_exe = Path(self._tmp.name) / "PVsystCLI.exe"
        self.fake_cli_exe.write_bytes(b"stub")
        self.cli = PVsystCLI(self.fake_cli_exe, self.root)
        self.comp = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.comp)
        self.base_csv = _write_pv_load_csv(self.root / "Results" / "suzhou_base.csv", days=36)

    def _sample_spec(self) -> dict:
        return {
            "study_name": "Suzhou_120kW_CI",
            "existing_csv": self.base_csv.name,
            "installed_dc_kw": 120.0,
            "synthesize_components": [
                {
                    "component_type": "BTR",
                    "new_name": "Study_LFP200.BTR",
                    "manufacturer": "CellPower",
                    "model": "LFP-51V200Ah",
                    "params": {
                        "chemistry": "LFP",
                        "capacity_ah": 200.0,
                        "nominal_voltage_v": 51.2,
                        "n_cells_s": 16,
                    },
                }
            ],
            "auto_tou_schedule": True,
            "bess_sizing": {
                "bess_kwh_values": [0.0, 100.0, 200.0],
                "pcs_kw_values": [50.0, 100.0],
                "strategies": ["tou_arbitrage", "peak_shaving"],
                "rank_by": "npv",
                "verify_top_k": 0,
            },
            "tariff": {
                "mode": "tou",
                "import_tariff": 0.70,
                "feed_in_tariff": 0.35,
                "tou_schedule": _china_ci_tou(),
                "demand_charge": {
                    "billing_mode": "demand",
                    "demand_rate_per_kw_month": 42.0,
                },
            },
            "financial": {
                "currency": "CNY",
                "project_life_years": 20,
                "discount_rate_pct": 6.0,
                "capex_per_kw": 3500.0,
                "battery_capex_per_kwh": 950.0,
                "opex_pct_capex": 1.0,
                "battery_replacement_year": 12,
                "battery_replacement_cost_ratio": 0.6,
            },
            "sensitivity": {"enabled": True, "variation_pct": 15.0},
            "report": {
                "title": "苏州 120kW 工商业光储一站式可研报告",
                "output_name": "suzhou_feasibility.html",
                "language": "zh",
                "plotlyjs": "cdn",
            },
        }

    def test_dry_run_writes_nothing_and_confirm_guard(self) -> None:
        spec = self._sample_spec()
        prev = run_feasibility_study(self.cli, self.store, spec=spec, dry_run=True)
        self.assertEqual(prev["status"], "ready")
        self.assertTrue(prev["dry_run"])
        self.assertFalse((self.root / "ComposPV" / "Batteries" / "Study_LFP200.BTR").exists())
        self.assertFalse((self.root / "Results" / "suzhou_feasibility.html").exists())
        self.assertEqual(prev["tou_schedule"]["planned_cycles_count"], 2)
        self.assertIn("recommended_bess", prev["executive_summary"])

        # dry_run=False without confirm=True must fail closed
        with self.assertRaisesRegex(ValueError, "confirm=True"):
            run_feasibility_study(self.cli, self.store, spec=spec, dry_run=False, confirm=False)

    def test_full_end_to_end_execution_and_mcp_tool_roundtrip(self) -> None:
        spec = self._sample_spec()
        res = run_feasibility_study(self.cli, self.store, spec=spec, dry_run=False, confirm=True)
        self.assertIn(res["status"], ("completed", "needs_review"))
        self.assertTrue((self.root / "ComposPV" / "Batteries" / "Study_LFP200.BTR").is_file())
        rep_path = Path(res["report"]["report_path"])
        self.assertTrue(rep_path.is_file())
        self.assertEqual(res["report"]["chart_count"], 7)
        html_txt = rep_path.read_text(encoding="utf-8")
        self.assertIn("苏州 120kW 工商业光储一站式可研报告", html_txt)
        self.assertIn("两部制月度最大需量削峰对比", html_txt)
        self.assertIn("财务敏感性分析龙卷风图", html_txt)
        self.assertIn("光储容量寻优收益热力图", html_txt)
        self.assertIsNotNone(res["executive_summary"]["npv"])
        self.assertIsNotNone(res["executive_summary"]["recommended_bess"])

        # Verify MCP tool boundary accepts strict FeasibilityStudyRequest
        import pvsyst_auto.api.engineering as api_eng
        from pvsyst_auto.api.schemas import FeasibilityStudyRequest

        spec2 = self._sample_spec()
        spec2["report"]["output_name"] = "suzhou_feasibility_mcp.html"
        with patch.dict(os.environ, {"PVSYST_WORKSPACE": str(self.root), "PVSYST_CLI": str(self.fake_cli_exe)}):
            mcp_res = api_eng.pvsyst_run_feasibility_study(
                spec=FeasibilityStudyRequest.model_validate(spec2),
                dry_run=True,
            )
        self.assertEqual(mcp_res["status"], "ready")

    def test_blocked_diagnostics_and_explicit_none_config_fields(self) -> None:
        # Irregular CSV -> diagnostics blocked -> study status must be 'blocked', not 'completed'
        gap_csv = self.root / "Results" / "gap_study.csv"
        gap_csv.write_text(
            "PVSYST 8.1.7\ndate;GlobInc;EArray;E_Grid;PR\n;W/m2;kW;kW;ratio\n"
            "01/06/24 08:00;500;10;9.5;0.84\n01/06/24 08:15;500;10;9.5;0.84\n01/06/24 08:45;500;10;9.5;0.84\n",
            encoding="utf-8",
        )
        blocked_res = run_feasibility_study(
            self.cli,
            self.store,
            spec={"study_name": "BlockedStudy", "existing_csv": gap_csv.name, "installed_dc_kw": 100.0},
            dry_run=False,
            confirm=True,
        )
        self.assertEqual(blocked_res["status"], "blocked")
        diag_stage = next(s for s in blocked_res["stages"] if s["stage"] == "diagnostics")
        self.assertEqual(diag_stage["status"], "blocked")

        # Explicit None optional fields in bess_sizing / report must not raise TypeError
        spec_none = self._sample_spec()
        spec_none["bess_sizing"]["verify_top_k"] = None
        spec_none["bess_sizing"]["pcs_capex_per_kw"] = None
        spec_none["bess_sizing"]["rank_by"] = None
        spec_none["bess_sizing"]["bess_params"] = None
        spec_none["report"] = None
        prev_none = run_feasibility_study(self.cli, self.store, spec=spec_none, dry_run=True)
        self.assertEqual(prev_none["status"], "ready")

    def test_design_review_status_propagates_to_public_study(self) -> None:
        from pvsyst_auto.api.engineering import pvsyst_run_feasibility_study
        from pvsyst_auto.api.schemas import FeasibilityStudyRequest

        request = FeasibilityStudyRequest.model_validate({
            "study_name": "Review", "design": {"scope": "fixed_unshaded_grid",
                "new_project": "Plant.PRJ", "target_dc_kw": 100, "year": 2024},
        })
        simulation = {"status": "needs_review", "artifacts": {"simulate": {"csv": str(self.base_csv)}},
                      "metrics": {"nominal_dc_power_kw": 100}}
        with patch("pvsyst_auto.api.engineering.cli", return_value=self.cli), \
             patch("pvsyst_auto.api.engineering.variant_store", return_value=self.store), \
             patch("pvsyst_auto.workflows.feasibility.simulate_design", return_value=simulation), \
             patch("pvsyst_auto.workflows.feasibility.diagnose_simulation_csv", return_value={"status": "ok", "needs_review": False}):
            result = pvsyst_run_feasibility_study(spec=request, dry_run=False, confirm=True)
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(result["executive_summary"]["needs_review"])

    def test_design_spec_reads_nested_simulate_design_artifacts_and_stops_on_weather(self) -> None:
        base_spec = self._sample_spec()
        base_spec.pop("existing_csv")
        base_spec.pop("installed_dc_kw")
        base_spec["design"] = {
            "scope": "fixed_unshaded_grid",
            "new_project": "Plant.PRJ",
            "target_dc_kw": 100.0,
            "year": 2024,
        }
        base_spec["report"]["output_name"] = "design_chain.html"

        # 1. ready_for_weather stops early instead of continuing into empty downstream stages
        with patch("pvsyst_auto.workflows.feasibility.simulate_design", return_value={
            "status": "ready_for_weather", "issues": [{"field": "weather", "message": "prepare weather first"}],
        }):
            wait_res = run_feasibility_study(self.cli, self.store, spec=base_spec, dry_run=True)
        self.assertEqual(wait_res["status"], "ready_for_weather")
        self.assertEqual([s["stage"] for s in wait_res["stages"]], ["components", "simulation"])

        # 2. Real simulate_design nests csv path under artifacts.simulate.csv and actual DC under metrics
        from tests._fixtures import build_grid_vc0
        (self.root / "Projects" / "Plant.PRJ").write_text(
            "PVObject_=pvProject\n  MeteoFileName=Met.MET\nEnd of PVObject pvProject\n", encoding="utf-8",
        )
        (self.root / "Projects" / "Plant.VC1").write_text(
            build_grid_vc0(
                orientations=[{"id": 1, "tilt": 25.0, "azim": 0.0}],
                subarrays=[{"id": 1, "orient": 1, "name": "PV", "n_mod": 12, "n_str": 2}],
            ),
            encoding="utf-8",
        )
        nested_sim = {
            "status": "completed",
            "selected": {"template": {"project": "Seed.PRJ", "variant": "VC1"}},
            "artifacts": {"simulate": {"csv": str(self.base_csv)}},
            "metrics": {"nominal_dc_power_kw": 118.8},
            "preview": {"simulation": {"nominal_dc_power_kw": 118.8}},
        }
        with patch("pvsyst_auto.workflows.feasibility.simulate_design", return_value=nested_sim):
            res = run_feasibility_study(self.cli, self.store, spec=base_spec, dry_run=False, confirm=True)
        self.assertIn(res["status"], ("completed", "needs_review"))
        self.assertEqual(res["executive_summary"]["installed_dc_kw"], 118.8)
        self.assertIsNotNone(res["diagnostics"])
        self.assertIsNotNone(res["bess_optimization"])
        self.assertIsNotNone(res["economics"])
        self.assertTrue(Path(res["report"]["report_path"]).is_file())


if __name__ == "__main__":
    unittest.main()

"""Unit tests for bounded Diagnose -> Clone -> Patch -> Re-simulate optimization workflow."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.variants import VariantStore
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


if __name__ == "__main__":
    unittest.main()

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
        "date;GlobInc;EArray;E_Grid;PR;IL_Vmax;IL_Pmax;GlobShd",
        ";W/m2;kW;kW;ratio;kW;kW;W/m2",
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
            f"{il_pmax if gi > 0 else 0.0};{glob_shd if gi > 0 else 0.0}"
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

    def test_clean_baseline_returns_no_action_needed(self):
        _write_csv(self.root / "Results" / "base_clean.csv", pr=0.84)
        res = optimize_variant(
            self.cli, self.store, project="OptTest.PRJ", variant="VC0",
            csv_name="base_clean.csv", dry_run=False, confirm=True,
        )
        self.assertEqual(res["status"], "no_action_needed")
        self.assertTrue(res["converged"])
        self.assertEqual(self.cli.calls, [])


if __name__ == "__main__":
    unittest.main()

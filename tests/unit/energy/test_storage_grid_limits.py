"""Offline round trips for grid limits on native AC storage variants."""
import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.energy.storage import create_grid_storage_variant
from pvsyst_auto.energy.system import create_grid_storage_system
from pvsyst_auto.project.variants import VariantStore
from tests.unit.energy.test_energy import ARRAY, BATTERY, CLOSE, GRID_STORAGE, PV_SYSTEM, ROOT, STORAGE, STORAGE_SYSTEM


class StorageGridLimitTests(unittest.TestCase):
    def fixture(self, flags, limit, *, existing=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name)
        projects = workspace / "Projects"
        projects.mkdir()
        (projects / "Fixture.PRJ").write_text(
            "PVObject_=pvProject\n  Comment=Fixture\nEnd of PVObject pvProject\n", encoding="utf-8",
        )
        system = STORAGE_SYSTEM.replace("$10010040", flags) if existing else PV_SYSTEM.replace("$00", flags)
        system = system.replace("    SystemType=Grid\n", "    SystemType=Grid\n" + limit)
        peak = GRID_STORAGE.format(strategy="PeakShaving", unavail="").replace("PVAvailable", "PVToGridMin")
        source = ROOT.format(root_flags="$00") + ARRAY + system + (peak if existing else "") + CLOSE
        (projects / "Fixture.VC0").write_text(source, encoding="utf-8", newline="\r\n")
        return VariantStore(workspace, ComponentStore(workspace)), projects / "Fixture.VC1", system

    def edit(self, store, **updates):
        return create_grid_storage_variant(store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
                                           updates=updates, dry_run=False)

    def build(self, store, **updates):
        return create_grid_storage_system(store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
                                          battery={**BATTERY, "library": "builtin"},
                                          storage={**STORAGE, "strategy": "peak_shaving", **updates}, dry_run=False)

    def test_unrelated_storage_edits_keep_limit_text_and_unit(self):
        for flags, key, value in (("$11100040", "grid_limit_kw", "6000.000"),
                                  ("$15100040", "grid_limit_kw", "6000.000"),
                                  ("$17158040", "grid_limit_kva", "330.000")):
            for updates in ({"initial_soc": 0.55}, {"discharging_strategy": "after_sunset"}):
                with self.subTest(flags=flags, updates=updates):
                    store, target, system = self.fixture(flags, f"    PLimGrid={value}\n    LimGrSeparLoss=True\n")
                    result = self.edit(store, **updates)
                    self.assertEqual(result["status"], "created", result)
                    self.assertIn(system.replace("\n", "\r\n").encode(), target.read_bytes())
                    for state in (result["before"], result["after"]):
                        self.assertEqual({k: v for k, v in state.items() if k.startswith("grid_limit_")},
                                         {key: float(value)})

    def test_explicit_kw_edits_preserve_active_limit_or_start_at_injection(self):
        for flags, expected_flags, separate_loss in (("$11100040", "$11100040", "True"),
                                                      ("$15100040", "$15100040", "True"),
                                                      ("$12100040", "$15100040", "False")):
            with self.subTest(flags=flags):
                store, target, _ = self.fixture(flags, "    PLimGrid=330.000\n    LimGrSeparLoss=True\n")
                result = self.edit(store, grid_limit_kw=331)
                self.assertEqual(result["status"], "created", result)
                self.assertEqual(result["after"]["system_flags"], expected_flags)
                self.assertEqual(result["after"]["grid_limit_kw"], 331)
                self.assertIn(f"    PLimGrid=331.0\n    LimGrSeparLoss={separate_loss}\n",
                              target.read_text(encoding="utf-8"))

    def test_active_kva_rejects_explicit_kw_on_both_storage_paths(self):
        for flags, existing in (("$17158040", True), ("$07158000", False)):
            with self.subTest(existing=existing):
                store, target, _ = self.fixture(flags, "    PLimGrid=330.0\n    LimGrSeparLoss=True\n", existing=existing)
                result = (self.edit if existing else self.build)(store, grid_limit_kw=331)
                self.assertEqual(result["status"], "blocked", result)
                self.assertRegex(result["issues"][0], r"apparent.*kVA.*grid_limit_kw")
                self.assertFalse(target.exists())

    def test_new_peak_shaving_preserves_an_inherited_kw_limit(self):
        store, target, _ = self.fixture("$05100000", "    PLimGrid=6000.000\n    LimGrSeparLoss=True\n", existing=False)
        result = self.build(store)
        self.assertEqual(result["status"], "created", result)
        self.assertEqual(result["configuration"]["system_flags"], "$15100040")
        self.assertEqual(result["configuration"]["grid_limit_kw"], 6000)
        self.assertIn("    PLimGrid=6000.000\n    LimGrSeparLoss=True\n", target.read_text(encoding="utf-8"))

    def test_new_peak_shaving_does_not_inherit_kva_as_kw(self):
        store, target, _ = self.fixture("$07158000", "    PLimGrid=330.0\n    LimGrSeparLoss=True\n", existing=False)
        result = self.build(store)
        self.assertEqual(result["status"], "blocked", result)
        self.assertRegex(result["issues"][0], r"apparent.*kVA.*grid_limit_kw")
        self.assertFalse(target.exists())

"""External-contract unit tests for PR 4: asymmetric MPPT remainder sizing fallback & v0.9.0."""
from __future__ import annotations

import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from pvsyst_auto.api.context import TOOL_GROUPS
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.engineering.sizing import suggest_sizing
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.tracker_layout import plan_tracker_polygon_layout
from pvsyst_auto.workflows.design import prepare_design_project
from tests._fixtures import init_full_workspace
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN, _FakeUtilityCLI
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT

REPO_ROOT = Path(__file__).resolve().parents[3]
OND_100KW_4MPPT = (
    UTILITY_OND.replace("Model=50kW", "Model=100kW")
    .replace("PNomConv=50", "PNomConv=100")
    .replace("PMaxOUT=50", "PMaxOUT=100")
    .replace("NbMPPT=2", "NbMPPT=4")
)


class AsymmetricMpptSizingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = init_full_workspace(Path(self._tmp.name))
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV" / "PVmodules" / "Bifi440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        (self.ws / "ComposPV" / "Inverters" / "Inv50.OND").write_text(UTILITY_OND, encoding="utf-8")
        (self.ws / "ComposPV" / "Inverters" / "Inv100_4MPPT.OND").write_text(OND_100KW_4MPPT, encoding="utf-8")
        self.components = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.components)
        self.cli = _FakeUtilityCLI(self.ws, year=2024, nominal_dc_kw=52.36, use_sfi_writer=False)

    def test_version_0_9_0_and_tool_budget_preserved(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["version"], "0.9.0")
        all_tools = {name for group in TOOL_GROUPS.values() for name in group}
        default_tools = all_tools - set(TOOL_GROUPS["Admin & Diagnostics"]) - set(TOOL_GROUPS["Expert & Low-Level"])
        self.assertEqual(len(default_tools), 48)
        self.assertEqual(len(all_tools), 75)

    def test_uniform_first_when_uniform_fits_tolerance(self) -> None:
        # 56.32 kW = 128 mods = 8 * 16 fits uniformly (4 strings/MPPT on 1x 50kW 2-MPPT inverter)
        res = suggest_sizing(
            self.store,
            pan_name="Bifi440.PAN",
            ond_name="Inv50.OND",
            target_dc_kw=56.32,
            capacity_tolerance_pct=0.5,
        )
        self.assertEqual(res["status"], "ready")
        self.assertEqual(res["recommended"]["module_count"], 128)
        self.assertEqual(res["recommended"]["parallel_strings_per_mppt"], 4)
        self.assertNotIn("asymmetric_mppt", res["recommended"])
        self.assertNotIn("allow_asymmetric_mppt", res["capacity"])

    def test_case1_single_shared_inverter_and_4mppt_fallback(self) -> None:
        # 52.36 kW = 119 mods = 17 * 7 (odd string count -> cannot hit +/-0.5% uniformly on 2-MPPT)
        blocked = suggest_sizing(
            self.store,
            pan_name="Bifi440.PAN",
            ond_name="Inv50.OND",
            target_dc_kw=52.36,
            capacity_tolerance_pct=0.5,
            allow_asymmetric_mppt=False,
        )
        self.assertEqual(blocked["status"], "blocked")

        # Auto_from_components on ground_sheds falls back cleanly to 2 shared subarrays on 1 inverter
        prep = prepare_design_project(
            self.cli,
            self.store,
            dry_run=False,
            spec={
                "scope": "ground_sheds",
                "new_project": "AsymCase1.PRJ",
                "year": 2024,
                "sizing_mode": "auto_from_components",
                "target_dc_kw": 52.36,
                "auto_sizing": {"capacity_tolerance_pct": 0.5},
                "tilt": 20.0,
                "azimuth": 0.0,
                "module": {"file_name": "Bifi440.PAN"},
                "inverter": {"file_name": "Inv50.OND"},
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            },
        )
        self.assertIn(prep["status"], ("prepared", "needs_review"), prep)
        plan = prep["preview"]["project_preparation"]["capacity_plan"]
        self.assertTrue(plan.get("asymmetric_mppt"))
        self.assertIsNone(plan["parallel_strings_per_mppt"])
        self.assertEqual(plan["module_count"], 119)
        self.assertEqual(plan["inverter_count"], 1)
        self.assertEqual(len(plan["subarray_allocations"]), 2)
        self.assertTrue(prep["utility_summary"].get("asymmetric_mppt"))
        val = self.store.validate_structure("AsymCase1.PRJ", "VC0")
        self.assertTrue(val["valid"], val)
        self.assertEqual(len(val["subarray_ids"]), 2)
        vc = (self.ws / "Projects" / "AsymCase1.VC0").read_text(encoding="latin-1")
        self.assertEqual(vc.count("PowerSharingInverterId=0"), 2)
        self.assertEqual(vc.count("Flags=$001E0502"), 2)
        pnoms = [float(x) for x in re.findall(r"PNomAC_InMPPT=([0-9.]+)", vc)]
        self.assertAlmostEqual(sum(pnoms), 50.0, places=2)

        # 4-MPPT 100kW inverter @ 112.20 kW (255 = 17 * 15 -> k=3 @ 4 + 1 @ 3)
        res_4m = suggest_sizing(
            self.store,
            pan_name="Bifi440.PAN",
            ond_name="Inv100_4MPPT.OND",
            target_dc_kw=112.20,
            capacity_tolerance_pct=0.3,
        )
        self.assertEqual(res_4m["status"], "ready")
        self.assertTrue(res_4m["recommended"]["asymmetric_mppt"])
        self.assertEqual(res_4m["recommended"]["inverter_count"], 1)
        self.assertEqual(res_4m["recommended"]["module_count"], 255)
        allocs = res_4m["recommended"]["subarray_allocations"]
        self.assertEqual(len(allocs), 2)
        self.assertEqual((allocs[0]["n_inv_mppt"], allocs[0]["n_string_ch"]), (3, 12))
        self.assertEqual((allocs[1]["n_inv_mppt"], allocs[1]["n_string_ch"]), (1, 3))
        self.assertAlmostEqual(
            allocs[0]["n_inv_mppt"] * allocs[0]["pnom_ac_in_mppt"] + allocs[1]["n_inv_mppt"] * allocs[1]["pnom_ac_in_mppt"],
            100.0,
            places=2,
        )

    def test_case2_full_plus_shared_remainder_and_polygon_ground_tracker(self) -> None:
        # 112.20 kW = 255 = 17 * 15 mods on 2x 50kW 2-MPPT inverters:
        # 1 full uniform inverter (8 str, unshared) + 1 shared remainder inverter (4 + 3 str)
        cli2 = _FakeUtilityCLI(self.ws, year=2024, nominal_dc_kw=112.20, use_sfi_writer=False)
        poly = [[0.0, 0.0], [32.0, 0.0], [32.0, 80.0], [0.0, 38.0]]
        pl = plan_tracker_polygon_layout(
            poly,
            axis_azimuth_deg=0.0,
            pitch_ew_m=6.0,
            module_width_m=1.032,
            module_length_m=1.996,
            module_rows=1,
            spacing_x_m=0.02,
            spacing_y_m=0.02,
            setback_m=1.0,
            n_mod_serie=17,
        )
        if pl["total_modules"] != 255 or len({t["module_columns"] for t in pl["tables"]}) < 2:
            poly = next(
                [[0.0, 0.0], [float(xs), 0.0], [float(xs), float(ye)], [0.0, 38.0]]
                for xs in (32, 34, 38)
                for ye in range(55, 130)
                if (cand := plan_tracker_polygon_layout(
                    [[0.0, 0.0], [float(xs), 0.0], [float(xs), float(ye)], [0.0, 38.0]],
                    axis_azimuth_deg=0.0,
                    pitch_ew_m=6.0,
                    module_width_m=1.032,
                    module_length_m=1.996,
                    module_rows=1,
                    spacing_x_m=0.02,
                    spacing_y_m=0.02,
                    setback_m=1.0,
                    n_mod_serie=17,
                ))["total_modules"] == 255 and len({t["module_columns"] for t in cand["tables"]}) >= 2
            )

        prep = prepare_design_project(
            cli2,
            self.store,
            dry_run=False,
            spec={
                "scope": "ground_tracker",
                "new_project": "AsymCase2Trk.PRJ",
                "year": 2024,
                "sizing_mode": "auto_from_components",
                "target_dc_kw": 112.20,
                "auto_sizing": {"capacity_tolerance_pct": 0.3},
                "azimuth": 0.0,
                "module": {"file_name": "Bifi440.PAN"},
                "inverter": {"file_name": "Inv50.OND"},
                "ground_layout": {"pitch_m": 6.0, "module_rows": 1, "setback_m": 1.0, "site_polygon": poly},
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            },
        )
        self.assertIn(prep["status"], ("prepared", "needs_review"), prep)
        plan = prep["preview"]["project_preparation"]["capacity_plan"]
        self.assertTrue(plan.get("asymmetric_mppt"))
        self.assertEqual(plan["inverter_count"], 2)
        self.assertEqual(plan["module_count"], 255)
        self.assertEqual(len(plan["subarray_allocations"]), 3)
        val = self.store.validate_structure("AsymCase2Trk.PRJ", "VC0")
        self.assertTrue(val["valid"], val)
        self.assertEqual(len(val["subarray_ids"]), 3)
        vc = (self.ws / "Projects" / "AsymCase2Trk.VC0").read_text(encoding="latin-1")
        self.assertEqual(vc.count("PowerSharingInverterId=-1"), 1)
        self.assertEqual(vc.count("PowerSharingInverterId=0"), 2)
        self.assertGreaterEqual(vc.count("=pvShdArrayTracking"), 4)

    def test_strict_capacity_and_scope_guards(self) -> None:
        # Strict mode with explicit capacity.allow_asymmetric_mppt=True succeeds on ground_sheds
        res_sug = suggest_sizing(
            self.store, pan_name="Bifi440.PAN", ond_name="Inv50.OND", target_dc_kw=52.36, capacity_tolerance_pct=0.5
        )
        strict_ok = prepare_design_project(
            self.cli,
            self.store,
            dry_run=True,
            spec={
                "scope": "ground_sheds",
                "new_project": "StrictAsym.PRJ",
                "year": 2024,
                "target_dc_kw": 52.36,
                "tilt": 20.0,
                "azimuth": 0.0,
                "module": {"file_name": "Bifi440.PAN"},
                "inverter": {"file_name": "Inv50.OND"},
                "capacity": res_sug["capacity"],
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            },
        )
        self.assertEqual(strict_ok["status"], "ready", strict_ok)

        # Rejected on ground_dome, fixed_unshaded_grid, and sub_orientations when True
        for bad_name, spec in (
            (
                "dome_auto",
                {
                    "scope": "ground_dome", "new_project": "BadDome.PRJ", "year": 2024,
                    "sizing_mode": "auto_from_components", "target_dc_kw": 52.36,
                    "auto_sizing": {"allow_asymmetric_mppt": True, "capacity_tolerance_pct": 0.5},
                    "tilt": 10.0, "azimuth": 90.0,
                    "module": {"file_name": "Bifi440.PAN"}, "inverter": {"file_name": "Inv50.OND"},
                    "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                },
            ),
            (
                "fixed_strict",
                {
                    "scope": "fixed_unshaded_grid", "new_project": "BadFixed.PRJ", "year": 2024,
                    "target_dc_kw": 52.36, "tilt": 20.0, "azimuth": 0.0,
                    "module": {"file_name": "Bifi440.PAN"}, "inverter": {"file_name": "Inv50.OND"},
                    "capacity": res_sug["capacity"],
                    "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                },
            ),
            (
                "sub_orients",
                {
                    "scope": "ground_sheds", "new_project": "BadSubO.PRJ", "year": 2024,
                    "sizing_mode": "auto_from_components", "target_dc_kw": 52.36,
                    "auto_sizing": {"allow_asymmetric_mppt": True},
                    "ground_layout": {
                        "pitch_m": 10.0,
                        "sub_orientations": [
                            {"tilt": 20.0, "azimuth": -15.0, "weight": 1.0},
                            {"tilt": 20.0, "azimuth": 15.0, "weight": 1.0},
                        ],
                    },
                    "module": {"file_name": "Bifi440.PAN"}, "inverter": {"file_name": "Inv50.OND"},
                    "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                },
            ),
        ):
            with self.subTest(bad_name=bad_name):
                blocked = prepare_design_project(self.cli, self.store, dry_run=True, spec=spec)
                self.assertEqual(blocked["status"], "blocked", blocked)
                self.assertTrue(any("allow_asymmetric_mppt" in str(i) for i in blocked["issues"]), blocked["issues"])

        # ground_dome with omitted allow_asymmetric_mppt does NOT activate asymmetric fallback (remains blocked when uniform fails)
        dome_omitted = prepare_design_project(
            self.cli,
            self.store,
            dry_run=True,
            spec={
                "scope": "ground_dome",
                "new_project": "DomeOmit.PRJ",
                "year": 2024,
                "sizing_mode": "auto_from_components",
                "target_dc_kw": 52.36,
                "auto_sizing": {"capacity_tolerance_pct": 0.5},
                "tilt": 10.0,
                "azimuth": 90.0,
                "module": {"file_name": "Bifi440.PAN"},
                "inverter": {"file_name": "Inv50.OND"},
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            },
        )
        self.assertEqual(dome_omitted["status"], "blocked", dome_omitted)
        self.assertEqual(dome_omitted["stage"], "design_envelope")

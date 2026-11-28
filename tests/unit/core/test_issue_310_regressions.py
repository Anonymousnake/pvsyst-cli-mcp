"""Regression tests for GitHub Issue #310."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.energy.transformers import configure_transformers
from pvsyst_auto.engineering.asymmetric_sizing import build_asymmetric_subarrays
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.template import synthesize_utility_project
from pvsyst_auto.project.tracking import convert_orientation
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.partition import compute_table_partition_dims
from pvsyst_auto.workflows.design import prepare_design_project
from tests._fixtures import init_full_workspace
from tests.unit.core.test_bifacial_utility import UTILITY_MONO_PAN, UTILITY_OND, UTILITY_PAN, _FakeUtilityCLI, _capacity_spec
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


class Issue310RegressionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = init_full_workspace(Path(self._tmp.name))
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV" / "PVmodules" / "Mono440.PAN").write_text(UTILITY_MONO_PAN, encoding="utf-8")
        (self.ws / "ComposPV" / "PVmodules" / "Bifi440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        (self.ws / "ComposPV" / "PVmodules" / "HalfCut440.PAN").write_text(
            UTILITY_PAN.replace("  Technol=mtSiMono\n", "  Technol=mtSiMono\n  SubModuleLayout=slTwinHalfCells\n", 1),
            encoding="utf-8",
        )
        (self.ws / "ComposPV" / "Inverters" / "Inv50_2MPPT.OND").write_text(UTILITY_OND, encoding="utf-8")
        (self.ws / "ComposPV" / "Inverters" / "Inv50_1MPPT.OND").write_text(
            UTILITY_OND.replace("NbMPPT=2", "NbMPPT=1"), encoding="utf-8",
        )
        (self.ws / "ComposPV" / "Inverters" / "Inv100_3MPPT.OND").write_text(
            UTILITY_OND.replace("Model=50kW", "Model=100kW")
            .replace("PNomConv=50", "PNomConv=100")
            .replace("PMaxOUT=50", "PMaxOUT=100")
            .replace("NbMPPT=2", "NbMPPT=3"),
            encoding="utf-8",
        )
        self.comp = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.comp)
        self.cli = _FakeUtilityCLI(self.ws, year=2024, nominal_dc_kw=52.8, use_sfi_writer=False)

    def test_convert_orientation_shading_and_dome_mppt_and_dual_balance(self) -> None:
        # 1. Unshaded fixed -> sheds preserves empty pvShading, reports stripped_3d_shading=False, allows update_parameters
        unshaded = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Mono440.PAN", ond_name="Inv50_2MPPT.OND",
            n_mod_serie=20, n_string_ch=12, n_inverter=2, n_inv_mppt=4,
            independent_mppt=True, pnom_ac_in_mppt=25.0,
        )
        self.store.create_project("Unsh.PRJ", unshaded["prj_text"], {"VC0": unshaded["vc0_text"]})
        to_sheds = convert_orientation(self.store, project="Unsh.PRJ", source_variant="VC0", new_variant="VC1", target_type="sheds", dry_run=False)
        self.assertFalse(to_sheds["orientation_conversion"]["stripped_3d_shading"])
        vc1 = (self.ws / "Projects" / "Unsh.VC1").read_text(encoding="latin-1")
        self.assertIn("PVObject_Ombrage=pvShading", vc1)
        self.assertIn("Flags=$00080000", vc1)
        edit = self.store.update_parameters("Unsh.PRJ", "VC1", subarray_updates={"NStringCh": 16}, dry_run=False)
        self.assertTrue(edit["changed"])

        # 2. Multi-MPPT independent fixed -> mixed dome collapses MPPTNode -> StringNode, clears $400, scales PNomAC_InMPPT
        to_dome = convert_orientation(
            self.store, project="Unsh.PRJ", source_variant="VC0", new_variant="VC2",
            target_type="dome", params={"tilt_deg": 10.0, "azimuth_deg": 90.0}, dry_run=False,
        )
        self.assertEqual(to_dome["status"], "created")
        self.assertTrue(any("pvShdArrayDome" in w for w in to_dome["warnings"]))
        self.assertTrue(self.store.validate_structure("Unsh.PRJ", "VC2")["valid"])
        vc2 = (self.ws / "Projects" / "Unsh.VC2").read_text(encoding="latin-1")
        self.assertIn("Flags=$001E0042", vc2)
        self.assertIn("NInvMPPT=1", vc2)
        self.assertIn("PNomAC_InMPPT=50.00", vc2)
        self.assertNotIn("MPPTNode", vc2)

        # 3. Validate NoOrientation2 + Flags $400 rejected cleanly
        vc2_raw = (self.ws / "Projects" / "Unsh.VC2").read_bytes()
        bad_flags = vc2_raw.replace(b"Flags=$001E0042", b"Flags=$001E0442")
        (self.ws / "Projects" / "Unsh.VC3").write_bytes(bad_flags)
        val = self.store.validate_structure("Unsh.PRJ", "VC3")
        self.assertIs(val["valid"], False)
        self.assertTrue(any("$400" in msg for msg in val["issues"]))

        # 4. 3D scene -> fixed replaces 3D pvShading with empty pvShading and reports stripped_3d_shading=True
        shaded = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="RegularSheds", pan_name="Mono440.PAN", ond_name="Inv50_2MPPT.OND",
            n_mod_serie=20, n_string_ch=6, n_inverter=1, n_inv_mppt=1,
            independent_mppt=False, pnom_ac_in_mppt=50.0,
            shed_count=3, module_rows=2, module_columns=20,
        )
        self.store.create_project("Shaded.PRJ", shaded["prj_text"], {"VC0": shaded["vc0_text"]})
        stripped = convert_orientation(self.store, project="Shaded.PRJ", source_variant="VC0", new_variant="VC1", target_type="fixed", dry_run=False)
        self.assertTrue(stripped["orientation_conversion"]["stripped_3d_shading"])
        sh_vc1 = (self.ws / "Projects" / "Shaded.VC1").read_text(encoding="latin-1")
        self.assertIn("PVObject_Ombrage=pvShading", sh_vc1)
        self.assertIn("Flags=$00080000", sh_vc1)
        self.assertNotIn("pvShdArrayTable", sh_vc1)
        self.assertTrue(self.store.update_parameters("Shaded.PRJ", "VC1", subarray_updates={"NStringCh": 8}, dry_run=False)["changed"])

        # 5. Unbalanced 2-subarray -> dome blocked without writing VC1
        unbal = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Mono440.PAN", ond_name="Inv50_1MPPT.OND",
            orientations=[{"id": 1, "field_type": "FixedPlane", "tilt_deg": 20.0, "azimuth_deg": 0.0}],
            subarrays=[
                {"subarray_id": 1, "orientation_id": 1, "n_mod_serie": 20, "n_string_ch": 3, "n_inverter": 1, "n_inv_mppt": 1, "independent_mppt": False, "pnom_ac_in_mppt": 50.0},
                {"subarray_id": 2, "orientation_id": 1, "n_mod_serie": 20, "n_string_ch": 2, "n_inverter": 1, "n_inv_mppt": 1, "independent_mppt": False, "pnom_ac_in_mppt": 50.0},
            ],
        )
        self.store.create_project("Unbal.PRJ", unbal["prj_text"], {"VC0": unbal["vc0_text"]})
        blocked = convert_orientation(self.store, project="Unbal.PRJ", source_variant="VC0", new_variant="VC1", target_type="dome", dry_run=False)
        self.assertEqual(blocked["status"], "blocked")
        self.assertFalse((self.ws / "Projects" / "Unbal.VC1").exists())

    def test_2p_tracker_partition_dims_folded_strings(self) -> None:
        _, _, px1, py1 = compute_table_partition_dims(
            long_shed_m=4.0, larg_shed_m=14.0, module_rows=2, module_columns=14,
            module_orient="moPortrait", is_tracker=True, electrical_shading="partition",
            series_modules=28, is_half_cut=True,
        )
        self.assertEqual((px1, py1), (1, 2))

        _, _, px3, py3 = compute_table_partition_dims(
            long_shed_m=4.0, larg_shed_m=42.0, module_rows=2, module_columns=42,
            module_orient="moPortrait", is_tracker=True, electrical_shading="partition",
            series_modules=28, is_half_cut=True,
        )
        self.assertEqual((px3, py3), (3, 2))

        # End-to-end 2P tracker polygon with series_modules=20 -> 2x10 clipped folded string!
        cap20 = _capacity_spec(target_dc_kw=52.8)
        cap20.update(capacity_tolerance_pct=10.0)
        prep_2p = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec={
                "scope": "ground_tracker",
                "new_project": "Trk2P.PRJ",
                "year": 2024,
                "target_dc_kw": 52.8,
                "azimuth": 0.0,
                "module": {"file_name": "HalfCut440.PAN"},
                "inverter": {"file_name": "Inv50_2MPPT.OND"},
                "capacity": cap20,
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                "ground_layout": {
                    "model_mode": "3d", "pitch_m": 10.0, "module_rows": 2,
                    "site_polygon": [[0.0, 0.0], [56.0, 0.0], [56.0, 11.5], [0.0, 11.5]],
                    "setback_m": 0.0, "max_table_columns": 10,
                    "electrical_shading": "partition",
                },
            },
        )
        self.assertIn(prep_2p["status"], ("prepared", "needs_review"), prep_2p)
        vc_2p = (self.ws / "Projects" / "Trk2P.VC0").read_text(encoding="latin-1")
        self.assertIn("NModSerie=20", vc_2p)
        self.assertIn("NModInHeight=2", vc_2p)
        self.assertIn("Flags=$000A0300", vc_2p)

    def test_ground_dome_even_strings_and_pre_weather_utility_preflight(self) -> None:
        # 1) 1-MPPT inverter on ground_dome with odd target (~14 strings -> let's pick 3 strings = 158.4 kW -> must not pick odd 3)
        dome_auto = prepare_design_project(
            self.cli, self.store, dry_run=True,
            spec={
                "scope": "ground_dome",
                "new_project": "DomeEven.PRJ",
                "year": 2024,
                "sizing_mode": "auto_from_components",
                "target_dc_kw": 79.2,  # 180 mods = 9 strings of 20 (odd!), or 8/10 strings of 18/20 (even!)
                "tilt": 10.0,
                "azimuth": 90.0,
                "module": {"file_name": "Mono440.PAN"},
                "inverter": {"file_name": "Inv50_1MPPT.OND"},
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                "ground_layout": {"pitch_m": 6.0, "shed_count": 2, "module_rows": 1, "module_columns": 45},
            },
        )
        if dome_auto["status"] == "ready":
            self.assertEqual(dome_auto["preview"]["utility_capacity"]["string_count"] % 2, 0)
        else:
            # Or blocked cleanly in sizing rather than failing later in resolve_dome_subarrays
            self.assertEqual(dome_auto["stage"], "design_envelope")

        # 2) Remote weather mode with invalid pitch <= dome_footprint stops at utility_preflight BEFORE download_weather or ready_for_weather
        with patch("pvsyst_auto.workflows.design.download_weather") as dl_mock:
            bad_dome = prepare_design_project(
                self.cli, self.store, dry_run=True,
                spec={
                    "scope": "ground_dome",
                    "new_project": "BadDomePitch.PRJ",
                    "year": 2024,
                    "sizing_mode": "auto_from_components",
                    "auto_sizing": {"capacity_tolerance_pct": 10.0},
                    "target_dc_kw": 88.0,
                    "tilt": 10.0,
                    "azimuth": 90.0,
                    "module": {"file_name": "Mono440.PAN"},
                    "inverter": {"file_name": "Inv50_2MPPT.OND"},
                    "weather": {
                        "mode": "open_meteo",
                        "latitude": 46.24,
                        "longitude": 6.11,
                        "site_label": "Geneva",
                        "altitude": 400.0,
                        "timezone_name": "Europe/Zurich",
                    },
                    "ground_layout": {"pitch_m": 4.1, "shed_count": 5, "module_rows": 1, "module_columns": 20},
                },
            )
            dl_mock.assert_not_called()
        self.assertEqual(bad_dome["status"], "blocked", bad_dome)
        self.assertEqual(bad_dome["stage"], "utility_preflight")

    def test_asymmetric_mppt_3mppt_100kw_exact_ac_at_transformer_bounds(self) -> None:
        # 20 x 100kW 3-MPPT inverters: 19 uniform (p_uni=33.33 -> 1899.81 instead of 1900) + 1 shared (33.33+33.33+33.34=100)
        allocs = build_asymmetric_subarrays(series=20, ports=3, ac_kw=100.0, count=20, s_full=4, s_high=5, s_low=4, k=1)
        total_str = sum(a["n_string_ch"] for a in allocs)
        synth = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="Sheds", pan_name="Bifi440.PAN", ond_name="Inv100_3MPPT.OND",
            n_mod_serie=20, n_string_ch=total_str, n_inverter=20, n_inv_mppt=60,
            independent_mppt=True, pnom_ac_in_mppt=33.33,
            subarray_allocations=allocs, pitch_m=10.0,
        )
        self.store.create_project("Asym20x100.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
        # Exact 80% (1600 kVA) and exact 200% (4000 kVA) of true 2000 kW AC must BOTH pass!
        for kva, vc_new in ((1600.0, "VC1"), (4000.0, "VC2")):
            with self.subTest(kva=kva):
                res = configure_transformers(
                    self.store, project="Asym20x100.PRJ", source_variant="VC0", new_variant=vc_new,
                    mv={"count": 1, "output_voltage_kv": 35.0, "nominal_power_kva": kva, "iron_loss_kw": 2.0, "copper_loss_kw": 12.0},
                    dry_run=True,
                )
                self.assertEqual(res["status"], "ready", res["issues"])
                self.assertAlmostEqual(res["transformers"]["inverter_ac_power_kw"], 2000.0, places=3)


if __name__ == "__main__":
    unittest.main()

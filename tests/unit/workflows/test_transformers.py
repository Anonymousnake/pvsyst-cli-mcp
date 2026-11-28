"""Unit tests for MV/HV multi-stage transformer & AC collection network builder/editor."""
from __future__ import annotations
from pathlib import Path
import tempfile
import unittest
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.energy.transformers import apply_transformers_to_variant_text, configure_transformers
from pvsyst_auto.project.circuit import transformer_state
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.variants import VariantStore
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT
class TransformersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "PVsyst8.1_Data"
        for sub in ("Projects", "Sites", "Meteo", "Results", "Models", "ComposPV/PVmodules", "ComposPV/Inverters"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.root / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.root / "ComposPV/PVmodules/Lab440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        (self.root / "ComposPV/Inverters/Lab50.OND").write_text(UTILITY_OND, encoding="utf-8")
        self.comp = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.comp)
        synth = synthesize_project_and_variant(self.store, project_comment="TrfTest", variant_comment="Base",
            site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET", pan_name="Lab440.PAN", ond_name="Lab50.OND",
            n_mod_serie=20, n_string_ch=100, n_inverter=10, n_inv_mppt=1, independent_mppt=False, pnom_ac_in_mppt=50.0, tilt_deg=25.0, azimuth_deg=0.0)
        vc0 = synth["vc0_text"].replace("      NoOrientation=1", "      PNomSizing=440.0\n      NoOrientation=1")
        self.store.create_project("TrfTest.PRJ", synth["prj_text"], {"VC0": vc0})
    def test_public_tool_requires_reference_when_source_has_none(self) -> None:
        from unittest.mock import patch
        from pvsyst_auto.api.projects import pvsyst_configure_transformers
        from pvsyst_auto.api.schemas import MvTransformerSpec
        source = self.root / "Projects" / "TrfTest.VC0"
        text = (source.read_text(encoding="utf-8-sig")
            .replace("      PNomSizing=440.0\n", "") .replace("      PVModule=Lab440.PAN\n", "      PVModule=Missing.PAN\n"))
        source.write_text(text, encoding="utf-8")
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            result = pvsyst_configure_transformers("TrfTest.PRJ", "VC0", "VC1", mv=MvTransformerSpec(), dry_run=False)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("stc_ref_power_kw", " ".join(result["issues"]))
            self.assertFalse((self.root / "Projects" / "TrfTest.VC1").exists())
            result = pvsyst_configure_transformers("TrfTest.PRJ", "VC0", "VC1", mv=MvTransformerSpec(stc_ref_power_kw=500), dry_run=False)
            self.assertEqual(result["status"], "created")
            self.assertEqual(result["transformers"]["mv"]["stc_ref_power_kw"], 500)
    def test_shared_inverter_nameplate_fallback_counts_parent_once(self) -> None:
        subs = [dict(subarray_id=i, orientation_id=i, n_mod_serie=20, n_string_ch=5,
                     n_inverter=1, n_inv_mppt=1, independent_mppt=True,
                     power_sharing_inverter_id=0, pnom_ac_in_mppt=0)
                for i in (1, 2)]
        synth = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN", ond_name="Lab50.OND",
            orientations=[{"id": 1}, {"id": 2}], subarrays=subs,
        )
        for index, source in enumerate((synth["vc0_text"], synth["vc0_text"].replace(
                "PNomAC_InMPPT=0.00", "PNomAC_InMPPT=25.00", 1))):
            project = f"Shared{index}.PRJ"
            self.store.create_project(project, synth["prj_text"], {"VC0": source})
            result = configure_transformers(
                self.store, project=project, source_variant="VC0", new_variant="VC1",
                mv={}, hv={"nominal_power_kva": 50}, dry_run=False,
            )
            self.assertEqual(result["status"], "created", result["issues"])
            self.assertEqual(result["transformers"]["inverter_ac_power_kw"], 50)
            self.assertEqual(result["transformers"]["mv"]["iron_loss_kw"], 0.05)
    def test_public_hv_fraction_matches_generated_datasheet(self) -> None:
        from unittest.mock import patch
        from pvsyst_auto.api.projects import pvsyst_configure_transformers
        from pvsyst_auto.api.schemas import HvTransformerSpec, MvTransformerSpec
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            result = pvsyst_configure_transformers("TrfTest.PRJ", "VC0", "VC1", mv=MvTransformerSpec(stc_ref_power_kw=1000),
                hv=HvTransformerSpec(stc_ref_power_kw=1000, nominal_power_kva=800, iron_loss_frac=0.005, copper_loss_frac=0.01), dry_run=False)
        self.assertEqual(result["status"], "created")
        hv = result["transformers"]["hv"]
        self.assertAlmostEqual(hv["ds_iron_loss"] / hv["stc_ref_power_kw"], hv["iron_loss_frac"])
        self.assertAlmostEqual(hv["ds_copper_loss"] * hv["stc_ref_power_kw"] / hv["ds_nom_power"]**2, hv["copper_loss_frac"])
    def test_mv_only_then_mv_hv_cascade_then_remove(self) -> None:
        # 1. Dry-run preview writes nothing
        prev = configure_transformers(self.store, project="TrfTest.PRJ", source_variant="VC0", new_variant="VC1",
            mv={"count": 2, "output_voltage_kv": 20.0, "stc_ref_power_kw": 440.0,
                "iron_loss_frac": 0.0012, "copper_loss_frac": 0.011, "night_disconnect": True,
                "cable": {"length_m": 450.0, "metal": "Copper", "section_mm2": 150}}, dry_run=True)
        self.assertEqual(prev["status"], "ready")
        self.assertFalse((self.root / "Projects" / "TrfTest.VC1").exists())
        # 2. Execute MV-only with 2 parallel transformers + MV cable + night disconnect
        mv_res = configure_transformers(self.store, project="TrfTest.PRJ", source_variant="VC0", new_variant="VC1",
            mv={"count": 2, "output_voltage_kv": 20.0, "stc_ref_power_kw": 440.0,
                "iron_loss_frac": 0.0012, "copper_loss_frac": 0.011, "night_disconnect": True,
                "cable": {"length_m": 450.0, "metal": "Copper", "section_mm2": 150}}, dry_run=False)
        self.assertEqual(mv_res["status"], "created")
        self.assertEqual(mv_res["transformers"]["layers"], 1)
        self.assertFalse(mv_res["transformers"]["nested_hv_mv"])
        vc1_text = (self.root / "Projects" / "TrfTest.VC1").read_text(encoding="utf-8-sig")
        self.assertIn("MVNbTransfos=2", vc1_text)
        self.assertIn("NTransformers=2", vc1_text)
        self.assertIn("NightDisconnect=True", vc1_text)
        self.assertIn("StdCrossSect=ISO60228_150", vc1_text)
        self.assertIn("Length=450 m", vc1_text)
        # 3. Upgrade VC1 -> VC2 with cascaded MV + HV substation + HV cable + datasheet specs
        hv_res = configure_transformers(self.store, project="TrfTest.PRJ", source_variant="VC1", new_variant="VC2",
            mv={"count": 2, "output_voltage_kv": 35.0, "stc_ref_power_kw": 440.0,
                "nominal_power_kva": 315.0, "iron_loss_kw": 0.45, "copper_loss_kw": 3.5, "night_disconnect": False,
                "cable": {"length_m": 600.0, "metal": "Copper", "section_mm2": 185}}, hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 440.0,
                "nominal_power_kva": 630.0, "iron_loss_kw": 0.7, "copper_loss_kw": 4.5, "night_disconnect": False,
                "cable": {"length_m": 2500.0, "metal": "Aluminium", "section_mm2": 240}}, dry_run=False)
        self.assertEqual(hv_res["status"], "created", hv_res)
        self.assertEqual(hv_res["transformers"]["layers"], 2)
        self.assertTrue(hv_res["transformers"]["nested_hv_mv"])
        vc2_lines = (self.root / "Projects" / "TrfTest.VC2").read_text(encoding="utf-8-sig").splitlines(keepends=True)
        st = transformer_state(vc2_lines)
        self.assertEqual(st["issues"], [])
        self.assertEqual(st["count"], 2)
        self.assertTrue(st["nested"])
        self.assertEqual(st["layers"][0]["fields"]["IsHVTransformer"], "True")
        self.assertEqual(st["layers"][0]["fields"]["OutputVoltage"], "110 kV")
        self.assertEqual(st["layers"][1]["fields"]["IsHVTransformer"], "False")
        self.assertEqual(st["layers"][1]["fields"]["OutputVoltage"], "35 kV")
        vc2_text = "".join(vc2_lines)
        self.assertIn("VOutHVTransfo=110000", vc2_text)
        self.assertIn("HVTrfDSNomPower=630.00", vc2_text)
        self.assertIn("MVTrfDSNomPower=315.00", vc2_text)
        # 4. Strip all transformers cleanly VC2 -> VC3
        rm_res = configure_transformers(self.store, project="TrfTest.PRJ", source_variant="VC2", new_variant="VC3",
            remove_transformers=True, dry_run=False)
        self.assertEqual(rm_res["status"], "created")
        self.assertEqual(rm_res["transformers"]["layers"], 0)
        vc3_lines = (self.root / "Projects" / "TrfTest.VC3").read_text(encoding="utf-8-sig").splitlines(keepends=True)
        self.assertEqual(transformer_state(vc3_lines)["count"], 0)
        self.assertEqual(self.store.validate_structure("TrfTest.PRJ", "VC3")["valid"], True)
    def test_public_tool_clears_saved_results(self) -> None:
        from unittest.mock import patch
        from pvsyst_auto.api.projects import pvsyst_configure_transformers
        from pvsyst_auto.api.schemas import MvTransformerSpec
        source = self.root / "Projects" / "TrfTest.VC0"
        text = source.read_text(encoding="utf-8-sig")
        root_flags = text.splitlines()[3]
        text = text.replace(root_flags, root_flags[:-1] + "1", 1)
        cache = ("  VarList, list of=1 TVarSimul\n    VarSimul_1=E_Grid\n" "    End of VarSimul=E_Grid\n  End of the List VarList\n")
        text = text.replace("End of PVObject pvVCalcul", cache + "End of PVObject pvVCalcul")
        source.write_text(text, encoding="utf-8")
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            result = pvsyst_configure_transformers("TrfTest.PRJ", "VC0", "VC1", mv=MvTransformerSpec(), dry_run=False)
        self.assertEqual(result["status"], "created")
        output = (self.root / "Projects" / "TrfTest.VC1").read_text(encoding="utf-8-sig")
        self.assertNotIn("VarList, list of=", output)
        self.assertEqual(int(output.splitlines()[3].split("$")[1], 16) & 1, 0)
    def test_validation_rejects_invalid_combinations(self) -> None:
        r1 = configure_transformers(self.store, project="TrfTest.PRJ", source_variant="VC0", new_variant="VC9",
            mv={"count": 1}, remove_transformers=True, dry_run=True)
        self.assertEqual(r1["status"], "blocked")
        vc0 = (self.root / "Projects" / "TrfTest.VC0").read_text(encoding="utf-8-sig")
        with self.assertRaisesRegex(ValueError, "section_mm2"):
            apply_transformers_to_variant_text(vc0, mv={"stc_ref_power_kw": 440, "cable": {"length_m": 100, "section_mm2": 999}})
        # Without components / PNomSizing / STCRefPower / explicit stc_ref_power_kw, must NOT silently default to 1000 kW
        vc0_no_ref = vc0.replace("      PNomSizing=440.0\n", "")
        with self.assertRaisesRegex(ValueError, "stc_ref_power_kw"): apply_transformers_to_variant_text(vc0_no_ref, mv={"iron_loss_frac": 0.001})
    def test_auto_infers_120mw_stc_ref_power_from_pan_and_bom_ground_sheds_workflow(self) -> None:
        from pvsyst_auto.energy.dc import create_dc_cabling_variant
        from pvsyst_auto.project.bifacial import create_shed_variant
        from pvsyst_auto.project.template import synthesize_utility_project
        # 1. Synthesize 129.36 MWp bifacial ground_sheds variant with UTF-8 BOM (28 * 10500 * 440 Wp = 129,360 kWp)
        synth = synthesize_utility_project(self.store, project_comment="Utility120MW", variant_comment="BaseSheds",
            site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET", field_type="Sheds", pan_name="Lab440.PAN",
            ond_name="Lab50.OND", n_mod_serie=28, n_string_ch=10500, n_inverter=350,
            n_inv_mppt=2100, independent_mppt=True, pnom_ac_in_mppt=50.0, tilt_deg=25.0,
            azimuth_deg=0.0, pitch_m=10.0, collector_width_m=4.6, shed_count=50, bifacial={"enabled": True, "ground_albedo": 0.25}, with_bom=True)
        # P0-2: apply_transformers_to_variant_text handles leading UTF-8 BOM cleanly without Unmatched closing tag
        vc_trf, trf_sum = apply_transformers_to_variant_text(synth["vc0_text"],
            mv={"count": 38, "output_voltage_kv": 35.0, "iron_loss_frac": 0.00095, "copper_loss_frac": 0.010},
            hv={"output_voltage_kv": 110.0, "nominal_power_kva": 130000.0, "iron_loss_kw": 84.5, "copper_loss_kw": 480.0}, default_ref_power_kw=129360.0)
        self.assertTrue(vc_trf.startswith("\ufeff"))
        self.assertAlmostEqual(trf_sum["mv"]["stc_ref_power_kw"], 129360.0, places=1)
        # PVsyst 8.1.7 evaluates MV fractional iron loss on PNomInvAC (2100 * 50 kW = 105,000 kW), not DC STC
        self.assertAlmostEqual(trf_sum["inverter_ac_power_kw"], 105000.0, places=1)
        self.assertAlmostEqual(trf_sum["mv"]["iron_loss_kw"], 105000.0 * 0.00095, places=2)
        # P0-1: configure_transformers auto-infers 129,360 kWp from Lab440.PAN (NOT 1000 kW!)
        self.store.create_project("Utility120MW.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
        trf_res = configure_transformers(self.store, project="Utility120MW.PRJ", source_variant="VC0", new_variant="VC1",
            mv={"count": 38, "output_voltage_kv": 35.0, "iron_loss_frac": 0.00095, "copper_loss_frac": 0.010},
            hv={"output_voltage_kv": 110.0, "nominal_power_kva": 130000.0, "iron_loss_kw": 84.5, "copper_loss_kw": 480.0}, dry_run=False)
        self.assertEqual(trf_res["status"], "created", trf_res)
        self.assertAlmostEqual(trf_res["transformers"]["stc_ref_power_kw"], 129360.0, places=1)
        self.assertEqual(trf_res["transformers"]["stc_ref_power_source"], "pan_subarray_stc")
        self.assertAlmostEqual(trf_res["transformers"]["inverter_ac_power_kw"], 105000.0, places=1)
        self.assertAlmostEqual(trf_res["transformers"]["mv"]["iron_loss_kw"], 105000.0 * 0.00095, places=2)
        # Undersized 1 MVA transformer on 105 MW AC array is blocked by PVsyst 8.1.7 80% undersizing bound
        undersized = configure_transformers(self.store, project="Utility120MW.PRJ", source_variant="VC0", new_variant="VC2",
            mv={"count": 1, "output_voltage_kv": 35.0, "nominal_power_kva": 1000.0, "iron_loss_kw": 1.0, "copper_loss_kw": 5.0}, dry_run=True)
        self.assertEqual(undersized["status"], "blocked")
        self.assertTrue(any("84000" in msg or "80%" in msg for msg in undersized["issues"]), undersized["issues"])
        # Oversized 1000 MVA transformer (>200% of 105 MW AC) is blocked by PVsyst 8.1.7 TransfoPowerGTInverterXX
        oversized = configure_transformers(self.store, project="Utility120MW.PRJ", source_variant="VC0", new_variant="VC2",
            mv={"count": 1, "output_voltage_kv": 35.0, "nominal_power_kva": 1000000.0, "iron_loss_kw": 50.0, "copper_loss_kw": 500.0}, dry_run=True)
        self.assertEqual(oversized["status"], "blocked", oversized)
        self.assertTrue(any("210000" in msg or "200%" in msg for msg in oversized["issues"]), oversized["issues"])
        # P0-4: After transformers on bifacial Sheds, combined soiling + unavailability works in ONE transaction!
        vc1_info = self.store.inspect("Utility120MW.PRJ", "VC1")
        comb = self.store.update_parameters("Utility120MW.PRJ", "VC1", vc1_info["sha256"], soiling_updates={"mode": "constant", "yearly_loss_pct": 2.5},
            system_unavailability_updates={"mode": "random", "time_fraction_pct": 1.5, "period_count": 6}, dry_run=False)
        self.assertTrue(comb["changed"])
        self.assertIn("soiling", comb)
        self.assertIn("system_unavailability", comb)
        # P0-4: orientation_updates (FieldTilt / FieldAzim) and subarray_updates work on bifacial Sheds!
        vc1_info2 = self.store.inspect("Utility120MW.PRJ", "VC1")
        ori_edit = self.store.update_parameters("Utility120MW.PRJ", "VC1", vc1_info2["sha256"],
            orientation_updates={"FieldTilt": 28.0, "FieldAzim": -30.0}, dry_run=False)
        self.assertTrue(ori_edit["changed"])
        vc1_info3 = self.store.inspect("Utility120MW.PRJ", "VC1")
        cap_edit = self.store.update_parameters("Utility120MW.PRJ", "VC1", vc1_info3["sha256"], subarray_updates={"NStringCh": 12600}, dry_run=False)
        self.assertTrue(cap_edit["changed"])
        # P0-4: DC cabling works on bifacial Sheds even after transformers + unavailability!
        dc_res = create_dc_cabling_variant(self.store, project="Utility120MW.PRJ", source_variant="VC1", new_variant="VC3",
            dc={"mode": "percent", "value": 1.25}, dry_run=False)
        self.assertEqual(dc_res["status"], "created", dc_res)
        self.assertEqual(dc_res["dc"]["loss_percent"], "1.25")
        # P0-4: create_shed_variant works on synthesized Sheds with non-zero root Flags + transformers + bifacial!
        shed_res = create_shed_variant(self.store, project="Utility120MW.PRJ", source_variant="VC1", new_variant="VC4",
            geometry={"field_azimuth_deg": 45.0, "pitch_m": 11.5}, dry_run=False)
        self.assertEqual(shed_res["status"], "created", shed_res)
        self.assertEqual(shed_res["geometry"]["FieldAzim"], "45.0")
        self.assertEqual(shed_res["geometry"]["Pitch"], "11.500")
    def _make_demo_utility_vc(self, *, with_grid_limit_kw: float | None = None) -> str:
        """Build a 10,120 kWp DC / 8,000 kW AC ordinary-mode variant matching _DEMO_Utility.VC0."""
        vc0 = (self.root / "Projects" / "TrfTest.VC0").read_text(encoding="utf-8-sig")
        # Ordinary mode: 4 x 2,000 kW inverter (missing on disk like encrypted InverterDB.csv Generic_2000kW.OND)
        vc = (vc0 .replace("      PNomSizing=440.0\n", "") .replace("      GInverter=Lab50.OND", "      GInverter=Generic_2000kW.OND")
            .replace("      NModSerie=20", "      NModSerie=25") .replace("      NStringCh=100", "      NStringCh=920")
            .replace("      NStrOrient1=100", "      NStrOrient1=920") .replace("      NInverter=10", "      NInverter=4")
            .replace("      PNomAC_InMPPT=50.00", "      PNomAC_InMPPT=2000.00") .replace("          NElements=10\n", "          NElements=4\n")
            .replace("            NElements=10\n", "            NElements=230\n"))
        if with_grid_limit_kw is not None:
            vc = vc.replace("  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid",
                f"  PVObject_System=pvSystem\n    Flags=$01000000\n    SystemType=Grid\n    PLimGrid={with_grid_limit_kw:.1f}\n    LimGrSeparLoss=True")
        return vc
    def test_pvsyst817_mv_fractional_uses_inverter_ac_basis_and_warns_on_explicit_stc_mismatch(self) -> None:
        vc_demo = self._make_demo_utility_vc()
        out_text, summary = apply_transformers_to_variant_text(vc_demo, mv={"count": 1,
                "output_voltage_kv": 20.0, "stc_ref_power_kw": 10120.0, "iron_loss_frac": 0.001, "copper_loss_frac": 0.010,
                "cable": {"length_m": 500.0, "metal": "Copper", "section_mm2": 500}}, components=self.comp)
        self.assertAlmostEqual(summary["inverter_ac_power_kw"], 8000.0, places=3)
        self.assertEqual(summary["inverter_ac_power_source"], "subarray_pnom_ac_in_mppt")
        mv = summary["mv"]
        self.assertEqual(mv["loss_basis"], "inverter_ac_nominal")
        # Native simulation iron loss = 0.001 * 8000 kW = 8.0 kW (NOT 10.12 kW)
        self.assertAlmostEqual(mv["iron_loss_kw"], 8.0, places=5)
        self.assertAlmostEqual(mv["main_iron_loss_frac"], 0.001, places=5)
        self.assertAlmostEqual(mv["main_copper_loss_frac"], 0.010, places=5)
        self.assertAlmostEqual(mv["stc_iron_loss_frac"], 8.0 / 10120.0, places=5)
        self.assertAlmostEqual(mv["stc_copper_loss_frac"], 0.010 * 10120.0 / 8000.0, places=5)
        self.assertIn("TrfIronLossFrac=0.00100", out_text)
        self.assertIn("TrfResLossFrac=0.01000", out_text)
        self.assertIn("STCIronLoss=8.00000", out_text)
        self.assertIn("STCIronLossFrac=0.00079", out_text)
        self.assertIn("STCCopperLossFrac=0.01265", out_text)
        self.assertTrue(any("PNomInvAC=8000 kW" in w for w in summary["warnings"]), summary["warnings"])
    def test_pvsyst817_hv_and_mv_native_power_bounds_and_grid_limit(self) -> None:
        vc_demo = self._make_demo_utility_vc()
        mv_ok = {"count": 1, "output_voltage_kv": 20.0, "stc_ref_power_kw": 10120.0, "iron_loss_frac": 0.001, "copper_loss_frac": 0.010}
        # Issue #255 reproducer: 20,240 kVA HV on 8,000 kW inverter (> 200% = 16,000 kVA) MUST be rejected
        with self.assertRaisesRegex(ValueError, "TransfoPowerGTInverterXX"):
            apply_transformers_to_variant_text(vc_demo, mv=mv_ok,
                hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 20240.0, "iron_loss_kw": 50.6, "copper_loss_kw": 404.8},
                components=self.comp)
        # Exact native upper (16,000 kVA = 2.00x) and lower (6,400 kVA = 0.80x) bounds pass
        for valid_kva in (6400.0, 16000.0):
            _, s = apply_transformers_to_variant_text(vc_demo, mv=mv_ok,
                hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": valid_kva, "iron_loss_kw": 40.0, "copper_loss_kw": 250.0},
                components=self.comp)
            self.assertAlmostEqual(s["hv"]["ds_nom_power"], valid_kva, places=2)
        # Strictly below 80% (6,399 kVA) and above 200% (16,001 kVA) fail
        with self.assertRaisesRegex(ValueError, "TransfoPowerLTInverterXX"):
            apply_transformers_to_variant_text(vc_demo, mv=mv_ok,
                hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 6399.0, "iron_loss_kw": 40.0, "copper_loss_kw": 250.0},
                components=self.comp)
        with self.assertRaisesRegex(ValueError, "TransfoPowerGTInverterXX"):
            apply_transformers_to_variant_text(vc_demo, mv=mv_ok,
                hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 16001.0, "iron_loss_kw": 40.0, "copper_loss_kw": 250.0},
                components=self.comp)
        # Active PLimGrid=5000 kW lowers the 80% floor to 4000 kVA (matching TransfoPowerLTGridLimXX)
        vc_lim = self._make_demo_utility_vc(with_grid_limit_kw=5000.0)
        _, s_lim = apply_transformers_to_variant_text(vc_lim, mv=mv_ok,
            hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 4000.0, "iron_loss_kw": 20.0, "copper_loss_kw": 150.0},
            components=self.comp)
        self.assertAlmostEqual(s_lim["grid_limit_kw"], 5000.0, places=2)
        with self.assertRaisesRegex(ValueError, "PLimGrid=5000"):
            apply_transformers_to_variant_text(vc_lim, mv=mv_ok,
                hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 3999.0, "iron_loss_kw": 20.0, "copper_loss_kw": 150.0},
                components=self.comp)
        # Omitted hv.nominal_power_kva defaults to 1.15x PNomInvAC (9,200 kVA), safe even at high DC/AC
        _, s_def = apply_transformers_to_variant_text(vc_demo, mv=mv_ok,
            hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 18000.0}, components=self.comp)
        self.assertAlmostEqual(s_def["hv"]["ds_nom_power"], 9200.0, places=2)
    def test_pvsyst817_parallel_mv_datasheet_scaling_and_native_checks(self) -> None:
        vc_demo = self._make_demo_utility_vc()
        # 2 x 4,500 kVA MV datasheet transformers (4 kW iron, 40 kW copper per unit) on 8,000 kW AC / 10,120 kWp DC
        out_text, summary = apply_transformers_to_variant_text(vc_demo, mv={"count": 2,
                "output_voltage_kv": 20.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 4500.0, "iron_loss_kw": 4.0,
                "copper_loss_kw": 40.0}, components=self.comp)
        mv = summary["mv"]
        self.assertAlmostEqual(mv["iron_loss_kw"], 8.0, places=5)
        self.assertAlmostEqual(mv["stc_iron_loss_frac"], 8.0 / 10120.0, places=5)
        self.assertAlmostEqual(mv["stc_copper_loss_frac"], 40.0 * (10120.0 / 2.0) / (4500.0 ** 2), places=5)
        self.assertIn("STCIronLoss=8.00000", out_text)
        self.assertIn("MVTrfDSNomPower=4500.00", out_text)
        self.assertIn("MVTrfDSIronLoss=4.0000", out_text)
        self.assertIn("MVTrfDSCopperLoss=40.0000", out_text)
        # Parallel MV per-unit oversizing: 2 x 8,100 kVA (> 2.0 * 4,000 kW per unit) rejected
        with self.assertRaisesRegex(ValueError, "TransfoPowerGTInverterXX"):
            apply_transformers_to_variant_text(vc_demo,
                mv={"count": 2, "output_voltage_kv": 20.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 8100.0, "iron_loss_kw": 4.0, "copper_loss_kw": 40.0},
                components=self.comp)
        # Datasheet zero iron/copper rejected (TransfoDataSheetsNotWellDef)
        with self.assertRaisesRegex(ValueError, "must be > 0"):
            apply_transformers_to_variant_text(vc_demo,
                mv={"count": 1, "output_voltage_kv": 20.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 9000.0, "iron_loss_kw": 8.0, "copper_loss_kw": 0.0},
                components=self.comp)
        with self.assertRaisesRegex(ValueError, "must be > 0"):
            apply_transformers_to_variant_text(vc_demo,
                mv={"count": 1, "output_voltage_kv": 20.0, "stc_ref_power_kw": 10120.0, "iron_loss_frac": 0.001, "copper_loss_frac": 0.010},
                hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 15000.0, "iron_loss_kw": 0.0, "copper_loss_kw": 300.0},
                components=self.comp)
        # Native loss upper bounds: HV iron/nom > 1% and copper/nom > 5% rejected; MV STC copper > 5% rejected
        with self.assertRaisesRegex(ValueError, "iron/nom <= 1%"):
            apply_transformers_to_variant_text(vc_demo,
                mv={"count": 1, "output_voltage_kv": 20.0, "stc_ref_power_kw": 10120.0, "iron_loss_frac": 0.001, "copper_loss_frac": 0.010},
                hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 15000.0, "iron_loss_kw": 200.0, "copper_loss_kw": 300.0},
                components=self.comp)
        with self.assertRaisesRegex(ValueError, "copper/nom <= 5%"):
            apply_transformers_to_variant_text(vc_demo,
                mv={"count": 1, "output_voltage_kv": 20.0, "stc_ref_power_kw": 10120.0, "iron_loss_frac": 0.001, "copper_loss_frac": 0.010},
                hv={"output_voltage_kv": 110.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 15000.0, "iron_loss_kw": 40.0, "copper_loss_kw": 825.0},
                components=self.comp)
        with self.assertRaisesRegex(ValueError, "copper <= 5%"):
            apply_transformers_to_variant_text(vc_demo,
                mv={"count": 1, "output_voltage_kv": 20.0, "stc_ref_power_kw": 10120.0, "iron_loss_frac": 0.001, "copper_loss_frac": 0.045},
                components=self.comp)
        # Voltage ordering: HV < MV rejected (TransfoHT_DefGridVoltage)
        with self.assertRaisesRegex(ValueError, "TransfoHT_DefGridVoltage"):
            apply_transformers_to_variant_text(vc_demo,
                mv={"count": 1, "output_voltage_kv": 35.0, "stc_ref_power_kw": 10120.0, "iron_loss_frac": 0.001, "copper_loss_frac": 0.010},
                hv={"output_voltage_kv": 30.0, "stc_ref_power_kw": 10120.0, "nominal_power_kva": 10000.0, "iron_loss_kw": 10.0, "copper_loss_kw": 50.0},
                components=self.comp)
    def test_in_place_single_mv_night_disconnect_preserves_flags_wire_section_and_stc(self) -> None:
        """Issue #274: night_disconnect-only update preserves cable section, bits 23/24, and STC ref."""
        import re
        from unittest.mock import patch
        from pvsyst_auto.api.projects import pvsyst_configure_transformers
        from pvsyst_auto.api.schemas import MvTransformerSpec
        init_res = configure_transformers(self.store, project="TrfTest.PRJ", source_variant="VC0",
            new_variant="VC1", mv={"count": 1, "output_voltage_kv": 20.0,
                "stc_ref_power_kw": 440.0, "night_disconnect": False, "cable": {"length_m": 450.0, "metal": "Copper", "section_mm2": 150}}, dry_run=False)
        self.assertEqual(init_res["status"], "created", init_res)
        mv_path = self.root / "Projects" / "TrfTest.VC1"
        vc_mv = mv_path.read_text(encoding="utf-8-sig")
        vc_mv = re.sub(r"(PVObject_PVMainArray=pvMainArray\r?\n\s+Flags=)\$[0-9A-Fa-f]+", r"\1$01F00400", vc_mv, count=1)
        vc_mv = vc_mv.replace("NightDisconnect=False", "MVWireNoSect=2\r\n          NightDisconnect=False", 1)
        mv_path.write_text("\ufeff" + vc_mv, encoding="utf-8")
        stc_before = [line.strip() for line in vc_mv.splitlines() if "STC" in line]
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            on_res = pvsyst_configure_transformers("TrfTest.PRJ", "VC1", "VC2", mv=MvTransformerSpec(night_disconnect=True), dry_run=False)
            self.assertEqual(on_res["status"], "created", on_res)
            vc_on = (self.root / "Projects" / "TrfTest.VC2").read_text(encoding="utf-8-sig")
            self.assertIn("Flags=$21F00400", vc_on)
            self.assertIn("MVWireNoSect=2", vc_on)
            self.assertIn("NightDisconnect=True", vc_on)
            self.assertEqual([line.strip() for line in vc_on.splitlines() if "STC" in line], stc_before)
            off_res = pvsyst_configure_transformers("TrfTest.PRJ", "VC2", "VC3", mv=MvTransformerSpec(night_disconnect=False), dry_run=False)
            self.assertEqual(off_res["status"], "created", off_res)
            vc_off = (self.root / "Projects" / "TrfTest.VC3").read_text(encoding="utf-8-sig")
            self.assertIn("Flags=$01F00400", vc_off)
            self.assertIn("MVWireNoSect=2", vc_off)
            self.assertIn("NightDisconnect=False", vc_off)
            self.assertEqual([line.strip() for line in vc_off.splitlines() if "STC" in line], stc_before)
if __name__ == "__main__": unittest.main()

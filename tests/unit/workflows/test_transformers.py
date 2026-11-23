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
        synth = synthesize_project_and_variant(
            self.store,
            project_comment="TrfTest",
            variant_comment="Base",
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            n_mod_serie=20,
            n_string_ch=100,
            n_inverter=10,
            n_inv_mppt=10,
            tilt_deg=25.0,
            azimuth_deg=0.0,
        )
        vc0 = synth["vc0_text"].replace("      NoOrientation=1", "      PNomSizing=440.0\n      NoOrientation=1")
        self.store.create_project("TrfTest.PRJ", synth["prj_text"], {"VC0": vc0})

    def test_public_tool_requires_reference_when_source_has_none(self) -> None:
        from unittest.mock import patch
        from pvsyst_auto.api.projects import pvsyst_configure_transformers
        from pvsyst_auto.api.schemas import MvTransformerSpec

        source = self.root / "Projects" / "TrfTest.VC0"
        text = (
            source.read_text(encoding="utf-8-sig")
            .replace("      PNomSizing=440.0\n", "")
            .replace("      PVModule=Lab440.PAN\n", "      PVModule=Missing.PAN\n")
        )
        source.write_text(text, encoding="utf-8")
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            result = pvsyst_configure_transformers(
                "TrfTest.PRJ", "VC0", "VC1", mv=MvTransformerSpec(), dry_run=False,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertIn("stc_ref_power_kw", " ".join(result["issues"]))
            self.assertFalse((self.root / "Projects" / "TrfTest.VC1").exists())
            result = pvsyst_configure_transformers(
                "TrfTest.PRJ", "VC0", "VC1", mv=MvTransformerSpec(stc_ref_power_kw=500), dry_run=False,
            )
            self.assertEqual(result["status"], "created")
            self.assertEqual(result["transformers"]["mv"]["stc_ref_power_kw"], 500)

    def test_public_hv_fraction_matches_generated_datasheet(self) -> None:
        from unittest.mock import patch
        from pvsyst_auto.api.projects import pvsyst_configure_transformers
        from pvsyst_auto.api.schemas import HvTransformerSpec, MvTransformerSpec

        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            result = pvsyst_configure_transformers(
                "TrfTest.PRJ", "VC0", "VC1", mv=MvTransformerSpec(stc_ref_power_kw=1000),
                hv=HvTransformerSpec(stc_ref_power_kw=1000, nominal_power_kva=2000,
                                     iron_loss_frac=0.005, copper_loss_frac=0.01), dry_run=False,
            )
        self.assertEqual(result["status"], "created")
        hv = result["transformers"]["hv"]
        self.assertAlmostEqual(hv["ds_iron_loss"] / hv["stc_ref_power_kw"], hv["iron_loss_frac"])
        self.assertAlmostEqual(hv["ds_copper_loss"] * hv["stc_ref_power_kw"] / hv["ds_nom_power"]**2,
                               hv["copper_loss_frac"])

    def test_mv_only_then_mv_hv_cascade_then_remove(self) -> None:
        # 1. Dry-run preview writes nothing
        prev = configure_transformers(
            self.store, project="TrfTest.PRJ", source_variant="VC0", new_variant="VC1",
            mv={
                "count": 2, "output_voltage_kv": 20.0, "stc_ref_power_kw": 440.0,
                "iron_loss_frac": 0.0012, "copper_loss_frac": 0.011,
                "night_disconnect": True,
                "cable": {"length_m": 450.0, "metal": "Copper", "section_mm2": 150},
            },
            dry_run=True,
        )
        self.assertEqual(prev["status"], "ready")
        self.assertFalse((self.root / "Projects" / "TrfTest.VC1").exists())

        # 2. Execute MV-only with 2 parallel transformers + MV cable + night disconnect
        mv_res = configure_transformers(
            self.store, project="TrfTest.PRJ", source_variant="VC0", new_variant="VC1",
            mv={
                "count": 2, "output_voltage_kv": 20.0, "stc_ref_power_kw": 440.0,
                "iron_loss_frac": 0.0012, "copper_loss_frac": 0.011,
                "night_disconnect": True,
                "cable": {"length_m": 450.0, "metal": "Copper", "section_mm2": 150},
            },
            dry_run=False,
        )
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
        hv_res = configure_transformers(
            self.store, project="TrfTest.PRJ", source_variant="VC1", new_variant="VC2",
            mv={
                "count": 2, "output_voltage_kv": 35.0, "stc_ref_power_kw": 440.0,
                "nominal_power_kva": 1250.0, "iron_loss_kw": 1.1, "copper_loss_kw": 12.0,
                "night_disconnect": False,
                "cable": {"length_m": 600.0, "metal": "Copper", "section_mm2": 185},
            },
            hv={
                "output_voltage_kv": 110.0, "stc_ref_power_kw": 440.0,
                "nominal_power_kva": 3000.0, "iron_loss_kw": 2.4, "copper_loss_kw": 21.0,
                "night_disconnect": False,
                "cable": {"length_m": 2500.0, "metal": "Aluminium", "section_mm2": 240},
            },
            dry_run=False,
        )
        self.assertEqual(hv_res["status"], "created")
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
        self.assertIn("HVTrfDSNomPower=3000.00", vc2_text)
        self.assertIn("MVTrfDSNomPower=1250.00", vc2_text)

        # 4. Strip all transformers cleanly VC2 -> VC3
        rm_res = configure_transformers(
            self.store, project="TrfTest.PRJ", source_variant="VC2", new_variant="VC3",
            remove_transformers=True, dry_run=False,
        )
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
        cache = ("  VarList, list of=1 TVarSimul\n    VarSimul_1=E_Grid\n"
                 "    End of VarSimul=E_Grid\n  End of the List VarList\n")
        text = text.replace("End of PVObject pvVCalcul", cache + "End of PVObject pvVCalcul")
        source.write_text(text, encoding="utf-8")
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            result = pvsyst_configure_transformers(
                "TrfTest.PRJ", "VC0", "VC1", mv=MvTransformerSpec(), dry_run=False,
            )
        self.assertEqual(result["status"], "created")
        output = (self.root / "Projects" / "TrfTest.VC1").read_text(encoding="utf-8-sig")
        self.assertNotIn("VarList, list of=", output)
        self.assertEqual(int(output.splitlines()[3].split("$")[1], 16) & 1, 0)

    def test_validation_rejects_invalid_combinations(self) -> None:
        r1 = configure_transformers(
            self.store, project="TrfTest.PRJ", source_variant="VC0", new_variant="VC9",
            mv={"count": 1}, remove_transformers=True, dry_run=True,
        )
        self.assertEqual(r1["status"], "blocked")
        vc0 = (self.root / "Projects" / "TrfTest.VC0").read_text(encoding="utf-8-sig")
        with self.assertRaisesRegex(ValueError, "section_mm2"):
            apply_transformers_to_variant_text(vc0, mv={"stc_ref_power_kw": 440, "cable": {"length_m": 100, "section_mm2": 999}})
        # Without components / PNomSizing / STCRefPower / explicit stc_ref_power_kw, must NOT silently default to 1000 kW
        vc0_no_ref = vc0.replace("      PNomSizing=440.0\n", "")
        with self.assertRaisesRegex(ValueError, "stc_ref_power_kw"):
            apply_transformers_to_variant_text(vc0_no_ref, mv={"iron_loss_frac": 0.001})

    def test_auto_infers_120mw_stc_ref_power_from_pan_and_bom_ground_sheds_workflow(self) -> None:
        from pvsyst_auto.energy.dc import create_dc_cabling_variant
        from pvsyst_auto.project.sheds import create_shed_variant
        from pvsyst_auto.project.synthesize import synthesize_utility_project

        # 1. Synthesize 129.36 MWp bifacial ground_sheds variant with UTF-8 BOM (28 * 10500 * 440 Wp = 129,360 kWp)
        synth = synthesize_utility_project(
            self.store,
            project_comment="Utility120MW",
            variant_comment="BaseSheds",
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            field_type="Sheds",
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            n_mod_serie=28,
            n_string_ch=10500,
            n_inverter=350,
            n_inv_mppt=2100,
            independent_mppt=True,
            pnom_ac_in_mppt=50.0,
            tilt_deg=25.0,
            azimuth_deg=0.0,
            pitch_m=10.0,
            collector_width_m=4.6,
            shed_count=50,
            bifacial={"enabled": True, "ground_albedo": 0.25},
            with_bom=True,
        )
        # P0-2: apply_transformers_to_variant_text handles leading UTF-8 BOM cleanly without Unmatched closing tag
        vc_trf, trf_sum = apply_transformers_to_variant_text(
            synth["vc0_text"],
            mv={"count": 38, "output_voltage_kv": 35.0, "iron_loss_frac": 0.00095, "copper_loss_frac": 0.010},
            hv={"output_voltage_kv": 110.0, "nominal_power_kva": 130000.0, "iron_loss_kw": 84.5, "copper_loss_kw": 480.0},
            default_ref_power_kw=129360.0,
        )
        self.assertTrue(vc_trf.startswith("\ufeff"))
        self.assertAlmostEqual(trf_sum["mv"]["stc_ref_power_kw"], 129360.0, places=1)
        self.assertAlmostEqual(trf_sum["mv"]["iron_loss_kw"], 129360.0 * 0.00095, places=1)

        # P0-1: configure_transformers auto-infers 129,360 kWp from Lab440.PAN (NOT 1000 kW!)
        self.store.create_project("Utility120MW.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
        trf_res = configure_transformers(
            self.store, project="Utility120MW.PRJ", source_variant="VC0", new_variant="VC1",
            mv={"count": 38, "output_voltage_kv": 35.0, "iron_loss_frac": 0.00095, "copper_loss_frac": 0.010},
            hv={"output_voltage_kv": 110.0, "nominal_power_kva": 130000.0, "iron_loss_kw": 84.5, "copper_loss_kw": 480.0},
            dry_run=False,
        )
        self.assertEqual(trf_res["status"], "created", trf_res)
        self.assertAlmostEqual(trf_res["transformers"]["stc_ref_power_kw"], 129360.0, places=1)
        self.assertEqual(trf_res["transformers"]["stc_ref_power_source"], "pan_subarray_stc")
        self.assertAlmostEqual(trf_res["transformers"]["mv"]["iron_loss_kw"], 129360.0 * 0.00095, places=2)

        # Undersized 1 MVA transformer on 129.36 MWp array fails copper bounds WITH inferred stc_ref_power_kw in error
        undersized = configure_transformers(
            self.store, project="Utility120MW.PRJ", source_variant="VC0", new_variant="VC2",
            mv={"count": 1, "output_voltage_kv": 35.0, "nominal_power_kva": 1000.0, "iron_loss_kw": 1.0, "copper_loss_kw": 5.0},
            dry_run=True,
        )
        self.assertEqual(undersized["status"], "blocked")
        self.assertTrue(any("129360" in msg for msg in undersized["issues"]), undersized["issues"])

        # Oversized transformer (>5x array STC) passes dry_run with a clear magnitude mismatch warning
        mismatch = configure_transformers(
            self.store, project="Utility120MW.PRJ", source_variant="VC0", new_variant="VC2",
            mv={"count": 1, "output_voltage_kv": 35.0, "nominal_power_kva": 1000000.0, "iron_loss_kw": 50.0, "copper_loss_kw": 500.0},
            dry_run=True,
        )
        self.assertEqual(mismatch["status"], "ready", mismatch)
        self.assertTrue(any("stc_ref_power_kw" in w for w in mismatch["warnings"]), mismatch["warnings"])

        # P0-4: After transformers on bifacial Sheds, combined soiling + unavailability works in ONE transaction!
        vc1_info = self.store.inspect("Utility120MW.PRJ", "VC1")
        comb = self.store.update_parameters(
            "Utility120MW.PRJ", "VC1", vc1_info["sha256"],
            soiling_updates={"mode": "constant", "yearly_loss_pct": 2.5},
            system_unavailability_updates={"mode": "random", "time_fraction_pct": 1.5, "period_count": 6},
            dry_run=False,
        )
        self.assertTrue(comb["changed"])
        self.assertIn("soiling", comb)
        self.assertIn("system_unavailability", comb)

        # P0-4: orientation_updates (FieldTilt / FieldAzim) and subarray_updates work on bifacial Sheds!
        vc1_info2 = self.store.inspect("Utility120MW.PRJ", "VC1")
        ori_edit = self.store.update_parameters(
            "Utility120MW.PRJ", "VC1", vc1_info2["sha256"],
            orientation_updates={"FieldTilt": 28.0, "FieldAzim": -30.0},
            dry_run=False,
        )
        self.assertTrue(ori_edit["changed"])

        vc1_info3 = self.store.inspect("Utility120MW.PRJ", "VC1")
        cap_edit = self.store.update_parameters(
            "Utility120MW.PRJ", "VC1", vc1_info3["sha256"],
            subarray_updates={"NStringCh": 12600},
            dry_run=False,
        )
        self.assertTrue(cap_edit["changed"])

        # P0-4: DC cabling works on bifacial Sheds even after transformers + unavailability!
        dc_res = create_dc_cabling_variant(
            self.store, project="Utility120MW.PRJ", source_variant="VC1", new_variant="VC3",
            dc={"mode": "percent", "value": 1.25}, dry_run=False,
        )
        self.assertEqual(dc_res["status"], "created", dc_res)
        self.assertEqual(dc_res["dc"]["loss_percent"], "1.25")

        # P0-4: create_shed_variant works on synthesized Sheds with non-zero root Flags + transformers + bifacial!
        shed_res = create_shed_variant(
            self.store, project="Utility120MW.PRJ", source_variant="VC1", new_variant="VC4",
            geometry={"field_azimuth_deg": 45.0, "pitch_m": 11.5}, dry_run=False,
        )
        self.assertEqual(shed_res["status"], "created", shed_res)
        self.assertEqual(shed_res["geometry"]["FieldAzim"], "45.0")
        self.assertEqual(shed_res["geometry"]["Pitch"], "11.500")


if __name__ == "__main__":
    unittest.main()

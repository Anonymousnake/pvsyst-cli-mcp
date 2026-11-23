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
        self.store.create_project("TrfTest.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})

    def test_mv_only_then_mv_hv_cascade_then_remove(self) -> None:
        # 1. Dry-run preview writes nothing
        prev = configure_transformers(
            self.store, project="TrfTest.PRJ", source_variant="VC0", new_variant="VC1",
            mv={
                "count": 2, "output_voltage_kv": 20.0,
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
                "count": 2, "output_voltage_kv": 20.0,
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
                "count": 2, "output_voltage_kv": 35.0,
                "nominal_power_kva": 1250.0, "iron_loss_kw": 1.1, "copper_loss_kw": 12.0,
                "night_disconnect": False,
                "cable": {"length_m": 600.0, "metal": "Copper", "section_mm2": 185},
            },
            hv={
                "output_voltage_kv": 110.0,
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
            apply_transformers_to_variant_text(vc0, mv={"cable": {"length_m": 100, "section_mm2": 999}})


if __name__ == "__main__":
    unittest.main()

"""Unit tests for commercial component filtering, metadata indexing, and zero-template synthesis."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.components.builder import build_sourced_component
from pvsyst_auto.components.single_diode import (
    derive_single_diode_parameters,
    synthesize_ond_from_datasheet,
    synthesize_pan_from_datasheet,
)
from pvsyst_auto.components.store import ComponentStore


class CommercialComponentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "work"
        for sub in ("ComposPV/PVmodules", "ComposPV/Inverters", "Results"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.store = ComponentStore(self.root)

    def test_single_diode_solver_and_zero_template_pan_synthesis(self):
        # Realistic 620 Wp N-type TOPCon bifacial module (72 half-cut cells)
        diode = derive_single_diode_parameters(
            voc=52.8, isc=14.65, vmp=44.6, imp=13.91, n_cells_s=72,
        )
        self.assertGreater(diode["RShunt"], 100.0)
        self.assertGreater(diode["RSerie"], 0.01)
        self.assertGreaterEqual(diode["Gamma"], 0.85)
        self.assertLessEqual(diode["Gamma"], 1.50)
        self.assertLess(diode["mpp_slope_residual_a"], 0.15)

        pan_text, meta = synthesize_pan_from_datasheet(
            manufacturer="ApexSolar",
            model="HiTOP 620W Bifacial",
            pnom_w=620.0,
            isc_a=14.65,
            voc_v=52.8,
            imp_a=13.91,
            vmp_v=44.6,
            width_m=1.134,
            height_m=2.382,
            n_cells_s=72,
            n_cells_p=2,
            bifaciality_factor=0.80,
            vmax_iec_v=1500.0,
        )
        self.assertTrue(meta["bifacial"])
        self.assertEqual(meta["flags"], "$00800000")
        self.assertIn("BifacialityFactor=0.800", pan_text)
        report = self.store._inspect("PAN", pan_text.encode("utf-8"))
        self.assertEqual(report["structural_errors"], [])
        self.assertEqual(report["known_value_errors"], [])

    def test_zero_template_ond_synthesis_validates_cleanly(self):
        ond_text, meta = synthesize_ond_from_datasheet(
            manufacturer="GridPower",
            model="SG110CX-V2",
            pnom_ac_kw=110.0,
            pmax_ac_kw=110.0,
            vmpp_min_v=200.0,
            vmpp_max_v=1000.0,
            vabs_max_v=1100.0,
            effic_max_pct=98.7,
            effic_euro_pct=98.4,
            nb_mppt=9,
            nb_inputs=18,
            idc_max_a=270.0,
        )
        self.assertEqual(meta["nb_mppt"], 9)
        self.assertEqual(meta["curve_points"], 8)
        report = self.store._inspect("OND", ond_text.encode("utf-8"))
        self.assertEqual(report["structural_errors"], [])
        self.assertEqual(report["known_value_errors"], [])

    def test_build_sourced_component_zero_template_pan_and_ond_roundtrip(self):
        sources = {
            "DS1": {
                "kind": "manufacturer",
                "title": "ApexSolar 620W Datasheet",
                "locator": "ApexSolar_HiTOP620B_rev1.pdf",
                "text": (
                    "ApexSolar HiTOP-620B: PNom 620 W, Voc 52.8 V, Isc 14.65 A, "
                    "Vmp 44.6 V, Imp 13.91 A, Width 1134 mm, Height 2382 mm, "
                    "BifacialityFactor 80 %, muVocSpec -0.25 %/degC, MuISC 0.045 %/degC, "
                    "VMaxIEC 1500 V, NCelS 72 cells."
                ),
            }
        }
        def decl(field, val, unit, excerpt):
            return {
                "field": field,
                "value": val,
                "unit": unit,
                "source_id": "DS1",
                "excerpt": excerpt,
                "source_value_text": str(val),
                "source_unit_text": unit,
            }

        declarations = [
            decl("PNom", 620, "W", "PNom 620 W"),
            decl("Voc", 52.8, "V", "Voc 52.8 V"),
            decl("ISC", 14.65, "A", "Isc 14.65 A"),
            decl("Vmp", 44.6, "V", "Vmp 44.6 V"),
            decl("Imp", 13.91, "A", "Imp 13.91 A"),
            decl("Width", 1134, "mm", "Width 1134 mm"),
            decl("Height", 2382, "mm", "Height 2382 mm"),
            decl("BifacialityFactor", 80, "%", "BifacialityFactor 80 %"),
            decl("muVocSpec", -0.25, "%/degC", "muVocSpec -0.25 %/degC"),
            decl("MuISC", 0.045, "%/degC", "MuISC 0.045 %/degC"),
            decl("VMaxIEC", 1500, "V", "VMaxIEC 1500 V"),
            decl("NCelS", 72, "cells", "NCelS 72 cells"),
        ]
        preview = build_sourced_component(
            self.store,
            component_type="PAN",
            template_name=None,
            new_name="Apex_620W_Bifi.PAN",
            manufacturer="ApexSolar",
            model="HiTOP-620B",
            sources=sources,
            declarations=declarations,
            dry_run=True,
        )
        self.assertEqual(preview["status"], "needs_review", preview["issues"])
        self.assertTrue(preview["synthesized"])
        self.assertTrue(preview["model_refitted"])
        self.assertFalse((self.root / "ComposPV" / "PVmodules" / "Apex_620W_Bifi.PAN").exists())

        blocked = build_sourced_component(
            self.store,
            component_type="PAN",
            template_name=None,
            new_name="Apex_620W_Bifi.PAN",
            manufacturer="ApexSolar",
            model="HiTOP-620B",
            sources=sources,
            declarations=declarations,
            dry_run=False,
            acknowledge_inherited_model=False,
        )
        self.assertEqual(blocked["status"], "blocked")

        created = build_sourced_component(
            self.store,
            component_type="PAN",
            template_name=None,
            new_name="Apex_620W_Bifi.PAN",
            manufacturer="ApexSolar",
            model="HiTOP-620B",
            sources=sources,
            declarations=declarations,
            dry_run=False,
            acknowledge_inherited_model=True,
        )
        self.assertTrue(created["component_created"])
        self.assertTrue((self.root / "ComposPV" / "PVmodules" / "Apex_620W_Bifi.PAN").is_file())
        self.assertTrue((self.root / "Results" / "Apex_620W_Bifi.PAN.sources.json").is_file())

    def test_component_electrical_filtering_and_metadata_index_cache(self):
        pan_mono, _ = synthesize_pan_from_datasheet(
            manufacturer="Alpha", model="Mono-450", pnom_w=450.0, isc_a=11.5, voc_v=49.2,
            imp_a=10.8, vmp_v=41.7, width_m=1.05, height_m=2.10, vmax_iec_v=1000.0,
        )
        pan_bifi, _ = synthesize_pan_from_datasheet(
            manufacturer="Beta", model="Bifi-620", pnom_w=620.0, isc_a=14.6, voc_v=52.8,
            imp_a=13.9, vmp_v=44.6, width_m=1.13, height_m=2.38, bifaciality_factor=0.80, vmax_iec_v=1500.0,
        )
        ond_small, _ = synthesize_ond_from_datasheet(
            manufacturer="Gamma", model="Inv-10kW", pnom_ac_kw=10.0,
            vmpp_min_v=200.0, vmpp_max_v=850.0, vabs_max_v=1000.0, nb_mppt=2,
        )
        ond_big, _ = synthesize_ond_from_datasheet(
            manufacturer="Delta", model="Inv-110kW", pnom_ac_kw=110.0,
            vmpp_min_v=200.0, vmpp_max_v=1000.0, vabs_max_v=1500.0, nb_mppt=9,
        )
        (self.root / "ComposPV" / "PVmodules" / "Alpha_450.PAN").write_text(pan_mono, encoding="utf-8")
        (self.root / "ComposPV" / "PVmodules" / "Beta_620.PAN").write_text(pan_bifi, encoding="utf-8")
        (self.root / "ComposPV" / "Inverters" / "Gamma_10k.OND").write_text(ond_small, encoding="utf-8")
        (self.root / "ComposPV" / "Inverters" / "Delta_110k.OND").write_text(ond_big, encoding="utf-8")

        bifi_res = self.store.list("PAN", bifacial=True, min_power_w=550.0, min_system_voltage_v=1500.0)
        self.assertEqual(bifi_res["total"], 1)
        self.assertEqual(bifi_res["items"][0]["name"], "Beta_620.PAN")
        self.assertEqual(bifi_res["items"][0]["specs"]["pnom_w"], 620.0)
        self.assertTrue(bifi_res["items"][0]["specs"]["bifacial"])

        mono_res = self.store.list("PAN", bifacial=False, max_power_w=500.0)
        self.assertEqual([i["name"] for i in mono_res["items"]], ["Alpha_450.PAN"])

        ond_res = self.store.list("OND", min_power_w=50_000.0, min_mppt=6, min_system_voltage_v=1500.0)
        self.assertEqual([i["name"] for i in ond_res["items"]], ["Delta_110k.OND"])
        self.assertEqual(ond_res["items"][0]["specs"]["nb_mppt"], 9)

        # Verify workspace index persisted & cache hit avoids re-reading unchanged files
        index_path = self.root / "ComposPV" / ".mcp-component-index.json"
        self.assertTrue(index_path.is_file())
        saved_index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertIn("PAN", saved_index)
        self.assertIn("OND", saved_index)
        with patch.object(self.store, "_read", side_effect=AssertionError("Should hit mtime cache")):
            cached = self.store.list("PAN", manufacturer="Beta")
        self.assertEqual(cached["total"], 1)


if __name__ == "__main__":
    unittest.main()

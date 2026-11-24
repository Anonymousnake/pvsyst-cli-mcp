"""Unit tests for commercial component filtering, metadata indexing, and zero-template synthesis."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.api.components import pvsyst_build_sourced_component
from pvsyst_auto.components.builder import build_sourced_component
from pvsyst_auto.components.single_diode import (
    derive_single_diode_parameters,
    synthesize_btr_from_datasheet,
    synthesize_ond_from_datasheet,
    synthesize_pan_from_datasheet,
)
from pvsyst_auto.components.store import ComponentStore


class CommercialComponentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "work"
        for sub in ("ComposPV/PVmodules", "ComposPV/Inverters", "ComposPV/Batteries", "Results"):
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
        self.assertFalse(meta["single_diode"]["fallback"])
        self.assertEqual(meta["flags"], "$00800000")
        self.assertIn("BifacialityFactor=0.800", pan_text)
        report = self.store._inspect("PAN", pan_text.encode("utf-8"))
        self.assertEqual(report["structural_errors"], [])
        self.assertEqual(report["known_value_errors"], [])

    def test_fallback_reports_actual_mpp_slope_residual(self):
        diode = derive_single_diode_parameters(voc=50.0, isc=13.0, vmp=42.0, imp=12.74, n_cells_s=96)
        self.assertTrue(diode["fallback"])
        self.assertGreater(diode["mpp_slope_residual_a"], 1.0)

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

    def test_inverter_curve_does_not_exceed_declared_maximum_efficiency(self):
        for emax, eeuro in ((90.0, 89.0), (75.0, 73.0), (50.0, 48.0)):
            with self.subTest(effic_max_pct=emax, effic_euro_pct=eeuro):
                text, _ = synthesize_ond_from_datasheet(
                    manufacturer="Lab", model="Inverter", pnom_ac_kw=10,
                    vmpp_min_v=200, vmpp_max_v=850, vabs_max_v=1000,
                    effic_max_pct=emax, effic_euro_pct=eeuro,
                )
                points = [tuple(map(int, line.split("=", 1)[1].split(",")))
                          for line in text.splitlines() if line.strip().startswith("Point_")]
                active = [(p_in, p_out) for p_in, p_out in points if p_in > 0 and p_out > 0]
                effs = [p_out / p_in for p_in, p_out in active]
                for eff in effs:
                    self.assertLessEqual(eff, emax / 100.0 + 1e-9)
                # Peak at 50% load (index 5) exceeds both light-load (10%, index 2) and full-load (index 6)
                self.assertGreater(effs[5], effs[2])
                self.assertGreater(effs[5], effs[6])
                self.assertEqual(max(p_out for _, p_out in active), 10000)

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

    def test_zero_template_li_ion_btr_lfp_and_nmc_and_sourced_builder(self) -> None:
        lfp_text, lfp_meta = synthesize_btr_from_datasheet(
            manufacturer="CellPower",
            model="LFP-51V200Ah",
            chemistry="LFP",
            capacity_ah=200.0,
            nominal_voltage_v=51.2,
            n_cells_s=16,
            internal_resistance_mohm=0.8,
            coulombic_efficiency=0.985,
            cycle_life_80dod=6000,
        )
        self.assertEqual(lfp_meta["batt_technol"], "Li_LFP")
        self.assertAlmostEqual(lfp_meta["energy_kwh"], 10.24, places=2)
        self.assertIn("BattTechnol=Li_LFP", lfp_text)
        self.assertIn("CapaCourant=TCubicProfile", lfp_text)
        rep = self.store._inspect("BTR", lfp_text.encode("utf-8"))
        self.assertEqual(rep["structural_errors"], [])
        self.assertEqual(rep["known_value_errors"], [])

        nmc_text, nmc_meta = synthesize_btr_from_datasheet(
            manufacturer="CellPower",
            model="NMC-100kWh",
            chemistry="NMC",
            energy_kwh=100.0,
            nominal_voltage_v=511.0,
        )
        self.assertEqual(nmc_meta["batt_technol"], "Li_NMC")
        self.assertAlmostEqual(nmc_meta["energy_kwh"], 100.0, places=1)

        sources = {
            "DS_BTR": {
                "kind": "manufacturer",
                "title": "LFP Rack Datasheet",
                "locator": "CellPower_LFP200.pdf",
                "text": (
                    "CellPower LFP-51V200Ah: CapNomC10 200 Ah, VNom 51.2 V, "
                    "NCellSerie 16 cells, ResInterne 0.8 mOhm, EfficI 98.5 %, "
                    "IMaxCharge 100 A, IMaxDischarge 200 A, SOHEOL 80 %."
                ),
            }
        }
        def d(field, val, unit, excerpt):
            return {
                "field": field, "value": val, "unit": unit, "source_id": "DS_BTR",
                "excerpt": excerpt, "source_value_text": str(val), "source_unit_text": unit,
            }
        decls = [
            d("CapNomC10", 200, "Ah", "CapNomC10 200 Ah"),
            d("VNom", 51.2, "V", "VNom 51.2 V"),
            d("NCellSerie", 16, "cells", "NCellSerie 16 cells"),
            d("ResInterne", 0.8, "mOhm", "ResInterne 0.8 mOhm"),
            d("EfficI", 98.5, "%", "EfficI 98.5 %"),
            d("IMaxCharge", 100, "A", "IMaxCharge 100 A"),
            d("IMaxDischarge", 200, "A", "IMaxDischarge 200 A"),
            d("SOHEOL", 80, "%", "SOHEOL 80 %"),
        ]
        prev = build_sourced_component(
            self.store, component_type="BTR", new_name="CellPower_LFP200.BTR",
            manufacturer="CellPower", model="LFP-51V200Ah",
            sources=sources, declarations=decls, chemistry="LFP", dry_run=True,
        )
        self.assertEqual(prev["status"], "needs_review")
        self.assertTrue(prev["synthesized"])
        self.assertEqual(prev["synthesized_metadata"]["batt_technol"], "Li_LFP")

        created = build_sourced_component(
            self.store, component_type="BTR", new_name="CellPower_LFP200.BTR",
            manufacturer="CellPower", model="LFP-51V200Ah",
            sources=sources, declarations=decls, chemistry="LFP",
            dry_run=False, acknowledge_inherited_model=True,
        )
        self.assertTrue(created["component_created"])
        self.assertTrue((self.root / "ComposPV" / "Batteries" / "CellPower_LFP200.BTR").is_file())

    def test_three_voltage_ond_synthesis_and_curve_editing(self) -> None:
        ond_text, meta = synthesize_ond_from_datasheet(
            manufacturer="GridPower",
            model="SG110CX-3V",
            pnom_ac_kw=110.0,
            pmax_ac_kw=110.0,
            vmpp_min_v=200.0,
            vmpp_max_v=1000.0,
            vabs_max_v=1100.0,
            effic_max_pct=98.7,
            effic_euro_pct=98.4,
            nb_mppt=9,
            three_voltage_voltages=(450.0, 620.0, 850.0),
            three_voltage_effic_max=(98.3, 98.7, 98.1),
            three_voltage_effic_euro=(98.0, 98.4, 97.8),
        )
        self.assertTrue(meta["three_voltage"])
        self.assertEqual(meta["flags"], "$1560")
        self.assertIn("VNomEff=450.0,620.0,850.0,", ond_text)
        self.assertIn("ProfilPIOV1=TCubicProfile", ond_text)
        self.assertIn("ProfilPIOV2=TCubicProfile", ond_text)
        self.assertIn("ProfilPIOV3=TCubicProfile", ond_text)

        self.store.create("OND", "SG110_3V.OND", ond_text)
        info = self.store.inspect("OND", "SG110_3V.OND", include_curve_csv=True)
        self.assertTrue(info["curves"]["control"]["three_voltage"])
        self.assertEqual(info["curves"]["control"]["errors"], [])
        by_path = {item["path"]: item for item in info["curves"]["items"]}
        self.assertFalse(by_path["Converter/ProfilPIO"]["editable"])
        self.assertTrue(by_path["Converter/ProfilPIOV1"]["editable"])
        self.assertTrue(by_path["Converter/ProfilPIOV2"]["editable"])
        self.assertTrue(by_path["Converter/ProfilPIOV3"]["editable"])
        self.assertIn("Converter/ProfilPIOV1", info["curve_csv"])

        new_v1 = [p[:] for p in by_path["Converter/ProfilPIOV1"]["points"]]
        new_v1[-1][1] = new_v1[-1][1] - 500
        new_v3 = [p[:] for p in by_path["Converter/ProfilPIOV3"]["points"]]
        new_v3[-1][1] = new_v3[-1][1] - 600
        upd = self.store.update(
            "OND", "SG110_3V.OND", {},
            expected_sha256=info["sha256"],
            curve_updates={"Converter/ProfilPIOV1": new_v1, "Converter/ProfilPIOV3": new_v3},
        )
        self.assertTrue(upd["changed"])
        info2 = self.store.inspect("OND", "SG110_3V.OND")
        by_path2 = {item["path"]: item for item in info2["curves"]["items"]}
        self.assertEqual(by_path2["Converter/ProfilPIOV1"]["points"][-1][1], new_v1[-1][1])
        self.assertEqual(by_path2["Converter/ProfilPIOV3"]["points"][-1][1], new_v3[-1][1])

    def test_pan_update_optional_single_diode_refit_backward_compatible(self) -> None:
        pan_text, _ = synthesize_pan_from_datasheet(
            manufacturer="ApexSolar",
            model="HiTOP-600",
            pnom_w=600.0,
            isc_a=14.30,
            voc_v=52.0,
            imp_a=13.55,
            vmp_v=44.3,
            width_m=1.134,
            height_m=2.382,
            n_cells_s=72,
            n_cells_p=2,
        )
        self.store.create("PAN", "Apex600.PAN", pan_text)
        info0 = self.store.inspect("PAN", "Apex600.PAN")
        orig_rserie = info0["fields"]["RSerie"][0]

        no_refit = self.store.update(
            "PAN", "Apex600.PAN",
            {"PNom": "625.0", "Voc": "53.2", "Vmp": "45.1", "ISC": "14.70", "Imp": "13.86"},
            expected_sha256=info0["sha256"],
            dry_run=True,
        )
        self.assertNotIn("single_diode_refit", no_refit)
        self.assertNotIn("RSerie", no_refit["diff"])

        with_refit = self.store.update(
            "PAN", "Apex600.PAN",
            {"PNom": "625.0", "Voc": "53.2", "Vmp": "45.1", "ISC": "14.70", "Imp": "13.86"},
            expected_sha256=info0["sha256"],
            dry_run=False,
            refit_single_diode=True,
        )
        self.assertTrue(with_refit["changed"])
        self.assertTrue(with_refit["single_diode_refit"]["refitted"])
        self.assertIn("RSerie", with_refit["single_diode_refit"]["patched_fields"])
        info1 = self.store.inspect("PAN", "Apex600.PAN")
        self.assertNotEqual(info1["fields"]["RSerie"][0], orig_rserie)

        with self.assertRaisesRegex(ValueError, "PAN components only"):
            self.store.update("OND", "any.OND", {"PNomConv": "50"}, refit_single_diode=True)

        incomplete_pan = "\r\n".join(
            line for line in pan_text.splitlines() if not line.strip().startswith("Imp=")
        ) + "\r\n"
        (self.root / "ComposPV" / "PVmodules" / "Incomplete.PAN").write_text(incomplete_pan, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "requires STC fields: Imp"):
            self.store.update("PAN", "Incomplete.PAN", {"Voc": "54.0"}, dry_run=True, refit_single_diode=True)

    def test_three_voltage_curve_and_btr_cell_voltage_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot exceed p_in"):
            synthesize_ond_from_datasheet(
                manufacturer="Lab", model="Inv3V", pnom_ac_kw=10.0,
                vmpp_min_v=200, vmpp_max_v=850, vabs_max_v=1000,
                three_voltage_voltages=[300, 600, 800],
                three_voltage_curves={"ProfilPIOV1": [[100, 0], [500, 600], [5000, 4800], [10500, 10000]]},
            )
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            synthesize_ond_from_datasheet(
                manufacturer="Lab", model="Inv3V", pnom_ac_kw=10.0,
                vmpp_min_v=200, vmpp_max_v=850, vabs_max_v=1000,
                three_voltage_voltages=[300, 600, 800],
                three_voltage_curves={"ProfilPIOV1": [[500, 0], [500, 400], [5000, 4800], [10500, 10000]]},
            )
        with self.assertRaisesRegex(ValueError, "4..11"):
            synthesize_ond_from_datasheet(
                manufacturer="Lab", model="Inv3V", pnom_ac_kw=10.0,
                vmpp_min_v=200, vmpp_max_v=850, vabs_max_v=1000,
                three_voltage_voltages=[300, 600, 800],
                three_voltage_curves={"ProfilPIOV1": [[100 * i, 90 * i] for i in range(1, 13)]},
            )

        with self.assertRaisesRegex(ValueError, "Per-cell nominal voltage"):
            synthesize_btr_from_datasheet(
                manufacturer="Lab", model="BadBTR", chemistry="LFP",
                capacity_ah=100.0, n_cells_s=16, nominal_voltage_v=100.0,
            )
        with self.assertRaisesRegex(ValueError, "cycle_life_80dod must be an integer"):
            synthesize_btr_from_datasheet(
                manufacturer="Lab", model="BadBTR", chemistry="LFP",
                capacity_ah=100.0, cycle_life_80dod=True,  # type: ignore[arg-type]
            )

    def test_sourced_component_field_unit_hints_and_schema_docs(self) -> None:
        sources = {
            "ds": {
                "kind": "manufacturer",
                "title": "Test Module Datasheet",
                "locator": "page 1",
                "text": "Isc temperature coefficient: 5.2 mA/°C, Nominal voltage 800 V, Power: 600 kW",
            }
        }
        pan_res = build_sourced_component(
            self.store,
            component_type="PAN",
            new_name="Demo600.PAN",
            manufacturer="TestCo",
            model="M600",
            sources=sources,
            declarations=[{
                "field": "muIsc",
                "value": 5.2,
                "unit": "mA/degC",
                "source_id": "ds",
                "excerpt": "5.2 mA/°C",
                "source_value_text": "5.2",
                "source_unit_text": "mA/°C",
            }],
            dry_run=True,
        )
        self.assertEqual(pan_res["status"], "blocked")
        issue = pan_res["issues"][0]
        self.assertIn("MuISC", issue["did_you_mean"])
        self.assertIn("MuISC", issue["allowed_fields"])
        self.assertIn("MuISC", pan_res["allowed_fields"])

        ond_res = build_sourced_component(
            self.store,
            component_type="OND",
            new_name="Demo300.OND",
            manufacturer="InvCo",
            model="I300",
            sources=sources,
            declarations=[{
                "field": "VNomEff",
                "value": 800.0,
                "unit": "V",
                "source_id": "ds",
                "excerpt": "800 V",
                "source_value_text": "800",
                "source_unit_text": "V",
            }],
            dry_run=True,
        )
        self.assertEqual(ond_res["status"], "blocked")
        self.assertIn("VNomEff1", ond_res["issues"][0]["did_you_mean"])
        self.assertIn("VOutConv", ond_res["allowed_fields"])

        bad_unit = build_sourced_component(
            self.store,
            component_type="PAN",
            new_name="DemoBadUnit.PAN",
            manufacturer="TestCo",
            model="M600",
            sources=sources,
            declarations=[{
                "field": "PNom",
                "value": 600.0,
                "unit": "kW",
                "source_id": "ds",
                "excerpt": "600 kW",
                "source_value_text": "600",
                "source_unit_text": "kW",
            }],
            dry_run=True,
        )
        self.assertEqual(bad_unit["status"], "blocked")
        self.assertEqual(bad_unit["issues"][0]["allowed_units"], ["W"])
        doc = pvsyst_build_sourced_component.__doc__ or ""
        for token in ("MuISC", "muVocSpec", "VOutConv", "VNomEff1..3", "CapNomC10"):
            self.assertIn(token, doc)


if __name__ == "__main__":
    unittest.main()

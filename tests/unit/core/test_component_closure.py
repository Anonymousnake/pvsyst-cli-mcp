"""Unit tests for v0.5.0 Phase 3 component closure: zero-template Li-ion BTR, 3-voltage OND, and PAN single-diode refit."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from pvsyst_auto.components.builder import build_sourced_component
from pvsyst_auto.components.single_diode import (
    synthesize_btr_from_datasheet,
    synthesize_ond_from_datasheet,
    synthesize_pan_from_datasheet,
)
from pvsyst_auto.components.store import ComponentStore


class ComponentClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "work"
        for sub in ("ComposPV/PVmodules", "ComposPV/Inverters", "ComposPV/Batteries", "Results"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.store = ComponentStore(self.root)

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

        # Sourced builder zero-template BTR roundtrip
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

        # Edit V1 and V3 simultaneously via curve_updates
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
        pan_text, meta = synthesize_pan_from_datasheet(
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

        # Default refit_single_diode=False leaves RSerie untouched (100% backward-compatible)
        no_refit = self.store.update(
            "PAN", "Apex600.PAN",
            {"PNom": "625.0", "Voc": "53.2", "Vmp": "45.1", "ISC": "14.70", "Imp": "13.86"},
            expected_sha256=info0["sha256"],
            dry_run=True,
        )
        self.assertNotIn("single_diode_refit", no_refit)
        self.assertNotIn("RSerie", no_refit["diff"])

        # Explicit refit_single_diode=True recalculates RShunt/Rp_0/RSerie/Gamma
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

        # Reject refit_single_diode=True on non-PAN
        with self.assertRaisesRegex(ValueError, "PAN components only"):
            self.store.update("OND", "any.OND", {"PNomConv": "50"}, refit_single_diode=True)

        # PAN missing required STC fields raises clean ValueError (not raw KeyError/TypeError)
        incomplete_pan = "\r\n".join(
            line for line in pan_text.splitlines() if not line.strip().startswith("Imp=")
        ) + "\r\n"
        (self.root / "ComposPV" / "PVmodules" / "Incomplete.PAN").write_text(incomplete_pan, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "requires STC fields: Imp"):
            self.store.update("PAN", "Incomplete.PAN", {"Voc": "54.0"}, dry_run=True, refit_single_diode=True)

    def test_three_voltage_curve_and_btr_cell_voltage_validation(self) -> None:
        # 1. Invalid 3V custom curves (p_out > p_in, non-increasing p_in, > 11 points) must be rejected
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

        # 2. Mismatched BTR nominal_voltage_v / n_cells_s outside cell window must be rejected
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


if __name__ == "__main__":
    unittest.main()

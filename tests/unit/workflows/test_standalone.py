"""Unit tests for v0.6.0 Phase 6: Stand-Alone off-grid PV+BESS+Genset synthesis, diagnostics, and diesel economics."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.api.schemas import SingleDesignRequest
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.engineering.diagnostics import diagnose_standalone
from pvsyst_auto.engineering.economics import evaluate_standalone_economics
from pvsyst_auto.project.standalone import generate_genset_text, synthesize_standalone_project
from pvsyst_auto.project.variants import VariantStore
from tests.unit.core.test_bifacial_utility import UTILITY_PAN
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


class StandAloneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "PVsyst8.1_Data"
        for sub in ("Projects", "Sites", "Meteo", "Results", "Models",
                    "ComposPV/PVmodules", "ComposPV/Batteries", "ComposPV/Gensets"):
            (self.ws / sub).mkdir(parents=True, exist_ok=True)
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV/PVmodules/Lab440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        self.comp = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.comp)

    def test_generate_genset_passes_component_store_validation(self) -> None:
        txt = generate_genset_text(
            model="Diesel_5kW",
            nominal_power_kw=5.0,
            fuel_consumption_l_per_h=1.6,
            fuel_type="Diesel",
        )
        gen_path = self.ws / "ComposPV/Gensets/Diesel_5kW.GEN"
        gen_path.write_text(txt, encoding="utf-8")
        info = self.comp.inspect("GEN", "Diesel_5kW.GEN")
        self.assertEqual(info["format"], "text")
        self.comp._require_valid(info, complete=True)
        self.assertEqual(info["fields"]["TypeGen"], ["Diesel"])
        self.assertAlmostEqual(float(info["fields"]["PNomGen"][0]), 5.0)
        self.assertAlmostEqual(float(info["fields"]["CFuelHor"][0]), 1.6)

    def test_synthesize_standalone_with_and_without_genset(self) -> None:
        s1 = synthesize_standalone_project(
            self.store,
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN",
            battery_file="Generic_Solar_12V160Ah.BTR",
            n_mod_serie=2,
            n_string_ch=3,
            n_batt_serie=4,
            n_batt_parall=2,
            daily_load_kwh=12.0,
        )
        self.assertIn("SystemType=Battery", s1["vc0_text"])
        self.assertIn("SystemKind=Standalone system with batteries", s1["vc0_text"])
        self.assertIn("Controller=Universal_Controller_LA_MPPT.RLT", s1["vc0_text"])
        self.assertIn("Flags=$0040", s1["vc0_text"])
        self.assertNotIn("GensetFile=", s1["vc0_text"])
        self.assertIn("PVObject_UserNeeds=pvLoad", s1["vc0_text"])

        s2 = synthesize_standalone_project(
            self.store,
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN",
            battery_file="Generic_LI_Module_180Ah.BTR",
            controller_type="direct",
            genset_file="Diesel_5kW.GEN",
            backup_operating_power_kw=2.5,
            load={
                "mode": "household",
                "modulation": "yearly",
                "appliances": [{"name": "Cabin", "count": 1, "power_w": 500.0, "hours": list(range(6, 22))}],
            },
        )
        self.assertIn("Controller=Universal_Controller_LA_Direct.RLT", s2["vc0_text"])
        self.assertIn("Flags=$0060", s2["vc0_text"])
        self.assertIn("GensetFile=Diesel_5kW.GEN", s2["vc0_text"])
        self.assertIn("PEffBackUp=2.500", s2["vc0_text"])
        self.assertIn("LoadType=HouseHold", s2["vc0_text"])

    def test_standalone_diagnostics_and_diesel_economics(self) -> None:
        bal = {
            "e_avail_kwh": 10000.0,
            "e_unused_kwh": 3200.0,
            "e_user_kwh": 6000.0,
            "e_solar_kwh": 4000.0,
            "e_backup_kwh": 2000.0,
            "e_missing_kwh": 350.0,
            "soc_mean": 0.38,
            "t_lol_hours": 500.0,
            "lol_pct": 5.71,
            "backup_runtime_hours": 800.0,
            "backup_fuel_liters": 640.0,
        }
        diag = diagnose_standalone(bal, target_lol_pct=5.0, has_genset=True)
        codes = {f["code"] for f in diag["findings"]}
        self.assertEqual(
            codes,
            {
                "STANDALONE_LOL_HIGH",
                "STANDALONE_UNUSED_ENERGY_HIGH",
                "STANDALONE_SOC_LOW",
                "STANDALONE_GENSET_RELIANCE_HIGH",
            },
        )

        eco = evaluate_standalone_economics(
            bal,
            capex_total=25000.0,
            annual_om_opex=400.0,
            fuel_price_per_liter=1.80,
            genset_opex_per_hour=0.50,
            avoided_tariff_per_kwh=0.45,
            financial={"currency": "USD", "project_life_years": 20, "discount_rate_pct": 6.0},
        )
        self.assertAlmostEqual(eco["annual_fuel_cost"], 640.0 * 1.80, places=2)
        self.assertAlmostEqual(eco["annual_genset_maintenance_cost"], 800.0 * 0.50, places=2)
        self.assertAlmostEqual(eco["total_year1_opex"], 400.0 + 1152.0 + 400.0, places=2)
        self.assertGreater(eco["lcoe"], 0.0)

    def test_single_design_request_schema_accepts_standalone(self) -> None:
        req = SingleDesignRequest(
            new_project="OffGridCabin.PRJ",
            scope="stand_alone",
            tilt=20.0,
            azimuth=0.0,
            module={"file_name": "Lab440.PAN"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            standalone={
                "battery_file": "Generic_Solar_12V160Ah.BTR",
                "n_mod_serie": 2,
                "n_string_ch": 4,
                "n_batt_serie": 4,
                "n_batt_parall": 2,
                "daily_load_kwh": 10.0,
                "genset_spec": {
                    "model": "CabinDiesel5kW",
                    "nominal_power_kw": 5.0,
                    "fuel_consumption_l_per_h": 1.5,
                },
                "backup_operating_power_kw": 2.5,
                "economics": {
                    "capex_total": 18000.0,
                    "fuel_price_per_liter": 1.65,
                    "genset_opex_per_hour": 0.40,
                },
            },
        )
        self.assertEqual(req.scope, "stand_alone")
        self.assertEqual(req.standalone.battery_file, "Generic_Solar_12V160Ah.BTR")


if __name__ == "__main__":
    unittest.main()

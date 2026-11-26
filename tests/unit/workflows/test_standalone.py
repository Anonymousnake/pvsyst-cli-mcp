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
    def test_simulate_design_rejects_unsupported_standalone_inputs(self) -> None:
        from unittest.mock import Mock, patch
        from pvsyst_auto.api import simulation
        cli = Mock()
        cli.run_simulation.side_effect = ValueError("native runner stub")
        weather = {"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"}
        cases = (("Losses", weather, {"UFactor": 30}, "losses overrides"),
            ("Weather", {**weather, "mode": "csv", "csv_name": "Measured.csv"}, None, "weather.mode='existing'"), ("Cabin", weather, None, None))
        with patch.multiple(simulation, cli=lambda: cli, variant_store=lambda: self.store):
            for name, weather_spec, losses, issue in cases:
                request = SingleDesignRequest(new_project=f"{name}.PRJ", scope="stand_alone", losses=losses,
                    module={"file_name": "Lab440.PAN"}, weather=weather_spec, standalone={"battery_file": "Generic_Solar_12V160Ah.BTR"})
                for dry_run in (True, False) if issue else (True,):
                    with self.subTest(name=name, dry_run=dry_run):
                        result = simulation.pvsyst_simulate_design(request, dry_run=dry_run)
                        self.assertEqual(result["status"], "blocked" if issue else "ready")
                        if issue: self.assertIn(issue, result["issues"][0]["message"])
        self.assertFalse(list((self.ws / "Projects").iterdir()))
        cli.run_simulation.assert_not_called()
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "PVsyst8.1_Data"
        for sub in ("Projects", "Sites", "Meteo", "Results", "Models", "ComposPV/PVmodules", "ComposPV/Batteries", "ComposPV/Gensets"):
            (self.ws / sub).mkdir(parents=True, exist_ok=True)
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV/PVmodules/Lab440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        self.comp = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.comp)
    def test_simulate_design_validates_inline_genset_without_overwriting(self) -> None:
        from unittest.mock import Mock, patch
        from pvsyst_auto.api import simulation
        filename = "Shared.GEN"
        original = generate_genset_text(model="Existing", nominal_power_kw=5, fuel_consumption_l_per_h=1.6)
        self.comp.create("GEN", filename, original)
        target = self.ws / "ComposPV/Gensets" / filename
        cli = Mock()
        cli.run_simulation.side_effect = ValueError("native runner stub")
        cases = ((0.005, True, "blocked", "fuel_consumption_l_per_h"), (2.5, True, "ready", None), (2.5, False, "blocked", "Generated GEN already exists"))
        with patch.multiple(simulation, cli=lambda: cli, variant_store=lambda: self.store):
            for fuel, dry_run, status, issue in cases:
                with self.subTest(fuel=fuel, dry_run=dry_run):
                    request = SingleDesignRequest(new_project="Cabin.PRJ", scope="stand_alone", module={"file_name": "Lab440.PAN"},
                        weather={"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                        standalone={"battery_file": "Generic_Solar_12V160Ah.BTR", "genset_spec": {
                            "file_name": "New.GEN" if status == "ready" else filename, "nominal_power_kw": 10, "fuel_consumption_l_per_h": fuel}})
                    result = simulation.pvsyst_simulate_design(request, dry_run=dry_run)
                    self.assertEqual(target.read_bytes(), original.encode("utf-8"))
                    self.assertEqual(result["status"], status)
                    if issue: self.assertIn(issue, result["issues"][0]["message"])
        self.assertFalse((self.ws / "ComposPV/Gensets/New.GEN").exists())
        self.assertFalse((self.ws / "Projects/Cabin.PRJ").exists())
        cli.run_simulation.assert_not_called()
    def test_generate_genset_passes_component_store_validation(self) -> None:
        txt = generate_genset_text(model="Diesel_5kW", nominal_power_kw=5.0, fuel_consumption_l_per_h=1.6, fuel_type="Diesel")
        gen_path = self.ws / "ComposPV/Gensets/Diesel_5kW.GEN"
        gen_path.write_text(txt, encoding="utf-8")
        info = self.comp.inspect("GEN", "Diesel_5kW.GEN")
        self.assertEqual(info["format"], "text")
        self.comp._require_valid(info, complete=True)
        self.assertEqual(info["fields"]["TypeGen"], ["Diesel"])
        self.assertAlmostEqual(float(info["fields"]["PNomGen"][0]), 5.0)
        self.assertAlmostEqual(float(info["fields"]["CFuelHor"][0]), 1.6)
    def test_prepare_design_project_blocks_standalone_before_simulation(self) -> None:
        from unittest.mock import Mock, patch
        from pvsyst_auto.api import projects
        cli = Mock()
        cli.run_simulation.side_effect = ValueError("native runner stub")
        request = SingleDesignRequest(new_project="Cabin.PRJ", scope="stand_alone", module={"file_name": "Lab440.PAN"},
            weather={"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"}, standalone={"battery_file": "Generic_Solar_12V160Ah.BTR"})
        with patch.multiple(projects, cli=lambda: cli, variant_store=lambda: self.store):
            result = projects.pvsyst_prepare_design_project(request, dry_run=False)
            preview = projects.pvsyst_prepare_design_project(request, dry_run=True)
        cli.run_simulation.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(preview["status"], "blocked")
        self.assertIn("pvsyst_simulate_design", result["issues"][0]["message"])
        self.assertFalse((self.ws / "Projects/Cabin.PRJ").exists())
    def test_synthesize_standalone_with_and_without_genset(self) -> None:
        s1 = synthesize_standalone_project(self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN", battery_file="Generic_Solar_12V160Ah.BTR", n_mod_serie=2, n_string_ch=3,
            n_batt_serie=4, n_batt_parall=2, daily_load_kwh=12.0)
        self.assertIn("SystemType=Battery", s1["vc0_text"])
        self.assertIn("SystemKind=Standalone system with batteries", s1["vc0_text"])
        self.assertIn("Controller=Universal_Controller_LA_MPPT.RLT", s1["vc0_text"])
        self.assertIn("Flags=$0040", s1["vc0_text"])
        self.assertNotIn("GensetFile=", s1["vc0_text"])
        self.assertIn("PVObject_UserNeeds=pvLoad", s1["vc0_text"])
        s2 = synthesize_standalone_project(self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN", battery_file="Generic_LI_Module_180Ah.BTR", controller_type="direct", genset_file="Diesel_5kW.GEN",
            backup_operating_power_kw=2.5, load={"mode": "household", "modulation": "yearly",
                "appliances": [{"name": "Cabin", "count": 1, "power_w": 500.0, "hours": list(range(6, 22))}]})
        self.assertIn("Controller=Universal_Controller_LA_Direct.RLT", s2["vc0_text"])
        self.assertIn("Flags=$0060", s2["vc0_text"])
        self.assertIn("GensetFile=Diesel_5kW.GEN", s2["vc0_text"])
        self.assertIn("PEffBackUp=2.500", s2["vc0_text"])
        self.assertIn("LoadType=HouseHold", s2["vc0_text"])
    def test_simulate_design_checks_annual_result_coverage(self) -> None:
        from datetime import datetime, timedelta
        from unittest.mock import Mock, patch
        from pvsyst_auto.api import simulation
        cli = Mock()
        start = datetime(2024, 1, 1)
        for name, hours, blank_at in (("Short", 24, None), ("Blank", 8784, 0), ("Leap", 8784, None)):
            with self.subTest(name=name):
                def simulate(project, variant, *, out_csv, **kwargs):
                    rows = (f"{start + timedelta(hours=i):%d/%m/%y %H:%M};" f"{'' if i == blank_at else 1};{int(i < 4392)}\n" for i in range(hours))
                    out_csv.write_text("date;E_User;T_LOL\n;kWh;h\n" + "".join(rows), encoding="utf-8")
                    return {}
                cli.run_simulation.side_effect = simulate
                request = SingleDesignRequest(new_project=f"{name}.PRJ", scope="stand_alone", module={"file_name": "Lab440.PAN"},
                    weather={"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                    standalone={"battery_file": "Generic_Solar_12V160Ah.BTR", "economics": {"capex_total": 1000}})
                with patch.multiple(simulation, cli=lambda: cli, variant_store=lambda: self.store):
                    result = simulation.pvsyst_simulate_design(request, dry_run=False)
                if name == "Leap": self.assertEqual(result["energy_balance"]["lol_pct"], 50.0)
                else:
                    self.assertEqual(result["status"], "blocked")
                    reason = "complete hourly calendar year" if name == "Short" else "complete E_User column"
                    self.assertIn(reason, result["issues"][0]["message"])
                    self.assertNotIn("economics", result)
    def test_standalone_diagnostics_and_diesel_economics(self) -> None:
        bal = {"e_avail_kwh": 10000.0, "e_unused_kwh": 3200.0, "e_user_kwh": 6000.0,
            "e_solar_kwh": 4000.0, "e_backup_kwh": 2000.0, "e_missing_kwh": 350.0, "soc_mean": 0.38,
            "t_lol_hours": 500.0, "lol_pct": 5.71, "backup_runtime_hours": 800.0, "backup_fuel_liters": 640.0}
        diag = diagnose_standalone(bal, target_lol_pct=5.0, has_genset=True)
        codes = {f["code"] for f in diag["findings"]}
        self.assertEqual(codes, {"STANDALONE_LOL_HIGH", "STANDALONE_UNUSED_ENERGY_HIGH", "STANDALONE_SOC_LOW", "STANDALONE_GENSET_RELIANCE_HIGH"})
        eco = evaluate_standalone_economics(bal, capex_total=25000.0, annual_om_opex=400.0, fuel_price_per_liter=1.80, genset_opex_per_hour=0.50,
            avoided_tariff_per_kwh=0.45, financial={"currency": "USD", "project_life_years": 20, "discount_rate_pct": 6.0})
        self.assertAlmostEqual(eco["annual_fuel_cost"], 640.0 * 1.80, places=2)
        self.assertAlmostEqual(eco["annual_genset_maintenance_cost"], 800.0 * 0.50, places=2)
        self.assertAlmostEqual(eco["total_year1_opex"], 400.0 + 1152.0 + 400.0, places=2)
        self.assertGreater(eco["lcoe"], 0.0)
    def test_simulate_design_economics_books_battery_replacement(self) -> None:
        from datetime import datetime, timedelta
        from unittest.mock import Mock, patch
        from pvsyst_auto.api import simulation
        cli = Mock()
        start = datetime(2023, 1, 1)
        def simulate(project, variant, *, out_csv, **kwargs):
            rows = (f"{start + timedelta(hours=i):%d/%m/%y %H:%M};1\n" for i in range(8760))
            out_csv.write_text("date;E_User\n;kWh\n" + "".join(rows), encoding="utf-8")
            return {}
        cli.run_simulation.side_effect = simulate
        for name, cost in (("Quoted", 600), ("Unquoted", None)):
            financial = {"project_life_years": 2, "discount_rate_pct": 0.0, "first_year_degradation_pct": 0.0, "annual_degradation_pct": 0.0,
                         "battery_replacement_year": 2}
            if cost is not None: financial["battery_replacement_cost"] = cost
            request = SingleDesignRequest(new_project=f"{name}.PRJ", scope="stand_alone", module={"file_name": "Lab440.PAN"},
                weather={"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                standalone={"battery_file": "Generic_Solar_12V160Ah.BTR", "economics": {"capex_total": 1000, "financial": financial}})
            with self.subTest(name=name), patch.multiple(simulation, cli=lambda: cli, variant_store=lambda: self.store):
                result = simulation.pvsyst_simulate_design(request, dry_run=False)
                if cost is not None:
                    self.assertEqual(result["economics"]["npv"], -1600.0)
                    self.assertEqual(result["economics"]["lcoe"], round(1600 / 17520, 5))
                else:
                    self.assertEqual(result["status"], "blocked")
                    self.assertIn("battery_replacement_cost is required", result["issues"][0]["message"])
    def test_single_design_request_schema_accepts_standalone(self) -> None:
        req = SingleDesignRequest(new_project="OffGridCabin.PRJ", scope="stand_alone", tilt=20.0, azimuth=0.0, module={"file_name": "Lab440.PAN"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"}, standalone={
                "battery_file": "Generic_Solar_12V160Ah.BTR", "n_mod_serie": 2, "n_string_ch": 4, "n_batt_serie": 4,
                "n_batt_parall": 2, "daily_load_kwh": 10.0,
                "genset_spec": {"model": "CabinDiesel5kW", "nominal_power_kw": 5.0, "fuel_consumption_l_per_h": 1.5}, "backup_operating_power_kw": 2.5,
                "economics": {"capex_total": 18000.0, "fuel_price_per_liter": 1.65, "genset_opex_per_hour": 0.40}})
        self.assertEqual(req.scope, "stand_alone")
        self.assertEqual(req.standalone.battery_file, "Generic_Solar_12V160Ah.BTR")
    def test_simulate_design_propagates_standalone_diagnostic_review(self) -> None:
        from datetime import datetime, timedelta
        from unittest.mock import Mock, patch
        from pvsyst_auto.api import simulation
        cli = Mock()
        start = datetime(2023, 1, 1)
        for lost_hours, status in ((876, "needs_review"), (0, "completed")):
            with self.subTest(lost_hours=lost_hours):
                def simulate(project, variant, *, out_csv, **kwargs):
                    rows = (f"{start + timedelta(hours=i):%d/%m/%y %H:%M};"
                            f"{int(i >= lost_hours)};{int(i < lost_hours)};{int(i < lost_hours)}\n" for i in range(8760))
                    out_csv.write_text("date;E_User;E_Miss;T_LOL\n;kWh;kWh;h\n" + "".join(rows), encoding="utf-8")
                    return {}
                cli.run_simulation.side_effect = simulate
                request = SingleDesignRequest(new_project=f"Cabin{lost_hours}.PRJ", scope="stand_alone", module={"file_name": "Lab440.PAN"},
                    weather={"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                    standalone={"battery_file": "Generic_Solar_12V160Ah.BTR", "target_lol_pct": 5.0})
                with patch.multiple(simulation, cli=lambda: cli, variant_store=lambda: self.store):
                    result = simulation.pvsyst_simulate_design(request, dry_run=False)
                self.assertEqual(result["diagnostics"]["needs_review"], lost_hours > 0)
                self.assertEqual(result["status"], status)
    def test_dry_run_preflight_blocks_collisions_and_bad_economics_without_writing(self) -> None:
        from unittest.mock import Mock, patch
        from pvsyst_auto.api import simulation
        cli = Mock()
        cli.run_simulation.side_effect = AssertionError("CLI should not run on blocked dry_run")
        # 1. Case-insensitive existing .GEN collision blocked on dry_run without overwriting
        gen_path = self.ws / "ComposPV" / "Gensets" / "ExistingGen.GEN"
        gen_path.write_text("KEEP_EXISTING\n", encoding="utf-8")
        req_gen = SingleDesignRequest(new_project="GenClash.PRJ", scope="stand_alone", module={"file_name": "Lab440.PAN"},
            weather={"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            standalone={"battery_file": "Generic_Solar_12V160Ah.BTR", "genset_spec": {
                "file_name": "existinggen.gen", "model": "Existing", "nominal_power_kw": 5.0, "fuel_consumption_l_per_h": 1.5}})
        with patch.multiple(simulation, cli=lambda: cli, variant_store=lambda: self.store):
            res_gen = simulation.pvsyst_simulate_design(req_gen, dry_run=True)
        self.assertEqual(res_gen["status"], "blocked")
        self.assertIn("already exists", res_gen["issues"][0]["message"])
        self.assertEqual(gen_path.read_text(encoding="utf-8"), "KEEP_EXISTING\n")
        # 2. Existing project collision blocked on dry_run
        (self.ws / "Projects" / "ExistingProj.PRJ").write_text("PVObject_=pvProject\n", encoding="utf-8")
        req_proj = SingleDesignRequest(new_project="ExistingProj.PRJ", scope="stand_alone", module={"file_name": "Lab440.PAN"},
            weather={"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"}, standalone={"battery_file": "Generic_Solar_12V160Ah.BTR"})
        with patch.multiple(simulation, cli=lambda: cli, variant_store=lambda: self.store):
            res_proj = simulation.pvsyst_simulate_design(req_proj, dry_run=True)
        self.assertEqual(res_proj["status"], "blocked")
        self.assertIn("already exists", res_proj["issues"][0]["message"])
        # 3. Unquoted battery_replacement_year blocked at dry_run before simulation or .GEN write
        req_eco = SingleDesignRequest(new_project="BadEco.PRJ", scope="stand_alone", module={"file_name": "Lab440.PAN"},
            weather={"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"}, standalone={"battery_file": "Generic_Solar_12V160Ah.BTR",
                "genset_spec": {"file_name": "ShouldNotWrite.GEN", "model": "G", "nominal_power_kw": 5.0, "fuel_consumption_l_per_h": 1.5},
                "economics": {"capex_total": 1000.0, "financial": {"battery_replacement_year": 5}}})
        with patch.multiple(simulation, cli=lambda: cli, variant_store=lambda: self.store):
            res_eco = simulation.pvsyst_simulate_design(req_eco, dry_run=True)
        self.assertEqual(res_eco["status"], "blocked")
        self.assertIn("battery_replacement_cost", res_eco["issues"][0]["message"])
        self.assertFalse((self.ws / "ComposPV" / "Gensets" / "ShouldNotWrite.GEN").exists())
    def test_standalone_energy_balance_scales_wh_and_checks_all_columns_and_blank_soc(self) -> None:
        from datetime import datetime, timedelta
        from unittest.mock import Mock, patch
        from pvsyst_auto.api import simulation
        cli = Mock()
        start = datetime(2023, 1, 1)
        def sim_wh(project, variant, *, out_csv, **kwargs):
            rows = (f"{start + timedelta(hours=i):%d/%m/%y %H:%M};1000;600;400;0;\n" for i in range(8760))
            out_csv.write_text("date;E_User;E_Solar;E_BkUp;T_LOL;SOCmean\n;Wh;Wh;Wh;h;\n" + "".join(rows), encoding="utf-8")
            return {}
        cli.run_simulation.side_effect = sim_wh
        req_wh = SingleDesignRequest(new_project="WhUnit.PRJ", scope="stand_alone", module={"file_name": "Lab440.PAN"},
            weather={"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"}, standalone={"battery_file": "Generic_Solar_12V160Ah.BTR"})
        with patch.multiple(simulation, cli=lambda: cli, variant_store=lambda: self.store):
            res_wh = simulation.pvsyst_simulate_design(req_wh, dry_run=False)
        self.assertEqual(res_wh["status"], "completed")
        self.assertAlmostEqual(res_wh["energy_balance"]["e_user_kwh"], 8760.0, places=2)
        self.assertAlmostEqual(res_wh["energy_balance"]["e_solar_kwh"], 5256.0, places=2)
        self.assertAlmostEqual(res_wh["energy_balance"]["e_backup_kwh"], 3504.0, places=2)
        self.assertEqual(res_wh["energy_balance"]["soc_mean"], 0.0)
        def sim_blank_tlol(project, variant, *, out_csv, **kwargs):
            rows = (f"{start + timedelta(hours=i):%d/%m/%y %H:%M};1;{'' if i == 10 else 0}\n" for i in range(8760))
            out_csv.write_text("date;E_User;T_LOL\n;kWh;h\n" + "".join(rows), encoding="utf-8")
            return {}
        cli.run_simulation.side_effect = sim_blank_tlol
        req_blank = SingleDesignRequest(new_project="BlankTLol.PRJ", scope="stand_alone", module={"file_name": "Lab440.PAN"},
            weather={"sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"}, standalone={"battery_file": "Generic_Solar_12V160Ah.BTR"})
        with patch.multiple(simulation, cli=lambda: cli, variant_store=lambda: self.store):
            res_blank = simulation.pvsyst_simulate_design(req_blank, dry_run=False)
        self.assertEqual(res_blank["status"], "blocked")
        self.assertIn("complete T_LOL column", res_blank["issues"][0]["message"])
if __name__ == "__main__": unittest.main()

"""Unit tests for P1 features: multi-orientation ground_sheds, multi-CSV merge,
weather enhancements (synthetic_from_site, twilight DHI tolerance, region enum),
and composable region-neutral project finance & tax engine."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.api.weather import pvsyst_create_site
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.engineering.economics import evaluate_economics
from pvsyst_auto.native.cli import PVsystCLI
from pvsyst_auto.project.synthesize import placeholder_site_block
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.weather import acquisition
from pvsyst_auto.workflows.design import prepare_design_project, simulate_design
from pvsyst_auto.workflows.report import generate_report
from pvsyst_auto.workflows.utility import validate_utility_spec
from tests._fixtures import write_sfi_csv as _write_sfi_csv
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN


def _write_workspace_scaffold(root: Path) -> Path:
    ws = root / "PVsyst8.1_Data"
    for folder in (
        "ComposPV/PVmodules", "ComposPV/Inverters", "ComposPV/Batteries",
        "ComposPV/Clients", "ComposPV/Gensets", "ComposPV/Pumps",
        "ComposPV/Regulators", "ComposPV/RegulPmp", "ComposPV/Sellers",
        "DataMeas", "Meteo", "Models", "Other", "Projects", "Results",
        "Shadings", "Sites", "Templates", "UserBatch", "UserData",
        "UserHourly", "UserHourlyParams", "UserImages", "UserOptimization",
        "UserRecycleBin",
    ):
        (ws / folder).mkdir(parents=True, exist_ok=True)
    (ws / "ComposPV" / "PVmodules" / "TestSolar_TS600.PAN").write_bytes(
        b"\xef\xbb\xbf" + UTILITY_PAN.replace("\n", "\r\n").encode("utf-8")
    )
    (ws / "ComposPV" / "Inverters" / "TestInv_300K.OND").write_bytes(
        b"\xef\xbb\xbf" + UTILITY_OND.replace("\n", "\r\n").encode("utf-8")
    )
    _, site_body = placeholder_site_block("TestSite.SIT", "TestSite")
    site_text = "\ufeffPVObject_=pvSite\r\n" + "\r\n".join(f"  {ln}" for ln in site_body) + "\r\nEnd of PVObject pvSite\r\n"
    (ws / "Sites" / "TestSite.SIT").write_text(site_text, encoding="utf-8")
    return ws


class _FakeCLI:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.run_calls: list[dict] = []

    def _require(self, command: str, option: str, allow_window_creation=None) -> None:
        return None

    def command_help(self, command: str, allow_window_creation=None) -> str:
        return "Available language: [en, zh]"

    def version(self, allow_window_creation=None) -> str:
        return "8.1.7"

    def license_info(self, allow_window_creation=None) -> dict:
        return {"status": "ACTIVATED", "remaining_executions": None}

    def _redact(self, text: str) -> str:
        return text

    def run_simulation(self, project: str, variant: str, **kwargs) -> dict:
        self.run_calls.append({"project": project, "variant": variant, **kwargs})
        out_csv = Path(kwargs["out_csv"])
        pdf = Path(kwargs["report_pdf"])
        rows = []
        start = datetime(2024, 1, 1, 0, 0)
        for h in range(8784):
            dt = start + timedelta(hours=h)
            is_day = 8 <= dt.hour < 16
            rows.append([
                dt.strftime("%d/%m/%y %H:%M"),
                500.0 if is_day else 0.0,
                480.0 if is_day else 0.0,
                160.0 if is_day else 0.0,
                147.2064 if is_day else 0.0,
                0.82 if is_day else 0.0,
                0.0,
            ])
        _write_sfi_csv(
            out_csv,
            [("GlobInc", "W/m²"), ("GlobShd", "W/m²"), ("EArray", "kW"), ("E_Grid", "kW"), ("PR", "ratio"), ("IL_Pmax", "kW")],
            rows,
            project=project,
            site=Path(kwargs["site"]).name if kwargs.get("site") else "TestSite.SIT",
            met="Synthetic_TestSite.MET",
            variant=f"{Path(project).stem}.{variant}",
        )
        pdf.write_bytes(b"%PDF-1.4\n%fake\n")
        return {"returncode": 0, "warnings": [], "args": []}


class MultiOrientationGroundShedsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = _write_workspace_scaffold(Path(self._tmp.name))
        self.store = VariantStore(self.ws, ComponentStore(self.ws))
        self.cli = _FakeCLI(self.ws)

    def test_validate_utility_spec_multi_orientation_rules(self):
        spec = {
            "scope": "ground_sheds",
            "ground_layout": {
                "model_mode": "unlimited",
                "pitch_m": 10.0,
                "sub_orientations": [
                    {"label": "South", "tilt": 25.0, "azimuth": 0.0, "weight": 0.5},
                    {"label": "East", "tilt": 20.0, "azimuth": -90.0, "weight": 0.3},
                    {"label": "West", "tilt": 20.0, "azimuth": 90.0, "weight": 0.2},
                ],
            },
        }
        issues: list[dict] = []
        validate_utility_spec(spec, "ground_sheds", issues)
        self.assertEqual(issues, [])

        # Mixed target_dc_kw & weight rejected
        bad_spec = {
            "scope": "ground_sheds",
            "ground_layout": {
                "sub_orientations": [
                    {"tilt": 25.0, "azimuth": 0.0, "weight": 0.5},
                    {"tilt": 20.0, "azimuth": 90.0, "target_dc_kw": 1000.0},
                ],
            },
        }
        bad_issues: list[dict] = []
        validate_utility_spec(bad_spec, "ground_sheds", bad_issues)
        self.assertTrue(any("not a mix" in i["message"] for i in bad_issues))

    def test_prepare_and_validate_multi_orientation_ground_sheds_project(self):
        spec = {
            "scope": "ground_sheds",
            "new_project": "Hillside_3Slope.PRJ",
            "year": 2024,
            "target_dc_kw": 3600.0,
            "module": {"file_name": "TestSolar_TS600.PAN"},
            "inverter": {"file_name": "TestInv_300K.OND"},
            "sizing_mode": "auto_from_components",
            "weather": {"mode": "synthetic_from_site", "sit_name": "TestSite.SIT"},
            "ground_layout": {
                "model_mode": "unlimited",
                "pitch_m": 11.0,
                "shed_count": 20,
                "module_rows": 2,
                "sub_orientations": [
                    {"label": "South_Slope", "tilt": 26.0, "azimuth": 0.0, "weight": 0.5},
                    {"label": "East_Slope", "tilt": 18.0, "azimuth": -90.0, "weight": 0.3, "pitch_m": 9.5},
                    {"label": "West_Slope", "tilt": 18.0, "azimuth": 90.0, "weight": 0.2, "pitch_m": 9.5},
                ],
            },
        }
        dry = prepare_design_project(self.cli, self.store, spec=spec, dry_run=True)
        self.assertEqual(dry["status"], "ready", dry.get("issues"))
        sub_o = dry["utility_summary"]["sub_orientations"]
        self.assertEqual(len(sub_o), 3)
        self.assertAlmostEqual(sum(s["share_pct"] for s in sub_o), 100.0, places=1)
        self.assertEqual([s["azimuth_deg"] for s in sub_o], [0.0, -90.0, 90.0])

        exec_res = prepare_design_project(self.cli, self.store, spec=spec, dry_run=False)
        self.assertIn(exec_res["status"], ("prepared", "needs_review"), exec_res)
        struct = self.store.validate_structure("Hillside_3Slope.PRJ", "VC0")
        self.assertTrue(struct["valid"], struct)
        params = self.store.inspect_parameters("Hillside_3Slope.PRJ", "VC0")
        self.assertEqual(len(params["orientations"]), 3)
        self.assertEqual(len(params["subarrays"]), 3)

    def test_synthetic_from_site_runs_simulation_without_met(self):
        spec = {
            "scope": "ground_sheds",
            "new_project": "SynthWeather.PRJ",
            "year": 2024,
            "target_dc_kw": 360.0,
            "tilt": 22.0,
            "azimuth": 0.0,
            "module": {"file_name": "TestSolar_TS600.PAN"},
            "inverter": {"file_name": "TestInv_300K.OND"},
            "sizing_mode": "auto_from_components",
            "weather": {"mode": "synthetic_from_site", "sit_name": "TestSite.SIT"},
        }
        sim = simulate_design(self.cli, self.store, spec=spec, dry_run=False)
        self.assertIn(sim["status"], ("completed", "needs_review"), sim)
        self.assertEqual(len(self.cli.run_calls), 1)
        call = self.cli.run_calls[0]
        self.assertTrue(call["synthetic_weather"])
        self.assertEqual(Path(call["site"]).name, "TestSite.SIT")
        self.assertIsNone(call.get("met"))


class WeatherEnhancementsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Meteo").mkdir()
        (self.root / "Results").mkdir()

    def test_region_error_and_schema_expose_allowed_values(self):
        fake_exe = self.root / "PVsystCLI.exe"
        fake_exe.write_bytes(b"fake")
        cli = PVsystCLI(cli_path=fake_exe, workspace=self.root)
        with patch.object(cli, "_require", return_value=None):
            with self.assertRaisesRegex(ValueError, "Unknown region.*asia.*europe"):
                cli.create_site_details("S", 30.0, 120.0, self.root / "Meteo" / "m.csv", self.root / "s.sit", region="Asia")
        # Tool schema exposes lowercase Literal enum
        ann = pvsyst_create_site.__annotations__["region"]
        self.assertIn("asia", str(ann))
        self.assertIn("north_america", str(ann))

    def test_nasa_power_twilight_dhi_tolerance_and_strict_opt_out(self):
        year = 2022
        offset_hours = 8
        utc_start = datetime(year, 1, 1, 0, 0) - timedelta(hours=offset_hours)
        ghi_map, dhi_map, t2m_map = {}, {}, {}
        twilight_key = None
        for step in range(8760):
            utc_dt = utc_start + timedelta(hours=step)
            key = utc_dt.strftime("%Y%m%d%H")
            local_h = (utc_dt.hour + offset_hours) % 24
            ghi = 500.0 if 8 <= local_h <= 16 else 0.0
            dhi = 150.0 if ghi > 0 else 0.0
            if step == 100:
                # Exact real-world failure case: GHI=14.48, DHI=17.26
                ghi = 14.48
                dhi = 17.26
                twilight_key = key
            ghi_map[key] = ghi
            dhi_map[key] = dhi
            t2m_map[key] = 15.0
        payload = {
            "geometry": {"coordinates": [110.0, 35.0, 400.0]},
            "properties": {
                "parameter": {
                    "ALLSKY_SFC_SW_DWN": ghi_map,
                    "ALLSKY_SFC_SW_DIFF": dhi_map,
                    "T2M": t2m_map,
                }
            },
        }
        raw = json.dumps(payload)

        with patch.object(acquisition, "_provider_get", return_value=(payload, raw, "https://power.larc.nasa.gov/mock")):
            ok = acquisition.download_weather(
                self.root, csv_name="nasa_twilight_ok.csv", year=year,
                latitude=35.0, longitude=110.0, site_name="TwSite",
                country_code="CN", timezone_name="Asia/Shanghai",
                provider="nasa_power", dry_run=False,
            )
            self.assertEqual(ok["status"], "prepared", ok["issues"])
            self.assertTrue(any("twilight" in w.lower() and twilight_key in w for w in ok["warnings"]))

            strict = acquisition.download_weather(
                self.root, csv_name="nasa_twilight_strict.csv", year=year,
                latitude=35.0, longitude=110.0, site_name="TwSite",
                country_code="CN", timezone_name="Asia/Shanghai",
                provider="nasa_power", strict_dhi=True, dry_run=False,
            )
            self.assertEqual(strict["status"], "blocked")
            self.assertTrue(any("DHI (17.26) > GHI (14.48)" in i for i in strict["issues"]))


class ComposableProjectFinanceAndMultiCsvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Results").mkdir()

    def _write_slope_csv(self, name: str, kw_day: float, pr_val: float) -> Path:
        start = datetime(2024, 1, 1, 0, 0)
        rows = []
        for h in range(240):
            dt = start + timedelta(hours=h)
            is_day = 9 <= dt.hour < 15
            rows.append([
                dt.strftime("%d/%m/%y %H:%M"),
                500.0 if is_day else 0.0,
                kw_day * 1.1 if is_day else 0.0,
                kw_day if is_day else 0.0,
                pr_val if is_day else 0.0,
            ])
        return _write_sfi_csv(
            self.root / "Results" / name,
            [("GlobInc", "W/m²"), ("EArray", "kW"), ("E_Grid", "kW"), ("PR", "ratio")],
            rows,
        )

    def test_multi_csv_portfolio_merge_in_economics_and_report(self):
        self._write_slope_csv("south.csv", 100.0, 0.84)
        self._write_slope_csv("east.csv", 60.0, 0.80)
        self._write_slope_csv("west.csv", 40.0, 0.78)

        res = evaluate_economics(
            workspace=self.root,
            scenarios=[{
                "label": "Combined_3Slope",
                "csv_names": ["south.csv", "east.csv", "west.csv"],
                "csv_weights": [0.5, 0.3, 0.2],
                "installed_dc_kw": 200.0,
                "capex_total": 600_000.0,
            }],
            financial={"currency": "CNY", "first_year_degradation_pct": 0.0, "annual_degradation_pct": 0.0},
            tariff={"mode": "feed_in", "feed_in_tariff": 0.35},
        )
        # 10 days * 6 day-hours = 60 h; weighted power = 0.5*100 + 0.3*60 + 0.2*40 = 76 kW -> 4560 kWh
        sc = res["scenarios"][0]
        self.assertAlmostEqual(sc["year1_energy_kwh"], 4560.0, places=2)
        self.assertEqual(len(sc["settlement"]["merged_sources"]), 3)

        rep = generate_report(
            workspace=self.root,
            title="Portfolio Merged Report",
            output_name="portfolio_merged.html",
            scenarios=[{
                "label": "Combined_3Slope",
                "csv_names": ["south.csv", "east.csv", "west.csv"],
                "csv_weights": [0.5, 0.3, 0.2],
                "installed_dc_kw": 200.0,
                "capex_total": 600_000.0,
            }],
            financial={"currency": "CNY"},
            tariff={"mode": "feed_in", "feed_in_tariff": 0.35},
            dry_run=False,
        )
        self.assertEqual(rep["status"], "generated")
        self.assertTrue(Path(rep["report_path"]).is_file())

    def test_mixed_site_units_and_load_at_project_exit(self):
        from pvsyst_auto.workflows.report import _summarize_multi_csv_for_report
        stamp = "01/06/24 12:00"
        base = self.root / "Results"
        a = _write_sfi_csv(base / "a.csv", [("GlobInc", "W/m2"), ("PR", "%"), ("E_Grid", "kWh"),
                                             ("E_User", "kWh")], [[stamp, 1000, 80, 0, 10]])
        b = _write_sfi_csv(base / "b.csv", [("GlobInc", "kW/m2"), ("PR", "%"), ("E_Grid", "kWh")],
                           [[stamp, 1, 60, -5]])
        self.assertEqual(_summarize_multi_csv_for_report([a, b], [1, 1])["overall_weighted_pr"], 0.7)
        result = evaluate_economics(workspace=self.root, scenarios=[{"csv_names": [a.name, b.name],
                                    "csv_weights": [1, 1], "capex_total": 100}],
                                    financial={"project_life_years": 1, "financing": {"equity_ratio_pct": 20,
                                               "loan_tenor_years": 10}}, include_cashflow_schedule=True)
        scenario = result["scenarios"][0]
        self.assertEqual(scenario["year1_energy_kwh"], 10)
        self.assertEqual(scenario["financials"]["cashflow_schedule"][0]["debt_principal"], 80)

    def test_composable_vat_cit_financing_and_staged_opex(self):
        # 100 MW utility plant: 150,000,000 kWh/yr, CAPEX = 350,000,000 CNY, FIT = 0.35 CNY/kWh (VAT-inclusive)
        base_scenario = [{
            "label": "Utility_100MW",
            "annual_yield_kwh": 150_000_000.0,
            "installed_dc_kw": 100_000.0,
            "capex_total": 350_000_000.0,
        }]
        tariff = {"mode": "feed_in", "feed_in_tariff": 0.35}

        # 1) Plain pre-tax unlevered (backwards compatibility)
        plain = evaluate_economics(
            scenarios=base_scenario,
            financial={"currency": "CNY", "project_life_years": 25, "opex_per_kw_year": 45.0},
            tariff=tariff,
        )["scenarios"][0]["financials"]
        self.assertNotIn("tax_summary", plain)
        self.assertNotIn("financing_summary", plain)

        # 2) VAT with vs without fixed-asset input VAT credit
        vat_no_credit = evaluate_economics(
            scenarios=base_scenario,
            financial={
                "currency": "CNY", "project_life_years": 25, "opex_per_kw_year": 45.0,
                "vat": {"rate_pct": 13.0, "tariff_includes_vat": True, "surtax_pct": 12.0},
            },
            tariff=tariff,
        )["scenarios"][0]["financials"]

        vat_with_credit = evaluate_economics(
            scenarios=base_scenario,
            financial={
                "currency": "CNY", "project_life_years": 25, "opex_per_kw_year": 45.0,
                "vat": {
                    "rate_pct": 13.0,
                    "tariff_includes_vat": True,
                    "deductible_capex_share": 0.85,
                    "surtax_pct": 12.0,
                },
            },
            tariff=tariff,
        )["scenarios"][0]["financials"]
        self.assertGreater(vat_with_credit["pre_tax_project_irr_pct"], vat_no_credit["pre_tax_project_irr_pct"] + 0.5)
        self.assertIsNotNone(vat_with_credit["tax_summary"]["vat_credit_exhausted_year"])
        self.assertEqual(vat_with_credit["cashflow_schedule"][0]["vat_payable"], 0.0)

        # 3) Full stack: VAT + CIT (3 exempt + 3 half-rate) + 20% equity / 80% debt + staged OPEX + upfront lease
        full = evaluate_economics(
            scenarios=base_scenario,
            financial={
                "currency": "CNY",
                "project_life_years": 25,
                "discount_rate_pct": 6.0,
                "opex_escalation_pct": 0.0,
                "upfront_opex": 5_000_000.0,
                "opex_tiers": [
                    {"start_year": 1, "end_year": 5, "opex_per_kw_year": 35.0, "label": "warranty_om"},
                    {"start_year": 6, "end_year": 25, "opex_per_kw_year": 50.0, "label": "post_warranty_om"},
                    {"start_year": 1, "end_year": 25, "extra_annual_opex": 1_000_000.0, "label": "land_lease"},
                ],
                "vat": {
                    "rate_pct": 13.0,
                    "tariff_includes_vat": True,
                    "deductible_capex_share": 0.85,
                    "surtax_pct": 12.0,
                },
                "cit": {
                    "rate_pct": 25.0,
                    "exempt_years": 3,
                    "half_rate_years": 3,
                    "depreciation_years": 20,
                    "residual_value_pct": 5.0,
                    "loss_carryforward_years": 5,
                },
                "financing": {
                    "equity_ratio_pct": 20.0,
                    "loan_tenor_years": 15,
                    "loan_grace_years": 1,
                    "loan_interest_rate_pct": 3.5,
                    "repayment_method": "equal_principal",
                },
            },
            tariff=tariff,
            rank_by="equity_irr",
        )["scenarios"][0]["financials"]

        sched = full["cashflow_schedule"]
        # Years 1..5 OPEX = 100_000 * 35 + 1_000_000 = 4,500,000; Year 6+ = 6,000,000
        self.assertAlmostEqual(sched[0]["opex"], 4_500_000.0, places=2)
        self.assertAlmostEqual(sched[5]["opex"], 6_000_000.0, places=2)
        # CIT holiday: Years 1-3 = 0, Year 4 > 0 (12.5%), Year 7 > Year 4 (25%)
        self.assertEqual(sched[0]["cit_project"], 0.0)
        self.assertEqual(sched[2]["cit_project"], 0.0)
        self.assertGreater(sched[3]["cit_project"], 0.0)
        self.assertGreater(sched[6]["cit_project"], sched[3]["cit_project"])
        # Year 1 is interest-only grace period (principal = 0)
        self.assertEqual(sched[0]["debt_principal"], 0.0)
        self.assertGreater(sched[0]["debt_interest"], 0.0)
        self.assertGreater(sched[1]["debt_principal"], 0.0)
        # Year 16 has zero debt service
        self.assertEqual(sched[15]["debt_service"], 0.0)
        # Post-tax IRR < Pre-tax IRR, and Equity IRR > Post-tax Project IRR (3.5% debt < project IRR)
        self.assertLess(full["post_tax_project_irr_pct"], full["pre_tax_project_irr_pct"])
        self.assertGreater(full["financing_summary"]["equity_irr_pct"], full["post_tax_project_irr_pct"])
        self.assertGreater(full["financing_summary"]["min_dscr"], 1.0)


if __name__ == "__main__":
    unittest.main()

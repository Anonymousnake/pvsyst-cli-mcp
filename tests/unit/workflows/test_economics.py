"""Unit tests for PV & PV+BESS financial, TOU tariff, LCOE, NPV, and IRR evaluator."""
from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.engineering.economics import evaluate_economics


def _write_sfi_csv(path: Path, columns: list[tuple[str, str]], rows: list[list]) -> Path:
    lines = [
        "Project;EconTest.PRJ",
        "Geographical Site;EconSite.SIT",
        "Weather data;EconMet.MET",
        "Simulation variant;EconTest.VC0",
        "date;" + ";".join(c for c, _ in columns),
        ";" + ";".join(u for _, u in columns),
    ]
    for row in rows:
        lines.append(";".join(str(v) for v in row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_ci_tou_csv(path: Path, *, days: int = 10, step_minutes: int = 60, with_battery: bool = False) -> Path:
    cols = [("E_Grid", "kW"), ("E_User", "kW"), ("EFrGrid", "kW"), ("EBatCh", "kW"), ("EBatDis", "kW")]
    rows: list[list] = []
    start = datetime(2024, 6, 1, 0, 0)
    total = int(days * 24 * 60 / step_minutes)
    for i in range(total):
        stamp = start + timedelta(minutes=i * step_minutes)
        h = stamp.hour
        load = 50.0 if 8 <= h < 20 else 20.0
        pv_gen = 80.0 if 10 <= h < 16 else (20.0 if 8 <= h < 10 or 16 <= h < 18 else 0.0)
        if with_battery:
            # Charge 20 kW midday (11-15), discharge 25 kW evening peak (18-20)
            bat_ch = 20.0 if 11 <= h < 15 else 0.0
            bat_dis = 25.0 if 18 <= h < 20 else 0.0
            net_pv = pv_gen - bat_ch + bat_dis
        else:
            bat_ch, bat_dis = 0.0, 0.0
            net_pv = pv_gen
        export_kw = max(0.0, net_pv - load)
        import_kw = max(0.0, load - net_pv)
        rows.append([
            stamp.strftime("%d/%m/%y %H:%M"),
            round(export_kw, 4),
            round(load, 4),
            round(import_kw, 4),
            round(bat_ch, 4),
            round(bat_dis, 4),
        ])
    return _write_sfi_csv(path, cols, rows)


class EconomicsEngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for sub in ("Results", "UserBatch", "UserHourly"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def test_flat_feed_in_closed_form_lcoe_npv_irr_and_payback(self):
        # 100 kW PV, 150,000 kWh/yr, CAPEX=400,000 CNY, OPEX=5,000/yr, FIT=0.40/kWh, 0% deg, 0% esc, 6% disc, 25 yr
        res = evaluate_economics(
            scenarios=[{
                "label": "Utility_100kW",
                "annual_yield_kwh": 150_000.0,
                "installed_dc_kw": 100.0,
                "capex_total": 400_000.0,
                "annual_opex": 5_000.0,
                "replacement_events": [{"year": 12, "cost": 30_000.0, "label": "inverter"}],
            }],
            financial={
                "currency": "CNY",
                "project_life_years": 25,
                "discount_rate_pct": 6.0,
                "first_year_degradation_pct": 0.0,
                "annual_degradation_pct": 0.0,
                "opex_escalation_pct": 0.0,
                "tariff_escalation_pct": 0.0,
            },
            tariff={"mode": "feed_in", "feed_in_tariff": 0.40},
        )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["best_scenario"], "Utility_100kW")
        sc = res["scenarios"][0]
        fin = sc["financials"]
        self.assertEqual(sc["specific_yield_kwh_per_kwp"], 1500.0)
        self.assertEqual(fin["year1_gross_benefit"], 60_000.0)
        self.assertEqual(fin["year1_net_benefit"], 55_000.0)
        # Verify exact discounted NPV and LCOE manually
        pv_factor_sum = sum(1.0 / (1.06 ** y) for y in range(1, 26))
        expected_npv = -400_000.0 + 55_000.0 * pv_factor_sum - 30_000.0 / (1.06 ** 12)
        expected_lcoe = (400_000.0 + 5_000.0 * pv_factor_sum + 30_000.0 / (1.06 ** 12)) / (150_000.0 * pv_factor_sum)
        self.assertAlmostEqual(fin["npv"], expected_npv, places=1)
        self.assertAlmostEqual(fin["lcoe"], expected_lcoe, places=4)
        self.assertIsNotNone(fin["irr_pct"])
        self.assertGreater(fin["irr_pct"], 12.0)
        self.assertAlmostEqual(fin["simple_payback_years"], 400_000.0 / 55_000.0, places=2)
        self.assertEqual(len(fin["cashflow_schedule"]), 25)
        self.assertEqual(fin["cashflow_schedule"][11]["replacement_cost"], 30_000.0)

    def test_first_year_benefit_includes_first_year_degradation(self):
        res = evaluate_economics(
            scenarios=[
                {"label": "A", "annual_yield_kwh": 100_000.0, "annual_opex": 15_900.0},
                {"label": "B", "annual_yield_kwh": 80_000.0, "annual_opex": 8_000.0},
            ],
            financial={"first_year_degradation_pct": 2.0},
            tariff={"feed_in_tariff": 0.4},
            rank_by="year1_net_benefit",
        )
        fin = res["scenarios"][0]["financials"]
        year1 = fin["cashflow_schedule"][0]
        self.assertEqual((fin["year1_gross_benefit"], fin["year1_net_benefit"]),
                         (year1["gross_benefit"], year1["net_cashflow"]))
        self.assertEqual(res["best_scenario"], "B")

    def test_tou_pv_and_bess_settlement_consistent_across_hourly_and_15min(self):
        csv_60 = _make_ci_tou_csv(self.root / "Results" / "bess_60m.csv", days=10, step_minutes=60, with_battery=True)
        csv_15 = _make_ci_tou_csv(self.root / "Results" / "bess_15m.csv", days=10, step_minutes=15, with_battery=True)
        csv_pv = _make_ci_tou_csv(self.root / "Results" / "pv_only.csv", days=10, step_minutes=60, with_battery=False)

        tou = {
            "mode": "tou",
            "feed_in_tariff": 0.35,
            "import_tariff": 0.70,
            "tou_schedule": [
                {"name": "valley", "hours": [0, 1, 2, 3, 4, 5, 6, 7], "import_rate": 0.30, "export_rate": 0.25},
                {"name": "peak", "hours": [18, 19, 20, 21], "import_rate": 1.35, "export_rate": 0.45},
                {"name": "flat", "hours": [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 22, 23], "import_rate": 0.75, "export_rate": 0.35},
            ],
        }
        fin = {"currency": "CNY", "capex_per_kw": 3500.0, "battery_capex_per_kwh": 1000.0, "opex_pct_capex": 1.5}
        res = evaluate_economics(
            workspace=self.root,
            scenarios=[
                {"label": "PV_Only", "csv_name": csv_pv.name, "installed_dc_kw": 100.0},
                {"label": "BESS_60m", "csv_name": csv_60.name, "installed_dc_kw": 100.0, "battery_kwh": 100.0},
                {"label": "BESS_15m", "csv_name": csv_15.name, "installed_dc_kw": 100.0, "battery_kwh": 100.0},
            ],
            financial=fin,
            tariff=tou,
            rank_by="year1_net_benefit",
        )
        by_label = {s["label"]: s for s in res["scenarios"]}
        self.assertEqual(by_label["BESS_60m"]["settlement"]["step_minutes"], 60.0)
        self.assertEqual(by_label["BESS_15m"]["settlement"]["step_minutes"], 15.0)
        # 60-min and 15-min profiles have identical piecewise-constant powers -> exact same energy & TOU benefit!
        self.assertAlmostEqual(
            by_label["BESS_60m"]["financials"]["year1_gross_benefit"],
            by_label["BESS_15m"]["financials"]["year1_gross_benefit"],
            places=2,
        )
        # Shifting cheap midday export (0.35) to evening peak (1.35) increases year-1 gross benefit!
        self.assertGreater(
            by_label["BESS_60m"]["financials"]["year1_gross_benefit"],
            by_label["PV_Only"]["financials"]["year1_gross_benefit"],
        )
        self.assertIn("peak", by_label["BESS_60m"]["settlement"]["tou_breakdown"])
        self.assertEqual(len(by_label["BESS_60m"]["settlement"]["typical_day_profile"]), 24)

    def test_grid_import_cost_is_settled_without_user_load(self):
        signed = _write_sfi_csv(self.root / "Results" / "signed.csv", [("E_Grid", "kW")], [
            ["01/06/24 17:45", 20.0],
            ["01/06/24 18:00", -4.0],
        ])
        separate = _write_sfi_csv(self.root / "Results" / "separate.csv",
                                  [("EToGrid", "Wh"), ("EFrGrid", "Wh")], [
            ["01/06/24 17:45", 5000.0, 0.0],
            ["01/06/24 18:00", 0.0, 1000.0],
        ])
        res = evaluate_economics(
            workspace=self.root,
            scenarios=[{"csv_name": csv.name} for csv in (signed, separate)],
            financial={"first_year_degradation_pct": 0.0},
            tariff={"mode": "tou", "feed_in_tariff": 0.4, "import_tariff": 0.3,
                    "tou_schedule": [{"name": "peak", "hours": [18], "import_rate": 0.8}]},
        )
        for scenario in res["scenarios"]:
            with self.subTest(scenario=scenario["label"]):
                summary = scenario["settlement"]["energy_summary"]
                self.assertEqual((summary["grid_import_kwh"], summary["actual_grid_bill"],
                                  scenario["financials"]["year1_gross_benefit"]), (1.0, 0.8, 1.2))

    def test_batch_summary_csv_ranking_and_mcp_tool_roundtrip(self):
        (self.root / "UserBatch" / "Sweep_Results.CSV").write_text(
            "Ident;Create hourly;Plane tilt;Error;E_Grid;PR\n"
            "SIM_1;;15;;142000;0.82\n"
            "SIM_2;;25;;158000;0.84\n"
            "SIM_3;;35;;151000;0.83\n",
            encoding="utf-8",
        )
        import pvsyst_auto.api.engineering as api_eng
        from pvsyst_auto.api.schemas import EconomicsFinancialConfig, EconomicsTariffConfig

        fake_cli = self.root / "PVsystCLI.exe"
        fake_cli.write_bytes(b"stub")
        with patch.dict(os.environ, {"PVSYST_WORKSPACE": str(self.root), "PVSYST_CLI": str(fake_cli)}):
            res = api_eng.pvsyst_evaluate_economics(
                batch_summary_csv="Sweep_Results.CSV",
                financial=EconomicsFinancialConfig(fixed_capex=450_000.0, opex_pct_capex=1.2),
                tariff=EconomicsTariffConfig(mode="feed_in", feed_in_tariff=0.42),
                rank_by="lcoe",
                include_cashflow_schedule=False,
            )
        self.assertEqual(res["scenario_count"], 3)
        self.assertEqual(res["best_scenario"], "SIM_2")
        self.assertEqual([r["label"] for r in res["ranking"]], ["SIM_2", "SIM_3", "SIM_1"])
        self.assertEqual([s["batch_parameters"] for s in res["scenarios"]],
                         [{"Plane tilt": 15}, {"Plane tilt": 25}, {"Plane tilt": 35}])
        self.assertNotIn("cashflow_schedule", res["scenarios"][0]["financials"])

    def test_batch_summary_multiline_headers_populate_batch_parameters(self):
        (self.root / "UserBatch" / "MultiHdr_Results.CSV").write_text(
            "PVsyst simulations Batch mode;;;;;;;;\n"
            "Ident;Create hourly;Plane;Soiling;Simul;Error;E_Grid;PR\n"
            ";file;tilt;Loss;Comment;;kWh;ratio\n"
            ";File name;[deg];[%];;;;\n"
            "SIM_1;;20;1.5;Tilt20;;13500;0.85\n"
            "SIM_2;;30;2.0;Tilt30;;14250;0.87\n",
            encoding="utf-8",
        )
        res = evaluate_economics(
            workspace=self.root,
            batch_summary_csv="MultiHdr_Results.CSV",
            financial={"first_year_degradation_pct": 0.0},
            tariff={"feed_in_tariff": 0.4},
        )
        self.assertEqual(
            res["scenarios"][0]["batch_parameters"],
            {"Plane tilt": 20, "Soiling Loss": 1.5, "Simul Comment": "Tilt20"},
        )
        self.assertEqual(
            res["scenarios"][1]["batch_parameters"],
            {"Plane tilt": 30, "Soiling Loss": 2.0, "Simul Comment": "Tilt30"},
        )

    def test_batch_summary_can_omit_performance_ratio(self):
        (self.root / "UserBatch" / "EnergyResults.CSV").write_text(
            "Ident;Create hourly;Error;E_Grid\n"
            "SIM_1;;;142000\n",
            encoding="utf-8",
        )
        res = evaluate_economics(
            workspace=self.root,
            batch_summary_csv="EnergyResults.CSV",
            financial={"first_year_degradation_pct": 0.0},
            tariff={"feed_in_tariff": 0.4},
        )
        self.assertEqual(res["scenarios"][0]["year1_energy_kwh"], 142_000.0)
        self.assertEqual(res["scenarios"][0]["financials"]["year1_gross_benefit"], 56_800.0)

    def test_batch_hourly_reference_resolves_in_user_hourly(self):
        csv_name = "Sweep_SIM_1.CSV"
        for folder, value in (("Results", 40.0), ("UserHourly", 10.0)):
            _write_sfi_csv(self.root / folder / csv_name, [("E_Grid", "kWh")], [
                ["01/06/24 12:00", value], ["01/06/24 13:00", value],
            ])
        (self.root / "UserBatch" / "SweepResults.CSV").write_text(
            "Ident;Create hourly;Error;E_Grid;PR\n"
            f"SIM_1;{csv_name};;20;0.83\n",
            encoding="utf-8",
        )
        res = evaluate_economics(
            workspace=self.root,
            scenarios=[{"label": "Standalone", "csv_name": csv_name}],
            batch_summary_csv="SweepResults.CSV",
            financial={"first_year_degradation_pct": 0.0},
        )
        self.assertEqual([s["year1_energy_kwh"] for s in res["scenarios"]], [80.0, 20.0])

    def test_validation_guards(self):
        with self.assertRaisesRegex(ValueError, "rank_by"):
            evaluate_economics(scenarios=[{"label": "A", "annual_yield_kwh": 1000.0}], rank_by="bad")
        with self.assertRaisesRegex(ValueError, "tou_schedule"):
            evaluate_economics(scenarios=[{"label": "A", "annual_yield_kwh": 1000.0}], tariff={"mode": "tou"})
        with self.assertRaisesRegex(ValueError, "installed_dc_kw"):
            evaluate_economics(
                scenarios=[{"label": "A", "annual_yield_kwh": 1000.0}],
                financial={"capex_per_kw": 3500.0},
            )

    def test_two_part_demand_charge_and_capacity_comparison(self) -> None:
        # 2 months (Jan & Feb) across a contiguous hourly boundary: load peak 200 kW, import peak 120 kW -> 80 kW shaved each month
        rows = [
            ("31/01/24 22:00", 80.0, 200.0, 120.0, 0.0, 0.0),
            ("31/01/24 23:00", 60.0, 150.0, 90.0, 0.0, 0.0),
            ("01/02/24 00:00", 80.0, 200.0, 120.0, 0.0, 0.0),
            ("01/02/24 01:00", 50.0, 140.0, 90.0, 0.0, 0.0),
        ]
        csv_path = self.root / "Results" / "demand_case.csv"
        _write_sfi_csv(
            csv_path,
            [("EToGrid", "kW"), ("E_User", "kW"), ("EFrGrid", "kW"), ("EBatCh", "kW"), ("EBatDis", "kW")],
            [list(r) for r in rows],
        )
        tariff_demand = {
            "mode": "self_consumption_flat",
            "import_tariff": 0.8,
            "feed_in_tariff": 0.3,
            "demand_charge": {
                "billing_mode": "demand",
                "demand_rate_per_kw_month": 40.0,
                "months": [1, 2],
            },
        }
        res_dc = evaluate_economics(
            workspace=self.root,
            scenarios=[{"label": "Demand", "csv_name": csv_path.name, "capex_total": 100000.0}],
            financial={"project_life_years": 10, "discount_rate_pct": 6.0},
            tariff=tariff_demand,
        )
        dc = res_dc["scenarios"][0]["settlement"]["demand_charge_summary"]
        self.assertIsNotNone(dc)
        # 2 active months * (200 - 120) kW * 40 CNY/kW = 6400 CNY
        self.assertAlmostEqual(dc["baseline_annual_demand_charge"], 16000.0, places=2)
        self.assertAlmostEqual(dc["actual_annual_demand_charge"], 9600.0, places=2)
        self.assertAlmostEqual(dc["annual_demand_savings"], 6400.0, places=2)
        self.assertAlmostEqual(res_dc["scenarios"][0]["financials"]["year1_demand_savings"], 6400.0, places=2)

        # Compare mode vs transformer capacity billing (400 kVA * 28 CNY/kVA * 2 mo = 22400 > 9600 => recommend demand)
        tariff_cmp = {
            "mode": "self_consumption_flat",
            "import_tariff": 0.8,
            "feed_in_tariff": 0.3,
            "demand_charge": {
                "billing_mode": "compare",
                "demand_rate_per_kw_month": 40.0,
                "capacity_rate_per_kva_month": 28.0,
                "contract_capacity_kva": 400.0,
                "months": [1, 2],
            },
        }
        res_cmp = evaluate_economics(
            workspace=self.root,
            scenarios=[{"label": "Compare", "csv_name": csv_path.name, "capex_total": 100000.0}],
            financial={"project_life_years": 10, "discount_rate_pct": 6.0},
            tariff=tariff_cmp,
        )
        dc_cmp = res_cmp["scenarios"][0]["settlement"]["demand_charge_summary"]
        self.assertEqual(dc_cmp["recommended_billing_mode"], "demand")
        self.assertAlmostEqual(dc_cmp["annual_capacity_charge"], 22400.0, places=2)

    def test_battery_replacement_and_sensitivity_tornado(self) -> None:
        base = evaluate_economics(
            scenarios=[{"label": "BESS", "annual_yield_kwh": 150000.0, "installed_dc_kw": 100.0, "battery_kwh": 200.0}],
            financial={
                "project_life_years": 20,
                "discount_rate_pct": 6.0,
                "capex_per_kw": 3500.0,
                "battery_capex_per_kwh": 1000.0,
                "opex_pct_capex": 1.0,
            },
            tariff={"mode": "self_consumption_flat", "import_tariff": 0.9, "feed_in_tariff": 0.35, "self_consumption_ratio": 0.8},
        )
        with_rep = evaluate_economics(
            scenarios=[{"label": "BESS", "annual_yield_kwh": 150000.0, "installed_dc_kw": 100.0, "battery_kwh": 200.0}],
            financial={
                "project_life_years": 20,
                "discount_rate_pct": 6.0,
                "capex_per_kw": 3500.0,
                "battery_capex_per_kwh": 1000.0,
                "opex_pct_capex": 1.0,
                "battery_replacement_year": 12,
                "battery_replacement_cost_ratio": 0.6,
            },
            tariff={"mode": "self_consumption_flat", "import_tariff": 0.9, "feed_in_tariff": 0.35, "self_consumption_ratio": 0.8},
            sensitivity={"enabled": True, "variation_pct": 15.0},
        )
        sc_rep = with_rep["scenarios"][0]
        # Year 12 replacement = 200 kWh * 1000 * 0.6 = 120,000
        yr12 = sc_rep["financials"]["cashflow_schedule"][11]
        self.assertEqual(yr12["year"], 12)
        self.assertAlmostEqual(yr12["replacement_cost"], 120000.0, places=2)
        self.assertLess(sc_rep["financials"]["npv"], base["scenarios"][0]["financials"]["npv"])

        sens = sc_rep["sensitivity"]
        self.assertEqual(sens["variation_pct"], 15.0)
        self.assertEqual(len(sens["factors"]), 4)
        # Factors must be sorted descending by npv_swing
        swings = [f["npv_swing"] for f in sens["factors"]]
        self.assertEqual(swings, sorted(swings, reverse=True))
        # Tariff +15% raises NPV; CAPEX +15% lowers NPV
        by_name = {f["factor"]: f for f in sens["factors"]}
        self.assertGreater(by_name["tariff"]["high"]["npv"], by_name["tariff"]["low"]["npv"])
        self.assertLess(by_name["capex"]["high"]["npv"], by_name["capex"]["low"]["npv"])

    def test_tou_period_name_requires_consistent_rates(self):
        csv = _write_sfi_csv(self.root / "Results" / "seasons.csv", [("E_Grid", "kWh")], [
            ["31/05/24 23:00", 10.0],
            ["01/06/24 00:00", 10.0],
        ])
        may = {"name": "off_peak", "months": [5], "hours": [23], "export_rate": 0.3}
        june = {"name": "off_peak", "months": [6], "hours": [0], "export_rate": 0.4}
        with self.assertRaisesRegex(ValueError, "off_peak.*different rates"):
            evaluate_economics(
                workspace=self.root,
                scenarios=[{"csv_name": csv.name}],
                tariff={"mode": "tou", "tou_schedule": [may, june]},
            )

    def test_irregular_timestamp_cadence_is_rejected(self):
        gap_csv = self.root / "Results" / "gap_econ.csv"
        gap_csv.write_text(
            "PVSYST 8.1.7\n"
            "date;E_Grid\n"
            ";kW\n"
            "01/06/24 08:00;10.0\n"
            "01/06/24 08:15;10.0\n"
            "01/06/24 08:45;10.0\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "regular cadence"):
            evaluate_economics(
                workspace=self.root,
                scenarios=[{"label": "Gap", "csv_name": "gap_econ.csv"}],
                financial={"fixed_capex": 100_000.0},
                tariff={"mode": "feed_in", "feed_in_tariff": 0.4},
            )

    def test_no_best_scenario_when_ranking_metric_is_unavailable(self):
        res = evaluate_economics(
            scenarios=[
                {"label": "A", "annual_yield_kwh": 1_000.0},
                {"label": "B", "annual_yield_kwh": 2_000.0},
            ],
            financial={"fixed_capex": 50_000.0},
            tariff={"feed_in_tariff": 0.4},
            rank_by="simple_payback_years",
        )
        self.assertEqual([row["simple_payback_years"] for row in res["ranking"]], [None, None])
        self.assertIsNone(res["best_scenario"])


if __name__ == "__main__":
    unittest.main()

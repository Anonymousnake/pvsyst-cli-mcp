"""Unit tests for TOU PowerShift 48-slot planner and two-stage hybrid PV+BESS sizing optimizer."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from pvsyst_auto.api.schemas import GridStorageUpdates
from pvsyst_auto.energy.tou_planner import plan_tou_storage_schedule
from pvsyst_auto.workflows.bess_optimize import optimize_bess_sizing


def _china_ci_tou() -> list[dict]:
    return [
        {"name": "valley_night", "hours": [0, 1, 2, 3, 4, 5, 6, 7], "import_rate": 0.30, "export_rate": 0.30},
        {"name": "peak_morning", "hours": [8, 9, 10, 11], "import_rate": 1.15, "export_rate": 0.35},
        {"name": "midday_flat", "hours": [12, 13, 14, 15], "import_rate": 0.60, "export_rate": 0.35},
        {"name": "flat_afternoon", "hours": [16, 17], "import_rate": 0.75, "export_rate": 0.35},
        {"name": "sharp_evening", "hours": [18, 19, 20, 21], "import_rate": 1.35, "export_rate": 0.35},
        {"name": "late_flat", "hours": [22, 23], "import_rate": 0.65, "export_rate": 0.35},
    ]


def _write_pv_load_csv(path: Path, *, days: int = 30) -> Path:
    cols = [("GlobInc", "W/m2"), ("EArray", "kW"), ("E_Grid", "kW"), ("PR", "ratio"), ("E_User", "kW"), ("EFrGrid", "kW")]
    lines = [
        "Project;BessOpt.PRJ",
        "Geographical Site;Site.SIT",
        "Weather data;Met.MET",
        "Simulation variant;BessOpt.VC0",
        "date;" + ";".join(c for c, _ in cols),
        ";" + ";".join(u for _, u in cols),
    ]
    # Contiguous hourly timestamps starting Jan 1 so regular-cadence checks always pass
    start = datetime(2024, 1, 1, 0, 0)
    for step in range(max(1, days) * 24):
        dt = start + timedelta(hours=step)
        h = dt.hour
        gi = 800.0 if 9 <= h < 16 else (300.0 if h in (7, 8, 16, 17) else 0.0)
        pv = 120.0 * (gi / 1000.0) * 0.85 if gi > 0 else 0.0
        # Commercial load with sharp evening peak (18-21) and morning peak (8-11)
        if 18 <= h <= 21:
            load = 140.0
        elif 8 <= h <= 11:
            load = 110.0
        elif 12 <= h <= 17:
            load = 85.0
        else:
            load = 25.0
        imp = max(0.0, load - pv)
        lines.append(
            f"{dt.strftime('%d/%m/%y %H:%M')};{gi:.1f};{pv * 1.03:.3f};{pv:.3f};"
            f"{'0.85' if gi > 0 else ''};{load:.2f};{imp:.2f}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TouPlannerAndBessOptimizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for sub in ("Results", "UserHourly", "Models", "Projects"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tou_arbitrage_rejects_seasonal_or_day_specific_windows(self) -> None:
        from pvsyst_auto.api.schemas import EconomicsTariffConfig, EconomicsFinancialConfig
        from pvsyst_auto.api.simulation import pvsyst_optimize_bess_sizing

        csv_path = _write_pv_load_csv(self.root / "Results" / "seasonal.csv", days=2)
        for restriction in ({"months": [1, 2, 3]}, {"day_type": "weekend"}):
            with self.subTest(restriction=restriction):
                tariff = EconomicsTariffConfig(mode="tou", import_tariff=0.5, tou_schedule=[
                    {"name": "valley", "hours": list(range(8)), "import_rate": 0.1, **restriction},
                    {"name": "peak", "hours": list(range(16, 24)), "import_rate": 1.0},
                ])
                with patch("pvsyst_auto.api.simulation.cli") as mock_cli, patch("pvsyst_auto.api.simulation.variant_store"):
                    mock_cli.return_value.workspace = self.root
                    with self.assertRaisesRegex(ValueError, "month.*day"):
                        pvsyst_optimize_bess_sizing(
                            baseline_csv=csv_path.name, installed_dc_kw=120, bess_kwh_values=[100],
                            pcs_kw_values=[50], tariff=tariff,
                            financial=EconomicsFinancialConfig(fixed_capex=1000),
                            strategies=["tou_arbitrage"],
                        )

    def test_plan_tou_two_charge_two_discharge_and_schema_compat(self) -> None:
        plan = plan_tou_storage_schedule(
            tou_schedule=_china_ci_tou(),
            charge_power_kw=100.0,
            discharge_power_kw=100.0,
            usable_energy_kwh=200.0,
            cycles_per_day=2,
            round_trip_efficiency=0.88,
            min_spread=0.10,
        )
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(plan["planned_cycles_count"], 2)
        self.assertEqual(len(plan["charging_schedule_48"]), 48)
        self.assertEqual(len(plan["discharging_schedule_48"]), 48)

        # Charge and discharge slots must never overlap
        for ch, dis in zip(plan["charging_schedule_48"], plan["discharging_schedule_48"]):
            self.assertFalse(ch > 0 and dis > 0)

        # Cycle 1 charges in valley_night (0..7) and discharges in peak_morning (8..11)
        # Cycle 2 charges in midday_flat (12..15) and discharges in sharp_evening (18..21)
        c1, c2 = plan["cycles"]
        self.assertEqual(c1["charge_period"], "valley_night")
        self.assertEqual(c1["discharge_period"], "peak_morning")
        self.assertEqual(c2["charge_period"], "midday_flat")
        self.assertEqual(c2["discharge_period"], "sharp_evening")

        # Power capped so planned_discharge_kwh <= usable_energy_kwh * sqrt(rte)
        self.assertLessEqual(c1["planned_discharge_kwh"], 200.0)
        self.assertLessEqual(c2["planned_discharge_kwh"], 200.0)

        # Must validate cleanly against GridStorageUpdates (drop-in for PVsyst native storage!)
        validated = GridStorageUpdates.model_validate(plan["storage_updates"])
        self.assertEqual(validated.strategy, "power_shift")
        self.assertEqual(validated.discharging_strategy, "specified_hours")
        self.assertEqual(len(validated.charging_schedule_48), 48)

    def test_short_charge_window_limits_scheduled_discharge(self) -> None:
        plan = plan_tou_storage_schedule(
            tou_schedule=[{"name": "valley", "hours": [0], "import_rate": 0.1}],
            default_import_rate=0.8, charge_power_kw=10, discharge_power_kw=20,
            usable_energy_kwh=100, round_trip_efficiency=0.81, cycles_per_day=1,
        )
        charged = sum(plan["charging_schedule_48"]) / 2
        discharged = sum(plan["discharging_schedule_48"]) / 2
        self.assertEqual(charged, 10)
        self.assertAlmostEqual(discharged, 8.1, delta=0.023)
        self.assertLessEqual(discharged, charged * 0.81)

    def test_exact_spread_and_previous_evening_charge(self) -> None:
        for rates, spread, window in [([0.2]*12 + [0.3]*12, 0.1, range(12)),
                                      ([0.9]*8 + [1.0]*4 + [0.2]*12, 0.25, range(12, 24))]:
            with self.subTest(rates=rates):
                schedule = [{"hours": [h for h in range(24) if rates[h] == rate], "import_rate": rate}
                            for rate in sorted(set(rates))]
                plan = plan_tou_storage_schedule(tou_schedule=schedule, charge_power_kw=10,
                                                 cycles_per_day=1, round_trip_efficiency=1, min_spread=spread)
                self.assertEqual([cycle["charge_hours"] for cycle in plan["cycles"]], [list(window)])

    def test_public_stage2_allocates_available_variants(self) -> None:
        from pvsyst_auto.api.schemas import BessVerifyConfig, EconomicsFinancialConfig, EconomicsTariffConfig
        from pvsyst_auto.api.simulation import pvsyst_optimize_bess_sizing

        csv_path = _write_pv_load_csv(self.root / "Results" / "variant.csv", days=2)
        projects = self.root / "Projects"
        for suffix in ("VC0", "VC1", "VC3", "vc4"):
            (projects / f"Demo.{suffix}").write_text("stub", encoding="utf-8")
        store = MagicMock()
        store._path.return_value = projects / "Demo.PRJ"
        fake_cli = MagicMock(workspace=self.root)
        def fake_sim(project, variant, *, sfi, out_csv, log_level):
            _write_pv_load_csv(out_csv, days=2)
            return {"warnings": []}
        fake_cli.run_simulation.side_effect = fake_sim
        kwargs = dict(baseline_csv=csv_path.name, installed_dc_kw=100, bess_kwh_values=[100, 200],
                      pcs_kw_values=[50], tariff=EconomicsTariffConfig(mode="self_consumption_flat", import_tariff=1),
                      financial=EconomicsFinancialConfig(fixed_capex=1000), verify_top_k=2,
                      verify_config=BessVerifyConfig(project="Demo.PRJ", source_variant="VC0",
                          battery_file_name="Cell.BTR", battery_unit_kwh=100))
        with patch("pvsyst_auto.api.simulation.cli", return_value=fake_cli), \
             patch("pvsyst_auto.api.simulation.variant_store", return_value=store), \
             patch("pvsyst_auto.energy.system.create_grid_storage_system", return_value={"status": "created"}) as create:
            preview = pvsyst_optimize_bess_sizing(**kwargs)
            result = pvsyst_optimize_bess_sizing(**kwargs, dry_run=False, confirm=True)
            names = [p["new_variant"] for p in preview["stage2_verification"]["planned_variants"]]
            self.assertEqual(names, ["VC2", "VC5"])
            self.assertEqual([p["new_variant"] for p in result["stage2_verification"]["planned_variants"]], names)
            self.assertEqual([c.kwargs["new_variant"] for c in create.call_args_list], names)
            self.assertEqual([c.args[1] for c in fake_cli.run_simulation.call_args_list], names)
            kwargs["verify_config"].variant_prefix = "VC1"
            with self.assertRaisesRegex(ValueError, "three characters"):
                pvsyst_optimize_bess_sizing(**kwargs)

    def test_plan_tou_idle_when_spread_too_small(self) -> None:
        flat_tou = [
            {"name": "low", "hours": list(range(0, 12)), "import_rate": 0.50},
            {"name": "high", "hours": list(range(12, 24)), "import_rate": 0.55},
        ]
        plan = plan_tou_storage_schedule(
            tou_schedule=flat_tou,
            charge_power_kw=50.0,
            round_trip_efficiency=0.88,
            min_spread=0.05,
        )
        self.assertEqual(plan["status"], "idle")
        self.assertEqual(plan["planned_cycles_count"], 0)
        self.assertEqual(sum(plan["charging_schedule_48"]), 0.0)
        self.assertEqual(sum(plan["discharging_schedule_48"]), 0.0)
        self.assertTrue(plan["warnings"])

    def test_stage1_hybrid_bess_grid_pareto_and_heatmap(self) -> None:
        csv_path = _write_pv_load_csv(self.root / "Results" / "base_pv.csv", days=36)
        tariff = {
            "mode": "tou",
            "import_tariff": 0.70,
            "feed_in_tariff": 0.35,
            "tou_schedule": _china_ci_tou(),
            "demand_charge": {
                "billing_mode": "demand",
                "demand_rate_per_kw_month": 42.0,
            },
        }
        financial = {
            "currency": "CNY",
            "project_life_years": 20,
            "discount_rate_pct": 6.0,
            "capex_per_kw": 3500.0,
            "battery_capex_per_kwh": 950.0,
            "opex_pct_capex": 1.0,
            "battery_replacement_year": 12,
            "battery_replacement_cost_ratio": 0.6,
        }
        res = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=csv_path.name,
            installed_dc_kw=120.0,
            bess_kwh_values=[0.0, 100.0, 200.0, 300.0],
            pcs_kw_values=[50.0, 100.0],
            strategies=["self_consumption", "tou_arbitrage", "peak_shaving"],
            tariff=tariff,
            financial=financial,
            rank_by="npv",
            verify_top_k=0,
            dry_run=True,
        )
        self.assertEqual(res["status"], "completed")
        # 1 PV-only + 3 non-zero bess * 2 pcs * 3 strategies = 19 candidates
        self.assertEqual(res["evaluated_count"], 19)
        self.assertEqual(res["best"]["rank"], 1)
        # Pareto frontier has non-decreasing capex and strictly increasing npv
        pareto = res["pareto_frontier"]
        self.assertGreaterEqual(len(pareto), 1)
        for a, b in zip(pareto, pareto[1:]):
            self.assertLessEqual(a["capex_total"], b["capex_total"])
            self.assertGreater(b["npv"], a["npv"])

        # Heatmap dimensions match pcs_kw x nonzero bess_kwh
        hm = res["heatmap"]
        self.assertEqual(hm["bess_kwh"], [100.0, 200.0, 300.0])
        self.assertEqual(hm["pcs_kw"], [50.0, 100.0])
        self.assertEqual(len(hm["z"]), 2)
        self.assertEqual(len(hm["z"][0]), 3)

        # Peak shaving strategy achieves positive peak demand reduction
        ps_cands = [c for c in res["candidates"] if c["strategy"] == "peak_shaving"]
        self.assertTrue(any(c["avg_monthly_peak_reduction_kw"] > 0 for c in ps_cands))

    def test_stage2_dry_run_guard_and_mocked_cli_reconciliation(self) -> None:
        csv_path = _write_pv_load_csv(self.root / "Results" / "base_s2.csv", days=24)
        tariff = {"mode": "tou", "import_tariff": 0.7, "feed_in_tariff": 0.3, "tou_schedule": _china_ci_tou()}
        financial = {"project_life_years": 15, "discount_rate_pct": 6.0, "capex_per_kw": 3500.0, "battery_capex_per_kwh": 900.0}
        v_cfg = {
            "project": "Demo.PRJ",
            "source_variant": "VC0",
            "battery_file_name": "LFP_100kWh.BTR",
            "battery_unit_kwh": 100.0,
            "variant_prefix": "VC",
        }

        # dry_run=True plans Stage 2 without launching CLI
        plan = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=csv_path.name,
            installed_dc_kw=120.0,
            bess_kwh_values=[100.0, 200.0],
            pcs_kw_values=[50.0],
            tariff=tariff,
            financial=financial,
            verify_top_k=2,
            verify_config=v_cfg,
            dry_run=True,
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["stage2_verification"]["status"], "ready_for_cli_verification")
        self.assertEqual(len(plan["stage2_verification"]["planned_variants"]), 2)

        # dry_run=False without confirm=True must fail closed
        with self.assertRaisesRegex(ValueError, "confirm=True"):
            optimize_bess_sizing(
                workspace=self.root,
                baseline_csv=csv_path.name,
                installed_dc_kw=120.0,
                bess_kwh_values=[100.0, 200.0],
                pcs_kw_values=[50.0],
                tariff=tariff,
                financial=financial,
                verify_top_k=1,
                verify_config=v_cfg,
                dry_run=False,
                confirm=False,
            )

        # Mocked Stage 2 execution reconciles fast model vs PVsyst exact CSV
        fake_cli = MagicMock()
        fake_cli.workspace = self.root

        def _fake_sim(project, variant, *, sfi, out_csv, log_level=3):
            _write_pv_load_csv(Path(out_csv), days=24)
            return {"returncode": 0, "warnings": []}

        fake_cli.run_simulation.side_effect = _fake_sim
        fake_store = MagicMock()

        with patch("pvsyst_auto.energy.system.create_grid_storage_system", return_value={"status": "created"}) as mock_create:
            verified = optimize_bess_sizing(
                workspace=self.root,
                cli=fake_cli,
                store=fake_store,
                baseline_csv=csv_path.name,
                installed_dc_kw=120.0,
                bess_kwh_values=[100.0, 200.0],
                pcs_kw_values=[50.0],
                tariff=tariff,
                financial=financial,
                verify_top_k=1,
                verify_config=v_cfg,
                dry_run=False,
                confirm=True,
            )
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["stage2_verification"]["status"], "verified")
        self.assertEqual(mock_create.call_count, 1)
        comps = verified["stage2_verification"]["verified_comparisons"]
        self.assertEqual(len(comps), 1)
        self.assertIn("npv_diff", comps[0]["deltas"])

        # Re-running Stage 2 with same prefix + peak_shaving + pcs_capex_per_kw must not crash
        # on existing SFI, must omit load_manifest for peak_shaving, and must include PCS CAPEX
        v_cfg_ps = dict(v_cfg, load_manifest_name="load_manifest.json")
        with patch("pvsyst_auto.energy.system.create_grid_storage_system", return_value={"status": "created"}) as mock_create_ps:
            verified_ps = optimize_bess_sizing(
                workspace=self.root,
                cli=fake_cli,
                store=fake_store,
                baseline_csv=csv_path.name,
                installed_dc_kw=120.0,
                bess_kwh_values=[100.0],
                pcs_kw_values=[50.0],
                strategies=["peak_shaving"],
                tariff=tariff,
                financial=financial,
                pcs_capex_per_kw=600.0,
                verify_top_k=1,
                verify_config=v_cfg_ps,
                dry_run=False,
                confirm=True,
            )
        self.assertEqual(verified_ps["status"], "verified")
        self.assertIsNone(mock_create_ps.call_args.kwargs["load_manifest_name"])
        # 120*3500 + 100*900 + 50*600 = 540,000
        self.assertEqual(verified_ps["stage2_verification"]["verified_comparisons"][0]["capex_total"], 540_000.0)

    def test_public_stage2_stops_on_blocked_storage_creation(self) -> None:
        from pvsyst_auto.api.schemas import BessVerifyConfig, EconomicsFinancialConfig, EconomicsTariffConfig
        from pvsyst_auto.api.simulation import pvsyst_optimize_bess_sizing

        csv_path = _write_pv_load_csv(self.root / "Results" / "blocked.csv", days=2)
        fake_cli = MagicMock(workspace=self.root)
        fake_cli.run_simulation.side_effect = RuntimeError("unexpected simulation")
        with patch("pvsyst_auto.api.simulation.cli", return_value=fake_cli), \
             patch("pvsyst_auto.api.simulation.variant_store"), \
             patch("pvsyst_auto.energy.system.create_grid_storage_system", return_value={
                 "status": "blocked", "issues": ["battery missing"]}):
            with self.assertRaisesRegex(ValueError, "battery missing"):
                pvsyst_optimize_bess_sizing(
                    baseline_csv=csv_path.name, installed_dc_kw=100, bess_kwh_values=[100], pcs_kw_values=[50],
                    tariff=EconomicsTariffConfig(mode="self_consumption_flat", import_tariff=1),
                    financial=EconomicsFinancialConfig(fixed_capex=1000), verify_top_k=1,
                    verify_config=BessVerifyConfig(project="Demo.PRJ", source_variant="VC0",
                        battery_file_name="Cell.BTR", battery_unit_kwh=100, variant_prefix="VC"),
                    dry_run=False, confirm=True,
                )
        fake_cli.run_simulation.assert_not_called()

    def test_tou_wraparound_no_load_sc_totals_self_cons_pv_reconstruction_and_irregular_cadence(self) -> None:
        # 1. TOU planner must not schedule daytime charge after segment-0 peak when a later peak exists
        wrap_tou = [
            {"name": "midnight_peak", "hours": [0, 1], "import_rate": 1.50, "export_rate": 0.30},
            {"name": "morning_valley", "hours": [2, 3, 4, 5, 6, 7], "import_rate": 0.30, "export_rate": 0.30},
            {"name": "day_flat", "hours": [8, 9, 10, 11, 12, 13, 14, 15, 16, 17], "import_rate": 0.70, "export_rate": 0.30},
            {"name": "evening_peak", "hours": [18, 19, 20, 21, 22, 23], "import_rate": 1.50, "export_rate": 0.30},
        ]
        plan = plan_tou_storage_schedule(tou_schedule=wrap_tou, charge_power_kw=50.0, cycles_per_day=2)
        for cyc in plan["cycles"]:
            # Segment-0 peak [0, 1] must never be paired with daytime hours [2..7] after it
            if cyc["discharge_hours"] == [0, 1]:
                self.assertTrue(all(h >= 24 for h in cyc["charge_hours"]))  # impossible -> not paired
        # Evening peak [18..23] should get the cheapest preceding window [2..7]
        ev_cycles = [c for c in plan["cycles"] if c["discharge_hours"] == [18, 19, 20, 21, 22, 23]]
        self.assertEqual(len(ev_cycles), 1)
        self.assertEqual(ev_cycles[0]["charge_hours"], [2, 3, 4, 5, 6, 7])

        # 2. No-load self_consumption_ratio must reflect in self_consumed_kwh, self_consumption_pct, and pv_export_kwh
        pv_only_csv = self.root / "Results" / "pv_only_sc.csv"
        pv_only_csv.write_text(
            "PVSYST 8.1.7\ndate;E_Grid\n;kWh\n"
            "01/06/24 12:00;100.0\n01/06/24 13:00;100.0\n",
            encoding="utf-8",
        )
        res_sc = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=pv_only_csv.name,
            installed_dc_kw=100.0,
            bess_kwh_values=[0.0],
            pcs_kw_values=[50.0],
            tariff={"mode": "self_consumption_flat", "import_tariff": 0.8, "feed_in_tariff": 0.3, "self_consumption_ratio": 0.6},
            financial={"fixed_capex": 1000.0},
        )
        self.assertAlmostEqual(res_sc["candidates"][0]["self_consumption_pct"], 60.0, places=2)

        # 3. True PVsyst self-consumption CSV (EToGrid + EFrGrid + E_User, negative net E_Grid) reconstructs total PV
        real_sc_csv = self.root / "Results" / "real_sc.csv"
        real_sc_csv.write_text(
            "PVSYST 8.1.7\ndate;E_Grid;EToGrid;EFrGrid;E_User\n;kWh;kWh;kWh;kWh\n"
            "01/06/24 12:00;20;20;0;60\n01/06/24 13:00;-80;0;80;80\n",
            encoding="utf-8",
        )
        res_real = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=real_sc_csv.name,
            installed_dc_kw=100.0,
            bess_kwh_values=[0.0],
            pcs_kw_values=[50.0],
            tariff={"mode": "self_consumption_flat", "import_tariff": 0.8, "feed_in_tariff": 0.3},
            financial={"fixed_capex": 1000.0},
        )
        # Total PV = 20 export + 60 direct self-consumption = 80 kWh -> self_consumption_pct = 75%
        self.assertAlmostEqual(res_real["candidates"][0]["self_consumption_pct"], 75.0, places=2)

        # 4. Irregular cadence baseline CSV is rejected instead of falling back to 60 minutes
        gap_csv = self.root / "Results" / "bess_gap.csv"
        gap_csv.write_text(
            "PVSYST 8.1.7\ndate;E_Grid\n;kW\n"
            "01/06/24 08:00;10\n01/06/24 08:15;10\n01/06/24 08:45;10\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "regular cadence"):
            optimize_bess_sizing(
                workspace=self.root,
                baseline_csv=gap_csv.name,
                installed_dc_kw=100.0,
                bess_kwh_values=[0.0],
                pcs_kw_values=[50.0],
                tariff={"mode": "feed_in", "feed_in_tariff": 0.4},
                financial={"fixed_capex": 1000.0},
            )

        # 5. Incompatible unit on E_Grid must raise ValueError instead of valuing PV at 0 kW
        bad_unit_csv = self.root / "Results" / "bess_bad_unit.csv"
        bad_unit_csv.write_text(
            "PVSYST 8.1.7\ndate;E_Grid\n;%\n01/06/24 08:00;10\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "E_Grid.*%"):
            optimize_bess_sizing(
                workspace=self.root,
                baseline_csv=bad_unit_csv.name,
                installed_dc_kw=100.0,
                bess_kwh_values=[0.0],
                pcs_kw_values=[50.0],
                tariff={"mode": "feed_in", "feed_in_tariff": 0.4},
                financial={"fixed_capex": 1000.0},
            )

        # 6. Grid-charged TOU arbitrage BESS must not inflate PV self_consumption_pct above 100%
        small_pv_csv = self.root / "Results" / "small_pv_large_load.csv"
        rows = ["PVSYST 8.1.7", "date;E_Grid;E_User", ";kW;kW"]
        start = datetime(2024, 1, 1, 0, 0)
        for step in range(48):
            dt = start + timedelta(hours=step)
            pv_val = 5.0 if 10 <= dt.hour <= 14 else 0.0
            rows.append(f"{dt.strftime('%d/%m/%y %H:%M')};{pv_val:.1f};80.0")
        small_pv_csv.write_text("\n".join(rows) + "\n", encoding="utf-8")
        res_grid_ch = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=small_pv_csv.name,
            installed_dc_kw=10.0,
            bess_kwh_values=[200.0],
            pcs_kw_values=[50.0],
            strategies=["tou_arbitrage"],
            tariff={"mode": "tou", "import_tariff": 0.7, "feed_in_tariff": 0.3, "tou_schedule": _china_ci_tou()},
            financial={"fixed_capex": 1000.0, "battery_capex_per_kwh": 900.0},
        )
        cand = res_grid_ch["candidates"][0]
        self.assertGreater(cand["battery_discharge_kwh"], 0.0)
        self.assertLessEqual(cand["self_consumption_pct"], 100.0)

    def test_full_year_months_allowed_and_stage2_unique_csv_and_prefix_preflight(self) -> None:
        from pvsyst_auto.api.schemas import BessVerifyConfig, EconomicsFinancialConfig, EconomicsTariffConfig
        from pvsyst_auto.api.simulation import pvsyst_optimize_bess_sizing

        # 1. Explicit full-year months=[1..12] is accepted by BESS TOU dispatch
        csv_path = _write_pv_load_csv(self.root / "Results" / "full_months.csv", days=2)
        full_year_tou = [dict(p, months=list(range(1, 13))) for p in _china_ci_tou()]
        res_full = optimize_bess_sizing(
            workspace=self.root,
            baseline_csv=csv_path.name,
            installed_dc_kw=100.0,
            bess_kwh_values=[100.0],
            pcs_kw_values=[50.0],
            strategies=["tou_arbitrage"],
            tariff={"mode": "tou", "import_tariff": 0.7, "feed_in_tariff": 0.3, "tou_schedule": full_year_tou},
            financial={"fixed_capex": 1000.0, "battery_capex_per_kwh": 900.0},
        )
        self.assertEqual(len(res_full["candidates"]), 1)

        # 2. Explicit variant_prefix detects occupied variant on disk during dry_run=True
        proj_dir = self.root / "Projects"
        proj_dir.mkdir(exist_ok=True)
        (proj_dir / "Demo.VC1").write_text("existing\n", encoding="utf-8")
        fake_cli = MagicMock(workspace=self.root)
        with patch("pvsyst_auto.api.simulation.cli", return_value=fake_cli), \
             patch("pvsyst_auto.api.simulation.variant_store"):
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                pvsyst_optimize_bess_sizing(
                    baseline_csv=csv_path.name, installed_dc_kw=100, bess_kwh_values=[100], pcs_kw_values=[50],
                    tariff=EconomicsTariffConfig(mode="self_consumption_flat", import_tariff=1),
                    financial=EconomicsFinancialConfig(fixed_capex=1000), verify_top_k=1,
                    verify_config=BessVerifyConfig(project="Demo.PRJ", source_variant="VC0",
                        battery_file_name="Cell.BTR", battery_unit_kwh=100, variant_prefix="VC"),
                    dry_run=True,
                )

        # 3. Auto-allocated Stage 2 variant names output CSV with new_var so reruns never collide
        from pvsyst_auto.components.store import ComponentStore
        from pvsyst_auto.project.variants import VariantStore
        (proj_dir / "Demo2.PRJ").write_text("PVObject_=pvProject\n", encoding="utf-8")
        (proj_dir / "Demo2.VC0").write_text("PVObject_=pvVariant\n", encoding="utf-8")
        (proj_dir / "Demo2.VC1").write_text("PVObject_=pvVariant\n", encoding="utf-8")
        recorded_csvs: list[str] = []

        def fake_run(project, variant, *, out_csv, **kwargs):
            recorded_csvs.append(Path(out_csv).name)
            _write_pv_load_csv(Path(out_csv), days=2)
            return {}

        fake_cli2 = MagicMock(workspace=self.root)
        fake_cli2.run_simulation.side_effect = fake_run
        with patch("pvsyst_auto.api.simulation.cli", return_value=fake_cli2), \
             patch("pvsyst_auto.api.simulation.variant_store", return_value=VariantStore(self.root, ComponentStore(self.root))), \
             patch("pvsyst_auto.energy.system.create_grid_storage_system", return_value={"status": "created"}):
            res2 = pvsyst_optimize_bess_sizing(
                baseline_csv=csv_path.name, installed_dc_kw=100, bess_kwh_values=[100], pcs_kw_values=[50],
                tariff=EconomicsTariffConfig(mode="self_consumption_flat", import_tariff=1),
                financial=EconomicsFinancialConfig(fixed_capex=1000), verify_top_k=1,
                verify_config=BessVerifyConfig(project="Demo2.PRJ", source_variant="VC0",
                    battery_file_name="Cell.BTR", battery_unit_kwh=100),
                dry_run=False, confirm=True,
            )
        self.assertEqual(res2["stage2_verification"]["verified_comparisons"][0]["variant"], "VC2")
        self.assertEqual(recorded_csvs, ["VC2_100kWh_50kW.csv"])


if __name__ == "__main__":
    unittest.main()

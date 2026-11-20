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
from pvsyst_auto.workflows.report import generate_report


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
    # 12 representative days (one per month) scaled or N days across months
    for month in range(1, 13):
        n_d = max(1, days // 12)
        for d in range(1, n_d + 1):
            start = datetime(2024, month, d, 0, 0)
            for h in range(24):
                dt = start + timedelta(hours=h)
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

    def test_stage1_hybrid_bess_grid_pareto_heatmap_and_report(self) -> None:
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

        # Feed heatmap + Pareto straight into interactive Plotly report!
        rep = generate_report(
            workspace=self.root,
            title="BESS Optimization Report",
            output_name="bess_opt.html",
            scenarios=[{"label": "Base PV", "csv_name": csv_path.name, "installed_dc_kw": 120.0}],
            financial=financial,
            tariff=tariff,
            bess_optimization=res,
            language="en",
            plotlyjs="cdn",
            dry_run=False,
        )
        self.assertEqual(rep["status"], "generated")
        html_txt = Path(rep["report_path"]).read_text(encoding="utf-8")
        self.assertIn("PV+BESS Sizing Heatmap", html_txt)

    def test_stage2_dry_run_guard_and_mocked_cli_reconciliation(self) -> None:
        csv_path = _write_pv_load_csv(self.root / "Results" / "base_s2.csv", days=24)
        tariff = {"mode": "tou", "import_tariff": 0.7, "feed_in_tariff": 0.3, "tou_schedule": _china_ci_tou()}
        financial = {"project_life_years": 15, "discount_rate_pct": 6.0, "capex_per_kw": 3500.0, "battery_capex_per_kwh": 900.0}
        v_cfg = {
            "project": "Demo.PRJ",
            "source_variant": "VC0",
            "battery_file_name": "LFP_100kWh.BTR",
            "battery_unit_kwh": 100.0,
            "variant_prefix": "VB",
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

        with patch("pvsyst_auto.energy.system.create_grid_storage_system") as mock_create:
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


if __name__ == "__main__":
    unittest.main()

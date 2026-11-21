"""Unit & offline E2E tests for one-shot feasibility study orchestrator (pvsyst_run_feasibility_study)."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.native.cli import PVsystCLI
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.workflows.feasibility import run_feasibility_study
from tests.unit.workflows.test_bess_optimize import _china_ci_tou, _write_pv_load_csv


class FeasibilityStudyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        for sub in ("Projects", "Models", "Results", "UserHourly", "ComposPV/PVmodules", "ComposPV/Inverters", "ComposPV/Batteries"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.fake_cli_exe = Path(self._tmp.name) / "PVsystCLI.exe"
        self.fake_cli_exe.write_bytes(b"stub")
        self.cli = PVsystCLI(self.fake_cli_exe, self.root)
        self.comp = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.comp)
        self.base_csv = _write_pv_load_csv(self.root / "Results" / "suzhou_base.csv", days=36)

    def _sample_spec(self) -> dict:
        return {
            "study_name": "Suzhou_120kW_CI",
            "existing_csv": self.base_csv.name,
            "installed_dc_kw": 120.0,
            "synthesize_components": [
                {
                    "component_type": "BTR",
                    "new_name": "Study_LFP200.BTR",
                    "manufacturer": "CellPower",
                    "model": "LFP-51V200Ah",
                    "params": {
                        "chemistry": "LFP",
                        "capacity_ah": 200.0,
                        "nominal_voltage_v": 51.2,
                        "n_cells_s": 16,
                    },
                }
            ],
            "auto_tou_schedule": True,
            "bess_sizing": {
                "bess_kwh_values": [0.0, 100.0, 200.0],
                "pcs_kw_values": [50.0, 100.0],
                "strategies": ["tou_arbitrage", "peak_shaving"],
                "rank_by": "npv",
                "verify_top_k": 0,
            },
            "tariff": {
                "mode": "tou",
                "import_tariff": 0.70,
                "feed_in_tariff": 0.35,
                "tou_schedule": _china_ci_tou(),
                "demand_charge": {
                    "billing_mode": "demand",
                    "demand_rate_per_kw_month": 42.0,
                },
            },
            "financial": {
                "currency": "CNY",
                "project_life_years": 20,
                "discount_rate_pct": 6.0,
                "capex_per_kw": 3500.0,
                "battery_capex_per_kwh": 950.0,
                "opex_pct_capex": 1.0,
                "battery_replacement_year": 12,
                "battery_replacement_cost_ratio": 0.6,
            },
            "sensitivity": {"enabled": True, "variation_pct": 15.0},
            "report": {
                "title": "苏州 120kW 工商业光储一站式可研报告",
                "output_name": "suzhou_feasibility.html",
                "language": "zh",
                "plotlyjs": "cdn",
            },
        }

    def test_dry_run_writes_nothing_and_confirm_guard(self) -> None:
        spec = self._sample_spec()
        prev = run_feasibility_study(self.cli, self.store, spec=spec, dry_run=True)
        self.assertEqual(prev["status"], "ready")
        self.assertTrue(prev["dry_run"])
        self.assertFalse((self.root / "ComposPV" / "Batteries" / "Study_LFP200.BTR").exists())
        self.assertFalse((self.root / "Results" / "suzhou_feasibility.html").exists())
        self.assertEqual(prev["tou_schedule"]["planned_cycles_count"], 2)
        self.assertIn("recommended_bess", prev["executive_summary"])

        # dry_run=False without confirm=True must fail closed
        with self.assertRaisesRegex(ValueError, "confirm=True"):
            run_feasibility_study(self.cli, self.store, spec=spec, dry_run=False, confirm=False)

    def test_full_end_to_end_execution_and_mcp_tool_roundtrip(self) -> None:
        spec = self._sample_spec()
        res = run_feasibility_study(self.cli, self.store, spec=spec, dry_run=False, confirm=True)
        self.assertIn(res["status"], ("completed", "needs_review"))
        self.assertTrue((self.root / "ComposPV" / "Batteries" / "Study_LFP200.BTR").is_file())
        rep_path = Path(res["report"]["report_path"])
        self.assertTrue(rep_path.is_file())
        self.assertEqual(res["report"]["chart_count"], 7)
        html_txt = rep_path.read_text(encoding="utf-8")
        self.assertIn("苏州 120kW 工商业光储一站式可研报告", html_txt)
        self.assertIn("两部制月度最大需量削峰对比", html_txt)
        self.assertIn("财务敏感性分析龙卷风图", html_txt)
        self.assertIn("光储容量寻优收益热力图", html_txt)
        self.assertIsNotNone(res["executive_summary"]["npv"])
        self.assertIsNotNone(res["executive_summary"]["recommended_bess"])

        # Verify MCP tool boundary accepts strict FeasibilityStudyRequest
        import pvsyst_auto.api.engineering as api_eng
        from pvsyst_auto.api.schemas import FeasibilityStudyRequest

        spec2 = self._sample_spec()
        spec2["report"]["output_name"] = "suzhou_feasibility_mcp.html"
        with patch.dict(os.environ, {"PVSYST_WORKSPACE": str(self.root), "PVSYST_CLI": str(self.fake_cli_exe)}):
            mcp_res = api_eng.pvsyst_run_feasibility_study(
                spec=FeasibilityStudyRequest.model_validate(spec2),
                dry_run=True,
            )
        self.assertEqual(mcp_res["status"], "ready")

    def test_blocked_diagnostics_and_explicit_none_config_fields(self) -> None:
        # Irregular CSV -> diagnostics blocked -> study status must be 'blocked', not 'completed'
        gap_csv = self.root / "Results" / "gap_study.csv"
        gap_csv.write_text(
            "PVSYST 8.1.7\ndate;GlobInc;EArray;E_Grid;PR\n;W/m2;kW;kW;ratio\n"
            "01/06/24 08:00;500;10;9.5;0.84\n01/06/24 08:15;500;10;9.5;0.84\n01/06/24 08:45;500;10;9.5;0.84\n",
            encoding="utf-8",
        )
        blocked_res = run_feasibility_study(
            self.cli,
            self.store,
            spec={"study_name": "BlockedStudy", "existing_csv": gap_csv.name, "installed_dc_kw": 100.0},
            dry_run=False,
            confirm=True,
        )
        self.assertEqual(blocked_res["status"], "blocked")
        diag_stage = next(s for s in blocked_res["stages"] if s["stage"] == "diagnostics")
        self.assertEqual(diag_stage["status"], "blocked")

        # Explicit None optional fields in bess_sizing / report must not raise TypeError
        spec_none = self._sample_spec()
        spec_none["bess_sizing"]["verify_top_k"] = None
        spec_none["bess_sizing"]["pcs_capex_per_kw"] = None
        spec_none["bess_sizing"]["rank_by"] = None
        spec_none["bess_sizing"]["bess_params"] = None
        spec_none["report"] = None
        prev_none = run_feasibility_study(self.cli, self.store, spec=spec_none, dry_run=True)
        self.assertEqual(prev_none["status"], "ready")

    def test_design_spec_reads_nested_simulate_design_artifacts_and_stops_on_weather(self) -> None:
        base_spec = self._sample_spec()
        base_spec.pop("existing_csv")
        base_spec.pop("installed_dc_kw")
        base_spec["design"] = {
            "scope": "fixed_unshaded_grid",
            "new_project": "Plant.PRJ",
            "target_dc_kw": 100.0,
            "year": 2024,
        }
        base_spec["report"]["output_name"] = "design_chain.html"

        # 1. ready_for_weather stops early instead of continuing into empty downstream stages
        with patch("pvsyst_auto.workflows.feasibility.simulate_design", return_value={
            "status": "ready_for_weather", "issues": [{"field": "weather", "message": "prepare weather first"}],
        }):
            wait_res = run_feasibility_study(self.cli, self.store, spec=base_spec, dry_run=True)
        self.assertEqual(wait_res["status"], "ready_for_weather")
        self.assertEqual([s["stage"] for s in wait_res["stages"]], ["components", "simulation"])

        # 2. Real simulate_design nests csv path under artifacts.simulate.csv and actual DC under metrics
        from tests._fixtures import build_grid_vc0
        (self.root / "Projects" / "Plant.PRJ").write_text(
            "PVObject_=pvProject\n  MeteoFileName=Met.MET\nEnd of PVObject pvProject\n", encoding="utf-8",
        )
        (self.root / "Projects" / "Plant.VC1").write_text(
            build_grid_vc0(
                orientations=[{"id": 1, "tilt": 25.0, "azim": 0.0}],
                subarrays=[{"id": 1, "orient": 1, "name": "PV", "n_mod": 12, "n_str": 2}],
            ),
            encoding="utf-8",
        )
        nested_sim = {
            "status": "completed",
            "selected": {"template": {"project": "Seed.PRJ", "variant": "VC1"}},
            "artifacts": {"simulate": {"csv": str(self.base_csv)}},
            "metrics": {"nominal_dc_power_kw": 118.8},
            "preview": {"simulation": {"nominal_dc_power_kw": 118.8}},
        }
        with patch("pvsyst_auto.workflows.feasibility.simulate_design", return_value=nested_sim):
            res = run_feasibility_study(self.cli, self.store, spec=base_spec, dry_run=False, confirm=True)
        self.assertIn(res["status"], ("completed", "needs_review"))
        self.assertEqual(res["executive_summary"]["installed_dc_kw"], 118.8)
        self.assertIsNotNone(res["diagnostics"])
        self.assertIsNotNone(res["bess_optimization"])
        self.assertIsNotNone(res["economics"])
        self.assertTrue(Path(res["report"]["report_path"]).is_file())


if __name__ == "__main__":
    unittest.main()

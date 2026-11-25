"""Unit tests for interactive Plotly HTML engineering report generator (zh/en, single & multi-scenario)."""
from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.workflows.report import generate_report
from tests._fixtures import write_sfi_csv


@lru_cache(maxsize=8)
def _report_csv_text(scale: float, pr: float, with_load_bess: bool) -> str:
    cols = [
        ("GlobInc", "W/m2"), ("EArray", "kW"), ("E_Grid", "kW"),
        ("PR", "ratio"), ("IL_Pmax", "kW"), ("ShdElec", "kW"), ("EGrdLim", "kW"),
    ]
    if with_load_bess:
        cols.extend([("E_User", "kW"), ("EFrGrid", "kW"), ("EBatCh", "kW"), ("EBatDis", "kW")])
    lines = [
        "Project;ReportTest.PRJ",
        "Geographical Site;Site.SIT",
        "Weather data;Met.MET",
        "Simulation variant;ReportTest.VC0",
        "date;" + ";".join(c for c, _ in cols),
        ";" + ";".join(u for _, u in cols),
    ]
    # Contiguous full-year hourly timestamps so all 12 months have regular cadence.
    start = datetime(2024, 1, 1, 0, 0)
    for step in range(366 * 24):
        dt = start + timedelta(hours=step)
        h = dt.hour
        gi = 750.0 if 7 <= h < 17 else 0.0
        ea = 100.0 * scale * (gi / 1000.0) * pr if gi > 0 else 0.0
        clip = 1.5 * scale if 11 <= h <= 13 else 0.0
        eg = max(0.0, ea - clip) * 0.98
        row = [
            dt.strftime("%d/%m/%y %H:%M"),
            f"{gi:.2f}", f"{ea:.3f}", f"{eg:.3f}",
            f"{pr:.3f}" if gi > 0 else "",
            f"{clip:.3f}", "0.0", "0.0",
        ]
        if with_load_bess:
            load = 40.0 if 8 <= h < 20 else 15.0
            ch = 15.0 if 11 <= h < 15 else 0.0
            dis = 20.0 if 18 <= h < 20 else 0.0
            imp = max(0.0, load - eg - dis + ch)
            row.extend([f"{load:.2f}", f"{imp:.2f}", f"{ch:.2f}", f"{dis:.2f}"])
        lines.append(";".join(row))
    return "\n".join(lines) + "\n"


def _write_report_csv(path: Path, *, scale: float = 1.0, pr: float = 0.84, with_load_bess: bool = False) -> Path:
    path.write_text(_report_csv_text(scale, pr, with_load_bess), encoding="utf-8")
    return path


class InteractiveReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Results").mkdir(parents=True, exist_ok=True)
        (self.root / "Projects").mkdir(parents=True, exist_ok=True)
        (self.root / "ComposPV" / "PVmodules").mkdir(parents=True, exist_ok=True)
        (self.root / "ComposPV" / "Inverters").mkdir(parents=True, exist_ok=True)
        (self.root / "UserBatch").mkdir(parents=True, exist_ok=True)

    def test_single_and_multi_scenario_plotly_html_reports_zh_and_en(self):
        csv_pv = _write_report_csv(self.root / "Results" / "pv_base.csv", scale=1.0, pr=0.84, with_load_bess=True)
        csv_bifi = _write_report_csv(self.root / "Results" / "pv_bifi.csv", scale=1.12, pr=0.86, with_load_bess=True)

        tou = {
            "mode": "tou",
            "feed_in_tariff": 0.35,
            "import_tariff": 0.75,
            "tou_schedule": [
                {"name": "valley", "hours": [0, 1, 2, 3, 4, 5, 6, 7], "import_rate": 0.30, "export_rate": 0.25},
                {"name": "peak", "hours": [18, 19, 20, 21], "import_rate": 1.35, "export_rate": 0.45},
                {"name": "flat", "hours": [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 22, 23], "import_rate": 0.75, "export_rate": 0.35},
            ],
        }
        fin = {"currency": "CNY", "capex_per_kw": 3500.0, "battery_capex_per_kwh": 900.0, "opex_pct_capex": 1.2}

        # Dry run writes nothing
        preview = generate_report(
            workspace=self.root,
            title="苏州 100kW 光储对比方案",
            output_name="suzhou_compare.html",
            scenarios=[
                {"label": "单面基线", "csv_name": csv_pv.name, "installed_dc_kw": 100.0, "battery_kwh": 100.0},
                {"label": "双面优化", "csv_name": csv_bifi.name, "installed_dc_kw": 100.0, "battery_kwh": 100.0},
            ],
            financial=fin,
            tariff=tou,
            language="zh",
            plotlyjs="cdn",
            dry_run=True,
        )
        self.assertEqual(preview["status"], "ready")
        self.assertFalse((self.root / "Results" / "suzhou_compare.html").exists())

        # Execute zh multi-scenario report (4 Plotly charts: monthly, waterfall, typical 24h TOU, cashflow)
        done = generate_report(
            workspace=self.root,
            title="苏州 100kW 光储对比方案",
            output_name="suzhou_compare.html",
            scenarios=[
                {"label": "单面基线", "csv_name": csv_pv.name, "installed_dc_kw": 100.0, "battery_kwh": 100.0},
                {"label": "双面优化", "csv_name": csv_bifi.name, "installed_dc_kw": 100.0, "battery_kwh": 100.0},
            ],
            financial=fin,
            tariff=tou,
            language="zh",
            plotlyjs="cdn",
            dry_run=False,
        )
        self.assertEqual(done["status"], "generated")
        self.assertEqual(done["chart_count"], 4)
        self.assertEqual(done["best_scenario"], "双面优化")
        html_text = Path(done["report_path"]).read_text(encoding="utf-8")
        self.assertIn("plotly", html_text.lower())
        self.assertIn("苏州 100kW 光储对比方案", html_text)
        self.assertIn("月度发电量与性能比", html_text)
        self.assertIn("光伏系统能量损失瀑布图", html_text)

        # Overwrite guard refuses unless overwrite=True
        with self.assertRaises(FileExistsError):
            generate_report(
                workspace=self.root, title="Dup", output_name="suzhou_compare.html",
                scenarios=[{"label": "A", "csv_name": csv_pv.name}],
            )

        # MCP tool roundtrip in English
        import pvsyst_auto.api.engineering as api_eng
        from pvsyst_auto.api.schemas import ReportScenarioInput

        fake_cli = self.root / "PVsystCLI.exe"
        fake_cli.write_bytes(b"stub")
        with patch.dict(os.environ, {"PVSYST_WORKSPACE": str(self.root), "PVSYST_CLI": str(fake_cli)}):
            en_res = api_eng.pvsyst_generate_report(
                title="English Single Report",
                output_name="en_report.html",
                scenarios=[ReportScenarioInput(label="Base", csv_name=csv_pv.name, installed_dc_kw=100.0)],
                language="en",
                plotlyjs="cdn",
            )
        self.assertEqual(en_res["status"], "generated")
        self.assertEqual(en_res["chart_count"], 2)
        en_html = Path(en_res["report_path"]).read_text(encoding="utf-8")
        self.assertIn("Monthly Generation &amp; Performance Ratio", en_html)

    def test_report_energy_across_month_boundary_with_explicit_export(self):
        from pvsyst_auto.workflows.report import _summarize_csv_for_report
        csv = self.root / "Results" / "boundary.csv"
        csv.write_text("date;EArray;E_Grid;EToGrid\n;Wh;Wh;Wh\n"
                       "31/01/24 23:00;50;40;4\n01/02/24 00:00;50;40;4\n", encoding="utf-8")
        with patch("pvsyst_auto.workflows.report._plotly_div", return_value="") as charts:
            generate_report(workspace=self.root, title="Energy", output_name="energy.html",
                            scenarios=[{"csv_name": csv.name}])
        waterfall, = (call.args[0].data[0] for call in charts.call_args_list
                      if call.args[0].data[0].type == "waterfall")
        with self.subTest(metric="waterfall export"):
            self.assertEqual(waterfall.y[-1], 0.01)
        with self.subTest(metric="annual yield"):
            self.assertEqual(_summarize_csv_for_report(csv)["annual_e_grid_kwh"], 0.01)

    def test_report_with_demand_charge_sensitivity_and_bess_heatmap(self) -> None:
        csv_pv = self.root / "Results" / "ci_bess.csv"
        _write_report_csv(csv_pv, scale=1.0, with_load_bess=True)
        tariff_dc = {
            "mode": "self_consumption_flat",
            "import_tariff": 0.9,
            "feed_in_tariff": 0.35,
            "demand_charge": {"billing_mode": "demand", "demand_rate_per_kw_month": 40.0},
        }
        fin = {
            "currency": "CNY",
            "project_life_years": 20,
            "discount_rate_pct": 6.0,
            "capex_per_kw": 3500.0,
            "battery_capex_per_kwh": 1000.0,
            "battery_replacement_year": 12,
            "battery_replacement_cost_ratio": 0.6,
        }
        bess_opt = {
            "heatmap": {
                "metric": "npv",
                "bess_kwh": [100, 200],
                "pcs_kw": [50, 100],
                "z": [[120000, 150000], [130000, 165000]],
            },
            "pareto_frontier": [
                {"bess_kwh": 100, "pcs_kw": 50, "capex_total": 450000, "npv": 120000, "irr_pct": 14.5},
                {"bess_kwh": 200, "pcs_kw": 100, "capex_total": 550000, "npv": 165000, "irr_pct": 15.8},
            ],
        }
        res = generate_report(
            workspace=self.root,
            title="工商业两部制光储可研",
            output_name="ci_bess_report.html",
            scenarios=[{"label": "光储方案", "csv_name": csv_pv.name, "installed_dc_kw": 100.0, "battery_kwh": 200.0}],
            financial=fin,
            tariff=tariff_dc,
            sensitivity={"enabled": True, "variation_pct": 15.0},
            bess_optimization=bess_opt,
            language="zh",
            plotlyjs="cdn",
            dry_run=False,
        )
        self.assertEqual(res["status"], "generated")
        # 4 base charts + demand peak + sensitivity tornado + bess heatmap = 7 charts
        self.assertEqual(res["chart_count"], 7)
        body = Path(res["report_path"]).read_text(encoding="utf-8")
        self.assertIn("两部制月度最大需量削峰对比", body)
        self.assertIn("财务敏感性分析龙卷风图", body)
        self.assertIn("光储容量寻优收益热力图", body)

    def test_report_rejects_missing_grid_energy(self):
        csv_path = self.root / "Results" / "no_grid.csv"
        csv_path.write_text(
            "PVSYST 8.1.7\ndate;GlobInc;EArray;PR\n;W/m2;kW;ratio\n"
            "01/06/24 08:00;500;10;0.84\n01/06/24 09:00;500;10;0.84\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "E_Grid or EToGrid"):
            generate_report(workspace=self.root, title="Missing grid", output_name="missing.html",
                            scenarios=[{"label": "Base", "csv_name": csv_path.name}])

    def test_report_rejects_incompatible_units_and_tornado_uses_actual_npv(self):
        bad_unit = self.root / "Results" / "bad_unit.csv"
        bad_unit.write_text("date;E_Grid\n;%\n01/06/24 08:00;10\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "E_Grid.*%"):
            generate_report(workspace=self.root, title="Bad Unit", output_name="bad_unit.html",
                            scenarios=[{"label": "Base", "csv_name": bad_unit.name}])

        csv_pv = self.root / "Results" / "tor_pv.csv"
        _write_report_csv(csv_pv, scale=1.0)
        with patch("pvsyst_auto.workflows.report._plotly_div", return_value="") as charts:
            generate_report(
                workspace=self.root,
                title="Tornado Check",
                output_name="tor.html",
                scenarios=[{"label": "Base", "csv_name": csv_pv.name, "installed_dc_kw": 100.0}],
                financial={"currency": "CNY", "capex_per_kw": 3500.0, "project_life_years": 20},
                tariff={"mode": "feed_in", "feed_in_tariff": 0.4},
                sensitivity={"enabled": True, "variation_pct": 15.0},
                dry_run=False,
            )
        tor_figs = [c.args[0] for c in charts.call_args_list if any(getattr(t, "orientation", None) == "h" for t in c.args[0].data)]
        self.assertEqual(len(tor_figs), 1)
        for trace in tor_figs[0].data:
            self.assertIn("%{customdata", trace.hovertemplate)
            self.assertNotIn("%{x", trace.hovertemplate)
            self.assertEqual(len(trace.customdata), len(trace.x))
            # customdata holds actual post-shock NPV (= base + delta), not the delta itself
            self.assertAlmostEqual(trace.customdata[0], trace.base + trace.x[0], places=1)

    def test_report_rejects_irregular_timestamp_cadence(self):
        gap_csv = self.root / "Results" / "gap_report.csv"
        gap_csv.write_text(
            "PVSYST 8.1.7\n"
            "date;GlobInc;EArray;E_Grid;PR\n"
            ";W/m2;kW;kW;ratio\n"
            "01/06/24 08:00;500;10;9.5;0.84\n"
            "01/06/24 08:15;500;10;9.5;0.84\n"
            "01/06/24 08:45;500;10;9.5;0.84\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "regular cadence"):
            generate_report(
                workspace=self.root,
                title="Gap",
                output_name="gap_report.html",
                scenarios=[{"label": "Gap", "csv_name": "gap_report.csv"}],
            )

    def test_best_scenario_matches_energy_ranking_without_economics(self):
        _write_report_csv(self.root / "Results" / "small.csv", scale=1.0)
        _write_report_csv(self.root / "Results" / "large.csv", scale=2.0)
        result = generate_report(
            workspace=self.root, title="Ranking", output_name="ranking.html", language="en",
            scenarios=[{"label": "Small", "csv_name": "small.csv"},
                       {"label": "Large", "csv_name": "large.csv"}],
        )
        report = (self.root / "Results" / "ranking.html").read_text(encoding="utf-8")
        self.assertIn("<tr><td>#1</td><td><strong>Large</strong>", report)
        self.assertEqual(result["best_scenario"], "Large")
        # Multi-scenario report promotes best_scenario to primary and labels it in header & single-scenario charts
        self.assertIn("Primary scenario: Large", report)
        self.assertIn("PV Energy Loss Waterfall (Large)", report)

    def test_report_self_consumption_yield_aligns_with_economics(self):
        # Self-consumption CSV where net E_Grid = EToGrid - EFrGrid is negative overall,
        # while utilized PV energy = EToGrid + (E_User - EFrGrid) = 20 + 60 = 80 kWh.
        sc_csv = self.root / "Results" / "self_cons.csv"
        sc_csv.write_text(
            "PVSYST 8.1.7\n"
            "date;GlobInc;EArray;E_Grid;EToGrid;EFrGrid;E_User;PR\n"
            ";W/m2;kWh;kWh;kWh;kWh;kWh;ratio\n"
            "01/06/24 12:00;800;85;20;20;0;60;0.84\n"
            "01/06/24 13:00;0;0;-100;0;100;100;\n",
            encoding="utf-8",
        )
        res = generate_report(
            workspace=self.root,
            title="Self Consumption Alignment",
            output_name="self_cons.html",
            language="en",
            scenarios=[{"label": "SC", "csv_name": sc_csv.name}],
            tariff={"mode": "self_consumption_flat", "import_tariff": 0.8, "feed_in_tariff": 0.3},
            financial={"fixed_capex": 1000.0},
        )
        html_text = Path(res["report_path"]).read_text(encoding="utf-8")
        # KPI yield and ranking table yield both show 80.0 kWh (not -80.0 from net E_Grid)
        self.assertIn("80.0 kWh", html_text)
        self.assertIn("<td>80.0</td>", html_text)

    def test_report_rejects_duplicate_labels_and_passes_resolved_csv_path(self):
        csv_a = _write_report_csv(self.root / "Results" / "dup_a.csv", scale=1.0)
        csv_b = _write_report_csv(self.root / "Results" / "dup_b.csv", scale=2.0)
        with self.assertRaisesRegex(ValueError, "Duplicate scenario label"):
            generate_report(
                workspace=self.root, title="Dup", output_name="dup.html",
                scenarios=[{"label": "Base", "csv_name": csv_a.name},
                           {"label": " Base ", "csv_name": csv_b.name}],
            )
        # Direct workspace=None report with economics uses resolved csv_path instead of bare filename
        res = generate_report(
            workspace=None, title="Standalone", output_name="standalone_report.html",
            scenarios=[{"label": "Direct", "csv_name": str(csv_a)}],
            tariff={"mode": "feed_in", "feed_in_tariff": 0.4},
            financial={"fixed_capex": 10000.0},
            dry_run=True,
        )
        self.assertEqual(res["status"], "ready")
        self.assertTrue(res["has_economics"])

    def test_report_accepts_batch_selected_outputs(self):
        write_sfi_csv(self.root / "Results" / "base.csv", [("E_Grid", "kWh")],
                      [["01/06/24 12:00", 3], ["01/06/24 13:00", 4]])
        for metric in ("E_Grid", "EArray"):
            with self.subTest(metric=metric):
                (self.root / "UserBatch" / "tilt.csv").write_text(
                    f"Ident;Create hourly;Plane tilt;Error;{metric}\n;;[deg];;[MWh]\n"
                    "SIM_1;;15;;180.5\nSIM_2;;20;;184.2\n", encoding="utf-8")
                result = generate_report(workspace=self.root, title="Tilt", output_name=f"{metric}.html",
                                         scenarios=[{"csv_name": "base.csv"}], batch_summary_csv="tilt.csv")
                self.assertTrue(result["has_batch_sweep"])
                self.assertIn(f"{metric} (MWh)", Path(result["report_path"]).read_text(encoding="utf-8"))

    def test_three_dimensional_sweep_preserves_groups_and_pr_units(self):
        from pvsyst_auto.workflows.report import _build_batch_sweep_figure
        rows = [{"parameters": {"tilt": t, "pitch": p, "count": c}, "values": {"E_Grid": 10, "PR": 1.2}}
                for t in (10, 20) for p in (3, 4) for c in (1, 2)]
        fig = _build_batch_sweep_figure({"units": {"PR": "%"}, "scenarios": rows}, palette=["blue"], opt_label="Best")
        curves = [trace for trace in fig.data if trace.name.endswith("PR (%)")]
        self.assertEqual(len(curves), 4)
        self.assertTrue(all(list(trace.y) == [1.2, 1.2] for trace in curves))

    def test_report_renders_1d_and_2d_batch_sweep_curves(self):
        start = datetime(2024, 6, 1, 0, 0)
        rows = []
        for h in range(72):
            dt = start + timedelta(hours=h)
            day = 8 <= dt.hour < 16
            rows.append([
                dt.strftime("%d/%m/%y %H:%M"),
                600.0 if day else 0.0,
                110.0 if day else 0.0,
                100.0 if day else 0.0,
                0.83 if day else 0.0,
            ])
        write_sfi_csv(
            self.root / "Results" / "base.csv",
            [("GlobInc", "W/m²"), ("EArray", "kW"), ("E_Grid", "kW"), ("PR", "ratio")],
            rows,
        )

        batch_1d = "\n".join([
            "PVsyst Batch simulation results",
            "Ident;Create hourly;Plane tilt;Error;E_Grid;PR",
            ";;[deg];;[MWh];[ratio]",
            "SIM_1;;15;;180.5;0.825",
            "SIM_2;;20;;184.2;0.828",
            "SIM_3;;25;;186.1;0.830",
            "SIM_4;;30;;185.0;0.829",
        ]) + "\n"
        (self.root / "UserBatch" / "TiltSweep_BatchResults.CSV").write_text(batch_1d, encoding="utf-8")

        dry = generate_report(
            workspace=self.root,
            title="Tilt Optimization Report",
            output_name="tilt_report.html",
            scenarios=[{"label": "Base", "csv_name": "base.csv", "installed_dc_kw": 200.0}],
            batch_summary_csv="TiltSweep_BatchResults.CSV",
            language="zh",
            dry_run=True,
        )
        self.assertEqual(dry["status"], "ready")
        self.assertTrue(dry["has_batch_sweep"])

        gen = generate_report(
            workspace=self.root,
            title="Tilt Optimization Report",
            output_name="tilt_report.html",
            scenarios=[{"label": "Base", "csv_name": "base.csv", "installed_dc_kw": 200.0}],
            batch_summary_csv="TiltSweep_BatchResults.CSV",
            language="zh",
            dry_run=False,
        )
        self.assertEqual(gen["status"], "generated")
        self.assertTrue(gen["has_batch_sweep"])
        self.assertGreaterEqual(gen["chart_count"], 3)
        html_text = Path(gen["report_path"]).read_text(encoding="utf-8")
        self.assertIn("参数扫描寻优曲线", html_text)
        self.assertIn("Plane tilt", html_text)

        batch_2d = "\n".join([
            "PVsyst Batch simulation results",
            "Ident;Create hourly;Plane tilt;Sheds pitch;Error;E_Grid;PR",
            ";;[deg];[m];;[MWh];[ratio]",
            "SIM_1;;20;9;;181.0;0.820",
            "SIM_2;;25;9;;182.5;0.822",
            "SIM_3;;20;11;;184.0;0.829",
            "SIM_4;;25;11;;186.2;0.833",
        ]) + "\n"
        (self.root / "UserBatch" / "Grid2D_BatchResults.CSV").write_text(batch_2d, encoding="utf-8")
        gen2 = generate_report(
            workspace=self.root,
            title="2D Sweep Report",
            output_name="grid2d_report.html",
            scenarios=[{"label": "Base", "csv_name": "base.csv", "installed_dc_kw": 200.0}],
            batch_summary_csv="Grid2D_BatchResults.CSV",
            language="en",
            dry_run=False,
        )
        self.assertEqual(gen2["status"], "generated")
        self.assertIn("Parametric Batch Sweep Curves", Path(gen2["report_path"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for interactive Plotly HTML engineering report generator (zh/en, single & multi-scenario)."""
from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.workflows.report import generate_report


def _write_report_csv(path: Path, *, scale: float = 1.0, pr: float = 0.84, with_load_bess: bool = False) -> Path:
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
    # Cover 12 representative days (one per month) so every month has data
    for month in range(1, 13):
        start = datetime(2024, month, 15, 0, 0)
        for h in range(24):
            dt = start + timedelta(hours=h)
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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()

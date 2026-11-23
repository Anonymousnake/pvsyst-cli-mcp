"""Unit tests for P2 UX and reporting enhancements:
- P2-1: pvsyst_build_sourced_component field/unit hints (did_you_mean + allowed_fields + schema docs)
- P2-2: compact design preview by default (verbose=False) with opt-in verbose=True
- P2-3: report_language alignment in simulate_design + batch sweep curve chart in generate_report
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from pvsyst_auto.api.components import pvsyst_build_sourced_component
from pvsyst_auto.api.schemas import SingleDesignRequest
from pvsyst_auto.components.builder import build_sourced_component
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.workflows.design import simulate_design
from pvsyst_auto.workflows.report import generate_report
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


def _write_sfi_csv(path: Path, columns: list[tuple[str, str]], rows: list[list], *,
                   project: str = "P.PRJ", site: str = "Geneva_MN90.SIT",
                   met: str = "Geneva_MN90_SYN.MET", variant: str = "P.VC0") -> Path:
    lines = [
        f"Project;{project}",
        f"Geographical Site;{site}",
        f"Weather data;{met}",
        f"Simulation variant;{variant}",
        "date;" + ";".join(c for c, _ in columns),
        ";" + ";".join(u for _, u in columns),
    ]
    for row in rows:
        lines.append(";".join(str(v) for v in row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class SourcedComponentHintsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)
        for sub in ("ComposPV/PVmodules", "ComposPV/Inverters", "Results"):
            (self.ws / sub).mkdir(parents=True, exist_ok=True)
        self.store = ComponentStore(self.ws)

    def test_unknown_field_returns_did_you_mean_and_allowed_fields(self):
        sources = {
            "ds": {
                "kind": "manufacturer",
                "title": "Test Module Datasheet",
                "locator": "page 1",
                "text": "Isc temperature coefficient: 5.2 mA/°C, Nominal voltage 800 V",
            }
        }
        pan_res = build_sourced_component(
            self.store,
            component_type="PAN",
            new_name="Demo600.PAN",
            manufacturer="TestCo",
            model="M600",
            sources=sources,
            declarations=[{
                "field": "muIsc",
                "value": 5.2,
                "unit": "mA/degC",
                "source_id": "ds",
                "excerpt": "5.2 mA/°C",
                "source_value_text": "5.2",
                "source_unit_text": "mA/°C",
            }],
            dry_run=True,
        )
        self.assertEqual(pan_res["status"], "blocked")
        issue = pan_res["issues"][0]
        self.assertIn("MuISC", issue["did_you_mean"])
        self.assertIn("MuISC", issue["allowed_fields"])
        self.assertIn("MuISC", pan_res["allowed_fields"])

        ond_res = build_sourced_component(
            self.store,
            component_type="OND",
            new_name="Demo300.OND",
            manufacturer="InvCo",
            model="I300",
            sources=sources,
            declarations=[{
                "field": "VNomEff",
                "value": 800.0,
                "unit": "V",
                "source_id": "ds",
                "excerpt": "800 V",
                "source_value_text": "800",
                "source_unit_text": "V",
            }],
            dry_run=True,
        )
        self.assertEqual(ond_res["status"], "blocked")
        self.assertIn("VNomEff1", ond_res["issues"][0]["did_you_mean"])
        self.assertIn("VOutConv", ond_res["allowed_fields"])

    def test_bad_unit_returns_allowed_units_and_tool_doc_lists_canonical_keys(self):
        sources = {
            "ds": {
                "kind": "manufacturer",
                "title": "Datasheet",
                "locator": "p1",
                "text": "Power: 600 kW",
            }
        }
        res = build_sourced_component(
            self.store,
            component_type="PAN",
            new_name="DemoBadUnit.PAN",
            manufacturer="TestCo",
            model="M600",
            sources=sources,
            declarations=[{
                "field": "PNom",
                "value": 600.0,
                "unit": "kW",
                "source_id": "ds",
                "excerpt": "600 kW",
                "source_value_text": "600",
                "source_unit_text": "kW",
            }],
            dry_run=True,
        )
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["issues"][0]["allowed_units"], ["W"])
        doc = pvsyst_build_sourced_component.__doc__ or ""
        for token in ("MuISC", "muVocSpec", "VOutConv", "VNomEff1..3", "CapNomC10"):
            self.assertIn(token, doc)


class CompactPreviewAndReportLanguageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "PVsyst8.1_Data"
        for folder in (
            "ComposPV/PVmodules", "ComposPV/Inverters", "ComposPV/Batteries",
            "ComposPV/Clients", "ComposPV/Gensets", "ComposPV/Pumps",
            "ComposPV/Regulators", "ComposPV/RegulPmp", "ComposPV/Sellers",
            "DataMeas", "Meteo", "Models", "Other", "Projects", "Results",
            "Shadings", "Sites", "Templates", "UserBatch", "UserData",
            "UserHourly", "UserHourlyParams", "UserImages", "UserOptimization",
            "UserRecycleBin",
        ):
            (self.ws / folder).mkdir(parents=True, exist_ok=True)
        (self.ws / "ComposPV/PVmodules/Lab440.PAN").write_bytes(
            b"\xef\xbb\xbf" + UTILITY_PAN.replace("\n", "\r\n").encode("utf-8")
        )
        (self.ws / "ComposPV/Inverters/Lab50.OND").write_bytes(
            b"\xef\xbb\xbf" + UTILITY_OND.replace("\n", "\r\n").encode("utf-8")
        )
        (self.ws / "Sites/Geneva_MN90.SIT").write_bytes(
            b"\xef\xbb\xbf" + SITE_TEXT.replace("\n", "\r\n").encode("utf-8")
        )
        (self.ws / "Meteo/Geneva_MN90_SYN.MET").write_bytes(
            b"\xef\xbb\xbf" + MET_TEXT.replace("\n", "\r\n").encode("utf-8")
        )
        # Seed several unusable _DEMO_*.PRJ files to test template scan folding
        for idx in range(5):
            (self.ws / "Projects" / f"_DEMO_Junk_{idx}.PRJ").write_text("not a valid prj", encoding="utf-8")
        self.store = VariantStore(self.ws, ComponentStore(self.ws))

    def test_compact_preview_folds_rejected_templates_unless_verbose(self):
        calls: list[dict] = []

        class _FakeCLI:
            workspace = self.ws
            def _require(self, *a, **k): return None
            def command_help(self, *a, **k): return "Available language: [en, zh]"
            def version(self, *a, **k): return "8.1.7"
            def license_info(self, *a, **k): return {"status": "ACTIVATED", "remaining_executions": None}
            def _redact(self, s): return s
            def run_simulation(self, project, variant, **kwargs):
                calls.append(kwargs)
                out_csv = Path(kwargs["out_csv"])
                pdf = Path(kwargs["report_pdf"])
                rows = []
                start = datetime(1990, 1, 1, 0, 0)
                for h in range(8760):
                    dt = start + timedelta(hours=h)
                    day = 8 <= dt.hour < 16
                    rows.append([
                        dt.strftime("%d/%m/%y %H:%M"),
                        500.0 if day else 0.0,
                        480.0 if day else 0.0,
                        160.0 if day else 0.0,
                        147.2064 if day else 0.0,
                        0.82 if day else 0.0,
                        0.0,
                    ])
                _write_sfi_csv(
                    out_csv,
                    [("GlobInc", "W/m²"), ("GlobShd", "W/m²"), ("EArray", "kW"), ("E_Grid", "kW"), ("PR", "ratio"), ("IL_Pmax", "kW")],
                    rows,
                    project=project,
                    site="Geneva_MN90.SIT",
                    met="Geneva_MN90_SYN.MET",
                    variant=f"{Path(project).stem}.{variant}",
                )
                pdf.write_bytes(b"%PDF-1.4\n%fake\n")
                return {"returncode": 0, "warnings": [], "args": []}

        req = SingleDesignRequest(
            scope="ground_sheds",
            new_project="CompactLang.PRJ",
            year=1990,
            target_dc_kw=360.0,
            tilt=22.0,
            azimuth=0.0,
            report_language="zh",
            sizing_mode="auto_from_components",
            module={"file_name": "Lab440.PAN"},
            inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
        )
        spec = req.model_dump(exclude_none=True)

        compact = simulate_design(_FakeCLI(), self.store, spec=spec, dry_run=True, verbose=False)
        self.assertEqual(compact["status"], "ready", compact.get("issues"))
        tpl_block = compact["preview"]["discovery"]["candidates"]["templates"]
        self.assertIsInstance(tpl_block, dict)
        self.assertEqual(tpl_block["summary"]["rejected_count"], 5)
        self.assertTrue(tpl_block["summary"]["truncated"])
        self.assertNotIn("selection_rule", compact["preview"]["discovery"])

        full = simulate_design(_FakeCLI(), self.store, spec=spec, dry_run=True, verbose=True)
        self.assertIsInstance(full["preview"]["discovery"]["candidates"]["templates"], list)
        self.assertEqual(len(full["preview"]["discovery"]["candidates"]["templates"]), 5)
        self.assertIn("selection_rule", full["preview"]["discovery"])

        # Execution forwards report_language='zh' to cli.run_simulation
        done = simulate_design(_FakeCLI(), self.store, spec=spec, dry_run=False, verbose=False)
        self.assertIn(done["status"], ("completed", "needs_review"), done)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["report_language"], "zh")


class ReportBatchSweepCurvesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for sub in ("Results", "UserBatch"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        # Create minimal SFI CSV
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
        _write_sfi_csv(
            self.root / "Results" / "base.csv",
            [("GlobInc", "W/m²"), ("EArray", "kW"), ("E_Grid", "kW"), ("PR", "ratio")],
            rows,
        )

    def test_three_dimensional_sweep_preserves_groups_and_pr_units(self):
        from pvsyst_auto.workflows.report import _build_batch_sweep_figure
        rows = [{"parameters": {"tilt": t, "pitch": p, "count": c}, "values": {"E_Grid": 10, "PR": 1.2}}
                for t in (10, 20) for p in (3, 4) for c in (1, 2)]
        fig = _build_batch_sweep_figure({"units": {"PR": "%"}, "scenarios": rows}, palette=["blue"], opt_label="Best")
        curves = [trace for trace in fig.data if trace.name.endswith("PR (%)")]
        self.assertEqual(len(curves), 4)
        self.assertTrue(all(list(trace.y) == [1.2, 1.2] for trace in curves))

    def test_report_renders_1d_and_2d_batch_sweep_curves(self):
        # 1D tilt sweep CSV (PVsyst UserBatch Results format)
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

        # 2D tilt x pitch sweep CSV
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

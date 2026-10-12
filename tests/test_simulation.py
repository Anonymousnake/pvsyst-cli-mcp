"""Bounded template workflow and annual acceptance; no native CLI execution."""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pvsyst_cli import PVsystCLI, PVsystError
from pvsyst_components import ComponentStore
from pvsyst_results import annual_summary
from pvsyst_simulation import simulate_template
from pvsyst_variants import VariantStore
from test_components import PAN, OND
from test_grid import grid_text, ARRAY


class SimulationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for folder in ("Projects", "Results", "Models", "Sites", "Meteo", "ComposPV/PVmodules", "ComposPV/Inverters"):
            (self.root / folder).mkdir(parents=True)
        site = "PVObject_=pvSite\nNomF=place.SIT\nSite=Place\nEnd of PVObject pvSite\n"
        (self.root / "Sites/place.SIT").write_text(site)
        (self.root / "Meteo/weather.MET").write_bytes(b"opaque fixture")
        (self.root / "ComposPV/PVmodules/module.PAN").write_text(
            PAN.replace("Manufacturer=Lab", "PNom=999\n    GRef=1\n    Manufacturer=Lab"))
        (self.root / "ComposPV/Inverters/inverter.OND").write_text(OND)
        project = ("PVObject_=pvProject\nComment=Template\nMeteoFileName=old.MET\n"
                   + site.replace("PVObject_=", "PVObject_SitePrj=") + "End of PVObject pvProject\n")
        weather = (site.replace("PVObject_=", "PVObject_SiteSimul=")
                   + "PVObject_MeteoSimul=pvMeteo\nNomF=old.MET\nSiteM=Place\n"
                   + site.replace("PVObject_=", "PVObject_SiteMet=") + "End of PVObject pvMeteo\n")
        array = ARRAY.replace("SubArrayId=1", "SubArrayId=1\nPVModule=module.PAN\nGInverter=inverter.OND")
        variant = grid_text(arrays=array).replace("Version=8.1.6\n", "Version=8.1.6\nPRCorrTemp=False\n" + weather, 1)
        (self.root / "Projects/Template.PRJ").write_text(project)
        (self.root / "Projects/Template.VC0").write_text(variant)
        (self.root / "Projects/Template.VC1").write_text(variant)
        self.originals = {p.name: p.read_bytes() for p in (self.root / "Projects").iterdir()}
        self.cli = PVsystCLI(sys.executable, self.root)
        self.cli._help_cache["run-simulation"] = "Available language: [en]"
        self.store = VariantStore(self.root, ComponentStore(self.root, lock=self.cli._lock), self.cli._lock)
        for name, value in (("license_info", {"status": "TRIAL", "remaining_executions": 5}),
                            ("version", "PVsystCLI 8.1.6"), ("_require", None)):
            mock = patch.object(self.cli, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.request = dict(template_project="Template.PRJ", variant="VC0", new_project="Work.PRJ",
                            pan_name="module.PAN", ond_name="inverter.OND", site_name="place.SIT",
                            met_name="weather.MET", tilt=35, year=1990)
        self.nominal = 72 * 440 / 1000

    def csv(self, path, year=1990, hours=None, mode="", project="Work.PRJ"):
        begin, end = datetime(year, 1, 1), datetime(year + 1, 1, 1)
        hours = hours if hours is not None else int((end - begin).total_seconds() / 3600)
        text = (f"Project;{project}\nGeographical Site;place.SIT\nWeather data;weather.MET\n"
                f"Simulation variant;{Path(project).stem}.VC0\n"
                "date;E_Grid;EArray;IL_Pmax;GlobInc;PR\n;kW;kW;kW;W/m2;ratio\n")
        rows = []
        for index in range(hours):
            stamp = begin + timedelta(hours=index)
            light = 500 if index % 2 else 0
            grid = self.nominal * light / 1000 * 0.8
            value = f"{grid};{grid + 1 if light else 0};{1 if light else 0};{light};{0.8 if light else ''}"
            if mode == "nonfinite" and index == 1:
                value = "nan;1;0;500;0.8"
            if mode == "gap" and index == 1:
                stamp += timedelta(hours=1)
            if mode == "pr" and index == 1:
                value = f"{grid};{grid};0;500;0.1"
            rows.append(stamp.strftime("%d/%m/%y %H:%M") + ";" + value + "\n")
        path.write_text(text + "".join(rows), encoding="utf-8")

    def native_run(self, *args):
        project = next(a[3:] for a in args if a.startswith("-p:"))
        target = Path(next(a[5:] for a in args if a.startswith("-ocf:")))
        pdf = Path(next(a[5:] for a in args if a.startswith("-rpf:")))
        self.assertIn("-sd:1990.01.01", args)
        self.assertIn("-ed:1990.12.31", args)
        self.assertIn("-ts:hour", args)
        self.csv(target, project=project)
        pdf.write_bytes(b"%PDF-1.4\nfixture")
        return subprocess.CompletedProcess([], 0, "Warning: fixture sizing warning\nSimulation done in 0min 1sec", "Host ID: fixture-secret")

    def test_template_preview_execution_failure_and_reverse_recovery(self):
        preview = simulate_template(self.cli, self.store, **self.request)
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(list((self.root / "Results").iterdir()), [])
        self.assertFalse((self.root / "Projects/Work.PRJ").exists())
        with patch.object(self.cli, "_run", side_effect=self.native_run) as native:
            result = simulate_template(self.cli, self.store, **self.request, dry_run=False)
            native.assert_called_once()
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["template_preserved"])
        self.assertEqual(result["metrics"]["rows"], 8760)
        self.assertAlmostEqual(result["metrics"]["pr_ratio"], 0.8)
        self.assertEqual(result["metrics"]["clipping_loss_kwh"], 4380)
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertIn("Warning: fixture sizing warning", result["warnings"])
        manifest = Path(result["artifacts"]["manifest"])
        self.assertEqual(json.loads(manifest.read_text()), result)
        for step in result["recovery"]:
            method = self.store.restore if step["tool"] == "pvsyst_restore_variant" else self.store.restore_project_sources
            method(**step["arguments"])
        for name, data in self.originals.items():
            self.assertEqual((self.root / "Projects" / name).read_bytes(), data)
            self.assertEqual((self.root / "Projects" / name.replace("Template", "Work")).read_bytes(), data)
        blocked = simulate_template(self.cli, self.store, **self.request, dry_run=False)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(json.loads(manifest.read_text()), result)
        for error in (PVsystError("Native failure"), subprocess.TimeoutExpired("fixture", 1)):
            args = {**self.request, "new_project": "Failed" + type(error).__name__ + ".PRJ"}
            with patch.object(self.cli, "_run", side_effect=error):
                failed = simulate_template(self.cli, self.store, **args, dry_run=False)
            self.assertEqual((failed["status"], failed["stage"]), ("failed", "simulation"))
            self.assertTrue(failed["template_preserved"])
            self.assertTrue(failed["recovery"])
            self.assertEqual(json.loads(Path(failed["artifacts"]["manifest"]).read_text()), failed)
        def suspicious_run(*args):
            response = self.native_run(*args)
            path = Path(next(a[5:] for a in args if a.startswith("-ocf:")))
            path.write_text(path.read_text().replace(";1;500;0.8", ";1000000;500;0.8"))
            return response
        with patch.object(self.cli, "_run", side_effect=suspicious_run):
            review = simulate_template(self.cli, self.store, **{**self.request, "new_project": "Review.PRJ"}, dry_run=False)
        self.assertEqual(review["status"], "needs_review")
        self.assertTrue(review["metrics"]["diagnostic_flags"])
        self.assertTrue(review["template_preserved"])
        missing = simulate_template(self.cli, self.store)
        self.assertEqual({item["field"] for item in missing["issues"]}, set(self.request))
        for changes in ({"year": True}, {"tilt": True}, {"tilt": float("nan")}, {"year": 2069}, {"pan_name": "missing.PAN"},
                        {"site_name": "missing.SIT", "met_name": "missing.MET"}):
            blocked = simulate_template(self.cli, self.store, **{**self.request, "new_project": "Unused.PRJ", **changes})
            self.assertEqual(blocked["status"], "blocked")
            json.dumps(blocked, allow_nan=False)
            self.assertFalse((self.root / "Projects/Unused.PRJ").exists())

    def test_annual_acceptance_calendar_units_coverage_and_nonfinite_values(self):
        path = self.root / "Results/annual.csv"
        for year, hours in ((1990, 8760), (2020, 8784)):
            self.csv(path, year)
            report = annual_summary(path, year, self.nominal)
            self.assertEqual(report["rows"], hours)
            self.assertAlmostEqual(report["pr_ratio"], 0.8)
            self.assertAlmostEqual(report["grid_energy_kwh"], self.nominal * 0.5 * 0.8 * hours / 2)
        for kwargs in ({"hours": 48}, {"mode": "gap"}, {"mode": "nonfinite"}, {"hours": 8761}):
            self.csv(path, **kwargs)
            with self.assertRaises(ValueError):
                annual_summary(path, 1990, self.nominal)
        self.csv(path)
        for year, power in ((2069, self.nominal), (True, self.nominal), (1990, 0), (1990, float("inf")), (1990, self.nominal * 2)):
            with self.assertRaises(ValueError):
                annual_summary(path, year, power)
        text = path.read_text()
        path.write_text(text.replace(";W/m2;ratio", ";unknown;ratio"))
        with self.assertRaises(ValueError):
            annual_summary(path, 1990, self.nominal)

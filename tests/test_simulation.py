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

    def test_capacity_envelope_plan_configuration_and_recovery(self):
        projects = self.root / "Projects"
        original = (projects / "Template.VC0").read_text()
        mppt = ("MPPTNode Start;\nNElements=1\nSubArrayId=1\nSubArrayName=Array A\nChildren=Start\n"
                "StringNode Start;\nNElements=3\nSubArrayId=1\nSubArrayName=Array A\n"
                "StringNode End;\nChildren=End\nMPPTNode End;")
        begin = original.index("    StringNode Start;")
        end = original.index("    StringNode End;", begin) + len("    StringNode End;")
        text = original[:begin] + mppt + original[end:]
        text = text.replace("  FieldType=FixedPlane", "  Flags=$00\n  FieldType=FixedPlane")
        text = text.replace("NInvMPPT=1", "NInvMPPT=2\nFlags=$0400\nVersion=8.1.6\nPowerSharingInverterId=-1")
        text = text.replace("End of PVObject pvVCalcul", "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
        (projects / "Template.VC0").write_text(text)
        module = self.root / "ComposPV/PVmodules/module.PAN"
        module.write_text(module.read_text().replace("  TRef=25", "  VMaxIEC=1000\n  VMaxUL=600\n  TRef=25"))
        inverter = self.root / "ComposPV/Inverters/inverter.OND"
        inverter.write_text(inverter.read_text().replace("  Converter=TConverter", "  NbMPPT=1\n  Converter=TConverter")
                            .replace("    PNomConv=50", "    ModeOper=MPPT\n    CompPMax=Lim\n    CompVMax=Lim\n    PNomConv=50"))
        originals = {p.name: p.read_bytes() for p in projects.iterdir()}
        constraints = dict(target_dc_kw=28.16, max_dc_ac_ratio=1.3, voltage_standard="IEC",
            cold_voc_v=60, hot_vmp_v=36, cold_vmp_v=46, module_max_operating_current_a=12,
            module_max_short_circuit_current_a=14, mppt_max_operating_current_a=50,
            mppt_max_short_circuit_current_a=60, design_min_temperature_c=-10,
            design_min_operating_temperature_c=20, design_max_operating_temperature_c=60,
            design_basis="Synthetic declared envelope for contract regression; not manufacturer certification")
        request = {**self.request, "capacity": constraints}
        preview = simulate_template(self.cli, self.store, **request)
        self.assertEqual(preview["status"], "ready", preview)
        plan = preview["capacity_plan"]
        self.assertEqual((plan["series_modules"], plan["string_count"], plan["inverter_count"]), (16, 4, 1))
        self.assertEqual(plan["checks"]["mppt_operating_current_a"], 48)
        self.assertEqual(list((self.root / "Results").iterdir()), [])
        self.nominal = plan["installed_dc_kw"]
        with patch.object(self.cli, "_run", side_effect=self.native_run):
            result = simulate_template(self.cli, self.store, **request, dry_run=False)
        self.assertEqual(result["status"], "completed", result)
        self.assertAlmostEqual(result["metrics"]["nominal_dc_power_kw"], 28.16)
        self.assertEqual(len(result["capacity_plan"]["steps"]), 3)
        values = self.store.inspect_parameters("Work.PRJ", "VC0")["subarrays"][0]["values"]
        self.assertEqual((values["NModSerie"], values["NStringCh"], values["NInverter"]), ("16", "4", "1"))
        for step in result["recovery"]:
            method = self.store.restore if step["tool"] == "pvsyst_restore_variant" else self.store.restore_project_sources
            method(**step["arguments"])
        for name, data in originals.items():
            self.assertEqual((projects / name).read_bytes(), data)
            self.assertEqual((projects / name.replace("Template", "Work")).read_bytes(), data)
        for capacity in ({}, {**constraints, "target_dc_kw": True}, {**constraints, "cold_voc_v": 1e8},
                         {**constraints, "voltage_standard": "guess"}, {**constraints, "hot_vmp_v": 1000},
                         {**constraints, "mppt_max_operating_current_a": 1}, {**constraints, "unknown": 1},
                         {**constraints, "capacity_tolerance_pct": None}, {**constraints, "target_dc_kw": float("nan")}):
            blocked = simulate_template(self.cli, self.store, **{**request, "new_project": "Blocked.PRJ", "capacity": capacity})
            self.assertEqual(blocked["status"], "blocked", blocked)
            self.assertTrue(blocked["issues"])
            self.assertFalse((projects / "Blocked.PRJ").exists())
        with patch.object(self.cli, "_run", side_effect=PVsystError("Native failure")):
            failed = simulate_template(self.cli, self.store, **{**request, "new_project": "SizedFailure.PRJ"}, dry_run=False)
        self.assertEqual(failed["status"], "failed", failed)
        self.assertTrue(failed["template_preserved"])
        self.assertTrue(failed["recovery"])

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

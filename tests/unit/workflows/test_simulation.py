"""Bounded template workflow, annual acceptance, and terminal manifest tests; no native CLI execution."""
import calendar
import csv
import functools
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pvsyst_auto.workflows.house as house_flow
import pvsyst_auto.workflows.revision as revision_flow
import pvsyst_auto.workflows.simulation as pvsyst_simulation
from pvsyst_auto.components.store import ComponentStore, electrical_inventory
from pvsyst_auto.components.pan import validation_fields as pan_validation_fields
from pvsyst_auto.engineering.sizing import plan_capacity
from pvsyst_auto.native.cli import PVsystCLI, PVsystError
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.results import annual_summary
from pvsyst_auto.weather.preparation import prepare_weather
from pvsyst_auto.workflows.simulation import simulate_template
from tests.unit.core.test_components import OND, PAN
from tests.unit.core.test_grid import ARRAY, grid_text


class Unanticipated(Exception):
    """Any error outside the anticipated failure set of a workflow."""


OVERSIZED_PROJECT_ROW = "Project;" + "x" * (csv.field_size_limit() + 1)
HANDOFF = {"status": "ready", "issues": [], "review_items": [], "missing_items": [], "saved": {}}
FAKE_MET_HEAD = (
    "PVObject_=pvMeteo\r\n  Version=8.1.6\r\n  Flags=$0143\r\n  IsSourceUt=False\r\n"
    "  HourShift=0\r\n  TimeShift=0\r\n  TimeShiftTotalMin=0\r\n  PasAccum=60\r\n"
    "  VarMeteoStepDuration=60\r\n  PVObject_SiteMet=pvSite\r\n    TimeZone=8.0\r\n"
    "    HLeg=True\r\n  End of PVObject pvSite\r\n  End of PVObject pvMeteo\r\n"
).encode("utf-8")


@functools.lru_cache(maxsize=None)
def _annual_csv_rows(year, hours, mode, nominal):
    begin = datetime(year, 1, 1)
    rows = []
    for index in range(hours):
        stamp = begin + timedelta(hours=index)
        light = 500 if index % 2 else 0
        grid = nominal * light / 1000 * 0.8
        il_pmax = 1000000 if (mode == "suspicious" and light) else (1 if light else 0)
        value = f"{grid};{grid + 1 if light else 0};{il_pmax};{light};{0.8 if light else ''}"
        if mode == "nonfinite" and index == 1:
            value = "nan;1;0;500;0.8"
        if mode == "gap" and index == 1:
            stamp += timedelta(hours=1)
        rows.append(stamp.strftime("%d/%m/%y %H:%M") + ";" + value + "\n")
    return "".join(rows)


@functools.lru_cache(maxsize=4)
def _station_weather_text(year):
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerows([["#Weather data hourly data"], ["#Site", "Test site"], ["#Country", "China"],
                      ["#Data Source", "Synthetic fixture"], ["#Time step", "Hour"],
                      ["#Latitude", 30], ["#Longitude", 110], ["#Altitude", 100], ["#Time Zone", 8],
                      ["Year", "Month", "Day", "Hour", "GHI", "Tamb", "DHI"],
                      ["", "", "", "", "W/m2", "deg.C", "W/m2"]])
    start = datetime(year, 1, 1)
    for hour in range(8784 if calendar.isleap(year) else 8760):
        stamp = start + timedelta(hours=hour)
        power = 100 if 6 <= stamp.hour < 18 else 0
        writer.writerow([stamp.year, stamp.month, stamp.day, stamp.hour, power, 20, power / 2])
    return stream.getvalue()


def _native_weather_commands(*args):
    argument = lambda flag: next(v[len(flag) + 2:] for v in args if v.startswith("-" + flag + ":"))
    if args[0] == "create-site":
        with Path(argument("wdf")).open(newline="") as stream:
            rows = list(csv.reader(stream, delimiter=";"))
        series = list(zip(*[[float(value) for value in row] for row in rows[2:]]))
        monthly = [field + "=" + ",".join(map(str, (*values, sum(values)))) + ","
                   for field, values in zip(("GlobalH", "TAmb", "DiffuseH"), series)]
        target = Path(argument("osf"))
        target.write_text(
            "PVObject_=pvSite\nNomF=" + target.name + "\nSite=Test site\nCountry=China\n"
            "Latitude=30\nLongitude=110\nAltitude=100\nTimeZone=8\nHLeg=True\n"
            + "\n".join(monthly) + "\nEnd of PVObject pvSite\n", encoding="utf-8-sig")
        return subprocess.CompletedProcess([], 0, "Site created", "")
    Path(argument("omf")).write_bytes(FAKE_MET_HEAD)
    return subprocess.CompletedProcess(
        [], 0, "The imported weather data are shifted by 25 minutes\nConversion ended successfully...", "")


@functools.lru_cache(maxsize=8)
def _weather_results_rows(source_text, nominal, delta):
    rows = list(csv.reader(source_text.splitlines()))[1:]
    lines = []
    for index, (date, ghi, dhi, temp) in enumerate(rows):
        stamp = datetime.strptime(date, "%Y-%m-%d %H:%M")
        light = 500 if index % 2 else 0
        grid = nominal * light / 1000 * 0.8
        observed = float(ghi) + (delta if index == 100 else 0.0)
        lines.append(f"{stamp.strftime('%d/%m/%y %H:%M')};{grid};{grid + 1 if light else 0};"
                     f"{1 if light else 0};{light};{0.8 if light else ''};{observed};{dhi};{temp}\n")
    return "".join(lines)


def _setup_workspace(test_case, *, meteo_name="old.MET"):
    temp = tempfile.TemporaryDirectory()
    test_case.addCleanup(temp.cleanup)
    root = Path(temp.name)
    for folder in ("Projects", "Results", "Models", "Sites", "Meteo", "ComposPV/PVmodules", "ComposPV/Inverters"):
        (root / folder).mkdir(parents=True)
    site = "PVObject_=pvSite\nNomF=place.SIT\nSite=Place\nEnd of PVObject pvSite\n"
    (root / "Sites/place.SIT").write_text(site)
    (root / "Meteo/weather.MET").write_bytes(b"opaque fixture")
    pan_text = PAN.replace("Manufacturer=Lab", "PNom=999\n    GRef=1\n    Manufacturer=Lab")
    (root / "ComposPV/PVmodules/module.PAN").write_text(pan_text)
    (root / "ComposPV/Inverters/inverter.OND").write_text(OND)
    project = (f"PVObject_=pvProject\nComment=Template\nMeteoFileName={meteo_name}\n"
               + site.replace("PVObject_=", "PVObject_SitePrj=") + "End of PVObject pvProject\n")
    weather = (site.replace("PVObject_=", "PVObject_SiteSimul=")
               + f"PVObject_MeteoSimul=pvMeteo\nNomF={meteo_name}\nSiteM=Place\n"
               + site.replace("PVObject_=", "PVObject_SiteMet=") + "End of PVObject pvMeteo\n")
    array = ARRAY.replace("SubArrayId=1", "SubArrayId=1\nPVModule=module.PAN\nGInverter=inverter.OND")
    variant = grid_text(arrays=array).replace("Version=8.1.6\n", "Version=8.1.6\nPRCorrTemp=False\n" + weather, 1)
    (root / "Projects/Template.PRJ").write_text(project)
    (root / "Projects/Template.VC0").write_text(variant)
    (root / "Projects/Template.VC1").write_text(variant)
    originals = {p.name: p.read_bytes() for p in (root / "Projects").iterdir()}
    cli = PVsystCLI(sys.executable, root)
    cli._help_cache["run-simulation"] = "Available language: [en]"
    store = VariantStore(root, ComponentStore(root, lock=cli._lock), cli._lock)
    for name, value in (("license_info", {"status": "TRIAL", "remaining_executions": 5}),
                        ("version", "PVsystCLI 8.1.6"), ("_require", None)):
        mock = patch.object(cli, name, return_value=value)
        mock.start()
        test_case.addCleanup(mock.stop)
    return root, originals, cli, store


def _native_outputs(project_row=None, minimal=False):
    def run(project, variant, *, out_csv, report_pdf, **options):
        rows = [project_row or f"Project;{project}", "Geographical Site;place.SIT", "Weather data;weather.MET",
                f"Simulation variant;{Path(project).stem}.{variant}",
                "date;E_Grid;EArray;IL_Pmax;GlobInc;PR", ";kW;kW;kW;W/m2;ratio",
                "01/01/90 00:00;0;0;0;0;" if minimal else _annual_csv_rows(1990, 8760, "", 31.68).splitlines()[0]]
        Path(out_csv).write_text("\n".join(rows) + "\n", encoding="utf-8")
        Path(report_pdf).write_bytes(b"%PDF-1.4\nfixture")
        return {"csv": str(out_csv), "pdf": str(report_pdf), "warnings": [], "diagnostics": ""}
    return run


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.root, self.originals, self.cli, self.store = _setup_workspace(self)
        self.request = dict(template_project="Template.PRJ", variant="VC0", new_project="Work.PRJ",
                            pan_name="module.PAN", ond_name="inverter.OND", site_name="place.SIT",
                            met_name="weather.MET", tilt=35, year=1990)
        self.nominal = 72 * 440 / 1000

    def csv(self, path, year=1990, hours=None, mode="", project="Work.PRJ"):
        begin, end = datetime(year, 1, 1), datetime(year + 1, 1, 1)
        hours = hours if hours is not None else int((end - begin).total_seconds() / 3600)
        header = (f"Project;{project}\nGeographical Site;place.SIT\nWeather data;weather.MET\n"
                  f"Simulation variant;{Path(project).stem}.VC0\n"
                  "date;E_Grid;EArray;IL_Pmax;GlobInc;PR\n;kW;kW;kW;W/m2;ratio\n")
        path.write_text(header + _annual_csv_rows(year, hours, mode, self.nominal), encoding="utf-8")

    def native_run(self, *args, mode=""):
        project = next(a[3:] for a in args if a.startswith("-p:"))
        target = Path(next(a[5:] for a in args if a.startswith("-ocf:")))
        pdf = Path(next(a[5:] for a in args if a.startswith("-rpf:")))
        self.assertIn("-sd:1990.01.01", args)
        self.assertIn("-ed:1990.12.31", args)
        self.assertIn("-ts:hour", args)
        self.csv(target, project=project, mode=mode)
        pdf.write_bytes(b"%PDF-1.4\nfixture")
        return subprocess.CompletedProcess([], 0, "Warning: fixture sizing warning\nSimulation done in 0min 1sec",
                                           "Host ID: fixture-secret")

    def weather_results(self, path, source, *, project, site, met, variant, delta=0.0):
        header = (f"Project;{project}\nGeographical Site;{site}\nWeather data;{met}\n"
                  f"Simulation variant;{Path(project).stem}.{variant}\n"
                  "date;E_Grid;EArray;IL_Pmax;GlobInc;PR;GlobHor;DiffHor;T_Amb\n"
                  ";kW;kW;kW;W/m2;ratio;W/m2;W/m2;degC\n")
        body = _weather_results_rows(source.read_text(encoding="utf-8"), self.nominal, delta)
        path.write_text(header + body, encoding="utf-8")

    def weather_native(self, *, delta=0.0, failure=None):
        def run(*args):
            if args[0] != "run-simulation":
                return _native_weather_commands(*args)
            if failure is not None:
                raise failure
            argument = lambda flag: next(v[len(flag) + 2:] for v in args if v.startswith("-" + flag + ":"))
            self.assertIn("-sd:2020.01.01", args)
            self.assertIn("-ed:2020.12.31", args)
            self.assertIn("viGlobHor,viDiffHor,viT_Amb", Path(argument("isf")).read_text())
            met = Path(argument("imf"))
            self.weather_results(Path(argument("ocf")), met.parent / (met.stem + "_import.csv"),
                                 project=Path(argument("p")).name, site=Path(argument("s")).name,
                                 met=met.name, variant="VC0", delta=delta)
            Path(argument("rpf")).write_bytes(b"%PDF-1.4\nfixture")
            return subprocess.CompletedProcess([], 0, "Warning: fixture sizing warning\nSimulation done in 0min 1sec", "")
        return run

    def test_weather_manifest_field_errors_block_before_any_write(self):
        cases = {
            "list_weather.json": [1, 2],
            "text_weather.json": "not an object",
            "noconversion_weather.json": {"status": "prepared", "year": 1990,
                                          "time_basis": "local_standard_interval_start", "source_preserved": True},
            "warnings_weather.json": {"status": "prepared", "year": 1990, "source_preserved": True,
                                      "time_basis": "local_standard_interval_start",
                                      "conversion": {"diagnostics": "text", "warnings": [1]}},
            "diagnostics_weather.json": {"status": "prepared", "year": 1990, "source_preserved": True,
                                         "time_basis": "local_standard_interval_start",
                                         "conversion": {"diagnostics": None, "warnings": []}},
            "year_weather.json": {"status": "prepared", "year": 2019, "source_preserved": True,
                                  "time_basis": "local_standard_interval_start",
                                  "conversion": {"diagnostics": "text", "warnings": []}},
        }
        for name, data in cases.items():
            (self.root / "Results" / name).write_text(json.dumps(data), encoding="utf-8")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        for name in (*cases, "absent_weather.json", "../escape.json"):
            with self.subTest(manifest=name):
                blocked = simulate_template(self.cli, self.store, **{**self.request,
                                                                     "new_project": "Blocked.PRJ",
                                                                     "weather_manifest_name": name})
                self.assertEqual(blocked["status"], "blocked", blocked)
                self.assertEqual([item["field"] for item in blocked["issues"]], ["weather_manifest_name"], blocked)
                self.assertTrue(blocked["issues"][0]["message"], blocked)
                json.dumps(blocked, allow_nan=False)
                self.assertFalse((self.root / "Projects/Blocked.PRJ").exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_template_preview_execution_failure_race_and_non_destructive_recovery(self):
        preview = simulate_template(self.cli, self.store, **self.request)
        self.assertEqual((preview["status"], list((self.root / "Results").iterdir())), ("ready", []))
        self.assertFalse((self.root / "Projects/Work.PRJ").exists())

        with patch.object(self.cli, "_run", side_effect=self.native_run) as native:
            result = simulate_template(self.cli, self.store, **self.request, dry_run=False)
            native.assert_called_once()
        self.assertEqual((result["status"], result["stage"], result["template_preserved"]),
                         ("needs_review", "completed", True), result)
        self.assertEqual(result["metrics"]["rows"], 8760)
        self.assertAlmostEqual(result["metrics"]["pr_ratio"], 0.8)
        self.assertEqual(result["metrics"]["clipping_loss_kwh"], 4380)
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertIn("Warning: fixture sizing warning", result["warnings"])
        manifest = Path(result["artifacts"]["manifest"])
        self.assertEqual(json.loads(manifest.read_text()), result)
        self.assertTrue(result["recovery"])
        self.assertTrue({"restore_variant", "restore_project_sources"}.isdisjoint(
            {step["action"] for step in result["recovery"]}))
        for name, data in self.originals.items():
            self.assertEqual((self.root / "Projects" / name).read_bytes(), data)

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

        with patch.object(self.cli, "_run", side_effect=lambda *a: self.native_run(*a, mode="suspicious")):
            review = simulate_template(self.cli, self.store, **{**self.request, "new_project": "Review.PRJ"}, dry_run=False)
        self.assertEqual(review["status"], "needs_review")
        self.assertTrue(review["metrics"]["diagnostic_flags"])
        self.assertTrue(review["template_preserved"])

        real, seen = pvsyst_simulation.fingerprint, []

        def racy(path):
            name = Path(path).name
            if name.startswith("Race_annual_") and name.endswith(".csv"):
                seen.append(name)
                if len(seen) > 1:
                    with Path(path).open("a", encoding="utf-8") as stream:
                        stream.write("\n")
            return real(path)

        with patch.object(self.cli, "_run", side_effect=self.native_run), \
                patch.object(pvsyst_simulation, "fingerprint", side_effect=racy):
            changed = simulate_template(self.cli, self.store, **{**self.request, "new_project": "Race.PRJ"}, dry_run=False)
        self.assertEqual((changed["status"], changed["metrics"]["rows"]), ("failed", 8760), changed)
        self.assertIn("across result validation stages", changed["error"])
        self.assertTrue(changed["template_preserved"])

        missing = simulate_template(self.cli, self.store)
        self.assertEqual({item["field"] for item in missing["issues"]}, set(self.request))
        for changes in ({"year": True}, {"tilt": True}, {"tilt": float("nan")}, {"year": 2069},
                        {"pan_name": "missing.PAN"}, {"site_name": "missing.SIT", "met_name": "missing.MET"}):
            bad = simulate_template(self.cli, self.store, **{**self.request, "new_project": "Unused.PRJ", **changes})
            self.assertEqual(bad["status"], "blocked")
            json.dumps(bad, allow_nan=False)
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
        text = text.replace("End of PVObject pvVCalcul",
                            "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
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
            design_basis="Synthetic declared envelope for regression test; not manufacturer certification")
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
        self.assertEqual((result["status"], result["stage"]), ("needs_review", "completed"), result)
        self.assertAlmostEqual(result["metrics"]["nominal_dc_power_kw"], 28.16)
        self.assertEqual(len(result["capacity_plan"]["steps"]), 3)
        values = self.store.inspect_parameters("Work.PRJ", "VC0")["subarrays"][0]["values"]
        self.assertEqual((values["NModSerie"], values["NStringCh"], values["NInverter"]), ("16", "4", "1"))
        self.assertTrue(result["recovery"])
        self.assertTrue({"restore_variant", "restore_project_sources"}.isdisjoint(
            {step["action"] for step in result["recovery"]}))
        for name, data in originals.items():
            self.assertEqual((projects / name).read_bytes(), data)

        for capacity in ({}, {**constraints, "target_dc_kw": float("nan")}):
            blocked = simulate_template(self.cli, self.store, **{**request, "new_project": "Blocked.PRJ",
                                                                 "capacity": capacity})
            self.assertEqual(blocked["status"], "blocked", blocked)
            self.assertTrue(blocked["issues"])
            self.assertFalse((projects / "Blocked.PRJ").exists())

        _, lines = self.store._text((projects / "Template.VC0").read_bytes())
        pan = pan_validation_fields(module.read_text())
        ond = electrical_inventory("OND", inverter.read_text())
        for capacity in ({}, {**constraints, "target_dc_kw": True}, {**constraints, "cold_voc_v": 1e8},
                         {**constraints, "voltage_standard": "guess"}, {**constraints, "hot_vmp_v": 1000},
                         {**constraints, "mppt_max_operating_current_a": 1}, {**constraints, "unknown": 1},
                         {**constraints, "capacity_tolerance_pct": None}, {**constraints, "target_dc_kw": float("nan")}):
            with self.subTest(capacity=capacity):
                checked = plan_capacity(self.store, lines, 1, pan, ond, capacity)
                self.assertEqual(checked["status"], "blocked", checked)
                self.assertTrue(checked["issues"])

        with patch.object(self.cli, "_run", side_effect=PVsystError("Native failure")):
            failed = simulate_template(self.cli, self.store, **{**request, "new_project": "SizedFailure.PRJ"}, dry_run=False)
        self.assertEqual((failed["status"], failed["template_preserved"]), ("failed", True), failed)
        self.assertTrue(failed["recovery"])

    def test_annual_acceptance_calendar_units_coverage_and_nonfinite_values(self):
        path = self.root / "Results/annual.csv"
        for year, hours in ((1990, 8760), (2020, 8784)):
            self.csv(path, year)
            report = annual_summary(path, year, self.nominal)
            self.assertEqual(report["rows"], hours)
            self.assertAlmostEqual(report["pr_ratio"], 0.8)
            self.assertAlmostEqual(report["grid_energy_kwh"], self.nominal * 0.5 * 0.8 * hours / 2)
        for kwargs in ({"hours": 48}, {"hours": 48, "mode": "gap"}, {"hours": 48, "mode": "nonfinite"}, {"hours": 8761}):
            self.csv(path, **kwargs)
            with self.assertRaises(ValueError):
                annual_summary(path, 1990, self.nominal)
        self.csv(path)
        for year, power in ((2069, self.nominal), (True, self.nominal), (1990, 0),
                            (1990, float("inf")), (1990, self.nominal * 2)):
            with self.assertRaises(ValueError):
                annual_summary(path, year, power)
        self.csv(path, hours=24)
        path.write_text(path.read_text().replace(";W/m2;ratio", ";unknown;ratio"))
        with self.assertRaises(ValueError):
            annual_summary(path, 1990, self.nominal)

    def test_weather_bound_run_comparison_and_non_destructive_failure_recovery(self):
        self.cli._help_cache["convert-meteo"] = "--input-csv-file"
        source = self.root / "Meteo/station_2020.csv"
        source.write_text(_station_weather_text(2020), encoding="utf-8-sig")
        native = self.weather_native()
        with patch.object(self.cli, "_run", side_effect=native):
            prepared = prepare_weather(self.cli, csv_name=source.name, year=2020, sit_name="station.SIT",
                                       met_name="station.MET", country_code="CN",
                                       time_basis="local_standard_interval_start", dry_run=False)
        self.assertEqual((prepared["status"], prepared["source_preserved"]), ("needs_review", True), prepared)
        manifest = Path(prepared["artifacts"]["manifest"])
        data = json.loads(manifest.read_text())
        data["conversion"]["warnings"] = []
        manifest.write_text(json.dumps(data), encoding="utf-8")
        source_hash = data["source"]["sha256"]
        request = {**self.request, "year": 2020, "site_name": "station.SIT", "met_name": "station.MET",
                   "weather_manifest_name": manifest.name}
        with patch.object(self.cli, "_run", side_effect=native):
            preview = simulate_template(self.cli, self.store, **{**request, "new_project": "Wx.PRJ"})
            self.assertEqual(preview["status"], "ready", preview)
            self.assertIn("Weather preparation remains needs_review; original review state is retained",
                          preview["warnings"])
            self.assertEqual([line for line in preview["warnings"] if line.startswith("The imported weather")], [])
            self.assertEqual([asset for asset in preview["inputs"] if asset.startswith("weather_")],
                             ["weather_manifest", "weather_sit", "weather_met", "weather_mef",
                              "weather_normalized_csv", "weather_monthly_csv", "weather_source"])
            self.assertEqual(list((self.root / "Results").glob("Wx_annual_*")), [])
            result = simulate_template(self.cli, self.store, **{**request, "new_project": "Wx.PRJ"}, dry_run=False)
            self.assertEqual((result["status"], result["metrics"]["rows"]), ("needs_review", 8784), result)
            self.assertEqual(result["native_context"]["Weather data"], "station.MET")
            self.assertEqual(result["native_context"]["Geographical Site"], "station.SIT")
            self.assertFalse(result["weather_comparison"]["needs_review"])
            self.assertTrue(result["weather_comparison"]["timestamps_match"])
            self.assertEqual(result["weather_comparison"]["rows"], 8784)
            self.assertEqual(result["weather_comparison"]["columns"]["GlobHor"]["max_abs_delta"], 0.0)
            self.assertEqual(result["weather_comparison"]["columns"]["T_Amb"]["outside_review_threshold"], 0)
            self.assertEqual(result["weather_comparison"]["review_thresholds"],
                             {"irradiance_w_m2": 0.1, "temperature_degC": 0.01})
        with patch.object(self.cli, "_run", side_effect=self.weather_native(delta=0.5)):
            shifted = simulate_template(self.cli, self.store, **{**request, "new_project": "WxShift.PRJ"}, dry_run=False)
        self.assertEqual(shifted["status"], "needs_review", shifted)
        self.assertTrue(shifted["weather_comparison"]["needs_review"])
        globhor = shifted["weather_comparison"]["columns"]["GlobHor"]
        self.assertEqual((globhor["max_abs_delta"], globhor["outside_review_threshold"]), (0.5, 1))
        self.assertEqual(json.loads(Path(shifted["artifacts"]["manifest"]).read_text()), shifted)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)

        failing = self.weather_native(failure=PVsystError("Native weather-bound failure"))
        with patch.object(self.cli, "_run", side_effect=failing):
            failed = simulate_template(self.cli, self.store, **{**request, "new_project": "WxFail.PRJ"}, dry_run=False)
        self.assertEqual((failed["status"], failed["stage"], failed["template_preserved"]),
                         ("failed", "simulation", True), failed)
        self.assertIn("Native weather-bound failure", failed["error"])
        self.assertTrue(failed["recovery"])
        self.assertTrue({"restore_variant", "restore_project_sources"}.isdisjoint(
            {step["action"] for step in failed["recovery"]}))
        self.assertIn("Weather preparation remains needs_review", " ".join(failed["warnings"]))
        for name, raw in self.originals.items():
            self.assertEqual((self.root / "Projects" / name).read_bytes(), raw)
        self.assertEqual(json.loads(Path(failed["artifacts"]["manifest"]).read_text()), failed)


class TerminalStateTests(unittest.TestCase):
    def test_simulation_terminal_manifest_and_save_error_retention(self):
        root, originals, cli, store = _setup_workspace(self)
        request = dict(template_project="Template.PRJ", variant="VC0", new_project="Work.PRJ",
                       pan_name="module.PAN", ond_name="inverter.OND", site_name="place.SIT",
                       met_name="weather.MET", tilt=35, year=1990)
        for index, metrics in enumerate(({"rows": 8760}, None)):
            with self.subTest(metrics=metrics):
                with patch.object(cli, "run_simulation", side_effect=_native_outputs(minimal=True)), \
                        patch.object(pvsyst_simulation, "annual_summary", return_value=metrics):
                    result = simulate_template(cli, store, **{**request, "new_project": f"Work{index}.PRJ"}, dry_run=False)
                self.assertEqual((result["status"], result["stage"], result["template_preserved"]),
                                 ("failed", "results", True), result)
                self.assertTrue(result["error"])
                self.assertEqual(json.loads(Path(result["artifacts"]["manifest"]).read_text()), result)

        with patch.object(cli, "run_simulation", side_effect=_native_outputs(OVERSIZED_PROJECT_ROW, minimal=True)):
            with self.assertRaises(csv.Error):
                simulate_template(cli, store, **request, dry_run=False)
        record = json.loads((root / "Results/Work_annual_1990.json").read_text())
        self.assertEqual((record["status"], record["stage"]), ("failed", "results"))
        self.assertIn("field larger than field limit", record["error"])

        written = pvsyst_simulation.write_json

        def terminal_save_fails(path, rec, first=False):
            if rec["status"] == "failed":
                raise OSError("disk full while saving the terminal manifest")
            return written(path, rec, first=first)

        with patch.object(cli, "run_simulation", side_effect=_native_outputs(OVERSIZED_PROJECT_ROW, minimal=True)), \
                patch.object(pvsyst_simulation, "write_json", side_effect=terminal_save_fails):
            with self.assertRaises(csv.Error):
                simulate_template(cli, store, **{**request, "new_project": "WorkFail.PRJ"}, dry_run=False)
        for name, data in originals.items():
            self.assertEqual((root / "Projects" / name).read_bytes(), data)

    def test_revision_terminal_manifest_states(self):
        root, originals, cli, store = _setup_workspace(self, meteo_name="weather.MET")
        spec = dict(source_project="Template.PRJ", source_variant="VC0", new_project="Revision.PRJ",
                    new_variant="VC2", objective="Regression: terminal manifest state", year=1990,
                    nominal_dc_power_kw=31.68, power_source="Fixture declaration", changes={"tilt": 30})
        with patch("pvsyst_auto.engineering.handoff.build_project_handoff", return_value=dict(HANDOFF)):
            for index, metrics in enumerate(({"rows": 8760}, None)):
                with self.subTest(metrics=metrics):
                    with patch.object(cli, "run_simulation", side_effect=_native_outputs(minimal=True)), \
                            patch.object(revision_flow, "annual_summary", return_value=metrics):
                        record = revision_flow.run_engineering_task(
                            cli, store, **{**spec, "new_project": f"Revision{index}.PRJ"}, dry_run=False)
                    self.assertEqual((record["status"], record["stage"]), ("failed", "results"), record)
                    self.assertFalse(record["native_consumption_verified"])
                    self.assertTrue(record["source_preserved"])
                    saved = json.loads(Path(record["artifacts"]["manifest"]).read_text())
                    self.assertEqual((saved["status"], saved["error"], saved["source_preserved"]),
                                     ("failed", record["error"], True))

            with patch.object(cli, "run_simulation", side_effect=_native_outputs(OVERSIZED_PROJECT_ROW, minimal=True)):
                with self.assertRaises(csv.Error):
                    revision_flow.run_engineering_task(cli, store, **spec, dry_run=False)
            record = json.loads((root / "Results/Revision_engineering_1990.json").read_text())
            self.assertEqual((record["status"], record["stage"], record["native_consumption_verified"]),
                             ("failed", "results", False))
            self.assertIn("field larger than field limit", record["error"])
        for name, data in originals.items():
            self.assertEqual((root / "Projects" / name).read_bytes(), data)

    def test_house_checkpoint_unanticipated_error_leaves_needs_attention(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        for folder in ("Projects", "Results", "Models", "UserData", "Sites", "Meteo"):
            (root / folder).mkdir()
        (root / "Projects/Source.PRJ").write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n")
        (root / "Projects/Source.VC0").write_text("PVObject_=pvVCalcul\nEnd of PVObject pvVCalcul\n")
        (root / "Sites/place.SIT").write_text("PVObject_=pvSite\nEnd of PVObject pvSite\n")
        (root / "Meteo/weather.MET").write_bytes(b"opaque fixture")
        paths = {"source_project": root / "Projects/Source.PRJ", "source_variant": root / "Projects/Source.VC0",
                 "working_project": root / "Projects/Work.PRJ", "working_variant": root / "Projects/Work.VC0",
                 "scene": root / "UserData/Work_house.pvc2", "sfi": root / "Models/Work_house.SFI",
                 "manifest": root / "Results/Work_house_task.json",
                 "site": root / "Sites/place.SIT", "met": root / "Meteo/weather.MET"}
        request = {"source_project": "Source.PRJ", "source_variant": "VC0", "new_project": "Work.PRJ",
                   "variant_name": "House", "year": 2022, "house": {"kind": "mesh"}, "layout": {"tables": []}}
        store = type("HouseStore", (), {
            "workspace": root, "lock": threading.RLock(),
            "_root": staticmethod(lambda: root / "Projects"),
            "_belongs_to_project": staticmethod(lambda path, proj: path.stem.casefold() == Path(proj).stem.casefold()),
            "_read": staticmethod(lambda path: Path(path).read_bytes()),
            "create_project": staticmethod(lambda proj, content, variants: (
                (root / "Projects" / proj).write_text(content),
                [(root / "Projects" / f"{Path(proj).stem}.{m}").write_text(t) for m, t in variants.items()]
            )),
        })()

        def scene(st, components, *, dry_run, **arguments):
            if dry_run:
                return {"status": "ready", "issues": [], "warnings": [], "geometry": {"pv_table_count": 1}}
            raise Unanticipated("scene writer crashed")

        with patch.object(house_flow, "_prepare", return_value=(paths, {"scene_name": "Work_house.pvc2"})), \
                patch.object(house_flow, "create_house_pv_scene", side_effect=scene):
            with self.assertRaises(Unanticipated):
                house_flow.run_house_task(store, object(), object(), request=request, dry_run=False, confirm=True)
        record = json.loads(paths["manifest"].read_text())
        self.assertEqual((record["status"], record.get("stage"), record["phase"], record["native_consumption_verified"]),
                         ("needs_attention", "stopped", "project_created", False))
        self.assertIn("Unanticipated: scene writer crashed", record["issues"])


if __name__ == "__main__":
    unittest.main()

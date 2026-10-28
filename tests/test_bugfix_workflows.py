"""Workflow regressions: an execution-phase failure always leaves a terminal manifest.

The template, the variant store and its guarded editors are real. Native execution, the
engineering handoff and the house scene/GUI entry points are replaced at their public
boundaries, as tests/test_design_flow.py does; nothing here invokes PVsyst.
"""
import csv
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pvsyst_auto.workflows.house as house_flow
import pvsyst_auto.workflows.revision as revision_flow
import pvsyst_auto.workflows.simulation as template_flow
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.variants import VariantStore
from test_components import PAN, OND
from test_grid import grid_text, ARRAY


class Unanticipated(Exception):
    """Any error outside the anticipated failure set of a workflow."""


# A context field beyond the csv module limit raises csv.Error, which is not a ValueError.
OVERSIZED_PROJECT_ROW = "Project;" + "x" * (csv.field_size_limit() + 1)
HANDOFF = {"status": "ready", "issues": [], "review_items": [], "missing_items": [], "saved": {}}


class FakeCLI:
    """PVsystCLI at its protocol boundary: capability/licence queries and one scripted run."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.native = None

    def _require(self, command, option):
        return None

    def license_info(self):
        return {"status": "LICENSED"}

    def version(self):
        return "PVsystCLI 8.1.6"

    @staticmethod
    def _redact(text):
        return str(text)

    def run_simulation(self, project, variant, **options):
        return self.native(project, variant, **options)


def native_outputs(project_row=None):
    """A native run writing a context-correct CSV and a PDF; project_row replaces the Project row."""
    def run(project, variant, *, out_csv, report_pdf, **options):
        rows = [project_row or f"Project;{project}", "Geographical Site;place.SIT", "Weather data;weather.MET",
                f"Simulation variant;{Path(project).stem}.{variant}",
                "date;E_Grid;EArray;IL_Pmax;GlobInc;PR", ";kW;kW;kW;W/m2;ratio", "01/01/90 00:00;0;0;0;0;"]
        Path(out_csv).write_text("\n".join(rows) + "\n", encoding="utf-8")
        Path(report_pdf).write_bytes(b"%PDF-1.4\nfixture")
        return {"csv": str(out_csv), "pdf": str(report_pdf), "warnings": [], "diagnostics": ""}
    return run


def read_manifest(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class TemplateWorkspace(unittest.TestCase):
    """The test_simulation template: one fixed grid subarray bound to workspace SIT/MET/PAN/OND."""

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
        project = ("PVObject_=pvProject\nComment=Template\nMeteoFileName=weather.MET\n"
                   + site.replace("PVObject_=", "PVObject_SitePrj=") + "End of PVObject pvProject\n")
        weather = (site.replace("PVObject_=", "PVObject_SiteSimul=")
                   + "PVObject_MeteoSimul=pvMeteo\nNomF=weather.MET\nSiteM=Place\n"
                   + site.replace("PVObject_=", "PVObject_SiteMet=") + "End of PVObject pvMeteo\n")
        array = ARRAY.replace("SubArrayId=1", "SubArrayId=1\nPVModule=module.PAN\nGInverter=inverter.OND")
        variant = grid_text(arrays=array).replace("Version=8.1.6\n", "Version=8.1.6\nPRCorrTemp=False\n" + weather, 1)
        (self.root / "Projects/Template.PRJ").write_text(project)
        (self.root / "Projects/Template.VC0").write_text(variant)
        self.originals = {p.name: p.read_bytes() for p in (self.root / "Projects").iterdir()}
        lock = threading.RLock()
        self.store = VariantStore(self.root, ComponentStore(self.root, lock=lock), lock)
        self.cli = FakeCLI(self.root)

    def assert_template_untouched(self):
        for name, data in self.originals.items():
            self.assertEqual((self.root / "Projects" / name).read_bytes(), data)


class SimulationTerminalStateTests(TemplateWorkspace):
    def setUp(self):
        super().setUp()
        self.request = dict(template_project="Template.PRJ", variant="VC0", new_project="Work.PRJ",
                            pan_name="module.PAN", ond_name="inverter.OND", site_name="place.SIT",
                            met_name="weather.MET", tilt=35, year=1990)
        preview = template_flow.simulate_template(self.cli, self.store, **self.request)
        self.assertEqual(preview["status"], "ready", preview)

    def test_incomplete_phase_record_is_a_recorded_failure(self):
        """KeyError/TypeError in execution return failed, re-check the template and persist it."""
        for index, metrics in enumerate(({"rows": 8760}, None)):
            with self.subTest(metrics=metrics):
                self.cli.native = native_outputs()
                with patch.object(template_flow, "annual_summary", return_value=metrics):
                    result = template_flow.simulate_template(
                        self.cli, self.store, **{**self.request, "new_project": f"Work{index}.PRJ"}, dry_run=False)
                self.assertEqual((result["status"], result["stage"]), ("failed", "results"), result)
                self.assertTrue(result["error"])
                self.assertTrue(result["template_preserved"])
                self.assertEqual(read_manifest(result["artifacts"]["manifest"]), result)
        self.assert_template_untouched()

    def test_unanticipated_error_propagates_with_a_terminal_manifest(self):
        """A malformed native CSV raises csv.Error to the caller, but never leaves "running" behind."""
        self.cli.native = native_outputs(OVERSIZED_PROJECT_ROW)
        with self.assertRaises(csv.Error):
            template_flow.simulate_template(self.cli, self.store, **self.request, dry_run=False)
        record = read_manifest(self.root / "Results/Work_annual_1990.json")
        self.assertEqual((record["status"], record["stage"]), ("failed", "results"))
        self.assertIn("field larger than field limit", record["error"])
        self.assert_template_untouched()

    def test_failed_terminal_save_keeps_the_original_error(self):
        written = template_flow.write_json

        def terminal_save_fails(path, record, first=False):
            if record["status"] == "failed":
                raise OSError("disk full while saving the terminal manifest")
            return written(path, record, first=first)

        self.cli.native = native_outputs(OVERSIZED_PROJECT_ROW)
        with patch.object(template_flow, "write_json", side_effect=terminal_save_fails):
            with self.assertRaises(csv.Error):
                template_flow.simulate_template(self.cli, self.store, **self.request, dry_run=False)


class RevisionTerminalStateTests(TemplateWorkspace):
    def setUp(self):
        super().setUp()
        handoff = patch("pvsyst_auto.engineering.handoff.build_project_handoff", return_value=dict(HANDOFF))
        handoff.start()
        self.addCleanup(handoff.stop)
        self.spec = dict(source_project="Template.PRJ", source_variant="VC0", new_project="Revision.PRJ",
                         new_variant="VC2", objective="Regression: terminal manifest state", year=1990,
                         nominal_dc_power_kw=31.68, power_source="Fixture declaration", changes={"tilt": 30})
        preview = revision_flow.run_engineering_task(self.cli, self.store, **self.spec)
        self.assertEqual(preview["status"], "ready", preview)

    def test_incomplete_phase_record_is_a_recorded_failure(self):
        """KeyError/TypeError in execution return failed, re-check the source and persist it."""
        for index, metrics in enumerate(({"rows": 8760}, None)):
            with self.subTest(metrics=metrics):
                self.cli.native = native_outputs()
                with patch.object(revision_flow, "annual_summary", return_value=metrics):
                    record = revision_flow.run_engineering_task(
                        self.cli, self.store, **{**self.spec, "new_project": f"Revision{index}.PRJ"}, dry_run=False)
                self.assertEqual((record["status"], record["stage"]), ("failed", "results"), record)
                self.assertFalse(record["native_consumption_verified"])
                self.assertTrue(record["source_preserved"])
                saved = read_manifest(record["artifacts"]["manifest"])
                self.assertEqual((saved["status"], saved["error"], saved["source_preserved"]),
                                 ("failed", record["error"], True))
        self.assert_template_untouched()

    def test_unanticipated_error_propagates_with_a_terminal_manifest(self):
        self.cli.native = native_outputs(OVERSIZED_PROJECT_ROW)
        with self.assertRaises(csv.Error):
            revision_flow.run_engineering_task(self.cli, self.store, **self.spec, dry_run=False)
        record = read_manifest(self.root / "Results/Revision_engineering_1990.json")
        self.assertEqual((record["status"], record["stage"]), ("failed", "results"))
        self.assertFalse(record["native_consumption_verified"])
        self.assertIn("field larger than field limit", record["error"])
        self.assert_template_untouched()


class HouseStore:
    """The VariantStore members the house task touches before its scene stage."""

    def __init__(self, root):
        self.workspace = root
        self.lock = threading.RLock()

    def _root(self):
        return self.workspace / "Projects"

    @staticmethod
    def _belongs_to_project(path, project):
        return path.stem.casefold() == Path(project).stem.casefold()

    @staticmethod
    def _read(path):
        return Path(path).read_bytes()

    def create_project(self, project, content, variants):
        (self._root() / project).write_text(content)
        for member, text in variants.items():
            (self._root() / f"{Path(project).stem}.{member}").write_text(text)


class HouseCheckpointTerminalStateTests(unittest.TestCase):
    def test_unanticipated_error_after_a_stage_leaves_needs_attention(self):
        """A crash after a "running" stage save still records a terminal checkpoint, then propagates."""
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

        def scene(store, components, *, dry_run, **arguments):
            if dry_run:
                return {"status": "ready", "issues": [], "warnings": [], "geometry": {"pv_table_count": 1}}
            raise Unanticipated("scene writer crashed")

        with patch.object(house_flow, "_prepare", return_value=(paths, {"scene_name": "Work_house.pvc2"})), \
                patch.object(house_flow, "create_house_pv_scene", side_effect=scene), \
                patch.object(house_flow, "open_native_project", return_value={"status": "ready_to_open"}):
            with self.assertRaises(Unanticipated):
                house_flow.run_house_task(HouseStore(root), object(), object(), request=request,
                                          dry_run=False, confirm=True)
        record = read_manifest(paths["manifest"])
        self.assertEqual((record["status"], record.get("stage"), record["phase"]),
                         ("needs_attention", "stopped", "project_created"))
        self.assertFalse(record["native_consumption_verified"])
        self.assertIn("Unanticipated: scene writer crashed", record["issues"])


if __name__ == "__main__":
    unittest.main()

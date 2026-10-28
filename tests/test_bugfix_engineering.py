"""Regression tests for the part-7 engineering review fixes (blocks, comparison, inputs, review, sourced house)."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.components.editor import objects
from pvsyst_auto.engineering.comparison import RUN_FIELDS, compare_engineering_scenarios
from pvsyst_auto.engineering.handoff import _load_records
from pvsyst_auto.engineering.inputs import discover_design_inputs
from pvsyst_auto.engineering.review import review_project_inputs
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.workflows.blocks import prepare_block_project
from pvsyst_auto.workflows.design import _prune_empty
from pvsyst_auto.workflows.sourced_house import run_sourced_house_task

from test_design_inputs import FakeComponents, FakeVariants, template_spec


class Workspace(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for folder in ("Projects", "Results", "Sites", "Meteo"):
            (self.root / folder).mkdir()

    def files(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))


class BlockProjectStatusTests(Workspace):
    def test_locked_source_read_is_blocked_not_failed(self):
        (self.root / "Projects" / "SOURCE.PRJ").write_bytes(b"PVObject_=pvProject\r\nEnd of PVObject pvProject\r\n")
        (self.root / "Projects" / "SOURCE.VC0").write_bytes(b"PVObject_=pvVCalcul\r\nEnd of PVObject pvVCalcul\r\n")
        store = VariantStore(self.root, None)

        def locked(path):
            raise PermissionError(13, "The process cannot access the file", path.name)

        store._read = locked
        before = self.files()
        result = prepare_block_project(store, "SOURCE.PRJ", "VC0", "RESULT.PRJ",
                                       [{"id": "roof", "source_subarray_id": 1, "count": 2}], dry_run=False)
        # Nothing was published: a locked source is a blocked precondition, not a failed write.
        self.assertEqual(result["status"], "blocked", result)
        self.assertFalse(result["prepared"])
        self.assertIn("cannot access", result["issues"][0])
        self.assertEqual(self.files(), before)


CSV = """PVsyst export
Project;{project}
Simulation variant;{stem}.{variant}
Geographical Site;Site.SIT
Weather data;Weather.MET

date;E_Grid;GlobInc
;kW;W/m2
01/01/24 00:00;0;0
01/01/24 01:00;{first};100
01/01/24 02:00;{second};200
"""
HANDOFF = {"status": "ready", "review_items": [],
           "saved": {"project": {"values": {}}, "system": {"SystemType": "Grid"},
                     "sources": {"variant": {"site": {"name": "Site.SIT"}, "meteo": {"name": "Weather.MET"}}}}}


class ComparisonTests(Workspace):
    def setUp(self):
        super().setUp()
        self.store = type("Store", (), {"lock": threading.RLock(), "workspace": self.root})()
        self.scenarios = []
        for label, project, variant, values, changes in (("baseline", "Alpha.PRJ", "VC0", (1.5, 2.5), []),
                                                          ("candidate", "Beta.PRJ", "VC1", (2.0, 3.0), ["system"])):
            name = label + ".csv"
            (self.root / "Results" / name).write_text(CSV.format(
                project=project, stem=Path(project).stem, variant=variant, first=values[0], second=values[1]),
                encoding="utf-8", newline="\r\n")
            self.scenarios.append({"label": label, "project": project, "variant": variant,
                                   "csv_name": name, "declared_changes": changes})

    def compare(self, **kwargs):
        with patch("pvsyst_auto.engineering.handoff.build_project_handoff", return_value=json.loads(json.dumps(HANDOFF))):
            return compare_engineering_scenarios(self.store, objective="Compare two roofs", **kwargs)

    def test_written_report_keeps_needs_review_and_records_created(self):
        preview = self.compare(scenarios=self.scenarios, output_name="cmp.json", dry_run=True)
        self.assertEqual(preview["status"], "needs_review", preview)
        self.assertFalse(preview["created"])
        written = self.compare(scenarios=self.scenarios, output_name="cmp.json", dry_run=False)
        # Unbound historical inputs keep needs_review, but the written reports must stay visible.
        self.assertEqual(written["status"], "needs_review", written)
        self.assertTrue(written["review_items"])
        self.assertTrue(written["created"])
        self.assertEqual(written["written_artifacts"], ["csv", "markdown", "json"])
        recorded = json.loads((self.root / "Results" / "cmp.json").read_text(encoding="utf-8"))
        self.assertEqual((recorded["status"], recorded["created"]), ("needs_review", True))
        self.assertEqual(recorded["written_artifacts"], ["csv", "markdown", "json"])

    def test_bound_handoff_without_saved_sources_is_blocked(self):
        manifest = {key: None for key in RUN_FIELDS}
        manifest["operation"] = "engineering_revision"
        (self.root / "Results" / "run0.json").write_text(json.dumps(manifest), encoding="utf-8")
        scenarios = [dict(self.scenarios[0], run_manifest_name="run0.json"), self.scenarios[1]]
        binding = {"operation": "engineering_revision", "status": "completed", "stage": "completed", "year": 2024,
                   "input_assets": {}, "input_handoff": {"status": "ready", "saved": {"project": {"values": {}}}}}
        with patch("pvsyst_auto.workflows.revision.load_engineering_run", return_value=binding):
            result = self.compare(scenarios=scenarios, output_name="bound.json", dry_run=False)
        self.assertEqual(result["status"], "blocked", result)
        self.assertTrue(result["issues"])
        self.assertFalse(result["created"])
        self.assertFalse((self.root / "Results" / "bound.json").exists())


class HandoffLoadRecordTests(unittest.TestCase):
    def test_daily_profile_is_kept_and_hourly_cache_omitted(self):
        # Characterizes the removed dead ValHour clause: a pvLoad ValHour_N is a declared input.
        text = ("PVObject_UserNeeds=pvLoad\n  Comment=Daily\n  LoadType=DailyProfile\n"
                "  ValHour_1=1.000,2.000,3.000,6,\n  Day_1=1,2,3\nEnd of PVObject pvLoad\n")
        load = _load_records(objects(text))
        record = load["objects"][0]
        self.assertEqual(record["values"]["ValHour_1"], "1.000,2.000,3.000,6,")
        self.assertEqual(record["omitted_cache_fields"], ["Day_1"])
        self.assertFalse(load["hourly_cache_contents_included"])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "Projects").mkdir()
        components = FakeComponents(self.root, {"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        self.spec = template_spec()
        self.store = FakeVariants(self.root, components, {"Template": {"variants": ["VC0"], "VC0": self.spec}})
        self.patches = [patch("pvsyst_auto.engineering.inputs.electrical_inventory",
                              side_effect=lambda kind, text: {"fields": {"source": text}}),
                        patch("pvsyst_auto.engineering.inputs.pan_validation_fields", return_value={})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def discover(self, **kwargs):
        return discover_design_inputs(self.store, module_manufacturer="Lab", module_model="440W",
                                      inverter_manufacturer="Lab", inverter_model="50kW",
                                      template_project="Template.PRJ", variant="VC0", **kwargs)

    def test_unreadable_template_inverter_count_rejects_the_template(self):
        self.spec["parameters"]["subarrays"][0]["values"]["NInverter"] = "2.0"
        plan = {"status": "ready", "inverter_count": 2, "steps": [], "capacity_error_pct": 0.0, "issues": []}
        with patch("pvsyst_auto.engineering.inputs.plan_capacity", return_value=plan):
            result = self.discover(capacity={"target_dc_kw": 10.0})
        self.assertEqual(result["status"], "blocked", result)
        self.assertIsNone(result["selected"])
        template = result["candidates"]["templates"][0]
        self.assertFalse(template["valid"])
        self.assertIn("NInverter", "; ".join(template["reasons"]))

    def test_tilt_never_filters_or_blocks_discovery(self):
        for tilt in (95, "steep", float("nan")):
            with self.subTest(tilt=tilt):
                result = self.discover(tilt=tilt)
                self.assertEqual(result["status"], "ready", result)
                self.assertFalse(any(item["field"] == "tilt" for item in result["issues"]))


class ReviewStore:
    def __init__(self, root, orientation):
        self.workspace, self.lock, self.orientation = root, threading.RLock(), orientation

    def inspect_project(self, project):
        names = {"site": {"name": "Site.SIT"}, "meteo": {"name": "Weather.MET"}}
        return {"sources": {"Work.PRJ": names, "Work.VC0": names}, "files": {"Work.PRJ": "1", "Work.VC0": "2"},
                "warnings": []}

    def inspect_parameters(self, project, variant):
        values = {"NoOrientation": "1", "NModSerie": "10", "NStringCh": "2", "NInverter": "1"}
        return {"system_parameters": {"SystemType": "Grid"}, "subarrays": [{"id": 1, "values": values}],
                "orientations": [{"id": 1, "values": {"FieldType": "FixedPlane", **self.orientation}}],
                "main_array": {"values": {}}}

    def inspect(self, project, variant):
        return {"subarrays": [{"references": {"PAN": ["Module.PAN"], "OND": ["Inverter.OND"]}}]}

    def _path(self, project, variant=None):
        return self.root_path / (project if variant is None else f"{Path(project).stem}.{variant}")

    @property
    def root_path(self):
        return self.workspace / "Projects"

    def _read(self, path):
        return b"PVObject_=pvProject\r\n  Comment=Work\r\nEnd of PVObject pvProject\r\n"


class ReviewPrecisionTests(Workspace):
    def review(self, declared_tilt, saved_tilt):
        baseline = {"operation": "prepare_project", "status": "prepared", "prepared": True, "simulated": False,
                    "metrics": None, "working_project": "Work.PRJ", "working_variant": "VC0",
                    "request": {"year": 2024, "new_project": "Work.PRJ", "variant": "VC0",
                                "pan_name": "Module.PAN", "ond_name": "Inverter.OND", "site_name": "Site.SIT",
                                "met_name": "Weather.MET", "tilt": declared_tilt, "azimuth": 0.0},
                    "preparation_context": {"request": {}, "review_required": False},
                    "capacity_plan": {"status": "ready", "series_modules": 10, "string_count": 2, "inverter_count": 1}}
        (self.root / "Results" / "Work_prepare_2024.json").write_text(json.dumps(baseline), encoding="utf-8")
        store = ReviewStore(self.root, {"FieldTilt": saved_tilt, "FieldAzim": "0.000"})
        return review_project_inputs(store, project="Work.PRJ", variant="VC0",
                                     preparation_manifest_name="Work_prepare_2024.json")

    def test_saved_angles_compare_within_editor_precision(self):
        for declared, saved, expected in ((30.0005, "30.0005", "matches"),    # more saved digits than three
                                          (30, "29.99999999", "matches"),      # another writer's float noise
                                          (30.0004, "30.000", "matches"),      # the editor's own serialization
                                          (30, "30.0006", "differs"),          # beyond half a third-decimal unit
                                          (30, "1e1000000", "differs")):       # absurd magnitude never raises
            with self.subTest(declared=declared, saved=saved):
                result = self.review(declared, saved)
                rows = {row["field"]: row for row in result["comparisons"]}
                self.assertEqual(rows["tilt"]["status"], expected, result)
                self.assertEqual(rows["azimuth"]["status"], "matches")
                self.assertEqual(result["status"], "reviewed" if expected == "matches" else "needs_review")


class SourcedHouseTests(Workspace):
    def setUp(self):
        super().setUp()
        components = type("Components", (), {"builtin": None})()
        components._path = lambda kind, name, library="workspace": self.root / "Components" / name
        (self.root / "Components").mkdir()
        for name in ("module.PAN", "inverter.OND"):
            (self.root / "Components" / name).write_bytes(b"component")
        (self.root / "Sites" / "Site.SIT").write_bytes(b"site")
        (self.root / "Meteo" / "Weather.MET").write_bytes(b"met")
        self.store = type("Store", (), {"lock": threading.RLock(), "workspace": self.root,
                                        "components": components})()
        self.store._path = lambda project, variant=None: self.root / "Projects" / project
        self.store.inspect_project = lambda project: {"files": {"Source.PRJ": "1", "Source.VC0": "2"}}
        self.design = {"new_project": "Source.PRJ", "year": 2024, "tilt": 20, "azimuth": 0,
                       "module": {"file_name": "module.PAN"}, "inverter": {"file_name": "inverter.OND"},
                       "weather": {"mode": "existing", "sit_name": "Site.SIT", "met_name": "Weather.MET",
                                   "weather_manifest_name": "Weather_weather.json"}}
        self.binding = {"status": "ready", "needs_review": False, "electrical_design": {"target_dc_kw": 10.0}}

    def run_task(self, request=None, binding=None, **flags):
        request = request if request is not None else {"design": dict(self.design), "new_project": "Final.PRJ",
                                                       "variant_name": "House", "house": {}, "layout": {}}
        return run_sourced_house_task(self.store, None, None, request=request,
                                      binding=self.binding if binding is None else binding, **flags)

    def test_non_object_binding_or_design_is_blocked(self):
        for request, binding in ((None, "not-a-binding"),
                                 ({"design": "not-a-design", "new_project": "Final.PRJ"}, None),
                                 ({"design": dict(self.design, module="module.PAN"), "new_project": "Final.PRJ"}, None)):
            with self.subTest(request=request, binding=binding):
                result = self.run_task(request, binding)
                self.assertEqual(result["status"], "blocked", result)
                self.assertTrue(result["issues"])

    def test_malformed_weather_manifest_at_resume_is_blocked(self):
        design = _prune_empty(dict(self.design, target_dc_kw=10.0,
                                   electrical_design=self.binding["electrical_design"]))
        checkpoint = {"operation": "prepare_project", "status": "prepared", "prepared": True, "simulated": False,
                      "native_consumption_verified": False, "metrics": None, "working_project": "Source.PRJ",
                      "working_variant": "VC0", "request": {"variant": "VC0"},
                      "working_files": {"Source.PRJ": "1", "Source.VC0": "2"},
                      "preparation_context": {"request": design, "source_binding": self.binding}}
        (self.root / "Results" / "Source_prepare_2024.json").write_text(json.dumps(checkpoint), encoding="utf-8")
        for error in (AttributeError("'NoneType' object has no attribute 'casefold'"), KeyError("created_files")):
            with self.subTest(error=type(error).__name__):
                with patch("pvsyst_auto.workflows.sourced_house.load_weather_manifest", side_effect=error):
                    result = self.run_task(resume=True)
                self.assertEqual(result["status"], "blocked", result)
                self.assertEqual(result["issues"], ["Incomplete weather preparation manifest: Weather_weather.json"])
                self.assertFalse(result["native_consumption_verified"])


if __name__ == "__main__":
    unittest.main()

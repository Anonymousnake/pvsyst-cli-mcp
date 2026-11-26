"""Consolidated engineering and scene/hardware verification tests."""
import ctypes
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from textwrap import indent
from unittest.mock import Mock, patch

from pvsyst_auto.components.editor import objects
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.engineering.comparison import RUN_FIELDS, compare_engineering_scenarios
from pvsyst_auto.engineering.handoff import _load_records, build_project_handoff
from pvsyst_auto.engineering.inputs import discover_design_inputs
from pvsyst_auto.engineering.review import review_project_inputs
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.results import fingerprint
from pvsyst_auto.scene.author import house_profile, source_profile
from pvsyst_auto.scene.mapping import verify_grouped_hardware
from pvsyst_auto.workflows import house as house_flow
from pvsyst_auto.workflows.blocks import prepare_block_project
from pvsyst_auto.workflows.design import _prune_empty
from pvsyst_auto.workflows.sourced_house import run_sourced_house_task

from tests._fixtures import FakeCliBase
from tests.unit.workflows.test_design_flow import FakeComponents, FakeVariants, template_spec

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

ORIENT = """  PVObject_=pvOrient
    NoOrient={id}
    FieldType=FixedPlane
    FieldTilt={tilt}
    FieldAzim={azimuth}
    BaseSlope=0
    UsesTiltAver=False
    UsesAzimAver=False
    UsesSlopeAver=False
  End of TOrientGroup
"""
BRANCH = """InverterNode Start;
  NElements=1
  SubArrayId={id}
  SubArrayName={name}
  Children=Start
    StringNode Start;
      NElements=2
      SubArrayId={id}
      SubArrayName={name}
    StringNode End;
  Children=End
InverterNode End;
"""
ARRAY = """  PVObject_=pvSubArray
    Comment={name}
    SubArrayId={id}
    NoOrientation={id}
    PVModule=module.PAN
    GInverter=inverter.OND
    NModSerie=9
    NStringCh=2
    NStrOrient1=2
    NInverter=1
    NInvMPPT=1
{extra}  End of PVObject pvSubArray
"""
TABLE = """    PVObject_{id}=pvShdArrayTable
      NoOrient={id}
      NModInHeight=3
      NModInWidth=6
      DInclin={tilt}
      DAzimut={azimuth}
      BaseSlope=0.000000
    End of PVObject pvShdArrayTable
"""
PLANES = ({"id": 1, "name": "Array A", "tilt": 30.1234567, "azimuth": -12.3},
          {"id": 2, "name": "Array B", "tilt": 20.0, "azimuth": 90.0})
SITE_VARIANT = """PVObject_=pvVCalcul
  Version=8.1.6
  Flags=$00
  PVObject_SiteSimul=pvSite
    Latitude={latitude}
    Longitude=151.2
    Altitude=10
  End of PVObject pvSite
  Orientations, list of=1 TOrientGroup
  PVObject_=pvOrient
    NoOrient=1
    Flags=$00
    FieldType=FixedPlane
    FieldTilt=20
    FieldAzim=0
  End of TOrientGroup
  End of Orientations
  PVObject_PVMainArray=pvMainArray
  PVObject_SystemCircuit=pvCircuit
  Version=8.0.0
  BaseNode=Start
InjectionPointNode Start;
Children=Start
{branch}Children=End
InjectionPointNode End;
  BaseNode=End
  End of PVObject pvCircuit
{array}  End of PVObject pvMainArray
  PVObject_System=pvSystem
    SystemType=Grid
  End of PVObject pvSystem
End of PVObject pvVCalcul
"""


def grouped_variant(*, tilt_text="30.1234567", tables=None, extra=""):
    text = "PVObject_=pvVCalcul\n  Version=8.1.6\n"
    text += "".join(ORIENT.format(id=p["id"], azimuth=p["azimuth"],
                                  tilt=tilt_text if p["id"] == 1 else p["tilt"]) for p in PLANES)
    text += ("  PVObject_PVMainArray=pvMainArray\n  PVObject_SystemCircuit=pvCircuit\n"
             "  Version=8.0.0\n  BaseNode=Start\nInjectionPointNode Start;\nChildren=Start\n"
             + "".join(BRANCH.format(**p) for p in PLANES)
             + "Children=End\nInjectionPointNode End;\n  BaseNode=End\n  End of PVObject pvCircuit\n")
    text += "".join(ARRAY.format(extra=extra if p["id"] == 1 else "", **p) for p in PLANES)
    text += "  End of PVObject pvMainArray\n"
    if tables is not None:
        text += ("  PVObject_Ombrage=pvShading\n    Flags=$00000100\n"
                 "    ListeObjets, list of=2 TShdObject\n"
                 + "".join(TABLE.format(id=p["id"], tilt=tilt, azimuth=azimuth)
                           for p, (tilt, azimuth) in zip(PLANES, tables))
                 + "    End of ListeObjets\n  End of PVObject pvShading\n")
    return text + "End of PVObject pvVCalcul\n"


class ReviewStore:
    def __init__(self, root, orientation):
        self.workspace, self.lock, self.orientation = root, threading.RLock(), orientation

    def inspect_project(self, project):
        names = {"site": {"name": "Site.SIT"}, "meteo": {"name": "Weather.MET"}}
        return {"sources": {"Work.PRJ": names, "Work.VC0": names},
                "files": {"Work.PRJ": "1", "Work.VC0": "2"}, "warnings": []}

    def inspect_parameters(self, project, variant):
        values = {"NoOrientation": "1", "NModSerie": "10", "NStringCh": "2", "NInverter": "1"}
        return {"system_parameters": {"SystemType": "Grid"}, "subarrays": [{"id": 1, "values": values}],
                "orientations": [{"id": 1, "values": {"FieldType": "FixedPlane", **self.orientation}}],
                "main_array": {"values": {}}}

    def inspect(self, project, variant):
        return {"subarrays": [{"references": {"PAN": ["Module.PAN"], "OND": ["Inverter.OND"]}}]}

    def _path(self, project, variant=None):
        return (self.workspace / "Projects") / (project if variant is None else f"{Path(project).stem}.{variant}")

    def _read(self, path):
        return b"PVObject_=pvProject\r\n  Comment=Work\r\nEnd of PVObject pvProject\r\n"


class EngineeringTests(unittest.TestCase):
    def workspace(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        for folder in ("Projects", "Results", "Sites", "Meteo"):
            (root / folder).mkdir()
        return root

    def test_locked_source_read_is_blocked_not_failed(self):
        root = self.workspace()
        (root / "Projects" / "SOURCE.PRJ").write_bytes(b"PVObject_=pvProject\r\nEnd of PVObject pvProject\r\n")
        (root / "Projects" / "SOURCE.VC0").write_bytes(b"PVObject_=pvVCalcul\r\nEnd of PVObject pvVCalcul\r\n")
        store = VariantStore(root, None)
        store._read = lambda path: (_ for _ in ()).throw(
            PermissionError(13, "The process cannot access the file", path.name))
        before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        result = prepare_block_project(store, "SOURCE.PRJ", "VC0", "RESULT.PRJ",
                                       [{"id": "roof", "source_subarray_id": 1, "count": 2}], dry_run=False)
        self.assertEqual(result["status"], "blocked", result)
        self.assertFalse(result["prepared"])
        self.assertIn("cannot access", result["issues"][0])
        self.assertEqual(sorted(str(p.relative_to(root)) for p in root.rglob("*")), before)

    def test_comparison_keeps_needs_review_and_blocks_unbound_handoff(self):
        root = self.workspace()
        store = type("Store", (), {"lock": threading.RLock(), "workspace": root})()
        scenarios = []
        for label, project, variant, values, changes in (("baseline", "Alpha.PRJ", "VC0", (1.5, 2.5), []),
                                                          ("candidate", "Beta.PRJ", "VC1", (2.0, 3.0), ["system"])):
            name = label + ".csv"
            (root / "Results" / name).write_text(CSV.format(
                project=project, stem=Path(project).stem, variant=variant, first=values[0], second=values[1]),
                encoding="utf-8", newline="\r\n")
            scenarios.append({"label": label, "project": project, "variant": variant,
                              "csv_name": name, "declared_changes": changes})
        with patch("pvsyst_auto.engineering.handoff.build_project_handoff",
                   side_effect=lambda *a, **k: json.loads(json.dumps(HANDOFF))):
            preview = compare_engineering_scenarios(store, objective="Compare two roofs", scenarios=scenarios,
                                                  output_name="cmp.json", dry_run=True)
            self.assertEqual((preview["status"], preview["created"]), ("needs_review", False))
            written = compare_engineering_scenarios(store, objective="Compare two roofs", scenarios=scenarios,
                                                  output_name="cmp.json", dry_run=False)
            self.assertEqual((written["status"], written["created"]), ("needs_review", True))
            self.assertTrue(written["review_items"])
            self.assertEqual(written["written_artifacts"], ["csv", "markdown", "json"])
            recorded = json.loads((root / "Results" / "cmp.json").read_text(encoding="utf-8"))
            self.assertEqual((recorded["status"], recorded["created"], recorded["written_artifacts"]),
                             ("needs_review", True, ["csv", "markdown", "json"]))

            manifest = {key: None for key in RUN_FIELDS}
            manifest["operation"] = "engineering_revision"
            (root / "Results" / "run0.json").write_text(json.dumps(manifest), encoding="utf-8")
            binding = {"operation": "engineering_revision", "status": "completed", "stage": "completed",
                       "year": 2024, "input_assets": {},
                       "input_handoff": {"status": "ready", "saved": {"project": {"values": {}}}}}
            with patch("pvsyst_auto.workflows.revision.load_engineering_run", return_value=binding):
                blocked = compare_engineering_scenarios(
                    store, objective="Compare two roofs",
                    scenarios=[dict(scenarios[0], run_manifest_name="run0.json"), scenarios[1]],
                    output_name="bound.json", dry_run=False)
            self.assertEqual((blocked["status"], blocked["created"]), ("blocked", False))
            self.assertTrue(blocked["issues"])
            self.assertFalse((root / "Results" / "bound.json").exists())

    def test_handoff_daily_profile_is_kept_and_hourly_cache_omitted(self):
        text = ("PVObject_UserNeeds=pvLoad\n  Comment=Daily\n  LoadType=DailyProfile\n"
                "  ValHour_1=1.000,2.000,3.000,6,\n  Day_1=1,2,3\nEnd of PVObject pvLoad\n")
        load = _load_records(objects(text))
        record = load["objects"][0]
        self.assertEqual(record["values"]["ValHour_1"], "1.000,2.000,3.000,6,")
        self.assertEqual(record["omitted_cache_fields"], ["Day_1"])
        self.assertFalse(load["hourly_cache_contents_included"])

    def test_handoff_uses_the_unique_hourly_source_not_nested_cache_fields(self):
        root = self.workspace()
        load_dir = root / "UserHourlyParams"
        load_dir.mkdir()
        (load_dir / "Load.csv").write_text("sample", encoding="utf-8")
        project = root / "Projects" / "Work.PRJ"
        project.write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        store = VariantStore(root, ComponentStore(root))
        cache = ("      ParSimulH=TVarSimul\n        Varident=ELoadIN\n"
                 "        Day_0101=+0+0\n      End of VarSimul=viELoadIN\n")
        source = f"      FileName=Load.csv\n      PathName={load_dir}\n"
        template = "    HourlyLoad=THourlyParams\n{}{}    End of THourlyParams\n"
        hourly = template.format(source, cache)
        decoy = "FileName=Decoy.csv\nPathName=C:\\Other\n"
        decoy_cache = cache.replace("        Day_", indent(decoy, "        ") + "        Day_")
        cases = (
            ("valid", hourly),
            ("nested decoy", template.format(source, decoy_cache)),
            ("missing source", indent(decoy, "    ") + template.format("", decoy_cache)),
            ("duplicate typed HourlyLoad", hourly + hourly.replace("Load.csv", "Decoy.csv")),
        )
        for label, body in cases:
            with self.subTest(case=label):
                text = grouped_variant().replace("End of PVObject pvVCalcul\n", (
                    f"  PVObject_UserNeeds=pvLoad\n{body}  End of PVObject pvLoad\nEnd of PVObject pvVCalcul\n"))
                project.with_suffix(".VC0").write_text(text, encoding="utf-8-sig")
                result = build_project_handoff(store, project=project.name, variant="VC0")
                if label == "duplicate typed HourlyLoad":
                    self.assertEqual(result["status"], "blocked", result)
                    self.assertIn("HourlyLoad", result["issues"][0])
                    self.assertEqual(result["external_dependencies"], [])
                    continue
                self.assertNotEqual(result["status"], "blocked", result)
                load, = result["saved"]["load"]["objects"]
                dep, = [item for item in result["external_dependencies"] if item["kind"] == "load"]
                has_source = label != "missing source"
                expected_name, expected_path = ("Load.csv", str(load_dir)) if has_source else (None, None)
                self.assertEqual((dep["name"], dep["saved_path"], dep["path_matches_workspace"]),
                                 (expected_name, expected_path, has_source))
                self.assertEqual(load["hourly_source"], {"file_name": expected_name, "saved_path": expected_path,
                    "workspace_folder": "UserHourlyParams", "workspace_file": has_source,
                    "runtime_binding_verified": False})
                values = {"HourlyLoad": "THourlyParams", "ParSimulH": "TVarSimul", "Varident": "ELoadIN"}
                if has_source:
                    values.update(FileName=expected_name, PathName=expected_path)
                self.assertEqual(load["values"], values)
                self.assertEqual(load["omitted_cache_fields"], ["Day_0101"])

    @unittest.skipUnless(os.name == "nt", "Windows path aliases require Windows")
    def test_handoff_recognizes_workspace_path_with_short_name_components(self):
        root = self.workspace()
        load_dir = root / "UserHourlyParams"
        other_dir = root / "Other"
        for folder in (load_dir, other_dir):
            folder.mkdir()
            (folder / "Load.csv").write_text("sample", encoding="utf-8")
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.windll.kernel32.GetShortPathNameW(str(load_dir), buf, len(buf))
        if not 0 < size < len(buf) or Path(buf.value) == load_dir:
            self.skipTest("The workspace directory has no NTFS short alias")
        short = Path(buf.value)
        mixed = short.parent / load_dir.name if short.parent != load_dir.parent else load_dir.parent / short.name
        project = root / "Projects" / "Work.PRJ"
        project.write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        store = VariantStore(root, ComponentStore(root))
        for directory, matches in ((load_dir, True), (short, True), (mixed, True), (other_dir, False)):
            with self.subTest(directory=directory):
                text = grouped_variant().replace("End of PVObject pvVCalcul\n", (
                    "  PVObject_UserNeeds=pvLoad\n    HourlyLoad=THourlyParams\n"
                    f"      FileName=Load.csv\n      PathName={directory}\n"
                    "    End of THourlyParams\n  End of PVObject pvLoad\nEnd of PVObject pvVCalcul\n"))
                project.with_suffix(".VC0").write_text(text, encoding="utf-8-sig")
                result = build_project_handoff(store, project=project.name, variant="VC0")
                self.assertNotEqual(result["status"], "blocked", result)
                load = next(item for item in result["external_dependencies"] if item["kind"] == "load")
                self.assertEqual(load["resolution"], "workspace")
                self.assertEqual(load["path_matches_workspace"], matches)
                self.assertEqual(any("PathName" in note for note in result["review_items"]), not matches)

    def test_discovery_rejects_float_inverter_count_and_allows_unbounded_tilt(self):
        root = self.workspace()
        components = FakeComponents(root, {"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        spec = template_spec()
        store = FakeVariants(root, components, {"Template": {"variants": ["VC0"], "VC0": spec}})
        with patch("pvsyst_auto.engineering.inputs.electrical_inventory",
                   side_effect=lambda kind, text: {"fields": {"source": text}}), \
                patch("pvsyst_auto.engineering.inputs.pan_validation_fields", return_value={}):
            for tilt in (95, "steep", float("nan")):
                with self.subTest(tilt=tilt):
                    result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                                    inverter_manufacturer="Lab", inverter_model="50kW",
                                                    template_project="Template.PRJ", variant="VC0", tilt=tilt)
                    self.assertEqual(result["status"], "ready", result)
                    self.assertFalse(any(item["field"] == "tilt" for item in result["issues"]))
            spec["parameters"]["subarrays"][0]["values"]["NInverter"] = "2.0"
            plan = {"status": "ready", "inverter_count": 2, "steps": [], "capacity_error_pct": 0.0, "issues": []}
            with patch("pvsyst_auto.engineering.inputs.plan_capacity", return_value=plan):
                rejected = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                                  inverter_manufacturer="Lab", inverter_model="50kW",
                                                  template_project="Template.PRJ", variant="VC0",
                                                  capacity={"target_dc_kw": 10.0})
            self.assertEqual(rejected["status"], "blocked", rejected)
            self.assertIsNone(rejected["selected"])
            template = rejected["candidates"]["templates"][0]
            self.assertFalse(template["valid"])
            self.assertIn("NInverter", "; ".join(template["reasons"]))

    def test_review_saved_angles_compare_within_editor_precision(self):
        root = self.workspace()
        for declared, saved, expected in ((30.0005, "30.0005", "matches"),
                                          (30, "29.99999999", "matches"),
                                          (30.0004, "30.000", "matches"),
                                          (30, "30.0006", "differs"),
                                          (30, "1e1000000", "differs")):
            with self.subTest(declared=declared, saved=saved):
                baseline = {"operation": "prepare_project", "status": "prepared", "prepared": True,
                            "simulated": False, "metrics": None, "working_project": "Work.PRJ",
                            "working_variant": "VC0",
                            "request": {"year": 2024, "new_project": "Work.PRJ", "variant": "VC0",
                                        "pan_name": "Module.PAN", "ond_name": "Inverter.OND",
                                        "site_name": "Site.SIT", "met_name": "Weather.MET",
                                        "tilt": declared, "azimuth": 0.0},
                            "preparation_context": {"request": {}, "review_required": False},
                            "capacity_plan": {"status": "ready", "series_modules": 10,
                                              "string_count": 2, "inverter_count": 1}}
                (root / "Results" / "Work_prepare_2024.json").write_text(json.dumps(baseline), encoding="utf-8")
                store = ReviewStore(root, {"FieldTilt": saved, "FieldAzim": "0.000"})
                result = review_project_inputs(store, project="Work.PRJ", variant="VC0",
                                               preparation_manifest_name="Work_prepare_2024.json")
                rows = {row["field"]: row for row in result["comparisons"]}
                self.assertEqual((rows["tilt"]["status"], rows["azimuth"]["status"]), (expected, "matches"))
                self.assertEqual(result["status"], "reviewed" if expected == "matches" else "needs_review")

    def test_sourced_house_blocks_malformed_inputs_and_incomplete_resume_weather(self):
        root = self.workspace()
        (root / "Components").mkdir()
        for name in ("module.PAN", "inverter.OND"):
            (root / "Components" / name).write_bytes(b"component")
        (root / "Sites" / "Site.SIT").write_bytes(b"site")
        (root / "Meteo" / "Weather.MET").write_bytes(b"met")
        components = type("Components", (), {"builtin": None})()
        components._path = lambda kind, name, library="workspace": root / "Components" / name
        store = type("Store", (), {"lock": threading.RLock(), "workspace": root, "components": components})()
        store._path = lambda project, variant=None: root / "Projects" / project
        store.inspect_project = lambda project: {"files": {"Source.PRJ": "1", "Source.VC0": "2"}}
        design = {"new_project": "Source.PRJ", "year": 2024, "tilt": 20, "azimuth": 0,
                  "module": {"file_name": "module.PAN"}, "inverter": {"file_name": "inverter.OND"},
                  "weather": {"mode": "existing", "sit_name": "Site.SIT", "met_name": "Weather.MET",
                              "weather_manifest_name": "Weather_weather.json"}}
        binding = {"status": "ready", "needs_review": False, "electrical_design": {"target_dc_kw": 10.0}}

        def run_task(request=None, custom_binding=None, **flags):
            req = request if request is not None else {"design": dict(design), "new_project": "Final.PRJ",
                                                       "variant_name": "House", "house": {}, "layout": {}}
            return run_sourced_house_task(store, None, None, request=req,
                                          binding=binding if custom_binding is None else custom_binding, **flags)

        for req, custom_binding in ((None, "not-a-binding"),
                                    ({"design": "not-a-design", "new_project": "Final.PRJ"}, None),
                                    ({"design": dict(design, module="module.PAN"), "new_project": "Final.PRJ"}, None)):
            with self.subTest(request=req, binding=custom_binding):
                blocked = run_task(req, custom_binding)
                self.assertEqual(blocked["status"], "blocked", blocked)
                self.assertTrue(blocked["issues"])

        pruned = _prune_empty(dict(design, target_dc_kw=10.0, electrical_design=binding["electrical_design"]))
        checkpoint = {"operation": "prepare_project", "status": "prepared", "prepared": True, "simulated": False,
                      "native_consumption_verified": False, "metrics": None, "working_project": "Source.PRJ",
                      "working_variant": "VC0", "request": {"variant": "VC0"},
                      "working_files": {"Source.PRJ": "1", "Source.VC0": "2"},
                      "preparation_context": {"request": pruned, "source_binding": binding}}
        (root / "Results" / "Source_prepare_2024.json").write_text(json.dumps(checkpoint), encoding="utf-8")
        for error in (AttributeError("'NoneType' object has no attribute 'casefold'"), KeyError("created_files")):
            with self.subTest(error=type(error).__name__):
                with patch("pvsyst_auto.workflows.sourced_house.load_weather_manifest", side_effect=error):
                    resumed = run_task(resume=True)
                self.assertEqual(resumed["status"], "blocked", resumed)
                self.assertEqual(resumed["issues"], ["Incomplete weather preparation manifest: Weather_weather.json"])
                self.assertFalse(resumed["native_consumption_verified"])

    def test_grouped_hardware_six_decimal_angles_and_rejection_reasons(self):
        source = {"orientations": [{"id": p["id"], "module_total": 18, "tilt_deg": p["tilt"],
                                    "field_azimuth_deg": p["azimuth"]} for p in PLANES]}
        native_tables = (("30.123457", "-12.300000"), ("20.000000", "90.000000"))
        baseline = grouped_variant()

        def verify(saved):
            return verify_grouped_hardware(objects(baseline), objects(saved),
                                           baseline.splitlines(True), saved.splitlines(True), source)

        for label, saved in (("re-serialized plane", grouped_variant(tilt_text="30.123457", tables=native_tables)),
                             ("unchanged plane", grouped_variant(tables=native_tables))):
            with self.subTest(label):
                result = verify(saved)
                self.assertTrue(result["source_orientations_preserved"])
                self.assertEqual([row["orientation_id"] for row in result["orientation_mapping"]], [1, 2])
                self.assertEqual(len(result["circuit_mapping"]), 2)

        moved = (("30.2", "-12.300000"), native_tables[1])
        for pattern, saved in (("changed FieldTilt", grouped_variant(tilt_text="30.2", tables=native_tables)),
                               ("Native PV table differs", grouped_variant(tables=moved)),
                               ("changed its inverter input power declaration",
                                grouped_variant(tables=native_tables, extra="    PNomAC_InMPPT=5000\n"))):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValueError, pattern):
                    verify(saved)

    def test_source_profile_hemisphere_and_latitude_bounds(self):
        root = self.workspace()
        store = VariantStore(root, ComponentStore(root))

        def profile(latitude):
            text = SITE_VARIANT.format(latitude=latitude, branch=BRANCH.format(**PLANES[0]),
                                       array=ARRAY.format(extra="", **PLANES[0]))
            _, lines = store._text(text.replace("\n", "\r\n").encode("utf-8"))
            arrays, system = store._sections(lines)
            return source_profile(store, lines, arrays, system)

        self.assertEqual(profile("45.5")["site"]["latitude"], 45.5)
        for latitude, pattern in (("-33.9", "northern hemisphere"), ("-91", "Latitude must be >= -90")):
            with self.subTest(latitude=latitude):
                with self.assertRaisesRegex(ValueError, pattern):
                    profile(latitude)

    def test_preserve_source_hardware_checks_trial_exhaustion_in_preflight(self):
        root = self.workspace()
        store = VariantStore(root, ComponentStore(root))
        exhausted_cli = FakeCliBase(version="8.1.7", license_status="TRIAL", remaining_executions=0)

        req = {
            "prepare_mode": "preserve_source_hardware",
            "source_project": "Src.PRJ",
            "source_variant": "VC0",
            "new_project": "Out.PRJ",
            "variant_name": "Preserved",
            "design": {"year": 2024},
            "house": {"kind": "mesh", "meshes": [{"name": "r", "vertices": [[0, 0, 0], [4, 0, 0], [0, 4, 0]], "triangles": [[0, 1, 2]]}]},
            "layout": {"module_rows": 1, "module_columns": 1, "orientation": "landscape",
                       "row_spacing_m": 0.0, "column_spacing_m": 0.0, "origin_m": [0.0, 0.0, 0.0], "roof_mesh": "r"},
        }
        res = run_sourced_house_task(store, store.components, exhausted_cli, request=req, dry_run=True)
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["stage"], "native_preflight")
        self.assertIn("no remaining trial executions", res["issues"][0])

    def test_preserve_source_hardware_resume_only_checks_quota_for_new_attempts(self):
        request = {
            "prepare_mode": "preserve_source_hardware", "source_project": "Src.PRJ",
            "source_variant": "VC0", "new_project": "Out.PRJ", "variant_name": "Preserved",
            "design": {"year": 2024}, "report_pdf": False, "allow_window_creation": False,
            "house": {"kind": "mesh", "meshes": [{"name": "r", "vertices": [[0, 0, 0], [4, 0, 0], [0, 4, 0]],
                                                   "triangles": [[0, 1, 2]]}]},
            "layout": {"module_rows": 1, "module_columns": 1, "orientation": "landscape",
                       "row_spacing_m": 0.0, "column_spacing_m": 0.0, "origin_m": [0.0, 0.0, 0.0], "roof_mesh": "r"},
        }
        plan = {"scene_name": "house.pvc2", "obstacles": [], "house_parts": [], "pv_tables": [],
                "source": {"module_total": 1}, "shading_mode": "linear"}
        scene = {"status": "ready", "create_arguments": {"project": "Out.PRJ", "variant": "VC0",
                 "house": house_profile(request["house"]), "layout": request["layout"]}}
        for phase, corrupt in (("simulated", False), ("completed", False),
                               ("completed", True), ("native_saved", False)):
            with self.subTest(phase=phase, corrupt=corrupt):
                current = {**request, "allow_window_creation": phase == "native_saved"}
                child = {key: value for key, value in current.items() if key not in ("prepare_mode", "design")}
                child["year"] = 2024
                root = self.workspace()
                store = VariantStore(root, ComponentStore(root))
                cli = Mock()
                cli.version.return_value = "8.1.7"
                cli.license_info.return_value = {"status": "TRIAL", "remaining_executions": 0}
                cli._redact.side_effect = str
                cli.run_simulation.side_effect = AssertionError("No new simulation should run")
                paths = {"working_project": root / "Projects" / "Out.PRJ",
                         "working_variant": root / "Projects" / "Out.VC0",
                         "native_variant": root / "Projects" / "Out.VC1",
                         "scene": root / "house.pvc2", "sfi": root / "house.SFI"}
                for key, path in paths.items():
                    path.write_text(key, encoding="utf-8")
                guards = {key: fingerprint(path) for key, path in paths.items()}
                manifest = paths["manifest"] = root / "Results" / "Out_house_task.json"
                output = root / "Results" / "Out_house_run01.csv"
                record = {"schema": 1, "request": child, "phase": phase,
                          "status": "completed" if phase == "completed" else "needs_attention",
                          "issues": [], "guards": guards, "artifacts": {}, "warnings": [], "variant": "VC1",
                          "runs": [{"number": 1, "status": "running", "csv": output.name}]}
                if phase != "native_saved":
                    output.write_text("recorded output", encoding="utf-8")
                    record["runs"][0].update(status="returned", native={"warnings": []})
                    record["output_guards"] = {"csv": fingerprint(output)}
                manifest.write_text(json.dumps(record), encoding="utf-8")
                before = manifest.read_bytes()
                if corrupt:
                    output.write_text("changed output", encoding="utf-8")
                with patch.object(house_flow, "_prepare", return_value=(paths, plan)), \
                        patch.object(house_flow, "read_house_pv_scene", return_value=scene), \
                        patch.object(house_flow, "_verify_new"), \
                        patch.object(house_flow, "_annual", return_value={"metrics": {"diagnostic_flags": []}}) as annual:
                    result = run_sourced_house_task(store, store.components, cli, request=current, dry_run=False,
                                                    confirm=True, resume=True, retry_simulation=phase == "native_saved")
                cli.run_simulation.assert_not_called()
                self.assertIsNotNone(result["house"], result)
                self.assertEqual(result["house"]["runs"], record["runs"], result)
                if phase == "native_saved":
                    self.assertEqual(result["status"], "needs_attention", result)
                    self.assertIn("no remaining trial executions", result["house"]["issues"][0])
                    cli.version.assert_called_once_with(allow_window_creation=True)
                    cli.license_info.assert_called_once_with(allow_window_creation=True)
                    annual.assert_not_called()
                else:
                    cli.version.assert_not_called()
                    cli.license_info.assert_not_called()
                    if phase == "completed":
                        self.assertEqual(manifest.read_bytes(), before)
                    else:
                        self.assertEqual(json.loads(manifest.read_bytes())["phase"], "completed")
                    if corrupt:
                        self.assertFalse(result["native_consumption_verified"])
                        self.assertIn("Recorded native outputs changed", result["house"]["issues"][0])
                        annual.assert_not_called()
                    else:
                        self.assertEqual((result["status"], result["stage"]), ("completed", "completed"), result)
                        self.assertTrue(result["native_consumption_verified"])
                        annual.assert_called_once_with(output, output.with_suffix(".pdf"), child, plan, "VC1", require_pdf=False)


if __name__ == "__main__":
    unittest.main()

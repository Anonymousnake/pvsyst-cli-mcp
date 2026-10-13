"""Contract tests for read-only design input discovery.

Component and variant stores are faked at their boundaries: no real component parse, no
hashing, no native simulation, and no PVsyst installation.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_design_inputs import SELECTION_RULE, discover_design_inputs

GRID = {"valid": True, "system_type": "Grid", "issues": [], "unchecked_checks": []}


def template_spec(*, subarrays=1, planes=1, grid=True, prcorr=True, geometry=None):
    parameters = {"subarrays": [{"id": 1, "values": {"NoOrientation": "1", "NModSerie": "12",
                                                     "NStringCh": "6", "NInvMPPT": "1", "NInverter": "2"}}] * subarrays,
                  "orientations": [{"id": 1, "values": {"FieldTilt": "20"}}] * planes}
    structure = GRID if grid else {"valid": False, "system_type": "Grid", "issues": ["counts differ"],
                                   "unchecked_checks": []}
    return {"parameters": parameters, "structure": structure, "prcorr": [3] if prcorr else [], "geometry": geometry}


class FakeComponents:
    """Loose-file listing and component access without parsing or hashing real files."""

    def __init__(self, root, workspace, installed=None, builtin=None, unusable=()):
        self.root = root
        self.files = {"workspace": dict(workspace), "builtin": dict(installed or {})}
        self.builtin = builtin
        self.unusable = set(unusable)
        for library, entries in self.files.items():
            (root / library).mkdir(parents=True, exist_ok=True)
            for name in entries:
                (root / library / name).write_bytes(b"")

    def list(self, kind, library, query="", offset=0, limit=200):
        if library == "builtin" and self.builtin is None:
            raise ValueError("Installed component library is not configured")
        items = [{"name": name, "format": "text", "manufacturer": manufacturer, "model": model}
                 for name, (manufacturer, model) in sorted(self.files[library].items())
                 if query.casefold() in f"{name} {manufacturer} {model}".casefold()]
        return {"total": len(items), "offset": offset, "items": items[offset:offset + limit]}

    def _path(self, kind, name, library="workspace"):
        if library == "builtin" and self.builtin is None:
            raise ValueError("Installed component library is not configured")
        return self.root / library / name

    def _read(self, path):
        if path.name in self.unusable:
            raise ValueError("Component object type or format does not match extension")
        return f"{path.parent.name}/{path.name}".encode()

    def _inspect(self, kind, raw):
        name = raw.decode().split("/")[-1]
        for library in ("workspace", "builtin"):
            if name in self.files[library]:
                manufacturer, model = self.files[library][name]
                return {"format": "text", "manufacturer": manufacturer, "model": model,
                        "structural_errors": [], "warnings": []}
        raise ValueError("Unknown component")

    def _require_valid(self, info, complete=False):
        if info["structural_errors"] or info["warnings"]:
            raise ValueError("Invalid component")


class FakeVariants:
    """Per-variant gate results, including injected failures, without reading real files."""

    def __init__(self, root, components, projects):
        self.root = root
        self.components = components
        self.projects = projects
        self.current = ("Template", "VC0")

    @staticmethod
    def _base(project):
        return project[:-4] if project.upper().endswith(".PRJ") else project

    def _root(self):
        return self.root / "Projects"

    def _spec(self, project, variant):
        return self.projects[self._base(project)][variant]

    def inspect_project(self, project):
        spec = self.projects[self._base(project)]
        if isinstance(spec, str):
            raise ValueError(spec)
        return {"project": project, "variants": list(spec["variants"]),
                "files": {}, "sources": {}, "warnings": []}

    def inspect_parameters(self, project, variant):
        return self._spec(project, variant)["parameters"]

    def validate_structure(self, project, variant):
        return self._spec(project, variant)["structure"]

    def _path(self, project, variant=None):
        base = self._base(project)
        return self.root / "Projects" / (base if variant is None else f"{base}.{variant}")

    def _read(self, path):
        self.current = path.name.rsplit(".", 1)
        return path.name.encode()

    def _text(self, data):
        self.current = data.decode().rsplit(".", 1)
        return "", ["PVObject_=pvVCalcul\n", "  Version=8.1.6\n", "PVObject_=pvOrient\n", "  PRCorrTemp=False\n"]

    def _field_indices(self, lines, bounds, key):
        return list(self.projects[self.current[0]][self.current[1]]["prcorr"]) if key == "PRCorrTemp" else []

    def _require_no_geometry(self, lines, require_shading_section=False):
        if error := self.projects[self.current[0]][self.current[1]].get("geometry"):
            raise ValueError(error)

    def _ordinary_fixed_plane(self, lines, bounds, allow_missing_flags=False):
        return None

    def _orientation(self, lines, number):
        return (2, 3)


class DesignInputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "Projects").mkdir()
        self.electrical = patch("pvsyst_design_inputs.electrical_inventory",
                                side_effect=lambda kind, text: {"fields": {"source": text}})
        self.parameters = patch("pvsyst_design_inputs.pan_validation_fields",
                                return_value={"Technol": ["mtSiMono"], "GRef": ["1000"], "TRef": ["25"]})

    def components(self, workspace, installed=None, builtin=None, unusable=()):
        return FakeComponents(self.root, workspace, installed, builtin, unusable)

    def variants(self, projects, components):
        return FakeVariants(self.root, components, projects)

    def projects(self, store, names):
        for name in names:
            (self.root / "Projects" / f"{name}.PRJ").write_bytes(b"")
        return store

    def test_exact_identity_binds_selected_files_and_electrical(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        store = self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}}, components)
        with self.electrical:
            result = discover_design_inputs(store, module_manufacturer="lab", module_model="440w",
                                            inverter_manufacturer="LAB", inverter_model="50KW",
                                            template_project="Template.PRJ", variant="VC0")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["selection_rule"], SELECTION_RULE)
        self.assertEqual(result["selected"]["PAN"]["name"], "module.PAN")
        self.assertEqual(result["selected"]["PAN"]["library"], "workspace")
        self.assertEqual((result["selected"]["PAN"]["manufacturer"], result["selected"]["PAN"]["model"]),
                         ("Lab", "440W"))
        self.assertEqual(result["selected"]["PAN"]["electrical"]["fields"]["source"], "workspace/module.PAN")
        self.assertEqual(result["selected"]["OND"]["electrical"]["fields"]["source"], "workspace/inverter.OND")
        self.assertEqual(result["selected"]["template"]["subarray_id"], 1)
        self.assertNotIn("_text", result["selected"]["PAN"])

    def test_resolved_components_survive_a_missing_template(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        store = self.variants({}, components)  # empty workspace: no PRJ member at all
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        with self.electrical:
            result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                            inverter_manufacturer="Lab", inverter_model="50kW")
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["selected"])
        self.assertEqual(result["candidates"]["templates"], [])
        self.assertEqual(result["resolved_components"]["PAN"]["name"], "module.PAN")
        self.assertEqual(result["resolved_components"]["OND"]["name"], "inverter.OND")
        self.assertEqual(result["resolved_components"]["PAN"]["electrical"]["fields"]["source"],
                         "workspace/module.PAN")
        message = next(item["message"] for item in result["issues"] if item["field"] == "template_project")
        self.assertIn("No readable workspace PRJ", message)
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")), before)

    def test_duplicate_missing_and_unusable_identity_block_specifically(self):
        entries = {"a.PAN": ("Lab", "440W"), "b.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")}
        store = self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}},
                              self.components(entries))
        with self.electrical:
            ambiguous = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                               inverter_manufacturer="Lab", inverter_model="50kW",
                                               template_project="Template.PRJ", variant="VC0")
        self.assertEqual(ambiguous["status"], "blocked")
        self.assertIsNone(ambiguous["selected"])
        self.assertIsNone(ambiguous["resolved_components"])
        message = next(item["message"] for item in ambiguous["issues"] if item["field"] == "PAN.name")
        self.assertIn("2 usable workspace files", message)
        self.assertEqual([item["name"] for item in ambiguous["candidates"]["PAN"]], ["a.PAN", "b.PAN"])
        self.assertTrue(all(item["valid"] for item in ambiguous["candidates"]["PAN"]))

        missing = discover_design_inputs(store, module_manufacturer="Other", module_model="1W",
                                         inverter_manufacturer="Lab", inverter_model="50kW",
                                         template_project="Template.PRJ", variant="VC0")
        self.assertEqual(missing["status"], "blocked")
        message = next(item["message"] for item in missing["issues"] if item["field"] == "PAN.name")
        self.assertIn("No loose file matches Other 1W", message)
        self.assertIn("encrypted built-in database cannot be enumerated", message)
        self.assertEqual(missing["candidates"]["PAN"], [])
        self.assertIsNone(missing["resolved_components"])

        broken = self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}},
                               self.components(entries, unusable={"a.PAN", "b.PAN"}))
        unusable = discover_design_inputs(broken, module_manufacturer="Lab", module_model="440W",
                                          inverter_manufacturer="Lab", inverter_model="50kW",
                                          template_project="Template.PRJ", variant="VC0")
        self.assertEqual(unusable["status"], "blocked")
        message = next(item["message"] for item in unusable["issues"] if item["field"] == "PAN.name")
        self.assertIn("No usable component", message)
        self.assertIn("does not match extension", message)

    def test_installed_selection_keeps_identity_under_workspace_shadow(self):
        components = self.components({"other.PAN": ("Lab", "500W"), "inverter.OND": ("Lab", "50kW")},
                                     installed={"installed.PAN": ("Lab", "440W")},
                                     builtin=str(self.root / "installed"))
        store = self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}}, components)
        with self.electrical:
            selected = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                              inverter_manufacturer="Lab", inverter_model="50kW",
                                              template_project="Template.PRJ", variant="VC0")
        self.assertEqual(selected["status"], "ready", selected)
        self.assertEqual((selected["selected"]["PAN"]["name"], selected["selected"]["PAN"]["library"]),
                         ("installed.PAN", "builtin"))
        self.assertEqual(selected["selected"]["PAN"]["electrical"]["fields"]["source"], "builtin/installed.PAN")

        components.files["workspace"]["installed.PAN"] = ("Lab", "300W")
        (self.root / "workspace" / "installed.PAN").write_bytes(b"")
        shadowed = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                          inverter_manufacturer="Lab", inverter_model="50kW",
                                          template_project="Template.PRJ", variant="VC0")
        self.assertEqual(shadowed["status"], "blocked")
        self.assertIsNone(shadowed["selected"])
        message = next(item["message"] for item in shadowed["issues"] if item["field"] == "PAN.name")
        self.assertIn("No usable component", message)
        self.assertIn("Shadowed by a workspace file", message)
        self.assertTrue(any("shadows the installed-library match" in warning for warning in shadowed["warnings"]))
        self.assertEqual([item["library"] for item in shadowed["candidates"]["PAN"]], ["builtin"])
        self.assertFalse(shadowed["candidates"]["PAN"][0]["valid"])

    def test_workspace_identity_wins_over_shadowed_identical_installed_file(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")},
                                     installed={"module.PAN": ("Lab", "440W"),
                                                "inverter.OND": ("Lab", "50kW")},
                                     builtin=str(self.root / "installed"))
        store = self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}}, components)
        with self.electrical:
            result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                            inverter_manufacturer="Lab", inverter_model="50kW",
                                            template_project="Template.PRJ", variant="VC0")
        # An unusable installed copy of the same name must not block the usable workspace identity.
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["selected"]["PAN"]["library"], "workspace")
        self.assertEqual(result["selected"]["OND"]["library"], "workspace")
        self.assertEqual([(item["library"], item["valid"]) for item in result["candidates"]["PAN"]],
                         [("workspace", True), ("builtin", False)])

    def test_template_gates_report_reasons_and_select_stably(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        projects = {
            "Alpha": {"variants": ["VC0"], "VC0": template_spec()},
            "Other": {"variants": ["VC0", "VC1", "VC2", "VC3"],
                      "VC0": template_spec(),
                      "VC1": template_spec(subarrays=2),
                      "VC2": template_spec(prcorr=False),
                      "VC3": template_spec(geometry="Operation has an active shading scene")},
            "Template": {"variants": ["VC0"], "VC0": template_spec(grid=False)},
        }
        store = self.projects(self.variants(projects, components), projects)
        with self.electrical:
            result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                            inverter_manufacturer="Lab", inverter_model="50kW")
        self.assertEqual(result["status"], "ready", result)
        keys = [f"{item['project']}/{item['variant']}" for item in result["candidates"]["templates"]]
        self.assertEqual(keys, ["Alpha.PRJ/VC0", "Other.PRJ/VC0", "Other.PRJ/VC1", "Other.PRJ/VC2",
                                "Other.PRJ/VC3", "Template.PRJ/VC0"])
        rejected = {key: item["reasons"] for key, item in zip(keys, result["candidates"]["templates"])
                    if not item["valid"]}
        self.assertIn("exactly one subarray and one fixed plane", rejected["Other.PRJ/VC1"][0])
        self.assertIn("PRCorrTemp=False", rejected["Other.PRJ/VC2"][0])
        self.assertIn("active shading scene", rejected["Other.PRJ/VC3"][0])
        self.assertIn("completely checked Grid template", rejected["Template.PRJ/VC0"][0])
        self.assertTrue(any(warning.startswith("Other.PRJ/VC1 is not eligible") for warning in result["warnings"]))
        self.assertEqual(result["selected"]["template"]["project"], "Alpha.PRJ")
        self.assertIn("stable project/variant name", result["selection_rule"])

    def test_broken_projects_and_missing_variants_do_not_block_eligible_templates(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        projects = {"Alpha": {"variants": ["VC0", "VC9"], "VC0": template_spec(), "VC9": template_spec()},
                    "Broken": "Unrecognized project sidecar: Broken.VC0",
                    "Other": {"variants": ["VC0"], "VC0": template_spec()}}
        store = self.projects(self.variants(projects, components), projects)
        with self.electrical:
            scanned = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                             inverter_manufacturer="Lab", inverter_model="50kW")
        self.assertEqual(scanned["status"], "ready", scanned)
        self.assertEqual(scanned["issues"], [])
        candidates = {f"{item['project']}/{item['variant']}": item
                      for item in scanned["candidates"]["templates"]}
        self.assertIn("Project is not readable", candidates["Broken.PRJ/"]["reasons"][0])
        self.assertTrue(any(warning.startswith("Broken.PRJ/ is not eligible") for warning in scanned["warnings"]))

        with self.electrical:
            mismatch = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                              inverter_manufacturer="Lab", inverter_model="50kW",
                                              variant="VC9")
        # Only Alpha declares VC9; the missing variant elsewhere must not block it.
        self.assertEqual(mismatch["status"], "ready", mismatch)
        self.assertEqual(mismatch["issues"], [])
        self.assertEqual(mismatch["selected"]["template"]["project"], "Alpha.PRJ")
        rejected = {f"{item['project']}/{item['variant']}": item["reasons"]
                    for item in mismatch["candidates"]["templates"] if not item["valid"]}
        self.assertIn("Variant VC9 is missing from this project", rejected["Other.PRJ/VC9"][0])

    def test_unusable_scan_or_explicit_broken_project_blocks_with_reason(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        store = self.projects(self.variants({"Broken": "Unrecognized project sidecar: Broken.VC0"}, components),
                              {"Broken"})
        with self.electrical:
            scanned = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                             inverter_manufacturer="Lab", inverter_model="50kW")
        self.assertEqual(scanned["status"], "blocked")
        self.assertIsNone(scanned["selected"])
        message = next(item["message"] for item in scanned["issues"] if item["field"].startswith("template "))
        self.assertIn("Unrecognized project sidecar", message)

        explicit = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                          inverter_manufacturer="Lab", inverter_model="50kW",
                                          template_project="Broken.PRJ", variant="VC0")
        self.assertEqual(explicit["status"], "blocked")
        self.assertEqual(explicit["candidates"]["templates"], [])
        message = next(item["message"] for item in explicit["issues"] if item["field"] == "template_project")
        self.assertIn("Unrecognized project sidecar", message)

    def test_capacity_plan_gates_and_orders_candidates(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        projects = {"Far": {"variants": ["VC0"], "VC0": template_spec()},
                    "Near": {"variants": ["VC0"], "VC0": template_spec()}}
        store = self.projects(self.variants(projects, components), projects)
        capacity = {"target_dc_kw": 28.16, "voltage_standard": "IEC", "design_basis": "declared envelope"}
        plans = [{"status": "ready", "inverter_count": 5, "steps": [], "capacity_error_pct": 0.0, "issues": []},
                 {"status": "ready", "inverter_count": 2, "steps": [{"NStringCh": 4}],
                  "capacity_error_pct": 0.0, "issues": []}]
        with self.electrical, self.parameters, patch("pvsyst_design_inputs.plan_capacity", side_effect=plans):
            result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                            inverter_manufacturer="Lab", inverter_model="50kW",
                                            capacity=capacity)
        # Far is listed first, but the template closest to the planned inverter count wins.
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["selected"]["template"]["project"], "Near.PRJ")
        self.assertEqual(result["selected"]["template"]["capacity_plan"]["inverter_count"], 2)
        self.assertEqual(result["candidates"]["templates"][0]["project"], "Far.PRJ")

        blocked_plan = {"status": "blocked", "issues": [{"field": "capacity.target_dc_kw",
                                                         "message": "No supported integral plan"}]}
        with self.electrical, self.parameters, patch("pvsyst_design_inputs.plan_capacity", return_value=blocked_plan):
            blocked = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                             inverter_manufacturer="Lab", inverter_model="50kW",
                                             capacity=capacity)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIsNone(blocked["selected"])
        self.assertEqual([item["valid"] for item in blocked["candidates"]["templates"]], [False, False])
        message = next(item["message"] for item in blocked["issues"] if item["field"].startswith("template "))
        self.assertIn("No supported integral plan", message)

    def test_discovery_writes_nothing(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        store = self.projects(self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}},
                                            components), {"Template"})
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        with self.electrical:
            result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W",
                                            inverter_manufacturer="Lab", inverter_model="50kW")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()

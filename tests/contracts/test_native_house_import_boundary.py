"""Public stdio-MCP contract for the ``method="native"`` headless house/PV import.

Everything here talks to ``pvsyst_import_house_pv_scene`` / ``pvsyst_get_variant_parameters``
through a real MCP stdio session; no internal builder/scene helper is imported, so an
internal refactor that keeps behaviour does not break these tests. The contract pinned is
the unified native contract:

- dry_run builds and verifies the native records in memory and writes nothing; the
  STORED flags stay false while the preflight status reports a clean pass;
- execute needs dry_run=false AND confirm=true;
- after execution the new variant is only STORED: imported / native_consumption_verified /
  native_geometry_verified / native_module_layout_verified stay false (PVsyst has not
  loaded or consumed it), stored_geometry_verified becomes true, and
  stored_module_layout_verified only for the module_layout mode that really wrote a layout;
- linear shading Flags=$20000100; module_layout Flags=$20100900 with a matching layout,
  read back through the public get_variant_parameters surface;
- the signed source FieldAzim (-45) is written as -45, never 315 (negative fixture);
- the source variant/PAN stay byte-identical and unknown scenes reject without writing.

A second method pins the single-orientation linear extension: one fixed source plane with
one sub-array and 1..3 landscape tables is imported as linear ($20000100), each table keeps
NoOrient=1 and the 36 source modules, while a single-orientation module_layout or more than
three tables is refused before any variant is written. The same method pins the high-level
boundary: ``pvsyst_run_house_task`` keeps the released two-orientation profile, so a legal
single-orientation spec is refused at preflight (two orientations required) with no write.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "pvsyst_mcp_server.py"
IMPORT_TOOL = "pvsyst_import_house_pv_scene"
READ_TOOL = "pvsyst_get_variant_parameters"
READ_SCENE_TOOL = "pvsyst_read_house_pv_scene"
RUN_HOUSE_TOOL = "pvsyst_run_house_task"
SCENE = "Grouped_Roof_Native_Source.pvc2"
PROJECT = "GROUPED_ROOF_NATIVE.PRJ"
SOURCE_VARIANT = "VC1"

# Single source orientation (one fixed plane, one sub-array, 36 modules = 18 x 2 strings).
# The private fixture supplies ROOF_SOURCE.PRJ/VC0 plus 1/2/3/4 named-table declarations.
SINGLE_PROJECT = "ROOF_SOURCE.PRJ"
SINGLE_SOURCE_VARIANT = "VC0"
# The written PAN name is the module's commercial Model field, not the file stem:
# ComposPV/PVmodules/Public_JKM600N_78HL4_BDV.PAN has Model=JKM600N-78HL4-BDV.
SINGLE_MODULE = "JKM600N-78HL4-BDV"
SINGLE_MODULE_TOTAL = 36
SINGLE_LINEAR_SCENES = (
    ("Single_Orient_One_Table.pvc2", 1),
    ("Single_Orient_Two_Tables.pvc2", 2),
    ("Single_Orient_Three_Tables.pvc2", 3),
)
SINGLE_OVER_LIMIT_SCENE = "Single_Orient_Four_Tables.pvc2"

# The four fields that describe true native loader/engine consumption. They are false for
# this headless adapter no matter what, because no PVsyst run happens here.
NOT_LOADED = ("imported", "native_consumption_verified",
              "native_geometry_verified", "native_module_layout_verified")


def _require_fixture(name):
    value = os.environ.get(name)
    if not value:
        raise unittest.SkipTest("Set %s to a PVsyst 8.1.6 data root" % name)
    return Path(value)


def _payload(response):
    if response.is_error:
        raise AssertionError(response.content)
    return json.loads("\n".join(item.text for item in response.content if hasattr(item, "text")))


class _Workspace:
    """System-temp copy of a fixture root; the fixture itself stays untouched."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.root = Path(tempfile.mkdtemp(prefix="pvsyst-native-house-"))
        for folder in ("Projects", "Models", "UserData", "Results"):
            (self.root / folder).mkdir()
        shutil.copytree(fixture / "ComposPV", self.root / "ComposPV")
        self.cli = self.root / "PVsystCLI.exe"
        self.cli.write_bytes(b"Not an executable")

    def copy(self, *relative):
        for item in relative:
            target = self.root / item
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.fixture / item, target)

    def projects(self):
        return {path.name: path.read_bytes() for path in (self.root / "Projects").iterdir()}

    def params(self):
        return StdioServerParameters(command=sys.executable, args=[str(SERVER)], cwd=str(REPO),
                                     env=dict(os.environ, PVSYST_WORKSPACE=str(self.root),
                                              PVSYST_CLI=str(self.cli)))

    def cleanup(self, ok):
        if ok:
            shutil.rmtree(self.root)
        else:
            print("Retained native import failure fixture: " + str(self.root), file=sys.stderr)


class NativeHouseImportBoundary(unittest.TestCase):
    def assert_not_native_loaded(self, result):
        for key in NOT_LOADED:
            self.assertFalse(result[key], "%s must stay false (native not loaded): %r" % (key, result))

    def test_dry_run_create_and_reject(self):
        fixture = _require_fixture("PVSYST_NATIVE_HOUSE_FIXTURE")
        asyncio.run(self._dry_run_create_and_reject(fixture))

    async def _dry_run_create_and_reject(self, fixture):
        ws = _Workspace(fixture)
        ws.copy(Path("Projects") / PROJECT,
                Path("Projects") / ("GROUPED_ROOF_NATIVE." + SOURCE_VARIANT),
                Path("UserData") / SCENE)
        originals = ws.projects()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(**extra):
                        args = dict({"scene_name": SCENE, "variant_name": "Native scene",
                                     "method": "native"}, **extra)
                        return _payload(await session.call_tool(IMPORT_TOOL, args))

                    async def flags(project, variant):
                        value = _payload(await session.call_tool(
                            READ_TOOL, {"project": project, "variant": variant}))
                        return value["shading_scene"]

                    # dry run (linear): clean preflight, nothing written, stored flags false.
                    dry = await call(dry_run=True)
                    self.assertEqual(dry["status"], "ready_to_import", dry)
                    self.assert_not_native_loaded(dry)
                    self.assertFalse(dry["stored_geometry_verified"], dry)
                    self.assertFalse(dry["stored_module_layout_verified"], dry)
                    self.assertEqual({p.name for p in (ws.root / "Projects").iterdir()}, set(originals))

                    # dry run (module_layout): still a clean preflight through the same gate.
                    dry_ml = await call(dry_run=True, shading_mode="module_layout")
                    self.assertEqual(dry_ml["status"], "ready_to_import", dry_ml)
                    self.assertFalse(dry_ml["stored_geometry_verified"], dry_ml)
                    self.assertFalse(dry_ml["stored_module_layout_verified"], dry_ml)
                    self.assertEqual({p.name for p in (ws.root / "Projects").iterdir()}, set(originals))

                    # execution gate.
                    unconfirmed = await call(dry_run=False, confirm=False)
                    self.assertEqual(unconfirmed["status"], "confirmation_required", unconfirmed)
                    self.assertEqual({p.name for p in (ws.root / "Projects").iterdir()}, set(originals))

                    # linear execution: stored geometry true, no layout, no native load.
                    created = await call(dry_run=False, confirm=True)
                    self.assertEqual(created["status"], "native_variant_created", created)
                    self.assertTrue(created["native_variant_created"])
                    self.assertTrue(created["stored_geometry_verified"], created)
                    self.assertFalse(created["stored_module_layout_verified"], created)
                    self.assert_not_native_loaded(created)
                    self.assertTrue(created["variant"])
                    text = Path(created["file"]).read_bytes().decode("utf-8-sig")
                    self.assertEqual(text.count("=pvShdArrayTable"), 2)
                    self.assertEqual(text.count("=pvShdGenObject"), 40)
                    for stripped in ("VarSimul", "Histogram=THistogramme", "pvShdFTable"):
                        self.assertNotIn(stripped, text)
                    linear_scene = await flags(created["project"], created["variant"])
                    self.assertEqual(linear_scene["flags"], "$20000100", linear_scene)
                    self.assertFalse(linear_scene["module_layout"]["configured"], linear_scene)
                    self.assertIn("DAzimut=0.000000", text)
                    self.assertIn("DAzimut=90.000000", text)

                    # detailed execution: Flags=$20100900 with a matching stored layout.
                    layout = await call(dry_run=False, confirm=True, shading_mode="module_layout",
                                        variant_name="Native layout scene")
                    self.assertEqual(layout["status"], "native_variant_created", layout)
                    self.assertTrue(layout["stored_geometry_verified"], layout)
                    self.assertTrue(layout["stored_module_layout_verified"], layout)
                    self.assert_not_native_loaded(layout)
                    detailed_scene = await flags(layout["project"], layout["variant"])
                    self.assertEqual(detailed_scene["flags"], "$20100900", detailed_scene)
                    self.assertTrue(detailed_scene["module_layout"]["configured"], detailed_scene)
                    self.assertEqual(detailed_scene["module_layout"]["status"], "stored_consistent",
                                     detailed_scene)

                    # unknown scene rejects without creating anything.
                    rejected = _payload(await session.call_tool(
                        IMPORT_TOOL, {"scene_name": "Unknown.pvc2", "variant_name": "Native reject",
                                      "method": "native"}))
                    self.assertIn(rejected["status"], ("needs_context", "blocked"), rejected)
                    self.assertIsNone(rejected["variant"])
                    self.assertFalse(rejected["native_variant_created"])

                # source integrity: originals byte-identical, no result artefacts.
                for name, data in originals.items():
                    self.assertEqual((ws.root / "Projects" / name).read_bytes(), data)
                self.assertEqual(list((ws.root / "Results").iterdir()), [])
                self.assertEqual([p.name for p in (ws.root / "UserData").iterdir()], [SCENE])
            ok = True
        finally:
            ws.cleanup(ok)

    def test_signed_field_azimuth_negative_recipe(self):
        fixture = _require_fixture("PVSYST_NATIVE_HOUSE_NEGATIVE_FIXTURE")
        asyncio.run(self._signed_field_azimuth_negative_recipe(fixture))

    async def _signed_field_azimuth_negative_recipe(self, fixture):
        """Real public recipe with source FieldAzim -45/+45: the written table azimuth is
        signed (-45), never 315, and stored geometry verifies across the turn."""
        ws = _Workspace(fixture)
        scene = "RoofAnchorMinus45.pvc2"
        ws.copy(Path("Projects") / "SCENE_COORDINATES.PRJ",
                Path("Projects") / "SCENE_COORDINATES.VC4",
                Path("UserData") / scene)
        originals = ws.projects()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    args = dict({"scene_name": scene, "variant_name": "Native negative",
                                 "method": "native"})
                    dry = _payload(await session.call_tool(IMPORT_TOOL, dict(args, dry_run=True)))
                    self.assertEqual(dry["status"], "ready_to_import", dry)
                    self.assertFalse(dry["stored_geometry_verified"], dry)
                    self.assertEqual({p.name for p in (ws.root / "Projects").iterdir()}, set(originals))

                    created = _payload(await session.call_tool(
                        IMPORT_TOOL, dict(args, dry_run=False, confirm=True)))
                    self.assertEqual(created["status"], "native_variant_created", created)
                    self.assertTrue(created["stored_geometry_verified"], created)
                    self.assert_not_native_loaded(created)
                    text = Path(created["file"]).read_bytes().decode("utf-8-sig")
                    self.assertIn("DAzimut=-45.000000", text)
                    self.assertIn("DAzimut=45.000000", text)
                    self.assertNotIn("DAzimut=315", text)
                    self.assertIn("Flags=$20000100", text)

                for name, data in originals.items():
                    self.assertEqual((ws.root / "Projects" / name).read_bytes(), data)
            ok = True
        finally:
            ws.cleanup(ok)

    def test_single_orientation_linear_tables(self):
        fixture = _require_fixture("PVSYST_NATIVE_HOUSE_SINGLE_FIXTURE")
        asyncio.run(self._single_orientation_linear_tables(fixture))

    async def _single_orientation_linear_tables(self, fixture):
        """One fixed source plane, one sub-array, 1..3 landscape tables, linear mode.

        Every check goes through the public MCP tools plus the written variant bytes; no
        internal builder/reader helper is imported. A single-orientation module_layout or a
        fourth table is refused before any variant is written.
        """
        ws = _Workspace(fixture)
        source_file = Path(SINGLE_PROJECT).stem + "." + SINGLE_SOURCE_VARIANT
        ws.copy(Path("Projects") / SINGLE_PROJECT, Path("Projects") / source_file,
                *(Path("UserData") / name for name, _ in SINGLE_LINEAR_SCENES),
                Path("UserData") / SINGLE_OVER_LIMIT_SCENE)
        originals = ws.projects()
        source_text = (ws.root / "Projects" / source_file).read_bytes().decode("utf-8-sig")
        hardware = tuple(line.strip() for line in source_text.splitlines()
                         if line.strip().startswith(("PVModule=", "GInverter=")))
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def import_scene(scene, **extra):
                        args = dict({"scene_name": scene, "variant_name": "Native " + scene,
                                     "method": "native"}, **extra)
                        return _payload(await session.call_tool(IMPORT_TOOL, args))

                    async def scene_state(project, variant):
                        value = _payload(await session.call_tool(
                            READ_TOOL, {"project": project, "variant": variant}))
                        return value["shading_scene"]

                    def project_names():
                        return {p.name for p in (ws.root / "Projects").iterdir()}

                    for scene, expected_tables in SINGLE_LINEAR_SCENES:
                        before = project_names()
                        dry = await import_scene(scene, dry_run=True)
                        self.assertEqual(dry["status"], "ready_to_import", dry)
                        self.assert_not_native_loaded(dry)
                        self.assertFalse(dry["stored_geometry_verified"], dry)
                        self.assertFalse(dry["stored_module_layout_verified"], dry)
                        self.assertEqual(project_names(), before)

                        created = await import_scene(scene, dry_run=False, confirm=True)
                        self.assertEqual(created["status"], "native_variant_created", created)
                        self.assertTrue(created["native_variant_created"], created)
                        self.assertTrue(created["stored_geometry_verified"], created)
                        self.assertFalse(created["stored_module_layout_verified"], created)
                        self.assert_not_native_loaded(created)

                        state = await scene_state(created["project"], created["variant"])
                        self.assertEqual(state["flags"], "$20000100", state)
                        self.assertFalse(state["module_layout"]["configured"], state)
                        bindings = state["pv_bindings"]
                        self.assertEqual(bindings["status"], "stored_consistent", bindings)
                        self.assertEqual(len(bindings["tables"]), expected_tables, bindings)
                        self.assertEqual({row["orientation_id"] for row in bindings["tables"]},
                                         {1}, bindings)
                        self.assertEqual(sum(row["module_count"] for row in bindings["tables"]),
                                         SINGLE_MODULE_TOTAL, bindings)
                        for row in bindings["tables"]:
                            self.assertEqual(row["values"]["PanneauPVName"].split(";")[0].casefold(),
                                             SINGLE_MODULE.casefold(), row["values"])
                        text = Path(created["file"]).read_bytes().decode("utf-8-sig")
                        self.assertEqual(text.count("=pvShdArrayTable"), expected_tables)
                        for token in hardware:
                            self.assertIn(token, text)

                    # Single-orientation module_layout is refused before writing.
                    before = project_names()
                    blocked = await import_scene(SINGLE_LINEAR_SCENES[1][0], dry_run=False,
                                                 confirm=True, shading_mode="module_layout")
                    self.assertIn(blocked["status"], ("blocked", "needs_context"), blocked)
                    # Refused for being non-linear (public issue text), not just any preflight miss.
                    self.assertIn("linear", " ".join(blocked["issues"]).casefold(), blocked)
                    self.assertFalse(blocked["native_variant_created"], blocked)
                    self.assertFalse(blocked["stored_module_layout_verified"], blocked)
                    self.assertEqual(project_names(), before)

                    # A fourth table is refused before writing (author keeps 1..64; Native caps at 3).
                    before = project_names()
                    over = await import_scene(SINGLE_OVER_LIMIT_SCENE, dry_run=True)
                    self.assertIn(over["status"], ("blocked", "needs_context"), over)
                    # Refused by the 1..3 table bound (public issue text), not an earlier miss.
                    self.assertIn("1..3", " ".join(over["issues"]), over)
                    self.assertFalse(over["native_variant_created"], over)
                    self.assertEqual(project_names(), before)

                    # High-level pvsyst_run_house_task keeps the released two-orientation
                    # profile: a legal single-orientation spec must stop at preflight as
                    # blocked with no write, not be silently imported. The spec is built from
                    # the public scene-read create arguments (real SIT/MET/year fixture), so no
                    # internal builder/helper is imported and no confirm/simulation is run.
                    ws.copy(Path("Sites") / "MCP_DESIGN93.SIT",
                            Path("Meteo") / "MCP_DESIGN93.MET")
                    read = _payload(await session.call_tool(
                        READ_SCENE_TOOL, {"scene_name": SINGLE_LINEAR_SCENES[0][0]}))
                    self.assertEqual(read["status"], "ready", read)
                    create_args = read["create_arguments"]
                    spec = {
                        "source_project": create_args["project"],
                        "source_variant": create_args["variant"],
                        "new_project": "SINGLE_HIGHLEVEL_GUARD.PRJ",
                        "variant_name": "High-level single guard",
                        "year": 2024,
                        "house": create_args["house"],
                        "layout": create_args["layout"],
                        "module_geometry": create_args["module_geometry"],
                        "shading_mode": "linear",
                        "scene_import_method": "native",
                    }
                    before = project_names()
                    high = _payload(await session.call_tool(
                        RUN_HOUSE_TOOL, {"spec": spec, "dry_run": True}))
                    self.assertIn(high["status"], ("blocked", "needs_attention"), high)
                    # Blocked by the high-level two-orientation requirement, not a weather or
                    # duplicate-project miss; nothing was written.
                    self.assertIn("two-orientation", " ".join(high["issues"]).casefold(), high)
                    self.assertFalse(high["native_consumption_verified"], high)
                    self.assertEqual(project_names(), before)

                for name, data in originals.items():
                    self.assertEqual((ws.root / "Projects" / name).read_bytes(), data)
                self.assertEqual(list((ws.root / "Results").iterdir()), [])
            ok = True
        finally:
            ws.cleanup(ok)


if __name__ == "__main__":
    unittest.main()

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
NoOrient=1 and the 36 source modules. Single-orientation ``module_layout`` is a narrow fine
profile (one plane, one sub-array carrying two source strings, exactly two landscape tables,
each table NModSerie modules for 36 total); shapes outside that recipe - a one-table or a
three-table fine - are refused before any variant is written, and more than three tables is
still refused. The same method pins the high-level boundary: ``pvsyst_run_house_task`` now
routes a single-orientation spec through the REAL native gate instead of a blanket
two-orientation pre-reject, so a legal single-orientation linear spec passes preflight
(ready, no write) while an unsupported single-orientation shape (a three-table fine with no
binding) is still refused before any write by that gate.

A third method pins the accepted single-orientation two-table/two-string fine profile end to
end through the public tools: both stored tables keep ``NoSubArrayPar=1`` (a table-level
reference to the one sub-array, which is not the same as the per-table global string id) and
declare distinct global string ids 1/2 with NModSerie modules each, the two tables get
distinct native creation identities, the source PAN/OND electrical hardware is preserved, and
a derived bad-data copy that rewrites the second table's string 2 -> 1 reads back as an
inconsistent stored layout (still zero native consumption).
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
# The single-orientation fine profile: exactly two landscape tables over two source strings on
# one sub-array (NModSerie modules per table, 36 total). The same fixture/scenes are reused;
# only the module_layout recipe differs from the linear 1..3-table cases.
SINGLE_FINE_SCENE = "Single_Orient_Two_Tables.pvc2"

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


def _object_blocks(lines, object_type):
    """Raw line slices of every ``PVObject_<n>=<object_type>`` block.

    Plain text scanning only; no internal builder/reader helper is imported, so the contract
    stays on the produced bytes rather than an internal object model.
    """
    blocks, current = [], None
    closing = "End of PVObject " + object_type
    for line in lines:
        if current is None:
            head = line.strip()
            if head.startswith("PVObject_") and head.endswith("=" + object_type):
                current = []
        elif line.strip() == closing:
            blocks.append(current)
            current = None
        else:
            current.append(line)
    return blocks


def _field(block, key):
    for line in block:
        head, sep, value = line.strip().partition("=")
        if sep and head == key:
            return value
    return None


def _module_records(block):
    """(module_count, per-module string ids) from a ``pvMLTable`` block's Module_ rows."""
    ids = []
    for line in block:
        head, sep, value = line.strip().partition("=")
        if sep and head.startswith("Module_") and value.endswith(","):
            ids.append(int(value.rstrip(",").split(",")[3]))
    return len(ids), ids


def _rewrite_second_table_string(text, old="2", new="1"):
    """Return ``text`` with the second pvMLTable's module string id old->new (bad-data copy)."""
    out, index, inside = [], -1, False
    for line in text.splitlines(keepends=True):
        head = line.strip()
        if head.startswith("PVObject_") and head.endswith("=pvMLTable"):
            index, inside = index + 1, True
        elif inside and head == "End of PVObject pvMLTable":
            inside = False
        elif inside and index == 1 and head.startswith("Module_") and head.partition("=")[2].endswith(","):
            key, _, value = head.partition("=")
            parts = value.rstrip(",").split(",")
            if parts[3] == old:
                parts[3] = new
                indent = line[:len(line) - len(line.lstrip())]
                eol = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
                line = indent + key + "=" + ",".join(parts) + "," + eol
        out.append(line)
    return "".join(out)


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
        internal builder/reader helper is imported. Single-orientation ``module_layout`` is
        only accepted for the exact two-table/two-string fine recipe (pinned elsewhere); a
        one-table or three-table fine is refused before any variant is written, and a fourth
        table is refused by the 1..3 bound.
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

                    # Single-orientation module_layout is now the narrow two-table/two-string
                    # fine profile (pinned by test_single_fine_two_table_module_layout). A
                    # legal source that does NOT meet that exact recipe - a one-table or a
                    # three-table fine - is refused before writing with an explicit table/string
                    # issue. The old blanket "every single fine refused" scope no longer applies.
                    for wrong_scene, shape in ((SINGLE_LINEAR_SCENES[0][0], "one table"),
                                               (SINGLE_LINEAR_SCENES[2][0], "three tables")):
                        before = project_names()
                        blocked = await import_scene(wrong_scene, dry_run=False,
                                                     confirm=True, shading_mode="module_layout")
                        self.assertIn(blocked["status"], ("blocked", "needs_context"), (shape, blocked))
                        issues = " ".join(blocked["issues"]).casefold()
                        self.assertTrue("table" in issues or "string" in issues, (shape, blocked))
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

                    # High-level pvsyst_run_house_task now routes a single-orientation spec
                    # through the REAL native gate (the blanket two-orientation pre-reject was
                    # removed for round03's explicit-layout extension). A legal single-orientation
                    # linear spec therefore passes preflight (ready) with no write; an
                    # unsupported single-orientation shape is still refused before any write by
                    # that gate. Both specs are built from the public scene-read create arguments
                    # (real SIT/MET/year fixture), so no internal builder/helper is imported and
                    # no confirm/simulation is run.
                    ws.copy(Path("Sites") / "MCP_DESIGN93.SIT",
                            Path("Meteo") / "MCP_DESIGN93.MET")

                    async def high_spec(scene, name, shading_mode):
                        read = _payload(await session.call_tool(
                            READ_SCENE_TOOL, {"scene_name": scene}))
                        self.assertEqual(read["status"], "ready", read)
                        args = read["create_arguments"]
                        return {
                            "source_project": args["project"],
                            "source_variant": args["variant"],
                            "new_project": name,
                            "variant_name": "High-level single " + name,
                            "year": 2024,
                            "house": args["house"],
                            "layout": args["layout"],
                            "module_geometry": args["module_geometry"],
                            "shading_mode": shading_mode,
                            "scene_import_method": "native",
                        }

                    # (a) Legal single-orientation linear -> the real gate admits it: ready, no write.
                    before = project_names()
                    legal = _payload(await session.call_tool(
                        RUN_HOUSE_TOOL, {"spec": await high_spec(
                            SINGLE_LINEAR_SCENES[0][0], "SINGLE_HIGHLEVEL_LEGAL.PRJ", "linear"),
                            "dry_run": True}))
                    self.assertEqual(legal["status"], "ready", legal)
                    self.assertFalse(legal["native_consumption_verified"], legal)
                    self.assertEqual(project_names(), before)

                    # (b) Unsupported single-orientation shape (three-table fine, no binding) is
                    # still refused before any write by the real native gate, NOT by a stale
                    # two-orientation pre-reject.
                    reverse = _payload(await session.call_tool(
                        RUN_HOUSE_TOOL, {"spec": await high_spec(
                            SINGLE_LINEAR_SCENES[2][0], "SINGLE_HIGHLEVEL_REVERSE.PRJ", "module_layout"),
                            "dry_run": True}))
                    self.assertIn(reverse["status"], ("blocked", "needs_attention"), reverse)
                    issues = " ".join(reverse["issues"]).casefold()
                    self.assertIn("table", issues, reverse)
                    self.assertNotIn("two-orientation", issues, reverse)
                    self.assertFalse(reverse["native_consumption_verified"], reverse)
                    self.assertEqual(project_names(), before)

                for name, data in originals.items():
                    self.assertEqual((ws.root / "Projects" / name).read_bytes(), data)
                self.assertEqual(list((ws.root / "Results").iterdir()), [])
            ok = True
        finally:
            ws.cleanup(ok)

    def test_single_fine_two_table_module_layout(self):
        fixture = _require_fixture("PVSYST_NATIVE_HOUSE_SINGLE_FIXTURE")
        asyncio.run(self._single_fine_two_table_module_layout(fixture))

    async def _single_fine_two_table_module_layout(self, fixture):
        """Single-orientation fine profile: one plane, one sub-array of two source strings,
        exactly two landscape tables, imported in ``module_layout`` mode.

        Everything goes through the public stdio-MCP tools plus the written variant bytes; no
        internal builder/reader helper is imported. Pinned facts:

        - dry_run previews in memory (``ready_to_import``, stored_* false, nothing written);
        - execution creates one variant whose two tables keep ``NoSubArrayPar=1`` (the one
          sub-array, a table-level reference distinct from the global string id) and declare
          distinct global string ids 1/2, each holding NModSerie modules (36 total);
        - the two stored tables get distinct native creation identities, so the per-table slot
          map cannot collapse them (dual identity is only exercised by the earlier method);
        - stored_geometry_verified / stored_module_layout_verified are true while imported /
          native_* stay false;
        - the source PAN/OND electrical hardware is preserved;
        - a derived bad-data copy rewriting the second table's string 2 -> 1 reads back as an
          inconsistent stored layout, still with zero native consumption.
        """
        ws = _Workspace(fixture)
        scene = SINGLE_FINE_SCENE
        source_file = Path(SINGLE_PROJECT).stem + "." + SINGLE_SOURCE_VARIANT
        ws.copy(Path("Projects") / SINGLE_PROJECT, Path("Projects") / source_file,
                Path("UserData") / scene)
        originals = ws.projects()
        source_lines = (ws.root / "Projects" / source_file).read_bytes().decode(
            "utf-8-sig").splitlines()
        electrical = {}
        for line in source_lines:
            head, sep, value = line.strip().partition("=")
            if sep and head in ("PVModule", "GInverter", "NModSerie", "NStringCh", "NStrOrient1",
                                "NInverter", "NInvMPPT", "PowerSharingInverterId"):
                electrical.setdefault(head, value)
        # The fine recipe is exactly two tables over two source strings of NModSerie modules,
        # two total independent MPPTs (one per string) and no shared inverter inputs.
        self.assertEqual(int(electrical["NStringCh"]), 2, electrical)
        self.assertEqual(int(electrical["NInvMPPT"]), 2, electrical)
        nmod = int(electrical["NModSerie"])
        self.assertEqual(nmod * 2, SINGLE_MODULE_TOTAL, electrical)
        if "PowerSharingInverterId" in electrical:
            self.assertEqual(electrical["PowerSharingInverterId"], "-1", electrical)
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    args = dict(scene_name=scene, variant_name="Native single fine",
                                method="native")

                    def project_names():
                        return {p.name for p in (ws.root / "Projects").iterdir()}

                    before = project_names()
                    dry = _payload(await session.call_tool(
                        IMPORT_TOOL, dict(args, dry_run=True, shading_mode="module_layout")))
                    self.assertEqual(dry["status"], "ready_to_import", dry)
                    self.assert_not_native_loaded(dry)
                    self.assertFalse(dry["stored_geometry_verified"], dry)
                    self.assertFalse(dry["stored_module_layout_verified"], dry)
                    self.assertEqual(project_names(), before)

                    created = _payload(await session.call_tool(
                        IMPORT_TOOL, dict(args, dry_run=False, confirm=True,
                                          shading_mode="module_layout")))
                    self.assertEqual(created["status"], "native_variant_created", created)
                    self.assertTrue(created["native_variant_created"], created)
                    self.assertTrue(created["stored_geometry_verified"], created)
                    self.assertTrue(created["stored_module_layout_verified"], created)
                    self.assert_not_native_loaded(created)

                    state = _payload(await session.call_tool(
                        READ_TOOL, {"project": created["project"], "variant": created["variant"]}
                    ))["shading_scene"]
                    self.assertEqual(state["flags"], "$20100900", state)
                    layout = state["module_layout"]
                    self.assertTrue(layout["configured"], layout)
                    self.assertEqual(layout["status"], "stored_consistent", layout)
                    self.assertEqual(layout["module_count"], SINGLE_MODULE_TOTAL, layout)
                    self.assertEqual(len(layout["tables"]), 2, layout)
                    bindings = state["pv_bindings"]
                    self.assertEqual(bindings["status"], "stored_consistent", bindings)
                    self.assertEqual({row["orientation_id"] for row in bindings["tables"]},
                                     {1}, bindings)
                    self.assertEqual(sum(row["module_count"] for row in bindings["tables"]),
                                     SINGLE_MODULE_TOTAL, bindings)

                    text = Path(created["file"]).read_bytes().decode("utf-8-sig")
                    lines = text.splitlines()
                    tables = _object_blocks(lines, "pvShdArrayTable")
                    ml_tables = _object_blocks(lines, "pvMLTable")
                    self.assertEqual(len(tables), 2, text.count("=pvShdArrayTable"))
                    self.assertEqual(len(ml_tables), 2, text.count("=pvMLTable"))
                    created_ids = [_field(block, "NoObjCreated") for block in tables]
                    self.assertEqual(len(set(created_ids)), 2, created_ids)
                    self.assertEqual([_field(block, "NoSubArrayPar") for block in ml_tables],
                                     ["1", "1"], "both tables stay on the one sub-array")
                    slots = [_field(block, "NoField3D") for block in ml_tables]
                    self.assertEqual(len(set(slots)), 2, slots)
                    self.assertEqual(sorted(_field(block, "NoField3DCreated") for block in ml_tables),
                                     sorted(created_ids), "each ML table references one stored table")
                    id_sets, counts = [], []
                    for block in ml_tables:
                        count, ids = _module_records(block)
                        counts.append(count)
                        id_sets.append(tuple(sorted(set(ids))))
                    self.assertEqual(sorted(id_sets), [(1,), (2,)], id_sets)
                    self.assertEqual(counts, [nmod, nmod], counts)
                    self.assertEqual(sum(counts), SINGLE_MODULE_TOTAL, counts)
                    for token in ("PVModule", "GInverter", "NModSerie", "NStringCh",
                                  "NStrOrient1", "NInverter", "NInvMPPT"):
                        if token in electrical:
                            self.assertIn("%s=%s" % (token, electrical[token]), text)

                    # Derived bad-data integrity: never mutate the created variant, copy it,
                    # rewrite the second table's module string id 2 -> 1, and confirm the public
                    # read reports an inconsistent stored layout (still zero native consumption).
                    bad_variant = "VCBAD1"
                    bad_file = (ws.root / "Projects"
                                / (Path(created["project"]).stem + "." + bad_variant))
                    bad_file.write_bytes(_rewrite_second_table_string(text).encode("utf-8"))
                    bad = _payload(await session.call_tool(
                        READ_TOOL, {"project": created["project"], "variant": bad_variant}))
                    bad_scene = bad["shading_scene"]
                    self.assertTrue(bad_scene["module_layout"]["configured"], bad_scene)
                    self.assertNotEqual(bad_scene["module_layout"]["status"], "stored_consistent",
                                        "rewritten string 2->1 must read back inconsistent")
                    self.assertFalse(bad_scene["pv_bindings"]["native_consumption_verified"], bad)

                for name, data in originals.items():
                    self.assertEqual((ws.root / "Projects" / name).read_bytes(), data)
                self.assertEqual(list((ws.root / "Results").iterdir()), [])
            ok = True
        finally:
            ws.cleanup(ok)


if __name__ == "__main__":
    unittest.main()

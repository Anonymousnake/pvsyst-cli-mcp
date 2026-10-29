"""Public stdio-MCP contract for the explicit per-cell ``string_layout`` binding (round03).

Everything here talks to the public MCP tools through a real stdio session; no internal
builder/reader/verifier helper is imported, so an internal refactor that keeps behaviour
does not break the contract. No PVsyst CLI/GUI is executed: the fake ``PVsystCLI.exe`` is a
placeholder that cannot run, and every "write" below only produces MCP-side files.

Contract pinned (external, persisted behaviour):

- ``pvsyst_create_house_pv_scene`` accepts an optional sibling ``string_layout`` block
  ``{tables:[{name,subarray_id,cells:[{row,col,global_string}]}]}``, stores it verbatim in
  the ``.pvc2`` recipe, and ``pvsyst_read_house_pv_scene`` round-trips it in
  ``create_arguments.string_layout`` (``string_layout_state`` reported).
- Omitting the block keeps the legacy recipe shape: the readback has no ``string_layout``
  key and the scene bytes contain no ``string_layout`` token (additive sibling, no version
  bump, no v3).
- A 3-table / cross-string single-subarray layout (each table 12 cells, per-global-string
  capacity ``NModSerie=18``; ``g1=T1+T2[seq1..6]=18``, ``g2=T2[seq7..12]+T3=18``) imports
  as a native module layout: ``status=native_variant_created``,
  ``stored_module_layout_verified=true`` but the four loader/engine flags
  (``imported``/``native_consumption_verified``/``native_geometry_verified``/
  ``native_module_layout_verified``) stay false, and the STORED readback is
  ``stored_consistent`` with three tables of 12 modules, one source subarray and three
  distinct native creation identities (no repeated slot identity).
- A draft (any ``global_string==0``) saves/reads as ``draft_incomplete`` but blocks native
  import before any write.
- Invalid bindings (over-capacity, duplicate cell, unknown global string, wrong subarray)
  are rejected (schema error, author block, or import block) and never create a native
  variant or touch the source variants.
- A scene whose recipe carries a binding is refused for ``method='gui'`` or
  ``shading_mode='linear'`` instead of silently dropping the per-cell wiring.

Env: ``PVSYST_GENERAL_BINDING_ROOT`` = the ``general-layout-fixture-r3`` role directory
containing ``env/env.json`` and ``requests/create_house_3table_complete.json``. The fixture
root comes from ``env.json`` (single fixed plane, one subarray, NModSerie=18, NStringCh=2,
36 modules, pristine ROOF_SOURCE.VC0). The test skips when the env var is absent.
"""
import asyncio
import copy
from collections import Counter
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
CREATE_TOOL = "pvsyst_create_house_pv_scene"
READ_SCENE_TOOL = "pvsyst_read_house_pv_scene"
IMPORT_TOOL = "pvsyst_import_house_pv_scene"
READ_VARIANT_TOOL = "pvsyst_get_variant_parameters"

NATIVE_NOT_LOADED = ("imported", "native_consumption_verified",
                     "native_geometry_verified", "native_module_layout_verified")


def _payload(response):
    if response.is_error:
        raise AssertionError(response.content)
    return json.loads("\n".join(item.text for item in response.content if hasattr(item, "text")))


def _role_dir():
    value = os.environ.get("PVSYST_GENERAL_BINDING_ROOT")
    if not value:
        raise unittest.SkipTest("Set PVSYST_GENERAL_BINDING_ROOT to the general-layout-fixture-r3 role dir")
    return Path(value)


def _complete_args(role):
    env = json.loads((role / "env" / "env.json").read_text(encoding="utf-8"))
    request = json.loads((role / "requests" / "create_house_3table_complete.json").read_text(encoding="utf-8"))
    args = request["arguments"]
    binding = args["string_layout"]
    # The requested complete assignment must match the fixture's declared truth.
    per_table = [Counter(cell["global_string"] for cell in table["cells"]) for table in binding["tables"]]
    assert per_table == [Counter({1: 12}), Counter({1: 6, 2: 6}), Counter({2: 12})], per_table
    total = Counter()
    for table in binding["tables"]:
        total.update(cell["global_string"] for cell in table["cells"])
    assert total == Counter({1: 18, 2: 18}), total
    return Path(env["fixture_root"]), args


class _Workspace:
    def __init__(self, fixture):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvsyst-general-binding-"))
        self.root = self.tmp / "PVsyst8.1_Data"
        shutil.copytree(fixture, self.root)
        self.cli = self.root / "PVsystCLI.exe"
        self.cli.write_bytes(b"Not an executable: this contract must not run PVsyst")

    def projects(self):
        return {path.name: path.read_bytes() for path in (self.root / "Projects").iterdir() if path.is_file()}

    def names(self):
        return {path.name for path in (self.root / "Projects").iterdir() if path.is_file()}

    def params(self):
        return StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
            cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(self.root), PVSYST_CLI=str(self.cli)))

    def cleanup(self, ok):
        if ok:
            shutil.rmtree(self.tmp)
        else:
            print("Retained general-binding failure fixture: " + str(self.tmp), file=sys.stderr)


class GeneralStringLayoutBindingBoundary(unittest.TestCase):
    def test_schema_exposes_string_layout_fields(self):
        """The public request schemas expose the new sibling/selector fields (no fixture needed)."""
        asyncio.run(self._schema())

    async def _schema(self):
        with tempfile.TemporaryDirectory(prefix="pvsyst-schema-") as directory:
            root = Path(directory)
            for folder in ("Projects", "UserData", "Results", "Sites", "Meteo", "Models"):
                (root / folder).mkdir()
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"Not an executable")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    schemas = {tool.name: json.dumps(tool.input_schema)
                               for tool in (await session.list_tools()).tools}
        self.assertIn('"string_layout"', schemas[CREATE_TOOL])
        self.assertIn('"string_layout"', schemas["pvsyst_run_house_task"])
        sourced = schemas["pvsyst_run_sourced_house_task"]
        for key in ("string_layout", "shading_mode", "scene_import_method"):
            self.assertIn('"%s"' % key, sourced, key)

    def test_public_general_binding_boundary(self):
        role = _role_dir()
        fixture, args = _complete_args(role)
        asyncio.run(self.check(fixture, args))

    async def check(self, fixture, base_args):
        ws = _Workspace(fixture)
        originals = ws.projects()
        results_before = set((ws.root / "Results").iterdir())
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def create(arguments):
                        return await session.call_tool(CREATE_TOOL, arguments)

                    async def read_scene(scene_name):
                        return _payload(await session.call_tool(READ_SCENE_TOOL, {"scene_name": scene_name}))

                    async def import_scene(scene_name, **extra):
                        arguments = dict({"scene_name": scene_name, "variant_name": "General", "method": "native",
                                          "shading_mode": "module_layout"}, **extra)
                        return await session.call_tool(IMPORT_TOOL, arguments)

                    def scene(name):
                        return ws.root / "UserData" / (name + ".pvc2")

                    def variant_args(arguments, name, drop_binding=False, dry_run=False):
                        changed = copy.deepcopy(arguments)
                        changed["output_name"] = name + ".pvc2"
                        changed["dry_run"] = dry_run
                        if drop_binding:
                            changed.pop("string_layout", None)
                        return changed

                    def mutate(cells_change):
                        changed = copy.deepcopy(base_args)
                        changed["string_layout"] = cells_change(copy.deepcopy(base_args["string_layout"]))
                        return changed

                    # 1) Complete binding: create stores it, read round-trips it as an additive sibling.
                    complete = variant_args(base_args, "STRING_LAYOUT_R3_3T")
                    result = _payload(await create(complete))
                    self.assertEqual(result["status"], "created", result)
                    self.assertEqual(result.get("string_layout_state"), "complete", result)
                    self.assertTrue(scene("STRING_LAYOUT_R3_3T").is_file())
                    self.assertIn(b"string_layout", scene("STRING_LAYOUT_R3_3T").read_bytes())
                    loaded = await read_scene("STRING_LAYOUT_R3_3T.pvc2")
                    self.assertEqual(loaded["status"], "ready", loaded)
                    self.assertEqual(loaded.get("string_layout_state"), "complete", loaded)
                    binding = loaded["create_arguments"]["string_layout"]
                    self.assertEqual([table["name"] for table in binding["tables"]], ["T1", "T2", "T3"])
                    self.assertEqual([len(table["cells"]) for table in binding["tables"]], [12, 12, 12])
                    total = Counter(cell["global_string"] for table in binding["tables"] for cell in table["cells"])
                    self.assertEqual(total, Counter({1: 18, 2: 18}), total)

                    # 2) Omitting the block keeps the legacy shape: no key, no token (no v3).
                    legacy = variant_args(base_args, "STRING_LAYOUT_R3_LEGACY", drop_binding=True)
                    self.assertEqual(_payload(await create(legacy))["status"], "created")
                    self.assertNotIn(b"string_layout", scene("STRING_LAYOUT_R3_LEGACY").read_bytes())
                    legacy_read = await read_scene("STRING_LAYOUT_R3_LEGACY.pvc2")
                    self.assertNotIn("string_layout", legacy_read["create_arguments"])
                    self.assertNotIn("string_layout_state", legacy_read)

                    # 3) Preview then execute the 3-table cross-string native module layout.
                    before = ws.names()
                    preview = _payload(await import_scene("STRING_LAYOUT_R3_3T.pvc2", dry_run=True))
                    self.assertEqual(preview["status"], "ready_to_import", preview)
                    self.assertTrue(preview["precheck_passed"], preview)
                    self.assertFalse(preview["stored_module_layout_verified"], preview)
                    self.assertEqual(ws.names(), before)
                    made = _payload(await import_scene("STRING_LAYOUT_R3_3T.pvc2", dry_run=False, confirm=True))
                    self.assertEqual(made["status"], "native_variant_created", made)
                    self.assertTrue(made["native_variant_created"], made)
                    self.assertTrue(made["stored_geometry_verified"], made)
                    self.assertTrue(made["stored_module_layout_verified"], made)
                    for key in NATIVE_NOT_LOADED:
                        self.assertFalse(made[key], "%s must stay false: %r" % (key, made))

                    # 4) STORED readback: three 12-cell tables, one subarray, distinct identities.
                    shading = _payload(await session.call_tool(READ_VARIANT_TOOL, {
                        "project": made["project"], "variant": made["variant"]}))["shading_scene"]
                    self.assertEqual(shading["flags"], "$20100900", shading)
                    self.assertEqual(shading["pv_bindings"]["status"], "stored_consistent", shading["pv_bindings"])
                    layout = shading["module_layout"]
                    self.assertTrue(layout["configured"], layout)
                    self.assertEqual(layout["status"], "stored_consistent", layout)
                    self.assertEqual(layout["module_count"], 36, layout)
                    tables = layout["tables"]
                    self.assertEqual(len(tables), 3, tables)
                    self.assertEqual([table["module_count"] for table in tables], [12, 12, 12], tables)
                    self.assertEqual({table["subarray_id"] for table in tables}, {1}, tables)
                    self.assertEqual(len({table["native_created_id"] for table in tables}), 3, tables)
                    self.assertFalse(layout["native_consumption_verified"], layout)

                    # 5) Draft (global_string=0) saves/reads but blocks import before writing.
                    def to_draft(binding):
                        for table in binding["tables"]:
                            if table["name"] == "T2":
                                for cell in table["cells"][6:]:
                                    cell["global_string"] = 0
                            if table["name"] == "T3":
                                for cell in table["cells"]:
                                    cell["global_string"] = 0
                        return binding

                    draft = mutate(to_draft)
                    draft["output_name"] = "STRING_LAYOUT_R3_DRAFT.pvc2"
                    draft_result = _payload(await create(draft))
                    self.assertEqual(draft_result["status"], "created", draft_result)
                    self.assertEqual(draft_result.get("string_layout_state"), "draft_incomplete", draft_result)
                    draft_read = await read_scene("STRING_LAYOUT_R3_DRAFT.pvc2")
                    self.assertEqual(draft_read.get("string_layout_state"), "draft_incomplete", draft_read)
                    zeros = sum(cell["global_string"] == 0
                                for table in draft_read["create_arguments"]["string_layout"]["tables"]
                                for cell in table["cells"])
                    self.assertGreater(zeros, 0, draft_read)
                    before = ws.names()
                    for flags in ({"dry_run": True}, {"dry_run": False, "confirm": True}):
                        blocked = _payload(await import_scene("STRING_LAYOUT_R3_DRAFT.pvc2", **flags))
                        self.assertIn(blocked["status"], ("blocked", "needs_context"), blocked)
                        self.assertFalse(blocked["native_variant_created"], blocked)
                        self.assertFalse(blocked["stored_module_layout_verified"], blocked)
                        self.assertEqual(ws.names(), before)

                    # 6) Invalid bindings are rejected and never create a variant or touch the source.
                    def over_capacity(binding):
                        for table in binding["tables"]:
                            if table["name"] == "T2":
                                for cell in table["cells"]:
                                    cell["global_string"] = 1
                        return binding

                    def duplicate_cell(binding):
                        target = next(table for table in binding["tables"] if table["name"] == "T1")
                        target["cells"].append(dict(target["cells"][0]))
                        return binding

                    def unknown_global(binding):
                        target = next(table for table in binding["tables"] if table["name"] == "T1")
                        target["cells"][0]["global_string"] = 3
                        return binding

                    def wrong_subarray(binding):
                        for table in binding["tables"]:
                            if table["name"] == "T1":
                                table["subarray_id"] = 2
                        return binding

                    for label, builder, name in (
                        ("overcap", over_capacity, "STRING_LAYOUT_R3_BAD_CAP"),
                        ("dupcell", duplicate_cell, "STRING_LAYOUT_R3_BAD_DUP"),
                        ("unknownglobal", unknown_global, "STRING_LAYOUT_R3_BAD_GLOBAL"),
                        ("subarray", wrong_subarray, "STRING_LAYOUT_R3_BAD_SUB"),
                    ):
                        arguments = mutate(builder)
                        arguments["output_name"] = name + ".pvc2"
                        before = ws.names()
                        response = await create(arguments)
                        rejected = response.is_error
                        if not rejected:
                            stored = _payload(response)
                            if stored["status"] in ("blocked", "failed"):
                                rejected = True
                            else:
                                outcome = await import_scene(name + ".pvc2", dry_run=False, confirm=True)
                                rejected = outcome.is_error
                                if not rejected:
                                    payload = _payload(outcome)
                                    rejected = (payload["status"] in ("blocked", "needs_context", "failed")
                                                and not payload.get("native_variant_created"))
                        self.assertTrue(rejected, label)
                        self.assertEqual(ws.names(), before, label)
                        self.assertEqual(set((ws.root / "Results").iterdir()), results_before, label)

                    # 7) A binding-carrying scene is not silently dropped by GUI/linear import.
                    before = ws.names()
                    for label, arguments in (
                        ("native_linear", {"scene_name": "STRING_LAYOUT_R3_3T.pvc2", "variant_name": "Drop native linear",
                                           "method": "native", "shading_mode": "linear", "dry_run": True}),
                        ("gui_module_layout", {"scene_name": "STRING_LAYOUT_R3_3T.pvc2", "variant_name": "Drop gui layout",
                                               "method": "gui", "shading_mode": "module_layout", "dry_run": True}),
                    ):
                        response = await session.call_tool(IMPORT_TOOL, arguments)
                        self.assertFalse(response.is_error, (label, response.content))
                        dropped = _payload(response)
                        self.assertEqual(dropped["status"], "blocked", (label, dropped))
                        self.assertIn("string_layout", " ".join(dropped["issues"]), (label, dropped))
                        self.assertEqual(ws.names(), before, label)

                    # 8) The legacy None scene still imports as a plain linear layout (old path intact).
                    linear = _payload(await session.call_tool(IMPORT_TOOL, {
                        "scene_name": "STRING_LAYOUT_R3_LEGACY.pvc2", "variant_name": "Legacy linear",
                        "method": "native", "shading_mode": "linear", "dry_run": False, "confirm": True}))
                    self.assertEqual(linear["status"], "native_variant_created", linear)
                    self.assertTrue(linear["stored_geometry_verified"], linear)
                    self.assertFalse(linear["stored_module_layout_verified"], linear)
                    for key in NATIVE_NOT_LOADED:
                        self.assertFalse(linear[key], linear)

            # The source variants (including the pristine VC0) stay byte-identical; no Results output.
            for name, data in originals.items():
                self.assertEqual((ws.root / "Projects" / name).read_bytes(), data, name)
            self.assertEqual(set((ws.root / "Results").iterdir()), results_before)
            ok = True
        finally:
            ws.cleanup(ok)


if __name__ == "__main__":
    unittest.main()

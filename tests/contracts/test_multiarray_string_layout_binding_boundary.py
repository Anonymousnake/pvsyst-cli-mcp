"""Public stdio-MCP contract for the multi-subarray explicit per-cell ``string_layout`` binding (round05).

Everything here talks to the public MCP tools through a real stdio session. It reuses the
single-subarray harness in ``test_general_string_layout_binding_boundary`` (``_Workspace``,
``_payload``, ``NATIVE_NOT_LOADED``) instead of copying it, so an internal builder/reader
refactor that keeps behaviour does not break the contract. No PVsyst CLI/GUI is executed: the
fake ``PVsystCLI.exe`` is a placeholder that cannot run, and every "write" below only produces
MCP-side files.

Contract pinned (external, persisted behaviour) for two source subarrays, each with two strings,
72 modules total (``SA1`` -> global 1/2, ``SA2`` -> global 3/4, each string 18 modules = source
``NModSerie``):

- ``pvsyst_create_house_pv_scene`` accepts a ``string_layout`` with one table per source subarray,
  stores it, and ``pvsyst_read_house_pv_scene`` round-trips it in
  ``create_arguments.string_layout`` (``string_layout_state="complete"``).
- Native import (``method="native"``, ``shading_mode="module_layout"``) creates a fresh variant
  whose STORED readback is ``stored_consistent`` with 72 modules over two tables of 36, both
  source subarray ids ``{1, 2}`` present exactly once, four global strings each 18 modules, and a
  relational bijection: the two native creation identities and the two orientation ids are each
  distinct (the exact ``NoObjCreated`` numbers are NOT pinned), and each table's ``string_ids``
  belong to its own source subarray's ordinal range (``{1: {1,2}, 2: {3,4}}``). The four loader
  flags stay false (``native_consumption_verified`` etc.).
- A cross-array swap (``global 1,2 <-> 3,4`` so every string still totals 18 but a table claims a
  global string owned by the other subarray) is refused before any ``.VCx`` is written; no VC /
  Results side effect, source bytes unchanged.
- A draft (any ``global_string==0``) saves/reads as ``draft_incomplete`` but blocks native import
  before any write.
- Source ``.VCx``/``.PRJ`` bytes and pre-existing ``UserData`` scenes are unchanged; no Results
  output.

Env: ``PVSYST_MULTIARRAY_BINDING_ROOT`` = the ``multisub-r5`` role directory containing
``env/env.json`` (``fixture_root``) and ``requests/create_house_2x2_complete.json``. The test
skips when the env var is absent. Prepared-only: 0 actual PVsyst CLI/Native.
"""
import asyncio
import copy
from collections import Counter
import json
import os
from pathlib import Path
import sys
import unittest

from mcp import ClientSession
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_general_string_layout_binding_boundary import (  # noqa: E402  (reuse, do not copy)
    NATIVE_NOT_LOADED, _payload, _Workspace,
)

CREATE_TOOL = "pvsyst_create_house_pv_scene"
READ_SCENE_TOOL = "pvsyst_read_house_pv_scene"
IMPORT_TOOL = "pvsyst_import_house_pv_scene"
READ_VARIANT_TOOL = "pvsyst_get_variant_parameters"


def _role_dir():
    value = os.environ.get("PVSYST_MULTIARRAY_BINDING_ROOT")
    if not value:
        raise unittest.SkipTest("Set PVSYST_MULTIARRAY_BINDING_ROOT to the multisub-r5 role dir")
    return Path(value)


def _complete_args(role):
    env = json.loads((role / "env" / "env.json").read_text(encoding="utf-8"))
    request = json.loads((role / "requests" / "create_house_2x2_complete.json").read_text(encoding="utf-8"))
    args = request["arguments"]
    binding = args["string_layout"]
    # The requested complete assignment must match the fixture's declared truth:
    # one table per source subarray, two whole strings per table (SA1=1/2, SA2=3/4).
    per_table = [Counter(cell["global_string"] for cell in table["cells"]) for table in binding["tables"]]
    assert per_table == [Counter({1: 18, 2: 18}), Counter({3: 18, 4: 18})], per_table
    assert [table["subarray_id"] for table in binding["tables"]] == [1, 2], binding["tables"]
    total = Counter()
    for table in binding["tables"]:
        total.update(cell["global_string"] for cell in table["cells"])
    assert total == Counter({1: 18, 2: 18, 3: 18, 4: 18}), total
    return Path(env["fixture_root"]), args


class MultiArrayStringLayoutBindingBoundary(unittest.TestCase):
    def test_public_multiarray_binding_boundary(self):
        role = _role_dir()
        fixture, args = _complete_args(role)
        asyncio.run(self.check(fixture, args))

    async def check(self, fixture, base_args):
        ws = _Workspace(fixture)
        originals = ws.projects()
        userdata_before = {path.name: path.read_bytes()
                           for path in (ws.root / "UserData").iterdir() if path.is_file()}
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
                        arguments = dict({"scene_name": scene_name, "variant_name": "Multi",
                                          "method": "native", "shading_mode": "module_layout"}, **extra)
                        return await session.call_tool(IMPORT_TOOL, arguments)

                    async def read_variant(project, variant):
                        return _payload(await session.call_tool(
                            READ_VARIANT_TOOL, {"project": project, "variant": variant}))["shading_scene"]

                    def scene(name):
                        return ws.root / "UserData" / (name + ".pvc2")

                    def variant_args(arguments, name):
                        changed = copy.deepcopy(arguments)
                        changed["output_name"] = name + ".pvc2"
                        return changed

                    def mutate(cells_change):
                        changed = copy.deepcopy(base_args)
                        changed["string_layout"] = cells_change(copy.deepcopy(base_args["string_layout"]))
                        return changed

                    # 1) Complete two-subarray binding: create stores it, read round-trips it.
                    complete = variant_args(base_args, "MULTI_R5_COMPLETE")
                    result = _payload(await create(complete))
                    self.assertEqual(result["status"], "created", result)
                    self.assertEqual(result.get("string_layout_state"), "complete", result)
                    self.assertTrue(scene("MULTI_R5_COMPLETE").is_file())
                    self.assertIn(b"string_layout", scene("MULTI_R5_COMPLETE").read_bytes())
                    loaded = await read_scene("MULTI_R5_COMPLETE.pvc2")
                    self.assertEqual(loaded["status"], "ready", loaded)
                    self.assertEqual(loaded.get("string_layout_state"), "complete", loaded)
                    binding = loaded["create_arguments"]["string_layout"]
                    self.assertEqual([table["name"] for table in binding["tables"]], ["T1", "T2"])
                    self.assertEqual([len(table["cells"]) for table in binding["tables"]], [36, 36])
                    self.assertEqual([table["subarray_id"] for table in binding["tables"]], [1, 2])
                    total = Counter(cell["global_string"] for table in binding["tables"] for cell in table["cells"])
                    self.assertEqual(total, Counter({1: 18, 2: 18, 3: 18, 4: 18}), total)

                    # 2) Preview then execute the two-subarray native module layout.
                    before = ws.names()
                    preview = _payload(await import_scene("MULTI_R5_COMPLETE.pvc2", dry_run=True))
                    self.assertEqual(preview["status"], "ready_to_import", preview)
                    self.assertTrue(preview["precheck_passed"], preview)
                    self.assertFalse(preview["stored_module_layout_verified"], preview)
                    self.assertEqual(ws.names(), before)
                    made = _payload(await import_scene("MULTI_R5_COMPLETE.pvc2", dry_run=False, confirm=True))
                    self.assertEqual(made["status"], "native_variant_created", made)
                    self.assertTrue(made["native_variant_created"], made)
                    self.assertTrue(made["stored_geometry_verified"], made)
                    self.assertTrue(made["stored_module_layout_verified"], made)
                    for key in NATIVE_NOT_LOADED:
                        self.assertFalse(made[key], "%s must stay false: %r" % (key, made))

                    # 3) STORED readback: 72 modules, two 36-cell tables, subarray {1,2} once each,
                    #    four global strings each 18, and a relational (not numeric) identity bijection.
                    shading = await read_variant(made["project"], made["variant"])
                    self.assertEqual(shading["flags"], "$20100900", shading)
                    self.assertEqual(shading["pv_bindings"]["status"], "stored_consistent", shading["pv_bindings"])
                    layout = shading["module_layout"]
                    self.assertTrue(layout["configured"], layout)
                    self.assertEqual(layout["status"], "stored_consistent", layout)
                    self.assertEqual(layout["module_count"], 72, layout)
                    self.assertFalse(layout["native_consumption_verified"], layout)
                    tables = layout["tables"]
                    self.assertEqual(len(tables), 2, tables)
                    self.assertEqual([table["module_count"] for table in tables], [36, 36], tables)
                    self.assertEqual({table["subarray_id"] for table in tables}, {1, 2}, tables)
                    self.assertEqual(len({table["subarray_id"] for table in tables}), 2, tables)
                    self.assertEqual(len({table["native_created_id"] for table in tables}), 2, tables)
                    self.assertEqual(len({table["orientation_id"] for table in tables}), 2, tables)
                    self.assertEqual(len({(table["subarray_id"], table["orientation_id"]) for table in tables}), 2, tables)
                    # Each table's global strings belong to its own source subarray ordinal range.
                    self.assertEqual({table["subarray_id"]: set(table["string_ids"]) for table in tables},
                                     {1: {1, 2}, 2: {3, 4}}, tables)

                    # 4) Cross-array swap (1,2 <-> 3,4): every string still 18 but a table claims a
                    #    global string owned by the other subarray -> refused before any .VCx write.
                    def swapped(binding):
                        remap = {1: 3, 2: 4, 3: 1, 4: 2}
                        for table in binding["tables"]:
                            for cell in table["cells"]:
                                cell["global_string"] = remap[cell["global_string"]]
                        return binding

                    swap = mutate(swapped)
                    swap["output_name"] = "MULTI_R5_SWAP.pvc2"
                    before = ws.names()
                    response = await create(swap)
                    rejected = response.is_error
                    if not rejected:
                        stored = _payload(response)
                        if stored["status"] in ("blocked", "failed"):
                            rejected = True
                        else:
                            outcome = await import_scene("MULTI_R5_SWAP.pvc2", dry_run=False, confirm=True)
                            rejected = outcome.is_error
                            if not rejected:
                                payload = _payload(outcome)
                                rejected = (payload["status"] in ("blocked", "needs_context", "failed")
                                            and not payload.get("native_variant_created"))
                    self.assertTrue(rejected, "cross-array swap must be refused before writing a variant")
                    self.assertEqual(ws.names(), before, "cross-array swap has no VC side effect")
                    self.assertEqual(set((ws.root / "Results").iterdir()), results_before)

                    # 5) Draft (global_string=0) saves/reads but blocks native import before writing.
                    def to_draft(binding):
                        for table in binding["tables"]:
                            if table["name"] == "T2":
                                for cell in table["cells"][:9]:
                                    cell["global_string"] = 0
                        return binding

                    draft = mutate(to_draft)
                    draft["output_name"] = "MULTI_R5_DRAFT.pvc2"
                    draft_result = _payload(await create(draft))
                    self.assertEqual(draft_result["status"], "created", draft_result)
                    self.assertEqual(draft_result.get("string_layout_state"), "draft_incomplete", draft_result)
                    draft_read = await read_scene("MULTI_R5_DRAFT.pvc2")
                    self.assertEqual(draft_read.get("string_layout_state"), "draft_incomplete", draft_read)
                    zeros = sum(cell["global_string"] == 0
                                for table in draft_read["create_arguments"]["string_layout"]["tables"]
                                for cell in table["cells"])
                    self.assertGreater(zeros, 0, draft_read)
                    before = ws.names()
                    for flags in ({"dry_run": True}, {"dry_run": False, "confirm": True}):
                        blocked = _payload(await import_scene("MULTI_R5_DRAFT.pvc2", **flags))
                        self.assertIn(blocked["status"], ("blocked", "needs_context"), blocked)
                        self.assertFalse(blocked["native_variant_created"], blocked)
                        self.assertFalse(blocked["stored_module_layout_verified"], blocked)
                        self.assertEqual(ws.names(), before)

            # Source variants and pre-existing scenes stay byte-identical; no Results output.
            for name, data in originals.items():
                self.assertEqual((ws.root / "Projects" / name).read_bytes(), data, name)
            for name, data in userdata_before.items():
                self.assertEqual((ws.root / "UserData" / name).read_bytes(), data, name)
            self.assertEqual(set((ws.root / "Results").iterdir()), results_before)
            ok = True
        finally:
            ws.cleanup(ok)


if __name__ == "__main__":
    unittest.main()

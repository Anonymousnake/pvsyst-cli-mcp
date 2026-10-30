"""Public stdio-MCP contract for grouped per-orientation multi-table ``string_layout`` (round08).

A grouped (version 2) source has two fixed source subarrays on two orientations.  This contract
pins that a single source orientation may be split over several native PV tables: orientation 1
owns two 18-cell tables (globals 1 and 2), orientation 2 owns one 36-cell table (globals 3/4) --
3 tables, 72 modules total.

Contract pinned (external, persisted behaviour):
- ``pvsyst_create_house_pv_scene`` accepts 3 grouped tables, each naming its ``orientation_id``,
  plus a ``string_layout`` binding, stores it, and ``pvsyst_read_house_pv_scene`` round-trips it
  (``string_layout_state="complete"``).
- Native import (``method="native"``, ``shading_mode="module_layout"``) creates a fresh variant
  whose STORED readback is ``stored_consistent`` with 72 modules over THREE tables spanning the
  two source orientations (18/18/36), distinct native creation identities, per-table string ids
  inside their own source subarray's global range (SA1 {1,2}, SA2 {3,4}); loader flags stay false.
- Splitting one orientation over several tables is the round08 addition; the two-table / one
  table per orientation grouped shape remains valid (regression covered elsewhere).
- A wrong per-orientation module total (a source orientation's tables not summing to its
  ``NModSerie x NStringCh``) is refused at create time.
- A cross-owner binding (a table claims a global string owned by the other subarray) is refused
  before any ``.VCx`` is written; a draft (``global_string==0``) saves/reads ``draft_incomplete``
  but blocks native import before any write.
- Source ``.VCx``/``.PRJ`` bytes and pre-existing ``UserData`` scenes are unchanged; no Results.

Env: ``PVSYST_GROUP3_MULTITABLE_ROOT`` = the ``grouped-multitable-r8`` role directory containing
``env/env.json`` (``fixture_root``) and ``requests/create_house_group3table.json``. The test
skips when the env var is absent. Prepared-only: 0 actual PVsyst CLI / Native / GUI.
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
    value = os.environ.get("PVSYST_GROUP3_MULTITABLE_ROOT")
    if not value:
        raise unittest.SkipTest("Set PVSYST_GROUP3_MULTITABLE_ROOT to the grouped-multitable-r8 role dir")
    return Path(value)


def _complete_args(role):
    env = json.loads((role / "env" / "env.json").read_text(encoding="utf-8"))
    request = json.loads((role / "requests" / "create_house_group3table.json").read_text(encoding="utf-8"))
    args = request["arguments"]
    binding = args["string_layout"]
    per_table = [Counter(cell["global_string"] for cell in table["cells"]) for table in binding["tables"]]
    assert per_table == [Counter({1: 18}), Counter({2: 18}), Counter({3: 18, 4: 18})], per_table
    assert [table["subarray_id"] for table in binding["tables"]] == [1, 1, 2], binding["tables"]
    total = Counter()
    for table in binding["tables"]:
        total.update(cell["global_string"] for cell in table["cells"])
    assert total == Counter({1: 18, 2: 18, 3: 18, 4: 18}), total
    return Path(env["fixture_root"]), args


class GroupedMultitableBindingBoundary(unittest.TestCase):
    def test_public_grouped_multitable_boundary(self):
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
                        arguments = dict({"scene_name": scene_name, "variant_name": "Grouped3",
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

                    # 1) Complete 3-table grouped binding: create stores it, read round-trips it.
                    complete = variant_args(base_args, "GROUPED3_R8_COMPLETE")
                    result = _payload(await create(complete))
                    self.assertEqual(result["status"], "created", result)
                    self.assertEqual(result.get("string_layout_state"), "complete", result)
                    self.assertTrue(scene("GROUPED3_R8_COMPLETE").is_file())
                    self.assertIn(b"string_layout", scene("GROUPED3_R8_COMPLETE").read_bytes())
                    loaded = await read_scene("GROUPED3_R8_COMPLETE.pvc2")
                    self.assertEqual(loaded["status"], "ready", loaded)
                    self.assertEqual(loaded.get("string_layout_state"), "complete", loaded)
                    binding = loaded["create_arguments"]["string_layout"]
                    self.assertEqual([table["name"] for table in binding["tables"]], ["T1a", "T1b", "T3"])
                    self.assertEqual([len(table["cells"]) for table in binding["tables"]], [18, 18, 36])
                    self.assertEqual([table["subarray_id"] for table in binding["tables"]], [1, 1, 2])
                    self.assertEqual([table["orientation_id"]
                                      for table in loaded["create_arguments"]["layout"]["tables"]], [1, 1, 2])
                    total = Counter(cell["global_string"] for table in binding["tables"] for cell in table["cells"])
                    self.assertEqual(total, Counter({1: 18, 2: 18, 3: 18, 4: 18}), total)

                    # 2) Preview then execute the grouped 3-table native module layout.
                    before = ws.names()
                    preview = _payload(await import_scene("GROUPED3_R8_COMPLETE.pvc2", dry_run=True))
                    self.assertEqual(preview["status"], "ready_to_import", preview)
                    self.assertTrue(preview["precheck_passed"], preview)
                    self.assertFalse(preview["stored_module_layout_verified"], preview)
                    self.assertEqual(ws.names(), before)
                    made = _payload(await import_scene("GROUPED3_R8_COMPLETE.pvc2", dry_run=False, confirm=True))
                    self.assertEqual(made["status"], "native_variant_created", made)
                    self.assertTrue(made["native_variant_created"], made)
                    self.assertTrue(made["stored_geometry_verified"], made)
                    self.assertTrue(made["stored_module_layout_verified"], made)
                    for key in NATIVE_NOT_LOADED:
                        self.assertFalse(made[key], "%s must stay false: %r" % (key, made))

                    # 3) STORED readback: 3 tables (18/18/36) across orientations 1/1/2, 72 modules,
                    #    distinct creation identities, per-table strings in their own subarray range.
                    shading = await read_variant(made["project"], made["variant"])
                    self.assertEqual(shading["flags"], "$20100900", shading)
                    self.assertEqual(shading["pv_bindings"]["status"], "stored_consistent", shading["pv_bindings"])
                    layout = shading["module_layout"]
                    self.assertTrue(layout["configured"], layout)
                    self.assertEqual(layout["status"], "stored_consistent", layout)
                    self.assertEqual(layout["module_count"], 72, layout)
                    self.assertFalse(layout["native_consumption_verified"], layout)
                    tables = layout["tables"]
                    self.assertEqual(len(tables), 3, tables)
                    self.assertEqual([table["module_count"] for table in tables], [18, 18, 36], tables)
                    self.assertEqual({table["subarray_id"] for table in tables}, {1, 2}, tables)
                    self.assertEqual({table["orientation_id"] for table in tables}, {1, 2}, tables)
                    self.assertEqual(len({table["native_created_id"] for table in tables}), 3, tables)
                    self.assertEqual([table["orientation_id"] for table in tables], [1, 1, 2], tables)
                    self.assertEqual([table["string_ids"] for table in tables], [[1], [2], [3, 4]], tables)
                    by_sub = {}
                    for table in tables:
                        by_sub.setdefault(table["subarray_id"], set()).update(table["string_ids"])
                    self.assertEqual(by_sub, {1: {1, 2}, 2: {3, 4}}, tables)
                    self.assertEqual(sum(table["module_count"] for table in tables), 72, tables)

                    # 4) Wrong per-orientation total (ori1 = 18 + 36 = 54, not 36): refused at create.
                    def wrong_group(layout_tables):
                        bad = copy.deepcopy(base_args)
                        bad["layout"]["tables"][1]["module_rows"] = 6   # T1b becomes 6x6 = 36 -> ori1 sum 54
                        bad["string_layout"]["tables"][1]["cells"] = [
                            {"row": r, "col": c, "global_string": 2}
                            for r in range(1, 7) for c in range(1, 7)]
                        return bad

                    wrong = wrong_group(None)
                    wrong["output_name"] = "GROUPED3_R8_BADTOTAL.pvc2"
                    before = ws.names()
                    response = await create(wrong)
                    rejected = response.is_error
                    if not rejected:
                        stored = _payload(response)
                        rejected = stored["status"] in ("blocked", "failed")
                    self.assertTrue(rejected, "wrong per-orientation total must be refused")
                    self.assertEqual(ws.names(), before)

                    # 5) Cross-owner binding (T3 claims globals 1/2 owned by subarray 1): refused.
                    def cross_owner(binding):
                        for cell in binding["tables"][2]["cells"]:
                            cell["global_string"] = 1 if cell["row"] <= 3 else 2
                        return binding

                    swap = mutate(cross_owner)
                    swap["output_name"] = "GROUPED3_R8_SWAP.pvc2"
                    before = ws.names()
                    response = await create(swap)
                    rejected = response.is_error
                    if not rejected:
                        stored = _payload(response)
                        if stored["status"] in ("blocked", "failed"):
                            rejected = True
                        else:
                            outcome = await import_scene("GROUPED3_R8_SWAP.pvc2", dry_run=False, confirm=True)
                            rejected = outcome.is_error
                            if not rejected:
                                payload = _payload(outcome)
                                rejected = (payload["status"] in ("blocked", "needs_context", "failed")
                                            and not payload.get("native_variant_created"))
                    self.assertTrue(rejected, "cross-owner binding must be refused before writing a variant")
                    self.assertEqual(ws.names(), before, "cross-owner binding has no VC side effect")
                    self.assertEqual(set((ws.root / "Results").iterdir()), results_before)

                    # 6) Draft (global_string=0) saves/reads but blocks native import before writing.
                    def to_draft(binding):
                        for cell in binding["tables"][0]["cells"][:6]:
                            cell["global_string"] = 0
                        return binding

                    draft = mutate(to_draft)
                    draft["output_name"] = "GROUPED3_R8_DRAFT.pvc2"
                    draft_result = _payload(await create(draft))
                    self.assertEqual(draft_result["status"], "created", draft_result)
                    self.assertEqual(draft_result.get("string_layout_state"), "draft_incomplete", draft_result)
                    draft_read = await read_scene("GROUPED3_R8_DRAFT.pvc2")
                    self.assertEqual(draft_read.get("string_layout_state"), "draft_incomplete", draft_read)
                    zeros = sum(cell["global_string"] == 0
                                for table in draft_read["create_arguments"]["string_layout"]["tables"]
                                for cell in table["cells"])
                    self.assertGreater(zeros, 0, draft_read)
                    before = ws.names()
                    for flags in ({"dry_run": True}, {"dry_run": False, "confirm": True}):
                        blocked = _payload(await import_scene("GROUPED3_R8_DRAFT.pvc2", **flags))
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

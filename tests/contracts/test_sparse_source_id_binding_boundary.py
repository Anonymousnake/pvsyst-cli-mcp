"""Public stdio-MCP contract for sparse source ``SubArrayId`` identity in the explicit per-cell
``string_layout`` binding (round08, B lane grouped-sparse-r8).

The pre-round08 stored-layout classifier only accepted appearance-order source ids ``{1, 2}`` or a
single ``{1}``; a legal two-subarray source whose ids are *not* ``{1, 2}`` (here ``7, 2`` in file
order) was refused at native import even though create/read and the explicit builder accepted it.
This contract pins that the classifier now keys on the real source structure (two distinct
positive ``SubArrayId`` values, appearance order) exactly like the builder's source-data range.

Contract pinned (external, persisted behaviour), on a source copy ``A2S2_R4.VCS0`` whose subarrays
are ids ``7`` (NoOrientation 1) then ``2`` (NoOrientation 2), each 2 strings of NModSerie 18
(72 modules), two distinct azimuth planes 0/90:

- ``pvsyst_create_house_pv_scene`` accepts the 2-table per-cell binding (T1 -> subarray 7, T2 ->
  subarray 2) and ``pvsyst_read_house_pv_scene`` round-trips it in file/position order ``[7, 2]``
  (never a numeric id sort ``[2, 7]``), ``string_layout_state="complete"``.
- Native import (``method="native"``, ``shading_mode="module_layout"``) creates a genuinely fresh
  variant (``VC1``; the target VC did not pre-exist) whose STORED readback is ``stored_consistent``
  with 72 modules over two 36-cell tables; table order is ``[7, 2]`` (positional, not numeric),
  owners are ``{7: {1, 2}, 2: {3, 4}}`` by source position, two distinct native creation
  identities, and the four loader flags stay false.
- The legacy None (no ``string_layout``) path keeps its original ``{1, 2}`` requirement and stays
  refused for the sparse source (``requires source arrays 1/2 on distinct orientations``).
- A cross-owner binding (T2 claims globals 1/2 owned by the positional first subarray 7) is refused
  before any ``.VCx``; source ``.VCx``/``.PRJ`` bytes and UserData scenes are unchanged; no Results.

Env: ``PVSYST_SPARSE_BINDING_ROOT`` = the ``grouped-sparse-r8`` role directory containing
``env/env.json`` (``fixture_root``) and ``requests/create_sparse_2table.json``. The test skips when
the env var is absent. Prepared-only: 0 actual PVsyst CLI / Native / GUI.
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
    value = os.environ.get("PVSYST_SPARSE_BINDING_ROOT")
    if not value:
        raise unittest.SkipTest("Set PVSYST_SPARSE_BINDING_ROOT to the grouped-sparse-r8 role dir")
    return Path(value)


def _sparse_args(role):
    env = json.loads((role / "env" / "env.json").read_text(encoding="utf-8"))
    request = json.loads((role / "requests" / "create_sparse_2table.json").read_text(encoding="utf-8"))
    args = request["arguments"]
    binding = args["string_layout"]
    # The requested complete assignment must match the fixture's declared sparse truth.
    assert [table["subarray_id"] for table in binding["tables"]] == [7, 2], binding["tables"]
    assert [table["orientation_id"] for table in args["layout"]["tables"]] == [1, 2], args["layout"]
    per_table = [Counter(cell["global_string"] for cell in table["cells"]) for table in binding["tables"]]
    assert per_table == [Counter({1: 18, 2: 18}), Counter({3: 18, 4: 18})], per_table
    total = Counter()
    for table in binding["tables"]:
        total.update(cell["global_string"] for cell in table["cells"])
    assert total == Counter({1: 18, 2: 18, 3: 18, 4: 18}), total
    return Path(env["fixture_root"]), args


class SparseSourceIdBindingBoundary(unittest.TestCase):
    def test_public_sparse_source_id_binding(self):
        role = _role_dir()
        fixture, args = _sparse_args(role)
        asyncio.run(self.check(fixture, args))

    async def check(self, fixture, base_args):
        ws = _Workspace(fixture)
        originals = ws.projects()
        # The fresh import must create a new target: the fixture must NOT pre-contain A2S2_R4.VC1.
        self.assertNotIn("A2S2_R4.VC1", originals, "fixture must not carry a pre-imported target")
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
                        arguments = dict({"scene_name": scene_name, "variant_name": "Sparse",
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

                    # 1) Complete 2-table sparse binding: create stores it, read round-trips it.
                    complete = variant_args(base_args, "SPARSE_R8_COMPLETE")
                    result = _payload(await create(complete))
                    self.assertEqual(result["status"], "created", result)
                    self.assertEqual(result.get("string_layout_state"), "complete", result)
                    self.assertTrue(scene("SPARSE_R8_COMPLETE").is_file())
                    self.assertIn(b"string_layout", scene("SPARSE_R8_COMPLETE").read_bytes())
                    loaded = await read_scene("SPARSE_R8_COMPLETE.pvc2")
                    self.assertEqual(loaded["status"], "ready", loaded)
                    self.assertEqual(loaded.get("string_layout_state"), "complete", loaded)
                    binding = loaded["create_arguments"]["string_layout"]
                    self.assertEqual([table["name"] for table in binding["tables"]], ["T1", "T2"])
                    # Positional/appearance order 7 then 2 -- NOT a numeric id sort (2 then 7).
                    self.assertEqual([table["subarray_id"] for table in binding["tables"]], [7, 2])
                    self.assertEqual([table["orientation_id"]
                                      for table in loaded["create_arguments"]["layout"]["tables"]], [1, 2])
                    total = Counter(cell["global_string"] for table in binding["tables"] for cell in table["cells"])
                    self.assertEqual(total, Counter({1: 18, 2: 18, 3: 18, 4: 18}), total)

                    # 2) Preview then execute the sparse native module layout.
                    before = ws.names()
                    preview = _payload(await import_scene("SPARSE_R8_COMPLETE.pvc2", dry_run=True))
                    self.assertEqual(preview["status"], "ready_to_import", preview)
                    self.assertTrue(preview["precheck_passed"], preview)
                    self.assertFalse(preview["stored_module_layout_verified"], preview)
                    self.assertEqual(ws.names(), before)
                    made = _payload(await import_scene("SPARSE_R8_COMPLETE.pvc2", dry_run=False, confirm=True))
                    self.assertEqual(made["status"], "native_variant_created", made)
                    self.assertTrue(made["native_variant_created"], made)
                    self.assertEqual(made["variant"], "VC1", made)  # genuinely fresh
                    self.assertTrue(made["stored_geometry_verified"], made)
                    self.assertTrue(made["stored_module_layout_verified"], made)
                    for key in NATIVE_NOT_LOADED:
                        self.assertFalse(made[key], "%s must stay false: %r" % (key, made))

                    # 3) STORED readback: 72 modules, two 36-cell tables in positional order 7,2,
                    #    owners {7:{1,2}, 2:{3,4}} by source position (not a numeric id sort).
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
                    # Positional order: subarray 7 (orientation 1) precedes subarray 2 (orientation 2).
                    self.assertEqual([table["subarray_id"] for table in tables], [7, 2], tables)
                    self.assertEqual([table["orientation_id"] for table in tables], [1, 2], tables)
                    self.assertEqual(len({table["native_created_id"] for table in tables}), 2, tables)
                    self.assertEqual([table["string_ids"] for table in tables], [[1, 2], [3, 4]], tables)
                    self.assertEqual({table["subarray_id"]: set(table["string_ids"]) for table in tables},
                                     {7: {1, 2}, 2: {3, 4}}, tables)
                    self.assertEqual(sum(table["module_count"] for table in tables), 72, tables)

                    # 4) Legacy None keeps its original {1,2} requirement: sparse source is refused.
                    legacy = copy.deepcopy(base_args)
                    legacy.pop("string_layout", None)
                    legacy["output_name"] = "SPARSE_R8_LEGACY.pvc2"
                    self.assertEqual(_payload(await create(legacy))["status"], "created")
                    before = ws.names()
                    legacy_import = _payload(await import_scene("SPARSE_R8_LEGACY.pvc2",
                                                                dry_run=False, confirm=True))
                    self.assertIn(legacy_import["status"], ("blocked", "needs_context"), legacy_import)
                    self.assertFalse(legacy_import["native_variant_created"], legacy_import)
                    self.assertIn("source arrays 1/2", " ".join(legacy_import.get("issues") or []), legacy_import)
                    self.assertEqual(ws.names(), before)

                    # 5) Cross-owner binding (T2 claims globals 1/2 owned by positional subarray 7) is refused.
                    def cross_owner(binding_in):
                        for cell in binding_in["tables"][1]["cells"]:
                            cell["global_string"] = 1 if cell["row"] <= 3 else 2
                        return binding_in

                    swap = copy.deepcopy(base_args)
                    swap["string_layout"] = cross_owner(copy.deepcopy(base_args["string_layout"]))
                    swap["output_name"] = "SPARSE_R8_SWAP.pvc2"
                    before = ws.names()
                    response = await create(swap)
                    rejected = response.is_error
                    if not rejected:
                        stored = _payload(response)
                        if stored["status"] in ("blocked", "failed"):
                            rejected = True
                        else:
                            outcome = await import_scene("SPARSE_R8_SWAP.pvc2", dry_run=False, confirm=True)
                            rejected = outcome.is_error
                            if not rejected:
                                payload = _payload(outcome)
                                rejected = (payload["status"] in ("blocked", "needs_context", "failed")
                                            and not payload.get("native_variant_created"))
                    self.assertTrue(rejected, "cross-owner binding must be refused before writing a variant")
                    self.assertEqual(ws.names(), before, "cross-owner binding has no VC side effect")
                    self.assertEqual(set((ws.root / "Results").iterdir()), results_before)

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

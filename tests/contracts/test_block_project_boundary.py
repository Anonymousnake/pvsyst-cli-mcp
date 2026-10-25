"""Public block-project preparation contract with an explicit local native fixture."""
import asyncio
import copy
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


class BlockProjectBoundary(unittest.TestCase):
    def test_public_preparation_and_preservation(self):
        fixture = os.environ.get("PVSYST_BLOCK_FIXTURE_WORKSPACE")
        if not fixture:
            self.skipTest("Set PVSYST_BLOCK_FIXTURE_WORKSPACE to the explicit two-block native fixture")
        asyncio.run(self.check(Path(fixture)))

    async def check(self, fixture):
        directory = Path(tempfile.mkdtemp(prefix="pvsyst-block-contract-"))
        try:
            projects = directory / "Projects"
            projects.mkdir()
            for extension in ("PRJ", "VC1"):
                shutil.copyfile(fixture / "Projects" / f"DUAL_ROOF_INPUT.{extension}",
                                projects / f"BLOCK_SOURCE.{extension}")
            original = {p.name: p.read_bytes() for p in projects.iterdir()}
            executable = directory / "PVsystCLI.exe"
            executable.write_bytes(b"Preparation must not execute native software")
            parameters = StdioServerParameters(command=sys.executable,
                args=["-B", str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO),
                env=dict(os.environ, PVSYST_WORKSPACE=str(directory), PVSYST_CLI=str(executable)))
            request = dict(source_project="BLOCK_SOURCE.PRJ", source_variant="VC1",
                new_project="BLOCK_RESULT.PRJ", blocks=[
                    dict(id="south-block", source_subarray_id=1, count=2),
                    dict(id="west-block", source_subarray_id=2, count=3)])
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def prepare(args):
                        reply = await session.call_tool("pvsyst_prepare_block_project", args)
                        return reply, None if reply.is_error else json.loads("\n".join(
                            item.text for item in reply.content if hasattr(item, "text")))

                    reply, preview = await prepare(request)
                    self.assertFalse(reply.is_error, reply.content)
                    self.assertEqual(preview["status"], "ready", preview)
                    self.assertEqual({p.name: p.read_bytes() for p in projects.iterdir()}, original)
                    self.assertEqual(preview["serialized_counts"]["totals"]["inverters"], 5)
                    self.assertEqual(preview["serialized_counts"]["totals"]["strings"], 5)
                    self.assertEqual([len(row["subarray_ids"]) for row in preview["mapping"]], [2, 3])
                    self.assertEqual([row["orientation_id"] for row in preview["mapping"]], [1, 2])

                    invalid = []
                    for mode in ("missing", "duplicate_source", "duplicate_label", "unknown_source",
                                 "limit", "bool", "float", "extra", "path"):
                        bad = copy.deepcopy(request)
                        if mode == "missing":
                            bad["blocks"].pop()
                        elif mode == "duplicate_source":
                            bad["blocks"][1]["source_subarray_id"] = 1
                        elif mode == "duplicate_label":
                            bad["blocks"][1]["id"] = "south-block"
                        elif mode == "unknown_source":
                            bad["blocks"][1]["source_subarray_id"] = 999
                        elif mode == "limit":
                            bad["blocks"][0]["count"] = 100
                        elif mode == "bool":
                            bad["blocks"][0]["count"] = True
                        elif mode == "float":
                            bad["blocks"][0]["count"] = 2.0
                        elif mode == "extra":
                            bad["overwrite"] = True
                        else:
                            bad["new_project"] = "../ESCAPE.PRJ"
                        bad["dry_run"] = False
                        invalid.append((mode, bad))
                    for mode, bad in invalid:
                        with self.subTest(invalid=mode):
                            reply, result = await prepare(bad)
                            self.assertTrue(reply.is_error or result["status"] == "blocked", result)
                            self.assertEqual({p.name: p.read_bytes() for p in projects.iterdir()}, original)

                    # Even a no-expansion request must reject active common controls.
                    source_path = projects / "BLOCK_SOURCE.VC1"
                    native = original[source_path.name]
                    system = b"PVObject_System=pvSystem"
                    begin = native.index(system)
                    end = native.index(b"End of PVObject pvSystem", begin)
                    control = native[begin:end]
                    self.assertEqual(control.count(b"Flags=$00"), 1)
                    active = control.replace(b"Flags=$00", b"Flags=$01000000")
                    source_path.write_bytes(native[:begin] + active + native[end:])
                    no_expansion = copy.deepcopy(request)
                    no_expansion["dry_run"] = False
                    for block in no_expansion["blocks"]:
                        block["count"] = 1
                    reply, refused = await prepare(no_expansion)
                    self.assertTrue(reply.is_error or refused["status"] == "blocked", refused)
                    self.assertEqual(sorted(p.name for p in projects.iterdir()), sorted(original))
                    self.assertEqual(source_path.read_bytes(), native[:begin] + active + native[end:])
                    source_path.write_bytes(native)

                    reply, prepared = await prepare(dict(request, dry_run=False))
                    self.assertFalse(reply.is_error, reply.content)
                    self.assertEqual(prepared["status"], "prepared", prepared)
                    self.assertEqual(prepared["mapping"], preview["mapping"])
                    self.assertFalse(prepared["native_consumption_verified"])
                    self.assertFalse(prepared["simulated"])
                    self.assertEqual(sorted(p.name for p in projects.iterdir()),
                        ["BLOCK_RESULT.PRJ", "BLOCK_RESULT.VC1", "BLOCK_SOURCE.PRJ", "BLOCK_SOURCE.VC1"])
                    published = {p.name: p.read_bytes() for p in projects.iterdir()}
                    self.assertEqual(published["BLOCK_RESULT.PRJ"], original["BLOCK_SOURCE.PRJ"])
                    for name, data in original.items():
                        self.assertEqual(published[name], data)
                    reply, refused = await prepare(dict(request, dry_run=False))
                    self.assertTrue(reply.is_error or refused["status"] == "blocked")
                    self.assertEqual({p.name: p.read_bytes() for p in projects.iterdir()}, published)
                    response = await session.call_tool("pvsyst_validate_variant_structure",
                        dict(project="BLOCK_RESULT.PRJ", variant="VC1"))
                    self.assertFalse(response.is_error, response.content)
                    structure = json.loads("\n".join(x.text for x in response.content if hasattr(x, "text")))
                    self.assertTrue(structure["valid"], structure)
                    self.assertEqual(len(structure["subarray_ids"]), 5)
                    self.assertEqual(structure["orientation_ids"], [1, 2])
                    self.assertEqual(sum(x["circuit_module_count"]
                        for x in structure["grid_circuit"]["subarrays"]), 90)
            self.assertEqual(executable.read_bytes(), b"Preparation must not execute native software")
        except BaseException:
            print(f"Block preparation failure inputs retained: {directory}", file=sys.stderr)
            raise
        else:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()

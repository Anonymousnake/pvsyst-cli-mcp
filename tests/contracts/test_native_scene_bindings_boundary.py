"""Read-only public saved-scene declaration contract, not source geometry verification."""
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


class NativeSceneBindingsBoundary(unittest.TestCase):
    def test_public_saved_binding_boundary(self):
        fixture = os.environ.get("PVSYST_NATIVE_SCENE_BINDING_FIXTURE")
        if not fixture:
            self.skipTest("Set PVSYST_NATIVE_SCENE_BINDING_FIXTURE to the explicit native workspace")
        asyncio.run(self.check(Path(fixture)))

    async def check(self, fixture):
        root = Path(tempfile.mkdtemp(prefix="pvsyst-native-bindings-"))
        succeeded = False
        try:
            (root / "Projects").mkdir()
            for name in ("GROUPED_ROOF_NATIVE.PRJ", "GROUPED_ROOF_NATIVE.VC1", "GROUPED_ROOF_NATIVE.VC2"):
                shutil.copy2(fixture / "Projects" / name, root / "Projects" / name)
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"Not executable: this contract must not query native CLI")
            original = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            members = set(root.rglob("*"))
            path = root / "Projects" / "GROUPED_ROOF_NATIVE.VC2"
            saved = path.read_bytes().decode("utf-8-sig")
            begin = saved.index("PVObject_1=pvShdArrayTable")
            end = saved.index("End of PVObject pvShdArrayTable", begin)
            block = saved[begin:end]

            def changed(old, new):
                self.assertEqual(block.count(old), 1)
                return saved[:begin] + block.replace(old, new) + saved[end:]

            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def inspect(variant="VC2"):
                        before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                        response = await session.call_tool("pvsyst_get_variant_parameters",
                            {"project": "GROUPED_ROOF_NATIVE.PRJ", "variant": variant})
                        self.assertFalse(response.is_error, response.content)
                        value = json.loads("\n".join(c.text for c in response.content if hasattr(c, "text")))
                        self.assertEqual(set(root.rglob("*")), members)
                        self.assertTrue(all(p.read_bytes() == data for p, data in before.items()))
                        return value["shading_scene"]

                    active = await inspect()
                    self.assertEqual(active["mode"], "active")
                    review = active["pv_bindings"]
                    self.assertEqual(review["status"], "stored_consistent", review)
                    self.assertEqual({g["orientation_id"] for g in review["groups"]}, {1, 2})
                    self.assertEqual([g["electrical_module_count"] for g in review["groups"]], [18, 18])
                    self.assertEqual([g["scene_module_count"] for g in review["groups"]], [18, 18])
                    self.assertIn("not house/PVCollada", review["coordinate_frame"])
                    self.assertFalse(review["source_geometry_verified"])
                    self.assertFalse(review["native_consumption_verified"])
                    self.assertFalse(review["generic_geometry_decoded"])
                    tables = {t["orientation_id"]: t for t in review["tables"]}
                    self.assertEqual(tables[1]["origin_m"], [-0.8, -0.395305, 7.56367])
                    self.assertEqual(tables[2]["origin_m"], [-70.395309, 0.8, 7.56367])
                    empty = await inspect("VC1")
                    self.assertEqual(empty["mode"], "empty")
                    self.assertNotIn("pv_bindings", empty)
                    for label, old, new, status in (
                        ("tilt", "DInclin=28.000000", "DInclin=29.000000", "needs_review"),
                        ("slope", "BaseSlope=0.000000", "BaseSlope=1.000000", "needs_review"),
                        ("modules", "NModInHeight=3", "NModInHeight=4", "needs_review"),
                        ("missing_reference", "NoOrient=2", "NoOrient=3", "needs_review"),
                        ("duplicate", "NoOrient=2", "NoOrient=2\n      NoOrient=2", "unsupported"),
                        ("nonfinite", "DOrigine=-70.395309,0.800000,7.563670", "DOrigine=nan,0,0", "unsupported"),
                        ("version", "Version=8.1.6", "Version=8.0.0", "unsupported"),
                    ):
                        path.write_bytes(changed(old, new).encode("utf-8-sig"))
                        broken = await inspect()
                        self.assertEqual(broken["mode"], "active", label)
                        self.assertEqual(broken["pv_bindings"]["status"], status, (label, broken))
                        self.assertTrue(broken["pv_bindings"]["issues"], label)
                        self.assertFalse(broken["pv_bindings"]["source_geometry_verified"])
                    path.write_bytes(changed("DOrigine=-70.395309,0.800000,7.563670", "DOrigine=300,400,500").encode("utf-8-sig"))
                    moved = (await inspect())["pv_bindings"]
                    self.assertEqual(moved["status"], "stored_consistent")
                    self.assertEqual(next(t for t in moved["tables"] if t["orientation_id"] == 2)["origin_m"], [300, 400, 500])
                    self.assertFalse(moved["source_geometry_verified"])
                    label = "Comment=" + tables[2]["values"]["Comment"]
                    path.write_bytes(changed(label, "Comment=End of TOrientGroup").encode("utf-8-sig"))
                    labelled = (await inspect())["pv_bindings"]
                    self.assertEqual(labelled["status"], "stored_consistent", labelled)
                    self.assertEqual(next(t for t in labelled["tables"] if t["orientation_id"] == 2)["values"]["Comment"],
                                     "End of TOrientGroup")
                    path.write_bytes(saved.replace("pvShdGenObject", "pvShdUnmappedObject").encode("utf-8-sig"))
                    unknown = await inspect()
                    self.assertEqual(unknown["mode"], "active")
                    self.assertEqual(unknown["pv_bindings"]["status"], "unsupported")
                    first, found, last = saved.rpartition("SubArrayId=2")
                    self.assertTrue(found)
                    path.write_bytes((first + "SubArrayId=1" + last).encode("utf-8-sig"))
                    before = path.read_bytes()
                    response = await session.call_tool("pvsyst_get_variant_parameters",
                        {"project": "GROUPED_ROOF_NATIVE.PRJ", "variant": "VC2"})
                    if not response.is_error:
                        invalid = json.loads("\n".join(c.text for c in response.content if hasattr(c, "text")))
                        self.assertNotEqual(invalid["shading_scene"]["pv_bindings"]["status"], "stored_consistent")
                    self.assertEqual(path.read_bytes(), before)
                    path.write_bytes(original[path])
            self.assertTrue(all(p.read_bytes() == data for p, data in original.items()))
            succeeded = True
        finally:
            if succeeded:
                shutil.rmtree(root)
            else:
                print("Retained saved binding failure fixture: " + str(root), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()

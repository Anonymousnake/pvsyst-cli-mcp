"""Public stored-layout integrity and strict option contract; no native execution."""
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


class ModuleLayoutBoundary(unittest.TestCase):
    def test_stored_layout_and_options(self):
        fixture = os.environ.get("PVSYST_MODULE_LAYOUT_WORKSPACE")
        if not fixture:
            self.skipTest("Set PVSYST_MODULE_LAYOUT_WORKSPACE to the explicit two-array native layout fixture")
        asyncio.run(self.check(Path(fixture)))

    async def check(self, fixture):
        root = Path(tempfile.mkdtemp(prefix="pvsyst-module-layout-"))
        succeeded = False
        try:
            for folder in ("Projects", "Models", "UserData", "Results"):
                (root / folder).mkdir()
            for name in ("COMPLEX_SHADING.PRJ", "COMPLEX_SHADING.VC2", "COMPLEX_SHADING.VC3"):
                shutil.copy2(fixture / "Projects" / name, root / "Projects" / name)
            shutil.copytree(fixture / "ComposPV", root / "ComposPV")
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"Not an executable")
            originals = {path: path.read_bytes() for path in (root / "Projects").iterdir()}
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        response = await session.call_tool(tool, args)
                        self.assertFalse(response.is_error, response.content)
                        return json.loads("\n".join(item.text for item in response.content if hasattr(item, "text")))

                    async def inspect(variant):
                        value = await call("pvsyst_get_variant_parameters", {"project": "COMPLEX_SHADING.PRJ", "variant": variant})
                        return value["shading_scene"]["module_layout"]

                    self.assertFalse((await inspect("VC2"))["configured"])
                    stored = await inspect("VC3")
                    self.assertTrue(stored["configured"])
                    self.assertEqual(stored["status"], "stored_consistent", stored)
                    self.assertEqual(stored["module_count"], 36)
                    self.assertEqual({row["subarray_id"] for row in stored["tables"]}, {1, 2})
                    self.assertFalse(stored["native_consumption_verified"])
                    path = root / "Projects" / "COMPLEX_SHADING.VC3"
                    source = path.read_bytes()
                    path.write_bytes(source.replace(b"ModLayoutGlobState=mlDefStrings", b"ModLayoutGlobState=mlDefInverters", 1))
                    advanced = await inspect("VC3")
                    self.assertEqual(advanced["status"], "stored_consistent", advanced)
                    self.assertEqual(advanced["global_state"], "mlDefInverters")
                    self.assertFalse(advanced["native_consumption_verified"])
                    path.write_bytes(source)
                    mutations = (
                        (b"Module_1=1,1,1,1,", b"Module_1=1,1,1,0,"),
                        (b"Module_2=2,1,2,1,", b"Module_2=2,1,1,1,"),
                        (b"NoField3DCreated=3", b"NoField3DCreated=9999"),
                        (b"NoOrient=2", b"NoOrient=3"),
                        (b"NoSubField=1", b"NoSubField=2"),
                        (b"SubFields, list of=2 PVObjects", b"SubFields, list of=3 PVObjects"),
                        (b"Modules, list of=18 tModule", b"Modules, list of=17 tModule"),
                    )
                    for old, new in mutations:
                        self.assertIn(old, source)
                        candidate = source.replace(old, new, 1)
                        path.write_bytes(candidate)
                        review = await inspect("VC3")
                        self.assertTrue(review["configured"])
                        self.assertEqual(review["status"], "needs_review", (old, review))
                        self.assertFalse(review["native_consumption_verified"])
                        self.assertEqual(path.read_bytes(), candidate)
                        path.write_bytes(source)
                    for invalid in (True, "invented_mode"):
                        response = await session.call_tool("pvsyst_import_house_pv_scene", {
                            "scene_name": "Unknown.pvc2", "variant_name": "Layout option contract",
                            "shading_mode": invalid})
                        self.assertTrue(response.is_error)
                    exported = await call("pvsyst_build_sfi", {"filename": "ElectricalShading.SFI", "variables": "shading"})
                    self.assertIn("viGlobShd", exported["variables"])
                    self.assertIn("viShdElec", exported["variables"])
                    self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))
                    self.assertEqual(list((root / "Results").iterdir()), [])
                    self.assertEqual(list((root / "UserData").iterdir()), [])
            succeeded = True
        finally:
            if succeeded:
                shutil.rmtree(root)
            else:
                print("Retained public Module Layout failure fixture: " + str(root), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()

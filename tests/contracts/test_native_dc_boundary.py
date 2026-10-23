"""Public native DC read/preflight boundary; no desktop interaction or native engine."""
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


class NativeDCBoundary(unittest.TestCase):
    def test_public_boundary(self):
        configured = os.environ.get("PVSYST_CONTRACT_WORKSPACE")
        if not configured:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the explicit native fixture workspace")
        asyncio.run(self.check(Path(configured)))

    async def check(self, configured):
        with tempfile.TemporaryDirectory(prefix="pvsyst-dc-contract-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            for suffix in ("PRJ", "VCD", "VCF", "VCG", "VCR", "VCS"):
                name = "LOSS_CONTROLS." + suffix
                shutil.copy2(configured / "Projects" / name, projects / name)
            original = {p: p.read_bytes() for p in projects.iterdir()}
            text = (projects / "LOSS_CONTROLS.VCF").read_bytes().decode("utf-8-sig")
            self.assertIn("RSCablageCh=61.520", text)
            (projects / "LOSS_CONTROLS.VCZ0").write_bytes(text.replace("RSCablageCh=61.520", "RSCablageCh=60.000").encode())
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"Not a native executable")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        result = await session.call_tool(tool, args)
                        self.assertFalse(result.is_error, result.content)
                        return json.loads("\n".join(x.text for x in result.content if hasattr(x, "text")))

                    for variant, mode, resistance, percent in (("VCF", "percent", "61.520", "2.50"),
                                                               ("VCG", "resistance", "45.000", "1.83")):
                        state = await call("pvsyst_get_variant_parameters", dict(project="LOSS_CONTROLS.PRJ", variant=variant))
                        self.assertEqual(state["dc_cabling"]["mode"], mode)
                        self.assertEqual(state["dc_cabling"]["resistance_mohm"], resistance)
                        self.assertEqual(state["dc_cabling"]["loss_percent"], percent)
                        result = await call("pvsyst_create_native_dc_cabling_variant", dict(project="LOSS_CONTROLS.PRJ",
                            source_variant=variant, dc=dict(mode=mode, value=2.5), variant_name="Native DC Contract"))
                        self.assertEqual(result["status"], "blocked")
                        self.assertEqual(result["source_dc"]["mode"], mode)
                        self.assertEqual(result["native_actions"], [])
                        self.assertFalse(result["variant_created"])
                    for source, value in (("VCD", 0.001), ("VCD", -1), ("VCD", 101), ("VCD", 1e100),
                                           ("VCR", 2.5), ("VCS", 2.5), ("VCZ0", 2.5)):
                        result = await call("pvsyst_create_native_dc_cabling_variant", dict(project="LOSS_CONTROLS.PRJ",
                            source_variant=source, dc=dict(mode="percent", value=value), variant_name="Native DC Contract"))
                        self.assertEqual(result["status"], "blocked")
                        self.assertEqual(result["native_actions"], [])
                        self.assertFalse(result["variant_created"])
                    legacy = await call("pvsyst_create_ac_cabling_variant", dict(project="LOSS_CONTROLS.PRJ",
                        source_variant="VCF", new_variant="VCLEGACY", ac={"length_m": 100}))
                    self.assertEqual(legacy["status"], "blocked")
                    self.assertFalse((projects / "LOSS_CONTROLS.VCLEGACY").exists())
                    bad = await session.call_tool("pvsyst_create_native_dc_cabling_variant", dict(project="LOSS_CONTROLS.PRJ",
                        source_variant="VCF", dc=dict(mode="percent", value="2.5"), variant_name="Native DC Contract"))
                    self.assertTrue(bad.is_error)
            self.assertTrue(all(p.read_bytes() == before for p, before in original.items()))
            self.assertEqual(set(p.name for p in projects.iterdir()), set(p.name for p in original) | {"LOSS_CONTROLS.VCZ0"})


if __name__ == "__main__":
    unittest.main()

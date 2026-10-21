"""Public single-MV night on/read/off contract and dependency/source guards."""
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


class MVNightContract(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip_and_native_profile_dependency_guards(self):
        configured = os.getenv("PVSYST_CONTRACT_WORKSPACE")
        if not configured:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the native fixture workspace")
        source = Path(configured) / "Projects"
        with tempfile.TemporaryDirectory(prefix="pvsyst-mv-night-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            for suffix in ("PRJ", "VCS", "VCT", "VCV", "VCW"):
                name = "LOSS_CONTROLS." + suffix
                shutil.copy2(source / name, projects / name)
            original = {path: path.read_bytes() for path in projects.iterdir()}
            text = (projects / "LOSS_CONTROLS.VCS").read_bytes().decode("utf-8-sig")
            for suffix, before, after in (("VCZ0", "NightDisconnect=False", "NightDisconnect=True"),
                                          ("VCZ1", "Version=8.1.6", "Version=9.9.9"),
                                          ("VCZ2", "SystemType=Grid", "SystemType=StandAlone"),
                                          ("VCZ3", "NStringCh=40", "NStringCh=41")):
                self.assertIn(before, text)
                (projects / ("LOSS_CONTROLS." + suffix)).write_bytes(text.replace(before, after).encode())
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"No native execution in public MV contract")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        result = await session.call_tool(tool, args)
                        self.assertFalse(result.is_error, result.content)
                        return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))

                    async def create(source_variant, target, night, dry_run=False):
                        return await call("pvsyst_create_mv_night_disconnect_variant", dict(project="LOSS_CONTROLS.PRJ",
                            source_variant=source_variant, new_variant=target, night_disconnect=night, dry_run=dry_run))

                    preview = await create("VCS", "VCY0", True, True)
                    self.assertEqual(preview["status"], "ready", preview)
                    self.assertFalse((projects / "LOSS_CONTROLS.VCY0").exists())
                    self.assertEqual((await create("VCS", "VCY0", True))["status"], "created")
                    state = await call("pvsyst_get_variant_parameters", {"project": "LOSS_CONTROLS.PRJ", "variant": "VCY0"})
                    self.assertEqual(state["transformers"]["layers"][0]["fields"]["NightDisconnect"], "True")
                    self.assertEqual(state["main_array"]["values"]["Flags"], "$21F00400")
                    self.assertEqual((await create("VCY0", "VCY1", False))["status"], "created")
                    self.assertEqual((projects / "LOSS_CONTROLS.VCY1").read_bytes(), original[projects / "LOSS_CONTROLS.VCS"])
                    count_state = await call("pvsyst_get_variant_parameters", {"project": "LOSS_CONTROLS.PRJ", "variant": "VCV"})
                    self.assertEqual(count_state["main_array"]["values"]["MVNbTransfos"], "2")
                    hv_state = await call("pvsyst_get_variant_parameters", {"project": "LOSS_CONTROLS.PRJ", "variant": "VCW"})
                    self.assertEqual(hv_state["transformers"]["count"], 2)
                    self.assertTrue(hv_state["transformers"]["nested"])
                    self.assertEqual(hv_state["transformers"]["layers"][0]["fields"]["IsHVTransformer"], "True")
                    for variant in ("VCV", "VCW", "VCZ0", "VCZ1", "VCZ2", "VCZ3"):
                        result = await create(variant, "VCBAD", True)
                        self.assertEqual(result["status"], "blocked", result)
                        self.assertFalse((projects / "LOSS_CONTROLS.VCBAD").exists())
            self.assertTrue(all(path.read_bytes() == data for path, data in original.items()))


if __name__ == "__main__":
    unittest.main()

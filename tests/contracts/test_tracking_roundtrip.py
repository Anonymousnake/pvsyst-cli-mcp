"""Native tracking create/read/create closure through the public MCP protocol."""
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


class TrackingRoundtripContract(unittest.IsolatedAsyncioTestCase):
    async def test_type_owned_partial_edits_preserve_source_and_reject_bad_pairs(self):
        configured = os.getenv("PVSYST_CONTRACT_WORKSPACE")
        if not configured:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the native fixture workspace")
        source = Path(configured) / "Projects"
        with tempfile.TemporaryDirectory(prefix="pvsyst-tracking-contract-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            for name in ("ORIENTATION_CONTROLS.PRJ", "ORIENTATION_CONTROLS.VCK",
                         "ORIENTATION_CONTROLS.VCQ", "ORIENTATION_CONTROLS.VCS"):
                shutil.copy2(source / name, projects / name)
            originals = {path: path.read_bytes() for path in projects.iterdir()}
            text = (projects / "ORIENTATION_CONTROLS.VCQ").read_bytes().decode("utf-8-sig")
            self.assertEqual(text.count("MinAzim=-150.0"), 1)
            (projects / "ORIENTATION_CONTROLS.VCZ0").write_bytes(text.replace("MinAzim=-150.0", "MinAzim=180.0").encode())
            executable = root / "PVsystCLI.exe"
            executable.write_bytes(b"No native execution in protocol contract")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            params = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO), env=environment)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        result = await session.call_tool(tool, args)
                        self.assertFalse(result.is_error, result.content)
                        return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))

                    async def create(source_variant, new_variant, tracking, dry_run=False):
                        return await call("pvsyst_create_tracking_variant", dict(project="ORIENTATION_CONTROLS.PRJ",
                            source_variant=source_variant, new_variant=new_variant, tracking=tracking, dry_run=dry_run))

                    legacy = {"axis_azimuth_deg": 10.1, "min_rotation_deg": -55.1, "max_rotation_deg": 50.2,
                              "backtracking": True, "irradiance_optimization": False}
                    self.assertEqual((await create("VCK", "VCY0", legacy))["status"], "created")
                    created = await create("VCQ", "VCY1", {"min_azimuth_deg": -120})
                    self.assertEqual(created["status"], "created")
                    state = await call("pvsyst_get_variant_parameters", {"project": "ORIENTATION_CONTROLS.PRJ", "variant": "VCY1"})
                    values = state["orientations"][0]["values"]
                    self.assertEqual(values["FieldType"], "TrackTwoAxis")
                    self.assertEqual(values["MinAzim"], "-120.0")
                    self.assertEqual(values["MaxAzim"], "150.0")
                    self.assertEqual((await create("VCY1", "VCY2", {"max_azimuth_deg": 120}))["status"], "created")
                    for variant, controls in (("VCQ", {"min_azimuth_deg": 151}),
                                               ("VCQ", {"wind_stow": True}),
                                               ("VCQ", {"min_tilt_deg": -5.01}),
                                               ("VCQ", {"min_tilt_deg": 1e100}),
                                               ("VCZ0", {"axis_azimuth_deg": 10})):
                        result = await create(variant, "VCBAD", controls)
                        self.assertEqual(result["status"], "blocked", result)
                        self.assertFalse((projects / "ORIENTATION_CONTROLS.VCBAD").exists())
                    frame = await create("VCS", "VCY3", {"frame_min_tilt_deg": -5}, dry_run=True)
                    self.assertEqual(frame["status"], "ready", frame)
                    self.assertFalse((projects / "ORIENTATION_CONTROLS.VCY3").exists())
            self.assertTrue(all(path.read_bytes() == before for path, before in originals.items()))


if __name__ == "__main__":
    unittest.main()

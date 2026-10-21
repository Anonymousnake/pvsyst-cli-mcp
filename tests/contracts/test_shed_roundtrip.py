"""Public native shed creation/read/revision contract, isolated from native execution."""
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


class ShedRoundtripContract(unittest.IsolatedAsyncioTestCase):
    async def test_scope_precision_and_type_preserving_revision(self):
        configured = os.getenv("PVSYST_CONTRACT_WORKSPACE")
        if not configured:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the native fixture workspace")
        source = Path(configured) / "Projects"
        with tempfile.TemporaryDirectory(prefix="pvsyst-shed-contract-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            for suffix in ("PRJ", "VCI", "VCJ"):
                name = "ORIENTATION_CONTROLS." + suffix
                shutil.copy2(source / name, projects / name)
            original = {path: path.read_bytes() for path in projects.iterdir()}
            text = (projects / "ORIENTATION_CONTROLS.VCI").read_bytes().decode("utf-8-sig")
            self.assertEqual(text.count("FieldTilt=20.0"), 1)
            for suffix, content in (("VCZ0", text.replace("Version=8.1.6", "Version=9.9.9")),
                                    ("VCZ1", text.replace("FieldTilt=20.0", "FieldTilt=15.123"))):
                (projects / ("ORIENTATION_CONTROLS." + suffix)).write_bytes(content.encode())
            executable = root / "PVsystCLI.exe"
            executable.write_bytes(b"No native execution in public contract")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO), env=environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, arguments):
                        result = await session.call_tool(tool, arguments)
                        self.assertFalse(result.is_error, result.content)
                        return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))

                    async def create(variant, target, geometry, dry_run=False):
                        return await call("pvsyst_create_shed_geometry_variant",
                            dict(project="ORIENTATION_CONTROLS.PRJ", source_variant=variant,
                                 new_variant=target, geometry=geometry, dry_run=dry_run))

                    self.assertEqual((await create("VCI", "VCY0", {"pitch_m": 6.5}, True))["status"], "ready")
                    self.assertFalse((projects / "ORIENTATION_CONTROLS.VCY0").exists())
                    self.assertEqual((await create("VCI", "VCY0", {"pitch_m": 6.5}))["status"], "created")
                    state = await call("pvsyst_get_variant_parameters", {"project": "ORIENTATION_CONTROLS.PRJ", "variant": "VCY0"})
                    self.assertEqual(state["orientations"][0]["values"]["Pitch"], "6.500")
                    self.assertEqual((await create("VCY0", "VCY1", {"field_tilt_deg": 15.1}))["status"], "created")
                    state = await call("pvsyst_get_variant_parameters", {"project": "ORIENTATION_CONTROLS.PRJ", "variant": "VCY1"})
                    self.assertEqual(state["orientations"][0]["values"]["FieldType"], "Sheds")
                    self.assertEqual(state["orientations"][0]["values"]["FieldTilt"], "15.1")
                    self.assertEqual((await create("VCJ", "VCY2", {"electrical_effect": True}))["status"], "created")
                    for variant, changes in (("VCI", {"field_tilt_deg": 15.123}),
                                              ("VCI", {"pitch_m": 0.0001}),
                                              ("VCI", {"collector_width_m": 0.02}),
                                              ("VCI", {"shed_count": 0}),
                                              ("VCZ0", {"pitch_m": 6.5}),
                                              ("VCZ1", {"field_tilt_deg": 15.1})):
                        result = await create(variant, "VCBAD", changes)
                        self.assertEqual(result["status"], "blocked", result)
                        self.assertFalse((projects / "ORIENTATION_CONTROLS.VCBAD").exists())
                    self.assertEqual((await create("VCI", "VCY0", {"pitch_m": 7}))["status"], "blocked")
            self.assertTrue(all(path.read_bytes() == data for path, data in original.items()))


if __name__ == "__main__":
    unittest.main()

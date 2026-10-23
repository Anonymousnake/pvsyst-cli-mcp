"""Public project-opener schema/path/failed-preflight preservation; no GUI execution."""
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


class NativeProjectBoundary(unittest.TestCase):
    def test_public_boundary(self):
        workspace = os.environ.get("PVSYST_CONTRACT_WORKSPACE")
        if not workspace:
            self.skipTest("Set the explicit PVSYST_CONTRACT_WORKSPACE fixture workspace")
        asyncio.run(self.check(Path(workspace)))

    async def check(self, workspace):
        with tempfile.TemporaryDirectory(prefix="pvsyst-open-contract-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            project = projects / "OpenControls.PRJ"
            shutil.copy2(workspace / "Projects" / "LOSS_CONTROLS.PRJ", project)
            original = project.read_bytes()
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"Not a native executable")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tool = next(t for t in (await session.list_tools()).tools if t.name == "pvsyst_open_native_project")
                    self.assertEqual(set(tool.input_schema["properties"]), {"project", "dry_run", "confirm"})
                    for args in ({"project": "OpenControls.PRJ"},
                                 {"project": "OpenControls.PRJ", "dry_run": False},
                                 {"project": "../OpenControls.PRJ", "dry_run": False, "confirm": True},
                                 {"project": "Missing.PRJ", "dry_run": False, "confirm": True}):
                        result = await session.call_tool("pvsyst_open_native_project", args)
                        self.assertFalse(result.is_error, result.content)
                        value = json.loads("\n".join(x.text for x in result.content if hasattr(x, "text")))
                        self.assertEqual(value["status"], "blocked")
                        self.assertFalse(value["owned"])
                        self.assertEqual(value["native_actions"], [])
                        self.assertFalse(value["writes_ini"])
                    for extra in ({"dry_run": "false"}, {"confirm": 1}, {"replace_existing": True}):
                        result = await session.call_tool("pvsyst_open_native_project", dict(project="OpenControls.PRJ", **extra))
                        self.assertTrue(result.is_error)
            self.assertEqual(project.read_bytes(), original)
            self.assertEqual([p.name for p in projects.iterdir()], ["OpenControls.PRJ"])


if __name__ == "__main__":
    unittest.main()

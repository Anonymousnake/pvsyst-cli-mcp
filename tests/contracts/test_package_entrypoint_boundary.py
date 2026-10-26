"""Public stdio launch contract: checkout compatibility, module and installed console."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]


class PackageEntrypointBoundary(unittest.TestCase):
    async def capture(self, command, source_checkout=False):
        with tempfile.TemporaryDirectory(prefix="pvsyst-entry-contract-") as temporary:
            root = Path(temporary) / "PVsyst8.1_Data"
            root.mkdir()
            binary = Path(temporary) / "PVsystCLI.exe"
            binary.write_bytes(b"Public entry contract must never execute native software")
            env = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(binary),
                       PYTHONDONTWRITEBYTECODE="1")
            env.pop("PYTHONPATH", None)
            if source_checkout:
                env["PYTHONPATH"] = str(REPO / "src")
            parameters = StdioServerParameters(command=command[0], args=command[1:],
                                               cwd=temporary, env=env)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    names = {tool.name for tool in tools}
                    self.assertTrue({"pvsyst_capabilities", "pvsyst_run_engineering_task",
                                     "pvsyst_review_engineering_brief", "pvsyst_run_house_task",
                                     "pvsyst_prepare_block_project"}.issubset(names))
                    reply = await session.call_tool("pvsyst_review_engineering_brief", {
                        "brief": {"sources": [{"id": "source", "title": "Declared fixture",
                                              "locator": "Public entry contract"}],
                                  "claims": [{"id": "count", "field": "module_count",
                                              "scope": "project", "value": 1, "unit": "count",
                                              "source_id": "source", "pdf_page": 1,
                                              "excerpt": "One declared module"}]}})
                    self.assertFalse(reply.is_error, reply.content)
                    value = json.loads("\n".join(item.text for item in reply.content
                                                if hasattr(item, "text")))
                    self.assertIn(value["status"], ("blocked", "needs_review"))
                    self.assertFalse(value["native_consumption_verified"])
                    unknown = await session.call_tool("pvsyst_review_engineering_brief",
                                                      {"brief": {}, "unexpected": True})
                    self.assertTrue(unknown.is_error)
                    self.assertIn("Unknown argument", "\n".join(
                        item.text for item in unknown.content if hasattr(item, "text")))
            self.assertEqual(list(root.iterdir()), [])
            return {"tools": sorted([tool.model_dump(mode="json", exclude_none=True)
                                      for tool in tools], key=lambda item: item["name"]),
                    "brief": value}

    def test_checkout_module_and_legacy_launch(self):
        async def check():
            module = await self.capture([sys.executable, "-m", "pvsyst_auto"], True)
            legacy = await self.capture([sys.executable, str(REPO / "pvsyst_mcp_server.py")])
            self.assertEqual(module, legacy)
        asyncio.run(check())

    def test_installed_console_and_module_launch(self):
        python = os.environ.get("PVSYST_PACKAGE_INSTALLED_PYTHON")
        console = os.environ.get("PVSYST_PACKAGE_INSTALLED_CONSOLE")
        if not python or not console:
            self.skipTest("Provide explicit wheel-installed Python and console executable")
        self.assertTrue(Path(python).is_file())
        self.assertTrue(Path(console).is_file())

        async def check():
            module = await self.capture([python, "-m", "pvsyst_auto"])
            entry = await self.capture([console])
            source = await self.capture([sys.executable, "-m", "pvsyst_auto"], True)
            self.assertEqual(module, entry)
            self.assertEqual(source, entry)
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()

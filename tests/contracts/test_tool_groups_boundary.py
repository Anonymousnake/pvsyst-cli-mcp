"""Public MCP tool-group discovery and call contract; never executes PVsyst."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]


class ToolGroupsBoundary(unittest.TestCase):
    def test_explicit_groups_preserve_default_and_strict_calls(self):
        asyncio.run(self.check())

    async def check(self):
        directory = Path(tempfile.mkdtemp(prefix="pvsyst-groups-contract-"))
        try:
            workspace = directory / "PVsyst8.1_Data"
            workspace.mkdir()
            binary = directory / "PVsystCLI.exe"
            binary.write_bytes(b"Tool discovery must never execute native software")
            env = dict(os.environ, PVSYST_WORKSPACE=str(workspace), PVSYST_CLI=str(binary),
                       PYTHONPATH=str(REPO / "src"), PYTHONDONTWRITEBYTECODE="1")
            env.pop("PVSYST_TOOL_GROUPS", None)

            async def capture(groups, module=False):
                current = dict(env)
                if groups is not None:
                    current["PVSYST_TOOL_GROUPS"] = groups
                args = ["-m", "pvsyst_auto"] if module else [str(REPO / "pvsyst_mcp_server.py")]
                parameters = StdioServerParameters(command=sys.executable, args=args,
                                                   cwd=str(directory), env=current)
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = sorted([item.model_dump(mode="json", by_alias=True, exclude_none=True)
                                        for item in (await session.list_tools()).tools],
                                       key=lambda item: item["name"])
                        if groups is not None:
                            reply = await session.call_tool("pvsyst_clone_project", {
                                "source_project": "SOURCE.PRJ", "new_project": "NEW.PRJ"})
                            self.assertTrue(reply.is_error)
                            text = "\n".join(item.text for item in reply.content if hasattr(item, "text"))
                            self.assertRegex(text.lower(), r"unknown tool|not found|not registered")
                        if groups and "engineering" in groups:
                            reply = await session.call_tool("pvsyst_review_engineering_brief", {
                                "brief": {}, "unexpected": True})
                            self.assertTrue(reply.is_error)
                            self.assertIn("Unknown argument", "\n".join(
                                item.text for item in reply.content if hasattr(item, "text")))
                return tools

            full = await capture(None)
            self.assertIn("pvsyst_clone_project", {item["name"] for item in full})
            selected = await capture("setup, engineering,results", module=True)
            expected = [item for item in full if item["description"].split("]", 1)[0][1:]
                        in {"Setup & License", "Engineering", "Results"}]
            self.assertEqual(selected, expected)
            results = await capture("results")
            self.assertEqual(results, [item for item in full
                                      if item["description"].startswith("[Results]")])
            self.assertEqual(list(workspace.iterdir()), [])
            for index, invalid in enumerate(("", "engineering,", "unknown", "results,results")):
                output = directory / f"invalid-{index}.txt"
                with output.open("w", encoding="utf-8") as log:
                    process = subprocess.run([sys.executable, "-m", "pvsyst_auto"],
                        cwd=directory, env={**env, "PVSYST_TOOL_GROUPS": invalid},
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=30)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("PVSYST_TOOL_GROUPS requires", output.read_text(encoding="utf-8"))
        except BaseException:
            print(f"Retained tool-group contract case: {directory}")
            raise
        else:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()

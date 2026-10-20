"""Public report-selection preflight; no native executable or model fixtures."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]


class ReportPagesContract(unittest.IsolatedAsyncioTestCase):
    async def test_selection_is_public_and_invalid_pages_stop_before_writes(self):
        with tempfile.TemporaryDirectory(prefix="pvsyst-report-contract-") as directory:
            root = Path(directory)
            executable = root / "PVsystCLI.exe"
            executable.write_bytes(b"No native execution in preflight contract")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO), env=environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {item.name: item for item in (await session.list_tools()).tools}
                    self.assertIn("report_pages", tools["pvsyst_simulate_template"].input_schema["properties"])
                    for pages in (["summary", "losses"], [], ["SUMMARY"], [1], "summary"):
                        for design in (False, True):
                            tool = "pvsyst_simulate_design" if design else "pvsyst_simulate_template"
                            arguments = {"spec": {"report_pages": pages}} if design else {"report_pages": pages}
                            result = await session.call_tool(tool, arguments)
                            if isinstance(pages, str) or pages == [1]:
                                self.assertTrue(result.is_error)
                                continue
                            self.assertFalse(result.is_error, result.content)
                            payload = json.loads("\n".join(item.text for item in result.content
                                                           if hasattr(item, "text")))
                            self.assertEqual(payload["status"], "blocked")
                            page_issues = [item for item in payload["issues"]
                                           if item.get("field") == "report_pages"]
                            if pages == ["summary", "losses"]:
                                self.assertFalse(page_issues)
                                if not design:
                                    self.assertEqual(payload["request"]["report_pages"], pages)
                            else:
                                self.assertTrue(page_issues)
            self.assertEqual(set(root.iterdir()), {executable})


if __name__ == "__main__":
    unittest.main()

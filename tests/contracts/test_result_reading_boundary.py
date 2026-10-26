"""Public native CSV reading compatibility: dates, missing values, pages and strict compare."""
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


class ResultReadingBoundary(unittest.TestCase):
    def test_timestamp_compatibility_missing_values_and_strict_comparison(self):
        asyncio.run(self.check())

    async def check(self):
        directory = Path(tempfile.mkdtemp(prefix="pvsyst-result-contract-"))
        try:
            root = directory / "PVsyst8.1_Data"
            results = root / "Results"
            results.mkdir(parents=True)
            executable = directory / "PVsystCLI.exe"
            executable.write_bytes(b"Reading existing CSV must not execute PVsyst")
            rows = [("01/01/24 00:00", "1", "0.5"),
                    ("1/1/24 1:0", "2", "0.6"),
                    ("29/02/24 23:59", "", "0.7"),
                    ("31/12/68 23:00", "3", ""),
                    ("01/01/69 00:00", "4", "0.8"),
                    ("29/02/23 12:00", "999", "1"),
                    ("2024-01-01T00:00", "999", "1")]
            for name, separator in (("semicolon.csv", ";"), ("comma.csv", ",")):
                text = separator.join(("date", "E_Grid", "PR")) + "\n"
                text += separator.join(("", "kW", "ratio")) + "\n\n"
                text += "\n".join(separator.join(row) for row in rows) + "\n"
                (results / name).write_text(text, encoding="utf-8-sig")
            malformed = "date;E_Grid;PR\n;kW;ratio\n01/01/24 00:00;1;0.5\n29/02/23 00:00;2;0.5\n"
            for name in ("strict-a.csv", "strict-b.csv"):
                (results / name).write_text(malformed, encoding="utf-8")
            frozen = {path.name: path.read_bytes() for path in results.iterdir()}
            env = dict(os.environ, PVSYST_CLI=str(executable), PVSYST_WORKSPACE=str(root),
                       PYTHONDONTWRITEBYTECODE="1")
            env.pop("PVSYST_TOOL_GROUPS", None)
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(directory), env=env)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        reply = await session.call_tool(tool, args)
                        self.assertFalse(reply.is_error, reply.content)
                        return json.loads("\n".join(item.text for item in reply.content
                                                    if hasattr(item, "text")))

                    for name in ("semicolon.csv", "comma.csv"):
                        page = await call("pvsyst_read_rows", {"csv_name": name,
                                          "offset": 1, "limit": 2})
                        self.assertEqual(page["total"], 5)
                        self.assertEqual(page["rows"], [["2024-01-01 01:00:00", 2.0, 0.6],
                                                       ["2024-02-29 23:59:00", None, 0.7]])
                        pivot = await call("pvsyst_read_rows", {"csv_name": name,
                                           "offset": 3, "limit": 2})
                        self.assertEqual([row[0] for row in pivot["rows"]],
                                         ["2068-12-31 23:00:00", "1969-01-01 00:00:00"])
                        summary = await call("pvsyst_read_results", {"csv_name": name})
                        self.assertEqual(summary["rows"], 5)
                        self.assertEqual(summary["summary"]["E_Grid"]["sum"], 10)
                        self.assertEqual(summary["summary"]["E_Grid"]["missing_samples"], 1)
                        self.assertIsNone(summary["summary"]["E_Grid"]["energy_kwh"])
                    reply = await session.call_tool("pvsyst_read_results", {
                        "csv_name": "strict-a.csv", "compare_to": "strict-b.csv"})
                    self.assertTrue(reply.is_error)
                    self.assertIn("Invalid timestamp", "\n".join(
                        item.text for item in reply.content if hasattr(item, "text")))
            self.assertEqual({path.name: path.read_bytes() for path in results.iterdir()}, frozen)
        except BaseException:
            print(f"Retained result-reading contract case: {directory}")
            raise
        else:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()

"""Public batch-summary reader compatibility for the MCP tool ``pvsyst_read_batch_results``.

Covers the external naming requirement driven only through the public stdio server:
documented indexed ``<prefix>BatchResults_<n>.CSV`` names (0 included), the existing
unindexed ``<prefix>BatchResults.CSV`` name, and rejection of malformed names and path
escapes. The fixture is a sanitized native-format summary (a plain ``Ident``/``Create
hourly`` header plus two ``SIM_`` rows) and carries no private workspace paths, routes or
component-database references. Reading is pure: the fake ``PVsystCLI.exe`` is never run.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]

SUMMARY = ("Ident;Create hourly;Error;E_Grid;PR\n"
           "SIM_1;sim_1.csv;;29624;0.8105\n"
           "SIM_2;sim_2.csv;;29564;0.8083\n")


class BatchSummaryReaderBoundary(unittest.TestCase):
    def test_indexed_and_unindexed_names_with_isolated_negatives(self):
        asyncio.run(self.check())

    async def check(self):
        directory = Path(tempfile.mkdtemp(prefix="pvsyst-batch-summary-contract-"))
        try:
            root = directory / "PVsyst8.1_Data"
            batch = root / "UserBatch"
            batch.mkdir(parents=True)
            executable = directory / "PVsystCLI.exe"
            executable.write_bytes(b"Reading a batch summary must not execute PVsyst")
            indexed = "_DEMO_Residential_BatchResults_0.CSV"
            unindexed = "_DEMO_Residential_BatchResults.CSV"
            for name in (indexed, unindexed, "notes.csv"):
                (batch / name).write_text(SUMMARY, encoding="utf-8")
            (batch / "sub").mkdir()
            (batch / "sub" / indexed).write_text(SUMMARY, encoding="utf-8")
            frozen = {path.relative_to(root).as_posix(): path.read_bytes()
                      for path in root.rglob("*") if path.is_file()}
            env = dict(os.environ, PVSYST_CLI=str(executable), PVSYST_WORKSPACE=str(root),
                       PYTHONDONTWRITEBYTECODE="1")
            env.pop("PVSYST_TOOL_GROUPS", None)
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(directory), env=env)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def read(name):
                        reply = await session.call_tool("pvsyst_read_batch_results",
                                                        {"summary_name": name})
                        text = "\n".join(item.text for item in reply.content
                                         if hasattr(item, "text"))
                        return (None, text) if reply.is_error else (json.loads(text), None)

                    for name in (indexed, unindexed):
                        data, error = await read(name)
                        self.assertIsNone(error, error)
                        self.assertEqual(data["columns"], ["E_Grid", "PR"])
                        self.assertEqual([row["id"] for row in data["scenarios"]],
                                         ["SIM_1", "SIM_2"])
                        self.assertEqual([row["values"]["E_Grid"] for row in data["scenarios"]],
                                         [29624.0, 29564.0])
                        self.assertEqual([row["values"]["PR"] for row in data["scenarios"]],
                                         [0.8105, 0.8083])
                        self.assertEqual([row["hourly_file"] for row in data["scenarios"]],
                                         ["sim_1.csv", "sim_2.csv"])
                    for name in ("notes.csv", "_DEMO_Residential_BatchParams_0.CSV",
                                 "summary.txt", "../" + indexed, "sub/" + indexed):
                        data, error = await read(name)
                        self.assertIsNone(data)
                        self.assertIsNotNone(error)
            after = {path.relative_to(root).as_posix(): path.read_bytes()
                     for path in root.rglob("*") if path.is_file()}
            self.assertEqual(after, frozen)
        except BaseException:
            print(f"Retained batch-summary contract case: {directory}")
            raise
        else:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()

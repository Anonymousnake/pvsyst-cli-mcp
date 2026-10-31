"""Public batch-summary reader compatibility for the MCP tool ``pvsyst_read_batch_results``.

Covers the external naming requirement driven only through the public stdio server:
documented indexed ``<prefix>BatchResults_<n>.CSV`` names (0 included), the existing
unindexed ``<prefix>BatchResults.CSV`` name, and rejection of malformed names and path
escapes. The fixture is a sanitized native-format summary (a plain ``Ident``/``Create
hourly`` header plus two ``SIM_`` rows) and carries no private workspace paths, routes or
component-database references. Reading is pure: the fake ``PVsystCLI.exe`` is never run.
A separate opt-in class exercises the REAL indexed-batch completion plus public readback
exactly once (one run-simulation request) and is skipped unless explicitly enabled.
"""
import asyncio
import json
import math
import os
import re
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


class RealNativeIndexedBatchNumerical(unittest.TestCase):
    """Opt-in REAL native indexed-batch completion + public readback (skipped by default).

    The permanent external gate for the production completion path: one
    ``pvsyst_run_simulation`` batch call must detect the freshly produced INDEXED
    ``<prefix>Results_<n>.CSV`` summary (the legacy unindexed glob used to miss it),
    then one pure ``pvsyst_read_batch_results`` readback returns the same SIM_1/SIM_2
    scenarios as finite numbers. Hourly CSVs must be present and complete; no PDF;
    the five source inputs keep their exact bytes. This consumes a license execution,
    so it runs only when explicitly opted in. Budget: one run-simulation request.

    Env (each required; anything missing -> skip, never a silent green):
      PVSYST_CONTRACT_REAL_NATIVE=1
      PVSYST_CONTRACT_CLI=<installed PVsystCLI.exe>
      PVSYST_CONTRACT_WORKSPACE=<PVsyst8.1_Data with Projects + UserBatch params/rvt>
      PVSYST_CONTRACT_BATCH_JSON={"project":"...","variant":"VC1",
          "batch_params_name":"...Params_0.CSV","batch_rvt_name":"...RVT"}
    """

    def setUp(self):
        if os.name != "nt":
            self.skipTest("real native batch completion is Windows-only")
        if os.environ.get("PVSYST_CONTRACT_REAL_NATIVE") != "1":
            self.skipTest("Set PVSYST_CONTRACT_REAL_NATIVE=1 to opt into the indexed batch gate")
        self.cli = os.environ.get("PVSYST_CONTRACT_CLI")
        self.workspace = os.environ.get("PVSYST_CONTRACT_WORKSPACE")
        raw = os.environ.get("PVSYST_CONTRACT_BATCH_JSON")
        if not self.cli or not Path(self.cli).is_file():
            self.skipTest("Set PVSYST_CONTRACT_CLI to a real installed PVsystCLI.exe")
        if not self.workspace or not Path(self.workspace).is_dir():
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to a real PVsyst8.1_Data workspace")
        if not raw:
            self.skipTest("Set PVSYST_CONTRACT_BATCH_JSON to the batch fixture request")
        try:
            spec = json.loads(raw)
        except ValueError as exc:
            self.fail("PVSYST_CONTRACT_BATCH_JSON is not valid JSON: %s" % exc)
        if (not isinstance(spec, dict) or not spec.get("project")
                or not spec.get("variant") or not spec.get("batch_params_name")):
            self.fail("PVSYST_CONTRACT_BATCH_JSON needs project, variant, batch_params_name")
        self.request = {"project": spec["project"], "variant": spec["variant"],
                        "batch_params_name": spec["batch_params_name"],
                        "batch_rvt_name": spec.get("batch_rvt_name", "")}

    def test_indexed_batch_completion_and_public_readback(self):
        asyncio.run(self.check())

    async def check(self):
        ws = Path(self.workspace)
        batch_dir = ws / "UserBatch"
        before_names = {p.name for p in batch_dir.iterdir() if p.is_file()}
        # Source inputs (project file + its variant + UserBatch params/rvt) must keep
        # their exact bytes: no fake substitution to make completion appear.
        watched = [p for p in (ws / "Projects").iterdir() if p.is_file()]
        watched += [p for p in batch_dir.iterdir() if p.is_file()]
        before_bytes = {str(p): p.read_bytes() for p in watched if p.is_file()}

        env = dict(os.environ, PVSYST_CLI=self.cli, PVSYST_WORKSPACE=self.workspace,
                   PYTHONDONTWRITEBYTECODE="1")
        env.pop("PVSYST_TOOL_GROUPS", None)
        env.pop("PVSYST_BUILTIN_COMPONENTS", None)
        server = StdioServerParameters(command=sys.executable,
            args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO), env=env)
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                reply = await session.call_tool("pvsyst_run_simulation", dict(self.request))
                self.assertFalse(reply.is_error, reply.content)
                value = json.loads("\n".join(item.text for item in reply.content
                                             if hasattr(item, "text")))
                summary = value.get("batch_summary")
                self.assertIsInstance(summary, str)
                summary_name = Path(summary).name
                # Fresh INDEXED <prefix>Results_<n>.CSV (index present, not the legacy bare name).
                self.assertRegex(summary_name, r"(?i)Results_\d+\.CSV$")
                self.assertNotIn(summary_name, before_names,
                                 "completion summary must be freshly produced, not pre-existing")
                # Batch numerics: two scenarios, no PDF, all hourly present.
                self.assertIsNone(value.get("pdf"))
                self.assertEqual(value.get("batch_runs"), 2)
                hourly = value.get("batch_hourly") or []
                self.assertEqual(len(hourly), 2)
                for path in hourly:
                    self.assertTrue(Path(path).is_file(), "missing hourly CSV: %s" % path)

                readback = await session.call_tool("pvsyst_read_batch_results",
                                                   {"summary_name": summary_name})
                self.assertFalse(readback.is_error, readback.content)
                data = json.loads("\n".join(item.text for item in readback.content
                                            if hasattr(item, "text")))
                self.assertEqual(data["columns"], ["E_Grid", "PR"])
                self.assertEqual([row["id"] for row in data["scenarios"]], ["SIM_1", "SIM_2"])
                for row in data["scenarios"]:
                    for column in ("E_Grid", "PR"):
                        number = row["values"][column]
                        self.assertIsInstance(number, (int, float))
                        self.assertTrue(math.isfinite(number), "%s not finite" % column)

        # Hourly completeness/finiteness from the produced artifacts (pure local read,
        # not an extra MCP tool call): 8760 finite hourly rows, no NaN, no gaps.
        sys.path.insert(0, str(REPO / "src"))
        from pvsyst_auto.native.cli import summarize
        for path in hourly:
            stats = summarize(path, "E_Grid")
            self.assertEqual(stats["rows"], 8760)
            self.assertTrue(stats["complete"], "hourly column has non-finite values")
            self.assertEqual(stats["missing_samples"], 0)

        after_bytes = {key: Path(key).read_bytes() for key in before_bytes}
        self.assertEqual(after_bytes, before_bytes, "source input bytes changed during the run")


if __name__ == "__main__":
    unittest.main()

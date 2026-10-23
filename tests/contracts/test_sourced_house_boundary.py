"""Public cited-input draft/review/schema contract. No executable CLI, GUI or annual run."""
import asyncio
import copy
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


class SourcedHouseBoundary(unittest.TestCase):
    def test_public_source_boundary(self):
        fixture = os.environ.get("PVSYST_SOURCED_HOUSE_FIXTURE")
        workspace = os.environ.get("PVSYST_SOURCED_COMPONENT_WORKSPACE")
        if not fixture or not workspace:
            self.skipTest("Explicit cited-source JSON and component workspace required; no native claim")
        asyncio.run(self.check(Path(fixture), Path(workspace)))

    def test_completed_readonly_resume(self):
        fixture = os.environ.get("PVSYST_SOURCED_HOUSE_FIXTURE")
        workspace = os.environ.get("PVSYST_SOURCED_COMPONENT_WORKSPACE")
        if not fixture or not workspace:
            self.skipTest("Explicit completed source-house fixture required")
        asyncio.run(self.completed_check(Path(fixture), Path(workspace)))

    async def completed_check(self, fixture, workspace):
        spec = json.loads((fixture / "declared-sourced-house-spec.json").read_text())
        source = workspace / "Results" / f"{Path(spec['design']['new_project']).stem}_prepare_{spec['design']['year']}.json"
        child = workspace / "Results" / (Path(spec["new_project"]).stem + "_house_task.json")
        if not child.is_file() or json.loads(child.read_text()).get("phase") != "completed":
            self.skipTest("Completed native house fixture absent; no completed-resume claim")
        before = {path: path.read_bytes() for path in (source, child)}
        members = sorted(workspace.rglob("*"))
        with tempfile.TemporaryDirectory(prefix="pvsyst-source-resume-") as directory:
            fake = Path(directory) / "PVsystCLI.exe"
            fake.write_bytes(b"Not an executable")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(workspace), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    reply = await session.call_tool("pvsyst_run_sourced_house_task",
                        dict(spec=spec, resume=True, retry_simulation=True, accept_source_review=True))
                    self.assertFalse(reply.is_error, reply.content)
                    value = json.loads("\n".join(x.text for x in reply.content if hasattr(x, "text")))
                    self.assertEqual(value["status"], "ready", value)
                    self.assertFalse(value["native_consumption_verified"])
                    self.assertTrue(value["source_preparation"]["needs_review"])
                    self.assertEqual(len(value["house"]["runs"]), len(json.loads(before[child])["runs"]))
                    self.assertEqual(members, sorted(workspace.rglob("*")))
                    self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))

    async def check(self, fixture, workspace):
        spec = json.loads((fixture / "declared-sourced-house-spec.json").read_text())
        with tempfile.TemporaryDirectory(prefix="pvsyst-source-boundary-") as directory:
            root = Path(directory) / "PVsyst8.1_Data"
            for kind, folder in (("module", "ComposPV/PVmodules"), ("inverter", "ComposPV/Inverters")):
                name = spec["design"][kind]["file_name"]
                self.assertEqual(Path(name).name, name)
                target = root / folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(workspace / folder / name, target)
            original = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            before = sorted(root.rglob("*"))
            fake = Path(directory) / "PVsystCLI.exe"
            fake.write_bytes(b"Not an executable")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tool = next(t for t in (await session.list_tools()).tools if t.name == "pvsyst_run_sourced_house_task")
                    self.assertEqual(set(tool.input_schema["properties"]),
                        {"spec", "dry_run", "confirm", "resume", "retry_simulation", "accept_source_review"})
                    async def call(value, **flags):
                        reply = await session.call_tool(tool.name, dict(spec=value, **flags))
                        self.assertFalse(reply.is_error, reply.content)
                        return json.loads("\n".join(x.text for x in reply.content if hasattr(x, "text")))
                    missing = copy.deepcopy(spec)
                    missing.update(sources={}, declarations=[])
                    for flags in ({}, {"dry_run": False, "confirm": True, "accept_source_review": True}):
                        value = await call(missing, **flags)
                        self.assertEqual(value["status"], "needs_context")
                        self.assertTrue(value["missing_fields"])
                        self.assertIsInstance(value["source_binding"]["electrical_design"], dict)
                        self.assertFalse(value["native_consumption_verified"])
                    for flags in ({}, {"dry_run": False, "confirm": True}):
                        value = await call(spec, **flags)
                        self.assertEqual(value["status"], "needs_review")
                        self.assertTrue(value["source_binding"]["needs_review"])
                        self.assertIsNone(value["source_preparation"])
                        self.assertIsNone(value["house"])
                    bad = copy.deepcopy(spec)
                    bad["declarations"][0]["source_value_text"] = "mismatched numeric token"
                    value = await call(bad, accept_source_review=True)
                    self.assertEqual(value["status"], "needs_context")
                    self.assertTrue(value["issues"])
                    for change in ({"dry_run": "false"}, {"accept_source_review": 1}, {"unexpected": True}):
                        reply = await session.call_tool(tool.name, dict(spec=spec, **change))
                        self.assertTrue(reply.is_error)
                    self.assertEqual(before, sorted(root.rglob("*")))
                    self.assertTrue(all(path.read_bytes() == raw for path, raw in original.items()))


if __name__ == "__main__":
    unittest.main()

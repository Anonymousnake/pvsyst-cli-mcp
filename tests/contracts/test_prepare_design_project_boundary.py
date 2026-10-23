"""Public strict/missing-input/non-simulation status contract, without a native executable."""
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


class PrepareDesignProjectBoundary(unittest.TestCase):
    def test_public_boundary(self):
        asyncio.run(self.check())

    def test_native_prepared_project_without_annual_outputs(self):
        fixture = os.environ.get("PVSYST_PREPARATION_FIXTURE")
        binary = os.environ.get("PVSYST_PREPARATION_CLI")
        if not fixture or not binary:
            self.skipTest("Explicit native preparation fixture and CLI required; no native claim")
        asyncio.run(self.native_check(Path(fixture), Path(binary)))

    async def native_check(self, fixture, binary):
        spec = json.loads((fixture / "declared-project-spec.json").read_text())
        source = fixture / "PVsyst8.1_Data"
        self.assertTrue(binary.is_file())
        self.assertEqual(spec["weather"]["mode"], "existing")
        self.assertFalse(spec["weather"].get("weather_manifest_name"))
        spec["new_project"] = "ContractPrepared.PRJ"
        files = [("Projects", spec["template_project"]),
                 ("Projects", Path(spec["template_project"]).stem + "." + spec["variant"]),
                 ("ComposPV/PVmodules", spec["module"]["file_name"]),
                 ("ComposPV/Inverters", spec["inverter"]["file_name"]),
                 ("Sites", spec["weather"]["sit_name"]), ("Meteo", spec["weather"]["met_name"])]
        directory = Path(tempfile.mkdtemp(prefix="pvsyst-prepare-native-"))
        try:
            root = directory / "PVsyst8.1_Data"
            originals = {}
            for folder, name in files:
                self.assertEqual(Path(name).name, name)
                path = root / folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                originals[path] = (source / folder / name).read_bytes()
                shutil.copy2(source / folder / name, path)
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(binary)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    reply = await session.call_tool("pvsyst_prepare_design_project", {"spec": spec, "dry_run": False})
                    self.assertFalse(reply.is_error, reply.content)
                    value = json.loads("\n".join(x.text for x in reply.content if hasattr(x, "text")))
                    self.assertEqual(value["status"], "prepared", value)
                    self.assertTrue(value["prepared"])
                    self.assertFalse(value["simulated"])
                    self.assertFalse(value["native_consumption_verified"])
                    self.assertIsNone(value["metrics"])
                    self.assertEqual(value["prepared_project"]["project"], spec["new_project"])
                    self.assertTrue((root / "Projects" / spec["new_project"]).is_file())
                    for ext in ("*.csv", "*.pdf", "*.SFI"):
                        self.assertEqual(list(root.rglob(ext)), [])
                    stored = json.loads(Path(value["prepared_project"]["manifest"]).read_text())
                    self.assertEqual(stored["status"], "prepared")
                    self.assertFalse(stored["simulated"])
                    self.assertEqual(stored["preparation_context"]["request"]["new_project"], spec["new_project"])
                    self.assertTrue(all(path.read_bytes() == raw for path, raw in originals.items()))
        except BaseException:
            print(f"Native preparation failure artifacts retained: {directory}", file=sys.stderr)
            raise
        else:
            shutil.rmtree(directory)

    async def check(self):
        with tempfile.TemporaryDirectory(prefix="pvsyst-prepare-project-") as directory:
            root = Path(directory) / "PVsyst8.1_Data"
            root.mkdir()
            binary = Path(directory) / "PVsystCLI.exe"
            binary.write_bytes(b"Not an executable")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(binary)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    tool = next(t for t in tools if t.name == "pvsyst_prepare_design_project")
                    self.assertEqual(set(tool.input_schema["properties"]), {"spec", "dry_run"})
                    for spec in ({}, {"new_project": "SourceProject.PRJ", "year": 2024},
                                 {"new_project": "SourceProject.PRJ", "year": 2069},
                                 {"new_project": "../SourceProject.PRJ", "year": 2024}):
                        for dry_run in (True, False):
                            reply = await session.call_tool("pvsyst_prepare_design_project", {"spec": spec, "dry_run": dry_run})
                            self.assertFalse(reply.is_error, reply.content)
                            value = json.loads("\n".join(x.text for x in reply.content if hasattr(x, "text")))
                            self.assertEqual(value["status"], "blocked")
                            self.assertFalse(value["prepared"])
                            self.assertFalse(value["simulated"])
                            self.assertFalse(value["native_consumption_verified"])
                            self.assertIsNone(value["metrics"])
                            self.assertTrue(value["issues"])
                            self.assertEqual(list(root.iterdir()), [])
                    for args in ({"spec": {}, "dry_run": "false"}, {"spec": {"year": True}},
                                 {"spec": {"unknown_input": 1}}, {"spec": {}, "run_annual": True}):
                        reply = await session.call_tool("pvsyst_prepare_design_project", args)
                        self.assertTrue(reply.is_error)
                        self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

"""Public engineering handoff/delivery boundaries; no fake native results or hash tests."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]


class EngineeringWorkflowBoundary(unittest.TestCase):
    def test_readonly_handoff_and_rejected_requests(self):
        fixture = os.environ.get("PVSYST_PREPARATION_FIXTURE")
        if not fixture:
            self.skipTest("Explicit native-export preparation fixture required")
        asyncio.run(self.readonly(Path(fixture) / "PVsyst8.1_Data"))

    async def readonly(self, source):
        with tempfile.TemporaryDirectory(prefix="pvsyst-engineering-contract-") as directory:
            root = Path(directory) / "PVsyst8.1_Data"
            for folder, name in (("Projects", "RESIDENTIAL_INPUT.PRJ"), ("Projects", "RESIDENTIAL_INPUT.VC0"),
                                 ("Sites", "MCP_DESIGN93.SIT"), ("Meteo", "MCP_DESIGN93.MET")):
                target = root / folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / folder / name, target)
            fake = Path(directory) / "PVsystCLI.exe"
            fake.write_bytes(b"Not executable: read-only and malformed requests must not invoke PVsyst")
            original = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(name, args):
                        response = await session.call_tool(name, args)
                        self.assertFalse(response.is_error, response.content)
                        return json.loads("\n".join(item.text for item in response.content if hasattr(item, "text")))

                    handoff = await call("pvsyst_project_handoff", dict(project="RESIDENTIAL_INPUT.PRJ", variant="VC0"))
                    self.assertIn(handoff["status"], ("ready", "needs_review"), handoff)
                    self.assertTrue(handoff["readonly"])
                    self.assertFalse(handoff["native_consumption_verified"])
                    self.assertEqual(len(handoff["saved"]["subarrays"]), 1)
                    self.assertTrue(handoff["missing_items"])
                    for project in ("../RESIDENTIAL_INPUT.PRJ", "CON.PRJ"):
                        reply = await call("pvsyst_project_handoff", dict(project=project, variant="VC0"))
                        self.assertEqual(reply["status"], "blocked", reply)
                    spec = dict(source_project="RESIDENTIAL_INPUT.PRJ", source_variant="VC0", new_project="REVISION.PRJ",
                                new_variant="VCRUN", objective="Explicit contract fixture revision", year=2024,
                                nominal_dc_power_kw=21.6, power_source="Fixture declaration", changes={"tilt": 30})
                    for patch in ({"new_project": "../ESCAPE.PRJ"}, {"new_project": "RESIDENTIAL_INPUT.PRJ"},
                                  {"changes": {"tracking": {"backtracking": True}}}):
                        value = await call("pvsyst_run_engineering_task", {"spec": {**spec, **patch}, "dry_run": False})
                        self.assertEqual(value["status"], "blocked", value)
                        self.assertFalse(value["native_consumption_verified"])
                    response = await session.call_tool("pvsyst_run_engineering_task", {"spec": {**spec, "year": True}})
                    self.assertTrue(response.is_error)
                    response = await session.call_tool("pvsyst_project_handoff", dict(project="RESIDENTIAL_INPUT.PRJ", variant="VC0", typo=True))
                    self.assertTrue(response.is_error)
                    for name in ("../missing.json", "missing.json"):
                        value = await call("pvsyst_export_engineering_delivery", dict(manifest_name=name, output_name="delivery.zip", dry_run=False))
                        self.assertEqual(value["status"], "blocked", value)
                    self.assertEqual(original, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_native_run_delivery_and_changed_input_refusal(self):
        fixture = os.environ.get("PVSYST_ENGINEERING_RUN_FIXTURE")
        if not fixture:
            self.skipTest("Explicit completed engineering native run fixture required")
        asyncio.run(self.delivery(Path(fixture)))

    async def delivery(self, fixture):
        record = json.loads((fixture / "Results" / "ENGINEERING_FIXED_engineering_2024.json").read_text())
        self.assertTrue(record["native_consumption_verified"])
        with tempfile.TemporaryDirectory(prefix="pvsyst-engineering-delivery-contract-") as directory:
            root = Path(directory) / "PVsyst8.1_Data"
            files = [("Projects", name) for name in record["working_files"]]
            files += [(asset["folder"], asset["name"]) for asset in record["input_assets"].values() if asset["library"] == "workspace"]
            files += [("Results", Path(record["artifacts"][kind]).name) for kind in ("csv", "pdf")]
            for folder, name in files:
                target = root / folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fixture / folder / name, target)
            manifest_name = Path(record["artifacts"]["manifest"]).name
            for kind in ("csv", "pdf", "manifest"):
                record["artifacts"][kind] = str(root / "Results" / Path(record["artifacts"][kind]).name)
            (root / "Results" / manifest_name).write_text(json.dumps(record), encoding="utf-8")
            fake = Path(directory) / "PVsystCLI.exe"
            fake.write_bytes(b"Not executable: packaging a recorded run must not invoke PVsyst")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(name, args):
                        response = await session.call_tool(name, args)
                        self.assertFalse(response.is_error, response.content)
                        return json.loads("\n".join(item.text for item in response.content if hasattr(item, "text")))

                    args = dict(manifest_name=manifest_name, output_name="delivery.zip")
                    preview = await call("pvsyst_export_engineering_delivery", args)
                    self.assertEqual(preview["status"], "ready", preview)
                    self.assertFalse((root / "Results" / "delivery.zip").exists())
                    exported = await call("pvsyst_export_engineering_delivery", dict(args, dry_run=False))
                    self.assertEqual(exported["status"], "exported", exported)
                    self.assertEqual(exported["task_status"], record["status"])
                    with zipfile.ZipFile(root / "Results" / "delivery.zip") as archive:
                        self.assertIn("summary.md", archive.namelist())
                        self.assertIn("inputs.md", archive.namelist())
                        selected = "Projects/" + Path(record["working_project"]).stem + "." + record["working_variant"]
                        self.assertEqual(archive.read(selected), (root / selected).read_bytes())
                        self.assertFalse(any(name.startswith("ComposPV/") or name.startswith("Meteo/") for name in archive.namelist()))
                    duplicate = await call("pvsyst_export_engineering_delivery", dict(args, dry_run=False))
                    self.assertEqual(duplicate["status"], "blocked", duplicate)
                    parameters = await call("pvsyst_get_variant_parameters", dict(project=record["working_project"], variant=record["working_variant"]))
                    await call("pvsyst_update_variant_parameters", dict(project=record["working_project"], variant=record["working_variant"],
                        expected_sha256=parameters["sha256"], subarray_id=parameters["subarrays"][0]["id"],
                        orientation_id=parameters["orientations"][0]["id"], orientation_updates={"FieldTilt": 31}))
                    refused = await call("pvsyst_export_engineering_delivery", dict(args, output_name="changed.zip", dry_run=False))
                    self.assertEqual(refused["status"], "blocked", refused)
                    self.assertFalse((root / "Results" / "changed.zip").exists())

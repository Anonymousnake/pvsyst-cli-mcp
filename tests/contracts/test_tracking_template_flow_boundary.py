"""Explicit tracking-flow public contract; no simulated native outputs or physics."""
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


class TrackingTemplateFlowBoundary(unittest.TestCase):
    def test_strict_mode_and_no_write_boundary(self):
        asyncio.run(self.check())

    async def check(self):
        with tempfile.TemporaryDirectory(prefix="pvsyst-tracking-flow-") as directory:
            root = Path(directory) / "PVsyst8.1_Data"
            root.mkdir()
            binary = Path(directory) / "PVsystCLI.exe"
            binary.write_bytes(b"Not executable: malformed requests must stop before native checks")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(binary)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tool = next(t for t in (await session.list_tools()).tools if t.name == "pvsyst_simulate_template")
                    self.assertIn("tracking", tool.input_schema["properties"])
                    self.assertIn("tracking_variant", tool.input_schema["properties"])
                    base = {"template_project": "SOURCE.PRJ", "variant": "VC0", "new_project": "NEW.PRJ",
                            "pan_name": "SOURCE.PAN", "ond_name": "SOURCE.OND", "site_name": "SOURCE.SIT",
                            "met_name": "SOURCE.MET", "year": 2024, "tracking": {"backtracking": True},
                            "tracking_variant": "VC1", "nominal_dc_power_kw": 440,
                            "tracking_power_source": "Caller-declared native template report fixture"}
                    for patch in ({"tilt": 20}, {"azimuth": 0}, {"capacity": {}}, {"losses": {}},
                                  {"tracking_variant": ""}, {"tracking": None},
                                  {"nominal_dc_power_kw": 0}, {"nominal_dc_power_kw": None},
                                  {"tracking_power_source": ""}):
                        for dry_run in (True, False):
                            reply = await session.call_tool("pvsyst_simulate_template", {**base, **patch, "dry_run": dry_run})
                            self.assertFalse(reply.is_error, reply.content)
                            value = json.loads("\n".join(c.text for c in reply.content if hasattr(c, "text")))
                            self.assertEqual(value["status"], "blocked", value)
                            self.assertTrue(value["issues"])
                            self.assertIsNone(value["metrics"])
                            self.assertFalse(value.get("native_consumption_verified", False))
                            self.assertEqual(list(root.iterdir()), [])
                    for patch in ({"tracking": {"backtracking": "true"}},
                                  {"tracking": {"axis_azimuth_deg": True}},
                                  {"tracking": {"unknown": 1}}, {"tracking_variant": True},
                                  {"nominal_dc_power_kw": True}, {"tracking_power_source": 440}):
                        reply = await session.call_tool("pvsyst_simulate_template", {**base, **patch})
                        self.assertTrue(reply.is_error)
                        self.assertEqual(list(root.iterdir()), [])

    def test_explicit_native_preflight_without_simulation(self):
        fixture = os.environ.get("PVSYST_TRACKING_FLOW_FIXTURE")
        binary = os.environ.get("PVSYST_TRACKING_FLOW_CLI")
        if not fixture or not binary:
            self.skipTest("Explicit native tracking fixture/CLI required; no native claim")
        asyncio.run(self.native(Path(fixture), Path(binary)))

    async def native(self, fixture, binary):
        request = json.loads((fixture / "request.json").read_text())
        request.update(new_project="ContractTrackingPreview.PRJ", dry_run=True)
        root = fixture / "PVsyst8.1_Data"
        originals = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        members = set(root.rglob("*"))
        params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
            cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(binary)))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                reply = await session.call_tool("pvsyst_simulate_template", request)
                self.assertFalse(reply.is_error, reply.content)
                value = json.loads("\n".join(c.text for c in reply.content if hasattr(c, "text")))
                self.assertEqual(value["status"], "ready", value)
                self.assertEqual(value["scope"], "existing_unlim_tracking_grid")
                self.assertFalse(value["native_consumption_verified"])
                self.assertEqual(value["configuration_preview"]["field_type"], "UnlimTracking")
                self.assertEqual(value["recovery"][0]["action"], "archive_isolated_tracking_task")
                self.assertTrue(value["review_required"])
                self.assertEqual(value["component_resolution"], "native_database_uninspected")
                for patch in ({"tracking": {"axis_azimuth_deg": 21}},
                              {"tracking_variant": request["variant"]}, {"pan_name": "OTHER.PAN"},
                              {"tracking": None, "tracking_variant": "", "nominal_dc_power_kw": None,
                               "tracking_power_source": "", "tilt": 25}):
                    reply = await session.call_tool("pvsyst_simulate_template", {**request, **patch})
                    self.assertFalse(reply.is_error, reply.content)
                    blocked = json.loads("\n".join(c.text for c in reply.content if hasattr(c, "text")))
                    self.assertEqual(blocked["status"], "blocked", blocked)
                    self.assertIsNone(blocked["metrics"])
                self.assertEqual(set(root.rglob("*")), members)
                self.assertTrue(all(p.read_bytes() == raw for p, raw in originals.items()))


if __name__ == "__main__":
    unittest.main()

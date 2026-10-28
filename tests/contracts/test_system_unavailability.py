"""PV-system outage removal and recovery through the public MCP protocol."""
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
PROJECT = "PV_SYSTEM_AVAILABILITY.PRJ"
VARIANT = "PV_SYSTEM_AVAILABILITY.VC0"


class SystemUnavailabilityContract(unittest.IsolatedAsyncioTestCase):
    async def test_clear_preview_noop_and_restore(self):
        configured = os.getenv("PVSYST_CONTRACT_WORKSPACE")
        if not configured:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the native outage fixture workspace")
        source = Path(configured) / "Projects"
        original = (source / VARIANT).read_bytes()
        with tempfile.TemporaryDirectory(prefix="pvsyst-outage-contract-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            for name in (PROJECT, VARIANT):
                shutil.copy2(source / name, projects / name)
            target = projects / VARIANT
            executable = root / "PVsystCLI.exe"
            executable.write_bytes(b"Native execution excluded from protocol contract")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            environment.pop("PVSYST_TOOL_GROUPS", None)
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO), env=environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, arguments, error=False):
                        response = await session.call_tool(tool, arguments)
                        self.assertEqual(bool(response.is_error), error, response.content)
                        if not error:
                            return json.loads("\n".join(item.text for item in response.content
                                                       if hasattr(item, "text")))

                    async def get():
                        return await call("pvsyst_get_variant_parameters", {"project": PROJECT, "variant": "VC0"})

                    async def update(error=False, **declaration):
                        current = await get()
                        return await call("pvsyst_update_variant_parameters",
                            {"project": PROJECT, "variant": "VC0", "expected_sha256": current["sha256"],
                             "system_unavailability_updates": {"mode": "none"}, **declaration}, error)

                    before = (await get())["system_unavailability"]
                    self.assertTrue(before["configured"])
                    self.assertGreater(before["period_count"], 0)
                    preview = await update(dry_run=True)
                    self.assertTrue(preview["changed"])
                    self.assertFalse(preview["system_unavailability"]["after"]["configured"])
                    self.assertNotIn("backup_name", preview)
                    self.assertEqual(target.read_bytes(), original)
                    self.assertFalse((projects / ".mcp-variant-backups").exists())
                    for declaration in (
                        {"system_unavailability_updates": {"mode": "periods"}},
                        {"system_unavailability_updates": {"mode": "none", "periods": []}},
                        {"system_unavailability_updates": True},
                        {"ageing_enabled": False}, {"soiling_updates": {"mode": "none"}},
                        {"main_array_updates": {}}, {"subarray_id": 2}, {"orientation_id": 2},
                    ):
                        with self.subTest(declaration=declaration):
                            await update(error=True, **declaration)
                            self.assertEqual(target.read_bytes(), original)
                    changed = await update()
                    self.assertTrue(changed["changed"])
                    self.assertFalse((await get())["system_unavailability"]["configured"])
                    self.assertEqual(changed["system_unavailability"]["before"], before)
                    self.assertEqual((projects / ".mcp-variant-backups" / changed["backup_name"]).read_bytes(), original)
                    removed = target.read_bytes()
                    repeated = await update()
                    self.assertFalse(repeated["changed"])
                    self.assertNotIn("backup_name", repeated)
                    self.assertEqual(target.read_bytes(), removed)
                    current = await get()
                    await call("pvsyst_restore_variant", {"project": PROJECT, "variant": "VC0",
                        "backup_name": changed["backup_name"], "expected_sha256": current["sha256"], "confirm": True})
                    self.assertEqual(target.read_bytes(), original)
                    self.assertEqual((await get())["system_unavailability"], before)
                    text = original.decode("utf-8-sig")
                    cases = (
                        text.replace("Version=8.1.6", "Version=8.1.0", 1),
                        text.replace("SystemType=Grid", "SystemType=Grid\n    UnexpectedSystemField=1", 1),
                        text.replace("DefMode=Periods", "DefMode=Periods\n      UnexpectedOutageField=1", 1),
                        text.replace("DefMode=Periods", "DefMode=Unknown", 1),
                        text.replace("DefMode=Periods", "DefMode=Periods\n      DefMode=Periods", 1),
                    )
                    for candidate in cases:
                        target.write_text(candidate, encoding="utf-8", newline="")
                        malformed = target.read_bytes()
                        await update(error=True)
                        self.assertEqual(target.read_bytes(), malformed)
        self.assertEqual((source / VARIANT).read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

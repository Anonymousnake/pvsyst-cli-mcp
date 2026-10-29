"""Opt-in real-native public MCP launch contract.

The one permanent external acceptance gate for the Windows private non-input-desktop
CLI launch: the product CLI adapter must complete a normal real PVsystCLI round-trip
from a session that offers no interactive user desktop.

Only observable public behavior is asserted — one `pvsyst_capabilities` call and its
unchanged return shape. Desktop names, window handles, jobs, threads and process
cleanup are NOT inspected here; those OS-lifecycle facts are proven in a throwaway
private harness (see contracts/cli-isolation-r1), never in this permanent suite.

This test launches the REAL executable (no fake stub), so it must run inside the
documented non-interactive fixture session on the acceptance host. A plain
`subprocess` that merely inherited an interactive desktop would not prove the
requirement, so the precondition is the session itself, not any in-process assertion.

Env (all required; anything missing -> skip):
  PVSYST_CONTRACT_REAL_NATIVE=1   explicit opt-in; absent means "do not run native"
  PVSYST_CONTRACT_CLI=<path>      real installed PVsystCLI.exe
  PVSYST_CONTRACT_WORKSPACE=<dir> real PVsyst8.1_Data workspace (read-only here)

This phase runs zero native requests; the launch command is a draft for the
post-freeze gate, not a result.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]
REQUIRED_COMMANDS = ("run-simulation", "create-site", "convert-meteo")


class NativeCliLaunchE2E(unittest.TestCase):
    def setUp(self):
        if os.environ.get("PVSYST_CONTRACT_REAL_NATIVE") != "1":
            self.skipTest("Set PVSYST_CONTRACT_REAL_NATIVE=1 to opt into the real-native launch gate")
        cli = os.environ.get("PVSYST_CONTRACT_CLI")
        workspace = os.environ.get("PVSYST_CONTRACT_WORKSPACE")
        if not cli or not Path(cli).is_file():
            self.skipTest("Set PVSYST_CONTRACT_CLI to a real installed PVsystCLI.exe")
        if not workspace or not Path(workspace).is_dir():
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to a real PVsyst8.1_Data workspace")
        self.cli = cli
        self.workspace = workspace

    def test_capabilities_round_trip_without_interactive_desktop(self):
        asyncio.run(self.check())

    async def check(self):
        env = dict(os.environ, PVSYST_CLI=self.cli, PVSYST_WORKSPACE=self.workspace,
                   PYTHONDONTWRITEBYTECODE="1")
        env.pop("PVSYST_TOOL_GROUPS", None)
        env.pop("PVSYST_BUILTIN_COMPONENTS", None)
        params = StdioServerParameters(
            command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
            cwd=str(REPO), env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                reply = await session.call_tool("pvsyst_capabilities", {})
                self.assertFalse(reply.is_error, reply.content)
                value = json.loads("\n".join(item.text for item in reply.content
                                             if hasattr(item, "text")))
                # Unchanged public return shape: {"version": str, "commands": {name: [options]}}
                self.assertEqual(set(value), {"version", "commands"})
                self.assertIsInstance(value["version"], str)
                self.assertTrue(value["version"].strip())
                self.assertIsInstance(value["commands"], dict)
                for name in REQUIRED_COMMANDS:
                    self.assertIn(name, value["commands"])
                    self.assertIsInstance(value["commands"][name], list)


if __name__ == "__main__":
    unittest.main()

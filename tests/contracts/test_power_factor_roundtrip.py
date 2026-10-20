"""Public edit/read/edit contract using caller-supplied native text fixtures."""
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
MONTHLY = [0.484, 0.470, 0.456, 0.441, 0.426, 0.411, 0.395, 0.379, 0.363, 0.395, 0.426, 0.456]


class PowerFactorRoundtripContract(unittest.IsolatedAsyncioTestCase):
    async def test_monthly_pnom_survives_read_and_clear_in_both_public_entries(self):
        configured = os.getenv("PVSYST_CONTRACT_WORKSPACE")
        if not configured:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the native fixture workspace")
        source = Path(configured) / "Projects"
        names = ("ENERGY_CONTROLS.PRJ", "ENERGY_CONTROLS.VCE", "POWER_CONTROLS.PRJ", "POWER_CONTROLS.VCH")
        with tempfile.TemporaryDirectory(prefix="pvsyst-pnom-contract-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            for name in names:
                shutil.copy2(source / name, projects / name)
            original_inputs = {source / name: (source / name).read_bytes() for name in names}
            executable = root / "PVsystCLI.exe"
            executable.write_bytes(b"Native execution excluded from protocol contract")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO), env=environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, arguments):
                        result = await session.call_tool(tool, arguments)
                        self.assertFalse(result.is_error, result.content)
                        return json.loads("\n".join(item.text for item in result.content
                                                   if hasattr(item, "text")))

                    for project, variant, combined in (("ENERGY_CONTROLS.PRJ", "VCE", False),
                                                       ("POWER_CONTROLS.PRJ", "VCH", True)):
                        identity = {"project": project, "variant": variant}
                        field = "power_controls" if combined else "power_factor"
                        for pnom in ("active", "apparent", "native"):
                            for pf in ({"mode": "monthly", "pnom_limit": pnom, "monthly_tan_phi": MONTHLY},
                                       {"mode": "none", "pnom_limit": "native"}):
                                before = await call("pvsyst_get_variant_parameters", identity)
                                if combined:
                                    declaration = {"power_controls_updates": {"power_factor": pf,
                                        "grid_limit": {"mode": "none", "quantity": "active", "point": "inverter"}}}
                                else:
                                    declaration = {"power_factor_updates": pf}
                                changed = await call("pvsyst_update_variant_parameters",
                                    dict(identity, expected_sha256=before["sha256"], **declaration))
                                after = await call("pvsyst_get_variant_parameters", identity)
                                semantic = after[field]["power_factor"] if combined else after[field]
                                self.assertEqual(semantic["mode"], pf["mode"])
                                self.assertEqual(semantic["pnom_limit"], pf["pnom_limit"])
                                self.assertEqual(semantic["monthly_tan_phi"], pf.get("monthly_tan_phi"))
                                expected = dict(mode="configured", **changed[field]["after"]) if combined else changed[field]["after"]
                                self.assertEqual(after[field], expected)
            self.assertTrue(all(path.read_bytes() == before for path, before in original_inputs.items()))


if __name__ == "__main__":
    unittest.main()

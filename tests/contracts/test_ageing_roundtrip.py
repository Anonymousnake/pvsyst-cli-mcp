"""Native-template edit/read/enable contract via the public MCP protocol only."""
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


class AgeingRoundtripContract(unittest.IsolatedAsyncioTestCase):
    async def test_enable_and_model_edits_share_complete_template_validation(self):
        configured = os.getenv("PVSYST_CONTRACT_WORKSPACE")
        if not configured:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the native fixture workspace")
        source = Path(configured) / "Projects"
        with tempfile.TemporaryDirectory(prefix="pvsyst-ageing-contract-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            for name in ("MODEL_INPUT_CONFIG.PRJ", "MODEL_INPUT_CONFIG.VCN"):
                shutil.copy2(source / name, projects / name)
            original = (source / "MODEL_INPUT_CONFIG.VCN").read_bytes()
            text = original.decode("utf-8-sig")
            self.assertEqual(text.count("Flags2=$0010082A"), 1)
            self.assertEqual(text.count("ProgressMode=1"), 1)
            malformed = text.replace("Flags2=$0010082A", "Flags2=$082A").replace("ProgressMode=1", "ProgressMode=2")
            (projects / "MODEL_INPUT_CONFIG.VCZ0").write_bytes(malformed.encode("utf-8"))
            padded = text.replace("Flags2=$0010082A", "Flags2=$0000082a")
            (projects / "MODEL_INPUT_CONFIG.VCZ1").write_bytes(padded.encode("utf-8"))
            executable = root / "PVsystCLI.exe"
            executable.write_bytes(b"Native execution excluded from protocol contract")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO), env=environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, arguments, error=False):
                        result = await session.call_tool(tool, arguments)
                        self.assertEqual(bool(result.is_error), error, result.content)
                        if error:
                            return
                        return json.loads("\n".join(item.text for item in result.content
                                                   if hasattr(item, "text")))

                    async def get(variant="VCN"):
                        return await call("pvsyst_get_variant_parameters",
                                          {"project": "MODEL_INPUT_CONFIG.PRJ", "variant": variant})

                    async def update(declaration, variant="VCN", error=False):
                        current = await get(variant)
                        return await call("pvsyst_update_variant_parameters",
                            dict(project="MODEL_INPUT_CONFIG.PRJ", variant=variant,
                                 expected_sha256=current["sha256"], **declaration), error)

                    baseline = (await get())["ageing"]
                    for enabled in (False, False, True):
                        changed = await update({"ageing_enabled": enabled})
                        after = (await get())["ageing"]
                        self.assertEqual(after, changed["ageing"]["after"])
                        self.assertIs(after["enabled"], enabled)
                        self.assertEqual(after["template"], baseline["template"])
                    values = {"simulation_year": 10, "annual_module_degradation_percent": 0.60,
                              "keep_mc_values": True, "modules_rms_year_isc_percent": 1.50,
                              "modules_rms_year_voc_percent": 1.20}
                    changed = await update({"ageing_updates": values})
                    after = (await get())["ageing"]
                    self.assertEqual(after, changed["ageing"]["after"])
                    self.assertEqual(after["template"]["KeepMCValues"], "True")
                    self.assertEqual(after["template"]["ModulesRMSYearISC"], "1.50")
                    self.assertEqual(after["template"]["ModulesRMSYearVoc"], "1.20")
                    self.assertEqual(after["stored_loss_factor_percent"], baseline["stored_loss_factor_percent"])
                    before = (projects / "MODEL_INPUT_CONFIG.VCZ0").read_bytes()
                    await update({"ageing_enabled": True}, "VCZ0", error=True)
                    self.assertEqual((projects / "MODEL_INPUT_CONFIG.VCZ0").read_bytes(), before)
                    self.assertIn("issue", (await get("VCZ0"))["ageing"])
                    await update({"ageing_enabled": True}, "VCZ1")
                    self.assertIs((await get("VCZ1"))["ageing"]["enabled"], True)
                    await update({"ageing_enabled": False})
                    before = (projects / "MODEL_INPUT_CONFIG.VCN").read_bytes()
                    await update({"ageing_updates": values}, error=True)
                    self.assertEqual((projects / "MODEL_INPUT_CONFIG.VCN").read_bytes(), before)
            self.assertEqual((source / "MODEL_INPUT_CONFIG.VCN").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

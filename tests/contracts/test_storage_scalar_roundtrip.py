"""Public storage scalar edits preserve recorded native scheduling, not a Python dispatch."""
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


class StorageScalarContract(unittest.IsolatedAsyncioTestCase):
    async def test_preserved_strategy_and_semantic_source_candidate_guards(self):
        configured = os.getenv("PVSYST_CONTRACT_WORKSPACE")
        if not configured:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the native fixture workspace")
        source = Path(configured) / "Projects"
        with tempfile.TemporaryDirectory(prefix="pvsyst-storage-scalar-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            for suffix in ("PRJ", "VCM", "VCL"):
                name = "PEAK_CONFIG." + suffix
                shutil.copy2(source / name, projects / name)
            original = {path: path.read_bytes() for path in projects.iterdir()}
            text = (projects / "PEAK_CONFIG.VCM").read_bytes().decode("utf-8-sig")
            for suffix, before, after in (("VCZ0", "ChargingStrategy=PVToGridMin", "ChargingStrategy=Invented"),
                                          ("VCZ1", "SOCDischDecl=0.2", "SOCDischDecl=0.7"),
                                          ("VCZ2", "SOCDischDecl=0.2", "SOCDischDecl=1e-400"),
                                          ("VCZ3", "HourBegDisch=20", "HourBegDisch=24"),
                                          ("VCZ4", "PVObject_GridStorage=pvGridStorage", "PVObject_GridStorage = pvGridStorage")):
                self.assertEqual(text.count(before), 1)
                (projects / ("PEAK_CONFIG." + suffix)).write_bytes(text.replace(before, after).encode())
            executable = root / "PVsystCLI.exe"
            executable.write_bytes(b"No native execution in scalar protocol contract")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            params = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO), env=environment)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        result = await session.call_tool(tool, args)
                        self.assertFalse(result.is_error, result.content)
                        return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))

                    async def create(variant, target, updates, dry_run=False):
                        return await call("pvsyst_create_grid_storage_variant",
                            dict(project="PEAK_CONFIG.PRJ", source_variant=variant,
                                 new_variant=target, updates=updates, dry_run=dry_run))

                    preview = await create("VCM", "VCY0", {"initial_soc": 0.5}, True)
                    self.assertEqual(preview["status"], "ready", preview)
                    self.assertFalse((projects / "PEAK_CONFIG.VCY0").exists())
                    self.assertEqual((await create("VCM", "VCY0", {"initial_soc": 0.5}))["status"], "created")
                    state = await call("pvsyst_get_variant_parameters", {"project": "PEAK_CONFIG.PRJ", "variant": "VCY0"})
                    values = state["grid_storage"]
                    self.assertEqual(values["mode"], "stored")
                    self.assertEqual((values["strategy"], values["charging_strategy"], values["discharging_strategy"]),
                                     ("PeakShaving", "PVToGridMin", "MinMaxHour"))
                    self.assertEqual(values["initial_soc"], 0.5)
                    self.assertEqual((values["HourBegDisch"], values["HourEndDisch"]), (20, 6))
                    self.assertEqual((await create("VCY0", "VCY1", {"max_charge_ac_kw": 100}))["status"], "created")
                    self.assertEqual((await create("VCL", "VCY2", {"max_discharge_ac_kw": 100}))["status"], "created")
                    switched = await call("pvsyst_create_peak_shaving_variant", dict(project="PEAK_CONFIG.PRJ",
                        source_variant="VCY1", new_variant="VCY3", discharge={"mode": "after_sunset"}, dry_run=False))
                    self.assertEqual(switched["status"], "created", switched)
                    self.assertEqual((await create("VCY3", "VCY4", {"max_discharge_ac_kw": 80}))["status"], "created")
                    state = await call("pvsyst_get_variant_parameters", {"project": "PEAK_CONFIG.PRJ", "variant": "VCY4"})
                    self.assertEqual(state["grid_storage"]["initial_soc"], 0.5)
                    self.assertEqual(state["grid_storage"]["max_charge_ac_kw"], 100)
                    self.assertEqual(state["grid_storage"]["max_discharge_ac_kw"], 80)
                    self.assertEqual(state["grid_storage"]["discharging_strategy"], "AfterSunset")
                    state = await call("pvsyst_get_variant_parameters", {"project": "PEAK_CONFIG.PRJ", "variant": "VCZ4"})
                    self.assertEqual(state["grid_storage"]["mode"], "stored")
                    for variant, updates in (("VCM", {"soc_min": 0.7}),
                                              ("VCM", {"charger_max_efficiency_pct": 95}),
                                              ("VCZ0", {"initial_soc": 0.5}),
                                              ("VCZ1", {"initial_soc": 0.8, "reserve_soc": 0.8}),
                                              ("VCZ2", {"soc_min": 0.2}),
                                              ("VCZ3", {"initial_soc": 0.5})):
                        result = await create(variant, "VCBAD", updates)
                        self.assertEqual(result["status"], "blocked", result)
                        self.assertFalse((projects / "PEAK_CONFIG.VCBAD").exists())
            self.assertTrue(all(path.read_bytes() == data for path, data in original.items()))


if __name__ == "__main__":
    unittest.main()

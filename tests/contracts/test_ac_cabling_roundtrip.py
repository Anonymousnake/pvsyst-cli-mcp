"""Public native AC enable/read/re-edit with DC isolation and source/precision guards."""
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


class ACCablingContract(unittest.IsolatedAsyncioTestCase):
    async def test_uniform_owners_reedit_and_dc_isolation(self):
        configured = os.getenv("PVSYST_CONTRACT_WORKSPACE")
        if not configured:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the native fixture workspace")
        with tempfile.TemporaryDirectory(prefix="pvsyst-ac-contract-") as directory:
            root = Path(directory)
            projects = root / "Projects"
            projects.mkdir()
            for suffix in ("PRJ", "VCD", "VCR", "VCS"):
                name = "LOSS_CONTROLS." + suffix
                shutil.copy2(Path(configured) / "Projects" / name, projects / name)
            original = {p: p.read_bytes() for p in projects.iterdir()}
            active = (projects / "LOSS_CONTROLS.VCR").read_bytes().decode("utf-8-sig")
            self.assertEqual(active.count("Length=100 m"), 2)
            (projects / "LOSS_CONTROLS.VCZ0").write_bytes(active.replace("Length=100 m", "Length=90 m", 1).encode())
            (projects / "LOSS_CONTROLS.VCZ1").write_bytes(active.replace("Version=8.1.6", "Version=9.9.9").encode())
            hv = (projects / "LOSS_CONTROLS.VCS").read_bytes().decode("utf-8-sig")
            for before, after in (("Flags=$01F00400", "Flags=$01B00400"), ("MVNbTransfos=1", "MVNbTransfos=0"),
                                  ("IsHVTransformer=False", "IsHVTransformer=True")):
                self.assertIn(before, hv)
                hv = hv.replace(before, after, 1)
            (projects / "LOSS_CONTROLS.VCZ2").write_bytes(hv.encode())
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"No native execution in public AC contract")
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        result = await session.call_tool(tool, args)
                        self.assertFalse(result.is_error, result.content)
                        return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))

                    async def create(source, target, ac, dry_run=False):
                        return await call("pvsyst_create_ac_cabling_variant", dict(project="LOSS_CONTROLS.PRJ",
                            source_variant=source, new_variant=target, ac=ac, dry_run=dry_run))

                    preview = await create("VCD", "VCY0", {"length_m": 100}, True)
                    self.assertEqual(preview["status"], "ready", preview)
                    self.assertFalse((projects / "LOSS_CONTROLS.VCY0").exists())
                    self.assertEqual((await create("VCD", "VCY0", {"length_m": 100}))["status"], "created")
                    self.assertEqual((await create("VCY0", "VCY1", {"length_m": 50.1, "metal": "Aluminium", "section": "ISO60228_16"}))["status"], "created")
                    self.assertEqual((await create("VCY1", "VCY2", {"length_m": 75.2}))["status"], "created")
                    state = await call("pvsyst_get_variant_parameters", {"project": "LOSS_CONTROLS.PRJ", "variant": "VCY2"})
                    self.assertEqual(state["ac_cabling"], {"mode": "active", "length_m": 75.2,
                        "metal": "Aluminium", "section": "ISO60228_16", "inverter_groups": 2, "inverter_count": 6})
                    def dc_lines(raw):
                        return [line for line in raw.decode("utf-8-sig").splitlines() if "Resistance=" in line]
                    self.assertEqual(dc_lines((projects / "LOSS_CONTROLS.VCY2").read_bytes()),
                                     dc_lines((projects / "LOSS_CONTROLS.VCR").read_bytes()))
                    self.assertEqual((await create("VCD", "VCY3", {"length_m": 0.1}))["status"], "created")
                    minimum = await call("pvsyst_get_variant_parameters", {"project": "LOSS_CONTROLS.PRJ", "variant": "VCY3"})
                    self.assertEqual(minimum["ac_cabling"]["length_m"], 0.1)
                    hv_state = await call("pvsyst_get_variant_parameters", {"project": "LOSS_CONTROLS.PRJ", "variant": "VCZ2"})
                    self.assertEqual(hv_state["main_array"]["values"]["MVNbTransfos"], "0")
                    self.assertEqual(hv_state["transformers"]["layers"][0]["fields"]["IsHVTransformer"], "True")
                    self.assertEqual(hv_state["ac_cabling"]["mode"], "active")
                    for source, ac in (("VCD", {"length_m": 0.01}), ("VCD", {"length_m": 0.0001}),
                                        ("VCD", {"length_m": 100.05}), ("VCD", {"length_m": 1e100}),
                                        ("VCZ0", {"length_m": 100}), ("VCZ1", {"length_m": 100}),
                                        ("VCS", {"length_m": 100}), ("VCZ2", {"length_m": 100})):
                        result = await create(source, "VCBAD", ac)
                        self.assertEqual(result["status"], "blocked", result)
                        self.assertFalse((projects / "LOSS_CONTROLS.VCBAD").exists())
            self.assertTrue(all(path.read_bytes() == before for path, before in original.items()))


if __name__ == "__main__":
    unittest.main()

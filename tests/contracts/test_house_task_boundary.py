"""Public house-task schema/preflight/checkpoint non-mutation; no native GUI/run."""
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


class HouseTaskBoundary(unittest.TestCase):
    def test_public_boundary(self):
        fixture = os.environ.get("PVSYST_CONTRACT_WORKSPACE")
        if not fixture:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the explicit native fixture workspace")
        asyncio.run(self.check(Path(fixture)))

    async def check(self, fixture):
        with tempfile.TemporaryDirectory(prefix="pvsyst-house-task-contract-") as directory:
            root = Path(directory)
            for folder in ("Projects", "UserData", "Sites", "Meteo", "Results"):
                (root / folder).mkdir()
            names = (("Projects", "HOUSE_SCENE.PRJ"), ("Projects", "HOUSE_SCENE.VC0"),
                     ("Sites", "Hawaii_2020.SIT"), ("UserData", "House_Revised_R46B.pvc2"))
            for folder, name in names:
                shutil.copy2(fixture / folder / name, root / folder / name)
            # The fake CLI cannot execute; this placeholder is not claimed as native weather.
            (root / "Meteo" / "Hawaii_2020_1min_ClipCorr.MET").write_bytes(b"Non-native boundary fixture")
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"Not an executable")
            originals = {root / folder / name: (root / folder / name).read_bytes() for folder, name in names}
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(fake)))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(args):
                        z = await session.call_tool("pvsyst_run_house_task", args)
                        self.assertFalse(z.is_error, z.content)
                        return json.loads("\n".join(x.text for x in z.content if hasattr(x, "text")))

                    z = await session.call_tool("pvsyst_read_house_pv_scene", {"scene_name": "House_Revised_R46B.pvc2"})
                    self.assertFalse(z.is_error)
                    recovered = json.loads("\n".join(x.text for x in z.content if hasattr(x, "text")))["create_arguments"]
                    spec = dict(source_project="HOUSE_SCENE.PRJ", source_variant="VC0", new_project="HouseTaskContract.PRJ",
                        variant_name="House task contract", year=2022, house=recovered["house"], layout=recovered["layout"],
                        module_geometry=recovered["module_geometry"])
                    for change in ({}, {"year": 2069}, {"report_pages": []}, {"report_pages": ["invented_page"]},
                                   {"new_project": "HOUSE_SCENE.PRJ"}, {"new_project": "../HouseTaskContract.PRJ"}):
                        value = await call({"spec": dict(spec, **change)})
                        self.assertEqual(value["status"], "blocked")
                        self.assertFalse(value["native_consumption_verified"])
                        self.assertFalse((root / "Projects" / "HouseTaskContract.PRJ").exists())
                        self.assertEqual(list((root / "Results").iterdir()), [])
                    checkpoint = root / "Results" / "HouseTaskContract_house_task.json"
                    for payload in (b'null', b'[]', b'{"schema":999,"request":{"unexpected":"untrusted"}}'):
                        checkpoint.write_bytes(payload)
                        for dry_run in (True, False):
                            value = await call({"spec": spec, "resume": True, "dry_run": dry_run, "confirm": True})
                            self.assertEqual(value["status"], "blocked")
                            self.assertFalse(value["native_consumption_verified"])
                            self.assertEqual(checkpoint.read_bytes(), payload)
                    conflict = root / "Results" / "HouseOutputConflict_house_run01.csv"
                    conflict.write_bytes(b"Existing user result")
                    value = await call({"spec": dict(spec, new_project="HouseOutputConflict.PRJ"),
                                        "dry_run": False, "confirm": True})
                    self.assertEqual(value["status"], "blocked")
                    self.assertTrue(any("first-run outputs" in message for message in value["issues"]))
                    self.assertEqual(conflict.read_bytes(), b"Existing user result")
                    self.assertFalse((root / "Projects" / "HouseOutputConflict.PRJ").exists())
                    bad = copy.deepcopy(spec)
                    bad["year"] = True
                    z = await session.call_tool("pvsyst_run_house_task", {"spec": bad})
                    self.assertTrue(z.is_error)
                    z = await session.call_tool("pvsyst_run_house_task", {"spec": spec, "force_close_existing": True})
                    self.assertTrue(z.is_error)
            self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))
            self.assertFalse((root / "UserData" / "HouseTaskContract_house.pvc2").exists())


if __name__ == "__main__":
    unittest.main()

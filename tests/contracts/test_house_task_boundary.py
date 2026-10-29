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

    def test_scene_import_method_boundary(self):
        """Optional scene_import_method wire/default/gate contract; no native run claimed.

        Pins only the external request/dry-run contract of the new routing field: the
        closed enum, that omission means the existing GUI route, and that a native
        request never writes or reports consumption on a preview / without confirm.
        The positive native preparation + full-year E2E needs a double fixed two-table
        fixture and a real CLI; it is a later single-executor lease, so it is not
        asserted here. The already-available native record preparation is covered by
        test_native_house_import_boundary.py through pvsyst_import_house_pv_scene.
        """
        fixture = os.environ.get("PVSYST_CONTRACT_WORKSPACE")
        if not fixture:
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to the explicit native fixture workspace")
        asyncio.run(self.check_scene_import_method(Path(fixture)))

    async def check_scene_import_method(self, fixture):
        with tempfile.TemporaryDirectory(prefix="pvsyst-house-method-contract-") as directory:
            root = Path(directory)
            for folder in ("Projects", "UserData", "Sites", "Meteo", "Results"):
                (root / folder).mkdir()
            names = (("Projects", "HOUSE_SCENE.PRJ"), ("Projects", "HOUSE_SCENE.VC0"),
                     ("Sites", "Hawaii_2020.SIT"), ("UserData", "House_Revised_R46B.pvc2"))
            for folder, name in names:
                shutil.copy2(fixture / folder / name, root / folder / name)
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
                    base = dict(source_project="HOUSE_SCENE.PRJ", source_variant="VC0", new_project="HouseMethodContract.PRJ",
                        variant_name="House method contract", year=2022, house=recovered["house"], layout=recovered["layout"],
                        module_geometry=recovered["module_geometry"])
                    produced = root / "Projects" / "HouseMethodContract.PRJ"

                    # 1) The optional route field is accepted for both declared values; the
                    #    reply is a normal result dict, never a schema error, and a preview
                    #    writes nothing and never reports native consumption.
                    for method in ("gui", "native"):
                        value = await call({"spec": dict(base, scene_import_method=method)})
                        self.assertFalse(value["native_consumption_verified"], value)
                        self.assertFalse(produced.exists())
                        self.assertEqual(list((root / "Results").iterdir()), [])

                    # 2) Omission means the existing GUI route: an explicit "gui" produces
                    #    the same status/stage/consumption outcome on the same request.
                    for scenario in (base, dict(base, new_project="HOUSE_SCENE.PRJ")):
                        omitted = await call({"spec": scenario})
                        explicit = await call({"spec": dict(scenario, scene_import_method="gui")})
                        for key in ("status", "stage", "native_consumption_verified"):
                            self.assertEqual(omitted[key], explicit[key], (scenario["new_project"], key))

                    # 3) A native route never bypasses preflight: the same invalid request
                    #    stays blocked with a concrete issue and no consumption claim.
                    blocked = await call({"spec": dict(base, new_project="HOUSE_SCENE.PRJ", scene_import_method="native")})
                    self.assertEqual(blocked["status"], "blocked", blocked)
                    self.assertTrue(blocked["issues"])
                    self.assertFalse(blocked["native_consumption_verified"])
                    self.assertFalse(produced.exists())

                    # 4) Preview and confirm-less execution never write; status is never a
                    #    completed/native-consumed claim without a real confirmed run.
                    for flags in ({}, {"dry_run": False, "confirm": False}):
                        value = await call({"spec": dict(base, scene_import_method="native"), **flags})
                        self.assertNotEqual(value["status"], "completed", value)
                        self.assertFalse(value["native_consumption_verified"], value)
                        self.assertFalse(produced.exists())
                        self.assertEqual(list((root / "Results").iterdir()), [])

                    # 5) The route is a closed enum, not a free string or truthy shortcut.
                    for bad in ("pdf", "GUI", 1, True):
                        z = await session.call_tool("pvsyst_run_house_task",
                            {"spec": dict(base, scene_import_method=bad)})
                        self.assertTrue(z.is_error, (bad, z.content))

            self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))
            self.assertFalse(produced.exists())


if __name__ == "__main__":
    unittest.main()

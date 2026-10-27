"""Public candidate authoring/readback contract; no GUI or simulation."""
import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]
NS = {"c": "http://www.collada.org/2008/03/COLLADASchema", "pv": "https://pvcollada.org/2026/XMLSchema"}


class GroupedHouseSceneBoundary(unittest.TestCase):
    def test_public_candidate_boundary(self):
        fixture = os.environ.get("PVSYST_GROUPED_SCENE_FIXTURE")
        cli = os.environ.get("PVSYST_GROUPED_SCENE_CLI")
        if not fixture or not cli:
            self.skipTest("Set the explicit native scene fixture and CLI (version query only)")
        asyncio.run(self.check(Path(fixture), cli))

    def test_single_orientation_grouped_preflight_is_blocked(self):
        from unittest import mock
        from pvsyst_auto.native import scene as native_scene
        source = {"orientations": [{"id": 1, "field_azimuth_deg": 0.0}], "field_azimuth_deg": 0}
        ready = {"status": "ready", "source": source, "create_arguments": {"variant": "VC1"},
                 "geometry": {"pv_table_count": 1, "house_parts": 1}}
        with mock.patch.object(native_scene, "read_house_pv_scene", return_value=ready), \
                mock.patch.object(native_scene, "_prepare_source",
                                  return_value={"snapshot": {"source": source}, "table_profiles": []}):
            with self.assertRaisesRegex(native_scene.NativeBridgeError, "two distinct source planes"):
                native_scene._preflight(None, None, "One.pvc2", {}, store=object(), allow_groups=True)

    async def check(self, fixture, cli):
        seed = json.loads((fixture / "geometry-candidates.json").read_text())
        single = copy.deepcopy(next(item["args"] for item in seed["calls"]
                                   if item["tool"] == "pvsyst_create_house_pv_scene"))
        root = Path(tempfile.mkdtemp(prefix="pvsyst-grouped-scene-"))
        succeeded = False
        try:
            source = fixture / "PVsyst8.1_Data"
            for folder in ("ComposPV", "Sites", "Meteo"):
                shutil.copytree(source / folder, root / folder)
            for folder in ("Projects", "UserData", "Results"):
                (root / folder).mkdir()
            for name in ("DUAL_ROOF_INPUT.PRJ", "DUAL_ROOF_INPUT.VC1", "ROOF_GEOMETRY_A.PRJ", "ROOF_GEOMETRY_A.VC1"):
                shutil.copy2(source / "Projects" / name, root / "Projects" / name)
            original = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            original_members = set(root.rglob("*"))
            first = copy.deepcopy(single["layout"]["tables"][0])

            def grouped(shift=(70.0, 0.0, 0.0)):
                def rotated(point):
                    return [shift[0] + point[1], shift[1] - point[0], shift[2] + point[2]]
                house = copy.deepcopy(single["house"])
                second_parts = copy.deepcopy(house["meshes"])
                for part in second_parts:
                    part["name"] = "B_" + part["name"]
                    part["vertices"] = [rotated(point) for point in part["vertices"]]
                house["meshes"] += second_parts
                a = dict(copy.deepcopy(first), name="Source plane A", orientation_id=1)
                b = dict(copy.deepcopy(first), name="Source plane B", orientation_id=2,
                         origin_m=rotated(first["origin_m"]), roof_mesh="B_" + first["roof_mesh"])
                return dict(project="DUAL_ROOF_INPUT.PRJ", variant="VC1", house=house,
                            layout={"tables": [a, b]}, module_geometry=single["module_geometry"],
                            output_name="GroupedCandidate.pvc2")

            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=cli))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        response = await session.call_tool(tool, args)
                        self.assertFalse(response.is_error, response.content)
                        return json.loads("\n".join(c.text for c in response.content if hasattr(c, "text")))

                    create = "pvsyst_create_house_pv_scene"
                    read_scene = "pvsyst_read_house_pv_scene"
                    args = grouped()
                    preview = await call(create, args)
                    self.assertEqual(preview["status"], "ready", preview)
                    self.assertEqual([p["id"] for p in preview["source"]["orientations"]], [1, 2])
                    self.assertFalse(preview["native_consumption_verified"])
                    self.assertEqual(set(root.rglob("*")), original_members)
                    self.assertTrue(all(p.read_bytes() == data for p, data in original.items()))
                    for mutation in ("wrong_count", "duplicate_id", "unknown_id", "projection_overlap"):
                        bad = grouped((0.0, 10.0, 0.0)) if mutation == "projection_overlap" else copy.deepcopy(args)
                        if mutation == "wrong_count":
                            bad["layout"]["tables"][0]["module_rows"] = 2
                            bad["layout"]["tables"][1]["module_rows"] = 4
                        if mutation == "duplicate_id":
                            bad["layout"]["tables"][1]["orientation_id"] = 1
                        if mutation == "unknown_id":
                            bad["layout"]["tables"][1]["orientation_id"] = 3
                        stopped = await call(create, bad)
                        self.assertEqual(stopped["status"], "blocked", (mutation, stopped))
                    bad = copy.deepcopy(args)
                    bad["layout"]["tables"][0]["orientation_id"] = True
                    rejected = await session.call_tool(create, bad)
                    self.assertTrue(rejected.is_error)
                    made = await call(create, dict(args, dry_run=False))
                    self.assertEqual(made["status"], "created", made)
                    path = root / "UserData" / args["output_name"]
                    saved = path.read_bytes()
                    recovered = await call(read_scene, {"scene_name": path.name})
                    self.assertEqual(recovered["status"], "ready", recovered)
                    self.assertFalse(recovered["source_verified"])
                    self.assertFalse(recovered["native_consumption_verified"])
                    self.assertEqual(recovered["create_arguments"]["layout"], args["layout"])
                    replay = dict(recovered["create_arguments"], output_name="GroupedRecreated.pvc2", dry_run=False)
                    self.assertEqual((await call(create, replay))["status"], "created")
                    variant_file = root / "Projects" / "DUAL_ROOF_INPUT.VC1"
                    original_variant = variant_file.read_bytes()
                    needle = b"GInverter=Generic_9kW.OND"
                    prefix, found, suffix = original_variant.rpartition(needle)
                    self.assertTrue(found)
                    variant_file.write_bytes(prefix + b"GInverter=Different.OND" + suffix)
                    mixed = await call(create, dict(args, output_name="MixedModels.pvc2"))
                    self.assertEqual(mixed["status"], "blocked", mixed)
                    self.assertTrue(any("same PAN/OND" in issue for issue in mixed["issues"]), mixed)
                    variant_file.write_bytes(original_variant)
                    await call("pvsyst_clone_variant", {"project": args["project"], "source_variant": "VC1", "new_variant": "VC3"})
                    for identifier, azimuth in ((1, 30.0), (2, 120.0)):
                        current = await call("pvsyst_get_variant_parameters", {"project": args["project"], "variant": "VC3"})
                        await call("pvsyst_update_variant_parameters", {"project": args["project"], "variant": "VC3",
                                   "expected_sha256": current["sha256"], "orientation_id": identifier,
                                   "orientation_updates": {"FieldAzim": azimuth}})
                    anchor = dict(args, variant="VC3", output_name="NonzeroAnchor.pvc2", dry_run=False)
                    anchored = await call(create, anchor)
                    self.assertEqual(anchored["status"], "created", anchored)
                    self.assertEqual(anchored["geometry"]["rotate_deg"], -30.0)
                    self.assertEqual((await call(read_scene, {"scene_name": anchor["output_name"]}))["status"], "ready")
                    tree = ET.fromstring(saved)
                    subject = tree.find("c:asset/c:subject", NS)
                    recipe = json.loads(subject.text)
                    recipe["source"]["orientations"][1]["module_total"] += 1
                    subject.text = json.dumps(recipe)
                    path.write_bytes(ET.tostring(tree, encoding="utf-8", xml_declaration=True))
                    self.assertEqual((await call(read_scene, {"scene_name": path.name}))["status"], "unsupported")
                    tree = ET.fromstring(saved)
                    pv = tree.find("c:library_visual_scenes/c:visual_scene/c:node/c:node[@id='PVTable2']/c:rotate", NS)
                    self.assertIsNotNone(pv)
                    pv.text = "0 0 1 0"
                    path.write_bytes(ET.tostring(tree, encoding="utf-8", xml_declaration=True))
                    self.assertEqual((await call(read_scene, {"scene_name": path.name}))["status"], "unsupported")
                    tree = ET.fromstring(saved)
                    ET.SubElement(tree, "{" + NS["c"] + "}extra", {"id": "Unconsumed"})
                    path.write_bytes(ET.tostring(tree, encoding="utf-8", xml_declaration=True))
                    self.assertEqual((await call(read_scene, {"scene_name": path.name}))["status"], "unsupported")
                    path.write_bytes(saved)
                    old = dict(single, output_name="LegacyNamed.pvc2", dry_run=False)
                    self.assertEqual((await call(create, old))["status"], "created")
                    old_read = await call(read_scene, {"scene_name": old["output_name"]})
                    self.assertEqual(old_read["status"], "ready", old_read)
                    self.assertNotIn("orientation_id", old_read["create_arguments"]["layout"]["tables"][0])
                    self.assertEqual(path.read_bytes(), saved)
            self.assertTrue(all(p.read_bytes() == data for p, data in original.items()))
            self.assertEqual(list((root / "Results").iterdir()), [])
            succeeded = True
        finally:
            if succeeded:
                shutil.rmtree(root)
            else:
                print("Retained grouped scene failure fixture: " + str(root), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()

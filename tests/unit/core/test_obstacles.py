"""Consolidated 3D scene obstacles (building, wall, mesh, native oeTree) and terrain DEM tests."""
from __future__ import annotations
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pvsyst_auto.api.schemas import SingleDesignRequest
from pvsyst_auto.components.editor import objects
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.native.headless import _verify_new, import_house_pv_scene_native
from pvsyst_auto.project.template import synthesize_utility_project
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.author import SceneValidationError, create_house_pv_scene, obstacle_profiles
from pvsyst_auto.scene.reader import plan_ground_polygon_layout, read_house_pv_scene, shading_state
from pvsyst_auto.scene.terrain import build_terrain_mesh, emit_pvshd_ground
from pvsyst_auto.workflows.house import run_house_task
from pvsyst_auto.workflows.utility import validate_utility_spec
from tests._fixtures import build_grid_vc0, init_workspace, roof_mesh as _roof_mesh, sloped_roof_mesh
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT
SINGLE_2T_VC0 = build_grid_vc0(comment="BaseSingle", version="8.1.6", orientations=[{"id": 1, "tilt": 25.0, "azim": 20.0}],
    subarrays=[{"id": 1, "orient": 1, "name": "PV Array", "pan": "Mod600.PAN", "ond": "Inv50.OND",
        "n_mod": 12, "n_str": 2, "n_inv": 2, "n_mppt": 2, "flags": "$001E0402", "flags2": "$20"}], mv_nb_transfos=0)
MG = {"model_name": "Mod600", "nom_power_wp": 600.0, "width_m": 1.134, "length_m": 2.465, "geometry_depth_m": 0.03}
class ObstaclePipelineTests(unittest.TestCase):
    def _setup_ws(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = init_workspace(Path(tmp.name).resolve() / "ws")
        (ws / "Projects/Demo.PRJ").write_text(
            "PVObject_=pvProject\n  PVObject_SitePrj=pvSite\n    NomF=Demo.SIT\n  End of PVObject pvSite\n  MeteoFileName=Demo.MET\nEnd of PVObject pvProject\n",
            encoding="utf-8")
        (ws / "Projects/Demo.VC0").write_bytes(SINGLE_2T_VC0.replace("\n", "\r\n").encode("utf-8"))
        (ws / "Sites/Demo.SIT").write_text("PVObject_=pvSite\n  Latitude=45.0\n  Longitude=5.0\n  Altitude=10.0\nEnd of PVObject pvSite\n",
            encoding="utf-8")
        (ws / "Meteo/Demo.MET").write_text("PVObject_=pvMeteo\nEnd of PVObject pvMeteo\n", encoding="utf-8")
        comps = ComponentStore(ws)
        return ws, comps, VariantStore(ws, comps)
    def _obstacles(self):
        return [{"kind": "building", "name": "NeighborSouth", "origin_m": [0.0, -20.0, 0.0],
             "length_m": 10.0, "width_m": 6.0, "height_m": 7.5, "rotation_deg": 15.0, "roof": {"kind": "gable", "pitch_deg": 20.0}},
            {"kind": "wall", "name": "ParapetWall", "start_m": [0.0, -3.0, 0.0], "end_m": [15.0, -3.0, 0.0], "height_m": 1.8, "thickness_m": 0.25},
            {"kind": "mesh", "name": "ChimneyPrism", "vertices": [[1.0, 1.0, 0.0], [2.0, 1.0, 0.0], [2.0, 2.0, 0.0], [1.5, 1.5, 3.0]],
             "triangles": [[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]]}, {"kind": "tree", "name": "OakSouth", "origin_m": [12.0, -14.0, 0.0],
             "top_height_m": 11.0, "middle_height_m": 8.0, "bottom_height_m": 3.5, "trunk_height_m": 2.5, "crown_diameter_m": 6.0, "trunk_diameter_m": 0.6,
             "rotation_deg": 30.0}]
    def test_obstacle_validation_rejects_invalid_and_duplicate_names(self):
        with self.assertRaises(SceneValidationError):
            obstacle_profiles([{"kind": "building", "name": "MainRoof", "origin_m": [0, 0, 0],
                                "length_m": 5, "width_m": 4, "height_m": 3}], existing_names=["MainRoof"])
        with self.assertRaises(SceneValidationError):
            obstacle_profiles([{"kind": "wall", "name": "BadWall", "start_m": [1, 1, 0], "end_m": [1, 1, 0], "height_m": 2.0}])
        with self.assertRaises(SceneValidationError): obstacle_profiles([{"kind": "unknown", "name": "X"}])
    def test_author_reader_and_headless_linear_and_module_layout_with_obstacles(self):
        ws, comps, store = self._setup_ws()
        house = {"kind": "mesh", "meshes": [_roof_mesh("MainRoof", 0.0, 0.0, 25.0)]}
        layout = {"tables": [{"name": "RowA", "roof_mesh": "MainRoof", "module_rows": 2, "module_columns": 6,
             "row_spacing_m": 0, "column_spacing_m": 0, "orientation": "landscape", "origin_m": [1.0, 1.0, 1.0 * math.tan(math.radians(25.0)) + 0.3]},
            {"name": "RowB", "roof_mesh": "MainRoof", "module_rows": 2, "module_columns": 6,
             "row_spacing_m": 0, "column_spacing_m": 0, "orientation": "landscape", "origin_m": [1.0, 6.0, 6.0 * math.tan(math.radians(25.0)) + 0.3]}]}
        obstacles = self._obstacles()
        created = create_house_pv_scene(store, comps, project="Demo.PRJ", variant="VC0", output_name="WithObstacles.pvc2",
            house=house, layout=layout, module_geometry=MG, obstacles=obstacles, dry_run=False)
        self.assertEqual(created["status"], "created", created)
        self.assertEqual(created["geometry"]["obstacle_count"], 4)
        self.assertEqual(created["geometry"]["tree_count"], 1)
        self.assertEqual(created["geometry"]["obstacle_mesh_count"], 3)
        readback = read_house_pv_scene(comps, "WithObstacles.pvc2")
        self.assertEqual(readback["status"], "ready", readback)
        self.assertEqual(len(readback["create_arguments"]["obstacles"]), 4)
        self.assertEqual(readback["create_arguments"]["obstacles"][3]["kind"], "tree")
        self.assertEqual(readback["create_arguments"]["obstacles"][3]["crown_diameter_m"], 6.0)
        for mode in ("linear", "module_layout"):
            with self.subTest(shading_mode=mode):
                imp = import_house_pv_scene_native(store, comps, "WithObstacles.pvc2", f"Obs {mode}", dry_run=False, confirm=True, shading_mode=mode)
                self.assertEqual(imp["status"], "native_variant_created", imp)
                self.assertTrue(imp["stored_geometry_verified"])
                self.assertEqual(imp["obstacle_count"], 4)
                self.assertEqual(imp["tree_count"], 1)
                self.assertEqual(imp["obstacle_mesh_count"], 3)
                vc_path = Path(imp["file"])
                vc_text = vc_path.read_text(encoding="utf-8-sig")
                blocks = objects(vc_text)
                elems = [b for b in blocks if b.object_type == "pvShdElemObject"]
                gens = [b for b in blocks if b.object_type == "pvShdGenObject"]
                self.assertEqual(len(elems), 1)
                self.assertEqual(len(gens), 4)  # 1 house mesh + 3 mesh obstacles (building, wall, mesh)
                tree = elems[0]
                self.assertEqual(tree.one("ObjetElemType")[1], "oeTree")
                self.assertEqual(tree.one("Source")[1], "osPVsyst")
                self.assertEqual(tree.one("Flags")[1], "$02200004")
                self.assertEqual(tree.one("Params")[1], "11.000,8.000,3.500,2.500,6.000,0.600,")
                self.assertEqual(float(tree.one("DAzimut")[1]), 50.0)  # 20.0 + 30.0
                sh_state = shading_state(vc_text.splitlines(True))
                self.assertEqual(sh_state["mode"], "active")
                self.assertEqual(sh_state["pv_bindings"]["status"], "stored_consistent")
                if mode == "module_layout":
                    self.assertTrue(imp["stored_module_layout_verified"])
                    self.assertEqual(sh_state["module_layout"]["status"], "stored_consistent")
                verified = _verify_new(vc_path, (ws / "Projects/Demo.VC0").read_bytes(), 1, 2, 24,
                    shading_mode=mode, obstacle_gen_count=3, obstacle_tree_count=1)
                self.assertEqual(verified["trees"], 1)
                self.assertEqual(verified["house"], 4)
    def test_house_workflow_dry_run_accepts_obstacles_without_gui(self):
        ws, comps, store = self._setup_ws()
        house = {"kind": "mesh", "meshes": [_roof_mesh("MainRoof", 0.0, 0.0, 25.0)]}
        layout = {"tables": [{"name": "RowA", "roof_mesh": "MainRoof", "module_rows": 2, "module_columns": 6,
             "row_spacing_m": 0.001, "column_spacing_m": 0, "orientation": "landscape", "origin_m": [1.0, 1.0, 1.0 * math.tan(math.radians(25.0)) + 0.3]},
            {"name": "RowB", "roof_mesh": "MainRoof", "module_rows": 2, "module_columns": 6,
             "row_spacing_m": 0, "column_spacing_m": 0, "orientation": "landscape", "origin_m": [1.0, 6.0, 6.0 * math.tan(math.radians(25.0)) + 0.3]}]}
        req = {"source_project": "Demo.PRJ", "source_variant": "VC0", "new_project": "HouseOut.PRJ",
            "variant_name": "Obs House", "year": 2020, "house": house, "layout": layout,
            "obstacles": self._obstacles(), "module_geometry": MG, "shading_mode": "module_layout", "report_pdf": False}
        fake_cli = type("Cli", (), {"cli": str(ws / "PVsystCLI.exe"), "list_variants": lambda _s, _p: ["VC0"]})()
        blocked = run_house_task(store, comps, fake_cli, request={**req, "allow_window_creation": False}, dry_run=False, confirm=True)
        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assertIn("House shading recomputation requires", blocked["issues"][0])
        out = run_house_task(store, comps, fake_cli, request=req, dry_run=True)
        self.assertEqual(out["status"], "ready", out)
        self.assertEqual(out["request_summary"]["shading_mode"], "module_layout")
        self.assertFalse((ws / "Projects/HouseOut.PRJ").exists())
        precise_layout = {"tables": [dict(layout["tables"][0], row_spacing_m=0.0005), layout["tables"][1]]}
        before = set(ws.rglob("*"))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                blocked = run_house_task(store, comps, fake_cli, request={**req, "layout": precise_layout}, dry_run=dry_run, confirm=not dry_run)
                self.assertEqual(blocked["status"], "blocked", blocked)
                self.assertRegex(blocked["issues"][0], r"RowA.*row_spacing_m=0\.0005 m.*0\.001 m")
                self.assertEqual(set(ws.rglob("*")), before)
    def test_portrait_spacing_three_roofs_powersharing_and_no_shading_source(self):
        ws, comps, store = self._setup_ws()
        # 3-orientation source WITHOUT pre-existing pvShading: SA1 unshared (-1), SA2+SA3 shared PowerSharingInverterId=1
        three_roof_vc0 = build_grid_vc0(comment="3-Roof Source", version="8.1.7", main_flags="$01900400",
            include_site_met=False, include_shading=False, orient_version=True,
            orientations=[{"id": 1, "tilt": 25.0, "azim": 0.0}, {"id": 2, "tilt": 20.0, "azim": -90.0}, {"id": 3, "tilt": 20.0, "azim": 90.0}],
            circuit_inverters=[
                {"n_elements": 1, "subarray_id": 1, "name": "South", "mppts": [{"n_elements": 1, "subarray_id": 1, "name": "South", "strings": 1}]}, {
                    "n_elements": 1, "subarray_id": None, "name": "East; West", "mppts": [
                        {"n_elements": 1, "subarray_id": 2, "name": "East", "strings": 1},
                        {"n_elements": 1, "subarray_id": 3, "name": "West", "strings": 1}]}],
            subarrays=[{"id": 1, "orient": 1, "name": "South", "n_mod": 10, "n_str": 1, "flags": "$019E0402", "flags2": "$20", "power_sharing_id": -1},
                {"id": 2, "orient": 2, "name": "East", "n_mod": 10, "n_str": 1, "flags": "$019E0502", "flags2": "$20", "power_sharing_id": 1},
                {"id": 3, "orient": 3, "name": "West", "n_mod": 10, "n_str": 1, "flags": "$019E0502", "flags2": "$20", "power_sharing_id": 1}])
        (ws / "Projects/Demo.VC1").write_bytes(three_roof_vc0.replace("\n", "\r\n").encode("utf-8"))
        sloped = sloped_roof_mesh
        house = {"kind": "mesh", "meshes": [sloped("SouthRoof", [0.0, 0.0, 4.0], 14.0, 8.0, 25.0, 0.0),
            sloped("EastRoof", [25.0, 0.0, 4.0], 14.0, 8.0, 20.0, -90.0), sloped("WestRoof", [-25.0, 0.0, 4.0], 14.0, 8.0, 20.0, 90.0)]}
        layout = {"tables": [{"name": "SouthTbl", "orientation_id": 1, "roof_mesh": "SouthRoof",
             "module_rows": 2, "module_columns": 5, "orientation": "portrait", "row_spacing_m": 0.03, "column_spacing_m": 0.04,
             "origin_m": [1.0, 0.5, 4.0 + 0.5 * math.tan(math.radians(25.0)) + 0.2]}, {"name": "EastTbl", "orientation_id": 2, "roof_mesh": "EastRoof",
             "module_rows": 2, "module_columns": 5, "orientation": "portrait", "row_spacing_m": 0.03, "column_spacing_m": 0.04,
             "origin_m": [25.0 - 0.5 * math.cos(math.radians(20.0)), 1.0, 4.0 + 0.5 * math.sin(math.radians(20.0)) + 0.2]},
            {"name": "WestTbl", "orientation_id": 3, "roof_mesh": "WestRoof", "module_rows": 2, "module_columns": 5, "orientation": "portrait",
             "row_spacing_m": 0.03, "column_spacing_m": 0.04,
             "origin_m": [-25.0 + 0.5 * math.cos(math.radians(20.0)), -1.0, 4.0 + 0.5 * math.sin(math.radians(20.0)) + 0.2]}]}
        sc = create_house_pv_scene(store, comps, project="Demo.PRJ", variant="VC1", output_name="ThreeRoofPortrait.pvc2",
            house=house, layout=layout, module_geometry=MG, dry_run=False)
        self.assertEqual(sc["status"], "created", sc)
        source_path = ws / "Projects/Demo.VC1"
        source_before = source_path.read_bytes()
        variants_before = set(source_path.parent.iterdir())
        for mode, field, spacing in (("linear", "column_spacing_m", 0.0204), ("module_layout", "row_spacing_m", 0.030001)):
            with self.subTest(unsupported_spacing=field):
                name = f"SubMillimetre-{mode}.pvc2"
                precise_layout = {"tables": [dict(layout["tables"][0], **{field: spacing}), *layout["tables"][1:]]}
                authored = create_house_pv_scene(store, comps, project="Demo.PRJ", variant="VC1", output_name=name,
                    house=house, layout=precise_layout, module_geometry=MG, dry_run=False)
                self.assertEqual(authored["status"], "created", authored)
                readback = read_house_pv_scene(comps, name)
                self.assertEqual(readback["create_arguments"]["layout"]["tables"][0][field], spacing)
                blocked = import_house_pv_scene_native(store, comps, name, "Submillimetre", shading_mode=mode,
                    dry_run=mode == "linear", confirm=mode != "linear")
                self.assertEqual((blocked["status"], blocked["stage"]), ("blocked", "preflight"))
                self.assertFalse(blocked["precheck_passed"])
                self.assertRegex(blocked["issues"][0], rf"SouthTbl.*{field}={spacing} m.*0\.001 m")
        self.assertEqual(set(source_path.parent.iterdir()), variants_before)
        self.assertEqual(source_path.read_bytes(), source_before)
        # Verify BOTH implicit 3-roof module_layout AND linear succeed on unshaded PowerSharing source
        for mode in ("linear", "module_layout"):
            imp = import_house_pv_scene_native(store, comps, "ThreeRoofPortrait.pvc2", f"3R {mode}", dry_run=False, confirm=True, shading_mode=mode)
            self.assertEqual(imp["status"], "native_variant_created", imp)
            self.assertTrue(imp["stored_geometry_verified"])
            vc_text = Path(imp["file"]).read_text(encoding="utf-8-sig")
            self.assertIn("ModuleOrient=moPortrait", vc_text)
            self.assertIn("ModSpacingX=0.040", vc_text)
            self.assertIn("ModSpacingY=0.030", vc_text)
            sh = shading_state(vc_text.splitlines(True))
            self.assertEqual(sh["mode"], "active")
            self.assertEqual(len(sh["pv_bindings"]["tables"]), 3)
            if mode == "module_layout":
                self.assertTrue(imp["stored_module_layout_verified"])
                self.assertEqual(sh["module_layout"]["status"], "stored_consistent")
                self.assertIn("ModOrient=moPortrait", vc_text)
        # Explicit string_layout also works with portrait + positive spacing + PowerSharing
        sl = {"tables": [{"name": "SouthTbl", "subarray_id": 1, "cells": [{"row": r, "col": c, "global_string": 1} for r in (1, 2) for c in range(1, 6)]},
            {"name": "EastTbl", "subarray_id": 2, "cells": [{"row": r, "col": c, "global_string": 2} for r in (1, 2) for c in range(1, 6)]},
            {"name": "WestTbl", "subarray_id": 3, "cells": [{"row": r, "col": c, "global_string": 3} for r in (1, 2) for c in range(1, 6)]}]}
        sc_exp = create_house_pv_scene(store, comps, project="Demo.PRJ", variant="VC1", output_name="ThreeRoofExplicit.pvc2",
            house=house, layout=layout, string_layout=sl, module_geometry=MG, dry_run=False)
        self.assertEqual(sc_exp["status"], "created", sc_exp)
        imp_exp = import_house_pv_scene_native(store, comps, "ThreeRoofExplicit.pvc2", "3R explicit",
            dry_run=False, confirm=True, shading_mode="module_layout")
        self.assertEqual(imp_exp["status"], "native_variant_created", imp_exp)
        self.assertTrue(imp_exp["stored_module_layout_verified"])
    def test_grouped_owner_resolution_variant_ids_assemble_variant_and_nonzero_anchor_verify(self):
        from pvsyst_auto.native.cli import PVsystCLI
        from pvsyst_auto.native.headless import (_next_variant_id, _resolve_table_owner, _support_gate, assemble_variant, build_pvshading)
        from pvsyst_auto.scene.author import house_profile, table_profiles
        ws, comps, store = self._setup_ws()
        # 1. _resolve_table_owner: non-matching SubArrayId vs NoOrientation (SA2->Orient1, SA3->Orient2)
        orientations_offset = [{"id": 1, "subarray_id": 2, "module_total": 10, "tilt_deg": 25.0, "azimuth_deg": 180.0, "field_azimuth_deg": 0.0},
            {"id": 2, "subarray_id": 3, "module_total": 10, "tilt_deg": 20.0, "azimuth_deg": 90.0, "field_azimuth_deg": -90.0}]
        self.assertEqual(_resolve_table_owner({"name": "T1", "orientation_id": 1}, orientations_offset), 2)
        self.assertEqual(_resolve_table_owner({"name": "T2", "orientation_id": 2}, orientations_offset), 3)
        # Shared orientation requires explicit source_subarray_id
        shared_orients = [{"id": 1, "subarray_id": 1, "module_total": 10, "tilt_deg": 25.0, "azimuth_deg": 180.0, "field_azimuth_deg": 0.0},
            {"id": 1, "subarray_id": 2, "module_total": 10, "tilt_deg": 25.0, "azimuth_deg": 180.0, "field_azimuth_deg": 0.0}]
        with self.assertRaisesRegex(ValueError, "source_subarray_id"): _resolve_table_owner({"name": "Ambiguous", "orientation_id": 1}, shared_orients)
        self.assertEqual(_resolve_table_owner({"name": "Bound", "orientation_id": 1, "source_subarray_id": 2}, shared_orients), 2)
        # End-to-end build_pvshading + _support_gate on SA2->Orient1, SA3->Orient2 without source_subarray_id
        offset_vc = build_grid_vc0(comment="Offset SubArray IDs", version="8.1.6", main_flags="$01900400", include_site=False, include_shading=False,
            orient_version=True, orientations=[{"id": 1, "tilt": 25.0, "azim": 20.0}, {"id": 2, "tilt": 20.0, "azim": -70.0}],
            subarrays=[{"id": 2, "orient": 1, "name": "South", "n_mod": 10, "n_str": 1, "flags": "$019E0402", "flags2": "$20"},
                {"id": 3, "orient": 2, "name": "East", "n_mod": 10, "n_str": 1, "flags": "$019E0402", "flags2": "$20"}])
        sloped = sloped_roof_mesh
        # Note: COLLADA frame uses orientation 1 as y-up roof, orientation 2 rotated by +90 deg relative to orientation 1
        house_2r = {"kind": "mesh", "meshes": [sloped("Roof1", [0.0, 0.0, 4.0], 20.0, 8.0, 25.0, 0.0),
            sloped("Roof2", [25.0, 0.0, 4.0], 20.0, 8.0, 20.0, -90.0)]}
        pocket_2r = house_profile(house_2r)
        source_2r = {"pan_name": "Mod600.PAN", "module_total": 20, "tilt_deg": 25.0,
            "azimuth_deg": 200.0, "field_azimuth_deg": 20.0, "site": {"latitude": 45.0, "longitude": 5.0, "altitude": 10.0}, "orientations": [
                {"id": 1, "subarray_id": 2, "module_total": 10, "tilt_deg": 25.0, "azimuth_deg": 200.0, "field_azimuth_deg": 20.0},
                {"id": 2, "subarray_id": 3, "module_total": 10, "tilt_deg": 20.0, "azimuth_deg": 110.0, "field_azimuth_deg": -70.0}]}
        modules_meta = {"name": "Mod600", "width_m": 1.134, "length_m": 2.465, "depth_m": 0.03}
        tables_2r = table_profiles({"tables": [{"name": "SouthT", "orientation_id": 1, "roof_mesh": "Roof1",
             "module_rows": 2, "module_columns": 5, "orientation": "landscape", "row_spacing_m": 0.0, "column_spacing_m": 0.0,
             "origin_m": [1.0, 0.5, 4.0 + 0.5 * math.tan(math.radians(25.0)) + 0.2]}, {"name": "EastT", "orientation_id": 2, "roof_mesh": "Roof2",
             "module_rows": 2, "module_columns": 5, "orientation": "landscape", "row_spacing_m": 0.0, "column_spacing_m": 0.0,
             "origin_m": [25.0 - 0.5 * math.cos(math.radians(20.0)), 1.0, 4.0 + 0.5 * math.sin(math.radians(20.0)) + 0.2]},
        ]}, pocket_2r, modules_meta, source_2r)
        _support_gate(source_2r, tables_2r, offset_vc, "module_layout")
        shd_text, slot_map = build_pvshading(house_2r, tables_2r, modules_meta, source_2r, "linear", max_tables=64)
        self.assertIn("DAzimut=20.000000", shd_text)
        self.assertIn("DAzimut=-70.000000", shd_text)
        # 2. _prep._verify_new passes anchor_turn on non-zero anchor grouped variants (FieldAzim=20.0)
        assembled_2r = assemble_variant(offset_vc, shd_text)
        target_2r = ws / "Projects" / "Demo.VC8"
        target_2r.write_bytes(assembled_2r.encode("utf-8"))
        verified_2r = _verify_new(target_2r, offset_vc.encode("utf-8"), 2, 2, 20,
            scene_context={"house": pocket_2r, "tables": tables_2r, "modules": modules_meta, "source": source_2r})
        self.assertTrue(verified_2r["house_geometry_verified"])
        self.assertTrue(verified_2r["pv_geometry_verified"])
        target_2r.unlink()
        # 3. _next_variant_id base-36 3-character progression (VC0..VC9, VCA..VCZ, VD0..VZZ; never VC10)
        proj_dir = ws / "Projects"
        for i in range(1, 10): (proj_dir / f"Demo.VC{i}").write_text("v", encoding="utf-8")
        self.assertEqual(_next_variant_id(store, "Demo.PRJ"), "VCA")
        for ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"[10:]: (proj_dir / f"Demo.VC{ch}").write_text("v", encoding="utf-8")
        self.assertEqual(_next_variant_id(store, "Demo.PRJ"), "VD0")
        (proj_dir / "Demo.VC10").write_text("bad4char", encoding="utf-8")
        fake_exe = ws / "PVsystCLI.exe"
        fake_exe.write_bytes(b"MZ")
        cli_check = PVsystCLI(fake_exe, ws)
        listed = cli_check.list_variants("Demo.PRJ")
        self.assertIn("VCA", listed)
        self.assertNotIn("VC10", listed)
        # 4. assemble_variant strips pre-existing pvModuleLayout and allows TEcoEvaluation TVarSimul
        warm_with_ml_and_econ = offset_vc.replace("End of PVObject pvVCalcul", "  EconomicEval=TEcoEvaluation\n" "    Currency=EUR\n"
            "    HourlyTariff=THourlyParams\n" "      ParSimulH=TVarSimul\n" "        Varident=TarifH\n" "      End of VarSimul=viTarifH\n"
            "    End of THourlyParams\n" "  End of TEcoEvaluation\n" "  PVObject_ModuleLayout=pvModuleLayout\n" "    Flags=$0B00\n"
            "  End of PVObject pvModuleLayout\n" "End of PVObject pvVCalcul")
        linear_out = assemble_variant(warm_with_ml_and_econ, shd_text, module_layout=None)
        self.assertNotIn("pvModuleLayout", linear_out)
        self.assertIn("ParSimulH=TVarSimul", linear_out)
        new_ml = "  PVObject_ModuleLayout=pvModuleLayout\n    Comment=Fresh\n  End of PVObject pvModuleLayout"
        ml_out = assemble_variant(warm_with_ml_and_econ, shd_text, module_layout=new_ml)
        self.assertEqual(ml_out.count("PVObject_ModuleLayout=pvModuleLayout"), 1)
        self.assertIn("Comment=Fresh", ml_out)
        for leftover in ("  PVObject_Simul=pvSimulFile\n  End of TSimulFich\n", "  End of VarSimul=E_Grid\n"):
            bad_src = offset_vc.replace("End of PVObject pvVCalcul", leftover + "End of PVObject pvVCalcul")
            with self.assertRaisesRegex(ValueError, "derived item still present"): assemble_variant(bad_src, shd_text)
def _sloped_hill_points() -> list[list[float]]:
    """5x5 elevation grid with a 10% E-W slope and 5% N-S ridge."""
    pts = []
    for ix in range(-2, 3):
        for iy in range(-2, 3):
            x = ix * 30.0
            y = iy * 30.0
            z = 0.10 * x + 0.05 * y + 1.5 * (1.0 - abs(ix) * 0.25)
            pts.append([x, y, round(z, 3)])
    return pts
class TerrainDemTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "PVsyst8.1_Data"
        for sub in ("Projects", "Sites", "Meteo", "UserData", "ComposPV/PVmodules", "ComposPV/Inverters"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.root / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.root / "ComposPV/PVmodules/Lab440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        (self.root / "ComposPV/Inverters/Lab50.OND").write_text(UTILITY_OND, encoding="utf-8")
        self.store = VariantStore(self.root, ComponentStore(self.root))
    def test_triangulation_interpolation_csv_lonlat_and_geotiff_guard(self) -> None:
        # 1. Pure-Python Delaunay + linear plane exact interpolation check
        plane_pts = [[0.0, 0.0, 10.0], [100.0, 0.0, 20.0], [100.0, 100.0, 25.0], [0.0, 100.0, 15.0]]
        mesh = build_terrain_mesh({"points": plane_pts, "name": "TestSlope"})
        self.assertEqual(len(mesh.vertices), 4)
        self.assertEqual(len(mesh.triangles), 2)
        z_mid, normal = mesh.sample(50.0, 50.0)
        self.assertAlmostEqual(z_mid, 17.5, places=4)
        self.assertGreater(normal[2], 0.9)
        slope_ew = mesh.slope_deg_along_direction(50.0, 50.0, 1.0, 0.0, span_m=10.0)
        self.assertAlmostEqual(slope_ew, 5.7106, places=2)  # atan(0.10)
        # 2. CSV + lonlat ENU projection
        csv_path = self.root / "UserData" / "hill.csv"
        csv_path.write_text("lon,lat,elev\n" "120.000,31.000,10.0\n" "120.001,31.000,15.0\n"
            "120.001,31.001,18.0\n" "120.000,31.001,12.0\n", encoding="utf-8")
        mesh_csv = build_terrain_mesh({"csv_name": "hill.csv", "coordinate_system": "lonlat"}, workspace=self.root)
        self.assertGreater(mesh_csv.bounds["max_x"] - mesh_csv.bounds["min_x"], 80.0)
        self.assertEqual(len(mesh_csv.triangles), 2)
        # 3. pvShdGround emission format check
        lines = emit_pvshd_ground(mesh, object_index=5)
        joined = "\n".join(lines)
        self.assertIn("PVObject_5=pvShdGround", joined)
        self.assertIn("Source=osGroundCSV", joined)
        self.assertIn("NPElements=8", joined)
        self.assertIn("NElements=2", joined)
        self.assertIn("Faces=-1,", joined)
        self.assertIn("End of PVObject pvShdGround", joined)
        # 4. Graceful GeoTIFF guard when rasterio [geo] extra is missing
        tif_path = self.root / "UserData" / "site.tif"
        tif_path.write_bytes(b"fake-tif")
        with patch.dict("sys.modules", {"rasterio": None}):
            with self.assertRaisesRegex(ValueError, r"pvsyst-cli-mcp\[geo\]"): build_terrain_mesh({"geotiff_name": "site.tif"}, workspace=self.root)
    def test_terrain_shed_rows_follow_azimuth(self) -> None:
        import re
        from pvsyst_auto.api.projects import pvsyst_prepare_design_project
        from pvsyst_auto.api.schemas import SingleDesignRequest
        from tests.unit.core.test_bifacial_utility import _capacity_spec, _FakeUtilityCLI
        request = SingleDesignRequest(scope='ground_sheds', new_project='Terrain.PRJ', year=2024, target_dc_kw=100,
            tilt=22, azimuth=90, module={'file_name': 'Lab440.PAN'}, inverter={'file_name': 'Lab50.OND'},
            weather={'mode': 'existing', 'sit_name': 'Geneva_MN90.SIT', 'met_name': 'Geneva_MN90_SYN.MET'},
            capacity=_capacity_spec(), ground_layout={'model_mode': '3d', 'pitch_m': 11, 'shed_count': 6,
                'module_rows': 2, 'module_columns': 19, 'terrain': {'points': [[-100, -100, 0], [100, -100, 0], [100, 100, 0], [-100, 100, 0]]}})
        with patch('pvsyst_auto.api.projects.cli', return_value=_FakeUtilityCLI(self.root)), \
             patch('pvsyst_auto.api.projects.variant_store', return_value=self.store), \
             patch('pvsyst_auto.workflows.utility.inspect_met_calendar', return_value={'issues': [], 'warnings': [], 'year': 2024, 'rows': 8784}):
            result = pvsyst_prepare_design_project(request, dry_run=False)
        self.assertEqual(result['status'], 'prepared', result)
        text = (self.root / 'Projects/Terrain.VC0').read_text()
        blocks = re.findall(r'PVObject_\d+=pvShdArrayTable.*?End of PVObject pvShdArrayTable', text, re.S)
        origins = [tuple(map(float, re.search(r'DOrigine=([^\r\n]+)', b).group(1).split(','))) for b in blocks]
        self.assertEqual(len(origins), 6)
        self.assertAlmostEqual(origins[1][0] - origins[0][0], 11)
        self.assertAlmostEqual(origins[1][1] - origins[0][1], 0)
    def test_synthesize_utility_sheds_and_trackers_with_terrain(self) -> None:
        pts = _sloped_hill_points()
        # 1. Fixed sheds draped on sloped DEM
        shed_res = synthesize_utility_project(self.store, project_comment="TerrainShed", variant_comment="ShedDEM",
            site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET", field_type="RegularSheds", pan_name="Lab440.PAN",
            ond_name="Lab50.OND", n_mod_serie=20, n_string_ch=10, n_inverter=2, n_inv_mppt=1, shed_count=4, module_rows=2, module_columns=25,
            pitch_m=10.0, tilt_deg=25.0, terrain={"points": pts, "name": "HillDEM", "follow_slope": True})
        vc_shed = shed_res["vc0_text"]
        self.assertIn("=pvShdArrayTable", vc_shed)
        self.assertIn("=pvShdGround", vc_shed)
        self.assertIn("terrain", shed_res["utility_summary"])
        self.assertEqual(shed_res["utility_summary"]["terrain"]["vertex_count"], 25)
        # 2. Single-axis trackers draped on sloped DEM
        trk_res = synthesize_utility_project(self.store, project_comment="TerrainTrk", variant_comment="TrkDEM",
            site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET", field_type="TrackHorizNSAxis", pan_name="Lab440.PAN",
            ond_name="Lab50.OND", n_mod_serie=20, n_string_ch=10, n_inverter=2, n_inv_mppt=1, shed_count=4, module_rows=2, module_columns=25,
            pitch_m=10.0, terrain={"points": pts, "name": "HillDEM", "follow_slope": True})
        vc_trk = trk_res["vc0_text"]
        self.assertIn("PVObject_4=pvShdArrayTracking", vc_trk)
        self.assertIn("PVObject_5=pvShdGround", vc_trk)
        # Non-zero axis tilt computed from 5% N-S slope (~2.86 deg)
        tilts = [t["axis_tilt_deg"] for t in trk_res["utility_summary"]["terrain"]["trackers"]]
        self.assertTrue(any(abs(t) > 1.5 for t in tilts))
    def test_polygon_clipper_rectangular_l_shape_and_narrow_vertical(self) -> None:
        with self.subTest(shape="rectangle_with_setback"):
            rect = [[0.0, 0.0], [50.0, 0.0], [50.0, 40.0], [0.0, 40.0]]
            plan = plan_ground_polygon_layout(rect, tilt_deg=25.0, azimuth_deg=0.0,
                pitch_m=8.0, module_width_m=1.134, module_length_m=2.278, module_rows=2,
                module_orientation="portrait", setback_m=2.0, max_table_columns=16, base_slope_deg=5.0)
            self.assertEqual(plan["site_area_m2"], 2000.0)
            self.assertGreaterEqual(plan["row_count"], 4)
            self.assertGreater(plan["table_count"], 4)
            self.assertGreater(plan["total_modules"], 100)
            self.assertEqual(plan["base_slope_deg"], 5.0)
            self.assertTrue(all(t["module_columns"] <= 16 for t in plan["tables"]))
        with self.subTest(shape="concave_l_shape"):
            l_poly = [[0.0, 0.0], [60.0, 0.0], [60.0, 20.0], [25.0, 20.0], [25.0, 50.0], [0.0, 50.0]]
            l_plan = plan_ground_polygon_layout(l_poly, tilt_deg=20.0, azimuth_deg=15.0,
                pitch_m=9.0, module_width_m=1.052, module_length_m=2.131, module_rows=2, setback_m=1.0)
            self.assertAlmostEqual(l_plan["site_area_m2"], 60 * 20 + 25 * 30, places=1)
            self.assertGreater(l_plan["table_count"], 2)
        for vertices in ([[0, 0], [10, 0], [10, 2], [0, 2]], [[0, 2], [10, 2], [10, 0], [0, 0]]):
            with self.subTest(shape="narrow_vertical", vertices=vertices):
                narrow = plan_ground_polygon_layout(vertices, tilt_deg=90, pitch_m=3, module_width_m=1,
                    module_length_m=1, module_rows=1, spacing_x_m=0, spacing_y_m=0)
                self.assertEqual(narrow["total_modules"], 10)
                self.assertEqual(narrow["row_count"], 1)
    def test_utility_synthesis_with_site_polygon_obstacles_and_nonzero_base_slope(self) -> None:
        (self.root / "Sites" / "TemplateSite.SIT").write_text("PVObject_=pvSite\n  NomF=TemplateSite.SIT\n  Site=TemplateSite\n"
            "  Latitude=46.24\n  Longitude=6.11\nEnd of PVObject pvSite\n", encoding="utf-8")
        (self.root / "Meteo" / "TemplateMeteo.MET").write_bytes(b"fixture")
        poly = [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]]
        obstacles = [{"kind": "building", "name": "Substation", "origin_m": [42.0, 5.0, 0.0], "length_m": 6.0, "width_m": 4.0, "height_m": 3.5},
            {"kind": "tree", "name": "SouthOak", "origin_m": [20.0, -6.0, 0.0], "top_height_m": 8.0, "middle_height_m": 6.0, "bottom_height_m": 3.0,
             "trunk_height_m": 2.5, "crown_diameter_m": 4.0, "trunk_diameter_m": 0.4}]
        synth = synthesize_utility_project(self.store, project_comment="Ground3DPoly", variant_comment="Finite 3D + Slope",
            site_name="TemplateSite.SIT", met_name="TemplateMeteo.MET", field_type="RegularSheds", pan_name="Generic_Mono_300W.PAN",
            ond_name="Generic_7.5kW.OND", n_mod_serie=14, n_string_ch=6, n_inverter=2,
            n_inv_mppt=6, independent_mppt=True, pnom_ac_in_mppt=2.5, tilt_deg=25.0, azimuth_deg=0.0, pitch_m=8.0, module_rows=2, site_polygon=poly,
            setback_m=1.0, obstacles=obstacles, base_slope_deg=5.0)
        vc0 = synth["vc0_text"]
        self.assertIn("BaseSlope=5.0\r\n", vc0)
        self.assertIn("BaseSlope=5.000000\r\n", vc0)
        self.assertIn("pvShdArrayTable", vc0)
        self.assertIn("pvShdGenObject", vc0)
        self.assertIn("pvShdElemObject", vc0)
        self.assertIn("ground_3d_layout", synth["utility_summary"])
        self.assertEqual(synth["utility_summary"]["obstacle_count"], 2)
        self.assertEqual(synth["utility_summary"]["base_slope_deg"], 5.0)
        self.store.create_project("Ground3DPoly.PRJ", synth["prj_text"], {"VC0": vc0})
        val = self.store.validate_structure("Ground3DPoly.PRJ", "VC0")
        self.assertTrue(val["valid"], val)
    def test_utility_spec_and_schema_validation_for_ground_3d_fields(self) -> None:
        req = SingleDesignRequest(scope="ground_sheds", new_project="PolyGround.PRJ", year=2023,
            target_dc_kw=50.0, tilt=25.0, azimuth=0.0, sizing_mode="auto_from_components",
            module={"file_name": "Lab.PAN"}, inverter={"file_name": "Lab.OND"},
            weather={"mode": "existing", "sit_name": "S.SIT", "met_name": "M.MET"}, ground_layout={"model_mode": "3d", "pitch_m": 8.0,
                "site_polygon": [[0, 0], [40, 0], [40, 30], [0, 30]], "setback_m": 1.0, "base_slope_deg": 4.0,
                "obstacles": [{"kind": "wall", "name": "Fence", "start_m": [0, -2, 0], "end_m": [10, -2, 0], "height_m": 2.5, "thickness_m": 0.3}]})
        issues: list[dict] = []
        validate_utility_spec(req.model_dump(exclude_none=True), "ground_sheds", issues)
        self.assertEqual(issues, [])
        trk_issues: list[dict] = []
        validate_utility_spec({"scope": "ground_tracker",
            "ground_layout": {"site_polygon": [[0, 0], [40, 0], [40, 30], [0, 30]], "base_slope_deg": 3.0}}, "ground_tracker", trk_issues)
        fields = {i["field"] for i in trk_issues}
        self.assertIn("ground_layout.site_polygon", fields)
        self.assertIn("ground_layout.base_slope_deg", fields)
        # Boundary float-grouping regression: 30 modules fit cleanly across the exact polygon span
        boundary = plan_ground_polygon_layout([[500000.0, 0.0], [500017.28999990004, 0.0], [500017.28999990004, 10.0], [500000.0, 10.0]],
            tilt_deg=0.0, azimuth_deg=0.0, pitch_m=6.0, module_width_m=1.134, module_length_m=2.0, module_rows=1, spacing_x_m=0.02, setback_m=0.0)
        self.assertEqual(boundary["total_modules"], 30)
if __name__ == "__main__": unittest.main()

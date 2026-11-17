"""Unit tests for environment obstacles (building, wall, mesh, native oeTree) across author, reader, and headless import."""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.components.editor import objects
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.native._prep import _verify_new
from pvsyst_auto.native.headless import import_house_pv_scene_native
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.author import SceneValidationError, create_house_pv_scene, obstacle_profiles
from pvsyst_auto.scene.reader import read_house_pv_scene
from pvsyst_auto.scene.state import shading_state
from pvsyst_auto.workflows.house import run_house_task


def _roof_mesh(name, ox, oy, tilt_deg, rot_deg=0.0):
    t, r = math.radians(tilt_deg), math.radians(rot_deg)

    def pt(x, y):
        dy, dz = y * math.cos(t), y * math.sin(t)
        return [round(ox + x * math.cos(r) - dy * math.sin(r), 6),
                round(oy + x * math.sin(r) + dy * math.cos(r), 6), round(dz, 6)]

    return {"name": name, "vertices": [pt(-2, -2), pt(40, -2), pt(40, 25), pt(-2, 25)],
            "triangles": [[0, 1, 2], [0, 2, 3]]}


SINGLE_2T_VC0 = """PVObject_=pvVCalcul
  Version=8.1.6
  Flags=$00100000
  Comment=BaseSingle
  PVObject_SiteSimul=pvSite
    NomF=Demo.SIT
    Site=Demo
    Latitude=45.0
    Longitude=5.0
    Altitude=10.0
  End of PVObject pvSite
  PVObject_MeteoSimul=pvMeteo
    NomF=Demo.MET
    SiteM=Demo
    PVObject_SiteMet=pvSite
      NomF=Demo.SIT
      Site=Demo
      Latitude=45.0
      Longitude=5.0
      Altitude=10.0
    End of PVObject pvSite
  End of PVObject pvMeteo
  Orientations, list of=1 TOrientGroup
    PVObject_=pvOrient
      NoOrient=1
      Flags=$00
      FieldType=FixedPlane
      FieldTilt=25.0
      FieldAzim=20.0
      BaseSlope=0.0
      UsesTiltAver=False
      UsesAzimAver=False
      UsesSlopeAver=False
    End of TOrientGroup
  End of Orientations
  PVObject_PVMainArray=pvMainArray
    Flags=$00100400
    Flags2=$082A
    MVNbTransfos=0
    PVObject_SystemCircuit=pvCircuit
      Version=8.1.6
      Flags=$00
      InjectionPointNode Start;
        Children=Start
          InverterNode Start;
            NElements=2
            SubArrayId=1
            SubArrayName=PV Array
            Children=Start
              MPPTNode Start;
                NElements=1
                SubArrayId=1
                SubArrayName=PV Array
                Children=Start
                  StringNode Start;
                    NElements=1
                    SubArrayId=1
                    SubArrayName=PV Array
                  StringNode End;
                Children=End
              MPPTNode End;
            Children=End
          InverterNode End;
        Children=End
      InjectionPointNode End;
    End of PVObject pvCircuit
    SubArrays=Start
      PVObject_=pvSubArray
        Comment=PV Array
        Version=8.1.6
        Flags=$001E0402
        Flags2=$20
        SubArrayId=1
        PVModule=Mod600.PAN
        GInverter=Inv50.OND
        NoOrientation=1
        NModSerie=12
        NStringCh=2
        NStrOrient1=2
        NInverter=2
        NInvMPPT=2
        PowerSharingInverterId=-1
      End of PVObject pvSubArray
    EndTags
  End of PVObject pvMainArray
  PVObject_Ombrage=pvShading
    Flags=$00
  End of PVObject pvShading
  PVObject_System=pvSystem
    Flags=$00
    SystemType=Grid
  End of PVObject pvSystem
End of PVObject pvVCalcul
"""

MG = {"model_name": "Mod600", "nom_power_wp": 600.0, "width_m": 1.134, "length_m": 2.465, "geometry_depth_m": 0.03}


class ObstaclePipelineTests(unittest.TestCase):
    def _setup_ws(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = Path(tmp.name).resolve() / "ws"
        for sub in ("Projects", "Sites", "Meteo", "UserData", "Models", "Results",
                    "ComposPV/PVmodules", "ComposPV/Inverters"):
            (ws / sub).mkdir(parents=True, exist_ok=True)
        (ws / "Projects/Demo.PRJ").write_text(
            "PVObject_=pvProject\n  PVObject_SitePrj=pvSite\n    NomF=Demo.SIT\n  End of PVObject pvSite\n  MeteoFileName=Demo.MET\nEnd of PVObject pvProject\n",
            encoding="utf-8",
        )
        (ws / "Projects/Demo.VC0").write_bytes(SINGLE_2T_VC0.replace("\n", "\r\n").encode("utf-8"))
        (ws / "Sites/Demo.SIT").write_text(
            "PVObject_=pvSite\n  Latitude=45.0\n  Longitude=5.0\n  Altitude=10.0\nEnd of PVObject pvSite\n",
            encoding="utf-8",
        )
        (ws / "Meteo/Demo.MET").write_text("PVObject_=pvMeteo\nEnd of PVObject pvMeteo\n", encoding="utf-8")
        comps = ComponentStore(ws)
        return ws, comps, VariantStore(ws, comps)

    def _obstacles(self):
        return [
            {"kind": "building", "name": "NeighborSouth", "origin_m": [0.0, -20.0, 0.0],
             "length_m": 10.0, "width_m": 6.0, "height_m": 7.5, "rotation_deg": 15.0,
             "roof": {"kind": "gable", "pitch_deg": 20.0}},
            {"kind": "wall", "name": "ParapetWall", "start_m": [0.0, -3.0, 0.0],
             "end_m": [15.0, -3.0, 0.0], "height_m": 1.8, "thickness_m": 0.25},
            {"kind": "mesh", "name": "ChimneyPrism",
             "vertices": [[1.0, 1.0, 0.0], [2.0, 1.0, 0.0], [2.0, 2.0, 0.0], [1.5, 1.5, 3.0]],
             "triangles": [[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]]},
            {"kind": "tree", "name": "OakSouth", "origin_m": [12.0, -14.0, 0.0],
             "top_height_m": 11.0, "middle_height_m": 8.0, "bottom_height_m": 3.5,
             "trunk_height_m": 2.5, "crown_diameter_m": 6.0, "trunk_diameter_m": 0.6,
             "rotation_deg": 30.0},
        ]

    def test_obstacle_validation_rejects_invalid_and_duplicate_names(self):
        with self.assertRaises(SceneValidationError):
            obstacle_profiles([{"kind": "building", "name": "MainRoof", "origin_m": [0, 0, 0],
                                "length_m": 5, "width_m": 4, "height_m": 3}], existing_names=["MainRoof"])
        with self.assertRaises(SceneValidationError):
            obstacle_profiles([{"kind": "wall", "name": "BadWall", "start_m": [1, 1, 0],
                                "end_m": [1, 1, 0], "height_m": 2.0}])
        with self.assertRaises(SceneValidationError):
            obstacle_profiles([{"kind": "unknown", "name": "X"}])

    def test_author_reader_and_headless_linear_and_module_layout_with_obstacles(self):
        ws, comps, store = self._setup_ws()
        house = {"kind": "mesh", "meshes": [_roof_mesh("MainRoof", 0.0, 0.0, 25.0)]}
        layout = {"tables": [
            {"name": "RowA", "roof_mesh": "MainRoof", "module_rows": 2, "module_columns": 6,
             "row_spacing_m": 0, "column_spacing_m": 0, "orientation": "landscape",
             "origin_m": [1.0, 1.0, 1.0 * math.tan(math.radians(25.0)) + 0.3]},
            {"name": "RowB", "roof_mesh": "MainRoof", "module_rows": 2, "module_columns": 6,
             "row_spacing_m": 0, "column_spacing_m": 0, "orientation": "landscape",
             "origin_m": [1.0, 6.0, 6.0 * math.tan(math.radians(25.0)) + 0.3]},
        ]}
        obstacles = self._obstacles()
        created = create_house_pv_scene(
            store, comps, project="Demo.PRJ", variant="VC0", output_name="WithObstacles.pvc2",
            house=house, layout=layout, module_geometry=MG, obstacles=obstacles, dry_run=False,
        )
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
                imp = import_house_pv_scene_native(
                    store, comps, "WithObstacles.pvc2", f"Obs {mode}",
                    dry_run=False, confirm=True, shading_mode=mode,
                )
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

                verified = _verify_new(
                    vc_path, (ws / "Projects/Demo.VC0").read_bytes(), 1, 2, 24,
                    shading_mode=mode, obstacle_gen_count=3, obstacle_tree_count=1,
                )
                self.assertEqual(verified["trees"], 1)
                self.assertEqual(verified["house"], 4)

    def test_house_workflow_dry_run_accepts_obstacles_without_gui(self):
        ws, comps, store = self._setup_ws()
        house = {"kind": "mesh", "meshes": [_roof_mesh("MainRoof", 0.0, 0.0, 25.0)]}
        layout = {"tables": [
            {"name": "RowA", "roof_mesh": "MainRoof", "module_rows": 2, "module_columns": 6,
             "row_spacing_m": 0, "column_spacing_m": 0, "orientation": "landscape",
             "origin_m": [1.0, 1.0, 1.0 * math.tan(math.radians(25.0)) + 0.3]},
            {"name": "RowB", "roof_mesh": "MainRoof", "module_rows": 2, "module_columns": 6,
             "row_spacing_m": 0, "column_spacing_m": 0, "orientation": "landscape",
             "origin_m": [1.0, 6.0, 6.0 * math.tan(math.radians(25.0)) + 0.3]},
        ]}
        req = {
            "source_project": "Demo.PRJ",
            "source_variant": "VC0",
            "new_project": "HouseOut.PRJ",
            "variant_name": "Obs House",
            "year": 2020,
            "house": house,
            "layout": layout,
            "obstacles": self._obstacles(),
            "module_geometry": MG,
            "shading_mode": "module_layout",
            "report_pdf": False,
            "allow_window_creation": False,
        }
        fake_cli = type("Cli", (), {"cli": str(ws / "PVsystCLI.exe"), "list_variants": lambda _s, _p: ["VC0"]})()
        out = run_house_task(store, comps, fake_cli, request=req, dry_run=True)
        self.assertEqual(out["status"], "ready", out)
        self.assertEqual(out["request_summary"]["shading_mode"], "module_layout")
        self.assertFalse((ws / "Projects/HouseOut.PRJ").exists())

    def test_portrait_spacing_three_roofs_powersharing_and_no_shading_source(self):
        from pvsyst_auto.scene.author import _rotate_z
        ws, comps, store = self._setup_ws()
        # 3-orientation source WITHOUT pre-existing pvShading: SA1 unshared (-1), SA2+SA3 shared PowerSharingInverterId=1
        three_roof_vc0 = """PVObject_=pvVCalcul
  Comment=3-Roof Source
  Version=8.1.7
  Flags=$00100000
  PVObject_SiteSimul=pvSite
    NomF=Demo.SIT
    Latitude=45.0
    Longitude=5.0
    Altitude=10.0
  End of PVObject pvSite
  PVObject_MeteoSimul=pvMeteo
    NomF=Demo.MET
  End of PVObject pvMeteo
  Orientations, list of=3 TOrientGroup
    PVObject_=pvOrient
      Version=8.1.7
      Flags=$00
      NoOrient=1
      FieldType=FixedPlane
      FieldTilt=25.0
      FieldAzim=0.0
      BaseSlope=0.0
      UsesTiltAver=False
      UsesAzimAver=False
      UsesSlopeAver=False
    End of TOrientGroup
    PVObject_=pvOrient
      Version=8.1.7
      Flags=$00
      NoOrient=2
      FieldType=FixedPlane
      FieldTilt=20.0
      FieldAzim=-90.0
      BaseSlope=0.0
      UsesTiltAver=False
      UsesAzimAver=False
      UsesSlopeAver=False
    End of TOrientGroup
    PVObject_=pvOrient
      Version=8.1.7
      Flags=$00
      NoOrient=3
      FieldType=FixedPlane
      FieldTilt=20.0
      FieldAzim=90.0
      BaseSlope=0.0
      UsesTiltAver=False
      UsesAzimAver=False
      UsesSlopeAver=False
    End of TOrientGroup
  End of Orientations
  PVObject_PVMainArray=pvMainArray
    Flags=$01900400
    Flags2=$082A
    PVObject_SystemCircuit=pvCircuit
      Version=8.1.7
      Flags=$00
      InjectionPointNode Start;
        Children=Start
          InverterNode Start;
            NElements=1
            SubArrayId=1
            SubArrayName=South
            Children=Start
              MPPTNode Start;
                NElements=1
                SubArrayId=1
                SubArrayName=South
                Children=Start
                  StringNode Start;
                    NElements=1
                    SubArrayId=1
                    SubArrayName=South
                  StringNode End;
                Children=End
              MPPTNode End;
            Children=End
          InverterNode End;
          InverterNode Start;
            NElements=1
            SubArrayName=East; West
            Children=Start
              MPPTNode Start;
                NElements=1
                SubArrayId=2
                SubArrayName=East
                Children=Start
                  StringNode Start;
                    NElements=1
                    SubArrayId=2
                    SubArrayName=East
                  StringNode End;
                Children=End
              MPPTNode End;
              MPPTNode Start;
                NElements=1
                SubArrayId=3
                SubArrayName=West
                Children=Start
                  StringNode Start;
                    NElements=1
                    SubArrayId=3
                    SubArrayName=West
                  StringNode End;
                Children=End
              MPPTNode End;
            Children=End
          InverterNode End;
        Children=End
      InjectionPointNode End;
    End of PVObject pvCircuit
    SubArrays=Start
      PVObject_=pvSubArray
        Comment=South
        Version=8.1.7
        Flags=$019E0402
        Flags2=$20
        SubArrayId=1
        PVModule=Mod600.PAN
        GInverter=Inv50.OND
        NoOrientation=1
        NModSerie=10
        NStringCh=1
        NStrOrient1=1
        NInverter=1
        NInvMPPT=1
        PowerSharingInverterId=-1
      End of PVObject pvSubArray
      PVObject_=pvSubArray
        Comment=East
        Version=8.1.7
        Flags=$019E0502
        Flags2=$20
        SubArrayId=2
        PVModule=Mod600.PAN
        GInverter=Inv50.OND
        NoOrientation=2
        NModSerie=10
        NStringCh=1
        NStrOrient1=1
        NInverter=1
        NInvMPPT=1
        PowerSharingInverterId=1
      End of PVObject pvSubArray
      PVObject_=pvSubArray
        Comment=West
        Version=8.1.7
        Flags=$019E0502
        Flags2=$20
        SubArrayId=3
        PVModule=Mod600.PAN
        GInverter=Inv50.OND
        NoOrientation=3
        NModSerie=10
        NStringCh=1
        NStrOrient1=1
        NInverter=1
        NInvMPPT=1
        PowerSharingInverterId=1
      End of PVObject pvSubArray
    EndTags
  End of PVObject pvMainArray
  PVObject_System=pvSystem
    Flags=$00
    SystemType=Grid
  End of PVObject pvSystem
End of PVObject pvVCalcul
"""
        (ws / "Projects/Demo.VC1").write_bytes(three_roof_vc0.replace("\n", "\r\n").encode("utf-8"))

        def sloped(name, origin, wx, sy, tilt, azim):
            t = math.radians(tilt)
            pts = [(0.0, 0.0, 0.0), (wx, 0.0, 0.0), (wx, sy * math.cos(t), sy * math.sin(t)), (0.0, sy * math.cos(t), sy * math.sin(t))]
            verts = [[round(origin[k] + _rotate_z(p, -azim)[k], 6) for k in range(3)] for p in pts]
            return {"name": name, "vertices": verts, "triangles": [[0, 1, 2], [0, 2, 3]]}

        house = {"kind": "mesh", "meshes": [
            sloped("SouthRoof", [0.0, 0.0, 4.0], 14.0, 8.0, 25.0, 0.0),
            sloped("EastRoof", [25.0, 0.0, 4.0], 14.0, 8.0, 20.0, -90.0),
            sloped("WestRoof", [-25.0, 0.0, 4.0], 14.0, 8.0, 20.0, 90.0),
        ]}
        layout = {"tables": [
            {"name": "SouthTbl", "orientation_id": 1, "roof_mesh": "SouthRoof",
             "module_rows": 2, "module_columns": 5, "orientation": "portrait",
             "row_spacing_m": 0.03, "column_spacing_m": 0.04,
             "origin_m": [1.0, 0.5, 4.0 + 0.5 * math.tan(math.radians(25.0)) + 0.2]},
            {"name": "EastTbl", "orientation_id": 2, "roof_mesh": "EastRoof",
             "module_rows": 2, "module_columns": 5, "orientation": "portrait",
             "row_spacing_m": 0.03, "column_spacing_m": 0.04,
             "origin_m": [25.0 - 0.5 * math.cos(math.radians(20.0)), 1.0, 4.0 + 0.5 * math.sin(math.radians(20.0)) + 0.2]},
            {"name": "WestTbl", "orientation_id": 3, "roof_mesh": "WestRoof",
             "module_rows": 2, "module_columns": 5, "orientation": "portrait",
             "row_spacing_m": 0.03, "column_spacing_m": 0.04,
             "origin_m": [-25.0 + 0.5 * math.cos(math.radians(20.0)), -1.0, 4.0 + 0.5 * math.sin(math.radians(20.0)) + 0.2]},
        ]}
        sc = create_house_pv_scene(
            store, comps, project="Demo.PRJ", variant="VC1", output_name="ThreeRoofPortrait.pvc2",
            house=house, layout=layout, module_geometry=MG, dry_run=False,
        )
        self.assertEqual(sc["status"], "created", sc)

        # Verify BOTH implicit 3-roof module_layout AND linear succeed on unshaded PowerSharing source
        for mode in ("linear", "module_layout"):
            imp = import_house_pv_scene_native(
                store, comps, "ThreeRoofPortrait.pvc2", f"3R {mode}",
                dry_run=False, confirm=True, shading_mode=mode,
            )
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
        sl = {"tables": [
            {"name": "SouthTbl", "subarray_id": 1, "cells": [{"row": r, "col": c, "global_string": 1} for r in (1, 2) for c in range(1, 6)]},
            {"name": "EastTbl", "subarray_id": 2, "cells": [{"row": r, "col": c, "global_string": 2} for r in (1, 2) for c in range(1, 6)]},
            {"name": "WestTbl", "subarray_id": 3, "cells": [{"row": r, "col": c, "global_string": 3} for r in (1, 2) for c in range(1, 6)]},
        ]}
        sc_exp = create_house_pv_scene(
            store, comps, project="Demo.PRJ", variant="VC1", output_name="ThreeRoofExplicit.pvc2",
            house=house, layout=layout, string_layout=sl, module_geometry=MG, dry_run=False,
        )
        self.assertEqual(sc_exp["status"], "created", sc_exp)
        imp_exp = import_house_pv_scene_native(
            store, comps, "ThreeRoofExplicit.pvc2", "3R explicit",
            dry_run=False, confirm=True, shading_mode="module_layout",
        )
        self.assertEqual(imp_exp["status"], "native_variant_created", imp_exp)
        self.assertTrue(imp_exp["stored_module_layout_verified"])

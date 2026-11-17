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

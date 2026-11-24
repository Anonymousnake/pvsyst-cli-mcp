"""Unit tests for 3D terrain DEM triangulation, CSV/lonlat/GeoTIFF loaders, table draping, and pvShdGround."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.api.schemas import SingleDesignRequest
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.synthesize import synthesize_utility_project
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.ground import plan_ground_polygon_layout
from pvsyst_auto.scene.terrain import (
    build_terrain_mesh,
    emit_pvshd_ground,
)
from pvsyst_auto.workflows.utility import validate_utility_spec
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


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
        csv_path.write_text(
            "lon,lat,elev\n"
            "120.000,31.000,10.0\n"
            "120.001,31.000,15.0\n"
            "120.001,31.001,18.0\n"
            "120.000,31.001,12.0\n",
            encoding="utf-8",
        )
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
            with self.assertRaisesRegex(ValueError, r"pvsyst-cli-mcp\[geo\]"):
                build_terrain_mesh({"geotiff_name": "site.tif"}, workspace=self.root)

    def test_terrain_shed_rows_follow_azimuth(self) -> None:
        import re
        from pvsyst_auto.api.projects import pvsyst_prepare_design_project
        from pvsyst_auto.api.schemas import SingleDesignRequest
        from tests.unit.core.test_bifacial_utility import _capacity_spec, _FakeUtilityCLI

        request = SingleDesignRequest(
            scope='ground_sheds', new_project='Terrain.PRJ', year=2024, target_dc_kw=100,
            tilt=22, azimuth=90, module={'file_name': 'Lab440.PAN'}, inverter={'file_name': 'Lab50.OND'},
            weather={'mode': 'existing', 'sit_name': 'Geneva_MN90.SIT', 'met_name': 'Geneva_MN90_SYN.MET'},
            capacity=_capacity_spec(), ground_layout={'model_mode': '3d', 'pitch_m': 11, 'shed_count': 6,
                'module_rows': 2, 'module_columns': 19, 'terrain': {'points': [
                    [-100, -100, 0], [100, -100, 0], [100, 100, 0], [-100, 100, 0]]}})
        with patch('pvsyst_auto.api.projects.cli', return_value=_FakeUtilityCLI(self.root)), \
             patch('pvsyst_auto.api.projects.variant_store', return_value=self.store), \
             patch('pvsyst_auto.workflows.utility.inspect_met_calendar', return_value={
                 'issues': [], 'warnings': [], 'year': 2024, 'rows': 8784}):
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
        shed_res = synthesize_utility_project(
            self.store,
            project_comment="TerrainShed",
            variant_comment="ShedDEM",
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            field_type="RegularSheds",
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            n_mod_serie=20,
            n_string_ch=10,
            n_inverter=2,
            n_inv_mppt=1,
            shed_count=4,
            module_rows=2,
            module_columns=25,
            pitch_m=10.0,
            tilt_deg=25.0,
            terrain={"points": pts, "name": "HillDEM", "follow_slope": True},
        )
        vc_shed = shed_res["vc0_text"]
        self.assertIn("=pvShdArrayTable", vc_shed)
        self.assertIn("=pvShdGround", vc_shed)
        self.assertIn("terrain", shed_res["utility_summary"])
        self.assertEqual(shed_res["utility_summary"]["terrain"]["vertex_count"], 25)

        # 2. Single-axis trackers draped on sloped DEM
        trk_res = synthesize_utility_project(
            self.store,
            project_comment="TerrainTrk",
            variant_comment="TrkDEM",
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            field_type="TrackHorizNSAxis",
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            n_mod_serie=20,
            n_string_ch=10,
            n_inverter=2,
            n_inv_mppt=1,
            shed_count=4,
            module_rows=2,
            module_columns=25,
            pitch_m=10.0,
            terrain={"points": pts, "name": "HillDEM", "follow_slope": True},
        )
        vc_trk = trk_res["vc0_text"]
        self.assertIn("PVObject_4=pvShdArrayTracking", vc_trk)
        self.assertIn("PVObject_5=pvShdGround", vc_trk)
        # Non-zero axis tilt computed from 5% N-S slope (~2.86 deg)
        tilts = [t["axis_tilt_deg"] for t in trk_res["utility_summary"]["terrain"]["trackers"]]
        self.assertTrue(any(abs(t) > 1.5 for t in tilts))

    def test_polygon_clipper_rectangular_l_shape_and_narrow_vertical(self) -> None:
        # 50m x 40m rectangle with 2m setback -> 46m x 36m buildable
        rect = [[0.0, 0.0], [50.0, 0.0], [50.0, 40.0], [0.0, 40.0]]
        plan = plan_ground_polygon_layout(
            rect,
            tilt_deg=25.0,
            azimuth_deg=0.0,
            pitch_m=8.0,
            module_width_m=1.134,
            module_length_m=2.278,
            module_rows=2,
            module_orientation="portrait",
            setback_m=2.0,
            max_table_columns=16,
            base_slope_deg=5.0,
        )
        self.assertEqual(plan["site_area_m2"], 2000.0)
        self.assertGreaterEqual(plan["row_count"], 4)
        self.assertGreater(plan["table_count"], 4)
        self.assertGreater(plan["total_modules"], 100)
        self.assertEqual(plan["base_slope_deg"], 5.0)
        self.assertTrue(all(t["module_columns"] <= 16 for t in plan["tables"]))

        # Concave L-shaped polygon (top-right quadrant cut out)
        l_poly = [[0.0, 0.0], [60.0, 0.0], [60.0, 20.0], [25.0, 20.0], [25.0, 50.0], [0.0, 50.0]]
        l_plan = plan_ground_polygon_layout(
            l_poly,
            tilt_deg=20.0,
            azimuth_deg=15.0,
            pitch_m=9.0,
            module_width_m=1.052,
            module_length_m=2.131,
            module_rows=2,
            setback_m=1.0,
        )
        self.assertAlmostEqual(l_plan["site_area_m2"], 60 * 20 + 25 * 30, places=1)
        self.assertGreater(l_plan["table_count"], 2)

        for vertices in (
            [[0, 0], [10, 0], [10, 2], [0, 2]],
            [[0, 2], [10, 2], [10, 0], [0, 0]],
        ):
            with self.subTest(vertices=vertices):
                narrow = plan_ground_polygon_layout(
                    vertices, tilt_deg=90, pitch_m=3, module_width_m=1,
                    module_length_m=1, module_rows=1, spacing_x_m=0, spacing_y_m=0,
                )
                self.assertEqual(narrow["total_modules"], 10)
                self.assertEqual(narrow["row_count"], 1)

    def test_utility_synthesis_with_site_polygon_obstacles_and_nonzero_base_slope(self) -> None:
        (self.root / "Sites" / "TemplateSite.SIT").write_text(
            "PVObject_=pvSite\n  NomF=TemplateSite.SIT\n  Site=TemplateSite\n"
            "  Latitude=46.24\n  Longitude=6.11\nEnd of PVObject pvSite\n",
            encoding="utf-8",
        )
        (self.root / "Meteo" / "TemplateMeteo.MET").write_bytes(b"fixture")
        poly = [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]]
        obstacles = [
            {"kind": "building", "name": "Substation", "origin_m": [42.0, 5.0, 0.0], "length_m": 6.0, "width_m": 4.0, "height_m": 3.5},
            {"kind": "tree", "name": "SouthOak", "origin_m": [20.0, -6.0, 0.0],
             "top_height_m": 8.0, "middle_height_m": 6.0, "bottom_height_m": 3.0,
             "trunk_height_m": 2.5, "crown_diameter_m": 4.0, "trunk_diameter_m": 0.4},
        ]
        synth = synthesize_utility_project(
            self.store,
            project_comment="Ground3DPoly",
            variant_comment="Finite 3D + Slope",
            site_name="TemplateSite.SIT",
            met_name="TemplateMeteo.MET",
            field_type="RegularSheds",
            pan_name="Generic_Mono_300W.PAN",
            ond_name="Generic_7.5kW.OND",
            n_mod_serie=14,
            n_string_ch=6,
            n_inverter=2,
            n_inv_mppt=6,
            independent_mppt=True,
            pnom_ac_in_mppt=2.5,
            tilt_deg=25.0,
            azimuth_deg=0.0,
            pitch_m=8.0,
            module_rows=2,
            site_polygon=poly,
            setback_m=1.0,
            obstacles=obstacles,
            base_slope_deg=5.0,
        )
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
        req = SingleDesignRequest(
            scope="ground_sheds",
            new_project="PolyGround.PRJ",
            year=2023,
            target_dc_kw=50.0,
            tilt=25.0,
            azimuth=0.0,
            sizing_mode="auto_from_components",
            module={"file_name": "Lab.PAN"},
            inverter={"file_name": "Lab.OND"},
            weather={"mode": "existing", "sit_name": "S.SIT", "met_name": "M.MET"},
            ground_layout={
                "model_mode": "3d",
                "pitch_m": 8.0,
                "site_polygon": [[0, 0], [40, 0], [40, 30], [0, 30]],
                "setback_m": 1.0,
                "base_slope_deg": 4.0,
                "obstacles": [{"kind": "wall", "name": "Fence", "start_m": [0, -2, 0], "end_m": [10, -2, 0], "height_m": 2.5, "thickness_m": 0.3}],
            },
        )
        issues: list[dict] = []
        validate_utility_spec(req.model_dump(exclude_none=True), "ground_sheds", issues)
        self.assertEqual(issues, [])

        trk_issues: list[dict] = []
        validate_utility_spec({
            "scope": "ground_tracker",
            "ground_layout": {
                "site_polygon": [[0, 0], [40, 0], [40, 30], [0, 30]],
                "base_slope_deg": 3.0,
            },
        }, "ground_tracker", trk_issues)
        fields = {i["field"] for i in trk_issues}
        self.assertIn("ground_layout.site_polygon", fields)
        self.assertIn("ground_layout.base_slope_deg", fields)


if __name__ == "__main__":
    unittest.main()

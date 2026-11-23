"""Unit tests for 3D terrain DEM triangulation, CSV/lonlat/GeoTIFF loaders, table draping, and pvShdGround."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.synthesize import synthesize_utility_project
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.terrain import (
    build_terrain_mesh,
    drape_shed_tables_on_terrain,
    emit_pvshd_ground,
    project_lonlat_to_enu,
)
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


if __name__ == "__main__":
    unittest.main()

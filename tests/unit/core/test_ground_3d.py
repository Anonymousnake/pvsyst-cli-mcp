"""Unit tests for finite 3D ground polygon layout, obstacles, and verified BaseSlope."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from pvsyst_auto.api.schemas import SingleDesignRequest
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.synthesize import synthesize_utility_project
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.ground import build_ground_3d_shading, plan_ground_polygon_layout
from pvsyst_auto.workflows.utility import validate_utility_spec


class Ground3DLayoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for sub in ("Projects", "Sites", "Meteo", "ComposPV/PVmodules", "ComposPV/Inverters"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.components = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.components)
        (self.root / "Sites" / "TemplateSite.SIT").write_text(
            "PVObject_=pvSite\n  NomF=TemplateSite.SIT\n  Site=TemplateSite\n"
            "  Latitude=46.24\n  Longitude=6.11\nEnd of PVObject pvSite\n",
            encoding="utf-8",
        )
        (self.root / "Meteo" / "TemplateMeteo.MET").write_bytes(b"fixture")

    def test_polygon_clipper_rectangular_l_shape_and_rotated_with_setback(self):
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

    def test_utility_synthesis_with_site_polygon_obstacles_and_nonzero_base_slope(self):
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

    def test_utility_spec_and_schema_validation_for_ground_3d_fields(self):
        # Valid ground_sheds with site_polygon + obstacles + base_slope_deg parses cleanly through Pydantic
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

        # Reject site_polygon on ground_tracker
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

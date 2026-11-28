"""Regression tests for GitHub Issue #309."""
from __future__ import annotations

import math
from pathlib import Path
import tempfile
import unittest

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.cad_import import COLLADA_141_NS, parse_pvc_141
from pvsyst_auto.scene.dome import build_dome_3d_shading, dome_footprint_depth_m
from pvsyst_auto.scene.dxf_import import parse_ascii_dxf
from pvsyst_auto.scene.exclusions import point_in_polygon, rect_overlaps_polygon
from pvsyst_auto.scene.reader import plan_ground_polygon_layout
from pvsyst_auto.workflows.design import prepare_design_project
from pvsyst_auto.workflows.report_layout import _table_corners_3d, resolve_report_layout
from tests._fixtures import init_full_workspace
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN, _FakeUtilityCLI
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


def _make_sloped_pvc(
    *,
    long_s: float = 20.0,
    larg_s: float = 4.0,
    tilt_deg: float = 20.0,
    azimuth_deg: float = 0.0,
    base_slope_deg: float = 6.0,
    cols: int = 2,
    rows: int = 1,
) -> str:
    """Build COLLADA 1.4.1 fixed tables transformed using the exact PVsyst BaseSlope + Tilt + Azimuth basis."""
    geoms, insts = [], []
    pitch = 10.0
    for r in range(rows):
        for c in range(cols):
            gid = f"Sloped_{r}_{c}"
            ox = c * (long_s + 2.0) + 0.5 * long_s
            oy = r * pitch + 5.0
            oz = 0.8
            corners = _table_corners_3d({
                "kind": "fixed",
                "origin_m": [ox, oy, oz],
                "long_shed_m": long_s,
                "larg_shed_m": larg_s,
                "tilt_deg": tilt_deg,
                "azimuth_deg": azimuth_deg,
                "base_slope_deg": base_slope_deg,
                "type_origine": 2,
            })
            bot = [(x, y, z - 0.05) for x, y, z in corners]
            flt = " ".join(f"{v:.6f}" for p in (*corners, *bot) for v in p)
            geoms.append(f"""
    <geometry id="{gid}">
      <mesh>
        <source id="{gid}-pos"><float_array id="{gid}-arr" count="24">{flt}</float_array></source>
        <vertices id="{gid}-v"><input semantic="POSITION" source="#{gid}-pos" /></vertices>
        <triangles material="Material0" count="2"><input semantic="VERTEX" source="#{gid}-v" offset="0" /><p>0 1 2 0 2 3</p></triangles>
        <frame_parameters>
          <module_width>1000</module_width><module_height>1980</module_height>
          <module_x_spacing>20</module_x_spacing><module_y_spacing>20</module_y_spacing>
        </frame_parameters>
      </mesh>
    </geometry>""")
            insts.append(f'<instance_geometry url="#{gid}" />')
    return f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="{COLLADA_141_NS}" version="1.4.1">
  <asset><unit meter="1.0" name="meter" /></asset>
  <library_geometries>{"".join(geoms)}</library_geometries>
  <library_visual_scenes><visual_scene id="Scene">
    <node id="PVcase">{"".join(insts)}</node>
  </visual_scene></library_visual_scenes>
</COLLADA>"""


class Issue309RegressionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = init_full_workspace(Path(self._tmp.name))
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV" / "PVmodules" / "Bifi440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        (self.ws / "ComposPV" / "Inverters" / "Inv50.OND").write_text(UTILITY_OND, encoding="utf-8")
        self.components = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.components)
        self.cli = _FakeUtilityCLI(self.ws, year=2024, nominal_dc_kw=52.8, use_sfi_writer=False)

    def test_type_origine2_footprints_stay_inside_site_and_clear_exclusions(self) -> None:
        site = [[0.0, 0.0], [30.0, 0.0], [30.0, 25.0], [0.0, 25.0]]
        excl = [[[0.0, 6.5], [30.0, 6.5], [30.0, 9.9], [0.0, 9.9]]]

        shed_plan = plan_ground_polygon_layout(
            site, tilt_deg=20.0, azimuth_deg=0.0, pitch_m=10.0,
            module_width_m=1.032, module_length_m=1.996, module_rows=2,
            setback_m=0.0, exclusion_zones=excl,
        )
        for tbl in shed_plan["tables"]:
            corners = _table_corners_3d({**tbl, "kind": "fixed", "type_origine": 2})
            for cx, cy, _ in corners:
                self.assertTrue(-1e-6 <= cx <= 30.0 + 1e-6 and -1e-6 <= cy <= 25.0 + 1e-6, corners)
            self.assertFalse(rect_overlaps_polygon(
                min(c[0] for c in corners), max(c[0] for c in corners),
                min(c[1] for c in corners), max(c[1] for c in corners),
                [(p[0], p[1]) for p in excl[0]], tol=1e-4,
            ))

        _, dome_plan = build_dome_3d_shading(
            site_polygon=site, terrain_mesh=None, terrain_opts=None,
            shed_count=2, pitch_m=10.0, tilt_deg=20.0, azimuth_deg=0.0,
            base_slope_deg=0.0, top_spacing_m=0.2, module_rows=2, module_columns=10,
            module_width_m=1.032, module_length_m=1.996, module_name="Bifi",
            module_orient="moPortrait", spacing_x_m=0.02, spacing_y_m=0.02,
            setback_m=0.0, ground_clearance_m=0.5, max_table_columns=None,
            exclusion_zones=excl,
        )
        dome_depth = dome_footprint_depth_m(dome_plan["tables"][0]["larg_shed_m"], 20.0, 0.2)
        for tbl in dome_plan["tables"]:
            ox, oy, _ = tbl["origin_m"]
            half_u = 0.5 * tbl["long_shed_m"]
            self.assertGreaterEqual(oy - dome_depth, -1e-6)
            self.assertLessEqual(oy, 25.0 + 1e-6)
            self.assertFalse(rect_overlaps_polygon(ox - half_u, ox + half_u, oy - dome_depth, oy, [(p[0], p[1]) for p in excl[0]], tol=1e-4))

    def test_sloped_pvc_recovers_axes_without_double_counting_base_slope(self) -> None:
        (self.ws / "UserData" / "Sloped20.pvc").write_text(
            _make_sloped_pvc(long_s=20.0, larg_s=4.0, tilt_deg=20.0, azimuth_deg=0.0, base_slope_deg=6.0, cols=4),
            encoding="utf-8",
        )
        parsed = parse_pvc_141(self.ws, "Sloped20.pvc")
        tbl = parsed["tables"][0]
        self.assertAlmostEqual(tbl["long_shed_m"], 20.0, places=3)
        self.assertAlmostEqual(tbl["larg_shed_m"], 4.0, places=3)
        self.assertAlmostEqual(tbl["tilt_deg"], 20.0, places=2)
        self.assertAlmostEqual(tbl["azimuth_deg"], 0.0, places=2)
        self.assertAlmostEqual(tbl["base_slope_deg"], 6.0, places=2)
        self.assertAlmostEqual(parsed["dominant_azimuth_deg"], 0.0, places=2)

        # Flat tilt=0 on sloped ground should not swap long_shed and larg_shed
        (self.ws / "UserData" / "SlopedFlat.pvc").write_text(
            _make_sloped_pvc(long_s=20.0, larg_s=4.0, tilt_deg=0.0, azimuth_deg=0.0, base_slope_deg=6.0),
            encoding="utf-8",
        )
        flat = parse_pvc_141(self.ws, "SlopedFlat.pvc")["tables"][0]
        self.assertAlmostEqual(flat["long_shed_m"], 20.0, places=3)
        self.assertAlmostEqual(flat["larg_shed_m"], 4.0, places=3)
        self.assertAlmostEqual(flat["tilt_deg"], 0.0, places=2)

        # Rotated + sloped case passes prepare_design_project tilt/azimuth consistency check
        (self.ws / "UserData" / "SlopedRot.pvc").write_text(
            _make_sloped_pvc(long_s=20.0, larg_s=4.0, tilt_deg=25.0, azimuth_deg=35.0, base_slope_deg=10.0),
            encoding="utf-8",
        )
        rot = parse_pvc_141(self.ws, "SlopedRot.pvc")
        self.assertAlmostEqual(rot["dominant_tilt_deg"], 25.0, places=2)
        self.assertAlmostEqual(rot["dominant_azimuth_deg"], 35.0, places=2)
        self.assertAlmostEqual(rot["tables"][0]["base_slope_deg"], 10.0, places=2)

        prep = prepare_design_project(
            self.cli, self.store, dry_run=True,
            spec={
                "scope": "ground_sheds",
                "new_project": "SlopedPvc.PRJ",
                "year": 2024,
                "sizing_mode": "auto_from_components",
                "auto_sizing": {"capacity_tolerance_pct": 12.0},
                "target_dc_kw": 52.8,
                "tilt": 20.0,
                "azimuth": 0.0,
                "module": {"file_name": "Bifi440.PAN"},
                "inverter": {"file_name": "Inv50.OND"},
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                "ground_layout": {"pvc_name": "Sloped20.pvc"},
            },
        )
        self.assertEqual(prep["status"], "ready", prep)

    def test_type_origine1_house_rooftop_aligns_over_roof_mesh(self) -> None:
        # Roof spanning X in [0, 10], Y in [0, 5]; TypeOrigine=1 origin at top-right (10, 5)
        corners = _table_corners_3d({
            "kind": "fixed",
            "origin_m": [10.0, 5.0, 3.0],
            "long_shed_m": 8.0,
            "larg_shed_m": 3.0,
            "tilt_deg": 20.0,
            "azimuth_deg": 0.0,
            "type_origine": 1,
        })
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        self.assertGreaterEqual(min(xs), 0.0)
        self.assertLessEqual(max(xs), 10.0)
        self.assertGreaterEqual(min(ys), 0.0)
        self.assertLessEqual(max(ys), 5.0)
        self.assertAlmostEqual(min(xs), 2.0, places=6)
        self.assertAlmostEqual(max(xs), 10.0, places=6)

    def test_dxf_nearly_closed_rounding_and_tiny_z_obstacle(self) -> None:
        # Open curves whose closing gap is 5e-5 (< 1e-4) and collapses after 4-decimal rounding,
        # plus an obstacle closed curve at Z=0.004 mm (4e-6 m <= 1e-3) under $INSUNITS=4
        dxf = "\n".join([
            "0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "4", "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "0",
            "10", "0", "20", "0", "10", "100000", "20", "0",
            "10", "100000", "20", "100000", "10", "0", "20", "100000", "10", "0.0", "20", "0.05",
            "0", "LWPOLYLINE", "8", "KEEPOUT", "70", "0",
            "10", "10000", "20", "10000", "10", "20000", "20", "10000",
            "10", "20000", "20", "20000", "10", "10000", "20", "20000", "10", "10000.0", "20", "10000.05",
            "0", "LWPOLYLINE", "8", "OBSTACLES", "70", "1", "38", "0.004",
            "10", "-10000", "20", "10000", "10", "-5000", "20", "10000",
            "10", "-5000", "20", "15000", "10", "-10000", "20", "15000",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "EdgeMm.dxf").write_text(dxf, encoding="utf-8")
        parsed = parse_ascii_dxf(self.ws, "EdgeMm.dxf")
        self.assertEqual(len(parsed["site_polygon"]), 4)
        self.assertEqual(len(parsed["exclusion_zones"]), 1)
        self.assertEqual(len(parsed["exclusion_zones"][0]), 4)
        self.assertEqual(len(parsed["obstacles"]), 1)
        zs = [v[2] for v in parsed["obstacles"][0]["vertices"]]
        self.assertAlmostEqual(min(zs), 0.0, places=4)
        self.assertAlmostEqual(max(zs), 4.0, places=4)

    def test_resolve_report_layout_preserves_vcx_and_validates_strictly(self) -> None:
        vc_txt = "\n".join([
            "PVObject_=pvVCalcul",
            "  Comment=With3D",
            "  PVObject_Ombrage=pvShading",
            "    Flags=$000A0100",
            "    ListeObjets, list of=2",
            "      PVObject_1=pvShdGround",
            "        Comment=SiteDEM",
            "        DOrigine=0.000,0.000,0.000",
            "        NPts=3",
            "        Point_1=0.000,0.000,1.000",
            "        Point_2=10.000,0.000,1.000",
            "        Point_3=10.000,10.000,2.000",
            "        NFaces=1",
            "        Faces=0,1,2,-1",
            "      End of PVObject pvShdGround",
            "      PVObject_2=pvShdElemObject",
            "        Comment=OakTree",
            "        DOrigine=20.000,20.000,0.000",
            "        ObjetElemType=oeTree",
            "        Params=6.000,4.000,2.000,1.500,4.000,0.400,",
            "      End of PVObject pvShdElemObject",
            "    End of ListeObjets",
            "  End of PVObject pvShading",
            "End of PVObject pvVCalcul",
        ]) + "\n"
        (self.ws / "Projects" / "Overlay.PRJ").write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        (self.ws / "Projects" / "Overlay.VC0").write_text(vc_txt, encoding="utf-8")

        merged, _, _ = resolve_report_layout(
            primary_scenario={"label": "Base", "project": "Overlay.PRJ", "variant": "VC0"},
            all_scenarios=[{"label": "Base"}],
            top_layout_preview={"site_polygon": [[0, 0], [30, 0], [30, 30], [0, 30]], "exclusion_zones": [], "obstacles": [], "terrain_points": []},
            top_include_3d=False,
            store=self.store,
            workspace=self.ws,
        )
        self.assertIsNotNone(merged["terrain_mesh"])
        self.assertEqual(len(merged["obstacles"]), 1)
        self.assertEqual(len(merged["site_polygon"]), 4)

        for bad_lp in (
            {"obstacles": [{"kind": "tree", "name": "T1"}]},
            {"obstacles": [{"name": "Mesh2D", "vertices": [[0, 0], [1, 0], [1, 1]]}]},
            {"tables": [{"origin_m": ["bad"]}]},
        ):
            with self.subTest(bad_lp=bad_lp):
                with self.assertRaises(ValueError):
                    resolve_report_layout(
                        primary_scenario={"label": "Bad"},
                        all_scenarios=[{"label": "Bad"}],
                        top_layout_preview=bad_lp,
                        top_include_3d=False,
                        store=None,
                        workspace=self.ws,
                    )


if __name__ == "__main__":
    unittest.main()

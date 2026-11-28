"""Unit tests for PR 3: 2D exclusion zones, DXF BLOCKS/INSERT & curves, and 3-scope layout carving."""
from __future__ import annotations

import asyncio
import math
import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.api.context import create_mcp_server
from pvsyst_auto.api.schemas import GroundLayoutRequest, SingleDesignRequest
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.cad_import import resolve_cad_synthesis_inputs
from pvsyst_auto.scene.dome import build_dome_3d_shading, dome_footprint_depth_m
from pvsyst_auto.scene.dxf_import import MAX_DXF_ENTITIES, parse_ascii_dxf
from pvsyst_auto.scene.exclusions import (
    MAX_EXCLUSION_ZONES,
    point_in_polygon,
    polygon_x_shadow_in_band,
    rect_overlaps_polygon,
    subtract_exclusions_from_intervals,
    validate_exclusion_zones,
)
from pvsyst_auto.scene.reader import _rotate, plan_ground_polygon_layout, read_house_pv_scene
from pvsyst_auto.scene.tracker_layout import plan_tracker_polygon_layout
from pvsyst_auto.workflows.design import prepare_design_project
from pvsyst_auto.workflows.utility_validation import validate_utility_spec
from tests._fixtures import init_full_workspace
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN, _FakeUtilityCLI
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


def _design_spec(
    project_name: str,
    *,
    scope: str,
    target_kw: float,
    ground_layout: dict,
    tilt: float | None = 20.0,
    azim: float | None = 0.0,
) -> dict:
    spec: dict = {
        "scope": scope,
        "new_project": project_name,
        "year": 1990,
        "sizing_mode": "auto_from_components",
        "auto_sizing": {"capacity_tolerance_pct": 12.0},
        "target_dc_kw": target_kw,
        "module": {"file_name": "Bifi440.PAN"},
        "inverter": {"file_name": "Inv50.OND"},
        "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
        "ground_layout": ground_layout,
    }
    if tilt is not None:
        spec["tilt"] = tilt
    if azim is not None:
        spec["azimuth"] = azim
    return spec


class ExclusionsUnitTests(unittest.TestCase):
    def test_validate_exclusion_zones_and_rejections(self) -> None:
        self.assertEqual(validate_exclusion_zones(None), [])
        closed_dup = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]
        parsed = validate_exclusion_zones([closed_dup])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(parsed[0]), 4)

        bad_cases = [
            "not-a-list",
            [[[0, 0], [10, 0], [10, 10]]] * (MAX_EXCLUSION_ZONES + 1),
            [[[0, 0], [10, 0]]],
            [[[i, 0] for i in range(65)]],
            [[[0, 0], [10, 0, 1], [10, 10]]],
            [[[True, 0], [10, 0], [10, 10]]],
            [[[float("nan"), 0], [10, 0], [10, 10]]],
            [[[0, 0], [5, 0], [10, 0]]],  # collinear zero area
        ]
        for bad in bad_cases:
            with self.subTest(bad=str(bad)[:40]):
                with self.assertRaises(ValueError):
                    validate_exclusion_zones(bad)

    def test_point_in_polygon_and_rect_overlap(self) -> None:
        u_poly = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (20.0, 20.0), (20.0, 5.0), (10.0, 5.0), (10.0, 20.0), (0.0, 20.0)]
        self.assertTrue(point_in_polygon(5.0, 10.0, u_poly))
        self.assertTrue(point_in_polygon(0.0, 10.0, u_poly))  # boundary
        self.assertFalse(point_in_polygon(15.0, 10.0, u_poly))  # open interior bay

        # Exact touching edge allowed, overlap > tol detected, including UTM scale
        rect_poly = [(20.0, 0.0), (30.0, 0.0), (30.0, 10.0), (20.0, 10.0)]
        self.assertFalse(rect_overlaps_polygon(10.0, 20.0, 0.0, 10.0, rect_poly))
        self.assertTrue(rect_overlaps_polygon(10.0, 20.01, 0.0, 10.0, rect_poly))
        self.assertTrue(rect_overlaps_polygon(15.0, 35.0, 2.0, 8.0, [(22.0, 0.0), (28.0, 0.0), (28.0, 10.0), (22.0, 10.0)]))

        utm_poly = [(500020.0, 4500000.0), (500030.0, 4500000.0), (500030.0, 4500010.0), (500020.0, 4500010.0)]
        self.assertFalse(rect_overlaps_polygon(500010.0, 500020.0, 4500000.0, 4500010.0, utm_poly))
        self.assertTrue(rect_overlaps_polygon(500010.0, 500020.05, 4500000.0, 4500010.0, utm_poly))

        # Consistent unshrunk-box overlap semantics (touching=False, tiny interior overlap/corner poke=True)
        self.assertFalse(rect_overlaps_polygon(10.0, 20.0, 0.0, 10.0, rect_poly, tol=0.0))
        self.assertTrue(rect_overlaps_polygon(10.0, 20.001, 0.0, 10.0, rect_poly))
        self.assertFalse(rect_overlaps_polygon(10.0, 20.001, 0.0, 10.0, rect_poly, tol=0.05))
        self.assertTrue(rect_overlaps_polygon(10.0, 20.005, 0.0, 10.0, [(20.0, 4.0), (25.0, 4.0), (25.0, 6.0), (20.0, 6.0)]))
        # Concave U-poly: open bay allowed, overlapping arms rejected
        self.assertFalse(rect_overlaps_polygon(10.0, 20.0, 5.0, 15.0, u_poly))
        self.assertTrue(rect_overlaps_polygon(9.99, 20.0, 5.0, 15.0, u_poly))

    def test_interval_subtraction_trapezoid_slab_cases(self) -> None:
        # 1. Small pad completely inside band (y0=0, y1=10)
        pad = [(12.0, 3.0), (18.0, 3.0), (18.0, 7.0), (12.0, 7.0)]
        self.assertEqual(subtract_exclusions_from_intervals([(0.0, 40.0)], [pad], 0.0, 10.0), [(0.0, 12.0), (18.0, 40.0)])

        # 2. Diagonal road crossing band y0=0..10: swept shadow [15, 25]
        road = [(15.0, 0.0), (18.0, 0.0), (25.0, 10.0), (22.0, 10.0)]
        self.assertEqual(subtract_exclusions_from_intervals([(0.0, 40.0)], [road], 0.0, 10.0), [(0.0, 15.0), (25.0, 40.0)])

        # 3. Concave U-shape: open bay [10, 20] stays open when band is in y=[8, 18], blocked when band hits bottom y=[0, 10]
        u_poly = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (20.0, 20.0), (20.0, 5.0), (10.0, 5.0), (10.0, 20.0), (0.0, 20.0)]
        self.assertEqual(polygon_x_shadow_in_band(u_poly, 8.0, 18.0), [(0.0, 10.0), (20.0, 30.0)])
        self.assertEqual(polygon_x_shadow_in_band(u_poly, 0.0, 10.0), [(0.0, 30.0)])

        # 4. Overlapping exclusions + zero-height band (y0 == y1)
        ex1 = [(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0)]
        ex2 = [(15.0, 2.0), (25.0, 2.0), (25.0, 8.0), (15.0, 8.0)]
        self.assertEqual(subtract_exclusions_from_intervals([(0.0, 40.0)], [ex1, ex2], 5.0, 5.0), [(0.0, 10.0), (25.0, 40.0)])


class DxfBlocksAndCurvesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = init_full_workspace(Path(self._tmp.name))
        self.components = ComponentStore(self.ws)

    def test_blocks_insert_transforms_layer0_inheritance_and_guards(self) -> None:
        dxf = "\n".join([
            "0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "6", "0", "ENDSEC",
            "0", "SECTION", "2", "BLOCKS",
            # PAD_BLK with base (10, 10, 0), square [10..12] x [10..12] on layer "0"
            "0", "BLOCK", "8", "0", "2", "PAD_BLK", "10", "10.0", "20", "10.0", "30", "0.0",
            "0", "LWPOLYLINE", "8", "0", "70", "1",
            "10", "10.0", "20", "10.0", "10", "12.0", "20", "10.0",
            "10", "12.0", "20", "12.0", "10", "10.0", "20", "12.0",
            "0", "ENDBLK",
            # Deep nesting L8..L1 (8 levels allowed; >8 rejected in Deep9.dxf below)
            *[ln for idx in range(8, 0, -1) for ln in (
                ["0", "BLOCK", "2", f"L{idx}", "10", "0", "20", "0", "30", "0",
                 "0", "CIRCLE", "8", "DEEP_EXCL", "10", f"{10 + idx * 5}", "20", "40", "40", "1.0"]
                + (["0", "INSERT", "8", "0", "2", f"L{idx + 1}", "10", "0", "20", "0", "30", "0"] if idx < 8 else [])
                + ["0", "ENDBLK"]
            )],
            # Cyclic A -> B -> A
            "0", "BLOCK", "2", "CYC_A", "10", "0", "20", "0", "30", "0",
            "0", "CIRCLE", "8", "VOID_CYC", "10", "5", "20", "5", "40", "1.5",
            "0", "INSERT", "8", "0", "2", "CYC_B", "10", "0", "20", "0", "30", "0",
            "0", "ENDBLK",
            "0", "BLOCK", "2", "CYC_B", "10", "0", "20", "0", "30", "0",
            "0", "INSERT", "8", "0", "2", "CYC_A", "10", "0", "20", "0", "30", "0",
            "0", "ENDBLK",
            "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "100", "20", "0", "10", "100", "20", "80", "10", "0", "20", "80",
            # INSERT PAD_BLK at (30, 20), scale=(2, 1), rot=90 -> covers [28..30] x [20..24], layer "0" -> "XFMR_PAD"
            "0", "INSERT", "8", "XFMR_PAD", "2", "PAD_BLK", "10", "30.0", "20", "20.0", "30", "0.0",
            "41", "2.0", "42", "1.0", "43", "1.0", "50", "90.0",
            # Mirrored MINSERT (2x1) on ACCESS_ROAD
            "0", "INSERT", "8", "ACCESS_ROAD", "2", "PAD_BLK", "10", "70.0", "20", "20.0", "30", "0.0",
            "41", "-1.0", "42", "2.0", "43", "1.0", "50", "0.0", "70", "2", "71", "1", "44", "6.0", "45", "0.0",
            "0", "INSERT", "8", "0", "2", "L1", "10", "0", "20", "0", "30", "0",
            "0", "INSERT", "8", "0", "2", "CYC_A", "10", "0", "20", "0", "30", "0",
            "0", "INSERT", "8", "XFMR_PAD", "2", "MISSING_BLK", "10", "0", "20", "0", "30", "0",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "Blocks.dxf").write_text(dxf, encoding="utf-8")
        parsed = parse_ascii_dxf(self.ws, "Blocks.dxf")
        self.assertEqual(parsed["site_area_m2"], 8000.0)
        # 1 rotated pad + 2 mirrored minsert pads + 8 deep circles (L9 stopped at depth 8!) + 1 cyc_a circle = 12 exclusions
        self.assertEqual(len(parsed["exclusion_zones"]), 12, parsed["exclusion_zones"])
        # Rotated pad bounds: X in [28, 30], Y in [20, 24]
        self.assertTrue(any(
            abs(min(p[0] for p in z) - 28.0) < 1e-3 and abs(max(p[0] for p in z) - 30.0) < 1e-3
            and abs(min(p[1] for p in z) - 20.0) < 1e-3 and abs(max(p[1] for p in z) - 24.0) < 1e-3
            for z in parsed["exclusion_zones"]
        ))
        # L8 circle at x=50 MUST be present
        self.assertTrue(any(abs(sum(p[0] for p in z) / len(z) - 50.0) < 0.2 for z in parsed["exclusion_zones"]))

        # Exceeding MAX_DXF_INSERT_DEPTH (9 block levels B0..B8) fails loudly instead of silently dropping
        deep9 = "\n".join([
            "0", "SECTION", "2", "BLOCKS",
            *[ln for idx in range(9) for ln in (
                ["0", "BLOCK", "2", f"B{idx}", "10", "0", "20", "0", "30", "0"]
                + (["0", "INSERT", "8", "0", "2", f"B{idx + 1}", "10", "0", "20", "0", "30", "0"] if idx < 8 else [
                    "0", "LWPOLYLINE", "8", "0", "70", "1",
                    "10", "2", "20", "2", "10", "6", "20", "2", "10", "6", "20", "6", "10", "2", "20", "6",
                ])
                + ["0", "ENDBLK"]
            )],
            "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "50", "20", "0", "10", "50", "20", "50", "10", "0", "20", "50",
            "0", "INSERT", "8", "KEEPOUT", "2", "B0", "10", "0", "20", "0", "30", "0",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "Deep9.dxf").write_text(deep9, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "depth"):
            parse_ascii_dxf(self.ws, "Deep9.dxf")

        # INSERT with extrusion (210=0, 220=0, 230=-1) transforms OCS (-20, 20, 0) -> WCS [16..20] x [20..24]
        ocs_dxf = "\n".join([
            "0", "SECTION", "2", "BLOCKS",
            "0", "BLOCK", "2", "SQ", "10", "0", "20", "0", "30", "0",
            "0", "LWPOLYLINE", "8", "0", "70", "1",
            "10", "0", "20", "0", "10", "4", "20", "0", "10", "4", "20", "4", "10", "0", "20", "4",
            "0", "ENDBLK", "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "100", "20", "0", "10", "100", "20", "100", "10", "0", "20", "100",
            "0", "INSERT", "8", "KEEPOUT", "2", "SQ", "10", "-20", "20", "20", "30", "0",
            "210", "0", "220", "0", "230", "-1",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "OcsExtrude.dxf").write_text(ocs_dxf, encoding="utf-8")
        ocs_parsed = parse_ascii_dxf(self.ws, "OcsExtrude.dxf")
        self.assertEqual(len(ocs_parsed["exclusion_zones"]), 1)
        z_ocs = ocs_parsed["exclusion_zones"][0]
        self.assertAlmostEqual(min(p[0] for p in z_ocs), 16.0, places=3)
        self.assertAlmostEqual(max(p[0] for p in z_ocs), 20.0, places=3)
        self.assertAlmostEqual(min(p[1] for p in z_ocs), 20.0, places=3)
        self.assertAlmostEqual(max(p[1] for p in z_ocs), 24.0, places=3)

        # Entity-level OCS extrusion (210=0, 220=0, 230=-1) on CIRCLE, LWPOLYLINE, and ARC transforms to WCS and carves layout
        ent_ocs_dxf = "\n".join([
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "100", "20", "0", "10", "100", "20", "100", "10", "0", "20", "100",
            "0", "CIRCLE", "8", "KEEPOUT", "10", "-20", "20", "20", "30", "0", "40", "4",
            "210", "0", "220", "0", "230", "-1",
            "0", "LWPOLYLINE", "8", "KEEPOUT", "70", "1",
            "210", "0", "220", "0", "230", "-1",
            "10", "-40", "20", "20", "10", "-36", "20", "20", "10", "-36", "20", "24", "10", "-40", "20", "24",
            "0", "ARC", "8", "NO_PV", "10", "-60", "20", "20", "30", "0", "40", "3", "50", "0", "51", "180",
            "210", "0", "220", "0", "230", "-1",
            "0", "LINE", "8", "NO_PV", "10", "57", "20", "20", "30", "0", "11", "63", "21", "20", "31", "0",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "EntOcs.dxf").write_text(ent_ocs_dxf, encoding="utf-8")
        ent_ocs = parse_ascii_dxf(self.ws, "EntOcs.dxf")
        self.assertEqual(len(ent_ocs["exclusion_zones"]), 3)
        ex_bounds = [(round(min(p[0] for p in z), 1), round(max(p[0] for p in z), 1),
                      round(min(p[1] for p in z), 1), round(max(p[1] for p in z), 1)) for z in ent_ocs["exclusion_zones"]]
        self.assertIn((16.0, 24.0, 16.0, 24.0), ex_bounds)
        self.assertIn((36.0, 40.0, 20.0, 24.0), ex_bounds)
        self.assertIn((57.0, 63.0, 20.0, 23.0), ex_bounds)
        ent_plan = plan_ground_polygon_layout(
            ent_ocs["site_polygon"], tilt_deg=0.0, azimuth_deg=0.0, pitch_m=5.0,
            module_width_m=1.0, module_length_m=2.0, module_rows=1, exclusion_zones=ent_ocs["exclusion_zones"],
        )
        for tbl in ent_plan["tables"]:
            ox, oy, _ = tbl["origin_m"]
            half_w = 0.5 * float(tbl["long_shed_m"])
            for ex in ent_ocs["exclusion_zones"]:
                self.assertFalse(rect_overlaps_polygon(
                    ox - half_w, ox + half_w, oy - ent_plan["proj_depth_m"], oy, [(p[0], p[1]) for p in ex], tol=1e-4,
                ))

        # Elevated 2D POLYLINE uses owning POLYLINE elevation (30=20) with tilted extrusion (210=0, 220=0.6, 230=0.8) even when vertices carry 30=0
        elev_poly_dxf = "\n".join([
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "100", "20", "0", "10", "100", "20", "100", "10", "0", "20", "100",
            "0", "POLYLINE", "8", "KEEPOUT", "66", "1", "70", "1", "30", "20.0",
            "210", "0.0", "220", "0.6", "230", "0.8",
            *[ln for vx, vy in ((-20.0, -20.0), (-24.0, -20.0), (-24.0, -24.0), (-20.0, -24.0))
              for ln in ("0", "VERTEX", "8", "KEEPOUT", "70", "0", "10", str(vx), "20", str(vy), "30", "0.0")],
            "0", "SEQEND",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "ElevPoly2d.dxf").write_text(elev_poly_dxf, encoding="utf-8")
        ep_parsed = parse_ascii_dxf(self.ws, "ElevPoly2d.dxf")
        self.assertEqual(len(ep_parsed["exclusion_zones"]), 1)
        ez = ep_parsed["exclusion_zones"][0]
        self.assertAlmostEqual(min(p[0] for p in ez), 20.0, places=3)
        self.assertAlmostEqual(max(p[0] for p in ez), 24.0, places=3)
        self.assertAlmostEqual(min(p[1] for p in ez), 28.0, places=3)
        self.assertAlmostEqual(max(p[1] for p in ez), 31.2, places=3)
        ep_plan = plan_ground_polygon_layout(
            ep_parsed["site_polygon"], tilt_deg=0.0, azimuth_deg=0.0, pitch_m=5.0,
            module_width_m=1.0, module_length_m=2.0, module_rows=1, exclusion_zones=ep_parsed["exclusion_zones"],
        )
        for tbl in ep_plan["tables"]:
            ox, oy, _ = tbl["origin_m"]
            half_w = 0.5 * float(tbl["long_shed_m"])
            self.assertFalse(rect_overlaps_polygon(
                ox - half_w, ox + half_w, oy - ep_plan["proj_depth_m"], oy, [(p[0], p[1]) for p in ez], tol=1e-4,
            ))

        # Expansion budget guard: huge MINSERT, empty-block 1e9 MINSERT, and nested array explosion fail fast cleanly
        cap_dxf = "\n".join([
            "0", "SECTION", "2", "BLOCKS",
            "0", "BLOCK", "2", "DOT", "10", "0", "20", "0", "30", "0",
            "0", "POINT", "8", "TOPO_DTM", "10", "0", "20", "0", "30", "1.0",
            "0", "ENDBLK", "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "50", "20", "0", "10", "50", "20", "50", "10", "0", "20", "50",
            "0", "INSERT", "8", "TOPO_DTM", "2", "DOT", "10", "0", "20", "0", "30", "0",
            "70", "300", "71", "300", "44", "0.1", "45", "0.1",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "Cap.dxf").write_text(cap_dxf, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exceeds maximum"):
            parse_ascii_dxf(self.ws, "Cap.dxf")

        empty_bomb = "\n".join([
            "0", "SECTION", "2", "BLOCKS",
            "0", "BLOCK", "2", "EMPTY", "10", "0", "20", "0", "30", "0",
            "0", "ENDBLK",
            "0", "BLOCK", "2", "INNER", "10", "0", "20", "0", "30", "0",
            "0", "INSERT", "8", "0", "2", "EMPTY", "10", "0", "20", "0", "70", "250", "71", "1",
            "0", "ENDBLK", "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "50", "20", "0", "10", "50", "20", "50", "10", "0", "20", "50",
            "0", "INSERT", "8", "0", "2", "EMPTY", "10", "0", "20", "0", "70", "1000000000", "71", "1",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "Bomb1.dxf").write_text(empty_bomb, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exceeds maximum"):
            parse_ascii_dxf(self.ws, "Bomb1.dxf")

        nested_dxf = "\n".join([
            "0", "SECTION", "2", "BLOCKS",
            "0", "BLOCK", "2", "EMPTY", "10", "0", "20", "0", "30", "0",
            "0", "ENDBLK",
            "0", "BLOCK", "2", "INNER", "10", "0", "20", "0", "30", "0",
            "0", "INSERT", "8", "0", "2", "EMPTY", "10", "0", "20", "0", "70", "250", "71", "1",
            "0", "ENDBLK", "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "50", "20", "0", "10", "50", "20", "50", "10", "0", "20", "50",
            "0", "INSERT", "8", "0", "2", "INNER", "10", "0", "20", "0", "70", "250", "71", "1",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "Bomb2.dxf").write_text(nested_dxf, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exceed"):
            parse_ascii_dxf(self.ws, "Bomb2.dxf")

    def test_bulge_circle_arc_line_stitch_spline_and_layer_roles(self) -> None:
        # Closed 2-vertex bulged circle as exclusion, bulged chamfered boundary, stitched ARC+LINE road,
        # control-point B-spline pond, fit-point spline buffer, and terrain ARC.
        dxf = "\n".join([
            "0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "6", "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES",
            # Boundary with rounded corner via bulge on vertex 1
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0",
            "10", "90", "20", "0", "42", "0.41421356",
            "10", "100", "20", "10",
            "10", "100", "20", "80",
            "10", "0", "20", "80",
            # 2-vertex closed circle via trailing bulge=1.0 on KEEPOUT_HOLE
            "0", "LWPOLYLINE", "8", "KEEPOUT_HOLE", "70", "1",
            "10", "16", "20", "20", "42", "1.0",
            "10", "24", "20", "20", "42", "1.0",
            # CIRCLE on RETENTION_POND (exclusion) and TREE_OBST (obstacle)
            "0", "CIRCLE", "8", "RETENTION_POND", "10", "50", "20", "20", "40", "4.0",
            "0", "CIRCLE", "8", "TREE_OBST", "10", "-10", "20", "20", "40", "2.0", "39", "6.0",
            # Open ARC + 3 LINEs on NO_PV (out-of-order + reversed line) -> stitched closed loop
            "0", "ARC", "8", "NO_PV", "10", "70", "20", "50", "30", "0", "40", "5.0", "50", "0.0", "51", "180.0",
            "0", "LINE", "8", "NO_PV", "10", "75", "20", "45", "30", "0", "11", "75", "21", "50", "31", "0",
            "0", "LINE", "8", "NO_PV", "10", "65", "20", "50", "30", "0", "11", "65", "21", "45", "31", "0",
            "0", "LINE", "8", "NO_PV", "10", "65", "20", "45", "30", "0", "11", "75", "21", "45", "31", "0",
            # Closed B-spline with control points + knots on ENV_BUFFER
            "0", "SPLINE", "8", "ENV_BUFFER", "70", "1", "71", "3",
            "40", "0", "40", "0", "40", "0", "40", "0", "40", "1", "40", "2", "40", "2", "40", "2", "40", "2",
            "10", "20", "20", "55", "30", "0", "10", "26", "20", "55", "30", "0",
            "10", "26", "20", "63", "30", "0", "10", "20", "20", "63", "30", "0",
            "10", "20", "20", "55", "30", "0",
            # Fit-point closed spline on VOID_ZONE
            "0", "SPLINE", "8", "VOID_ZONE", "70", "1", "71", "3",
            "11", "40", "21", "55", "11", "48", "21", "55", "11", "48", "21", "63", "11", "40", "21", "63",
            # Terrain ARC
            "0", "ARC", "8", "TOPO_DTM", "10", "50", "20", "40", "30", "2.5", "40", "20.0", "50", "0", "51", "90",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "Curves.dxf").write_text(dxf, encoding="utf-8")
        parsed = parse_ascii_dxf(self.ws, "Curves.dxf")
        self.assertGreater(len(parsed["site_polygon"]), 6)
        self.assertEqual(len(parsed["exclusion_zones"]), 5, parsed["exclusion_zones"])
        self.assertEqual(len(parsed["obstacles"]), 1)
        self.assertGreaterEqual(len(parsed["terrain_points"]), 4)

        preview = read_house_pv_scene(self.components, "Curves.dxf")
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["exclusion_zones"], parsed["exclusion_zones"])
        self.assertEqual(preview["geometry"]["exclusion_zone_count"], 5)

        # Explicit dxf_layers={"exclusions": "CUSTOM_KEEP"} ignores KEEPOUT_HOLE/NO_PV while keeping auto boundary
        dxf2 = "\n".join([
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "50", "20", "0", "10", "50", "20", "50", "10", "0", "20", "50",
            "0", "CIRCLE", "8", "KEEPOUT_HOLE", "10", "15", "20", "15", "40", "3.0",
            "0", "CIRCLE", "8", "CUSTOM_KEEP", "10", "35", "20", "35", "40", "4.0",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "RoleMap.dxf").write_text(dxf2, encoding="utf-8")
        mapped = parse_ascii_dxf(self.ws, "RoleMap.dxf", dxf_layers={"exclusions": "CUSTOM_KEEP"})
        self.assertEqual(len(mapped["exclusion_zones"]), 1)
        self.assertAlmostEqual(sum(p[0] for p in mapped["exclusion_zones"][0]) / len(mapped["exclusion_zones"][0]), 35.0, places=1)

    def test_ellipse_exclusions_count_cap_and_self_intersection_rejections(self) -> None:
        # 1. Closed ELLIPSE + INSERT-transformed ELLIPSE + half-ellipse stitched with LINE on KEEPOUT
        ell_dxf = "\n".join([
            "0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "6", "0", "ENDSEC",
            "0", "SECTION", "2", "BLOCKS",
            "0", "BLOCK", "2", "ELL_BLK", "10", "0", "20", "0", "30", "0",
            "0", "ELLIPSE", "8", "0", "10", "0", "20", "0", "30", "0",
            "11", "4", "21", "0", "31", "0", "40", "0.5", "41", "0", "42", f"{2.0 * math.pi:.12f}",
            "0", "ENDBLK", "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "100", "20", "0", "10", "100", "20", "100", "10", "0", "20", "100",
            "0", "ELLIPSE", "8", "KEEPOUT", "10", "50", "20", "50", "30", "0",
            "11", "10", "21", "0", "31", "0", "40", "0.5", "41", "0", "42", f"{2.0 * math.pi:.12f}",
            "0", "INSERT", "8", "KEEPOUT", "2", "ELL_BLK", "10", "20", "20", "20", "30", "0", "50", "90.0",
            "0", "ELLIPSE", "8", "NO_PV", "10", "25", "20", "75", "30", "0",
            "11", "6", "21", "0", "31", "0", "40", "0.5", "41", "0", "42", f"{math.pi:.12f}",
            "0", "LINE", "8", "NO_PV", "10", "19", "20", "75", "30", "0", "11", "31", "21", "75", "31", "0",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "Ellipses.dxf").write_text(ell_dxf, encoding="utf-8")
        ell_parsed = parse_ascii_dxf(self.ws, "Ellipses.dxf")
        self.assertEqual(ell_parsed["entity_counts"].get("ELLIPSE"), 3)
        self.assertEqual(len(ell_parsed["exclusion_zones"]), 3)
        self.assertGreater(ell_parsed["exclusion_zones"][0] and sum(
            rect_overlaps_polygon(49, 51, 49, 51, [(p[0], p[1]) for p in z]) for z in ell_parsed["exclusion_zones"]
        ), 0)
        ell_plan = plan_ground_polygon_layout(
            ell_parsed["site_polygon"], tilt_deg=20.0, azimuth_deg=0.0, pitch_m=8.0,
            module_width_m=1.0, module_length_m=2.0, module_rows=2, module_orientation="portrait",
            exclusion_zones=ell_parsed["exclusion_zones"],
        )
        for z in ell_parsed["exclusion_zones"]:
            pts = [(p[0], p[1]) for p in z]
            for tbl in ell_plan["tables"]:
                cx, cy = tbl["origin_m"][0], tbl["origin_m"][1] - 0.5 * ell_plan["proj_depth_m"]
                self.assertFalse(point_in_polygon(cx, cy, pts))

        # 2. > MAX_EXCLUSION_ZONES (129 in DXF or 128 in DXF + 1 user keepout) fails loudly
        def _sq(idx: int) -> list[str]:
            x0, y0 = 2.0 + (idx % 20) * 4.5, 2.0 + (idx // 20) * 4.5
            return [
                "0", "LWPOLYLINE", "8", "KEEPOUT", "70", "1",
                "10", str(x0), "20", str(y0), "10", str(x0 + 2.0), "20", str(y0),
                "10", str(x0 + 2.0), "20", str(y0 + 2.0), "10", str(x0), "20", str(y0 + 2.0),
            ]
        dxf_128 = [
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "100", "20", "0", "10", "100", "20", "100", "10", "0", "20", "100",
        ]
        for i in range(MAX_EXCLUSION_ZONES):
            dxf_128.extend(_sq(i))
        (self.ws / "UserData" / "Ex128.dxf").write_text("\n".join([*dxf_128, "0", "ENDSEC", "0", "EOF"]) + "\n", encoding="utf-8")
        (self.ws / "UserData" / "Ex129.dxf").write_text("\n".join([*dxf_128, *_sq(128), "0", "ENDSEC", "0", "EOF"]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, str(MAX_EXCLUSION_ZONES)):
            parse_ascii_dxf(self.ws, "Ex129.dxf")
        store = VariantStore(self.ws, self.components)
        with self.assertRaisesRegex(ValueError, str(MAX_EXCLUSION_ZONES)):
            resolve_cad_synthesis_inputs(
                store, field_type="RegularSheds", pvc_name=None, dxf_name="Ex128.dxf",
                dxf_layers=None, dxf_unit_scale=None, tilt_deg=20.0, azimuth_deg=0.0, axis_azimuth_deg=0.0,
                pitch_m=8.0, tilt_explicit=True, azimuth_explicit=True, obstacles=None,
                exclusion_zones=[[[90.0, 90.0], [95.0, 90.0], [95.0, 95.0], [90.0, 95.0]]],
                terrain=None, module_name="Mod", version="8.1.7",
            )

        # 3. Self-intersecting (bowtie) and fold-back exclusions rejected inline, via validation, and from DXF
        bowtie = [[0.0, 0.0], [4.0, 4.0], [0.0, 4.0], [3.0, 0.0]]
        foldback = [[0.0, 0.0], [4.0, 0.0], [2.0, 0.0], [4.0, 4.0], [0.0, 4.0]]
        for bad_poly in (bowtie, foldback):
            with self.assertRaisesRegex(ValueError, "simple"):
                validate_exclusion_zones([bad_poly])
        bowtie_dxf = "\n".join([
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "30", "20", "0", "10", "30", "20", "30", "10", "0", "20", "30",
            "0", "LWPOLYLINE", "8", "KEEPOUT", "70", "1",
            "10", "0", "20", "0", "10", "4", "20", "4", "10", "0", "20", "4", "10", "3", "20", "0",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"
        (self.ws / "UserData" / "Bowtie.dxf").write_text(bowtie_dxf, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "self-intersecting"):
            parse_ascii_dxf(self.ws, "Bowtie.dxf")
        # Symmetric zero-signed-area bowtie must also be rejected (never silently dropped)
        sym_bowtie_dxf = bowtie_dxf.replace("10\n3\n20\n0", "10\n4\n20\n0")
        (self.ws / "UserData" / "SymBowtie.dxf").write_text(sym_bowtie_dxf, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "self-intersecting"):
            parse_ascii_dxf(self.ws, "SymBowtie.dxf")
        # Concave L-shape remains accepted
        l_shape = [[2.0, 2.0], [8.0, 2.0], [8.0, 4.0], [4.0, 4.0], [4.0, 8.0], [2.0, 8.0]]
        self.assertEqual(len(validate_exclusion_zones([l_shape])), 1)


class ThreeScopeExclusionLayoutTests(unittest.TestCase):
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
        self.cli = _FakeUtilityCLI(self.ws, year=1990, nominal_dc_kw=52.80, use_sfi_writer=False)

    def _assert_zero_table_overlap(
        self, tables: list[dict], excl_world: list[list[float]], *, scope: str, proj_depth: float | None = None,
    ) -> None:
        excl_tuples = [[(float(x), float(y)) for x, y in poly] for poly in excl_world]
        for tbl in tables:
            ox, oy, _ = tbl["origin_m"]
            az_deg = float(tbl.get("axis_azimuth_deg" if scope == "ground_tracker" else "azimuth_deg", 0.0))
            rot_rad = math.radians(az_deg)
            u_c, v_c = _rotate(ox, oy, rot_rad)
            excl_rot = [[_rotate(px, py, rot_rad) for px, py in p] for p in excl_tuples]
            if scope == "ground_tracker":
                half_u, half_v = 0.5 * float(tbl["long_shed_m"]), 0.5 * float(tbl["larg_shed_m"])
                u0, u1, v0, v1 = u_c - half_u, u_c + half_u, v_c - half_v, v_c + half_v
            else:
                assert proj_depth is not None
                half_u = 0.5 * float(tbl["long_shed_m"])
                u0, u1, v0, v1 = u_c - half_u, u_c + half_u, v_c - proj_depth, v_c
            for e_idx, e_poly in enumerate(excl_rot):
                self.assertFalse(
                    rect_overlaps_polygon(u0, u1, v0, v1, e_poly, tol=1e-4),
                    f"{scope} table {tbl['name']} [{u0:.2f},{u1:.2f}]x[{v0:.2f},{v1:.2f}] overlaps exclusion #{e_idx}",
                )

    def test_carving_and_backward_compat_across_all_three_scopes(self) -> None:
        site = [[0.0, 0.0], [90.0, 0.0], [90.0, 70.0], [0.0, 70.0]]
        pad = [[38.0, 26.0], [52.0, 26.0], [52.0, 42.0], [38.0, 42.0]]
        diag_road = [[18.0, 0.0], [23.0, 0.0], [35.0, 70.0], [30.0, 70.0]]
        u_shape = [[62.0, 12.0], [82.0, 12.0], [82.0, 36.0], [76.0, 36.0], [76.0, 20.0], [68.0, 20.0], [68.0, 36.0], [62.0, 36.0]]
        excls = [pad, diag_road, u_shape]

        # 1. ground_sheds (with rotated azimuth=15 deg)
        shed_base = plan_ground_polygon_layout(
            site, tilt_deg=20.0, azimuth_deg=15.0, pitch_m=8.0, module_width_m=1.052, module_length_m=2.131,
            module_rows=2, setback_m=1.0, max_table_columns=30,
        )
        shed_carved = plan_ground_polygon_layout(
            site, tilt_deg=20.0, azimuth_deg=15.0, pitch_m=8.0, module_width_m=1.052, module_length_m=2.131,
            module_rows=2, setback_m=1.0, max_table_columns=30, exclusion_zones=excls,
        )
        self.assertEqual(shed_base["exclusion_zone_count"], 0)
        self.assertEqual(shed_carved["exclusion_zone_count"], 3)
        self.assertLess(shed_carved["total_modules"], shed_base["total_modules"])
        self._assert_zero_table_overlap(shed_carved["tables"], excls, scope="ground_sheds", proj_depth=shed_carved["proj_depth_m"])

        # 2. ground_tracker (with axis_azimuth_deg=-10 deg & integer string divisibility n_mod_serie=10, 2P -> mult 5)
        trk_base = plan_tracker_polygon_layout(
            site, axis_azimuth_deg=-10.0, pitch_ew_m=8.0, module_width_m=1.052, module_length_m=2.131,
            module_rows=2, setback_m=1.0, max_table_columns=30, n_mod_serie=10,
        )
        trk_carved = plan_tracker_polygon_layout(
            site, axis_azimuth_deg=-10.0, pitch_ew_m=8.0, module_width_m=1.052, module_length_m=2.131,
            module_rows=2, setback_m=1.0, max_table_columns=30, n_mod_serie=10, exclusion_zones=excls,
        )
        self.assertLess(trk_carved["total_modules"], trk_base["total_modules"])
        self.assertTrue(all(t["module_columns"] % 5 == 0 and t["module_count"] % 10 == 0 for t in trk_carved["tables"]))
        self._assert_zero_table_overlap(trk_carved["tables"], excls, scope="ground_tracker")

        # 3. ground_dome
        _, dome_base = build_dome_3d_shading(
            site_polygon=site, terrain_mesh=None, terrain_opts=None, shed_count=4, pitch_m=6.0, tilt_deg=10.0,
            azimuth_deg=90.0, base_slope_deg=0.0, top_spacing_m=0.2, module_rows=1, module_columns=20,
            module_width_m=1.052, module_length_m=2.131, module_name="Bifi", module_orient="moPortrait",
            spacing_x_m=0.02, spacing_y_m=0.02, setback_m=1.0, ground_clearance_m=0.5, max_table_columns=25,
        )
        _, dome_carved = build_dome_3d_shading(
            site_polygon=site, terrain_mesh=None, terrain_opts=None, shed_count=4, pitch_m=6.0, tilt_deg=10.0,
            azimuth_deg=90.0, base_slope_deg=0.0, top_spacing_m=0.2, module_rows=1, module_columns=20,
            module_width_m=1.052, module_length_m=2.131, module_name="Bifi", module_orient="moPortrait",
            spacing_x_m=0.02, spacing_y_m=0.02, setback_m=1.0, ground_clearance_m=0.5, max_table_columns=25,
            exclusion_zones=excls,
        )
        self.assertLess(dome_carved["total_modules"], dome_base["total_modules"])
        dome_depth = dome_footprint_depth_m(1 * 2.131, 10.0, 0.2)
        self._assert_zero_table_overlap(dome_carved["tables"], excls, scope="ground_dome", proj_depth=dome_depth)

    def test_end_to_end_prepare_with_dxf_and_inline_exclusions_all_three_scopes(self) -> None:
        # DXF with BLOCK/INSERT transformer pad + circular pond + outer boundary
        dxf_tmpl = "\n".join([
            "0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "6", "0", "ENDSEC",
            "0", "SECTION", "2", "BLOCKS",
            "0", "BLOCK", "2", "PAD", "10", "0", "20", "0", "30", "0",
            "0", "LWPOLYLINE", "8", "0", "70", "1",
            "10", "0", "20", "0", "10", "8", "20", "0", "10", "8", "20", "8", "10", "0", "20", "8",
            "0", "ENDBLK", "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES",
            "0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1",
            "10", "0", "20", "0", "10", "{w}", "20", "0", "10", "{w}", "20", "{h}", "10", "0", "20", "{h}",
            "0", "INSERT", "8", "XFMR_PAD", "2", "PAD", "10", "{px}", "20", "{py}", "30", "0",
            "0", "CIRCLE", "8", "POND", "10", "{cx}", "20", "{cy}", "40", "{r}",
            "0", "ENDSEC", "0", "EOF",
        ]) + "\n"

        # 1. ground_sheds via DXF + extra inline exclusion_zones -> exact 120 modules (52.8 kW)
        (self.ws / "UserData" / "ShedEx.dxf").write_text(
            dxf_tmpl.format(w=30.0, h=25.0, px=11.0, py=0.0, cx=15.0, cy=12.0, r=4.0), encoding="utf-8"
        )
        parsed_shed = parse_ascii_dxf(self.ws, "ShedEx.dxf")
        extra_pad = [[11.0, 18.0], [19.0, 18.0], [19.0, 25.0], [11.0, 25.0]]
        plan_s = plan_ground_polygon_layout(
            parsed_shed["site_polygon"], tilt_deg=20.0, azimuth_deg=0.0, pitch_m=10.0,
            module_width_m=1.032, module_length_m=1.996, module_rows=2, setback_m=0.0,
            max_table_columns=20, exclusion_zones=[*parsed_shed["exclusion_zones"], extra_pad],
        )
        self.assertEqual(plan_s["total_modules"], 120)
        prep_s = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec(
                "DxfShedEx.PRJ", scope="ground_sheds", target_kw=52.8,
                ground_layout={"dxf_name": "ShedEx.dxf", "pitch_m": 10.0, "module_rows": 2,
                               "max_table_columns": 20, "exclusion_zones": [extra_pad]},
                tilt=20.0, azim=0.0,
            ),
        )
        self.assertIn(prep_s["status"], ("prepared", "needs_review"), prep_s)
        self.assertEqual(prep_s["utility_summary"]["exclusion_zone_count"], 3)
        self._assert_zero_table_overlap(
            prep_s["utility_summary"]["ground_3d_layout"]["tables"],
            [*parsed_shed["exclusion_zones"], extra_pad],
            scope="ground_sheds", proj_depth=plan_s["proj_depth_m"],
        )
        self.assertTrue(self.store.validate_structure("DxfShedEx.PRJ", "VC0")["valid"])

        # 2. ground_tracker via DXF + inline road -> exact 120 modules (52.8 kW)
        (self.ws / "UserData" / "TrkEx.dxf").write_text(
            dxf_tmpl.format(w=50.0, h=35.0, px=16.0, py=0.0, cx=41.0, cy=17.0, r=5.0), encoding="utf-8"
        )
        parsed_trk = parse_ascii_dxf(self.ws, "TrkEx.dxf")
        trk_extra = [[16.0, 8.0], [27.0, 8.0], [27.0, 35.0], [16.0, 35.0]]
        plan_t = plan_tracker_polygon_layout(
            parsed_trk["site_polygon"], axis_azimuth_deg=0.0, pitch_ew_m=8.0,
            module_width_m=1.032, module_length_m=1.996, module_rows=1, setback_m=0.0,
            max_table_columns=15, n_mod_serie=15, exclusion_zones=[*parsed_trk["exclusion_zones"], trk_extra],
        )
        self.assertEqual(plan_t["total_modules"], 120)
        prep_t = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec(
                "DxfTrkEx.PRJ", scope="ground_tracker", target_kw=52.8,
                ground_layout={"dxf_name": "TrkEx.dxf", "pitch_m": 8.0, "module_rows": 1,
                               "max_table_columns": 15, "exclusion_zones": [trk_extra]},
                tilt=None, azim=0.0,
            ),
        )
        self.assertIn(prep_t["status"], ("prepared", "needs_review"), prep_t)
        self.assertEqual(prep_t["utility_summary"]["exclusion_zone_count"], 3)
        self._assert_zero_table_overlap(
            prep_t["utility_summary"]["ground_3d_layout"]["tables"],
            [*parsed_trk["exclusion_zones"], trk_extra],
            scope="ground_tracker",
        )
        self.assertTrue(self.store.validate_structure("DxfTrkEx.PRJ", "VC0")["valid"])

        # 3. ground_dome via inline site_polygon + interior cutout exclusion_zones -> exact 120 modules (52.8 kW)
        dome_site = [[0.0, 0.0], [33.0, 0.0], [33.0, 13.0], [0.0, 13.0]]
        dome_ex = [[[10.0, 7.0], [10.9, 7.0], [10.9, 13.0], [10.0, 13.0]]]
        _, dome_lay = build_dome_3d_shading(
            site_polygon=dome_site, terrain_mesh=None, terrain_opts=None, shed_count=4, pitch_m=7.0,
            tilt_deg=12.0, azimuth_deg=0.0, base_slope_deg=0.0, top_spacing_m=0.2, module_rows=1,
            module_columns=20, module_width_m=1.032, module_length_m=1.996, module_name="Bifi",
            module_orient="moPortrait", spacing_x_m=0.02, spacing_y_m=0.02, setback_m=0.0,
            ground_clearance_m=0.5, max_table_columns=30, exclusion_zones=dome_ex,
        )
        self.assertEqual(dome_lay["total_modules"], 120)
        prep_d = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec(
                "DomeEx.PRJ", scope="ground_dome", target_kw=52.8,
                ground_layout={"site_polygon": dome_site, "exclusion_zones": dome_ex, "pitch_m": 7.0,
                               "module_rows": 1, "max_table_columns": 30, "dome_wiring": "mixed"},
                tilt=12.0, azim=0.0,
            ),
        )
        self.assertIn(prep_d["status"], ("prepared", "needs_review"), prep_d)
        self.assertEqual(prep_d["utility_summary"]["exclusion_zone_count"], 1)
        self._assert_zero_table_overlap(
            prep_d["utility_summary"]["ground_3d_layout"]["tables"],
            dome_ex, scope="ground_dome", proj_depth=dome_lay["dome_footprint_depth_m"],
        )
        self.assertTrue(self.store.validate_structure("DomeEx.PRJ", "VC0")["valid"])


class ValidationSchemaAndBudgetTests(unittest.TestCase):
    def test_schema_validation_guards_and_budget(self) -> None:
        ex = [[[1.0, 1.0], [5.0, 1.0], [5.0, 5.0], [1.0, 5.0]]]
        req = SingleDesignRequest(
            scope="ground_sheds", new_project="ExVal.PRJ", year=2023, target_dc_kw=50.0, tilt=20.0, azimuth=0.0,
            ground_layout={"model_mode": "3d", "site_polygon": [[0, 0], [40, 0], [40, 30], [0, 30]], "exclusion_zones": ex},
        )
        issues: list[dict] = []
        validate_utility_spec(req.model_dump(exclude_none=True), "ground_sheds", issues)
        self.assertEqual(issues, [])

        # Reject invalid/incompatible combinations
        bad_layouts = [
            {"model_mode": "unlimited", "site_polygon": [[0, 0], [40, 0], [40, 30], [0, 30]], "exclusion_zones": ex},
            {"sub_orientations": [{"tilt": 20.0, "azimuth": 0.0, "weight": 1.0}], "exclusion_zones": ex},
            {"pvc_name": "Plant.pvc", "exclusion_zones": ex},
            {"exclusion_zones": ex},  # missing site_polygon or dxf_name
            {"site_polygon": [[0, 0], [40, 0], [40, 30], [0, 30]], "exclusion_zones": [[[0, 0], [5, 0], [10, 0]]]},
        ]
        for gl in bad_layouts:
            bad_issues: list[dict] = []
            validate_utility_spec({"scope": "ground_sheds", "tilt": 20.0, "azimuth": 0.0, "ground_layout": gl}, "ground_sheds", bad_issues)
            self.assertTrue(bad_issues, f"Expected validation rejection for {gl}")

        # Verify strict schema accepts GroundLayoutRequest with exclusion_zones
        gl_model = GroundLayoutRequest(site_polygon=[[0, 0], [20, 0], [20, 20], [0, 20]], exclusion_zones=ex)
        self.assertEqual(gl_model.exclusion_zones, ex)

        # Tool budget & max .py line count
        async def _counts() -> dict[str | None, int]:
            return {grp: len(await create_mcp_server(tool_groups=grp).list_tools()) for grp in (None, "all", "admin", "expert")}
        self.assertEqual(asyncio.run(_counts()), {None: 48, "all": 75, "admin": 4, "expert": 23})
        root = Path(__file__).resolve().parents[3]
        for py in (root / "src" / "pvsyst_auto").rglob("*.py"):
            self.assertLessEqual(len(py.read_text(encoding="utf-8").splitlines()), 900, f"{py} exceeds 900 lines")


if __name__ == "__main__":
    unittest.main()

"""External-contract unit tests for PR 6: pure-stdlib .pvc 1.4.1 / ASCII .dxf CAD import and v0.10.0."""
from __future__ import annotations

import math
import re
import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.api.context import TOOL_GROUPS
from pvsyst_auto.api.projects import pvsyst_import_house_pv_scene, pvsyst_read_house_pv_scene
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.cad_import import COLLADA_141_NS, parse_pvc_141
from pvsyst_auto.scene.dxf_import import parse_ascii_dxf
from pvsyst_auto.scene.reader import read_house_pv_scene
from pvsyst_auto.workflows.design import prepare_design_project
from tests._fixtures import init_full_workspace
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN, _FakeUtilityCLI
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


def _make_pvc_fixed(*, rows: int = 2, cols: int = 2, xx_doctype: bool = False) -> str:
    """Build a COLLADA 1.4.1 fixed-frame .pvc fixture with Fbx_Root, inch units, obstacle, and terrain."""
    inch_to_m = 0.0254
    w_in, d_in = 16.95 / inch_to_m, 3.80 / inch_to_m
    y_drop_in, z_rise_in = (3.80 * math.cos(math.radians(25.0))) / inch_to_m, (3.80 * math.sin(math.radians(25.0))) / inch_to_m
    pitch_in = 9.0 / inch_to_m
    # Raw Fbx coords before Fbx_Root matrix [1,0,0,0, 0,0,-1,0, 0,1,0,0, 0,0,0,1]: (x, y, z) -> (x, -z, +y)
    # Using y_base = 0.8m above ground, z_raw negative so -z_raw becomes positive +Y in world coords.
    y0_in = 0.8 / inch_to_m
    geoms, insts = [], []
    idx = 0
    for r in range(rows):
        for c in range(cols):
            gid = f"Frame{idx}"
            idx += 1
            x0 = c * (w_in + 1.0 / inch_to_m)
            x1 = x0 + w_in
            z_bot = -(r * pitch_in + y_drop_in)
            z_top = -(r * pitch_in)
            y_top = y0_in + z_rise_in
            y_bot = y0_in
            # 8 corners: top quad 0..3, thin bottom quad 4..7
            pts = [
                (x0, y_top, z_top), (x1, y_top, z_top), (x1, y_bot, z_bot), (x0, y_bot, z_bot),
                (x0, y_top - 1.0, z_top), (x1, y_top - 1.0, z_top), (x1, y_bot - 1.0, z_bot), (x0, y_bot - 1.0, z_bot),
            ]
            flt = " ".join(f"{v:.4f}" for p in pts for v in p)
            geoms.append(f"""
    <geometry id="{gid}">
      <mesh>
        <source id="{gid}-pos"><float_array id="{gid}-arr" count="24">{flt}</float_array></source>
        <vertices id="{gid}-v"><input semantic="POSITION" source="#{gid}-pos" /></vertices>
        <triangles material="Material0" count="2"><input semantic="VERTEX" source="#{gid}-v" offset="0" /><p>0 1 2 0 2 3</p></triangles>
        <frame_parameters>
          <module_width>940</module_width><module_height>1675</module_height>
          <module_x_spacing>20</module_x_spacing><module_y_spacing>20</module_y_spacing>
          <module_manufacturer>generic</module_manufacturer><module_name>generic</module_name>
        </frame_parameters>
      </mesh>
    </geometry>""")
            insts.append(f'<instance_geometry url="#{gid}"><bind_material><technique_common><instance_material symbol="Material0" target="#Material0" /></technique_common></bind_material></instance_geometry>')

    # Non-PV obstacle cube (BuildingBox) and non-collinear terrain mesh (Topography_mesh)
    geoms.append(f"""
    <geometry id="Bldg1">
      <mesh>
        <source id="Bldg1-pos"><float_array id="Bldg1-arr" count="12">
          {-5.0/inch_to_m:.3f} 0 {-10.0/inch_to_m:.3f}
          {-5.0/inch_to_m:.3f} {6.0/inch_to_m:.3f} {-10.0/inch_to_m:.3f}
          {-2.0/inch_to_m:.3f} {6.0/inch_to_m:.3f} {-10.0/inch_to_m:.3f}
          {-2.0/inch_to_m:.3f} 0 {-10.0/inch_to_m:.3f}
        </float_array></source>
        <vertices id="Bldg1-v"><input semantic="POSITION" source="#Bldg1-pos" /></vertices>
        <triangles material="Material1" count="2"><input semantic="VERTEX" source="#Bldg1-v" offset="0" /><p>0 1 2 0 2 3</p></triangles>
      </mesh>
    </geometry>
    <geometry id="TopoMesh">
      <mesh>
        <source id="TopoMesh-pos"><float_array id="TopoMesh-arr" count="12">
          {-20.0/inch_to_m:.3f} 0 0
          {50.0/inch_to_m:.3f} 0 0
          {50.0/inch_to_m:.3f} {1.0/inch_to_m:.3f} {-40.0/inch_to_m:.3f}
          {-20.0/inch_to_m:.3f} {1.0/inch_to_m:.3f} {-40.0/inch_to_m:.3f}
        </float_array></source>
        <vertices id="TopoMesh-v"><input semantic="POSITION" source="#TopoMesh-pos" /></vertices>
        <triangles material="Material2" count="2"><input semantic="VERTEX" source="#TopoMesh-v" offset="0" /><p>0 1 2 0 2 3</p></triangles>
      </mesh>
    </geometry>""")
    insts.append('<instance_geometry url="#Bldg1"><bind_material><technique_common><instance_material symbol="Material1" target="#Material1" /></technique_common></bind_material></instance_geometry>')
    insts.append('<instance_geometry url="#TopoMesh"><bind_material><technique_common><instance_material symbol="Material2" target="#Material2" /></technique_common></bind_material></instance_geometry>')
    dt = "<!DOCTYPE COLLADA>\n" if xx_doctype else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
{dt}<COLLADA xmlns="{COLLADA_141_NS}" version="1.4.1">
  <asset><unit meter="{inch_to_m}" name="Inch" /></asset>
  <library_materials>
    <material id="Material0" name="Frames" />
    <material id="Material1" name="Building_obstacle" />
    <material id="Material2" name="Topography_mesh" />
  </library_materials>
  <library_geometries>{"".join(geoms)}</library_geometries>
  <library_visual_scenes><visual_scene id="Scene">
    <node id="Fbx_Root">
      <matrix>1 0 0 0 0 0 -1 0 0 1 0 0 0 0 0 1</matrix>
      <node id="PVcase">{"".join(insts)}</node>
    </node>
  </visual_scene></library_visual_scenes>
  <scene><instance_visual_scene url="#Scene" /></scene>
</COLLADA>"""


def _make_pvc_tracker(*, count: int = 6) -> str:
    """Build a COLLADA 1.4.1 single-axis tracker .pvc fixture in millimeters (2x10 landscape = 20 modules each)."""
    geoms, insts = [], []
    span_y, chord_x = 20140.0, 2084.0
    for i in range(count):
        gid = f"Trk{i}"
        xc = 10000.0 + i * 7000.0
        x0, x1 = xc - chord_x / 2, xc + chord_x / 2
        y_elev = 1500.0
        z0, z1 = 0.0, -span_y
        pts = [(x0, y_elev, z0), (x0, y_elev, z1), (x1, y_elev, z1), (x1, y_elev, z0)]
        flt = " ".join(f"{v:.2f}" for p in pts for v in p)
        geoms.append(f"""
    <geometry id="{gid}">
      <mesh>
        <source id="{gid}-pos"><float_array id="{gid}-arr" count="12">{flt}</float_array></source>
        <vertices id="{gid}-v"><input semantic="POSITION" source="#{gid}-pos" /></vertices>
        <triangles material="Material0" count="2"><input semantic="VERTEX" source="#{gid}-v" offset="0" /><p>0 1 2 0 2 3</p></triangles>
        <tracker_parameters>
          <module_width>1032</module_width><module_height>1996</module_height>
          <module_x_spacing>20</module_x_spacing><module_y_spacing>20</module_y_spacing>
          <tracker_type>single_axis_trackers</tracker_type>
          <axis_vertices>
            <float_array count="3">{xc:.2f} {y_elev:.2f} {z0:.2f}</float_array>
            <float_array count="3">{xc:.2f} {y_elev + 300.0:.2f} {z1:.2f}</float_array>
          </axis_vertices>
          <min_phi>-30</min_phi><max_phi>30</max_phi>
        </tracker_parameters>
      </mesh>
    </geometry>""")
        insts.append(f'<instance_geometry url="#{gid}" />')
    return f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="{COLLADA_141_NS}" version="1.4.1">
  <asset><unit meter="0.001" name="millimeter" /></asset>
  <library_geometries>{"".join(geoms)}</library_geometries>
  <library_visual_scenes><visual_scene id="Scene">
    <node id="Fbx_Root">
      <matrix>1 0 0 0 0 0 -1 0 0 1 0 0 0 0 0 1</matrix>
      <node id="Trackers">{"".join(insts)}</node>
    </node>
  </visual_scene></library_visual_scenes>
</COLLADA>"""


def _dxf_text(*, insunits: int = 6, scale: float = 1.0, poly_pts: int = 4, rect: tuple[float, float] = (80.0, 50.0)) -> str:
    """Build a valid ASCII DXF containing LWPOLYLINE boundary, POLYLINE obstacle, 3DFACE obstacle, and POINT terrain."""
    w, h = rect
    lines = ["0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", str(insunits), "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
    lines.extend(["0", "LWPOLYLINE", "8", "SITE_BOUNDARY", "70", "1"])
    if poly_pts <= 4:
        pts = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
    else:
        pts = [(40.0 + 40.0 * math.cos(2 * math.pi * i / poly_pts), 25.0 + 25.0 * math.sin(2 * math.pi * i / poly_pts)) for i in range(poly_pts)]
    for x, y in pts:
        lines.extend(["10", f"{x * scale:.4f}", "20", f"{y * scale:.4f}"])
    # POLYLINE obstacle with extrusion thickness
    lines.extend(["0", "POLYLINE", "8", "OBSTACLES", "70", "1", "39", f"{5.0 * scale:.4f}"])
    for ox, oy in [(-10.0, 10.0), (-5.0, 10.0), (-5.0, 15.0), (-10.0, 15.0)]:
        lines.extend(["0", "VERTEX", "8", "OBSTACLES", "10", f"{ox * scale:.4f}", "20", f"{oy * scale:.4f}", "30", "0.0"])
    lines.extend(["0", "SEQEND"])
    # 3DFACE obstacle
    lines.extend([
        "0", "3DFACE", "8", "BUILDINGS_SHADE",
        "10", f"{-4.0 * scale:.4f}", "20", f"{-8.0 * scale:.4f}", "30", "0.0",
        "11", f"{-1.0 * scale:.4f}", "21", f"{-8.0 * scale:.4f}", "31", "0.0",
        "12", f"{-1.0 * scale:.4f}", "22", f"{-8.0 * scale:.4f}", "32", f"{4.0 * scale:.4f}",
        "13", f"{-4.0 * scale:.4f}", "23", f"{-8.0 * scale:.4f}", "33", f"{4.0 * scale:.4f}",
    ])
    # Terrain POINT spots
    for tx, ty, tz in [(0.0, 0.0, 0.0), (80.0, 0.0, 2.0), (80.0, 50.0, 3.0), (0.0, 50.0, 1.0)]:
        lines.extend(["0", "POINT", "8", "TOPO_DTM", "10", f"{tx * scale:.4f}", "20", f"{ty * scale:.4f}", "30", f"{tz * scale:.4f}"])
    lines.extend(["0", "ENDSEC", "0", "EOF"])
    return "\n".join(lines) + "\n"


def _design_spec(project_name: str, *, scope: str, target_kw: float, ground_layout: dict, tilt: float | None = 25.0, azim: float | None = 0.0) -> dict:
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


class CadPvcDxfImportTests(unittest.TestCase):
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

    def test_release_version_docs_and_tool_budget(self) -> None:
        root = Path(__file__).resolve().parents[3]
        pyproj = (root / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproj, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "0.11.0")
        guide = (root / "docs" / "workflows-guide.md").read_text(encoding="utf-8")
        self.assertIn("pvsyst_cli_mcp-0.11.0-py3-none-any.whl", guide)
        all_tools = {name for grp in TOOL_GROUPS.values() for name in grp}
        default_tools = all_tools - set(TOOL_GROUPS["Admin & Diagnostics"]) - set(TOOL_GROUPS["Expert & Low-Level"])
        self.assertEqual(len(default_tools), 48)
        self.assertEqual(len(all_tools), 75)
        for py in (root / "src" / "pvsyst_auto").rglob("*.py"):
            self.assertLessEqual(len(py.read_text(encoding="utf-8").splitlines()), 900, f"{py} exceeds 900 lines")

    def test_pvc_fixed_parse_preview_and_prepare(self) -> None:
        (self.ws / "UserData" / "FixedPlant.pvc").write_text(_make_pvc_fixed(rows=2, cols=2), encoding="utf-8")
        parsed = parse_pvc_141(self.ws, "FixedPlant.pvc")
        self.assertEqual(parsed["scene_kind"], "fixed")
        self.assertEqual(parsed["table_count"], 4)
        self.assertEqual(parsed["inferred_module_count"], 160)
        self.assertEqual(parsed["obstacle_count"], 1)
        self.assertEqual(parsed["terrain_point_count"], 4)
        self.assertAlmostEqual(parsed["dominant_tilt_deg"], 25.0, places=1)
        self.assertAlmostEqual(parsed["pitch_estimate_m"], 9.0, places=2)

        preview = read_house_pv_scene(self.components, "FixedPlant.pvc")
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["format"], "pvc_1.4.1")
        self.assertEqual(preview["recommended_design_args"]["scope"], "ground_sheds")

        # Prepare omitting tilt/azimuth -> auto-filled from .pvc dominant orientation
        prep = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("PvcFixed.PRJ", scope="ground_sheds", target_kw=52.8,
                              ground_layout={"pvc_name": "FixedPlant.pvc"}, tilt=None, azim=None),
        )
        self.assertIn(prep["status"], ("prepared", "needs_review"), prep)
        vc0 = (self.ws / "Projects" / "PvcFixed.VC0").read_text(encoding="utf-8-sig")
        self.assertEqual(vc0.count("=pvShdArrayTable"), 4)
        self.assertIn("TypeOrigine=2", vc0)
        self.assertIn("=pvShdGround", vc0)
        self.assertIn("=pvShdGenObject", vc0)
        self.assertEqual(prep["utility_summary"]["cad_import"]["table_count"], 4)

    def test_pvc_tracker_parse_and_prepare(self) -> None:
        (self.ws / "UserData" / "Trackers.pvc").write_text(_make_pvc_tracker(count=6), encoding="utf-8")
        parsed = parse_pvc_141(self.ws, "Trackers.pvc")
        self.assertEqual(parsed["scene_kind"], "tracker")
        self.assertEqual(parsed["table_count"], 6)
        self.assertEqual(parsed["min_phi_deg"], -30.0)
        self.assertEqual(parsed["max_phi_deg"], 30.0)
        self.assertGreater(parsed["tables"][0]["axis_tilt_deg"], 0.8)

        prep = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("PvcTrk.PRJ", scope="ground_tracker", target_kw=52.8,
                              ground_layout={"pvc_name": "Trackers.pvc"}, tilt=None, azim=None),
        )
        self.assertIn(prep["status"], ("prepared", "needs_review"), prep)
        vc0 = (self.ws / "Projects" / "PvcTrk.VC0").read_text(encoding="utf-8-sig")
        self.assertEqual(vc0.count("=pvShdArrayTracking"), 6)
        self.assertIn("TypeOrigine=7", vc0)
        self.assertIn("AxisOrig=2", vc0)
        self.assertIn("MinPhi=-30.0", vc0)
        self.assertIn("MaxPhi=30.0", vc0)

    def test_pvc_security_and_validation_guards(self) -> None:
        (self.ws / "UserData" / "Xxe.pvc").write_text(_make_pvc_fixed(xx_doctype=True), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            parse_pvc_141(self.ws, "Xxe.pvc")
        (self.ws / "UserData" / "BadVer.pvc").write_text(f'<COLLADA xmlns="{COLLADA_141_NS}" version="1.5.0" />', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "1.4.1"):
            parse_pvc_141(self.ws, "BadVer.pvc")
        (self.ws / "UserData" / "Small.pvc").write_text(_make_pvc_fixed(rows=1, cols=1), encoding="utf-8")
        # Scope mismatch (fixed .pvc on ground_tracker)
        bad_scope = prepare_design_project(
            self.cli, self.store, dry_run=True,
            spec=_design_spec("BadScope.PRJ", scope="ground_tracker", target_kw=20.0,
                              ground_layout={"pvc_name": "Small.pvc"}, tilt=None, azim=None),
        )
        self.assertEqual(bad_scope["status"], "blocked")
        # Tilt contradiction (> 5 deg from 25 deg)
        bad_tilt = prepare_design_project(
            self.cli, self.store, dry_run=True,
            spec=_design_spec("BadTilt.PRJ", scope="ground_sheds", target_kw=20.0,
                              ground_layout={"pvc_name": "Small.pvc"}, tilt=10.0, azim=0.0),
        )
        self.assertEqual(bad_tilt["status"], "blocked")
        # Under-sized 3D area (~64 m2 < ~120 m2)
        too_big = prepare_design_project(
            self.cli, self.store, dry_run=True,
            spec=_design_spec("TooBig.PRJ", scope="ground_sheds", target_kw=120.0,
                              ground_layout={"pvc_name": "Small.pvc"}, tilt=25.0, azim=0.0),
        )
        self.assertEqual(too_big["status"], "blocked")
        self.assertIn("exceeds imported .pvc 3D collector area", str(too_big["issues"]))

    def test_dxf_parse_simplification_override_and_three_scopes(self) -> None:
        (self.ws / "UserData" / "SiteMM.dxf").write_text(_dxf_text(insunits=4, scale=1000.0, poly_pts=90), encoding="utf-8")
        parsed_mm = parse_ascii_dxf(self.ws, "SiteMM.dxf")
        self.assertTrue(parsed_mm["boundary_simplified"])
        self.assertLessEqual(len(parsed_mm["site_polygon"]), 64)
        self.assertGreater(parsed_mm["site_area_m2"], 2500.0)
        self.assertEqual(len(parsed_mm["obstacles"]), 2)
        self.assertEqual(len(parsed_mm["terrain_points"]), 4)

        # Unit scale override + explicit dxf_layers mapping
        (self.ws / "UserData" / "Custom.dxf").write_text(_dxf_text(insunits=0, scale=10.0), encoding="utf-8")
        over = parse_ascii_dxf(
            self.ws, "Custom.dxf",
            dxf_layers={"boundary": "SITE_BOUNDARY", "obstacles": ["OBSTACLES", "BUILDINGS_SHADE"], "terrain": "TOPO_DTM"},
            unit_scale=0.1,
        )
        self.assertAlmostEqual(over["site_area_m2"], 4000.0, places=0)

        preview = read_house_pv_scene(self.components, "Custom.dxf")
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["format"], "ascii_dxf")

        for sc, name, tilt, rect, extra in (
            ("ground_sheds", "DxfSheds.PRJ", 20.0, (33.0, 15.0), {"pitch_m": 10.0, "module_rows": 2, "max_table_columns": 30}),
            ("ground_tracker", "DxfTrk.PRJ", None, (73.0, 20.0), {"pitch_m": 10.0, "module_rows": 1, "max_table_columns": 20}),
            ("ground_dome", "DxfDome.PRJ", 12.0, (33.0, 14.0), {"pitch_m": 7.0, "module_rows": 1, "max_table_columns": 30}),
        ):
            (self.ws / "UserData" / f"{name}.dxf").write_text(_dxf_text(insunits=6, scale=1.0, rect=rect), encoding="utf-8")
            prep = prepare_design_project(
                self.cli, self.store, dry_run=False,
                spec=_design_spec(name, scope=sc, target_kw=52.8,
                                  ground_layout={"dxf_name": f"{name}.dxf", **extra}, tilt=tilt, azim=0.0),
            )
            self.assertIn(prep["status"], ("prepared", "needs_review"), f"{sc}: {prep}")
            self.assertEqual(prep["utility_summary"]["cad_import"]["format"], "ascii_dxf")
            vc0 = (self.ws / "Projects" / f"{Path(name).stem}.VC0").read_text(encoding="utf-8-sig")
            self.assertIn("=pvShdGround", vc0)
            self.assertIn("=pvShdGenObject", vc0)

    def test_dxf_rejections_and_house_import_guard(self) -> None:
        (self.ws / "UserData" / "Bin.dxf").write_bytes(b"AutoCAD Binary DXF\r\n\x1a\x00")
        with self.assertRaisesRegex(ValueError, "Binary DXF"):
            parse_ascii_dxf(self.ws, "Bin.dxf")
        (self.ws / "UserData" / "Empty.dxf").write_text("0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "No closed boundary polygon"):
            parse_ascii_dxf(self.ws, "Empty.dxf")
        with self.assertRaisesRegex(ValueError, "Unsupported dxf_layers role"):
            parse_ascii_dxf(self.ws, "Empty.dxf", dxf_layers={"unknown": "0"})

        (self.ws / "UserData" / "Ok.pvc").write_text(_make_pvc_fixed(rows=1, cols=1), encoding="utf-8")
        (self.ws / "UserData" / "Ok.dxf").write_text(_dxf_text(), encoding="utf-8")
        for bad_gl in (
            {"pvc_name": "Ok.pvc", "dxf_name": "Ok.dxf"},
            {"pvc_name": "../bad.pvc"},
            {"pvc_name": "sub\\bad.pvc"},
            {"dxf_name": "sub/bad.dxf"},
            {"dxf_name": "CON.dxf"},
            {"dxf_name": "Ok.dxf", "site_polygon": [[0, 0], [10, 0], [10, 10]]},
            {"dxf_name": "Ok.dxf", "model_mode": "unlimited"},
            {"dxf_name": "Ok.dxf", "dxf_unit_scale": -1.0},
        ):
            res = prepare_design_project(self.cli, self.store, dry_run=True, spec=_design_spec("BadGL.PRJ", scope="ground_sheds", target_kw=20.0, ground_layout=bad_gl))
            self.assertEqual(res["status"], "blocked", bad_gl)

        from unittest.mock import patch
        with patch("pvsyst_auto.api.projects.components", return_value=self.components):
            prev_pvc = pvsyst_read_house_pv_scene("Ok.pvc")
            self.assertEqual(prev_pvc["status"], "ready")
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            blk = pvsyst_import_house_pv_scene("Ok.pvc", "VC9", dry_run=True)
            self.assertEqual(blk["status"], "blocked")

    def test_cad_double_sided_obstacle_dedup_and_staggered_pitch(self) -> None:
        # 1. Double-sided obstacle mesh in .pvc (duplicate reversed triangles) must be deduplicated cleanly
        pvc_dup = _make_pvc_fixed(rows=2, cols=2).replace(
            '<triangles material="Material1" count="2"><input semantic="VERTEX" source="#Bldg1-v" offset="0" /><p>0 1 2 0 2 3</p></triangles>',
            '<triangles material="Material1" count="4"><input semantic="VERTEX" source="#Bldg1-v" offset="0" /><p>0 1 2 0 2 3 2 1 0 3 2 0</p></triangles>',
        )
        (self.ws / "UserData" / "DupObs.pvc").write_text(pvc_dup, encoding="utf-8")
        parsed_pvc = parse_pvc_141(self.ws, "DupObs.pvc")
        self.assertEqual(len(parsed_pvc["obstacles"]), 1)
        self.assertEqual(len(parsed_pvc["obstacles"][0]["triangles"]), 2)
        prep_pvc = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("DupObsPvc.PRJ", scope="ground_sheds", target_kw=52.8,
                              ground_layout={"pvc_name": "DupObs.pvc", "ground_clearance_m": 0.8}, tilt=25.0, azim=0.0),
        )
        self.assertIn(prep_pvc["status"], ("prepared", "needs_review"), prep_pvc)

        # 2. Double-sided 3DFACE in ASCII .dxf (reversed corner order at identical coords) must deduplicate cleanly
        dxf_dup = _dxf_text(insunits=6, scale=1.0, rect=(33.0, 15.0)).replace(
            "\n0\nENDSEC\n0\nEOF\n",
            "\n" + "\n".join([
                "0", "3DFACE", "8", "BUILDINGS_SHADE",
                "10", "-4.0000", "20", "-8.0000", "30", "0.0",
                "11", "-4.0000", "21", "-8.0000", "31", "4.0",
                "12", "-1.0000", "22", "-8.0000", "32", "4.0",
                "13", "-1.0000", "23", "-8.0000", "33", "0.0",
                "0", "ENDSEC", "0", "EOF",
            ]) + "\n",
        )
        (self.ws / "UserData" / "DupObs.dxf").write_text(dxf_dup, encoding="utf-8")
        parsed_dxf = parse_ascii_dxf(self.ws, "DupObs.dxf")
        bldg_obs = [o for o in parsed_dxf["obstacles"] if o.get("kind") == "mesh" and "BUILDINGS_SHADE" in o.get("name", "")]
        self.assertEqual(len(bldg_obs), 1, parsed_dxf["obstacles"])
        self.assertEqual(len(bldg_obs[0]["triangles"]), 2)
        prep_dxf = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("DupObsDxf.PRJ", scope="ground_sheds", target_kw=52.8,
                              ground_layout={"dxf_name": "DupObs.dxf", "pitch_m": 10.0, "module_rows": 2, "max_table_columns": 30}, tilt=25.0, azim=0.0),
        )
        self.assertIn(prep_dxf["status"], ("prepared", "needs_review"), prep_dxf)

        # 3. Staggered .pvc blocks with Y-offset < coll_w must not fail pitch <= collector_width synthesis
        staggered = pvc_dup.replace(
            '<matrix>1 0 0 0 0 0 -1 0 0 1 0 0 0 0 0 1</matrix>\n      <node id="PVcase">',
            '<matrix>1 0 0 0 0 0 -1 0 0 1 0 0 0 0 0 1</matrix>\n      <node id="BlockB"><translate>0 0 -59.055</translate>'
            '<instance_geometry url="#Frame0" /></node>\n      <node id="PVcase">',
        )
        (self.ws / "UserData" / "Staggered.pvc").write_text(staggered, encoding="utf-8")
        prep_stag = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("StaggeredPvc.PRJ", scope="ground_sheds", target_kw=52.8,
                              ground_layout={"pvc_name": "Staggered.pvc"}, tilt=25.0, azim=0.0),
        )
        self.assertIn(prep_stag["status"], ("prepared", "needs_review"), prep_stag)


if __name__ == "__main__":
    unittest.main()

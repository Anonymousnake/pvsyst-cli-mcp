"""Offline unit tests for PR 4: mixed .pvc subsets, multi-orientation clustering, and 2D/3D report maps."""
from __future__ import annotations

from datetime import datetime, timedelta
import math
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from pvsyst_auto.api.schemas import GroundLayoutRequest, ReportScenarioInput
import pvsyst_auto.api.simulation as api_sim
from pvsyst_auto.components.editor import objects
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.cad_import import COLLADA_141_NS, build_pvc_3d_shading, parse_pvc_141
from pvsyst_auto.scene.pvc_clustering import (
    apportion_pvc_orientations,
    cluster_fixed_pvc_orientations,
    filter_pvc_subset,
)
from pvsyst_auto.scene.reader import read_house_pv_scene
from pvsyst_auto.workflows.design import prepare_design_project
from pvsyst_auto.workflows.report import generate_report
from pvsyst_auto.workflows.report_layout import _table_corners_3d, extract_vc_layout_preview
from tests._fixtures import init_full_workspace, write_sfi_csv
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN, _FakeUtilityCLI
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


def _fixed_geom_and_inst(
    gid: str, *, cx: float, cy: float, z_base: float = 0.8, tilt_deg: float = 20.0,
    azimuth_deg: float = 0.0, n_rows: int = 2, n_cols: int = 10,
) -> tuple[str, str]:
    """Emit one Collada 1.4.1 fixed frame geometry (meter units) rotated to (tilt_deg, azimuth_deg)."""
    mw, ml, sx, sy = 1.032, 1.996, 0.02, 0.02
    long_s = n_cols * mw + (n_cols - 1) * sx
    larg_s = n_rows * ml + (n_rows - 1) * sy
    proj = larg_s * math.cos(math.radians(tilt_deg))
    rise = larg_s * math.sin(math.radians(tilt_deg))
    az_rad = math.radians(azimuth_deg)
    c_az, s_az = math.cos(az_rad), math.sin(az_rad)

    def _to_world(du: float, dv: float, dz: float) -> tuple[float, float, float]:
        wx = cx + du * c_az + dv * s_az
        wy = cy - du * s_az + dv * c_az
        return (wx, wy, z_base + dz)

    top = [
        _to_world(-0.5 * long_s, 0.0, 0.0),
        _to_world(0.5 * long_s, 0.0, 0.0),
        _to_world(0.5 * long_s, -proj, rise),
        _to_world(-0.5 * long_s, -proj, rise),
    ]
    bot = [(x, y, z - 0.05) for x, y, z in top]
    flt = " ".join(f"{v:.4f}" for p in (*top, *bot) for v in p)
    geom = f"""
    <geometry id="{gid}">
      <mesh>
        <source id="{gid}-pos"><float_array id="{gid}-arr" count="24">{flt}</float_array></source>
        <vertices id="{gid}-v"><input semantic="POSITION" source="#{gid}-pos" /></vertices>
        <triangles material="MatPV" count="2"><input semantic="VERTEX" source="#{gid}-v" offset="0" /><p>0 1 2 0 2 3</p></triangles>
        <frame_parameters>
          <module_width>{int(mw * 1000)}</module_width><module_height>{int(ml * 1000)}</module_height>
          <module_x_spacing>{int(sx * 1000)}</module_x_spacing><module_y_spacing>{int(sy * 1000)}</module_y_spacing>
        </frame_parameters>
      </mesh>
    </geometry>"""
    inst = f'<instance_geometry url="#{gid}"><bind_material><technique_common><instance_material symbol="MatPV" target="#MatPV" /></technique_common></bind_material></instance_geometry>'
    return geom, inst


def _tracker_geom_and_inst(gid: str, *, xc: float, yc: float, z_elev: float = 1.5, n_cols: int = 20) -> tuple[str, str]:
    """Emit one Collada 1.4.1 single-axis tracker geometry (meter units)."""
    mw, ml, sx, sy = 1.032, 1.996, 0.02, 0.02
    larg_s = n_cols * mw + (n_cols - 1) * sx
    long_s = ml
    pts = [
        (xc - 0.5 * long_s, yc - 0.5 * larg_s, z_elev),
        (xc + 0.5 * long_s, yc - 0.5 * larg_s, z_elev),
        (xc + 0.5 * long_s, yc + 0.5 * larg_s, z_elev),
        (xc - 0.5 * long_s, yc + 0.5 * larg_s, z_elev),
    ]
    flt = " ".join(f"{v:.4f}" for p in pts for v in p)
    geom = f"""
    <geometry id="{gid}">
      <mesh>
        <source id="{gid}-pos"><float_array id="{gid}-arr" count="12">{flt}</float_array></source>
        <vertices id="{gid}-v"><input semantic="POSITION" source="#{gid}-pos" /></vertices>
        <triangles material="MatPV" count="2"><input semantic="VERTEX" source="#{gid}-v" offset="0" /><p>0 1 2 0 2 3</p></triangles>
        <tracker_parameters>
          <module_width>{int(mw * 1000)}</module_width><module_height>{int(ml * 1000)}</module_height>
          <module_x_spacing>{int(sx * 1000)}</module_x_spacing><module_y_spacing>{int(sy * 1000)}</module_y_spacing>
          <tracker_type>single_axis_trackers</tracker_type>
          <axis_vertices>
            <float_array count="3">{xc:.3f} {yc - 0.5 * larg_s:.3f} {z_elev:.3f}</float_array>
            <float_array count="3">{xc:.3f} {yc + 0.5 * larg_s:.3f} {z_elev + 0.25:.3f}</float_array>
          </axis_vertices>
          <min_phi>-55</min_phi><max_phi>55</max_phi>
        </tracker_parameters>
      </mesh>
    </geometry>"""
    inst = f'<instance_geometry url="#{gid}" />'
    return geom, inst


def _wrap_pvc_xml(geoms: list[str], insts: list[str], *, include_obs_terrain: bool = True) -> str:
    extra_g = list(geoms)
    extra_i = list(insts)
    if include_obs_terrain:
        extra_g.append("""
    <geometry id="ObsBldg">
      <mesh>
        <source id="ObsBldg-pos"><float_array id="ObsBldg-arr" count="24">
          -12 -10 0  -8 -10 0  -8 -6 0  -12 -6 0
          -12 -10 5  -8 -10 5  -8 -6 5  -12 -6 5
        </float_array></source>
        <vertices id="ObsBldg-v"><input semantic="POSITION" source="#ObsBldg-pos" /></vertices>
        <triangles material="MatObs" count="4"><input semantic="VERTEX" source="#ObsBldg-v" offset="0" /><p>4 5 6 4 6 7 0 1 5 0 5 4</p></triangles>
      </mesh>
    </geometry>
    <geometry id="TopoMesh">
      <mesh>
        <source id="TopoMesh-pos"><float_array id="TopoMesh-arr" count="15">
          -20 -20 0.0  80 -20 1.2  80 60 2.8  -20 60 1.5  30 20 1.9
        </float_array></source>
        <vertices id="TopoMesh-v"><input semantic="POSITION" source="#TopoMesh-pos" /></vertices>
        <triangles material="MatTopo" count="4"><input semantic="VERTEX" source="#TopoMesh-v" offset="0" /><p>0 1 4 1 2 4 2 3 4 3 0 4</p></triangles>
      </mesh>
    </geometry>""")
        extra_i.append('<instance_geometry url="#ObsBldg"><bind_material><technique_common><instance_material symbol="MatObs" target="#MatObs" /></technique_common></bind_material></instance_geometry>')
        extra_i.append('<instance_geometry url="#TopoMesh"><bind_material><technique_common><instance_material symbol="MatTopo" target="#MatTopo" /></technique_common></bind_material></instance_geometry>')
    return f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="{COLLADA_141_NS}" version="1.4.1">
  <asset><unit meter="1.0" name="meter" /></asset>
  <library_materials>
    <material id="MatPV" name="PV_Frames" />
    <material id="MatObs" name="Building_obstacle" />
    <material id="MatTopo" name="Topography_terrain" />
  </library_materials>
  <library_geometries>{"".join(extra_g)}</library_geometries>
  <library_visual_scenes><visual_scene id="Scene">
    <node id="PVcase">{"".join(extra_i)}</node>
  </visual_scene></library_visual_scenes>
</COLLADA>"""


def _write_short_sfi_csv(path: Path) -> Path:
    start = datetime(2024, 6, 1, 0, 0)
    rows = []
    for h in range(48):
        dt = start + timedelta(hours=h)
        day = 8 <= dt.hour < 17
        rows.append([
            dt.strftime("%d/%m/%y %H:%M"),
            650.0 if day else 0.0,
            115.0 if day else 0.0,
            105.0 if day else 0.0,
            0.84 if day else 0.0,
            1.2 if (day and 11 <= dt.hour <= 13) else 0.0,
        ])
    write_sfi_csv(
        path,
        [("GlobInc", "W/m2"), ("EArray", "kW"), ("E_Grid", "kW"), ("PR", "ratio"), ("IL_Pmax", "kW")],
        rows,
    )
    return path


def _design_spec(project: str, *, scope: str, target_kw: float, ground_layout: dict, ond_name: str = "Inv50.OND",
                 tilt: float | None = None, azim: float | None = None) -> dict:
    spec: dict = {
        "scope": scope,
        "new_project": project,
        "year": 1990,
        "sizing_mode": "auto_from_components",
        "auto_sizing": {"capacity_tolerance_pct": 12.0},
        "target_dc_kw": target_kw,
        "module": {"file_name": "Bifi440.PAN"},
        "inverter": {"file_name": ond_name},
        "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
        "ground_layout": ground_layout,
    }
    if tilt is not None:
        spec["tilt"] = tilt
    if azim is not None:
        spec["azimuth"] = azim
    return spec


class PvcMultiOrientAndReportMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = init_full_workspace(Path(self._tmp.name))
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV" / "PVmodules" / "Bifi440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        (self.ws / "ComposPV" / "Inverters" / "Inv50.OND").write_text(UTILITY_OND, encoding="utf-8")
        (self.ws / "ComposPV" / "Inverters" / "Inv50_4MPPT.OND").write_text(
            UTILITY_OND.replace("NbMPPT=2", "NbMPPT=4"), encoding="utf-8",
        )
        self.components = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.components)
        self.cli = _FakeUtilityCLI(self.ws, year=1990, nominal_dc_kw=52.80, use_sfi_writer=False)

    def _write_mixed_pvc(
        self, filename: str = "MixedSite.pvc", *, multi_fixed: bool = False, fixed_specs: list[tuple] | None = None
    ) -> None:
        geoms, insts = [], []
        if fixed_specs is None:
            fixed_specs = (
                [(0.0, 0.0, 22.0, 0.0, 2, 15), (0.0, 10.0, 23.0, 2.0, 2, 15),
                 (25.0, 0.0, 15.0, 90.0, 2, 8), (25.0, 10.0, 16.0, 88.0, 2, 8),
                 (45.0, 0.0, 14.0, -90.0, 2, 8), (45.0, 10.0, 15.0, -92.0, 2, 8)]
                if multi_fixed else
                [(0.0, 0.0, 25.0, 0.0, 2, 20), (0.0, 9.0, 25.0, 0.0, 2, 20),
                 (25.0, 0.0, 25.0, 0.0, 2, 20), (25.0, 9.0, 25.0, 0.0, 2, 20)]
            )
        for idx, (cx, cy, tilt, az, nr, nc) in enumerate(fixed_specs):
            g, ins = _fixed_geom_and_inst(f"Fix{idx}", cx=cx, cy=cy, tilt_deg=tilt, azimuth_deg=az, n_rows=nr, n_cols=nc)
            geoms.append(g); insts.append(ins)
        for idx in range(6):
            g, ins = _tracker_geom_and_inst(f"Trk{idx}", xc=60.0 + idx * 7.0, yc=10.0, n_cols=20)
            geoms.append(g); insts.append(ins)
        (self.ws / "UserData" / filename).write_text(_wrap_pvc_xml(geoms, insts), encoding="utf-8")

    def test_mixed_pvc_subset_filtering_and_validation(self) -> None:
        self._write_mixed_pvc("MixedSite.pvc")
        parsed_mixed = parse_pvc_141(self.ws, "MixedSite.pvc")
        self.assertEqual(parsed_mixed["scene_kind"], "mixed")
        self.assertEqual(parsed_mixed["fixed_count"], 4)
        self.assertEqual(parsed_mixed["tracker_count"], 6)
        with self.assertRaisesRegex(ValueError, "Mixed fixed \\+ tracker"):
            build_pvc_3d_shading(parsed_mixed, module_name="Bifi440")

        preview = read_house_pv_scene(self.components, "MixedSite.pvc")
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["geometry"]["available_subsets"], ["fixed", "tracker"])

        # Omitting pvc_subset still rejects mixed .pvc
        blocked_mix = prepare_design_project(
            self.cli, self.store, dry_run=True,
            spec=_design_spec("MixBlock.PRJ", scope="ground_sheds", target_kw=52.8,
                              ground_layout={"pvc_name": "MixedSite.pvc"}),
        )
        self.assertEqual(blocked_mix["status"], "blocked")
        self.assertIn("pvc_subset", str(blocked_mix["issues"]))

        # pvc_subset='fixed' with ground_sheds preserves obstacle & terrain
        prep_fixed = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("MixFixed.PRJ", scope="ground_sheds", target_kw=52.8,
                              ground_layout={"pvc_name": "MixedSite.pvc", "pvc_subset": "fixed"}),
        )
        self.assertIn(prep_fixed["status"], ("prepared", "needs_review"), prep_fixed)
        vc0_fix = (self.ws / "Projects" / "MixFixed.VC0").read_text(encoding="utf-8-sig")
        self.assertEqual(vc0_fix.count("=pvShdArrayTable"), 4)
        self.assertEqual(vc0_fix.count("=pvShdArrayTracking"), 0)
        self.assertIn("=pvShdGenObject", vc0_fix)
        self.assertIn("=pvShdGround", vc0_fix)
        self.assertEqual(prep_fixed["utility_summary"]["cad_import"]["pvc_subset"], "fixed")
        self.assertEqual(prep_fixed["utility_summary"]["cad_import"]["filtered_out_table_count"], 6)
        self.assertTrue(self.store.validate_structure("MixFixed.PRJ", "VC0")["valid"])

        # pvc_subset='tracker' with ground_tracker preserves obstacle & terrain
        prep_trk = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("MixTrk.PRJ", scope="ground_tracker", target_kw=52.8,
                              ground_layout={"pvc_name": "MixedSite.pvc", "pvc_subset": "tracker"}),
        )
        self.assertIn(prep_trk["status"], ("prepared", "needs_review"), prep_trk)
        vc0_trk = (self.ws / "Projects" / "MixTrk.VC0").read_text(encoding="utf-8-sig")
        self.assertEqual(vc0_trk.count("=pvShdArrayTracking"), 6)
        self.assertEqual(vc0_trk.count("=pvShdArrayTable"), 0)
        self.assertIn("=pvShdGenObject", vc0_trk)
        self.assertIn("=pvShdGround", vc0_trk)
        self.assertEqual(prep_trk["utility_summary"]["cad_import"]["pvc_subset"], "tracker")
        self.assertTrue(self.store.validate_structure("MixTrk.PRJ", "VC0")["valid"])

        # Validation guards for pvc_subset
        for bad_scope, bad_gl in (
            ("ground_tracker", {"pvc_name": "MixedSite.pvc", "pvc_subset": "fixed"}),
            ("ground_sheds", {"pvc_name": "MixedSite.pvc", "pvc_subset": "tracker"}),
            ("ground_sheds", {"pvc_subset": "fixed"}),
            ("ground_sheds", {"pvc_name": "MixedSite.pvc", "pvc_subset": "dome"}),
        ):
            res = prepare_design_project(
                self.cli, self.store, dry_run=True,
                spec=_design_spec("BadSub.PRJ", scope=bad_scope, target_kw=52.8, ground_layout=bad_gl),
            )
            self.assertEqual(res["status"], "blocked", (bad_scope, bad_gl))

        # Empty subset guard (requesting tracker from fixed-only parsed dict)
        only_fixed = filter_pvc_subset(parsed_mixed, "fixed")
        with self.assertRaisesRegex(ValueError, "no tracker PV tables"):
            filter_pvc_subset(only_fixed, "tracker")

    def test_fixed_pvc_multi_orientation_clustering_dedicated_and_shared_mppt(self) -> None:
        self._write_mixed_pvc("MixedMultiFixed.pvc", multi_fixed=True)

        # Unit test circular azimuth wraparound (+178 and -179 cluster together around 180)
        wrap_tables = [
            {"name": "N1", "tilt_deg": 20.0, "azimuth_deg": 178.0, "base_slope_deg": 0.0, "long_shed_m": 10.0, "larg_shed_m": 4.0, "origin_m": [0, 0, 1], "module_count": 20},
            {"name": "N2", "tilt_deg": 21.0, "azimuth_deg": -179.0, "base_slope_deg": 0.0, "long_shed_m": 10.0, "larg_shed_m": 4.0, "origin_m": [0, 8, 1], "module_count": 20},
        ]
        tagged_w, sum_w = cluster_fixed_pvc_orientations(wrap_tables, 5.0)
        self.assertEqual(len(sum_w), 1)
        self.assertTrue(all(t["orientation_id"] == 1 for t in tagged_w))
        self.assertGreater(abs(sum_w[0]["azimuth_deg"]), 175.0)

        # >8 clusters rejected cleanly
        nine_tables = [
            {"name": f"O{i}", "tilt_deg": 10.0 + i * 5.0, "azimuth_deg": -120.0 + i * 30.0, "base_slope_deg": 0.0,
             "long_shed_m": 10.0, "larg_shed_m": 4.0, "origin_m": [i * 15.0, 0.0, 1.0], "module_count": 20}
            for i in range(9)
        ]
        with self.assertRaisesRegex(ValueError, "exceeding the maximum of 8"):
            cluster_fixed_pvc_orientations(nine_tables, 1.0)

        # Fixed-only multi-orient .pvc without tolerance preserves single orientation behavior
        geoms_only, insts_only = [], []
        for idx, (cx, cy, tilt, az, nr, nc) in enumerate([
            (0.0, 0.0, 22.0, 0.0, 2, 20), (0.0, 10.0, 23.0, 2.0, 2, 20),
            (0.0, 20.0, 22.0, 0.0, 2, 20), (0.0, 30.0, 23.0, -2.0, 2, 20),
            (30.0, 0.0, 15.0, 90.0, 2, 15), (30.0, 10.0, 16.0, 88.0, 2, 15),
            (30.0, 20.0, 15.0, 90.0, 2, 15), (30.0, 30.0, 16.0, 92.0, 2, 15),
            (60.0, 0.0, 15.0, -90.0, 2, 14), (60.0, 10.0, 15.0, -90.0, 2, 14),
            (60.0, 20.0, 15.0, -90.0, 2, 14), (60.0, 30.0, 15.0, -90.0, 2, 14),
        ]):
            g, ins = _fixed_geom_and_inst(f"MF{idx}", cx=cx, cy=cy, tilt_deg=tilt, azimuth_deg=az, n_rows=nr, n_cols=nc)
            geoms_only.append(g); insts_only.append(ins)
        (self.ws / "UserData" / "MultiFixed.pvc").write_text(_wrap_pvc_xml(geoms_only, insts_only), encoding="utf-8")

        single_prep = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("PvcSingleMode.PRJ", scope="ground_sheds", target_kw=140.0,
                              ground_layout={"pvc_name": "MultiFixed.pvc"}),
        )
        self.assertIn(single_prep["status"], ("prepared", "needs_review"), single_prep)
        vc0_single = (self.ws / "Projects" / "PvcSingleMode.VC0").read_text(encoding="utf-8-sig")
        self.assertIn("Orientations, list of=1 TOrientGroup", vc0_single)
        shd_single = vc0_single.split("PVObject_Ombrage=pvShading", 1)[1]
        self.assertEqual(shd_single.count("NoOrient=1"), 12)

        # With tolerance=5.0 deg and 3 inverters (140 kW -> 3 x Inv50): 3 dedicated Orientations + SubArrays
        multi_ded = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("PvcMultiDed.PRJ", scope="ground_sheds", target_kw=140.0,
                              ground_layout={"pvc_name": "MultiFixed.pvc", "pvc_orientation_tolerance_deg": 5.0}),
        )
        self.assertIn(multi_ded["status"], ("prepared", "needs_review"), multi_ded)
        struct_ded = self.store.validate_structure("PvcMultiDed.PRJ", "VC0")
        self.assertTrue(struct_ded["valid"], struct_ded)
        self.assertEqual(struct_ded["orientation_ids"], [1, 2, 3])
        self.assertEqual(struct_ded["subarray_ids"], [1, 2, 3])
        vc0_ded = (self.ws / "Projects" / "PvcMultiDed.VC0").read_text(encoding="utf-8-sig")
        self.assertIn("Orientations, list of=3 TOrientGroup", vc0_ded)
        shd_ded = vc0_ded.split("PVObject_Ombrage=pvShading", 1)[1]
        self.assertEqual(shd_ded.count("NoOrient=1"), 4)
        self.assertEqual(shd_ded.count("NoOrient=2"), 4)
        self.assertEqual(shd_ded.count("NoOrient=3"), 4)
        self.assertEqual(multi_ded["utility_summary"]["cad_import"]["orientation_count"], 3)
        self.assertEqual(len(multi_ded["utility_summary"]["sub_orientations"]), 3)

        # Shared multi-MPPT inverter apportionment (1 inverter, 4 MPPT inputs -> PowerSharingInverterId=0 across K=3)
        multi_shared = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("PvcMultiShr.PRJ", scope="ground_sheds", target_kw=52.8,
                              ond_name="Inv50_4MPPT.OND",
                              ground_layout={"pvc_name": "MixedMultiFixed.pvc", "pvc_subset": "fixed", "pvc_orientation_tolerance_deg": 5.0}),
        )
        self.assertIn(multi_shared["status"], ("prepared", "needs_review"), multi_shared)
        struct_shr = self.store.validate_structure("PvcMultiShr.PRJ", "VC0")
        self.assertTrue(struct_shr["valid"], struct_shr)
        vc0_shr = (self.ws / "Projects" / "PvcMultiShr.VC0").read_text(encoding="utf-8-sig")
        self.assertIn("Orientations, list of=3 TOrientGroup", vc0_shr)
        self.assertIn("PowerSharingInverterId=0", vc0_shr)
        # Largest South cluster gets 2 of 4 MPPT inputs (50%), West & East get 1 each (25% each)
        mppts = [s["mppt_count"] for s in multi_shared["utility_summary"]["sub_orientations"]]
        self.assertEqual(mppts, [2, 1, 1])

        # Infeasible 60/40/20 fixed cluster split (max feasible strings = 4+2+1 = 7 < 8) is blocked cleanly with NO .VC0 written
        self._write_mixed_pvc(
            "InfeasibleMultiFixed.pvc",
            fixed_specs=[
                (0.0, 0.0, 22.0, 0.0, 2, 15), (0.0, 10.0, 23.0, 2.0, 2, 15),
                (25.0, 0.0, 15.0, 90.0, 2, 10), (25.0, 10.0, 16.0, 88.0, 2, 10),
                (45.0, 0.0, 14.0, -90.0, 2, 10),
            ],
        )
        blocked_cap = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("InfeasiblePvc.PRJ", scope="ground_sheds", target_kw=52.8,
                              ond_name="Inv50_4MPPT.OND",
                              ground_layout={"pvc_name": "InfeasibleMultiFixed.pvc", "pvc_subset": "fixed", "pvc_orientation_tolerance_deg": 5.0}),
        )
        self.assertEqual(blocked_cap["status"], "blocked", blocked_cap)
        self.assertIn("collector area", str(blocked_cap["issues"]).lower())
        self.assertFalse((self.ws / "Projects" / "InfeasiblePvc.VC0").exists())

        # Capacitated apportionment shifts allocation away from a capped bucket while keeping area limits satisfied
        cap_clusters = [
            {"orientation_id": 1, "module_count": 100, "collector_area_m2": 70.0},
            {"orientation_id": 2, "module_count": 60, "collector_area_m2": 125.0},
            {"orientation_id": 3, "module_count": 30, "collector_area_m2": 65.0},
        ]
        unconstrained = apportion_pvc_orientations(
            cap_clusters, n_mod_serie=15, n_string_ch=8, n_inverter=1, n_inv_mppt=4, independent_mppt=True,
        )
        self.assertEqual([a["n_inv_mppt"] for a in unconstrained], [2, 1, 1])
        cap_alloc = apportion_pvc_orientations(
            cap_clusters, n_mod_serie=15, n_string_ch=8, n_inverter=1, n_inv_mppt=4, independent_mppt=True, module_area_m2=2.0,
        )
        self.assertEqual([a["n_inv_mppt"] for a in cap_alloc], [1, 2, 1])

        # Clustering performance: 200 jittered fixed tables complete into 1 orientation well under 1.0s
        jitter_tables = [
            {
                "name": f"J{i}", "tilt_deg": 20.0, "azimuth_deg": i * 0.001, "base_slope_deg": 0.0,
                "long_shed_m": 10.0, "larg_shed_m": 4.0, "origin_m": [0.0, i * 6.0, 0.8], "module_count": 20,
            }
            for i in range(200)
        ]
        t0 = time.perf_counter()
        _, j_sum = cluster_fixed_pvc_orientations(jitter_tables, 5.0)
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(j_sum), 1)
        self.assertLess(elapsed, 1.0)

        # Apportion impossibility check (1 inverter with 1 MPPT and 3 orientations)
        with self.assertRaisesRegex(ValueError, "Cannot apportion 3 .pvc orientations"):
            apportion_pvc_orientations(
                [{"orientation_id": 1, "module_count": 40}, {"orientation_id": 2, "module_count": 30}, {"orientation_id": 3, "module_count": 30}],
                n_mod_serie=15, n_string_ch=4, n_inverter=1, n_inv_mppt=1, independent_mppt=False,
            )

        # Tolerance validation bounds + strict schema checks
        for bad_tol in (0.1, 45.0):
            blk = prepare_design_project(
                self.cli, self.store, dry_run=True,
                spec=_design_spec("BadTol.PRJ", scope="ground_sheds", target_kw=52.8,
                                  ground_layout={"pvc_name": "MultiFixed.pvc", "pvc_orientation_tolerance_deg": bad_tol}),
            )
            self.assertEqual(blk["status"], "blocked")
        gl_ok = GroundLayoutRequest(pvc_name="MixedMultiFixed.pvc", pvc_subset="fixed", pvc_orientation_tolerance_deg=5.0)
        self.assertEqual(gl_ok.pvc_subset, "fixed")
        with self.assertRaises(ValidationError):
            GroundLayoutRequest(pvc_name="x.pvc", pvc_subset="dome")

    def test_report_2d_and_3d_layout_maps_and_vcx_auto_extraction(self) -> None:
        csv_path = _write_short_sfi_csv(self.ws / "Results" / "site_sim.csv")

        # 1. Backward compatibility: no layout_preview and no project/variant keeps chart_count untouched
        base_res = generate_report(
            workspace=self.ws, store=self.store, title="Baseline No Map", output_name="no_map.html",
            scenarios=[{"label": "Base", "csv_name": csv_path.name, "installed_dc_kw": 120.0}],
            language="en", plotlyjs="cdn", dry_run=False,
        )
        self.assertEqual(base_res["chart_count"], 2)
        self.assertFalse(base_res["has_layout_2d"])
        self.assertFalse(base_res["has_layout_3d"])

        # 2. Direct layout_preview (2D polygon + exclusion_zones + tables + 3D obstacles + terrain_points) in zh and en
        lp = {
            "site_polygon": [[0, 0], [80, 0], [80, 50], [0, 50]],
            "exclusion_zones": [{"name": "SubstationPad", "polygon": [[65, 5], [76, 5], [76, 15], [65, 15]]}],
            "tables": [
                {"kind": "fixed", "name": "S1", "origin_m": [20, 12, 1.0], "long_shed_m": 18.0, "larg_shed_m": 4.0, "tilt_deg": 22.0, "azimuth_deg": 0.0, "orientation_id": 1, "module_count": 36},
                {"kind": "fixed", "name": "W1", "origin_m": [48, 25, 1.5], "long_shed_m": 15.0, "larg_shed_m": 4.0, "tilt_deg": 15.0, "azimuth_deg": 90.0, "orientation_id": 2, "module_count": 30},
            ],
            "obstacles": [
                {"kind": "building", "name": "InverterHouse", "origin_m": [5, 35, 0], "length_m": 6, "width_m": 4, "height_m": 4.5, "rotation_deg": 0},
                {"kind": "tree", "name": "Oak1", "origin_m": [10, 8, 0], "top_height_m": 8, "middle_height_m": 5, "bottom_height_m": 2, "trunk_height_m": 2, "crown_diameter_m": 4, "trunk_diameter_m": 0.4},
            ],
            "terrain_points": [[0, 0, 0.0], [80, 0, 1.5], [80, 50, 3.0], [0, 50, 1.0], [40, 25, 1.8]],
        }
        zh_res = generate_report(
            workspace=self.ws, store=self.store, title="平面布置与三维地形报告", output_name="map_zh.html",
            scenarios=[{"label": "主方案", "csv_name": csv_path.name, "installed_dc_kw": 120.0}],
            layout_preview=lp, language="zh", plotlyjs="cdn", dry_run=False,
        )
        self.assertEqual(zh_res["chart_count"], 4)
        self.assertTrue(zh_res["has_layout_2d"])
        self.assertTrue(zh_res["has_layout_3d"])
        zh_html = Path(zh_res["report_path"]).read_text(encoding="utf-8")
        self.assertIn("场址总平面布置与阵列分布图 (2D)", zh_html)
        self.assertIn("三维地形与光伏阵列预览图 (3D)", zh_html)
        import json as _json
        for lbl_zh in ("场址边界", "避让/禁建区", "三维地形表面"):
            escaped = _json.dumps(lbl_zh)[1:-1].replace("/", "\\u002f")
            self.assertTrue(lbl_zh in zh_html or escaped in zh_html, lbl_zh)

        # Pure 2D without terrain/obstacles/include_3d omits 3D chart; include_3d=True enables it
        pure_2d = generate_report(
            workspace=self.ws, store=self.store, title="2D Only", output_name="map_2d_only.html",
            scenarios=[{"label": "2D", "csv_name": csv_path.name}],
            layout_preview={"site_polygon": [[0, 0], [30, 0], [30, 30], [0, 30]], "exclusion_zones": [[[2, 2], [6, 2], [6, 6], [2, 6]]]},
            include_3d_layout=False, language="en", dry_run=True,
        )
        self.assertTrue(pure_2d["has_layout_2d"])
        self.assertFalse(pure_2d["has_layout_3d"])

        # 3. Auto-extraction from saved VCx (multi-orientation fixed .pvc + terrain + obstacle) via MCP pvsyst_generate_report
        self._write_mixed_pvc("MixedReport.pvc", multi_fixed=True)
        prep_multi = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("ReportAutoPvc.PRJ", scope="ground_sheds", target_kw=52.8,
                              ond_name="Inv50_4MPPT.OND",
                              ground_layout={"pvc_name": "MixedReport.pvc", "pvc_subset": "fixed", "pvc_orientation_tolerance_deg": 5.0}),
        )
        self.assertIn(prep_multi["status"], ("prepared", "needs_review"), prep_multi)
        vc_text = (self.ws / "Projects" / "ReportAutoPvc.VC0").read_text(encoding="utf-8-sig")
        extracted = extract_vc_layout_preview(vc_text)
        self.assertIsNotNone(extracted)
        self.assertEqual(len(extracted["tables"]), 6)
        self.assertEqual(len(extracted["obstacles"]), 1)
        self.assertIn("terrain_mesh", extracted)

        fake_cli = self.ws / "PVsystCLI.exe"
        fake_cli.write_bytes(b"stub")
        with patch.dict(os.environ, {"PVSYST_WORKSPACE": str(self.ws), "PVSYST_CLI": str(fake_cli)}):
            mcp_rep = api_sim.pvsyst_generate_report(
                title="VC0 Auto Map Report",
                output_name="auto_vc0_en.html",
                scenarios=[ReportScenarioInput(
                    label="AutoVC0", csv_name=csv_path.name,
                    project="ReportAutoPvc.PRJ", variant="VC0",
                    installed_dc_kw=52.8, include_3d_layout=True,
                )],
                language="en", plotlyjs="cdn",
            )
        self.assertEqual(mcp_rep["status"], "generated")
        self.assertEqual(mcp_rep["chart_count"], 4)
        self.assertTrue(mcp_rep["has_layout_2d"])
        self.assertTrue(mcp_rep["has_layout_3d"])
        en_html = Path(mcp_rep["report_path"]).read_text(encoding="utf-8")
        self.assertIn("Site Plan-View &amp; PV Array Layout (2D)", en_html)
        self.assertIn("3D Terrain &amp; PV Array Preview (3D)", en_html)
        self.assertIn("Orient #1", en_html)
        self.assertIn("3D Terrain Surface", en_html)

        # 4. Auto-extraction on saved Tracker VC0 and Dome VC0 (both parse cleanly via extract_vc_layout_preview)
        prep_trk = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("ReportAutoTrk.PRJ", scope="ground_tracker", target_kw=52.8,
                              ground_layout={"pvc_name": "MixedReport.pvc", "pvc_subset": "tracker"}),
        )
        self.assertIn(prep_trk["status"], ("prepared", "needs_review"), prep_trk)
        trk_ext = extract_vc_layout_preview((self.ws / "Projects" / "ReportAutoTrk.VC0").read_text(encoding="utf-8-sig"))
        self.assertEqual(len(trk_ext["tables"]), 6)
        self.assertEqual(trk_ext["tables"][0]["kind"], "tracker")

        prep_dome = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("ReportAutoDome.PRJ", scope="ground_dome", target_kw=52.8, tilt=10.0, azim=90.0,
                              ground_layout={"model_mode": "3d", "pitch_m": 6.0, "shed_count": 3, "module_rows": 1, "module_columns": 20}),
        )
        self.assertIn(prep_dome["status"], ("prepared", "needs_review"), prep_dome)
        dome_ext = extract_vc_layout_preview((self.ws / "Projects" / "ReportAutoDome.VC0").read_text(encoding="utf-8-sig"))
        self.assertEqual(len(dome_ext["tables"]), 6)  # 3 front + 3 back dome rows
        self.assertEqual({t["dome_face"] for t in dome_ext["tables"]}, {"front", "back"})

        # 5. Saved VC0 with empty pvShading must NOT emit layout sections
        (self.ws / "Projects" / "EmptyShd.VC0").write_text(
            "PVObject_=pvVCalcul\n  Flags=$00100001\n  PVObject_Ombrage=pvShading\n    Flags=$00080000\n  End of PVObject pvShading\nEnd of PVObject pvVCalcul\n",
            encoding="utf-8",
        )
        self.assertIsNone(extract_vc_layout_preview((self.ws / "Projects" / "EmptyShd.VC0").read_text(encoding="utf-8")))

        # 6. Fixed-table geometry roundtrip preserves [-proj, 0] local Y extent (matches Collada source footprint)
        g_fix, i_fix = _fixed_geom_and_inst("SouthOne", cx=0.0, cy=0.0, z_base=0.8, tilt_deg=20.0, azimuth_deg=0.0, n_rows=4, n_cols=25)
        (self.ws / "UserData" / "SingleFixedGeom.pvc").write_text(_wrap_pvc_xml([g_fix], [i_fix], include_obs_terrain=False), encoding="utf-8")
        prep_one = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec=_design_spec("SingleGeom.PRJ", scope="ground_sheds", target_kw=44.0,
                              ground_layout={"pvc_name": "SingleFixedGeom.pvc"}),
        )
        self.assertIn(prep_one["status"], ("prepared", "needs_review"), prep_one)
        ext_one = extract_vc_layout_preview((self.ws / "Projects" / "SingleGeom.VC0").read_text(encoding="utf-8-sig"))
        self.assertEqual(len(ext_one["tables"]), 1)
        corners = _table_corners_3d(ext_one["tables"][0])
        ys = [c[1] for c in corners]
        zs = [c[2] for c in corners]
        proj_exp = (4 * 1.996 + 3 * 0.02) * math.cos(math.radians(20.0))
        rise_exp = (4 * 1.996 + 3 * 0.02) * math.sin(math.radians(20.0))
        self.assertAlmostEqual(min(ys), -proj_exp, places=2)
        self.assertAlmostEqual(max(ys), 0.0, places=2)
        self.assertAlmostEqual(min(zs), 0.8, places=2)
        self.assertAlmostEqual(max(zs), 0.8 + rise_exp, places=2)

        # 7. pvShdGenObject and pvShdGround vertices honor DAzimut and DInclin rotations
        vc_rot = "\n".join([
            "PVObject_=pvVCalcul",
            "  Flags=$00100001",
            "  PVObject_Ombrage=pvShading",
            "    Flags=$000A0100",
            "    ListeObjets, list of=2 TShdObject",
            "      PVObject_1=pvShdGenObject",
            "        Comment=RotatedObs",
            "        DOrigine=10.000000,20.000000,1.000000",
            "        DInclin=30.000000",
            "        BaseSlope=0.000000",
            "        DAzimut=90.000000",
            "        Sommets, TPoly3D=3 Point3D",
            "          Point_1=0.000000,0.000000,0.000000",
            "          Point_2=4.000000,0.000000,0.000000",
            "          Point_3=0.000000,2.000000,0.000000",
            "        End of TPoly3D",
            "        Faces=-1,0,1,2,",
            "      End of PVObject pvShdGenObject",
            "      PVObject_2=pvShdGround",
            "        Comment=RotatedGround",
            "        DOrigine=0.000000,0.000000,0.000000",
            "        DInclin=0.000000",
            "        BaseSlope=0.000000",
            "        DAzimut=90.000000",
            "        Sommets, TPoly3D=3 Point3D",
            "          Point_1=0.000000,0.000000,0.000000",
            "          Point_2=4.000000,0.000000,0.000000",
            "          Point_3=0.000000,2.000000,0.000000",
            "        End of TPoly3D",
            "        Faces=-1,0,1,2,",
            "      End of PVObject pvShdGround",
            "    End of ListeObjets",
            "  End of PVObject pvShading",
            "End of PVObject pvVCalcul",
        ])
        rot_ext = extract_vc_layout_preview(vc_rot)
        obs_verts = rot_ext["obstacles"][0]["vertices"]
        # At DAzimut=90, DInclin=30: local (4,0,0) -> world (10, 16, 1); local (0,2,0) -> world (10 + 2*cos(30), 20, 1 - 2*sin(30))
        self.assertAlmostEqual(obs_verts[1][0], 10.0, places=4)
        self.assertAlmostEqual(obs_verts[1][1], 16.0, places=4)
        self.assertAlmostEqual(obs_verts[1][2], 1.0, places=4)
        self.assertAlmostEqual(obs_verts[2][0], 10.0 + math.sqrt(3.0), places=4)
        self.assertAlmostEqual(obs_verts[2][1], 20.0, places=4)
        self.assertAlmostEqual(obs_verts[2][2], 0.0, places=4)
        gnd_verts = rot_ext["terrain_mesh"]["vertices"]
        self.assertAlmostEqual(gnd_verts[1][0], 0.0, places=4)
        self.assertAlmostEqual(gnd_verts[1][1], -4.0, places=4)
        self.assertAlmostEqual(gnd_verts[2][0], 2.0, places=4)
        self.assertAlmostEqual(gnd_verts[2][1], 0.0, places=4)

    def test_sloped_table_corners_preserve_3d_dimensions(self) -> None:
        def _assert_rect(corners: list[tuple[float, float, float]], w_exp: float, l_exp: float) -> None:
            self.assertAlmostEqual(math.dist(corners[0], corners[1]), w_exp, places=6)
            self.assertAlmostEqual(math.dist(corners[2], corners[3]), w_exp, places=6)
            self.assertAlmostEqual(math.dist(corners[1], corners[2]), l_exp, places=6)
            self.assertAlmostEqual(math.dist(corners[3], corners[0]), l_exp, places=6)
            for idx_corner in range(4):
                p_prev = corners[(idx_corner - 1) % 4]
                p_curr = corners[idx_corner]
                p_next = corners[(idx_corner + 1) % 4]
                e1 = tuple(p_next[a] - p_curr[a] for a in range(3))
                e2 = tuple(p_prev[a] - p_curr[a] for a in range(3))
                self.assertAlmostEqual(sum(e1[a] * e2[a] for a in range(3)), 0.0, places=6)

        # 1. Sloped tracker (type_origine=7 centered, type_origine=2 anchored on start edge)
        trk_centered = _table_corners_3d({
            "kind": "tracker", "origin_m": [10.0, 20.0, 5.0],
            "long_shed_m": 2.2, "larg_shed_m": 100.0,
            "axis_tilt_deg": 30.0, "axis_azimuth_deg": 0.0, "type_origine": 7,
        })
        _assert_rect(trk_centered, 2.2, 100.0)
        ys = [c[1] for c in trk_centered]
        zs = [c[2] for c in trk_centered]
        self.assertAlmostEqual(max(ys) - min(ys), 100.0 * math.cos(math.radians(30.0)), places=6)
        self.assertAlmostEqual(max(zs) - min(zs), 50.0, places=6)
        for ax, exp in enumerate((10.0, 20.0, 5.0)):
            self.assertAlmostEqual(sum(c[ax] for c in trk_centered) / 4.0, exp, places=6)

        trk_start = _table_corners_3d({
            "kind": "tracker", "origin_m": [10.0, 20.0, 5.0],
            "long_shed_m": 2.2, "larg_shed_m": 100.0,
            "axis_tilt_deg": 30.0, "axis_azimuth_deg": 35.0, "type_origine": 2,
        })
        _assert_rect(trk_start, 2.2, 100.0)
        for ax, exp in enumerate((10.0, 20.0, 5.0)):
            self.assertAlmostEqual(0.5 * (trk_start[0][ax] + trk_start[1][ax]), exp, places=6)

        # 2. Sloped fixed table (BaseSlope-only + combined tilt/slope/azimuth for TypeOrigine 1/2/7)
        fix_flat_slope = _table_corners_3d({
            "kind": "fixed", "origin_m": [0.0, 0.0, 1.0],
            "long_shed_m": 100.0, "larg_shed_m": 4.0,
            "tilt_deg": 0.0, "base_slope_deg": 30.0, "azimuth_deg": 0.0, "type_origine": 2,
        })
        _assert_rect(fix_flat_slope, 100.0, 4.0)
        xs = [c[0] for c in fix_flat_slope]
        zs_f = [c[2] for c in fix_flat_slope]
        self.assertAlmostEqual(max(xs) - min(xs), 100.0 * math.cos(math.radians(30.0)), places=6)
        self.assertAlmostEqual(max(zs_f) - min(zs_f), 50.0, places=6)

        orig = (5.0, -3.0, 2.0)
        for type_orig in (1, 2, 7):
            corners = _table_corners_3d({
                "kind": "fixed", "origin_m": list(orig),
                "long_shed_m": 20.0, "larg_shed_m": 4.0,
                "tilt_deg": 25.0, "base_slope_deg": 20.0, "azimuth_deg": 35.0, "type_origine": type_orig,
            })
            _assert_rect(corners, 20.0, 4.0)
            if type_orig == 1:
                for ax, exp in enumerate(orig):
                    self.assertAlmostEqual(corners[1][ax], exp, places=6)
            elif type_orig == 2:
                for ax, exp in enumerate(orig):
                    self.assertAlmostEqual(0.5 * (corners[0][ax] + corners[1][ax]), exp, places=6)
            else:
                for ax, exp in enumerate(orig):
                    self.assertAlmostEqual(sum(c[ax] for c in corners) / 4.0, exp, places=6)

        # 3. Sloped dome (front/back preserve dimensions + orthogonal corners + 0.2m equal-Z ridge gap)
        dome_front = _table_corners_3d({
            "kind": "dome", "dome_face": "front", "origin_m": [1.0, 2.0, 0.5],
            "long_shed_m": 20.0, "larg_shed_m": 3.0,
            "tilt_deg": 10.0, "base_slope_deg": 15.0, "azimuth_deg": 90.0, "top_spacing_m": 0.2,
        })
        dome_back = _table_corners_3d({
            "kind": "dome", "dome_face": "back", "origin_m": [1.0, 2.0, 0.5],
            "long_shed_m": 20.0, "larg_shed_m": 3.0,
            "tilt_deg": 10.0, "base_slope_deg": 15.0, "azimuth_deg": 90.0, "top_spacing_m": 0.2,
        })
        _assert_rect(dome_front, 20.0, 3.0)
        _assert_rect(dome_back, 20.0, 3.0)
        self.assertAlmostEqual(math.dist(dome_front[3], dome_back[0]), 0.2, places=6)
        self.assertAlmostEqual(math.dist(dome_front[2], dome_back[1]), 0.2, places=6)
        self.assertAlmostEqual(dome_front[3][2], dome_back[0][2], places=6)
        self.assertAlmostEqual(dome_front[2][2], dome_back[1][2], places=6)


if __name__ == "__main__":
    unittest.main()

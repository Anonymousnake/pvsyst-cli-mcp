"""Unit tests for PR 3: 3D tracker polygon clipping (integer string multiples), obstacles, and terrain."""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.api.context import TOOL_GROUPS
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.template import synthesize_utility_project
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.tracker_layout import (
    build_tracker_3d_shading,
    plan_tracker_polygon_layout,
)
from pvsyst_auto.workflows.design import prepare_design_project, simulate_design
from pvsyst_auto.workflows.utility import resolve_utility_field_type, validate_utility_spec
from tests._fixtures import init_full_workspace
from tests.unit.core.test_bifacial_utility import UTILITY_PAN, UTILITY_OND, _FakeUtilityCLI, _capacity_spec
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


def _tracker_dem_csv(path: Path) -> str:
    rows = ["x,y,z"]
    for x in (-120.0, 0.0, 120.0):
        for y in (-120.0, 0.0, 120.0):
            # Slope along both E-W and N-S so axis_tilt != 0
            rows.append(f"{x:.1f},{y:.1f},{0.015 * x + 0.025 * y + 5.0:.2f}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path.name


class Tracker3DPolygonTests(unittest.TestCase):
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

    def test_tool_budget_preserved(self) -> None:
        all_tools = {name for group in TOOL_GROUPS.values() for name in group}
        default_tools = all_tools - set(TOOL_GROUPS["Admin & Diagnostics"]) - set(TOOL_GROUPS["Expert & Low-Level"])
        self.assertEqual(len(default_tools), 48)
        self.assertEqual(len(all_tools), 75)

    def test_tracker_partition_consistency_and_whole_string_contract(self) -> None:
        _tracker_dem_csv(self.ws / "UserData" / "trk_dem.csv")
        common = dict(
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            field_type="TrackHorizNSAxis",
            pan_name="Bifi440.PAN",
            ond_name="Inv50.OND",
            n_mod_serie=10,
            n_string_ch=8,
            n_inverter=1,
            n_inv_mppt=2,
            independent_mppt=True,
            pnom_ac_in_mppt=25.0,
            pitch_m=8.0,
            module_rows=2,
            module_width_m=1.0,
            module_length_m=2.0,
            spacing_x_m=0.02,
            spacing_y_m=0.04,
        )
        reg_vc = synthesize_utility_project(self.store, shed_count=4, module_columns=10, **common)["vc0_text"]
        ter_vc = synthesize_utility_project(
            self.store, shed_count=4, module_columns=10, terrain={"csv_name": "trk_dem.csv"}, **common
        )["vc0_text"]
        poly_vc = synthesize_utility_project(
            self.store, site_polygon=[[0.0, 0.0], [30.0, 0.0], [30.0, 25.0], [0.0, 25.0]], **common
        )["vc0_text"]
        for vc in (reg_vc, ter_vc, poly_vc):
            self.assertIn("LongShed=4.040000", vc)
            self.assertIn("DXModule=2.040", vc)
            self.assertNotIn("DXModule=1.020", vc)

        # Whole-string divisibility via external synthesis contract for 2P coprime (n_mod_serie=15) & even (n_mod_serie=12)
        odd_vc = synthesize_utility_project(
            self.store,
            site_polygon=[[0.0, 0.0], [28.0, 0.0], [28.0, 40.0], [0.0, 22.0]],
            **{**common, "n_mod_serie": 15},
        )["vc0_text"]
        odd_widths = [int(w) for w in re.findall(r"NModInWidth=(\d+)", odd_vc)]
        self.assertGreater(len(set(odd_widths)), 1, odd_widths)
        self.assertTrue(all(w % 15 == 0 and (2 * w) % 15 == 0 for w in odd_widths), odd_widths)

        even_vc = synthesize_utility_project(
            self.store,
            site_polygon=[[0.0, 0.0], [28.0, 0.0], [28.0, 40.0], [0.0, 22.0]],
            **{**common, "n_mod_serie": 12},
        )["vc0_text"]
        even_widths = [int(w) for w in re.findall(r"NModInWidth=(\d+)", even_vc)]
        self.assertGreater(len(set(even_widths)), 1, even_widths)
        self.assertTrue(all(w % 6 == 0 and (2 * w) % 12 == 0 for w in even_widths), even_widths)

        # Invalid module_rows rejection via external prepare_design_project contract
        cli = _FakeUtilityCLI(self.ws, year=2024, nominal_dc_kw=44.0, use_sfi_writer=False)
        bad_rows = prepare_design_project(
            cli,
            self.store,
            dry_run=True,
            spec={
                "scope": "ground_tracker",
                "new_project": "BadRows.PRJ",
                "year": 2024,
                "target_dc_kw": 44.0,
                "azimuth": 0.0,
                "module": {"file_name": "Bifi440.PAN"},
                "inverter": {"file_name": "Inv50.OND"},
                "capacity": _capacity_spec(target_dc_kw=44.0),
                "ground_layout": {
                    "pitch_m": 6.0,
                    "module_rows": 0,
                    "site_polygon": [[0.0, 0.0], [30.0, 0.0], [30.0, 30.0], [0.0, 30.0]],
                },
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            },
        )
        self.assertEqual(bad_rows["status"], "blocked")
        self.assertTrue(any("module_rows" in str(item) for item in bad_rows["issues"]), bad_rows["issues"])

    def test_irregular_polygon_clipping_1p_and_2p_preserves_whole_strings(self) -> None:
        # Irregular tapered trapezoid wider at east than west -> produces varying tracker lengths
        trap = [[0.0, 20.0], [38.0, 0.0], [38.0, 80.0], [0.0, 60.0]]
        plan_1p = plan_tracker_polygon_layout(
            trap, axis_azimuth_deg=0.0, pitch_ew_m=6.0,
            module_width_m=1.032, module_length_m=1.996, module_rows=1,
            module_orientation="portrait", spacing_x_m=0.02, spacing_y_m=0.02,
            setback_m=1.0, ground_clearance_m=1.5, n_mod_serie=10,
        )
        cols_1p = [t["module_columns"] for t in plan_1p["tables"]]
        self.assertGreater(len(set(cols_1p)), 1, f"Expected variable row lengths, got {cols_1p}")
        for tbl in plan_1p["tables"]:
            self.assertEqual(tbl["module_count"] % 10, 0)
            self.assertEqual(tbl["module_columns"] % 10, 0)
        self.assertEqual(plan_1p["total_strings"], plan_1p["total_modules"] // 10)

        # 2P portrait with even n_mod_serie=12 -> columns multiple of 6 (12 modules per string)
        plan_2p = plan_tracker_polygon_layout(
            [[0.0, 0.0], [45.0, 0.0], [45.0, 60.0], [0.0, 60.0]],
            axis_azimuth_deg=15.0, pitch_ew_m=9.0,
            module_width_m=1.032, module_length_m=1.996, module_rows=2,
            module_orientation="portrait", spacing_x_m=0.02, spacing_y_m=0.02,
            setback_m=1.0, max_table_columns=30, n_mod_serie=12,
        )
        self.assertEqual(plan_2p["columns_per_string"], 6)
        for tbl in plan_2p["tables"]:
            self.assertEqual(tbl["module_count"] % 12, 0)
            self.assertLessEqual(tbl["module_columns"], 30)

        # Guards: pitch <= chord, max_table_columns < quantum, empty fit
        with self.assertRaisesRegex(ValueError, "chord width"):
            plan_tracker_polygon_layout(
                trap, pitch_ew_m=1.5, module_width_m=1.032, module_length_m=1.996, module_rows=1, n_mod_serie=10,
            )
        with self.assertRaisesRegex(ValueError, "smaller than one whole string quantum"):
            plan_tracker_polygon_layout(
                trap, pitch_ew_m=6.0, module_width_m=1.032, module_length_m=1.996,
                module_rows=1, max_table_columns=5, n_mod_serie=10,
            )
        with self.assertRaisesRegex(ValueError, "No integer-string tracker tables fit"):
            plan_tracker_polygon_layout(
                [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
                pitch_ew_m=6.0, module_width_m=1.032, module_length_m=1.996, module_rows=1, n_mod_serie=20,
            )

    def test_build_tracker_3d_shading_with_polygon_obstacles_and_terrain(self) -> None:
        from pvsyst_auto.scene.terrain import build_terrain_mesh
        _tracker_dem_csv(self.ws / "UserData" / "trk_dem.csv")
        mesh = build_terrain_mesh({"csv_name": "trk_dem.csv"}, workspace=self.ws)
        trap = [[0.0, 20.0], [38.0, 0.0], [38.0, 80.0], [0.0, 60.0]]
        plan = plan_tracker_polygon_layout(
            trap, axis_azimuth_deg=0.0, pitch_ew_m=6.0,
            module_width_m=1.032, module_length_m=1.996, module_rows=1, n_mod_serie=10, setback_m=1.0,
        )
        obs = [{
            "kind": "tree", "name": "Pine1", "origin_m": [-20.0, 40.0, 0.0],
            "top_height_m": 6.0, "middle_height_m": 4.0, "bottom_height_m": 2.0,
            "trunk_height_m": 2.0, "crown_diameter_m": 3.0, "trunk_diameter_m": 0.4,
        }]
        lines, terrain_sum = build_tracker_3d_shading(
            plan["tables"], module_name="440W", module_width_m=1.032, module_length_m=1.996,
            ground_clearance_m=1.6, obstacles=obs, terrain_mesh=mesh, terrain_opts={"follow_slope": True},
        )
        text = "\n".join(lines)
        self.assertEqual(text.count("=pvShdArrayTracking"), len(plan["tables"]))
        self.assertIn("TypeOrigine=7", text)
        self.assertIn("=pvShdGround", text)
        self.assertIn("ObjetElemType=oeTree", text)
        self.assertIsNotNone(terrain_sum)
        assert terrain_sum is not None
        self.assertEqual(terrain_sum["tracker_count"], len(plan["tables"]))
        self.assertTrue(all(t["axis_tilt_deg"] > 0.5 for t in terrain_sum["trackers"]))

    def test_ground_tracker_workflow_end_to_end_polygon_obstacles_and_mismatch(self) -> None:
        _tracker_dem_csv(self.ws / "UserData" / "trk_dem.csv")
        obs = [{
            "kind": "wall", "name": "PerimeterWall", "start_m": [-10.0, -5.0, 0.0],
            "end_m": [10.0, -5.0, 0.0], "height_m": 2.5, "thickness_m": 0.3,
        }]
        self.assertEqual(resolve_utility_field_type("ground_tracker", {"obstacles": obs}), ("TrackHorizNSAxis", True))
        self.assertEqual(resolve_utility_field_type("ground_tracker", {"site_polygon": [[0, 0], [30, 0], [30, 40], [0, 40]]}), ("TrackHorizNSAxis", True))

        # Explicit model_mode='unlimited' combined with polygon/obstacles must fail validation
        bad_issues: list[dict] = []
        validate_utility_spec(
            {"scope": "ground_tracker", "ground_layout": {"model_mode": "unlimited", "obstacles": obs}},
            "ground_tracker", bad_issues,
        )
        self.assertTrue(any(i["field"] == "ground_layout.model_mode" for i in bad_issues))

        # Build trapezoid polygon whose 1P 10-module-string count equals plan_capacity module_count for 88.0 kW (200 mods)
        # First check exact rectangle/trapezoid giving 200 modules (e.g., rows with 20, 30, 40, 50, 60 = 200 or similar)
        trap = [[0.0, 0.0], [30.0, 0.0], [30.0, 65.0], [0.0, 45.0]]
        preview = plan_tracker_polygon_layout(
            trap, axis_azimuth_deg=0.0, pitch_ew_m=6.0,
            module_width_m=1.032, module_length_m=1.996, module_rows=1,
            spacing_x_m=0.02, spacing_y_m=0.02, setback_m=1.0, n_mod_serie=10,
        )
        # Adjust a rectangle/polygon to an exact target if needed; test mismatch guard first with mismatched kW
        cli = _FakeUtilityCLI(self.ws, year=2024, nominal_dc_kw=88.0, use_sfi_writer=False)
        mismatch = prepare_design_project(
            cli, self.store, dry_run=True,
            spec={
                "scope": "ground_tracker", "new_project": "TrkMismatch.PRJ", "year": 2024,
                "target_dc_kw": 44.0, "azimuth": 0.0,
                "module": {"file_name": "Bifi440.PAN"}, "inverter": {"file_name": "Inv50.OND"},
                "capacity": _capacity_spec(target_dc_kw=44.0),
                "ground_layout": {
                    "pitch_m": 6.0, "module_rows": 1, "setback_m": 1.0,
                    "site_polygon": trap, "obstacles": obs,
                },
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            },
        )
        self.assertEqual(mismatch["status"], "blocked")
        self.assertIn("disagrees with electrical module count", str(mismatch["issues"]))

        # Irregular trapezoid yielding variable-length integer-string rows (cols=[28, 28, 42, 42], 140 mods = 61.6 kW)
        irreg = [[0.0, 0.0], [26.0, 0.0], [26.0, 62.0], [0.0, 36.0]]
        pl = plan_tracker_polygon_layout(
            irreg, axis_azimuth_deg=0.0, pitch_ew_m=6.0,
            module_width_m=1.032, module_length_m=1.996, module_rows=1,
            spacing_x_m=0.02, spacing_y_m=0.02, setback_m=1.0, n_mod_serie=14,
        )
        self.assertEqual([t["module_columns"] for t in pl["tables"]], [28, 28, 42, 42])
        self.assertEqual(pl["total_modules"], 140)
        target_kw = round(pl["total_modules"] * 0.44, 3)
        self.assertEqual(target_kw, 61.6)

        spec_ok = {
            "scope": "ground_tracker", "new_project": "TrkPolyOk.PRJ", "year": 2024,
            "target_dc_kw": target_kw, "azimuth": 0.0,
            "module": {"file_name": "Bifi440.PAN"}, "inverter": {"file_name": "Inv50.OND"},
            "capacity": _capacity_spec(target_dc_kw=target_kw),
            "ground_layout": {
                "pitch_m": 6.0, "module_rows": 1, "setback_m": 1.0, "ground_clearance_m": 1.5,
                "site_polygon": irreg, "obstacles": obs,
                "terrain": {"csv_name": "trk_dem.csv", "follow_slope": True},
            },
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
        }
        prep_ok = prepare_design_project(cli, self.store, spec=spec_ok, dry_run=False)
        self.assertIn(prep_ok["status"], ("prepared", "needs_review"), prep_ok)
        vc_txt = (self.ws / "Projects" / "TrkPolyOk.VC0").read_text(encoding="latin-1")
        self.assertEqual(vc_txt.count("=pvShdArrayTracking"), 4)
        self.assertIn("TypeOrigine=7", vc_txt)
        self.assertIn("NModInWidth=28", vc_txt)
        self.assertIn("NModInWidth=42", vc_txt)
        self.assertIn("=pvShdGround", vc_txt)
        self.assertIn("=pvShdGenObject", vc_txt)
        self.assertGreater(prep_ok["utility_summary"]["ground_3d_layout"]["tables"][0]["axis_tilt_deg"], 0.5)

        # Regular 3D tracker + obstacles (without site_polygon or terrain)
        reg_synth = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="TrackHorizNSAxis", pan_name="Bifi440.PAN", ond_name="Inv50.OND",
            n_mod_serie=10, n_string_ch=16, n_inverter=1, n_inv_mppt=2, independent_mppt=True,
            pnom_ac_in_mppt=25.0, pitch_m=6.0, shed_count=4, module_rows=2, module_columns=20,
            ground_clearance_m=1.6, obstacles=obs,
        )
        self.assertIn("ListeObjets, list of=2 TShdObject", reg_synth["vc0_text"])
        self.assertIn("=pvShdGenObject", reg_synth["vc0_text"])
        self.assertIn("0.000000,0.000000,1.600000", reg_synth["vc0_text"])

    def test_polygon_none_string_length_non_mutating_draping_rotated_azimuth_and_include_3d_guard(self) -> None:
        from pvsyst_auto.scene.reader import _point_in_polygon, validate_site_polygon
        from pvsyst_auto.scene.terrain import build_terrain_mesh

        poly = [[0.0, 0.0], [40.0, 0.0], [40.0, 60.0], [0.0, 60.0]]
        plan_none = plan_tracker_polygon_layout(
            poly, axis_azimuth_deg=15.0, pitch_ew_m=6.0,
            module_width_m=1.032, module_length_m=1.996, module_rows=1,
            spacing_x_m=0.02, spacing_y_m=0.02, setback_m=1.0, n_mod_serie=None,
        )
        self.assertIsNone(plan_none["total_strings"])
        self.assertTrue(all(t["string_count"] is None for t in plan_none["tables"]))
        poly_pts = validate_site_polygon(poly)
        self.assertTrue(all(_point_in_polygon(t["origin_m"][0], t["origin_m"][1], poly_pts) for t in plan_none["tables"]))

        _tracker_dem_csv(self.ws / "UserData" / "trk_dem.csv")
        mesh = build_terrain_mesh({"csv_name": "trk_dem.csv"}, workspace=self.ws)
        origins_before = [list(t["origin_m"]) for t in plan_none["tables"]]
        tilts_before = [t["axis_tilt_deg"] for t in plan_none["tables"]]
        _, tsum = build_tracker_3d_shading(
            plan_none["tables"], module_name="440W", module_width_m=1.032, module_length_m=1.996,
            ground_clearance_m=1.5, terrain_mesh=mesh, terrain_opts={"follow_slope": True},
        )
        self.assertEqual([list(t["origin_m"]) for t in plan_none["tables"]], origins_before)
        self.assertEqual([t["axis_tilt_deg"] for t in plan_none["tables"]], tilts_before)
        self.assertIsNotNone(tsum)
        assert tsum is not None
        self.assertNotEqual([list(t["origin_m"]) for t in tsum["trackers"]], origins_before)

        with self.assertRaisesRegex(ValueError, "include_3d_scene=True"):
            synthesize_utility_project(
                self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
                field_type="UnlimTracking", pan_name="Bifi440.PAN", ond_name="Inv50.OND",
                n_mod_serie=10, n_string_ch=4, n_inverter=1, n_inv_mppt=2, independent_mppt=True,
                pnom_ac_in_mppt=25.0, include_3d_scene=False, site_polygon=poly,
            )

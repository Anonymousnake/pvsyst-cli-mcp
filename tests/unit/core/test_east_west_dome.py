"""Unit tests for PR 2: East-West Dome orientation pairing, ground_dome workflows, and conversions."""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.api.context import TOOL_GROUPS
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.circuit import validate_structure
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.template import synthesize_utility_project
from pvsyst_auto.project.tracking import convert_orientation
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.dome import build_dome_3d_shading, dome_footprint_depth_m, opposite_azimuth_deg
from pvsyst_auto.workflows.design import prepare_design_project, simulate_design
from pvsyst_auto.workflows.utility import validate_utility_spec
from tests._fixtures import init_full_workspace
from tests.unit.core.test_bifacial_utility import (
    UTILITY_MONO_PAN,
    UTILITY_OND,
    _FakeUtilityCLI,
    _capacity_spec,
    _write_fake_annual_csv,
)
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


class _DomeFakeCLI(_FakeUtilityCLI):
    """Sums nominal DC across multi-subarray dual_mppt dome variants cleanly."""

    def run_simulation(self, project, variant, *, sfi=None, out_csv=None, report_pdf=None, **kwargs):
        self.runs.append({"project": project, "variant": variant, "sfi": sfi, "out_csv": out_csv, "report_pdf": report_pdf, **kwargs})
        vc_text = (self.workspace / "Projects" / f"{Path(project).stem}.{variant}").read_text(encoding="utf-8-sig")
        nom_kw = sum(
            int(ns) * int(nstr) * 440.0 / 1000.0
            for ns, nstr in re.findall(r"NModSerie=(\d+)\s+NStringCh=(\d+)", vc_text)
        )
        _write_fake_annual_csv(
            Path(out_csv), project=project, variant=variant,
            site=self.site, met=self.met, year=self.year, nominal_dc_kw=nom_kw, bifacial=False,
        )
        if report_pdf is not None:
            Path(report_pdf).write_bytes(b"%PDF-1.4\n%%EOF\n")
        return {"exit_code": 0, "warnings": [], "sfi_variables": Path(sfi).read_text(encoding="utf-8") if sfi else ""}


def _terrain_csv_for_dome(path: Path) -> str:
    rows = ["x,y,z"]
    for x in (-80.0, 0.0, 80.0):
        for y in (-80.0, 0.0, 80.0):
            rows.append(f"{x:.1f},{y:.1f},{0.02 * x + 5.0:.2f}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path.name


class EastWestDomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = init_full_workspace(Path(self._tmp.name))
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV" / "PVmodules" / "Mono440.PAN").write_text(UTILITY_MONO_PAN, encoding="utf-8")
        (self.ws / "ComposPV" / "Inverters" / "Inv50_2MPPT.OND").write_text(UTILITY_OND, encoding="utf-8")
        single_mppt_ond = UTILITY_OND.replace("NbMPPT=2", "NbMPPT=1")
        (self.ws / "ComposPV" / "Inverters" / "Inv50_1MPPT.OND").write_text(single_mppt_ond, encoding="utf-8")
        self.components = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.components)

    def test_tool_budget_preserved(self) -> None:
        all_tools = {name for group in TOOL_GROUPS.values() for name in group}
        default_tools = all_tools - set(TOOL_GROUPS["Admin & Diagnostics"]) - set(TOOL_GROUPS["Expert & Low-Level"])
        self.assertEqual(len(default_tools), 48)
        self.assertEqual(len(all_tools), 75)

    def test_opposite_azimuth_and_paired_dome_orientation_validation(self) -> None:
        self.assertEqual(opposite_azimuth_deg(90.0), -90.0)
        self.assertEqual(opposite_azimuth_deg(-70.0), 110.0)
        self.assertEqual(opposite_azimuth_deg(0.0), 180.0)
        self.assertEqual(opposite_azimuth_deg(180.0), 0.0)
        self.assertEqual(opposite_azimuth_deg(-180.0), 0.0)

        synth = synthesize_project_and_variant(
            self.store,
            project_comment="DomePair",
            variant_comment="PairCheck",
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            pan_name="Mono440.PAN",
            ond_name="Inv50_1MPPT.OND",
            orientations=[
                {"id": 1, "field_type": "Dome", "pair_orientation_id": 2, "tilt_deg": 10.0, "azimuth_deg": 90.0},
                {"id": 2, "field_type": "DomeBackfaces", "pair_orientation_id": 1, "tilt_deg": 10.0, "azimuth_deg": -90.0},
            ],
            subarrays=[{
                "subarray_id": 1, "orientation_id": 1, "orientation_id_2": 2, "n_str_orient_1": 5,
                "n_mod_serie": 20, "n_string_ch": 10, "n_inverter": 2, "n_inv_mppt": 1,
                "independent_mppt": False, "pnom_ac_in_mppt": 50.0,
            }],
        )
        self.store.create_project("DomePair.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
        status = self.store.validate_structure("DomePair.PRJ", "VC0")
        self.assertIs(status["valid"], True)
        self.assertTrue(status["checks_complete"])

        # Corrupt dome pair symmetry -> fails validation
        bad_vc = synth["vc0_text"].replace("IsMainOrient=False", "IsMainOrient=True")
        (self.ws / "Projects" / "DomePair.VC1").write_bytes(bad_vc.encode("utf-8"))
        bad_status = validate_structure(self.store, "DomePair.PRJ", "VC1")
        self.assertIs(bad_status["valid"], False)

        # Subarray referencing unknown NoOrientation2 -> fails validation
        bad_vc2 = synth["vc0_text"].replace("NoOrientation2=2", "NoOrientation2=9")
        (self.ws / "Projects" / "DomePair.VC2").write_bytes(bad_vc2.encode("utf-8"))
        bad_status2 = validate_structure(self.store, "DomePair.PRJ", "VC2")
        self.assertIs(bad_status2["valid"], False)

    def test_synthesize_dome_wiring_modes_and_3d_pairing(self) -> None:
        # 1) dual_mppt shared inverter (NbMPPT=2, NInverter=2, NStringCh=12 -> 3 strings per MPPT on each face)
        res_dual = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="Dome", pan_name="Mono440.PAN", ond_name="Inv50_2MPPT.OND",
            n_mod_serie=20, n_string_ch=12, n_inverter=2, n_inv_mppt=4,
            independent_mppt=True, pnom_ac_in_mppt=25.0,
            tilt_deg=10.0, azimuth_deg=90.0, pitch_m=6.0, top_spacing_m=0.25,
            shed_count=6, module_rows=1, module_columns=20, dome_wiring="dual_mppt",
        )
        vc_dual = res_dual["vc0_text"]
        self.assertEqual(res_dual["utility_summary"]["dome"]["dome_wiring"], "dual_mppt")
        self.assertEqual(res_dual["utility_summary"]["dome"]["back_azimuth_deg"], -90.0)
        self.assertIn("PVObject_=pvDomeOrient", vc_dual)
        self.assertIn("End of TDomeOrientGroup", vc_dual)
        self.assertIn("PVObject_1=pvShdArrayDome", vc_dual)
        self.assertIn("PVObject_2=pvShdArrayDome", vc_dual)
        self.assertIn("IsMainField=True", vc_dual)
        self.assertIn("IsMainField=False", vc_dual)
        self.assertIn("TopSpacing=0.250", vc_dual)
        self.assertIn("NoPairField=2", vc_dual)
        self.assertIn("NoPairField=1", vc_dual)
        # Both front and back 3D dome fields share front azimuth
        self.assertEqual(vc_dual.count("DAzimut=90.000000"), 2)
        self.assertIn("PowerSharingInverterId=0", vc_dual)
        self.assertIn("SubArrayId=2", vc_dual)
        self.store.create_project("DualTest.PRJ", res_dual["prj_text"], {"VC0": vc_dual})
        self.assertIs(self.store.validate_structure("DualTest.PRJ", "VC0")["valid"], True)

        # 2) auto falls back to mixed on single 1-MPPT inverter
        res_mixed = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="Dome", pan_name="Mono440.PAN", ond_name="Inv50_1MPPT.OND",
            n_mod_serie=20, n_string_ch=10, n_inverter=1, n_inv_mppt=1,
            independent_mppt=False, pnom_ac_in_mppt=50.0,
            tilt_deg=12.0, azimuth_deg=-70.0, pitch_m=6.0, top_spacing_m=0.2,
            shed_count=5, module_rows=1, module_columns=20, dome_wiring="auto",
        )
        vc_mixed = res_mixed["vc0_text"]
        self.assertEqual(res_mixed["utility_summary"]["dome"]["dome_wiring"], "mixed")
        self.assertEqual(res_mixed["utility_summary"]["dome"]["back_azimuth_deg"], 110.0)
        self.assertIn("NoOrientation2=2", vc_mixed)
        self.assertIn("NStrOrient1=5", vc_mixed)
        self.assertIn("Flags2=$0021", vc_mixed)
        self.store.create_project("MixedTest.PRJ", res_mixed["prj_text"], {"VC0": vc_mixed})
        self.assertIs(self.store.validate_structure("MixedTest.PRJ", "VC0")["valid"], True)

        # 3) dedicated-inverter dual_mppt on even count of 1-MPPT inverters
        res_ded = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="Dome", pan_name="Mono440.PAN", ond_name="Inv50_1MPPT.OND",
            n_mod_serie=20, n_string_ch=10, n_inverter=2, n_inv_mppt=2,
            independent_mppt=False, pnom_ac_in_mppt=50.0,
            tilt_deg=10.0, azimuth_deg=90.0, pitch_m=6.0, shed_count=5, module_rows=1, module_columns=20,
            dome_wiring="dual_mppt",
        )
        self.assertEqual(res_ded["utility_summary"]["dome"]["dome_wiring"], "dual_mppt")
        self.assertEqual(res_ded["vc0_text"].count("PowerSharingInverterId=-1"), 2)
        self.store.create_project("DedTest.PRJ", res_ded["prj_text"], {"VC0": res_ded["vc0_text"]})
        self.assertIs(self.store.validate_structure("DedTest.PRJ", "VC0")["valid"], True)

    def test_simulate_and_prepare_ground_dome_with_polygon_obstacles_and_terrain(self) -> None:
        _terrain_csv_for_dome(self.ws / "UserData" / "dome_dem.csv")
        cli = _DomeFakeCLI(self.ws, year=2024, nominal_dc_kw=88.0, use_sfi_writer=False)
        # First test regular 3D ground_dome via simulate_design (2 * 5 * 1 * 20 = 200 modules = 88.0 kW)
        spec_reg = {
            "scope": "ground_dome", "new_project": "DomeReg.PRJ", "year": 2024,
            "target_dc_kw": 88.0, "tilt": 10.0, "azimuth": 90.0,
            "module": {"file_name": "Mono440.PAN"}, "inverter": {"file_name": "Inv50_2MPPT.OND"},
            "capacity": _capacity_spec(target_dc_kw=88.0),
            "ground_layout": {
                "pitch_m": 6.0, "top_spacing_m": 0.2, "dome_wiring": "dual_mppt",
                "shed_count": 5, "module_rows": 1, "module_columns": 20, "ground_clearance_m": 0.6,
            },
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
        }
        sim_reg = simulate_design(cli, self.store, spec=spec_reg, dry_run=False)
        self.assertIn(sim_reg["status"], ("completed", "needs_review"), sim_reg)
        self.assertTrue(sim_reg["native_consumption_verified"])
        self.assertEqual(sim_reg["utility_summary"]["dome"]["dome_wiring"], "dual_mppt")

        # Deterministic rectangle polygon + obstacles + terrain (5 tables x 20 cols x 2 faces = 200 modules = 88.0 kW)
        poly = [[0.0, 0.0], [34.0, 0.0], [34.0, 24.0], [0.0, 24.0]]
        from pvsyst_auto.scene.terrain import build_terrain_mesh
        mesh = build_terrain_mesh({"csv_name": "dome_dem.csv"}, workspace=self.ws)
        tree_obs = {
            "kind": "tree", "name": "Oak1", "origin_m": [-15.0, 0.0, 0.0],
            "top_height_m": 6.0, "middle_height_m": 4.0, "bottom_height_m": 2.0,
            "trunk_height_m": 2.0, "crown_diameter_m": 3.0, "trunk_diameter_m": 0.4,
        }
        _, preview_3d = build_dome_3d_shading(
            site_polygon=poly, terrain_mesh=mesh, terrain_opts={"follow_slope": True},
            shed_count=4, pitch_m=6.0, tilt_deg=10.0, azimuth_deg=90.0, base_slope_deg=0.0,
            top_spacing_m=0.2, module_rows=1, module_columns=20,
            module_width_m=1.032, module_length_m=1.996, module_name="Mono",
            module_orient="moPortrait", spacing_x_m=0.02, spacing_y_m=0.02,
            setback_m=1.0, ground_clearance_m=0.5, max_table_columns=25,
            obstacles=[tree_obs],
        )
        poly_mods = preview_3d["total_modules"]
        self.assertEqual(poly_mods, 200)
        spec_poly = {
            "scope": "ground_dome", "new_project": "DomePoly.PRJ", "year": 2024,
            "target_dc_kw": 88.0, "tilt": 10.0, "azimuth": 90.0,
            "module": {"file_name": "Mono440.PAN"}, "inverter": {"file_name": "Inv50_2MPPT.OND"},
            "capacity": _capacity_spec(target_dc_kw=88.0),
            "ground_layout": {
                "pitch_m": 6.0, "top_spacing_m": 0.2, "dome_wiring": "mixed",
                "site_polygon": poly, "setback_m": 1.0, "module_rows": 1, "max_table_columns": 25,
                "terrain": {"csv_name": "dome_dem.csv", "follow_slope": True},
                "obstacles": [tree_obs],
            },
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
        }
        prep = prepare_design_project(cli, self.store, spec=spec_poly, dry_run=False)
        self.assertIn(prep["status"], ("prepared", "needs_review"), prep)
        poly_vc = (self.ws / "Projects" / "DomePoly.VC0").read_text(encoding="latin-1")
        self.assertIn("PVObject_1=pvShdArrayDome", poly_vc)
        self.assertIn("pvShdGround", poly_vc)
        self.assertIn("ObjetElemType=oeTree", poly_vc)

    def test_ground_dome_validation_guards(self) -> None:
        base = {
            "scope": "ground_dome", "new_project": "Guard.PRJ", "year": 2024,
            "target_dc_kw": 88.0, "tilt": 10.0, "azimuth": 90.0,
            "module": {"file_name": "Mono440.PAN"}, "inverter": {"file_name": "Inv50_2MPPT.OND"},
            "capacity": _capacity_spec(target_dc_kw=88.0),
            "ground_layout": {"pitch_m": 6.0, "top_spacing_m": 0.2},
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
        }
        bad_cases = [
            ("unlimited", {**base, "ground_layout": {"model_mode": "unlimited"}}),
            ("zero_tilt", {**base, "tilt": 0.0}),
            ("bifacial", {**base, "bifacial": {"enabled": True}}),
            ("sub_orients", {**base, "ground_layout": {"sub_orientations": [{"tilt": 10.0, "azimuth": 90.0, "weight": 1.0}]}}),
            ("bad_wiring", {**base, "ground_layout": {"dome_wiring": "series"}}),
            ("neg_gap", {**base, "ground_layout": {"top_spacing_m": -0.1}}),
        ]
        for label, bad_spec in bad_cases:
            with self.subTest(case=label):
                issues = []
                validate_utility_spec(bad_spec, "ground_dome", issues)
                self.assertTrue(issues, label)

        # Non-dome scopes reject dome-only layout keys
        shed_issues = []
        validate_utility_spec(
            {**base, "scope": "ground_sheds", "ground_layout": {"top_spacing_m": 0.2, "dome_wiring": "mixed"}},
            "ground_sheds", shed_issues,
        )
        self.assertGreaterEqual(len(shed_issues), 2)

        # Overlap guard: pitch_m <= dome_footprint_depth_m (Mono440.PAN length = 1.996 m)
        depth = dome_footprint_depth_m(1.996, 10.0, 0.2)
        with self.assertRaisesRegex(ValueError, "dome footprint depth"):
            synthesize_utility_project(
                self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
                field_type="Dome", pan_name="Mono440.PAN", ond_name="Inv50_2MPPT.OND",
                n_mod_serie=20, n_string_ch=12, n_inverter=2, n_inv_mppt=4,
                independent_mppt=True, pnom_ac_in_mppt=25.0,
                tilt_deg=10.0, azimuth_deg=90.0, pitch_m=depth - 0.1, top_spacing_m=0.2,
                shed_count=6, module_rows=1, module_columns=20,
            )

        # Odd string count guard
        with self.assertRaisesRegex(ValueError, "even total string count"):
            synthesize_utility_project(
                self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
                field_type="Dome", pan_name="Mono440.PAN", ond_name="Inv50_2MPPT.OND",
                n_mod_serie=20, n_string_ch=9, n_inverter=1, n_inv_mppt=1,
                independent_mppt=False, pnom_ac_in_mppt=50.0,
                tilt_deg=10.0, azimuth_deg=90.0, pitch_m=6.0, shed_count=5, module_rows=1, module_columns=18,
            )

    def test_convert_orientation_two_subarrays_and_tracker_roundtrip(self) -> None:
        # 2 non-mixed subarrays -> dome allocates NoOrientation=1 and NoOrientation=2
        dual_src = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Mono440.PAN", ond_name="Inv50_1MPPT.OND",
            orientations=[{"id": 1, "field_type": "FixedPlane", "tilt_deg": 20.0, "azimuth_deg": 0.0}],
            subarrays=[
                {"subarray_id": 1, "orientation_id": 1, "n_mod_serie": 20, "n_string_ch": 4, "n_inverter": 1, "n_inv_mppt": 1, "independent_mppt": False, "pnom_ac_in_mppt": 50.0},
                {"subarray_id": 2, "orientation_id": 1, "n_mod_serie": 20, "n_string_ch": 4, "n_inverter": 1, "n_inv_mppt": 1, "independent_mppt": False, "pnom_ac_in_mppt": 50.0},
            ],
        )
        self.store.create_project("ConvDual.PRJ", dual_src["prj_text"], {"VC0": dual_src["vc0_text"]})
        to_dome = convert_orientation(self.store, project="ConvDual.PRJ", source_variant="VC0", new_variant="VC1", target_type="dome", params={"tilt_deg": 10.0, "azimuth_deg": 90.0}, dry_run=False)
        self.assertTrue(self.store.validate_structure("ConvDual.PRJ", "VC1")["valid"])
        vc1 = (self.ws / "Projects" / "ConvDual.VC1").read_text(encoding="latin-1")
        self.assertIn("NoOrientation=1", vc1)
        self.assertIn("NoOrientation=2", vc1)
        self.assertNotIn("NoOrientation2=", vc1)

        to_trk = convert_orientation(self.store, project="ConvDual.PRJ", source_variant="VC1", new_variant="VC2", target_type="tracker_ns", dry_run=False)
        self.assertEqual(to_trk["orientation_conversion"]["target_type"], "tracker_ns")
        self.assertTrue(self.store.validate_structure("ConvDual.PRJ", "VC2")["valid"])

    def test_pr288_review_regressions_mixed_checks_readback_power_sharing_and_area(self) -> None:
        # 1) Reject >= 3 power-sharing subarrays converting to mixed dome before writing
        ps3 = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Mono440.PAN", ond_name="Inv50_2MPPT.OND",
            orientations=[{"id": 1, "field_type": "FixedPlane", "tilt_deg": 20.0, "azimuth_deg": 0.0}],
            subarrays=[
                {"subarray_id": i, "orientation_id": 1, "n_mod_serie": 20, "n_string_ch": 2,
                 "n_inverter": 1, "n_inv_mppt": 1, "independent_mppt": True,
                 "power_sharing_inverter_id": 0, "pnom_ac_in_mppt": 16.67}
                for i in (1, 2, 3)
            ],
        )
        self.store.create_project("PS3.PRJ", ps3["prj_text"], {"VC0": ps3["vc0_text"]})
        self.assertIs(self.store.validate_structure("PS3.PRJ", "VC0")["valid"], True)
        blocked = convert_orientation(self.store, project="PS3.PRJ", source_variant="VC0", new_variant="VC1", target_type="dome", dry_run=False)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(any("power-sharing" in str(msg).lower() for msg in blocked["issues"]))
        self.assertFalse((self.ws / "Projects" / "PS3.VC1").exists())

        # 2) Mixed-orientation validation catches invalid string splits & flags
        res_mixed = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="Dome", pan_name="Mono440.PAN", ond_name="Inv50_1MPPT.OND",
            n_mod_serie=20, n_string_ch=10, n_inverter=1, n_inv_mppt=1,
            independent_mppt=False, pnom_ac_in_mppt=50.0,
            tilt_deg=10.0, azimuth_deg=90.0, pitch_m=6.0, top_spacing_m=0.2,
            shed_count=5, module_rows=1, module_columns=20, dome_wiring="mixed",
        )
        self.store.create_project("MixCheck.PRJ", res_mixed["prj_text"], {"VC0": res_mixed["vc0_text"]})
        base_txt = res_mixed["vc0_text"]
        # Check regular dome GCR doubles collector width relative to single face
        self.assertEqual(
            res_mixed["utility_summary"]["gcr"],
            round((2.0 * res_mixed["utility_summary"]["collector_width_m"]) / 6.0, 4),
        )
        mutations = (
            ("nso1_gt_total", base_txt.replace("NStrOrient1=5", "NStrOrient1=99")),
            ("nso1_zero", base_txt.replace("NStrOrient1=5", "NStrOrient1=0")),
            ("flags_missing_40", base_txt.replace("Flags=$001E0042", "Flags=$001E0002")),
            ("flags_with_ps_100", base_txt.replace("Flags=$001E0042", "Flags=$001E0142")),
            ("flags2_missing_01", base_txt.replace("Flags2=$0021", "Flags2=$0020")),
            ("missing_ps_id", base_txt.replace("      PowerSharingInverterId=-1\r\n", "").replace("      PowerSharingInverterId=-1\n", "")),
            ("dup_nso1", base_txt.replace("NStrOrient1=5", "NStrOrient1=5\r\n      NStrOrient1=5")),
            ("no_no2_with_flag40", base_txt.replace("      NoOrientation2=2\r\n", "").replace("      NoOrientation2=2\n", "")),
        )
        for idx_m, (label, mutated) in enumerate(mutations, start=1):
            with self.subTest(mutation=label):
                vtag = f"VC{idx_m}"
                (self.ws / "Projects" / f"MixCheck.{vtag}").write_bytes(mutated.encode("utf-8"))
                st = validate_structure(self.store, "MixCheck.PRJ", vtag)
                self.assertIs(st["valid"], False, label)
                self.assertTrue(st["issues"], label)

        # 3) Public parameter reader exposes both pvDomeOrient entries + NoOrientation2
        params = self.store.inspect_parameters("MixCheck.PRJ", "VC0")
        self.assertEqual(len(params["orientations"]), 2)
        self.assertEqual([o["values"]["FieldType"] for o in params["orientations"]], ["Dome", "DomeBackfaces"])
        self.assertEqual(params["orientations"][0]["values"]["IsMainOrient"], "True")
        self.assertEqual(params["orientations"][1]["values"]["NoPairOrient"], "1")
        sub_vals = params["subarrays"][0]["values"]
        self.assertEqual(sub_vals["NoOrientation"], "1")
        self.assertEqual(sub_vals["NoOrientation2"], "2")
        self.assertEqual(sub_vals["NStrOrient1"], "5")

        # 4) Two-face collector area & GCR in 3D polygon dome summary
        poly = [[0.0, 0.0], [34.0, 0.0], [34.0, 24.0], [0.0, 24.0]]
        _, poly_summary = build_dome_3d_shading(
            site_polygon=poly, terrain_mesh=None, terrain_opts=None,
            shed_count=4, pitch_m=6.0, tilt_deg=10.0, azimuth_deg=90.0, base_slope_deg=0.0,
            top_spacing_m=0.2, module_rows=1, module_columns=20,
            module_width_m=1.032, module_length_m=1.996, module_name="Mono",
            module_orient="moPortrait", spacing_x_m=0.02, spacing_y_m=0.02,
            setback_m=1.0, ground_clearance_m=0.5, max_table_columns=25,
        )
        expected_two_face_area = round(2.0 * sum(t["long_shed_m"] * t["larg_shed_m"] for t in poly_summary["tables"]), 2)
        self.assertEqual(poly_summary["collector_area_m2"], expected_two_face_area)
        self.assertEqual(poly_summary["gcr"], round(expected_two_face_area / poly_summary["site_area_m2"], 4))
        self.assertGreater(poly_summary["collector_area_m2"], 400.0)

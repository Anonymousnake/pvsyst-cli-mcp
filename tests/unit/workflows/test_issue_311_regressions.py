"""Regression tests for GitHub Issue #311."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pvsyst_auto.api import context as api_context
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.energy.profiles import prepare_load_profile
from pvsyst_auto.energy.system import create_grid_storage_system
from pvsyst_auto.engineering.review import review_project_inputs
from pvsyst_auto.project.circuit_branches import clone_subarray
from pvsyst_auto.project.paths import variant_id
from pvsyst_auto.project.standalone import run_standalone_design
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.weather.horizon import HorizonFetchError, resolve_design_horizon
from pvsyst_auto.workflows.design import prepare_design_project
from pvsyst_auto.workflows.optimize import propose_remediations
from pvsyst_auto.workflows.revision import prepare_storage_revision
from pvsyst_auto.workflows.simulation import simulate_template
from tests._fixtures import init_full_workspace
from tests.unit.core.test_bifacial_utility import UTILITY_MONO_PAN, UTILITY_OND, _FakeUtilityCLI, _capacity_spec
from tests.unit.core.test_components import BTR
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT
from tests.unit.energy.test_energy import BATTERY, STORAGE, hourly


def _seed_fixed_template(store: VariantStore, *, name: str = "Tpl.PRJ", variant: str = "VC0", with_bom: bool = False) -> None:
    synth = synthesize_project_and_variant(
        store,
        site_name="Geneva_MN90.SIT",
        met_name="Geneva_MN90_SYN.MET",
        pan_name="Mono440.PAN",
        ond_name="Inv50_2MPPT.OND",
        n_mod_serie=20,
        n_string_ch=6,
        n_inverter=1,
        n_inv_mppt=2,
        independent_mppt=True,
        pnom_ac_in_mppt=25.0,
        with_bom=with_bom,
    )
    vc_text = synth["vc0_text"].replace("\r\n", "\n")
    vc_text = vc_text.replace("  Flags=$00\r\n", "  Flags=$00\r\n")
    vc_text = vc_text.replace(
        "PVObject_=pvVCalcul\n  Version=8.1.7\n",
        "PVObject_=pvVCalcul\n  Version=8.1.7\n  PRCorrTemp=False\n",
        1,
    )
    if not with_bom and vc_text.startswith("\ufeff"):
        vc_text = vc_text.lstrip("\ufeff")
    elif with_bom and not vc_text.startswith("\ufeff"):
        vc_text = "\ufeff" + vc_text
    store.create_project(name, synth["prj_text"], {variant: vc_text.replace("\n", "\r\n")})


class Issue311RegressionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = init_full_workspace(Path(self._tmp.name))
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV" / "PVmodules" / "Mono440.PAN").write_text(UTILITY_MONO_PAN, encoding="utf-8")
        (self.ws / "ComposPV" / "Inverters" / "Inv50_2MPPT.OND").write_text(UTILITY_OND, encoding="utf-8")
        (self.ws / "ComposPV" / "Batteries" / "Lead160.BTR").write_text(BTR, encoding="utf-8")
        self.comp = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.comp)
        self.cli = _FakeUtilityCLI(self.ws, year=2024, nominal_dc_kw=52.8, use_sfi_writer=False)

    def test_variant_id_unification_vd0_to_vzz_and_rejects_vc10(self) -> None:
        self.assertEqual(variant_id("vd0"), "VD0")
        self.assertEqual(variant_id("VZZ"), "VZZ")
        for bad in ("VC10", "VB0", "VA0", "VC", "VD00"):
            with self.assertRaises(ValueError):
                variant_id(bad)

        _seed_fixed_template(self.store, name="VIds.PRJ", with_bom=True)
        cloned = self.store.clone("VIds.PRJ", "VC0", "VD0")
        self.assertEqual(cloned["variant"], "VD0")
        inspected = self.store.inspect_project("VIds.PRJ")
        self.assertIn("VD0", inspected["variants"])
        params = self.store.inspect_parameters("VIds.PRJ", "VD0")
        self.assertEqual(params["variant"], "VD0")

        branch_res = clone_subarray(self.store, "VIds.PRJ", "VD0", 1)
        self.assertEqual(branch_res["variant"], "VD0")

        (self.ws / "Projects" / "VIds.VC10").write_text("bad4char", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unrecognized project sidecar"):
            self.store.inspect_project("VIds.PRJ")
        (self.ws / "Projects" / "VIds.VC10").unlink()

        with patch.dict("os.environ", {"PVSYST_WORKSPACE": str(self.ws)}):
            async def _read_proj():
                return await api_context.mcp.read_resource("pvsyst://workspace/projects")
            raw = asyncio.run(_read_proj())
        res_json = raw[0].content
        projects_map = {item["project"]: item["variants"] for item in json.loads(res_json)["projects"]}
        self.assertIn("VD0", projects_map["VIds.PRJ"])

    def test_composable_controls_guards_and_removals_first(self) -> None:
        _seed_fixed_template(self.store, name="Comp.PRJ", with_bom=True)
        # Disallowed incompatible combinations fail up-front on dry_run=True and dry_run=False
        for bad_kwargs in (
            {"power_factor_updates": {"mode": "cos_phi", "cos_phi": 0.95}, "power_controls_updates": {"power_factor": {"mode": "cos_phi", "cos_phi": 0.95}, "grid_limit": {"mode": "none"}}},
            {"grid_limit_updates": {"mode": "constant", "limit_kw": 40.0}, "inverter_temperature_updates": {"mode": "fixed", "fixed_c": 45.0}},
            {"auxiliary_updates": {"mode": "profile", "operating_kw": 1.0, "night_kw": 0.0, "operating_threshold_kw": 5.0, "proportional_w_per_kw": 0.0, "proportional_threshold_kw": 0.0}, "p50_p90_updates": {"meteo_annual_variab_pct": 3.0}},
            {"horizon_updates": {"mode": "profile", "profile_name": "Hills", "points": [[0, 0], [90, 10], [180, 0]]}, "power_controls_updates": {"power_factor": {"mode": "cos_phi", "cos_phi": 0.95}, "grid_limit": {"mode": "none"}}},
        ):
            with self.subTest(bad_kwargs=sorted(bad_kwargs)):
                with self.assertRaises(ValueError):
                    self.store.update_parameters("Comp.PRJ", "VC0", **bad_kwargs, dry_run=True)

        # Mount horizon and verify horizon_updates(mode="none") + horizon-sensitive control succeeds because removals sort first!
        self.store.update_parameters(
            "Comp.PRJ", "VC0",
            horizon_updates={"mode": "profile", "profile_name": "Ridge", "points": [[-180, 0], [-60, 8], [60, 8], [180, 0]]},
            dry_run=False,
        )
        # First clear horizon and apply inverter_temperature + soiling in one call!
        ok_combo = self.store.update_parameters(
            "Comp.PRJ", "VC0",
            horizon_updates={"mode": "none"},
            inverter_temperature_updates={"mode": "fixed", "offset_c": 0.0, "fixed_c": 45.0},
            soiling_updates={"mode": "constant", "yearly_loss_pct": 1.5},
            dry_run=False,
        )
        self.assertEqual(ok_combo["horizon"]["after"]["mode"], "none")
        self.assertEqual(ok_combo["inverter_temperature"]["after"]["mode"], "fixed")
        self.assertEqual(ok_combo["soiling"]["after"]["yearly_loss_pct"], 1.5)

    def test_optimize_propose_remediations_3d_and_sheds_advisories(self) -> None:
        # 1. Unlimited sheds with SHADING_LOSS_HIGH -> advisory (no orient_updates["Pitch"] crash)
        prep = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec={
                "scope": "ground_sheds",
                "new_project": "ShedsOpt.PRJ",
                "year": 2024,
                "target_dc_kw": 52.8,
                "tilt": 20.0,
                "azimuth": 0.0,
                "module": {"file_name": "Mono440.PAN"},
                "inverter": {"file_name": "Inv50_2MPPT.OND"},
                "capacity": _capacity_spec(target_dc_kw=52.8),
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                "ground_layout": {"model_mode": "unlimited", "pitch_m": 6.0, "shed_count": 4, "module_rows": 2, "module_columns": 15},
            },
        )
        self.assertIn(prep["status"], ("prepared", "needs_review"), prep)
        rem_shed = propose_remediations(self.store, "ShedsOpt.PRJ", "VC0", {"findings": [{"code": "SHADING_LOSS_HIGH", "message": "high shading"}]})
        self.assertFalse(rem_shed["has_executable_patch"])
        self.assertEqual(rem_shed["actions"], [])
        self.assertIn("pvsyst_create_shed_geometry_variant", rem_shed["advisories"][0]["message"])

        # 2. 3D scene with CLIPPING_LOSS_HIGH -> catches ValueError and emits advisory
        prep_3d = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec={
                "scope": "ground_sheds",
                "new_project": "Sheds3D.PRJ",
                "year": 2024,
                "target_dc_kw": 52.8,
                "tilt": 20.0,
                "azimuth": 0.0,
                "module": {"file_name": "Mono440.PAN"},
                "inverter": {"file_name": "Inv50_2MPPT.OND"},
                "capacity": _capacity_spec(target_dc_kw=52.8),
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                "ground_layout": {"model_mode": "3d", "pitch_m": 6.0, "shed_count": 3, "module_rows": 2, "module_columns": 20},
            },
        )
        self.assertIn(prep_3d["status"], ("prepared", "needs_review"), prep_3d)
        rem_3d = propose_remediations(self.store, "Sheds3D.PRJ", "VC0", {"findings": [{"code": "INVERTER_CLIPPING_HIGH", "message": "clipping"}]})
        self.assertFalse(rem_3d["has_executable_patch"])
        self.assertEqual(rem_3d["diff_preview"], "")
        self.assertEqual(rem_3d["advisories"][0]["action"], "manual_review")

    def test_review_project_inputs_utility_manifests_and_losses_ageing(self) -> None:
        prep = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec={
                "scope": "ground_sheds",
                "new_project": "UtilRev.PRJ",
                "year": 2024,
                "target_dc_kw": 52.8,
                "tilt": 20.0,
                "azimuth": 0.0,
                "losses": {"LIDLoss": 1.0, "ageing": {"simulation_year": 10, "annual_module_degradation_percent": 0.45}},
                "module": {"file_name": "Mono440.PAN"},
                "inverter": {"file_name": "Inv50_2MPPT.OND"},
                "capacity": _capacity_spec(target_dc_kw=52.8),
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                "ground_layout": {"model_mode": "unlimited", "pitch_m": 6.0, "shed_count": 4, "module_rows": 2, "module_columns": 15},
            },
        )
        self.assertIn("request", prep["preview"]["project_preparation"])
        saved_manifest = json.loads((self.ws / "Results" / "UtilRev_prepare_2024.json").read_text(encoding="utf-8"))
        self.assertIn("request", saved_manifest)
        rev = review_project_inputs(self.store, project="UtilRev.PRJ", variant="VC0", preparation_manifest_name="UtilRev_prepare_2024.json")
        self.assertEqual(rev["status"], "needs_review", rev)
        cmp_map = {row["field"]: row["status"] for row in rev["comparisons"]}
        self.assertEqual(cmp_map["ageing.enabled"], "matches")
        self.assertEqual(cmp_map["ageing.simulation_year"], "matches")
        self.assertEqual(cmp_map["ageing.annual_module_degradation_percent"], "matches")
        self.assertEqual(cmp_map["NModSerie"], "matches")

        # Legacy utility manifest missing top-level "request" also succeeds on tracker without tilt
        trk = prepare_design_project(
            self.cli, self.store, dry_run=False,
            spec={
                "scope": "ground_tracker",
                "new_project": "TrkRev.PRJ",
                "year": 2024,
                "target_dc_kw": 52.8,
                "azimuth": 0.0,
                "module": {"file_name": "Mono440.PAN"},
                "inverter": {"file_name": "Inv50_2MPPT.OND"},
                "capacity": _capacity_spec(target_dc_kw=52.8),
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                "ground_layout": {"model_mode": "unlimited", "pitch_m": 8.0, "shed_count": 4, "module_rows": 1, "module_columns": 30},
            },
        )
        mpath = self.ws / "Results" / "TrkRev_prepare_2024.json"
        legacy = json.loads(mpath.read_text(encoding="utf-8"))
        legacy.pop("request", None)
        mpath.write_text(json.dumps(legacy), encoding="utf-8")
        rev_trk = review_project_inputs(self.store, project="TrkRev.PRJ", variant="VC0", preparation_manifest_name="TrkRev_prepare_2024.json")
        self.assertEqual(rev_trk["status"], "reviewed", rev_trk)

    def test_revision_weak_grid_generated_availability_and_missing_csv_name(self) -> None:
        _seed_fixed_template(self.store, name="Stor.PRJ", with_bom=True)
        (self.ws / "UserData" / "raw.csv").write_text(hourly("Date;P Load", "W", "2500"), encoding="utf-8", newline="")
        prepared_load = prepare_load_profile(
            SimpleNamespace(_lock=threading.RLock(), workspace=self.ws),
            csv_name="raw.csv",
            year=2023,
            output_name="load.csv",
            utc_offset_hours=8,
            time_basis="local_standard_interval_start",
            dry_run=False,
        )
        self.assertEqual(prepared_load["status"], "prepared", prepared_load)

        created = create_grid_storage_system(
            self.store,
            project="Stor.PRJ",
            source_variant="VC0",
            new_variant="VC1",
            battery={**BATTERY, "file_name": "Lead160.BTR", "library": "workspace"},
            storage=STORAGE,
            load_manifest_name="load_load.json",
            dry_run=False,
        )
        self.assertEqual(created["status"], "created", created)
        gen_avail = {
            "mode": "generated", "year": 2023, "utc_offset_hours": 8,
            "time_basis": "local_standard_interval_start",
            "outage_count": 4, "average_duration_hours": 2.0, "seed": 7,
        }
        ok_gen = prepare_storage_revision(
            self.store, "Stor.PRJ", "VC1", "VC2",
            {"strategy": "weak_grid"}, "load_load.json", 2023, availability=gen_avail,
        )
        self.assertEqual(ok_gen["status"], "ready", ok_gen)
        self.assertEqual(ok_gen["input_assets"]["grid_availability"]["mode"], "generated")

        bad_csv = prepare_storage_revision(
            self.store, "Stor.PRJ", "VC1", "VC3",
            {"strategy": "weak_grid"}, "load_load.json", 2023,
            availability={"mode": "csv", "year": 2023, "utc_offset_hours": 8, "time_basis": "local_standard_interval_start"},
        )
        self.assertEqual(bad_csv["status"], "blocked")
        self.assertIn("csv_name", bad_csv["issues"][0])

    def test_standalone_preflight_blocks_before_writing_files(self) -> None:
        cli_ok = Mock()
        cli_ok.license_info.return_value = {"status": "ACTIVATED"}
        base_spec = {
            "scope": "stand_alone",
            "new_project": "CabinBad.PRJ",
            "module": {"file_name": "Mono440.PAN"},
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            "standalone": {
                "battery_file": "Lead160.BTR",
                "genset_spec": {"file_name": "PrecheckGen.GEN", "model": "G5", "nominal_power_kw": 5.0, "fuel_consumption_l_per_h": 1.5},
            },
        }
        # Bad report_pages and report_language fail in dry_run AND dry_run=False with zero writes
        with self.assertRaisesRegex(ValueError, "Unknown or empty PDF report page"):
            run_standalone_design(cli_ok, self.store, self.comp, {**base_spec, "report_pages": ["NOT_A_PAGE"]}, dry_run=True)
        with self.assertRaisesRegex(ValueError, "Unsupported report language"):
            run_standalone_design(cli_ok, self.store, self.comp, {**base_spec, "report_language": "xx"}, dry_run=False)
        self.assertFalse((self.ws / "ComposPV" / "Gensets" / "PrecheckGen.GEN").exists())
        self.assertFalse((self.ws / "Projects" / "CabinBad.PRJ").exists())

        # Pre-existing SFI collision and EXPIRED license blocked on dry_run=True
        (self.ws / "Models" / "CabinBad_standalone.SFI").write_text("exist", encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "CabinBad_standalone.SFI"):
            run_standalone_design(cli_ok, self.store, self.comp, base_spec, dry_run=True)
        (self.ws / "Models" / "CabinBad_standalone.SFI").unlink()

        cli_expired = Mock()
        cli_expired.license_info.return_value = {"status": "EXPIRED"}
        with self.assertRaisesRegex(ValueError, "license is not ready"):
            run_standalone_design(cli_expired, self.store, self.comp, base_spec, dry_run=False)
        self.assertFalse((self.ws / "Projects" / "CabinBad.PRJ").exists())

    def test_horizon_and_tracking_preflight_guards(self) -> None:
        (self.ws / "Sites" / "NoCoord.SIT").write_text("PVObject_=pvSite\n  Comment=NoCoords\nEnd of PVObject pvSite\n", encoding="utf-8")
        with self.assertRaisesRegex(HorizonFetchError, "latitude/longitude"):
            resolve_design_horizon(
                workspace=self.ws,
                horizon_spec={"mode": "pvgis"},
                weather_spec={"mode": "existing", "sit_name": "NoCoord.SIT", "fetch_horizon": True},
                sit_name="NoCoord.SIT",
                dry_run=True,
            )

        # BOM-less fixed template + non-ASCII horizon.profile_name blocks BEFORE remote weather download
        _seed_fixed_template(self.store, name="NoBomTpl.PRJ", with_bom=False)
        with patch("pvsyst_auto.workflows.design.download_weather") as dl_mock:
            blocked = prepare_design_project(
                self.cli,
                self.store,
                dry_run=False,
                spec={
                    "scope": "fixed_unshaded_grid",
                    "template_project": "NoBomTpl.PRJ",
                    "variant": "VC0",
                    "new_project": "HzNonAscii.PRJ",
                    "year": 2024,
                    "target_dc_kw": 52.8,
                    "tilt": 20.0,
                    "azimuth": 0.0,
                    "module": {"file_name": "Mono440.PAN"},
                    "inverter": {"file_name": "Inv50_2MPPT.OND"},
                    "capacity": _capacity_spec(target_dc_kw=52.8),
                    "weather": {"mode": "pvgis_tmy", "location_id": 1, "csv_name": "w.csv", "sit_name": "S.SIT", "met_name": "M.MET", "country_code": "CH"},
                    "horizon": {"mode": "points", "profile_name": "山脊地平线", "points": [[-180, 0], [-60, 10], [60, 10], [180, 0]]},
                },
            )
            dl_mock.assert_not_called()
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["stage"], "horizon")

        # simulate_template(tracking=...) rejects horizon and p50_p90
        for extra in ({"horizon": {"mode": "none"}}, {"p50_p90": {"meteo_annual_variab_pct": 3.0}}):
            res = simulate_template(
                self.cli, self.store,
                template_project="NoBomTpl.PRJ", variant="VC0", new_project="TrkBad.PRJ",
                year=2024, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
                pan_name="Mono440.PAN", ond_name="Inv50_2MPPT.OND",
                tracking={"axis_tilt_deg": 0.0}, tracking_variant="VC1",
                nominal_dc_power_kw=52.8, tracking_power_source="nameplate",
                dry_run=True, **extra,
            )
            self.assertEqual(res["status"], "blocked")
            self.assertIn("horizon/p50_p90", res["issues"][0]["message"])


if __name__ == "__main__":
    unittest.main()

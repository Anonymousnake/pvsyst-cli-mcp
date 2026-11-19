"""Unit tests for Phase 1 (v0.3.0): pvBiFacial model, utility ground sheds/trackers synthesis,
auto PAN dimensions, bifacial SFI, and simulate_design / prepare_design_project utility scopes."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.api.schemas import SingleDesignRequest
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.native.cli import VAR_GROUPS, build_sfi
from pvsyst_auto.project.bifacial import normalize_bifacial_spec, render_bifacial_block
from pvsyst_auto.project.synthesize import synthesize_utility_project
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.workflows.design import EXPECTED_WORKSPACE_BASENAME, prepare_design_project, simulate_design
from tests.unit.core.test_components import OND, PAN
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT

UTILITY_MONO_PAN = PAN.replace("  muVocSpec=-0.1485\n", "  muVocSpec=-0.1485\n  VMaxIEC=1500\n")
UTILITY_PAN = UTILITY_MONO_PAN.replace("  Flags=$00\n", "  Flags=$00800000\n", 1).replace(
    "  PNom=440\n", "  PNom=440\n  BifacialityFactor=0.800\n", 1
)
UTILITY_OND = OND.replace(
    "    EfficMax=98\n",
    "    EfficMax=98\n    ModeOper=MPPT\n    CompPMax=Lim\n    CompVMax=Lim\n",
).replace("End of PVObject pvGInverter", "  NbMPPT=2\nEnd of PVObject pvGInverter")


def _capacity_spec(**overrides):
    data = {
        "target_dc_kw": 100.0,
        "max_dc_ac_ratio": 1.2,
        "voltage_standard": "IEC",
        "design_basis": "declared datasheet envelope",
        "capacity_tolerance_pct": 5.0,
        "cold_voc_v": 52.0,
        "cold_vmp_v": 44.0,
        "hot_vmp_v": 34.0,
        "module_max_operating_current_a": 11.0,
        "module_max_short_circuit_current_a": 12.0,
        "mppt_max_operating_current_a": 35.0,
        "mppt_max_short_circuit_current_a": 40.0,
        "design_min_temperature_c": -10.0,
        "design_min_operating_temperature_c": -5.0,
        "design_max_operating_temperature_c": 70.0,
    }
    data.update(overrides)
    return data


def _write_fake_annual_csv(csv_path: Path, *, project: str, variant: str, site: str, met: str,
                           year: int, nominal_dc_kw: float, bifacial: bool = False):
    headers = ["date", "GlobInc", "GlobEff", "EArray", "E_Grid", "PR", "IL_Pmax", "GlobShd"]
    units = ["", "W/m\u00b2", "W/m\u00b2", "kW", "kW", "ratio", "kW", "W/m\u00b2"]
    if bifacial:
        headers.extend(["GlobGnd", "DiffGnd", "GlobBak", "BeamBak", "DifSBak", "BmSFBak", "MismBak", "PRBifi"])
        units.extend(["W/m\u00b2", "W/m\u00b2", "W/m\u00b2", "W/m\u00b2", "W/m\u00b2", "ratio", "kW", "ratio"])
    lines = [
        f"Project;{project}",
        f"Simulation variant;{Path(project).stem}.{variant}",
        f"Geographical Site;{site}",
        f"Weather data;{met}",
        "",
        ";".join(headers),
        ";".join(units),
    ]
    start = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    cur = start
    pr_val = 0.90 if bifacial else 0.84
    while cur < end:
        stamp = cur.strftime("%d/%m/%y %H:%M")
        if 8 <= cur.hour <= 16:
            gi = 500.0
            ge = 480.0
            egrid = (gi * 0.001) * nominal_dc_kw * pr_val
            earr = egrid * 1.02
            row = [stamp, f"{gi:.2f}", f"{ge:.2f}", f"{earr:.3f}", f"{egrid:.3f}", f"{pr_val:.4f}", "0.000", "490.00"]
            if bifacial:
                row.extend(["400.00", "150.00", "50.00", "20.00", "30.00", "0.800", "0.100", "0.8200"])
        else:
            row = [stamp, "0.00", "0.00", "0.000", "0.000", "", "0.000", "0.00"]
            if bifacial:
                row.extend(["0.00", "0.00", "0.00", "0.00", "0.00", "0.000", "0.000", ""])
        lines.append(";".join(row))
        cur += timedelta(hours=1)
    csv_path.write_bytes(("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8"))


class _FakeUtilityCLI:
    def __init__(self, workspace: Path, *, year: int = 2024, nominal_dc_kw: float = 100.0, bifacial: bool = False):
        self.workspace = workspace
        self.year = year
        self.nominal_dc_kw = nominal_dc_kw
        self.bifacial = bifacial
        self.runs = []

    def _require(self, command, option, *, allow_window_creation=None):
        return None

    def command_help(self, command="", *, allow_window_creation=None):
        return ""

    def version(self, *, allow_window_creation=None):
        return "8.1.7"

    def license_info(self, *, allow_window_creation=None):
        return {"status": "LICENSED", "remaining_days": 30}

    def _redact(self, text):
        return str(text)

    def run_simulation(self, project, variant, *, sfi=None, out_csv=None, report_pdf=None, **kwargs):
        self.runs.append({"project": project, "variant": variant, "sfi": sfi, "out_csv": out_csv,
                          "report_pdf": report_pdf, **kwargs})
        sfi_text = Path(sfi).read_text(encoding="utf-8") if sfi else ""
        is_bifi = "viGlobBak" in sfi_text
        # Read actual nominal_dc_kw from synthesized variant
        vc_text = (self.workspace / "Projects" / f"{Path(project).stem}.{variant}").read_text(encoding="utf-8-sig")
        import re
        n_ser = int(re.search(r"NModSerie=(\d+)", vc_text).group(1))
        n_str = int(re.search(r"NStringCh=(\d+)", vc_text).group(1))
        nom_kw = n_ser * n_str * 440.0 / 1000.0
        _write_fake_annual_csv(
            Path(out_csv), project=project, variant=variant,
            site="Geneva_MN90.SIT", met="Geneva_MN90_SYN.MET",
            year=self.year, nominal_dc_kw=nom_kw, bifacial=is_bifi,
        )
        if report_pdf is not None:
            Path(report_pdf).write_bytes(b"%PDF-1.4\n%%EOF\n")
        return {"exit_code": 0, "warnings": [], "sfi_variables": sfi_text}


class BifacialAndUtilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / EXPECTED_WORKSPACE_BASENAME
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in (
            "Projects", "Sites", "Meteo", "Models", "Results",
            "ComposPV/PVmodules", "ComposPV/Inverters",
        ):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "Sites" / "Geneva_MN90.SIT").write_bytes(
            b"\xef\xbb\xbf" + SITE_TEXT.replace("\n", "\r\n").encode("utf-8")
        )
        (self.root / "Meteo" / "Geneva_MN90_SYN.MET").write_bytes(
            b"\xef\xbb\xbf" + MET_TEXT.replace("\n", "\r\n").encode("utf-8")
        )
        (self.root / "ComposPV/PVmodules/Lab440.PAN").write_bytes(
            b"\xef\xbb\xbf" + UTILITY_PAN.replace("\n", "\r\n").encode("utf-8")
        )
        (self.root / "ComposPV/Inverters/Lab50.OND").write_bytes(
            b"\xef\xbb\xbf" + UTILITY_OND.replace("\n", "\r\n").encode("utf-8")
        )
        self.components = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.components)

    def test_bifacial_spec_validation_and_rendering(self):
        lines = render_bifacial_block({"ground_albedo": 0.25, "bifaciality_factor": 0.78}, field_type="Sheds")
        joined = "\n".join(lines)
        self.assertIn("BifacialModel=Sheds2D", joined)
        self.assertIn("ParamOrigin=UnlimSheds", joined)
        self.assertIn("GroundAlbedo=0.250", joined)
        self.assertIn("BiFacialityFactor=0.780", joined)
        self.assertIn("PVObject_BiFacialShedModel=pvBiFacialSheds", joined)

        tlines = render_bifacial_block({"height_above_ground_m": 2.4, "n_sheds_user": 12}, field_type="TrackHorizNSAxis")
        tjoined = "\n".join(tlines)
        self.assertIn("BifacialModel=Trackers2D", tjoined)
        self.assertIn("ParamOrigin=RegularSheds", tjoined)
        self.assertIn("PVObject_BiFacialTrackerModel=pvBiFacialTrackers", tjoined)
        self.assertIn("HeightGr=2.400", tjoined)
        self.assertIn("NShedsUser=12", tjoined)

        with self.assertRaisesRegex(ValueError, "Trackers2D"):
            normalize_bifacial_spec({"model": "Sheds2D"}, field_type="TrackHorizNSAxis")
        with self.assertRaisesRegex(ValueError, "Sheds2D"):
            normalize_bifacial_spec({"model": "Trackers2D"}, field_type="Sheds")
        with self.assertRaisesRegex(ValueError, "ground_albedo"):
            normalize_bifacial_spec({"ground_albedo": 1.5}, field_type="Sheds")
        with self.assertRaisesRegex(ValueError, "n_sheds_user"):
            normalize_bifacial_spec({"n_sheds_user": 0}, field_type="Sheds")

    def test_mount_update_and_remove_bifacial_via_variant_store(self):
        synth = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="Sheds", pan_name="Lab440.PAN", ond_name="Lab50.OND",
            bifacial=None,
        )
        self.store.create_project("UtilSheds.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
        info0 = self.store.inspect_parameters("UtilSheds.PRJ", "VC0")
        self.assertEqual(info0["orientations"][0]["values"]["FieldType"], "Sheds")
        self.assertNotIn("bifacial", info0["orientations"][0])

        dry = self.store.update_parameters(
            "UtilSheds.PRJ", "VC0", expected_sha256=info0["sha256"],
            bifacial_updates={"ground_albedo": 0.30, "bifaciality_factor": 0.80, "height_above_ground_m": 1.2},
            dry_run=True,
        )
        self.assertTrue(dry["changed"])
        self.assertEqual(dry["bifacial"]["action"], "mounted")
        self.assertEqual(self.store.inspect_parameters("UtilSheds.PRJ", "VC0")["sha256"], info0["sha256"])

        mounted = self.store.update_parameters(
            "UtilSheds.PRJ", "VC0", expected_sha256=info0["sha256"],
            bifacial_updates={"ground_albedo": 0.30, "bifaciality_factor": 0.80, "height_above_ground_m": 1.2},
            dry_run=False,
        )
        info1 = self.store.inspect_parameters("UtilSheds.PRJ", "VC0")
        self.assertEqual(info1["sha256"], mounted["sha256"])
        bif1 = info1["orientations"][0]["bifacial"]
        self.assertEqual((bif1["enabled"], bif1["model"], bif1["ground_albedo"], bif1["height_above_ground_m"]),
                         (True, "Sheds2D", 0.3, 1.2))

        updated = self.store.update_parameters(
            "UtilSheds.PRJ", "VC0", expected_sha256=info1["sha256"],
            bifacial_updates={"ground_albedo": 0.45},
            dry_run=False,
        )
        self.assertEqual(updated["bifacial"]["action"], "updated")
        info2 = self.store.inspect_parameters("UtilSheds.PRJ", "VC0")
        bif2 = info2["orientations"][0]["bifacial"]
        self.assertEqual((bif2["ground_albedo"], bif2["bifaciality_factor"], bif2["height_above_ground_m"]),
                         (0.45, 0.8, 1.2))

        removed = self.store.update_parameters(
            "UtilSheds.PRJ", "VC0", expected_sha256=info2["sha256"],
            bifacial_updates={"enabled": False},
            dry_run=False,
        )
        self.assertEqual(removed["bifacial"]["action"], "removed")
        info3 = self.store.inspect_parameters("UtilSheds.PRJ", "VC0")
        self.assertNotIn("bifacial", info3["orientations"][0])

    def test_synthesize_utility_auto_pan_dimensions_and_overlap_guard(self):
        # Auto-extracts LargApp=1.032 / LongApp=1.996 from workspace Lab440.PAN and computes collector_width_m
        reg = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="RegularSheds", pan_name="Lab440.PAN", ond_name="Lab50.OND",
            shed_count=6, pitch_m=10.0, tilt_deg=20.0, module_rows=2, module_columns=15,
            bifacial={"ground_albedo": 0.30, "bifaciality_factor": 0.80, "n_sheds_user": 6},
        )
        us = reg["utility_summary"]
        self.assertEqual(us["module_dimension_source"], "pan:workspace")
        self.assertAlmostEqual(us["module_width_m"], 1.032, places=3)
        self.assertAlmostEqual(us["module_length_m"], 1.996, places=3)
        self.assertAlmostEqual(us["collector_width_m"], 2 * 1.996 + 0.02, places=3)
        self.assertIn("PVObject_1=pvShdArrayShed", reg["vc0_text"])
        self.assertNotIn("AxisOrig=2", reg["vc0_text"])
        self.assertIn("PanneauPVName=440W;1.032;1.996", reg["vc0_text"])
        self.store.create_project("UtilRegSheds.PRJ", reg["prj_text"], {"VC0": reg["vc0_text"]})
        self.assertTrue(self.store.validate_structure("UtilRegSheds.PRJ", "VC0")["valid"])

        # Overlapping rows (pitch <= collector_width) must fail closed
        with self.assertRaisesRegex(ValueError, "must exceed collector_width_m"):
            synthesize_utility_project(
                self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
                field_type="Sheds", pan_name="Lab440.PAN", ond_name="Lab50.OND",
                pitch_m=3.5, module_rows=2,
            )

        # TrackHorizNSAxis keeps AxisOrig=2 and Trackers2D
        trk = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="TrackHorizNSAxis", pan_name="Lab440.PAN", ond_name="Lab50.OND",
            shed_count=4, pitch_m=12.0, backtracking=True, min_phi_deg=-55.0, max_phi_deg=55.0,
            bifacial={"ground_albedo": 0.30, "bifaciality_factor": 0.80, "height_above_ground_m": 2.5, "n_sheds_user": 4},
        )
        self.assertIn("FieldType=TrackHorizNSAxis", trk["vc0_text"])
        self.assertIn("PVObject_1=pvShdArrayTracking", trk["vc0_text"])
        self.assertIn("AxisOrig=2", trk["vc0_text"])
        self.assertIn("BifacialModel=Trackers2D", trk["vc0_text"])
        self.store.create_project("UtilTracker.PRJ", trk["prj_text"], {"VC0": trk["vc0_text"]})
        self.assertTrue(self.store.validate_structure("UtilTracker.PRJ", "VC0")["valid"])
        info_t = self.store.inspect_parameters("UtilTracker.PRJ", "VC0")
        self.assertEqual(info_t["orientations"][0]["bifacial"]["model"], "Trackers2D")
        self.assertEqual(info_t["orientations"][0]["values"]["MinPhi"], "-55.0")

        # UnlimTracking has Trackers2D / UnlimSheds and no 3D scene
        utrk = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="UnlimTracking", pan_name="Lab440.PAN", ond_name="Lab50.OND",
            bifacial={"ground_albedo": 0.25},
        )
        self.assertIn("FieldType=UnlimTracking", utrk["vc0_text"])
        self.assertIn("ParamOrigin=UnlimSheds", utrk["vc0_text"])
        self.assertNotIn("pvShdArrayTracking", utrk["vc0_text"])

    def test_bifacial_sfi_var_group(self):
        self.assertIn("bifacial", VAR_GROUPS)
        for v in ("viGlobBak", "viGlobGnd", "viBeamBak", "viDifSBak", "viPRBifi"):
            self.assertIn(v, VAR_GROUPS["bifacial"])
        sfi_path = self.root / "Models" / "bifi.SFI"
        build_sfi(sfi_path, VAR_GROUPS["bifacial"])
        content = sfi_path.read_text(encoding="utf-8")
        self.assertIn("viGlobBak", content)
        self.assertIn("viPRBifi", content)

    def test_design_flow_utility_validation_boundaries(self):
        cli = _FakeUtilityCLI(self.root)
        # ground_layout/bifacial rejected on fixed_unshaded_grid
        bad_fixed = simulate_design(cli, self.store, spec={
            "scope": "fixed_unshaded_grid", "new_project": "BadFixed.PRJ", "year": 2024,
            "target_dc_kw": 100.0, "tilt": 20.0, "azimuth": 0.0,
            "module": {"file_name": "Lab440.PAN"}, "inverter": {"file_name": "Lab50.OND"},
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            "capacity": _capacity_spec(),
            "bifacial": {"ground_albedo": 0.30},
        }, dry_run=True)
        self.assertEqual((bad_fixed["status"], bad_fixed["stage"]), ("blocked", "spec"))
        self.assertTrue(any(i["field"] == "bifacial" for i in bad_fixed["issues"]))

        # tilt rejected on ground_tracker
        bad_trk = simulate_design(cli, self.store, spec={
            "scope": "ground_tracker", "new_project": "BadTrk.PRJ", "year": 2024,
            "target_dc_kw": 100.0, "tilt": 20.0,
            "module": {"file_name": "Lab440.PAN"}, "inverter": {"file_name": "Lab50.OND"},
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            "capacity": _capacity_spec(),
        }, dry_run=True)
        self.assertEqual((bad_trk["status"], bad_trk["stage"]), ("blocked", "spec"))
        self.assertTrue(any(i["field"] == "tilt" for i in bad_trk["issues"]))

        # tracker-only keys rejected on ground_sheds
        bad_shed = simulate_design(cli, self.store, spec={
            "scope": "ground_sheds", "new_project": "BadShed.PRJ", "year": 2024,
            "target_dc_kw": 100.0, "tilt": 20.0, "azimuth": 0.0,
            "module": {"file_name": "Lab440.PAN"}, "inverter": {"file_name": "Lab50.OND"},
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            "capacity": _capacity_spec(),
            "ground_layout": {"backtracking": True},
        }, dry_run=True)
        self.assertEqual((bad_shed["status"], bad_shed["stage"]), ("blocked", "spec"))
        self.assertTrue(any(i["field"] == "ground_layout.backtracking" for i in bad_shed["issues"]))

        # template_project rejected on utility scopes
        bad_tpl = simulate_design(cli, self.store, spec={
            "scope": "ground_sheds", "new_project": "BadTpl.PRJ", "year": 2024,
            "target_dc_kw": 100.0, "tilt": 20.0, "azimuth": 0.0,
            "template_project": "Any.PRJ",
            "module": {"file_name": "Lab440.PAN"}, "inverter": {"file_name": "Lab50.OND"},
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            "capacity": _capacity_spec(),
        }, dry_run=True)
        self.assertEqual((bad_tpl["status"], bad_tpl["stage"]), ("blocked", "spec"))
        self.assertTrue(any(i["field"] == "template_project" for i in bad_tpl["issues"]))

        # Monofacial .PAN (Flags=$00 without bit 23) must be blocked at preflight when bifacial is requested
        (self.root / "ComposPV/PVmodules/Lab440Mono.PAN").write_bytes(
            b"\xef\xbb\xbf" + UTILITY_MONO_PAN.replace("\n", "\r\n").encode("utf-8")
        )
        fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal):
            bad_mono = simulate_design(cli, self.store, spec={
                "scope": "ground_sheds", "new_project": "BadMonoBifi.PRJ", "year": 2024,
                "target_dc_kw": 100.0, "tilt": 20.0, "azimuth": 0.0,
                "module": {"file_name": "Lab440Mono.PAN"}, "inverter": {"file_name": "Lab50.OND"},
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                "capacity": _capacity_spec(),
                "bifacial": {"ground_albedo": 0.30, "bifaciality_factor": 0.80},
            }, dry_run=True)
        self.assertEqual((bad_mono["status"], bad_mono["stage"]), ("blocked", "preflight"))
        self.assertTrue(any("bit 23" in i["message"] for i in bad_mono["issues"]))

    def test_simulate_and_prepare_utility_ground_sheds_and_trackers_end_to_end(self):
        cli = _FakeUtilityCLI(self.root, year=2024)
        fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}

        # 1) ground_sheds (3D + bifacial) via SingleDesignRequest schema -> dry_run then real run
        shed_req = SingleDesignRequest(
            scope="ground_sheds",
            new_project="GroundShed3D.PRJ",
            year=2024,
            target_dc_kw=100.0,
            tilt=22.0,
            azimuth=0.0,
            module={"file_name": "Lab440.PAN"},
            inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            capacity=_capacity_spec(),
            ground_layout={"model_mode": "3d", "pitch_m": 11.0, "shed_count": 6, "module_rows": 2, "module_columns": 19},
            bifacial={"ground_albedo": 0.30, "bifaciality_factor": 0.80, "height_above_ground_m": 1.2},
        )
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal):
            dry = simulate_design(cli, self.store, spec=shed_req.model_dump(exclude_none=True), dry_run=True)
            self.assertEqual((dry["status"], dry["stage"]), ("ready", "preflight"))
            self.assertEqual(dry["utility_summary"]["field_type"], "RegularSheds")
            self.assertTrue(dry["utility_summary"]["include_3d_scene"])
            self.assertTrue(dry["utility_summary"]["bifacial_enabled"])
            self.assertFalse((self.root / "Projects" / "GroundShed3D.PRJ").exists())

            ran = simulate_design(cli, self.store, spec=shed_req.model_dump(exclude_none=True), dry_run=False)
            self.assertEqual((ran["status"], ran["stage"]), ("completed", "completed"))
            self.assertTrue(ran["native_consumption_verified"])
            self.assertGreater(ran["bifacial_metrics"]["rear_irradiation_kwh_m2"], 0.0)
            self.assertEqual(len(cli.runs), 1)
            sfi_text = Path(cli.runs[0]["sfi"]).read_text(encoding="utf-8")
            self.assertIn("viGlobBak", sfi_text)
            self.assertIn("viGlobShd", sfi_text)
            info = self.store.inspect_parameters("GroundShed3D.PRJ", "VC0")
            self.assertEqual(info["orientations"][0]["bifacial"]["model"], "Sheds2D")
            self.assertEqual(info["orientations"][0]["bifacial"]["n_sheds_user"], 6)

        # 2) ground_tracker (3D + bifacial) via prepare_design_project (no CLI simulation)
        trk_req = SingleDesignRequest(
            scope="ground_tracker",
            new_project="GroundTracker3D.PRJ",
            year=2024,
            target_dc_kw=100.0,
            module={"file_name": "Lab440.PAN"},
            inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            capacity=_capacity_spec(),
            ground_layout={
                "model_mode": "3d", "pitch_m": 12.0, "shed_count": 6, "module_columns": 19,
                "min_phi_deg": -55.0, "max_phi_deg": 55.0, "backtracking": True,
            },
            bifacial={"ground_albedo": 0.28, "bifaciality_factor": 0.78, "height_above_ground_m": 2.4},
        )
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal):
            prep = prepare_design_project(cli, self.store, spec=trk_req.model_dump(exclude_none=True), dry_run=False)
            self.assertEqual((prep["status"], prep["stage"], prep["prepared"]), ("prepared", "prepared", True))
            self.assertEqual(len(cli.runs), 1)  # no additional simulation launched
            info_t = self.store.inspect_parameters("GroundTracker3D.PRJ", "VC0")
            self.assertEqual(info_t["orientations"][0]["values"]["FieldType"], "TrackHorizNSAxis")
            self.assertEqual(info_t["orientations"][0]["values"]["MinPhi"], "-55.0")
            self.assertEqual(info_t["orientations"][0]["bifacial"]["model"], "Trackers2D")


    def test_utility_rejects_options_unavailable_in_model_mode(self):
        cli = _FakeUtilityCLI(self.root)
        req = SingleDesignRequest(
            scope="ground_tracker", new_project="Geometry.PRJ", year=2024, target_dc_kw=100.0,
            module={"file_name": "Lab440.PAN"}, inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            capacity=_capacity_spec(),
            ground_layout={"model_mode": "3d", "shed_count": 6, "module_columns": 19},
        ).model_dump(exclude_none=True)
        checks = (
            ({"electrical_effect": True}, "requires model_mode='unlimited'"),
            ({"model_mode": "unlimited", "wind_stow": True}, "requires model_mode='3d'"),
        )
        for overrides, error in checks:
            spec = {**req, "ground_layout": {**req["ground_layout"], **overrides}}
            for tool in (prepare_design_project, simulate_design):
                for dry_run in (True, False):
                    with self.subTest(overrides=overrides, tool=tool.__name__, dry_run=dry_run):
                        result = tool(cli, self.store, spec=spec, dry_run=dry_run)
                        self.assertEqual(result["status"], "blocked")
                        self.assertIn(error, str(result["issues"]))
                        self.assertFalse(list((self.root / "Projects").iterdir()))
                        self.assertFalse(list((self.root / "Results").iterdir()))
        self.assertEqual(cli.runs, [])

    def test_partial_bifacial_edit_preserves_native_omissions_and_flags(self):
        # 1. Commercial-style shed pvBiFacial: PitchUser absent, NShedsUser=-9999, ShedTransparentFrac=0.005.
        shed_synth = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="RegularSheds", pan_name="Lab440.PAN", ond_name="Lab50.OND",
            bifacial={"ground_albedo": 0.30, "bifaciality_factor": 0.80, "height_above_ground_m": 0.5},
        )
        comm_vc = (
            shed_synth["vc0_text"]
            .replace("      MismatchLossFactor=0.100\r\n",
                     "      MismatchLossFactor=0.150\r\n      ShedTransparentFrac=0.005\r\n")
            .replace("        PitchUser=-1.000\r\n", "")
            .replace("        NShedsUser=-1\r\n", "        NShedsUser=-9999\r\n")
        )
        self.store.create_project("CommBifi.PRJ", shed_synth["prj_text"], {"VC0": comm_vc})
        info_c = self.store.inspect_parameters("CommBifi.PRJ", "VC0")
        upd_c = self.store.update_parameters(
            "CommBifi.PRJ", "VC0", expected_sha256=info_c["sha256"],
            bifacial_updates={"ground_albedo": 0.35}, dry_run=False,
        )
        self.assertEqual(upd_c["bifacial"]["action"], "updated")
        saved_c = (self.root / "Projects/CommBifi.VC0").read_text(encoding="utf-8-sig")
        self.assertIn("GroundAlbedo=0.350", saved_c)
        self.assertNotIn("PitchUser=", saved_c)
        self.assertIn("NShedsUser=-9999", saved_c)
        self.assertIn("ShedTransparentFrac=0.005", saved_c)

        # 2. Utility-style tracker pvBiFacial: tracker model Flags=$02 must stay $02.
        trk_synth = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="TrackHorizNSAxis", pan_name="Lab440.PAN", ond_name="Lab50.OND",
            bifacial={"ground_albedo": 0.30, "bifaciality_factor": 0.80, "height_above_ground_m": 2.5},
        )
        util_vc = trk_synth["vc0_text"].replace(
            "      PVObject_BiFacialTrackerModel=pvBiFacialTrackers\r\n        Flags=$00\r\n",
            "      PVObject_BiFacialTrackerModel=pvBiFacialTrackers\r\n        Flags=$02\r\n",
        )
        self.store.create_project("UtilBifi.PRJ", trk_synth["prj_text"], {"VC0": util_vc})
        info_u = self.store.inspect_parameters("UtilBifi.PRJ", "VC0")
        upd_u = self.store.update_parameters(
            "UtilBifi.PRJ", "VC0", expected_sha256=info_u["sha256"],
            bifacial_updates={"ground_albedo": 0.35}, dry_run=False,
        )
        self.assertEqual(upd_u["bifacial"]["action"], "updated")
        saved_u = (self.root / "Projects/UtilBifi.VC0").read_text(encoding="utf-8-sig")
        self.assertIn("GroundAlbedo=0.350", saved_u)
        self.assertIn("PVObject_BiFacialTrackerModel=pvBiFacialTrackers\n        Flags=$02\n", saved_u)

    def test_ground_3d_rejects_chord_override_pitch_overlap_and_module_count_mismatch(self):
        cli = _FakeUtilityCLI(self.root, year=2024)
        fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}

        # 1. Tracker pitch smaller than 3D scene chord (4.012 m) even when collector_width_m=3.0 is passed.
        trk_req = SingleDesignRequest(
            scope="ground_tracker", new_project="TightPitch.PRJ", year=2024, target_dc_kw=100.0,
            module={"file_name": "Lab440.PAN"}, inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            capacity=_capacity_spec(),
            ground_layout={"model_mode": "3d", "shed_count": 6, "module_columns": 19,
                           "collector_width_m": 3.0, "pitch_m": 3.5},
        ).model_dump(exclude_none=True)
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal):
            for dry_run in (True, False):
                res = simulate_design(cli, self.store, spec=trk_req, dry_run=dry_run)
                self.assertEqual(res["status"], "blocked", res)
                self.assertTrue(any("4.012" in i["message"] for i in res["issues"]), res["issues"])
            self.assertEqual(list((self.root / "Projects").iterdir()), [])

        # 2. Regular 3D shed scene module count (500) vs electrical count (228) is blocked with both counts.
        shed_req = SingleDesignRequest(
            scope="ground_sheds", new_project="CountMismatch.PRJ", year=2024, target_dc_kw=100.0,
            tilt=20.0, azimuth=0.0,
            module={"file_name": "Lab440.PAN"}, inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            capacity=_capacity_spec(),
            ground_layout={"model_mode": "3d"},
        ).model_dump(exclude_none=True)
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal):
            for dry_run in (True, False):
                res = prepare_design_project(cli, self.store, spec=shed_req, dry_run=dry_run)
                self.assertEqual(res["status"], "blocked", res)
                msg = " ".join(i["message"] for i in res["issues"])
                self.assertIn("500", msg)
                self.assertIn("228", msg)
            self.assertEqual(list((self.root / "Projects").iterdir()), [])

    def test_variant_update_rejects_mounting_bifacial_on_monofacial_pan(self):
        (self.root / "ComposPV/PVmodules/Mono440.PAN").write_bytes(
            b"\xef\xbb\xbf" + UTILITY_MONO_PAN.replace("\n", "\r\n").encode("utf-8")
        )
        cli = _FakeUtilityCLI(self.root, year=2024)
        fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}
        req = SingleDesignRequest(
            scope="ground_sheds", new_project="MonoShed.PRJ", year=2024, target_dc_kw=100.0,
            tilt=20.0, azimuth=0.0,
            module={"file_name": "Mono440.PAN"}, inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            capacity=_capacity_spec(),
            ground_layout={"model_mode": "unlimited"},
        ).model_dump(exclude_none=True)
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal):
            prep = prepare_design_project(cli, self.store, spec=req, dry_run=False)
        self.assertEqual(prep["status"], "prepared", prep)
        info = self.store.inspect_parameters("MonoShed.PRJ", "VC0")
        with self.assertRaisesRegex(ValueError, "monofacial.*bit 23"):
            self.store.update_parameters(
                "MonoShed.PRJ", "VC0", expected_sha256=info["sha256"],
                bifacial_updates={"ground_albedo": 0.30}, dry_run=True,
            )

    def test_utility_preserves_default_loss_bits_unless_explicitly_set(self):
        # Omitted pmism_mpp / module_quality_loss keep both default bits set ($001E0002 / $001E0402).
        default_synth = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="Sheds", pan_name="Lab440.PAN", ond_name="Lab50.OND",
        )
        self.assertIn("Flags=$001E0002", default_synth["vc0_text"])

        explicit_synth = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="Sheds", pan_name="Lab440.PAN", ond_name="Lab50.OND",
            pmism_mpp=1.5, module_quality_loss=-0.8,
        )
        self.assertIn("Flags=$000A0002", explicit_synth["vc0_text"])
        self.assertIn("PMismMPP=1.50", explicit_synth["vc0_text"])
        self.assertIn("PQualityModU=-0.80", explicit_synth["vc0_text"])

    def test_extract_bifacial_metrics_handles_subhourly_and_wh_m2_units(self):
        from pvsyst_auto.native.cli import result_units
        from pvsyst_auto.workflows.utility import _extract_bifacial_metrics

        stamps = ("01/06/24 10:00", "01/06/24 10:15", "01/06/24 10:30", "01/06/24 10:45")
        for label, g_unit, g_val, m_unit, m_val in (
            ("w_m2_kw", "W/m2", "400.0", "kW", "2.0"),
            ("wh_m2_kwh", "Wh/m2", "100.0", "kWh", "0.5"),
        ):
            with self.subTest(label=label):
                csv_path = self.root / "Results" / f"bifi_{label}.csv"
                lines = [
                    "PVSYST 8.1.7",
                    "date;GlobBak;GlobGnd;MismBak;PRBifi",
                    f";{g_unit};{g_unit};{m_unit};ratio",
                ] + [f"{s};{g_val};{g_val};{m_val};0.85" for s in stamps]
                csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                metrics = _extract_bifacial_metrics(csv_path, result_units(csv_path, strict=True))
                # 4 * 15 min = 1 h: 400 W/m2 * 1 h = 0.4 kWh/m2, 2 kW * 1 h = 2.0 kWh
                self.assertAlmostEqual(metrics["rear_irradiation_kwh_m2"], 0.4, places=6)
                self.assertAlmostEqual(metrics["ground_irradiation_kwh_m2"], 0.4, places=6)
                self.assertAlmostEqual(metrics["rear_mismatch_loss_kwh"], 2.0, places=6)
                self.assertAlmostEqual(metrics["mean_pr_bifi"], 0.85, places=6)


if __name__ == "__main__":
    unittest.main()

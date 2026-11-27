"""External-contract unit tests for PR 5: zero-template pvAgeing, economics curves, & feasibility sweep."""
from __future__ import annotations

from datetime import datetime, timedelta
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.api.context import TOOL_GROUPS
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.engineering.economics import evaluate_economics
from pvsyst_auto.native.cli import PVsystCLI
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.workflows.design import prepare_design_project
from pvsyst_auto.workflows.optimize import run_feasibility_study
from pvsyst_auto.workflows.simulation import simulate_template
from tests._fixtures import init_full_workspace, write_sfi_csv, write_utf8_crlf
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN, _FakeUtilityCLI
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


def _make_batch_ageing_csv(path: Path, rows: list[tuple[str, int, float, str]], *, unit: str = "kWh") -> Path:
    lines = [
        "PVsyst simulations Batch mode;;;;;;;",
        "Ident;Create hourly;Simulation;Simul;Error;E_Grid;PR",
        f";file;for Year;Comment;;[{unit}];ratio",
    ]
    for sid, yr, e_val, err in rows:
        lines.append(f"{sid};;{yr};Year_{yr};{err};{e_val:.3f};0.850")
    write_utf8_crlf(path, "\n".join(lines) + "\n", bom=True)
    return path


def _make_tou_sfi_csv(path: Path, *, days: int = 365) -> Path:
    cols = [("E_Grid", "kW"), ("E_User", "kW"), ("EFrGrid", "kW")]
    rows: list[list] = []
    start = datetime(1990, 1, 1, 0, 0)
    for i in range(days * 24):
        stamp = start + timedelta(hours=i)
        h = stamp.hour
        load = 40.0 if 8 <= h < 20 else 10.0
        pv = 100.0 if 10 <= h < 16 else 0.0
        rows.append([stamp.strftime("%d/%m/%y %H:%M"), round(max(0.0, pv - load), 4), round(load, 4), round(max(0.0, load - pv), 4)])
    return write_sfi_csv(path, cols, rows, project="TouAge.PRJ", site="Geneva_MN90.SIT", met="Geneva_MN90_SYN.MET", variant="TouAge.VC0")


class ZeroTemplateAgeingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = init_full_workspace(Path(self._tmp.name))
        (self.ws / "UserBatch").mkdir(exist_ok=True)
        (self.ws / "UserHourly").mkdir(exist_ok=True)
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV" / "PVmodules" / "Bifi440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        (self.ws / "ComposPV" / "Inverters" / "Inv50.OND").write_text(UTILITY_OND, encoding="utf-8")
        self.components = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.components)
        self.cli = _FakeUtilityCLI(self.ws, year=1990, nominal_dc_kw=52.80, use_sfi_writer=False)

    def test_tool_budget_preserved(self) -> None:
        all_tools = {name for group in TOOL_GROUPS.values() for name in group}
        default_tools = all_tools - set(TOOL_GROUPS["Admin & Diagnostics"]) - set(TOOL_GROUPS["Expert & Low-Level"])
        self.assertEqual(len(default_tools), 48)
        self.assertEqual(len(all_tools), 75)

    def test_zero_template_mount_toggle_update_and_multi_subarray_support(self) -> None:
        # Prepare a multi-subarray asymmetric MPPT project without any pvAgeing block
        prep = prepare_design_project(
            self.cli,
            self.store,
            dry_run=False,
            spec={
                "scope": "ground_sheds",
                "new_project": "MountAge.PRJ",
                "year": 1990,
                "sizing_mode": "auto_from_components",
                "target_dc_kw": 52.36,
                "auto_sizing": {"capacity_tolerance_pct": 0.5},
                "tilt": 20.0,
                "azimuth": 0.0,
                "module": {"file_name": "Bifi440.PAN"},
                "inverter": {"file_name": "Inv50.OND"},
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            },
        )
        self.assertIn(prep["status"], ("prepared", "needs_review"), prep)
        insp0 = self.store.inspect_parameters("MountAge.PRJ", "VC0")
        self.assertEqual(insp0["ageing"], {"mode": "absent", "enabled": False})

        # Enabling absent ageing block fails cleanly; disabling absent block is a no-op
        with self.assertRaisesRegex(ValueError, "mounting PVObject_Ageing"):
            self.store.update_parameters("MountAge.PRJ", "VC0", insp0["sha256"], 1, 1, ageing_enabled=True)
        noop_dis = self.store.update_parameters("MountAge.PRJ", "VC0", insp0["sha256"], 1, 1, ageing_enabled=False, dry_run=True)
        self.assertFalse(noop_dis["changed"])

        # Mount zero-template pvAgeing on multi-subarray variant
        mounted = self.store.update_parameters(
            "MountAge.PRJ", "VC0", insp0["sha256"], 1, 1,
            ageing_updates={
                "simulation_year": 10,
                "annual_module_degradation_percent": 0.50,
                "imp_degrad_sharing_percent": 75.0,
                "keep_mc_values": True,
                "modules_rms_year_isc_percent": 0.25,
                "modules_rms_year_voc_percent": 0.15,
            },
        )
        self.assertTrue(mounted["changed"])
        self.assertTrue(mounted["ageing"]["mounted"])
        self.assertTrue(self.store.validate_structure("MountAge.PRJ", "VC0")["valid"])

        insp1 = self.store.inspect_parameters("MountAge.PRJ", "VC0")
        self.assertEqual(insp1["main_array"]["values"]["Flags2"], "$0010082A")
        self.assertEqual(insp1["ageing"]["mode"], "mounted")
        self.assertTrue(insp1["ageing"]["enabled"])
        self.assertEqual(insp1["ageing"]["simulation_year"], 10)
        self.assertEqual(insp1["ageing"]["annual_module_degradation_percent"], 0.50)
        self.assertEqual(insp1["ageing"]["imp_degrad_sharing_percent"], 75.0)
        self.assertTrue(insp1["ageing"]["keep_mc_values"])
        self.assertTrue(insp1["ageing"]["has_cubic_profile"])
        self.assertAlmostEqual(insp1["ageing"]["stored_loss_factor_percent"], 4.75, places=2)

        # Subsequent whole-system edit (soiling) still works with Flags2=$0010082A
        soiled = self.store.update_parameters(
            "MountAge.PRJ", "VC0", insp1["sha256"], 1, 1,
            soiling_updates={"mode": "constant", "yearly_loss_pct": 2.0},
        )
        self.assertTrue(soiled["changed"])

        # Toggle ageing_enabled off while preserving custom Flags2 bit ($0010182A -> $182A -> $0010182A)
        vc_path = self.ws / "Projects" / "MountAge.VC0"
        vc_path.write_text(vc_path.read_text(encoding="utf-8").replace("Flags2=$0010082A", "Flags2=$0010182A"), encoding="utf-8")
        sha_custom = self.store.inspect("MountAge.PRJ", "VC0")["sha256"]
        dis = self.store.update_parameters("MountAge.PRJ", "VC0", sha_custom, 1, 1, ageing_enabled=False)
        self.assertEqual(dis["ageing"]["after"]["enabled"], False)
        self.assertEqual(self.store.inspect_parameters("MountAge.PRJ", "VC0")["main_array"]["values"]["Flags2"], "$182A")
        en = self.store.update_parameters("MountAge.PRJ", "VC0", dis["sha256"], 1, 1, ageing_enabled=True)
        self.assertTrue(en["ageing"]["after"]["enabled"])
        self.assertEqual(self.store.inspect_parameters("MountAge.PRJ", "VC0")["main_array"]["values"]["Flags2"], "$0010182A")

        # High year * deg clamps DegradLossFactor & AgeingLossPure at 100% cleanly
        hi = self.store.update_parameters(
            "MountAge.PRJ", "VC0", en["sha256"], 1, 1,
            ageing_updates={"simulation_year": 40, "annual_module_degradation_percent": 3.0},
        )
        self.assertEqual(hi["ageing"]["after"]["stored_loss_factor_percent"], 100.0)
        vc_text = vc_path.read_text(encoding="utf-8")
        self.assertIn("DegradLossFactor=100.00", vc_text)
        self.assertIn("AgeingLossPure=TCubicProfile", vc_text)

    def test_native_demo_commercial_ageing_shape_readable_and_editable(self) -> None:
        vc_src = (
            "PVObject_=pvVCalcul\r\n  Comment=DemoStyle\r\n  Version=8.1.7\r\n  Flags=$00100001\r\n  PRCorrTemp=False\r\n"
            "  Orientations, list of=1 TOrientGroup\r\n    PVObject_=pvOrient\r\n      Version=8.1.7\r\n      Flags=$00\r\n"
            "      NoOrient=1\r\n      FieldType=FixedPlane\r\n      FieldTilt=20.0\r\n      FieldAzim=0.0\r\n"
            "    End of TOrientGroup\r\n  End of Orientations\r\n"
            "  PVObject_PVMainArray=pvMainArray\r\n    Flags=$03B00440\r\n  Flags2=$0010082A\r\n  UFactor=29.0\r\n"
            "  NoYearSimul=10\r\n  DegradLossFactor=3.80\r\n  DegradMismFactor=1.97\r\n"
            "  PVObject_Ageing=pvAgeing\r\n    Flags=$00\r\n    KeepMCValues=True\r\n    PlotKind=1\r\n"
            "    CurveWarranty=1\r\n    ProgressMode=1\r\n    NSigmaLimit=2.0\r\n    ModuleDegradFactor=0.40\r\n"
            "    ImpDegradSharing=80.0\r\n    ModulesRMSYearISC=0.40\r\n    ModulesRMSYearVoc=0.40\r\n"
            "    AgeingLossPure=TCubicProfile\r\n      NPtsMax=7\r\n      NPtsEff=7\r\n      Mode=3\r\n"
            "      Point_1=0.0,0.000\r\n      Point_2=5.0,2.000\r\n      Point_3=10.0,4.000\r\n"
            "      Point_4=15.0,6.000\r\n      Point_5=20.0,8.000\r\n      Point_6=25.0,10.000\r\n      Point_7=30.0,12.000\r\n"
            "    End of TCubicProfile\r\n"
            "    AgeingLossAver=TCubicProfile\r\n      NPtsMax=7\r\n      NPtsEff=7\r\n      Point_1=0.0,-0.004\r\n"
            "    End of TCubicProfile\r\n  End of TAgeing\r\n"
            "  PVObject_SystemCircuit=pvCircuit\r\n    Version=8.1.7\r\n    Flags=$00\r\n"
            "    InjectionPointNode Start;\r\n      NElements=1\r\n      Children=Start\r\n"
            "        InverterNode Start;\r\n          NElements=1\r\n          SubArrayId=1\r\n"
            "          SubArrayName=PV Array\r\n          Children=Start\r\n"
            "            MPPTNode Start;\r\n              NElements=2\r\n              SubArrayId=1\r\n"
            "              SubArrayName=PV Array\r\n              Children=Start\r\n"
            "                StringNode Start;\r\n                  NElements=4\r\n                  SubArrayId=1\r\n"
            "                  SubArrayName=PV Array\r\n                StringNode End;\r\n"
            "              Children=End\r\n            MPPTNode End;\r\n          Children=End\r\n"
            "        InverterNode End;\r\n      Children=End\r\n    InjectionPointNode End;\r\n  End of PVObject pvCircuit\r\n"
            "  SubArrays=Start\r\n    PVObject_=pvSubArray\r\n      Comment=PV Array\r\n      Version=8.1.7\r\n"
            "      Flags=$001E0402\r\n      Flags2=$20\r\n      SubArrayId=1\r\n      PVModule=Bifi440.PAN\r\n"
            "      GInverter=Inv50.OND\r\n      NoOrientation=1\r\n      NModSerie=17\r\n      NStringCh=8\r\n"
            "      NStrOrient1=8\r\n      NInverter=1\r\n      NInvMPPT=2\r\n    End of PVObject pvSubArray\r\n"
            "  EndTags\r\n  End of PVObject pvMainArray\r\n  PVObject_System=pvSystem\r\n    Flags=$00\r\n    SystemType=Grid\r\n"
            "  End of PVObject pvSystem\r\nEnd of PVObject pvVCalcul\r\n"
        )
        write_utf8_crlf(self.ws / "Projects" / "DemoNative.PRJ", "PVObject_=pvProject\r\n  Version=8.1.7\r\n  Flags=$00\r\nEnd of PVObject pvProject\r\n")
        write_utf8_crlf(self.ws / "Projects" / "DemoNative.VC0", vc_src)
        insp = self.store.inspect_parameters("DemoNative.PRJ", "VC0")
        self.assertEqual(insp["ageing"]["mode"], "mounted")
        self.assertEqual(insp["ageing"]["stored_mismatch_factor_percent"], 1.97)
        self.assertTrue(insp["ageing"]["has_cubic_profile"])
        res = self.store.update_parameters(
            "DemoNative.PRJ", "VC0", insp["sha256"], 1, 1,
            ageing_updates={"simulation_year": 25, "annual_module_degradation_percent": 0.60},
        )
        self.assertFalse(res["ageing"]["mounted"])
        self.assertEqual(res["ageing"]["after"]["simulation_year"], 25)
        self.assertIn("Point_7=30.0,18.000", (self.ws / "Projects" / "DemoNative.VC0").read_text(encoding="utf-8"))

    def test_zero_template_utility_and_template_flows_accept_losses_ageing(self) -> None:
        for scope, name, extra in (
            ("ground_sheds", "ShedAge.PRJ", {"tilt": 20.0, "azimuth": 0.0}),
            ("ground_tracker", "TrkAge.PRJ", {"azimuth": 0.0}),
        ):
            with self.subTest(scope=scope):
                res = prepare_design_project(
                    self.cli,
                    self.store,
                    dry_run=False,
                    spec={
                        "scope": scope,
                        "new_project": name,
                        "year": 1990,
                        "sizing_mode": "auto_from_components",
                        "target_dc_kw": 52.80,
                        "losses": {
                            "UFactor": 25.0,
                            "ageing": {"simulation_year": 12, "annual_module_degradation_percent": 0.45},
                        },
                        **extra,
                        "module": {"file_name": "Bifi440.PAN"},
                        "inverter": {"file_name": "Inv50.OND"},
                        "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                    },
                )
                self.assertIn(res["status"], ("prepared", "needs_review"), res)
                self.assertEqual(res["utility_summary"]["ageing"]["simulation_year"], 12)
                self.assertTrue(self.store.validate_structure(name, "VC0")["valid"])
                insp = self.store.inspect_parameters(name, "VC0")
                self.assertEqual(insp["main_array"]["values"]["Flags2"], "$0010082A")
                self.assertEqual(insp["ageing"]["simulation_year"], 12)
                self.assertEqual(insp["ageing"]["annual_module_degradation_percent"], 0.45)
                # Subsequent UVFactor/UFactor edit succeeds cleanly when Flags2=$0010082A
                uf_edit = self.store.update_parameters(name, "VC0", insp["sha256"], 1, 1, main_array_updates={"UFactor": 27.0})
                self.assertTrue(uf_edit["changed"])

        # Seed / template path also accepts losses.ageing cleanly
        write_utf8_crlf(
            self.ws / "Projects" / "SeedAge.PRJ",
            (self.ws / "Projects" / "ShedAge.PRJ").read_text(encoding="utf-8-sig"),
            bom=True,
        )
        seed_vc = (self.ws / "Projects" / "ShedAge.VC0").read_text(encoding="utf-8-sig")
        seed_vc = seed_vc.replace("PVObject_PVMainArray=pvMainArray", "  Flags=$00100400").replace(
            "  Flags=$00100400\r\n    Flags=$00100400", "PVObject_PVMainArray=pvMainArray\r\n    Flags=$00100400"
        )
        # Simpler: inspect ShedAge VC0 via strip or use simulate_template on an unshaded FixedPlane template
        from pvsyst_auto.project.synthesize import synthesize_project_and_variant
        fixed = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Bifi440.PAN", ond_name="Inv50.OND", n_mod_serie=17, n_string_ch=6,
            n_inverter=1, n_inv_mppt=2, pnom_ac_in_mppt=25.0,
        )
        self.store.create_project("FixedSeed.PRJ", fixed["prj_text"], {"VC0": fixed["vc0_text"]})
        tpl_run = simulate_template(
            self.cli, self.store, template_project="FixedSeed.PRJ", variant="VC0", new_project="TplAgedRun.PRJ",
            pan_name="Bifi440.PAN", ond_name="Inv50.OND", site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            tilt=22.0, year=1990, losses={"ageing": {"simulation_year": 15, "annual_module_degradation_percent": 0.55}},
            dry_run=False,
        )
        self.assertEqual(tpl_run["status"], "completed", tpl_run)
        self.assertEqual(self.store.inspect_parameters("TplAgedRun.PRJ", "VC0")["ageing"]["simulation_year"], 15)

    def test_economics_piecewise_ageing_curve_and_batch_integration(self) -> None:
        # 1. Explicit yearly_energy_kwh with JSON string keys & linear interpolation/extrapolation (no double Year-1 deg)
        res = evaluate_economics(
            scenarios=[{
                "label": "ExplicitCurve",
                "yearly_energy_kwh": {"1": 100000.0, "10": 95500.0, "20": 90500.0},
                "installed_dc_kw": 100.0,
                "capex_total": 350000.0,
                "annual_opex": 5000.0,
            }],
            financial={"project_life_years": 25, "first_year_degradation_pct": 3.0, "annual_degradation_pct": 1.0},
            tariff={"mode": "feed_in", "feed_in_tariff": 0.40},
            sensitivity={"enabled": True, "variation_pct": 20.0},
        )
        sc = res["scenarios"][0]
        self.assertEqual(sc["degradation_source"], "ageing_curve")
        self.assertEqual(sc["year1_energy_kwh"], 100000.0)
        sched = sc["financials"]["cashflow_schedule"]
        self.assertEqual(len(sched), 25)
        self.assertEqual(sched[0]["energy_kwh"], 100000.0)
        self.assertAlmostEqual(sched[0]["degradation_factor"], 1.0, places=5)
        # Midpoint year 5: 100000 + (95500-100000)*(4/9) = 98000.0
        self.assertAlmostEqual(sched[4]["energy_kwh"], 98000.0, places=1)
        # Forward extrapolation year 25: 90500 + (-500/yr)*5 = 88000.0
        self.assertAlmostEqual(sched[24]["energy_kwh"], 88000.0, places=1)
        deg_row = next(r for r in sc["sensitivity"]["factors"] if r["factor"] == "degradation")
        self.assertGreater(deg_row["npv_swing"], 0.0)

        # 2. Standalone ageing_batch_csv + combined with hourly TOU csv_name (keeps TOU settlement!)
        bcsv = _make_batch_ageing_csv(
            self.ws / "UserBatch" / "Age_BatchResults.CSV",
            [("SIM_1", 1, 100.0, ""), ("SIM_2", 10, 95.0, ""), ("SIM_3", 25, 87.5, ""), ("SIM_4", 30, 0.0, "Error: skip")],
            unit="MWh",
        )
        standalone = evaluate_economics(
            workspace=self.ws,
            ageing_batch_csv=bcsv.name,
            financial={"project_life_years": 25, "fixed_capex": 300000.0},
            tariff={"mode": "feed_in", "feed_in_tariff": 0.40},
        )
        self.assertEqual(standalone["scenarios"][0]["year1_energy_kwh"], 100000.0)
        self.assertEqual(standalone["scenarios"][0]["financials"]["cashflow_schedule"][9]["energy_kwh"], 95000.0)
        self.assertEqual(standalone["scenarios"][0]["financials"]["cashflow_schedule"][24]["energy_kwh"], 87500.0)

        tou_csv = _make_tou_sfi_csv(self.ws / "Results" / "tou_base.csv", days=10)
        tou_cfg = {
            "mode": "tou", "import_tariff": 0.70, "feed_in_tariff": 0.35,
            "tou_schedule": [
                {"name": "peak", "hours": [10, 11, 12, 13, 14, 15], "import_rate": 1.20, "export_rate": 0.50},
                {"name": "other", "hours": [h for h in range(24) if h not in (10, 11, 12, 13, 14, 15)], "import_rate": 0.60, "export_rate": 0.30},
            ],
        }
        combo = evaluate_economics(
            workspace=self.ws,
            scenarios=[{"label": "TouAged", "csv_name": tou_csv.name, "installed_dc_kw": 100.0, "capex_total": 350000.0}],
            ageing_batch_csv=bcsv.name,
            financial={"project_life_years": 25},
            tariff=tou_cfg,
        )
        sc_tou = combo["scenarios"][0]
        self.assertEqual(sc_tou["degradation_source"], "ageing_curve")
        self.assertIn("settlement", sc_tou)
        self.assertAlmostEqual(sc_tou["financials"]["cashflow_schedule"][9]["degradation_factor"], 0.95, places=4)
        self.assertAlmostEqual(sc_tou["financials"]["cashflow_schedule"][24]["degradation_factor"], 0.875, places=4)

        # Invalid curve inputs fail closed
        for bad_kw in (
            {"scenarios": [{"label": "Both", "yearly_energy_kwh": {1: 100, 10: 90}, "ageing_batch_csv": bcsv.name}]},
            {"scenarios": [{"label": "One", "yearly_energy_kwh": {1: 100}}]},
            {"scenarios": [{"label": "Dup", "yearly_energy_kwh": {1: 100, "1": 95, 10: 90}}]},
            {"scenarios": [{"label": "Bool", "yearly_energy_kwh": {True: 100, 10: 90}}]},
        ):
            with self.assertRaises(ValueError):
                evaluate_economics(workspace=self.ws, financial={"project_life_years": 25}, **bad_kw)

    def test_feasibility_study_ageing_years_dry_run_and_execute(self) -> None:
        base_csv = _make_tou_sfi_csv(self.ws / "Results" / "fs_base.csv", days=36)
        from pvsyst_auto.project.synthesize import synthesize_project_and_variant
        synth = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Bifi440.PAN", ond_name="Inv50.OND", n_mod_serie=17, n_string_ch=6,
            n_inverter=1, n_inv_mppt=2, pnom_ac_in_mppt=25.0,
        )
        self.store.create_project("FsStudy.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})

        spec = {
            "study_name": "AgeFeas 2026",
            "existing_csv": base_csv.name,
            "existing_project": "FsStudy.PRJ",
            "existing_variant": "VC0",
            "installed_dc_kw": 100.0,
            "ageing_years": [25, 1, 10],
            "financial": {"currency": "CNY", "project_life_years": 25, "capex_per_kw": 3500.0, "annual_degradation_pct": 0.50},
            "tariff": {"mode": "feed_in", "feed_in_tariff": 0.45},
            "sensitivity": {"enabled": True, "variation_pct": 10.0},
        }
        prev = run_feasibility_study(self.cli, self.store, spec=spec, dry_run=True)
        self.assertEqual(prev["status"], "ready")
        self.assertEqual(prev["executive_summary"]["estimated_cli_calls"], 1)
        age_stage = next(s for s in prev["stages"] if s["stage"] == "ageing_sweep")
        self.assertEqual(age_stage["years"], [1, 10, 25])
        self.assertEqual(self.store.inspect_parameters("FsStudy.PRJ", "VC0")["ageing"]["mode"], "absent")

        fake_cli_exe = Path(self._tmp.name) / "PVsystCLI.exe"
        fake_cli_exe.write_bytes(b"stub")
        cli_run = PVsystCLI(fake_cli_exe, self.ws)

        def fake_batch(project, variant, *, batch_params, batch_rvt, **_):
            stem = Path(batch_params).name.replace("_BatchParams.CSV", "")
            summary = self.ws / "UserBatch" / f"{stem}_BatchResults.CSV"
            _make_batch_ageing_csv(summary, [("SIM_1", 1, 140000.0, ""), ("SIM_2", 10, 133000.0, ""), ("SIM_3", 25, 121500.0, "")])
            return {"batch_summary": str(summary), "batch_hourly": [], "batch_runs": 3, "seconds": 2, "warnings": []}

        with patch.object(cli_run, "license_info", return_value={"status": "ACTIVE"}), \
             patch.object(cli_run, "run_simulation", side_effect=fake_batch):
            done = run_feasibility_study(cli_run, self.store, spec=spec, dry_run=False, confirm=True)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["ageing_sweep"]["completed_count"], 3)
        self.assertEqual(self.store.inspect_parameters("FsStudy.PRJ", "VC0")["ageing"]["mode"], "mounted")
        self.assertEqual(done["economics"]["scenarios"][0]["degradation_source"], "ageing_curve")
        sched = done["economics"]["scenarios"][0]["financials"]["cashflow_schedule"]
        self.assertAlmostEqual(sched[9]["degradation_factor"], 133000.0 / 140000.0, places=4)
        self.assertAlmostEqual(sched[24]["degradation_factor"], 121500.0 / 140000.0, places=4)

        # Invalid ageing_years guards
        with self.assertRaisesRegex(ValueError, "ageing_years"):
            run_feasibility_study(self.cli, self.store, spec={**spec, "ageing_years": [1, 1]}, dry_run=True)
        with self.assertRaisesRegex(ValueError, "Grid PV systems"):
            run_feasibility_study(
                self.cli, self.store,
                spec={"study_name": "SA", "ageing_years": [1, 10], "design": {"scope": "stand_alone", "new_project": "S.PRJ", "year": 2024}},
                dry_run=True,
            )


if __name__ == "__main__":
    unittest.main()

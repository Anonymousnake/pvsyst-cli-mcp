"""Unit tests for physics loss-chain and time-series diagnostics engine and MCP tool."""
from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.engineering.diagnostics import (
    compact_diagnostics_summary,
    detect_native_refusal,
    diagnose_simulation_csv,
)
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.variants import VariantStore


def _write_csv(path: Path, columns: list[tuple[str, str]], rows: list[list]) -> Path:
    lines = [
        "Project;DiagTest.PRJ",
        "Geographical Site;DiagSite.SIT",
        "Weather data;DiagMet.MET",
        "Simulation variant;DiagTest.VC0",
        "date;" + ";".join(c for c, _ in columns),
        ";" + ";".join(u for _, u in columns),
    ]
    for r in rows:
        lines.append(";".join(str(v) for v in r))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _solar_rows(
    days: int = 3,
    *,
    step_minutes: int = 60,
    pr: float = 0.84,
    il_vmax: float = 0.0,
    il_pmax: float = 0.0,
    il_vmin: float = 0.0,
    il_imax: float = 0.0,
    glob_shd: float = 0.0,
    shd_elec: float = 0.0,
    slg_loss: float = 0.0,
    e_grd_lim: float = 0.0,
    glob_bak: float | None = None,
    e_user: float | None = None,
    e_miss: float | None = None,
    soc_end: float | None = None,
    night_export: float = 0.0,
    zero_day_hours: int = 0,
    egrid_gt_earray: bool = False,
    phase_shift_hours: int = 0,
) -> tuple[list[tuple[str, str]], list[list]]:
    cols = [
        ("GlobInc", "W/m2"),
        ("EArray", "kW"),
        ("E_Grid", "kW"),
        ("PR", "ratio"),
        ("IL_Vmax", "kW"),
        ("IL_Pmax", "kW"),
        ("IL_Vmin", "kW"),
        ("IL_Imax", "kW"),
        ("GlobHrz", "W/m2"),
        ("GlobShd", "W/m2"),
        ("ShdElec", "kW"),
        ("SlgLoss", "W/m2"),
        ("EGrdLim", "kW"),
    ]
    if glob_bak is not None:
        cols.append(("GlobBak", "W/m2"))
    if e_user is not None:
        cols.append(("E_User", "kW"))
    if e_miss is not None:
        cols.append(("E_Miss", "kW"))
    if soc_end is not None:
        cols.append(("SOC_End", "%"))

    start = datetime(2024, 6, 1, 0, 0)
    total_steps = int(days * 24 * 60 / step_minutes)
    rows: list[list] = []
    zero_remaining = int(zero_day_hours * 60 / step_minutes)

    for step in range(total_steps):
        stamp = start + timedelta(minutes=step * step_minutes)
        hour_f = stamp.hour + stamp.minute / 60.0
        # Simple symmetric solar curve 06:00..18:00 centered at 12:00
        if 6.0 <= hour_f < 18.0:
            gi = 800.0 * (1.0 - abs(hour_f - 12.0) / 6.0)
        else:
            gi = 0.0

        # Optionally shift power phase relative to irradiance
        p_hour = (hour_f - phase_shift_hours) % 24.0
        if 6.0 <= p_hour < 18.0:
            gi_p = 800.0 * (1.0 - abs(p_hour - 12.0) / 6.0)
        else:
            gi_p = 0.0

        earray = 100.0 * (gi_p / 1000.0) * pr if gi_p > 0 else 0.0
        if zero_remaining > 0 and gi >= 100.0:
            earray = 0.0
            zero_remaining -= 1
        egrid = (earray * 1.15 if egrid_gt_earray else earray * 0.98) if earray > 0 else 0.0
        if gi <= 0 and night_export > 0:
            egrid = night_export
        pr_val = pr if gi > 0 else ""

        row: list = [
            stamp.strftime("%d/%m/%y %H:%M"),
            round(gi, 3),
            round(earray, 4),
            round(egrid, 4),
            pr_val,
            il_vmax if gi > 0 else 0.0,
            il_pmax if gi > 0 else 0.0,
            il_vmin if gi > 0 else 0.0,
            il_imax if gi > 0 else 0.0,
            round(gi, 3),
            round(max(gi - glob_shd, 0.0), 3),
            shd_elec if gi > 0 else 0.0,
            slg_loss if gi > 0 else 0.0,
            e_grd_lim if gi > 0 else 0.0,
        ]
        if glob_bak is not None:
            row.append(glob_bak if gi > 0 else 0.0)
        if e_user is not None:
            row.append(e_user)
        if e_miss is not None:
            row.append(e_miss)
        if soc_end is not None:
            row.append(soc_end)
        rows.append(row)
    return cols, rows


class DiagnosticsEngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for sub in ("Projects", "Results", "ComposPV/PVmodules", "ComposPV/Inverters"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.components = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.components)

    def test_clean_monofacial_hourly_has_no_findings(self):
        cols, rows = _solar_rows(days=3, pr=0.84)
        csv_path = _write_csv(self.root / "Results" / "clean.csv", cols, rows)
        diag = diagnose_simulation_csv(csv_path)
        self.assertEqual(diag["status"], "ok")
        self.assertEqual(diag["max_severity"], "none")
        self.assertFalse(diag["needs_review"])
        self.assertEqual(diag["finding_count"], 0)
        self.assertEqual(diag["step_minutes"], 60.0)
        self.assertAlmostEqual(diag["weighted_pr"], 0.84, places=3)

        compact = compact_diagnostics_summary(diag)
        self.assertEqual(compact["status"], "ok")
        self.assertEqual(compact["top_findings"], [])

    def test_bifacial_pr_range_and_overbound_detection(self):
        # PR=0.98 is normal for bifacial (GlobBak > 0), but flagged for monofacial
        cols_b, rows_b = _solar_rows(days=2, pr=0.98, glob_bak=120.0)
        diag_b = diagnose_simulation_csv(_write_csv(self.root / "Results" / "bifi_ok.csv", cols_b, rows_b))
        self.assertTrue(diag_b["is_bifacial"])
        self.assertEqual(diag_b["status"], "ok")

        cols_m, rows_m = _solar_rows(days=2, pr=0.98)
        diag_m = diagnose_simulation_csv(_write_csv(self.root / "Results" / "mono_high.csv", cols_m, rows_m))
        self.assertFalse(diag_m["is_bifacial"])
        self.assertTrue(diag_m["needs_review"])
        self.assertEqual([f["code"] for f in diag_m["findings"]], ["PR_TOO_HIGH"])

    def test_inverter_limits_and_vc0_attribution(self):
        synth = synthesize_project_and_variant(
            None,
            project_comment="Diag Attribution",
            variant_comment="Base",
            pan_name="Test.PAN",
            ond_name="Test.OND",
            n_mod_serie=24,
            n_string_ch=12,
            n_inverter=2,
            n_inv_mppt=4,
            independent_mppt=True,
            pnom_ac_in_mppt=25.0,
        )
        self.store.create_project("DiagAttr.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})

        cols, rows = _solar_rows(
            days=2, pr=0.60, il_vmax=2.0, il_pmax=5.0, il_vmin=3.0, il_imax=2.0,
        )
        csv_path = _write_csv(self.root / "Results" / "inv_limits.csv", cols, rows)
        diag = diagnose_simulation_csv(csv_path, store=self.store, project="DiagAttr.PRJ", variant="VC0")
        codes = {f["code"] for f in diag["findings"]}
        self.assertTrue(diag["variant_attribution_used"])
        self.assertTrue(diag["needs_review"])
        self.assertTrue({
            "PR_TOO_LOW", "INVERTER_OVERVOLTAGE", "INVERTER_CLIPPING_HIGH",
            "INVERTER_UNDERVOLTAGE", "INVERTER_CURRENT_LIMIT",
        }.issubset(codes))
        vmax = next(f for f in diag["findings"] if f["code"] == "INVERTER_OVERVOLTAGE")
        self.assertIn("NModSerie=24", vmax["root_cause_hint"])

    def test_shading_soiling_curtailment_and_storage_findings(self):
        cols, rows = _solar_rows(
            days=2, pr=0.80,
            glob_shd=90.0, shd_elec=5.0, slg_loss=40.0, e_grd_lim=4.0,
            e_user=50.0, e_miss=10.0, soc_end=2.0,
        )
        csv_path = _write_csv(self.root / "Results" / "loss_storage.csv", cols, rows)
        diag = diagnose_simulation_csv(csv_path)
        codes = {f["code"] for f in diag["findings"]}
        self.assertTrue({
            "SHADING_LOSS_HIGH", "ELECTRICAL_SHADING_HIGH", "SOILING_LOSS_HIGH",
            "GRID_CURTAILMENT_HIGH", "UNMET_LOAD_HIGH", "SOC_PINNED_LOW",
        }.issubset(codes))
        compact = compact_diagnostics_summary(diag, max_findings=3)
        self.assertEqual(len(compact["top_findings"]), 3)
        self.assertEqual(compact["finding_count"], diag["finding_count"])

    def test_near_shading_uses_loss_between_native_irradiance_stages(self):
        with_hrz = [("GlobInc", "W/m2"), ("GlobHrz", "W/m2"), ("GlobShd", "W/m2")]
        without_hrz = [("GlobInc", "W/m2"), ("GlobShd", "W/m2")]
        for cols, before in ((with_hrz, [720, 700]), (without_hrz, [700])):
            for after, expected in ((690, []), (490, ["SHADING_LOSS_HIGH"])):
                with self.subTest(columns=[c for c, _ in cols], after=after):
                    rows = [[stamp, *before, after] for stamp in ("07/08/90 11:00", "07/08/90 12:00")]
                    path = _write_csv(self.root / "native_shading.csv", cols, rows)
                    self.assertEqual([f["code"] for f in diagnose_simulation_csv(path)["findings"]], expected)

    def test_time_series_anomalies_and_subhourly_step_awareness(self):
        # 15-minute data with 30 hours of daytime zero output, nighttime export, E_Grid > EArray
        cols, rows = _solar_rows(
            days=5, step_minutes=15, pr=0.82,
            zero_day_hours=30, night_export=5.0, egrid_gt_earray=True,
        )
        csv_path = _write_csv(self.root / "Results" / "subhour_anom.csv", cols, rows)
        diag = diagnose_simulation_csv(csv_path)
        self.assertEqual(diag["step_minutes"], 15.0)
        codes = {f["code"] for f in diag["findings"]}
        self.assertTrue({
            "HIGH_IRRADIANCE_ZERO_OUTPUT", "NIGHTTIME_EXPORT", "E_GRID_EXCEEDS_EARRAY",
        }.issubset(codes))
        zero_f = next(f for f in diag["findings"] if f["code"] == "HIGH_IRRADIANCE_ZERO_OUTPUT")
        self.assertAlmostEqual(zero_f["observed"], 30.0, places=1)
        self.assertEqual(zero_f["severity"], "high")

        # Separate phase-lag check (3h solar centroid mismatch)
        cols_p, rows_p = _solar_rows(days=3, step_minutes=15, pr=0.82, phase_shift_hours=3)
        diag_p = diagnose_simulation_csv(_write_csv(self.root / "Results" / "phase_lag.csv", cols_p, rows_p))
        self.assertIn("IRRADIANCE_OUTPUT_PHASE_MISMATCH", {f["code"] for f in diag_p["findings"]})

    def test_planned_system_unavailability_downgrades_zero_output_to_info(self):
        synth = synthesize_project_and_variant(
            None,
            project_comment="Planned Outage",
            variant_comment="Outage",
            pan_name="Test.PAN",
            ond_name="Test.OND",
            n_mod_serie=18,
            n_string_ch=6,
            n_inverter=1,
            n_inv_mppt=2,
            independent_mppt=True,
            pnom_ac_in_mppt=25.0,
        )
        self.store.create_project("Outage.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
        # Inject an active SystemUnavail via inspect_parameters mock or real update
        with patch.object(
            self.store, "inspect_parameters",
            return_value={
                "subarrays": [{"values": {"NModSerie": "18"}}],
                "orientations": [{"values": {}, "bifacial": {"enabled": False}}],
                "detailed_losses": {"system_unavailability": {"enabled": True}},
            },
        ):
            cols, rows = _solar_rows(days=4, pr=0.82, zero_day_hours=30)
            csv_path = _write_csv(self.root / "Results" / "outage.csv", cols, rows)
            diag = diagnose_simulation_csv(csv_path, store=self.store, project="Outage.PRJ", variant="VC0")
        zero_f = next(f for f in diag["findings"] if f["code"] == "HIGH_IRRADIANCE_ZERO_OUTPUT")
        self.assertEqual(zero_f["severity"], "info")
        self.assertFalse(diag["needs_review"])

    def test_threshold_overrides_and_validation(self):
        cols, rows = _solar_rows(days=2, pr=0.80)
        csv_path = _write_csv(self.root / "Results" / "thresh.csv", cols, rows)
        diag = diagnose_simulation_csv(csv_path, thresholds={"pr_min": 0.85, "pr_max": 0.95})
        self.assertEqual([f["code"] for f in diag["findings"]], ["PR_TOO_LOW"])

        with self.assertRaisesRegex(ValueError, "Unknown diagnostic threshold"):
            diagnose_simulation_csv(csv_path, thresholds={"unknown_key": 1.0})
        with self.assertRaisesRegex(ValueError, "pr_min must be less than"):
            diagnose_simulation_csv(csv_path, thresholds={"pr_min": 0.95, "pr_max": 0.90})
        with self.assertRaisesRegex(ValueError, "Provide both project and variant"):
            diagnose_simulation_csv(csv_path, store=self.store, project="OnlyProj.PRJ")

    def test_mcp_tool_pvsyst_diagnose_results(self):
        import pvsyst_auto.api.results as api_results
        cols, rows = _solar_rows(days=2, pr=0.84)
        _write_csv(self.root / "Results" / "mcp_diag.csv", cols, rows)
        fake_cli = self.root / "PVsystCLI.exe"
        fake_cli.write_bytes(b"stub")
        with patch.dict(os.environ, {"PVSYST_WORKSPACE": str(self.root), "PVSYST_CLI": str(fake_cli)}):
            res = api_results.pvsyst_diagnose_results("mcp_diag.csv")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["finding_count"], 0)

    def test_issue_192_kva_grid_limit_and_apparent_energy_above_limit(self):
        """Issue #192: kVA grid limit caps active power at L*cos(phi) and EApGrid > PLimGrid is normal."""
        synth = synthesize_project_and_variant(
            None,
            project_comment="kVA Limit",
            variant_comment="Apparent 210 kVA",
            pan_name="Test.PAN",
            ond_name="Test.OND",
            n_mod_serie=20,
            n_string_ch=30,
            n_inverter=1,
            n_inv_mppt=1,
        )
        vc0 = synth["vc0_text"].replace(
            "  PVObject_System=pvSystem\r\n    Flags=$00\r\n",
            "  PVObject_System=pvSystem\r\n    Flags=$07100000\r\n"
            "    TanPhi=0.484\r\n    PLimGrid=210.00\r\n    LimGrSeparLoss=True\r\n",
        )
        self.assertIn("PLimGrid=210.00", vc0)
        self.store.create_project("KvaLimit.PRJ", synth["prj_text"], {"VC0": vc0})

        # Peak E_Grid=189.02 kW (matches 210 * cos(atan(0.484)) = 189.024 kW),
        # while EApGrid peaks at 245.62 kVA (> 210 kVA configured limit — real PVsystCLI 8.1.7 behavior).
        cols = [
            ("GlobInc", "W/m2"),
            ("EArray", "kW"),
            ("E_Grid", "kW"),
            ("EApGrid", "kVA"),
            ("EGrdLim", "kW"),
            ("PR", "ratio"),
        ]
        rows = []
        start = datetime(2024, 6, 1, 0, 0)
        for h in range(24):
            stamp = (start + timedelta(hours=h)).strftime("%d/%m/%y %H:%M")
            if 6 <= h < 18:
                gi = 800.0 if 10 <= h <= 14 else 400.0
                ea = 225.0 if 10 <= h <= 14 else 110.0
                eg = 189.02 if 10 <= h <= 14 else 107.0
                eap = 245.62 if 10 <= h <= 14 else 118.9
                curt = 32.0 if 10 <= h <= 14 else 0.0
                rows.append([stamp, gi, ea, eg, eap, curt, 0.78])
            else:
                rows.append([stamp, 0.0, 0.0, 0.0, 0.0, 0.0, ""])
        csv_path = _write_csv(self.root / "Results" / "kva_limit.csv", cols, rows)
        diag = diagnose_simulation_csv(csv_path, store=self.store, project="KvaLimit.PRJ", variant="VC0")

        glc = diag["grid_limit_context"]
        self.assertIsNotNone(glc)
        self.assertEqual(glc["quantity"], "apparent")
        self.assertAlmostEqual(glc["configured_limit"], 210.0, places=2)
        self.assertAlmostEqual(glc["active_dispatch_cap_kw"], 189.024, places=2)
        self.assertAlmostEqual(glc["peak_e_grid_kw"], 189.02, places=2)
        self.assertAlmostEqual(glc["peak_eap_grid_kva"], 245.62, places=2)
        self.assertTrue(glc["active_cap_binding"])
        self.assertTrue(glc["apparent_exceeds_configured_is_expected"])

        codes = [f["code"] for f in diag["findings"]]
        # EApGrid=245.62 > 210 kVA must NOT trigger an anomaly; only curtailment is reported with kVA*cos(phi) hint
        self.assertEqual(codes, ["GRID_CURTAILMENT_HIGH"])
        curt_f = diag["findings"][0]
        self.assertIn("189.02 kW", curt_f["root_cause_hint"])
        self.assertIn("210 kVA", curt_f["root_cause_hint"])

    def test_issue_192_load_block_without_root_flag_bit19_triggers_load_not_enabled(self):
        """Issue #192: pvLoad present with root Flags bit 19 ($00080000) clear triggers LOAD_NOT_ENABLED."""
        synth = synthesize_project_and_variant(
            None,
            project_comment="Disabled Load",
            variant_comment="No Bit19",
            pan_name="Test.PAN",
            ond_name="Test.OND",
            n_mod_serie=18,
            n_string_ch=6,
            n_inverter=1,
            n_inv_mppt=1,
        )
        pvload_block = (
            "  PVObject_UserNeeds=pvLoad\r\n"
            "    Flags=$4000\r\n"
            "    LoadType=HourlyFile\r\n"
            "  End of PVObject pvLoad\r\n"
        )
        vc0_disabled = synth["vc0_text"].replace(
            "End of PVObject pvVCalcul", pvload_block + "End of PVObject pvVCalcul"
        )
        vc0_enabled = vc0_disabled.replace("  Flags=$00100001\r\n", "  Flags=$00180001\r\n", 1)
        self.assertIn("  Flags=$00180001\r\n", vc0_enabled)
        self.store.create_project("LoadBit19.PRJ", synth["prj_text"], {"VC0": vc0_disabled, "VC1": vc0_enabled})

        cols, rows = _solar_rows(days=2, pr=0.84)
        csv_path = _write_csv(self.root / "Results" / "no_load_cols.csv", cols, rows)

        diag_off = diagnose_simulation_csv(csv_path, store=self.store, project="LoadBit19.PRJ", variant="VC0")
        self.assertTrue(diag_off["needs_review"])
        self.assertIn("LOAD_NOT_ENABLED", {f["code"] for f in diag_off["findings"]})
        finding = next(f for f in diag_off["findings"] if f["code"] == "LOAD_NOT_ENABLED")
        self.assertEqual(finding["severity"], "high")
        self.assertIn("$00080000", finding["root_cause_hint"])

        diag_on = diagnose_simulation_csv(csv_path, store=self.store, project="LoadBit19.PRJ", variant="VC1")
        self.assertNotIn("LOAD_NOT_ENABLED", {f["code"] for f in diag_on["findings"]})

    def test_issue_192_demo_tracking_clipping_calibration_and_native_refusal_helper(self):
        """Issue #192: real _DEMO_Tracking.VC3 clipping (~2.31%) & detect_native_refusal."""
        # Calibrate against real PVsystCLI 8.1.7 sample: E_Grid=47096.49 kWh, IL_Pmax=1112.27 kWh over 21 h
        cols = [("GlobInc", "W/m2"), ("EArray", "kW"), ("E_Grid", "kW"), ("IL_Pmax", "kW"), ("PR", "ratio")]
        rows = []
        start = datetime(2024, 6, 1, 0, 0)
        # 21 clipping hours summing to IL_Pmax=1112.27 kWh (peak E_Grid=900 kW) + 51 non-clipping daytime hours
        for i in range(21):
            stamp = (start + timedelta(hours=i)).strftime("%d/%m/%y %H:%M")
            rows.append([stamp, 950.0, 960.0, 900.0, round(1112.27 / 21.0, 6), 0.80])
        rem_grid = 47096.49 - 21 * 900.0
        for i in range(51):
            stamp = (start + timedelta(hours=21 + i)).strftime("%d/%m/%y %H:%M")
            eg = rem_grid / 51.0
            rows.append([stamp, 650.0, eg * 1.02, eg, 0.0, 0.83])
        csv_path = _write_csv(self.root / "Results" / "demo_tracking_clip.csv", cols, rows)

        # Clean under default 3.0% threshold (~2.31% clipping)
        diag_default = diagnose_simulation_csv(csv_path)
        self.assertEqual(diag_default["status"], "ok")
        self.assertNotIn("INVERTER_CLIPPING_HIGH", {f["code"] for f in diag_default["findings"]})

        # Flagged under tighter 2.0% threshold
        diag_tight = diagnose_simulation_csv(csv_path, thresholds={"clipping_loss_max_pct": 2.0})
        clip = next(f for f in diag_tight["findings"] if f["code"] == "INVERTER_CLIPPING_HIGH")
        self.assertGreater(clip["observed"], 2.2)
        self.assertLess(clip["observed"], 2.5)

        # Native refusal extractor
        transcript = (
            "Loading project...\n"
            "Simulation cannot be run.\n"
            "The hourly load cannot be used with the Peak Shaving strategy.\n"
            "\n"
            "Exiting PVsystCLI.\n"
        )
        refusal = detect_native_refusal(transcript)
        self.assertIsNotNone(refusal)
        self.assertIn("Simulation cannot be run.", refusal)
        self.assertIn("Peak Shaving", refusal)
        self.assertIsNone(detect_native_refusal("Simulation done in 0min 2sec"))

    def test_issue_199_subhourly_wh_zero_output_storage_missing_ebatdis_monthly_pf_and_sim_findings(self):
        import subprocess
        import pvsyst_auto.api.simulation as api_simulation
        from pvsyst_auto.native.cli import PVsystCLI

        # 1. 15-min GlobInc encoded as 50 Wh/m2 (= 200 W/m2) over 30 hours reports 30h HIGH_IRRADIANCE_ZERO_OUTPUT.
        start = datetime(2024, 6, 1, 0, 0)
        rows_w = []
        rows_wh = []
        for idx_step in range(120):  # 120 * 15 min = 30 hours
            stamp = (start + timedelta(minutes=15 * idx_step)).strftime("%d/%m/%y %H:%M")
            rows_w.append([stamp, 200.0, 0.0, 0.0])
            rows_wh.append([stamp, 50.0, 0.0, 0.0])
        csv_w = _write_csv(
            self.root / "Results" / "zero_w.csv",
            [("GlobInc", "W/m2"), ("EArray", "kW"), ("E_Grid", "kW")],
            rows_w,
        )
        csv_wh = _write_csv(
            self.root / "Results" / "zero_wh.csv",
            [("GlobInc", "Wh/m2"), ("EArray", "kW"), ("E_Grid", "kW")],
            rows_wh,
        )
        for p in (csv_w, csv_wh):
            diag = diagnose_simulation_csv(p)
            zero_f = next((f for f in diag["findings"] if f["code"] == "HIGH_IRRADIANCE_ZERO_OUTPUT"), None)
            self.assertIsNotNone(zero_f, f"missing zero output finding for {p.name}: {diag['findings']}")
            self.assertEqual(zero_f["severity"], "high")
            self.assertAlmostEqual(zero_f["observed"], 30.0, places=2)

        # 2. Storage variant without EBatDis column skips NIGHTTIME_EXPORT and E_GRID_EXCEEDS_EARRAY.
        synth_st = synthesize_project_and_variant(
            None, project_comment="Storage", variant_comment="Storage VC0",
            pan_name="Test.PAN", ond_name="Test.OND",
            n_mod_serie=18, n_string_ch=6, n_inverter=1, n_inv_mppt=2,
            independent_mppt=True, pnom_ac_in_mppt=25.0,
        )
        storage_vc = synth_st["vc0_text"].replace(
            "  PVObject_System=pvSystem\r\n    Flags=$00\r\n    SystemType=Grid\r\n  End of PVObject pvSystem\r\n",
            "  PVObject_System=pvSystem\r\n    Flags=$10010040\r\n    SystemType=Grid\r\n"
            "    BatteryFile=Fixture.BTR\r\n    NBattSerie=1\r\n    NBattParall=1\r\n  End of PVObject pvSystem\r\n"
            "  PVObject_GridStorage=pvGridStorage\r\n    Flags=$00\r\n    Strategy=SelfConsumption\r\n"
            "    ChargingStrategy=PVAvailable\r\n    DischStrategy=Immediate\r\n"
            "    BattSoCInit=0.5\r\n    SOCChDecl=0.9\r\n    SOCDischDecl=0.2\r\n    SOCDischSelf=0.2\r\n"
            "    PMaxCharge=10\r\n    PMaxDisch=10\r\n    BattRectMaxEffic=97\r\n    BattRectEuroEffic=95\r\n"
            "    BattInvMaxEffic=97\r\n    BattInvEuroEffic=95\r\n  End of PVObject pvGridStorage\r\n",
        )
        self.store.create_project("StorageDiag.PRJ", synth_st["prj_text"], {"VC0": storage_vc})
        csv_no_dis = _write_csv(
            self.root / "Results" / "storage_no_ebatdis.csv",
            [("GlobInc", "W/m2"), ("EArray", "kWh"), ("E_Grid", "kWh")],
            [["01/06/24 22:00", 0.0, 0.0, 5.0], ["01/06/24 23:00", 0.0, 0.0, 4.0]],
        )
        diag_st = diagnose_simulation_csv(csv_no_dis, store=self.store, project="StorageDiag.PRJ", variant="VC0")
        codes_st = {f["code"] for f in diag_st["findings"]}
        self.assertNotIn("NIGHTTIME_EXPORT", codes_st)
        self.assertNotIn("E_GRID_EXCEEDS_EARRAY", codes_st)

        # 3. Monthly power factor (Flags=$07300000, PLimGrid=210, TanPhiM=0.484*12) computes cos_phi=0.900114 & cap=189.0239 kW.
        tan_phi_m_uniform = ",".join(["0.484"] * 12) + ","
        vc_monthly_uniform = synth_st["vc0_text"].replace(
            "  PVObject_System=pvSystem\r\n    Flags=$00\r\n    SystemType=Grid\r\n  End of PVObject pvSystem\r\n",
            "  PVObject_System=pvSystem\r\n    Flags=$07300000\r\n    SystemType=Grid\r\n"
            f"    PLimGrid=210\r\n    LimGrSeparLoss=True\r\n    TanPhiM={tan_phi_m_uniform}\r\n"
            "  End of PVObject pvSystem\r\n",
        )
        tan_phi_m_varied = ",".join(["0.484"] * 6 + ["0.200"] * 6) + ","
        vc_monthly_varied = vc_monthly_uniform.replace(tan_phi_m_uniform, tan_phi_m_varied)
        self.store.create_project("MonthlyPF.PRJ", synth_st["prj_text"], {"VC0": vc_monthly_uniform, "VC1": vc_monthly_varied})
        csv_grid = _write_csv(
            self.root / "Results" / "monthly_pf_grid.csv",
            [("GlobInc", "W/m2"), ("EArray", "kWh"), ("E_Grid", "kWh")],
            [["01/06/24 11:00", 900.0, 195.0, 189.02], ["01/06/24 12:00", 900.0, 195.0, 188.0]],
        )
        diag_pf_u = diagnose_simulation_csv(csv_grid, store=self.store, project="MonthlyPF.PRJ", variant="VC0")
        gl_u = diag_pf_u["grid_limit_context"]
        self.assertEqual(gl_u["power_factor_mode"], "monthly")
        self.assertAlmostEqual(gl_u["cos_phi"], 0.900114, places=5)
        self.assertAlmostEqual(gl_u["active_dispatch_cap_kw"], 189.0239, places=3)
        self.assertTrue(gl_u["active_cap_binding"])

        diag_pf_v = diagnose_simulation_csv(csv_grid, store=self.store, project="MonthlyPF.PRJ", variant="VC1")
        gl_v = diag_pf_v["grid_limit_context"]
        self.assertEqual(gl_v["power_factor_mode"], "monthly")
        self.assertIsNone(gl_v["cos_phi"])
        self.assertIsNone(gl_v["active_dispatch_cap_kw"])
        self.assertEqual(len(gl_v["monthly_active_dispatch_cap_kw"]), 12)
        self.assertAlmostEqual(gl_v["monthly_active_dispatch_cap_kw"][5], 189.0239, places=3)
        self.assertTrue(gl_v["active_cap_binding"])

        # 4. Single CSV pvsyst_run_simulation attaches physical_diagnostics and sets status="needs_review" for critical findings.
        fake_cli_exe = self.root / "PVsystCLI.exe"
        fake_cli_exe.write_bytes(b"stub")
        client = PVsystCLI(fake_cli_exe, self.root)
        client._help_cache["run-simulation"] = " --output-csv-file|-ocf: "
        out_csv = self.root / "Results" / "ov_run.csv"

        def _fake_run(*args, **kwargs):
            _write_csv(
                out_csv,
                [("GlobInc", "W/m2"), ("EArray", "kWh"), ("E_Grid", "kWh"), ("IL_Vmax", "kWh")],
                [["01/06/24 11:00", 900.0, 604.8, 590.0, 33.0], ["01/06/24 12:00", 900.0, 604.8, 590.0, 33.0]],
            )
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 2sec", "")

        import pvsyst_auto.api.context as api_ctx
        with patch.object(api_ctx, "cli", return_value=client), \
                patch.object(api_simulation, "cli", return_value=client), \
                patch.object(api_simulation, "variant_store", return_value=self.store), \
                patch.object(client, "license_info", return_value={"status": "LICENSED"}), \
                patch.object(client, "_run", side_effect=_fake_run):
            sim = api_simulation.pvsyst_run_simulation(
                project="MonthlyPF.PRJ", variant="VC0", csv_name="ov_run",
            )
        self.assertIn("Simulation done in", sim["diagnostics"])
        self.assertEqual(sim["status"], "needs_review")
        self.assertEqual(sim["physical_diagnostics"]["max_severity"], "critical")
        self.assertTrue(any(f["code"] == "INVERTER_OVERVOLTAGE" for f in sim["physical_diagnostics"]["top_findings"]))

    def test_issue_201_irregular_subhourly_cadence_returns_blocked(self):
        # 15-min rows covering 08:00..10:00 with 09:00 removed -> 8 rows, irregular cadence -> blocked.
        stamps = [
            "01/06/24 08:00", "01/06/24 08:15", "01/06/24 08:30", "01/06/24 08:45",
            "01/06/24 09:15", "01/06/24 09:30", "01/06/24 09:45", "01/06/24 10:00",
        ]
        csv_gap = _write_csv(
            self.root / "Results" / "gap_15min.csv",
            [("GlobInc", "W/m2"), ("EArray", "kW"), ("E_Grid", "kW")],
            [[stamp, 500.0, 0.0, 0.0] for stamp in stamps],
        )
        diag = diagnose_simulation_csv(csv_gap, thresholds={"zero_output_max_hours": 3.0})
        self.assertEqual(diag["status"], "blocked", diag)
        self.assertIsNone(diag["step_minutes"])
        self.assertEqual(diag["finding_count"], 0)
        self.assertTrue(diag["issues"])

        # pvsyst_run_simulation must surface blocked diagnostics as status="needs_review" with issues preserved.
        import subprocess
        import pvsyst_auto.api.context as api_ctx
        import pvsyst_auto.api.simulation as api_simulation
        from pvsyst_auto.native.cli import PVsystCLI

        synth = synthesize_project_and_variant(
            None, project_comment="GapSim", variant_comment="GapSim",
            pan_name="Test.PAN", ond_name="Test.OND",
            n_mod_serie=18, n_string_ch=6, n_inverter=1, n_inv_mppt=2,
            independent_mppt=True, pnom_ac_in_mppt=25.0,
        )
        self.store.create_project("GapSim.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
        fake_cli_exe = self.root / "PVsystCLI.exe"
        fake_cli_exe.write_bytes(b"stub")
        client = PVsystCLI(fake_cli_exe, self.root)
        client._help_cache["run-simulation"] = " --output-csv-file|-ocf: "
        out_csv = self.root / "Results" / "gap_sim_run.csv"

        def _fake_gap_run(*args, **kwargs):
            _write_csv(
                out_csv,
                [("GlobInc", "W/m2"), ("EArray", "kW"), ("E_Grid", "kW")],
                [[stamp, 500.0, 0.0, 0.0] for stamp in stamps],
            )
            return subprocess.CompletedProcess([], 0, "Simulation done in 0min 2sec", "")

        with patch.object(api_ctx, "cli", return_value=client), \
                patch.object(api_simulation, "cli", return_value=client), \
                patch.object(api_simulation, "variant_store", return_value=self.store), \
                patch.object(client, "license_info", return_value={"status": "LICENSED"}), \
                patch.object(client, "_run", side_effect=_fake_gap_run):
            sim = api_simulation.pvsyst_run_simulation(
                project="GapSim.PRJ", variant="VC0", csv_name="gap_sim_run",
            )
        self.assertEqual(sim["status"], "needs_review")
        self.assertEqual(sim["physical_diagnostics"]["status"], "blocked")
        self.assertTrue(sim["physical_diagnostics"]["issues"])


if __name__ == "__main__":
    unittest.main()

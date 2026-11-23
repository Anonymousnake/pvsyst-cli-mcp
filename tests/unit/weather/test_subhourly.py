"""Unit tests for sub-hourly (5/10/15/30 min) weather CSV, MEF, MET, SFI, and results pipeline."""
from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
import tempfile
import unittest

from pvsyst_auto.native.cli import build_sfi, summarize_results
from pvsyst_auto.results import annual_summary
from pvsyst_auto.weather.csv import inspect_weather_csv
from pvsyst_auto.weather.preparation import check_generated_met, compare_native_weather, render_mef


@lru_cache(maxsize=8)
def _subhourly_weather_text(year: int, step_minutes: int, unit: str) -> str:
    lines = [
        "#Weather data sub-hourly data",
        "#Site;TestSite",
        "#Country;Switzerland",
        "#Data Source;Synthetic",
        f"#Time step;{step_minutes} min",
        f"#Year;{year}",
        "#Latitude;46.24",
        "#Longitude;6.11",
        "#Altitude;425",
        "#Time Zone;1",
        "Year;Month;Day;Hour;Minute;GHI;DHI;Tamb",
        f";;;;;{unit};{unit};deg.C",
    ]
    start = datetime(year, 1, 1, 0, 0)
    end = datetime(year + 1, 1, 1, 0, 0)
    step = timedelta(minutes=step_minutes)
    cur = start
    while cur < end:
        ghi_w = 400.0 if 8 <= cur.hour < 16 else 0.0
        dhi_w = 120.0 if 8 <= cur.hour < 16 else 0.0
        if unit == "MJ/m2":
            # Energy per step in MJ/m2: W/m2 * step_hours * 3.6 / 1000
            factor = (step_minutes / 60.0) * 3.6 / 1000.0
            ghi_val = f"{ghi_w * factor:.6f}"
            dhi_val = f"{dhi_w * factor:.6f}"
        else:
            ghi_val = f"{ghi_w:.1f}"
            dhi_val = f"{dhi_w:.1f}"
        lines.append(f"{cur.year};{cur.month};{cur.day};{cur.hour};{cur.minute};{ghi_val};{dhi_val};20.0")
        cur += step
    return "\n".join(lines) + "\n"


def _write_subhourly_weather_csv(path: Path, *, year: int = 2023, step_minutes: int = 15, unit: str = "W/m2"):
    path.write_text(_subhourly_weather_text(year, step_minutes, unit), encoding="utf-8")


@lru_cache(maxsize=8)
def _subhourly_result_text(year: int, step_minutes: int, dc_kw: float) -> str:
    lines = [
        "PVSYST 8.1.7",
        "date;GlobHor;DiffHor;T_Amb;GlobInc;EArray;E_Grid;IL_Pmax;PR",
        ";W/m2;W/m2;degC;W/m2;kW;kW;kW;ratio",
    ]
    start = datetime(year, 1, 1, 0, 0)
    end = datetime(year + 1, 1, 1, 0, 0)
    step = timedelta(minutes=step_minutes)
    cur = start
    while cur < end:
        if 8 <= cur.hour < 16:
            # GlobInc = 500 W/m2 -> reference power = 0.5 * 10 kWp = 5 kW -> E_Grid = 4.0 kW => PR = 0.80
            lines.append(f"{cur:%d/%m/%y %H:%M};400.0;120.0;20.0;500.0;4.2;4.0;0.1;0.800")
        else:
            lines.append(f"{cur:%d/%m/%y %H:%M};0.0;0.0;20.0;0.0;0.0;0.0;0.0;0.0")
        cur += step
    return "\n".join(lines) + "\n"


def _write_subhourly_result_csv(path: Path, *, year: int = 2023, step_minutes: int = 15, dc_kw: float = 10.0):
    path.write_text(_subhourly_result_text(year, step_minutes, dc_kw), encoding="utf-8")


class SubHourlyPipelineUnitTests(unittest.TestCase):
    def test_inspect_15min_weather_csv_w_m2_and_mj_m2_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            p_w = Path(tmp) / "w15.csv"
            p_mj = Path(tmp) / "mj15.csv"
            _write_subhourly_weather_csv(p_w, year=2023, step_minutes=15, unit="W/m2")
            _write_subhourly_weather_csv(p_mj, year=2023, step_minutes=15, unit="MJ/m2")

            rw = inspect_weather_csv(p_w, 2023)
            rmj = inspect_weather_csv(p_mj, 2023)
            self.assertTrue(rw["valid"], rw["issues"])
            self.assertTrue(rmj["valid"], rmj["issues"])
            self.assertEqual(rw["step_minutes"], 15)
            self.assertEqual(rw["format"], "pvsyst_standard_subhourly")
            self.assertEqual(rw["rows"], 35040)
            self.assertEqual(rw["expected_rows"], 35040)
            # Jan has 31 days * 8 sunlit hours * 0.4 kW/m2 = 99.2 kWh/m2
            self.assertAlmostEqual(rw["months"][0]["GHI"]["observed_kwh_m2"], 99.2, places=3)
            self.assertAlmostEqual(rmj["months"][0]["GHI"]["observed_kwh_m2"], 99.2, places=3)

            # Requesting mismatched step_minutes must flag an issue
            bad = inspect_weather_csv(p_w, 2023, step_minutes=30)
            self.assertFalse(bad["valid"])

    def test_render_mef_and_check_generated_met_for_subhourly(self):
        mef15 = render_mef(15)
        self.assertIn("MeasureStep=15", mef15)
        self.assertIn("15-minute sub-hourly", mef15)
        with self.assertRaises(ValueError):
            render_mef(7)

        with tempfile.TemporaryDirectory() as tmp:
            met = Path(tmp) / "Site_15m.MET"
            met.write_text(
                "\n".join([
                    "PVObject_=pvMeteo",
                    "  IsSourceUt=False",
                    "  HourShift=0",
                    "  TimeShift=0",
                    "  TimeShiftTotalMin=0",
                    "  PasAccum=15",
                    "  VarMeteoVarAcc=SubHour",
                    "  VarMeteoStepDuration=15",
                    "  PVObject_SiteMet=pvSite",
                    "    HLeg=True",
                    "    TimeZone=1.0",
                    "  End of PVObject pvSite",
                    "End of PVObject pvMeteo",
                ]) + "\n",
                encoding="utf-8",
            )
            ok = check_generated_met(met, time_zone=1.0, expected_step_minutes=15)
            self.assertTrue(ok["valid"], ok["issues"])
            self.assertEqual(ok["step_minutes"], 15)
            wrong = check_generated_met(met, time_zone=1.0, expected_step_minutes=60)
            self.assertFalse(wrong["valid"])

    def test_build_sfi_time_step_subhour(self):
        with tempfile.TemporaryDirectory() as tmp:
            sfi = Path(tmp) / "Sub15.SFI"
            build_sfi(sfi, "all_common", time_step="subhour")
            text = sfi.read_text(encoding="utf-8")
            self.assertIn("TypeValW=SubHour", text)
            with self.assertRaises(ValueError):
                build_sfi(Path(tmp) / "Bad.SFI", "energy", time_step="15min")

    def test_annual_summary_and_compare_native_weather_15min(self):
        with tempfile.TemporaryDirectory() as tmp:
            wcsv = Path(tmp) / "w15.csv"
            rcsv = Path(tmp) / "r15.csv"
            _write_subhourly_weather_csv(wcsv, year=2023, step_minutes=15, unit="W/m2")
            _write_subhourly_result_csv(rcsv, year=2023, step_minutes=15, dc_kw=10.0)

            summary = annual_summary(rcsv, 2023, nominal_dc_power_kw=10.0)
            self.assertEqual(summary["step_minutes"], 15)
            self.assertEqual(summary["rows"], 35040)
            self.assertEqual(summary["last"], "2023-12-31 23:45:00")
            # 365 days * 8 sunlit hours * 4.0 kW = 11680 kWh
            self.assertAlmostEqual(summary["grid_energy_kwh"], 11680.0, places=3)
            # 365 days * 8 sunlit hours * 0.5 kW/m2 = 1460 kWh/m2
            self.assertAlmostEqual(summary["incident_irradiation_kwh_m2"], 1460.0, places=3)
            self.assertAlmostEqual(summary["pr_ratio"], 0.80, places=4)

            sres = summarize_results(rcsv, ("E_Grid", "EArray"))
            self.assertEqual(sres["step_minutes"], 15.0)
            self.assertAlmostEqual(sres["summary"]["E_Grid"]["energy_kwh"], 11680.0, places=3)

            wrep = inspect_weather_csv(wcsv, 2023)
            cmp = compare_native_weather(rcsv, wcsv, wrep)
            self.assertTrue(cmp["timestamps_match"])
            self.assertEqual(cmp["regular_step_minutes"], 15.0)
            self.assertAlmostEqual(cmp["signed_energy_delta_kwh_m2"]["GlobHor"], 0.0, places=6)

    def test_summarize_results_sums_wh_and_kwh_interval_energy_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamps = ("01/06/23 08:00", "01/06/23 08:15", "01/06/23 08:30", "01/06/23 08:45")
            for unit, val in (("kWh", "1.0"), ("Wh", "1000.0")):
                with self.subTest(unit=unit):
                    csv_path = Path(tmp) / f"energy_{unit}.csv"
                    lines = ["PVSYST 8.1.7", "date;E_Grid", f";{unit}"] + [f"{s};{val}" for s in stamps]
                    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    res = summarize_results(csv_path, ("E_Grid",))
                    self.assertEqual(res["step_minutes"], 15.0)
                    self.assertAlmostEqual(res["summary"]["E_Grid"]["energy_kwh"], 4.0, places=6)
                    self.assertAlmostEqual(res["summary"]["E_Grid"]["observed_energy_kwh"], 4.0, places=6)

    def test_prepare_weather_blocks_utc_plus_13_in_preflight_before_writing(self):
        from pvsyst_auto.native.cli import PVsystCLI
        from pvsyst_auto.weather.preparation import prepare_weather

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "PVsyst8.1_Data"
            for sub in ("Meteo", "Sites", "Results"):
                (ws / sub).mkdir(parents=True, exist_ok=True)
            tonga_csv = ws / "Meteo" / "tonga_15min.csv"
            _write_subhourly_weather_csv(tonga_csv, year=2023, step_minutes=15, unit="W/m2")
            tonga_csv.write_text(
                tonga_csv.read_text(encoding="utf-8")
                .replace("#Site;TestSite", "#Site;Nukualofa")
                .replace("#Country;Switzerland", "#Country;Tonga")
                .replace("#Latitude;46.24", "#Latitude;-21.14")
                .replace("#Longitude;6.11", "#Longitude;-175.20")
                .replace("#Time Zone;1", "#Time Zone;13"),
                encoding="utf-8",
            )
            fake_cli_exe = Path(tmp) / "PVsystCLI.exe"
            fake_cli_exe.write_bytes(b"stub")
            cli = PVsystCLI(fake_cli_exe, ws)
            before_files = set(ws.rglob("*"))
            for dry_run in (True, False):
                with self.subTest(dry_run=dry_run):
                    res = prepare_weather(
                        cli,
                        csv_name="tonga_15min.csv",
                        year=2023,
                        sit_name="Tonga.SIT",
                        met_name="Tonga.MET",
                        country_code="TO",
                        time_basis="local_standard_interval_start",
                        step_minutes=15,
                        dry_run=dry_run,
                    )
                    self.assertEqual(res["status"], "blocked", res)
                    self.assertTrue(any("Timezone outside [-12, 12]" in msg for msg in res["issues"]), res["issues"])
                    self.assertEqual(set(ws.rglob("*")), before_files)


if __name__ == "__main__":
    unittest.main()

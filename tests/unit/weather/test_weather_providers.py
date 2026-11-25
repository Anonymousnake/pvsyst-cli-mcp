"""Offline mocked unit tests for PVGIS v5.3 TMY and NASA POWER weather acquisition providers."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.api.weather import pvsyst_create_site
from pvsyst_auto.native.cli import PVsystCLI
from pvsyst_auto.weather import acquisition
from pvsyst_auto.weather.csv import inspect_weather_csv


def _fake_pvgis_payload(year_label: int = 2015) -> tuple[dict, str]:
    start = datetime(year_label, 1, 1, 0, 0)
    rows = []
    for h in range(8760):
        dt = start + timedelta(hours=h)
        ghi = 600.0 if 6 <= dt.hour <= 16 else 0.0
        dhi = 180.0 if ghi > 0 else 0.0
        rows.append({
            "time(UTC)": dt.strftime("%Y%m%d:%H%M"),
            "T2m": 18.5,
            "G(h)": ghi,
            "Gd(h)": dhi,
        })
    payload = {
        "inputs": {
            "location": {"latitude": 31.32, "longitude": 120.72, "elevation": 12.0},
            "meteo_data": {"radiation_db": "PVGIS-ERA5"},
        },
        "outputs": {"tmy_hourly": rows},
    }
    return payload, json.dumps(payload)


def _fake_nasa_payload(year: int = 2023, offset_hours: int = 8, sentinel_at: str | None = None) -> tuple[dict, str]:
    utc_start = datetime(year, 1, 1, 0, 0) - timedelta(hours=offset_hours)
    ghi_map, dhi_map, t2m_map = {}, {}, {}
    for step in range(8760):
        utc_dt = utc_start + timedelta(hours=step)
        key = utc_dt.strftime("%Y%m%d%H")
        local_h = (utc_dt.hour + offset_hours) % 24
        ghi = 550.0 if 8 <= local_h <= 16 else 0.0
        dhi = 150.0 if ghi > 0 else 0.0
        if key == sentinel_at:
            ghi = -999.0
        ghi_map[key] = ghi
        dhi_map[key] = dhi
        t2m_map[key] = 16.0
    payload = {
        "geometry": {"coordinates": [120.72, 31.32, 10.0]},
        "properties": {
            "parameter": {
                "ALLSKY_SFC_SW_DWN": ghi_map,
                "ALLSKY_SFC_SW_DIFF": dhi_map,
                "T2M": t2m_map,
            }
        },
    }
    return payload, json.dumps(payload)


class MultiProviderWeatherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Meteo").mkdir()
        (self.root / "Results").mkdir()

    def test_pvgis_tmy_plan_execution_and_csv_validation(self):
        # Leap year rejected for 8760h TMY
        bad = acquisition.download_weather(
            self.root, csv_name="suzhou_tmy.csv", year=2024,
            latitude=31.32, longitude=120.72, site_name="Suzhou",
            country_code="CN", timezone_name="Asia/Shanghai",
            provider="pvgis_tmy", dry_run=True,
        )
        self.assertEqual(bad["status"], "blocked")
        self.assertTrue(any("non-leap" in msg for msg in bad["issues"]))

        plan = acquisition.download_weather(
            self.root, csv_name="suzhou_tmy.csv", year=2021,
            latitude=31.32, longitude=120.72, site_name="Suzhou",
            country_code="CN", timezone_name="Asia/Shanghai",
            provider="pvgis_tmy", dry_run=True,
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["standard_utc_offset_hours"], 8)
        self.assertIn("re.jrc.ec.europa.eu", plan["plan"]["request_url"])

        payload, raw_text = _fake_pvgis_payload()
        with patch.object(acquisition, "_provider_get", return_value=(payload, raw_text, plan["plan"]["request_url"])):
            done = acquisition.download_weather(
                self.root, csv_name="suzhou_tmy.csv", year=2021,
                latitude=31.32, longitude=120.72, site_name="Suzhou",
                country_code="CN", timezone_name="Asia/Shanghai",
                provider="pvgis_tmy", dry_run=False,
            )
        self.assertEqual(done["status"], "prepared", done["issues"])
        self.assertEqual(done["created_files"]["csv"]["rows"], 8760)
        csv_path = self.root / "Meteo" / "suzhou_tmy.csv"
        report = inspect_weather_csv(csv_path, expected_year=2021)
        self.assertTrue(report["valid"], report.get("issues"))
        self.assertEqual(report["rows"], 8760)

    def test_nasa_power_execution_and_sentinel_rejection(self):
        payload, raw_text = _fake_nasa_payload(2023, offset_hours=8)
        with patch.object(acquisition, "_provider_get", return_value=(payload, raw_text, "https://power.larc.nasa.gov/api/temporal/hourly/point?ok=1")):
            done = acquisition.download_weather(
                self.root, csv_name="suzhou_nasa_2023.csv", year=2023,
                latitude=31.32, longitude=120.72, site_name="Suzhou",
                country_code="CN", timezone_name="Asia/Shanghai",
                provider="nasa_power", dry_run=False,
            )
        self.assertEqual(done["status"], "prepared", done["issues"])
        report = inspect_weather_csv(self.root / "Meteo" / "suzhou_nasa_2023.csv", expected_year=2023)
        self.assertTrue(report["valid"], report.get("issues"))

        # Sentinel -999 must block instead of silently writing negative irradiance
        bad_payload, bad_raw = _fake_nasa_payload(2023, offset_hours=8, sentinel_at="2023061504")
        with patch.object(acquisition, "_provider_get", return_value=(bad_payload, bad_raw, "https://power.larc.nasa.gov/api/temporal/hourly/point?bad=1")):
            blocked = acquisition.download_weather(
                self.root, csv_name="suzhou_nasa_bad.csv", year=2023,
                latitude=31.32, longitude=120.72, site_name="Suzhou",
                country_code="CN", timezone_name="Asia/Shanghai",
                provider="nasa_power", dry_run=False,
            )
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(any("sentinel" in msg for msg in blocked["issues"]))

    def test_execution_records_blocked_manifest_on_weather_output_collision(self):
        for provider, raw_suffix in (
            ("open_meteo_era5", "openmeteo"), ("pvgis_tmy", "pvgis_tmy"), ("nasa_power", "nasapower"),
        ):
            for dry_run in (False, True):
                for existing_name in ("collision.csv", f"collision_{raw_suffix}_raw.json"):
                    with self.subTest(provider=provider, dry_run=dry_run, existing=existing_name):
                        workspace = self.root / provider / str(dry_run) / existing_name
                        (workspace / "Meteo").mkdir(parents=True)
                        (workspace / "Results").mkdir()
                        existing = workspace / "Meteo" / existing_name
                        existing.write_bytes(b"existing output\n")
                        with patch.object(acquisition, "_provider_get") as get:
                            result = acquisition.download_weather(
                                workspace, csv_name="collision.csv", year=2023,
                                latitude=31.32, longitude=120.72, site_name="Suzhou", country_code="CN",
                                timezone_name="Asia/Shanghai", altitude=12.0, provider=provider, dry_run=dry_run,
                            )
                        get.assert_not_called()
                        self.assertEqual((result["status"], result["stage"]), ("blocked", "plan"))
                        self.assertIn("refusing to overwrite", result["issues"][0])
                        self.assertEqual(existing.read_bytes(), b"existing output\n")
                        self.assertEqual(list((workspace / "Meteo").iterdir()), [existing])
                        manifest = workspace / "Results" / "collision_acquisition.json"
                        if dry_run:
                            self.assertFalse(manifest.exists())
                            self.assertEqual(result["created_files"], {})
                        else:
                            self.assertEqual(result["created_files"], {"manifest": {"name": manifest.name}})
                            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), result)

    def test_mcp_acquire_weather_one_step_prepare_chain(self):
        import pvsyst_auto.api.weather as api_weather

        fake_cli = self.root / "PVsystCLI.exe"
        fake_cli.write_bytes(b"stub")
        payload, raw_text = _fake_pvgis_payload()
        with patch.dict(os.environ, {"PVSYST_WORKSPACE": str(self.root), "PVSYST_CLI": str(fake_cli)}):
            plan = api_weather.pvsyst_acquire_weather(
                csv_name="chain_tmy.csv", year=2021,
                latitude=31.32, longitude=120.72, site_name="Suzhou",
                country_code="CN", timezone_name="Asia/Shanghai",
                provider="pvgis_tmy", prepare=True, dry_run=True,
            )
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["preparation_plan"]["sit_name"], "chain_tmy.SIT")
            self.assertEqual(plan["preparation_plan"]["met_name"], "chain_tmy.MET")

            with patch.object(acquisition, "_provider_get", return_value=(payload, raw_text, "https://re.jrc.ec.europa.eu/api/v5_3/tmy?ok=1")), \
                    patch.object(api_weather, "prepare_weather", return_value={"status": "prepared", "sit": "chain_tmy.SIT", "met": "chain_tmy.MET"}) as prep_mock:
                done = api_weather.pvsyst_acquire_weather(
                    csv_name="chain_tmy.csv", year=2021,
                    latitude=31.32, longitude=120.72, site_name="Suzhou",
                    country_code="CN", timezone_name="Asia/Shanghai",
                    provider="pvgis_tmy", prepare=True, dry_run=False,
                )
            self.assertEqual(done["status"], "prepared")
            self.assertEqual(done["preparation"]["status"], "prepared")
            prep_mock.assert_called_once()

            with patch.object(acquisition, "_provider_get", return_value=(payload, raw_text, "https://re.jrc.ec.europa.eu/api/v5_3/tmy?ok=1")), \
                    patch.object(api_weather, "prepare_weather", return_value={"status": "blocked", "issues": ["SIT exists"]}):
                failed = api_weather.pvsyst_acquire_weather(
                    csv_name="chain_tmy2.csv", year=2021,
                    latitude=31.32, longitude=120.72, site_name="Suzhou",
                    country_code="CN", timezone_name="Asia/Shanghai",
                    provider="pvgis_tmy", prepare=True, dry_run=False,
                )
            self.assertEqual((failed["status"], failed["stage"]), ("blocked", "preparation"))
            self.assertEqual(failed["preparation"]["issues"], ["SIT exists"])

    def test_nasa_power_blocks_future_year_in_preflight_with_zero_requests(self):
        from datetime import timezone as dt_tz

        future_year = datetime.now(dt_tz.utc).year + 1
        with patch.object(acquisition, "_provider_get") as mock_get:
            for dry_run in (True, False):
                with self.subTest(dry_run=dry_run):
                    res = acquisition.download_weather(
                        self.root, csv_name="future.csv", year=future_year,
                        latitude=31.32, longitude=120.72, site_name="Suzhou",
                        country_code="CN", timezone_name="Asia/Shanghai",
                        provider="nasa_power", dry_run=dry_run,
                    )
                    self.assertEqual((res["status"], res["stage"]), ("blocked", "preflight"), res)
                    self.assertTrue(res["issues"])
        mock_get.assert_not_called()

    def test_region_error_and_schema_expose_allowed_values(self):
        fake_exe = self.root / "PVsystCLI.exe"
        fake_exe.write_bytes(b"fake")
        cli = PVsystCLI(cli_path=fake_exe, workspace=self.root)
        with patch.object(cli, "_require", return_value=None):
            with self.assertRaisesRegex(ValueError, "Unknown region.*asia.*europe"):
                cli.create_site_details("S", 30.0, 120.0, self.root / "Meteo" / "m.csv", self.root / "s.sit", region="Asia")
        ann = pvsyst_create_site.__annotations__["region"]
        self.assertIn("asia", str(ann))
        self.assertIn("north_america", str(ann))

    def test_nasa_power_twilight_dhi_tolerance_and_strict_opt_out(self):
        year = 2022
        offset_hours = 8
        utc_start = datetime(year, 1, 1, 0, 0) - timedelta(hours=offset_hours)
        ghi_map, dhi_map, t2m_map = {}, {}, {}
        twilight_key = None
        for step in range(8760):
            utc_dt = utc_start + timedelta(hours=step)
            key = utc_dt.strftime("%Y%m%d%H")
            local_h = (utc_dt.hour + offset_hours) % 24
            ghi = 500.0 if 8 <= local_h <= 16 else 0.0
            dhi = 150.0 if ghi > 0 else 0.0
            if step == 100:
                ghi = 14.48
                dhi = 17.26
                twilight_key = key
            ghi_map[key] = ghi
            dhi_map[key] = dhi
            t2m_map[key] = 15.0
        payload = {
            "geometry": {"coordinates": [110.0, 35.0, 400.0]},
            "properties": {
                "parameter": {
                    "ALLSKY_SFC_SW_DWN": ghi_map,
                    "ALLSKY_SFC_SW_DIFF": dhi_map,
                    "T2M": t2m_map,
                }
            },
        }
        raw = json.dumps(payload)

        with patch.object(acquisition, "_provider_get", return_value=(payload, raw, "https://power.larc.nasa.gov/mock")):
            ok = acquisition.download_weather(
                self.root, csv_name="nasa_twilight_ok.csv", year=year,
                latitude=35.0, longitude=110.0, site_name="TwSite",
                country_code="CN", timezone_name="Asia/Shanghai",
                provider="nasa_power", dry_run=False,
            )
            self.assertEqual(ok["status"], "prepared", ok["issues"])
            self.assertTrue(any("twilight" in w.lower() and twilight_key in w for w in ok["warnings"]))

            strict = acquisition.download_weather(
                self.root, csv_name="nasa_twilight_strict.csv", year=year,
                latitude=35.0, longitude=110.0, site_name="TwSite",
                country_code="CN", timezone_name="Asia/Shanghai",
                provider="nasa_power", strict_dhi=True, dry_run=False,
            )
            self.assertEqual(strict["status"], "blocked")
            self.assertTrue(any("DHI (17.26) > GHI (14.48)" in i for i in strict["issues"]))


if __name__ == "__main__":
    unittest.main()

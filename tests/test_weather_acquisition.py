"""Offline contract tests for the bounded Open-Meteo ERA5 acquisition module.

Every provider call is replaced by a local fake, so these tests never touch the
network and never compute a hash. They cover the public result shape, the time and
unit contract, overwrite protection, provider/HTTP failures, fixed standard offsets
(UTC+8 and UTC-10), leap years, missing provider hours and the blocked cases for
half-hour or year-varying standard offsets.
"""
import csv
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode
from zoneinfo import ZoneInfoNotFoundError

import pvsyst_auto.weather.acquisition as acquisition
from pvsyst_auto.weather.acquisition import WeatherAcquisitionError, download_weather, search_locations

HASH_KEYS = ("sha256", "hash", "fingerprint", "md5", "checksum")


def provider_labels(start, end):
    """Every whole UTC hour a provider would return for an inclusive date range."""
    first = datetime(start.year, start.month, start.day)
    last = datetime(end.year, end.month, end.day, 23, 0)
    return [first + timedelta(hours=index)
            for index in range(int((last - first).total_seconds() // 3600) + 1)]


def archive_payload(params, *, missing=(), dhi_above_ghi=False, unit="W/m\u00b2",
                    grid=(40.0, 116.5, 47.0)):
    """A provider-shaped payload whose values make the time mapping checkable."""
    labels = provider_labels(date.fromisoformat(params["start_date"]),
                             date.fromisoformat(params["end_date"]))
    ghi = [100.0 if 8 <= stamp.hour < 16 else 0.0 for stamp in labels]
    dhi = [value / 2 for value in ghi]
    temperature = [float(stamp.hour) for stamp in labels]
    if dhi_above_ghi:
        middle = len(labels) // 2          # well inside any requested year crop
        dhi[middle] = ghi[middle] + 10.0
    for stamp in missing:
        index = labels.index(stamp)
        ghi[index] = dhi[index] = temperature[index] = None
    return {"latitude": grid[0], "longitude": grid[1], "elevation": grid[2],
            "timezone": "GMT", "utc_offset_seconds": 0, "timezone_abbreviation": "GMT",
            "generationtime_ms": 1.0,
            "hourly_units": {"time": "iso8601", "shortwave_radiation": unit,
                             "diffuse_radiation": unit, "temperature_2m": "\u00b0C"},
            "hourly": {"time": [stamp.isoformat() for stamp in labels],
                       "shortwave_radiation": ghi, "diffuse_radiation": dhi,
                       "temperature_2m": temperature}}


def geocoding_search_payload():
    return {"results": [
        {"id": 1816670, "name": "Beijing", "latitude": 39.9075, "longitude": 116.39723,
         "elevation": 49.0, "feature_code": "PPLC", "country_code": "CN", "country": "China",
         "admin1": "Beijing", "timezone": "Asia/Shanghai", "population": 11716620},
        {"id": 8404324, "name": "Beijing", "latitude": 30.72608, "longitude": 108.67483,
         "elevation": 583.0, "feature_code": "PPL", "country_code": "CN", "country": "China",
         "admin1": "Chongqing", "timezone": "Asia/Shanghai"}]}


def fake_provider(*, search=None, single=None, archive_kwargs=None, error=None):
    """Return a stand-in for the module's single HTTP entry point."""
    archive_kwargs = dict(archive_kwargs or {})

    def call(url, params, **_kwargs):
        if error is not None:
            raise error
        if url == acquisition.GEOCODING_SEARCH_URL:
            payload = geocoding_search_payload() if search is None else search
        elif url == acquisition.GEOCODING_GET_URL:
            payload = geocoding_search_payload()["results"][0] if single is None else single
        else:
            payload = archive_payload(params, **archive_kwargs)
        return payload, json.dumps(payload), url + "?" + urlencode(params)

    return call


def read_standard_csv(path):
    text = path.read_text(encoding="utf-8")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    metadata = {row[0]: row[1] for row in rows if row and row[0].startswith("#") and len(row) > 1}
    header_index = next(index for index, row in enumerate(rows) if row and row[0] == "Year")
    return metadata, rows[header_index], rows[header_index + 1], rows[header_index + 2:]


def collected_keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            collected_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            collected_keys(item, found)
    return found


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary.name)
        (self.workspace / "Meteo").mkdir()
        (self.workspace / "Results").mkdir()

    def tearDown(self):
        self._temporary.cleanup()

    def run_download(self, provider_callable, **overrides):
        options = dict(csv_name="beijing_2024.csv", year=2024, latitude=39.9075,
                       longitude=116.39723, site_name="Beijing", country_code="CN",
                       timezone_name="Asia/Shanghai", altitude=49.0, dry_run=False)
        options.update(overrides)
        with patch.object(acquisition, "_provider_get", provider_callable):
            return download_weather(self.workspace, **options)

    def test_search_locations_returns_every_candidate(self):
        with patch.object(acquisition, "_provider_get", fake_provider()):
            result = search_locations("Beijing", country_code="CN")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["candidate_count"], 2)
        self.assertTrue(result["ambiguous"])
        self.assertTrue(result["selection_required"])
        self.assertEqual([item["location_id"] for item in result["candidates"]], [1816670, 8404324])
        self.assertEqual(result["candidates"][0]["timezone_name"], "Asia/Shanghai")
        self.assertEqual(result["candidates"][0]["country_code"], "CN")
        self.assertEqual(collected_keys(result) & set(HASH_KEYS), set())

    def test_search_locations_blocks_without_a_candidate(self):
        with patch.object(acquisition, "_provider_get", fake_provider(search={})):
            result = search_locations("Nowhere")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["selection_required"])
        self.assertIn("no geocoding candidate", result["issues"][0])

    def test_search_locations_reports_transport_failure(self):
        failure = WeatherAcquisitionError("HTTP 429 from geocoding-api.open-meteo.com: slow down")
        with patch.object(acquisition, "_provider_get", fake_provider(error=failure)):
            result = search_locations("Beijing")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("HTTP 429", result["issues"][0])

    def test_dry_run_plan_for_utc_plus_eight(self):
        plan = self.run_download(fake_provider(), dry_run=True)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["stage"], "plan")
        self.assertEqual(plan["standard_utc_offset_hours"], 8)
        self.assertEqual(plan["plan"]["target_rows"], 8784)
        self.assertTrue(plan["plan"]["leap_year"])
        self.assertEqual(plan["plan"]["utc_interval_range"],
                         ["2023-12-31T16:00:00Z", "2024-12-31T16:00:00Z"])
        self.assertEqual(plan["plan"]["request_start_date"], "2023-12-31")
        self.assertEqual(plan["plan"]["request_end_date"], "2024-12-31")
        self.assertIn("models=era5", plan["plan"]["request_url"])
        self.assertIn("timezone=GMT", plan["plan"]["request_url"])
        self.assertEqual(plan["artifacts"]["csv"], str(self.workspace / "Meteo" / "beijing_2024.csv"))
        self.assertFalse(list((self.workspace / "Meteo").iterdir()))
        self.assertTrue(any("instantaneous" in warning for warning in plan["warnings"]))

    def test_dry_run_plan_for_utc_minus_ten(self):
        plan = self.run_download(fake_provider(), dry_run=True, year=2023, csv_name="honolulu.csv",
                                 latitude=21.3, longitude=-157.85, site_name="Honolulu",
                                 country_code="US", timezone_name="Pacific/Honolulu", altitude=5.0)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["standard_utc_offset_hours"], -10)
        self.assertEqual(plan["plan"]["target_rows"], 8760)
        self.assertFalse(plan["plan"]["leap_year"])
        self.assertEqual(plan["plan"]["utc_interval_range"],
                         ["2023-01-01T10:00:00Z", "2024-01-01T10:00:00Z"])

    def test_standard_offset_ignores_daylight_saving(self):
        plan = self.run_download(fake_provider(), dry_run=True, csv_name="berlin.csv",
                                 latitude=52.52437, longitude=13.41053, site_name="Berlin",
                                 country_code="DE", timezone_name="Europe/Berlin", altitude=74.0)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["standard_utc_offset_hours"], 1)
        self.assertEqual(plan["plan"]["utc_interval_range"],
                         ["2023-12-31T23:00:00Z", "2024-12-31T23:00:00Z"])

    def test_half_hour_zone_is_blocked_instead_of_rebinned(self):
        result = self.run_download(fake_provider(), dry_run=True, csv_name="kolkata.csv",
                                   latitude=22.57, longitude=88.36, site_name="Kolkata",
                                   country_code="IN", timezone_name="Asia/Kolkata", altitude=9.0)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("not a whole hour", result["issues"][0])
        self.assertIn("does not do", result["issues"][0])

    def test_year_varying_standard_offset_is_blocked(self):
        result = self.run_download(fake_provider(), dry_run=True, year=2018, csv_name="casablanca.csv",
                                   latitude=33.57, longitude=-7.59, site_name="Casablanca",
                                   country_code="MA", timezone_name="Africa/Casablanca", altitude=50.0)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("not constant during 2018", result["issues"][0])
        self.assertIn("no single local standard time basis", result["issues"][0])

    def test_unknown_iana_zone_is_blocked_with_a_readable_issue(self):
        result = self.run_download(fake_provider(), dry_run=True, timezone_name="Mars/Olympus")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("unknown IANA zone", result["issues"][0])

    def test_missing_timezone_database_is_reported(self):
        with patch.object(acquisition, "_have_timezone_database", lambda: False), \
                patch.object(acquisition, "ZoneInfo",
                             side_effect=ZoneInfoNotFoundError("no zone data")):
            result = self.run_download(fake_provider(), dry_run=True,
                                       timezone_name="Asia/Shanghai")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("no IANA time zone database", result["issues"][0])

    def test_location_id_uses_geocoding_and_refuses_conflicting_country(self):
        resolved = self.run_download(fake_provider(), dry_run=True, location_id=1816670,
                                     csv_name="beijing_2024.csv", latitude=None, longitude=None,
                                     site_name="", country_code="", timezone_name="", altitude=None)
        self.assertEqual(resolved["status"], "ready")
        self.assertEqual(resolved["location"]["location_id"], 1816670)
        self.assertEqual(resolved["location"]["elevation_m"], 49.0)
        self.assertIn("geocoding", resolved["location"]["evidence"]["coordinates"])
        conflict = self.run_download(fake_provider(), dry_run=True, location_id=1816670,
                                     csv_name="beijing_2024.csv", latitude=None, longitude=None,
                                     site_name="", country_code="US", timezone_name="", altitude=None)
        self.assertEqual(conflict["status"], "blocked")
        self.assertIn("contradicts the geocoded ISO code", conflict["issues"][0])

    def test_location_id_and_coordinates_are_mutually_exclusive(self):
        result = self.run_download(fake_provider(), dry_run=True, location_id=1816670)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("not both", result["issues"][0])

    def test_explicit_coordinates_require_metadata(self):
        result = self.run_download(fake_provider(), dry_run=True, timezone_name="")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("timezone_name", result["issues"][0])
        self.assertIn("nothing is inferred", result["issues"][0])

    def test_year_and_method_are_verified(self):
        too_old = self.run_download(fake_provider(), dry_run=True, year=1939)
        self.assertEqual(too_old["status"], "blocked")
        self.assertIn("1940..2068", too_old["issues"][0])
        method = self.run_download(fake_provider(), dry_run=True, temperature_method="instant")
        self.assertEqual(method["status"], "blocked")
        self.assertIn("temperature_method", method["issues"][0])

    def test_execution_writes_raw_csv_and_manifest_without_hashes(self):
        result = self.run_download(fake_provider())
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["stage"], "done")
        self.assertTrue(result["raw_preserved"])
        raw_path = self.workspace / "Meteo" / "beijing_2024_openmeteo_raw.json"
        csv_path = self.workspace / "Meteo" / "beijing_2024.csv"
        manifest_path = self.workspace / "Results" / "beijing_2024_acquisition.json"
        for path in (raw_path, csv_path, manifest_path):
            self.assertTrue(path.is_file(), path)
        self.assertEqual(json.loads(raw_path.read_text(encoding="utf-8"))["timezone"], "GMT")
        metadata, header, units, rows = read_standard_csv(csv_path)
        self.assertEqual(header, ["Year", "Month", "Day", "Hour", "GHI", "DHI", "Tamb"])
        self.assertEqual(units, ["", "", "", "", "W/m2", "W/m2", "deg.C"])
        self.assertEqual(metadata["#Time step"], "Hour")
        self.assertEqual(metadata["#Time Zone"], "8")
        self.assertEqual(metadata["#Country"], "CN")
        self.assertEqual(len(rows), 8784)
        self.assertEqual(rows[0][:4], ["2024", "1", "1", "0"])
        self.assertEqual(rows[-1][:4], ["2024", "12", "31", "23"])
        self.assertIn(["2024", "2", "29", "12"], [row[:4] for row in rows])
        self.assertEqual([row[:4] for row in rows].count(["2024", "3", "1", "0"]), 1)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for text in (json.dumps(result), json.dumps(manifest), csv_path.read_text(encoding="utf-8")):
            lowered = text.lower()
            for key in HASH_KEYS:
                self.assertNotIn(key, lowered)
        self.assertEqual(result["provider_echo"]["grid_latitude"], 40.0)
        self.assertEqual(manifest["created_files"]["csv"]["rows"], 8784)
        self.assertEqual(manifest["created_files"]["manifest"],
                         {"name": "beijing_2024_acquisition.json"})
        self.assertEqual(manifest, result)

    def test_temperature_is_the_mean_of_the_two_utc_endpoints(self):
        result = self.run_download(fake_provider())
        self.assertEqual(result["status"], "prepared")
        _metadata, _header, _units, rows = read_standard_csv(
            self.workspace / "Meteo" / "beijing_2024.csv")
        # local 2024-01-01T00:00 at UTC+8 bounds the UTC instants 16:00 and 17:00.
        self.assertEqual(rows[0][6], "16.5")
        # local 2024-01-01T01:00 bounds 17:00 and 18:00.
        self.assertEqual(rows[1][6], "17.5")

    def test_missing_provider_hours_block_and_keep_the_raw_response(self):
        missing = (datetime(2024, 6, 1, 3, 0), datetime(2024, 6, 1, 4, 0))
        result = self.run_download(fake_provider(archive_kwargs={"missing": missing}))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stage"], "validation")
        self.assertTrue(result["raw_preserved"])
        self.assertTrue((self.workspace / "Meteo" / "beijing_2024_openmeteo_raw.json").is_file())
        self.assertFalse((self.workspace / "Meteo" / "beijing_2024.csv").exists())
        self.assertIn("nothing is filled", result["issues"][0])
        manifest_path = self.workspace / "Results" / "beijing_2024_acquisition.json"
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), result)

    def test_execution_creates_its_folders_but_a_plan_never_does(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            plan = download_weather(workspace, csv_name="beijing_2024.csv", year=2024,
                                    latitude=39.9075, longitude=116.39723, site_name="Beijing",
                                    country_code="CN", timezone_name="Asia/Shanghai",
                                    altitude=49.0, dry_run=True)
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(list(workspace.iterdir()), [])
            with patch.object(acquisition, "_provider_get", fake_provider()):
                executed = download_weather(workspace, csv_name="beijing_2024.csv", year=2024,
                                            latitude=39.9075, longitude=116.39723,
                                            site_name="Beijing", country_code="CN",
                                            timezone_name="Asia/Shanghai", altitude=49.0,
                                            dry_run=False)
            self.assertEqual(executed["status"], "prepared")
            self.assertTrue((workspace / "Meteo" / "beijing_2024.csv").is_file())
            self.assertTrue((workspace / "Results" / "beijing_2024_acquisition.json").is_file())
            empty = download_weather(workspace, csv_name="", year=2024, latitude=39.9075,
                                     longitude=116.39723, site_name="Beijing", country_code="CN",
                                     timezone_name="Asia/Shanghai", altitude=49.0, dry_run=True)
            self.assertEqual(empty["status"], "ready")
            self.assertFalse(empty["created_files"])

    def test_http_failure_is_reported_as_failed(self):
        failure = WeatherAcquisitionError("HTTP 429 from archive-api.open-meteo.com: rate limited")
        result = self.run_download(fake_provider(error=failure))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "request")
        self.assertIn("HTTP 429", result["issues"][0])
        self.assertFalse(list((self.workspace / "Meteo").iterdir()))
        manifest_path = self.workspace / "Results" / "beijing_2024_acquisition.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["issues"], result["issues"])
        self.assertFalse(manifest["raw_preserved"])

    def test_existing_manifest_is_not_overwritten_either(self):
        manifest_path = self.workspace / "Results" / "beijing_2024_acquisition.json"
        manifest_path.write_text('{"status": "existing"}\n', encoding="utf-8")
        result = self.run_download(fake_provider())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("refusing to overwrite", result["issues"][0])
        self.assertEqual(manifest_path.read_text(encoding="utf-8"), '{"status": "existing"}\n')

    def test_existing_destination_is_never_overwritten(self):
        csv_path = self.workspace / "Meteo" / "beijing_2024.csv"
        csv_path.write_text("existing content\n", encoding="utf-8")
        result = self.run_download(fake_provider())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("refusing to overwrite", result["issues"][0])
        self.assertEqual(csv_path.read_text(encoding="utf-8"), "existing content\n")
        self.assertFalse((self.workspace / "Meteo" / "beijing_2024_openmeteo_raw.json").exists())

    def test_diffuse_above_global_blocks_the_year(self):
        result = self.run_download(fake_provider(archive_kwargs={"dhi_above_ghi": True}))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("exceeds global horizontal irradiance", result["issues"][0])
        self.assertFalse((self.workspace / "Meteo" / "beijing_2024.csv").exists())

    def test_provider_unit_mismatch_blocks_the_year(self):
        result = self.run_download(fake_provider(archive_kwargs={"unit": "kJ/m\u00b2"}))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("is not one of", result["issues"][0])

    def test_non_gmt_provider_echo_blocks_the_year(self):
        def call(url, params, **_kwargs):
            payload = archive_payload(params)
            payload["utc_offset_seconds"] = 3600
            return payload, json.dumps(payload), url + "?" + urlencode(params)

        result = self.run_download(call)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("did not echo the requested GMT context", result["issues"][0])

    def test_unpublished_future_year_is_blocked_before_any_request(self):
        future = datetime.now(timezone.utc).year + 1
        mocked = MagicMock(side_effect=fake_provider())
        with patch.object(acquisition, "_provider_get", mocked):
            result = download_weather(self.workspace, csv_name="future.csv", year=future,
                                      latitude=39.9075, longitude=116.39723, site_name="Beijing",
                                      country_code="CN", timezone_name="Asia/Shanghai",
                                      altitude=49.0, dry_run=True)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("not fully published", result["issues"][0])
        self.assertEqual(mocked.call_count, 0)

    def test_negative_daylight_saving_zone_is_blocked(self):
        result = self.run_download(fake_provider(), dry_run=True, csv_name="dublin.csv",
                                   latitude=53.35, longitude=-6.26, site_name="Dublin",
                                   country_code="IE", timezone_name="Europe/Dublin", altitude=20.0)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("negative daylight-saving offset", result["issues"][0])
        self.assertIn("Europe/Dublin", result["issues"][0])

    def test_positive_standard_offsets_stay_exact(self):
        for zone, country, hours in (("Europe/Berlin", "DE", 1), ("Australia/Sydney", "AU", 10),
                                     ("Asia/Shanghai", "CN", 8), ("Pacific/Honolulu", "US", -10)):
            result = self.run_download(fake_provider(), dry_run=True, csv_name=zone.split("/")[1] + ".csv",
                                       latitude=10.0, longitude=20.0, site_name="Place",
                                       country_code=country, timezone_name=zone, altitude=10.0)
            self.assertEqual(result["status"], "ready", (zone, result["issues"]))
            self.assertEqual(result["standard_utc_offset_hours"], hours, zone)

    def test_offset_beyond_the_installed_cli_profile_is_blocked(self):
        for zone, name in (("Pacific/Tongatapu", "tonga"), ("Pacific/Kiritimati", "kiritimati")):
            result = self.run_download(fake_provider(), dry_run=True, csv_name=name + ".csv",
                                       latitude=-21.14, longitude=-175.2, site_name="Island",
                                       country_code="TO", timezone_name=zone, altitude=5.0)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("installed PVsyst CLI profile -12..12", result["issues"][0])
            self.assertIn("not a data error", result["issues"][0])
        lower = self.run_download(fake_provider(), dry_run=True, csv_name="midway.csv",
                                  latitude=28.2, longitude=-177.4, site_name="Midway",
                                  country_code="UM", timezone_name="Etc/GMT+12", altitude=5.0)
        self.assertEqual(lower["status"], "ready")
        self.assertEqual(lower["standard_utc_offset_hours"], -12)

    def test_geocoding_lookup_must_return_the_requested_id(self):
        impostor = dict(geocoding_search_payload()["results"][0])
        impostor["id"] = 999999
        result = self.run_download(fake_provider(single=impostor), dry_run=True, location_id=1816670,
                                   latitude=None, longitude=None, site_name="", country_code="",
                                   timezone_name="", altitude=None)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("refusing to use a different place", result["issues"][0])

    def test_geocoded_coordinates_and_country_code_are_preflighted(self):
        far = dict(geocoding_search_payload()["results"][0], latitude=95.0)
        invalid_country = dict(geocoding_search_payload()["results"][0], country_code="USA")
        for payload, expected in ((far, "is outside -90..90"),
                                  (invalid_country, "is not an uppercase ISO alpha-2 code")):
            result = self.run_download(fake_provider(single=payload), dry_run=True,
                                       location_id=1816670, latitude=None, longitude=None,
                                       site_name="", country_code="", timezone_name="",
                                       altitude=None)
            self.assertEqual(result["status"], "blocked")
            self.assertIn(expected, result["issues"][0])

    def test_site_name_must_be_one_printable_line(self):
        for name, expected in (("x" * 201, "1..200 printable UTF-8 bytes"),
                               ("two\nlines", "1..200 printable UTF-8 bytes"),
                               ("", "explicit coordinates require")):
            result = self.run_download(fake_provider(), dry_run=True, site_name=name)
            self.assertEqual(result["status"], "blocked")
            self.assertIn(expected, result["issues"][0])

    def test_coordinate_bounds_are_checked(self):
        for overrides, expected in (({"latitude": 91.0}, "latitude must lie"),
                                    ({"longitude": 181.0}, "longitude must lie")):
            result = self.run_download(fake_provider(), dry_run=True, **overrides)
            self.assertEqual(result["status"], "blocked")
            self.assertIn(expected, result["issues"][0])


if __name__ == "__main__":
    unittest.main()

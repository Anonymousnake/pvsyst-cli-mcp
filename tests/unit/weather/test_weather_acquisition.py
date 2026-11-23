"""Offline tests for the Open-Meteo ERA5 weather acquisition module."""
import csv
import functools
import http.client
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


@functools.lru_cache(maxsize=16)
def _base_archive(start_iso, end_iso, unit, grid):
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    first = datetime(start.year, start.month, start.day)
    last = datetime(end.year, end.month, end.day, 23, 0)
    labels = tuple(first + timedelta(hours=index)
                   for index in range(int((last - first).total_seconds() // 3600) + 1))
    ghi = tuple(100.0 if 8 <= stamp.hour < 16 else 0.0 for stamp in labels)
    dhi = tuple(value / 2 for value in ghi)
    temperature = tuple(float(stamp.hour) for stamp in labels)
    times = tuple(stamp.isoformat() for stamp in labels)
    return labels, times, ghi, dhi, temperature, grid, unit


def archive_payload(params, *, missing=(), dhi_above_ghi=False, unit="W/m\u00b2",
                    grid=(40.0, 116.5, 47.0)):
    """A provider-shaped payload whose values make the time mapping checkable."""
    labels, times, base_ghi, base_dhi, base_temp, grid, unit = _base_archive(
        params["start_date"], params["end_date"], unit, tuple(grid))
    ghi, dhi, temperature = list(base_ghi), list(base_dhi), list(base_temp)
    if dhi_above_ghi:
        middle = len(labels) // 2
        dhi[middle] = ghi[middle] + 10.0
    for stamp in missing:
        index = labels.index(stamp)
        ghi[index] = dhi[index] = temperature[index] = None
    return {"latitude": grid[0], "longitude": grid[1], "elevation": grid[2],
            "timezone": "GMT", "utc_offset_seconds": 0, "timezone_abbreviation": "GMT",
            "generationtime_ms": 1.0,
            "hourly_units": {"time": "iso8601", "shortwave_radiation": unit,
                             "diffuse_radiation": unit, "temperature_2m": "\u00b0C"},
            "hourly": {"time": list(times), "shortwave_radiation": ghi,
                       "diffuse_radiation": dhi, "temperature_2m": temperature}}


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


class FakeResponse:
    """A provider body delivered in fixed chunks; not an http.client.HTTPResponse."""
    headers = {}

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, _size=-1):
        return self.chunks.pop(0) if self.chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SteppingClock:
    def __init__(self, step):
        self.now, self.step = 0.0, step

    def monotonic(self):
        value = self.now
        self.now += self.step
        return value


class FakeSocket:
    def __init__(self, data):
        self.data = data

    def makefile(self, *_args, **_kwargs):
        return io.BytesIO(self.data)


def frozen_datetime(moment):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is not None else moment.replace(tzinfo=None)
    return Frozen


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolve like the workspace code does: %TEMP% can be an 8.3 short path.
        self.workspace = Path(temporary.name).resolve()
        (self.workspace / "Meteo").mkdir()
        (self.workspace / "Results").mkdir()

    def run_download(self, provider_callable, *, workspace=None, **overrides):
        options = dict(csv_name="beijing_2024.csv", year=2024, latitude=39.9075,
                       longitude=116.39723, site_name="Beijing", country_code="CN",
                       timezone_name="Asia/Shanghai", altitude=49.0, dry_run=False)
        options.update(overrides)
        with patch.object(acquisition, "_provider_get", provider_callable):
            return download_weather(workspace or self.workspace, **options)

    def test_provider_read_and_decode_failures_become_acquisition_errors(self):
        def response(read_effect):
            stream = MagicMock()
            stream.__enter__.return_value.read.side_effect = read_effect
            return stream

        for effect, message in ((http.client.IncompleteRead(b"{"), "transport failure"),
                                (ConnectionResetError("reset by peer"), "transport failure"),
                                ([b"\xff\xfe", b""], "invalid UTF-8")):
            with self.subTest(message=message, effect=type(effect).__name__):
                with patch.object(acquisition.urllib.request, "urlopen",
                                  return_value=response(effect)):
                    with self.assertRaisesRegex(WeatherAcquisitionError, message):
                        acquisition._provider_get(acquisition.ARCHIVE_URL, {"x": 1})

    def test_provider_body_deadline_prompt_read_and_bounded_sockets(self):
        slow = FakeResponse([b"{"] + [b" "] * 9 + [b"}"])
        with patch.object(acquisition.urllib.request, "urlopen", return_value=slow), \
                patch.object(acquisition, "time", SteppingClock(100.0), create=True):
            with self.assertRaisesRegex(WeatherAcquisitionError, "request deadline"):
                acquisition._provider_get(acquisition.ARCHIVE_URL, {"x": 1})
        self.assertTrue(slow.chunks, "the read must stop before the body is exhausted")

        prompt = FakeResponse([b'{"ok": ', b"true}"])
        with patch.object(acquisition.urllib.request, "urlopen", return_value=prompt), \
                patch.object(acquisition, "time", SteppingClock(0.0), create=True):
            payload, raw, _url = acquisition._provider_get(acquisition.ARCHIVE_URL, {"x": 1})
        self.assertEqual((payload, raw), ({"ok": True}, '{"ok": true}'))

        body = json.dumps({"hourly": list(range(5000))}).encode()
        framed = (b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body)) + body
        chunked = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                   + b"".join(b"%x\r\n%s\r\n" % (len(part), part)
                              for part in (body[:7000], body[7000:]))
                   + b"0\r\n\r\n")
        for framing, raw in (("Content-Length", framed), ("chunked", chunked)):
            with self.subTest(framing=framing):
                response = http.client.HTTPResponse(FakeSocket(raw))
                response.begin()
                self.assertEqual(acquisition._read_bounded(response, deadline=float("inf")), body)

    def test_search_locations_returns_candidates_without_hashes(self):
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

    def test_search_locations_empty_and_http_error(self):
        with patch.object(acquisition, "_provider_get", fake_provider(search={})):
            empty = search_locations("Nowhere")
        self.assertEqual((empty["status"], empty["candidates"], empty["selection_required"]),
                         ("blocked", [], True))
        self.assertIn("no geocoding candidate", empty["issues"][0])

        failure = WeatherAcquisitionError("HTTP 429 from geocoding-api.open-meteo.com: slow down")
        with patch.object(acquisition, "_provider_get", fake_provider(error=failure)):
            failed = search_locations("Beijing")
        self.assertEqual(failed["status"], "blocked")
        self.assertIn("HTTP 429", failed["issues"][0])

    def test_dry_run_plan_standard_offsets_ignore_daylight_saving(self):
        cases = (
            ("Asia/Shanghai", "CN", 2024, 8, 8784, True, ["2023-12-31T16:00:00Z", "2024-12-31T16:00:00Z"]),
            ("Pacific/Honolulu", "US", 2023, -10, 8760, False, ["2023-01-01T10:00:00Z", "2024-01-01T10:00:00Z"]),
            ("Europe/Berlin", "DE", 2024, 1, 8784, True, ["2023-12-31T23:00:00Z", "2024-12-31T23:00:00Z"]),
            ("Australia/Sydney", "AU", 2024, 10, 8784, True, ["2023-12-31T14:00:00Z", "2024-12-31T14:00:00Z"]),
            ("Etc/GMT+12", "UM", 2024, -12, 8784, True, ["2024-01-01T12:00:00Z", "2025-01-01T12:00:00Z"]),
        )
        for zone, country, year, offset, rows, leap, utc_range in cases:
            with self.subTest(zone=zone):
                csv_name = f"{zone.split('/')[-1].lower()}.csv"
                plan = self.run_download(fake_provider(), dry_run=True, year=year, csv_name=csv_name,
                                         country_code=country, timezone_name=zone)
                self.assertEqual(plan["status"], "ready", plan["issues"])
                self.assertEqual(plan["stage"], "plan")
                self.assertEqual(plan["standard_utc_offset_hours"], offset)
                self.assertEqual(plan["plan"]["target_rows"], rows)
                self.assertEqual(plan["plan"]["leap_year"], leap)
                self.assertEqual(plan["plan"]["utc_interval_range"], utc_range)
                self.assertIn("models=era5", plan["plan"]["request_url"])
                self.assertIn("timezone=GMT", plan["plan"]["request_url"])
                self.assertEqual(plan["artifacts"]["csv"], str(self.workspace / "Meteo" / csv_name))
                self.assertTrue(any("instantaneous" in w for w in plan["warnings"]))
        shanghai = self.run_download(fake_provider(), dry_run=True)
        self.assertEqual((shanghai["plan"]["request_start_date"], shanghai["plan"]["request_end_date"]),
                         ("2023-12-31", "2024-12-31"))
        self.assertFalse(list((self.workspace / "Meteo").iterdir()))

    def test_blocked_timezones_non_constant_half_hour_dst_and_bounds(self):
        cases = (
            ({"timezone_name": "Asia/Kolkata", "country_code": "IN"}, ("not a whole hour", "does not do")),
            ({"year": 2018, "timezone_name": "Africa/Casablanca", "country_code": "MA"},
             ("not constant during 2018", "no single local standard time basis")),
            ({"timezone_name": "Europe/Dublin", "country_code": "IE"},
             ("negative daylight-saving offset", "Europe/Dublin")),
            ({"timezone_name": "Pacific/Tongatapu", "country_code": "TO"},
             ("installed PVsyst CLI profile -12..12", "not a data error")),
            ({"timezone_name": "Pacific/Kiritimati", "country_code": "KI"},
             ("installed PVsyst CLI profile -12..12", "not a data error")),
        )
        for overrides, snippets in cases:
            with self.subTest(timezone=overrides["timezone_name"]):
                result = self.run_download(fake_provider(), dry_run=True, **overrides)
                self.assertEqual(result["status"], "blocked")
                for snippet in snippets:
                    self.assertIn(snippet, result["issues"][0])

    def test_unknown_iana_zone_and_missing_tzdata(self):
        unknown = self.run_download(fake_provider(), dry_run=True, timezone_name="Mars/Olympus")
        self.assertEqual(unknown["status"], "blocked")
        self.assertIn("unknown IANA zone", unknown["issues"][0])

        with patch.object(acquisition, "_have_timezone_database", lambda: False), \
                patch.object(acquisition, "ZoneInfo", side_effect=ZoneInfoNotFoundError("no zone data")):
            missing = self.run_download(fake_provider(), dry_run=True, timezone_name="Asia/Shanghai")
        self.assertEqual(missing["status"], "blocked")
        self.assertIn("no IANA time zone database", missing["issues"][0])

    def test_geocoding_location_id_resolution_and_preflight(self):
        resolved = self.run_download(fake_provider(), dry_run=True, location_id=1816670,
                                     latitude=None, longitude=None, site_name="",
                                     country_code="", timezone_name="", altitude=None)
        self.assertEqual((resolved["status"], resolved["location"]["location_id"],
                          resolved["location"]["elevation_m"]), ("ready", 1816670, 49.0))
        self.assertIn("geocoding", resolved["location"]["evidence"]["coordinates"])

        conflict = self.run_download(fake_provider(), dry_run=True, location_id=1816670,
                                     latitude=None, longitude=None, site_name="",
                                     country_code="US", timezone_name="", altitude=None)
        self.assertEqual(conflict["status"], "blocked")
        self.assertIn("contradicts the geocoded ISO code", conflict["issues"][0])

        base = geocoding_search_payload()["results"][0]
        for overrides, msg in (
            ({"id": 999999}, "refusing to use a different place"),
            ({"latitude": 95.0}, "is outside -90..90"),
            ({"country_code": "USA"}, "is not an uppercase ISO alpha-2 code"),
        ):
            with self.subTest(overrides=overrides):
                res = self.run_download(fake_provider(single=dict(base, **overrides)), dry_run=True,
                                        location_id=1816670, latitude=None, longitude=None,
                                        site_name="", country_code="", timezone_name="", altitude=None)
                self.assertEqual(res["status"], "blocked")
                self.assertIn(msg, res["issues"][0])

    def test_explicit_location_validation_and_coordinate_bounds(self):
        for overrides, snippets in (
            ({"location_id": 1816670}, ("not both",)),
            ({"timezone_name": ""}, ("timezone_name", "nothing is inferred")),
            ({"site_name": "x" * 201}, ("1..200 printable UTF-8 bytes",)),
            ({"site_name": "two\nlines"}, ("1..200 printable UTF-8 bytes",)),
            ({"site_name": ""}, ("explicit coordinates require",)),
            ({"latitude": 91.0}, ("latitude must lie",)),
            ({"longitude": 181.0}, ("longitude must lie",)),
            ({"temperature_method": "instant"}, ("temperature_method",)),
        ):
            with self.subTest(overrides=overrides):
                result = self.run_download(fake_provider(), dry_run=True, **overrides)
                self.assertEqual(result["status"], "blocked")
                for snippet in snippets:
                    self.assertIn(snippet, result["issues"][0])

    def test_era5_and_prepared_year_bounds_block_before_request(self):
        provider = MagicMock(side_effect=fake_provider())
        for year, dry_run in ((1940, True), (1950, True), (1968, False)):
            with self.subTest(year=year, dry_run=dry_run):
                result = self.run_download(provider, year=year, dry_run=dry_run)
                self.assertEqual(result["status"], "blocked", result)
                self.assertIn("1969..2068", result["issues"][0])
                self.assertIn("two-digit", result["issues"][0])
        too_old = self.run_download(provider, year=1939, dry_run=True)
        self.assertEqual(too_old["status"], "blocked")
        self.assertIn("1940..2068", too_old["issues"][0])
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(list((self.workspace / "Meteo").iterdir()), [])
        self.assertEqual(list((self.workspace / "Results").iterdir()), [])
        self.assertEqual(self.run_download(provider, year=1969, dry_run=True)["status"], "ready")

    def test_unpublished_year_and_era5_lag_block_before_request(self):
        provider = MagicMock(side_effect=fake_provider())
        future = datetime.now(timezone.utc).year + 1
        res_future = self.run_download(provider, year=future, csv_name="future.csv", dry_run=True)
        self.assertEqual(res_future["status"], "blocked")
        self.assertIn("not fully published", res_future["issues"][0])

        early = frozen_datetime(datetime(2026, 1, 3, 12, tzinfo=timezone.utc))
        with patch.object(acquisition, "datetime", early):
            for dry_run in (True, False):
                with self.subTest(dry_run=dry_run):
                    res = self.run_download(provider, year=2025, dry_run=dry_run)
                    self.assertEqual(res["status"], "blocked", res)
                    self.assertIn("not fully published", res["issues"][0])
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(list((self.workspace / "Meteo").iterdir()), [])

        later = frozen_datetime(datetime(2026, 1, 7, 12, tzinfo=timezone.utc))
        with patch.object(acquisition, "datetime", later):
            self.assertEqual(self.run_download(provider, year=2025, dry_run=True)["status"], "ready")

    def test_number_text_never_writes_negative_zero(self):
        for value in (-1e-7, -4e-7, -0.0, 0, 1e-7):
            with self.subTest(value=value):
                self.assertEqual(acquisition._number_text(value), "0")
        self.assertEqual(acquisition._number_text(-1.5), "-1.5")
        self.assertEqual(acquisition._number_text(2.000001), "2.000001")
        site = {"name": "Equator", "country_code": "EC", "latitude": -1e-7, "longitude": -78.5,
                "elevation_m": 2850.0}
        text = acquisition._standard_csv_text([(datetime(2024, 1, 1), 0.0, 0.0, -2e-7)],
                                              site=site, year=2024, offset_hours=-5)
        self.assertIn("#Latitude;0\n", text)
        self.assertTrue(text.endswith("2024;1;1;0;0;0;0\n"), text)

    def test_execution_writes_csv_endpoint_mean_and_manifest_without_hashes(self):
        result = self.run_download(fake_provider())
        self.assertEqual((result["status"], result["stage"], result["raw_preserved"]), ("prepared", "done", True))
        raw_path = self.workspace / "Meteo" / "beijing_2024_openmeteo_raw.json"
        csv_path = self.workspace / "Meteo" / "beijing_2024.csv"
        manifest_path = self.workspace / "Results" / "beijing_2024_acquisition.json"
        for path in (raw_path, csv_path, manifest_path):
            self.assertTrue(path.is_file(), path)
        self.assertEqual(json.loads(raw_path.read_text(encoding="utf-8"))["timezone"], "GMT")
        metadata, header, units, rows = read_standard_csv(csv_path)
        self.assertEqual(header, ["Year", "Month", "Day", "Hour", "GHI", "DHI", "Tamb"])
        self.assertEqual(units, ["", "", "", "", "W/m2", "W/m2", "deg.C"])
        self.assertEqual((metadata["#Time step"], metadata["#Time Zone"], metadata["#Country"]), ("Hour", "8", "CN"))
        self.assertEqual((len(rows), rows[0][:4], rows[-1][:4]),
                         (8784, ["2024", "1", "1", "0"], ["2024", "12", "31", "23"]))
        self.assertIn(["2024", "2", "29", "12"], [row[:4] for row in rows])
        self.assertEqual([row[:4] for row in rows].count(["2024", "3", "1", "0"]), 1)
        # local 2024-01-01T00:00 at UTC+8 bounds UTC 16:00 and 17:00; 01:00 bounds 17:00 and 18:00.
        self.assertEqual((rows[0][6], rows[1][6]), ("16.5", "17.5"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for text in (json.dumps(result), json.dumps(manifest), csv_path.read_text(encoding="utf-8")):
            lowered = text.lower()
            for key in HASH_KEYS:
                self.assertNotIn(key, lowered)
        self.assertEqual(result["provider_echo"]["grid_latitude"], 40.0)
        self.assertEqual(manifest["created_files"]["csv"]["rows"], 8784)
        self.assertEqual(manifest["created_files"]["manifest"], {"name": "beijing_2024_acquisition.json"})
        self.assertEqual(manifest, result)

    def test_execution_creates_folders_while_plan_never_does(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            plan = self.run_download(fake_provider(), workspace=workspace, dry_run=True)
            self.assertEqual((plan["status"], list(workspace.iterdir())), ("ready", []))
            executed = self.run_download(fake_provider(), workspace=workspace, dry_run=False)
            self.assertEqual(executed["status"], "prepared")
            self.assertTrue((workspace / "Meteo" / "beijing_2024.csv").is_file())
            self.assertTrue((workspace / "Results" / "beijing_2024_acquisition.json").is_file())
            empty = self.run_download(fake_provider(), workspace=workspace, csv_name="", dry_run=True)
            self.assertEqual((empty["status"], empty["created_files"]), ("ready", {}))

    def test_missing_provider_hours_block_and_keep_raw_response(self):
        missing = (datetime(2024, 6, 1, 3, 0), datetime(2024, 6, 1, 4, 0))
        result = self.run_download(fake_provider(archive_kwargs={"missing": missing}))
        self.assertEqual((result["status"], result["stage"], result["raw_preserved"]),
                         ("blocked", "validation", True))
        self.assertTrue((self.workspace / "Meteo" / "beijing_2024_openmeteo_raw.json").is_file())
        self.assertFalse((self.workspace / "Meteo" / "beijing_2024.csv").exists())
        self.assertIn("nothing is filled", result["issues"][0])
        manifest_path = self.workspace / "Results" / "beijing_2024_acquisition.json"
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), result)

    def test_http_failure_is_reported_as_failed(self):
        failure = WeatherAcquisitionError("HTTP 429 from archive-api.open-meteo.com: rate limited")
        result = self.run_download(fake_provider(error=failure))
        self.assertEqual((result["status"], result["stage"]), ("failed", "request"))
        self.assertIn("HTTP 429", result["issues"][0])
        self.assertFalse(list((self.workspace / "Meteo").iterdir()))
        manifest_path = self.workspace / "Results" / "beijing_2024_acquisition.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual((manifest["status"], manifest["issues"], manifest["raw_preserved"]),
                         ("failed", result["issues"], False))

    def test_existing_csv_or_manifest_is_never_overwritten(self):
        for target_rel, content, forbid_raw in (
            ("Results/beijing_2024_acquisition.json", '{"status": "existing"}\n', False),
            ("Meteo/beijing_2024.csv", "existing content\n", True),
        ):
            with self.subTest(target=target_rel), tempfile.TemporaryDirectory() as raw:
                ws = Path(raw)
                (ws / "Meteo").mkdir()
                (ws / "Results").mkdir()
                target = ws / target_rel
                target.write_text(content, encoding="utf-8")
                result = self.run_download(fake_provider(), workspace=ws)
                self.assertEqual(result["status"], "blocked")
                self.assertIn("refusing to overwrite", result["issues"][0])
                self.assertEqual(target.read_text(encoding="utf-8"), content)
                if forbid_raw:
                    self.assertFalse((ws / "Meteo" / "beijing_2024_openmeteo_raw.json").exists())

    def test_provider_validation_rejects_dhi_gt_ghi_bad_unit_and_non_gmt_echo(self):
        def bad_echo(url, params, **_kwargs):
            payload = archive_payload(params)
            payload["utc_offset_seconds"] = 3600
            return payload, json.dumps(payload), url + "?" + urlencode(params)

        cases = (
            ("dhi_above_ghi", fake_provider(archive_kwargs={"dhi_above_ghi": True}),
             "exceeds global horizontal irradiance"),
            ("bad_unit", fake_provider(archive_kwargs={"unit": "kJ/m\u00b2"}), "is not one of"),
            ("non_gmt", bad_echo, "did not echo the requested GMT context"),
        )
        for label, provider, expected in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as raw:
                ws = Path(raw)
                (ws / "Meteo").mkdir()
                (ws / "Results").mkdir()
                result = self.run_download(provider, workspace=ws)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(expected, result["issues"][0])
                self.assertFalse((ws / "Meteo" / "beijing_2024.csv").exists())


if __name__ == "__main__":
    unittest.main()

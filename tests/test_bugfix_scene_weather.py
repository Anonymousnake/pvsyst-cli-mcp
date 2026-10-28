"""Regression tests for the scene/weather review fixes (part 6).

Each test pins one verified defect: a provider body read without an overall deadline, a
download year range wider than the prepare/review range, a publication bound that ignored the
documented ERA5 lag, negative-zero native text, trailing CSV delimiters, non-atomic weather
manifests, an unreachable ``completed`` review status, exact grouped-angle comparisons, a
misleading one-sided PNomAC_InMPPT reason and an unreachable southern-hemisphere message.
"""
import http.client
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pvsyst_auto.project.paths as paths_module
import pvsyst_auto.weather.acquisition as acquisition
import pvsyst_auto.weather.preparation as preparation
import pvsyst_auto.weather.review as review_module
from pvsyst_auto.components.editor import objects
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.native.cli import PVsystCLI
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.author import source_profile
from pvsyst_auto.scene.mapping import verify_grouped_hardware
from pvsyst_auto.weather.acquisition import WeatherAcquisitionError, download_weather
from pvsyst_auto.weather.csv import inspect_weather_csv
from pvsyst_auto.weather.preparation import normalized_rows, prepare_weather

from test_weather import native_weather_commands, weather_text
from test_weather_acquisition import fake_provider
from test_weather_review import ReviewFixture, weather_csv_text


def with_trailing_delimiter(text, delimiter=";"):
    return "".join(line + delimiter + "\n" for line in text.splitlines())


class FakeResponse:
    """A provider body delivered in fixed chunks; not an http.client.HTTPResponse."""
    headers = {}

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, size=-1):
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

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.data)


def frozen_datetime(moment):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is not None else moment.replace(tzinfo=None)
    return Frozen


class AcquisitionFixes(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        (self.workspace / "Meteo").mkdir()
        (self.workspace / "Results").mkdir()

    def download(self, provider, **overrides):
        options = dict(csv_name="site.csv", year=2024, latitude=39.9075, longitude=116.39723,
                       site_name="Beijing", country_code="CN", timezone_name="Asia/Shanghai",
                       altitude=49.0, dry_run=True)
        options.update(overrides)
        with patch.object(acquisition, "_provider_get", provider):
            return download_weather(self.workspace, **options)

    def test_number_text_never_writes_a_negative_zero(self):
        for value in (-1e-7, -4e-7, -0.0, 0, 1e-7):
            self.assertEqual(acquisition._number_text(value), "0", value)
        self.assertEqual(acquisition._number_text(-1.5), "-1.5")
        self.assertEqual(acquisition._number_text(2.000001), "2.000001")
        site = {"name": "Equator", "country_code": "EC", "latitude": -1e-7, "longitude": -78.5,
                "elevation_m": 2850.0}
        text = acquisition._standard_csv_text([(datetime(2024, 1, 1), 0.0, 0.0, -2e-7)],
                                              site=site, year=2024, offset_hours=-5)
        self.assertIn("#Latitude;0\n", text)
        self.assertTrue(text.endswith("2024;1;1;0;0;0;0\n"), text)

    def test_years_the_prepare_and_review_range_rejects_are_blocked_before_any_request(self):
        provider = MagicMock(side_effect=fake_provider())
        for year, dry_run in ((1940, True), (1950, True), (1968, False)):
            with self.subTest(year=year, dry_run=dry_run):
                result = self.download(provider, year=year, dry_run=dry_run)
                self.assertEqual(result["status"], "blocked", result)
                self.assertIn("1969..2068", result["issues"][0])
                self.assertIn("two-digit", result["issues"][0])
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(list((self.workspace / "Meteo").iterdir()), [])
        self.assertEqual(list((self.workspace / "Results").iterdir()), [])
        # Outside the ERA5 archive the original range message is kept.
        self.assertIn("1940..2068", self.download(provider, year=1939)["issues"][0])
        self.assertEqual(self.download(provider, year=1969)["status"], "ready")

    def test_a_year_inside_the_era5_publication_lag_is_blocked_before_any_request(self):
        provider = MagicMock(side_effect=fake_provider())
        early = frozen_datetime(datetime(2026, 1, 3, 12, tzinfo=timezone.utc))
        with patch.object(acquisition, "datetime", early):
            for dry_run in (True, False):
                result = self.download(provider, year=2025, dry_run=dry_run)
                self.assertEqual(result["status"], "blocked", result)
                self.assertIn("not fully published", result["issues"][0])
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(list((self.workspace / "Meteo").iterdir()), [])
        later = frozen_datetime(datetime(2026, 1, 7, 12, tzinfo=timezone.utc))
        with patch.object(acquisition, "datetime", later):
            self.assertEqual(self.download(provider, year=2025)["status"], "ready")

    def test_a_trickling_provider_body_stops_at_the_overall_deadline(self):
        response = FakeResponse([b"{"] + [b" "] * 9 + [b"}"])
        with patch.object(acquisition.urllib.request, "urlopen", return_value=response), \
                patch.object(acquisition, "time", SteppingClock(100.0), create=True):
            with self.assertRaisesRegex(WeatherAcquisitionError, "request deadline"):
                acquisition._provider_get(acquisition.ARCHIVE_URL, {"x": 1})
        self.assertTrue(response.chunks, "the read must stop before the body is exhausted")

    def test_a_prompt_provider_body_is_read_completely(self):
        response = FakeResponse([b'{"ok": ', b"true}"])
        with patch.object(acquisition.urllib.request, "urlopen", return_value=response), \
                patch.object(acquisition, "time", SteppingClock(0.0), create=True):
            payload, raw, _url = acquisition._provider_get(acquisition.ARCHIVE_URL, {"x": 1})
        self.assertEqual((payload, raw), ({"ok": True}, '{"ok": true}'))

    def test_real_http_responses_are_read_with_bounded_socket_reads(self):
        body = json.dumps({"hourly": list(range(5000))}).encode()
        framed = (b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body)) + body
        chunked = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                   + b"".join(b"%x\r\n%s\r\n" % (len(part), part)
                              for part in (body[:7000], body[7000:]))
                   + b"0\r\n\r\n")
        for raw in (framed, chunked):
            response = http.client.HTTPResponse(FakeSocket(raw))
            response.begin()
            self.assertEqual(acquisition._read_bounded(response, deadline=float("inf")), body)


class TrailingDelimiterCsv(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "weather.csv"

    def inspect(self, text):
        self.path.write_text(text, encoding="utf-8-sig", newline="")
        return inspect_weather_csv(self.path)

    def test_trailing_delimiters_are_not_an_empty_column(self):
        plain = self.inspect(weather_text(2023))
        for separator in (";", ","):
            with self.subTest(separator=separator):
                report = self.inspect(with_trailing_delimiter(weather_text(2023, separator), separator))
                self.assertIs(report["valid"], True, report["issues"])
                self.assertEqual(report["units"], plain["units"])
                self.assertEqual(report["ignored_columns"], [])
                self.assertEqual(report["months"], plain["months"])
                self.assertEqual(report["metadata"], plain["metadata"])
        rows = list(normalized_rows(self.path, report))
        self.assertEqual(len(rows), 8760)
        self.assertEqual(rows[6], (datetime(2023, 1, 1, 6), 100.0, 50.0, 20.0))

    def test_trailing_delimiters_on_data_rows_only(self):
        lines = weather_text(2023).splitlines()
        text = "".join(line + (";" if line[:1].isdigit() else "") + "\n" for line in lines)
        report = self.inspect(text)
        self.assertIs(report["valid"], True, report["issues"])
        self.assertEqual(report["rows"], 8760)

    def test_only_empty_trailing_cells_are_dropped(self):
        source = weather_text(2023)
        with self.assertRaisesRegex(ValueError, "distinct nonempty column names"):
            self.inspect(source.replace("Year;Month;", "Year;;Month;"))
        with self.assertRaisesRegex(ValueError, "Unit row must have one cell"):
            self.inspect(source.replace(";deg.C;W/m2\n", ";deg.C;W/m2;m/s\n").replace(
                "GHI;Tamb;DHI\n", "GHI;Tamb;DHI;\n"))
        report = self.inspect(source.replace("2023;1;1;6;100.0;20;50.0\n",
                                             "2023;1;1;6;100.0;20;50.0;7\n"))
        self.assertIs(report["valid"], False)
        self.assertIn("data width differs", " ".join(report["issues"]))


class PrepareManifestWrites(unittest.TestCase):
    request = dict(csv_name="weather.csv", year=2020, country_code="CN",
                   time_basis="local_standard_interval_start", dry_run=False)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "Meteo").mkdir()
        (self.root / "Meteo" / "weather.csv").write_text(
            weather_text(year=2020).replace("#Country;Test", "#Country;China"), encoding="utf-8-sig")
        self.cli = PVsystCLI(sys.executable, self.root)
        self.cli._help_cache["create-site"] = "\n".join(
            "--" + name + ":" for name in ("site-name", "altitude", "timezone", "country-code"))
        self.cli._help_cache["convert-meteo"] = "--input-csv-file"

    def prepare(self, stem):
        return prepare_weather(self.cli, sit_name=stem + ".SIT", met_name=stem + ".MET", **self.request)

    def assert_only_manifest(self, stem):
        # No temporary copy of any name may be left next to the manifest.
        self.assertEqual([path.name for path in (self.root / "Results").iterdir()], [stem + "_weather.json"])

    def test_a_failed_save_keeps_the_last_complete_manifest(self):
        real = self.cli.create_site_details

        def unserializable(*args, **kwargs):
            return dict(real(*args, **kwargs), fixture_ratio=float("nan"))

        with patch.object(self.cli, "_run", side_effect=native_weather_commands()), \
                patch.object(self.cli, "create_site_details", side_effect=unserializable):
            result = self.prepare("Nan")
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("not JSON compliant", result["error"])
        self.assertTrue(any(issue.startswith("Weather manifest write failed")
                            for issue in result["issues"]), result["issues"])
        manifest = json.loads((self.root / "Results" / "Nan_weather.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["status"], manifest["stage"]), ("running", "site"))
        self.assert_only_manifest("Nan")

    def test_a_replace_failure_is_reported_and_leaves_no_temporary_file(self):
        class LockedManifest:
            """The real os module, except that every replace is refused."""
            def __getattr__(self, name):
                return getattr(os, name)

            @staticmethod
            def replace(*_args):
                raise PermissionError("fixture: manifest is locked")

        with patch.object(self.cli, "_run", side_effect=native_weather_commands()), \
                patch.object(paths_module, "os", LockedManifest()):
            result = self.prepare("Locked")
        self.assertEqual((result["status"], result["stage"]), ("failed", "normalized_inputs"), result)
        self.assertIn("Weather manifest write failed: fixture: manifest is locked", result["issues"])
        manifest = json.loads((self.root / "Results" / "Locked_weather.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["stage"], "preflight")
        self.assert_only_manifest("Locked")

    def test_a_final_save_failure_never_masks_the_exception_in_flight(self):
        manifest = self.root / "Results" / "Masked_weather.json"

        def unexpected(path, *, time_zone):
            manifest.unlink()
            manifest.mkdir()  # the final save can no longer replace this path
            raise RuntimeError("fixture: unexpected native failure")

        with patch.object(self.cli, "_run", side_effect=native_weather_commands()), \
                patch.object(preparation, "check_generated_met", side_effect=unexpected):
            with self.assertRaisesRegex(RuntimeError, "fixture: unexpected native failure"):
                self.prepare("Masked")
        self.assert_only_manifest("Masked")


class ReviewStatusAndTrailingCsv(ReviewFixture):
    def test_the_documented_status_contract_matches_the_reachable_statuses(self):
        report = self.review(acquisition_manifest_name="fixture_acquisition.json")
        self.assertEqual(report["status"], "needs_review")
        self.assertNotIn("completed", report["scope"])
        self.assertIn("never reports ``completed``", review_module.__doc__)

    def test_a_prepared_csv_with_trailing_delimiters_is_reviewed(self):
        (self.root / "Meteo" / self.csv_name).write_text(
            with_trailing_delimiter(weather_csv_text(self.year, self.rows)), encoding="utf-8-sig")
        report = self.review(acquisition_manifest_name="fixture_acquisition.json")
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["csv_to_native"]["rows"], len(self.rows))
        self.assertTrue(report["provider_to_csv"]["matches"])


ORIENT = """  PVObject_=pvOrient
    NoOrient={id}
    FieldType=FixedPlane
    FieldTilt={tilt}
    FieldAzim={azimuth}
    BaseSlope=0
    UsesTiltAver=False
    UsesAzimAver=False
    UsesSlopeAver=False
  End of TOrientGroup
"""
BRANCH = """InverterNode Start;
  NElements=1
  SubArrayId={id}
  SubArrayName={name}
  Children=Start
    StringNode Start;
      NElements=2
      SubArrayId={id}
      SubArrayName={name}
    StringNode End;
  Children=End
InverterNode End;
"""
ARRAY = """  PVObject_=pvSubArray
    Comment={name}
    SubArrayId={id}
    NoOrientation={id}
    PVModule=module.PAN
    GInverter=inverter.OND
    NModSerie=9
    NStringCh=2
    NStrOrient1=2
    NInverter=1
    NInvMPPT=1
{extra}  End of PVObject pvSubArray
"""
TABLE = """    PVObject_{id}=pvShdArrayTable
      NoOrient={id}
      NModInHeight=3
      NModInWidth=6
      DInclin={tilt}
      DAzimut={azimuth}
      BaseSlope=0.000000
    End of PVObject pvShdArrayTable
"""
PLANES = ({"id": 1, "name": "Array A", "tilt": 30.1234567, "azimuth": -12.3},
          {"id": 2, "name": "Array B", "tilt": 20.0, "azimuth": 90.0})


def grouped_variant(*, tilt_text="30.1234567", tables=None, extra=""):
    """Two fixed planes with exclusive one-inverter branches; tables only on the saved side."""
    text = "PVObject_=pvVCalcul\n  Version=8.1.6\n"
    text += "".join(ORIENT.format(id=plane["id"], azimuth=plane["azimuth"],
                                  tilt=tilt_text if plane["id"] == 1 else plane["tilt"])
                    for plane in PLANES)
    text += ("  PVObject_PVMainArray=pvMainArray\n  PVObject_SystemCircuit=pvCircuit\n"
             "  Version=8.0.0\n  BaseNode=Start\nInjectionPointNode Start;\nChildren=Start\n"
             + "".join(BRANCH.format(**plane) for plane in PLANES)
             + "Children=End\nInjectionPointNode End;\n  BaseNode=End\n  End of PVObject pvCircuit\n")
    text += "".join(ARRAY.format(extra=extra if plane["id"] == 1 else "", **plane) for plane in PLANES)
    text += "  End of PVObject pvMainArray\n"
    if tables is not None:
        text += ("  PVObject_Ombrage=pvShading\n    Flags=$00000100\n"
                 "    ListeObjets, list of=2 TShdObject\n"
                 + "".join(TABLE.format(id=plane["id"], tilt=tilt, azimuth=azimuth)
                           for plane, (tilt, azimuth) in zip(PLANES, tables))
                 + "    End of ListeObjets\n  End of PVObject pvShading\n")
    return text + "End of PVObject pvVCalcul\n"


class GroupedHardwareVerification(unittest.TestCase):
    source = {"orientations": [{"id": plane["id"], "module_total": 18, "tilt_deg": plane["tilt"],
                                "field_azimuth_deg": plane["azimuth"]} for plane in PLANES]}
    native_tables = (("30.123457", "-12.300000"), ("20.000000", "90.000000"))

    def verify(self, saved):
        texts = [text.replace("End of TOrientGroup", "End of PVObject pvOrient")
                 for text in (grouped_variant(), saved)]
        return verify_grouped_hardware(objects(texts[0]), objects(texts[1]),
                                       texts[0].splitlines(True), texts[1].splitlines(True),
                                       self.source)

    def test_six_decimal_native_angles_match_their_source_within_the_angle_tolerance(self):
        for label, saved in (
                ("re-serialized plane", grouped_variant(tilt_text="30.123457", tables=self.native_tables)),
                ("unchanged plane", grouped_variant(tables=self.native_tables))):
            with self.subTest(label):
                result = self.verify(saved)
                self.assertTrue(result["source_orientations_preserved"])
                self.assertEqual([row["orientation_id"] for row in result["orientation_mapping"]], [1, 2])
                self.assertEqual(len(result["circuit_mapping"]), 2)

    def test_real_angle_changes_still_fail(self):
        with self.assertRaisesRegex(ValueError, "changed FieldTilt"):
            self.verify(grouped_variant(tilt_text="30.2", tables=self.native_tables))
        moved = (("30.2", "-12.300000"), self.native_tables[1])
        with self.assertRaisesRegex(ValueError, "Native PV table differs"):
            self.verify(grouped_variant(tables=moved))

    def test_a_one_sided_inverter_power_declaration_is_reported_as_a_change(self):
        saved = grouped_variant(tables=self.native_tables, extra="    PNomAC_InMPPT=5000\n")
        with self.assertRaisesRegex(ValueError, "changed its inverter input power declaration"):
            self.verify(saved)


SITE_VARIANT = """PVObject_=pvVCalcul
  Version=8.1.6
  Flags=$00
  PVObject_SiteSimul=pvSite
    Latitude={latitude}
    Longitude=151.2
    Altitude=10
  End of PVObject pvSite
  Orientations, list of=1 TOrientGroup
  PVObject_=pvOrient
    NoOrient=1
    Flags=$00
    FieldType=FixedPlane
    FieldTilt=20
    FieldAzim=0
  End of TOrientGroup
  End of Orientations
  PVObject_PVMainArray=pvMainArray
  PVObject_SystemCircuit=pvCircuit
  Version=8.0.0
  BaseNode=Start
InjectionPointNode Start;
Children=Start
{branch}Children=End
InjectionPointNode End;
  BaseNode=End
  End of PVObject pvCircuit
{array}  End of PVObject pvMainArray
  PVObject_System=pvSystem
    SystemType=Grid
  End of PVObject pvSystem
End of PVObject pvVCalcul
"""


class SourceProfileLatitude(unittest.TestCase):
    def profile(self, latitude):
        with tempfile.TemporaryDirectory() as raw:
            store = VariantStore(raw, ComponentStore(raw))
            text = SITE_VARIANT.format(latitude=latitude, branch=BRANCH.format(**PLANES[0]),
                                       array=ARRAY.format(extra="", **PLANES[0]))
            _, lines = store._text(text.replace("\n", "\r\n").encode("utf-8"))
            arrays, system = store._sections(lines)
            return source_profile(store, lines, arrays, system)

    def test_a_southern_site_gets_the_hemisphere_reason(self):
        self.assertEqual(self.profile("45.5")["site"]["latitude"], 45.5)
        with self.assertRaisesRegex(ValueError, "northern hemisphere"):
            self.profile("-33.9")
        with self.assertRaisesRegex(ValueError, "Latitude must be >= -90"):
            self.profile("-91")


if __name__ == "__main__":
    unittest.main()

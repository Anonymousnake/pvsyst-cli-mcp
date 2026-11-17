"""Tests for weather evidence review, provider correlation, and generated MET guards."""
import calendar
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from pvsyst_auto.weather.csv import inspect_weather_values
from pvsyst_auto.weather.preparation import MEF_TEMPLATE, check_generated_met, compare_native_weather
from pvsyst_auto.weather.review import review_weather

OFFSET = 8


def year_hours(year):
    return (366 if calendar.isleap(year) else 365) * 24


def provider_labels(first_day, last_day):
    first = datetime(first_day.year, first_day.month, first_day.day)
    last = datetime(last_day.year, last_day.month, last_day.day, 23, 0)
    return [first + timedelta(hours=index)
            for index in range(int((last - first).total_seconds() // 3600) + 1)]


def provider_values(labels, *, null_labels=(), tam_offset=0.0):
    ghi, dhi, tamb = [], [], []
    for stamp in labels:
        day = 100.0 if 8 <= stamp.hour < 16 else 0.0
        if stamp in null_labels:
            ghi.append(None), dhi.append(None), tamb.append(None)
            continue
        ghi.append(day)
        dhi.append(day / 2.0)
        tamb.append(round(-5.0 + stamp.hour * 0.5 + tam_offset, 1))
    return ghi, dhi, tamb


def provider_payload(year, *, offset=OFFSET, null_labels=(), timezone="GMT", utc_offset=0,
                     ghi_unit="W/m\u00b2", tam_unit="\u00b0C"):
    utc_start = datetime(year, 1, 1) - timedelta(hours=offset)
    utc_end = utc_start + timedelta(hours=year_hours(year))
    labels = provider_labels(utc_start.date(), utc_end.date())
    ghi, dhi, tamb = provider_values(labels, null_labels=null_labels)
    return {
        "latitude": 40.0, "longitude": 116.5, "elevation": 47.0,
        "timezone": timezone, "utc_offset_seconds": utc_offset,
        "hourly_units": {"time": "iso8601", "shortwave_radiation": ghi_unit,
                         "diffuse_radiation": ghi_unit, "temperature_2m": tam_unit},
        "hourly": {"time": [stamp.isoformat() for stamp in labels],
                   "shortwave_radiation": ghi, "diffuse_radiation": dhi, "temperature_2m": tamb},
    }


def csv_rows(year, *, offset=OFFSET, drop=None, tam_offset=0.0, negative_at=None):
    hours = year_hours(year)
    utc_start = datetime(year, 1, 1) - timedelta(hours=offset)
    utc_end = utc_start + timedelta(hours=hours)
    labels = provider_labels(utc_start.date(), utc_end.date())
    index = {stamp: position for position, stamp in enumerate(labels)}
    ghi, dhi, tamb = provider_values(labels, tam_offset=tam_offset)
    rows = []
    for position in range(hours):
        local = datetime(year, 1, 1) + timedelta(hours=position)
        if local == drop:
            continue
        radiation = utc_start + timedelta(hours=position + 1)
        start = utc_start + timedelta(hours=position)
        mean = (tamb[index[start]] + tamb[index[radiation]]) / 2.0
        value = ghi[index[radiation]]
        if negative_at is not None and position == negative_at:
            value = -3.0
        rows.append((local, value, dhi[index[radiation]], mean))
    return rows


def _text(value):
    if isinstance(value, str):
        return value
    return str(int(value)) if float(value) == int(value) else f"{value:.6f}".rstrip("0").rstrip(".")


def weather_csv_text(year, rows, *, extra_metadata=(), units=None):
    units = units or {"GHI": "W/m2", "DHI": "W/m2", "Tamb": "deg.C"}
    lines = ["#Weather data hourly data", "#Site;Fixture Site", "#Country;CN",
             "#Data Source;Independent offline fixture", "#Time step;Hour", f"#Year;{year}",
             "#Latitude;39.9075", "#Longitude;116.39723", "#Altitude;47", f"#Time Zone;{OFFSET}"]
    lines.extend(extra_metadata)
    lines.append("Year;Month;Day;Hour;GHI;DHI;Tamb")
    lines.append(";;;;%s;%s;%s" % (units["GHI"], units["DHI"], units["Tamb"]))
    for stamp, ghi, dhi, tamb in rows:
        lines.append("%d;%d;%d;%d;%s;%s;%s" % (stamp.year, stamp.month, stamp.day, stamp.hour,
                                               _text(ghi), _text(dhi), _text(tamb)))
    return "\n".join(lines) + "\n"


def native_csv_text(rows, *, units=("W/m\u00b2", "W/m\u00b2", "\u00b0C"), shift_hours=0,
                    glob_delta=0.0, sit="FIXTURE.SIT", met="FIXTURE.MET", context=True):
    lines = ["PVSYST 8.1.6", ";File;File date;Description",
             "Project;FIXTURE.PRJ;01/01/24 00h00;offline fixture;"]
    if context:
        lines.append(f"Geographical Site;{sit};01/01/24 00h00;Fixture Site;CN;Asia;;")
        lines.append(f"Weather data;{met};01/01/24 00h00;Fixture Site;Custom file;Imported;")
    lines += ["", "Simulation:;Hourly values; from 01/01/24; to 31/12/24", "",
              "date;GlobInc;GlobHor;DiffHor;T_Amb",
              "    ;W/m\u00b2;%s;%s;%s" % (units[0], units[1], units[2])]
    for stamp, ghi, dhi, tamb in rows:
        label = (stamp + timedelta(hours=shift_hours)).strftime("%d/%m/%y %H:%M")
        lines.append("%s;0;%s;%s;%s" % (label, _text(ghi + glob_delta), _text(dhi), _text(tamb)))
    return "\n".join(lines) + "\n"


def acquisition_manifest(year, *, raw_name="fixture_raw.json", status="prepared",
                         timezone_name="Asia/Shanghai", method="endpoint_mean",
                         time_basis="local_standard_interval_start", manifest_year=None,
                         csv_name="fixture_weather.csv"):
    return {
        "status": status, "year": manifest_year if manifest_year is not None else year,
        "method": method, "time_basis": time_basis,
        "location": {"name": "Fixture Site", "country_code": "CN", "latitude": 39.9075,
                     "longitude": 116.39723, "timezone_name": timezone_name},
        "created_files": {"raw_json": {"name": raw_name}, "csv": {"name": csv_name}},
        "artifacts": {"csv": "/definitely/not/trusted/fixture.csv"},
    }


def weather_manifest(*, met_name="FIXTURE.MET", sit_name="FIXTURE.SIT", source_name="fixture_weather.csv",
                     status="needs_review", time_basis="local_standard_interval_start", year=2024):
    return {"status": status, "year": year, "time_basis": time_basis,
            "source": {"name": source_name},
            "created_files": {"met": {"name": met_name}, "sit": {"name": sit_name}}}


def met_text(hleg="False", *, site="Fixture Site", binary_after=True, var_meteo=True,
             end_of_data=True, unknown_object=False, date_deb="01/01/24 00:00"):
    text = ("PVObject_=pvMeteo\r\n  Comment=fixture\r\n  Version=8.1.6\r\n  Flags=$0143\r\n"
            "  IsSourceUt=False\r\n  HourShift=0\r\n  TimeShift=0\r\n  TimeShiftTotalMin=0\r\n"
            "  PasAccum=60\r\n  VarMeteoStepDuration=60\r\n  DateDebF=%s\r\n"
            "  PVObject_SiteMet=pvSite\r\n"
            "    Version=8.1.6\r\n    Flags=$37\r\n    Site=%s\r\n    TimeZone=8.0\r\n"
            "    HLeg=%s\r\n  End of PVObject pvSite\r\n" % (date_deb, site, hleg))
    if unknown_object:
        text += "  PVObject_SomeChild=pvOther\r\n    HourShift=7\r\n  End of PVObject pvOther\r\n"
    if var_meteo:
        text += "  VarMeteo, list of=1 TVarSimul\r\n    VarSimul_1=GlobHor\r\n      Day_0101=+0+GW+5zb\r\n"
    if end_of_data:
        text += "  End of Data\r\n"
    text += "  End of PVObject pvMeteo\r\n"
    if binary_after:
        text += "\x00\x01\x02binary-hour-array-not-read\r\n"
    return text


def sit_text(hleg="True"):
    return ("PVObject_=pvSite\r\n  Version=8.1.6\r\n  Site=Fixture Site\r\n"
            "  TimeZone=8.0\r\n  HLeg=%s\r\n  End of PVObject pvSite\r\n" % hleg)


class ReviewFixture(unittest.TestCase):
    year = 2024
    csv_name = "fixture_weather.csv"
    native_name = "fixture_native.csv"

    @classmethod
    def setUpClass(cls):
        cls._base_rows = csv_rows(cls.year)
        cls._base_csv = weather_csv_text(cls.year, cls._base_rows).encode("utf-8-sig")
        cls._base_native = native_csv_text(cls._base_rows).encode("utf-8-sig")
        cls._base_provider = json.dumps(provider_payload(cls.year)).encode("utf-8")
        cls._base_manifest = json.dumps(acquisition_manifest(cls.year)).encode("utf-8")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for folder in ("Meteo", "Results", "Sites"):
            (self.root / folder).mkdir()
        self.rows = self._base_rows
        self._reset_baseline()

    def _reset_baseline(self):
        (self.root / "Meteo" / self.csv_name).write_bytes(self._base_csv)
        (self.root / "Results" / self.native_name).write_bytes(self._base_native)
        (self.root / "Meteo" / "fixture_raw.json").write_bytes(self._base_provider)
        (self.root / "Results" / "fixture_acquisition.json").write_bytes(self._base_manifest)

    def write_csv(self, *, rows=None, extra_metadata=(), units=None):
        rows = self.rows if rows is None else rows
        (self.root / "Meteo" / self.csv_name).write_text(
            weather_csv_text(self.year, rows, extra_metadata=extra_metadata, units=units),
            encoding="utf-8-sig")

    def write_native(self, *, rows=None, units=("W/m\u00b2", "W/m\u00b2", "\u00b0C"),
                     shift_hours=0, glob_delta=0.0, sit="FIXTURE.SIT", met="FIXTURE.MET", context=True):
        rows = self.rows if rows is None else rows
        (self.root / "Results" / self.native_name).write_text(
            native_csv_text(rows, units=units, shift_hours=shift_hours, glob_delta=glob_delta,
                            sit=sit, met=met, context=context),
            encoding="utf-8-sig")

    def write_provider(self, *, payload=None, manifest=None):
        if payload is not None:
            (self.root / "Meteo" / "fixture_raw.json").write_text(json.dumps(payload), encoding="utf-8")
        if manifest is not None:
            (self.root / "Results" / "fixture_acquisition.json").write_text(json.dumps(manifest), encoding="utf-8")

    def review(self, **overrides):
        arguments = {"csv_name": self.csv_name, "native_csv_name": self.native_name, "year": self.year}
        arguments.update(overrides)
        return review_weather(self.root, **arguments)


class WeatherReviewTests(ReviewFixture):
    def test_value_inspector_complete_year_and_gap(self):
        report = inspect_weather_values(self.root / "Meteo" / self.csv_name, self.year)
        self.assertIs(report["valid"], True)
        self.assertEqual((report["rows"], report["missing_hours"], report["delimiter"]), (8784, 0, ";"))
        self.assertEqual((report["units"]["GHI"], report["metadata"]["#Time Zone"]), ("W/m2", "8"))
        self.assertAlmostEqual(report["months"][1]["GHI"]["observed_kwh_m2"], 23.2, places=6)
        self.assertAlmostEqual(sum(m["GHI"]["observed_kwh_m2"] for m in report["months"]), 366 * 0.8, places=6)

        self.write_csv(rows=csv_rows(self.year, drop=datetime(self.year, 6, 1, 12)))
        gapped = inspect_weather_values(self.root / "Meteo" / self.csv_name, self.year)
        self.assertIs(gapped["valid"], False)
        self.assertEqual(gapped["missing_hours"], 1)
        self.assertIn("2024-06-01 12:00:00", gapped["missing_examples"])

    def test_complete_year_trailing_csv_native_delta_and_compare_stats(self):
        report = self.review(acquisition_manifest_name="fixture_acquisition.json")
        self.assertEqual((report["status"], report["issues"]), ("needs_review", []))
        self.assertNotIn("scope", report)
        self.assertTrue((report["csv_to_native"]["timestamps_match"], report["provider_to_csv"]["matches"]) == (True, True))
        self.assertEqual(report["provider_to_csv"]["rows_compared"], year_hours(self.year))

        trailed = "".join(line + ";\n" for line in self._base_csv.decode("utf-8-sig").splitlines())
        (self.root / "Meteo" / self.csv_name).write_text(trailed, encoding="utf-8-sig")
        trailed_report = self.review(acquisition_manifest_name="fixture_acquisition.json")
        self.assertTrue(trailed_report["provider_to_csv"]["matches"])
        self.assertEqual(trailed_report["csv_to_native"]["rows"], year_hours(self.year))

        self._reset_baseline()
        self.write_native(glob_delta=0.5)
        delta_report = self.review()
        self.assertGreater(delta_report["csv_to_native"]["columns"]["GlobHor"]["outside_review_threshold"], 0)
        self.assertTrue(any("csv_to_native" in item for item in delta_report["review_items"]))

        source = self.root / "Meteo" / self.csv_name
        rep = inspect_weather_values(source, self.year)
        comp = compare_native_weather(self.root / "Results" / self.native_name, source, rep)
        self.assertTrue(comp["needs_review"])
        self.assertEqual(len(comp["monthly"]), 12)
        for col in comp["columns"].values():
            self.assertAlmostEqual(col["max_abs_delta"], col["max_abs"])
            self.assertAlmostEqual(col["mean_signed"], col["signed_delta_sum"] / col["samples"])

    def test_blocked_sources_units_and_arguments(self):
        for mutator, expected in (
            (lambda: self.write_native(shift_hours=1), "Native weather labels"),
            (lambda: self.write_native(units=("kW/m\u00b2", "W/m\u00b2", "\u00b0C")), "W/m2"),
            (lambda: self.write_native(units=("W/m\u00b2", "W/m\u00b2", "K")), "degrees C"),
            (lambda: self.write_csv(rows=csv_rows(self.year, negative_at=200)), "negative"),
            (lambda: self.write_csv(extra_metadata=("#Time Shift;15",)), "#Time Shift"),
        ):
            self._reset_baseline()
            mutator()
            rep = self.review()
            self.assertEqual(rep["status"], "blocked")
            self.assertTrue(any(expected in issue for issue in rep["issues"]), rep["issues"])

        for kwargs in ({"csv_name": "../escape.csv"}, {"csv_name": "escape.txt"}, {"native_csv_name": "absent.csv"},
                       {"year": True}, {"year": 2024.0}, {"year": 1900}, {"year": None}):
            self.assertEqual(self.review(**kwargs)["status"], "blocked")

    def test_provider_evidence_validation_and_mismatch(self):
        self.assertIsNone(self.review()["provider_to_csv"])

        self.write_provider(payload=provider_payload(self.year, null_labels=(datetime(self.year, 3, 1, 9),)))
        null_rep = self.review(acquisition_manifest_name="fixture_acquisition.json")
        self.assertGreater(null_rep["provider_to_csv"]["rows_without_a_provider_row"], 0)

        for change, msg in (
            (lambda: self.write_provider(payload=provider_payload(self.year, timezone="Europe/Paris", utc_offset=3600)), "GMT"),
            (lambda: self.write_provider(manifest=acquisition_manifest(self.year, manifest_year=self.year - 1)), "year"),
            (lambda: self.write_provider(manifest=acquisition_manifest(self.year, method="instantaneous")), "method"),
            (lambda: (self.root / "Meteo" / "fixture_raw.json").write_text("[1,2]"), "not a JSON object"),
        ):
            self._reset_baseline()
            change()
            rep = self.review(acquisition_manifest_name="fixture_acquisition.json")
            self.assertIsNone(rep["provider_to_csv"])
            self.assertTrue(any(msg in item for item in rep["review_items"]), rep["review_items"])

        self._reset_baseline()
        broken = list(self.rows)
        stamp, ghi, dhi, tamb = broken[5000]
        broken[5000] = (stamp, ghi + 40.0, dhi, tamb)
        self.write_csv(rows=broken)
        self.write_native(rows=broken)
        mismatch = self.review(acquisition_manifest_name="fixture_acquisition.json")
        self.assertFalse(mismatch["provider_to_csv"]["matches"])
        self.assertGreater(mismatch["provider_to_csv"]["columns"]["GHI"]["max_abs_delta"], 30.0)


class TimeBasisAndMetTests(ReviewFixture):
    def write_time_basis(self, *, met_hleg="False", sit_hleg="True", site="Fixture Site",
                         var_meteo=True, end_of_data=True, unknown_object=False,
                         date_deb="01/01/24 00:00", native_met="FIXTURE.MET", native_context=True):
        (self.root / "Meteo" / "FIXTURE.MET").write_text(
            met_text(met_hleg, site=site, var_meteo=var_meteo,
                     end_of_data=end_of_data, unknown_object=unknown_object, date_deb=date_deb),
            encoding="utf-8-sig")
        (self.root / "Sites" / "FIXTURE.SIT").write_text(sit_text(sit_hleg), encoding="utf-8-sig")
        (self.root / "Results" / "fixture_weather.json").write_text(json.dumps(weather_manifest()), encoding="utf-8")
        self.write_native(context=native_context, met=native_met)

    def test_time_basis_binding_header_markers_and_conflicts(self):
        self.assertIsNone(self.review()["time_basis"]["context_matches"])

        self.write_time_basis(site="\u5317\u4eac\u6d4b\u8bd5\u7ad9")
        rep = self.review(weather_manifest_name="fixture_weather.json")
        basis = rep["time_basis"]
        self.assertTrue((basis["reviewed"], basis["context_matches"]) == (True, True))
        self.assertEqual((basis["consumed_met"], basis["declared_source_year"]), ("FIXTURE.MET", 2024))
        self.assertEqual(basis["met_site_fields"]["Site"][0]["value"], "\u5317\u4eac\u6d4b\u8bd5\u7ad9")
        self.assertNotIn("HLeg", basis["met_fields"])
        self.assertNotIn("VarSimul_1", basis["met_fields"])
        self.assertTrue(any("HLeg" in item for item in rep["review_items"]))

        for kwargs, reason in (
            ({"var_meteo": False, "end_of_data": False}, "end of the pvMeteo object"),
            ({"var_meteo": False, "end_of_data": True}, "End of Data"),
            ({"var_meteo": False, "end_of_data": False, "unknown_object": True}, "unknown nested object"),
        ):
            self.write_time_basis(**kwargs)
            stop = self.review(weather_manifest_name="fixture_weather.json")["time_basis"]["met_header_read_stop"]
            self.assertIn(reason, stop["reason"])

        for kwargs, msg in (
            ({"native_met": "OTHER.MET"}, "does not name the manifest"),
            ({"native_context": False}, "does not name the manifest"),
            ({"date_deb": "01/01/19 00:00"}, "declares its own data year"),
        ):
            self.write_time_basis(**kwargs)
            conflicts = self.review(weather_manifest_name="fixture_weather.json")["time_basis"]["conflicts"]
            self.assertTrue(any(msg in item for item in conflicts), conflicts)

    def test_generated_met_declarations_and_mef_template(self):
        met = self.root / "Meteo" / "GEN.MET"
        met.write_text(met_text("True"), encoding="utf-8-sig")
        self.assertTrue(check_generated_met(met, time_zone=8)["valid"])
        met.write_text(met_text("True").replace("  HourShift=0\r\n", ""), encoding="utf-8-sig")
        self.assertTrue(check_generated_met(met, time_zone=8)["unchecked_checks"])

        bad_cases = (
            (met_text("False"), 8, "HLeg"),
            (met_text("True"), 9, "TimeZone"),
            (met_text("True").replace("  TimeShift=0\r\n", "  TimeShift=15\r\n"), 8, "TimeShift=15"),
            (met_text("True").replace("    HLeg=True\r\n", "    HLeg=True\r\n    HLeg=True\r\n"), 8, "more than once"),
            (met_text("True").replace("  TimeShift=0\r\n", "  TimeShift=0\r\n  TimeShift=0\r\n"), 8, "more than once"),
            (met_text("True").replace("  IsSourceUt=False\r\n", "  IsSourceUt=True\r\n"), 8, "IsSourceUt=True"),
            (met_text("True").replace("    HLeg=True\r\n", ""), 8, "no SiteMet HLeg"),
        )
        for text, tz, msg in bad_cases:
            met.write_text(text, encoding="utf-8-sig")
            res = check_generated_met(met, time_zone=tz)
            self.assertFalse(res["valid"])
            self.assertTrue(any(msg in issue for issue in res["issues"]), res["issues"])

        self.assertIn("RefTimeS=LegalTime", MEF_TEMPLATE)
        self.assertNotIn("SolarTime", MEF_TEMPLATE)


if __name__ == "__main__":
    unittest.main()

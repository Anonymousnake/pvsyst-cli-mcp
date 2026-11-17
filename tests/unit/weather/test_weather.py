"""Public weather-format validation with known hourly energy/coverage oracles."""
import calendar
import csv
import io
import json
import sys
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from mcp.server.mcpserver.exceptions import ToolError

from pvsyst_auto.native.cli import PVsystCLI, PVsystError, build_monthly_weather_csv
from pvsyst_auto.weather.preparation import load_weather_manifest, compare_native_weather
from pvsyst_auto.weather.csv import inspect_weather_csv
from pvsyst_auto.api import app as _registered_app, context as server
import pvsyst_auto.api.weather as api_weather


def weather_text(year=2023, separator=";", skip=None, diffuse=True, unit="W/m2", extra=None):
    """One complete hourly year; `extra` adds validated columns for profile counterexamples."""
    extra = list(extra or ())
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=separator, lineterminator="\n")
    writer.writerows([["#Weather data hourly data"], ["#Site", "Test site"], ["#Country", "Test"],
                      ["#Data Source", "Synthetic fixture"], ["#Time step", "Hour"],
                      ["#Latitude", 30], ["#Longitude", 110], ["#Altitude", 100], ["#Time Zone", 8]])
    headers = ["Year", "Month", "Day", "Hour", "GHI", "Tamb"] + (["DHI"] if diffuse else []) + extra
    units = ["", "", "", "", unit, "deg.C"] + ([unit] if diffuse else []) + ["m/s"] * len(extra)
    writer.writerow(headers)
    writer.writerow(units)
    start = datetime(year, 1, 1)
    for hour in range(8784 if calendar.isleap(year) else 8760):
        if hour == skip:
            continue
        stamp = start + timedelta(hours=hour)
        power = 100 if 6 <= stamp.hour < 18 else 0
        factor = 1.0 if unit == "W/m2" else 0.0036
        writer.writerow([stamp.year, stamp.month, stamp.day, stamp.hour, power * factor, 20] +
                        ([power / 2 * factor] if diffuse else []) + [2] * len(extra))
    return stream.getvalue()


def native_weather_commands(site="Test site", country="China", conversion="Conversion ended successfully...",
                            log=None):
    """Fake create-site/convert-meteo boundary: builds the SIT from the monthly CSV it is given.

    Coordinates, altitude and timezone mirror the fixture metadata in `weather_text`, so a
    generated SIT only passes `check_site` for sources built by that helper.
    """
    def run(*args):
        def argument(flag):
            return next(value[len(flag) + 2:] for value in args if value.startswith("-" + flag + ":"))

        if log is not None:
            log.append(list(args))
        if args[0] == "create-site":
            with Path(argument("wdf")).open(newline="") as stream:
                rows = list(csv.reader(stream, delimiter=";"))
            series = list(zip(*[[float(value) for value in row] for row in rows[2:]]))
            monthly = [field + "=" + ",".join(map(str, (*values, sum(values)))) + ","
                       for field, values in zip(("GlobalH", "TAmb", "DiffuseH"), series)]
            target = Path(argument("osf"))
            target.write_text(
                "PVObject_=pvSite\nNomF=" + target.name + "\nSite=" + site + "\nCountry=" + country + "\n"
                "Latitude=30\nLongitude=110\nAltitude=100\nTimeZone=8\nHLeg=True\n"
                + "\n".join(monthly) + "\nEnd of PVObject pvSite\n", encoding="utf-8-sig")
            return subprocess.CompletedProcess([], 0, "Site created", "")
        Path(argument("omf")).write_bytes(FAKE_MET_HEAD)
        return subprocess.CompletedProcess([], 0, conversion, "")
    return run


# A generated .MET starts with its ASCII owning fields; the prepare guard reads only these,
# so the fake native runner must return this shape rather than an opaque placeholder.
FAKE_MET_HEAD = (
    "PVObject_=pvMeteo\r\n  Version=8.1.6\r\n  Flags=$0143\r\n  IsSourceUt=False\r\n"
    "  HourShift=0\r\n  TimeShift=0\r\n  TimeShiftTotalMin=0\r\n  PasAccum=60\r\n"
    "  VarMeteoStepDuration=60\r\n  PVObject_SiteMet=pvSite\r\n    TimeZone=8.0\r\n"
    "    HLeg=True\r\n  End of PVObject pvSite\r\n  End of PVObject pvMeteo\r\n"
).encode("utf-8")


class WeatherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "Meteo").mkdir()
        self.path = self.root / "Meteo" / "weather.csv"

    def inspect(self, value, expected_year=None):
        self.path.write_text(value, encoding="utf-8-sig", newline="")
        before = self.path.read_bytes()
        report = inspect_weather_csv(self.path, expected_year)
        self.assertEqual(before, self.path.read_bytes())
        json.dumps(report, allow_nan=False)
        return report

    def test_normal_and_leap_year_energy_and_hash(self):
        for year, delimiter, expected in ((2023, ";", 8760), (2024, ",", 8784)):
            report = self.inspect(weather_text(year, delimiter))
            self.assertTrue(report["valid"])
            self.assertTrue(report["raw_hourly_coverage_complete"])
            self.assertEqual(report["rows"], expected)
            self.assertEqual(len(report["sha256"]), 64)
            self.assertAlmostEqual(report["months"][0]["GHI"]["observed_kwh_m2"], 31 * 12 * 0.1)
            self.assertEqual(report["months"][1]["Tamb"]["finite_samples"], calendar.monthrange(year, 2)[1] * 24)
            self.assertEqual(report["months"][0]["Tamb"]["observed_mean_degC"], 20)

    def test_mj_energy_conversion_matches_hourly_watts(self):
        a = self.inspect(weather_text())
        b = self.inspect(weather_text(unit="MJ/m2"))
        self.assertTrue(b["valid"])
        self.assertEqual(a["months"], b["months"])

    def test_missing_night_or_leap_day_is_not_complete(self):
        for year, skip in ((2023, 0), (2024, 24 * 59)):
            r = self.inspect(weather_text(year=year, skip=skip))
            self.assertFalse(r["valid"])
            self.assertEqual(r["missing_hours"], 1)
            self.assertEqual(len(r["missing_examples"]), 1)

    def test_duplicate_reversed_and_wrong_year_are_diagnosed(self):
        source = weather_text()
        lines = source.splitlines()
        r = self.inspect(source + lines[-1] + "\n")
        self.assertEqual(r["duplicate_timestamps"], 1)
        self.assertFalse(r["valid"])
        r = self.inspect("\n".join(lines[:-2] + lines[-2:][::-1]) + "\n")
        self.assertEqual(r["nonincreasing_timestamps"], 1)
        self.assertFalse(r["raw_hourly_coverage_complete"])
        r = self.inspect(source, 2024)
        self.assertEqual((r["missing_hours"], r["unexpected_timestamps"]), (8784, 8760))

    def test_missing_nonnumeric_and_physical_errors_are_not_zeros(self):
        source = weather_text()
        for value in ("", "NaN", "Infinity", "1e999", "-1", "true"):
            with self.subTest(value=value):
                r = self.inspect(source.replace("2023;1;1;6;100.0", "2023;1;1;6;" + value))
                self.assertFalse(r["valid"])
                self.assertIn("GHI", " ".join(r["issues"]))
                self.assertEqual(r["months"][0]["GHI"]["finite_samples"], 743)
        r = self.inspect(source.replace("100.0;20;50.0", "100.0;20;101.0", 1))
        self.assertIn("DHI exceeds GHI", " ".join(r["issues"]))

    def test_metadata_missing_duplicates_ranges_and_identifier(self):
        source = weather_text()
        for text in (source.replace("#Site;Test site\n", ""),
                     source.replace("#Latitude;30", "#Latitude;91"),
                     source.replace("#Time Zone;8", "#Time Zone;8\n#Time Zone;0"),
                     source.replace("#Site;", "#Year;0\n#Site;"),
                     source.replace("#Weather data hourly data", "#unknown"),
                     "\n" + source):
            self.assertFalse(self.inspect(text)["valid"])

    def test_metadata_year_and_reordered_columns_with_optional_weather(self):
        original = list(csv.reader(io.StringIO(weather_text()), delimiter=";"))
        output = io.StringIO()
        writer = csv.writer(output, delimiter=",", lineterminator="\n")
        header_index = next(i for i, row in enumerate(original) if row[0] == "Year")
        writer.writerows(original[:header_index])
        writer.writerow(["#Year", "2023"])
        writer.writerow(["Hour", "Day", "Month", "Tamb", "GHI", "DHI", "WindVel", "RH"])
        writer.writerow(["", "", "", "deg.C", "W/m2", "W/m2", "m/s", "%"])
        writer.writerows([[row[3], row[2], row[1], row[5], row[4], row[6], 2, 50]
                          for row in original[header_index + 2:]])
        r = self.inspect(output.getvalue())
        self.assertTrue(r["valid"])
        self.assertEqual(r["expected_year"], 2023)
        r = self.inspect(output.getvalue().replace(",2,50\n", ",2,101\n"))
        self.assertFalse(r["valid"])
        self.assertEqual(r["issue_count"], 8760)
        self.assertEqual(len(r["issues"]), 50)
        self.assertTrue(r["issues_truncated"])

    def test_unknown_units_do_not_produce_mislabeled_temperature_or_energy(self):
        source = weather_text()
        r = self.inspect(source.replace("W/m2;deg.C;W/m2", "kWh/m2;degF;W/m2"))
        self.assertFalse(r["valid"])
        self.assertIsNone(r["months"][0]["GHI"]["observed_kwh_m2"])
        self.assertIsNone(r["months"][0]["Tamb"]["observed_mean_degC"])
        r = self.inspect(source.replace("#Time step;Hour", "#Time step;Day"))
        self.assertFalse(r["valid"])
        self.assertIsNone(r["months"][0]["GHI"]["observed_kwh_m2"])

    def test_explicit_time_shift_and_reference_are_unchecked(self):
        source = weather_text()
        for tag in ("#Hour Shift;1", "#Time Shift;30", "#Time reference;UT"):
            r = self.inspect(source.replace("#Site;", tag + "\n#Site;"))
            self.assertIsNone(r["valid"])
            self.assertTrue(r["raw_hourly_coverage_complete"])
            self.assertTrue(r["unchecked_checks"])

    def test_off_hour_labels_and_invalid_dates_are_not_normalized(self):
        source = weather_text()
        shifted = source.replace("Year;Month;Day;Hour;", "Year;Month;Day;Hour;Minute;")
        shifted = shifted.replace(";;;;W/m2", ";;;;;W/m2")
        shifted = "\n".join(line.replace(";100.0;20", ";30;100.0;20").replace(";0.0;20", ";30;0.0;20")
                            if line.startswith("2023;") else line for line in shifted.splitlines())
        r = self.inspect(shifted)
        self.assertEqual(r["nonzero_minute_records"], 8760)
        self.assertFalse(r["raw_hourly_coverage_complete"])
        r = self.inspect(source.replace("2023;1;1;0", "2023;2;29;0", 1))
        self.assertEqual(r["invalid_dates"], 1)

    def test_incomplete_poa_and_missing_diffuse_are_explicit(self):
        r = self.inspect(weather_text().replace("GHI", "GPI"))
        self.assertIsNone(r["valid"])
        self.assertIn("POA", " ".join(r["unchecked_checks"]))
        r = self.inspect(weather_text(diffuse=False))
        self.assertTrue(r["valid"])
        self.assertIn("diffuse", " ".join(r["warnings"]))

    def test_change_during_inspection_and_large_input_fail(self):
        import pvsyst_auto.weather.csv as pvsyst_weather
        self.path.write_text(weather_text(), encoding="utf-8")
        raw = self.path.read_bytes()
        with patch.object(pvsyst_weather, "read_bytes", side_effect=[raw, raw + b"\n"]):
            with self.assertRaisesRegex(ValueError, "changed during"):
                inspect_weather_csv(self.path)
        with patch.object(pvsyst_weather, "MAX_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "reader limit"):
                inspect_weather_csv(self.path)

    def test_prepare_weather_contract_provenance_review_and_failure(self):
        self.path.write_text(weather_text(year=2020).replace('#Country;Test', '#Country;China'), encoding='utf-8-sig')
        raw = self.path.read_bytes()
        for name, text in (('gap.csv', weather_text(year=2020, skip=0)),
                           ('extra.csv', weather_text(year=2020, extra=['WindVel'])),
                           ('nodhi.csv', weather_text(year=2020, diffuse=False))):
            (self.root / 'Meteo' / name).write_text(text, encoding='utf-8-sig')
        untouched = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        cli = PVsystCLI(sys.executable, self.root)
        cli._help_cache['create-site'] = '\n'.join('--' + name + ':' for name in ('site-name', 'altitude', 'timezone', 'country-code'))
        cli._help_cache['convert-meteo'] = '--input-csv-file'
        request = dict(csv_name='weather.csv', year=2020, sit_name='New.SIT', met_name='New.MET',
                       country_code='CN', time_basis='local_standard_interval_start')
        def native(*args):
            def arg(flag):
                return next(value[len(flag) + 2:] for value in args if value.startswith('-' + flag + ':'))
            if args[0] == 'create-site':
                self.assertEqual((arg('tz'), arg('alt'), arg('cc')), ('8.0', '100.0', 'CN'))
                with Path(arg('wdf')).open(newline='') as stream:
                    rows = list(csv.reader(stream, delimiter=';'))
                series = list(zip(*[[float(value) for value in row] for row in rows[2:]]))
                monthly = []
                for field, values in zip(('GlobalH', 'TAmb', 'DiffuseH'), series):
                    monthly.append(field + '=' + ','.join(map(str, (*values, sum(values)))) + ',')
                Path(arg('osf')).write_text('PVObject_=pvSite\nSite=Test site\nCountry=China\n'
                    'Latitude=30\nLongitude=110\nAltitude=100\nTimeZone=8\nHLeg=True\n'
                    + '\n'.join(monthly) + '\nEnd of PVObject pvSite\n', encoding='utf-8-sig')
                return subprocess.CompletedProcess([], 0, 'Site created', '')
            self.assertEqual(args[0], 'convert-meteo')
            self.assertEqual(arg('imt'), '0')
            Path(arg('omf')).write_bytes(FAKE_MET_HEAD)
            return subprocess.CompletedProcess([], 0, 'The imported weather data are shifted by 25 minutes\nConversion ended successfully...', '')
        with patch.object(server, '_cli', cli):
            preview = api_weather.pvsyst_prepare_weather(**request)
            self.assertEqual(preview['status'], 'ready', preview)
            # 只读预检不得写入任何文件
            self.assertEqual(untouched, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
            for changes in ({'time_basis': 'UTC'}, {'year': 2021}, {'country_code': ''}, {'csv_name': '../weather.csv'},
                            {'csv_name': 'gap.csv'}, {'csv_name': 'extra.csv'}, {'csv_name': 'nodhi.csv'}):
                blocked = api_weather.pvsyst_prepare_weather(**{**request, **changes})
                self.assertEqual(blocked['status'], 'blocked', blocked)
                self.assertTrue(blocked['issues'], blocked)
                self.assertEqual(untouched, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
            with patch.object(cli, '_run', side_effect=native):
                prepared = api_weather.pvsyst_prepare_weather(**request, dry_run=False)
            self.assertEqual(prepared['status'], 'needs_review', prepared)
            self.assertFalse(prepared['native_met_verified'])
            self.assertTrue(prepared['source_preserved'])
            self.assertEqual(self.path.read_bytes(), raw)
            self.assertEqual(json.loads(Path(prepared['artifacts']['manifest']).read_text()), prepared)
            assets, report, warnings = load_weather_manifest(self.root, 'New_weather.json', 2020, 'New.SIT', 'New.MET')
            self.assertEqual(report['rows'], 8784)
            self.assertIn('25 minutes', warnings[0])
            with Path(assets['weather_normalized_csv']['path']).open(newline='') as stream:
                normalized = list(csv.reader(stream))
            self.assertEqual((len(normalized), normalized[1][0], normalized[-1][0]), (8785, '2020-01-01 00:00', '2020-12-31 23:00'))
            # Public native export comparison, not an assertion about MET internals.
            output = self.root / 'Results/weather-output.csv'
            with output.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.writer(stream, delimiter=';')
                writer.writerow(['date', 'GlobHor', 'DiffHor', 'T_Amb'])
                writer.writerow(['', 'W/m2', 'W/m2', 'degC'])
                for date, ghi, dhi, temp in normalized[1:]:
                    writer.writerow([datetime.strptime(date, '%Y-%m-%d %H:%M').strftime('%d/%m/%y %H:%M'), ghi, dhi, temp])
            comparison = compare_native_weather(output, self.path, report)
            self.assertFalse(comparison['needs_review'])
            self.assertEqual(comparison['rows'], 8784)
            before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            self.assertEqual(api_weather.pvsyst_prepare_weather(**request, dry_run=False)['status'], 'blocked')
            self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
            self.path.write_bytes(raw + b'\n')
            with self.assertRaisesRegex(ValueError, 'changed'):
                load_weather_manifest(self.root, 'New_weather.json', 2020, 'New.SIT', 'New.MET')
            self.path.write_bytes(raw)
            with patch.object(cli, '_run', side_effect=PVsystError('Native site failure')):
                failed = api_weather.pvsyst_prepare_weather(**{**request, 'sit_name': 'Fail.SIT', 'met_name': 'Fail.MET'}, dry_run=False)
            self.assertEqual((failed['status'], failed['stage']), ('failed', 'site'))
            self.assertTrue(failed['source_preserved'])
            self.assertIn('normalized_csv', failed['created_files'])
            self.assertEqual(json.loads(Path(failed['artifacts']['manifest']).read_text()), failed)

    def test_leap_year_mj_units_import_equivalence_and_review_inheritance(self):
        cli = PVsystCLI(sys.executable, self.root)
        cli._help_cache['create-site'] = '\n'.join(
            '--' + name + ':' for name in ('site-name', 'altitude', 'timezone', 'country-code'))
        cli._help_cache['convert-meteo'] = '--input-csv-file'
        for name, unit in (('watts.csv', 'W/m2'), ('joules.csv', 'MJ/m2')):
            (self.root / 'Meteo' / name).write_text(
                weather_text(year=2020, unit=unit).replace('#Country;Test', '#Country;China'),
                encoding='utf-8-sig')
        request = dict(year=2020, country_code='CN', time_basis='local_standard_interval_start', dry_run=False)
        native = native_weather_commands(
            conversion='The imported weather data are shifted by 25 minutes\nConversion ended successfully...')
        with patch.object(server, '_cli', cli), patch.object(cli, '_run', side_effect=native):
            watts = api_weather.pvsyst_prepare_weather(csv_name='watts.csv', sit_name='W.SIT', met_name='W.MET', **request)
            joules = api_weather.pvsyst_prepare_weather(csv_name='joules.csv', sit_name='J.SIT', met_name='J.MET', **request)
        for prepared in (watts, joules):
            self.assertEqual(prepared['status'], 'needs_review', prepared)
            self.assertTrue(prepared['source_preserved'])

        def rows(prepared):
            with Path(prepared['artifacts']['normalized_csv']).open(newline='') as stream:
                return list(csv.reader(stream))

        watt_rows, joule_rows = rows(watts), rows(joules)
        self.assertEqual((len(watt_rows), watt_rows[0], watt_rows[1][0], watt_rows[-1][0]),
                         (8785, ['Date', 'GHI', 'DHI', 'Tamb'], '2020-01-01 00:00', '2020-12-31 23:00'))
        self.assertEqual(len(joule_rows), len(watt_rows))
        for index, (left, right) in enumerate(zip(watt_rows[1:], joule_rows[1:])):
            self.assertEqual((left[0], len(left)), (right[0], len(right)))
            for watt, joule in zip(left[1:], right[1:]):
                self.assertAlmostEqual(float(watt), float(joule), places=9, msg=f'row {index}')
        manifest = Path(watts['artifacts']['manifest'])
        self.assertEqual(manifest.name, 'W_weather.json')
        data = json.loads(manifest.read_text())
        self.assertEqual((data['status'], len(data['conversion']['warnings'])), ('needs_review', 1))
        self.assertIn('shifted by 25 minutes', data['conversion']['diagnostics'])
        # 清空 conversion.warnings 后仍须继承清单的 needs_review 状态
        data['conversion']['warnings'] = []
        manifest.write_text(json.dumps(data), encoding='utf-8')
        assets, report, warnings = load_weather_manifest(self.root, 'W_weather.json', 2020, 'W.SIT', 'W.MET')
        self.assertEqual(warnings, ['Weather preparation remains needs_review; original review state is retained'])
        self.assertEqual(report['rows'], 8784)
        self.assertTrue(assets['weather_source']['path'].endswith('watts.csv'))

    def test_prepare_weather_artifact_hash_error_retains_failed_manifest(self):
        import pvsyst_auto.weather.preparation as pvsyst_weather_flow
        cli = PVsystCLI(sys.executable, self.root)
        cli._help_cache['create-site'] = '\n'.join(
            '--' + name + ':' for name in ('site-name', 'altitude', 'timezone', 'country-code'))
        cli._help_cache['convert-meteo'] = '--input-csv-file'
        self.path.write_text(weather_text(year=2020).replace('#Country;Test', '#Country;China'), encoding='utf-8-sig')
        raw = self.path.read_bytes()
        real = pvsyst_weather_flow.fingerprint

        def unreadable(path):
            if Path(path).suffix.lower() == '.mef':
                raise OSError('fixture: MEF unreadable at hash collection')
            return real(path)

        with patch.object(server, '_cli', cli), patch.object(cli, '_run', side_effect=native_weather_commands()), \
                patch.object(pvsyst_weather_flow, 'fingerprint', side_effect=unreadable):
            failed = api_weather.pvsyst_prepare_weather(csv_name='weather.csv', year=2020, sit_name='K.SIT',
                                                   met_name='K.MET', country_code='CN',
                                                   time_basis='local_standard_interval_start', dry_run=False)
        self.assertEqual(failed['status'], 'failed', failed)
        self.assertEqual(failed['created_files']['mef'],
                         {'name': 'K.MEF', 'hash_error': 'fixture: MEF unreadable at hash collection'})
        self.assertIn('Artifact hash collection failed: mef', failed['issues'])
        self.assertTrue(failed['source_preserved'])
        self.assertTrue((self.root / 'Meteo/K.MET').is_file())
        self.assertEqual(self.path.read_bytes(), raw)
        # 收尾失败仍保留已生成文件与原始 failed 清单
        self.assertEqual(json.loads(Path(failed['artifacts']['manifest']).read_text()), failed)

    def test_native_weather_details_and_diagnostics_contracts(self):
        """Public native boundary the workflow relies on: details dicts and redacted diagnostics."""
        cli = PVsystCLI(sys.executable, self.root)
        cli._help_cache['create-site'] = '\n'.join(
            '--' + name + ':' for name in ('site-name', 'altitude', 'timezone', 'country-code'))
        cli._help_cache['convert-meteo'] = '--input-csv-file'
        source = self.root / 'Meteo/source.csv'
        source.write_text(weather_text(year=2020), encoding='utf-8-sig')
        (self.root / 'Sites').mkdir(exist_ok=True)
        monthly = build_monthly_weather_csv(self.root / 'Meteo/monthly.csv', [120.0] * 12, [20.0] * 12, [60.0] * 12)
        mef = self.root / 'Meteo/source.MEF'
        mef.write_bytes(b'\xef\xbb\xbfPVObject_=pvMeteoFormat\nEnd of PVObject pvMeteoFormat\n')
        sit = self.root / 'Sites/source.SIT'
        sit.write_bytes(b'PVObject_=pvSite\nEnd of PVObject pvSite\n')
        conversion = ('The imported weather data are shifted by 25 minutes\nConversion ended successfully...')
        log = []
        with patch.object(cli, '_run', side_effect=native_weather_commands(
                site='Test site', country='Test', conversion=conversion, log=log)):
            created = cli.create_site_details('Test site', 30, 110, monthly, self.root / 'Sites/new.SIT',
                                              altitude=100, timezone=8, country_code='TT',
                                              source='Synthetic fixture', log_level=3)
            converted = cli.convert_meteo_details(source, mef, sit, self.root / 'Meteo/new.MET',
                                                  timeshift=0, log_level=3)
            wrapped = cli.create_site('Test site', 30, 110, monthly, self.root / 'Sites/plain.SIT',
                                      country_code='TT')
        self.assertEqual([entry[0] for entry in log], ['create-site', 'convert-meteo', 'create-site'])
        self.assertIn('-cc:TT', log[0])
        self.assertIn('-imt:0', log[1])
        self.assertEqual(created['sit'], str(self.root / 'Sites/new.SIT'))
        self.assertEqual(created['warnings'], [])
        self.assertIn('Site created', created['diagnostics'])
        self.assertEqual(converted['met'], str(self.root / 'Meteo/new.MET'))
        self.assertEqual(converted['warnings'], ['The imported weather data are shifted by 25 minutes'])
        self.assertIn('Conversion ended successfully', converted['diagnostics'])
        self.assertEqual(wrapped, Path(self.root / 'Sites/plain.SIT'))
        rejection = subprocess.CompletedProcess(
            [], 3, 'Error: synthetic native rejection\nHost ID: fixture-secret', '')
        with patch.object(cli, '_run', return_value=rejection):
            for call in (lambda: cli.convert_meteo_details(source, mef, sit, self.root / 'Meteo/rejected.MET',
                                                          timeshift=0, log_level=3),
                         lambda: cli.create_site_details('Test site', 30, 110, monthly,
                                                         self.root / 'Sites/rejected.SIT', country_code='TT')):
                with self.assertRaises(PVsystError) as caught:
                    call()
                self.assertIsInstance(caught.exception.diagnostics, str)
                self.assertIn('synthetic native rejection', caught.exception.diagnostics)
                self.assertIn('[redacted]', caught.exception.diagnostics)
                self.assertNotIn('fixture-secret', str(caught.exception) + caught.exception.diagnostics)
        self.assertFalse((self.root / 'Meteo/rejected.MET').exists())
        self.assertFalse((self.root / 'Sites/rejected.SIT').exists())

    def test_mcp_paths_and_expected_year(self):
        self.path.write_text(weather_text(), encoding="utf-8")
        with patch.object(server, "_cli", PVsystCLI(sys.executable, self.root)):
            self.assertTrue(api_weather.pvsyst_inspect_weather_csv("weather.csv", 2023)["valid"])
            for name in ("../weather.csv", "weather.MET", "C:\\outside.csv"):
                with self.assertRaisesRegex(ToolError, "filename"):
                    api_weather.pvsyst_inspect_weather_csv(name)
        for year in (True, 0, 9999, "2023", 2023.0):
            with self.assertRaises(ValueError):
                inspect_weather_csv(self.path, year)


if __name__ == "__main__":
    unittest.main()

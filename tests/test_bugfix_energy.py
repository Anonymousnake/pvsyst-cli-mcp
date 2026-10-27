"""Regressions for verified energy-module defects: atomic edits, anchors, text insertion, numbers."""
import calendar
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pvsyst_auto.components.editor import ObjectBlock
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.energy import ac, auxiliaries, iam, inverter_temperature, soiling
from pvsyst_auto.energy.load import create_load_variant
from pvsyst_auto.energy.profiles import prepare_load_profile
from pvsyst_auto.energy.storage import create_grid_storage_variant
from pvsyst_auto.energy.system import create_grid_storage_system
from pvsyst_auto.energy.unavailability import system_unavail_state
from pvsyst_auto.project.variants import VariantStore


def crlf(text):
    return text.replace("\n", "\r\n").splitlines(keepends=True)


def drifting_reader(module, change):
    """Delegate to the real reader, but report `change` merged into every re-read state."""
    real, calls = module._read, []

    def read(lines, bounds):
        result = real(lines, bounds)
        calls.append(bounds)
        return result if len(calls) == 1 else (*result[:-1], {**result[-1], **change})
    return read


MAIN = """PVObject_PVMainArray=pvMainArray
  Flags=$00100400
{soiling}PVObject_SystemCircuit=pvCircuit
End of PVObject pvCircuit
End of PVObject pvMainArray
"""
NONE_SOILING = ("Soiling=Soiling\n  IsYearly=False\n  IsMonthly=False\n  IsFromFile=False\n"
                "  YearlyValue=0.020\nEndTags\n")
CACHED_SOILING = ("Soiling=Soiling\n  IsYearly=True\n  IsMonthly=False\n  IsFromFile=False\n"
                  "  YearlyValue=0.025\n  MonthlyValue=" + "0.010," * 12 + "\nEndTags\n")


class SoilingTests(unittest.TestCase):
    def main(self, block=""):
        lines = crlf(MAIN.format(soiling=block))
        return lines, (0, len(lines) - 1)

    def test_failed_candidate_read_leaves_lines_untouched(self):
        lines, bounds = self.main()
        original = list(lines)
        real, calls = soiling._profile, []

        def flaky(lines, bounds):
            calls.append(bounds)
            if len(calls) > 1:
                raise ValueError("simulated candidate read failure")
            return real(lines, bounds)
        with mock.patch.object(soiling, "_profile", flaky):
            with self.assertRaisesRegex(ValueError, "simulated"):
                soiling.replace_soiling(lines, bounds, {"mode": "constant", "yearly_loss_pct": 2.5})
        self.assertEqual(lines, original)

    def test_same_value_requests_keep_the_stored_block(self):
        for block, updates in ((NONE_SOILING, {"mode": "none"}),
                               (CACHED_SOILING, {"mode": "constant", "yearly_loss_pct": 2.5})):
            with self.subTest(updates=updates):
                lines, bounds = self.main(block)
                original = list(lines)
                change = soiling.replace_soiling(lines, bounds, updates)
                self.assertEqual(lines, original)
                self.assertEqual(change["before"], change["after"])

    def test_change_is_written_and_verified(self):
        lines, bounds = self.main()
        change = soiling.replace_soiling(lines, bounds, {"mode": "constant", "yearly_loss_pct": 2.5})
        self.assertEqual(change["after"], {"mode": "constant", "yearly_loss_pct": 2.5})
        self.assertEqual(lines[2:8], crlf("Soiling=Soiling\n  IsYearly=True\n  IsMonthly=False\n"
                                          "  IsFromFile=False\n  YearlyValue=0.025\nEndTags\n"))


SYSTEM = """PVObject_=pvVCalcul
  Version=8.1.6
  PVObject_System=pvSystem
    Flags=$00
    SystemType=Grid
  End of PVObject pvSystem
End of PVObject pvVCalcul
"""


class SystemEditorTests(unittest.TestCase):
    """Auxiliary and inverter-temperature edits re-read a candidate before committing it."""
    CASES = ((auxiliaries, auxiliaries.replace_auxiliaries,
              {"mode": "constant", "operating_kw": 5, "night_kw": 2},
              ["    AuxPower_Const=5000.000\r\n", "    AuxPower_Night=2000.000\r\n"], "$00040000"),
             (inverter_temperature, inverter_temperature.replace_inverter_temperature,
              {"mode": "offset", "offset_c": 2.5, "fixed_c": 30},
              ["    TempShift=2.5\r\n", "    TempFixed=30.0\r\n"], "$4000"))

    def test_candidate_that_reads_back_differently_is_not_committed(self):
        for module, replace, updates, _, _ in self.CASES:
            with self.subTest(module=module.__name__):
                lines = crlf(SYSTEM)
                original = list(lines)
                with mock.patch.object(module, "_read", drifting_reader(module, {"mode": "drifted"})):
                    with self.assertRaisesRegex(ValueError, "differs from the request"):
                        replace(lines, (2, 5), updates)
                self.assertEqual(lines, original)

    def test_verified_candidate_is_committed(self):
        for module, replace, updates, written, flags in self.CASES:
            with self.subTest(module=module.__name__):
                lines = crlf(SYSTEM)
                change = replace(lines, (2, 5), updates)
                self.assertEqual(lines[3], "    Flags=" + flags + "\r\n")
                self.assertEqual(lines[5:7], written)
                self.assertEqual(change["after"]["mode"], updates["mode"])


SUBARRAY = """PVObject_=pvSubArray
  Version=8.1.6
  Flags2={flags2}
{iam}End of PVObject pvSubArray
"""
ASHRAE = ("  PVObject_IAM=pvIAM\n    Flags=$00\n    IAMMode=Ashrae\n    B0_IAM=0.050\n"
          "  End of PVObject pvIAM\n")


class IamAnchorTests(unittest.TestCase):
    """No WiringResistance sibling: only inserting a missing Ashrae block needs that anchor."""
    def subarray(self, flags2, block):
        lines = crlf(SUBARRAY.format(flags2=flags2, iam=block))
        return lines, (0, len(lines) - 1)

    def test_component_switch_keeps_the_retained_block(self):
        lines, bounds = self.subarray("$00", ASHRAE)
        change = iam.replace_iam_model(lines, bounds, {"mode": "component"})
        self.assertEqual(change["after"], {"effective": "component", "stored_model": "Ashrae",
                                           "stored_ashrae_b0": 0.05})
        self.assertEqual(lines[2], "  Flags2=$20\r\n")
        self.assertEqual(lines[3:8], crlf(ASHRAE))

    def test_existing_ashrae_block_is_edited_in_place(self):
        lines, bounds = self.subarray("$00", ASHRAE)
        change = iam.replace_iam_model(lines, bounds, {"mode": "ashrae", "ashrae_b0": 0.06})
        self.assertEqual(change["after"]["stored_ashrae_b0"], 0.06)
        self.assertEqual(lines[6], "    B0_IAM=0.060\r\n")

    def test_inserting_a_missing_block_still_requires_the_anchor(self):
        lines, bounds = self.subarray("$20", "")
        original = list(lines)
        with self.assertRaisesRegex(ValueError, "WiringResistance"):
            iam.replace_iam_model(lines, bounds, {"mode": "ashrae", "ashrae_b0": 0.05})
        self.assertEqual(lines, original)


class UnavailabilityTests(unittest.TestCase):
    def state(self, period_lines, count):
        lines = crlf("PVObject_System=pvSystem\n  SystemUnavail=TUnavailability\n    DefMode=Random\n"
                     f"    NbPeriods={count}\n{period_lines}  End of PVObject pvUnavail\nEnd of PVObject pvSystem\n")
        return system_unavail_state(lines, (0, len(lines) - 1))

    def test_non_ascii_digit_period_count_is_reported_not_raised(self):
        state = self.state("    DateBeg=01/01/90 00:00\n    Duration=24\n", "²")
        self.assertIn("NbPeriods does not match the stored period list", state["issues"])

    def test_period_without_duration_is_an_issue(self):
        complete = self.state("    DateBeg=01/01/90 00:00\n    Duration=24\n", 1)
        self.assertNotIn("issues", complete)
        state = self.state("    DateBeg=01/01/90 00:00\n    DateBeg=02/01/90 00:00\n    Duration=24\n", 2)
        self.assertEqual([period["duration"] for period in state["periods"]], [None, "24"])
        self.assertEqual(state["issues"], ["1 SystemUnavail period(s) lack a Duration within three lines of DateBeg"])


class AcMessageTests(unittest.TestCase):
    def test_messages_keep_their_spaces_and_flag_syntax(self):
        with self.assertRaisesRegex(ValueError, r"at most 100000 m and in exact 0\.1 m steps"):
            ac._length(0)
        main = ObjectBlock("PVObject_PVMainArray", 1, "pvMainArray", {"MVNbTransfos": [(0, "0")]})
        circuit = ObjectBlock("PVObject_PVMainArray/PVObject_SystemCircuit", 2, "pvCircuit",
                              {"Version": [(0, "8.1.6")], "Flags": [(1, "$01")]})
        store = SimpleNamespace(_sections=lambda lines: ({1: (0, 1)}, (2, 3)),
                                _orientation=lambda lines, number: (4, 5),
                                _ordinary_fixed_plane=lambda lines, bounds: None)
        with mock.patch.object(ac, "_require_scope"), \
                mock.patch.object(ac, "_layout", return_value=({}, main, None, [], [], [], circuit)):
            with self.assertRaisesRegex(ValueError, r"^Requires native 8\.1\.6 circuit Flags=\$00$"):
                ac._profile(store, [])


ROOT = "PVObject_=pvVCalcul\n  Comment=Fixture\n  Version=8.1.6\n  Flags={root_flags}\n"
ARRAY = "  PVObject_=pvSubArray\n    SubArrayId=1\n    NModSerie=10\n  End of PVObject pvSubArray\n"
PV_SYSTEM = "  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n  End of PVObject pvSystem\n"
STORAGE_SYSTEM = ("  PVObject_System=pvSystem\n    Flags=$10010040\n    SystemType=Grid\n"
                  "    BatteryFile=Fixture.BTR\n    NBattSerie=1\n    NBattParall=2\n  End of PVObject pvSystem\n")
OLD_LOAD = ("  PVObject_UserNeeds=pvLoad\n    Flags=$4000\n    LoadType=HourlyFile\n"
            "    Day_0101=+oldcache\n  End of PVObject pvLoad\n")
SNAPSHOT_LOAD = ("  PVObject_UserNeeds=pvLoad\n    Flags=$4000\n    DateBeg=01/01/23 00:00\n"
                 "    DateEnd=31/12/23 23:00\n  End of PVObject pvLoad\n")
GRID_STORAGE = ("  PVObject_GridStorage=pvGridStorage\n    Flags=$00\n    Strategy={strategy}\n"
                "    ChargingStrategy=PVAvailable\n    DischStrategy=Immediate\n    BattSoCInit=0.6\n"
                "    SOCChDecl=0.95\n    SOCDischDecl=0.2\n    SOCDischSelf=0.5\n    PMaxCharge=380\n"
                "    PMaxDisch=170\n    BattRectMaxEffic=97\n    BattRectEuroEffic=95\n    BattInvMaxEffic=97\n"
                "    BattInvEuroEffic=95\n{unavail}  End of PVObject pvGridStorage\n")
OLD_UNAVAIL = "    PVObject_GridUnavail=pvUnavail\n      Comment=old\n      Flags=$00\n    End of PVObject pvUnavail\n"
CLOSE = "End of PVObject pvVCalcul\n"
BTR = ("PVObject_=pvBattery\n  Version=8.1.6\n  Flags=$00\n  BattTechnol=Pb_Sealed_AGM\n  CapNomC10=160\n"
       "  AlphaSOC=0.2\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Fixture\n    Model=12V160Ah\n"
       "  End of PVObject pvCommercial\nEnd of PVObject pvBattery\n")
BATTERY = {"file_name": "Fixture.BTR", "series_count": 4, "parallel_count": 4,
           "monthly_temperature_c": [20] * 12, "cycle_sow": 1, "static_sow": 1}
STORAGE = {"strategy": "self_consumption", "soc_min": 0.2, "soc_max": 0.9, "initial_soc": 0.5, "reserve_soc": 0.2,
           "max_charge_ac_kw": 3, "max_discharge_ac_kw": 5, "charger_max_efficiency_pct": 97,
           "charger_euro_efficiency_pct": 95, "battery_inverter_max_efficiency_pct": 97,
           "battery_inverter_euro_efficiency_pct": 95}


def hourly(header, unit, value, year=2023, first=None):
    start = datetime(year, 1, 1)
    hours = 24 * (366 if calendar.isleap(year) else 365)
    return "".join([header + "\n", ";" + unit + "\n"] + [
        (start + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M") + ";" + (first if first and h < 24 else value) + "\n"
        for h in range(hours)])


class WorkspaceTests(unittest.TestCase):
    """New-VC builders on CRLF sources: plain numbers, line-start insertion, no stray newline."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for folder in ("Projects", "UserData", "ComposPV/Batteries"):
            (self.root / folder).mkdir(parents=True)
        (self.root / "Projects/Fixture.PRJ").write_text(
            "PVObject_=pvProject\n  Comment=Fixture\nEnd of PVObject pvProject\n", encoding="utf-8")
        (self.root / "ComposPV/Batteries/Fixture.BTR").write_text(BTR, encoding="utf-8", newline="")
        self.store = VariantStore(self.root, ComponentStore(self.root))

    def source(self, text):
        (self.root / "Projects/Fixture.VC0").write_bytes(text.replace("\n", "\r\n").encode())

    def created(self, result):
        self.assertEqual(result["status"], "created", result)
        text = (self.root / "Projects/Fixture.VC1").read_bytes().decode("utf-8")
        self.assertEqual(text.count("\n"), text.count("\r\n"), "no bare LF line in a CRLF variant")
        return text

    def prepare_load(self):
        (self.root / "UserData/raw.csv").write_text(hourly("Date;P Load", "W", "2500"), encoding="utf-8", newline="")
        cli = SimpleNamespace(_lock=threading.RLock(), workspace=self.root)
        prepared = prepare_load_profile(cli, csv_name="raw.csv", year=2023, output_name="load.csv",
                                        utc_offset_hours=8, time_basis="local_standard_interval_start",
                                        dry_run=False)
        self.assertEqual(prepared["status"], "prepared", prepared)

    def bind_load(self):
        return create_load_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
                                   load_manifest_name="load_load.json", dry_run=False)

    def build_storage(self, **changes):
        return create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0",
                                          new_variant="VC1", load_manifest_name="load_load.json",
                                          dry_run=False, **{"battery": BATTERY, "storage": STORAGE, **changes})

    def test_storage_scalars_never_use_exponent_notation(self):
        self.source(ROOT.format(root_flags="$00") + ARRAY + STORAGE_SYSTEM
                    + GRID_STORAGE.format(strategy="SelfConsumption", unavail="") + CLOSE)
        result = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC0",
                                             new_variant="VC1", dry_run=False,
                                             updates={"soc_min": 0.00001, "max_charge_ac_kw": 1e16,
                                                      "max_discharge_ac_kw": 1, "initial_soc": 0.4})
        text = self.created(result)
        for line in ("SOCDischDecl=0.00001", "PMaxCharge=10000000000000000",
                     "PMaxDisch=1.0", "BattSoCInit=0.4"):
            self.assertIn("    " + line + "\r\n", text)
        self.assertEqual(result["after"]["soc_min"], 0.00001)

    def test_weak_grid_rebinding_replaces_only_the_availability_lines(self):
        source = (ROOT.format(root_flags="$00080000") + ARRAY + STORAGE_SYSTEM + SNAPSHOT_LOAD
                  + GRID_STORAGE.format(strategy="WeakGrid", unavail=OLD_UNAVAIL) + CLOSE)
        self.source(source)
        (self.root / "UserData/grid.csv").write_text(hourly("Date;Grid Available", "ratio", "1", first="0"),
                                                    encoding="ascii", newline="")
        result = create_grid_storage_variant(
            self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1", dry_run=False,
            updates={"strategy": "weak_grid"}, availability={"csv_name": "grid.csv", "year": 2023,
                                                             "utc_offset_hours": 8,
                                                             "time_basis": "local_standard_interval_start"})
        text = self.created(result)
        self.assertNotIn("Comment=old", text)
        self.assertTrue(text.endswith("    End of PVObject pvUnavail\r\n  End of PVObject pvGridStorage\r\n" + CLOSE.replace("\n", "\r\n")))

    def test_storage_system_numbers_reference_temperature_and_line_start_insertion(self):
        self.prepare_load()
        self.source(ROOT.format(root_flags="$00100001") + ARRAY + PV_SYSTEM + "  " + CLOSE)
        battery = {**BATTERY, "monthly_temperature_c": [0.00001] + [20] * 11, "cycle_sow": 0.00001}
        result = self.build_storage(battery=battery, storage={**STORAGE, "soc_min": 0.00001})
        text = self.created(result)
        for line in ("CycleSOWBeg=0.00001", "StaticSOWBeg=1.0", "TempBattRef=0.00001",
                     "TempBattMois=0.00001" + ",20.0" * 11 + ",", "SOCDischDecl=0.00001", "PMaxCharge=3.0"):
            self.assertIn("    " + line + "\r\n", text)
        self.assertIn("  End of PVObject pvSystem\r\n  PVObject_UserNeeds=pvLoad\r\n", text)
        self.assertTrue(text.endswith("  End of PVObject pvGridStorage\r\n  End of PVObject pvVCalcul\r\n"))
        self.assertTrue(any("TempBattRef" in warning and "January" in warning for warning in result["warnings"]))

    def test_storage_system_replaces_an_existing_load_without_a_blank_line(self):
        self.prepare_load()
        self.source(ROOT.format(root_flags="$00100001") + ARRAY + PV_SYSTEM + OLD_LOAD + CLOSE)
        text = self.created(self.build_storage())
        self.assertNotIn("+oldcache", text)
        self.assertIn("  End of PVObject pvLoad\r\n  PVObject_GridStorage=pvGridStorage\r\n", text)

    def test_load_rebinding_keeps_neighbouring_lines(self):
        self.prepare_load()
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + "\n" + OLD_LOAD + CLOSE)
        text = self.created(self.bind_load())
        self.assertNotIn("+oldcache", text)
        self.assertIn("  End of PVObject pvSystem\r\n\r\n  PVObject_UserNeeds=pvLoad\r\n", text)
        self.assertTrue(text.endswith("  End of PVObject pvLoad\r\nEnd of PVObject pvVCalcul\r\n"))

    def test_new_load_goes_before_an_indented_closing_line(self):
        self.prepare_load()
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + "  " + CLOSE)
        text = self.created(self.bind_load())
        self.assertIn("  End of PVObject pvSystem\r\n  PVObject_UserNeeds=pvLoad\r\n", text)
        self.assertTrue(text.endswith("  End of PVObject pvLoad\r\n  End of PVObject pvVCalcul\r\n"))

    def test_blank_row_in_prepared_load_blocks_instead_of_raising(self):
        self.prepare_load()
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + CLOSE)
        prepared = self.root / "UserHourlyParams/load.csv"
        prepared.write_bytes(prepared.read_bytes() + b"\r\n")
        result = self.bind_load()
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["issues"], ["Every prepared load row must be one Date;P Load pair"])
        self.assertFalse((self.root / "Projects/Fixture.VC1").exists())


if __name__ == "__main__":
    unittest.main()

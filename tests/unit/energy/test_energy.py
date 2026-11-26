"""Tests for energy loss blocks, DC cabling, hourly loads, outages, and grid storage."""
from __future__ import annotations
import calendar
import functools
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.energy import dc as iam, dc as soiling, system as auxiliaries, system as inverter_temperature
from pvsyst_auto.energy.profiles import create_load_variant
from pvsyst_auto.energy.storage import peak_shaving_state
from pvsyst_auto.energy.profiles import prepare_load_profile
from pvsyst_auto.energy.storage import (create_grid_storage_variant, decode_subhour_schedule, encode_subhour_schedule)
from pvsyst_auto.energy.system import create_grid_storage_system
from pvsyst_auto.energy.profiles import system_unavail_state
from pvsyst_auto.project.variants import VariantStore
from decimal import Decimal
from pvsyst_auto.energy.dc import create_dc_cabling_variant, dc_cabling_state
from pvsyst_auto.energy.profiles import native_availability
from pvsyst_auto.energy.profiles import replace_system_unavailability
from pvsyst_auto.native.load import flat_load, native_load_block
def crlf(text):
    return text.replace("\n", "\r\n").splitlines(keepends=True)
MAIN = """PVObject_PVMainArray=pvMainArray
  Flags=$00100400
{soiling}PVObject_SystemCircuit=pvCircuit
End of PVObject pvCircuit
End of PVObject pvMainArray
"""
NONE_SOILING = ("Soiling=Soiling\n  IsYearly=False\n  IsMonthly=False\n  IsFromFile=False\n" "  YearlyValue=0.020\nEndTags\n")
CACHED_SOILING = ("Soiling=Soiling\n  IsYearly=True\n  IsMonthly=False\n  IsFromFile=False\n"
                  "  YearlyValue=0.025\n  MonthlyValue=" + "0.010," * 12 + "\nEndTags\n")
SYSTEM = """PVObject_=pvVCalcul
  Version=8.1.6
  PVObject_System=pvSystem
    Flags=$00
    SystemType=Grid
  End of PVObject pvSystem
End of PVObject pvVCalcul
"""
SUBARRAY = """PVObject_=pvSubArray
  Version=8.1.6
  Flags2={flags2}
{iam}End of PVObject pvSubArray
"""
ASHRAE = ("  PVObject_IAM=pvIAM\n    Flags=$00\n    IAMMode=Ashrae\n    B0_IAM=0.050\n" "  End of PVObject pvIAM\n")
ROOT = "PVObject_=pvVCalcul\n  Comment=Fixture\n  Version=8.1.6\n  Flags={root_flags}\n"
ARRAY = "  PVObject_=pvSubArray\n    SubArrayId=1\n    NModSerie=10\n  End of PVObject pvSubArray\n"
PV_SYSTEM = "  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n  End of PVObject pvSystem\n"
STORAGE_SYSTEM = ("  PVObject_System=pvSystem\n    Flags=$10010040\n    SystemType=Grid\n"
                  "    BatteryFile=Fixture.BTR\n    NBattSerie=1\n    NBattParall=2\n  End of PVObject pvSystem\n")
OLD_LOAD = ("  PVObject_UserNeeds=pvLoad\n    Flags=$4000\n    LoadType=HourlyFile\n" "    Day_0101=+oldcache\n  End of PVObject pvLoad\n")
SNAPSHOT_LOAD = ("  PVObject_UserNeeds=pvLoad\n    Flags=$4000\n    DateBeg=01/01/23 00:00\n" "    DateEnd=31/12/23 23:00\n  End of PVObject pvLoad\n")
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
BATTERY = {"file_name": "Fixture.BTR", "series_count": 4, "parallel_count": 4, "monthly_temperature_c": [20] * 12, "cycle_sow": 1, "static_sow": 1}
STORAGE = {"strategy": "self_consumption", "soc_min": 0.2, "soc_max": 0.9, "initial_soc": 0.5, "reserve_soc": 0.2,
           "max_charge_ac_kw": 3, "max_discharge_ac_kw": 5, "charger_max_efficiency_pct": 97,
           "charger_euro_efficiency_pct": 95, "battery_inverter_max_efficiency_pct": 97, "battery_inverter_euro_efficiency_pct": 95}
@functools.lru_cache(maxsize=8)
def hourly(header, unit, value, year=2023, first=None):
    start = datetime(year, 1, 1)
    hours = 24 * (366 if calendar.isleap(year) else 365)
    return "".join([header + "\n", ";" + unit + "\n"] + [
        (start + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M") + ";" + (first if first and h < 24 else value) + "\n" for h in range(hours)])
class EnergyBlockTests(unittest.TestCase):
    def test_soiling_noop_and_edit(self):
        for block, updates in ((NONE_SOILING, {"mode": "none"}), (CACHED_SOILING, {"mode": "constant", "yearly_loss_pct": 2.5})):
            lines = crlf(MAIN.format(soiling=block))
            original = list(lines)
            change = soiling.replace_soiling(lines, (0, len(lines) - 1), updates)
            self.assertEqual(lines, original)
            self.assertEqual(change["before"], change["after"])
        lines = crlf(MAIN.format(soiling=""))
        change = soiling.replace_soiling(lines, (0, len(lines) - 1), {"mode": "constant", "yearly_loss_pct": 2.5})
        self.assertEqual(change["after"], {"mode": "constant", "yearly_loss_pct": 2.5})
        self.assertEqual(lines[2:8], crlf("Soiling=Soiling\n  IsYearly=True\n  IsMonthly=False\n" "  IsFromFile=False\n  YearlyValue=0.025\nEndTags\n"))
    def test_auxiliaries_and_inverter_temperature_edits(self):
        cases = ((auxiliaries.replace_auxiliaries, {"mode": "constant", "operating_kw": 5, "night_kw": 2},
             ["    AuxPower_Const=5000.000\r\n", "    AuxPower_Night=2000.000\r\n"], "$00040000"),
            (inverter_temperature.replace_inverter_temperature, {"mode": "offset", "offset_c": 2.5, "fixed_c": 30},
             ["    TempShift=2.5\r\n", "    TempFixed=30.0\r\n"], "$4000"))
        for replace, updates, written, flags in cases:
            lines = crlf(SYSTEM)
            change = replace(lines, (2, 5), updates)
            self.assertEqual(lines[3], "    Flags=" + flags + "\r\n")
            self.assertEqual(lines[5:7], written)
            self.assertEqual(change["after"]["mode"], updates["mode"])
    def test_iam_switch_edit_and_missing_anchor_guard(self):
        lines = crlf(SUBARRAY.format(flags2="$00", iam=ASHRAE))
        change = iam.replace_iam_model(lines, (0, len(lines) - 1), {"mode": "component"})
        self.assertEqual(change["after"], {"effective": "component", "stored_model": "Ashrae", "stored_ashrae_b0": 0.05})
        self.assertEqual(lines[2], "  Flags2=$20\r\n")
        self.assertEqual(lines[3:8], crlf(ASHRAE))
        lines = crlf(SUBARRAY.format(flags2="$00", iam=ASHRAE))
        change = iam.replace_iam_model(lines, (0, len(lines) - 1), {"mode": "ashrae", "ashrae_b0": 0.06})
        self.assertEqual(change["after"]["stored_ashrae_b0"], 0.06)
        self.assertEqual(lines[6], "    B0_IAM=0.060\r\n")
        lines = crlf(SUBARRAY.format(flags2="$20", iam=""))
        original = list(lines)
        with self.assertRaisesRegex(ValueError, "WiringResistance"):
            iam.replace_iam_model(lines, (0, len(lines) - 1), {"mode": "ashrae", "ashrae_b0": 0.05})
        self.assertEqual(lines, original)
    def test_unavailability_and_peak_shaving_state_parsing(self):
        def unavail_state(period_lines, count, header="  SystemUnavail=TUnavailability\n"):
            lines = crlf(f"PVObject_System=pvSystem\n{header}    DefMode=Random\n"
                         f"    NbPeriods={count}\n{period_lines}  End of PVObject pvUnavail\nEnd of PVObject pvSystem\n")
            return system_unavail_state(lines, (0, len(lines) - 1))
        self.assertIn("NbPeriods does not match the stored period list",
                      unavail_state("    DateBeg=01/01/90 00:00\n    Duration=24\n", "\u00b2")["issues"])
        self.assertNotIn("issues", unavail_state("    DateBeg=01/01/90 00:00\n    Duration=24\n", 1))
        missing_dur = unavail_state("    DateBeg=01/01/90 00:00\n    DateBeg=02/01/90 00:00\n    Duration=24\n", 2)
        self.assertEqual(missing_dur["issues"], ["1 SystemUnavail period(s) lack a Duration within three lines of DateBeg"])
        spaced = unavail_state("", 0, header="  SystemUnavail = TUnavailability\n")
        self.assertEqual((spaced["configured"], spaced["mode"], spaced["period_count"]), (True, "Random", 0))
        record = (GRID_STORAGE.format(strategy="PeakShaving", unavail="")
                  .replace("PVAvailable", "PVToGridMin").replace("DischStrategy=Immediate", "DischStrategy=AfterSunset"))
        for owner in ("PVObject_GridStorage=pvGridStorage", "PVObject_GridStorage = pvGridStorage"):
            state = peak_shaving_state(crlf(record.replace("PVObject_GridStorage=pvGridStorage", owner)))
            self.assertEqual((state.get("mode"), state["discharge"]), ("configured", {"mode": "after_sunset"}))
class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._seed_dir = tempfile.TemporaryDirectory()
        seed = Path(cls._seed_dir.name)
        for folder in ("Projects", "UserData", "ComposPV/Batteries"): (seed / folder).mkdir(parents=True)
        (seed / "Projects/Fixture.PRJ").write_text("PVObject_=pvProject\n  Comment=Fixture\nEnd of PVObject pvProject\n", encoding="utf-8")
        (seed / "ComposPV/Batteries/Fixture.BTR").write_text(BTR, encoding="utf-8", newline="")
        (seed / "UserData/raw.csv").write_text(hourly("Date;P Load", "W", "2500"), encoding="utf-8", newline="")
        cli = SimpleNamespace(_lock=threading.RLock(), workspace=seed)
        prepared = prepare_load_profile(cli, csv_name="raw.csv", year=2023, output_name="load.csv",
                                        utc_offset_hours=8, time_basis="local_standard_interval_start", dry_run=False)
        assert prepared["status"] == "prepared", prepared
    @classmethod
    def tearDownClass(cls):
        cls._seed_dir.cleanup()
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "ws"
        shutil.copytree(self._seed_dir.name, self.root)
        self.store = VariantStore(self.root, ComponentStore(self.root))
    def source(self, text):
        (self.root / "Projects/Fixture.VC0").write_bytes(text.replace("\n", "\r\n").encode())
    def created(self, result):
        self.assertEqual(result["status"], "created", result)
        text = (self.root / "Projects/Fixture.VC1").read_bytes().decode("utf-8")
        self.assertEqual(text.count("\n"), text.count("\r\n"), "no bare LF line in a CRLF variant")
        return text
    def bind_load(self):
        return create_load_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
                                   load_manifest_name="load_load.json", dry_run=False)
    def build_storage(self, **changes):
        return create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1", load_manifest_name="load_load.json",
                                          dry_run=False, **{"battery": BATTERY, "storage": STORAGE, **changes})
    def test_storage_and_component_swaps_reject_leading_space_battery_bindings(self):
        name = " Fixture.BTR"
        (self.root / "ComposPV/Batteries" / name).write_text(BTR, encoding="utf-8")
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + CLOSE)
        for library in ("workspace", "builtin"):
            result = self.build_storage(battery={**BATTERY, "file_name": name, "library": library})
            self.assertEqual(result["status"], "blocked", result)
            self.assertIn("Battery references cannot start with whitespace", result["issues"][0])
            self.assertFalse((self.root / "Projects/Fixture.VC1").exists())
        self.created(self.build_storage())
        variant = self.root / "Projects/Fixture.VC1"
        variant.write_bytes(variant.read_bytes().replace(b"BatteryFile=Fixture.BTR", b"BatteryFile= Fixture.BTR"))
        before = variant.read_bytes()
        with self.assertRaisesRegex(ValueError, "Battery references cannot start with whitespace"): self.store.update("Fixture.PRJ", "VC1", {"BTR": name})
        self.assertEqual(variant.read_bytes(), before)
        self.store.update("Fixture.PRJ", "VC1", {"BTR": "Fixture.BTR"})
        self.assertIn(b"BatteryFile=Fixture.BTR\r\n", variant.read_bytes())
    def test_storage_scalars_never_use_exponent_notation(self):
        self.source(ROOT.format(root_flags="$00") + ARRAY + STORAGE_SYSTEM + GRID_STORAGE.format(strategy="SelfConsumption", unavail="") + CLOSE)
        result = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1", dry_run=False,
                                             updates={"soc_min": 0.00001, "max_charge_ac_kw": 1e16, "max_discharge_ac_kw": 1, "initial_soc": 0.4})
        text = self.created(result)
        for line in ("SOCDischDecl=0.00001", "PMaxCharge=10000000000000000", "PMaxDisch=1.0", "BattSoCInit=0.4"):
            self.assertIn("    " + line + "\r\n", text)
        self.assertEqual(result["after"]["soc_min"], 0.00001)
    def test_storage_edits_invalidate_plot_only_saved_results(self):
        plot = ("  SimulPlots=Start\n    PVObject_=pvSimulPlot\n      VarY=E_Grid\n"
                "      NValRes=2\n      ValuesY=1,2,\n    End of PVObject pvSimulPlot\n  EndTags\n")
        existing = STORAGE_SYSTEM + GRID_STORAGE.format(strategy="SelfConsumption", unavail="")
        for target, system, edit, inputs in (
            ("VC1", PV_SYSTEM, create_grid_storage_system, {"battery": BATTERY, "storage": STORAGE, "load_manifest_name": "load_load.json"}),
            ("VC2", existing, create_grid_storage_variant, {"updates": {"max_charge_ac_kw": 999}})):
            with self.subTest(edit=edit.__name__):
                self.source(ROOT.format(root_flags="$00100001") + ARRAY + system + plot + CLOSE)
                before = self.store.inspect_parameters("Fixture.PRJ", "VC0", include_saved_results=True)["saved_results"]
                self.assertTrue(before["present"])
                result = edit(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant=target, dry_run=False, **inputs)
                self.assertEqual(result["status"], "created", result)
                after = self.store.inspect_parameters("Fixture.PRJ", target, include_saved_results=True)["saved_results"]
                self.assertFalse(after["present"])
                self.assertEqual(int(after["root_flags"][1:], 16) & 1, 0)
    def test_weak_grid_rebinding_replaces_only_the_availability_lines(self):
        source = (ROOT.format(root_flags="$00080000") + ARRAY + STORAGE_SYSTEM + SNAPSHOT_LOAD
                  + GRID_STORAGE.format(strategy="WeakGrid", unavail=OLD_UNAVAIL) + CLOSE)
        self.source(source)
        (self.root / "UserData/grid.csv").write_text(hourly("Date;Grid Available", "ratio", "1", first="0"), encoding="ascii", newline="")
        result = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1", dry_run=False,
            updates={"strategy": "weak_grid"}, availability={"csv_name": "grid.csv", "year": 2023, "utc_offset_hours": 8,
                                                             "time_basis": "local_standard_interval_start"})
        text = self.created(result)
        self.assertNotIn("Comment=old", text)
        self.assertTrue(text.endswith("    End of PVObject pvUnavail\r\n  End of PVObject pvGridStorage\r\n" + CLOSE.replace("\n", "\r\n")))
    def test_new_storage_preserves_owning_system_unavailability_bit(self):
        from pvsyst_auto.energy.profiles import replace_system_unavailability
        for source_flags, has_outage, controls in (("$00", False, {"strategy": "peak_shaving", "grid_limit_kw": 6000.0}),
            ("$05100000", False, {"strategy": "power_shift", "charging_pv_fraction": 0.4, "discharging_power_kw": 3000.0}),
            ("$05100000", True, {"strategy": "power_shift", "charging_pv_fraction": 0.4, "discharging_power_kw": 3000.0})):
            with self.subTest(source_flags=source_flags, has_outage=has_outage, strategy=controls["strategy"]):
                text = ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM.replace("$00", source_flags) + CLOSE
                lines = text.splitlines(keepends=True)
                if has_outage:
                    replace_system_unavailability(lines, self.store._sections(lines)[1], {
                        "mode": "periods", "periods": [{"date_beg": "03/06/90 00:00", "duration_hours": 48}]})
                self.source("".join(lines))
                result = create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
                    battery=BATTERY, storage={**STORAGE, **controls}, dry_run=False)
                written = self.created(result).splitlines(keepends=True)
                saved = self.root / "Projects/Fixture.VC1"
                saved.unlink()
                state = system_unavail_state(written, self.store._sections(written)[1])
                self.assertEqual(state["configured"], has_outage)
                self.assertEqual(bool(int(result["configuration"]["system_flags"][1:], 16) & 0x00010000), has_outage)
    def test_storage_system_load_insertion_and_replacement(self):
        self.source(ROOT.format(root_flags="$00100001") + ARRAY + PV_SYSTEM + "  " + CLOSE)
        battery = {**BATTERY, "monthly_temperature_c": [0.00001] + [20] * 11, "cycle_sow": 0.00001}
        text = self.created(self.build_storage(battery=battery, storage={**STORAGE, "soc_min": 0.00001}))
        for line in ("CycleSOWBeg=0.00001", "StaticSOWBeg=1.0", "TempBattRef=0.00001",
                     "TempBattMois=0.00001" + ",20.0" * 11 + ",", "SOCDischDecl=0.00001", "PMaxCharge=3.0"):
            self.assertIn("    " + line + "\r\n", text)
        self.assertIn("  End of PVObject pvSystem\r\n  PVObject_UserNeeds=pvLoad\r\n", text)
        self.assertTrue(text.endswith("  End of PVObject pvGridStorage\r\n  End of PVObject pvVCalcul\r\n"))
        (self.root / "Projects/Fixture.VC1").unlink()
        self.source(ROOT.format(root_flags="$00100001") + ARRAY + PV_SYSTEM + OLD_LOAD + CLOSE)
        replaced = self.created(self.build_storage())
        self.assertNotIn("+oldcache", replaced)
        self.assertIn("  End of PVObject pvLoad\r\n  PVObject_GridStorage=pvGridStorage\r\n", replaced)
    def test_load_rebinding_new_insertion_and_blank_row_block(self):
        from pvsyst_auto.native.load import create_native_load_variant
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + "\n" + OLD_LOAD + CLOSE)
        text = self.created(self.bind_load())
        self.assertNotIn("+oldcache", text)
        self.assertIn("  End of PVObject pvSystem\r\n\r\n  PVObject_UserNeeds=pvLoad\r\n", text)
        self.assertTrue(text.endswith("  End of PVObject pvLoad\r\nEnd of PVObject pvVCalcul\r\n"))
        (self.root / "Projects/Fixture.VC1").unlink()
        self.source(ROOT.format(root_flags="$00100020") + ARRAY + PV_SYSTEM + "  " + CLOSE)
        inserted = self.created(create_native_load_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            load={"mode": "hourly", "load_manifest_name": "load_load.json"}, dry_run=False))
        self.assertEqual(inserted.splitlines()[3], "  Flags=$00180020")
        self.assertIn("  End of PVObject pvSystem\r\n  PVObject_UserNeeds=pvLoad\r\n", inserted)
        self.assertTrue(inserted.endswith("  End of PVObject pvLoad\r\n  End of PVObject pvVCalcul\r\n"))
        with self.assertRaisesRegex(ValueError, "hourly load accepts only"):
            create_native_load_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC2",
                load={"mode": "hourly", "load_manifest_name": "load_load.json", "extra": 1})
        (self.root / "Projects/Fixture.VC1").unlink()
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + CLOSE)
        prepared = self.root / "UserHourlyParams/load.csv"
        prepared.write_bytes(prepared.read_bytes() + b"\r\n")
        blocked = self.bind_load()
        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assertEqual(blocked["issues"], ["Every prepared load row must be one Date;P Load pair"])
        self.assertFalse((self.root / "Projects/Fixture.VC1").exists())
    def test_load_rebinding_invalidates_saved_results(self):
        cache = ("  VarList, list of=1 TVarSimul\n    VarSimul_1=E_User\n" "      Varident=E_User\n      ValMonth=10,20,\n"
                 "    End of VarSimul=viE_User\n  End of the List VarList\n")
        self.source(ROOT.format(root_flags="$00080001") + ARRAY + PV_SYSTEM + OLD_LOAD + cache + CLOSE)
        text = self.created(self.bind_load())
        state = self.store.inspect_parameters("Fixture.PRJ", "VC1", include_saved_results=True)["saved_results"]
        self.assertFalse(state["present"])
        self.assertEqual(state["root_flags"], "$00080000")
        self.assertIn("    Flags=$4000\r\n", text)
        source = self.store.inspect_parameters("Fixture.PRJ", "VC0", include_saved_results=True)["saved_results"]
        self.assertTrue(source["present"])
    def test_legacy_outages_are_reported_but_read_only(self):
        legacy = {"UnavTotDuration": "7.30", "UnavNbPeriods": "3", "UnavDuration": "58,58,58,1,1,", "UnavBegDate": "15000,15001,15002,"}
        system = PV_SYSTEM.replace("Flags=$00", "Flags=$00010000").replace(
            "  End of PVObject pvSystem", "".join(f"    {key} = {value}\n" for key, value in legacy.items()) + "  End of PVObject pvSystem")
        self.source(ROOT.format(root_flags="$00") + "  PVObject_PVMainArray=pvMainArray\n    Flags=$00100400\n    Flags2=$082A\n" +
                    ARRAY.replace("    SubArrayId=1", "    Version=8.1.6\n    SubArrayId=1") + "  End of PVObject pvMainArray\n" + system + CLOSE)
        source = self.root / "Projects/Fixture.VC0"
        original = source.read_bytes()
        state = self.store.inspect_parameters("Fixture.PRJ", "VC0")["system_unavailability"]
        self.assertEqual(state, {"configured": True, "mode": "legacy", "legacy_fields": legacy})
        digest = self.store.inspect("Fixture.PRJ", "VC0")["sha256"]
        for updates in ({"mode": "none"}, {"mode": "periods", "periods": [{"date_beg": "01/01/23 12:00", "duration_hours": 24}]}):
            with self.assertRaisesRegex(ValueError, "Legacy outage fields are read-only"):
                self.store.update_parameters("Fixture.PRJ", "VC0", digest, system_unavailability_updates=updates)
            self.assertEqual(source.read_bytes(), original)
    def test_system_unavailability_rejects_duration_rounding_to_zero(self):
        from pvsyst_auto.energy.profiles import replace_system_unavailability
        lines = crlf(SYSTEM)
        original = list(lines)
        period = {"date_beg": "01/01/23 12:00", "duration_hours": 0.0001}
        updates = {"mode": "periods", "periods": [period]}
        with self.assertRaisesRegex(ValueError, "rounds to 0.000"): replace_system_unavailability(lines, (2, 5), updates)
        self.assertEqual(lines, original)
        period["duration_hours"] = 0.0006
        result = replace_system_unavailability(lines, (2, 5), updates)
        self.assertEqual(result["after"]["periods"], [{"date_beg": "01/01/23 12:00", "duration": "0.001"}])
        self.assertIn("        Duration=0.001\r\n", lines)
    def test_system_unavailability_authoring_and_geometry_combo(self):
        from pvsyst_auto.energy.profiles import replace_system_unavailability
        from pvsyst_auto.native.load import create_native_load_variant
        lines = crlf(SYSTEM)
        added = replace_system_unavailability(lines, (2, 5),
            {"mode": "periods", "periods": [{"date_beg": "10/01/23 08:00", "duration_hours": 12.5}, {"date_beg": "15/06/23 00:00", "duration_hours": 24}]})
        self.assertTrue(added["after"]["configured"])
        self.assertEqual(added["after"]["mode"], "Periods")
        self.assertEqual(added["after"]["period_count"], 2)
        self.assertIn("    Flags=$00010000\r\n", lines)
        self.assertIn("    SystemUnavail=TUnavailability\r\n", lines)
        self.assertIn("        DateBeg=10/01/23 08:00\r\n", lines)
        self.assertIn("        Duration=12.500\r\n", lines)
        replaced = replace_system_unavailability(lines, (2, len(lines) - 2),
            {"mode": "periods", "periods": [{"date_beg": "01/03/23 09:00", "duration_hours": 4}]})
        self.assertEqual(replaced["before"]["period_count"], 2)
        self.assertEqual(replaced["after"]["period_count"], 1)
        removed = replace_system_unavailability(lines, (2, len(lines) - 2), {"mode": "none"})
        self.assertFalse(removed["after"]["configured"])
        self.assertIn("    Flags=$00000000\r\n", lines)
        self.assertNotIn("SystemUnavail", "".join(lines))
        for bad in ({"mode": "periods", "periods": []}, {"mode": "periods", "periods": [{"date_beg": "31/02/23 00:00", "duration_hours": 4}]},
            {"mode": "periods", "periods": [{"date_beg": "01/01/23 00:00", "duration_hours": 0}]},
            {"mode": "none", "periods": [{"date_beg": "01/01/23 00:00", "duration_hours": 1}]}):
            with self.assertRaises(ValueError): replace_system_unavailability(list(lines), (2, len(lines) - 2), bad)
        # Native load and peak shaving now compose cleanly with an active 3D shading scene + VarList
        circuit = ("    PVObject_SystemCircuit=pvCircuit\n      Version=8.1.6\n      Flags=$00\n"
            "      InjectionPointNode Start;\n        Children=Start\n          InverterNode Start;\n"
            "            NElements=1\n            SubArrayId=1\n            SubArrayName=PV Array\n"
            "            Children=Start\n              MPPTNode Start;\n                NElements=1\n"
            "                SubArrayId=1\n                SubArrayName=PV Array\n" "                Children=Start\n                  StringNode Start;\n"
            "                    NElements=2\n                    SubArrayId=1\n"
            "                    SubArrayName=PV Array\n                  StringNode End;\n"
            "                Children=End\n              MPPTNode End;\n            Children=End\n"
            "          InverterNode End;\n        Children=End\n      InjectionPointNode End;\n" "    End of PVObject pvCircuit\n")
        shaded = ("PVObject_=pvVCalcul\n  Comment=Fixture\n  Version=8.1.6\n  Flags=$00100001\n"
            "  PVObject_PVMainArray=pvMainArray\n    Flags=$01900400\n    Flags2=$082A\n" + circuit
            + "    PVObject_=pvSubArray\n      Comment=PV Array\n      Version=8.1.6\n      Flags=$019E1002\n"
            "      SubArrayId=1\n      NModSerie=10\n      NStringCh=2\n      NInverter=1\n"
            "      NInvMPPT=1\n      NoOrientation=1\n      NStrOrient1=2\n" "      PowerSharingInverterId=-1\n      PNomAC_InMPPT=5.00\n"
            "    End of PVObject pvSubArray\n  End of PVObject pvMainArray\n" "  PVObject_=pvOrient\n    Version=8.1.6\n    Flags=$00\n    NoOrient=1\n"
            "    FieldType=FixedPlane\n    FieldTilt=20.0\n    FieldAzim=0.0\n" "    BaseSlope=0.0\n    UsesTiltAver=False\n    UsesAzimAver=False\n"
            "    UsesSlopeAver=False\n  End of TOrientGroup\n" "  PVObject_Ombrage=pvShading\n    Flags=$00\n  End of PVObject pvShading\n"
            "  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n  End of PVObject pvSystem\n" "  SimulPlots=Start\n  End of List SimulPlots\n"
            "  VarList, list of=1 TVarSimul\n    VarSimul_1=E_Grid\n  End of the List VarList\n" "End of PVObject pvVCalcul\n")
        self.source(shaded)
        loaded = create_native_load_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            load={"mode": "constant", "constant_kw": 2.5}, dry_run=False)
        vc1_text = self.created(loaded)
        self.assertIn("Flags=$00180000\r\n", vc1_text)
        self.assertIn("PVObject_Ombrage=pvShading\r\n", vc1_text)
        self.assertNotIn("VarList, list of=", vc1_text)
        # update_parameters system_unavailability_updates works with active pvShading + VarList
        self.source(shaded)
        digest = self.store.inspect("Fixture.PRJ", "VC0")["sha256"]
        upd = self.store.update_parameters("Fixture.PRJ", "VC0", digest,
            system_unavailability_updates={"mode": "periods", "periods": [{"date_beg": "10/01/23 08:00", "duration_hours": 10}]})
        self.assertTrue(upd["changed"])
        self.assertEqual(upd["system_unavailability"]["after"]["period_count"], 1)
        vc0_after = (self.root / "Projects/Fixture.VC0").read_text(encoding="utf-8")
        self.assertIn("PVObject_Ombrage=pvShading", vc0_after)
        self.assertNotIn("VarList, list of=", vc0_after)
        self.assertIn("Flags=$00100000", vc0_after)
    def test_power_shift_schedule_preserves_small_slots_with_a_large_peak(self):
        slots = [3000.0, 0.02] + [0.0] * 45 + [3000.0]
        encoded = encode_subhour_schedule(slots)
        self.assertEqual(decode_subhour_schedule(encoded), slots)
        self.assertTrue(encoded.startswith("48+50+"))
        self.assertTrue(all(len(token) <= 3 for token in encoded.split("+")[2:]))
    def test_power_shift_schedule_accepts_six_decimal_values_and_float_noise(self):
        for value, expected, scale in ((0.333333, 0.333333, 270000), (0.1 + 0.2, 0.3, 300000), (0.000001, 0.000001, 1000000)):
            with self.subTest(value=value):
                encoded = encode_subhour_schedule([value] * 48, maximum=1.0)
                self.assertTrue(encoded.startswith(f"48+{scale}+"))
                self.assertEqual(decode_subhour_schedule(encoded), [expected] * 48)
    def test_power_shift_schedule_warns_when_using_native_approximation(self):
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + CLOSE)
        slots = [2500.123] + [0.0] * 47
        result = create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            battery=BATTERY, storage={**STORAGE, "strategy": "power_shift", "charging_pv_fraction": 0.3, "discharging_schedule_48": slots}, dry_run=False)
        self.assertEqual(result["status"], "created", result["issues"])
        updated = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC1", new_variant="VC2",
            updates={"discharging_schedule_48": slots}, dry_run=True)
        self.assertEqual(updated["status"], "ready", updated["issues"])
        for response, key in ((result, "configuration"), (updated, "after")):
            with self.subTest(key=key):
                self.assertEqual(response[key]["discharging_schedule_48"], [2500.111111] + [0.0] * 47)
                self.assertTrue(any("discharging_schedule_48" in warning and "0.011889" in warning
                                    for warning in response["warnings"]), response["warnings"])
    def test_power_shift_schedule_rejects_positive_slots_that_become_zero(self):
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + CLOSE)
        result = create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            battery=BATTERY, storage={**STORAGE, "strategy": "power_shift", "charging_pv_fraction": 0.3, "discharging_power_kw": 3000.0}, dry_run=False)
        self.assertEqual(result["status"], "created", result)
        for target, slots, issue in (("VC2", [3000.0, 0.001] + [0.0] * 46, "would encode as zero"),
            ("VC3", [5e-324] * 48, "rounds to zero at six decimal places")):
            with self.subTest(slot=slots[1]):
                invalid = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC1", new_variant=target,
                    updates={"discharging_schedule_48": slots}, dry_run=False)
                self.assertEqual(invalid["status"], "blocked", invalid.get("issues"))
                self.assertIn(issue, invalid["issues"][0])
                self.assertFalse((self.root / "Projects" / f"Fixture.{target}").exists())
    def test_powershift_peakshaving_from_scratch_and_builtin_li_btr(self):
        # Verify exact TParSimulSeries base62 roundtrip matches PVsyst 8.1.7 _DEMO_Utility.VCA encoding
        chg_slots = [0.0] * 18 + [0.3] * 16 + [0.0] * 14
        encoded_chg = encode_subhour_schedule(chg_slots, maximum=1.0)
        self.assertTrue(encoded_chg.startswith("48+300000+0+0+"))
        self.assertIn("+NPc+NPc+", encoded_chg)
        self.assertEqual(decode_subhour_schedule(encoded_chg), chg_slots)
        dis_slots = [3000.0] * 14 + [0.0] * 24 + [3000.0] * 10
        encoded_dis = encode_subhour_schedule(dis_slots)
        self.assertTrue(encoded_dis.startswith("48+30+NPc+"))
        self.assertEqual(decode_subhour_schedule(encoded_dis), dis_slots)
        shaded = ("PVObject_=pvVCalcul\n  Comment=Fixture\n  Version=8.1.7\n  Flags=$00100001\n"
            + "  PVObject_PVMainArray=pvMainArray\n    Flags=$01900400\n    Flags2=$082A\n"
            + "    PVObject_SystemCircuit=pvCircuit\n      Version=8.1.7\n      Flags=$00\n"
            + "      InjectionPointNode Start;\n        Children=Start\n          InverterNode Start;\n"
            + "            NElements=1\n            SubArrayId=1\n            SubArrayName=PV Array\n"
            + "            Children=Start\n              MPPTNode Start;\n                NElements=1\n"
            + "                SubArrayId=1\n                SubArrayName=PV Array\n"
            + "                Children=Start\n                  StringNode Start;\n"
            + "                    NElements=2\n                    SubArrayId=1\n"
            + "                    SubArrayName=PV Array\n                  StringNode End;\n"
            + "                Children=End\n              MPPTNode End;\n            Children=End\n"
            + "          InverterNode End;\n        Children=End\n      InjectionPointNode End;\n" + "    End of PVObject pvCircuit\n"
            + "    PVObject_=pvSubArray\n      Comment=PV Array\n      Version=8.1.7\n      Flags=$019E1002\n"
            + "      Flags2=$20\n      SubArrayId=1\n      NModSerie=10\n      NStringCh=2\n"
            + "      NInverter=1\n      NInvMPPT=1\n      NoOrientation=1\n      NStrOrient1=2\n"
            + "    End of PVObject pvSubArray\n  End of PVObject pvMainArray\n" + "  Orientations, list of=1 TOrientGroup\n"
            + "    PVObject_=pvOrient\n      Version=8.1.7\n      Flags=$00\n      NoOrient=1\n"
            + "      FieldType=FixedPlane\n      FieldTilt=20.0\n      FieldAzim=0.0\n" + "    End of TOrientGroup\n  End of Orientations\n"
            + "  PVObject_Ombrage=pvShading\n    Flags=$00\n  End of PVObject pvShading\n"
            + "  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n  End of PVObject pvSystem\n"
            + "  VarList, list of=1 TVarSimul\n    VarSimul_1=E_Grid\n  End of the List VarList\n" + "End of PVObject pvVCalcul\n")
        self.source(shaded)
        builtin_batt = {"file_name": "Generic_LI_Module_180Ah.BTR", "library": "builtin", "series_count": 2,
            "parallel_count": 4, "monthly_temperature_c": [20] * 12, "cycle_sow": 1, "static_sow": 1}
        ps_base = {**STORAGE, "strategy": "power_shift", "charging_strategy": "pv_prod_fraction",
            "charging_pv_fraction": 0.3, "charging_schedule_48": chg_slots, "discharging_strategy": "specified_hours", "discharging_power_kw": 3.0,
            "discharging_schedule_48": [3.0] * 12 + [0.0] * 24 + [3.0] * 12}
        res_ps = create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            battery=builtin_batt, storage=ps_base, load_manifest_name=None, dry_run=False)
        vc1 = self.created(res_ps)
        self.assertIn("BatteryFile=Generic_LI_Module_180Ah.BTR\r\n", vc1)
        self.assertIn("Strategy=PowerShift\r\n", vc1)
        self.assertIn("Varident=FPVChMx\r\n", vc1)
        self.assertIn("Varident=EDisMax\r\n", vc1)
        self.assertIn("PVObject_Ombrage=pvShading\r\n", vc1)
        self.assertNotIn("VarList, list of=", vc1)
        self.assertEqual(res_ps["battery_library"], "builtin")
        params = self.store.inspect_parameters("Fixture.PRJ", "VC1")
        self.assertEqual(params["grid_storage"]["mode"], "stored")
        self.assertEqual(params["grid_storage"]["charging_schedule_48"], chg_slots)
        # Switch VC1 -> VC2 (SpecChargPower + Night)
        res_ps2 = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC1", new_variant="VC2",
            updates={"charging_strategy": "spec_charg_power", "charging_power_kw": 4.0, "charging_schedule_48": [0.0] * 18 + [4.0] * 16 + [0.0] * 14,
                "discharging_strategy": "night", "discharging_power_kw": 2.5}, dry_run=False)
        self.assertEqual(res_ps2["status"], "created", res_ps2)
        vc2 = (self.root / "Projects/Fixture.VC2").read_text(encoding="utf-8")
        self.assertIn("ChargingStrategy=SpecChargPower", vc2)
        self.assertIn("DischStrategy=Night", vc2)
        self.assertNotIn("DischargingPowerParSim", vc2)
        # Unsupported PowerShift + AfterSunset fails closed
        bad_ps = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC1", new_variant="VC3",
            updates={"discharging_strategy": "after_sunset", "discharging_power_kw": 2.0}, dry_run=False)
        self.assertEqual(bad_ps["status"], "blocked")
        # Create PeakShaving from scratch on 3D VC0 -> VC4, then switch hour_window -> VC5
        res_pk = create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC4", battery=builtin_batt, storage={
                **STORAGE, "strategy": "peak_shaving", "charging_strategy": "pv_above_thresh", "discharging_strategy": "after_sunset",
                "grid_limit_kw": 4.5}, dry_run=False)
        self.assertEqual(res_pk["status"], "created", res_pk)
        vc4 = (self.root / "Projects/Fixture.VC4").read_text(encoding="utf-8")
        self.assertIn("PLimGrid=4.5", vc4)
        self.assertIn("Strategy=PeakShaving", vc4)
        # DC-coupled / hybrid storage fails closed cleanly (PVsyst 8.1.7 Common AC Bus boundary)
        bad_dc = create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC6",
            battery=builtin_batt, storage={**ps_base, "coupling": "dc_coupled"}, load_manifest_name=None, dry_run=False)
        self.assertEqual(bad_dc["status"], "blocked")
        self.assertIn("Common AC Bus", bad_dc["issues"][0])
        bad_dc_upd = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC1", new_variant="VC6",
            updates={"coupling": "hybrid", "soc_min": 0.25}, dry_run=False)
        self.assertEqual(bad_dc_upd["status"], "blocked")
        self.assertIn("Common AC Bus", bad_dc_upd["issues"][0])
    def test_omitted_inverter_nelements_and_system_unavail_direct_fields(self):
        from pvsyst_auto.components.editor import direct_fields
        from pvsyst_auto.energy.system import ac_cabling_state
        from pvsyst_auto.energy.dc import create_dc_cabling_variant
        from pvsyst_auto.energy.system import create_grid_storage_system
        from pvsyst_auto.energy.profiles import replace_system_unavailability
        # 8.1.0 style: omit NElements=1 on InverterNode and StringNode
        vc_no_nel = ("PVObject_=pvVCalcul\n  Comment=Fixture\n  Version=8.1.7\n  Flags=$00100000\n"
            "  PVObject_PVMainArray=pvMainArray\n    Flags=$01900400\n    Flags2=$082A\n" "    MVNbTransfos=0\n"
            "    PVObject_SystemCircuit=pvCircuit\n      Version=8.1.7\n      Flags=$00\n"
            "      InjectionPointNode Start;\n        Children=Start\n          InverterNode Start;\n"
            "            SubArrayId=1\n            SubArrayName=PV Array\n"
            "            Children=Start\n              MPPTNode Start;\n                NElements=2\n"
            "                SubArrayId=1\n                SubArrayName=PV Array\n" "                Children=Start\n                  StringNode Start;\n"
            "                    SubArrayId=1\n                    SubArrayName=PV Array\n"
            "                  StringNode End;\n                Children=End\n              MPPTNode End;\n"
            "            Children=End\n          InverterNode End;\n        Children=End\n"
            "      InjectionPointNode End;\n    End of PVObject pvCircuit\n"
            "    PVObject_=pvSubArray\n      Comment=PV Array\n      Version=8.1.7\n      Flags=$019E1002\n"
            "      Flags2=$20\n      SubArrayId=1\n      PVModule=Mod.PAN\n      GInverter=Inv.OND\n"
            "      NModSerie=10\n      NStringCh=2\n      NInverter=1\n      NInvMPPT=1\n" "      NoOrientation=1\n      NStrOrient1=2\n"
            "      RSCablageCh=0.0\n      RSCablagePC=0.00\n      ACWireLength=0.0\n" "    End of PVObject pvSubArray\n  End of PVObject pvMainArray\n"
            "  Orientations, list of=1 TOrientGroup\n    PVObject_=pvOrient\n      Version=8.1.7\n"
            "      Flags=$00\n      NoOrient=1\n      FieldType=FixedPlane\n      FieldTilt=20.0\n"
            "      FieldAzim=0.0\n      BaseSlope=0.0\n      UsesTiltAver=False\n"
            "      UsesAzimAver=False\n      UsesSlopeAver=False\n    End of TOrientGroup\n" "  End of Orientations\n"
            "  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n  End of PVObject pvSystem\n" "End of PVObject pvVCalcul\n")
        self.source(vc_no_nel)
        ac_st = ac_cabling_state(vc_no_nel.splitlines(True))
        self.assertEqual(ac_st["inverter_count"], 1, ac_st)
        dc_res = create_dc_cabling_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC8",
            dc={"mode": "resistance", "value": 150.0}, dry_run=False)
        self.assertEqual(dc_res["status"], "created", dc_res)
        vc8_lines = (self.root / "Projects/Fixture.VC8").read_text(encoding="utf-8").splitlines(True)
        _, system_bounds = self.store._sections(vc8_lines)
        replace_system_unavailability(vc8_lines, system_bounds, {"mode": "periods", "periods": [{"date_beg": "03/06/90 00:00", "duration_hours": 48}]})
        (self.root / "Projects/Fixture.VC8").write_text("".join(vc8_lines), encoding="utf-8")
        _, sys_after = self.store._sections(vc8_lines)
        fields = direct_fields(vc8_lines, sys_after)
        self.assertEqual(fields["SystemType"][0][1], "Grid")
        with self.assertRaises(ValueError):
            direct_fields(["PVObject_System=pvSystem\n", "  SystemUnavail=TUnavailability\n", "    PVObject_=pvUnavail\n",
                "End of PVObject pvSystem\n"], (0, 3))
        ps_res = create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC8", new_variant="VC9",
            battery={"file_name": "Generic_LI_Module_180Ah.BTR", "library": "builtin", "series_count": 1,
                "parallel_count": 2, "monthly_temperature_c": [20] * 12, "cycle_sow": 1, "static_sow": 1}, storage={**STORAGE, "strategy": "power_shift",
                "charging_strategy": "pv_prod_fraction", "charging_pv_fraction": 0.4,
                "charging_schedule_48": [0.0] * 18 + [0.4] * 16 + [0.0] * 14, "discharging_strategy": "specified_hours",
                "discharging_power_kw": 3.0, "discharging_schedule_48": [3.0] * 12 + [0.0] * 24 + [3.0] * 12}, load_manifest_name=None, dry_run=False)
        self.assertEqual(ps_res["status"], "created", ps_res)
    def test_storage_revision_unpack_grid_limit_bitmask_ac_no_nelements_and_random_unavail(self):
        from pvsyst_auto.energy.system import create_ac_cabling_variant
        from pvsyst_auto.energy.storage import grid_storage_state
        from pvsyst_auto.energy.profiles import replace_system_unavailability
        from pvsyst_auto.workflows.revision import prepare_storage_revision
        # 1. prepare_storage_revision succeeds with 3-tuple load_snapshot (no unpack error)
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + CLOSE)
        built = self.build_storage()
        self.assertEqual(built["status"], "created", built)
        rev = prepare_storage_revision(self.store, "Fixture.PRJ", "VC1", "VC2", {"soc_min": 0.25, "reserve_soc": 0.25}, "load_load.json", 2023)
        self.assertEqual(rev["status"], "ready", rev)
        self.assertEqual(rev["after"]["soc_min"], 0.25)
        # 2. Empty availability CSV fails with descriptive ValueError rather than empty StopIteration("")
        (self.root / "UserData/empty_avail.csv").write_text("", encoding="utf-8")
        bad_avail = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC1", new_variant="VC2",
            updates={"strategy": "weak_grid"},
            availability={"csv_name": "empty_avail.csv", "year": 2023, "utc_offset_hours": 8, "time_basis": "local_standard_interval_start"}, dry_run=True)
        self.assertEqual(bad_avail["status"], "blocked")
        self.assertIn("availability CSV", bad_avail["issues"][0])
        # 3. WeakGrid splicing survives VarList cache removal preceding pvGridStorage
        (self.root / "UserData/avail_ok.csv").write_text(hourly("Date;Grid Available", "ratio", "1", year=2023, first="0"), encoding="utf-8", newline="")
        vc1_text = (self.root / "Projects/Fixture.VC1").read_text(encoding="utf-8")
        vc1_cached = vc1_text.replace("  PVObject_GridStorage=pvGridStorage",
            "  VarList, list of=1 TVarSimul\r\n    VarSimul_1=E_Grid\r\n  End of the List VarList\r\n  PVObject_GridStorage=pvGridStorage")
        (self.root / "Projects/Fixture.VC1").write_bytes(vc1_cached.encode("utf-8"))
        wg = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC1", new_variant="VC2", updates={"strategy": "weak_grid"},
            availability={"csv_name": "avail_ok.csv", "year": 2023, "utc_offset_hours": 8, "time_basis": "local_standard_interval_start"}, dry_run=False)
        self.assertEqual(wg["status"], "created", wg)
        # 4. PeakShaving sets bit 24 (0x01000000 / 0x05000000), updates LimGrSeparLoss=False,
        #    and reads back PLimGrid without setting Constant PF bit 20 (0x00100000)
        pv_lim = ("PVObject_=pvVCalcul\n  Comment=Fixture\n  Version=8.1.7\n  Flags=$00\n"
            "  PVObject_PVMainArray=pvMainArray\n    Flags=$01900400\n    Flags2=$082A\n    MVNbTransfos=0\n"
            "    PVObject_SystemCircuit=pvCircuit\n      Version=8.1.6\n      Flags=$00\n"
            "      InjectionPointNode Start;\n        Children=Start\n          InverterNode Start;\n"
            "            SubArrayId=1\n            SubArrayName=PV Array\n"
            "            Children=Start\n              MPPTNode Start;\n                NElements=2\n"
            "                SubArrayId=1\n                SubArrayName=PV Array\n" "                Children=Start\n                  StringNode Start;\n"
            "                    SubArrayId=1\n                    SubArrayName=PV Array\n"
            "                  StringNode End;\n                Children=End\n              MPPTNode End;\n"
            "            Children=End\n          InverterNode End;\n        Children=End\n"
            "      InjectionPointNode End;\n    End of PVObject pvCircuit\n"
            "    PVObject_=pvSubArray\n      Comment=PV Array\n      Version=8.1.6\n      Flags=$019E1002\n"
            "      Flags2=$20\n      SubArrayId=1\n      NModSerie=10\n      NStringCh=2\n      NInverter=1\n"
            "      NInvMPPT=1\n      NoOrientation=1\n      NStrOrient1=2\n      ACWireLength=0.0\n"
            "    End of PVObject pvSubArray\n  End of PVObject pvMainArray\n"
            "  Orientations, list of=1 TOrientGroup\n    PVObject_=pvOrient\n      Version=8.1.6\n"
            "      Flags=$00\n      NoOrient=1\n      FieldType=FixedPlane\n      FieldTilt=20.0\n"
            "      FieldAzim=0.0\n      BaseSlope=0.0\n      UsesTiltAver=False\n"
            "      UsesAzimAver=False\n      UsesSlopeAver=False\n    End of TOrientGroup\n" "  End of Orientations\n"
            "  PVObject_System=pvSystem\n    Flags=$05000000\n    SystemType=Grid\n"
            "    PLimGrid=120.0\n    LimGrSeparLoss=True\n  End of PVObject pvSystem\n" "End of PVObject pvVCalcul\n")
        self.source(pv_lim)
        pk = create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC3", battery=BATTERY,
            storage={**STORAGE, "strategy": "peak_shaving", "charging_strategy": "pv_above_thresh", "discharging_strategy": "after_sunset"}, dry_run=False)
        self.assertEqual(pk["status"], "created", pk)
        vc3_lines = (self.root / "Projects/Fixture.VC3").read_text(encoding="utf-8").splitlines(True)
        _, sys3 = self.store._sections(vc3_lines)
        st3 = grid_storage_state(vc3_lines, sys3)
        self.assertEqual(st3["grid_limit_kw"], 120.0)
        self.assertIn("    LimGrSeparLoss=True\n", "".join(vc3_lines))
        # Inherit the injection-point limit without enabling constant PF.
        sys_flag_line = next(l for l in vc3_lines[sys3[0]:sys3[1]] if l.strip().startswith("Flags=$"))
        sys_flag_int = int(sys_flag_line.split("$")[1].strip(), 16)
        self.assertEqual(sys_flag_int & 0x07000000, 0x05000000)
        self.assertEqual(sys_flag_int & 0x00100000, 0)
        # 5. Sub-cent PowerShift scalar (0.004) survives roundtrip via decimal_text
        ps_tiny = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC3", new_variant="VC4",
            updates={"strategy": "power_shift", "charging_strategy": "pv_prod_fraction", "charging_pv_fraction": 0.004,
                "charging_schedule_48": [0.004] * 48, "discharging_strategy": "night", "discharging_power_kw": 0.004}, dry_run=False)
        self.assertEqual(ps_tiny["status"], "created", ps_tiny)
        self.assertEqual(ps_tiny["after"]["charging_pv_fraction"], 0.004)
        self.assertEqual(ps_tiny["after"]["discharging_power_kw"], 0.004)
        # 6. AC cabling works when InverterNode omits NElements=1 and pvMainArray omits UFAverEffic
        pv_plain = pv_lim.replace("Flags=$05000000\n    SystemType=Grid\n    PLimGrid=120.0\n    LimGrSeparLoss=True\n",
                                  "Flags=$00\n    SystemType=Grid\n").replace("Version=8.1.7", "Version=8.1.6")
        self.source(pv_plain)
        ac_res = create_ac_cabling_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC5",
            ac={"length_m": 25.0}, dry_run=False)
        self.assertEqual(ac_res["status"], "created", ac_res)
        self.assertEqual(ac_res["ac"]["length_m"], 25.0)
        self.assertEqual(ac_res["ac"]["inverter_count"], 1)
        # 7. Native DefMode=Random SystemUnavail with NbPeriods=3 and 0 DateBeg rows can be removed/replaced
        rand_lines = crlf("PVObject_System=pvSystem\n  Flags=$00010000\n  SystemType=Grid\n"
            "  SystemUnavail=TUnavailability\n    PVObject_=pvUnavail\n      Version=8.1.6\n      Flags=$00\n"
            "    DefMode=Random\n    RandomUnavSettings=TRandomUnavSettings\n      NbPeriods=3\n    EndTags\n"
            "  End of PVObject pvUnavail\nEnd of PVObject pvSystem\n")
        cleared = replace_system_unavailability(rand_lines, (0, len(rand_lines) - 1), {"mode": "none"})
        self.assertFalse(cleared["after"]["configured"])
    def test_peak_shaving_rejects_self_consumption_load_across_all_paths(self):
        from pvsyst_auto.native.load import create_native_load_variant
        target = self.root / "Projects/Fixture.VC1"
        source_path = self.root / "Projects/Fixture.VC0"
        system_kw = STORAGE_SYSTEM.replace("    SystemType=Grid\n",
            "    SystemType=Grid\n    PLimGrid=210.0\n    LimGrSeparLoss=True\n").replace("$10010040", "$15158040")
        # 1. SelfConsumption (with bit 19 set OR cleared, still carrying pvLoad) -> PeakShaving is blocked.
        for root_flags in ("$01080000", "$01000000"):
            vc_text = (ROOT.format(root_flags=root_flags) + ARRAY + system_kw + OLD_LOAD
                       + GRID_STORAGE.format(strategy="SelfConsumption", unavail="") + CLOSE)
            self.source(vc_text)
            before_bytes = source_path.read_bytes()
            for dry_run in (True, False):
                with self.subTest(case="self_consumption_to_peak", root_flags=root_flags, dry_run=dry_run):
                    res = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
                        updates={"strategy": "peak_shaving", "grid_limit_kw": 210.0}, dry_run=dry_run)
                    self.assertEqual(res["status"], "blocked", res)
                    self.assertRegex(res["issues"][0], r"PeakShaving.*self-consumption load")
                    self.assertFalse(target.exists())
                    self.assertEqual(source_path.read_bytes(), before_bytes)
        # 2. PowerShift carrying pvLoad -> PeakShaving is blocked; SelfConsumption with pvLoad -> PowerShift still works.
        ps_with_load = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC2",
            updates={"strategy": "power_shift", "charging_strategy": "pv_prod_fraction", "charging_pv_fraction": 0.3,
                "discharging_strategy": "night", "discharging_power_kw": 5.0}, dry_run=False)
        self.assertEqual(ps_with_load["status"], "created", ps_with_load)
        vc2_bytes = (self.root / "Projects/Fixture.VC2").read_bytes()
        blocked_ps = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC2", new_variant="VC1",
            updates={"strategy": "peak_shaving", "grid_limit_kw": 210.0}, dry_run=False)
        self.assertEqual(blocked_ps["status"], "blocked", blocked_ps)
        self.assertRegex(blocked_ps["issues"][0], r"PeakShaving.*self-consumption load")
        self.assertFalse(target.exists())
        self.assertEqual((self.root / "Projects/Fixture.VC2").read_bytes(), vc2_bytes)
        # 3. Existing PeakShaving variant carrying pvLoad rejects scalar edits and peak-shaving discharge edits.
        full_peak_with_load = ("PVObject_=pvVCalcul\n  Comment=Fixture\n  Version=8.1.7\n  Flags=$01080000\n"
            + "  PVObject_PVMainArray=pvMainArray\n    Flags=$01900400\n    Flags2=$082A\n"
            + "    PVObject_SystemCircuit=pvCircuit\n      Version=8.1.7\n      Flags=$00\n"
            + "      InjectionPointNode Start;\n        Children=Start\n          InverterNode Start;\n"
            + "            NElements=1\n            SubArrayId=1\n            SubArrayName=PV Array\n"
            + "            Children=Start\n              MPPTNode Start;\n                NElements=1\n"
            + "                SubArrayId=1\n                SubArrayName=PV Array\n"
            + "                Children=Start\n                  StringNode Start;\n"
            + "                    NElements=2\n                    SubArrayId=1\n"
            + "                    SubArrayName=PV Array\n                  StringNode End;\n"
            + "                Children=End\n              MPPTNode End;\n            Children=End\n"
            + "          InverterNode End;\n        Children=End\n      InjectionPointNode End;\n" + "    End of PVObject pvCircuit\n"
            + "    PVObject_=pvSubArray\n      Comment=PV Array\n      Version=8.1.7\n      Flags=$019E1002\n"
            + "      Flags2=$20\n      SubArrayId=1\n      NModSerie=10\n      NStringCh=2\n"
            + "      NInverter=1\n      NInvMPPT=1\n      NoOrientation=1\n      NStrOrient1=2\n"
            + "    End of PVObject pvSubArray\n  End of PVObject pvMainArray\n" + "  Orientations, list of=1 TOrientGroup\n"
            + "    PVObject_=pvOrient\n      Version=8.1.7\n      Flags=$00\n      NoOrient=1\n"
            + "      FieldType=FixedPlane\n      FieldTilt=20.0\n      FieldAzim=0.0\n" + "    End of TOrientGroup\n  End of Orientations\n"
            + system_kw + OLD_LOAD + GRID_STORAGE.format(strategy="PeakShaving", unavail="").replace("PVAvailable", "PVToGridMin") + CLOSE)
        self.source(full_peak_with_load)
        before_bytes = source_path.read_bytes()
        blocked_scalar = create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            updates={"initial_soc": 0.55}, dry_run=False)
        self.assertEqual(blocked_scalar["status"], "blocked", blocked_scalar)
        self.assertRegex(blocked_scalar["issues"][0], r"PeakShaving.*self-consumption load")
        self.assertFalse(target.exists())
        self.assertEqual(source_path.read_bytes(), before_bytes)
        # 4. Binding an hourly load onto an existing PeakShaving variant is blocked.
        clean_peak = (ROOT.format(root_flags="$00") + ARRAY + system_kw
            + GRID_STORAGE.format(strategy="PeakShaving", unavail="").replace("PVAvailable", "PVToGridMin") + CLOSE)
        self.source(clean_peak)
        before_bytes = source_path.read_bytes()
        for call in (lambda: create_load_variant(
                self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1", load_manifest_name="load_load.json", dry_run=False,
            ), lambda: create_native_load_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
                load={"mode": "hourly", "load_manifest_name": "load_load.json"}, dry_run=False)):
            blocked_load = call()
            self.assertEqual(blocked_load["status"], "blocked", blocked_load)
            self.assertRegex(blocked_load["issues"][0], r"PeakShaving.*self-consumption load")
            self.assertFalse(target.exists())
            self.assertEqual(source_path.read_bytes(), before_bytes)
        # 5. Creating PeakShaving from scratch with explicit load_manifest_name OR on a source with pvLoad is blocked.
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + CLOSE)
        before_bytes = source_path.read_bytes()
        blocked_sys_manifest = create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            battery=BATTERY, storage={**STORAGE, "strategy": "peak_shaving", "grid_limit_kw": 210.0}, load_manifest_name="load_load.json", dry_run=False)
        self.assertEqual(blocked_sys_manifest["status"], "blocked", blocked_sys_manifest)
        self.assertRegex(blocked_sys_manifest["issues"][0], r"PeakShaving.*self-consumption load")
        self.assertFalse(target.exists())
        self.assertEqual(source_path.read_bytes(), before_bytes)
        self.source(ROOT.format(root_flags="$00080000") + ARRAY + PV_SYSTEM + OLD_LOAD + CLOSE)
        before_bytes = source_path.read_bytes()
        blocked_sys_source_load = create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            battery=BATTERY, storage={**STORAGE, "strategy": "peak_shaving", "grid_limit_kw": 210.0}, load_manifest_name=None, dry_run=False)
        self.assertEqual(blocked_sys_source_load["status"], "blocked", blocked_sys_source_load)
        self.assertRegex(blocked_sys_source_load["issues"][0], r"PeakShaving.*self-consumption load")
        self.assertFalse(target.exists())
        self.assertEqual(source_path.read_bytes(), before_bytes)
    def test_storage_grid_limit_preservation_precision_and_kva_guards(self):
        target = self.root / "Projects/Fixture.VC1"
        def setup_limit(flags, limit, *, existing=True):
            target.unlink(missing_ok=True)
            system = STORAGE_SYSTEM.replace("$10010040", flags) if existing else PV_SYSTEM.replace("$00", flags)
            system = system.replace("    SystemType=Grid\n", "    SystemType=Grid\n" + limit)
            peak = GRID_STORAGE.format(strategy="PeakShaving", unavail="").replace("PVAvailable", "PVToGridMin")
            self.source(ROOT.format(root_flags="$00") + ARRAY + system + (peak if existing else "") + CLOSE)
            return system
        def edit_limit(**updates):
            return create_grid_storage_variant(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1", updates=updates, dry_run=False)
        def build_limit(**updates):
            return create_grid_storage_system(self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
                battery={**BATTERY, "library": "builtin"}, storage={**STORAGE, "strategy": "peak_shaving", **updates}, dry_run=False)
        # 1. Unrelated storage edits preserve exact PLimGrid text and unit (kW vs kVA)
        for flags, key, value in (("$11100040", "grid_limit_kw", "6000.000"),
            ("$15100040", "grid_limit_kw", "6000.000"), ("$17158040", "grid_limit_kva", "330.000")):
            for updates in ({"initial_soc": 0.55}, {"discharging_strategy": "after_sunset"}):
                with self.subTest(case="unrelated", flags=flags, updates=updates):
                    system = setup_limit(flags, f"    PLimGrid={value}\n    LimGrSeparLoss=True\n")
                    result = edit_limit(**updates)
                    self.assertEqual(result["status"], "created", result)
                    self.assertIn(system.replace("\n", "\r\n").encode(), target.read_bytes())
                    for state in (result["before"], result["after"]):
                        self.assertEqual({k: v for k, v in state.items() if k.startswith("grid_limit_")}, {key: float(value)})
        # 2. Explicit kW edits preserve active limit or start at injection
        for flags, expected_flags, separate_loss in (("$11100040", "$11100040", "True"),
            ("$15100040", "$15100040", "True"), ("$12100040", "$15100040", "False")):
            with self.subTest(case="explicit_kw", flags=flags):
                setup_limit(flags, "    PLimGrid=330.000\n    LimGrSeparLoss=True\n")
                result = edit_limit(grid_limit_kw=331)
                self.assertEqual(result["status"], "created", result)
                self.assertEqual(result["after"]["system_flags"], expected_flags)
                self.assertEqual(result["after"]["grid_limit_kw"], 331)
                self.assertIn(f"    PLimGrid=331.0\n    LimGrSeparLoss={separate_loss}\n", target.read_text(encoding="utf-8"))
        # 3. Native 0.1 kW increment precision guard
        for existing, flags in ((True, "$11100040"), (False, "$01100000")):
            with self.subTest(case="precision", existing=existing):
                setup_limit(flags, "    PLimGrid=6000.0\n    LimGrSeparLoss=False\n", existing=existing)
                save = edit_limit if existing else build_limit
                blocked = save(grid_limit_kw=330.05)
                self.assertEqual(blocked["status"], "blocked", blocked)
                self.assertIn("Use 0.1 kW increments", blocked["issues"][0])
                self.assertFalse(target.exists())
                created = save(grid_limit_kw=330.1)
                self.assertEqual(created["status"], "created", created)
                self.assertIn("    PLimGrid=330.1\n", target.read_text(encoding="utf-8"))
        # 4. Active kVA rejects explicit kW on both storage paths
        for flags, existing in (("$17158040", True), ("$07158000", False)):
            with self.subTest(case="kva_rejects_kw", existing=existing):
                setup_limit(flags, "    PLimGrid=330.0\n    LimGrSeparLoss=True\n", existing=existing)
                result = (edit_limit if existing else build_limit)(grid_limit_kw=331)
                self.assertEqual(result["status"], "blocked", result)
                self.assertRegex(result["issues"][0], r"apparent.*kVA.*grid_limit_kw")
                self.assertFalse(target.exists())
        # 5. New PeakShaving inherits active kW limit, but blocks inheriting kVA limit as kW
        setup_limit("$05100000", "    PLimGrid=6000.000\n    LimGrSeparLoss=True\n", existing=False)
        res_kw = build_limit()
        self.assertEqual(res_kw["status"], "created", res_kw)
        self.assertEqual(res_kw["configuration"]["system_flags"], "$15100040")
        self.assertEqual(res_kw["configuration"]["grid_limit_kw"], 6000)
        self.assertIn("    PLimGrid=6000.000\n    LimGrSeparLoss=True\n", target.read_text(encoding="utf-8"))
        setup_limit("$07158000", "    PLimGrid=330.0\n    LimGrSeparLoss=True\n", existing=False)
        res_kva = build_limit()
        self.assertEqual(res_kva["status"], "blocked", res_kva)
        self.assertRegex(res_kva["issues"][0], r"apparent.*kVA.*grid_limit_kw")
        self.assertFalse(target.exists())
JKM600_PAN = 'PVObject_=pvModule\n  Version=7.2\n  Flags=$00900043\n  PVObject_Commercial=pvCommercial\n    Flags=$0041\n    Manufacturer=Jinkosolar\n    Model=JKM600N-78HL4-BDV\n    Width=1.134\n    Height=2.465\n    Depth=0.030\n  End of PVObject pvCommercial\n  Technol=mtSiMono\n  NCelS=78\n  NCelP=2\n  NDiode=3\n  SubModuleLayout=slTwinHalfCells\n  GRef=1000\n  TRef=25.0\n  PNom=600\n  Isc=13.87\n  Voc=55.03\n  Imp=13.26\n  Vmp=45.25\n  RShunt=1100\n  RSerie=0.255\n  PVObject_IAM=pvIAM\n    Flags=$00\n  End of PVObject pvIAM\nEnd of PVObject pvModule\n'
OND = 'PVObject_=pvOnduleur\n  Version=8.1.6\n  Flags=$00\n  PVObject_Commercial=pvCommercial\n    Flags=$0041\n    Manufacturer=Solis\n    Model=Solis-50K\n  End of PVObject pvCommercial\n  PNomConv=50.0\nEnd of PVObject pvOnduleur\n'
VC_COLD = 'PVObject_=pvVCalcul\n  Comment=Base\n  Version=8.1.6\n  Flags=$00\n  Orientations, list of=1 TOrientGroup\n    PVObject_=pvOrient\n      Version=8.1.6\n      Flags=$00\n      NoOrient=1\n      FieldType=FixedPlane\n      FieldTilt=25.0\n      FieldAzim=20.0\n      BaseSlope=0.0\n      UsesTiltAver=False\n      UsesAzimAver=False\n      UsesSlopeAver=False\n    End of TOrientGroup\n  End of Orientations\n  PVObject_PVMainArray=pvMainArray\n    Flags=$00100400\n    Flags2=$082A\n    MVNbTransfos=0\n    PVObject_SystemCircuit=pvCircuit\n      Version=8.1.6\n      Flags=$00\n      InjectionPointNode Start;\n        Children=Start\n          InverterNode Start;\n            NElements=1\n            SubArrayId=1\n            SubArrayName=PV Array\n            Children=Start\n              MPPTNode Start;\n                NElements=4\n                SubArrayId=1\n                SubArrayName=PV Array\n                Children=Start\n                  StringNode Start;\n                    NElements=1\n                    SubArrayId=1\n                    SubArrayName=PV Array\n                  StringNode End;\n                Children=End\n              MPPTNode End;\n            Children=End\n          InverterNode End;\n        Children=End\n      InjectionPointNode End;\n    End of PVObject pvCircuit\n    SubArrays=Start\n      PVObject_=pvSubArray\n        Comment=PV Array\n        Version=8.1.6\n        Flags=$001E0402\n        Flags2=$20\n        SubArrayId=1\n        PVModule={pan}\n        GInverter=Solis50.OND\n        NoOrientation=1\n        NModSerie=15\n        NStringCh=4\n        NStrOrient1=4\n        NInverter=1\n        NInvMPPT=4\n        RSCablageCh=378.105\n        RSCablagePC=1.50\n        ACWireLength=0.0\n      End of PVObject pvSubArray\n    EndTags\n  End of PVObject pvMainArray\n  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n  End of PVObject pvSystem\nEnd of PVObject pvVCalcul\n'
class HeadlessDCCablingTests(unittest.TestCase):
    def _setup_ws(self, *, pan_name="JKM600.PAN", pan_bytes=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = Path(tmp.name) / "ws"
        (ws / "Projects").mkdir(parents=True)
        (ws / "ComposPV" / "PVmodules").mkdir(parents=True)
        (ws / "ComposPV" / "Inverters").mkdir(parents=True)
        (ws / "Projects" / "Demo.PRJ").write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        (ws / "Projects" / "Demo.VC0").write_bytes(VC_COLD.format(pan=pan_name).replace("\n", "\r\n").encode("utf-8"))
        if pan_bytes is not None: (ws / "ComposPV" / "PVmodules" / pan_name).write_bytes(pan_bytes)
        (ws / "ComposPV" / "Inverters" / "Solis50.OND").write_text(OND.replace("\n", "\r\n"), encoding="utf-8")
        comps = ComponentStore(ws)
        return ws, VariantStore(ws, comps)
    def test_percent_mode_computes_one_diode_resistance_and_inserts_cable_definition(self):
        ws, store = self._setup_ws(pan_bytes=JKM600_PAN.encode("utf-8"))
        preview = create_dc_cabling_variant(store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
            dc={"mode": "percent", "value": 1.5}, dry_run=True, variant_name="DC 1.50pct")
        self.assertEqual(preview["status"], "ready")
        self.assertFalse((ws / "Projects" / "Demo.VC1").exists())
        res = create_dc_cabling_variant(store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
            dc={"mode": "percent", "value": 1.5}, dry_run=False, variant_name="DC 1.50pct")
        self.assertEqual(res["status"], "created")
        self.assertEqual(res["dc"]["mode"], "percent")
        self.assertEqual(res["dc"]["loss_percent"], "1.50")
        self.assertTrue(res["dc"]["cable_definition_present"])
        # 1-diode R_array(1.5%) for JKM600 (Ns=15, Nstr=4) is ~193.7 mOhm (within 0.6% of PVsyst 192.688 mOhm)
        r_val = float(res["dc"]["resistance_mohm"])
        self.assertAlmostEqual(r_val, 193.699, delta=1.5)
        self.assertEqual(res["pan_stc"]["source"], "one_diode_stc")
        # Update VC1 -> VC2 switching to resistance=500 mOhm
        res2 = create_dc_cabling_variant(store, project="Demo.PRJ", source_variant="VC1", new_variant="VC2",
            dc={"mode": "resistance", "value": 500}, dry_run=False, variant_name="DC 500mOhm")
        self.assertEqual(res2["status"], "created")
        self.assertEqual(res2["dc"]["mode"], "resistance")
        self.assertEqual(res2["dc"]["resistance_mohm"], "500.000")
        self.assertAlmostEqual(float(res2["dc"]["loss_percent"]), 3.87, delta=0.05)
        txt2 = (ws / "Projects" / "Demo.VC2").read_text(encoding="utf-8-sig")
        self.assertIn("Flags=$01900400", txt2)
        self.assertIn("Flags=$019E0402", txt2)
        self.assertIn("Resistance=500 m\u03a9", txt2)
        self.assertEqual(dc_cabling_state(txt2.splitlines(True))["mode"], "resistance")
    def test_builtin_unreadable_pan_allows_resistance_and_rejects_percent(self):
        # Legacy binary PAN or missing built-in DB PAN
        for pan_bytes in (b"pvModule;binary-unreadable", None):
            ws, store = self._setup_ws(pan_name="BuiltinOnly.PAN", pan_bytes=pan_bytes)
            bad = create_dc_cabling_variant(store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
                dc={"mode": "percent", "value": 1.5}, dry_run=True)
            self.assertEqual(bad["status"], "blocked")
            self.assertIn("mode='resistance'", bad["issues"][0])
            good = create_dc_cabling_variant(store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
                dc={"mode": "resistance", "value": 500}, dry_run=False, variant_name="Builtin Res")
            self.assertEqual(good["status"], "created")
            self.assertEqual(good["dc"]["resistance_mohm"], "500.000")
            self.assertEqual(good["dc"]["loss_percent"], "0.00")
            self.assertIsNone(good["pan_stc"])
            self.assertTrue(any("0.00" in w for w in good["warnings"]))
    def test_rejects_excessive_resistance_when_loss_hits_99_percent(self):
        _, store = self._setup_ws(pan_bytes=JKM600_PAN.encode("utf-8"))
        res = create_dc_cabling_variant(store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
            dc={"mode": "resistance", "value": 15000}, dry_run=False)
        self.assertEqual(res["status"], "blocked")
        self.assertIn("99%", res["issues"][0])
        for pct in (99.0, 99.5, 100.0):
            bad_pct = create_dc_cabling_variant(store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
                dc={"mode": "percent", "value": pct}, dry_run=True)
            self.assertEqual(bad_pct["status"], "blocked")
            self.assertIn("99", bad_pct["issues"][0])
    def test_resistance_rejects_loss_that_rounds_up_to_99_percent(self):
        ws, store = self._setup_ws(pan_bytes=JKM600_PAN.encode("utf-8"))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = create_dc_cabling_variant(store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
                    dc={"mode": "resistance", "value": 12783.754}, dry_run=dry_run)
                self.assertEqual(result["status"], "blocked", result)
                self.assertIn("99.00%", result["issues"][0])
                self.assertFalse((ws / "Projects" / "Demo.VC1").exists())
        result = create_dc_cabling_variant(store, project="Demo.PRJ", source_variant="VC0", new_variant="VC2",
            dc={"mode": "resistance", "value": 12783.300}, dry_run=False)
        self.assertEqual(result["status"], "created", result)
        self.assertEqual(result["dc"]["loss_percent"], "98.99")
    def test_resistance_disagreement_is_readable_but_not_editable(self):
        ws, store = self._setup_ws()
        source = ws / "Projects/Demo.VC0"
        text = source.read_text(encoding="utf-8").replace("                    SubArrayId=1\n",
            "                    CableDefinition=Start\n" "                      Resistance=206.577633418868 m\u03a9\n"
            "                    CableDefinition=End\n" "                    SubArrayId=1\n")
        rounded = dc_cabling_state(text.replace("378.105", "206.578").splitlines(True))
        self.assertNotIn("issues", rounded)
        original = text.replace("378.105", "13.621").encode("utf-8")
        source.write_bytes(original)
        state = store.inspect_parameters("Demo.PRJ", "VC0")["dc_cabling"]
        self.assertEqual((state["mode"], state["resistance_mohm"], state["circuit_resistance_mohm"], state["resistance_difference_mohm"]),
                         ("resistance", "13.621", "206.577633418868", "192.956633418868"))
        blocked = create_dc_cabling_variant(store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
            dc={"mode": "resistance", "value": 500}, dry_run=False)
        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assertEqual(blocked["issues"], state["issues"])
        self.assertFalse((source.parent / "Demo.VC1").exists())
        self.assertEqual(source.read_bytes(), original)
    def test_cold_and_legacy_flag_readback_supported(self):
        ws, _ = self._setup_ws(pan_bytes=JKM600_PAN.encode("utf-8"))
        lines = (ws / "Projects" / "Demo.VC0").read_text(encoding="utf-8-sig").splitlines(True)
        cold = dc_cabling_state(lines)
        self.assertEqual(cold["mode"], "resistance")
        self.assertFalse(cold["cable_definition_present"])
        self.assertIsNone(cold["circuit_resistance_mohm"])
        self.assertEqual(Decimal(cold["resistance_mohm"]), Decimal("378.105"))
class SpecialLoadsAndOutagesTests(unittest.TestCase):
    def test_household_schema_default_slot(self) -> None:
        from pvsyst_auto.api.schemas import HouseholdNativeLoad
        load = HouseholdNativeLoad(mode="household", appliances=[{"name": "Lamp", "power_w": 20, "hours": [18]}])
        self.assertIn("HouseAppliance, NoApp=1", native_load_block(load.model_dump(), "\n"))
    def test_household_and_probability_load_serialization_and_roundtrip(self) -> None:
        # 1. Yearly HouseHold load
        hh_yearly = native_load_block({"mode": "household", "modulation": "yearly", "appliances": [
                {"name": "LED Lighting", "count": 8, "power_w": 15.0, "hours": [18, 19, 20, 21, 22]},
                {"name": "Fridge", "count": 1, "power_w": 120.0, "hours": list(range(24))}]}, "\r\n")
        self.assertIn("LoadType=HouseHold", hh_yearly)
        self.assertIn("UsesModul=Yearly", hh_yearly)
        self.assertIn("HoursDefined=16777215,16777215,", hh_yearly)
        # Verify flat_load round-trip recognizes HouseHold inside a root pvVCalcul wrapper
        wrapped_hh = f"PVObject_=pvVCalcul\r\n{hh_yearly}End of PVObject pvVCalcul\r\n"
        parsed_hh = flat_load(wrapped_hh.splitlines(keepends=True))
        self.assertIsNotNone(parsed_hh)
        self.assertEqual(parsed_hh[2], "HouseHold")
        # 2. Seasonal HouseHold load
        app = [{"name": "Lamp", "count": 2, "power_w": 20.0, "half_hours": [36, 37, 38, 39]}]
        hh_seas = native_load_block({"mode": "household",
            "modulation": "seasonal", "seasons": {"Summer": app, "Autumn": app, "Winter": app, "Spring": app}}, "\n")
        self.assertIn("UsesModul=Seasonal", hh_seas)
        self.assertIn("Appliances for period=Winter", hh_seas)
        # 3. Probability load (deterministically sampled into native embedded HourlyFile snapshot)
        prob_text = native_load_block({"mode": "probability", "power_step_kw": 2.5, "probabilities_pct": [10.0, 25.0, 35.0, 20.0, 10.0],
            "seed": 42}, "\r\n")
        self.assertIn("LoadType=HourlyFile", prob_text)
        self.assertIn("Day_0101=", prob_text)
        wrapped_pr = f"PVObject_=pvVCalcul\r\n{prob_text}End of PVObject pvVCalcul\r\n"
        parsed_pr = flat_load(wrapped_pr.splitlines(keepends=True))
        self.assertIsNotNone(parsed_pr)
        self.assertEqual(parsed_pr[2], "HourlyFile")
        with self.assertRaisesRegex(ValueError, "sum to 100.0%"):
            native_load_block({"mode": "probability", "power_step_kw": 2.0, "probabilities_pct": [30.0, 30.0]}, "\n")
    def test_generated_defaults_pass_through_storage_api(self) -> None:
        import os
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from pvsyst_auto.api import context as api_context
        from pvsyst_auto.api.schemas import GridAvailabilityProfile, GridStorageUpdates
        from pvsyst_auto.api.simulation import pvsyst_create_grid_storage_variant
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            (ws / "Projects").mkdir(parents=True)
            (ws / "ComposPV" / "Batteries").mkdir(parents=True)
            (ws / "Projects" / "Fixture.PRJ").write_text("PVObject_=pvProject\n  Comment=Fixture\nEnd of PVObject pvProject\n", encoding="utf-8")
            (ws / "Projects" / "Fixture.VC0").write_bytes((ROOT.format(root_flags="$00080000") + ARRAY + STORAGE_SYSTEM + SNAPSHOT_LOAD
                 + GRID_STORAGE.format(strategy="WeakGrid", unavail=OLD_UNAVAIL) + CLOSE) .replace("\n", "\r\n").encode("utf-8"))
            (ws / "ComposPV" / "Batteries" / "Fixture.BTR").write_text(BTR, encoding="utf-8", newline="")
            stub_cli = Path(td) / "PVsystCLI.exe"
            stub_cli.write_bytes(b"stub")
            declaration = GridAvailabilityProfile(mode="generated", year=2023, outage_count=4)
            api_context._cli = None
            try:
                with patch.dict(os.environ, {"PVSYST_WORKSPACE": str(ws), "PVSYST_CLI": str(stub_cli)}):
                    result = pvsyst_create_grid_storage_variant("Fixture.PRJ", "VC0", "VC1", GridStorageUpdates(strategy="weak_grid"),
                        dry_run=False, availability=declaration)
            finally:
                api_context._cli = None
        self.assertEqual((result["status"], result["grid_availability"]["outage_count"], result["grid_availability"]["seed"]), ("created", 4, 42))
    def test_generated_weak_grid_outage_availability_is_deterministic(self) -> None:
        decl = {"mode": "generated", "year": 2024, "outage_count": 20,
            "min_duration_hours": 3, "max_duration_hours": 6, "peak_hours": [18, 19, 20], "seed": 123}
        txt1, meta1 = native_availability(None, decl, "\r\n")
        txt2, meta2 = native_availability(None, decl, "\r\n")
        self.assertEqual(txt1, txt2)
        self.assertEqual(meta1["rows"], 8784)  # 2024 leap year
        self.assertGreater(meta1["unavailable_hours"], 40)
        self.assertIn("PVObject_GridUnavail=pvUnavail", txt1)
        self.assertIn("+0", txt1)
        self.assertIn("+G8", txt1)
    def test_generated_availability_preserves_declared_standard_offset(self) -> None:
        from pvsyst_auto.energy.profiles import TIME_BASIS
        decl = {'mode': 'generated', 'year': 2023, 'outage_count': 4, 'utc_offset_hours': 8}
        _, metadata = native_availability(None, decl, '\n')
        self.assertEqual(metadata['utc_offset_hours'], 8)
        self.assertEqual(metadata['time_basis'], TIME_BASIS)
    def test_random_system_unavailability_roundtrip(self) -> None:
        lines = ["PVObject_System=pvSystem\r\n", "  Flags=$00\r\n", "  SystemType=Grid\r\n", "End of PVObject pvSystem\r\n"]
        res = replace_system_unavailability(lines, (0, 3),
            {"mode": "random", "time_fraction_pct": 1.0, "period_count": 6, "min_duration_hours": 4, "max_duration_hours": 24, "random_seed": 42})
        self.assertEqual(res["after"]["mode"], "Periods")
        self.assertEqual(res["after"]["period_count"], 6)
        self.assertEqual(res["after"]["settings"]["NbPeriods"], "6")
        self.assertEqual(res["after"]["settings"]["TimeFraction"], "0.0100")
        self.assertAlmostEqual(sum(float(p['duration']) for p in res['after']['periods']), 87.6, places=1)
        joined = "".join(lines)
        self.assertIn("UnavailPeriods=Start", joined)
        self.assertIn("RandomInit =42", joined)
        self.assertIn("Flags=$00010000", joined)
        # Removal cleanly restores state
        rm = replace_system_unavailability(lines, (0, len(lines) - 1), {"mode": "none"})
        self.assertFalse(rm["after"]["configured"])
    def test_random_system_unavailability_rejects_infeasible_total(self) -> None:
        lines = ['PVObject_System=pvSystem\n', '  Flags=$00\n', '  SystemType=Grid\n', 'End of PVObject pvSystem\n']
        original = list(lines)
        with self.assertRaisesRegex(ValueError, 'time_fraction_pct'):
            replace_system_unavailability(lines, (0, 3), {'mode': 'random', 'time_fraction_pct': 10, 'period_count': 2,
                'min_duration_hours': 1, 'max_duration_hours': 5})
        self.assertEqual(lines, original)
    def test_unavailability_exact_boundary_and_availability_guards(self) -> None:
        # 1. Exact feasible boundary (5 * 5h = 25h) survives IEEE-754 float rounding
        lines = ['PVObject_System=pvSystem\n', '  Flags=$00\n', '  SystemType=Grid\n', 'End of PVObject pvSystem\n']
        res = replace_system_unavailability(lines, (0, 3), {'mode': 'random', 'time_fraction_pct': 100.0 * 25.0 / 8760.0, 'period_count': 5,
            'min_duration_hours': 5, 'max_duration_hours': 10, 'random_seed': 7})
        self.assertEqual(res['after']['period_count'], 5)
        # 2. Generated availability rejects out-of-range utc_offset_hours and unsupported time_basis
        with self.assertRaisesRegex(ValueError, 'utc_offset_hours'):
            native_availability(None, {'mode': 'generated', 'year': 2024, 'outage_count': 2, 'utc_offset_hours': 999}, '\n')
        with self.assertRaisesRegex(ValueError, 'time_basis'):
            native_availability(None, {'mode': 'generated', 'year': 2024, 'outage_count': 2, 'time_basis': 'dst_local'}, '\n')
        # 3. Explicit mode='csv' must not fall back to generated mode or raise KeyError when csv_name is missing
        with self.assertRaisesRegex(ValueError, 'csv_name'):
            native_availability(None,
                {'mode': 'csv', 'year': 2024, 'utc_offset_hours': 0, 'time_basis': 'local_standard_interval_start', 'outage_count': 4}, '\n')
    def test_random_unavailability_hits_exact_target_fraction_and_full_year(self) -> None:
        """Issue #275: random outages hit exact target hours and allow 01/01 00:00 full-year outage."""
        lines_80 = crlf(SYSTEM)
        res_80 = replace_system_unavailability(lines_80,
            (2, 5), {"mode": "random", "time_fraction_pct": 80, "period_count": 4, "min_duration_hours": 1, "max_duration_hours": 3000, "random_seed": 7})
        periods_80 = res_80["after"]["periods"]
        self.assertEqual(len(periods_80), 4)
        total_hours = 0.0
        intervals = []
        for entry in periods_80:
            dur = float(entry["duration"])
            self.assertGreaterEqual(dur, 1.0)
            self.assertLessEqual(dur, 3000.0)
            total_hours += dur
            start = datetime.strptime(entry["date_beg"], "%d/%m/%y %H:%M")
            end = start + timedelta(hours=dur)
            intervals.append((start, end))
        self.assertAlmostEqual(total_hours, 7008.0, places=6)
        intervals.sort()
        for prev, curr in zip(intervals, intervals[1:]): self.assertLessEqual(prev[1], curr[0])
        lines_100 = crlf(SYSTEM)
        res_100 = replace_system_unavailability(lines_100, (2, 5),
            {"mode": "random", "time_fraction_pct": 100, "period_count": 1, "min_duration_hours": 8760, "max_duration_hours": 8760, "random_seed": 7})
        periods_100 = res_100["after"]["periods"]
        self.assertEqual(len(periods_100), 1)
        self.assertEqual(periods_100[0]["date_beg"], "01/01/90 00:00")
        self.assertAlmostEqual(float(periods_100[0]["duration"]), 8760.0, places=6)
if __name__ == "__main__": unittest.main()

"""Tests for energy loss blocks, hourly load snapshots, and grid storage builders."""
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
from pvsyst_auto.energy import auxiliaries, iam, inverter_temperature, soiling
from pvsyst_auto.energy.load import create_load_variant
from pvsyst_auto.energy.peak import peak_shaving_state
from pvsyst_auto.energy.profiles import prepare_load_profile
from pvsyst_auto.energy.storage import (
    create_grid_storage_variant,
    decode_subhour_schedule,
    encode_subhour_schedule,
)
from pvsyst_auto.energy.system import create_grid_storage_system
from pvsyst_auto.energy.unavailability import system_unavail_state
from pvsyst_auto.project.variants import VariantStore


def crlf(text):
    return text.replace("\n", "\r\n").splitlines(keepends=True)


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
ASHRAE = ("  PVObject_IAM=pvIAM\n    Flags=$00\n    IAMMode=Ashrae\n    B0_IAM=0.050\n"
          "  End of PVObject pvIAM\n")
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


@functools.lru_cache(maxsize=8)
def hourly(header, unit, value, year=2023, first=None):
    start = datetime(year, 1, 1)
    hours = 24 * (366 if calendar.isleap(year) else 365)
    return "".join([header + "\n", ";" + unit + "\n"] + [
        (start + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M") + ";" + (first if first and h < 24 else value) + "\n"
        for h in range(hours)])


class EnergyBlockTests(unittest.TestCase):
    def test_soiling_noop_and_edit(self):
        for block, updates in ((NONE_SOILING, {"mode": "none"}),
                               (CACHED_SOILING, {"mode": "constant", "yearly_loss_pct": 2.5})):
            lines = crlf(MAIN.format(soiling=block))
            original = list(lines)
            change = soiling.replace_soiling(lines, (0, len(lines) - 1), updates)
            self.assertEqual(lines, original)
            self.assertEqual(change["before"], change["after"])
        lines = crlf(MAIN.format(soiling=""))
        change = soiling.replace_soiling(lines, (0, len(lines) - 1), {"mode": "constant", "yearly_loss_pct": 2.5})
        self.assertEqual(change["after"], {"mode": "constant", "yearly_loss_pct": 2.5})
        self.assertEqual(lines[2:8], crlf("Soiling=Soiling\n  IsYearly=True\n  IsMonthly=False\n"
                                          "  IsFromFile=False\n  YearlyValue=0.025\nEndTags\n"))

    def test_auxiliaries_and_inverter_temperature_edits(self):
        cases = (
            (auxiliaries.replace_auxiliaries, {"mode": "constant", "operating_kw": 5, "night_kw": 2},
             ["    AuxPower_Const=5000.000\r\n", "    AuxPower_Night=2000.000\r\n"], "$00040000"),
            (inverter_temperature.replace_inverter_temperature, {"mode": "offset", "offset_c": 2.5, "fixed_c": 30},
             ["    TempShift=2.5\r\n", "    TempFixed=30.0\r\n"], "$4000"),
        )
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
        for folder in ("Projects", "UserData", "ComposPV/Batteries"):
            (seed / folder).mkdir(parents=True)
        (seed / "Projects/Fixture.PRJ").write_text(
            "PVObject_=pvProject\n  Comment=Fixture\nEnd of PVObject pvProject\n", encoding="utf-8")
        (seed / "ComposPV/Batteries/Fixture.BTR").write_text(BTR, encoding="utf-8", newline="")
        (seed / "UserData/raw.csv").write_text(hourly("Date;P Load", "W", "2500"), encoding="utf-8", newline="")
        cli = SimpleNamespace(_lock=threading.RLock(), workspace=seed)
        prepared = prepare_load_profile(cli, csv_name="raw.csv", year=2023, output_name="load.csv",
                                        utc_offset_hours=8, time_basis="local_standard_interval_start",
                                        dry_run=False)
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
        for line in ("SOCDischDecl=0.00001", "PMaxCharge=10000000000000000", "PMaxDisch=1.0", "BattSoCInit=0.4"):
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
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + "\n" + OLD_LOAD + CLOSE)
        text = self.created(self.bind_load())
        self.assertNotIn("+oldcache", text)
        self.assertIn("  End of PVObject pvSystem\r\n\r\n  PVObject_UserNeeds=pvLoad\r\n", text)
        self.assertTrue(text.endswith("  End of PVObject pvLoad\r\nEnd of PVObject pvVCalcul\r\n"))

        (self.root / "Projects/Fixture.VC1").unlink()
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + "  " + CLOSE)
        inserted = self.created(self.bind_load())
        self.assertIn("  End of PVObject pvSystem\r\n  PVObject_UserNeeds=pvLoad\r\n", inserted)
        self.assertTrue(inserted.endswith("  End of PVObject pvLoad\r\n  End of PVObject pvVCalcul\r\n"))

        (self.root / "Projects/Fixture.VC1").unlink()
        self.source(ROOT.format(root_flags="$00") + ARRAY + PV_SYSTEM + CLOSE)
        prepared = self.root / "UserHourlyParams/load.csv"
        prepared.write_bytes(prepared.read_bytes() + b"\r\n")
        blocked = self.bind_load()
        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assertEqual(blocked["issues"], ["Every prepared load row must be one Date;P Load pair"])
        self.assertFalse((self.root / "Projects/Fixture.VC1").exists())

    def test_system_unavailability_authoring_and_geometry_combo(self):
        from pvsyst_auto.energy.unavailability import replace_system_unavailability
        from pvsyst_auto.native.load import create_native_load_variant
        from pvsyst_auto.energy.peak import create_peak_shaving_variant

        lines = crlf(SYSTEM)
        added = replace_system_unavailability(
            lines, (2, 5),
            {"mode": "periods", "periods": [
                {"date_beg": "10/01/23 08:00", "duration_hours": 12.5},
                {"date_beg": "15/06/23 00:00", "duration_hours": 24},
            ]},
        )
        self.assertTrue(added["after"]["configured"])
        self.assertEqual(added["after"]["mode"], "Periods")
        self.assertEqual(added["after"]["period_count"], 2)
        self.assertIn("    Flags=$00010000\r\n", lines)
        self.assertIn("    SystemUnavail=TUnavailability\r\n", lines)
        self.assertIn("        DateBeg=10/01/23 08:00\r\n", lines)
        self.assertIn("        Duration=12.500\r\n", lines)

        replaced = replace_system_unavailability(
            lines, (2, len(lines) - 2),
            {"mode": "periods", "periods": [{"date_beg": "01/03/23 09:00", "duration_hours": 4}]},
        )
        self.assertEqual(replaced["before"]["period_count"], 2)
        self.assertEqual(replaced["after"]["period_count"], 1)

        removed = replace_system_unavailability(lines, (2, len(lines) - 2), {"mode": "none"})
        self.assertFalse(removed["after"]["configured"])
        self.assertIn("    Flags=$00000000\r\n", lines)
        self.assertNotIn("SystemUnavail", "".join(lines))

        for bad in (
            {"mode": "periods", "periods": []},
            {"mode": "periods", "periods": [{"date_beg": "31/02/23 00:00", "duration_hours": 4}]},
            {"mode": "periods", "periods": [{"date_beg": "01/01/23 00:00", "duration_hours": 0}]},
            {"mode": "none", "periods": [{"date_beg": "01/01/23 00:00", "duration_hours": 1}]},
        ):
            with self.assertRaises(ValueError):
                replace_system_unavailability(list(lines), (2, len(lines) - 2), bad)

        # Native load and peak shaving now compose cleanly with an active 3D shading scene + VarList
        circuit = (
            "    PVObject_SystemCircuit=pvCircuit\n      Version=8.1.6\n      Flags=$00\n"
            "      InjectionPointNode Start;\n        Children=Start\n          InverterNode Start;\n"
            "            NElements=1\n            SubArrayId=1\n            SubArrayName=PV Array\n"
            "            Children=Start\n              MPPTNode Start;\n                NElements=1\n"
            "                SubArrayId=1\n                SubArrayName=PV Array\n"
            "                Children=Start\n                  StringNode Start;\n"
            "                    NElements=2\n                    SubArrayId=1\n"
            "                    SubArrayName=PV Array\n                  StringNode End;\n"
            "                Children=End\n              MPPTNode End;\n            Children=End\n"
            "          InverterNode End;\n        Children=End\n      InjectionPointNode End;\n"
            "    End of PVObject pvCircuit\n"
        )
        shaded = (
            "PVObject_=pvVCalcul\n  Comment=Fixture\n  Version=8.1.6\n  Flags=$00100001\n"
            "  PVObject_PVMainArray=pvMainArray\n    Flags=$01900400\n    Flags2=$082A\n"
            + circuit
            + "    PVObject_=pvSubArray\n      Comment=PV Array\n      Version=8.1.6\n      Flags=$019E1002\n"
            "      SubArrayId=1\n      NModSerie=10\n      NStringCh=2\n      NInverter=1\n"
            "      NInvMPPT=1\n      NoOrientation=1\n      NStrOrient1=2\n"
            "      PowerSharingInverterId=-1\n      PNomAC_InMPPT=5.00\n"
            "    End of PVObject pvSubArray\n  End of PVObject pvMainArray\n"
            "  PVObject_=pvOrient\n    Version=8.1.6\n    Flags=$00\n    NoOrient=1\n"
            "    FieldType=FixedPlane\n    FieldTilt=20.0\n    FieldAzim=0.0\n"
            "    BaseSlope=0.0\n    UsesTiltAver=False\n    UsesAzimAver=False\n"
            "    UsesSlopeAver=False\n  End of TOrientGroup\n"
            "  PVObject_Ombrage=pvShading\n    Flags=$00\n  End of PVObject pvShading\n"
            "  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n  End of PVObject pvSystem\n"
            "  SimulPlots=Start\n  End of List SimulPlots\n"
            "  VarList, list of=1 TVarSimul\n    VarSimul_1=E_Grid\n  End of the List VarList\n"
            "End of PVObject pvVCalcul\n"
        )
        self.source(shaded)
        loaded = create_native_load_variant(
            self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            load={"mode": "constant", "constant_kw": 2.5}, dry_run=False,
        )
        vc1_text = self.created(loaded)
        self.assertIn("Flags=$00180000\r\n", vc1_text)
        self.assertIn("PVObject_Ombrage=pvShading\r\n", vc1_text)
        self.assertNotIn("VarList, list of=", vc1_text)

        peak_src = (
            ROOT.format(root_flags="$00180000")
            + "  PVObject_PVMainArray=pvMainArray\n    Flags=$01900400\n    Flags2=$082A\n"
            + circuit
            + "    PVObject_=pvSubArray\n      Comment=PV Array\n      Version=8.1.6\n      Flags=$019E1002\n      Flags2=$20\n"
            + "      SubArrayId=1\n      NModSerie=10\n      NStringCh=2\n      NInverter=1\n"
            + "      NInvMPPT=1\n      NoOrientation=1\n      NStrOrient1=2\n"
            + "      PowerSharingInverterId=-1\n      PNomAC_InMPPT=5.00\n"
            + "    End of PVObject pvSubArray\n  End of PVObject pvMainArray\n"
            + "  Orientations, list of=1 TOrientGroup\n"
            + "    PVObject_=pvOrient\n      Version=8.1.6\n      Flags=$00\n      NoOrient=1\n"
            + "      FieldType=FixedPlane\n      FieldTilt=20.0\n      FieldAzim=0.0\n"
            + "      BaseSlope=0.0\n      UsesTiltAver=False\n      UsesAzimAver=False\n"
            + "      UsesSlopeAver=False\n    End of TOrientGroup\n  End of Orientations\n"
            + "  PVObject_Ombrage=pvShading\n    Flags=$00\n  End of PVObject pvShading\n"
            + "  PVObject_System=pvSystem\n    Flags=$17000040\n    SystemType=Grid\n"
            + "    BatteryFile=Universal_Adjustable_LI.BTR\n    LimGrSeparLoss=False\n"
            + "    TypeTempBatt=1\n    NBattSerie=15\n    NBattParall=10\n"
            + "    PLimGrid=200.000\n    SOHBattBeg=1.000\n    BattVoltage_Syst=768.000\n"
            + "    BattCapacity_Syst=1000.000\n    TempBattRef=20.000\n"
            + "    TempBattMois=" + "20.000," * 12 + "\n  End of PVObject pvSystem\n"
            + "  PVObject_GridStorage=pvGridStorage\n    Flags=$00\n    Strategy=PeakShaving\n"
            + "    ChargingStrategy=PVToGridMin\n    DischStrategy=AfterSunset\n"
            + "    BattSoCInit=0.600\n    SOCChDecl=0.950\n    SOCDischDecl=0.200\n    SOCDischSelf=0.500\n"
            + "    PMaxCharge=380.000\n    PMaxDisch=170.000\n    BattRectMaxEffic=97.000\n"
            + "    BattRectEuroEffic=95.000\n    BattInvMaxEffic=97.000\n    BattInvEuroEffic=95.000\n"
            + "  End of PVObject pvGridStorage\n"
            + CLOSE
        )
        self.source(peak_src)
        (self.root / "Projects/Fixture.VC1").unlink()
        peaked = create_peak_shaving_variant(
            self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            discharge={"mode": "hour_window", "start_hour": 18, "end_hour": 22}, dry_run=False,
        )
        vc1_peak = self.created(peaked)
        self.assertIn("DischStrategy=MinMaxHour\r\n", vc1_peak)
        self.assertIn("PVObject_Ombrage=pvShading\r\n", vc1_peak)

        # update_parameters system_unavailability_updates works with active pvShading + VarList
        self.source(shaded)
        digest = self.store.inspect("Fixture.PRJ", "VC0")["sha256"]
        upd = self.store.update_parameters(
            "Fixture.PRJ", "VC0", digest,
            system_unavailability_updates={"mode": "periods", "periods": [
                {"date_beg": "10/01/23 08:00", "duration_hours": 10},
            ]},
        )
        self.assertTrue(upd["changed"])
        self.assertEqual(upd["system_unavailability"]["after"]["period_count"], 1)
        vc0_after = (self.root / "Projects/Fixture.VC0").read_text(encoding="utf-8")
        self.assertIn("PVObject_Ombrage=pvShading", vc0_after)
        self.assertNotIn("VarList, list of=", vc0_after)
        self.assertIn("Flags=$00100000", vc0_after)

    def test_powershift_peakshaving_from_scratch_and_builtin_li_btr(self):
        from pvsyst_auto.energy.peak import create_peak_shaving_variant

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

        shaded = (
            "PVObject_=pvVCalcul\n  Comment=Fixture\n  Version=8.1.7\n  Flags=$00100001\n"
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
            + "          InverterNode End;\n        Children=End\n      InjectionPointNode End;\n"
            + "    End of PVObject pvCircuit\n"
            + "    PVObject_=pvSubArray\n      Comment=PV Array\n      Version=8.1.7\n      Flags=$019E1002\n"
            + "      Flags2=$20\n      SubArrayId=1\n      NModSerie=10\n      NStringCh=2\n"
            + "      NInverter=1\n      NInvMPPT=1\n      NoOrientation=1\n      NStrOrient1=2\n"
            + "    End of PVObject pvSubArray\n  End of PVObject pvMainArray\n"
            + "  Orientations, list of=1 TOrientGroup\n"
            + "    PVObject_=pvOrient\n      Version=8.1.7\n      Flags=$00\n      NoOrient=1\n"
            + "      FieldType=FixedPlane\n      FieldTilt=20.0\n      FieldAzim=0.0\n"
            + "    End of TOrientGroup\n  End of Orientations\n"
            + "  PVObject_Ombrage=pvShading\n    Flags=$00\n  End of PVObject pvShading\n"
            + "  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n  End of PVObject pvSystem\n"
            + "  VarList, list of=1 TVarSimul\n    VarSimul_1=E_Grid\n  End of the List VarList\n"
            + "End of PVObject pvVCalcul\n"
        )
        self.source(shaded)
        builtin_batt = {
            "file_name": "Generic_LI_Module_180Ah.BTR",
            "library": "builtin",
            "series_count": 2,
            "parallel_count": 4,
            "monthly_temperature_c": [20] * 12,
            "cycle_sow": 1,
            "static_sow": 1,
        }
        ps_base = {
            **STORAGE,
            "strategy": "power_shift",
            "charging_strategy": "pv_prod_fraction",
            "charging_pv_fraction": 0.3,
            "charging_schedule_48": chg_slots,
            "discharging_strategy": "specified_hours",
            "discharging_power_kw": 3.0,
            "discharging_schedule_48": [3.0] * 12 + [0.0] * 24 + [3.0] * 12,
        }
        res_ps = create_grid_storage_system(
            self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC1",
            battery=builtin_batt, storage=ps_base, load_manifest_name=None, dry_run=False,
        )
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
        res_ps2 = create_grid_storage_variant(
            self.store, project="Fixture.PRJ", source_variant="VC1", new_variant="VC2",
            updates={
                "charging_strategy": "spec_charg_power",
                "charging_power_kw": 4.0,
                "charging_schedule_48": [0.0] * 18 + [4.0] * 16 + [0.0] * 14,
                "discharging_strategy": "night",
                "discharging_power_kw": 2.5,
            },
            dry_run=False,
        )
        self.assertEqual(res_ps2["status"], "created", res_ps2)
        vc2 = (self.root / "Projects/Fixture.VC2").read_text(encoding="utf-8")
        self.assertIn("ChargingStrategy=SpecChargPower", vc2)
        self.assertIn("DischStrategy=Night", vc2)
        self.assertNotIn("DischargingPowerParSim", vc2)

        # Unsupported PowerShift + AfterSunset fails closed
        bad_ps = create_grid_storage_variant(
            self.store, project="Fixture.PRJ", source_variant="VC1", new_variant="VC3",
            updates={"discharging_strategy": "after_sunset", "discharging_power_kw": 2.0},
            dry_run=False,
        )
        self.assertEqual(bad_ps["status"], "blocked")

        # Create PeakShaving from scratch on 3D VC0 -> VC4, then switch hour_window -> VC5
        res_pk = create_grid_storage_system(
            self.store, project="Fixture.PRJ", source_variant="VC0", new_variant="VC4",
            battery=builtin_batt,
            storage={
                **STORAGE,
                "strategy": "peak_shaving",
                "charging_strategy": "pv_above_thresh",
                "discharging_strategy": "after_sunset",
                "grid_limit_kw": 4.5,
            },
            dry_run=False,
        )
        self.assertEqual(res_pk["status"], "created", res_pk)
        vc4 = (self.root / "Projects/Fixture.VC4").read_text(encoding="utf-8")
        self.assertIn("PLimGrid=4.5", vc4)
        self.assertIn("Strategy=PeakShaving", vc4)
        res_pk2 = create_peak_shaving_variant(
            self.store, project="Fixture.PRJ", source_variant="VC4", new_variant="VC5",
            discharge={"mode": "hour_window", "start_hour": 19, "end_hour": 6}, dry_run=False,
        )
        self.assertEqual(res_pk2["status"], "created", res_pk2)
        vc5 = (self.root / "Projects/Fixture.VC5").read_text(encoding="utf-8")
        self.assertIn("DischStrategy=MinMaxHour", vc5)
        self.assertIn("HourBegDisch=19", vc5)


if __name__ == "__main__":
    unittest.main()

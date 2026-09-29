"""Offline component fixtures; no installed PVsyst or simulation quota required."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from pvsyst_components import ComponentStore
from pvsyst_cli import PVsystCLI
import pvsyst_mcp_server as server


PAN = """PVObject_=pvModule
  Version=8.1.6
  Flags=$00
  PVObject_Commercial=pvCommercial
    Manufacturer=Lab
    Model=440W
  End of PVObject pvCommercial
  Technol=mtSiMono
  NCelS=72
  NCelP=1
  NDiode=3
  PNom=440
  LargApp=1.032
  LongApp=1.996
  GRef=1000
  TRef=25
  Absorb=0.9
  ISC=11.05
  Voc=49.6
  Imp=10.45
  Vmp=42.1
  MuISC=0.0055
  muVocSpec=-0.1485
End of PVObject pvModule
"""
OND = """PVObject_=pvGInverter
  Version=8.1.6
  Flags=$0570
  PVObject_Commercial=pvCommercial
    Manufacturer=Lab
    Model=50kW
  End of PVObject pvCommercial
  Converter=TConverter
    PNomConv=50
    PMaxOUT=50
    VMppMin=200
    VMPPMax=1000
    VAbsMax=1500
    EfficMax=98
    ProfilPIO=TCubicProfile
      Point_1=500,470
    End of TCubicProfile
  End of TConverter
End of PVObject pvGInverter
"""
BTR = """PVObject_=pvBattery
  Version=8.1.6
  Flags=$00
  PVObject_Commercial=pvCommercial
    Manufacturer=Lab
    Model=160Ah
  End of PVObject pvCommercial
  BattTechnol=Pb_Sealed_AGM
  CapNomC10=160
  AlphaSOC=0.2
  SelfDisch_Temp=TCubicProfile
    Point_1=0,1
  End of TCubicProfile
End of PVObject pvBattery
"""
GEN = """PVObject_=pvGenerator
  Version=8.1.6
  Flags=$00
  PVObject_Commercial=pvCommercial
    Manufacturer=Lab
    Model=50kVA
  End of PVObject pvCommercial
  TypeGen=Diesel
  PNomGen=50
  CFuelHor=0.25
End of PVObject pvGenerator
"""


class ComponentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name) / "work"
        self.builtin = Path(tmp.name) / "builtin" / "ComposPV"
        self.workspace.mkdir()
        self.store = ComponentStore(self.workspace, self.builtin)
        self.files = {}
        for kind, (folder, content) in {
            "PAN": ("PVmodules", PAN), "OND": ("Inverters", OND),
            "BTR": ("Batteries", BTR), "GEN": ("Gensets", GEN),
        }.items():
            directory = self.workspace / "ComposPV" / folder
            directory.mkdir(parents=True)
            path = directory / f"sample.{kind}"
            path.write_text(content, encoding="utf-8")
            self.files[kind] = path
        (self.builtin / "Inverters").mkdir(parents=True)
        (self.builtin / "Inverters" / "official.OND").write_text(OND, encoding="utf-8")

    def test_all_types_list_get_validate_and_pagination(self):
        for kind in self.files:
            with self.subTest(kind=kind):
                report = self.store.validate(kind, f"sample.{kind}")
                self.assertEqual(report["format"], "text")
                self.assertEqual(report["known_value_errors"], [])
                self.assertEqual(report["warnings"], [])
                self.assertEqual(self.store.list(kind, query="Lab")["total"], 1)
                self.assertEqual(len(self.store.inspect(kind, f"sample.{kind}", limit=1)["lines"]), 1)
        self.assertEqual(self.store.list("OND", "builtin")["items"][0]["name"], "official.OND")
        with self.assertRaises(ValueError):
            self.store.list("RLT")
        with self.assertRaises(ValueError):
            self.store.inspect("GEN", "sample.GEN", limit=201)

    def test_create_from_complete_text_without_template(self):
        for kind, text in (("PAN", PAN), ("OND", OND), ("BTR", BTR), ("GEN", GEN)):
            with self.subTest(kind=kind):
                created = self.store.create(kind, f"from_text.{kind}", text)
                self.assertEqual(created["type"], kind)
                self.assertEqual(self.store.validate(kind, f"from_text.{kind}")["structural_errors"], [])
                with self.assertRaises(FileExistsError):
                    self.store.create(kind, f"from_text.{kind}", text)
        with self.assertRaisesRegex(ValueError, "CFuelHor must be greater than zero"):
            self.store.create("GEN", "danger.GEN", GEN.replace("CFuelHor=0.25", "CFuelHor=0"))
        with self.assertRaisesRegex(ValueError, "Unclosed nested"):
            self.store.create("BTR", "broken.BTR", BTR.replace("  End of TCubicProfile\n", ""))
        with self.assertRaisesRegex(ValueError, "pvCommercial subobject"):
            self.store.create("GEN", "no_commercial.GEN",
                              GEN.replace("PVObject_Commercial=pvCommercial", "Comment=pvCommercial"))
        with self.assertRaisesRegex(ValueError, "TConverter and TCubicProfile"):
            self.store.create("OND", "no_curve.OND", OND.replace("ProfilPIO=TCubicProfile", "Curve=TCubicProfile"))
        self.assertFalse(self.files["GEN"].with_name("danger.GEN").exists())
        self.assertFalse(self.files["BTR"].with_name("broken.BTR").exists())

    def test_legacy_pan_bytes_copy_only(self):
        legacy = b"pvModule;legacy\n\x00\xff\r\npvCommercial;;"
        source = self.files["PAN"]
        source.write_bytes(legacy)
        self.assertEqual(self.store.validate("PAN", source.name)["format"], "legacy-binary")
        copied = self.store.copy("PAN", source.name, "legacy_copy.PAN")
        self.assertEqual(copied["format"], "legacy-binary")
        self.assertEqual(source.with_name("legacy_copy.PAN").read_bytes(), legacy)
        with self.assertRaisesRegex(ValueError, "Legacy binary"):
            self.store.update("PAN", source.name, {"Model": "Other"})
        with self.assertRaisesRegex(ValueError, "Legacy binary"):
            self.store.clone("PAN", source.name, "bad.PAN",
                             {"Manufacturer": "Other", "Model": "Other"})
        self.assertNotIn("diff", self.store.compare("PAN", source.name, "legacy_copy.PAN"))

    def test_clone_updates_backup_restore_and_no_overwrite(self):
        for kind in self.files:
            with self.subTest(kind=kind):
                target = f"fresh.{kind}"
                result = self.store.clone(kind, f"sample.{kind}", target,
                                          {"Manufacturer": "New", "Model": "Fresh"})
                self.assertEqual(result["type"], kind)
                self.assertEqual(self.store.validate(kind, target)["model"], "Fresh")
                with self.assertRaises(FileExistsError):
                    self.store.copy(kind, f"sample.{kind}", target)
                original = self.files[kind].read_bytes()
                changed = self.store.update(kind, f"sample.{kind}", {"Model": "Updated"})
                self.assertEqual(self.store.validate(kind, f"sample.{kind}")["model"], "Updated")
                with self.assertRaisesRegex(ValueError, "confirm=true"):
                    self.store.restore(kind, f"sample.{kind}", changed["backup_name"])
                restored = self.store.restore(kind, f"sample.{kind}",
                                              changed["backup_name"], confirm=True)
                self.assertIsNotNone(restored["previous_backup"])
                self.assertEqual(self.files[kind].read_bytes(), original)
        self.assertFalse(self.store.compare("GEN", "sample.GEN", "fresh.GEN")["same_bytes"])
        self.assertIn("-    Manufacturer=Lab", self.store.compare("GEN", "sample.GEN", "fresh.GEN")["diff"])

    def test_known_value_checks_and_unsupported_fields(self):
        original = self.files["GEN"].read_bytes()
        for value in ("0", "-5", "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.update("GEN", "sample.GEN", {"CFuelHor": value})
        self.assertEqual(self.files["GEN"].read_bytes(), original)
        with self.assertRaises(ValueError):
            self.store.update("GEN", "sample.GEN", {"PNomGen": "0"})
        with self.assertRaises(ValueError):
            self.store.update("BTR", "sample.BTR", {"AlphaSOC": "0.85"})
        with self.assertRaises(ValueError):
            self.store.update("BTR", "sample.BTR", {"SelfDisch_Temp": "9"})
        with self.assertRaises(ValueError):
            self.store.update("BTR", "sample.BTR", {"BattTechnol": "Li_NMC"})
        with self.assertRaises(ValueError):
            self.store.update("PAN", "sample.PAN", {"Voc": "10"})
        with self.assertRaises(ValueError):
            self.store.update("OND", "sample.OND", {"VAbsMax": "100"})
        with self.assertRaises(ValueError):
            self.store.update("GEN", "sample.GEN", {"Model": "x\nCFuelHor=0"})
        invalid = self.files["GEN"].with_name("incomplete.GEN")
        invalid.write_text(GEN.replace("  CFuelHor=0.25\n", ""), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.store.copy("GEN", invalid.name, "unsafe.GEN")
        self.assertFalse(invalid.with_name("unsafe.GEN").exists())

    def test_paths_and_backup_restrictions(self):
        for bad in ("../sample.GEN", "..\\sample.GEN", "C:\\sample.GEN",
                    "sample.OND", "sample.GEN.", "CON/GEN"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.copy("GEN", "sample.GEN", bad)
        saved = self.store.backup("GEN", "sample.GEN")
        with self.assertRaises(ValueError):
            self.store.restore("GEN", "sample.GEN", "../" + saved["backup_name"], True)
        self.assertEqual(self.store.copy("OND", "official.OND", "imported.OND", "builtin")["type"], "OND")
        self.assertEqual(self.store.list("OND")["total"], 2)

    def test_reject_symlink_outside_workspace(self):
        external = self.workspace.parent / "external.GEN"
        external.write_text(GEN, encoding="utf-8")
        path = self.files["GEN"]
        path.unlink()
        try:
            path.symlink_to(external)
        except OSError:
            self.skipTest("Symlink creation is unavailable on this Windows host")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.store.inspect("GEN", path.name)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.store.backup("GEN", path.name)
        self.assertEqual(external.read_text(encoding="utf-8"), GEN)
        path.unlink()
        other = path.with_name("other.GEN")
        other.write_text(GEN, encoding="utf-8")
        path.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.store.archive("GEN", path.name, True)
        self.assertTrue(other.is_file())

    def test_project_references_without_false_missing_claim(self):
        projects = self.workspace / "Projects"
        projects.mkdir()
        (projects / "fixture.PRJ").write_text("PVObject_=pvProject\n", encoding="utf-8")
        (projects / "fixture.VC0").write_text(
            "PVModule=sample.PAN\nGInverter=BuiltIn.OND\n"
            "BatteryFile=sample.BTR\nPVObject_System=pvSystem\n"
            "  Flags=$60\n  GensetFile=sample.GEN\n  PEffBackUp=40\n"
            "End of PVObject pvSystem\nVBkUpEncl_syst=0.9\n", encoding="utf-8")
        report = self.store.dependencies("fixture.PRJ", "VC0")
        statuses = {ref["type"]: ref["status"] for ref in report["references"]}
        self.assertEqual(statuses, {"PAN": "workspace", "OND": "unknown-or-builtin-db",
                                    "BTR": "workspace", "GEN": "workspace"})
        self.assertTrue(report["generator_configuration"]["enabled_flag"])
        self.assertEqual(report["generator_configuration"]["effective_backup_kw"], 40)
        with self.assertRaises(ValueError):
            self.store.dependencies("../fixture", "VC0")

    def test_archive_and_restore_only_unreferenced(self):
        project = self.workspace / "Projects"
        project.mkdir()
        (project / "use.VC0").write_text("  GensetFile=sample.GEN\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            self.store.archive("GEN", "sample.GEN")
        with self.assertRaisesRegex(ValueError, "referenced"):
            self.store.archive("GEN", "sample.GEN", True)
        self.assertTrue(self.files["GEN"].exists())
        (project / "use.VC0").write_text("  GensetFile=other.GEN\n", encoding="utf-8")
        original = self.files["GEN"].read_bytes()
        archived = self.store.archive("GEN", "sample.GEN", True)
        self.assertFalse(self.files["GEN"].exists())
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            self.store.restore_archive("GEN", "sample.GEN", archived["archive_name"])
        self.store.restore_archive("GEN", "sample.GEN", archived["archive_name"], True)
        self.assertEqual(self.files["GEN"].read_bytes(), original)
        with self.assertRaises(FileExistsError):
            self.store.restore_archive("GEN", "sample.GEN", archived["archive_name"], True)
        (project / "use.VC0").write_bytes(b"\xffGensetFile=sample.GEN\n")
        with self.assertRaisesRegex(ValueError, "Cannot safely inspect project references"):
            self.store.archive("GEN", "sample.GEN", True)
        self.assertTrue(self.files["GEN"].exists())

    def test_mcp_registration_and_component_call(self):
        client = PVsystCLI(Path(sys.executable), self.workspace)
        server._cli = client
        self.addCleanup(setattr, server, "_cli", None)
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual(len(tools), 29)
        self.assertEqual(server.pvsyst_validate_component("GEN", "sample.GEN")["model"], "50kVA")
        self.assertEqual(server.pvsyst_list_components("BTR")["total"], 1)

"""Offline component fixtures; no installed PVsyst or simulation quota required."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_components import ComponentStore, MAX_PROJECT_BYTES
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

    def test_inspect_reports_page_and_line_truncation_independently(self):
        path = self.files["BTR"]
        path.write_text(BTR + "\n".join(f"Extra_{i}=v" for i in range(130)) + "\n",
                        encoding="utf-8")
        first = self.store.inspect("BTR", path.name, limit=100)
        self.assertGreater(first["total_lines"], 100)
        self.assertTrue(first["page_truncated"])
        self.assertFalse(first["line_truncated"])
        self.assertTrue(first["lines_truncated"])
        last = self.store.inspect("BTR", path.name, offset=100, limit=100)
        self.assertFalse(last["page_truncated"])
        self.assertFalse(last["lines_truncated"])
        path.write_text(BTR + "Comment=" + "x" * 2100 + "\n", encoding="utf-8")
        long_line = self.store.inspect("BTR", path.name, offset=12, limit=100)
        self.assertTrue(long_line["line_truncated"])
        self.assertFalse(long_line["page_truncated"])
        self.assertTrue(long_line["lines_truncated"])

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

    def test_clone_and_update_preserve_utf8_bom(self):
        source = self.files["OND"]
        source.write_bytes(b"\xef\xbb\xbf" + OND.encode("utf-8"))
        self.store.clone("OND", source.name, "with_bom.OND",
                         {"Manufacturer": "New", "Model": "New"})
        self.assertTrue(source.with_name("with_bom.OND").read_bytes().startswith(b"\xef\xbb\xbf"))
        self.store.update("OND", source.name, {"Model": "Updated"})
        self.assertTrue(source.read_bytes().startswith(b"\xef\xbb\xbf"))

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
        self.store.update("GEN", "sample.GEN", {"TypeGen": "CustomFuel"})
        self.assertEqual(self.store.validate("GEN", "sample.GEN")["known_value_errors"], [])

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

    def test_subarray_thresholds_report_values_per_array(self):
        projects = self.workspace / "Projects"
        projects.mkdir()
        (projects / "fixture.PRJ").write_text("PVObject_=pvProject\n", encoding="utf-8")
        (projects / "fixture.VC0").write_text(
            "PVObject_=pvVariant\n"
            "PVObject_=pvSubArray\nVBkUpEncl_syst=0.250\n"
            "VBkUpDecl_syst=0.450\nEnd of PVObject pvSubArray\n"
            "PVObject_=pvSubArray\nVBkUpEncl_syst=0.350\n"
            "End of PVObject pvSubArray\n"
            "PVObject_System=pvSystem\nFlags=$20\nPEffBackUp=40\n"
            "GensetFile=sample.GEN\nEnd of PVObject pvSystem\n",
            encoding="utf-8")
        configuration = self.store.dependencies("fixture.PRJ", "VC0")["generator_configuration"]
        self.assertTrue(configuration["enabled_flag"])
        self.assertEqual(configuration["effective_backup_kw"], 40)
        self.assertEqual(configuration["subarray_thresholds"], [
            {"index": 0, "line": 2, "enclosure": "0.250", "release": "0.450"},
            {"index": 1, "line": 6, "enclosure": "0.350", "release": None},
        ])
        self.assertTrue(configuration["enclosure_threshold_present"])
        self.assertTrue(configuration["release_threshold_present"])

    def test_archive_recurses_into_project_subdirectories(self):
        projects = self.workspace / "Projects"
        nested = projects / "SubFolder"
        nested.mkdir(parents=True)
        (nested / "uses.VC0").write_text("GensetFile=sample.GEN\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SubFolder/uses.VC0"):
            self.store.archive("GEN", "sample.GEN", True)
        self.assertTrue(self.files["GEN"].exists())
        (nested / "uses.VC0").write_text("GensetFile=other.GEN\n", encoding="utf-8")
        result = self.store.archive("GEN", "sample.GEN", True)
        self.assertIn("archive_name", result)

    def test_archive_scans_large_projects_and_bounds_file_size(self):
        projects = self.workspace / "Projects"
        projects.mkdir()
        large = projects / "large.VC0"
        large.write_text("Comment=ok\n" * 200_000 + "GensetFile=sample.GEN\n",
                         encoding="utf-8")
        self.assertGreater(large.stat().st_size, 2_000_000)
        with self.assertRaisesRegex(ValueError, "referenced by: large.VC0"):
            self.store.archive("GEN", "sample.GEN", True)
        large.write_text("Comment=ok\n" * 200_000, encoding="utf-8")
        self.assertEqual(self.store._project_uses("GEN", "sample.GEN"), [])
        with large.open("ab") as stream:
            stream.truncate(MAX_PROJECT_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "oversized project file: large.VC0"):
            self.store.archive("GEN", "sample.GEN", True)
        self.assertTrue(self.files["GEN"].exists())

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

    def test_archive_rejects_nested_symlink_directory(self):
        projects = self.workspace / "Projects"
        projects.mkdir()
        external = self.workspace.parent / "external-projects"
        external.mkdir()
        (external / "uses.VC0").write_text("GensetFile=sample.GEN\n", encoding="utf-8")
        try:
            (projects / "linked").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is unavailable on this Windows host")
        with self.assertRaisesRegex(ValueError, "symlink project path: linked"):
            self.store.archive("GEN", "sample.GEN", True)
        self.assertTrue(self.files["GEN"].exists())

    def test_installed_library_discovery_uses_cli_not_workspace_name(self):
        install = self.workspace.parent / "install"
        install.mkdir()
        executable = install / "PVsystCLI.exe"
        executable.write_text("fixture", encoding="utf-8")
        official = install / "DataRO" / "PVsyst8.1_Data" / "ComposPV" / "Inverters"
        official.mkdir(parents=True)
        (official / "official.OND").write_text(OND, encoding="utf-8")
        server._cli = PVsystCLI(executable, self.workspace)
        self.addCleanup(setattr, server, "_cli", None)
        with patch.dict(os.environ, {"PVSYST_BUILTIN_COMPONENTS": ""}):
            self.assertEqual(server.pvsyst_list_components("OND", "builtin")["total"], 1)
            self.assertEqual(server.pvsyst_copy_component(
                "OND", "official.OND", "imported.OND", "builtin")["type"], "OND")
            second = install / "DataRO" / "PVsyst8.0_Data" / "ComposPV"
            second.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "Multiple DataRO component libraries"):
                server.components()
        with patch.dict(os.environ, {"PVSYST_BUILTIN_COMPONENTS": str(official.parent)}):
            self.assertEqual(server.pvsyst_list_components("OND", "builtin")["total"], 1)

    def test_mcp_registration_and_component_call(self):
        client = PVsystCLI(Path(sys.executable), self.workspace)
        server._cli = client
        self.addCleanup(setattr, server, "_cli", None)
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual(len(tools), 33)
        self.assertEqual(server.pvsyst_validate_component("GEN", "sample.GEN")["model"], "50kVA")
        self.assertEqual(server.pvsyst_list_components("BTR")["total"], 1)

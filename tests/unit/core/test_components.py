"""Offline component fixtures; no installed PVsyst or simulation quota required."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.components import commercial
from pvsyst_auto.components.builder import build_sourced_component
from pvsyst_auto.components.editor import has_object, objects, replace_key, replace_value, root_block
from pvsyst_auto.components.identity import sourced_component
from pvsyst_auto.components.store import ComponentStore, MAX_PROJECT_BYTES
from pvsyst_auto.native.cli import PVsystCLI
from pvsyst_auto.api import app as _registered_app, context as server
import pvsyst_auto.api.components as api_components


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
        converter = self.files["OND"]
        original = converter.read_bytes()
        for raw, status in (("0", "not_declared"), ("25", "declared"), ("nan", "invalid")):
            converter.write_text(OND.replace("    PNomConv=50", "    PNomConv=50\n    IDCMax=" + raw)
                                 .replace("    Model=50kW", "    Model=50kW\n    PNomConv=999"), encoding="utf-8")
            electrical = self.store.inspect("OND", "sample.OND")["electrical"]
            self.assertEqual(electrical["fields"]["PNomConv"]["value"], 50)
            self.assertEqual(electrical["fields"]["PNomConv"]["unit"], "kW")
            self.assertEqual(electrical["fields"]["PNomConv"]["path"], "Converter")
            self.assertEqual(electrical["fields"]["IDCMax"]["status"], status)
            self.assertEqual(electrical["fields"]["NbMPPT"]["status"], "missing")
        converter.write_bytes(original)
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

        li_btr = (
            "PVObject_=pvBattery\n  Version=8.1.7\n  Flags=$00\n"
            "  PVObject_Commercial=pvCommercial\n    Manufacturer=Lab\n    Model=LFP180\n"
            "  End of PVObject pvCommercial\n  BattTechnol=Li_LFP\n  CapaRef=180\n"
            "End of PVObject pvBattery\n"
        )
        self.store.create("BTR", "li_sample.BTR", li_btr)
        li_info = self.store.validate("BTR", "li_sample.BTR")
        self.assertEqual(li_info["warnings"], [])
        self.assertEqual(li_info["known_value_errors"], [])
        no_cap_btr = BTR.replace("  CapNomC10=160\n", "")
        with self.assertRaisesRegex(ValueError, "CapNomC10"):
            self.store.create("BTR", "pb_missing.BTR", no_cap_btr)

        hyb_ond = OND.replace("    PNomConv=50\n", "    IsHybrid=True\n    PNomConv=50\n")
        self.store.create("OND", "hybrid_meta.OND", hyb_ond)
        hyb_elec = self.store.inspect("OND", "hybrid_meta.OND")["electrical"]
        self.assertEqual(hyb_elec["modes"]["IsHybrid"], "True")
        self.assertTrue(any(item["field"] == "IsHybrid" for item in hyb_elec["unverified"]))

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
            self.assertEqual(api_components.pvsyst_list_components("OND", "builtin")["total"], 1)
            self.assertEqual(server.components().copy(
                "OND", "official.OND", "imported.OND", "builtin")["type"], "OND")
            second = install / "DataRO" / "PVsyst8.0_Data" / "ComposPV"
            second.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "Multiple DataRO component libraries"):
                server.components()
        with patch.dict(os.environ, {"PVSYST_BUILTIN_COMPONENTS": str(official.parent)}):
            self.assertEqual(api_components.pvsyst_list_components("OND", "builtin")["total"], 1)

    def test_native_closing_tags_root_block_and_object_starts(self):
        oriented = ("PVObject_=pvVCalcul\r\n  Version=8.1.6\r\n  Orientations, list of=1 TOrientGroup\r\n"
                    "    PVObject_=pvOrient\r\n      Comment=End of TOrientGroup\r\n      NoOrient=1\r\n"
                    "    End of TOrientGroup  \r\n  End of Orientations\r\nEnd of PVObject pvVCalcul\r\n")
        dome = ("PVObject_=pvVCalcul\n  Orientations, list of=1 TOrientGroup\n    PVObject_=pvDomeOrient\n"
                "      NoOrient=1\n    End of TDomeOrientGroup\n  End of Orientations\nEnd of PVObject pvVCalcul\n")
        self.assertEqual([b.object_type for b in objects(oriented)], ["pvVCalcul", "pvOrient"])
        self.assertEqual(objects(oriented)[1].fields["Comment"], [(4, "End of TOrientGroup")])
        self.assertEqual([b.object_type for b in objects(dome)], ["pvVCalcul", "pvDomeOrient"])
        with self.assertRaisesRegex(ValueError, "Unmatched object closing tag"):
            objects("PVObject_=pvVCalcul\n  PVObject_=pvOrient\n  End of TDomeOrientGroup\nEnd of PVObject pvVCalcul\n")
        self.assertEqual(root_block(["Comment=outside\n", *oriented.splitlines(keepends=True)]).object_type, "pvVCalcul")
        self.assertTrue(has_object(["  PVObject_UserNeeds = pvLoad\n"], "pvLoad"))
        self.assertFalse(has_object(["  PVObject_GridStorage=pvGridStorage"], "pvLoad"))

    def test_batchsimul_owns_fields_up_to_its_endtags(self):
        text = ("PVObject_=pvVCalcul\n  BatchSimul = TBatchSimul\n    Version=8.1.0\n"
                "    ParamsFileName=Batch.CSV\n  EndTags  \n  Version=8.1.7\n"
                "  Comment=TBatchSimul\nEnd of PVObject pvVCalcul\n")
        root, = objects(text)
        self.assertEqual(root.one("Version"), (5, "8.1.7"))
        self.assertEqual(root.one("Comment"), (6, "TBatchSimul"))
        self.assertNotIn("ParamsFileName", root.fields)
        self.assertNotIn("BatchSimul", root.fields)
        with self.assertRaisesRegex(ValueError, "Unmatched object closing tag"):
            objects(text.replace("  EndTags  \n", ""))

    def test_native_private_frames_keep_their_owners(self):
        with self.subTest(record="PrintSettings"):
            text = ("PVObject_=pvProject\nPrintSettings=TPrintSettings\n  PCNDec=1\nEndTags\n"
                    "MeteoFileName=Demo.MET\nEnd of PVObject pvProject\n")
            project, = objects(text)
            self.assertNotIn("PCNDec", project.fields)
            self.assertEqual(project.one("MeteoFileName")[1], "Demo.MET")

        with self.subTest(record="HouseHoldUses"):
            text = ("PVObject_UserNeeds=pvLoad\n  UsesModul=Seasonal\n"
                    "  HouseHoldUses=THouseHoldUses\n    UsesModul=Seasonal\n  End of HouseHoldUses\n"
                    "End of PVObject pvLoad\n")
            load, = objects(text)
            self.assertEqual(load.one("UsesModul"), (1, "Seasonal"))

        with self.subTest(record="WaterUse"):
            text = ("PVObject_UserNeeds=pvLoad\n  WaterUse=TWaterUse\n    FlowModul=Yearly\n"
                    "    PVObject_HydroCircuit=pvHydrCircuit\n      Version=8.1.7\n      Flags=$01\n"
                    "      Fluide=TFluide\n        FluidType=Water\n      End of TFluide\n"
                    "      CircElement, list of=1 tPipe\n        Pipe=1\n          Version=8.1.7\n"
                    "          PipeName=PE50\n        End of TPipe\n      End of List CircElement\n"
                    "    End of PVObject pvHydrCircuit\n  End of WaterUse\nEnd of PVObject pvLoad\n")
            load, circuit = objects(text)
            self.assertEqual(circuit.one("Version"), (4, "8.1.7"))
            self.assertEqual(circuit.one("Flags")[1], "$01")
            self.assertNotIn("PipeName", circuit.fields)
            self.assertNotIn("FluidType", circuit.fields)
            self.assertNotIn("FlowModul", load.fields)

    def test_pathless_object_field_error_names_type_line_and_occurrences(self):
        missing_text = "PVObject_=pvVCalcul\n  Version=8.1.6\nEnd of PVObject pvVCalcul\n"
        with self.assertRaises(ValueError) as missing:
            objects(missing_text)[0].one("MissingField")
        self.assertEqual(
            str(missing.exception),
            "MissingField must occur exactly once in pvVCalcul at line 1 (found 0)",
        )

        duplicate_text = (
            "PVObject_=pvVCalcul\n  Version=8.1.6\n  Version=8.1.7\n"
            "End of PVObject pvVCalcul\n"
        )
        with self.assertRaises(ValueError) as duplicate:
            objects(duplicate_text)[0].one("Version")
        self.assertEqual(
            str(duplicate.exception),
            "Version must occur exactly once in pvVCalcul at line 1 "
            "(found 2, at lines 2, 3)",
        )

    def test_replace_value_and_key_reject_line_breaks(self):
        for brk in ("\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.assertRaises(ValueError):
                replace_value("  Comment=Old\r\n", f"New{brk}Injected=1")
            with self.assertRaises(ValueError):
                replace_key("A=1\r\n", f"B{brk}C")
        self.assertEqual(replace_value("  Comment=Old \t\r\n", "New\tName"), "  Comment=New\tName \t\r\n")
        self.assertEqual(replace_value("A=1", "2"), "A=2")

    def test_ageing_nesting_and_commercial_identity_precedence(self):
        ageing = "  PVObject_Ageing=pvAgeing\n    Flags=$00\n  End of TAgeing\n"
        decoy = "  PVObject_Meta=pvMeta\n    Manufacturer=Decoy\n    Model=Decoy\n  End of PVObject pvMeta\n"
        for kind, text, closer in (("PAN", PAN, "End of PVObject pvModule"),
                                   ("OND", OND, "End of PVObject pvGInverter"),
                                   ("BTR", BTR, "End of PVObject pvBattery"),
                                   ("GEN", GEN, "End of PVObject pvGenerator")):
            self.store.create(kind, f"aged.{kind}", text.replace(closer, ageing + closer))
            self.assertEqual(self.store.validate(kind, f"aged.{kind}")["structural_errors"], [])
        with self.assertRaisesRegex(ValueError, "Unmatched closing tag: End of TAgeing"):
            self.store.create("GEN", "bad.GEN", GEN.replace("  TypeGen=", "  PVObject_Meta=pvMeta\n  End of TAgeing\n  TypeGen="))
        self.store.create("OND", "owned.OND", OND.replace("  PVObject_Commercial=", decoy + "  PVObject_Commercial="))
        info = self.store.inspect("OND", "owned.OND")
        self.assertEqual((info["manufacturer"], info["model"]), ("Lab", "50kW"))
        self.assertEqual(self.store.list("OND", query="Decoy")["total"], 0)
        missing_text = OND.replace("    Manufacturer=Lab\n", "").replace("  PVObject_Commercial=", decoy + "  PVObject_Commercial=")
        self.files["OND"].write_text(missing_text, encoding="utf-8")
        field, _, payload = sourced_component(self.store, "OND", self.files["OND"].name)
        self.assertEqual((field, payload), ("OND.manufacturer", None))

    def test_sourced_component_lookup_ascii_numbers_and_reference_bounds(self):
        field, message, payload = sourced_component(self.store, "OND", "absent.OND")
        self.assertEqual((field, payload), ("OND.name", None))
        self.assertIn("absent.OND is not a loose OND file", message)
        broken = self.store._path("OND", "broken.OND")
        broken.write_text(OND.replace("    End of TCubicProfile\n", ""), encoding="utf-8")
        field, message, _ = sourced_component(self.store, "OND", "broken.OND")
        self.assertIn("closing tag", message)
        from tests.unit.core.test_pan_gui_format import GUI_PAN
        for bad_pan, err in ((PAN.replace("Vmp=42.1", "Vmp=49.6"), "Voc must be greater than Vmp"),
                             (PAN.replace("Imp=10.45", "Imp=11.05"), "ISC must be greater than Imp"),
                             (GUI_PAN.replace("Imp=10.45", "Imp=11.05"), "ISC must be greater than Imp")):
            with self.assertRaisesRegex(ValueError, err):
                self.store.create("PAN", "bad_ref.PAN", bad_pan)
        for val in ("\uff14\uff14\uff10", "4_40", " 440"):
            with self.assertRaisesRegex(ValueError, "PNom must be a finite number"):
                self.store.update("PAN", "sample.PAN", {"PNom": val})
        with self.assertRaisesRegex(ValueError, "YearBeg must be a"):
            commercial._value("YearBeg", "\uff12\uff10\uff12\uff14")

    def test_sourced_builder_failure_race_and_nested_converter(self):
        def build(kind="PAN", fields=(("PNom", 440, "W", "440", "Wp"),), **options):
            text = "Mono 440W\n" + "\n".join(n + u for _, _, _, n, u in fields)
            return build_sourced_component(
                self.store, component_type=kind, template_name=f"sample.{kind}", new_name=f"Built.{kind}",
                manufacturer="Fixture", model="Mono 440W",
                sources={"doc": {"kind": "assumption", "title": "Fixture", "locator": "fixture://c", "model": "Mono 440W", "text": text}},
                declarations=[{"field": f, "value": v, "unit": u, "source_id": "doc",
                               "excerpt": n + q, "source_value_text": n, "source_unit_text": q}
                              for f, v, u, n, q in fields],
                **options,
            )

        self.assertEqual(build(fields=(("Vmp", 49.6, "V", "49.6", "V"),))["status"], "blocked")
        manifest = self.workspace / "Results" / "Built.PAN.sources.json"
        target = self.store._path("PAN", "Built.PAN")
        with patch.object(self.store, "_new", side_effect=OSError("simulated create failure")):
            failed = build(dry_run=False, acknowledge_inherited_model=True)
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(manifest.exists())
        self.assertFalse(target.exists())

        real_read, calls = self.store._read, []

        def racing_read(path):
            data = real_read(path)
            calls.append(path)
            if len(calls) == 2:
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text("foreign", encoding="utf-8")
            return data

        with patch.object(self.store, "_read", side_effect=racing_read):
            raced = build(dry_run=False, acknowledge_inherited_model=True)
        self.assertEqual(raced["status"], "failed")
        self.assertEqual(manifest.read_text(encoding="utf-8"), "foreign")
        self.assertFalse(target.exists())

        self.files["OND"].write_text(
            OND.replace("  Converter=TConverter", "  PVObject_Meta=pvMeta\n  Converter=TConverter")
               .replace("  End of TConverter\n", "  End of TConverter\n  End of PVObject pvMeta\n"),
            encoding="utf-8",
        )
        blocked = build(kind="OND", fields=(("PNomConv", 50, "kW", "50", "kW"),))
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("electrical fields are unreadable", blocked["issues"][-1]["message"])

    def test_component_archive_repair_and_snapshot_tampering(self):
        path = self.files["GEN"]
        incomplete = GEN.replace("  Flags=$00\n", "").encode()
        path.write_bytes(incomplete)
        archived = self.store.archive("GEN", path.name, True)
        restored = ComponentStore(self.workspace).restore_archive("GEN", path.name, archived["archive_name"], True)
        self.assertEqual(path.read_bytes(), incomplete)
        self.assertTrue(restored["integrity_verified"])

        invalid_orig = GEN.replace("CFuelHor=0.25", "CFuelHor=0").encode()
        path.write_bytes(invalid_orig)
        repaired = self.store.update("GEN", path.name, {"CFuelHor": "0.25"})
        self.store.restore("GEN", path.name, repaired["backup_name"], True)
        self.assertEqual(path.read_bytes(), invalid_orig)

        path.write_bytes(GEN.encode())
        saved = self.store.backup("GEN", path.name)
        meta = self.workspace / "ComposPV" / ".mcp-backups" / "GEN" / (saved["backup_name"] + ".json")
        info = json.loads(meta.read_text())
        info["name"] = "another.GEN"
        meta.write_text(json.dumps(info))
        with self.assertRaises(ValueError):
            self.store.restore("GEN", path.name, saved["backup_name"], True)
        meta.unlink()
        with self.assertRaises(ValueError):
            self.store.restore("GEN", path.name, saved["backup_name"], True)

        legacy = self.store._backup_folder("GEN") / (path.name + "." + "a" * 32 + ".bak")
        legacy.write_bytes(GEN.encode())
        res = self.store.restore("GEN", path.name, legacy.name, True)
        self.assertFalse(res["integrity_verified"])

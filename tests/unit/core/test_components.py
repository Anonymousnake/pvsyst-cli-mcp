"""Tests for PV components, curves, commercial metadata, generator state, and PAN GUI format."""
from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pvsyst_auto.components import builder as commercial
from pvsyst_auto.components.builder import build_sourced_component
from pvsyst_auto.components.editor import has_object, objects, replace_key, replace_value, root_block
from pvsyst_auto.components.editor import sourced_component
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.native.cli import PVsystCLI
from pvsyst_auto.api import context as server
import pvsyst_auto.api.projects as api_components
import hashlib
from pvsyst_auto.components.builder import capacity_ratio
from pvsyst_auto.components.builder import commercial_inventory
from pvsyst_auto.api.projects import pvsyst_build_sourced_component
from pvsyst_auto.components.single_diode import (derive_single_diode_parameters, synthesize_btr_from_datasheet, synthesize_ond_from_datasheet, synthesize_pan_from_datasheet)
import csv
import io
from pvsyst_auto.components.editor import HEADERS, MAX_CSV_BYTES, import_curve_csv
from tests._fixtures import write_utf8_crlf
from pvsyst_auto.project.variants import VariantStore
PAN = 'PVObject_=pvModule\n  Version=8.1.6\n  Flags=$00\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Lab\n    Model=440W\n  End of PVObject pvCommercial\n  Technol=mtSiMono\n  NCelS=72\n  NCelP=1\n  NDiode=3\n  PNom=440\n  LargApp=1.032\n  LongApp=1.996\n  GRef=1000\n  TRef=25\n  Absorb=0.9\n  ISC=11.05\n  Voc=49.6\n  Imp=10.45\n  Vmp=42.1\n  MuISC=0.0055\n  muVocSpec=-0.1485\nEnd of PVObject pvModule\n'
OND = 'PVObject_=pvGInverter\n  Version=8.1.6\n  Flags=$0570\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Lab\n    Model=50kW\n  End of PVObject pvCommercial\n  Converter=TConverter\n    PNomConv=50\n    PMaxOUT=50\n    VMppMin=200\n    VMPPMax=1000\n    VAbsMax=1500\n    EfficMax=98\n    ProfilPIO=TCubicProfile\n      Point_1=500,470\n    End of TCubicProfile\n  End of TConverter\nEnd of PVObject pvGInverter\n'
BTR = 'PVObject_=pvBattery\n  Version=8.1.6\n  Flags=$00\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Lab\n    Model=160Ah\n  End of PVObject pvCommercial\n  BattTechnol=Pb_Sealed_AGM\n  CapNomC10=160\n  AlphaSOC=0.2\n  SelfDisch_Temp=TCubicProfile\n    Point_1=0,1\n  End of TCubicProfile\nEnd of PVObject pvBattery\n'
GEN = 'PVObject_=pvGenerator\n  Version=8.1.6\n  Flags=$00\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Lab\n    Model=50kVA\n  End of PVObject pvCommercial\n  TypeGen=Diesel\n  PNomGen=50\n  CFuelHor=0.25\nEnd of PVObject pvGenerator\n'
class ComponentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name) / "work"
        self.builtin = Path(tmp.name) / "builtin" / "ComposPV"
        self.workspace.mkdir()
        self.store = ComponentStore(self.workspace, self.builtin)
        self.files = {}
        for kind, (folder, content) in {"PAN": ("PVmodules", PAN), "OND": ("Inverters", OND), "BTR": ("Batteries", BTR), "GEN": ("Gensets", GEN)}.items():
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
            converter.write_text(OND.replace("    PNomConv=50", "    PNomConv=50\n    IDCMax=" + raw) .replace("    Model=50kW", "    Model=50kW\n    PNomConv=999"), encoding="utf-8")
            electrical = self.store.inspect("OND", "sample.OND")["electrical"]
            self.assertEqual(electrical["fields"]["PNomConv"]["value"], 50)
            self.assertEqual(electrical["fields"]["PNomConv"]["unit"], "kW")
            self.assertEqual(electrical["fields"]["PNomConv"]["path"], "Converter")
            self.assertEqual(electrical["fields"]["IDCMax"]["status"], status)
            self.assertEqual(electrical["fields"]["NbMPPT"]["status"], "missing")
        converter.write_bytes(original)
        with self.assertRaises(ValueError): self.store.list("RLT")
        with self.assertRaises(ValueError): self.store.inspect("GEN", "sample.GEN", limit=201)
    def test_inspect_reports_page_and_line_truncation_independently(self):
        path = self.files["BTR"]
        path.write_text(BTR + "\n".join(f"Extra_{i}=v" for i in range(130)) + "\n", encoding="utf-8")
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
                with self.assertRaises(FileExistsError): self.store.create(kind, f"from_text.{kind}", text)
        with self.assertRaisesRegex(ValueError, "CFuelHor must be greater than zero"):
            self.store.create("GEN", "danger.GEN", GEN.replace("CFuelHor=0.25", "CFuelHor=0"))
        with self.assertRaisesRegex(ValueError, "Unclosed nested"): self.store.create("BTR", "broken.BTR", BTR.replace("  End of TCubicProfile\n", ""))
        with self.assertRaisesRegex(ValueError, "pvCommercial subobject"):
            self.store.create("GEN", "no_commercial.GEN", GEN.replace("PVObject_Commercial=pvCommercial", "Comment=pvCommercial"))
        with self.assertRaisesRegex(ValueError, "TConverter and TCubicProfile"):
            self.store.create("OND", "no_curve.OND", OND.replace("ProfilPIO=TCubicProfile", "Curve=TCubicProfile"))
        self.assertFalse(self.files["GEN"].with_name("danger.GEN").exists())
        self.assertFalse(self.files["BTR"].with_name("broken.BTR").exists())
    def test_legacy_pan_bytes_validate_and_compare(self):
        legacy = b"pvModule;legacy\n\x00\xff\r\npvCommercial;;"
        source = self.files["PAN"]
        source.write_bytes(legacy)
        source.with_name("legacy_copy.PAN").write_bytes(legacy)
        self.assertEqual(self.store.validate("PAN", source.name)["format"], "legacy-binary")
        with self.assertRaisesRegex(ValueError, "Legacy binary"): self.store.update("PAN", source.name, {"Model": "Other"})
        self.assertNotIn("diff", self.store.compare("PAN", source.name, "legacy_copy.PAN"))
    def test_update_backup_compare_bom_and_no_overwrite(self):
        with self.subTest(case="update_backup_compare"):
            for kind in self.files:
                with self.subTest(kind=kind):
                    original = self.files[kind].read_bytes()
                    target = f"fresh.{kind}"
                    self.store.create(kind, target, original.decode("utf-8").replace("Manufacturer=Lab", "Manufacturer=New"))
                    with self.assertRaises(FileExistsError): self.store.create(kind, target, original.decode("utf-8"))
                    changed = self.store.update(kind, f"sample.{kind}", {"Model": "Updated"})
                    self.assertEqual(self.store.validate(kind, f"sample.{kind}")["model"], "Updated")
                    backup = self.workspace / "ComposPV" / ".mcp-backups" / kind / changed["backup_name"]
                    self.assertEqual(backup.read_bytes(), original)
            self.assertFalse(self.store.compare("GEN", "sample.GEN", "fresh.GEN")["same_bytes"])
            self.assertIn("-    Manufacturer=Lab", self.store.compare("GEN", "sample.GEN", "fresh.GEN")["diff"])
        with self.subTest(case="preserve_bom"):
            source = self.files["OND"]
            source.write_bytes(b"\xef\xbb\xbf" + OND.encode("utf-8"))
            self.store.update("OND", source.name, {"Model": "Updated"})
            self.assertTrue(source.read_bytes().startswith(b"\xef\xbb\xbf"))
    def test_known_value_checks_and_unsupported_fields(self):
        original = self.files["GEN"].read_bytes()
        for value in ("0", "-5", "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError): self.store.update("GEN", "sample.GEN", {"CFuelHor": value})
        self.assertEqual(self.files["GEN"].read_bytes(), original)
        with self.assertRaises(ValueError): self.store.update("GEN", "sample.GEN", {"PNomGen": "0"})
        with self.assertRaises(ValueError): self.store.update("BTR", "sample.BTR", {"AlphaSOC": "0.85"})
        with self.assertRaises(ValueError): self.store.update("BTR", "sample.BTR", {"SelfDisch_Temp": "9"})
        with self.assertRaises(ValueError): self.store.update("BTR", "sample.BTR", {"BattTechnol": "Li_NMC"})
        with self.assertRaises(ValueError): self.store.update("PAN", "sample.PAN", {"Voc": "10"})
        with self.assertRaises(ValueError): self.store.update("OND", "sample.OND", {"VAbsMax": "100"})
        with self.assertRaises(ValueError): self.store.update("GEN", "sample.GEN", {"Model": "x\nCFuelHor=0"})
        with self.assertRaisesRegex(ValueError, "Incomplete"): self.store.create("GEN", "unsafe.GEN", GEN.replace("  CFuelHor=0.25\n", ""))
        self.assertFalse(self.files["GEN"].with_name("unsafe.GEN").exists())
        self.store.update("GEN", "sample.GEN", {"TypeGen": "CustomFuel"})
        self.assertEqual(self.store.validate("GEN", "sample.GEN")["known_value_errors"], [])
        li_btr = ("PVObject_=pvBattery\n  Version=8.1.7\n  Flags=$00\n" "  PVObject_Commercial=pvCommercial\n    Manufacturer=Lab\n    Model=LFP180\n"
            "  End of PVObject pvCommercial\n  BattTechnol=Li_LFP\n  CapaRef=180\n" "End of PVObject pvBattery\n")
        self.store.create("BTR", "li_sample.BTR", li_btr)
        li_info = self.store.validate("BTR", "li_sample.BTR")
        self.assertEqual(li_info["warnings"], [])
        self.assertEqual(li_info["known_value_errors"], [])
        no_cap_btr = BTR.replace("  CapNomC10=160\n", "")
        with self.assertRaisesRegex(ValueError, "CapNomC10"): self.store.create("BTR", "pb_missing.BTR", no_cap_btr)
        hyb_ond = OND.replace("    PNomConv=50\n", "    IsHybrid=True\n    PNomConv=50\n")
        self.store.create("OND", "hybrid_meta.OND", hyb_ond)
        hyb_elec = self.store.inspect("OND", "hybrid_meta.OND")["electrical"]
        self.assertEqual(hyb_elec["modes"]["IsHybrid"], "True")
        self.assertTrue(any(item["field"] == "IsHybrid" for item in hyb_elec["unverified"]))
    def test_paths_and_backup_restrictions(self):
        for bad in ("../sample.GEN", "..\\sample.GEN", "C:\\sample.GEN", "sample.OND", "sample.GEN.", "CON/GEN"):
            with self.subTest(bad=bad), self.assertRaises(ValueError): self.store.create("GEN", bad, GEN)
        saved = self.store.backup("GEN", "sample.GEN")
        backup_file = self.workspace / "ComposPV" / ".mcp-backups" / "GEN" / saved["backup_name"]
        meta_file = backup_file.with_name(backup_file.name + ".json")
        self.assertTrue(backup_file.is_file())
        self.assertTrue(meta_file.is_file())
        self.assertEqual(json.loads(meta_file.read_text(encoding="utf-8")), {"version": 2, "type": "GEN", "name": "sample.GEN", "sha256": saved["sha256"]})
        self.assertEqual(self.store.list("OND", "builtin")["total"], 1)
    def test_reject_symlink_outside_workspace(self):
        external = self.workspace.parent / "external.GEN"
        external.write_text(GEN, encoding="utf-8")
        path = self.files["GEN"]
        path.unlink()
        try:
            path.symlink_to(external)
        except OSError: self.skipTest("Symlink creation is unavailable on this Windows host")
        with self.assertRaisesRegex(ValueError, "Symlink"): self.store.inspect("GEN", path.name)
        with self.assertRaisesRegex(ValueError, "Symlink"): self.store.backup("GEN", path.name)
        self.assertEqual(external.read_text(encoding="utf-8"), GEN)
    def test_project_references_without_false_missing_claim(self):
        projects = self.workspace / "Projects"
        projects.mkdir()
        (projects / "fixture.PRJ").write_text("PVObject_=pvProject\n", encoding="utf-8")
        (projects / "fixture.VC0").write_text("PVModule=sample.PAN\nGInverter=BuiltIn.OND\n"
            "BatteryFile=sample.BTR\nPVObject_System=pvSystem\n" "  Flags=$60\n  GensetFile=sample.GEN\n  PEffBackUp=40\n"
            "End of PVObject pvSystem\nVBkUpEncl_syst=0.9\n", encoding="utf-8")
        report = self.store.dependencies("fixture.PRJ", "VC0")
        statuses = {ref["type"]: ref["status"] for ref in report["references"]}
        self.assertEqual(statuses, {"PAN": "workspace", "OND": "unknown-or-builtin-db", "BTR": "workspace", "GEN": "workspace"})
        self.assertTrue(report["generator_configuration"]["enabled_flag"])
        self.assertEqual(report["generator_configuration"]["effective_backup_kw"], 40)
        with self.assertRaises(ValueError): self.store.dependencies("../fixture", "VC0")
    def test_subarray_thresholds_report_values_per_array(self):
        projects = self.workspace / "Projects"
        projects.mkdir()
        (projects / "fixture.PRJ").write_text("PVObject_=pvProject\n", encoding="utf-8")
        (projects / "fixture.VC0").write_text("PVObject_=pvVariant\n" "PVObject_=pvSubArray\nVBkUpEncl_syst=0.250\n" "VBkUpDecl_syst=0.450\nEnd of PVObject pvSubArray\n"
            "PVObject_=pvSubArray\nVBkUpEncl_syst=0.350\n" "End of PVObject pvSubArray\n"
            "PVObject_System=pvSystem\nFlags=$20\nPEffBackUp=40\n" "GensetFile=sample.GEN\nEnd of PVObject pvSystem\n", encoding="utf-8")
        configuration = self.store.dependencies("fixture.PRJ", "VC0")["generator_configuration"]
        self.assertTrue(configuration["enabled_flag"])
        self.assertEqual(configuration["effective_backup_kw"], 40)
        self.assertEqual(configuration["subarray_thresholds"], [{"index": 0, "line": 2, "enclosure": "0.250", "release": "0.450"}, {"index": 1, "line": 6, "enclosure": "0.350", "release": None}])
        self.assertTrue(configuration["enclosure_threshold_present"])
        self.assertTrue(configuration["release_threshold_present"])
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
            self.assertEqual(server.components().inspect("OND", "official.OND", "builtin")["type"], "OND")
            second = install / "DataRO" / "PVsyst8.0_Data" / "ComposPV"
            second.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "Multiple DataRO component libraries"): server.components()
        with patch.dict(os.environ, {"PVSYST_BUILTIN_COMPONENTS": str(official.parent)}):
            self.assertEqual(api_components.pvsyst_list_components("OND", "builtin")["total"], 1)
    def test_native_closing_tags_root_block_and_object_starts(self):
        oriented = ("PVObject_=pvVCalcul\r\n  Version=8.1.6\r\n  Orientations, list of=1 TOrientGroup\r\n"
                    "    PVObject_=pvOrient\r\n      Comment=End of TOrientGroup\r\n      NoOrient=1\r\n"
                    "    End of TOrientGroup  \r\n  End of Orientations\r\nEnd of PVObject pvVCalcul\r\n")
        dome = ("PVObject_=pvVCalcul\n  Orientations, list of=1 TOrientGroup\n    PVObject_=pvDomeOrient\n" "      NoOrient=1\n    End of TDomeOrientGroup\n  End of Orientations\nEnd of PVObject pvVCalcul\n")
        self.assertEqual([b.object_type for b in objects(oriented)], ["pvVCalcul", "pvOrient"])
        self.assertEqual(objects(oriented)[1].fields["Comment"], [(4, "End of TOrientGroup")])
        self.assertEqual([b.object_type for b in objects(dome)], ["pvVCalcul", "pvDomeOrient"])
        with self.assertRaisesRegex(ValueError, "Unmatched object closing tag"):
            objects("PVObject_=pvVCalcul\n  PVObject_=pvOrient\n  End of TDomeOrientGroup\nEnd of PVObject pvVCalcul\n")
        self.assertEqual(root_block(["Comment=outside\n", *oriented.splitlines(keepends=True)]).object_type, "pvVCalcul")
        self.assertTrue(has_object(["  PVObject_UserNeeds = pvLoad\n"], "pvLoad"))
        self.assertFalse(has_object(["  PVObject_GridStorage=pvGridStorage"], "pvLoad"))
    def test_batchsimul_owns_fields_up_to_its_endtags(self):
        text = ("PVObject_=pvVCalcul\n  BatchSimul = TBatchSimul\n    Version=8.1.0\n" "    ParamsFileName=Batch.CSV\n  EndTags  \n  Version=8.1.7\n" "  Comment=TBatchSimul\nEnd of PVObject pvVCalcul\n")
        root, = objects(text)
        self.assertEqual(root.one("Version"), (5, "8.1.7"))
        self.assertEqual(root.one("Comment"), (6, "TBatchSimul"))
        self.assertNotIn("ParamsFileName", root.fields)
        self.assertNotIn("BatchSimul", root.fields)
        with self.assertRaisesRegex(ValueError, "Unmatched object closing tag"): objects(text.replace("  EndTags  \n", ""))
    def test_class_name_as_commercial_model_is_an_ordinary_field(self):
        self.files["PAN"].write_text(PAN.replace("Model=440W", "Model=TPrintSettings"), encoding="utf-8")
        report = self.store.validate("PAN", "sample.PAN")
        self.assertEqual(report["known_value_errors"], [])
        commercial = self.store.inspect("PAN", "sample.PAN")["commercial"]
        self.assertTrue(commercial["editable"], commercial)
        self.assertEqual(commercial["fields"]["Model"]["value"], "TPrintSettings")
    def test_native_private_frames_keep_their_owners(self):
        with self.subTest(record="PrintSettings"):
            text = ("PVObject_=pvProject\nPrintSettings=TPrintSettings\n  PCNDec=1\nEndTags\n" "MeteoFileName=Demo.MET\nEnd of PVObject pvProject\n")
            project, = objects(text)
            self.assertNotIn("PCNDec", project.fields)
            self.assertEqual(project.one("MeteoFileName")[1], "Demo.MET")
        with self.subTest(record="HouseHoldUses"):
            text = ("PVObject_UserNeeds=pvLoad\n  UsesModul=Seasonal\n" "  HouseHoldUses=THouseHoldUses\n    UsesModul=Seasonal\n  End of HouseHoldUses\n" "End of PVObject pvLoad\n")
            load, = objects(text)
            self.assertEqual(load.one("UsesModul"), (1, "Seasonal"))
        with self.subTest(record="WaterUse"):
            text = ("PVObject_UserNeeds=pvLoad\n  WaterUse=TWaterUse\n    FlowModul=Yearly\n" "    PVObject_HydroCircuit=pvHydrCircuit\n      Version=8.1.7\n      Flags=$01\n"
                    "      Fluide=TFluide\n        FluidType=Water\n      End of TFluide\n" "      CircElement, list of=1 tPipe\n        Pipe=1\n          Version=8.1.7\n"
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
        with self.assertRaises(ValueError) as missing: objects(missing_text)[0].one("MissingField")
        self.assertEqual(str(missing.exception), "MissingField must occur exactly once in pvVCalcul at line 1 (found 0)")
        duplicate_text = ("PVObject_=pvVCalcul\n  Version=8.1.6\n  Version=8.1.7\n" "End of PVObject pvVCalcul\n")
        with self.assertRaises(ValueError) as duplicate: objects(duplicate_text)[0].one("Version")
        self.assertEqual(str(duplicate.exception), "Version must occur exactly once in pvVCalcul at line 1 " "(found 2, at lines 2, 3)")
    def test_replace_value_and_key_reject_line_breaks(self):
        for brk in ("\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.assertRaises(ValueError): replace_value("  Comment=Old\r\n", f"New{brk}Injected=1")
            with self.assertRaises(ValueError): replace_key("A=1\r\n", f"B{brk}C")
        self.assertEqual(replace_value("  Comment=Old \t\r\n", "New\tName"), "  Comment=New\tName \t\r\n")
        self.assertEqual(replace_value("A=1", "2"), "A=2")
    def test_ageing_nesting_and_commercial_identity_precedence(self):
        ageing = "  PVObject_Ageing=pvAgeing\n    Flags=$00\n  End of TAgeing\n"
        decoy = "  PVObject_Meta=pvMeta\n    Manufacturer=Decoy\n    Model=Decoy\n  End of PVObject pvMeta\n"
        for kind, text, closer in (("PAN", PAN, "End of PVObject pvModule"), ("OND", OND, "End of PVObject pvGInverter"), ("BTR", BTR, "End of PVObject pvBattery"), ("GEN", GEN, "End of PVObject pvGenerator")):
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
        for bad_pan, err in ((PAN.replace("Vmp=42.1", "Vmp=49.6"), "Voc must be greater than Vmp"), (PAN.replace("Imp=10.45", "Imp=11.05"), "ISC must be greater than Imp"),
                             (GUI_PAN.replace("Imp=10.45", "Imp=11.05"), "ISC must be greater than Imp")):
            with self.assertRaisesRegex(ValueError, err): self.store.create("PAN", "bad_ref.PAN", bad_pan)
        for val in ("\uff14\uff14\uff10", "4_40", " 440"):
            with self.assertRaisesRegex(ValueError, "PNom must be a finite number"): self.store.update("PAN", "sample.PAN", {"PNom": val})
        with self.assertRaisesRegex(ValueError, "YearBeg must be a"): commercial._commercial_value("YearBeg", "\uff12\uff10\uff12\uff14")
    def test_sourced_builder_failure_race_and_nested_converter(self):
        def build(kind="PAN", fields=(("PNom", 440, "W", "440", "Wp"),), **options):
            text = "Mono 440W\n" + "\n".join(n + u for _, _, _, n, u in fields)
            return build_sourced_component(self.store, component_type=kind, template_name=f"sample.{kind}", new_name=f"Built.{kind}",
                manufacturer="Fixture", model="Mono 440W",
                sources={"doc": {"kind": "assumption", "title": "Fixture", "locator": "fixture://c", "model": "Mono 440W", "text": text}},
                declarations=[{"field": f, "value": v, "unit": u, "source_id": "doc", "excerpt": n + q, "source_value_text": n, "source_unit_text": q} for f, v, u, n, q in fields], **options)
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
        with patch.object(self.store, "_read", side_effect=racing_read): raced = build(dry_run=False, acknowledge_inherited_model=True)
        self.assertEqual(raced["status"], "failed")
        self.assertEqual(manifest.read_text(encoding="utf-8"), "foreign")
        self.assertFalse(target.exists())
        self.files["OND"].write_text(OND.replace("  Converter=TConverter", "  PVObject_Meta=pvMeta\n  Converter=TConverter")
               .replace("  End of TConverter\n", "  End of TConverter\n  End of PVObject pvMeta\n"), encoding="utf-8")
        blocked = build(kind="OND", fields=(("PNomConv", 50, "kW", "50", "kW"),))
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("electrical fields are unreadable", blocked["issues"][-1]["message"])
CAPACITY_POINTS = [[5, 0.85], [10, 1], [25, 1.1], [70, 1.25], [130, 1.35], [200, 1.4]]
CAPACITY_PROFILE = '  Capa_DischRate=TCubicProfile\n    NPtsMax=8\n    NPtsEff=6\n    Mode=1\n    LastCompile=$008D\n    Point_1=5,0.85\n    Point_2=10,1\n    Point_3=25,1.1\n    Point_4=70,1.25\n    Point_5=130,1.35\n    Point_6=200,1.4\n    Point_7=0,0\n    Point_8=0,0\n  End of TCubicProfile\n'
CURVED_BTR = BTR.replace("End of PVObject pvBattery", CAPACITY_PROFILE + "End of PVObject pvBattery")
class BatteryEditorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ComponentStore(self.root)
        self.store.create("BTR", "test.BTR", CURVED_BTR)
        self.path = self.root / "ComposPV" / "Batteries" / "test.BTR"
    def sha(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()
    def edit(self, **kwargs):
        args = {"updates": {}, "expected_sha256": self.sha(), "use_file_curve": True, "curve_updates": {"Capa_DischRate": CAPACITY_POINTS}}
        args.update(kwargs)
        return self.store.update("BTR", "test.BTR", **args)
    def rejected(self, **kwargs):
        original = self.path.read_bytes()
        with self.assertRaises(ValueError): self.edit(**kwargs)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
    def test_ratio_interpolation_boundaries_and_no_extrapolation(self):
        self.assertAlmostEqual(capacity_ratio(CAPACITY_POINTS), 1.3)
        for y in (1.15, 1.45):
            with self.subTest(case="boundary", y=y):
                self.assertEqual(capacity_ratio([[1, 0.8], [10, 1], [100, y], [200, 1.5]]), y)
                self.assertEqual(capacity_ratio([[100, y], [110, 1.46], [120, 1.48], [200, 1.5]]), y)
        for y in (1.14999, 1.45001):
            with self.subTest(case="out_of_range", y=y), self.assertRaises(ValueError): capacity_ratio([[1, 0.8], [10, 1], [100, y], [200, 1.5]])
    def test_inspection_and_explicit_activation_preview_apply_restore(self):
        info = self.store.inspect("BTR", "test.BTR")["curves"]
        self.assertEqual(info["control"]["source"], "unrecognized-tag")
        other, capacity = info["items"]
        self.assertFalse(other["editable"])
        self.assertEqual(other["cli_tag"], "IAutoShape")
        self.assertEqual(capacity["cli_tag"], "CapaCourant")
        self.assertTrue(capacity["editable"])
        self.assertTrue(capacity["requires_use_file_curve"])
        self.assertEqual(capacity["points"], CAPACITY_POINTS)
        self.assertAlmostEqual(capacity["ratio_100h"], 1.3)
        self.assertEqual(capacity["value_errors"], [])
        text = CURVED_BTR.replace("  Capa_DischRate=", "  Capa_DischRate \t=  ").replace("    Manufacturer=Lab", "    Flags=$AB\n    Manufacturer=Lab")
        original = b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode()
        self.path.write_bytes(original)
        preview = self.edit(updates={"Model": "Custom"}, dry_run=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
        self.assertEqual(preview["curve_control_before"]["path"], "Capa_DischRate")
        self.assertEqual(preview["curve_control_after"]["path"], "CapaCourant")
        self.assertEqual(preview["curve_control_after"]["source"], "file")
        edited = self.edit(updates={"Model": "Custom"})
        self.assertEqual(self.path.read_bytes(), original.replace(b"Capa_DischRate", b"CapaCourant").replace(b"Model=160Ah", b"Model=Custom"))
        self.assertEqual((self.root / "ComposPV" / ".mcp-backups" / "BTR" / edited["backup_name"]).read_bytes(), original)
    def test_canonical_noop_padded_edit_and_value_error_repair(self):
        with self.subTest(case="canonical_noop_and_padded_edit"):
            canon_text = CURVED_BTR.replace("Capa_DischRate", "CapaCourant").replace("5,0.85", "5.0,0.850")
            self.path.write_text(canon_text, encoding="utf-8")
            args = {"use_file_curve": False, "curve_updates": {"CapaCourant": CAPACITY_POINTS}}
            self.assertFalse(self.edit(**args)["changed"])
            self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
            points = [pair[:] for pair in CAPACITY_POINTS]
            points[2] = [25, 1.125]
            self.edit(use_file_curve=False, curve_updates={"CapaCourant": points})
            self.assertEqual(self.path.read_text(encoding="utf-8"), canon_text.replace("25,1.1", "25,1.125"))
        with self.subTest(case="value_error_inspection_and_repair"):
            broken = CURVED_BTR.replace("70,1.25", "7,1.25").replace("130,1.35", "8,1.35").replace("200,1.4", "9,1.4")
            self.path.write_text(broken, encoding="utf-8")
            curve = self.store.inspect("BTR", "test.BTR")["curves"]["items"][1]
            self.assertTrue(curve["structure_complete"])
            self.assertTrue(curve["editable"])
            self.assertTrue(curve["value_errors"])
            self.assertIsNone(curve["ratio_100h"])
            self.assertTrue(self.edit(dry_run=True)["changed"])
    def test_hash_activation_technology_and_version_guards(self):
        with self.subTest(case="hash_and_activation_requirements"):
            self.rejected(expected_sha256="0" * 64)
            no_hash = self.edit(expected_sha256=None, dry_run=True)
            self.assertFalse(no_hash["conflict_checked"])
            self.rejected(use_file_curve=False)
            self.rejected(curve_updates={"CapaCourant": CAPACITY_POINTS})
            self.rejected(curve_updates={"SelfDisch_Temp": CAPACITY_POINTS})
            self.rejected(curve_updates={"Capa_DischRate": CAPACITY_POINTS, "CapaCourant": CAPACITY_POINTS})
        for technology in ("Li_LFP", "NiCd_Open_Plates", "Pb Sealed AGM", "None", "unknown"):
            with self.subTest(case="unsupported_technology", technology=technology):
                self.path.write_text(CURVED_BTR.replace("Pb_Sealed_AGM", technology), encoding="utf-8")
                self.rejected()
                info = self.store.inspect("BTR", "test.BTR")["curves"]
                self.assertIsNone(info["control"]["ratio_100h_range"])
                self.assertFalse(info["items"][1]["editable"])
        with self.subTest(case="unsupported_version"):
            self.path.write_text(CURVED_BTR.replace("Version=8.1.6", "Version=8.0.0"), encoding="utf-8")
            self.rejected()
        for technology in ("Pb_Sealed_AGM", "btPb_Sealed_AGM", "pb_sealed_gel", "BTPB_SEALED_GEL"):
            with self.subTest(case="supported_technology", technology=technology):
                self.path.write_text(CURVED_BTR.replace("Pb_Sealed_AGM", technology), encoding="utf-8")
                self.assertTrue(self.edit(dry_run=True)["changed"])
    def test_malformed_blocks_profiles_and_bad_points_rejected(self):
        block_cases = [CURVED_BTR.replace(CAPACITY_PROFILE, CAPACITY_PROFILE * 2),
            CURVED_BTR.replace(CAPACITY_PROFILE, CAPACITY_PROFILE + CAPACITY_PROFILE.replace("Capa_DischRate", "CapaCourant")), CURVED_BTR.replace(CAPACITY_PROFILE, ""),
            CURVED_BTR.replace(CAPACITY_PROFILE, "").replace("  End of PVObject pvCommercial", CAPACITY_PROFILE + "  End of PVObject pvCommercial"),
            CURVED_BTR.replace("  BattTechnol=Pb_Sealed_AGM", "  BattTechnol=Pb_Sealed_AGM\n  BattTechnol=Pb_Sealed_Gel")]
        for idx, text in enumerate(block_cases):
            with self.subTest(case="malformed_block", idx=idx):
                self.path.write_text(text, encoding="utf-8")
                self.rejected()
        for old, new in (("NPtsEff=6", "NPtsEff=3"), ("NPtsMax=8", "NPtsMax=9000"), ("Point_8=0,0", "Point_7=0,0"), ("Point_8=0,0", "Point_8=nan,0"), ("Mode=1", "Mode=2"), ("Mode=1", "Other=1")):
            with self.subTest(case="malformed_profile", old=old, new=new):
                self.path.write_text(CURVED_BTR.replace(old, new), encoding="utf-8")
                self.rejected()
        self.path.write_text(CURVED_BTR, encoding="utf-8")
        point_cases = [CAPACITY_POINTS[:3], [[i + 1, 1.3] for i in range(257)], "points", None, [[x, y * 0.8] for x, y in CAPACITY_POINTS],
                       [[x, y * 1.2] for x, y in CAPACITY_POINTS], [[x / 10, y] for x, y in CAPACITY_POINTS], [[x + 100, y] for x, y in CAPACITY_POINTS]]
        for point in ([True, 0.8], ["5", 0.8], [0, 0.8], [5, 0], [5, 2], [5, float("nan")], [float("inf"), 0.8], [10, 0.8], [10 ** 400, 1]):
            point_cases.append([point] + CAPACITY_POINTS[1:])
        for points in point_cases:
            with self.subTest(points=points): self.rejected(updates={"Model": "Should not write"}, curve_updates={"Capa_DischRate": points})
COMMERCIAL = '    DataSource=Fixture\n    Width = 1.032\n    Height=1.996\n    Weight=24.5\n    Remarks, Count=2\n      Str_1=First\n      Str_2=Second\n    End of Remarks=Second\n'
class CommercialTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = ComponentStore(self.root)
        self.paths = {}
        for kind, content in {"PAN": PAN, "OND": OND, "BTR": BTR, "GEN": GEN}.items():
            content = content.replace("  Version=", "  Comment=Lab\tFixture\tStatus\n  Version=")
            content = content.replace("  End of PVObject pvCommercial", COMMERCIAL + "  End of PVObject pvCommercial")
            self.store.create(kind, f"test.{kind}", content)
            self.paths[kind] = self.store._path(kind, f"test.{kind}")
    def edit(self, kind="PAN", **kwargs):
        args = {"updates": {}, "expected_sha256": hashlib.sha256(self.paths[kind].read_bytes()).hexdigest()}
        return self.store.update(kind, f"test.{kind}", **(args | kwargs))
    def test_form_all_types_preview_apply_restore_and_noop(self):
        pan_path = self.paths["PAN"]
        pan_initial = pan_path.read_bytes()
        with self.subTest(case="pan_noop_and_bom_whitespace"):
            source = pan_path.read_text(encoding="utf-8").replace("Width = 1.032", "Width \t=  1.032  ")
            pan_path.write_bytes(b"\xef\xbb\xbf" + source.replace("\n", "\r\n").encode())
            noop_orig = pan_path.read_bytes()
            result = self.edit(commercial_updates={"Width": "1.032"}, remarks=["First", "Second"])
            self.assertFalse(result["changed"])
            self.assertNotIn("backup_name", result)
            self.assertEqual(pan_path.read_bytes(), noop_orig)
            self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
            self.edit(commercial_updates={"Depth": "0.031"}, remarks=["One", "Two", "Three", "Four", "Five"])
            data = pan_path.read_bytes()
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
            self.assertIn(b"Width \t=  1.032  ", data)
        pan_path.write_bytes(pan_initial)
        for kind, path in self.paths.items():
            with self.subTest(kind=kind):
                original = path.read_bytes()
                cform = self.store.inspect(kind, path.name)["commercial"]
                self.assertTrue(cform["editable"])
                self.assertEqual(cform["remarks"], ["First", "Second"])
                self.assertEqual(cform["fields"]["Width"]["unit"], "m")
                args = {"commercial_updates": {"Manufacturer": "Research Lab", "Model": "Edited", "Width": "1.2", "Height": "2.1", "Depth": "0.04", "Weight": "26",
                        "YearBeg": "2026", "NPieces": "2", "PriceDate": "30/09/26 00:15"}, "remarks": ["Alpha = beta", "设备资料", "Third"]}
                self.edit(kind, dry_run=True, **args)
                self.assertEqual(path.read_bytes(), original)
                edited = self.edit(kind, **args)
                content = path.read_text(encoding="utf-8")
                self.assertIn("Comment=Research Lab\tEdited\tStatus", content)
                self.assertIn("Width = 1.2", content)
                self.assertIn("Remarks, Count=3", content)
                self.assertIn("End of Remarks=Third", content)
                if kind == "PAN":
                    self.assertIn("LargApp=1.2", content)
                    self.assertIn("LongApp=2.1", content)
                self.assertEqual(self.store.inspect(kind, path.name)["commercial"]["remarks"], args["remarks"])
                self.assertEqual((self.root / "ComposPV" / ".mcp-backups" / kind / edited["backup_name"]).read_bytes(), original)
                path.write_bytes(original)
    def test_remarks_optional_fields_scoping_and_shared_snapshot(self):
        with self.subTest(case="clear_and_add_remarks_optional_fields"):
            pan_path = self.paths["PAN"]
            self.edit(remarks=[])
            self.assertNotIn(b"Remarks", pan_path.read_bytes())
            opt = self.edit(commercial_updates={"DataSource": "", "Depth": "0.03"}, remarks=["A", ""])
            form = opt["commercial_after"]
            self.assertEqual(form["remarks"], ["A", ""])
            self.assertEqual(form["fields"]["DataSource"]["value"], "")
            self.assertEqual(form["fields"]["Depth"]["value"], "0.03")
        gen_path = self.paths["GEN"]
        gen_base = gen_path.read_bytes()
        with self.subTest(case="scoped_scalar_preserves_outer_weight"):
            scoped_src = gen_path.read_text(encoding="utf-8").replace("  TypeGen=", "  Weight=123\n  TypeGen=")
            gen_path.write_text(scoped_src, encoding="utf-8")
            self.edit("GEN", commercial_updates={"Weight": "55"})
            self.assertIn("  Weight=123\n", gen_path.read_text(encoding="utf-8"))
            self.assertIn("    Weight=55\n", gen_path.read_text(encoding="utf-8"))
        with self.subTest(case="scalar_and_commercial_share_one_snapshot"):
            gen_path.write_bytes(gen_base)
            shared = self.edit("GEN", updates={"PNomGen": "55"}, commercial_updates={"Model": "55 kVA"})
            self.assertIn(b"PNomGen=55", gen_path.read_bytes())
            self.assertEqual((self.root / "ComposPV" / ".mcp-backups" / "GEN" / shared["backup_name"]).read_bytes(), gen_base)
    def test_guard_invalid_values_and_conflicting_arguments_leave_no_backup(self):
        bad = [{"expected_sha256": "0" * 64, "commercial_updates": {"Weight": "5"}}, {"commercial_updates": {"Flags": "$00"}}, {"commercial_updates": {"Weight": "nan"}},
            {"commercial_updates": {"Weight": "inf"}}, {"commercial_updates": {"Width": "0"}},
            {"commercial_updates": {"Depth": "-1"}}, {"commercial_updates": {"NPieces": "1.5"}},
            {"commercial_updates": {"NPieces": "2147483648"}}, {"commercial_updates": {"YearBeg": "-1"}},
            {"commercial_updates": {"PriceDate": "31/02/26 10:00"}}, {"commercial_updates": {"Model": ""}},
            {"commercial_updates": {"Model": "line\nbreak"}}, {"commercial_updates": {"Model": "tabs\tbreak"}},
            {"commercial_updates": {"Model": "Unicode\u2028break"}}, {"commercial_updates": {"Model": True}},
            {"commercial_updates": {"Model": "x" * 2049}}, {"remarks": ["a"] * 6}, {"remarks": "not an array"}, {"remarks": [None]},
            {"updates": {"Model": "a"}, "commercial_updates": {"Model": "b"}}]
        original = self.paths["PAN"].read_bytes()
        for args in bad:
            with self.subTest(args=str(args)[:120]), self.assertRaises(ValueError): self.edit(**args)
        self.assertEqual(self.paths["PAN"].read_bytes(), original)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
    def test_ambiguous_arrays_and_pan_dimension_counterparts(self):
        path = self.paths["PAN"]
        with self.subTest(case="pan_dimension_counterparts"):
            data = path.read_text(encoding="utf-8").replace("  LargApp=1.032", "  Other=1.032")
            path.write_text(data, encoding="utf-8")
            self.edit(commercial_updates={"Width": "1.1"})
            self.assertNotIn("LargApp", path.read_text(encoding="utf-8"))
            self.assertIn("Width = 1.1", path.read_text(encoding="utf-8"))
            dup_data = data.replace("  Other=1.032", "  LargApp=1.032\n  LargApp=1.032")
            path.write_text(dup_data, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "LargApp must occur exactly once"): self.edit(commercial_updates={"Width": "1.1"})
            self.assertEqual(path.read_text(encoding="utf-8"), dup_data)
        clean = PAN.replace("  Version=", "  Comment=Lab\tFixture\tStatus\n  Version=").replace("  End of PVObject pvCommercial", COMMERCIAL + "  End of PVObject pvCommercial")
        bad = [clean.replace("Count=2", "Count=3"), clean.replace("Str_2=", "Str_1="), clean.replace("End of Remarks=Second", "End of Remarks=Wrong"),
               clean.replace("    Weight=24.5", "    Weight=24.5\n    Weight=5"), clean.replace("    Remarks, Count=2", "    Remarks, Count=-1"),
               clean.replace(COMMERCIAL, COMMERCIAL + COMMERCIAL), clean.replace(COMMERCIAL, "    Str_1=Orphan\n"),
               clean.replace(COMMERCIAL, COMMERCIAL + "    Str_3=Orphan\n"), clean.replace("Version=8.1.6", "Version=8.0.6"),
               clean.replace("    Weight=24.5", "    PVObject_Price=pvPrice\n    End of PVObject pvPrice")]
        for text in bad:
            with self.subTest(case="ambiguous_array", text=text[:100]):
                path.write_text(text, encoding="utf-8")
                self.assertFalse(commercial_inventory("PAN", text)["editable"])
                with self.assertRaises(ValueError): self.edit(remarks=["Repair?"])
                self.assertEqual(path.read_text(encoding="utf-8"), text)
class CommercialComponentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "work"
        for sub in ("ComposPV/PVmodules", "ComposPV/Inverters", "ComposPV/Batteries", "Results"): (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.store = ComponentStore(self.root)
    def test_single_diode_solver_and_zero_template_pan_synthesis(self):
        # Realistic 620 Wp N-type TOPCon bifacial module (72 half-cut cells)
        diode = derive_single_diode_parameters(voc=52.8, isc=14.65, vmp=44.6, imp=13.91, n_cells_s=72)
        self.assertGreater(diode["RShunt"], 100.0)
        self.assertGreater(diode["RSerie"], 0.01)
        self.assertGreaterEqual(diode["Gamma"], 0.85)
        self.assertLessEqual(diode["Gamma"], 1.50)
        self.assertLess(diode["mpp_slope_residual_a"], 0.15)
        pan_text, meta = synthesize_pan_from_datasheet(manufacturer="ApexSolar", model="HiTOP 620W Bifacial", pnom_w=620.0,
            isc_a=14.65, voc_v=52.8, imp_a=13.91, vmp_v=44.6, width_m=1.134, height_m=2.382, n_cells_s=72, n_cells_p=2, bifaciality_factor=0.80, vmax_iec_v=1500.0)
        self.assertTrue(meta["bifacial"])
        self.assertFalse(meta["single_diode"]["fallback"])
        self.assertEqual(meta["flags"], "$00800000")
        self.assertIn("BifacialityFactor=0.800", pan_text)
        report = self.store._inspect("PAN", pan_text.encode("utf-8"))
        self.assertEqual(report["structural_errors"], [])
        self.assertEqual(report["known_value_errors"], [])
    def test_fallback_reports_actual_mpp_slope_residual(self):
        diode = derive_single_diode_parameters(voc=50.0, isc=13.0, vmp=42.0, imp=12.74, n_cells_s=96)
        self.assertTrue(diode["fallback"])
        self.assertGreater(diode["mpp_slope_residual_a"], 1.0)
    def test_zero_template_ond_synthesis_validates_cleanly(self):
        ond_text, meta = synthesize_ond_from_datasheet(manufacturer="GridPower", model="SG110CX-V2", pnom_ac_kw=110.0,
            pmax_ac_kw=110.0, vmpp_min_v=200.0, vmpp_max_v=1000.0, vabs_max_v=1100.0, effic_max_pct=98.7, effic_euro_pct=98.4, nb_mppt=9, nb_inputs=18, idc_max_a=270.0)
        self.assertEqual(meta["nb_mppt"], 9)
        self.assertEqual(meta["curve_points"], 8)
        report = self.store._inspect("OND", ond_text.encode("utf-8"))
        self.assertEqual(report["structural_errors"], [])
        self.assertEqual(report["known_value_errors"], [])
    def test_inverter_curve_does_not_exceed_declared_maximum_efficiency(self):
        for emax, eeuro in ((90.0, 89.0), (75.0, 73.0), (50.0, 48.0)):
            with self.subTest(effic_max_pct=emax, effic_euro_pct=eeuro):
                text, _ = synthesize_ond_from_datasheet(manufacturer="Lab", model="Inverter", pnom_ac_kw=10, vmpp_min_v=200, vmpp_max_v=850, vabs_max_v=1000, effic_max_pct=emax, effic_euro_pct=eeuro)
                points = [tuple(map(int, line.split("=", 1)[1].split(","))) for line in text.splitlines() if line.strip().startswith("Point_")]
                active = [(p_in, p_out) for p_in, p_out in points if p_in > 0 and p_out > 0]
                effs = [p_out / p_in for p_in, p_out in active]
                for eff in effs: self.assertLessEqual(eff, emax / 100.0 + 1e-9)
                # Peak at 50% load (index 5) exceeds both light-load (10%, index 2) and full-load (index 6)
                self.assertGreater(effs[5], effs[2])
                self.assertGreater(effs[5], effs[6])
                self.assertEqual(max(p_out for _, p_out in active), 10000)
    def test_build_sourced_component_zero_template_pan_and_ond_roundtrip(self):
        sources = {"DS1": {"kind": "manufacturer", "title": "ApexSolar 620W Datasheet", "locator": "ApexSolar_HiTOP620B_rev1.pdf", "text": (
                    "ApexSolar HiTOP-620B: PNom 620 W, Voc 52.8 V, Isc 14.65 A, " "Vmp 44.6 V, Imp 13.91 A, Width 1134 mm, Height 2382 mm, "
                    "BifacialityFactor 80 %, muVocSpec -0.25 %/degC, MuISC 0.045 %/degC, " "VMaxIEC 1500 V, NCelS 72 cells.")}}
        def decl(field, val, unit, excerpt):
            return {"field": field, "value": val, "unit": unit, "source_id": "DS1", "excerpt": excerpt, "source_value_text": str(val), "source_unit_text": unit}
        declarations = [decl("PNom", 620, "W", "PNom 620 W"), decl("Voc", 52.8, "V", "Voc 52.8 V"), decl("ISC", 14.65, "A", "Isc 14.65 A"),
            decl("Vmp", 44.6, "V", "Vmp 44.6 V"), decl("Imp", 13.91, "A", "Imp 13.91 A"),
            decl("Width", 1134, "mm", "Width 1134 mm"), decl("Height", 2382, "mm", "Height 2382 mm"),
            decl("BifacialityFactor", 80, "%", "BifacialityFactor 80 %"), decl("muVocSpec", -0.25, "%/degC", "muVocSpec -0.25 %/degC"),
            decl("MuISC", 0.045, "%/degC", "MuISC 0.045 %/degC"), decl("VMaxIEC", 1500, "V", "VMaxIEC 1500 V"), decl("NCelS", 72, "cells", "NCelS 72 cells")]
        pan_common = {"component_type": "PAN", "template_name": None, "new_name": "Apex_620W_Bifi.PAN", "manufacturer": "ApexSolar", "model": "HiTOP-620B", "sources": sources, "declarations": declarations}
        preview = build_sourced_component(self.store, **pan_common, dry_run=True)
        self.assertEqual(preview["status"], "needs_review", preview["issues"])
        self.assertTrue(preview["synthesized"])
        self.assertTrue(preview["model_refitted"])
        self.assertFalse((self.root / "ComposPV" / "PVmodules" / "Apex_620W_Bifi.PAN").exists())
        blocked = build_sourced_component(self.store, **pan_common, dry_run=False, acknowledge_inherited_model=False)
        self.assertEqual(blocked["status"], "blocked")
        created = build_sourced_component(self.store, **pan_common, dry_run=False, acknowledge_inherited_model=True)
        self.assertTrue(created["component_created"])
        self.assertTrue((self.root / "ComposPV" / "PVmodules" / "Apex_620W_Bifi.PAN").is_file())
        self.assertTrue((self.root / "Results" / "Apex_620W_Bifi.PAN.sources.json").is_file())
    def test_component_electrical_filtering_and_metadata_index_cache(self):
        pan_mono, _ = synthesize_pan_from_datasheet(manufacturer="Alpha", model="Mono-450", pnom_w=450.0, isc_a=11.5, voc_v=49.2, imp_a=10.8, vmp_v=41.7, width_m=1.05, height_m=2.10, vmax_iec_v=1000.0)
        pan_bifi, _ = synthesize_pan_from_datasheet(manufacturer="Beta", model="Bifi-620", pnom_w=620.0, isc_a=14.6, voc_v=52.8,
            imp_a=13.9, vmp_v=44.6, width_m=1.13, height_m=2.38, bifaciality_factor=0.80, vmax_iec_v=1500.0)
        ond_small, _ = synthesize_ond_from_datasheet(manufacturer="Gamma", model="Inv-10kW", pnom_ac_kw=10.0, vmpp_min_v=200.0, vmpp_max_v=850.0, vabs_max_v=1000.0, nb_mppt=2)
        ond_big, _ = synthesize_ond_from_datasheet(manufacturer="Delta", model="Inv-110kW", pnom_ac_kw=110.0, vmpp_min_v=200.0, vmpp_max_v=1000.0, vabs_max_v=1500.0, nb_mppt=9)
        (self.root / "ComposPV" / "PVmodules" / "Alpha_450.PAN").write_text(pan_mono, encoding="utf-8")
        (self.root / "ComposPV" / "PVmodules" / "Beta_620.PAN").write_text(pan_bifi, encoding="utf-8")
        (self.root / "ComposPV" / "Inverters" / "Gamma_10k.OND").write_text(ond_small, encoding="utf-8")
        (self.root / "ComposPV" / "Inverters" / "Delta_110k.OND").write_text(ond_big, encoding="utf-8")
        bifi_res = self.store.list("PAN", bifacial=True, min_power_w=550.0, min_system_voltage_v=1500.0)
        self.assertEqual(bifi_res["total"], 1)
        self.assertEqual(bifi_res["items"][0]["name"], "Beta_620.PAN")
        self.assertEqual(bifi_res["items"][0]["specs"]["pnom_w"], 620.0)
        self.assertTrue(bifi_res["items"][0]["specs"]["bifacial"])
        mono_res = self.store.list("PAN", bifacial=False, max_power_w=500.0)
        self.assertEqual([i["name"] for i in mono_res["items"]], ["Alpha_450.PAN"])
        ond_res = self.store.list("OND", min_power_w=50_000.0, min_mppt=6, min_system_voltage_v=1500.0)
        self.assertEqual([i["name"] for i in ond_res["items"]], ["Delta_110k.OND"])
        self.assertEqual(ond_res["items"][0]["specs"]["nb_mppt"], 9)
        # Default read-only list writes no disk index; persist_index=True persists cache under lock & cache hit avoids re-reading
        index_path = self.root / "ComposPV" / ".mcp-component-index.json"
        self.assertFalse(index_path.exists())
        self.store.list("PAN", persist_index=True)
        self.store.list("OND", persist_index=True)
        self.assertTrue(index_path.is_file())
        saved_index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertIn("PAN", saved_index)
        self.assertIn("OND", saved_index)
        with patch.object(self.store, "_read", side_effect=AssertionError("Should hit mtime cache")): cached = self.store.list("PAN", manufacturer="Beta")
        self.assertEqual(cached["total"], 1)
    def test_zero_template_li_ion_btr_lfp_and_nmc_and_sourced_builder(self) -> None:
        lfp_text, lfp_meta = synthesize_btr_from_datasheet(manufacturer="CellPower", model="LFP-51V200Ah", chemistry="LFP",
            capacity_ah=200.0, nominal_voltage_v=51.2, n_cells_s=16, internal_resistance_mohm=0.8, coulombic_efficiency=0.985, cycle_life_80dod=6000)
        self.assertEqual(lfp_meta["batt_technol"], "Li_LFP")
        self.assertAlmostEqual(lfp_meta["energy_kwh"], 10.24, places=2)
        self.assertIn("BattTechnol=Li_LFP", lfp_text)
        self.assertIn("CapaCourant=TCubicProfile", lfp_text)
        rep = self.store._inspect("BTR", lfp_text.encode("utf-8"))
        self.assertEqual(rep["structural_errors"], [])
        self.assertEqual(rep["known_value_errors"], [])
        nmc_text, nmc_meta = synthesize_btr_from_datasheet(manufacturer="CellPower", model="NMC-100kWh", chemistry="NMC", energy_kwh=100.0, nominal_voltage_v=511.0)
        self.assertEqual(nmc_meta["batt_technol"], "Li_NMC")
        self.assertAlmostEqual(nmc_meta["energy_kwh"], 100.0, places=1)
        sources = {"DS_BTR": {"kind": "manufacturer", "title": "LFP Rack Datasheet", "locator": "CellPower_LFP200.pdf", "text": (
                    "CellPower LFP-51V200Ah: CapNomC10 200 Ah, VNom 51.2 V, " "NCellSerie 16 cells, ResInterne 0.8 mOhm, EfficI 98.5 %, "
                    "IMaxCharge 100 A, IMaxDischarge 200 A, SOHEOL 80 %.")}}
        def d(field, val, unit, excerpt):
            return {"field": field, "value": val, "unit": unit, "source_id": "DS_BTR", "excerpt": excerpt, "source_value_text": str(val), "source_unit_text": unit}
        decls = [d("CapNomC10", 200, "Ah", "CapNomC10 200 Ah"), d("VNom", 51.2, "V", "VNom 51.2 V"), d("NCellSerie", 16, "cells", "NCellSerie 16 cells"),
            d("ResInterne", 0.8, "mOhm", "ResInterne 0.8 mOhm"), d("EfficI", 98.5, "%", "EfficI 98.5 %"),
            d("IMaxCharge", 100, "A", "IMaxCharge 100 A"), d("IMaxDischarge", 200, "A", "IMaxDischarge 200 A"), d("SOHEOL", 80, "%", "SOHEOL 80 %")]
        prev = build_sourced_component(self.store, component_type="BTR", new_name="CellPower_LFP200.BTR",
            manufacturer="CellPower", model="LFP-51V200Ah", sources=sources, declarations=decls, chemistry="LFP", dry_run=True)
        self.assertEqual(prev["status"], "needs_review")
        self.assertTrue(prev["synthesized"])
        self.assertEqual(prev["synthesized_metadata"]["batt_technol"], "Li_LFP")
        created = build_sourced_component(self.store, component_type="BTR", new_name="CellPower_LFP200.BTR",
            manufacturer="CellPower", model="LFP-51V200Ah", sources=sources, declarations=decls, chemistry="LFP", dry_run=False, acknowledge_inherited_model=True)
        self.assertTrue(created["component_created"])
        self.assertTrue((self.root / "ComposPV" / "Batteries" / "CellPower_LFP200.BTR").is_file())
    def test_three_voltage_ond_synthesis_and_curve_editing(self) -> None:
        ond_text, meta = synthesize_ond_from_datasheet(manufacturer="GridPower", model="SG110CX-3V", pnom_ac_kw=110.0,
            pmax_ac_kw=110.0, vmpp_min_v=200.0, vmpp_max_v=1000.0, vabs_max_v=1100.0,
            effic_max_pct=98.7, effic_euro_pct=98.4, nb_mppt=9, three_voltage_voltages=(450.0, 620.0, 850.0),
            three_voltage_effic_max=(98.3, 98.7, 98.1), three_voltage_effic_euro=(98.0, 98.4, 97.8))
        self.assertTrue(meta["three_voltage"])
        self.assertEqual(meta["flags"], "$1560")
        self.assertIn("VNomEff=450.0,620.0,850.0,", ond_text)
        self.assertIn("ProfilPIOV1=TCubicProfile", ond_text)
        self.assertIn("ProfilPIOV2=TCubicProfile", ond_text)
        self.assertIn("ProfilPIOV3=TCubicProfile", ond_text)
        self.store.create("OND", "SG110_3V.OND", ond_text)
        info = self.store.inspect("OND", "SG110_3V.OND", include_curve_csv=True)
        self.assertTrue(info["curves"]["control"]["three_voltage"])
        self.assertEqual(info["curves"]["control"]["errors"], [])
        by_path = {item["path"]: item for item in info["curves"]["items"]}
        self.assertFalse(by_path["Converter/ProfilPIO"]["editable"])
        self.assertTrue(by_path["Converter/ProfilPIOV1"]["editable"])
        self.assertTrue(by_path["Converter/ProfilPIOV2"]["editable"])
        self.assertTrue(by_path["Converter/ProfilPIOV3"]["editable"])
        self.assertIn("Converter/ProfilPIOV1", info["curve_csv"])
        new_v1 = [p[:] for p in by_path["Converter/ProfilPIOV1"]["points"]]
        new_v1[-1][1] = new_v1[-1][1] - 500
        new_v3 = [p[:] for p in by_path["Converter/ProfilPIOV3"]["points"]]
        new_v3[-1][1] = new_v3[-1][1] - 600
        upd = self.store.update("OND", "SG110_3V.OND", {}, expected_sha256=info["sha256"], curve_updates={"Converter/ProfilPIOV1": new_v1, "Converter/ProfilPIOV3": new_v3})
        self.assertTrue(upd["changed"])
        info2 = self.store.inspect("OND", "SG110_3V.OND")
        by_path2 = {item["path"]: item for item in info2["curves"]["items"]}
        self.assertEqual(by_path2["Converter/ProfilPIOV1"]["points"][-1][1], new_v1[-1][1])
        self.assertEqual(by_path2["Converter/ProfilPIOV3"]["points"][-1][1], new_v3[-1][1])
    def test_pan_update_optional_single_diode_refit_backward_compatible(self) -> None:
        pan_text, _ = synthesize_pan_from_datasheet(manufacturer="ApexSolar", model="HiTOP-600", pnom_w=600.0, isc_a=14.30, voc_v=52.0, imp_a=13.55, vmp_v=44.3, width_m=1.134, height_m=2.382, n_cells_s=72, n_cells_p=2)
        self.store.create("PAN", "Apex600.PAN", pan_text)
        info0 = self.store.inspect("PAN", "Apex600.PAN")
        orig_rserie = info0["fields"]["RSerie"][0]
        no_refit = self.store.update("PAN", "Apex600.PAN", {"PNom": "625.0", "Voc": "53.2", "Vmp": "45.1", "ISC": "14.70", "Imp": "13.86"}, expected_sha256=info0["sha256"], dry_run=True)
        self.assertNotIn("single_diode_refit", no_refit)
        self.assertNotIn("RSerie", no_refit["diff"])
        with_refit = self.store.update("PAN", "Apex600.PAN", {"PNom": "625.0", "Voc": "53.2", "Vmp": "45.1", "ISC": "14.70", "Imp": "13.86"}, expected_sha256=info0["sha256"], dry_run=False, refit_single_diode=True)
        self.assertTrue(with_refit["changed"])
        self.assertTrue(with_refit["single_diode_refit"]["refitted"])
        self.assertIn("RSerie", with_refit["single_diode_refit"]["patched_fields"])
        info1 = self.store.inspect("PAN", "Apex600.PAN")
        self.assertNotEqual(info1["fields"]["RSerie"][0], orig_rserie)
        with self.assertRaisesRegex(ValueError, "PAN components only"): self.store.update("OND", "any.OND", {"PNomConv": "50"}, refit_single_diode=True)
        incomplete_pan = "\r\n".join(line for line in pan_text.splitlines() if not line.strip().startswith("Imp=")) + "\r\n"
        (self.root / "ComposPV" / "PVmodules" / "Incomplete.PAN").write_text(incomplete_pan, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "requires STC fields: Imp"):
            self.store.update("PAN", "Incomplete.PAN", {"Voc": "54.0"}, dry_run=True, refit_single_diode=True)
    def test_three_voltage_curve_and_btr_cell_voltage_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot exceed p_in"):
            synthesize_ond_from_datasheet(manufacturer="Lab", model="Inv3V", pnom_ac_kw=10.0,
                vmpp_min_v=200, vmpp_max_v=850, vabs_max_v=1000, three_voltage_voltages=[300, 600, 800],
                three_voltage_curves={"ProfilPIOV1": [[100, 0], [500, 600], [5000, 4800], [10500, 10000]]})
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            synthesize_ond_from_datasheet(manufacturer="Lab", model="Inv3V", pnom_ac_kw=10.0,
                vmpp_min_v=200, vmpp_max_v=850, vabs_max_v=1000, three_voltage_voltages=[300, 600, 800],
                three_voltage_curves={"ProfilPIOV1": [[500, 0], [500, 400], [5000, 4800], [10500, 10000]]})
        with self.assertRaisesRegex(ValueError, "4..11"):
            synthesize_ond_from_datasheet(manufacturer="Lab", model="Inv3V", pnom_ac_kw=10.0,
                vmpp_min_v=200, vmpp_max_v=850, vabs_max_v=1000, three_voltage_voltages=[300, 600, 800],
                three_voltage_curves={"ProfilPIOV1": [[100 * i, 90 * i] for i in range(1, 13)]})
        with self.assertRaisesRegex(ValueError, "Per-cell nominal voltage"):
            synthesize_btr_from_datasheet(manufacturer="Lab", model="BadBTR", chemistry="LFP", capacity_ah=100.0, n_cells_s=16, nominal_voltage_v=100.0)
        with self.assertRaisesRegex(ValueError, "cycle_life_80dod must be an integer"):
            synthesize_btr_from_datasheet(manufacturer="Lab", model="BadBTR", chemistry="LFP",
                capacity_ah=100.0, cycle_life_80dod=True,  # type: ignore[arg-type]
            )
    def test_sourced_component_field_unit_hints_and_schema_docs(self) -> None:
        sources = {"ds": {"kind": "manufacturer", "title": "Test Module Datasheet", "locator": "page 1", "text": "Isc temperature coefficient: 5.2 mA/°C, Nominal voltage 800 V, Power: 600 kW"}}
        with self.subTest(case="pan_unknown_field_did_you_mean"):
            pan_res = build_sourced_component(self.store, component_type="PAN", new_name="Demo600.PAN",
                manufacturer="TestCo", model="M600", sources=sources, declarations=[{"field": "muIsc", "value": 5.2, "unit": "mA/degC", "source_id": "ds",
                    "excerpt": "5.2 mA/°C", "source_value_text": "5.2", "source_unit_text": "mA/°C"}], dry_run=True)
            self.assertEqual(pan_res["status"], "blocked")
            issue = pan_res["issues"][0]
            self.assertIn("MuISC", issue["did_you_mean"])
            self.assertIn("MuISC", issue["allowed_fields"])
            self.assertIn("MuISC", pan_res["allowed_fields"])
        with self.subTest(case="ond_unknown_field_did_you_mean"):
            ond_res = build_sourced_component(self.store, component_type="OND", new_name="Demo300.OND",
                manufacturer="InvCo", model="I300", sources=sources, declarations=[{"field": "VNomEff", "value": 800.0, "unit": "V", "source_id": "ds",
                    "excerpt": "800 V", "source_value_text": "800", "source_unit_text": "V"}], dry_run=True)
            self.assertEqual(ond_res["status"], "blocked")
            self.assertIn("VNomEff1", ond_res["issues"][0]["did_you_mean"])
            self.assertIn("VOutConv", ond_res["allowed_fields"])
        with self.subTest(case="bad_unit_and_schema_docs"):
            bad_unit = build_sourced_component(self.store, component_type="PAN", new_name="DemoBadUnit.PAN",
                manufacturer="TestCo", model="M600", sources=sources, declarations=[{"field": "PNom", "value": 600.0, "unit": "kW", "source_id": "ds",
                    "excerpt": "600 kW", "source_value_text": "600", "source_unit_text": "kW"}], dry_run=True)
            self.assertEqual(bad_unit["status"], "blocked")
            self.assertEqual(bad_unit["issues"][0]["allowed_units"], ["W"])
            doc = pvsyst_build_sourced_component.__doc__ or ""
            for token in ("MuISC", "muVocSpec", "VOutConv", "VNomEff1..3", "CapNomC10"): self.assertIn(token, doc)
POINTS = [[100.0, 80.0], [200.0, 180.0], [500.0, 470.0], [1000.0, 950.0]]
PROFILE = '      NPtsMax=6\n      NPtsEff=4\n      Mode=1\n      LastCompile=$008D\n      Point_1=100,80\n      Point_2=200,180\n      Point_3=500,470\n      Point_4=1000,950\n      Point_5=0,0\n      Point_6=0,0\n'
CURVED_OND = OND.replace("      Point_1=500,470\n", PROFILE)
class ComponentEditorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ComponentStore(self.root)
        self.store.create("OND", "test.OND", CURVED_OND)
        self.path = self.root / "ComposPV" / "Inverters" / "test.OND"
    def sha(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()
    def curve_edit(self, **kwargs):
        args = {"updates": {}, "curve_updates": {"Converter/ProfilPIO": POINTS}, "expected_sha256": self.sha(), "use_file_curve": True}
        args.update(kwargs)
        return self.store.update("OND", "test.OND", **args)
    def assert_rejected_without_write(self, **kwargs):
        original = self.path.read_bytes()
        with self.assertRaises(ValueError): self.curve_edit(**kwargs)
        self.assertEqual(original, self.path.read_bytes())
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
    def test_atomic_scalar_curve_preview_apply_and_file_mode_edit(self):
        file_mode_orig = CURVED_OND.replace("Flags=$0570", "Flags=$0560").replace("100,80", "1e2,80.0")
        with self.subTest(case="file_mode_noop"):
            self.path.write_text(file_mode_orig, encoding="utf-8")
            noop = self.curve_edit(use_file_curve=False)
            self.assertFalse(noop["changed"])
            self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
        with self.subTest(case="atomic_scalar_and_curve_preview_apply"):
            text = CURVED_OND.replace("Flags=$0570", "Flags \t=  $a0000570  ").replace("    Manufacturer=Lab", "    Flags=$0570\n    Manufacturer=Lab").replace("Point_2=200,180", "Point_2 \t=  200,180  ")
            original = b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode()
            self.path.write_bytes(original)
            points = [p[:] for p in POINTS]
            points[1] = [200, 170]
            args = {"updates": {"Model": "Custom"}, "curve_updates": {"Converter/ProfilPIO": points}}
            preview = self.curve_edit(dry_run=True, **args)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
            self.assertEqual(preview["curve_control_before"]["source"], "automatic")
            self.assertEqual(preview["curve_control_after"]["source"], "file")
            edited = self.curve_edit(**args)
            expected = original.replace(b"$a0000570", b"$a0000560").replace(b"200,180", b"200,170").replace(b"Model=50kW", b"Model=Custom")
            self.assertEqual(self.path.read_bytes(), expected)
            self.assertIn(b"    Flags=$0570", expected)
            self.assertEqual((self.root / "ComposPV" / ".mcp-backups" / "OND" / edited["backup_name"]).read_bytes(), original)
        with self.subTest(case="file_mode_edit"):
            self.path.write_text(file_mode_orig, encoding="utf-8")
            fpoints = [p[:] for p in POINTS]
            fpoints[2][1] = 460
            fres = self.curve_edit(use_file_curve=False, curve_updates={"Converter/ProfilPIO": fpoints})
            self.assertTrue(fres["changed"])
            self.assertEqual(self.path.read_text(encoding="utf-8"), file_mode_orig.replace("500,470", "500,460"))
    def test_sibling_curves_and_unhashed_scalar_whitespace_preservation(self):
        with self.subTest(case="sibling_curves_and_inactive_rows"):
            block = "    ProfilPIO=TCubicProfile\n" + PROFILE + "    End of TCubicProfile\n"
            sib_text = CURVED_OND.replace(block, block + block.replace("ProfilPIO=", "ProfilPIOV1="))
            self.path.write_text(sib_text, encoding="utf-8")
            spoints = [p[:] for p in POINTS]
            spoints[0] = [110, 85]
            self.curve_edit(curve_updates={"Converter/ProfilPIO": spoints})
            sib_expected = sib_text.replace("Flags=$0570", "Flags=$0560").replace("Point_1=100,80", "Point_1=110,85", 1)
            self.assertEqual(self.path.read_text(encoding="utf-8"), sib_expected)
            self.assertFalse(self.store.inspect("OND", "test.OND")["curves"]["items"][1]["editable"])
        with self.subTest(case="scalar_whitespace_without_hash"):
            self.path.write_text(CURVED_OND.replace("Model=50kW", "Model \t=  50kW  "), encoding="utf-8")
            scalar_res = self.store.update("OND", "test.OND", {"Model": "Changed"})
            self.assertFalse(scalar_res["conflict_checked"])
            self.assertIn(b"Model \t=  Changed  ", self.path.read_bytes())
            self.assertIn(b"Point_6=0,0", self.path.read_bytes())
    def test_hash_race_and_malformed_scalar_guards(self):
        original = self.path.read_bytes()
        for updates in ({"PNomConv": "nan"}, {"PNomConv": "0"}, {"Model": "line\nbreak"}, {"Point_1": "100,50"}, {}):
            with self.subTest(case="malformed_scalar", updates=updates):
                self.path.write_bytes(original)
                with self.assertRaises(ValueError): self.store.update("OND", "test.OND", updates, self.sha(), True)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
        with self.subTest(case="curve_hash_and_auto_switch"):
            self.path.write_bytes(original)
            no_hash = self.curve_edit(expected_sha256=None, dry_run=True)
            self.assertFalse(no_hash["conflict_checked"])
            self.assert_rejected_without_write(expected_sha256="0" * 64)
            self.assert_rejected_without_write(use_file_curve=False)
            self.assert_rejected_without_write(use_file_curve="true")
            self.assert_rejected_without_write(curve_updates=None, updates={"Model": "Changed"})
        with self.subTest(case="non_ond_rejected"):
            self.store.create("GEN", "test.GEN", GEN)
            info = self.store.inspect("GEN", "test.GEN")
            with self.assertRaisesRegex(ValueError, "only OND"):
                self.store.update("GEN", "test.GEN", {}, info["sha256"], curve_updates={"Converter/ProfilPIO": POINTS}, use_file_curve=True)
        with self.subTest(case="stale_hash_rejected"):
            self.path.write_bytes(original)
            before = self.sha()
            self.path.write_bytes(original.replace(b"Model=50kW", b"Model=External"))
            for dry_run in (True, False):
                with self.assertRaisesRegex(ValueError, "changed since inspection"):
                    self.store.update("OND", "test.OND", {"Model": "Overwrite"}, before, dry_run)
            self.assertIn(b"Model=External", self.path.read_bytes())
            self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
        with self.subTest(case="external_change_during_backup"):
            self.path.write_bytes(original)
            backup = self.store.backup
            def external_edit(*args):
                result = backup(*args)
                self.path.write_bytes(self.path.read_bytes().replace(b"Model=50kW", b"Model=External"))
                return result
            with patch.object(self.store, "backup", side_effect=external_edit):
                with self.assertRaisesRegex(ValueError, "changed while preparing"):
                    self.store.update("OND", "test.OND", {"Model": "Overwrite"}, self.sha())
            self.assertIn(b"Model=External", self.path.read_bytes())
            self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
    def test_unsupported_flags_malformed_profiles_and_invalid_points_rejected(self):
        cases = [CURVED_OND.replace("Flags=$0570", value) for value in ("Flags=$1570", "Flags=$1570\n  Flags=$0570", "Other=$0570", "Flags=1392", "Flags=$100000000")]
        cases += [CURVED_OND.replace("Version=8.1.6", "Version=8.2.0"), CURVED_OND.replace("Mode=1", "Mode=2")]
        for content in cases:
            with self.subTest(case="unsupported_control", content=content):
                self.path.write_text(content, encoding="utf-8")
                curve = self.store.inspect("OND", "test.OND")["curves"]["items"][0]
                self.assertFalse(curve["editable"])
                self.assertTrue(curve["edit_errors"])
                self.assert_rejected_without_write()
        with self.subTest(case="nested_flags_ignored"):
            nested = CURVED_OND.replace("  Flags=$0570\n", "").replace("    Manufacturer=Lab", "    Flags=$0570\n    Manufacturer=Lab")
            self.path.write_text(nested, encoding="utf-8")
            self.assert_rejected_without_write()
        block = "    ProfilPIO=TCubicProfile\n" + PROFILE + "    End of TCubicProfile\n"
        malformed = [CURVED_OND.replace(block, block + block), CURVED_OND.replace("NPtsEff=4", "NPtsEff=3"),
                     CURVED_OND.replace("NPtsEff=4", "NPtsEff=7"), CURVED_OND.replace("Point_6=0,0", "Point_5=0,0"),
                     CURVED_OND.replace("Point_6=0,0", "Point_6=nan,0"), CURVED_OND.replace("Mode=1", "Mode=1\n      Mode=1")]
        for idx, content in enumerate(malformed):
            with self.subTest(case="malformed_profile", idx=idx):
                self.path.write_text(content, encoding="utf-8")
                self.assert_rejected_without_write()
        self.path.write_text(CURVED_OND, encoding="utf-8")
        point_cases = [POINTS[:3], [[i + 1, i] for i in range(257)], "points", None]
        for pair in ([True, 80], ["100", 80], [100, float("nan")], [float("inf"), 80], [100, -1], [100, 101], [-100, 0], [200, 180], [100], [10 ** 400, 1]):
            point_cases.append([pair] + POINTS[1:])
        for points in point_cases:
            with self.subTest(case="invalid_points", points=points):
                self.assert_rejected_without_write(updates={"Model": "Changed"}, curve_updates={"Converter/ProfilPIO": points})
        for curves in ({}, {"Point_1": POINTS}, {"Converter/ProfilPIOV1": POINTS}, {"Converter/ProfilPIO": POINTS, "Converter/ProfilPIOV2": POINTS}):
            with self.subTest(case="invalid_curve_dict", keys=list(curves)): self.assert_rejected_without_write(curve_updates=curves)
def table(kind, path, points):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(HEADERS[kind, path])
    writer.writerows(points)
    return stream.getvalue()
class CurveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = ComponentStore(self.root)
        for kind, content in (("OND", CURVED_OND), ("BTR", CURVED_BTR)): self.store.create(kind, "curve." + kind, "\ufeff" + content.replace("\n", "\r\n"))
    def info(self, kind, **kwargs):
        return self.store.inspect(kind, "curve." + kind, **kwargs)
    def update(self, kind, **kwargs):
        args = {"updates": {}, "expected_sha256": self.info(kind)["sha256"], "use_file_curve": True}
        args.update(kwargs)
        return self.store.update(kind, "curve." + kind, **args)
    def test_add_resize_shrink_noop_and_restore(self):
        for kind, path, base, more in (("OND", "Converter/ProfilPIO", POINTS, [[1500, 1425], [2000, 1920], [2500, 2400]]), ("BTR", "Capa_DischRate", CAPACITY_POINTS, [[250, 1.42], [300, 1.44], [400, 1.46]])):
            file_path = self.store._path(kind, "curve." + kind)
            original = file_path.read_bytes()
            grown = base + more
            self.update(kind, curve_updates={path: grown}, dry_run=True)
            self.assertEqual(file_path.read_bytes(), original)
            changed = self.update(kind, curve_updates={path: grown})
            canonical = "CapaCourant" if kind == "BTR" else path
            curve = next(i for i in self.info(kind)["curves"]["items"] if i["path"] == canonical)
            self.assertEqual((curve["points"], curve["allocated_point_count"]), (grown, len(grown)))
            self.assertFalse(self.update(kind, curve_updates={canonical: grown})["changed"])
            fewer = [grown[0], grown[1], grown[3], grown[-1]]
            self.update(kind, curve_updates={canonical: fewer})
            curve = next(i for i in self.info(kind)["curves"]["items"] if i["path"] == canonical)
            self.assertEqual((curve["point_count"], curve["points"]), (4, fewer))
            self.assertEqual((self.root / "ComposPV" / ".mcp-backups" / kind / changed["backup_name"]).read_bytes(), original)
    def test_csv_roundtrip_growth_and_numeric_dialect(self):
        for kind, path, points, added in (("OND", "Converter/ProfilPIO", POINTS, [1500, 1425]), ("BTR", "Capa_DischRate", CAPACITY_POINTS, [300, 1.44])):
            self.assertNotIn("curve_csv", self.info(kind))
            self.update(kind, curve_updates={path: points})
            exported = self.info(kind, include_curve_csv=True)
            canonical = "CapaCourant" if kind == "BTR" else path
            self.assertEqual(import_curve_csv(kind, exported["curve_csv"]), {canonical: points})
            self.assertFalse(self.update(kind, curve_csv=exported["curve_csv"])["changed"])
            grown = points + [added]
            serialized = {canonical: "\ufeff" + table(kind, canonical, grown)}
            self.assertEqual(self.update(kind, curve_csv=serialized, dry_run=True)["diff"], self.update(kind, curve_updates={canonical: grown}, dry_run=True)["diff"])
            self.update(kind, curve_csv=serialized)
            curve = next(c for c in self.info(kind)["curves"]["items"] if c["path"] == canonical)
            self.assertEqual(curve["points"], grown)
        text = '\ufeffinput_watts,output_watts\r\n" 1e2 ",+8e1\r\n200,180\r\n500,470.0\r\n1E3,950\r\n'
        self.assertEqual(import_curve_csv("OND", {"Converter/ProfilPIO": text}), {"Converter/ProfilPIO": POINTS})
    def test_bad_csv_and_export_guards(self):
        good = table("OND", "Converter/ProfilPIO", [[int(x), int(y)] for x, y in POINTS])
        bad_csvs = [None, 123, "", good.split("\r\n", 1)[1], good.replace("input_watts", "input_kw"), good.replace(",", ";"), good.replace("100,80", "100,"), good + "\r\n",
                    good.replace("100,80", '"1\n00",80'), good.replace("100,80", "NaN,80"), good.replace("100,80", "1_00,80"), good.replace("100,80", "100,101"),
                    good.replace("100,80", "500,80"), table("OND", "Converter/ProfilPIO", POINTS[:3]),
                    "x" * (MAX_CSV_BYTES + 1), table("OND", "Converter/ProfilPIO", [[x + 1, x] for x in range(257)])]
        before = self.info("OND")["sha256"]
        for content in bad_csvs:
            with self.subTest(content=repr(content)[:40]), self.assertRaises(ValueError):
                self.update("OND", updates={"PNomConv": "45"}, curve_csv={"Converter/ProfilPIO": content})
            self.assertEqual(self.info("OND")["sha256"], before)
        for data in ({}, [], {"unknown": "x"}, {"CapaCourant": "x"}, {"Converter/ProfilPIO": "x", "Converter/ProfilPIOV1": "y"}):
            with self.assertRaises(ValueError): self.update("OND", curve_csv=data)
        self.assertIn("SelfDisch_Temp", self.info("BTR", include_curve_csv=True)["curve_csv_errors"])
        with self.assertRaises(ValueError): self.info("OND", include_curve_csv="true")
        with self.assertRaises(ValueError): self.store.inspect("PAN", "curve.PAN", include_curve_csv=True)
    def test_resize_preserves_siblings_metadata_compile_options_and_guards(self):
        text = CURVED_OND.replace("  Flags=$0570\n", "").replace("End of PVObject pvGInverter", "  Flags=$0570\nEnd of PVObject pvGInverter")
        b_start = text.index("    ProfilPIO=TCubicProfile")
        b_end = text.index("    End of TCubicProfile", b_start) + len("    End of TCubicProfile\n")
        sibling = text[b_start:b_end].replace("ProfilPIO=", "ProfilPIOV1=")
        text = (text[:b_end] + sibling + text[b_end:]).replace("      Mode=1", "      UnknownMetadata=preserve me\n      Mode=1", 1)
        ond_path = self.store._path("OND", "curve.OND")
        write_utf8_crlf(ond_path, text.replace("Point_1=100,80", "Point_1=100,80,0,0,0.9", 1))
        self.update("OND", curve_updates={"Converter/ProfilPIO": POINTS + [[1500, 1425]]})
        updated = ond_path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
        self.assertIn(sibling, updated)
        self.assertIn("UnknownMetadata=preserve me", updated)
        self.assertNotIn("80,0,0,0.9", updated)
        self.assertTrue(updated.endswith("  Flags=$0560\nEnd of PVObject pvGInverter\n"))
        for val in (None, "$0", "$008d"):
            txt = CURVED_OND.replace("      LastCompile=$008D\n", "" if val is None else f"      LastCompile={val}\n")
            write_utf8_crlf(ond_path, txt)
            self.update("OND", curve_updates={"Converter/ProfilPIO": POINTS + [[1500, 1425]]})
            raw = ond_path.read_bytes()
            self.assertEqual(b"LastCompile" in raw, val is not None)
        for bad in (CURVED_OND.replace("$008D", "$204D"), CURVED_OND.replace("Point_2=200,180", "Unknown=retain\n      Point_2=200,180"),
                    CURVED_OND.replace("Point_1=100,80\n      Point_2=200,180", "Point_2=200,180\n      Point_1=100,80")):
            write_utf8_crlf(ond_path, bad)
            with self.assertRaises(ValueError): self.update("OND", curve_updates={"Converter/ProfilPIO": POINTS + [[1500, 1425]]})
        write_utf8_crlf(ond_path, CURVED_OND)
        for bad_pts in ([[0, 0]] + POINTS, [POINTS[0], [100 + 1e-9, 90]] + POINTS[1:], [[i + 1, i] for i in range(257)]):
            with self.assertRaises(ValueError): self.update("OND", curve_updates={"Converter/ProfilPIO": bad_pts})
        largest = [[i + 1, i * 0.9] for i in range(256)]
        self.update("OND", curve_updates={"Converter/ProfilPIO": largest})
        self.assertEqual(self.info("OND")["curves"]["items"][0]["allocated_point_count"], 256)
class GeneratorTests(unittest.TestCase):
    def setUp(self):
        from tests.unit.core.test_variants import VARIANT
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "Projects").mkdir()
        (self.root / "Projects" / "Example.PRJ").write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        content = VARIANT.replace("    Flags=$60", "    Flags \t=  $ab0040  ")
        content = content.replace("    GensetFile=original.GEN\n", "")
        self.path = self.root / "Projects" / "Example.VC0"
        self.path.write_bytes(b"\xef\xbb\xbf" + content.replace("\n", "\r\n").encode())
        self.components = ComponentStore(self.root)
        self.components.create("GEN", "test.GEN", GEN)
        self.store = VariantStore(self.root, self.components)
        self.config = {"enabled": True, "filename": "test.GEN", "operating_power_kw": 40, "thresholds": {"1": {"VBkUpEncl_syst": 0.9, "VBkUpDecl_syst": 0.95}, "2": {"VBkUpEncl_syst": 0.25, "VBkUpDecl_syst": 0.45}}}
    def sha(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()
    def edit(self, generator=None, **kwargs):
        return self.store.update_parameters("Example.PRJ", "VC0", self.sha(), 1, 1, generator_updates=generator if generator is not None else self.config, **kwargs)
    def test_enable_preview_apply_disable_and_disabled_noop(self):
        original = self.path.read_bytes()
        info = self.store.inspect_parameters("Example.PRJ", "VC0")["generator"]
        self.assertTrue(info["supported"])
        self.assertFalse(info["enabled"])
        self.assertIsNone(info["filename"])
        self.assertIsNone(info["subarrays"][0]["VBkUpDecl_syst"])
        self.edit(dry_run=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "Projects" / ".mcp-variant-backups").exists())
        changed = self.edit()
        self.assertTrue(changed["generator_after"]["enabled"])
        self.assertEqual(changed["generator_component"]["nominal_power_kw"], 50)
        self.assertIn(b"Flags \t=  $ab0060  ", self.path.read_bytes())
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", self.path.read_bytes().replace(b"\r\n", b""))
        self.assertEqual((self.root / "Projects" / ".mcp-variant-backups" / changed["backup_name"]).read_bytes(), original)
        active = self.path.read_bytes()
        self.edit({"enabled": False})
        self.assertEqual(self.path.read_bytes(), active.replace(b"$ab0060", b"$ab0040"))
        self.path.write_bytes(original)
        self.edit(self.config | {"enabled": False})
        disabled_bytes = self.path.read_bytes()
        previous_backups = list((self.root / "Projects" / ".mcp-variant-backups").iterdir())
        noop = self.edit(self.config | {"enabled": False})
        self.assertFalse(noop["changed"])
        self.assertNotIn("backup_name", noop)
        self.assertEqual(self.path.read_bytes(), disabled_bytes)
        self.assertEqual(list((self.root / "Projects" / ".mcp-variant-backups").iterdir()), previous_backups)
    def test_version_810_nested_decoys_and_nominal_power_warning(self):
        original = self.path.read_bytes()
        with self.subTest(case="power_above_nominal_warning"):
            self.path.write_bytes(original)
            warned = self.edit(self.config | {"operating_power_kw": 60}, dry_run=True)
            self.assertIn("exceeds GEN nominal", warned["warnings"][0])
        with self.subTest(case="version_810_preserved"):
            self.path.write_bytes(original.replace(b"Version=8.1.6", b"Version=8.1.0", 1))
            v810 = self.edit()
            self.assertTrue(v810["generator_after"]["supported"])
            self.assertIn(b"Version=8.1.0", self.path.read_bytes())
        with self.subTest(case="nested_decoy_fields_preserved"):
            self.path.write_bytes(original)
            text = self.path.read_text(encoding="utf-8-sig")
            text = text.replace("    BatteryFile=", "    PVObject_Meta=pvMeta\n      Flags=$20\n" "      PEffBackUp=999\n    End of PVObject pvMeta\n    BatteryFile=")
            text = text.replace("        NModSerie=14", "        PVObject_IAM=pvIAM\n" "          VBkUpEncl_syst=0.77\n          Curve=TCubicProfile\n"
                "            Point_1=0,1\n          End of TCubicProfile\n" "        End of PVObject pvIAM\n        WiringResistance=TWiringResistance\n"
                "          TypeLayout=1\n        EndTags\n        NModSerie=14")
            self.path.write_text(text, encoding="utf-8")
            decoy = self.edit()
            result = self.path.read_text(encoding="utf-8")
            self.assertIn("      Flags=$20\n      PEffBackUp=999", result)
            self.assertIn("          VBkUpEncl_syst=0.77", result)
            self.assertEqual(decoy["generator_after"]["operating_power_kw"], 40)
    def test_invalid_settings_thresholds_and_scope_guards(self):
        invalid = [{"enabled": "true"}, {"operating_power_kw": True}, {"operating_power_kw": 0},
            {"operating_power_kw": float("nan")}, {"operating_power_kw": -1}, {"filename": "../test.GEN"}, {"filename": "absent.GEN"}, {"unknown": 1},
            {"thresholds": {"3": {"VBkUpEncl_syst": 0.9}}}, {"thresholds": {"01": {"VBkUpEncl_syst": 0.9}}},
            {"thresholds": {"1": {"VBkUpEncl_syst": 1.1}}}, {"thresholds": {"1": {"VBkUpEncl_syst": "0.9"}}}, {"thresholds": {"1": {"Other": 0.9}}}, {"thresholds": []}]
        original = self.path.read_bytes()
        for change in invalid:
            with self.subTest(change=change), self.assertRaises((ValueError, FileNotFoundError)): self.edit(self.config | change)
        for change in ({"enabled": True}, {"enabled": True, "filename": "test.GEN"}):
            with self.subTest(missing_fields=change), self.assertRaises(ValueError): self.edit(change)
        with self.subTest(case="incomplete_thresholds"):
            incomplete = self.config | {"thresholds": {"1": self.config["thresholds"]["1"]}}
            with self.assertRaisesRegex(ValueError, "subarray 2"): self.edit(incomplete)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "Projects" / ".mcp-variant-backups").exists())
        cases = [original.replace(b"Version=8.1.6", b"Version=8.0.6", 1), original.replace(b"SystemType=Battery", b"SystemType=Grid"),
            original.replace(b"SystemType=Battery", b"SystemType=Battery\r\n    Flags=$40"),
            original.replace(b"VBkUpEncl_syst=0.250", b"VBkUpEncl_syst=0.250\r\n        VBkUpEncl_syst=0.8")]
        for idx, case in enumerate(cases):
            with self.subTest(case="scope_guard", idx=idx):
                self.path.write_bytes(case)
                with self.assertRaises(ValueError): self.edit()
                self.assertEqual(self.path.read_bytes(), case)
    def test_backup_and_dependency_race_guards(self):
        original = self.path.read_bytes()
        backup = self.store._backup
        with self.subTest(case="external_variant_race"):
            self.path.write_bytes(original)
            def ext_race(*args):
                result = backup(*args)
                self.path.write_bytes(original.replace(b"NModSerie=14", b"NModSerie=16"))
                return result
            with patch.object(self.store, "_backup", side_effect=ext_race), self.assertRaisesRegex(ValueError, "changed while preparing"): self.edit()
            self.assertIn(b"NModSerie=16", self.path.read_bytes())
            self.assertFalse(list(self.path.parent.glob("*.tmp")))
        with self.subTest(case="generator_dependency_race"):
            self.path.write_bytes(original)
            def gen_race(*args):
                result = backup(*args)
                self.components._path("GEN", "test.GEN").write_text(GEN.replace("PNomGen=50", "PNomGen=60"), encoding="utf-8")
                return result
            with patch.object(self.store, "_backup", side_effect=gen_race), self.assertRaisesRegex(ValueError, "Generator component changed"): self.edit()
            self.assertEqual(self.path.read_bytes(), original)
        with self.subTest(case="builtin_shadow_race"):
            self.path.write_bytes(original)
            self.components._path("GEN", "test.GEN").write_text(GEN, encoding="utf-8")
            builtin = self.root / "DataRO"
            (builtin / "Gensets").mkdir(parents=True, exist_ok=True)
            (builtin / "Gensets" / "builtin.GEN").write_text(GEN, encoding="utf-8")
            self.components.builtin = builtin
            config = self.config | {"filename": "builtin.GEN"}
            preview = self.edit(config, dry_run=True)
            self.assertEqual(preview["generator_component"]["library"], "builtin")
            def shadow_race(*args):
                result = backup(*args)
                self.components._path("GEN", "builtin.GEN").write_text(GEN, encoding="utf-8")
                return result
            with patch.object(self.store, "_backup", side_effect=shadow_race), self.assertRaisesRegex(ValueError, "shadows builtin"): self.edit(config)
            self.assertEqual(self.path.read_bytes(), original)
GUI_PAN = (PAN.replace("    Model=440W", "    Model=440W\n    Width=1.032\n    Height=1.996")
           .replace("  LargApp=1.032\n", "").replace("  LongApp=1.996\n", "") .replace("  ISC=", "  Isc=").replace("  MuISC=", "  muISC="))
class PanGUIFormatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ComponentStore(self.root)
        self.store.create("PAN", "gui.PAN", "\ufeff" + GUI_PAN.replace("\n", "\r\n"))
        self.path = self.store._path("PAN", "gui.PAN")
    def test_gui_spelling_inspect_edit_and_noop(self):
        with self.subTest(case="inspect_gui_spelling"):
            info = self.store.inspect("PAN", "gui.PAN")
            self.assertFalse(info["warnings"] or info["structural_errors"] or info["known_value_errors"])
            self.assertEqual(info["fields"]["Isc"], ["11.05"])
            self.assertNotIn("ISC", info["fields"])
            self.assertTrue({"ISC", "Isc", "MuISC", "muISC"} <= set(info["editable_fields"]))
            self.assertEqual(set(info["scalar_aliases"]["ISC"]), {"ISC", "Isc"})
            for key in ("Width", "Height"):
                self.assertTrue(info["commercial"]["fields"][key]["editable"])
                self.assertFalse(info["commercial"]["fields"][key]["paired_root_present"])
        with self.subTest(case="mixed_edit_preview_apply_backup"):
            original = self.path.read_bytes()
            args = {"updates": {"ISC": "11.2", "MuISC": "0.006"}, "commercial_updates": {"Width": "1.05", "Height": "2.0"}, "expected_sha256": hashlib.sha256(original).hexdigest()}
            self.store.update("PAN", "gui.PAN", dry_run=True, **args)
            self.assertEqual(self.path.read_bytes(), original)
            edited = self.store.update("PAN", "gui.PAN", **args)
            data = self.path.read_bytes()
            self.assertIn(b"Isc=11.2", data)
            self.assertIn(b"muISC=0.006", data)
            self.assertIn(b"Width=1.05", data)
            self.assertNotIn(b"LargApp", data)
            self.assertNotIn(b"LongApp", data)
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
            self.assertEqual((self.root / "ComposPV" / ".mcp-backups" / "PAN" / edited["backup_name"]).read_bytes(), original)
        for label, source in (("pan", PAN), ("gui_pan", GUI_PAN)):
            for updates in ({"ISC": "11.05", "MuISC": "0.0055"}, {"Isc": "11.05", "muISC": "0.0055"}):
                with self.subTest(case="both_spellings_noop", source=label, updates=updates):
                    self.path.write_text(source, encoding="utf-8")
                    before = self.path.read_bytes()
                    result = self.store.update("PAN", "gui.PAN", updates)
                    self.assertFalse(result["changed"])
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertNotIn("backup_name", result)
    def test_duplicate_aliases_and_bad_gui_values_rejected(self):
        for updates in ({"ISC": "11.2", "Isc": "11.3"}, {"MuISC": ".005", "muISC": ".006"}):
            with self.subTest(case="conflicting_request_aliases", updates=updates):
                before = self.path.read_bytes()
                with self.assertRaises(ValueError): self.store.update("PAN", "gui.PAN", updates)
                self.assertEqual(self.path.read_bytes(), before)
        for key, source in (("Isc", GUI_PAN.replace("  Isc=11.05", "  Isc=11.05\n  ISC=11.05")), ("muISC", GUI_PAN.replace("  muISC=0.0055", "  muISC=0.0055\n  MuISC=0.0055"))):
            with self.subTest(case="duplicate_file_aliases", key=key):
                self.path.write_text(source, encoding="utf-8")
                self.assertTrue(self.store.inspect("PAN", "gui.PAN")["known_value_errors"])
                before = self.path.read_bytes()
                with self.assertRaises(ValueError): self.store.update("PAN", "gui.PAN", {"PNom": "441"})
                self.assertEqual(self.path.read_bytes(), before)
        sources = [GUI_PAN.replace("Width=1.032", f"Width={value}") for value in ("0", "-1", "nan", "x")]
        sources += [GUI_PAN.replace("Isc=11.05", f"Isc={value}") for value in ("0", "-1", "NaN", "10")]
        sources += [GUI_PAN.replace("muISC=0.0055", "muISC=inf"), GUI_PAN.replace("    Width=1.032\n", ""), GUI_PAN.replace("Width=1.032", "Width=1.032\n    Width=1.032")]
        for source in sources:
            with self.subTest(case="bad_gui_values", source=source):
                self.path.write_text(source, encoding="utf-8")
                info = self.store.inspect("PAN", "gui.PAN")
                self.assertTrue(info["known_value_errors"] or info["warnings"])
                before = self.path.read_bytes()
                with self.assertRaises(ValueError): self.store.update("PAN", "gui.PAN", {"Model": "Bad source"})
                self.assertEqual(self.path.read_bytes(), before)
    def test_object_scoping_and_metadata_isolation(self):
        for label, source in (("isc_in_commercial", GUI_PAN.replace("  Isc=11.05\n", "").replace("    Model=440W", "    Model=440W\n    Isc=11.05")),
            ("width_in_root", GUI_PAN.replace("    Width=1.032\n", "").replace("  PNom=440", "  PNom=440\n  Width=1.032"))):
            with self.subTest(case=label):
                self.path.write_text(source, encoding="utf-8")
                self.assertTrue(self.store.inspect("PAN", "gui.PAN")["warnings"])
                before = self.path.read_bytes()
                with self.assertRaises(ValueError): self.store.update("PAN", "gui.PAN", {"Isc": "11.2"})
                self.assertEqual(self.path.read_bytes(), before)
        with self.subTest(case="commercial_decoy_isc_preserved"):
            source = GUI_PAN.replace("    Model=440W", "    Model=440W\n    Isc=99")
            self.path.write_text(source, encoding="utf-8")
            self.store.update("PAN", "gui.PAN", {"ISC": "11.2"})
            self.assertIn("    Isc=99", self.path.read_text(encoding="utf-8"))
            self.assertIn("  Isc=11.2", self.path.read_text(encoding="utf-8"))
            electrical = self.store.inspect("PAN", "gui.PAN")["electrical"]
            self.assertEqual(electrical["fields"]["ISC"]["value"], 11.2)
            self.assertEqual(electrical["fields"]["ISC"]["source_key"], "Isc")
            self.assertEqual(electrical["fields"]["PNom"]["unit"], "W")
            self.assertEqual(electrical["fields"]["VMaxIEC"]["status"], "missing")
            self.assertTrue(electrical["unverified"])
        with self.subTest(case="root_manufacturer_decoy"):
            source = GUI_PAN.replace("  Version=", "  Manufacturer=Root metadata\n  Version=")
            self.path.write_text(source, encoding="utf-8")
            self.assertEqual(self.store.inspect("PAN", "gui.PAN")["manufacturer"], "Lab")
        with self.subTest(case="gen_isc_metadata_isolation"): self.store.create("GEN", "meta.GEN", GEN.replace("  TypeGen=", "  Isc=metadata\n  TypeGen="))
if __name__ == "__main__": unittest.main()

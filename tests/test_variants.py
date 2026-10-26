"""Offline variant cloning, scoped reference replacement and rollback tests."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.variants import VariantStore
from test_components import PAN, OND, BTR, GEN


VARIANT = """PVObject_=pvVCalcul
  Version=8.1.6
  PVObject_=pvOrient
    NoOrient=1
    FieldType=FixedPlane
    FieldTilt=20.0
    FieldAzim=0.0
  End of TOrientGroup
  PVObject_PVMainArray=pvMainArray
    SubArrays=Start
      PVObject_=pvSubArray
        SubArrayId=1
        PVModule=original.PAN
        GInverter=original.OND
        NModSerie=14
        VBkUpEncl_syst=0.250
      End of PVObject pvSubArray
      PVObject_=pvSubArray
        SubArrayId=2
        PVModule=original.PAN
        GInverter=original.OND
      End of PVObject pvSubArray
    EndTags
  End of PVObject pvMainArray
  PVObject_System=pvSystem
    Flags=$60
    SystemType=Battery
    BatteryFile=original.BTR
    GensetFile=original.GEN
  End of PVObject pvSystem
End of PVObject pvVCalcul
"""


class VariantTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name) / "work"
        (self.workspace / "Projects").mkdir(parents=True)
        (self.workspace / "Projects" / "Example.PRJ").write_text(
            "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        self.original = self.workspace / "Projects" / "Example.VC0"
        self.original.write_bytes(b"\xef\xbb\xbf" + VARIANT.replace("\n", "\r\n").encode("utf-8"))
        for kind, folder, text in (("PAN", "PVmodules", PAN), ("OND", "Inverters", OND),
                                   ("BTR", "Batteries", BTR), ("GEN", "Gensets", GEN)):
            directory = self.workspace / "ComposPV" / folder
            directory.mkdir(parents=True)
            for name in ("original", "new"):
                (directory / f"{name}.{kind}").write_text(text, encoding="utf-8")
        self.components = ComponentStore(self.workspace)
        self.store = VariantStore(self.workspace, self.components)

    def test_inspect_clone_scoped_references_and_preserve_original_bytes(self):
        before = self.original.read_bytes()
        original = self.store.inspect("Example.PRJ", "VC0")
        self.assertEqual([item["id"] for item in original["subarrays"]], [1, 2])
        clone = self.store.clone("Example.PRJ", "VC0", "VC1", {"PAN": "new.PAN"}, 2)
        path = self.workspace / "Projects" / "Example.VC1"
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        self.assertEqual(raw.count(b"PVModule=new.PAN"), 1)
        self.assertEqual(raw.count(b"PVModule=original.PAN"), 1)
        self.assertEqual(self.original.read_bytes(), before)
        refs = self.store.inspect("Example.PRJ", "VC1")
        self.assertEqual(refs["sha256"], clone["sha256"])
        self.assertEqual(refs["subarrays"][0]["references"]["PAN"], ["original.PAN"])
        self.assertEqual(refs["subarrays"][1]["references"]["PAN"], ["new.PAN"])
        with self.assertRaises(FileExistsError):
            self.store.clone("Example.PRJ", "VC0", "VC1")
        project = self.workspace / "Projects" / "Other.PRJ"
        project.write_bytes((self.workspace / "Projects" / "Example.PRJ").read_bytes())
        imported = self.store.clone("Other.PRJ", "VC0", "VC7", source_project="Example.PRJ")
        self.assertEqual(imported["source_project"], "Example.PRJ")
        self.assertEqual((project.parent / "Other.VC7").read_bytes(), before)
        self.assertEqual(self.original.read_bytes(), before)
        self.assertEqual(project.read_bytes(), (project.parent / "Example.PRJ").read_bytes())
        with self.assertRaises(FileExistsError):
            self.store.clone("Other.PRJ", "VC0", "VC7", source_project="Example.PRJ")
        with self.assertRaises(FileNotFoundError):
            self.store.clone("Other.PRJ", "VC0", "VC8", source_project="Missing.PRJ")
        with self.assertRaises(ValueError):
            self.store.clone("Other.PRJ", "VC0", "VC8", source_project="../Example.PRJ")
        self.assertFalse((project.parent / "Other.VC8").exists())

    def test_update_and_restore_with_hash_and_backup(self):
        initial = self.store.inspect("Example.PRJ", "VC0")
        changed = self.store.update("Example.PRJ", "VC0", {"GEN": "new.GEN"},
                                    initial["sha256"])
        self.assertEqual(self.store.inspect("Example.PRJ", "VC0")["system_references"]["GEN"],
                         ["new.GEN"])
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.update("Example.PRJ", "VC0", {"GEN": "original.GEN"},
                              initial["sha256"])
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            self.store.restore("Example.PRJ", "VC0", changed["backup_name"],
                               changed["sha256"])
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.restore("Example.PRJ", "VC0", changed["backup_name"],
                               initial["sha256"], True)
        restored = self.store.restore("Example.PRJ", "VC0", changed["backup_name"],
                                      changed["sha256"], True)
        self.assertEqual(restored["sha256"], initial["sha256"])
        self.assertNotEqual(restored["backup_name"], changed["backup_name"])
        self.assertEqual(self.original.read_bytes(),
                         (self.workspace / "Projects" / ".mcp-variant-backups" /
                          changed["backup_name"]).read_bytes())

    def test_missing_and_ambiguous_reference_rejected_without_write(self):
        before = self.original.read_bytes()
        with self.assertRaisesRegex(FileNotFoundError, "loose file"):
            self.store.clone("Example.PRJ", "VC0", "VC2", {"PAN": "missing.PAN"})
        with self.assertRaisesRegex(ValueError, "No section"):
            self.store.clone("Example.PRJ", "VC0", "VC2", {"PAN": "new.PAN"}, 99)
        with self.assertRaisesRegex(ValueError, "Choose one or more"):
            self.store.clone("Example.PRJ", "VC0", "VC2", {"Controller": "new.RLT"})
        for value in ("../new.PAN", "new.OND"):
            with self.assertRaises(ValueError):
                self.store.clone("Example.PRJ", "VC0", "VC2", {"PAN": value})
        with self.assertRaises(ValueError):
            self.store.inspect("../Example.PRJ", "VC0")
        with self.assertRaises(ValueError):
            self.store.clone("Example.PRJ", "VC0", "../VC2")
        duplicate = self.original.read_bytes().replace(b"PVModule=original.PAN\r\n",
                                                        b"PVModule=original.PAN\r\n        PVModule=original.PAN\r\n", 1)
        self.original.write_bytes(duplicate)
        with self.assertRaisesRegex(ValueError, "exactly once"):
            self.store.clone("Example.PRJ", "VC0", "VC2", {"PAN": "new.PAN"})
        self.assertFalse((self.workspace / "Projects" / "Example.VC2").exists())
        self.assertEqual(before.count(b"PVModule=original.PAN"), 2)

    def test_rejects_malformed_component_before_writing_variant(self):
        unsafe = self.workspace / "ComposPV" / "Gensets" / "unsafe.GEN"
        unsafe.write_text(GEN.replace("CFuelHor=0.25", "CFuelHor=0"), encoding="utf-8")
        before = self.original.read_bytes()
        with self.assertRaisesRegex(ValueError, "CFuelHor must be greater than zero"):
            self.store.clone("Example.PRJ", "VC0", "VC4", {"GEN": "unsafe.GEN"})
        self.assertFalse((self.workspace / "Projects" / "Example.VC4").exists())
        self.assertEqual(self.original.read_bytes(), before)

    def test_rejects_invalid_project_file(self):
        project = self.workspace / "Projects" / "Example.PRJ"
        project.write_text("unrecognized project", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "pvProject"):
            self.store.clone("Example.PRJ", "VC0", "VC4")
        self.assertFalse((self.workspace / "Projects" / "Example.VC4").exists())

    def test_rejects_linked_variant_and_backup_paths(self):
        outside = self.workspace.parent / "outside.VC0"
        outside.write_text(VARIANT, encoding="utf-8")
        link = self.workspace / "Projects" / "Example.VC5"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Symlink creation is unavailable on this Windows host")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.store.inspect("Example.PRJ", "VC5")
        self.assertEqual(outside.read_text(encoding="utf-8"), VARIANT)
        before = self.store.inspect("Example.PRJ", "VC0")
        changed = self.store.update("Example.PRJ", "VC0", {"PAN": "new.PAN"},
                                    before["sha256"])
        backup = self.workspace / "Projects" / ".mcp-variant-backups" / changed["backup_name"]
        backup.unlink()
        backup.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "Symlink backups"):
            self.store.restore("Example.PRJ", "VC0", backup.name, changed["sha256"], True)
        self.assertEqual(outside.read_text(encoding="utf-8"), VARIANT)

    def _prepare_grid_variant(self):
        branch = """  PVObject_SystemCircuit=pvCircuit
    InverterNode Start;
      SubArrayId=1
      SubArrayName=PV Array
      Children=Start
        StringNode Start;
          SubArrayId=1
          SubArrayName=PV Array
        StringNode End;
      Children=End
    InverterNode End;
  End of PVObject pvCircuit
"""
        data = VARIANT.replace("  PVObject_PVMainArray=pvMainArray\n",
                               "  PVObject_PVMainArray=pvMainArray\n" + branch)
        data = data.replace("PVObject_=pvSubArray\n        SubArrayId=1\n",
                            "PVObject_=pvSubArray\n        Comment=PV Array\n        NInverter=1\n        NStringCh=1\n        SubArrayId=1\n", 1)
        data = data.replace("        GInverter=original.OND\n", "        GInverter=original.OND\n        NoOrientation=1\n", 1)
        data = data.replace("    SystemType=Battery", "    SystemType=Grid")
        data = data.replace("End of PVObject pvVCalcul\n",
                            "  PVObject_Ombrage=pvShading\n    Flags=$00\n"
                            "  End of PVObject pvShading\nEnd of PVObject pvVCalcul\n")
        self.original.write_bytes(data.replace("\n", "\r\n").encode("utf-8"))
        return data

    def test_clone_subarray_updates_complete_branch_and_restores(self):
        data = self._prepare_grid_variant()
        before = self.store.inspect("Example.PRJ", "VC0")
        result = self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"])
        self.assertEqual(result["subarray_id"], 3)
        updated = self.original.read_bytes()
        self.assertEqual(updated.count(b"SubArrayId=3"), 3)
        self.assertEqual(updated.count(b"SubArrayName=Sub-array #3"), 2)
        self.assertEqual(self.store.inspect("Example.PRJ", "VC0")["subarrays"][-1]["id"], 3)
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"])
        removed = self.store.remove_subarray("Example.PRJ", "VC0", 3, result["sha256"])
        self.assertEqual(removed["sha256"], before["sha256"])
        self.assertEqual(self.original.read_bytes(), data.replace("\n", "\r\n").encode("utf-8"))
        self.store.restore("Example.PRJ", "VC0", removed["backup_name"], removed["sha256"], True)
        self.assertEqual(self.original.read_bytes(), updated)
        self.store.restore("Example.PRJ", "VC0", result["backup_name"], result["sha256"], True)
        self.assertEqual(self.original.read_bytes(), data.replace("\n", "\r\n").encode("utf-8"))
        self.original.write_bytes(data.replace("Flags=$00\n  End of PVObject pvShading",
                                               "Flags=$20\n  End of PVObject pvShading").encode("utf-8"))
        digest = self.store.inspect("Example.PRJ", "VC0")["sha256"]
        with self.assertRaisesRegex(ValueError, "Shading is active"):
            self.store.clone_subarray("Example.PRJ", "VC0", 1, digest)

        self.original.write_bytes(data.replace("\n", "\r\n").encode("utf-8"))
        state = self.store.inspect("Example.PRJ", "VC0")
        external = self.original.read_bytes().replace(b"FieldTilt=20.0", b"FieldTilt=22.0")
        save_backup = self.store._backup

        def external_edit(path, raw):
            name = save_backup(path, raw)
            path.write_bytes(external)
            return name

        with patch.object(self.store, "_backup", side_effect=external_edit):
            with self.assertRaisesRegex(ValueError, "changed while preparing"):
                self.store.clone_subarray("Example.PRJ", "VC0", 1, state["sha256"])
        self.assertEqual(self.original.read_bytes(), external)

    def test_project_and_variant_archives_restore_original_bytes(self):
        self.store.clone("Example.PRJ", "VC0", "VC1")
        initial = self.store.inspect_project("Example.PRJ")
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            self.store.archive_project("Example.PRJ", initial["files"])
        wrong = {**initial["files"], "Example.VC0": "0" * 64}
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.archive_project("Example.PRJ", wrong, True)
        variant = self.store.archive_variant("Example.PRJ", "VC1",
                                            initial["files"]["Example.VC1"], True)
        with self.assertRaisesRegex(ValueError, "last variant"):
            self.store.archive_variant("Example.PRJ", "VC0",
                                       initial["files"]["Example.VC0"], True)
        with self.assertRaisesRegex(ValueError, "destination already exists"):
            self.store.clone("Example.PRJ", "VC0", "VC1")
            self.store.restore_archive("Example.PRJ", variant["archive_name"], True)
        (self.workspace / "Projects" / "Example.VC1").unlink()
        self.store.restore_archive("Example.PRJ", variant["archive_name"], True)
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], initial["files"])
        archived = self.store.archive_project("Example.PRJ", initial["files"], True)
        self.assertFalse((self.workspace / "Projects" / "Example.PRJ").exists())
        self.store.restore_archive("Example.PRJ", archived["archive_name"], True)
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], initial["files"])

    def test_parameter_edits_are_scoped_guarded_and_reversible(self):
        self.original.write_bytes(self.original.read_bytes().replace(b"  Version=8.1.6", b"  Comment=Original variant\r\n  Version=8.1.6", 1))
        initial = self.store.inspect_parameters("Example.PRJ", "VC0")
        self.assertEqual(initial["variant_name"], "Original variant")
        self.assertEqual(initial["orientations"][0]["values"]["FieldTilt"], "20.0")
        changed = self.store.update_parameters("Example.PRJ", "VC0", initial["sha256"], 1, 1,
                  {"NModSerie": 15, "VBkUpEncl_syst": 0.9}, {"FieldTilt": 25, "FieldAzim": -10}, variant_name="Revised variant")
        updated = self.store.inspect_parameters("Example.PRJ", "VC0")
        self.assertEqual(updated["variant_name"], "Revised variant")
        self.assertEqual(changed["name_change"], {"before": "Original variant", "after": "Revised variant"})
        self.assertFalse(self.store.update_parameters("Example.PRJ", "VC0", changed["sha256"], 1, 1, variant_name="Revised variant")["changed"])
        self.assertEqual(updated["subarrays"][0]["values"]["VBkUpEncl_syst"], "0.900")
        self.assertEqual(updated["subarrays"][1]["values"], {})
        self.assertEqual(updated["orientations"][0]["values"]["FieldTilt"], "25.000")
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.update_parameters("Example.PRJ", "VC0", initial["sha256"], 1, 1,
                                         {"NModSerie": 12})
        with self.assertRaisesRegex(ValueError, "Invalid FieldTilt"):
            self.store.update_parameters("Example.PRJ", "VC0", changed["sha256"], 1, 1,
                                         orientation_updates={"FieldTilt": 91})
        restored = self.store.restore("Example.PRJ", "VC0", changed["backup_name"],
                                      changed["sha256"], True)
        self.assertEqual(restored["sha256"], initial["sha256"])
        baseline = self.original.read_bytes()
        backups = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
        for name in (True, 55, "", " padded ", "line\nFlags=$00", "tab\tname", "\ud800", "x" * 81, "\u540d" * 27):
            with self.assertRaisesRegex(ValueError, "Invalid variant_name"):
                self.store.update_parameters("Example.PRJ", "VC0", initial["sha256"], 1, 1, variant_name=name)
        own_name = b"  Comment=Original variant\r\n"
        for blocked in (baseline.replace(own_name, b""), baseline.replace(own_name, own_name * 2),
                        baseline.replace(b"End of PVObject pvVCalcul", b"Comment=Late root\r\nEnd of PVObject pvVCalcul"),
                        baseline.replace(own_name, b"").replace(b"    SubArrays=Start", b"    PVObject_Metadata=pvExtra\r\n      Comment=Decoy\r\n    End of PVObject pvExtra\r\n    SubArrays=Start")):
            self.original.write_bytes(blocked)
            state = self.store.inspect_parameters("Example.PRJ", "VC0")
            with self.assertRaisesRegex(ValueError, "owning header Comment"):
                self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, variant_name="Changed")
            self.assertEqual(self.original.read_bytes(), blocked)
        self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backups)
        bomless = baseline.removeprefix(b"\xef\xbb\xbf")
        self.original.write_bytes(bomless)
        state = self.store.inspect_parameters("Example.PRJ", "VC0")
        self.assertFalse(state["utf8_bom"])
        with self.assertRaisesRegex(ValueError, "require UTF-8 BOM"):
            self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, variant_name="\u5c4b\u9876\u65b9\u6848")
        with self.assertRaisesRegex(ValueError, "requires variant_name"):
            self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, subarray_updates={"NModSerie": 15}, add_utf8_bom=True)
        for invalid in (1, "true"):
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, variant_name="ASCII", add_utf8_bom=invalid)
        self.assertEqual(self.original.read_bytes(), bomless)
        self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backups)
        for title in ("\u5c4b\u9876\u65b9\u6848", "Original variant"):
            extra = {"orientation_updates": {"FieldTilt": 25}} if title != "Original variant" else {}
            backup_set = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
            preview = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, variant_name=title, add_utf8_bom=True, dry_run=True, **extra)
            self.assertEqual(self.original.read_bytes(), bomless)
            self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backup_set)
            edited = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, variant_name=title, add_utf8_bom=True, **extra)
            self.assertEqual(preview["sha256"], edited["sha256"])
            self.assertTrue(edited["bom_added"])
            self.assertTrue(self.store.inspect_parameters("Example.PRJ", "VC0")["utf8_bom"])
            expected = bomless.replace(own_name, b"  Comment=" + title.encode("utf-8") + b"\r\n")
            if extra:
                expected = expected.replace(b"FieldTilt=20.0", b"FieldTilt=25.000")
            self.assertEqual(self.original.read_bytes(), b"\xef\xbb\xbf" + expected)
            if title == "Original variant":
                self.assertEqual(edited["diff"], "")
                self.assertTrue(edited["changed"])
            backup_set = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
            noop = self.store.update_parameters("Example.PRJ", "VC0", edited["sha256"], 1, 1, variant_name=title, add_utf8_bom=True, **extra)
            self.assertFalse(noop["changed"])
            self.assertFalse(noop["bom_added"])
            self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backup_set)
            self.store.restore("Example.PRJ", "VC0", edited["backup_name"], edited["sha256"], True)
            self.assertEqual(self.original.read_bytes(), bomless)
        self.original.write_bytes(baseline)
        backups = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
        for field, message in (("UsesTiltAver=True", "Averaged"), ("UsesAzimAver=True", "Averaged"),
                               ("UsesSlopeAver=True", "Averaged"), ("Flags=$20", "ordinary Flags"),
                               ("Flags=$00\r\n    Flags=$00", "ordinary Flags"),
                               ("UsesTiltAver=False\r\n    UsesTiltAver=False", "Averaged"),
                               ("PVObject_Metadata=pvExtra\r\n    End of PVObject pvExtra", "flat fixed-plane")):
            blocked = baseline.replace(b"    FieldType=FixedPlane", ("    " + field + "\r\n    FieldType=FixedPlane").encode())
            self.original.write_bytes(blocked)
            state = self.store.inspect_parameters("Example.PRJ", "VC0")
            for preview in (False, True):
                with self.assertRaisesRegex(ValueError, message):
                    self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1,
                                                 orientation_updates={"FieldTilt": 25}, dry_run=preview)
                self.assertEqual(self.original.read_bytes(), blocked)
                self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backups)
        ordinary = baseline.replace(b"    FieldType=FixedPlane", b"    Flags=$00\r\n    UsesTiltAver=False\r\n    UsesAzimAver=False\r\n    UsesSlopeAver=False\r\n    FieldType=FixedPlane")
        self.original.write_bytes(ordinary)
        state = self.store.inspect_parameters("Example.PRJ", "VC0")
        edited = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, orientation_updates={"FieldTilt": 30})
        self.assertEqual(self.original.read_bytes(), ordinary.replace(b"FieldTilt=20.0", b"FieldTilt=30.000"))
        self.store.restore("Example.PRJ", "VC0", edited["backup_name"], edited["sha256"], True)
        self.assertEqual(self.original.read_bytes(), ordinary)
        for loss, version in (("LIDLoss", "8.1.0"), ("LIDLoss", "8.1.6"), ("PMismMPP", "8.1.6"),
                              ("PMismMPP", "8.1.0"), ("UFactor", "8.1.6"), ("UFactor", "8.1.0"),
                               ("ModuleQualityLoss", "8.1.6"), ("ModuleQualityLoss", "8.1.0")):
            main = loss == "UFactor"
            quality = loss == "ModuleQualityLoss"
            field_name = "PQualityModU" if quality else loss
            initial_value, target = ("20.0", 29) if main else ("0.00", -5 if quality else 3)
            scope = "main_array_updates" if main else "subarray_updates"
            fixture = VARIANT.replace("SystemType=Battery", "SystemType=Grid")
            if main:
                fixture = fixture.replace("SubArrayId=", f"Version={version}\n        SubArrayId=").replace(
                    "PVObject_PVMainArray=pvMainArray", "PVObject_PVMainArray=pvMainArray\n"
                    "    Flags=$00100400\n    Flags2=$082A\n    UFactor=20.0\n"
                    "    PVObject_Metadata=pvExtra\n      UFactor=99\n      Flags=$FFFFFFFF\n"
                    "      Flags2=$FFFF\n    End of PVObject pvExtra")
            elif quality:
                fixture = fixture.replace("SubArrayId=1", f"Version={version}\n        SubArrayId=1\n"
                    "        PQualityModU=0.00\n        PQualityModI=0.00\n        PQualityModP=99\n"
                    "        PMismMPP=2.00\n        Flags=$001E0402\n        PVObject_Metadata=pvExtra\n"
                    "          PQualityModU=99\n          PQualityModI=99\n          PQualityModP=99\n"
                    "          Flags=$FFFFFFFF\n        End of PVObject pvExtra")
            else:
                fixture = fixture.replace("SubArrayId=1", f"Version={version}\n        SubArrayId=1\n        {loss}=0.00\n"
                    f"        Flags=$001E0402\n        PVObject_Metadata=pvExtra\n          {loss}=99\n"
                    "          Flags=$FFFFFFFF\n        End of PVObject pvExtra")
            raw = b"\xef\xbb\xbf" + fixture.replace("\n", "\r\n").encode()
            self.original.write_bytes(raw)
            state = self.store.inspect_parameters("Example.PRJ", "VC0")
            values = state["main_array"]["values"] if main else state["subarrays"][0]["values"]
            self.assertEqual(values[field_name], initial_value)
            self.assertEqual(values["Flags"], "$00100400" if main else "$001E0402")
            updates = {scope: {loss: target}}
            preview = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, **updates, dry_run=True)
            self.assertEqual(self.original.read_bytes(), raw)
            changed = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, **updates)
            field = f"{field_name}={initial_value}".encode()
            expected = raw.replace(field, f"{field_name}={target:.3f}".encode())
            if quality:
                expected = expected.replace(b"PQualityModI=0.00", f"PQualityModI={target:.3f}".encode()).replace(b"Flags=$001E0402", b"Flags=$000E0402")
                self.assertEqual(changed["module_quality"], {"default_before": True, "default_after": False})
                self.assertEqual(preview["module_quality"], changed["module_quality"])
                self.assertEqual(values["PQualityModP"], "99")
            if loss == "PMismMPP":
                expected = expected.replace(b"Flags=$001E0402", b"Flags=$001A0402")
                self.assertEqual(preview["mismatch"], {"default_before": True, "default_after": False})
                self.assertEqual(changed["mismatch"], preview["mismatch"])
            self.assertEqual(changed["sha256"], preview["sha256"])
            self.assertEqual(self.original.read_bytes(), expected)
            self.assertFalse(self.store.update_parameters("Example.PRJ", "VC0", changed["sha256"], 1, 1, **updates)["changed"])
            self.store.restore("Example.PRJ", "VC0", changed["backup_name"], changed["sha256"], True)
            self.assertEqual(self.original.read_bytes(), raw)
            if quality:
                combined = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1,
                    {"ModuleQualityLoss": target, "PMismMPP": 5})
                self.assertEqual(self.original.read_bytes(), expected.replace(b"Flags=$000E0402", b"Flags=$000A0402").replace(b"PMismMPP=2.00", b"PMismMPP=5.000"))
                self.assertEqual(combined["mismatch"], {"default_before": True, "default_after": False})
                self.assertEqual(combined["module_quality"], {"default_before": True, "default_after": False})
                self.store.restore("Example.PRJ", "VC0", combined["backup_name"], combined["sha256"], True)
                self.assertEqual(self.original.read_bytes(), raw)
                for raw_field in ("PQualityModU", "PQualityModI", "PQualityModP"):
                    with self.assertRaisesRegex(ValueError, "supported"):
                        self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, {raw_field: target})
            backups = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
            for value in (14.99 if main else -5.01 if quality else -0.01,
                          20.01 if quality else 100.01, True, "3", float("inf"), float("nan")):
                with self.assertRaisesRegex(ValueError, f"Invalid {loss}"):
                    self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, **{scope: {loss: value}})
            guards = [(raw.replace(field + b"\r\n", b""), f"{field_name} must occur"),
                (raw.replace(field, field + f"\r\n        {field_name}=1".encode()), f"{field_name} must occur"),
                (raw.replace(("Version=" + version).encode(), b"Version=8.1.5"), "verified"),
                (raw.replace(b"SystemType=Grid", b"SystemType=Battery"), "verified"),
                (raw.replace(b"PVObject_PVMainArray", b"PVObject_ModuleLayout=1\r\n  PVObject_PVMainArray"), "module layout")]
            if main:
                guards += [(raw.replace(b"Flags=$00100400\r\n", b""), "verified main-array Flags"),
                    (raw.replace(b"Flags=$00100400", b"Flags=$00100400\r\n    Flags=$00"), "verified main-array Flags"),
                    (raw.replace(b"Flags2=$082A", b"Flags2=$00"), "verified main-array Flags"),
                    (raw.replace(b"PVObject_PVMainArray=pvMainArray", b"PVObject_PVMainArray=pvOther"), "complete main array")]
                with self.assertRaisesRegex(ValueError, "supported"):
                    self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, {loss: target})
                with self.assertRaisesRegex(ValueError, "separate"):
                    self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, **updates, independent_mppt_enabled=True)
            elif loss == "PMismMPP" or quality:
                if quality:
                    guards += [(raw.replace(b"        PQualityModI=0.00\r\n", b""), "PQualityModI must occur"),
                        (raw.replace(b"PQualityModI=0.00", b"PQualityModI=0.00\r\n        PQualityModI=1"), "PQualityModI must occur")]
                guards += [(raw.replace(b"Flags=$001E0402\r\n", b""), "one owning Flags"),
                    (raw.replace(b"Flags=$001E0402", b"Flags=$001E0402\r\n        Flags=$00"), "one owning Flags"),
                    (raw.replace(b"Flags=$001E0402", b"Flags=broken"), "hexadecimal Flags")]
            for blocked, message in guards:
                self.original.write_bytes(blocked)
                blocked_state = self.store.inspect("Example.PRJ", "VC0")
                with self.assertRaisesRegex(ValueError, message):
                    self.store.update_parameters("Example.PRJ", "VC0", blocked_state["sha256"], 1, 1, **updates)
                self.assertEqual(self.original.read_bytes(), blocked)
            self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backups)

    def test_structure_validator_reports_missing_orientation(self):
        checked = self.store.validate_structure("Example.PRJ", "VC0")
        self.assertFalse(checked["valid"])
        self.assertEqual(checked["subarray_ids"], [1, 2])
        self.assertTrue(any("needs one NoOrientation" in message for message in checked["issues"]))

    def test_inspect_parameters_ignores_shading_object_orientation_ids(self):
        data = self.original.read_bytes().replace(
            b"End of PVObject pvVCalcul\r\n",
            b"PVObject_Ombrage=pvShading\r\n    NoOrient=1\r\n"
            b"End of PVObject pvShading\r\nEnd of PVObject pvVCalcul\r\n")
        self.original.write_bytes(data)
        inspected = self.store.inspect_parameters("Example.PRJ", "VC0")
        self.assertEqual([item["id"] for item in inspected["orientations"]], [1])

    def test_orientation_edit_rejects_active_scene_without_table(self):
        data = self.original.read_bytes().replace(
            b"End of PVObject pvVCalcul\r\n",
            b"PVObject_Ombrage=pvShading\r\n    Flags=$20\r\n"
            b"End of PVObject pvShading\r\nEnd of PVObject pvVCalcul\r\n")
        self.original.write_bytes(data)
        digest = self.store.inspect_parameters("Example.PRJ", "VC0")["sha256"]
        for options in ({"orientation_updates": {"FieldTilt": 25}}, {"subarray_updates": {"NModSerie": 15}}):
            for preview in (False, True):
                with self.assertRaisesRegex(ValueError, "active shading scene"):
                    self.store.update_parameters("Example.PRJ", "VC0", digest, 1, 1, dry_run=preview, **options)
                self.assertEqual(self.original.read_bytes(), data)
        layout = VARIANT.replace("End of PVObject pvVCalcul", "PVObject_ModuleLayout=pvModuleLayout\nFlags=$0B10\nModLayoutGlobState=mlDefInverters\nEnd of PVObject pvModuleLayout\nEnd of PVObject pvVCalcul").encode()
        self.original.write_bytes(layout)
        digest = self.store.inspect_parameters("Example.PRJ", "VC0")["sha256"]
        for options in ({"orientation_updates": {"FieldTilt": 25}}, {"subarray_updates": {"NModSerie": 15}}):
            with self.assertRaisesRegex(ValueError, "module layout"):
                self.store.update_parameters("Example.PRJ", "VC0", digest, 1, 1, **options)
            self.assertEqual(self.original.read_bytes(), layout)
        self.assertFalse((self.workspace / "Projects" / ".mcp-variant-backups").exists())

    def test_orientation_with_shading_table_rejected(self):
        data = self.original.read_bytes().replace(b"    FieldAzim=0.0\r\n",
                        b"    FieldAzim=0.0\r\n    PVObject_ShdTableLin=pvShdFTable\r\n", 1)
        self.original.write_bytes(data)
        digest = self.store.inspect_parameters("Example.PRJ", "VC0")["sha256"]
        for options in ({"orientation_updates": {"FieldTilt": 30}}, {"subarray_updates": {"NModSerie": 15}}):
            with self.assertRaisesRegex(ValueError, "dependent shading table"):
                self.store.update_parameters("Example.PRJ", "VC0", digest, 1, 1, **options)
            self.assertEqual(self.original.read_bytes(), data)

    def test_inspect_project_reports_site_meteo_links_without_nested_false_match(self):
        (self.workspace / "Sites").mkdir()
        (self.workspace / "Meteo").mkdir()
        (self.workspace / "Sites" / "first.SIT").write_text("site", encoding="utf-8")
        (self.workspace / "Meteo" / "first.MET").write_bytes(b"met")
        (self.workspace / "Projects" / "Example.PRJ").write_text(
            "PVObject_=pvProject\nMeteoFileName=first.MET\n"
            "PVObject_SitePrj=pvSite\n  NomF=first.SIT\nEnd of PVObject pvSite\n"
            "End of PVObject pvProject\n", encoding="utf-8")
        variant = VARIANT.replace("  PVObject_=pvOrient\n",
            "  PVObject_SiteSimul=pvSite\n    NomF=second.SIT\n"
            "  End of PVObject pvSite\n  PVObject_MeteoSimul=pvMeteo\n"
            "    NomF=first.MET\n    PVObject_SiteMet=pvSite\n"
            "      NomF=New.SIT\n    End of PVObject pvSite\n"
            "  End of PVObject pvMeteo\n  PVObject_=pvOrient\n")
        self.original.write_text(variant, encoding="utf-8")
        inspected = self.store.inspect_project("Example.PRJ")
        self.assertTrue(inspected["sources"]["Example.PRJ"]["meteo"]["workspace_file"])
        self.assertEqual(inspected["sources"]["Example.VC0"]["meteo"]["name"], "first.MET")
        self.assertIn("Example.VC0: site differs from Example.PRJ", inspected["warnings"])
        self.assertTrue(any("second.SIT is not a workspace file" in item
                            for item in inspected["warnings"]))

    def _prepare_project_sources(self):
        (self.workspace / "Sites").mkdir()
        (self.workspace / "Meteo").mkdir()
        (self.workspace / "Sites" / "New.SIT").write_text(
            "PVObject_=pvSite\n  NomF=New.SIT\n  Site=New Site\n"
            "  Latitude=6.5\n  Longitude=80.1\nEnd of PVObject pvSite\n", encoding="utf-8")
        (self.workspace / "Meteo" / "New.MET").write_bytes(b"fixture weather")
        (self.workspace / "Projects" / "Example.PRJ").write_text(
            "PVObject_=pvProject\n  Comment=Old project\nMeteoFileName=Old.MET\n"
            "PVObject_SitePrj=pvSite\n  NomF=Old.SIT\n  Site=Old Site\n"
            "End of PVObject pvSite\nEnd of PVObject pvProject\n", encoding="utf-8")
        variant = VARIANT.replace("  PVObject_=pvOrient\n",
            "  PVObject_SiteSimul=pvSite\n    NomF=Old.SIT\n    Site=Old Site\n"
            "  End of PVObject pvSite\n  PVObject_MeteoSimul=pvMeteo\n"
            "    NomF=Old.MET\n    SiteM=Old Site\n"
            "    PVObject_SiteMet=pvSite\n      NomF=Old.SIT\n"
            "    End of PVObject pvSite\n  End of PVObject pvMeteo\n"
            "  PVObject_=pvOrient\n")
        self.original.write_bytes(b"\xef\xbb\xbf" + variant.replace("\n", "\r\n").encode("utf-8"))
        second = self.workspace / "Projects" / "Example.VC1"
        second.write_bytes(self.original.read_bytes())
        before = self.store.inspect_project("Example.PRJ")
        originals = {name: (self.workspace / "Projects" / name).read_bytes()
                     for name in before["files"]}
        return before, originals

    def test_update_and_restore_project_sources_across_all_variants(self):
        before, originals = self._prepare_project_sources()
        with self.assertRaisesRegex(ValueError, "existing workspace files"):
            self.store.update_project_sources("Example.PRJ", "missing.SIT", "New.MET", before["files"])
        changed = self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", before["files"])
        self.assertEqual(set(changed["backups"]), set(before["files"]))
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], changed["files"])
        self.assertEqual(self.store.inspect_project("Example.PRJ")["warnings"], [])
        for name in ("Example.VC0", "Example.VC1"):
            raw = (self.workspace / "Projects" / name).read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", raw)
            self.assertIn(b"NomF=New.MET", raw)
            self.assertIn(b"SiteM=New Site", raw)
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", before["files"])
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            self.store.restore_project_sources("Example.PRJ", changed["backups"], changed["files"])
        restored = self.store.restore_project_sources("Example.PRJ", changed["backups"],
                                                      changed["files"], True)
        self.assertEqual(restored["files"], before["files"])
        for name, data in originals.items():
            self.assertEqual((self.workspace / "Projects" / name).read_bytes(), data)
        project = self.workspace / "Projects" / "Example.PRJ"
        siblings = {name: ((self.workspace / "Projects" / name).stat().st_mtime_ns,
                          (self.workspace / "Projects" / name).stat().st_ino)
                    for name in before["files"] if name != project.name}
        self.assertEqual(before["project_name"], "Old project")
        for value in (True, 56, "", " padded ", "line\nFlags=$00", "\ud800", "x" * 81, "\u540d" * 27):
            with self.assertRaisesRegex(ValueError, "Invalid project_name"):
                self.store.update_project_parameters("Example.PRJ", before["files"], value)
        with self.assertRaisesRegex(ValueError, "require UTF-8 BOM"):
            self.store.update_project_parameters("Example.PRJ", before["files"], "\u5c4b\u9876")
        for title, add_bom in (("Revised project", False), ("\u5c4b\u9876", True), ("Old project", True),
                               ("X", False), ("A" * 80, False), ("\u540d" * 26 + "AB", True)):
            preview = self.store.update_project_parameters("Example.PRJ", before["files"], title, True, add_bom)
            self.assertEqual(project.read_bytes(), originals[project.name])
            edited = self.store.update_project_parameters("Example.PRJ", before["files"], title, False, add_bom)
            self.assertEqual(edited["files"], preview["files"])
            self.assertEqual(self.store.inspect_project("Example.PRJ")["project_name"], title)
            self.assertEqual(edited["bom_added"], add_bom)
            noop = self.store.update_project_parameters("Example.PRJ", edited["files"], title, False, add_bom)
            self.assertFalse(noop["changed"])
            self.assertNotIn("backups", noop)
            with self.assertRaisesRegex(ValueError, "changed since inspection"):
                self.store.update_project_parameters("Example.PRJ", before["files"], title)
            self.store.restore_project_sources("Example.PRJ", edited["backups"], edited["files"], True)
            self.assertEqual(project.read_bytes(), originals[project.name])
            for name, metadata in siblings.items():
                path = self.workspace / "Projects" / name
                self.assertEqual(path.read_bytes(), originals[name])
                self.assertEqual((path.stat().st_mtime_ns, path.stat().st_ino), metadata)
        own = originals[project.name].splitlines(keepends=True)[1]
        for blocked in (originals[project.name].replace(own, b""),
                        originals[project.name].replace(own, own * 2),
                        originals[project.name].replace(b"End of PVObject pvProject", b"Comment=Late\nEnd of PVObject pvProject"),
                        originals[project.name].replace(own, b"PrintSettings=TPrintSettings\nComment=Decoy\nEndTags\n")):
            project.write_bytes(blocked)
            state = self.store.inspect_project("Example.PRJ")
            with self.assertRaisesRegex(ValueError, "owning header Comment"):
                self.store.update_project_parameters("Example.PRJ", state["files"], "Changed")
            self.assertEqual(project.read_bytes(), blocked)
        project.write_bytes(originals[project.name])

    def test_source_update_rejects_malformed_site_and_variant_before_writes(self):
        before, originals = self._prepare_project_sources()
        site = self.workspace / "Sites" / "New.SIT"
        valid = site.read_text(encoding="utf-8")
        for invalid in (valid.replace("NomF=New.SIT", "NomF=Wrong.SIT"),
                        valid.replace("  Latitude=6.5", "  PVObject_Nested=pvSite"),
                        valid.replace("  Latitude=6.5", "  End of PVObject pvSite")):
            with self.subTest(site=invalid):
                site.write_text(invalid, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", before["files"])
                self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], before["files"])
        site.write_text(valid, encoding="utf-8")
        second = self.workspace / "Projects" / "Example.VC1"
        second.write_bytes(originals[second.name].replace(b"SiteM=Old Site", b"Unknown=Old Site"))
        current = self.store.inspect_project("Example.PRJ")["files"]
        with self.assertRaisesRegex(ValueError, "Expected one SiteM"):
            self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", current)
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], current)
        self.assertFalse((self.workspace / "Projects" / ".mcp-variant-backups").exists())

    def test_source_update_and_restore_roll_back_partial_write_failure(self):
        initial, _ = self._prepare_project_sources()
        real_replace = os.replace
        changed = None
        for operation in ("update", "restore"):
            with self.subTest(operation=operation):
                baseline = self.store.inspect_project("Example.PRJ")["files"]
                writes = 0

                def fail_second_replace(source, target):
                    nonlocal writes
                    writes += 1
                    if writes == 2:
                        raise OSError("injected replacement failure")
                    return real_replace(source, target)

                with patch("pvsyst_auto.project.variants.os.replace", side_effect=fail_second_replace):
                    with self.assertRaisesRegex(OSError, "injected replacement"):
                        if operation == "update":
                            self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", baseline)
                        else:
                            self.store.restore_project_sources("Example.PRJ", changed["backups"], baseline, True)
                self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], baseline)
                self.assertEqual(list((self.workspace / "Projects").glob("*.tmp")), [])
                if operation == "update":
                    changed = self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", baseline)
        restored = self.store.restore_project_sources("Example.PRJ", changed["backups"], changed["files"], True)
        self.assertEqual(restored["files"], initial["files"])

    def test_source_transaction_preserves_external_edits_before_commit(self):
        initial, _ = self._prepare_project_sources()
        changed = None
        for operation in ("update", "restore", "project_name"):
            with self.subTest(operation=operation):
                baseline = self.store.inspect_project("Example.PRJ")["files"]
                original_bytes = {name: (self.workspace / "Projects" / name).read_bytes()
                                  for name in baseline}
                real_backup = self.store._backup
                external = original_bytes["Example.VC1"].replace(b"NModSerie=14", b"NModSerie=13")

                def backup_then_edit(path, data, transaction_id=None):
                    name = real_backup(path, data, transaction_id)
                    if path.name == "Example.VC1":
                        path.write_bytes(external)
                    return name

                with patch.object(self.store, "_backup", side_effect=backup_then_edit):
                    with self.assertRaisesRegex(ValueError, "changed while preparing"):
                        if operation == "update":
                            self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", baseline)
                        elif operation == "restore":
                            self.store.restore_project_sources("Example.PRJ", changed["backups"], baseline, True)
                        else:
                            self.store.update_project_parameters("Example.PRJ", baseline, "Changed title")
                for name, data in original_bytes.items():
                    expected = external if name == "Example.VC1" else data
                    self.assertEqual((self.workspace / "Projects" / name).read_bytes(), expected)
                self.assertEqual(list((self.workspace / "Projects").glob("*.tmp")), [])
                (self.workspace / "Projects" / "Example.VC1").write_bytes(original_bytes["Example.VC1"])
                if operation == "update":
                    changed = self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", baseline)
        restored = self.store.restore_project_sources("Example.PRJ", changed["backups"], changed["files"], True)
        self.assertEqual(restored["files"], initial["files"])

    def test_source_transaction_detects_new_variant_before_commit(self):
        initial, originals = self._prepare_project_sources()
        real_backup = self.store._backup
        added = self.workspace / "Projects" / "Example.VC2"

        def backup_then_add(path, data, transaction_id=None):
            name = real_backup(path, data, transaction_id)
            if path.name == "Example.VC1":
                added.write_bytes(originals["Example.VC1"])
            return name

        with patch.object(self.store, "_backup", side_effect=backup_then_add):
            with self.assertRaisesRegex(ValueError, "changed while preparing"):
                self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", initial["files"])
        self.assertEqual(added.read_bytes(), originals["Example.VC1"])
        for name, data in originals.items():
            self.assertEqual((self.workspace / "Projects" / name).read_bytes(), data)
        self.assertEqual(list((self.workspace / "Projects").glob("*.tmp")), [])

    def test_source_restore_rejects_incomplete_stale_and_wrong_backup_sets(self):
        before, _ = self._prepare_project_sources()
        changed = self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", before["files"])
        wrong = {**changed["backups"], "Example.VC1": changed["backups"]["Example.VC0"]}
        for backups, hashes in (({}, changed["files"]), (changed["backups"], before["files"]),
                                (wrong, changed["files"])):
            with self.subTest(backups=backups, hashes=hashes):
                with self.assertRaises(ValueError):
                    self.store.restore_project_sources("Example.PRJ", backups, hashes, True)
                self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], changed["files"])

    def test_archive_variant_requires_a_valid_remaining_variant(self):
        before = self.store.inspect_project("Example.PRJ")["files"]
        sibling = self.workspace / "Projects" / "Example.VC1"
        sibling.write_bytes(b"not a valid variant")
        with self.assertRaises(ValueError):
            self.store.archive_variant("Example.PRJ", "VC0", before["Example.VC0"], True)
        self.assertTrue(self.original.is_file())
        sibling.unlink()
        (self.workspace / "Projects" / "Example.VC0.bak").write_bytes(b"sidecar")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            self.store.archive_variant("Example.PRJ", "VC0", before["Example.VC0"], True)
        self.assertTrue(self.original.is_file())

    def test_clone_project_rejects_orphan_destination_members(self):
        for name in ("Copied.VC9", "Copied.SHDP"):
            with self.subTest(name=name):
                orphan = self.workspace / "Projects" / name
                orphan.write_bytes(b"existing orphan")
                with self.assertRaises(FileExistsError):
                    self.store.clone_project("Example.PRJ", "Copied.PRJ")
                self.assertFalse((self.workspace / "Projects" / "Copied.PRJ").exists())
                self.assertEqual(orphan.read_bytes(), b"existing orphan")
                orphan.unlink()

    def test_clone_project_copies_all_variants_and_rejects_sidecars(self):
        second = self.workspace / "Projects" / "Example.VC1"
        second.write_bytes(self.original.read_bytes())
        result = self.store.clone_project("Example.PRJ", "Copied.PRJ")
        self.assertEqual(set(result["variants"]), {"VC0", "VC1"})
        self.assertEqual((self.workspace / "Projects" / "Copied.PRJ").read_bytes(),
                         (self.workspace / "Projects" / "Example.PRJ").read_bytes())
        self.assertEqual((self.workspace / "Projects" / "Copied.VC0").read_bytes(),
                         self.original.read_bytes())
        with self.assertRaises(FileExistsError):
            self.store.clone_project("Example.PRJ", "Copied.PRJ")
        (self.workspace / "Projects" / "Example.SHDP").write_bytes(b"scene")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            self.store.clone_project("Example.PRJ", "Another.PRJ")
        self.assertFalse((self.workspace / "Projects" / "Another.PRJ").exists())
        project_text = (self.workspace / "Projects" / "Example.PRJ").read_bytes().decode("utf-8")
        variant_text = self.original.read_bytes().decode("utf-8")
        variants = {"vc0": variant_text, "VC1": variant_text}
        created = self.store.create_project("Fresh.PRJ", project_text, variants)
        self.assertEqual(created["variants"], ["VC0", "VC1"])
        self.assertEqual((self.workspace / "Projects" / "Fresh.VC0").read_bytes(), self.original.read_bytes())
        with self.assertRaises(FileExistsError):
            self.store.create_project("Fresh.PRJ", project_text, variants)
        for content, members in (("broken", variants), (project_text, {}),
                                 (project_text, {"VC0": variant_text, "vc0": variant_text}),
                                 (project_text, {"VC0": variant_text, "VC1": "broken"}),
                                 (project_text, {"../VC0": variant_text})):
            with self.assertRaises(ValueError):
                self.store.create_project("Invalid.PRJ", content, members)
            self.assertEqual(list((self.workspace / "Projects").glob("Invalid.*")), [])
        opened = Path.open
        def fail_second(path, *args, **kwargs):
            if path.name == "Partial.VC1":
                raise OSError("injected second variant write failure")
            return opened(path, *args, **kwargs)
        with patch.object(Path, "open", fail_second), self.assertRaises(OSError):
            self.store.create_project("Partial.PRJ", project_text, variants)
        self.assertEqual(list((self.workspace / "Projects").glob("Partial.*")), [])
        self.assertEqual(self.original.read_bytes().decode("utf-8"), variant_text)


if __name__ == "__main__":
    unittest.main()

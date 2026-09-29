"""Offline variant cloning, scoped reference replacement and rollback tests."""
import tempfile
import unittest
from pathlib import Path

from pvsyst_components import ComponentStore
from pvsyst_variants import VariantStore
import pvsyst_mcp_server as server
from pvsyst_cli import PVsystCLI
from test_components import PAN, OND, BTR, GEN


VARIANT = """PVObject_=pvVCalcul
  Version=8.1.6
  PVObject_PVMainArray=pvMainArray
    SubArrays=Start
      PVObject_=pvSubArray
        SubArrayId=1
        PVModule=original.PAN
        GInverter=original.OND
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

    def test_mcp_variant_tools(self):
        server._cli = PVsystCLI(__import__("sys").executable, self.workspace)
        self.addCleanup(setattr, server, "_cli", None)
        inspected = server.pvsyst_get_variant_components("Example.PRJ", "VC0")
        cloned = server.pvsyst_clone_variant("Example.PRJ", "VC0", "VC3",
                                             {"BTR": "new.BTR"})
        self.assertEqual(cloned["variant"], "VC3")
        result = server.pvsyst_update_variant_components("Example.PRJ", "VC3",
                                                          {"OND": "new.OND"}, cloned["sha256"], 1)
        self.assertEqual(server.pvsyst_restore_variant("Example.PRJ", "VC3",
                                                       result["backup_name"], result["sha256"], True)["sha256"],
                         cloned["sha256"])
        self.assertEqual(len(inspected["subarrays"]), 2)


if __name__ == "__main__":
    unittest.main()

"""GUI-style PAN fields use explicit aliases and scoped positive dimensions."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from pvsyst_components import ComponentStore
from test_components import PAN, GEN


GUI_PAN = (PAN.replace("    Model=440W", "    Model=440W\n    Width=1.032\n    Height=1.996")
           .replace("  LargApp=1.032\n", "").replace("  LongApp=1.996\n", "")
           .replace("  ISC=", "  Isc=").replace("  MuISC=", "  muISC="))


class PanGUIFormatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ComponentStore(self.root)
        self.store.create("PAN", "gui.PAN", "\ufeff" + GUI_PAN.replace("\n", "\r\n"))
        self.path = self.store._path("PAN", "gui.PAN")

    def test_create_clone_copy_and_inspect_accept_gui_export_spelling(self):
        info = self.store.inspect("PAN", "gui.PAN")
        self.assertFalse(info["warnings"] or info["structural_errors"] or info["known_value_errors"])
        self.assertEqual(info["fields"]["Isc"], ["11.05"])
        self.assertNotIn("ISC", info["fields"])
        self.assertTrue({"ISC", "Isc", "MuISC", "muISC"} <= set(info["editable_fields"]))
        self.assertEqual(set(info["scalar_aliases"]["ISC"]), {"ISC", "Isc"})
        for key in ("Width", "Height"):
            self.assertTrue(info["commercial"]["fields"][key]["editable"])
            self.assertFalse(info["commercial"]["fields"][key]["paired_root_present"])
        self.store.clone("PAN", "gui.PAN", "clone.PAN", {"Manufacturer": "Other", "Model": "Copy", "ISC": "11.1"})
        self.assertIn(b"Isc=11.1", self.store._path("PAN", "clone.PAN").read_bytes())
        self.assertNotIn(b"LargApp", self.store._path("PAN", "clone.PAN").read_bytes())
        self.store.copy("PAN", "gui.PAN", "copy.PAN")
        self.assertEqual(self.path.read_bytes(), self.store._path("PAN", "copy.PAN").read_bytes())

    def test_mixed_edit_previews_aliases_dimensions_and_restore_preserve_bytes(self):
        original = self.path.read_bytes()
        args = {"updates": {"ISC": "11.2", "MuISC": "0.006"},
                "commercial_updates": {"Width": "1.05", "Height": "2.0"},
                "expected_sha256": hashlib.sha256(original).hexdigest()}
        preview = self.store.update("PAN", "gui.PAN", dry_run=True, **args)
        self.assertEqual(self.path.read_bytes(), original)
        edited = self.store.update("PAN", "gui.PAN", **args)
        self.assertEqual(edited["sha256"], preview["sha256"])
        data = self.path.read_bytes()
        self.assertIn(b"Isc=11.2", data)
        self.assertIn(b"muISC=0.006", data)
        self.assertIn(b"Width=1.05", data)
        self.assertNotIn(b"LargApp", data)
        self.assertNotIn(b"LongApp", data)
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.store.restore("PAN", "gui.PAN", edited["backup_name"], True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_both_input_spellings_preserve_target_keys_and_noop(self):
        for source in (PAN, GUI_PAN):
            self.path.write_text(source, encoding="utf-8")
            for updates in ({"ISC": "11.05", "MuISC": "0.0055"}, {"Isc": "11.05", "muISC": "0.0055"}):
                before = self.path.read_bytes()
                result = self.store.update("PAN", "gui.PAN", updates)
                self.assertFalse(result["changed"])
                self.assertEqual(self.path.read_bytes(), before)
                self.assertNotIn("backup_name", result)

    def test_duplicate_aliases_and_conflicting_requests_rejected(self):
        for updates in ({"ISC": "11.2", "Isc": "11.3"}, {"MuISC": ".005", "muISC": ".006"}):
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                self.store.update("PAN", "gui.PAN", updates)
            self.assertEqual(self.path.read_bytes(), before)
        for source in (GUI_PAN.replace("  Isc=11.05", "  Isc=11.05\n  ISC=11.05"),
                       GUI_PAN.replace("  muISC=0.0055", "  muISC=0.0055\n  MuISC=0.0055")):
            self.path.write_text(source, encoding="utf-8")
            self.assertTrue(self.store.inspect("PAN", "gui.PAN")["known_value_errors"])
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                self.store.update("PAN", "gui.PAN", {"PNom": "441"})
            self.assertEqual(self.path.read_bytes(), before)

    def test_bad_gui_numeric_values_and_missing_dimensions_are_not_hidden(self):
        sources = [GUI_PAN.replace("Width=1.032", f"Width={value}") for value in ("0", "-1", "nan", "x")]
        sources += [GUI_PAN.replace("Isc=11.05", f"Isc={value}") for value in ("0", "-1", "NaN", "10")]
        sources += [GUI_PAN.replace("muISC=0.0055", "muISC=inf"),
                    GUI_PAN.replace("    Width=1.032\n", ""),
                    GUI_PAN.replace("Width=1.032", "Width=1.032\n    Width=1.032")]
        for source in sources:
            with self.subTest(source=source):
                self.path.write_text(source, encoding="utf-8")
                info = self.store.inspect("PAN", "gui.PAN")
                self.assertTrue(info["known_value_errors"] or info["warnings"])
                before = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.store.update("PAN", "gui.PAN", {"Model": "Bad source"})
                self.assertEqual(self.path.read_bytes(), before)

    def test_physical_values_and_dimensions_must_belong_to_correct_objects(self):
        for source in (
            GUI_PAN.replace("  Isc=11.05\n", "").replace("    Model=440W", "    Model=440W\n    Isc=11.05"),
            GUI_PAN.replace("    Width=1.032\n", "").replace("  PNom=440", "  PNom=440\n  Width=1.032")):
            self.path.write_text(source, encoding="utf-8")
            self.assertTrue(self.store.inspect("PAN", "gui.PAN")["warnings"])
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                self.store.update("PAN", "gui.PAN", {"Isc": "11.2"})
            self.assertEqual(self.path.read_bytes(), before)
        # A similarly named metadata field is preserved, not mistaken for the physical one.
        source = GUI_PAN.replace("    Model=440W", "    Model=440W\n    Isc=99")
        self.path.write_text(source, encoding="utf-8")
        self.store.update("PAN", "gui.PAN", {"ISC": "11.2"})
        self.assertIn("    Isc=99", self.path.read_text(encoding="utf-8"))
        self.assertIn("  Isc=11.2", self.path.read_text(encoding="utf-8"))
        source = GUI_PAN.replace("  Version=", "  Manufacturer=Root metadata\n  Version=")
        self.path.write_text(source, encoding="utf-8")
        self.assertEqual(self.store.inspect("PAN", "gui.PAN")["manufacturer"], "Lab")
        # PAN aliases do not turn an unrelated component's metadata into a numeric field.
        self.store.create("GEN", "meta.GEN", GEN.replace("  TypeGen=", "  Isc=metadata\n  TypeGen="))

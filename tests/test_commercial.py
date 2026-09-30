"""Commercial forms exercise scoped edits, array resizing and exact recovery."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from pvsyst_components import ComponentStore
from pvsyst_commercial import commercial_inventory
from test_components import PAN, OND, BTR, GEN


COMMERCIAL = """    DataSource=Fixture
    Width = 1.032
    Height=1.996
    Weight=24.5
    Remarks, Count=2
      Str_1=First
      Str_2=Second
    End of Remarks=Second
"""


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

    def test_form_all_types_and_preview_apply_restore(self):
        for kind, path in self.paths.items():
            with self.subTest(kind=kind):
                original = path.read_bytes()
                form = self.store.inspect(kind, path.name)["commercial"]
                self.assertTrue(form["editable"])
                self.assertEqual(form["remarks"], ["First", "Second"])
                self.assertEqual(form["fields"]["Width"]["unit"], "m")
                args = {"commercial_updates": {"Manufacturer": "Research Lab", "Model": "Edited",
                        "Width": "1.2", "Height": "2.1", "Depth": "0.04", "Weight": "26",
                        "YearBeg": "2026", "NPieces": "2", "PriceDate": "30/09/26 00:15"},
                        "remarks": ["Alpha = beta", "设备资料", "Third"]}
                preview = self.edit(kind, dry_run=True, **args)
                self.assertEqual(path.read_bytes(), original)
                edited = self.edit(kind, **args)
                self.assertEqual(edited["sha256"], preview["sha256"])
                content = path.read_text(encoding="utf-8")
                self.assertIn("Comment=Research Lab\tEdited\tStatus", content)
                self.assertIn("Width = 1.2", content)
                self.assertIn("Remarks, Count=3", content)
                self.assertIn("End of Remarks=Third", content)
                if kind == "PAN":
                    self.assertIn("LargApp=1.2", content)
                    self.assertIn("LongApp=2.1", content)
                self.assertEqual(self.store.inspect(kind, path.name)["commercial"]["remarks"], args["remarks"])
                self.store.restore(kind, path.name, edited["backup_name"], True)
                self.assertEqual(path.read_bytes(), original)

    def test_noop_preserves_numeric_spelling_bom_and_mixed_whitespace(self):
        path = self.paths["PAN"]
        source = path.read_text(encoding="utf-8").replace("Width = 1.032", "Width \t=  1.032  ")
        path.write_bytes(b"\xef\xbb\xbf" + source.replace("\n", "\r\n").encode())
        original = path.read_bytes()
        result = self.edit(commercial_updates={"Width": "1.032"}, remarks=["First", "Second"])
        self.assertFalse(result["changed"])
        self.assertNotIn("backup_name", result)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
        self.edit(commercial_updates={"Depth": "0.031"}, remarks=["One", "Two", "Three", "Four", "Five"])
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertIn(b"Width \t=  1.032  ", data)

    def test_clear_then_add_remarks_and_optional_fields(self):
        self.edit(remarks=[])
        self.assertNotIn(b"Remarks", self.paths["PAN"].read_bytes())
        result = self.edit(commercial_updates={"DataSource": "", "Depth": "0.03"}, remarks=["A", ""])
        form = result["commercial_after"]
        self.assertEqual(form["remarks"], ["A", ""])
        self.assertEqual(form["fields"]["DataSource"]["value"], "")
        self.assertEqual(form["fields"]["Depth"]["value"], "0.03")

    def test_guard_invalid_values_and_conflicting_arguments_leave_no_backup(self):
        bad = [
            {"expected_sha256": None, "commercial_updates": {"Weight": "5"}},
            {"expected_sha256": "a" * 64, "commercial_updates": {"Weight": "5"}},
            {"commercial_updates": {"Flags": "$00"}},
            {"commercial_updates": {"Weight": "nan"}},
            {"commercial_updates": {"Weight": "inf"}},
            {"commercial_updates": {"Width": "0"}},
            {"commercial_updates": {"Depth": "-1"}},
            {"commercial_updates": {"NPieces": "1.5"}},
            {"commercial_updates": {"NPieces": "2147483648"}},
            {"commercial_updates": {"YearBeg": "-1"}},
            {"commercial_updates": {"PriceDate": "31/02/26 10:00"}},
            {"commercial_updates": {"Model": ""}},
            {"commercial_updates": {"Model": "line\nbreak"}},
            {"commercial_updates": {"Model": "tabs\tbreak"}},
            {"commercial_updates": {"Model": "Unicode\u2028break"}},
            {"commercial_updates": {"Model": True}},
            {"commercial_updates": {"Model": "x" * 2049}},
            {"remarks": ["a"] * 6}, {"remarks": "not an array"}, {"remarks": [None]},
            {"updates": {"Model": "a"}, "commercial_updates": {"Model": "b"}},
        ]
        original = self.paths["PAN"].read_bytes()
        for args in bad:
            with self.subTest(args=str(args)[:120]), self.assertRaises(ValueError):
                self.edit(**args)
        self.assertEqual(self.paths["PAN"].read_bytes(), original)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

    def test_scoped_scalar_does_not_change_other_objects(self):
        path = self.paths["GEN"]
        data = path.read_text(encoding="utf-8").replace("  TypeGen=", "  Weight=123\n  TypeGen=")
        path.write_text(data, encoding="utf-8")
        self.edit("GEN", commercial_updates={"Weight": "55"})
        self.assertIn("  Weight=123\n", path.read_text(encoding="utf-8"))
        self.assertIn("    Weight=55\n", path.read_text(encoding="utf-8"))

    def test_ambiguous_and_damaged_arrays_refused(self):
        path = self.paths["PAN"]
        original = path.read_text(encoding="utf-8")
        bad = [original.replace("Count=2", "Count=3"), original.replace("Str_2=", "Str_1="),
               original.replace("End of Remarks=Second", "End of Remarks=Wrong"),
               original.replace("    Weight=24.5", "    Weight=24.5\n    Weight=5"),
               original.replace("    Remarks, Count=2", "    Remarks, Count=-1"),
               original.replace(COMMERCIAL, COMMERCIAL + COMMERCIAL),
               original.replace(COMMERCIAL, "    Str_1=Orphan\n"),
               original.replace(COMMERCIAL, COMMERCIAL + "    Str_3=Orphan\n"),
               original.replace("Version=8.1.6", "Version=8.0.6"),
               original.replace("    Weight=24.5", "    PVObject_Price=pvPrice\n    End of PVObject pvPrice")]
        for text in bad:
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text[:100]):
                self.assertFalse(commercial_inventory("PAN", text)["editable"])
                with self.assertRaises(ValueError):
                    self.edit(remarks=["Repair?"])
                self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_pan_dimensions_require_unique_root_counterpart(self):
        path = self.paths["PAN"]
        data = path.read_text(encoding="utf-8").replace("  LargApp=1.032", "  Other=1.032")
        path.write_text(data, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "LargApp must occur exactly once"):
            self.edit(commercial_updates={"Width": "1.1"})
        self.assertEqual(path.read_text(encoding="utf-8"), data)

    def test_scalar_and_commercial_changes_share_one_snapshot(self):
        original = self.paths["GEN"].read_bytes()
        result = self.edit("GEN", updates={"PNomGen": "55"}, commercial_updates={"Model": "55 kVA"})
        self.assertIn(b"PNomGen=55", self.paths["GEN"].read_bytes())
        self.store.restore("GEN", "test.GEN", result["backup_name"], True)
        self.assertEqual(self.paths["GEN"].read_bytes(), original)

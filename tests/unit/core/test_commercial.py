"""Commercial forms exercise scoped edits, array resizing and exact recovery."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.components.commercial import commercial_inventory
from tests.unit.core.test_components import PAN, OND, BTR, GEN


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
                args = {"commercial_updates": {"Manufacturer": "Research Lab", "Model": "Edited",
                        "Width": "1.2", "Height": "2.1", "Depth": "0.04", "Weight": "26",
                        "YearBeg": "2026", "NPieces": "2", "PriceDate": "30/09/26 00:15"},
                        "remarks": ["Alpha = beta", "设备资料", "Third"]}
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
        bad = [
            {"expected_sha256": "0" * 64, "commercial_updates": {"Weight": "5"}},
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
            with self.assertRaisesRegex(ValueError, "LargApp must occur exactly once"):
                self.edit(commercial_updates={"Width": "1.1"})
            self.assertEqual(path.read_text(encoding="utf-8"), dup_data)

        clean = PAN.replace("  Version=", "  Comment=Lab\tFixture\tStatus\n  Version=").replace(
            "  End of PVObject pvCommercial", COMMERCIAL + "  End of PVObject pvCommercial")
        bad = [clean.replace("Count=2", "Count=3"), clean.replace("Str_2=", "Str_1="),
               clean.replace("End of Remarks=Second", "End of Remarks=Wrong"),
               clean.replace("    Weight=24.5", "    Weight=24.5\n    Weight=5"),
               clean.replace("    Remarks, Count=2", "    Remarks, Count=-1"),
               clean.replace(COMMERCIAL, COMMERCIAL + COMMERCIAL),
               clean.replace(COMMERCIAL, "    Str_1=Orphan\n"),
               clean.replace(COMMERCIAL, COMMERCIAL + "    Str_3=Orphan\n"),
               clean.replace("Version=8.1.6", "Version=8.0.6"),
               clean.replace("    Weight=24.5", "    PVObject_Price=pvPrice\n    End of PVObject pvPrice")]
        for text in bad:
            with self.subTest(case="ambiguous_array", text=text[:100]):
                path.write_text(text, encoding="utf-8")
                self.assertFalse(commercial_inventory("PAN", text)["editable"])
                with self.assertRaises(ValueError):
                    self.edit(remarks=["Repair?"])
                self.assertEqual(path.read_text(encoding="utf-8"), text)

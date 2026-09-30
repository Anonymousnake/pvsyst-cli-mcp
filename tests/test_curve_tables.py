"""Whole curve-table edits synchronize serialized capacity, count and point rows."""
import tempfile
import unittest
from pathlib import Path

from pvsyst_components import ComponentStore
from test_component_editor import CURVED_OND, POINTS
from test_battery_editor import CURVED_BTR, CAPACITY_POINTS


class CurveTableTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = ComponentStore(self.root)

    def setup_component(self, kind, text):
        path = self.store._path(kind, "test." + kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode())
        return path

    def edit(self, kind, path, points, **kwargs):
        args = {"curve_updates": {path: points}, "expected_sha256": self.store.inspect(kind, "test." + kind)["sha256"],
                "use_file_curve": True}
        args.update(kwargs)
        return self.store.update(kind, "test." + kind, {}, **args)

    def test_add_remove_resize_preview_noop_and_restore_for_both_types(self):
        for kind, source, curve, original_points, more in (
            ("OND", CURVED_OND, "Converter/ProfilPIO", POINTS, [[1500, 1425], [2000, 1920], [2500, 2400]]),
            ("BTR", CURVED_BTR, "Capa_DischRate", CAPACITY_POINTS, [[250, 1.42], [300, 1.44], [400, 1.46]])):
            with self.subTest(kind=kind):
                path = self.setup_component(kind, source)
                original = path.read_bytes()
                points = original_points + more
                preview = self.edit(kind, curve, points, dry_run=True)
                self.assertEqual(path.read_bytes(), original)
                changed = self.edit(kind, curve, points)
                self.assertEqual(changed["sha256"], preview["sha256"])
                canonical = "CapaCourant" if kind == "BTR" else curve
                new = next(item for item in self.store.inspect(kind, path.name)["curves"]["items"] if item["path"] == canonical)
                self.assertEqual(new["points"], points)
                self.assertEqual(new["allocated_point_count"], len(points))
                self.assertTrue(new["point_count_editable"])
                self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
                self.assertNotIn(b"\n", path.read_bytes().replace(b"\r\n", b""))
                self.assertFalse(self.edit(kind, canonical, points)["changed"])
                fewer = [points[0], points[1], points[3], points[-1]]
                self.edit(kind, canonical, fewer)
                new = next(item for item in self.store.inspect(kind, path.name)["curves"]["items"] if item["path"] == canonical)
                self.assertEqual((new["point_count"], new["allocated_point_count"]), (4, 4))
                self.assertEqual(new["points"], fewer)
                self.store.restore(kind, path.name, changed["backup_name"], True)
                self.assertEqual(path.read_bytes(), original)

    def test_resize_preserves_unrelated_curves_and_metadata_and_late_root_flags(self):
        text = CURVED_OND.replace("  Flags=$0570\n", "").replace("End of PVObject pvGInverter", "  Flags=$0570\nEnd of PVObject pvGInverter")
        block_start = text.index("    ProfilPIO=TCubicProfile")
        block_end = text.index("    End of TCubicProfile", block_start) + len("    End of TCubicProfile\n")
        original_block = text[block_start:block_end].replace("ProfilPIO=", "ProfilPIOV1=")
        text = text[:block_end] + original_block + text[block_end:]
        text = text.replace("      Mode=1", "      UnknownMetadata=preserve me\n      Mode=1", 1)
        path = self.setup_component("OND", text)
        self.edit("OND", "Converter/ProfilPIO", POINTS + [[1500, 1425]])
        updated = path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
        self.assertIn(original_block, updated)
        self.assertIn("UnknownMetadata=preserve me", updated)
        self.assertTrue(updated.endswith("  Flags=$0560\nEnd of PVObject pvGInverter\n"))

    def test_five_value_rows_are_read_and_native_coefficients_omitted_when_resized(self):
        text = CURVED_OND.replace("Point_1=100,80", "Point_1=100,80,0,0,0.9")
        path = self.setup_component("OND", text)
        self.assertEqual(self.store.inspect("OND", path.name)["curves"]["items"][0]["points"], POINTS)
        self.edit("OND", "Converter/ProfilPIO", POINTS + [[1500, 1425]])
        self.assertNotIn(b"80,0,0,0.9", path.read_bytes())
        self.assertIn(b"LastCompile=$008D", path.read_bytes())

    def test_unknown_compile_or_interleaved_rows_reject_count_change_without_write(self):
        for text in (CURVED_OND.replace("$008D", "$204D"),
                     CURVED_OND.replace("Point_2=200,180", "Unknown=retain\n      Point_2=200,180"),
                     CURVED_OND.replace("Point_1=100,80\n      Point_2=200,180", "Point_2=200,180\n      Point_1=100,80"),
                     CURVED_OND.replace("LastCompile=$008D", "LastCompile=$008D\n      LastCompile=$008D")):
            path = self.setup_component("OND", text)
            original = path.read_bytes()
            info = self.store.inspect("OND", path.name)["curves"]["items"][0]
            self.assertFalse(info["point_count_editable"])
            self.assertTrue(info["point_count_errors"])
            with self.assertRaises(ValueError):
                self.edit("OND", "Converter/ProfilPIO", POINTS + [[1500, 1425]])
            self.assertEqual(path.read_bytes(), original)

    def test_default_and_known_compile_options_preserved(self):
        for value in (None, "$0", "$19", "$008d", "$8089"):
            text = CURVED_OND.replace("      LastCompile=$008D\n", "" if value is None else "      LastCompile=" + value + "\n")
            path = self.setup_component("OND", text)
            self.edit("OND", "Converter/ProfilPIO", POINTS + [[1500, 1425]])
            if value is None:
                self.assertNotIn(b"LastCompile", path.read_bytes())
            else:
                self.assertIn(("LastCompile=" + value).encode(), path.read_bytes())

    def test_native_point_elimination_guards_and_count_limit(self):
        path = self.setup_component("OND", CURVED_OND)
        original = path.read_bytes()
        for points in ([[0, 0]] + POINTS, [[1e-10, 0]] + POINTS,
                       [POINTS[0], [100 + 1e-9, 90]] + POINTS[1:], [[i + 1, i] for i in range(257)]):
            with self.assertRaises(ValueError):
                self.edit("OND", "Converter/ProfilPIO", points)
            self.assertEqual(path.read_bytes(), original)
        largest = [[i + 1, i * 0.9] for i in range(256)]
        self.edit("OND", "Converter/ProfilPIO", largest)
        curve = self.store.inspect("OND", path.name)["curves"]["items"][0]
        self.assertEqual((curve["point_count"], curve["allocated_point_count"]), (256, 256))

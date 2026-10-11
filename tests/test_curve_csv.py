"""CSV transport must produce the same guarded edits as numeric point arrays."""
import csv
import io
import tempfile
import unittest
from pathlib import Path

from pvsyst_components import ComponentStore
from pvsyst_curve_csv import HEADERS, MAX_CSV_BYTES, import_curve_csv
from test_component_editor import CURVED_OND, POINTS
from test_battery_editor import CURVED_BTR, CAPACITY_POINTS


def table(kind, path, points):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(HEADERS[kind, path])
    writer.writerows(points)
    return stream.getvalue()


class CurveCSVTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ComponentStore(self.root)
        for kind, content in (("OND", CURVED_OND), ("BTR", CURVED_BTR)):
            self.store.create(kind, "curve." + kind, "\ufeff" + content.replace("\n", "\r\n"))

    def info(self, kind, **kwargs):
        return self.store.inspect(kind, "curve." + kind, **kwargs)

    def update(self, kind, **kwargs):
        args = {"updates": {}, "expected_sha256": self.info(kind)["sha256"], "use_file_curve": True}
        args.update(kwargs)
        return self.store.update(kind, "curve." + kind, **args)

    def test_export_roundtrip_in_file_mode_is_byte_identical_noop(self):
        for kind, path, points in (("OND", "Converter/ProfilPIO", POINTS), ("BTR", "Capa_DischRate", CAPACITY_POINTS)):
            self.assertNotIn("curve_csv", self.info(kind))
            self.update(kind, curve_updates={path: points})
            info = self.info(kind, include_curve_csv=True)
            canonical = "CapaCourant" if kind == "BTR" else path
            self.assertEqual(import_curve_csv(kind, info["curve_csv"]), {canonical: points})
            self.assertEqual(next(iter(csv.reader(io.StringIO(info["curve_csv"][canonical])))), list(HEADERS[kind, canonical]))
            unchanged = self.update(kind, curve_csv=info["curve_csv"])
            self.assertFalse(unchanged["changed"])
            self.assertNotIn("backup_name", unchanged)

    def test_csv_growth_matches_array_edit_for_both_types(self):
        for kind, path, points in (("OND", "Converter/ProfilPIO", POINTS + [[1500, 1425]]),
                                   ("BTR", "Capa_DischRate", CAPACITY_POINTS + [[300, 1.44]])):
            serialized = {path: "\ufeff" + table(kind, path, points)}
            array_preview = self.update(kind, curve_updates={path: points}, dry_run=True)
            csv_preview = self.update(kind, curve_csv=serialized, dry_run=True)
            self.assertEqual(csv_preview["diff"], array_preview["diff"])
            self.update(kind, curve_csv=serialized)
            canonical = "CapaCourant" if kind == "BTR" else path
            curve = next(c for c in self.info(kind)["curves"]["items"] if c["path"] == canonical)
            self.assertEqual(curve["points"], points)

    def test_numeric_csv_dialect_accepts_quotes_spaces_exponents_and_bom(self):
        text = '\ufeffinput_watts,output_watts\r\n" 1e2 ",+8e1\r\n200,180\r\n500,470.0\r\n1E3,950\r\n'
        self.assertEqual(import_curve_csv("OND", {"Converter/ProfilPIO": text}), {"Converter/ProfilPIO": POINTS})

    def test_bad_csv_rejected_before_any_scalar_write(self):
        good = table("OND", "Converter/ProfilPIO", [[int(x), int(y)] for x, y in POINTS])
        cases = [None, 123, "", good.split("\r\n", 1)[1], good.replace("input_watts", "input_kw"),
                 good.replace(",", ";"), good.replace("100,80", "100,"), good + "\r\n",
                 good.replace("100,80", "100,80,1"), good.replace("100,80", '"100,80'),
                 good.replace("100,80", '"1\n00",80'), good.replace("100,80", "=100,80"),
                 good.replace("100,80", "true,80"), good.replace("100,80", "NaN,80"),
                 good.replace("100,80", "1e999,80"), good.replace("100,80", "100,Infinity"),
                 good.replace("100,80", "1_00,80"), good.replace("100,80", "0,0"),
                 good.replace("100,80", "100,101"), good.replace("100,80", "100,-1"),
                 good.replace("100,80", "500,80"), good.replace("100,80", "100,8\x000"),
                 table("OND", "Converter/ProfilPIO", POINTS[:3])]
        before = self.info("OND")["sha256"]
        for content in cases:
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.update("OND", updates={"PNomConv": "45"}, curve_csv={"Converter/ProfilPIO": content})
            self.assertEqual(self.info("OND")["sha256"], before)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

    def test_resource_and_point_limits(self):
        for content in ("x" * (MAX_CSV_BYTES + 1), "\u4e2d" * (MAX_CSV_BYTES // 2),
                        table("OND", "Converter/ProfilPIO", [[x + 1, x] for x in range(257)])):
            with self.assertRaises(ValueError):
                self.update("OND", curve_csv={"Converter/ProfilPIO": content})
        points = [[x + 1, x * .9] for x in range(256)]
        parsed = import_curve_csv("OND", {"Converter/ProfilPIO": table("OND", "Converter/ProfilPIO", points)})
        self.assertEqual(parsed["Converter/ProfilPIO"], points)

    def test_export_errors_report_unsupported_axes_and_bad_structure(self):
        info = self.info("BTR", include_curve_csv=True)
        self.assertEqual(set(info["curve_csv"]), {"Capa_DischRate"})
        self.assertIn("SelfDisch_Temp", info["curve_csv_errors"])
        path = self.store._path("OND", "curve.OND")
        path.write_bytes(path.read_bytes().replace(b"Point_1=100,80", b"Point_1=invalid"))
        info = self.info("OND", include_curve_csv=True)
        self.assertEqual(info["curve_csv"], {})
        self.assertTrue(info["curve_csv_errors"]["Converter/ProfilPIO"])

    def test_explicit_curve_mapping_and_export_boolean_required(self):
        for data in ({}, [], {"unknown": "x"}, {"CapaCourant": "x"},
                     {"Converter/ProfilPIO": "x", "Converter/ProfilPIOV1": "y"}):
            with self.assertRaises(ValueError):
                self.update("OND", curve_csv=data)
        with self.assertRaises(ValueError):
            self.info("OND", include_curve_csv="true")
        with self.assertRaises(ValueError):
            self.store.inspect("PAN", "curve.PAN", include_curve_csv=True)

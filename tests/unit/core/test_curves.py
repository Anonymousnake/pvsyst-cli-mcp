"""Tests for component efficiency/capacity curve tables and CSV transport."""
import csv
import io
import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.components.curves import HEADERS, MAX_CSV_BYTES, import_curve_csv
from pvsyst_auto.components.store import ComponentStore
from tests._fixtures import write_utf8_crlf
from tests.unit.core.test_battery_editor import CAPACITY_POINTS, CURVED_BTR
from tests.unit.core.test_component_editor import CURVED_OND, POINTS


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
        for kind, content in (("OND", CURVED_OND), ("BTR", CURVED_BTR)):
            self.store.create(kind, "curve." + kind, "\ufeff" + content.replace("\n", "\r\n"))

    def info(self, kind, **kwargs):
        return self.store.inspect(kind, "curve." + kind, **kwargs)

    def update(self, kind, **kwargs):
        args = {"updates": {}, "expected_sha256": self.info(kind)["sha256"], "use_file_curve": True}
        args.update(kwargs)
        return self.store.update(kind, "curve." + kind, **args)

    def test_add_resize_shrink_noop_and_restore(self):
        for kind, path, base, more in (
            ("OND", "Converter/ProfilPIO", POINTS, [[1500, 1425], [2000, 1920], [2500, 2400]]),
            ("BTR", "Capa_DischRate", CAPACITY_POINTS, [[250, 1.42], [300, 1.44], [400, 1.46]]),
        ):
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
        for kind, path, points, added in (
            ("OND", "Converter/ProfilPIO", POINTS, [1500, 1425]),
            ("BTR", "Capa_DischRate", CAPACITY_POINTS, [300, 1.44]),
        ):
            self.assertNotIn("curve_csv", self.info(kind))
            self.update(kind, curve_updates={path: points})
            exported = self.info(kind, include_curve_csv=True)
            canonical = "CapaCourant" if kind == "BTR" else path
            self.assertEqual(import_curve_csv(kind, exported["curve_csv"]), {canonical: points})
            self.assertFalse(self.update(kind, curve_csv=exported["curve_csv"])["changed"])

            grown = points + [added]
            serialized = {canonical: "\ufeff" + table(kind, canonical, grown)}
            self.assertEqual(self.update(kind, curve_csv=serialized, dry_run=True)["diff"],
                             self.update(kind, curve_updates={canonical: grown}, dry_run=True)["diff"])
            self.update(kind, curve_csv=serialized)
            curve = next(c for c in self.info(kind)["curves"]["items"] if c["path"] == canonical)
            self.assertEqual(curve["points"], grown)

        text = '\ufeffinput_watts,output_watts\r\n" 1e2 ",+8e1\r\n200,180\r\n500,470.0\r\n1E3,950\r\n'
        self.assertEqual(import_curve_csv("OND", {"Converter/ProfilPIO": text}), {"Converter/ProfilPIO": POINTS})

    def test_bad_csv_and_export_guards(self):
        good = table("OND", "Converter/ProfilPIO", [[int(x), int(y)] for x, y in POINTS])
        bad_csvs = [None, 123, "", good.split("\r\n", 1)[1], good.replace("input_watts", "input_kw"),
                    good.replace(",", ";"), good.replace("100,80", "100,"), good + "\r\n",
                    good.replace("100,80", '"1\n00",80'), good.replace("100,80", "NaN,80"),
                    good.replace("100,80", "1_00,80"), good.replace("100,80", "100,101"),
                    good.replace("100,80", "500,80"), table("OND", "Converter/ProfilPIO", POINTS[:3]),
                    "x" * (MAX_CSV_BYTES + 1), table("OND", "Converter/ProfilPIO", [[x + 1, x] for x in range(257)])]
        before = self.info("OND")["sha256"]
        for content in bad_csvs:
            with self.subTest(content=repr(content)[:40]), self.assertRaises(ValueError):
                self.update("OND", updates={"PNomConv": "45"}, curve_csv={"Converter/ProfilPIO": content})
            self.assertEqual(self.info("OND")["sha256"], before)
        for data in ({}, [], {"unknown": "x"}, {"CapaCourant": "x"},
                     {"Converter/ProfilPIO": "x", "Converter/ProfilPIOV1": "y"}):
            with self.assertRaises(ValueError):
                self.update("OND", curve_csv=data)
        self.assertIn("SelfDisch_Temp", self.info("BTR", include_curve_csv=True)["curve_csv_errors"])
        with self.assertRaises(ValueError):
            self.info("OND", include_curve_csv="true")
        with self.assertRaises(ValueError):
            self.store.inspect("PAN", "curve.PAN", include_curve_csv=True)

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

        for bad in (CURVED_OND.replace("$008D", "$204D"),
                    CURVED_OND.replace("Point_2=200,180", "Unknown=retain\n      Point_2=200,180"),
                    CURVED_OND.replace("Point_1=100,80\n      Point_2=200,180", "Point_2=200,180\n      Point_1=100,80")):
            write_utf8_crlf(ond_path, bad)
            with self.assertRaises(ValueError):
                self.update("OND", curve_updates={"Converter/ProfilPIO": POINTS + [[1500, 1425]]})

        write_utf8_crlf(ond_path, CURVED_OND)
        for bad_pts in ([[0, 0]] + POINTS, [POINTS[0], [100 + 1e-9, 90]] + POINTS[1:], [[i + 1, i] for i in range(257)]):
            with self.assertRaises(ValueError):
                self.update("OND", curve_updates={"Converter/ProfilPIO": bad_pts})
        largest = [[i + 1, i * 0.9] for i in range(256)]
        self.update("OND", curve_updates={"Converter/ProfilPIO": largest})
        self.assertEqual(self.info("OND")["curves"]["items"][0]["allocated_point_count"], 256)


if __name__ == "__main__":
    unittest.main()

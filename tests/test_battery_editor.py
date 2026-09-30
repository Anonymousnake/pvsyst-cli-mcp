"""Synthetic BTR capacity curves: tag activation, native ratio and recovery."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from pvsyst_battery_editor import capacity_ratio
from pvsyst_components import ComponentStore
from test_components import BTR


CAPACITY_POINTS = [[5, 0.85], [10, 1], [25, 1.1], [70, 1.25], [130, 1.35], [200, 1.4]]
CAPACITY_PROFILE = """  Capa_DischRate=TCubicProfile
    NPtsMax=8
    NPtsEff=6
    Mode=1
    LastCompile=$008D
    Point_1=5,0.85
    Point_2=10,1
    Point_3=25,1.1
    Point_4=70,1.25
    Point_5=130,1.35
    Point_6=200,1.4
    Point_7=0,0
    Point_8=0,0
  End of TCubicProfile
"""
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
        args = {"updates": {}, "expected_sha256": self.sha(), "use_file_curve": True,
                "curve_updates": {"Capa_DischRate": CAPACITY_POINTS}}
        args.update(kwargs)
        return self.store.update("BTR", "test.BTR", **args)

    def rejected(self, **kwargs):
        original = self.path.read_bytes()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), self.assertRaises(ValueError):
                self.edit(dry_run=dry_run, **kwargs)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

    def test_inspection_distinguishes_ignored_names_and_supported_edit(self):
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

    def test_explicit_activation_preview_apply_and_exact_restore(self):
        text = CURVED_BTR.replace("  Capa_DischRate=", "  Capa_DischRate \t=  ").replace(
            "    Manufacturer=Lab", "    Flags=$AB\n    Manufacturer=Lab")
        original = b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode()
        self.path.write_bytes(original)
        preview = self.edit(updates={"Model": "Custom"}, dry_run=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
        self.assertEqual(preview["curve_control_before"]["path"], "Capa_DischRate")
        self.assertEqual(preview["curve_control_after"]["path"], "CapaCourant")
        self.assertEqual(preview["curve_control_after"]["source"], "file")
        edited = self.edit(updates={"Model": "Custom"})
        self.assertEqual(edited["sha256"], preview["sha256"])
        self.assertEqual(self.path.read_bytes(), original.replace(
            b"Capa_DischRate", b"CapaCourant").replace(b"Model=160Ah", b"Model=Custom"))
        self.store.restore("BTR", "test.BTR", edited["backup_name"], confirm=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_requires_hash_correct_existing_path_and_activation(self):
        self.rejected(expected_sha256=None)
        self.rejected(expected_sha256="0" * 64)
        self.rejected(use_file_curve=False)
        self.rejected(curve_updates={"CapaCourant": CAPACITY_POINTS})
        self.rejected(curve_updates={"SelfDisch_Temp": CAPACITY_POINTS})
        self.rejected(curve_updates={"Capa_DischRate": CAPACITY_POINTS, "CapaCourant": CAPACITY_POINTS})

    def test_canonical_noop_and_edit_preserve_padding_and_other_curves(self):
        text = CURVED_BTR.replace("Capa_DischRate", "CapaCourant").replace("5,0.85", "5.0,0.850")
        self.path.write_text(text, encoding="utf-8")
        args = {"use_file_curve": False, "curve_updates": {"CapaCourant": CAPACITY_POINTS}}
        self.assertFalse(self.edit(**args)["changed"])
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
        points = [pair[:] for pair in CAPACITY_POINTS]
        points[2] = [25, 1.125]
        self.edit(use_file_curve=False, curve_updates={"CapaCourant": points})
        self.assertEqual(self.path.read_text(encoding="utf-8"), text.replace("25,1.1", "25,1.125"))

    def test_technology_and_version_guards(self):
        for technology in ("Li_LFP", "NiCd_Open_Plates", "Pb Sealed AGM", "None", "unknown"):
            self.path.write_text(CURVED_BTR.replace("Pb_Sealed_AGM", technology), encoding="utf-8")
            self.rejected()
            info = self.store.inspect("BTR", "test.BTR")["curves"]
            self.assertIsNone(info["control"]["ratio_100h_range"])
            self.assertFalse(info["items"][1]["editable"])
        self.path.write_text(CURVED_BTR.replace("Version=8.1.6", "Version=8.0.0"), encoding="utf-8")
        self.rejected()
        for technology in ("Pb_Sealed_AGM", "btPb_Sealed_AGM", "pb_sealed_gel", "BTPB_SEALED_GEL"):
            self.path.write_text(CURVED_BTR.replace("Pb_Sealed_AGM", technology), encoding="utf-8")
            self.assertTrue(self.edit(dry_run=True)["changed"])

    def test_ambiguous_missing_and_nested_capacity_blocks_are_rejected(self):
        cases = [CURVED_BTR.replace(CAPACITY_PROFILE, CAPACITY_PROFILE * 2),
                 CURVED_BTR.replace(CAPACITY_PROFILE, CAPACITY_PROFILE + CAPACITY_PROFILE.replace(
                     "Capa_DischRate", "CapaCourant")),
                 CURVED_BTR.replace(CAPACITY_PROFILE, ""),
                 CURVED_BTR.replace(CAPACITY_PROFILE, "").replace(
                     "  End of PVObject pvCommercial", CAPACITY_PROFILE + "  End of PVObject pvCommercial"),
                 CURVED_BTR.replace("  BattTechnol=Pb_Sealed_AGM", "  BattTechnol=Pb_Sealed_AGM\n  BattTechnol=Pb_Sealed_Gel")]
        for text in cases:
            self.path.write_text(text, encoding="utf-8")
            self.rejected()

    def test_malformed_profiles_cannot_be_activated(self):
        for old, new in (("NPtsEff=6", "NPtsEff=3"), ("NPtsMax=8", "NPtsMax=9000"),
                         ("Point_8=0,0", "Point_7=0,0"), ("Point_8=0,0", "Point_8=nan,0"),
                         ("Mode=1", "Mode=2"), ("Mode=1", "Other=1")):
            self.path.write_text(CURVED_BTR.replace(old, new), encoding="utf-8")
            self.rejected()

    def test_bad_point_values_counts_and_ratio_rejected_before_scalar_write(self):
        cases = [CAPACITY_POINTS[:3], [[i + 1, 1.3] for i in range(257)], "points", None,
                 [[x, y * 0.8] for x, y in CAPACITY_POINTS],
                 [[x, y * 1.2] for x, y in CAPACITY_POINTS],
                 [[x / 10, y] for x, y in CAPACITY_POINTS],
                 [[x + 100, y] for x, y in CAPACITY_POINTS]]
        for point in ([True, 0.8], ["5", 0.8], [0, 0.8], [5, 0], [5, 2],
                      [5, float("nan")], [float("inf"), 0.8], [10, 0.8], [10 ** 400, 1]):
            cases.append([point] + CAPACITY_POINTS[1:])
        for points in cases:
            with self.subTest(points=points):
                self.rejected(updates={"Model": "Should not write"}, curve_updates={"Capa_DischRate": points})

    def test_ratio_interpolation_boundaries_and_no_extrapolation(self):
        self.assertAlmostEqual(capacity_ratio(CAPACITY_POINTS), 1.3)
        for y in (1.15, 1.45):
            self.assertEqual(capacity_ratio([[1, 0.8], [10, 1], [100, y], [200, 1.5]]), y)
            self.assertEqual(capacity_ratio([[100, y], [110, 1.46], [120, 1.48], [200, 1.5]]), y)
        for y in (1.14999, 1.45001):
            with self.assertRaises(ValueError):
                capacity_ratio([[1, 0.8], [10, 1], [100, y], [200, 1.5]])

    def test_inspection_reports_current_value_errors_but_allows_repair(self):
        text = CURVED_BTR.replace("70,1.25", "7,1.25").replace("130,1.35", "8,1.35").replace("200,1.4", "9,1.4")
        self.path.write_text(text, encoding="utf-8")
        curve = self.store.inspect("BTR", "test.BTR")["curves"]["items"][1]
        self.assertTrue(curve["structure_complete"])
        self.assertTrue(curve["editable"])
        self.assertTrue(curve["value_errors"])
        self.assertIsNone(curve["ratio_100h"])
        self.assertTrue(self.edit(dry_run=True)["changed"])

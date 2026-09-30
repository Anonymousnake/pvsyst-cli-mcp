"""Synthetic curve inspection and guarded component transaction tests."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_components import ComponentStore
from test_components import OND, GEN


POINTS = [[100.0, 80.0], [200.0, 180.0], [500.0, 470.0], [1000.0, 950.0]]
PROFILE = """      NPtsMax=6
      NPtsEff=4
      Mode=1
      LastCompile=$008D
      Point_1=100,80
      Point_2=200,180
      Point_3=500,470
      Point_4=1000,950
      Point_5=0,0
      Point_6=0,0
"""
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

    def test_inventory_describes_guarded_edit_and_excludes_inactive_padding(self):
        info = self.store.inspect("OND", "test.OND")
        curve = info["curves"]["items"][0]
        self.assertEqual(curve["path"], "Converter/ProfilPIO")
        self.assertTrue(curve["editable"])
        self.assertTrue(curve["requires_use_file_curve"])
        self.assertTrue(curve["requires_expected_sha256"])
        self.assertEqual(info["curves"]["control"]["source"], "automatic")
        self.assertTrue(curve["structure_complete"])
        self.assertEqual(curve["simulation_effect"], "unverified")
        self.assertEqual(curve["points"], POINTS)
        self.assertEqual(curve["point_count"], 4)
        self.assertIn("PNomConv", info["editable_fields"])

    def test_preview_then_apply_and_exact_restore(self):
        self.path.write_bytes(b"\xef\xbb\xbf" + CURVED_OND.replace("\n", "\r\n").encode())
        original = self.path.read_bytes()
        args = {"updates": {"Model": "Preview", "PNomConv": "45"}, "expected_sha256": self.sha()}
        preview = self.store.update("OND", "test.OND", dry_run=True, **args)
        self.assertTrue(preview["changed"])
        self.assertEqual(preview["before_sha256"], self.sha())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
        self.assertIn("+    PNomConv=45", preview["diff"])
        result = self.store.update("OND", "test.OND", **args)
        self.assertEqual(result["sha256"], preview["sha256"])
        self.assertEqual(self.path.read_bytes(), original.replace(
            b"PNomConv=50", b"PNomConv=45").replace(b"Model=50kW", b"Model=Preview"))
        self.store.restore("OND", "test.OND", result["backup_name"], confirm=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_stale_preview_and_apply_rejected_before_backup(self):
        before = self.sha()
        self.path.write_bytes(self.path.read_bytes().replace(b"Model=50kW", b"Model=External"))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), self.assertRaisesRegex(ValueError, "changed since inspection"):
                self.store.update("OND", "test.OND", {"Model": "Overwrite"}, before, dry_run)
        self.assertIn(b"Model=External", self.path.read_bytes())
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

    def test_existing_scalar_calls_and_whitespace_are_preserved(self):
        self.path.write_bytes(self.path.read_bytes().replace(b"Model=50kW", b"Model \t=  50kW  "))
        result = self.store.update("OND", "test.OND", {"Model": "Changed"})
        self.assertFalse(result["conflict_checked"])
        self.assertIn(b"Model \t=  Changed  ", self.path.read_bytes())
        self.assertIn(b"Point_6=0,0", self.path.read_bytes())

    def test_noop_creates_no_backup(self):
        result = self.store.update("OND", "test.OND", {"Model": "50kW"}, self.sha())
        self.assertFalse(result["changed"])
        self.assertNotIn("backup_name", result)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

    def test_malformed_preview_rejected_without_writes(self):
        original = self.path.read_bytes()
        for updates in ({"PNomConv": "nan"}, {"PNomConv": "0"}, {"Model": "line\nbreak"},
                        {"Point_1": "100,50"}, {}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.store.update("OND", "test.OND", updates, self.sha(), True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

    def test_curves_are_scoped_instead_of_flattened(self):
        block = "    ProfilPIO=TCubicProfile\n" + PROFILE + "    End of TCubicProfile\n"
        self.path.write_text(CURVED_OND.replace(block, block + block.replace("ProfilPIO=", "ProfilPIOV1=")), encoding="utf-8")
        curves = self.store.inspect("OND", "test.OND")["curves"]["items"]
        self.assertEqual([c["path"] for c in curves], ["Converter/ProfilPIO", "Converter/ProfilPIOV1"])
        self.assertTrue(all(c["structure_complete"] and c["points"] == POINTS for c in curves))

    def test_ambiguous_and_malformed_profiles_report_diagnostics(self):
        block = "    ProfilPIO=TCubicProfile\n" + PROFILE + "    End of TCubicProfile\n"
        broken = [CURVED_OND.replace(block, block + block),
                  CURVED_OND.replace("Point_6=0,0", "Point_5=0,0"),
                  CURVED_OND.replace("NPtsEff=4", "NPtsEff=7"),
                  CURVED_OND.replace("NPtsMax=6", "NPtsMax=1000000"),
                  CURVED_OND.replace("NPtsEff=4", "Unknown=4")]
        for content in broken:
            self.path.write_text(content, encoding="utf-8")
            curve = self.store.inspect("OND", "test.OND")["curves"]["items"][0]
            self.assertFalse(curve["structure_complete"])
            self.assertTrue(curve["errors"])

    def test_external_change_during_backup_is_not_overwritten(self):
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

    def test_other_types_keep_scalar_preview(self):
        self.store.create("GEN", "test.GEN", GEN)
        preview = self.store.update("GEN", "test.GEN", {"PNomGen": "60"}, dry_run=True)
        self.assertTrue(preview["changed"])
        self.assertEqual(self.store.inspect("GEN", "test.GEN")["fields"]["PNomGen"], ["50"])

    def curve_edit(self, **kwargs):
        args = {"updates": {}, "curve_updates": {"Converter/ProfilPIO": POINTS},
                "expected_sha256": self.sha(), "use_file_curve": True}
        args.update(kwargs)
        return self.store.update("OND", "test.OND", **args)

    def assert_rejected_without_write(self, **kwargs):
        original = self.path.read_bytes()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), self.assertRaises(ValueError):
                self.curve_edit(dry_run=dry_run, **kwargs)
        self.assertEqual(original, self.path.read_bytes())
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

    def test_curve_requires_hash_and_explicit_auto_switch(self):
        self.assert_rejected_without_write(expected_sha256=None)
        self.assert_rejected_without_write(expected_sha256="0" * 64)
        self.assert_rejected_without_write(use_file_curve=False)
        self.assert_rejected_without_write(use_file_curve="true")
        self.assert_rejected_without_write(curve_updates=None, updates={"Model": "Changed"})

    def test_atomic_scalar_curve_preview_preserves_scoped_flags_and_exact_bytes(self):
        text = CURVED_OND.replace("Flags=$0570", "Flags \t=  $a0000570  ").replace(
            "    Manufacturer=Lab", "    Flags=$0570\n    Manufacturer=Lab").replace(
            "Point_2=200,180", "Point_2 \t=  200,180  ")
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
        self.assertEqual(edited["sha256"], preview["sha256"])
        expected = original.replace(b"$a0000570", b"$a0000560").replace(
            b"200,180", b"200,170").replace(b"Model=50kW", b"Model=Custom")
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertIn(b"    Flags=$0570", expected)
        self.store.restore("OND", "test.OND", edited["backup_name"], confirm=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_file_mode_noop_and_edit_without_mode_switch(self):
        original = CURVED_OND.replace("Flags=$0570", "Flags=$0560").replace("100,80", "1e2,80.0")
        self.path.write_text(original, encoding="utf-8")
        result = self.curve_edit(use_file_curve=False)
        self.assertFalse(result["changed"])
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())
        points = [p[:] for p in POINTS]
        points[2][1] = 460
        result = self.curve_edit(use_file_curve=False, curve_updates={"Converter/ProfilPIO": points})
        self.assertTrue(result["changed"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), original.replace("500,470", "500,460"))

    def test_mode_switch_clears_only_bit_four_including_mixed_case_flags(self):
        for old, new in (("$0570", "$0560"), ("$Ab0005b0", "$Ab0005a0"), ("$10", "$00")):
            self.path.write_text(CURVED_OND.replace("$0570", old), encoding="utf-8")
            preview = self.curve_edit(dry_run=True)
            self.assertEqual(preview["curve_control_after"]["flags"], new)
            self.assertEqual(int(old[1:], 16) ^ int(new[1:], 16), 0x10)

    def test_unsupported_controls_and_ambiguous_root_flags(self):
        cases = [CURVED_OND.replace("Flags=$0570", value) for value in (
            "Flags=$1570", "Flags=$1570\n  Flags=$0570", "Other=$0570",
            "Flags=1392", "Flags=$100000000")]
        cases += [CURVED_OND.replace("Version=8.1.6", "Version=8.2.0"),
                  CURVED_OND.replace("Mode=1", "Mode=2")]
        for content in cases:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                curve = self.store.inspect("OND", "test.OND")["curves"]["items"][0]
                self.assertFalse(curve["editable"])
                self.assertTrue(curve["edit_errors"])
                self.assert_rejected_without_write()

    def test_nested_flags_do_not_replace_missing_root_flags(self):
        text = CURVED_OND.replace("  Flags=$0570\n", "").replace(
            "    Manufacturer=Lab", "    Flags=$0570\n    Manufacturer=Lab")
        self.path.write_text(text, encoding="utf-8")
        self.assert_rejected_without_write()

    def test_rejects_invalid_point_values_and_count_before_scalar_or_curve_write(self):
        cases = [POINTS[:3], [[i + 1, i] for i in range(257)], "points", None]
        for pair in ([True, 80], ["100", 80], [100, float("nan")], [float("inf"), 80],
                     [100, -1], [100, 101], [-100, 0], [200, 180], [100], [10 ** 400, 1]):
            cases.append([pair] + POINTS[1:])
        for points in cases:
            with self.subTest(points=points):
                self.assert_rejected_without_write(updates={"Model": "Changed"},
                    curve_updates={"Converter/ProfilPIO": points})
        for curves in ({}, {"Point_1": POINTS}, {"Converter/ProfilPIOV1": POINTS},
                       {"Converter/ProfilPIO": POINTS, "Converter/ProfilPIOV2": POINTS}):
            self.assert_rejected_without_write(curve_updates=curves)

    def test_malformed_profiles_cannot_be_edited(self):
        block = "    ProfilPIO=TCubicProfile\n" + PROFILE + "    End of TCubicProfile\n"
        cases = [CURVED_OND.replace(block, block + block),
                 CURVED_OND.replace("NPtsEff=4", "NPtsEff=3"),
                 CURVED_OND.replace("NPtsEff=4", "NPtsEff=7"),
                 CURVED_OND.replace("Point_6=0,0", "Point_5=0,0"),
                 CURVED_OND.replace("Point_6=0,0", "Point_6=nan,0"),
                 CURVED_OND.replace("Mode=1", "Mode=1\n      Mode=1")]
        for content in cases:
            self.path.write_text(content, encoding="utf-8")
            self.assert_rejected_without_write()

    def test_other_curves_and_inactive_rows_are_untouched(self):
        block = "    ProfilPIO=TCubicProfile\n" + PROFILE + "    End of TCubicProfile\n"
        text = CURVED_OND.replace(block, block + block.replace("ProfilPIO=", "ProfilPIOV1="))
        self.path.write_text(text, encoding="utf-8")
        points = [p[:] for p in POINTS]
        points[0] = [110, 85]
        self.curve_edit(curve_updates={"Converter/ProfilPIO": points})
        expected = text.replace("Flags=$0570", "Flags=$0560").replace("Point_1=100,80", "Point_1=110,85", 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), expected)
        curves = self.store.inspect("OND", "test.OND")["curves"]["items"]
        self.assertFalse(curves[1]["editable"])

    def test_curve_update_rejects_other_component_types(self):
        self.store.create("GEN", "test.GEN", GEN)
        info = self.store.inspect("GEN", "test.GEN")
        with self.assertRaisesRegex(ValueError, "only OND"):
            self.store.update("GEN", "test.GEN", {}, info["sha256"],
                curve_updates={"Converter/ProfilPIO": POINTS}, use_file_curve=True)

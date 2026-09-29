"""Synthetic curve inspection and guarded scalar transaction tests."""
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

    def test_inventory_is_readonly_and_excludes_inactive_padding(self):
        info = self.store.inspect("OND", "test.OND")
        curve = info["curves"]["items"][0]
        self.assertEqual(curve["path"], "Converter/ProfilPIO")
        self.assertFalse(curve["editable"])
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

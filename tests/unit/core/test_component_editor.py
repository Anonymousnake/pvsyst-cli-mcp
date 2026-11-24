"""Synthetic curve inspection and guarded component transaction tests."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.components.store import ComponentStore
from tests.unit.core.test_components import OND, GEN


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

    def curve_edit(self, **kwargs):
        args = {"updates": {}, "curve_updates": {"Converter/ProfilPIO": POINTS},
                "expected_sha256": self.sha(), "use_file_curve": True}
        args.update(kwargs)
        return self.store.update("OND", "test.OND", **args)

    def assert_rejected_without_write(self, **kwargs):
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.curve_edit(**kwargs)
        self.assertEqual(original, self.path.read_bytes())
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

    def test_scalar_and_curve_edit_lifecycle_whitespace_and_siblings(self):
        # 1. File-mode noop creates no backup
        file_mode_orig = CURVED_OND.replace("Flags=$0570", "Flags=$0560").replace("100,80", "1e2,80.0")
        self.path.write_text(file_mode_orig, encoding="utf-8")
        noop = self.curve_edit(use_file_curve=False)
        self.assertFalse(noop["changed"])
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

        # 2. Atomic scalar + curve preview creates no backup, apply preserves scoped flags & exact bytes
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
        expected = original.replace(b"$a0000570", b"$a0000560").replace(
            b"200,180", b"200,170").replace(b"Model=50kW", b"Model=Custom")
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertIn(b"    Flags=$0570", expected)
        self.assertEqual((self.root / "ComposPV" / ".mcp-backups" / "OND" / edited["backup_name"]).read_bytes(), original)

        # 3. File-mode edit without mode switch
        self.path.write_text(file_mode_orig, encoding="utf-8")
        fpoints = [p[:] for p in POINTS]
        fpoints[2][1] = 460
        fres = self.curve_edit(use_file_curve=False, curve_updates={"Converter/ProfilPIO": fpoints})
        self.assertTrue(fres["changed"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), file_mode_orig.replace("500,470", "500,460"))

        # 4. Other curves and inactive rows are untouched
        block = "    ProfilPIO=TCubicProfile\n" + PROFILE + "    End of TCubicProfile\n"
        sib_text = CURVED_OND.replace(block, block + block.replace("ProfilPIO=", "ProfilPIOV1="))
        self.path.write_text(sib_text, encoding="utf-8")
        spoints = [p[:] for p in POINTS]
        spoints[0] = [110, 85]
        self.curve_edit(curve_updates={"Converter/ProfilPIO": spoints})
        sib_expected = sib_text.replace("Flags=$0570", "Flags=$0560").replace("Point_1=100,80", "Point_1=110,85", 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), sib_expected)
        self.assertFalse(self.store.inspect("OND", "test.OND")["curves"]["items"][1]["editable"])

        # 5. Scalar calls without hash and whitespace preservation
        self.path.write_text(CURVED_OND.replace("Model=50kW", "Model \t=  50kW  "), encoding="utf-8")
        scalar_res = self.store.update("OND", "test.OND", {"Model": "Changed"})
        self.assertFalse(scalar_res["conflict_checked"])
        self.assertIn(b"Model \t=  Changed  ", self.path.read_bytes())
        self.assertIn(b"Point_6=0,0", self.path.read_bytes())

    def test_hash_race_and_malformed_scalar_guards(self):
        # 1. Malformed scalar preview rejected without writes or backups
        original = self.path.read_bytes()
        for updates in ({"PNomConv": "nan"}, {"PNomConv": "0"}, {"Model": "line\nbreak"},
                        {"Point_1": "100,50"}, {}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.store.update("OND", "test.OND", updates, self.sha(), True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

        # 2. Curve update requires hash & explicit auto switch & rejects other component types
        no_hash = self.curve_edit(expected_sha256=None, dry_run=True)
        self.assertFalse(no_hash["conflict_checked"])
        self.assert_rejected_without_write(expected_sha256="0" * 64)
        self.assert_rejected_without_write(use_file_curve=False)
        self.assert_rejected_without_write(use_file_curve="true")
        self.assert_rejected_without_write(curve_updates=None, updates={"Model": "Changed"})

        self.store.create("GEN", "test.GEN", GEN)
        info = self.store.inspect("GEN", "test.GEN")
        with self.assertRaisesRegex(ValueError, "only OND"):
            self.store.update("GEN", "test.GEN", {}, info["sha256"],
                curve_updates={"Converter/ProfilPIO": POINTS}, use_file_curve=True)

        # 3. Stale preview and apply rejected before backup
        before = self.sha()
        self.path.write_bytes(original.replace(b"Model=50kW", b"Model=External"))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), self.assertRaisesRegex(ValueError, "changed since inspection"):
                self.store.update("OND", "test.OND", {"Model": "Overwrite"}, before, dry_run)
        self.assertIn(b"Model=External", self.path.read_bytes())
        self.assertFalse((self.root / "ComposPV" / ".mcp-backups").exists())

        # 4. External change during backup is not overwritten
        self.path.write_bytes(original)
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

    def test_unsupported_flags_malformed_profiles_and_invalid_points_rejected(self):
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

        nested = CURVED_OND.replace("  Flags=$0570\n", "").replace(
            "    Manufacturer=Lab", "    Flags=$0570\n    Manufacturer=Lab")
        self.path.write_text(nested, encoding="utf-8")
        self.assert_rejected_without_write()

        block = "    ProfilPIO=TCubicProfile\n" + PROFILE + "    End of TCubicProfile\n"
        malformed = [CURVED_OND.replace(block, block + block),
                     CURVED_OND.replace("NPtsEff=4", "NPtsEff=3"),
                     CURVED_OND.replace("NPtsEff=4", "NPtsEff=7"),
                     CURVED_OND.replace("Point_6=0,0", "Point_5=0,0"),
                     CURVED_OND.replace("Point_6=0,0", "Point_6=nan,0"),
                     CURVED_OND.replace("Mode=1", "Mode=1\n      Mode=1")]
        for content in malformed:
            self.path.write_text(content, encoding="utf-8")
            self.assert_rejected_without_write()

        self.path.write_text(CURVED_OND, encoding="utf-8")
        point_cases = [POINTS[:3], [[i + 1, i] for i in range(257)], "points", None]
        for pair in ([True, 80], ["100", 80], [100, float("nan")], [float("inf"), 80],
                     [100, -1], [100, 101], [-100, 0], [200, 180], [100], [10 ** 400, 1]):
            point_cases.append([pair] + POINTS[1:])
        for points in point_cases:
            with self.subTest(points=points):
                self.assert_rejected_without_write(updates={"Model": "Changed"},
                    curve_updates={"Converter/ProfilPIO": points})
        for curves in ({}, {"Point_1": POINTS}, {"Converter/ProfilPIOV1": POINTS},
                       {"Converter/ProfilPIO": POINTS, "Converter/ProfilPIOV2": POINTS}):
            self.assert_rejected_without_write(curve_updates=curves)

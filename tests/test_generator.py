"""Generator configuration tests cover coupled flags, references and thresholds."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_components import ComponentStore
from pvsyst_variants import VariantStore
from test_components import GEN
from test_variants import VARIANT


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "Projects").mkdir()
        (self.root / "Projects" / "Example.PRJ").write_text(
            "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        content = VARIANT.replace("    Flags=$60", "    Flags \t=  $ab0040  ")
        content = content.replace("    GensetFile=original.GEN\n", "")
        self.path = self.root / "Projects" / "Example.VC0"
        self.path.write_bytes(b"\xef\xbb\xbf" + content.replace("\n", "\r\n").encode())
        self.components = ComponentStore(self.root)
        self.components.create("GEN", "test.GEN", GEN)
        self.store = VariantStore(self.root, self.components)
        self.config = {"enabled": True, "filename": "test.GEN", "operating_power_kw": 40,
            "thresholds": {"1": {"VBkUpEncl_syst": 0.9, "VBkUpDecl_syst": 0.95},
                           "2": {"VBkUpEncl_syst": 0.25, "VBkUpDecl_syst": 0.45}}}

    def sha(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def edit(self, generator=None, **kwargs):
        return self.store.update_parameters("Example.PRJ", "VC0", self.sha(), 1, 1,
            generator_updates=generator if generator is not None else self.config, **kwargs)

    def test_enable_preview_apply_disable_and_exact_restore(self):
        original = self.path.read_bytes()
        info = self.store.inspect_parameters("Example.PRJ", "VC0")["generator"]
        self.assertTrue(info["supported"])
        self.assertFalse(info["enabled"])
        self.assertIsNone(info["filename"])
        self.assertIsNone(info["subarrays"][0]["VBkUpDecl_syst"])
        preview = self.edit(dry_run=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "Projects" / ".mcp-variant-backups").exists())
        changed = self.edit()
        self.assertEqual(changed["sha256"], preview["sha256"])
        self.assertTrue(changed["generator_after"]["enabled"])
        self.assertEqual(changed["generator_component"]["nominal_power_kw"], 50)
        self.assertIn(b"Flags \t=  $ab0060  ", self.path.read_bytes())
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", self.path.read_bytes().replace(b"\r\n", b""))
        active = self.path.read_bytes()
        disabled = self.edit({"enabled": False})
        self.assertEqual(self.path.read_bytes(), active.replace(b"$ab0060", b"$ab0040"))
        self.store.restore("Example.PRJ", "VC0", changed["backup_name"], disabled["sha256"], True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_configure_while_disabled_and_noop_preserves_spelling(self):
        self.edit(self.config | {"enabled": False})
        original = self.path.read_bytes()
        previous_backups = list((self.root / "Projects" / ".mcp-variant-backups").iterdir())
        result = self.edit(self.config | {"enabled": False})
        self.assertFalse(result["changed"])
        self.assertNotIn("backup_name", result)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list((self.root / "Projects" / ".mcp-variant-backups").iterdir()), previous_backups)

    def test_810_variant_keeps_its_original_version(self):
        self.path.write_bytes(self.path.read_bytes().replace(b"Version=8.1.6", b"Version=8.1.0", 1))
        changed = self.edit()
        self.assertTrue(changed["generator_after"]["supported"])
        self.assertIn(b"Version=8.1.0", self.path.read_bytes())

    def test_invalid_settings_do_not_write_or_create_backup(self):
        invalid = [{"enabled": "true"}, {"operating_power_kw": True}, {"operating_power_kw": 0},
            {"operating_power_kw": float("nan")}, {"operating_power_kw": -1},
            {"filename": "../test.GEN"}, {"filename": "absent.GEN"}, {"unknown": 1},
            {"thresholds": {"3": {"VBkUpEncl_syst": 0.9}}},
            {"thresholds": {"01": {"VBkUpEncl_syst": 0.9}}},
            {"thresholds": {"1": {"VBkUpEncl_syst": 1.1}}},
            {"thresholds": {"1": {"VBkUpEncl_syst": "0.9"}}},
            {"thresholds": {"1": {"Other": 0.9}}}, {"thresholds": []}]
        original = self.path.read_bytes()
        for change in invalid:
            with self.subTest(change=change), self.assertRaises((ValueError, FileNotFoundError)):
                self.edit(self.config | change)
        for change in ({"enabled": True}, {"enabled": True, "filename": "test.GEN"}):
            with self.assertRaises(ValueError):
                self.edit(change)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "Projects" / ".mcp-variant-backups").exists())

    def test_enable_requires_all_subarray_thresholds(self):
        incomplete = self.config | {"thresholds": {"1": self.config["thresholds"]["1"]}}
        with self.assertRaisesRegex(ValueError, "subarray 2"):
            self.edit(incomplete)

    def test_nested_decoy_fields_are_preserved(self):
        text = self.path.read_text(encoding="utf-8-sig")
        text = text.replace("    BatteryFile=", "    PVObject_Meta=pvMeta\n      Flags=$20\n"
                            "      PEffBackUp=999\n    End of PVObject pvMeta\n    BatteryFile=")
        text = text.replace("        NModSerie=14", "        PVObject_IAM=pvIAM\n"
            "          VBkUpEncl_syst=0.77\n          Curve=TCubicProfile\n"
            "            Point_1=0,1\n          End of TCubicProfile\n"
            "        End of PVObject pvIAM\n        WiringResistance=TWiringResistance\n"
            "          TypeLayout=1\n        EndTags\n        NModSerie=14")
        self.path.write_text(text, encoding="utf-8")
        changed = self.edit()
        result = self.path.read_text(encoding="utf-8")
        self.assertIn("      Flags=$20\n      PEffBackUp=999", result)
        self.assertIn("          VBkUpEncl_syst=0.77", result)
        self.assertEqual(changed["generator_after"]["operating_power_kw"], 40)

    def test_duplicate_scope_unknown_version_and_system_refused(self):
        original = self.path.read_bytes()
        cases = [original.replace(b"Version=8.1.6", b"Version=8.0.6", 1),
            original.replace(b"SystemType=Battery", b"SystemType=Grid"),
            original.replace(b"SystemType=Battery", b"SystemType=Battery\r\n    Flags=$40"),
            original.replace(b"VBkUpEncl_syst=0.250", b"VBkUpEncl_syst=0.250\r\n        VBkUpEncl_syst=0.8")]
        for case in cases:
            self.path.write_bytes(case)
            with self.assertRaises(ValueError):
                self.edit()
            self.assertEqual(self.path.read_bytes(), case)

    def test_power_above_nominal_returns_warning_not_invented_native_rejection(self):
        result = self.edit(self.config | {"operating_power_kw": 60}, dry_run=True)
        self.assertIn("exceeds GEN nominal", result["warnings"][0])

    def test_stale_and_during_backup_races_preserve_external_change(self):
        digest = self.sha()
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b"NModSerie=14", b"NModSerie=15"))
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.update_parameters("Example.PRJ", "VC0", digest, 1, 1, generator_updates=self.config)
        backup = self.store._backup
        def race(*args):
            result = backup(*args)
            self.path.write_bytes(original.replace(b"NModSerie=14", b"NModSerie=16"))
            return result
        with patch.object(self.store, "_backup", side_effect=race), self.assertRaisesRegex(ValueError, "changed while preparing"):
            self.edit()
        self.assertIn(b"NModSerie=16", self.path.read_bytes())
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_generator_dependency_race_refuses_write(self):
        original = self.path.read_bytes()
        backup = self.store._backup
        def race(*args):
            result = backup(*args)
            self.components._path("GEN", "test.GEN").write_text(GEN.replace("PNomGen=50", "PNomGen=60"), encoding="utf-8")
            return result
        with patch.object(self.store, "_backup", side_effect=race), self.assertRaisesRegex(ValueError, "Generator component changed"):
            self.edit()
        self.assertEqual(self.path.read_bytes(), original)

    def test_existing_parameters_also_preview_without_backup(self):
        original = self.path.read_bytes()
        result = self.store.update_parameters("Example.PRJ", "VC0", self.sha(), 1, 1,
            orientation_updates={"FieldTilt": 22}, dry_run=True)
        self.assertTrue(result["changed"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.root / "Projects" / ".mcp-variant-backups").exists())

    def test_builtin_dependency_and_new_workspace_shadow(self):
        builtin = self.root / "DataRO"
        (builtin / "Gensets").mkdir(parents=True)
        (builtin / "Gensets" / "builtin.GEN").write_text(GEN, encoding="utf-8")
        self.components.builtin = builtin
        config = self.config | {"filename": "builtin.GEN"}
        preview = self.edit(config, dry_run=True)
        self.assertEqual(preview["generator_component"]["library"], "builtin")
        backup = self.store._backup
        original = self.path.read_bytes()
        def race(*args):
            result = backup(*args)
            self.components._path("GEN", "builtin.GEN").write_text(GEN, encoding="utf-8")
            return result
        with patch.object(self.store, "_backup", side_effect=race), self.assertRaisesRegex(ValueError, "shadows builtin"):
            self.edit(config)
        self.assertEqual(self.path.read_bytes(), original)

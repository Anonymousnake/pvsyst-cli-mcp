"""Regression tests for the component review fixes; offline fixtures, no native run."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.components import commercial
from pvsyst_auto.components.builder import build_sourced_component
from pvsyst_auto.components.editor import objects
from pvsyst_auto.components.identity import sourced_component
from pvsyst_auto.components.store import ComponentStore
from test_components import PAN, OND, BTR, GEN
from test_pan_gui_format import GUI_PAN


TEXTS = {"PAN": PAN, "OND": OND, "BTR": BTR, "GEN": GEN}
ROOT_CLOSE = {"PAN": "End of PVObject pvModule", "OND": "End of PVObject pvGInverter",
              "BTR": "End of PVObject pvBattery", "GEN": "End of PVObject pvGenerator"}
AGEING = "  PVObject_Ageing=pvAgeing\n    Flags=$00\n  End of TAgeing\n"
DECOY = "  PVObject_Meta=pvMeta\n    Manufacturer=Decoy\n    Model=Decoy\n  End of PVObject pvMeta\n"
MODEL = "Mono 440W half-cell"


class Workspace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ComponentStore(self.root)

    def write(self, kind, name, text):
        """Place a file without create(), so invalid fixtures can be inspected."""
        path = self.store._path(kind, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class NestingTests(Workspace):
    def test_native_ageing_close_tag_is_accepted_like_the_object_parser(self):
        for kind, text in TEXTS.items():
            with self.subTest(kind=kind):
                aged = text.replace(ROOT_CLOSE[kind], AGEING + ROOT_CLOSE[kind])
                self.assertIn("pvAgeing", [block.object_type for block in objects(aged)])
                self.store.create(kind, f"aged.{kind}", aged)
                self.assertEqual(self.store.validate(kind, f"aged.{kind}")["structural_errors"], [])

    def test_ageing_close_tag_must_still_match_its_object(self):
        broken = GEN.replace("  TypeGen=", "  PVObject_Meta=pvMeta\n  End of TAgeing\n  TypeGen=")
        with self.assertRaisesRegex(ValueError, "Unmatched closing tag: End of TAgeing"):
            self.store.create("GEN", "broken.GEN", broken)
        self.assertFalse(self.store._path("GEN", "broken.GEN").exists())


class OwnedIdentityTests(Workspace):
    def test_commercial_identity_wins_over_root_and_nested_decoys(self):
        cases = {"OND": (OND.replace("  PVObject_Commercial=", DECOY + "  PVObject_Commercial="), "50kW"),
                 "BTR": (BTR.replace("  Version=", "  Manufacturer=Root\n  Model=Root\n  Version="), "160Ah"),
                 "GEN": (GEN.replace("  PVObject_Commercial=", DECOY + "  PVObject_Commercial="), "50kVA")}
        for kind, (text, model) in cases.items():
            with self.subTest(kind=kind):
                self.store.create(kind, f"owned.{kind}", text)
                info = self.store.inspect(kind, f"owned.{kind}")
                self.assertEqual((info["manufacturer"], info["model"]), ("Lab", model))
                self.assertEqual(self.store.list(kind, query="Lab")["total"], 1)
                self.assertEqual(self.store.list(kind, query="Decoy")["total"], 0)
                self.assertEqual(self.store.list(kind, query="Root")["total"], 0)
        # identity.py binds the same owner for the same file.
        field, message, payload = sourced_component(self.store, "OND", "owned.OND")
        self.assertEqual((field, message), ("", ""))
        self.assertEqual((payload["manufacturer"], payload["model"]), ("Lab", "50kW"))

    def test_root_identity_is_used_when_the_form_omits_it(self):
        text = (GEN.replace("    Model=50kVA\n", "").replace("  PVObject_Commercial=", DECOY + "  PVObject_Commercial=")
                .replace("  TypeGen=", "  Model=Root model\n  TypeGen="))
        self.store.create("GEN", "root.GEN", text)
        self.assertEqual(self.store.validate("GEN", "root.GEN")["model"], "Root model")

    def test_identity_declared_only_in_another_object_is_missing(self):
        text = OND.replace("    Manufacturer=Lab\n", "").replace("  PVObject_Commercial=", DECOY + "  PVObject_Commercial=")
        self.write("OND", "nested.OND", text)
        info = self.store.inspect("OND", "nested.OND")
        self.assertIsNone(info["manufacturer"])
        self.assertIn("Missing verified field: Manufacturer", info["warnings"])
        with self.assertRaisesRegex(ValueError, "Incomplete template"):
            self.store.create("OND", "copy.OND", text)
        field, _, payload = sourced_component(self.store, "OND", "nested.OND")
        self.assertEqual(field, "OND.manufacturer")
        self.assertIsNone(payload)


class SourcedComponentTests(Workspace):
    def test_missing_file_without_installed_library_reports_the_file(self):
        field, message, payload = sourced_component(self.store, "OND", "absent.OND")
        self.assertEqual(field, "OND.name")
        self.assertIsNone(payload)
        self.assertIn("absent.OND is not a loose OND file", message)
        self.assertNotIn("not configured", message)

    def test_configured_installed_library_is_still_searched(self):
        builtin = self.root / "DataRO"
        (builtin / "Inverters").mkdir(parents=True)
        (builtin / "Inverters" / "official.OND").write_text(OND, encoding="utf-8")
        field, message, payload = sourced_component(ComponentStore(self.root, builtin), "OND", "official.OND")
        self.assertEqual((field, message), ("", ""))
        self.assertEqual(payload["model"], "50kW")

    def test_malformed_nesting_blocks_instead_of_raising(self):
        self.write("OND", "broken.OND", OND.replace("    End of TCubicProfile\n", ""))
        field, message, payload = sourced_component(self.store, "OND", "broken.OND")
        self.assertEqual(field, "OND.name")
        self.assertIn("closing tag", message)
        self.assertIsNone(payload)


class ReferencePointTests(Workspace):
    def test_maximum_power_point_must_lie_strictly_inside_the_curve(self):
        cases = [("Voc must be greater than Vmp", PAN.replace("Vmp=42.1", "Vmp=49.6")),
                 ("ISC must be greater than Imp", PAN.replace("Imp=10.45", "Imp=11.05")),
                 ("ISC must be greater than Imp", GUI_PAN.replace("Imp=10.45", "Imp=11.05"))]
        for message, text in cases:
            with self.subTest(message=message, gui="Isc=" in text):
                self.assertIn(message, self.store._inspect("PAN", text.encode())["known_value_errors"])
                with self.assertRaisesRegex(ValueError, message):
                    self.store.create("PAN", "equal.PAN", text)
                self.assertFalse(self.store._path("PAN", "equal.PAN").exists())

    def test_store_update_and_builder_agree_on_equal_reference_values(self):
        path = self.write("PAN", "Seed.PAN", PAN)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Voc must be greater than Vmp"):
            self.store.update("PAN", "Seed.PAN", {"Vmp": "49.6"})
        self.assertEqual(path.read_bytes(), before)
        result = build(self.store, fields=(("Vmp", 49.6, "V", "49.6", "V"),))
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.store._path("PAN", "Built.PAN").exists())
        # A strictly smaller cited Vmp is accepted by both entries.
        self.assertEqual(build(self.store, fields=(("Vmp", 42.0, "V", "42.0", "V"),))["status"], "needs_review")
        self.store.update("PAN", "Seed.PAN", {"Vmp": "42.0"})


class AsciiNumberTests(Workspace):
    def test_numeric_edits_accept_only_ascii_decimal_text(self):
        # float() takes these spellings, but PVsyst cannot read them back from the file.
        path = self.write("PAN", "Seed.PAN", PAN)
        before = path.read_bytes()
        for value in ("４４０", "4_40", " 440"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "PNom must be a finite number"):
                    self.store.update("PAN", "Seed.PAN", {"PNom": value})
                self.assertEqual(path.read_bytes(), before)
                with self.assertRaisesRegex(ValueError, "Weight must be a finite number"):
                    commercial._value("Weight", value.strip() + "x" if value == " 440" else value)
        with self.assertRaisesRegex(ValueError, "YearBeg must be a"):
            commercial._value("YearBeg", "２０２４")
        self.assertEqual(commercial._value("Weight", "24.5"), "24.5")
        self.assertEqual(commercial._value("YearBeg", "2024"), "2024")
        self.store.update("PAN", "Seed.PAN", {"PNom": "445"})


def build(store, kind="PAN", fields=(("PNom", 440, "W", "440", "Wp"),), **options):
    text = MODEL + "\n" + "\n".join(number + unit for _, _, _, number, unit in fields)
    return build_sourced_component(
        store, component_type=kind, template_name="Seed." + kind, new_name="Built." + kind,
        manufacturer="Fixture", model=MODEL,
        sources={"doc": {"kind": "assumption", "title": "Explicit test fixture",
                         "locator": "fixture://component", "model": MODEL, "text": text}},
        declarations=[{"field": field, "value": value, "unit": unit, "source_id": "doc",
                       "excerpt": number + quoted, "source_value_text": number, "source_unit_text": quoted}
                      for field, value, unit, number, quoted in fields],
        **options)


class BuilderCreationTests(Workspace):
    def setUp(self):
        super().setUp()
        self.write("PAN", "Seed.PAN", PAN)
        self.target = self.store._path("PAN", "Built.PAN")
        self.manifest = self.root / "Results" / "Built.PAN.sources.json"
        self.create = {"dry_run": False, "acknowledge_inherited_model": True}

    def test_failed_create_leaves_no_source_record_and_retry_succeeds(self):
        self.assertEqual(build(self.store)["status"], "needs_review")
        with patch.object(self.store, "_new", side_effect=OSError("simulated create failure")):
            failed = build(self.store, **self.create)
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["component_created"])
        self.assertIn("simulated create failure", failed["issues"][-1]["message"])
        self.assertTrue(any(item["field"] == "recovery" for item in failed["warnings"]))
        self.assertFalse(self.manifest.exists())
        self.assertFalse(self.target.exists())
        retried = build(self.store, **self.create)
        self.assertEqual(retried["status"], "needs_review", retried["issues"])
        self.assertTrue(retried["component_created"])
        self.assertTrue(self.manifest.is_file())
        self.assertTrue(self.target.is_file())

    def test_a_source_record_this_call_did_not_create_is_never_removed(self):
        read = self.store._read
        calls = []

        def racing_read(path):
            data = read(path)
            calls.append(path)
            if len(calls) == 2:  # the final template check right before creation
                self.manifest.parent.mkdir(parents=True, exist_ok=True)
                self.manifest.write_text("foreign", encoding="utf-8")
            return data

        with patch.object(self.store, "_read", side_effect=racing_read):
            result = build(self.store, **self.create)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.manifest.read_text(encoding="utf-8"), "foreign")
        self.assertFalse(self.target.exists())

    def test_unreadable_candidate_electrical_object_blocks_instead_of_raising(self):
        # The flattened store check accepts a Converter that is not a direct root child.
        self.write("OND", "Seed.OND", OND.replace("  Converter=TConverter", "  PVObject_Meta=pvMeta\n  Converter=TConverter")
                   .replace("  End of TConverter\n", "  End of TConverter\n  End of PVObject pvMeta\n"))
        self.assertEqual(self.store.validate("OND", "Seed.OND")["warnings"], [])
        result = build(self.store, "OND", (("PNomConv", 50, "kW", "50", "kW"),))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("electrical fields are unreadable", result["issues"][-1]["message"])
        self.assertFalse((self.root / "Results").exists())


if __name__ == "__main__":
    unittest.main()

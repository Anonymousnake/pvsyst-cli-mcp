"""Regression tests for the part-3 project review fixes (editor, circuit, template, tracking, seasonal)."""
import sys
import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.components.editor import ObjectBlock, replace_key, replace_value
from pvsyst_auto.components.store import electrical_inventory
from pvsyst_auto.project import seasonal
from pvsyst_auto.project.circuit import inventory, set_independent_mppt, set_power_sharing
from pvsyst_auto.project.template import _declare_explicit_counts, standard_seed_texts
from pvsyst_auto.project.tracking import tracking_values
from pvsyst_auto.project.variants import VariantStore

from test_standard_template import (OND_NAME, PAN_NAME, PAN_TEXT, SEED_PRJ, SEED_VC0, FakeCLI,
                                    FixtureComponents, crlf, ond_text)


def crlf_lines(text):
    return crlf(text).splitlines(keepends=True)


def variant_lines(groups, arrays):
    """A minimal Grid variant whose single injection point carries the given branches."""
    return crlf_lines(
        "PVObject_=pvVCalcul\nPVObject_PVMainArray=pvMainArray\nPVObject_SystemCircuit=pvCircuit\n"
        "  BaseNode=Start\n  InjectionPointNode Start;\n  Children=Start\n" + groups
        + "  Children=End\n  InjectionPointNode End;\n  BaseNode=End\nEnd of PVObject pvCircuit\n" + arrays
        + "End of PVObject pvMainArray\nPVObject_System=pvSystem\n  SystemType=Grid\n"
        "End of PVObject pvSystem\nEnd of PVObject pvVCalcul\n")


def subarray(identifier, name, flags, inputs=1, extra=""):
    return (f"PVObject_=pvSubArray\n  Comment=Array {name}\n  Version=8.1.6\n  Flags={flags}\n"
            f"  SubArrayId={identifier}\n  NoOrientation=1\n  NModSerie=12\n  NStringCh=6\n"
            f"  NStrOrient1=6\n  NInverter=2\n  NInvMPPT={inputs}\n  PowerSharingInverterId=-1\n"
            f"{extra}End of PVObject pvSubArray\n")


def port(identifier, name, elements="", strings=""):
    return (f"MPPTNode Start;\n{elements}SubArrayId={identifier}\nSubArrayName=Array {name}\n"
            f"Children=Start\nStringNode Start;\n{strings}SubArrayId={identifier}\n"
            f"SubArrayName=Array {name}\nStringNode End;\nChildren=End\nMPPTNode End;\n")


class ReplaceValueLineBreakTests(unittest.TestCase):
    """Finding 3: a value carrying a line break would inject a new Key=... record."""

    BREAKS = ("\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " ")

    def test_value_line_breaks_cannot_inject_a_record(self):
        for brk in self.BREAKS:
            for value in (f"New{brk}  Injected=1", f"New{brk}", brk):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        replace_value("  Comment=Old\r\n", value)

    def test_exactly_the_splitlines_boundaries_are_rejected(self):
        mismatched = []
        for code in range(sys.maxunicode + 1):
            char = chr(code)
            try:
                replace_value("A=1\r\n", f"x{char}y")
                rejected = False
            except ValueError:
                rejected = True
            if rejected != (len(f"a{char}b".splitlines()) > 1):
                mismatched.append(hex(code))
        self.assertEqual(mismatched, [])

    def test_single_line_values_keep_the_lossless_layout(self):
        self.assertEqual(replace_value("  Comment=Old \t\r\n", "New\tName"), "  Comment=New\tName \t\r\n")
        self.assertEqual(replace_value("A=1\n", ""), "A=\n")
        self.assertEqual(replace_value("A=1", "2"), "A=2")
        with self.assertRaises(TypeError):
            replace_value("A=1\n", 2)

    def test_key_line_breaks_stay_rejected(self):
        for brk in self.BREAKS:
            with self.subTest(brk=brk):
                with self.assertRaises(ValueError):
                    replace_key("A=1\r\n", f"B{brk}C")


class ExplicitCountLineTests(unittest.TestCase):
    """Finding 10: reverse-order insertions must report the rows' final line numbers."""

    def test_reported_lines_are_the_final_inserted_rows(self):
        group = ("InverterNode Start;\nSubArrayId=1\nSubArrayName=Array A\nChildren=Start\n"
                 + port(1, "A", elements="NElements=1\n") * 2 + "Children=End\nInverterNode End;\n")
        lines = variant_lines(group, subarray(1, "A", "$001E0002"))
        before = len(lines)
        added = _declare_explicit_counts(lines, 1)
        self.assertEqual(len(lines), before + 3)
        self.assertEqual(sorted(item["node"] for item in added), ["InverterNode", "StringNode", "StringNode"])
        self.assertEqual(len({item["line"] for item in added}), 3)
        for item in added:
            with self.subTest(item=item):
                self.assertEqual(lines[item["line"] - 1].strip(), "NElements=1")
                self.assertEqual(lines[item["line"] - 2].strip(), f"{item['node']} Start;")

    def test_seed_changes_index_the_derived_variant_text(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "Projects").mkdir()
        for folder, name, text in (("PVmodules", PAN_NAME, PAN_TEXT), ("Inverters", OND_NAME, ond_text(4))):
            (root / "ComposPV" / folder).mkdir(parents=True)
            (root / "ComposPV" / folder / name).write_bytes(crlf(text).encode("utf-8"))
        seeds = root / "install" / "DataRO" / "PVsyst8.1_Data" / "Projects"
        seeds.mkdir(parents=True)
        (root / "install" / "PVsystCLI.exe").write_bytes(b"stub")
        (seeds / "_DEMO_Residential.PRJ").write_bytes(crlf(SEED_PRJ).encode("utf-8"))
        (seeds / "_DEMO_Residential.VC0").write_bytes(b"\xef\xbb\xbf" + crlf(SEED_VC0).encode("utf-8"))
        derived = standard_seed_texts(FakeCLI(root / "install" / "PVsystCLI.exe", root),
                                      VariantStore(root, FixtureComponents(root)),
                                      ond_electrical=electrical_inventory("OND", crlf(ond_text(4))),
                                      pan_name=PAN_NAME, ond_name=OND_NAME)
        lines = derived["vc0_text"].splitlines()
        declared = [item for item in derived["changes"] if item.get("field") == "NElements"]
        self.assertEqual(sorted(item["node"] for item in declared), ["InverterNode", "StringNode"])
        for item in declared:
            with self.subTest(item=item):
                self.assertEqual(lines[item["line"] - 1].strip(), "NElements=1")
                self.assertEqual(lines[item["line"] - 2].strip(), f"{item['node']} Start;")


class CircuitFlagSpellingTests(unittest.TestCase):
    """Finding 11: toggling one Flags bit keeps the token's width and letter case."""

    EXCLUSIVE = ("InverterNode Start;\nNElements=2\nSubArrayId=1\nSubArrayName=Array A\nChildren=Start\n"
                 + port(1, "A", elements="NElements=3\n") + "Children=End\nInverterNode End;\n")

    @staticmethod
    def flags(lines):
        return [line.strip() for line in lines if line.strip().startswith("Flags=")]

    def test_independent_mppt_switch_keeps_the_flag_spelling(self):
        for before, after in (("$019e1002", "$019e1402"), ("$0000080a", "$00000c0a"),
                              ("$019E1002", "$019E1402")):
            with self.subTest(flags=before):
                lines = variant_lines(self.EXCLUSIVE, subarray(1, "A", before))
                arrays, _ = VariantStore._sections(lines)
                self.assertIs(inventory(lines, arrays)["counts_valid"], True)
                original = list(lines)
                set_independent_mppt(lines, arrays, 1, True)
                self.assertEqual(self.flags(lines), [f"Flags={after}"])
                self.assertIn("  NInvMPPT=6\r\n", lines)
                set_independent_mppt(lines, arrays, 1, False)
                self.assertEqual(lines, original)

    def test_enabling_an_enabled_lowercase_layout_changes_nothing(self):
        lines = variant_lines(self.EXCLUSIVE, subarray(1, "A", "$019e1402", inputs=6))
        arrays, _ = VariantStore._sections(lines)
        original = list(lines)
        self.assertTrue(set_independent_mppt(lines, arrays, 1, True)["enabled_before"])
        self.assertEqual(lines, original)

    def test_power_sharing_toggle_keeps_the_flag_spelling(self):
        shared = ("InverterNode Start;\nNElements=2\nSubArrayName=Array A; Array B\nChildren=Start\n"
                  + port(1, "A", strings="NElements=3\n") + port(2, "B", strings="NElements=3\n")
                  + "Children=End\nInverterNode End;\n")
        members = "".join(subarray(identifier, name, "$001e0402", inputs=2, extra="  GInverter=Shared.OND\n")
                          for identifier, name in ((1, "A"), (2, "B")))
        lines = variant_lines(shared, members)
        arrays, _ = VariantStore._sections(lines)
        self.assertIs(inventory(lines, arrays)["counts_valid"], True)
        original = list(lines)
        set_power_sharing(lines, arrays, 1, True)
        self.assertEqual(self.flags(lines), ["Flags=$001e0502"] * 2)
        set_power_sharing(lines, arrays, 1, False)
        self.assertEqual(lines, original)


class TrackingPairTokenTests(unittest.TestCase):
    """Finding 13: an unreadable saved pair token is a ValueError, not decimal.InvalidOperation."""

    @staticmethod
    def source(low, high="55.0"):
        return ObjectBlock("PVObject_", 1, "pvOrient", fields={"MinPhi": [(5, low)], "MaxPhi": [(6, high)]})

    def test_unreadable_saved_token_is_a_value_error(self):
        for token in ("abc", "", "NaN", "sNaN"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "MinPhi"):
                    tracking_values({"max_rotation_deg": 50}, "UnlimTracking", self.source(token))

    def test_readable_pairs_still_render_and_keep_their_order_check(self):
        self.assertEqual(tracking_values({"max_rotation_deg": 50}, "UnlimTracking", self.source("-50.0")),
                         {"MaxPhi": "50.0"})
        two_axis = ObjectBlock("PVObject_", 1, "pvOrient", fields={
            "MinAzim": [(5, "-90.0")], "MaxAzim": [(6, "90.0")], "MinTilt": [(7, "0.0")], "MaxTilt": [(8, "60.0")]})
        with self.assertRaisesRegex(ValueError, "MinAzim must stay below MaxAzim"):
            tracking_values({"min_azimuth_deg": 100}, "TrackTwoAxis", two_axis)


class SeasonalCommentTests(unittest.TestCase):
    """A Comment spelled like a T-class name is free text, not a nested object."""

    UPDATES = {"summer_tilt": 10.0, "winter_tilt": 50.0, "winter_months": [1, 2, 12]}

    @staticmethod
    def orientation(comment):
        return ["  PVObject_=pvOrient\r\n", f"    Comment={comment}\r\n", "    Version=8.1.6\r\n",
                "    Flags=$00\r\n", "    NoOrient=1\r\n", "    FieldType=FixedPlane\r\n",
                "    FieldTilt=20.0\r\n", "    FieldAzim=0.0\r\n", "    BaseSlope=0.0\r\n",
                "    UsesTiltAver=False\r\n", "    UsesAzimAver=False\r\n", "    UsesSlopeAver=False\r\n",
                "  End of TOrientGroup\r\n"]

    def test_comment_spelled_like_a_class_name_is_editable(self):
        for comment in ("TEST", "TSouth_roof", "TA"):
            with self.subTest(comment=comment):
                lines = self.orientation(comment)
                change = seasonal.replace_seasonal(lines, (0, len(lines) - 1), self.UPDATES)
                self.assertEqual(change["before"]["orientation_name"], comment)
                self.assertIn(f"    Comment={comment}\r\n", lines)
                self.assertIn("    FieldType=SeasonalTilt\r\n", lines)

    def test_a_written_orientation_name_reads_back_for_the_next_edit(self):
        lines = self.orientation("South")
        seasonal.replace_seasonal(lines, (0, len(lines) - 1), {**self.UPDATES, "orientation_name": "TSouth"})
        change = seasonal.replace_seasonal(lines, (0, len(lines) - 1), {**self.UPDATES, "winter_months": [11, 12]})
        self.assertEqual(change["before"]["orientation_name"], "TSouth")
        self.assertEqual(change["before"]["winter_months"], [1, 2, 12])
        self.assertIn("    WinterMonths=$0C00\r\n", lines)

    def test_nested_class_objects_still_stop_without_editing(self):
        lines = self.orientation("South")
        lines.insert(2, "    Converter=TConverter\r\n")
        original = list(lines)
        with self.assertRaisesRegex(ValueError, "Nested objects"):
            seasonal.replace_seasonal(lines, (0, len(lines) - 1), self.UPDATES)
        self.assertEqual(lines, original)


if __name__ == "__main__":
    unittest.main()

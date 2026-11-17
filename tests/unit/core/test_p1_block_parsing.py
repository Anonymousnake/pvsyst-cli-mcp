"""P1 guard: native PVObject closing tags are parsed once, by components.editor, without views."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import pvsyst_auto
from pvsyst_auto.components.editor import has_object, objects, root_block

SRC = Path(pvsyst_auto.__file__).resolve().parent

ORIENTED = ("PVObject_=pvVCalcul\r\n"                 # 1
            "  Version=8.1.6\r\n"                       # 2
            "  Orientations, list of=1 TOrientGroup\r\n"  # 3
            "    PVObject_=pvOrient\r\n"                # 4
            "      Comment=End of TOrientGroup\r\n"     # 5: a value, never a closing tag
            "      NoOrient=1\r\n"                      # 6
            "      FieldType=FixedPlane\r\n"            # 7
            "    End of TOrientGroup  \r\n"             # 8
            "  End of Orientations\r\n"                 # 9
            "  PVObject_PVMainArray=pvMainArray\r\n"    # 10
            "    Flags=$01900400\r\n"                   # 11
            "  End of PVObject pvMainArray\r\n"         # 12
            "End of PVObject pvVCalcul\r\n")            # 13
DOME = ("PVObject_=pvVCalcul\n"                         # 1
        "  Orientations, list of=2 TOrientGroup\n"      # 2
        "    PVObject_=pvDomeOrient\n"                  # 3
        "      NoOrient=1\n"                            # 4
        "      IsMainOrient=True\n"                     # 5
        "    End of TDomeOrientGroup\n"                 # 6
        "    PVObject_=pvDomeOrient\n"                  # 7
        "      NoOrient=2\n"                            # 8
        "      IsMainOrient=False\n"                    # 9
        "    End of TDomeOrientGroup\n"                 # 10
        "  End of Orientations\n"                       # 11
        "End of PVObject pvVCalcul\n")                  # 12


def _legacy_view(text):
    """The anchored rewrite the removed module copies applied before parsing."""
    for tag, kind in (("TOrientGroup", "pvOrient"), ("TDomeOrientGroup", "pvDomeOrient")):
        text = re.sub(r"(?m)^([ \t]*)End of %s([ \t]*)(?=\r?$)" % tag, r"\1End of PVObject %s\2" % kind, text)
    return text


class NativeClosingTags(unittest.TestCase):
    def test_orientation_record_closes_with_its_native_tag(self):
        blocks = objects(ORIENTED)
        self.assertEqual([(b.path, b.object_type, b.line, b.end_line) for b in blocks],
                         [("", "pvVCalcul", 1, 13), ("PVObject_", "pvOrient", 4, 8),
                          ("PVObject_PVMainArray", "pvMainArray", 10, 12)])
        orient = blocks[1]
        self.assertEqual(orient.fields, {"Comment": [(4, "End of TOrientGroup")], "NoOrient": [(5, "1")],
                                         "FieldType": [(6, "FixedPlane")]})
        self.assertEqual(blocks[0].fields, {"Version": [(1, "8.1.6")]})
        # Same records, line numbers and raw values as the former rewritten view.
        self.assertEqual(objects(ORIENTED), objects(_legacy_view(ORIENTED)))

    def test_dome_records_close_with_their_native_tag(self):
        blocks = objects(DOME)
        self.assertEqual([(b.path, b.object_type, b.line, b.end_line) for b in blocks],
                         [("", "pvVCalcul", 1, 12), ("PVObject_", "pvDomeOrient", 3, 6),
                          ("PVObject_", "pvDomeOrient", 7, 10)])
        self.assertEqual(blocks[1].fields, {"NoOrient": [(3, "1")], "IsMainOrient": [(4, "True")]})
        self.assertEqual(blocks[2].fields, {"NoOrient": [(7, "2")], "IsMainOrient": [(8, "False")]})
        self.assertEqual(objects(DOME), objects(_legacy_view(DOME)))

    def test_an_alias_closes_only_its_own_object(self):
        cases = {
            "orient closed as dome": ORIENTED.replace("End of TOrientGroup  ", "End of TDomeOrientGroup"),
            "dome closed as orient": DOME.replace("End of TDomeOrientGroup", "End of TOrientGroup", 1),
            "main array closed by an orientation tag": ORIENTED.replace("End of PVObject pvMainArray",
                                                                       "End of TOrientGroup"),
            "stray orientation tag": "PVObject_=pvVCalcul\nEnd of TOrientGroup\nEnd of PVObject pvVCalcul\n",
            "stray tag without any object": "End of TDomeOrientGroup\n",
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaisesRegex(ValueError, "Unmatched object closing tag at line"):
                objects(text)
        with self.assertRaisesRegex(ValueError, "Unclosed component object"):
            objects("PVObject_=pvVCalcul\n  PVObject_=pvOrient\n  End of Orientations\n")


class RootBlock(unittest.TestCase):
    def test_root_of_text_lines_and_slices(self):
        lines = ORIENTED.splitlines(keepends=True)
        for source in (ORIENTED, lines):
            self.assertEqual(root_block(source), objects(ORIENTED)[0])
        orient = root_block(lines, (3, 7))
        self.assertEqual((orient.object_type, orient.line, orient.end_line), ("pvOrient", 1, 5))
        self.assertEqual(orient.one("NoOrient"), (2, "1"))
        self.assertEqual(root_block(ORIENTED, (9, 11)).object_type, "pvMainArray")

    def test_missing_root_is_a_value_error(self):
        for source, bounds, message in (([], None, "Project file has no root PVObject block"),
                                        ("  Comment=orphan\r\n", None, "Project file has no root PVObject block"),
                                        (ORIENTED.splitlines(keepends=True), (1, 2), "Section has no root PVObject block"),
                                        (ORIENTED.splitlines(keepends=True), (40, 50), "Section has no root PVObject block")):
            with self.subTest(source=source, bounds=bounds):
                with self.assertRaises(ValueError) as caught:
                    root_block(source, bounds)
                self.assertEqual(str(caught.exception), message)


class NoPrivateOrientationViews(unittest.TestCase):
    """Block parsing lives in components.editor; modules must not regrow private views."""
    HELPERS = {"_view", "_block_of", "_root_block"}
    NATIVE = ("End of TOrientGroup", "End of TDomeOrientGroup")
    REWRITTEN = re.compile(r"End of PVObject pv(?:Dome)?Orient\b")

    def test_no_module_rewrites_or_wraps_the_orientation_closing_tags(self):
        offenders = []
        modules = sorted(SRC.rglob("*.py"))
        self.assertGreater(len(modules), 50)
        for path in modules:
            where = path.relative_to(SRC).as_posix()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"), str(path))):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in self.HELPERS:
                    offenders.append(f"{where}:{node.lineno} defines {node.name}")
                elif isinstance(node, ast.Name) and node.id == "ORIENT_CLOSE":
                    offenders.append(f"{where}:{node.lineno} uses ORIENT_CLOSE")
                elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                      and self.REWRITTEN.search(node.value)):
                    offenders.append(f"{where}:{node.lineno} spells a rewritten orientation closing tag")
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr in ("replace", "sub", "subn", "compile")
                      and any(isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                              and any(tag in arg.value for tag in self.NATIVE) for arg in node.args)):
                    offenders.append(f"{where}:{node.lineno} rewrites a native orientation closing tag")
        self.assertEqual(offenders, [])


class ObjectPresenceTests(unittest.TestCase):
    def test_only_an_object_start_counts_as_present(self):
        named = ["PVObject_=pvVCalcul\r\n", "  Comment=pvLoad and pvGridStorage study\r\n",
                 "  NomFLoad=old_pvLoad.csv\r\n", "End of PVObject pvVCalcul\r\n"]
        self.assertFalse(has_object(named, "pvLoad", "pvGridStorage"))
        for start in ("  PVObject_UserNeeds=pvLoad\r\n", "  PVObject_UserNeeds = pvLoad\n"):
            with self.subTest(start=start):
                self.assertTrue(has_object(named[:2] + [start] + named[2:], "pvLoad"))
        self.assertTrue(has_object(["  PVObject_GridStorage=pvGridStorage"], "pvLoad", "pvGridStorage"))
        self.assertFalse(has_object(["  PVObject_GridStorage=pvGridStorage"], "pvLoad"))

    def test_no_scope_guard_matches_object_names_as_substrings(self):
        guard = re.compile(r"""["']pv(?:Load|GridStorage)["'] in """)
        offenders = [f"{path.relative_to(SRC).as_posix()}:{number}"
                     for path in sorted(SRC.rglob("*.py"))
                     for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1)
                     if guard.search(line)]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

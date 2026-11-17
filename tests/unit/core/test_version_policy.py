"""The accepted native PVsyst versions live only in pvsyst_auto.versions."""
import ast
import re
import unittest
from pathlib import Path

from pvsyst_auto import versions

SOURCE = Path(versions.__file__).resolve().parent
VERSION = re.compile(r"\b[78]\.\d+\.\d+\b")


def _version_constants(node):
    """String constants that name a release, directly or inside a tuple/list/set literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and VERSION.search(node.value):
        yield node
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for element in node.elts:
            yield from _version_constants(element)


class VersionPolicyTests(unittest.TestCase):
    def test_no_literal_version_checks_outside_the_policy_module(self):
        offenders = []
        for path in sorted(SOURCE.rglob("*.py")):
            if path.name == "versions.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                operands = []
                if isinstance(node, ast.Compare):
                    operands = [node.left, *node.comparators]
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, (ast.Tuple, ast.Set, ast.List)):
                    operands = [node.value]
                for operand in operands:
                    for constant in _version_constants(operand):
                        offenders.append(f"{path.relative_to(SOURCE)}:{constant.lineno}: {constant.value!r}")
        self.assertEqual(offenders, [], "compare against pvsyst_auto.versions instead")

    def test_allow_lists_are_explicit_and_consistent(self):
        for name in ("NATIVE_TEXT", "NATIVE_TEXT_OR_SEED", "CLI_TRACKING_BUILDS"):
            value = getattr(versions, name)
            self.assertIsInstance(value, tuple, name)
            self.assertTrue(value and len(set(value)) == len(value), name)
        self.assertTrue(set(versions.NATIVE_TEXT) < set(versions.NATIVE_TEXT_OR_SEED))
        self.assertIn("8.1.0", versions.NATIVE_TEXT_OR_SEED)
        for token in versions.NATIVE_TEXT_OR_SEED:
            self.assertRegex(token, r"^8\.\d+\.\d+$")
        self.assertEqual(versions.TEXT_LABEL, "/".join(versions.NATIVE_TEXT))
        self.assertEqual(versions.SEED_LABEL, "/".join(versions.NATIVE_TEXT_OR_SEED))
        self.assertIn(versions.HEADLESS_RECORD_VERSION, versions.NATIVE_TEXT)

    def test_unverified_releases_stay_refused(self):
        for token in ("8.1.5", "8.2.0", "9.0.0"):
            self.assertNotIn(token, versions.NATIVE_TEXT_OR_SEED)
        # The Win32 bridge was recorded on one build only; widening it needs a GUI re-recording.
        self.assertEqual(versions.GUI_BUILD, ("8.1.6", "49278"))


if __name__ == "__main__":
    unittest.main()

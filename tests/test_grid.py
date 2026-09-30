"""Representative grouped circuits, sparse serialization and inconsistent inputs."""
import tempfile
import unittest
from pathlib import Path

from pvsyst_components import ComponentStore
from pvsyst_variants import VariantStore


GROUP = """InverterNode Start;
  NElements=2
  SubArrayId=1
  SubArrayName=Array A
  Children=Start
    StringNode Start;
      NElements=3
      SubArrayId=1
      SubArrayName=Array A
    StringNode End;
  Children=End
InverterNode End;
"""
ARRAY = """PVObject_=pvSubArray
  Comment=Array A
  SubArrayId=1
  NoOrientation=1
  NModSerie=12
  NStringCh=6
  NStrOrient1=6
  NInverter=2
  NInvMPPT=1
End of PVObject pvSubArray
"""


def grid_text(groups=GROUP, arrays=ARRAY):
    return ("PVObject_=pvVCalcul\n  Version=8.1.6\n"
            "PVObject_=pvOrient\n  NoOrient=1\n  FieldType=FixedPlane\n"
            "  FieldTilt=20\n  FieldAzim=0\nEnd of TOrientGroup\n"
            "PVObject_PVMainArray=pvMainArray\nPVObject_SystemCircuit=pvCircuit\n"
            "  Version=8.0.0\n  BaseNode=Start\nInjectionPointNode Start;\n"
            "Children=Start\n" + groups + "Children=End\nInjectionPointNode End;\n"
            "  BaseNode=End\nEnd of PVObject pvCircuit\n" + arrays +
            "End of PVObject pvMainArray\nPVObject_System=pvSystem\n"
            "  SystemType=Grid\nEnd of PVObject pvSystem\nEnd of PVObject pvVCalcul\n")


class GridTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "Projects").mkdir()
        (self.root / "Projects" / "Example.PRJ").write_text(
            "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        self.path = self.root / "Projects" / "Example.VC0"
        self.store = VariantStore(self.root, ComponentStore(self.root))

    def inspect(self, text):
        data = b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode()
        self.path.write_bytes(data)
        result = self.store.validate_structure("Example.PRJ", "VC0")
        self.assertEqual(self.path.read_bytes(), data)
        return result

    def test_uneven_groups_belonging_to_same_array_are_valid(self):
        groups = GROUP + GROUP.replace("NElements=2", "NElements=1").replace("NElements=3", "NElements=4")
        arrays = ARRAY.replace("NInverter=2", "NInverter=3").replace("NStringCh=6", "NStringCh=10")
        r = self.inspect(grid_text(groups, arrays))
        self.assertTrue(r["valid"])
        self.assertEqual(r["grid_circuit"]["totals"], {"inverters": 3, "strings": 10, "explicit_mppt_inputs": 0})
        self.assertEqual(r["grid_circuit"]["subarrays"][0]["declared_module_count"], 120)
        self.assertEqual(len(r["grid_circuit"]["inverter_groups"]), 2)

    def test_multiplication_and_missing_nelements_default_to_one(self):
        group = GROUP.replace("    StringNode Start;", "MPPTNode Start;\nNElements=3\nSubArrayId=1\nSubArrayName=Array A\nChildren=Start\n    StringNode Start;")
        group = group.replace("      NElements=3\n", "").replace("    StringNode End;", "    StringNode End;\nChildren=End\nMPPTNode End;")
        r = self.inspect(grid_text(group))
        self.assertTrue(r["valid"])
        self.assertEqual(r["grid_circuit"]["totals"], {"inverters": 2, "strings": 6, "explicit_mppt_inputs": 6})
        # NInvMPPT=1 is preserved as a declaration; MPPT-node presence is not its mode switch.
        self.assertEqual(r["grid_circuit"]["subarrays"][0]["declared"]["NInvMPPT"], "1")

    def test_count_disagreement_is_visible_without_rewriting_either_source(self):
        r = self.inspect(grid_text(arrays=ARRAY.replace("NStringCh=6", "NStringCh=8").replace("NInverter=2", "NInverter=1")))
        self.assertFalse(r["valid"])
        self.assertTrue(r["checks_complete"])
        self.assertEqual(len(r["issues"]), 2)
        row = r["grid_circuit"]["subarrays"][0]
        self.assertEqual(row["declared_module_count"], 96)
        self.assertEqual(row["circuit_module_count"], 72)

    def test_multiple_arrays_keep_separate_counts(self):
        group2 = GROUP.replace("SubArrayId=1", "SubArrayId=2").replace("Array A", "Array B")
        array2 = ARRAY.replace("SubArrayId=1", "SubArrayId=2").replace("Array A", "Array B")
        r = self.inspect(grid_text(GROUP + group2, ARRAY + array2))
        self.assertTrue(r["valid"])
        self.assertEqual(r["grid_circuit"]["totals"]["strings"], 12)
        self.assertEqual([r["circuit"]["inverters"] for r in r["grid_circuit"]["subarrays"]], [2, 2])

    def test_cable_and_nested_array_metadata_cannot_supply_fields(self):
        group = GROUP.replace("  NElements=2", "  CableDefinition=Start\nNElements=999\nSubArrayId=99\nCableDefinition=End\n  NElements=2")
        arrays = ARRAY.replace("  NStringCh=6", "PVObject_Metadata=pvExtra\nNStringCh=6\nEnd of PVObject pvExtra")
        r = self.inspect(grid_text(group, arrays))
        self.assertIsNone(r["valid"])
        self.assertFalse(r["checks_complete"])
        self.assertEqual(r["issues"], [])
        self.assertEqual(r["grid_circuit"]["totals"]["strings"], 6)
        self.assertIn("missing NStringCh", " ".join(r["unchecked_checks"]))

    def test_invalid_and_duplicate_counts_do_not_become_defaults(self):
        for value in ("0", "-1", "1.0", "nan", "2147483648", "1\nNElements=2"):
            with self.subTest(value=value):
                r = self.inspect(grid_text(GROUP.replace("NElements=2", "NElements=" + value)))
                self.assertFalse(r["valid"])
                self.assertTrue(r["issues"])
                self.assertIsNone(r["grid_circuit"]["totals"])
        r = self.inspect(grid_text(GROUP.replace("NElements=2", "NElements=2147483647")))
        self.assertFalse(r["valid"])
        self.assertIn("Expanded circuit count", " ".join(r["issues"]))

    def test_nodes_in_nested_metadata_cannot_be_counted(self):
        group = GROUP.replace("  Children=Start", "  CableDefinition=Start\n  Children=Start")
        group = group.replace("  Children=End", "  Children=End\n  CableDefinition=End")
        r = self.inspect(grid_text(group))
        self.assertFalse(r["valid"])
        self.assertIn("nested metadata", " ".join(r["issues"]))
        self.assertIsNone(r["grid_circuit"]["totals"])

    def test_repeated_injection_counts_are_explicitly_unchecked(self):
        r = self.inspect(grid_text().replace("InjectionPointNode Start;", "InjectionPointNode Start;\nNElements=2"))
        self.assertIsNone(r["valid"])
        self.assertIn("repeated transformer/injection", " ".join(r["unchecked_checks"]))
        self.assertIsNone(r["grid_circuit"]["totals"])

    def test_bad_references_and_names_are_rejected(self):
        for group in (GROUP.replace("SubArrayId=1", "SubArrayId=99"),
                      GROUP.replace("SubArrayName=Array A", "SubArrayName=Wrong"),
                      GROUP.replace("  SubArrayId=1", "  SubArrayId=1\nSubArrayId=1", 1)):
            r = self.inspect(grid_text(group))
            self.assertFalse(r["valid"])
            self.assertTrue(r["issues"])
        for name in ("Start", "End"):
            self.assertTrue(self.inspect(grid_text(GROUP.replace("Array A", name),
                                                  ARRAY.replace("Array A", name)))["valid"])

    def test_shared_allocation_is_incomplete_without_misleading_totals(self):
        # A shared inverter cannot be charged in full to both subarrays.
        group = GROUP.replace("  SubArrayId=1", "  SubArrayId=0", 1)
        r = self.inspect(grid_text(group))
        self.assertIsNone(r["valid"])
        self.assertEqual(r["issues"], [])
        self.assertIsNone(r["grid_circuit"]["totals"])
        self.assertFalse(r["grid_circuit"]["counts_complete"])

    def test_unknown_node_is_unchecked_and_malformed_tree_is_invalid(self):
        r = self.inspect(grid_text(GROUP.replace("StringNode", "OptimizerNode")))
        self.assertIsNone(r["valid"])
        self.assertIn("Unsupported circuit node", " ".join(r["unchecked_checks"]))
        for source in (grid_text().replace("StringNode End;", "MPPTNode End;"),
                       grid_text().replace("Children=Start", "Children=Start\nNElements=2", 1),
                       grid_text().replace("Children=End", "Wrong=End", 1)):
            r = self.inspect(source)
            self.assertFalse(r["valid"])
            self.assertIsNone(r["grid_circuit"]["totals"])

    def test_empty_missing_and_duplicate_circuits_are_invalid(self):
        for text in (grid_text(groups=""), grid_text().replace("PVObject_SystemCircuit=pvCircuit", "PVObject_Other=pvCircuit"),
                     grid_text().replace("PVObject_SystemCircuit=pvCircuit", "PVObject_SystemCircuit=pvCircuit\nPVObject_SystemCircuit=pvCircuit")):
            r = self.inspect(text)
            self.assertFalse(r["valid"])
            self.assertTrue(r["issues"])


if __name__ == "__main__":
    unittest.main()

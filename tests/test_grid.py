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
        for flags in ("$001E0002", "$001E0402"):
            arrays = ARRAY.replace("NInvMPPT=1", "NInvMPPT=1\nFlags=" + flags)
            text = grid_text(group, arrays).replace("End of PVObject pvVCalcul",
                "PVObject_Ombrage=pvShading\nFlags=$00080000\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
            before = self.inspect(text)
            original = self.path.read_bytes()
            self.assertEqual(before["grid_circuit"]["subarrays"][0]["declared"]["Flags"], flags)
            with self.assertRaisesRegex(ValueError, "MPPT"):
                self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"], clone_orientation=True)
            self.assertEqual(self.path.read_bytes(), original)
            copied = self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"])
            checked = self.store.validate_structure("Example.PRJ", "VC0")
            self.assertTrue(checked["valid"])
            self.assertEqual(checked["grid_circuit"]["totals"], {"inverters": 4, "strings": 12, "explicit_mppt_inputs": 12})
            self.assertEqual(self.path.read_bytes().count(b"NInvMPPT=1"), 2)
            self.assertTrue(all(row["declared"]["Flags"] == flags for row in checked["grid_circuit"]["subarrays"]))
            self.store.remove_subarray("Example.PRJ", "VC0", copied["subarray_id"], copied["sha256"])
            self.assertEqual(self.path.read_bytes(), original)

        for version, flags, inputs, valid, match in (
                ("8.1.0", "$001E0402", 6, True, True),
                ("8.1.0", "$001E0402", 1, False, False),
                ("8.1.0", "$001E0002", 1, True, None),
                ("8.1.6", "$001E0402", 6, True, True),
                ("8.1.6", "$001E0402", 1, False, False),
                ("8.1.6", "$001E0002", 1, True, None),
                ("8.1.5", "$001E0402", 1, True, None)):
            arrays = ARRAY.replace("NInvMPPT=1", f"Version={version}\nFlags={flags}\nNInvMPPT={inputs}")
            checked = self.inspect(grid_text(group, arrays))
            self.assertEqual(checked["valid"], valid)
            row = checked["grid_circuit"]["subarrays"][0]
            self.assertIs(row["explicit_mppt_inputs_match_declared"], match)
            if match is False:
                text = grid_text(group, arrays).replace("End of PVObject pvVCalcul",
                    "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
                before = self.inspect(text)
                original = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "Independent MPPT layout"):
                    self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"])
                self.assertEqual(self.path.read_bytes(), original)
        arrays = ARRAY.replace("NInvMPPT=1", "Version=8.1.0\nFlags=$001E0402\nNInvMPPT=1")
        checked = self.inspect(grid_text(arrays=arrays))
        self.assertTrue(checked["valid"])
        self.assertEqual(checked["grid_circuit"]["subarrays"][0]["independent_mppt_inputs_declared"], 1)
        self.assertIsNone(checked["grid_circuit"]["subarrays"][0]["explicit_mppt_inputs_match_declared"])
        arrays = ARRAY.replace("NInvMPPT=1", "Version=8.1.0\nFlags=$001E0402\nPowerSharingInverterId=-1\nNInvMPPT=6")
        text = grid_text(group, arrays).replace("End of PVObject pvVCalcul",
            "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
        for version in ("8.1.0", "8.1.6"):
            before = self.inspect(text.replace("Version=8.1.0", "Version=" + version))
            original = self.path.read_bytes()
            preview = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NInvMPPT": 12}, dry_run=True)
            self.assertEqual(preview["capacity"]["mppt_inputs_after"], 12)
            self.assertEqual(self.path.read_bytes(), original)
            changed = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NInvMPPT": 12})
            self.assertEqual(changed["sha256"], preview["sha256"])
            checked = self.store.validate_structure("Example.PRJ", "VC0")
            self.assertTrue(checked["valid"])
            self.assertEqual(checked["grid_circuit"]["totals"], {"inverters": 2, "strings": 12, "explicit_mppt_inputs": 12})
            self.store.restore("Example.PRJ", "VC0", changed["backup_name"], changed["sha256"], True)
            self.assertEqual(self.path.read_bytes(), original)
            preview = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NInverter": 4}, dry_run=True)
            self.assertEqual(self.path.read_bytes(), original)
            changed = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NInverter": 4})
            self.assertEqual(changed["sha256"], preview["sha256"])
            self.assertEqual(changed["capacity"]["mppt_inputs_after"], 12)
            self.assertEqual(changed["capacity"]["inverters_after"], 4)
            self.assertTrue(all(row["before"] == row["after"] == 3 for row in changed["capacity"]["mppt_groups"]))
            self.assertEqual(self.path.read_bytes(), original.replace(b"NInverter=2", b"NInverter=4").replace(b"NInvMPPT=6", b"NInvMPPT=12").replace(b"NStringCh=6", b"NStringCh=12").replace(b"NStrOrient1=6", b"NStrOrient1=12").replace(b"NElements=2", b"NElements=4"))
            self.store.restore("Example.PRJ", "VC0", changed["backup_name"], changed["sha256"], True)
            self.assertEqual(self.path.read_bytes(), original)
        for updates in ({"NInvMPPT": 1}, {"NInvMPPT": 6.5}, {"NInvMPPT": 12, "NStringCh": 12},
                        {"NInverter": 4, "NStringCh": 12}, {"NInverter": 4, "NInvMPPT": 12}):
            with self.assertRaises(ValueError):
                self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, updates)
            self.assertEqual(self.path.read_bytes(), original)
        grouped = grid_text(group + group, arrays.replace("NInverter=2", "NInverter=4").replace("NInvMPPT=6", "NInvMPPT=12").replace("NStringCh=6", "NStringCh=12").replace("NStrOrient1=6", "NStrOrient1=12")).replace("End of PVObject pvVCalcul", "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
        grouped_report = self.inspect(grouped)
        grouped_bytes = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "integral string/group"):
            self.store.update_parameters("Example.PRJ", "VC0", grouped_report["sha256"], 1, 1, {"NInverter": 3})
        self.assertEqual(self.path.read_bytes(), grouped_bytes)
        for altered in (text.replace("$001E0402", "$001E0002"), text.replace("Version=8.1.0", "Version=8.1.5"),
                        text.replace("PowerSharingInverterId=-1", "PowerSharingInverterId=0"),
                        text.replace("PowerSharingInverterId=-1\n", "")):
            before = self.inspect(altered)
            original = self.path.read_bytes()
            with self.assertRaises(ValueError):
                self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NInvMPPT": 12})
            self.assertEqual(self.path.read_bytes(), original)

        explicit = text.replace("    StringNode Start;", "    StringNode Start;\nNElements=1")
        for version in ("8.1.0", "8.1.6"):
            before = self.inspect(explicit.replace("Version=8.1.0", "Version=" + version))
            original = self.path.read_bytes()
            preview = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NStringCh": 12}, dry_run=True)
            self.assertEqual(preview["capacity"]["mppt_inputs_before"], preview["capacity"]["mppt_inputs_after"])
            self.assertEqual(self.path.read_bytes(), original)
            edited = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NStringCh": 12})
            self.assertEqual(edited["sha256"], preview["sha256"])
            result = self.store.validate_structure("Example.PRJ", "VC0")
            self.assertTrue(result["valid"])
            self.assertEqual(result["grid_circuit"]["totals"], {"inverters": 2, "strings": 12, "explicit_mppt_inputs": 6})
            self.store.restore("Example.PRJ", "VC0", edited["backup_name"], edited["sha256"], True)
            self.assertEqual(self.path.read_bytes(), original)
            with self.assertRaisesRegex(ValueError, "integral"):
                self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NStringCh": 9})
            self.assertEqual(self.path.read_bytes(), original)
        for candidate, reason in (
                (explicit.replace("Version=8.1.0", "Version=8.1.5"), "verified Version=8.1.0/8.1.6"),
                (text.replace("Version=8.1.0", "Version=8.1.6"), "explicit StringNode NElements")):
            before = self.inspect(candidate)
            original = self.path.read_bytes()
            with self.assertRaisesRegex(ValueError, reason):
                self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NStringCh": 12})
            self.assertEqual(self.path.read_bytes(), original)
        planes = text.replace("PVObject_=pvOrient", "Orientations, list of=1 TOrientGroup\nPVObject_=pvOrient").replace(
            "End of TOrientGroup\n", "End of TOrientGroup\nEnd of Orientations\n").replace("FieldType=FixedPlane", "Flags=$00\nFieldType=FixedPlane")
        for version in ("8.1.0", "8.1.6"):
            version_planes = planes.replace("Version=8.1.0", "Version=" + version)
            before = self.inspect(version_planes)
            original = self.path.read_bytes()
            copied_plane = self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"], clone_orientation=True)
            result = self.store.validate_structure("Example.PRJ", "VC0")
            self.assertTrue(result["valid"])
            self.assertEqual(result["orientation_ids"], [1, 2])
            self.assertEqual(result["grid_circuit"]["totals"], {"inverters": 4, "strings": 12, "explicit_mppt_inputs": 12})
            self.assertTrue(all(row["declared"]["Flags"] == "$001E0402" and row["declared"]["NInvMPPT"] == "6" for row in result["grid_circuit"]["subarrays"]))
            two_planes = self.path.read_bytes()
            with self.assertRaises(ValueError):
                self.store.update_parameters("Example.PRJ", "VC0", copied_plane["sha256"], 2, 2, {"NoOrientation": 1})
            self.assertEqual(self.path.read_bytes(), two_planes)
            two_text = two_planes.decode("utf-8-sig").replace("\r\n", "\n")
            for altered, reason in (
                    (two_text.replace("NoOrient=2\n", "NoOrient=2\nUsesTiltAver=True\n"), "Averaged orientations"),
                    (two_text.replace("SubArrayName=Array A\n", "SubArrayName=Array A\nOrientationId=2\n", 1), "outside owned records"),
                    (two_text.replace("Version=" + version, "Version=8.1.5"), "verified exclusive explicit MPPT")):
                guard = self.inspect(altered)
                guard_bytes = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, reason):
                    self.store.remove_subarray("Example.PRJ", "VC0", 2, guard["sha256"], remove_orientation=True)
                self.assertEqual(self.path.read_bytes(), guard_bytes)
                if reason == "outside owned records":
                    with self.assertRaisesRegex(ValueError, reason):
                        self.store.clone_subarray("Example.PRJ", "VC0", 1, guard["sha256"], clone_orientation=True)
                    self.assertEqual(self.path.read_bytes(), guard_bytes)
            self.inspect(two_text)
            third = self.store.clone_subarray("Example.PRJ", "VC0", 1, copied_plane["sha256"])
            prior = self.path.read_bytes()
            preview = self.store.update_parameters("Example.PRJ", "VC0", third["sha256"], third["subarray_id"], 1, {"NoOrientation": 2}, dry_run=True)
            self.assertEqual(self.path.read_bytes(), prior)
            linked = self.store.update_parameters("Example.PRJ", "VC0", third["sha256"], third["subarray_id"], 1, {"NoOrientation": 2})
            self.assertEqual(linked["sha256"], preview["sha256"])
            self.assertEqual([(a, b) for a, b in zip(prior.splitlines(), self.path.read_bytes().splitlines()) if a != b], [(b"  NoOrientation=1", b"  NoOrientation=2")])
            refs = self.store.inspect_parameters("Example.PRJ", "VC0")["subarrays"]
            self.assertEqual([a["values"]["NoOrientation"] for a in refs], ["1", "2", "2"])
            self.store.restore("Example.PRJ", "VC0", linked["backup_name"], linked["sha256"], True)
            deleted = self.store.remove_subarray("Example.PRJ", "VC0", third["subarray_id"], third["sha256"])
            self.assertEqual(self.path.read_bytes(), two_planes)
            self.store.remove_subarray("Example.PRJ", "VC0", copied_plane["subarray_id"], deleted["sha256"], remove_orientation=True)
            self.assertEqual(self.path.read_bytes(), original)
            for removed_id in (1, 2):
                before = self.inspect(version_planes)
                baseline = self.path.read_bytes()
                second_plane = self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"], clone_orientation=True)
                third_plane = self.store.clone_subarray("Example.PRJ", "VC0", 1, second_plane["sha256"], clone_orientation=True)
                tilted = self.store.update_parameters("Example.PRJ", "VC0", third_plane["sha256"], 1, 2, orientation_updates={"FieldTilt": 35})
                tilted = self.store.update_parameters("Example.PRJ", "VC0", tilted["sha256"], 1, 3, orientation_updates={"FieldTilt": 60})
                three = self.path.read_bytes()
                removed = self.store.remove_subarray("Example.PRJ", "VC0", removed_id, tilted["sha256"], remove_orientation=True)
                self.assertEqual(removed["orientation_id_map"], {"2": 1, "3": 2} if removed_id == 1 else {"3": 2})
                checked = self.store.validate_structure("Example.PRJ", "VC0")
                self.assertTrue(checked["valid"])
                self.assertEqual(checked["orientation_ids"], [1, 2])
                self.assertEqual(checked["grid_circuit"]["totals"], {"inverters": 4, "strings": 12, "explicit_mppt_inputs": 12})
                params = self.store.inspect_parameters("Example.PRJ", "VC0")
                self.assertEqual([a["id"] for a in params["subarrays"]], [2, 3] if removed_id == 1 else [1, 3])
                self.assertEqual([a["values"]["NoOrientation"] for a in params["subarrays"]], ["1", "2"])
                self.assertEqual(float(params["orientations"][1]["values"]["FieldTilt"]), 60)
                self.store.restore("Example.PRJ", "VC0", removed["backup_name"], removed["sha256"], True)
                self.assertEqual(self.path.read_bytes(), three)
                altered = three.decode("utf-8-sig").replace("\r\n", "\n").replace("SubArrayName=Array A\n", "SubArrayName=Array A\nOrientationId=1\n", 1)
                guard = self.inspect(altered)
                guard_bytes = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "outside owned records"):
                    self.store.remove_subarray("Example.PRJ", "VC0", removed_id, guard["sha256"], remove_orientation=True)
                self.assertEqual(self.path.read_bytes(), guard_bytes)
                self.store.restore("Example.PRJ", "VC0", second_plane["backup_name"], guard["sha256"], True)
                self.assertEqual(self.path.read_bytes(), baseline)
        whole = text.replace("$001E0402", "$001E0002").replace("NInvMPPT=6", "NInvMPPT=1")
        for version in ("8.1.0", "8.1.6"):
            before = self.inspect(whole.replace("Version=8.1.0", "Version=" + version))
            original = self.path.read_bytes()
            preview = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, independent_mppt_enabled=True, dry_run=True)
            self.assertEqual(preview["mppt_mode"]["inputs_after"], 6)
            self.assertEqual(self.path.read_bytes(), original)
            enabled = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, independent_mppt_enabled=True)
            self.assertEqual(enabled["sha256"], preview["sha256"])
            self.assertEqual(self.path.read_bytes(), original.replace(b"$001E0002", b"$001E0402").replace(b"NInvMPPT=1", b"NInvMPPT=6"))
            noop = self.store.update_parameters("Example.PRJ", "VC0", enabled["sha256"], 1, 1, independent_mppt_enabled=True)
            self.assertFalse(noop["changed"])
            self.assertNotIn("backup_name", noop)
            disabled = self.store.update_parameters("Example.PRJ", "VC0", enabled["sha256"], 1, 1, independent_mppt_enabled=False)
            self.assertEqual(self.path.read_bytes(), original)
            self.store.restore("Example.PRJ", "VC0", enabled["backup_name"], disabled["sha256"], True)
        for options in ({"independent_mppt_enabled": 1}, {"independent_mppt_enabled": True, "power_sharing_enabled": True}):
            with self.assertRaises(ValueError):
                self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, **options)
            self.assertEqual(self.path.read_bytes(), original)

        before = self.inspect(whole.replace("Version=8.1.0", "Version=8.1.5"))
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "verified Version=8.1.0/8.1.6"):
            self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, independent_mppt_enabled=True)
        self.assertEqual(self.path.read_bytes(), original)

        mixed_arrays = ARRAY.replace("NInverter=2", "NInverter=4").replace("NStringCh=6", "NStringCh=12").replace("NStrOrient1=6", "NStrOrient1=12").replace("NInvMPPT=1", "NInvMPPT=1\nVersion=8.1.0\nFlags=$001E0002\nPowerSharingInverterId=-1")
        mixed = grid_text(group + GROUP, mixed_arrays).replace("End of PVObject pvVCalcul",
            "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
        before = self.inspect(mixed)
        self.assertTrue(before["valid"])
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "direct explicit MPPT children"):
            self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, independent_mppt_enabled=True)
        self.assertEqual(self.path.read_bytes(), original)

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

    def test_shared_allocation_counts_only_verified_layouts(self):
        # A shared inverter cannot be charged in full to both subarrays.
        group = GROUP.replace("  SubArrayId=1", "  SubArrayId=0", 1)
        r = self.inspect(grid_text(group))
        self.assertIsNone(r["valid"])
        self.assertEqual(r["issues"], [])
        self.assertIsNone(r["grid_circuit"]["totals"])
        self.assertFalse(r["grid_circuit"]["counts_complete"])
        for version in ("8.1.0", "8.1.6"):
            leaf = GROUP[GROUP.index("    StringNode Start;"):GROUP.index("  Children=End")]
            port = "MPPTNode Start;\nSubArrayId=1\nSubArrayName=Array A\nChildren=Start\n" + leaf + "Children=End\nMPPTNode End;\n"
            shared = ("InverterNode Start;\nNElements=2\nSubArrayName=Array A; Array B\nChildren=Start\n" + port
                      + port.replace("SubArrayId=1", "SubArrayId=2").replace("Array A", "Array B") + "Children=End\nInverterNode End;\n")
            first = ARRAY.replace("NInvMPPT=1", "NInvMPPT=2\nVersion=" + version + "\nFlags=$001E0402\nGInverter=Shared.OND\nPowerSharingInverterId=0")
            second = first.replace("SubArrayId=1", "SubArrayId=2").replace("Array A", "Array B")
            for group_id in ("0", "-1"):
                r = self.inspect(grid_text(shared, (first + second).replace("PowerSharingInverterId=0", "PowerSharingInverterId=" + group_id)))
                self.assertTrue(r["valid"])
                self.assertEqual(r["grid_circuit"]["totals"], {"inverters": 2, "strings": 12, "explicit_mppt_inputs": 4})
                self.assertEqual(r["grid_circuit"]["inverter_groups"][0]["subarray_ids"], [1, 2])
                self.assertTrue(all(row["explicit_mppt_inputs_match_declared"] is True and row["shared_inverter_lines"]
                                    and row["circuit"]["inverters"] == 2 for row in r["grid_circuit"]["subarrays"]))
            for group_id in ("0", "-1"):
                member_text = grid_text(shared, (first + second).replace("PowerSharingInverterId=0", "PowerSharingInverterId=" + group_id)).replace(
                    "End of PVObject pvVCalcul", "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
                before = self.inspect(member_text)
                original = self.path.read_bytes()
                preview = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NStringCh": 12}, dry_run=True)
                self.assertIn("shared_parent_line", preview["capacity"])
                self.assertEqual(self.path.read_bytes(), original)
                edited = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NStringCh": 12})
                after = self.store.validate_structure("Example.PRJ", "VC0")
                self.assertTrue(after["valid"])
                self.assertEqual(after["grid_circuit"]["totals"], {"inverters": 2, "strings": 18, "explicit_mppt_inputs": 4})
                self.assertEqual(after["grid_circuit"]["subarrays"][1], before["grid_circuit"]["subarrays"][1])
                self.store.restore("Example.PRJ", "VC0", edited["backup_name"], edited["sha256"], True)
                self.assertEqual(self.path.read_bytes(), original)
                for updates in ({"NStringCh": 7}, {"NInvMPPT": 4}, {"NInverter": 4}):
                    with self.assertRaises(ValueError):
                        self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, updates)
                    self.assertEqual(self.path.read_bytes(), original)
            solo_array = first.replace("NInverter=2", "NInverter=1").replace("NInvMPPT=2", "NInvMPPT=1").replace("NStringCh=6", "NStringCh=3").replace("NStrOrient1=6", "NStrOrient1=3").replace("PowerSharingInverterId=0", "PowerSharingInverterId=-1")
            solo_group = "InverterNode Start;\nNElements=1\nSubArrayId=1\nSubArrayName=Array A\nChildren=Start\n" + port + "Children=End\nInverterNode End;\n"
            solo = grid_text(solo_group, solo_array).replace("End of PVObject pvVCalcul",
                "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
            before = self.inspect(solo)
            original = self.path.read_bytes()
            copied = self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"], share_inverter=True)
            self.assertTrue(copied["shared_parent_created"])
            after = self.store.validate_structure("Example.PRJ", "VC0")
            self.assertTrue(after["valid"])
            self.assertEqual(after["grid_circuit"]["totals"], {"inverters": 1, "strings": 6, "explicit_mppt_inputs": 2})
            self.assertTrue(all(row["declared"]["PowerSharingInverterId"] == "-1" for row in after["grid_circuit"]["subarrays"]))
            parent_bytes = self.path.read_bytes()
            removed = self.store.remove_subarray("Example.PRJ", "VC0", 2, copied["sha256"], shared_member=True)
            self.assertEqual(removed["remaining_member_id"], 1)
            self.assertEqual(self.path.read_bytes(), original)
            self.store.restore("Example.PRJ", "VC0", removed["backup_name"], removed["sha256"], True)
            self.assertEqual(self.path.read_bytes(), parent_bytes)
            for options in ({"shared_member": 1}, {"shared_member": True, "remove_orientation": True}):
                with self.assertRaises(ValueError):
                    self.store.remove_subarray("Example.PRJ", "VC0", 2, copied["sha256"], **options)
                self.assertEqual(self.path.read_bytes(), parent_bytes)
            enabled = self.store.update_parameters("Example.PRJ", "VC0", copied["sha256"], 1, 1, power_sharing_enabled=True)
            with self.assertRaises(ValueError):
                self.store.remove_subarray("Example.PRJ", "VC0", 2, enabled["sha256"], shared_member=True)
            active_bytes = self.path.read_bytes()
            disabled = self.store.update_parameters("Example.PRJ", "VC0", enabled["sha256"], 1, 1, power_sharing_enabled=False)
            disabled_bytes = self.path.read_bytes()
            deleted = self.store.remove_subarray("Example.PRJ", "VC0", 2, disabled["sha256"], shared_member=True)
            self.assertEqual(self.path.read_bytes(), original)
            restored = self.store.restore("Example.PRJ", "VC0", deleted["backup_name"], deleted["sha256"], True)
            self.assertEqual(self.path.read_bytes(), disabled_bytes)
            self.store.restore("Example.PRJ", "VC0", disabled["backup_name"], restored["sha256"], True)
            self.assertEqual(self.path.read_bytes(), active_bytes)
            self.store.restore("Example.PRJ", "VC0", enabled["backup_name"], enabled["sha256"], True)
            first_removed = self.store.remove_subarray("Example.PRJ", "VC0", 1, copied["sha256"], shared_member=True)
            result = self.store.validate_structure("Example.PRJ", "VC0")
            self.assertTrue(result["valid"])
            self.assertEqual(result["subarray_ids"], [2])
            self.assertEqual(result["grid_circuit"]["totals"], {"inverters": 1, "strings": 3, "explicit_mppt_inputs": 1})
            self.store.restore("Example.PRJ", "VC0", copied["backup_name"], first_removed["sha256"], True)
            self.assertEqual(self.path.read_bytes(), original)
            for options in ({"share_inverter": 1}, {"share_inverter": True, "clone_orientation": True}):
                with self.assertRaises(ValueError):
                    self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"], **options)
                self.assertEqual(self.path.read_bytes(), original)
            for altered in (solo.replace("$001E0402", "$001E0002"), solo.replace("NInvMPPT=1", "NInvMPPT=2"),
                            solo.replace("NInverter=1", "NInverter=2"), solo.replace("Version=" + version, "Version=8.1.5")):
                before = self.inspect(altered)
                original = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"], share_inverter=True)
                self.assertEqual(self.path.read_bytes(), original)
            disabled = (first + second).replace("PowerSharingInverterId=0", "PowerSharingInverterId=-1")
            text = grid_text(shared, disabled).replace("End of PVObject pvVCalcul",
                "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
            before = self.inspect(text)
            original = self.path.read_bytes()
            preview = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, power_sharing_enabled=True, dry_run=True)
            self.assertEqual(preview["power_sharing"]["subarray_ids"], [1, 2])
            self.assertEqual(self.path.read_bytes(), original)
            enabled = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, power_sharing_enabled=True)
            self.assertEqual(self.path.read_bytes(), original.replace(b"$001E0402", b"$001E0502").replace(b"PowerSharingInverterId=-1", b"PowerSharingInverterId=0"))
            unchanged = self.store.update_parameters("Example.PRJ", "VC0", enabled["sha256"], 2, 1, power_sharing_enabled=True)
            self.assertFalse(unchanged["changed"])
            self.assertNotIn("backup_name", unchanged)
            self.store.update_parameters("Example.PRJ", "VC0", enabled["sha256"], 2, 1, power_sharing_enabled=False)
            self.assertEqual(self.path.read_bytes(), original)
            self.store.restore("Example.PRJ", "VC0", enabled["backup_name"], before["sha256"], True)
            self.assertEqual(self.path.read_bytes(), original)
            for value in (1, "false"):
                with self.assertRaises(ValueError):
                    self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, power_sharing_enabled=value)
            with self.assertRaises(ValueError):
                self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NInvMPPT": 4}, power_sharing_enabled=True)
            self.assertEqual(self.path.read_bytes(), original)
            third = ARRAY.replace("SubArrayId=1", "SubArrayId=3").replace("Array A", "Array C").replace("NInvMPPT=1", "NInvMPPT=1\nPowerSharingInverterId=0")
            third_group = GROUP.replace("SubArrayId=1", "SubArrayId=3").replace("Array A", "Array C")
            collisions = grid_text(shared + third_group, disabled + third).replace("End of PVObject pvVCalcul",
                "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
            for altered in (collisions, text.replace("Comment=Array B\n", "Comment=Array B\nNStrOrient2=1\n"),
                            text.replace("SubArrayId=2\n  NoOrientation=1", "SubArrayId=2\n  NoOrientation=2")):
                before = self.inspect(altered)
                original = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, power_sharing_enabled=True)
                self.assertEqual(self.path.read_bytes(), original)
            for altered in (collisions.replace("PowerSharingInverterId=-1", "PowerSharingInverterId=0"),
                            text.replace("Comment=Array B\n", "Comment=Array B\nNStrOrient2=1\n"),
                            text.replace("SubArrayId=2\n  NoOrientation=1", "SubArrayId=2\n  NoOrientation=2")):
                before = self.inspect(altered)
                original = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NStringCh": 12})
                self.assertEqual(self.path.read_bytes(), original)
            for altered in (second.replace("NInvMPPT=2", "NInvMPPT=1"), second.replace("NStringCh=6", "NStringCh=5")):
                self.assertFalse(self.inspect(grid_text(shared, first + altered))["valid"])
            r = self.inspect(grid_text(shared + shared, first + second))
            self.assertIsNone(r["valid"])
            self.assertIsNone(r["grid_circuit"]["totals"])
            for altered in (second.replace("Shared.OND", "Other.OND"), second.replace("PowerSharingInverterId=0", "PowerSharingInverterId=1"),
                            second.replace("Version=" + version, "Version=" + ("8.1.6" if version == "8.1.0" else "8.1.0"))):
                r = self.inspect(grid_text(shared, first + altered))
                self.assertIsNone(r["valid"])
                self.assertIsNone(r["grid_circuit"]["totals"])
            r = self.inspect(grid_text(shared, (first + second).replace("Version=" + version, "Version=8.1.5")))
            self.assertIsNone(r["valid"])
            self.assertIsNone(r["grid_circuit"]["totals"])
            r = self.inspect(grid_text(shared.replace("SubArrayName=Array A; Array B", "SubArrayName=Wrong"), first + second))
            self.assertFalse(r["valid"])
            self.assertIsNone(r["grid_circuit"]["totals"])

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

    def test_capacity_resize_preserves_uneven_groups_and_syncs_strings(self):
        groups = GROUP + GROUP.replace("NElements=2", "NElements=4").replace("NElements=3", "NElements=5")
        groups = groups.replace("      SubArrayId=1", "      CableDefinition=Start\n        NElements=99\n        SubArrayName=metadata\n      CableDefinition=End\n      SubArrayId=1")
        arrays = ARRAY.replace("NInverter=2", "NInverter=6").replace("NStringCh=6", "NStringCh=26").replace("NStrOrient1=6", "NStrOrient1=26")
        text = grid_text(groups, arrays).replace("End of PVObject pvVCalcul",
            "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
        for updates, inverters, strings, parallel in (
                ({"NInverter": 12}, [4, 8], 52, [3, 5]),
                ({"NStringCh": 52}, [2, 4], 52, [6, 10]),
                ({"NInverter": 12, "NStringCh": 104}, [4, 8], 104, [6, 10])):
            with self.subTest(updates=updates):
                before = self.inspect(text)
                preview = self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1,
                                                       updates, dry_run=True)
                self.assertEqual(preview["capacity"]["strings_after"], strings)
                self.assertEqual(self.store.validate_structure("Example.PRJ", "VC0")["grid_circuit"]["totals"]["inverters"], 6)
                self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, updates)
                result = self.store.validate_structure("Example.PRJ", "VC0")
                self.assertTrue(result["valid"])
                self.assertEqual(result["grid_circuit"]["totals"]["strings"], strings)
                self.assertEqual(result["grid_circuit"]["subarrays"][0]["declared"]["NStrOrient1"], str(strings))
                self.assertEqual([g["inverters"] for g in result["grid_circuit"]["inverter_groups"]], inverters)
                self.assertEqual([g["strings"] for g in result["grid_circuit"]["inverter_groups"]],
                                 [count * load for count, load in zip(inverters, parallel)])

        before = self.inspect(text)
        original = self.path.read_bytes()
        copied = self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"])
        checked = self.store.validate_structure("Example.PRJ", "VC0")
        self.assertTrue(checked["valid"])
        self.assertEqual(checked["subarray_ids"], [1, 2])
        groups = checked["grid_circuit"]["inverter_groups"]
        for identifier in (1, 2):
            self.assertEqual([(g["inverters"], g["strings"]) for g in groups if g["subarray_id"] == identifier],
                             [(2, 6), (4, 20)])
        self.assertEqual(self.path.read_bytes().count(b"SubArrayName=metadata"), 4)
        with self.assertRaisesRegex(ValueError, "still referenced"):
            self.store.remove_subarray("Example.PRJ", "VC0", copied["subarray_id"], copied["sha256"], remove_orientation=True)
        self.store.remove_subarray("Example.PRJ", "VC0", copied["subarray_id"], copied["sha256"])
        self.assertEqual(self.path.read_bytes(), original)

        source = text.replace("PVObject_=pvOrient", "Orientations, list of=1 TOrientGroup\nPVObject_=pvOrient").replace(
            "End of TOrientGroup\n", "End of TOrientGroup\nEnd of Orientations\n").replace(
            "FieldType=FixedPlane", "Flags=$00\nFieldType=FixedPlane")
        before = self.inspect(source)
        original = self.path.read_bytes()
        copied = self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"], clone_orientation=True)
        self.assertEqual(copied["orientation_id"], 2)
        self.assertEqual(self.store.validate_structure("Example.PRJ", "VC0")["orientation_ids"], [1, 2])
        changed = self.store.update_parameters("Example.PRJ", "VC0", copied["sha256"], 2, 2,
                                               orientation_updates={"FieldTilt": 60})
        parameters = self.store.inspect_parameters("Example.PRJ", "VC0")
        self.assertEqual([o["values"]["FieldTilt"] for o in parameters["orientations"]], ["20", "60.000"])
        self.assertEqual([a["values"]["NoOrientation"] for a in parameters["subarrays"]], ["1", "2"])
        with self.assertRaisesRegex(ValueError, "must remain in use"):
            self.store.remove_subarray("Example.PRJ", "VC0", 2, changed["sha256"])
        two_arrays = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "no assigned subarray"):
            self.store.update_parameters("Example.PRJ", "VC0", changed["sha256"], 2, 1, {"NoOrientation": 1})
        self.assertEqual(self.path.read_bytes(), two_arrays)
        third = self.store.clone_subarray("Example.PRJ", "VC0", 1, changed["sha256"])
        before_assignment = self.path.read_bytes()
        preview = self.store.update_parameters("Example.PRJ", "VC0", third["sha256"], 3, 1,
            {"NoOrientation": 2, "NStringCh": 52}, dry_run=True)
        self.assertEqual(preview["orientation_assignment"], {"subarray_id": 3, "before": 1, "after": 2})
        self.assertEqual(preview["capacity"]["strings_after"], 52)
        self.assertEqual(self.path.read_bytes(), before_assignment)
        reassigned = self.store.update_parameters("Example.PRJ", "VC0", third["sha256"], 3, 1, {"NoOrientation": 2})
        self.assertEqual(self.path.read_bytes(), before_assignment.replace(
            b"SubArrayId=3\r\n  NoOrientation=1", b"SubArrayId=3\r\n  NoOrientation=2"))
        checked = self.store.validate_structure("Example.PRJ", "VC0")
        self.assertTrue(checked["valid"])
        self.assertEqual(checked["orientation_ids"], [1, 2])
        noop = self.store.update_parameters("Example.PRJ", "VC0", reassigned["sha256"], 3, 1, {"NoOrientation": 2})
        self.assertFalse(noop["changed"])
        self.assertNotIn("backup_name", noop)
        self.store.restore("Example.PRJ", "VC0", reassigned["backup_name"], reassigned["sha256"], True)
        self.assertEqual(self.path.read_bytes(), before_assignment)
        removed = self.store.remove_subarray("Example.PRJ", "VC0", 3, third["sha256"])
        self.assertEqual(self.path.read_bytes(), two_arrays)
        third_plane = self.store.clone_subarray("Example.PRJ", "VC0", 1, removed["sha256"], clone_orientation=True)
        third_tilt = self.store.update_parameters("Example.PRJ", "VC0", third_plane["sha256"], 3, 3,
            orientation_updates={"FieldTilt": 35})
        before_delete = self.path.read_bytes()
        outside = before_delete.decode("utf-8-sig").replace(
            "End of PVObject pvMainArray", "NoOrientation=3\r\nEnd of PVObject pvMainArray")
        checked = self.inspect(outside.replace("\r\n", "\n"))
        external_bytes = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "outside owned records"):
            self.store.remove_subarray("Example.PRJ", "VC0", 2, checked["sha256"], remove_orientation=True)
        self.assertEqual(self.path.read_bytes(), external_bytes)
        self.path.write_bytes(before_delete)
        for selected, mapping, tilts in ((2, {"3": 2}, ["20", "35.000"]),
                                         (1, {"2": 1, "3": 2}, ["60.000", "35.000"])):
            deleted = self.store.remove_subarray("Example.PRJ", "VC0", selected, third_tilt["sha256"], remove_orientation=True)
            self.assertEqual(deleted["orientation_id_map"], mapping)
            checked = self.store.validate_structure("Example.PRJ", "VC0")
            self.assertTrue(checked["valid"])
            self.assertEqual(checked["orientation_ids"], [1, 2])
            parameters = self.store.inspect_parameters("Example.PRJ", "VC0")
            self.assertEqual([o["values"]["FieldTilt"] for o in parameters["orientations"]], tilts)
            self.assertEqual([a["values"]["NoOrientation"] for a in parameters["subarrays"]], ["1", "2"])
            self.store.restore("Example.PRJ", "VC0", deleted["backup_name"], deleted["sha256"], True)
            self.assertEqual(self.path.read_bytes(), before_delete)
        deleted = self.store.remove_subarray("Example.PRJ", "VC0", 3, third_tilt["sha256"], remove_orientation=True)
        self.assertEqual(self.path.read_bytes(), two_arrays)
        self.store.remove_subarray("Example.PRJ", "VC0", 2, deleted["sha256"], remove_orientation=True)
        self.assertEqual(self.path.read_bytes(), original)
        wrong = source.replace("list of=1", "list of=2")
        checked = self.inspect(wrong)
        self.assertFalse(checked["valid"])
        with self.assertRaisesRegex(ValueError, "list count differs"):
            self.store.clone_subarray("Example.PRJ", "VC0", 1, checked["sha256"], clone_orientation=True)
        self.assertEqual(self.path.read_bytes(), wrong.replace("\n", "\r\n").encode("utf-8-sig"))

    def test_capacity_resize_stops_at_fractional_allocation_mppt_and_shading(self):
        base = grid_text().replace("End of PVObject pvVCalcul",
            "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
        layout_block = "PVObject_ModuleLayout=pvModuleLayout\nFlags=$0B10\nModLayoutGlobState=mlDefInverters\nEnd of PVObject pvModuleLayout\n"
        layout = base.replace("End of PVObject pvVCalcul", layout_block + "End of PVObject pvVCalcul")
        mppt = GROUP.replace("    StringNode Start;",  "MPPTNode Start;\nSubArrayId=1\nSubArrayName=Array A\nChildren=Start\n    StringNode Start;").replace(
            "    StringNode End;", "    StringNode End;\nChildren=End\nMPPTNode End;")
        fractional = base.replace(GROUP, GROUP + GROUP.replace("NElements=2", "NElements=1"))
        fractional = fractional.replace("NInverter=2", "NInverter=3").replace("NStringCh=6", "NStringCh=9").replace("NStrOrient1=6", "NStrOrient1=9")
        for text, updates, message in (
                (fractional, {"NInverter": 1}, "integral string/group allocation"),
                (base, {"NStringCh": 7}, "integral string/group allocation"),
                (base, {"NInverter": 4, "NStringCh": 6}, "integral string/group allocation"),
                (base.replace("  NoOrientation=1", "PVObject_Metadata=pvExtra\nNoOrientation=1\nEnd of PVObject pvExtra"),
                 {"NStringCh": 12}, "Capacity editing requires one fixed-plane orientation"),
                (base.replace("NInvMPPT=1", "NInvMPPT=4"), {"NStringCh": 12}, "ordinary NInvMPPT"),
                (base.replace("NInvMPPT=1", "NInvMPPT=1\nFlags=$001E0402"), {"NStringCh": 12}, "verified Version=8.1.0"),
                (base.replace("NInvMPPT=1", "NInvMPPT=1\nFlags=wrong"), {"NStringCh": 12}, "Unsupported subarray Flags"),
                (base.replace(GROUP, mppt), {"NStringCh": 12}, "Explicit MPPT"),
                (layout, {"NStringCh": 12}, "module layout"),
                (layout, {"NInverter": 4}, "module layout"),
                (layout, {"NModSerie": 15}, "module layout"),
                (base.replace("Flags=$00", "Flags=$20"), {"NStringCh": 12}, "Shading is active")):
            with self.subTest(message=message):
                before = self.inspect(text)
                original = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, message):
                    self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, updates)
                self.assertEqual(self.path.read_bytes(), original)

        assignment_base = base.replace("PVObject_=pvOrient", "Orientations, list of=1 TOrientGroup\nPVObject_=pvOrient").replace(
            "End of TOrientGroup\n", "End of TOrientGroup\nEnd of Orientations\n").replace(
            "FieldType=FixedPlane", "Flags=$00\nFieldType=FixedPlane")
        for text, destination, message in (
                (assignment_base, 2.5, "Invalid NoOrientation"),
                (assignment_base, 99, "Orientation ID must occur exactly once"),
                (assignment_base.replace("Flags=$00\nFieldType", "Flags=$20\nFieldType"), 1, "ordinary Flags"),
                (assignment_base.replace("FieldType=FixedPlane", "FieldType=FixedPlane\nUsesTiltAver=True"), 1, "Averaged"),
                (assignment_base.replace("NInvMPPT=1", "NInvMPPT=1\nPowerSharingInverterId=1"), 1, "Shared inverter"),
                (assignment_base.replace("NStrOrient1=6", "NStrOrient1=5"), 1, "all strings"),
                (assignment_base.replace("NInvMPPT=1", "NInvMPPT=1\nFlags=$001E0402"), 1, "verified exclusive explicit MPPT"),
                (assignment_base.replace(GROUP, mppt), 1, "ordinary branches without explicit MPPT"),
                (assignment_base.replace("End of PVObject pvVCalcul", layout_block + "End of PVObject pvVCalcul"), 1, "module layout"),
                (assignment_base.replace("Flags=$00", "Flags=$20"), 1, "Shading is active"),
                (assignment_base.replace("  SubArrayName=Array A", "  SubArrayName=Array A\nNoOrientation=1", 1),
                 1, "Circuit-level orientation")):
            with self.subTest(message=message):
                before = self.inspect(text)
                original = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, message):
                    self.store.update_parameters("Example.PRJ", "VC0", before["sha256"], 1, 1, {"NoOrientation": destination})
                self.assertEqual(self.path.read_bytes(), original)

        before = self.inspect(base)
        self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"])
        copied = self.path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
        blocked = self.inspect(copied.replace("End of PVObject pvVCalcul", layout_block + "End of PVObject pvVCalcul"))
        original = self.path.read_bytes()
        backups = list((self.root / "Projects" / ".mcp-variant-backups").iterdir())
        for operation in (lambda: self.store.clone_subarray("Example.PRJ", "VC0", 1, blocked["sha256"]),
                          lambda: self.store.remove_subarray("Example.PRJ", "VC0", 2, blocked["sha256"]),
                          lambda: self.store.update_parameters("Example.PRJ", "VC0", blocked["sha256"], 1, 1, independent_mppt_enabled=True)):
            with self.assertRaisesRegex(ValueError, "module layout"):
                operation()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(list((self.root / "Projects" / ".mcp-variant-backups").iterdir()), backups)

    def test_empty_missing_and_duplicate_circuits_are_invalid(self):
        for text in (grid_text(groups=""), grid_text().replace("PVObject_SystemCircuit=pvCircuit", "PVObject_Other=pvCircuit"),
                     grid_text().replace("PVObject_SystemCircuit=pvCircuit", "PVObject_SystemCircuit=pvCircuit\nPVObject_SystemCircuit=pvCircuit")):
            r = self.inspect(text)
            self.assertFalse(r["valid"])
            self.assertTrue(r["issues"])


if __name__ == "__main__":
    unittest.main()

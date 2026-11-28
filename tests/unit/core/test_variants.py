"""Offline variant cloning, scoped reference replacement and rollback tests."""
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pvsyst_auto.components.editor import ObjectBlock
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project import bifacial as seasonal
from pvsyst_auto.project.tracking import convert_orientation
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.tracking import tracking_values
from pvsyst_auto.project.variants import VariantStore
from tests._fixtures import write_utf8_crlf
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN
from tests.unit.core.test_components import PAN, OND, BTR, GEN
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT
VARIANT = """PVObject_=pvVCalcul
  Version=8.1.6
  PVObject_=pvOrient
    NoOrient=1
    FieldType=FixedPlane
    FieldTilt=20.0
    FieldAzim=0.0
  End of TOrientGroup
  PVObject_PVMainArray=pvMainArray
    SubArrays=Start
      PVObject_=pvSubArray
        SubArrayId=1
        PVModule=original.PAN
        GInverter=original.OND
        NModSerie=14
        VBkUpEncl_syst=0.250
      End of PVObject pvSubArray
      PVObject_=pvSubArray
        SubArrayId=2
        PVModule=original.PAN
        GInverter=original.OND
      End of PVObject pvSubArray
    EndTags
  End of PVObject pvMainArray
  PVObject_System=pvSystem
    Flags=$60
    SystemType=Battery
    BatteryFile=original.BTR
    GensetFile=original.GEN
  End of PVObject pvSystem
End of PVObject pvVCalcul
"""
RESULT_PLOTS = (b"  SimulPlots=Start\r\n" b"    PVObject_=pvSimulPlot\r\n      Flags=$000F\r\n      VarY=E_Grid\r\n"
                b"      BinLarg=20.0000\r\n      ShowOnReport=False\r\n      NValAcc = 0 \r\n      NValRes=0\r\n"
                b"    End of PVObject pvSimulPlot\r\n  EndTags\r\n")
RESULT_EXPORT = (b"  PVObject_Simul=pvSimulFile\r\n    FileNameWr=Output.CSV\r\n"
                 b"    VarIdentWr=viE_Grid\r\n    Units=uikWh\r\n    TypeValW=Hour\r\n  End of TSimulFich\r\n")
class VariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._seed_dir = tempfile.TemporaryDirectory()
        seed = Path(cls._seed_dir.name) / "work"
        (seed / "Projects").mkdir(parents=True)
        (seed / "Projects" / "Example.PRJ").write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        write_utf8_crlf(seed / "Projects" / "Example.VC0", VARIANT)
        for kind, folder, text in (("PAN", "PVmodules", PAN), ("OND", "Inverters", OND), ("BTR", "Batteries", BTR), ("GEN", "Gensets", GEN)):
            directory = seed / "ComposPV" / folder
            directory.mkdir(parents=True)
            for name in ("original", "new"): (directory / f"{name}.{kind}").write_text(text, encoding="utf-8")
    @classmethod
    def tearDownClass(cls):
        cls._seed_dir.cleanup()
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name) / "work"
        shutil.copytree(Path(self._seed_dir.name) / "work", self.workspace)
        self.original = self.workspace / "Projects" / "Example.VC0"
        self.components = ComponentStore(self.workspace)
        self.store = VariantStore(self.workspace, self.components)
    def test_inspect_clone_scoped_references_and_preserve_original_bytes(self):
        before = self.original.read_bytes()
        original = self.store.inspect("Example.PRJ", "VC0")
        self.assertEqual([item["id"] for item in original["subarrays"]], [1, 2])
        clone = self.store.clone("Example.PRJ", "VC0", "VC1", {"PAN": "new.PAN"}, 2)
        path = self.workspace / "Projects" / "Example.VC1"
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        self.assertEqual(raw.count(b"PVModule=new.PAN"), 1)
        self.assertEqual(raw.count(b"PVModule=original.PAN"), 1)
        self.assertEqual(self.original.read_bytes(), before)
        refs = self.store.inspect("Example.PRJ", "VC1")
        self.assertEqual(refs["sha256"], clone["sha256"])
        self.assertEqual(refs["subarrays"][0]["references"]["PAN"], ["original.PAN"])
        self.assertEqual(refs["subarrays"][1]["references"]["PAN"], ["new.PAN"])
        with self.assertRaises(FileExistsError): self.store.clone("Example.PRJ", "VC0", "VC1")
        project = self.workspace / "Projects" / "Other.PRJ"
        project.write_bytes((self.workspace / "Projects" / "Example.PRJ").read_bytes())
        imported = self.store.clone("Other.PRJ", "VC0", "VC7", source_project="Example.PRJ")
        self.assertEqual(imported["source_project"], "Example.PRJ")
        self.assertEqual((project.parent / "Other.VC7").read_bytes(), before)
        self.assertEqual(self.original.read_bytes(), before)
        self.assertEqual(project.read_bytes(), (project.parent / "Example.PRJ").read_bytes())
        with self.assertRaises(FileExistsError): self.store.clone("Other.PRJ", "VC0", "VC7", source_project="Example.PRJ")
        with self.assertRaises(FileNotFoundError): self.store.clone("Other.PRJ", "VC0", "VC8", source_project="Missing.PRJ")
        with self.assertRaises(ValueError): self.store.clone("Other.PRJ", "VC0", "VC8", source_project="../Example.PRJ")
        self.assertFalse((project.parent / "Other.VC8").exists())
    def test_component_changes_invalidate_only_owned_saved_results(self):
        # Deliberately use the old root indentation for a child, and atypical whitespace for the root.
        cold = self.original.read_bytes().replace(b"    Flags=$60", b"  Flags=$61").replace(b"GInverter=original.OND", b"GInverter = original.OND ", 1)
        self.original.write_bytes(cold)
        self.store.clone("Example.PRJ", "VC0", "VC1", {"OND": "new.OND"})
        self.assertEqual(self.original.with_suffix(".VC1").read_bytes(), cold.replace(b"GInverter = original.OND", b"GInverter = new.OND", 1))
        root = b"\tFlags = $00A0 \r\n"
        warm_plots = RESULT_PLOTS.replace(b"NValAcc = 0 \r\n      NValRes=0\r\n",
            b"NValAcc = 2 \r\n      NValRes=2\r\n" b"      ValuesX=1,2,\r\n      ValuesY_contd=3,4,\r\n"
            b"      ValuesZ = 0,1,\r\n      ValuesU_contd=9,5,\r\n"
            b"      Histogram=THistogramme\r\n        ValuesY=1,2,\r\n      End of THistogramme\r\n")
        cold = cold.replace(b"  Version=8.1.6\r\n", b"  Version=8.1.6\r\n" + root, 1).replace(
            b"End of PVObject pvVCalcul", RESULT_PLOTS + RESULT_EXPORT + b"End of PVObject pvVCalcul").replace(
            b"    SystemType=Battery\r\n", b"    SystemType=Battery\r\n    ValuesX=unrelated\r\n    NValRes=17\r\n")
        cache = (b"  VarList, list of=1 TVarSimul\r\n" b"    VarSimul_1=E_Grid\r\n      ValMonth=1.5,2.25\r\n    End of VarSimul=E_Grid\r\n"
                 b"  End of the List VarList\r\n" b"  Histogram=THistogramme\r\n    Ident=E_Grid\r\n  End of THistogramme\r\n")
        cached = cold.replace(root, root.replace(b"00A0", b"00A1")).replace(RESULT_PLOTS, warm_plots).replace(
            b"End of PVObject pvVCalcul", cache + b"End of PVObject pvVCalcul")
        self.original.write_bytes(cached)
        before = self.store.inspect_parameters("Example.PRJ", "VC0", include_saved_results=True)
        self.assertTrue(before["saved_results"]["present"])
        same = {"OND": "original.OND"}
        for target, updates in (("VC2", None), ("VC3", same)):
            self.store.clone("Example.PRJ", "VC0", target, updates)
            self.assertEqual(self.original.with_suffix(f".{target}").read_bytes(), cached)
        self.store.update("Example.PRJ", "VC0", same, before["sha256"])
        self.assertEqual(self.original.read_bytes(), cached)
        changes = {"OND": "new.OND"}
        expected = cold.replace(b"GInverter = original.OND", b"GInverter = new.OND", 1)
        self.store.clone("Example.PRJ", "VC0", "VC4", changes)
        self.assertEqual(self.original.with_suffix(".VC4").read_bytes(), expected)
        self.assertEqual(self.original.read_bytes(), cached)
        updated = self.store.update("Example.PRJ", "VC0", changes, before["sha256"])
        self.assertEqual(self.original.read_bytes(), expected)
        backup = self.original.parent / ".mcp-variant-backups" / updated["backup_name"]
        self.assertEqual(backup.read_bytes(), cached)
        after = self.store.inspect_parameters("Example.PRJ", "VC0", include_saved_results=True)
        self.assertFalse(after["saved_results"]["present"])
        self.assertEqual(after["saved_results"]["pv_simul_file"]["file_name_written"], "Output.CSV")
    def test_parameter_edit_invalidates_plot_only_saved_results(self):
        warm_plots = RESULT_PLOTS.replace(b"NValRes=0\r\n", b"NValRes=2\r\n      ValuesY=1,2,\r\n")
        source = self.original.read_bytes().replace(b"  Version=8.1.6\r\n", b"  Version=8.1.6\r\n  Flags=$00A1\r\n")
        self.original.write_bytes(source.replace(b"End of PVObject pvVCalcul", warm_plots + RESULT_EXPORT + b"End of PVObject pvVCalcul"))
        before = self.store.inspect_parameters("Example.PRJ", "VC0", include_saved_results=True)["saved_results"]
        self.assertTrue(before["present"])
        updated = self.store.update_parameters("Example.PRJ", "VC0", subarray_updates={"NModSerie": 15})
        after = self.store.inspect_parameters("Example.PRJ", "VC0", include_saved_results=True)["saved_results"]
        self.assertFalse(after["present"])
        self.assertEqual(after["root_flags"], "$00A0")
        self.assertEqual(after["pv_simul_file"], before["pv_simul_file"])
        written = self.original.read_bytes()
        self.assertIn(RESULT_PLOTS + RESULT_EXPORT, written)
        self.assertIn(b"NModSerie=15\r\n", written)
        self.assertEqual(updated["derived_cache"]["plot_sample_rows_removed"], 1)
        clean_edit = self.store.update_parameters("Example.PRJ", "VC0", subarray_updates={"NModSerie": 16}, dry_run=True)
        self.assertTrue(clean_edit["changed"])
        self.assertNotIn("derived_cache", clean_edit)
    def test_malformed_plot_scope_refuses_component_clone(self):
        plot = (b"  SimulPlots=Start\r\n    PVObject_=pvSimulPlot\r\n" b"      NValRes=2\r\n      ValuesX=1,2,\r\n    End of PVObject pvSimulPlot\r\n")
        cold = self.original.read_bytes().replace(b"Version=8.1.6", b"Version=8.1.6\r\n  Flags=$01", 1)
        for scope, error in ((plot, "SimulPlots"), (plot + plot + b"  EndTags\r\n", "SimulPlots"),
                             (plot.replace(b"NValRes=2", b"NValRes=bad") + b"  EndTags\r\n", "NValRes")):
            with self.subTest(error=error, scope=scope):
                before = cold.replace(b"  PVObject_System", scope + b"  PVObject_System")
                self.original.write_bytes(before)
                with self.assertRaisesRegex(ValueError, error): self.store.clone("Example.PRJ", "VC0", "VC1", {"OND": "new.OND"})
                self.assertEqual(self.original.read_bytes(), before)
                self.assertFalse(self.original.with_suffix(".VC1").exists())
    def test_update_writes_backup_with_hash_guard(self):
        before_bytes = self.original.read_bytes()
        initial = self.store.inspect("Example.PRJ", "VC0")
        changed = self.store.update("Example.PRJ", "VC0", {"GEN": "new.GEN"}, initial["sha256"])
        self.assertEqual(self.store.inspect("Example.PRJ", "VC0")["system_references"]["GEN"], ["new.GEN"])
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.update("Example.PRJ", "VC0", {"GEN": "original.GEN"}, initial["sha256"])
        self.assertEqual((self.workspace / "Projects" / ".mcp-variant-backups" / changed["backup_name"]).read_bytes(), before_bytes)
    def test_update_refuses_an_external_write_after_inspection(self):
        initial = self.store.inspect("Example.PRJ", "VC0")
        external = self.original.read_bytes() + b"\r\n"
        backup = self.store._backup
        def write_during_backup(path, data, transaction_id=None):
            # Another process (for example the PVsyst GUI) saves after the hash check.
            name = backup(path, data, transaction_id)
            path.write_bytes(external)
            return name
        with patch.object(self.store, "_backup", side_effect=write_during_backup):
            with self.assertRaisesRegex(ValueError, "changed while preparing"):
                self.store.update("Example.PRJ", "VC0", {"GEN": "new.GEN"}, initial["sha256"])
        self.assertEqual(self.original.read_bytes(), external)
    def test_clone_and_inspect_rejection_guards(self):
        before = self.original.read_bytes()
        with self.subTest(case="missing_or_ambiguous_reference"):
            with self.assertRaisesRegex(FileNotFoundError, "loose file"): self.store.clone("Example.PRJ", "VC0", "VC2", {"PAN": "missing.PAN"})
            with self.assertRaisesRegex(ValueError, "No section"): self.store.clone("Example.PRJ", "VC0", "VC2", {"PAN": "new.PAN"}, 99)
            with self.assertRaisesRegex(ValueError, "Choose one or more"): self.store.clone("Example.PRJ", "VC0", "VC2", {"Controller": "new.RLT"})
            for value in ("../new.PAN", "new.OND"):
                with self.assertRaises(ValueError): self.store.clone("Example.PRJ", "VC0", "VC2", {"PAN": value})
            with self.assertRaises(ValueError): self.store.inspect("../Example.PRJ", "VC0")
            with self.assertRaises(ValueError): self.store.clone("Example.PRJ", "VC0", "../VC2")
            duplicate = before.replace(b"PVModule=original.PAN\r\n", b"PVModule=original.PAN\r\n        PVModule=original.PAN\r\n", 1)
            self.original.write_bytes(duplicate)
            with self.assertRaisesRegex(ValueError, "exactly once"): self.store.clone("Example.PRJ", "VC0", "VC2", {"PAN": "new.PAN"})
            self.assertFalse((self.workspace / "Projects" / "Example.VC2").exists())
            self.assertEqual(before.count(b"PVModule=original.PAN"), 2)
        with self.subTest(case="malformed_component"):
            self.original.write_bytes(before)
            unsafe = self.workspace / "ComposPV" / "Gensets" / "unsafe.GEN"
            unsafe.write_text(GEN.replace("CFuelHor=0.25", "CFuelHor=0"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "CFuelHor must be greater than zero"):
                self.store.clone("Example.PRJ", "VC0", "VC4", {"GEN": "unsafe.GEN"})
            self.assertFalse((self.workspace / "Projects" / "Example.VC4").exists())
            self.assertEqual(self.original.read_bytes(), before)
        with self.subTest(case="invalid_project_file"):
            project = self.workspace / "Projects" / "Example.PRJ"
            orig_prj = project.read_text(encoding="utf-8")
            project.write_text("unrecognized project", encoding="utf-8")
            try:
                with self.assertRaisesRegex(ValueError, "pvProject"): self.store.clone("Example.PRJ", "VC0", "VC4")
                self.assertFalse((self.workspace / "Projects" / "Example.VC4").exists())
            finally:
                project.write_text(orig_prj, encoding="utf-8")
        with self.subTest(case="linked_variant_symlink"):
            outside = self.workspace.parent / "outside.VC0"
            outside.write_text(VARIANT, encoding="utf-8")
            link = self.workspace / "Projects" / "Example.VC5"
            try:
                link.symlink_to(outside)
            except OSError: self.skipTest("Symlink creation is unavailable on this Windows host")
            with self.assertRaisesRegex(ValueError, "Symlink"): self.store.inspect("Example.PRJ", "VC5")
            self.assertEqual(outside.read_text(encoding="utf-8"), VARIANT)
    def _prepare_grid_variant(self):
        branch = """  PVObject_SystemCircuit=pvCircuit
    InverterNode Start;
      SubArrayId=1
      SubArrayName=PV Array
      Children=Start
        StringNode Start;
          SubArrayId=1
          SubArrayName=PV Array
        StringNode End;
      Children=End
    InverterNode End;
  End of PVObject pvCircuit
"""
        data = VARIANT.replace("  PVObject_PVMainArray=pvMainArray\n", "  PVObject_PVMainArray=pvMainArray\n" + branch)
        data = data.replace("PVObject_=pvSubArray\n        SubArrayId=1\n",
                            "PVObject_=pvSubArray\n        Comment=PV Array\n        NInverter=1\n        NStringCh=1\n        SubArrayId=1\n", 1)
        data = data.replace("        GInverter=original.OND\n", "        GInverter=original.OND\n        NoOrientation=1\n", 1)
        data = data.replace("    SystemType=Battery", "    SystemType=Grid")
        data = data.replace("End of PVObject pvVCalcul\n", "  PVObject_Ombrage=pvShading\n    Flags=$00\n"
                            "  End of PVObject pvShading\nEnd of PVObject pvVCalcul\n")
        self.original.write_bytes(data.replace("\n", "\r\n").encode("utf-8"))
        return data
    def test_clone_subarray_updates_complete_branch(self):
        data = self._prepare_grid_variant()
        before = self.store.inspect("Example.PRJ", "VC0")
        result = self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"])
        self.assertEqual(result["subarray_id"], 3)
        updated = self.original.read_bytes()
        self.assertEqual(updated.count(b"SubArrayId=3"), 3)
        self.assertEqual(updated.count(b"SubArrayName=Sub-array #3"), 2)
        self.assertEqual(self.store.inspect("Example.PRJ", "VC0")["subarrays"][-1]["id"], 3)
        with self.assertRaisesRegex(ValueError, "changed since inspection"): self.store.clone_subarray("Example.PRJ", "VC0", 1, before["sha256"])
        removed = self.store.remove_subarray("Example.PRJ", "VC0", 3, result["sha256"])
        self.assertEqual(removed["sha256"], before["sha256"])
        self.assertEqual(self.original.read_bytes(), data.replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual((self.workspace / "Projects" / ".mcp-variant-backups" / removed["backup_name"]).read_bytes(), updated)
        self.assertEqual((self.workspace / "Projects" / ".mcp-variant-backups" / result["backup_name"]).read_bytes(),
            data.replace("\n", "\r\n").encode("utf-8"))
        self.original.write_bytes(data.replace("Flags=$00\n  End of PVObject pvShading", "Flags=$20\n  End of PVObject pvShading").encode("utf-8"))
        digest = self.store.inspect("Example.PRJ", "VC0")["sha256"]
        with self.assertRaisesRegex(ValueError, "Shading is active"): self.store.clone_subarray("Example.PRJ", "VC0", 1, digest)
        self.original.write_bytes(data.replace("\n", "\r\n").encode("utf-8"))
        state = self.store.inspect("Example.PRJ", "VC0")
        external = self.original.read_bytes().replace(b"FieldTilt=20.0", b"FieldTilt=22.0")
        save_backup = self.store._backup
        def external_edit(path, raw):
            name = save_backup(path, raw)
            path.write_bytes(external)
            return name
        with patch.object(self.store, "_backup", side_effect=external_edit):
            with self.assertRaisesRegex(ValueError, "changed while preparing"): self.store.clone_subarray("Example.PRJ", "VC0", 1, state["sha256"])
        self.assertEqual(self.original.read_bytes(), external)
    def test_subarray_clone_and_removal_invalidate_saved_results(self):
        cold = self._prepare_grid_variant().replace("  Version=8.1.6\n", "  Version=8.1.6\n  Flags=$00100000\n")
        cache = ("  VarList, list of=1 TVarSimul\n    VarSimul_1=GlobHor\n" "      Varident=GlobHor\n      ValMonth=10,20,\n"
                 "    End of VarSimul=viGlobHor\n  End of the List VarList\n")
        hot = cold.replace("Flags=$00100000", "Flags=$00100001").replace("End of PVObject pvVCalcul", cache + "End of PVObject pvVCalcul")
        self.original.write_bytes(hot.encode())
        self.store.clone_subarray("Example.PRJ", "VC0", 1)
        cloned = self.original.read_text(encoding="utf-8")
        for edit in ("clone", "remove"):
            with self.subTest(edit=edit):
                if edit == "remove":
                    warm = cloned.replace(cache, "").replace("Flags=$00100000", "Flags=$00100001")
                    warm = warm.replace("End of PVObject pvVCalcul", cache + "End of PVObject pvVCalcul")
                    self.original.write_text(warm, encoding="utf-8")
                    self.store.remove_subarray("Example.PRJ", "VC0", 3)
                state = self.store.inspect_parameters("Example.PRJ", "VC0", include_saved_results=True)["saved_results"]
                self.assertFalse(state["present"])
                self.assertEqual(state["root_flags"], "$00100000")
                self.assertIn(b"    Flags=$60", self.original.read_bytes())
    def test_parameter_edits_are_scoped_and_guarded(self):
        self.original.write_bytes(self.original.read_bytes().replace(b"  Version=8.1.6", b"  Comment=Original variant\r\n  Version=8.1.6", 1))
        baseline = self.original.read_bytes()
        initial = self.store.inspect_parameters("Example.PRJ", "VC0")
        self.assertEqual(initial["variant_name"], "Original variant")
        self.assertEqual(initial["orientations"][0]["values"]["FieldTilt"], "20.0")
        changed = self.store.update_parameters("Example.PRJ", "VC0", initial["sha256"], 1, 1,
                  {"NModSerie": 15, "VBkUpEncl_syst": 0.9}, {"FieldTilt": 25, "FieldAzim": -10}, variant_name="Revised variant")
        updated = self.store.inspect_parameters("Example.PRJ", "VC0")
        self.assertEqual(updated["variant_name"], "Revised variant")
        self.assertEqual(changed["name_change"], {"before": "Original variant", "after": "Revised variant"})
        self.assertFalse(self.store.update_parameters("Example.PRJ", "VC0", changed["sha256"], 1, 1, variant_name="Revised variant")["changed"])
        self.assertEqual(updated["subarrays"][0]["values"]["VBkUpEncl_syst"], "0.900")
        self.assertEqual(updated["subarrays"][1]["values"], {})
        self.assertEqual(updated["orientations"][0]["values"]["FieldTilt"], "25.000")
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.update_parameters("Example.PRJ", "VC0", initial["sha256"], 1, 1, {"NModSerie": 12})
        with self.assertRaisesRegex(ValueError, "Invalid FieldTilt"):
            self.store.update_parameters("Example.PRJ", "VC0", changed["sha256"], 1, 1, orientation_updates={"FieldTilt": 91})
        self.assertEqual((self.workspace / "Projects" / ".mcp-variant-backups" / changed["backup_name"]).read_bytes(), baseline)
        self.original.write_bytes(baseline)
        backups = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
        for name in (True, 55, "", " padded ", "line\nFlags=$00", "tab\tname", "\ud800", "x" * 81, "\u540d" * 27):
            with self.assertRaisesRegex(ValueError, "Invalid variant_name"):
                self.store.update_parameters("Example.PRJ", "VC0", initial["sha256"], 1, 1, variant_name=name)
        own_name = b"  Comment=Original variant\r\n"
        for blocked in (baseline.replace(own_name, b""), baseline.replace(own_name, own_name * 2),
                        baseline.replace(b"End of PVObject pvVCalcul", b"Comment=Late root\r\nEnd of PVObject pvVCalcul"),
                        baseline.replace(own_name, b"").replace(b"    SubArrays=Start", b"    PVObject_Metadata=pvExtra\r\n      Comment=Decoy\r\n    End of PVObject pvExtra\r\n    SubArrays=Start")):
            self.original.write_bytes(blocked)
            state = self.store.inspect_parameters("Example.PRJ", "VC0")
            with self.assertRaisesRegex(ValueError, "owning header Comment"):
                self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, variant_name="Changed")
            self.assertEqual(self.original.read_bytes(), blocked)
        self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backups)
        bomless = baseline.removeprefix(b"\xef\xbb\xbf")
        self.original.write_bytes(bomless)
        state = self.store.inspect_parameters("Example.PRJ", "VC0")
        self.assertFalse(state["utf8_bom"])
        with self.assertRaisesRegex(ValueError, "require UTF-8 BOM"):
            self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, variant_name="\u5c4b\u9876\u65b9\u6848")
        with self.assertRaisesRegex(ValueError, "requires variant_name"):
            self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, subarray_updates={"NModSerie": 15}, add_utf8_bom=True)
        for invalid in (1, "true"):
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, variant_name="ASCII", add_utf8_bom=invalid)
        self.assertEqual(self.original.read_bytes(), bomless)
        self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backups)
        for title in ("\u5c4b\u9876\u65b9\u6848", "Original variant"):
            extra = {"orientation_updates": {"FieldTilt": 25}} if title != "Original variant" else {}
            backup_set = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
            preview = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, variant_name=title, add_utf8_bom=True, dry_run=True, **extra)
            self.assertEqual(self.original.read_bytes(), bomless)
            self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backup_set)
            edited = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, variant_name=title, add_utf8_bom=True, **extra)
            self.assertEqual(preview["sha256"], edited["sha256"])
            self.assertTrue(edited["bom_added"])
            self.assertTrue(self.store.inspect_parameters("Example.PRJ", "VC0")["utf8_bom"])
            expected = bomless.replace(own_name, b"  Comment=" + title.encode("utf-8") + b"\r\n")
            if extra: expected = expected.replace(b"FieldTilt=20.0", b"FieldTilt=25.000")
            self.assertEqual(self.original.read_bytes(), b"\xef\xbb\xbf" + expected)
            if title == "Original variant":
                self.assertEqual(edited["diff"], "")
                self.assertTrue(edited["changed"])
            backup_set = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
            noop = self.store.update_parameters("Example.PRJ", "VC0", edited["sha256"], 1, 1, variant_name=title, add_utf8_bom=True, **extra)
            self.assertFalse(noop["changed"])
            self.assertFalse(noop["bom_added"])
            self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backup_set)
            self.original.write_bytes(bomless)
        self.original.write_bytes(baseline)
        backups = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
        for field, message in (("UsesTiltAver=True", "Averaged"), ("UsesAzimAver=True", "Averaged"),
                               ("UsesSlopeAver=True", "Averaged"), ("Flags=$20", "ordinary Flags"), ("Flags=$00\r\n    Flags=$00", "ordinary Flags"),
                               ("UsesTiltAver=False\r\n    UsesTiltAver=False", "Averaged"),
                               ("PVObject_Metadata=pvExtra\r\n    End of PVObject pvExtra", "flat fixed-plane")):
            blocked = baseline.replace(b"    FieldType=FixedPlane", ("    " + field + "\r\n    FieldType=FixedPlane").encode())
            self.original.write_bytes(blocked)
            state = self.store.inspect_parameters("Example.PRJ", "VC0")
            for preview in (False, True):
                with self.assertRaisesRegex(ValueError, message):
                    self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, orientation_updates={"FieldTilt": 25}, dry_run=preview)
                self.assertEqual(self.original.read_bytes(), blocked)
                self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backups)
        ordinary = baseline.replace(b"    FieldType=FixedPlane", b"    Flags=$00\r\n    UsesTiltAver=False\r\n    UsesAzimAver=False\r\n    UsesSlopeAver=False\r\n    FieldType=FixedPlane")
        self.original.write_bytes(ordinary)
        state = self.store.inspect_parameters("Example.PRJ", "VC0")
        edited = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, orientation_updates={"FieldTilt": 30})
        self.assertEqual(self.original.read_bytes(), ordinary.replace(b"FieldTilt=20.0", b"FieldTilt=30.000"))
        self.original.write_bytes(ordinary)
        for loss, version in (("LIDLoss", "8.1.0"), ("LIDLoss", "8.1.6"), ("PMismMPP", "8.1.6"),
                              ("PMismMPP", "8.1.0"), ("UFactor", "8.1.6"), ("UFactor", "8.1.0"),
                               ("ModuleQualityLoss", "8.1.6"), ("ModuleQualityLoss", "8.1.0")):
            main = loss == "UFactor"
            quality = loss == "ModuleQualityLoss"
            field_name = "PQualityModU" if quality else loss
            initial_value, target = ("20.0", 29) if main else ("0.00", -5 if quality else 3)
            scope = "main_array_updates" if main else "subarray_updates"
            fixture = VARIANT.replace("SystemType=Battery", "SystemType=Grid")
            if main:
                fixture = fixture.replace("SubArrayId=", f"Version={version}\n        SubArrayId=").replace(
                    "PVObject_PVMainArray=pvMainArray", "PVObject_PVMainArray=pvMainArray\n" "    Flags=$00100400\n    Flags2=$082A\n    UFactor=20.0\n"
                    "    PVObject_Metadata=pvExtra\n      UFactor=99\n      Flags=$FFFFFFFF\n" "      Flags2=$FFFF\n    End of PVObject pvExtra")
            elif quality:
                fixture = fixture.replace("SubArrayId=1", f"Version={version}\n        SubArrayId=1\n"
                    "        PQualityModU=0.00\n        PQualityModI=0.00\n        PQualityModP=99\n"
                    "        PMismMPP=2.00\n        Flags=$001E0402\n        PVObject_Metadata=pvExtra\n"
                    "          PQualityModU=99\n          PQualityModI=99\n          PQualityModP=99\n"
                    "          Flags=$FFFFFFFF\n        End of PVObject pvExtra")
            else:
                fixture = fixture.replace("SubArrayId=1", f"Version={version}\n        SubArrayId=1\n        {loss}=0.00\n"
                    f"        Flags=$001E0402\n        PVObject_Metadata=pvExtra\n          {loss}=99\n"
                    "          Flags=$FFFFFFFF\n        End of PVObject pvExtra")
            raw = b"\xef\xbb\xbf" + fixture.replace("\n", "\r\n").encode()
            self.original.write_bytes(raw)
            state = self.store.inspect_parameters("Example.PRJ", "VC0")
            values = state["main_array"]["values"] if main else state["subarrays"][0]["values"]
            self.assertEqual(values[field_name], initial_value)
            self.assertEqual(values["Flags"], "$00100400" if main else "$001E0402")
            updates = {scope: {loss: target}}
            preview = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, **updates, dry_run=True)
            self.assertEqual(self.original.read_bytes(), raw)
            changed = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, **updates)
            field = f"{field_name}={initial_value}".encode()
            expected = raw.replace(field, f"{field_name}={target:.3f}".encode())
            if quality:
                expected = expected.replace(b"PQualityModI=0.00", f"PQualityModI={target:.3f}".encode()).replace(b"Flags=$001E0402", b"Flags=$000E0402")
                self.assertEqual(changed["module_quality"], {"default_before": True, "default_after": False})
                self.assertEqual(preview["module_quality"], changed["module_quality"])
                self.assertEqual(values["PQualityModP"], "99")
            if loss == "PMismMPP":
                expected = expected.replace(b"Flags=$001E0402", b"Flags=$001A0402")
                self.assertEqual(preview["mismatch"], {"default_before": True, "default_after": False})
                self.assertEqual(changed["mismatch"], preview["mismatch"])
            self.assertEqual(changed["sha256"], preview["sha256"])
            self.assertEqual(self.original.read_bytes(), expected)
            self.assertFalse(self.store.update_parameters("Example.PRJ", "VC0", changed["sha256"], 1, 1, **updates)["changed"])
            self.original.write_bytes(raw)
            if quality:
                combined = self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, {"ModuleQualityLoss": target, "PMismMPP": 5})
                self.assertEqual(self.original.read_bytes(), expected.replace(b"Flags=$000E0402", b"Flags=$000A0402").replace(b"PMismMPP=2.00", b"PMismMPP=5.000"))
                self.assertEqual(combined["mismatch"], {"default_before": True, "default_after": False})
                self.assertEqual(combined["module_quality"], {"default_before": True, "default_after": False})
                self.original.write_bytes(raw)
                for raw_field in ("PQualityModU", "PQualityModI", "PQualityModP"):
                    with self.assertRaisesRegex(ValueError, "supported"):
                        self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, {raw_field: target})
            backups = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
            for value in (14.99 if main else -5.01 if quality else -0.01, 20.01 if quality else 100.01, True, "3", float("inf"), float("nan")):
                with self.assertRaisesRegex(ValueError, f"Invalid {loss}"):
                    self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, **{scope: {loss: value}})
            guards = [(raw.replace(field + b"\r\n", b""), f"{field_name} must occur"),
                (raw.replace(field, field + f"\r\n        {field_name}=1".encode()), f"{field_name} must occur"),
                (raw.replace(("Version=" + version).encode(), b"Version=8.1.5"), "verified"),
                (raw.replace(b"SystemType=Grid", b"SystemType=Battery"), "verified")]
            with_scene = raw.replace(b"PVObject_PVMainArray",
                b"PVObject_ModuleLayout=1\r\n  PVObject_Ombrage=pvShading\r\n    Flags=$20\r\n  End of PVObject pvShading\r\n  PVObject_PVMainArray")
            self.original.write_bytes(with_scene)
            scene_state = self.store.inspect("Example.PRJ", "VC0")
            self.store.update_parameters("Example.PRJ", "VC0", scene_state["sha256"], 1, 1, **updates)
            self.assertIn(b"PVObject_ModuleLayout=1", self.original.read_bytes())
            self.assertIn(b"PVObject_Ombrage=pvShading", self.original.read_bytes())
            self.original.write_bytes(raw)
            backups = list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir())
            if main:
                guards += [(raw.replace(b"Flags=$00100400\r\n", b""), "verified main-array Flags"),
                    (raw.replace(b"Flags=$00100400", b"Flags=$00100400\r\n    Flags=$00"), "verified main-array Flags"),
                    (raw.replace(b"Flags2=$082A", b"Flags2=$00"), "verified main-array Flags"),
                    (raw.replace(b"PVObject_PVMainArray=pvMainArray", b"PVObject_PVMainArray=pvOther"), "complete main array")]
                with self.assertRaisesRegex(ValueError, "supported"):
                    self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, {loss: target})
                with self.assertRaisesRegex(ValueError, "separate"):
                    self.store.update_parameters("Example.PRJ", "VC0", state["sha256"], 1, 1, **updates, independent_mppt_enabled=True)
            elif loss == "PMismMPP" or quality:
                if quality:
                    guards += [(raw.replace(b"        PQualityModI=0.00\r\n", b""), "PQualityModI must occur"),
                        (raw.replace(b"PQualityModI=0.00", b"PQualityModI=0.00\r\n        PQualityModI=1"), "PQualityModI must occur")]
                guards += [(raw.replace(b"Flags=$001E0402\r\n", b""), "one owning Flags"),
                    (raw.replace(b"Flags=$001E0402", b"Flags=$001E0402\r\n        Flags=$00"), "one owning Flags"),
                    (raw.replace(b"Flags=$001E0402", b"Flags=broken"), "hexadecimal Flags")]
            for blocked, message in guards:
                self.original.write_bytes(blocked)
                blocked_state = self.store.inspect("Example.PRJ", "VC0")
                with self.assertRaisesRegex(ValueError, message):
                    self.store.update_parameters("Example.PRJ", "VC0", blocked_state["sha256"], 1, 1, **updates)
                self.assertEqual(self.original.read_bytes(), blocked)
            self.assertEqual(list((self.workspace / "Projects" / ".mcp-variant-backups").iterdir()), backups)
    def test_structure_validator_reports_missing_orientation(self):
        checked = self.store.validate_structure("Example.PRJ", "VC0")
        self.assertFalse(checked["valid"])
        self.assertEqual(checked["subarray_ids"], [1, 2])
        self.assertTrue(any("needs one NoOrientation" in message for message in checked["issues"]))
    def test_inspect_parameters_ignores_shading_object_orientation_ids(self):
        data = self.original.read_bytes().replace(b"End of PVObject pvVCalcul\r\n",
            b"PVObject_Ombrage=pvShading\r\n    NoOrient=1\r\n" b"End of PVObject pvShading\r\nEnd of PVObject pvVCalcul\r\n")
        self.original.write_bytes(data)
        inspected = self.store.inspect_parameters("Example.PRJ", "VC0")
        self.assertEqual([item["id"] for item in inspected["orientations"]], [1])
    def test_orientation_edit_rejects_active_scene_layout_and_shading_table(self):
        with self.subTest(case="active_scene_and_module_layout"):
            data = self.original.read_bytes().replace(b"End of PVObject pvVCalcul\r\n",
                b"PVObject_Ombrage=pvShading\r\n    Flags=$20\r\n" b"End of PVObject pvShading\r\nEnd of PVObject pvVCalcul\r\n")
            self.original.write_bytes(data)
            digest = self.store.inspect_parameters("Example.PRJ", "VC0")["sha256"]
            for options in ({"orientation_updates": {"FieldTilt": 25}}, {"subarray_updates": {"NModSerie": 15}}):
                for preview in (False, True):
                    with self.assertRaisesRegex(ValueError, "active shading scene"):
                        self.store.update_parameters("Example.PRJ", "VC0", digest, 1, 1, dry_run=preview, **options)
                    self.assertEqual(self.original.read_bytes(), data)
            layout = VARIANT.replace("End of PVObject pvVCalcul", "PVObject_ModuleLayout=pvModuleLayout\nFlags=$0B10\nModLayoutGlobState=mlDefInverters\nEnd of PVObject pvModuleLayout\nEnd of PVObject pvVCalcul").encode()
            self.original.write_bytes(layout)
            digest = self.store.inspect_parameters("Example.PRJ", "VC0")["sha256"]
            for options in ({"orientation_updates": {"FieldTilt": 25}}, {"subarray_updates": {"NModSerie": 15}}):
                with self.assertRaisesRegex(ValueError, "module layout"): self.store.update_parameters("Example.PRJ", "VC0", digest, 1, 1, **options)
                self.assertEqual(self.original.read_bytes(), layout)
            self.assertFalse((self.workspace / "Projects" / ".mcp-variant-backups").exists())
        with self.subTest(case="dependent_shading_table"):
            write_utf8_crlf(self.original, VARIANT)
            data = self.original.read_bytes().replace(b"    FieldAzim=0.0\r\n", b"    FieldAzim=0.0\r\n    PVObject_ShdTableLin=pvShdFTable\r\n", 1)
            self.original.write_bytes(data)
            digest = self.store.inspect_parameters("Example.PRJ", "VC0")["sha256"]
            for options in ({"orientation_updates": {"FieldTilt": 30}}, {"subarray_updates": {"NModSerie": 15}}):
                with self.assertRaisesRegex(ValueError, "dependent shading table"):
                    self.store.update_parameters("Example.PRJ", "VC0", digest, 1, 1, **options)
                self.assertEqual(self.original.read_bytes(), data)
    def test_inspect_project_reports_site_meteo_links_without_nested_false_match(self):
        (self.workspace / "Sites").mkdir()
        (self.workspace / "Meteo").mkdir()
        (self.workspace / "Sites" / "first.SIT").write_text("site", encoding="utf-8")
        (self.workspace / "Meteo" / "first.MET").write_bytes(b"met")
        (self.workspace / "Projects" / "Example.PRJ").write_text("PVObject_=pvProject\nMeteoFileName=first.MET\n"
            "PVObject_SitePrj=pvSite\n  NomF=first.SIT\nEnd of PVObject pvSite\n" "End of PVObject pvProject\n", encoding="utf-8")
        variant = VARIANT.replace("  PVObject_=pvOrient\n", "  PVObject_SiteSimul=pvSite\n    NomF=second.SIT\n"
            "  End of PVObject pvSite\n  PVObject_MeteoSimul=pvMeteo\n" "    NomF=first.MET\n    PVObject_SiteMet=pvSite\n"
            "      NomF=New.SIT\n    End of PVObject pvSite\n" "  End of PVObject pvMeteo\n  PVObject_=pvOrient\n")
        self.original.write_text(variant, encoding="utf-8")
        inspected = self.store.inspect_project("Example.PRJ")
        self.assertTrue(inspected["sources"]["Example.PRJ"]["meteo"]["workspace_file"])
        self.assertEqual(inspected["sources"]["Example.VC0"]["meteo"]["name"], "first.MET")
        self.assertIn("Example.VC0: site differs from Example.PRJ", inspected["warnings"])
        self.assertTrue(any("second.SIT is not a workspace file" in item for item in inspected["warnings"]))
    def _prepare_project_sources(self):
        (self.workspace / "Sites").mkdir()
        (self.workspace / "Meteo").mkdir()
        (self.workspace / "Sites" / "New.SIT").write_text("PVObject_=pvSite\n  NomF=New.SIT\n  Site=New Site\n"
            "  Latitude=6.5\n  Longitude=80.1\nEnd of PVObject pvSite\n", encoding="utf-8")
        (self.workspace / "Meteo" / "New.MET").write_bytes(b"fixture weather")
        (self.workspace / "Projects" / "Example.PRJ").write_text("PVObject_=pvProject\n  Comment=Old project\nMeteoFileName=Old.MET\n"
            "PVObject_SitePrj=pvSite\n  NomF=Old.SIT\n  Site=Old Site\n" "End of PVObject pvSite\nEnd of PVObject pvProject\n", encoding="utf-8")
        variant = VARIANT.replace("  PVObject_=pvOrient\n", "  PVObject_SiteSimul=pvSite\n    NomF=Old.SIT\n    Site=Old Site\n"
            "  End of PVObject pvSite\n  PVObject_MeteoSimul=pvMeteo\n" "    NomF=Old.MET\n    SiteM=Old Site\n"
            "    PVObject_SiteMet=pvSite\n      NomF=Old.SIT\n" "    End of PVObject pvSite\n  End of PVObject pvMeteo\n" "  PVObject_=pvOrient\n")
        write_utf8_crlf(self.original, variant)
        second = self.workspace / "Projects" / "Example.VC1"
        second.write_bytes(self.original.read_bytes())
        before = self.store.inspect_project("Example.PRJ")
        originals = {name: (self.workspace / "Projects" / name).read_bytes() for name in before["files"]}
        return before, originals
    def test_project_source_replacement_invalidates_saved_results(self):
        self._prepare_project_sources()
        cache = (b"  VarList, list of=1 TVarSimul\r\n    VarSimul_1=GlobHor\r\n" b"      Varident=GlobHor\r\n      ValMonth=10,20,\r\n"
                 b"    End of VarSimul=viGlobHor\r\n  End of the List VarList\r\n")
        self.original.write_bytes(self.original.read_bytes().replace(b"  Version=8.1.6\r\n", b"  Version=8.1.6\r\n  Flags=$0001\r\n").replace(
            b"End of PVObject pvVCalcul", cache + b"End of PVObject pvVCalcul"))
        self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET")
        state = self.store.inspect_parameters("Example.PRJ", "VC0", include_saved_results=True)["saved_results"]
        self.assertFalse(state["present"])
        self.assertEqual(state["root_flags"], "$0000")
        self.assertIn(b"    Flags=$60\r\n", self.original.read_bytes())
    def test_update_project_sources_and_parameters_across_all_variants(self):
        before, originals = self._prepare_project_sources()
        with self.assertRaisesRegex(ValueError, "existing workspace files"):
            self.store.update_project_sources("Example.PRJ", "missing.SIT", "New.MET", before["files"])
        changed = self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", before["files"])
        self.assertEqual(set(changed["backups"]), set(before["files"]))
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], changed["files"])
        self.assertEqual(self.store.inspect_project("Example.PRJ")["warnings"], [])
        for name, data in originals.items():
            self.assertEqual((self.workspace / "Projects" / ".mcp-variant-backups" / changed["backups"][name]).read_bytes(), data)
        for name in ("Example.VC0", "Example.VC1"):
            raw = (self.workspace / "Projects" / name).read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", raw)
            self.assertIn(b"NomF=New.MET", raw)
            self.assertIn(b"SiteM=New Site", raw)
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", before["files"])
        for name, data in originals.items(): (self.workspace / "Projects" / name).write_bytes(data)
        project = self.workspace / "Projects" / "Example.PRJ"
        siblings = {name: ((self.workspace / "Projects" / name).stat().st_mtime_ns, (self.workspace / "Projects" / name).stat().st_ino)
                    for name in before["files"] if name != project.name}
        self.assertEqual(before["project_name"], "Old project")
        for value in (True, 56, "", " padded ", "line\nFlags=$00", "\ud800", "x" * 81, "\u540d" * 27):
            with self.assertRaisesRegex(ValueError, "Invalid project_name"): self.store.update_project_parameters("Example.PRJ", before["files"], value)
        with self.assertRaisesRegex(ValueError, "require UTF-8 BOM"): self.store.update_project_parameters("Example.PRJ", before["files"], "\u5c4b\u9876")
        for title, add_bom in (("Revised project", False), ("\u5c4b\u9876", True), ("Old project", True),
                               ("X", False), ("A" * 80, False), ("\u540d" * 26 + "AB", True)):
            preview = self.store.update_project_parameters("Example.PRJ", before["files"], title, True, add_bom)
            self.assertEqual(project.read_bytes(), originals[project.name])
            edited = self.store.update_project_parameters("Example.PRJ", before["files"], title, False, add_bom)
            self.assertEqual(edited["files"], preview["files"])
            self.assertEqual(self.store.inspect_project("Example.PRJ")["project_name"], title)
            self.assertEqual(edited["bom_added"], add_bom)
            noop = self.store.update_project_parameters("Example.PRJ", edited["files"], title, False, add_bom)
            self.assertFalse(noop["changed"])
            self.assertNotIn("backups", noop)
            with self.assertRaisesRegex(ValueError, "changed since inspection"):
                self.store.update_project_parameters("Example.PRJ", before["files"], title)
            self.assertEqual((self.workspace / "Projects" / ".mcp-variant-backups" / edited["backups"][project.name]).read_bytes(),
                originals[project.name])
            project.write_bytes(originals[project.name])
            for name, metadata in siblings.items():
                path = self.workspace / "Projects" / name
                self.assertEqual(path.read_bytes(), originals[name])
                self.assertEqual((path.stat().st_mtime_ns, path.stat().st_ino), metadata)
        own = originals[project.name].splitlines(keepends=True)[1]
        for blocked in (originals[project.name].replace(own, b""), originals[project.name].replace(own, own * 2),
                        originals[project.name].replace(b"End of PVObject pvProject", b"Comment=Late\nEnd of PVObject pvProject"),
                        originals[project.name].replace(own, b"PrintSettings=TPrintSettings\nComment=Decoy\nEndTags\n")):
            project.write_bytes(blocked)
            state = self.store.inspect_project("Example.PRJ")
            with self.assertRaisesRegex(ValueError, "owning header Comment"):
                self.store.update_project_parameters("Example.PRJ", state["files"], "Changed")
            self.assertEqual(project.read_bytes(), blocked)
        project.write_bytes(originals[project.name])
    def test_source_update_rejects_malformed_site_and_variant_before_writes(self):
        before, originals = self._prepare_project_sources()
        site = self.workspace / "Sites" / "New.SIT"
        valid = site.read_text(encoding="utf-8")
        for invalid in (valid.replace("NomF=New.SIT", "NomF=Wrong.SIT"), valid.replace("  Latitude=6.5", "  PVObject_Nested=pvSite"),
                        valid.replace("  Latitude=6.5", "  End of PVObject pvSite")):
            with self.subTest(site=invalid):
                site.write_text(invalid, encoding="utf-8")
                with self.assertRaises(ValueError): self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", before["files"])
                self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], before["files"])
        site.write_text(valid, encoding="utf-8")
        second = self.workspace / "Projects" / "Example.VC1"
        second.write_bytes(originals[second.name].replace(b"SiteM=Old Site", b"Unknown=Old Site"))
        current = self.store.inspect_project("Example.PRJ")["files"]
        with self.assertRaisesRegex(ValueError, "Expected one SiteM"): self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", current)
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], current)
        self.assertFalse((self.workspace / "Projects" / ".mcp-variant-backups").exists())
    def test_source_update_rolls_back_partial_write_failure(self):
        self._prepare_project_sources()
        real_replace = os.replace
        baseline = self.store.inspect_project("Example.PRJ")["files"]
        writes = 0
        def fail_second_replace(source, target):
            nonlocal writes
            writes += 1
            if writes == 2: raise OSError("injected replacement failure")
            return real_replace(source, target)
        with patch("pvsyst_auto.project.variants.os.replace", side_effect=fail_second_replace):
            with self.assertRaisesRegex(OSError, "injected replacement"): self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", baseline)
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], baseline)
        self.assertEqual(list((self.workspace / "Projects").glob("*.tmp")), [])
    def test_source_transaction_preserves_external_edits_before_commit(self):
        self._prepare_project_sources()
        for operation in ("update", "project_name"):
            with self.subTest(operation=operation):
                baseline = self.store.inspect_project("Example.PRJ")["files"]
                original_bytes = {name: (self.workspace / "Projects" / name).read_bytes() for name in baseline}
                real_backup = self.store._backup
                external = original_bytes["Example.VC1"].replace(b"NModSerie=14", b"NModSerie=13")
                def backup_then_edit(path, data, transaction_id=None):
                    name = real_backup(path, data, transaction_id)
                    if path.name == "Example.VC1": path.write_bytes(external)
                    return name
                with patch.object(self.store, "_backup", side_effect=backup_then_edit):
                    with self.assertRaisesRegex(ValueError, "changed while preparing"):
                        if operation == "update": self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", baseline)
                        else: self.store.update_project_parameters("Example.PRJ", baseline, "Changed title")
                for name, data in original_bytes.items():
                    expected = external if name == "Example.VC1" else data
                    self.assertEqual((self.workspace / "Projects" / name).read_bytes(), expected)
                self.assertEqual(list((self.workspace / "Projects").glob("*.tmp")), [])
                (self.workspace / "Projects" / "Example.VC1").write_bytes(original_bytes["Example.VC1"])
    def test_source_transaction_detects_new_variant_before_commit(self):
        initial, originals = self._prepare_project_sources()
        real_backup = self.store._backup
        added = self.workspace / "Projects" / "Example.VC2"
        def backup_then_add(path, data, transaction_id=None):
            name = real_backup(path, data, transaction_id)
            if path.name == "Example.VC1": added.write_bytes(originals["Example.VC1"])
            return name
        with patch.object(self.store, "_backup", side_effect=backup_then_add):
            with self.assertRaisesRegex(ValueError, "changed while preparing"):
                self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", initial["files"])
        self.assertEqual(added.read_bytes(), originals["Example.VC1"])
        for name, data in originals.items(): self.assertEqual((self.workspace / "Projects" / name).read_bytes(), data)
        self.assertEqual(list((self.workspace / "Projects").glob("*.tmp")), [])
    def test_clone_project_rejects_orphan_destination_members(self):
        for name in ("Copied.VC9", "Copied.SHDP"):
            with self.subTest(name=name):
                orphan = self.workspace / "Projects" / name
                orphan.write_bytes(b"existing orphan")
                with self.assertRaises(FileExistsError): self.store.clone_project("Example.PRJ", "Copied.PRJ")
                self.assertFalse((self.workspace / "Projects" / "Copied.PRJ").exists())
                self.assertEqual(orphan.read_bytes(), b"existing orphan")
                orphan.unlink()
    def test_clone_project_copies_all_variants_and_rejects_sidecars(self):
        second = self.workspace / "Projects" / "Example.VC1"
        second.write_bytes(self.original.read_bytes())
        result = self.store.clone_project("Example.PRJ", "Copied.PRJ")
        self.assertEqual(set(result["variants"]), {"VC0", "VC1"})
        self.assertEqual((self.workspace / "Projects" / "Copied.PRJ").read_bytes(), (self.workspace / "Projects" / "Example.PRJ").read_bytes())
        self.assertEqual((self.workspace / "Projects" / "Copied.VC0").read_bytes(), self.original.read_bytes())
        with self.assertRaises(FileExistsError): self.store.clone_project("Example.PRJ", "Copied.PRJ")
        (self.workspace / "Projects" / "Example.SHDP").write_bytes(b"scene")
        with self.assertRaisesRegex(ValueError, "sidecar"): self.store.clone_project("Example.PRJ", "Another.PRJ")
        self.assertFalse((self.workspace / "Projects" / "Another.PRJ").exists())
        project_text = (self.workspace / "Projects" / "Example.PRJ").read_bytes().decode("utf-8")
        variant_text = self.original.read_bytes().decode("utf-8")
        variants = {"vc0": variant_text, "VC1": variant_text}
        created = self.store.create_project("Fresh.PRJ", project_text, variants)
        self.assertEqual(created["variants"], ["VC0", "VC1"])
        self.assertEqual((self.workspace / "Projects" / "Fresh.VC0").read_bytes(), self.original.read_bytes())
        with self.assertRaises(FileExistsError): self.store.create_project("Fresh.PRJ", project_text, variants)
        for content, members in (("broken", variants), (project_text, {}), (project_text, {"VC0": variant_text, "vc0": variant_text}),
                                 (project_text, {"VC0": variant_text, "VC1": "broken"}), (project_text, {"../VC0": variant_text})):
            with self.assertRaises(ValueError): self.store.create_project("Invalid.PRJ", content, members)
            self.assertEqual(list((self.workspace / "Projects").glob("Invalid.*")), [])
        opened = Path.open
        def fail_second(path, *args, **kwargs):
            if path.name == "Partial.VC1": raise OSError("injected second variant write failure")
            return opened(path, *args, **kwargs)
        with patch.object(Path, "open", fail_second), self.assertRaises(OSError): self.store.create_project("Partial.PRJ", project_text, variants)
        self.assertEqual(list((self.workspace / "Projects").glob("Partial.*")), [])
        self.assertEqual(self.original.read_bytes().decode("utf-8"), variant_text)
    def test_optional_hash_guards_succeed_when_omitted_and_reject_when_stale(self):
        self._prepare_project_sources()
        renamed = self.store.update_project_parameters("Example.PRJ", project_name="Renamed")
        self.assertEqual(renamed["name_change"]["after"], "Renamed")
        sources_updated = self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET")
        self.assertEqual(sources_updated["site"], "New.SIT")
        params = self.store.update_parameters("Example.PRJ", "VC0", subarray_updates={"NModSerie": 21})
        self.assertTrue(params["changed"])
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.update_parameters("Example.PRJ", "VC0", expected_sha256="0" * 64, subarray_updates={"NModSerie": 22})
        comp = self.store.update("Example.PRJ", "VC0", {"PAN": "new.PAN"})
        self.assertIn("backup_name", comp)
    def test_size_limit_dotted_projects_and_system_validation(self):
        before = self.original.read_bytes()
        with patch("pvsyst_auto.project.variants.MAX_PROJECT_BYTES", len(before)):
            with self.assertRaisesRegex(ValueError, "exceeds 32 MB"):
                self.store.update_parameters("Example.PRJ", "VC0", None, 1, 1, orientation_updates={"FieldTilt": 25})
        self.store.clone("Example.PRJ", "VC0", "VC1")
        self.store.clone_project("Example.PRJ", "Example.East.PRJ")
        self.assertCountEqual(self.store.inspect_project("Example.PRJ")["variants"], ["VC0", "VC1"])
        self.store.clone_project("Example.PRJ", "\u5149\u4f0f.PRJ")
        self.assertEqual(self.components.dependencies("\u5149\u4f0f", "vc0")["project"], "\u5149\u4f0f.PRJ")
        for bad in ("../\u5149\u4f0f.PRJ", "CON.PRJ", "x.PRJ "):
            with self.assertRaises(ValueError): self.store.inspect(bad, "VC0")
        base = VARIANT.replace("GInverter=original.OND\n", "GInverter=original.OND\n        NoOrientation=1\n")
        missing_sys = re.sub(r"(?ms)^  PVObject_System=pvSystem\n.*?^  End of PVObject pvSystem\n", "", base)
        self.original.write_bytes(missing_sys.encode())
        self.assertIs(self.store.validate_structure("Example.PRJ", "VC0")["valid"], False)
    def test_tracking_pair_tokens_and_seasonal_tilt_edit(self):
        block = ObjectBlock("PVObject_", 1, "pvOrient", fields={"MinPhi": [(5, "NaN")], "MaxPhi": [(6, "55.0")]})
        with self.assertRaisesRegex(ValueError, "MinPhi"): tracking_values({"max_rotation_deg": 50}, "UnlimTracking", block)
        orient = ["  PVObject_=pvOrient\r\n", "    Comment=TSouth_roof\r\n", "    Version=8.1.6\r\n",
                  "    Flags=$00\r\n", "    NoOrient=1\r\n", "    FieldType=FixedPlane\r\n",
                  "    FieldTilt=20.0\r\n", "    FieldAzim=0.0\r\n", "    BaseSlope=0.0\r\n",
                  "    UsesTiltAver=False\r\n", "    UsesAzimAver=False\r\n", "    UsesSlopeAver=False\r\n", "  End of TOrientGroup\r\n"]
        change = seasonal.replace_seasonal(orient, (0, len(orient) - 1), {"summer_tilt": 10.0, "winter_tilt": 50.0, "winter_months": [1, 2, 12]})
        self.assertEqual(change["before"]["orientation_name"], "TSouth_roof")
        self.assertIn("    FieldType=SeasonalTilt\r\n", orient)
        self.assertIn("    WinterMonths=$0803\r\n", orient)
    def test_update_project_sources_preserves_distinct_met_header_and_sitemet(self):
        """Issue #193: rebinding SIT+MET must keep SIT in SiteSimul and MET's own SiteM/SiteMet/PasAccum."""
        before, _ = self._prepare_project_sources()
        met_text = ("PVObject_=pvMeteo\r\n" "  Comment=Real MET header\r\n" "  Version=8.1.7\r\n"
            "  Flags=$010084E1\r\n" "  NomF=Distinct_2022.MET\r\n" "  SiteM=Meteo Station 2022\r\n" "  DateDebF=01/01/22 00:00\r\n"
            "  DateFinF=31/12/22 23:00\r\n" "  PasAccum=1\r\n" "\r\n" "  PVObject_SiteMet=pvSite\r\n"
            "    NomF=Station2022.SIT\r\n" "    Site=Meteo Station 2022\r\n" "    Latitude=47.3700\r\n" "    Longitude=8.5500\r\n"
            "  End of PVObject pvSite\r\n" "End of PVObject pvMeteo\r\n")
        write_utf8_crlf(self.workspace / "Meteo" / "Distinct_2022.MET", met_text)
        self.store.update_project_sources("Example.PRJ", "New.SIT", "Distinct_2022.MET", before["files"])
        vc0 = (self.workspace / "Projects" / "Example.VC0").read_text(encoding="utf-8-sig")
        site_simul = vc0[vc0.index("PVObject_SiteSimul=pvSite"):vc0.index("PVObject_MeteoSimul=pvMeteo")]
        meteo_simul = vc0[vc0.index("PVObject_MeteoSimul=pvMeteo"):vc0.index("End of PVObject pvMeteo")]
        self.assertIn("NomF=New.SIT", site_simul)
        self.assertIn("Site=New Site", site_simul)
        self.assertIn("Latitude=6.5", site_simul)
        self.assertIn("NomF=Distinct_2022.MET", meteo_simul)
        self.assertIn("SiteM=Meteo Station 2022", meteo_simul)
        self.assertIn("DateDebF=01/01/22 00:00", meteo_simul)
        self.assertIn("DateFinF=31/12/22 23:00", meteo_simul)
        self.assertIn("PasAccum=1", meteo_simul)
        self.assertIn("NomF=Station2022.SIT", meteo_simul)
        self.assertIn("Latitude=47.3700", meteo_simul)
        self.assertNotIn("Latitude=6.5", meteo_simul)
class OrientationSpectralAndNativeEcoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "PVsyst8.1_Data"
        for sub in ("Projects", "Sites", "Meteo", "Results", "Models", "ComposPV/PVmodules", "ComposPV/Inverters"):
            (self.ws / sub).mkdir(parents=True, exist_ok=True)
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV/PVmodules/Lab440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        (self.ws / "ComposPV/Inverters/Lab50.OND").write_text(UTILITY_OND, encoding="utf-8")
        self.components = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.components)
        synth = synthesize_project_and_variant(self.store, project_comment="Phase5Test", variant_comment="Base",
            site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET", pan_name="Lab440.PAN", ond_name="Lab50.OND",
            n_mod_serie=20, n_string_ch=10, n_inverter=1, n_inv_mppt=1, tilt_deg=25.0, azimuth_deg=0.0)
        self._base_vc0 = synth["vc0_text"]
        self.store.create_project("Phase5Test.PRJ", synth["prj_text"], {"VC0": self._base_vc0})
        self.project = "Phase5Test.PRJ"
        self.variant = "VC0"
    def test_spectral_correction_enable_custom_disable(self) -> None:
        res = self.store.update_parameters(self.project, self.variant, spectral_updates={"enabled": True, "coeff_set": "CdTe", "use_rel_hum": False},
            dry_run=False)
        self.assertTrue(res["changed"])
        self.assertEqual(res["spectral"]["after"]["enabled"], True)
        self.assertEqual(res["spectral"]["after"]["coeff_set"], "CdTe")
        self.assertEqual(res["spectral"]["after"]["use_rel_hum"], False)
        res2 = self.store.update_parameters(self.project,
            self.variant, spectral_updates={"enabled": True, "coeff_set": "Custom", "coefficients": [0.85, -0.04, -0.005, 0.14, 0.004, -0.003]},
            dry_run=False)
        text = (self.ws / "Projects" / "Phase5Test.VC0").read_text(encoding="latin-1")
        self.assertIn("CoeffSet=Custom", text)
        self.assertIn("Coefficients=0.850000,-0.040000,-0.005000,0.140000,0.004000,-0.003000,", text)
        self.assertEqual(res2["spectral"]["after"]["use_default_coeffs"], False)
        res3 = self.store.update_parameters(self.project, self.variant, spectral_updates={"enabled": False}, dry_run=False)
        self.assertEqual(res3["spectral"]["after"]["enabled"], False)
    def test_native_economics_reads_both_cost_lists(self) -> None:
        self.store.update_parameters(
            self.project, self.variant,
            native_economics_updates={"capex_total": 100, "annual_opex": 7}, dry_run=False,
        )
        result = self.store.inspect_parameters(self.project, self.variant, include_economics=True)
        self.assertEqual(result["economics"]["lists"], {
            "InvestmentCosts": {"type": "InvestmentCost", "declared_count": 1},
            "OperatingCosts": {"type": "OperatingCost", "declared_count": 1},
        })
    def test_convert_orientation_all_targets(self) -> None:
        targets = [("fixed", {"tilt_deg": 28.0, "azimuth_deg": 10.0}, "FieldType=FixedPlane"),
            ("seasonal", {"summer_tilt_deg": 15.0, "winter_tilt_deg": 45.0, "winter_months": [1, 2, 11, 12]}, "FieldType=SeasonalTilt"),
            ("sheds", {"tilt_deg": 20.0, "pitch_m": 8.0, "collector_width_m": 4.0, "shed_count": 12}, "FieldType=Sheds"),
            ("tracker_ns", {"min_phi_deg": -55.0, "max_phi_deg": 55.0, "pitch_m": 6.0, "collector_width_m": 2.2, "backtracking": True}, "FieldType=UnlimTracking"),
            ("tracker_2axis", {"min_tilt_deg": 15.0, "max_tilt_deg": 75.0, "min_phi_deg": -120.0, "max_phi_deg": 120.0}, "FieldType=TrackTwoAxis")]
        for idx, (target, params, expected_marker) in enumerate(targets, start=1):
            with self.subTest(target=target):
                new_vc = f"VC{idx}"
                res = convert_orientation(self.store, project=self.project, source_variant=self.variant,
                    new_variant=new_vc, target_type=target, params=params, dry_run=False)
                self.assertFalse(res["dry_run"], target)
                self.assertEqual(res["orientation_conversion"]["target_type"], target)
                txt = (self.ws / "Projects" / f"Phase5Test.{new_vc}").read_text(encoding="latin-1")
                self.assertIn(expected_marker, txt)
                self.assertNotIn("PVObject_Ombrage=pvShading", txt)
    def test_public_convert_orientation_dome_and_paired_subarray_guards(self) -> None:
        from typing import get_args, get_type_hints
        from pvsyst_auto.api.projects import OrientationConvertParams, pvsyst_convert_orientation
        with self.subTest(case="schema_includes_dome"):
            hints = get_type_hints(pvsyst_convert_orientation)
            self.assertIn("dome", get_args(hints["target_type"]))
        source = self.ws / "Projects" / "Phase5Test.VC0"
        blocked_vc = self.ws / "Projects" / "Phase5Test.VC9"
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            to_dome = pvsyst_convert_orientation(self.project, "VC0", "VC6", target_type="dome", params=OrientationConvertParams(tilt_deg=12.0, azimuth_deg=90.0), dry_run=False)
            self.assertEqual(to_dome["orientation_conversion"]["target_type"], "dome")
            self.assertTrue(self.store.validate_structure(self.project, "VC6")["valid"])
            back_fixed = pvsyst_convert_orientation(self.project, "VC6", "VC7", target_type="fixed", params=OrientationConvertParams(tilt_deg=25.0, azimuth_deg=0.0), dry_run=False)
            self.assertEqual(back_fixed["orientation_conversion"]["target_type"], "fixed")
            self.assertTrue(self.store.validate_structure(self.project, "VC7")["valid"])
        dome_vc = (self.ws / "Projects" / "Phase5Test.VC6").read_text(encoding="latin-1")
        self.assertIn("PVObject_=pvDomeOrient", dome_vc)
        self.assertIn("NoOrientation2=2", dome_vc)
        self.assertIn("NStrOrient1=5", dome_vc)
        fixed_vc = (self.ws / "Projects" / "Phase5Test.VC7").read_text(encoding="latin-1")
        self.assertIn("FieldType=FixedPlane", fixed_vc)
        self.assertNotIn("NoOrientation2=", fixed_vc)
        self.assertIn("NStrOrient1=10", fixed_vc)
        for case, mutated, target_type in (
            ("odd_string_count_to_dome", self._base_vc0.replace("NStringCh=10", "NStringCh=9").replace("NStrOrient1=10", "NStrOrient1=9").replace("NElements=10", "NElements=9"), "dome"),
            ("malformed_pvDomeOrient", self._base_vc0.replace("FieldType=FixedPlane", "FieldType=Dome\n  PVObject_Dome=pvDomeOrient"), "fixed")):
            with self.subTest(case=case):
                blocked_vc.unlink(missing_ok=True)
                source.write_text(mutated, encoding="utf-8")
                with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
                    result = pvsyst_convert_orientation(self.project, "VC0", "VC9", target_type=target_type, dry_run=False)
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(blocked_vc.exists())
    def test_native_economics_write_and_remove(self) -> None:
        res = self.store.update_parameters(self.project, self.variant, native_economics_updates={
                "mode": "set", "capex_total": 450000.0, "annual_opex": 8000.0, "subsidies": 50000.0,
                "currency": "EUR", "project_life_years": 25, "discount_rate_pct": 6.0, "sale_tariff": 0.09, "consumption_tariff": 0.22}, dry_run=False)
        self.assertTrue(res["changed"])
        self.assertTrue(res["native_economics"]["configured"])
        txt = (self.ws / "Projects" / "Phase5Test.VC0").read_text(encoding="latin-1")
        self.assertIn("EconomicEval=TEcoEvaluation", txt)
        self.assertIn("Currency=EUR", txt)
        self.assertIn("UnitPrice=450000.00000", txt)
        self.assertIn("OwnFunds=400000.00", txt)
        self.assertIn("Subsidies=50000.00", txt)
        self.assertIn("End of TEcoEvaluation", txt)
        res2 = self.store.update_parameters(self.project, self.variant, native_economics_updates={"mode": "none"}, dry_run=False)
        self.assertFalse(res2["native_economics"]["configured"])
        txt2 = (self.ws / "Projects" / "Phase5Test.VC0").read_text(encoding="latin-1")
        self.assertNotIn("EconomicEval=TEcoEvaluation", txt2)
if __name__ == "__main__": unittest.main()

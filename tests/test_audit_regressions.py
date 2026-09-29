"""Regression coverage for the nine September audit findings; no vendor execution."""
import hashlib
import json
import re
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import test_components
import test_pvsyst
import test_variants
import pvsyst_mcp_server as server
from pvsyst_cli import PVsystCLI, summarize
from pvsyst_components import ComponentStore
from pvsyst_variants import VariantStore


@contextmanager
def fixture(case_type):
    case = case_type()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class AuditRegressions(unittest.TestCase):
    def test_reference_growth_rejected_before_update_or_clone_writes(self):
        with fixture(test_variants.VariantTests) as case:
            before = case.original.read_bytes()
            source = case.workspace / "ComposPV" / "PVmodules" / "new.PAN"
            longer = "much_longer_component_filename.PAN"
            source.with_name(longer).write_bytes(source.read_bytes())
            for clone in (False, True):
                with self.subTest(clone=clone), patch("pvsyst_variants.MAX_PROJECT_BYTES", len(before) + 1):
                    with self.assertRaisesRegex(ValueError, "exceeds 32 MB"):
                        if clone:
                            case.store.clone("Example.PRJ", "VC0", "VC1", {"PAN": longer})
                        else:
                            case.store.update("Example.PRJ", "VC0", {"PAN": longer}, digest(before))
                    self.assertEqual(case.original.read_bytes(), before)
                    self.assertFalse(case.original.with_suffix(".VC1").exists())
                    self.assertFalse((case.original.parent / ".mcp-variant-backups").exists())

    def test_parameter_growth_rejected_before_backup(self):
        with fixture(test_variants.VariantTests) as case:
            before = case.original.read_bytes()
            with patch("pvsyst_variants.MAX_PROJECT_BYTES", len(before)):
                with self.assertRaisesRegex(ValueError, "exceeds 32 MB"):
                    case.store.update_parameters("Example.PRJ", "VC0", digest(before), 1, 1,
                                                 orientation_updates={"FieldTilt": 25})
            self.assertEqual(case.original.read_bytes(), before)
            self.assertFalse((case.original.parent / ".mcp-variant-backups").exists())

    def test_subarray_growth_rejected_before_backup(self):
        with fixture(test_variants.VariantTests) as case:
            case._prepare_grid_variant()
            before = case.original.read_bytes()
            with patch("pvsyst_variants.MAX_PROJECT_BYTES", len(before)):
                with self.assertRaisesRegex(ValueError, "exceeds 32 MB"):
                    case.store.clone_subarray("Example.PRJ", "VC0", 1, digest(before))
            self.assertEqual(case.original.read_bytes(), before)
            self.assertFalse((case.original.parent / ".mcp-variant-backups").exists())

    def test_component_archive_restores_incomplete_or_unrecognized_bytes(self):
        for raw in (test_components.GEN.replace("  Flags=$00\n", "").encode(), b"unknown\x00\xff"):
            with self.subTest(raw=raw), fixture(test_components.ComponentTests) as case:
                path = case.files["GEN"]
                path.write_bytes(raw)
                archived = case.store.archive("GEN", path.name, True)
                self.assertFalse(path.exists())
                # A fresh store exercises persisted metadata, not an in-memory registry.
                restored = ComponentStore(case.workspace).restore_archive(
                    "GEN", path.name, archived["archive_name"], True)
                self.assertEqual(path.read_bytes(), raw)
                self.assertTrue(restored["integrity_verified"])
                self.assertTrue(restored["validation"]["warnings"] or
                                restored["validation"]["structural_errors"])

    def test_component_repair_can_roll_back_invalid_original(self):
        with fixture(test_components.ComponentTests) as case:
            path = case.files["GEN"]
            original = test_components.GEN.replace("CFuelHor=0.25", "CFuelHor=0").encode()
            path.write_bytes(original)
            repaired = case.store.update("GEN", path.name, {"CFuelHor": "0.25"})
            result = case.store.restore("GEN", path.name, repaired["backup_name"], True)
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(result["integrity_verified"])
            self.assertIn("CFuelHor must be greater than zero", result["validation"]["known_value_errors"])
            with self.assertRaises(ValueError):
                case.store.create("GEN", "invalid.GEN", original.decode())

    def test_component_snapshot_tampering_or_missing_metadata_rejected(self):
        for archived in (False, True):
            for change in ("bytes", "missing", "identity"):
                with self.subTest(archived=archived, change=change), fixture(test_components.ComponentTests) as case:
                    path = case.files["GEN"]
                    original = path.read_bytes()
                    saved = case.store.archive("GEN", path.name, True) if archived else case.store.backup("GEN", path.name)
                    name = saved["archive_name" if archived else "backup_name"]
                    folder = case.workspace / "ComposPV" / (".mcp-archive" if archived else ".mcp-backups") / "GEN"
                    metadata = folder / (name + ".json")
                    if change == "bytes":
                        (folder / name).write_bytes(original.replace(b"CFuelHor=0.25", b"CFuelHor=0.35"))
                    elif change == "missing":
                        metadata.unlink()
                    else:
                        info = json.loads(metadata.read_text())
                        info["name"] = "another.GEN"
                        metadata.write_text(json.dumps(info))
                    with self.assertRaises(ValueError):
                        if archived:
                            case.store.restore_archive("GEN", path.name, name, True)
                        else:
                            case.store.restore("GEN", path.name, name, True)
                    if archived:
                        self.assertFalse(path.exists())
                    else:
                        self.assertEqual(path.read_bytes(), original)

    def test_legacy_component_snapshot_restores_with_unverified_integrity(self):
        with fixture(test_components.ComponentTests) as case:
            path = case.files["GEN"]
            original = path.read_bytes()
            folder = case.store._backup_folder("GEN")
            name = path.name + "." + "a" * 32 + ".bak"
            (folder / name).write_bytes(original)
            result = case.store.restore("GEN", path.name, name, True)
            self.assertFalse(result["integrity_verified"])
            self.assertEqual(path.read_bytes(), original)

    def test_project_archive_enforces_restore_limit_before_moving(self):
        with fixture(test_variants.VariantTests) as case:
            for i in range(1, 128):
                case.original.with_suffix(f".VC{i}").write_bytes(case.original.read_bytes())
            initial = case.store.inspect_project("Example.PRJ")["files"]
            self.assertEqual(len(initial), 129)
            with self.assertRaisesRegex(ValueError, "exceeds 128 members"):
                case.store.archive_project("Example.PRJ", initial, True)
            self.assertEqual(case.store.inspect_project("Example.PRJ")["files"], initial)
            self.assertFalse((case.original.parent / ".mcp-project-archive").exists())
            case.original.with_suffix(".VC127").unlink()
            initial = case.store.inspect_project("Example.PRJ")["files"]
            archived = case.store.archive_project("Example.PRJ", initial, True)
            case.store.restore_archive("Example.PRJ", archived["archive_name"], True)
            self.assertEqual(case.store.inspect_project("Example.PRJ")["files"], initial)

    def test_dotted_projects_are_independent_and_true_sidecars_still_block(self):
        with fixture(test_variants.VariantTests) as case:
            initial = case.store.inspect_project("Example.PRJ")["files"]
            for name in ("Example.East.PRJ", "Example.VC99.PRJ", "Copied.East.PRJ"):
                case.store.clone_project("Example.PRJ", name)
            case.store.clone("Example.VC99.PRJ", "VC0", "VC77")
            self.assertEqual(case.store.inspect_project("Example.PRJ")["files"], initial)
            self.assertEqual(PVsystCLI(sys.executable, case.workspace).list_variants("Example.PRJ"), ["VC0"])
            case.store.clone_project("Example.PRJ", "Copied.PRJ")
            for sidecar in ("Example.SHDP", "Example.VC0.bak"):
                extra = case.original.parent / sidecar
                extra.write_bytes(b"unknown sidecar")
                try:
                    with self.assertRaisesRegex(ValueError, "sidecar"):
                        case.store.inspect_project("Example.PRJ")
                    with self.assertRaisesRegex(ValueError, "sidecar"):
                        case.store.clone_project("Example.PRJ", "Another.PRJ")
                finally:
                    extra.unlink()

    def test_unicode_project_dependencies_share_name_and_path_guards(self):
        with fixture(test_variants.VariantTests) as case:
            name = "光伏项目.东区.PRJ"
            case.store.clone_project("Example.PRJ", name)
            for project in (name, name[:-4]):
                report = case.components.dependencies(project, "vc0")
                self.assertEqual(report["project"], name)
                self.assertEqual({ref["type"] for ref in report["references"]}, {"PAN", "OND", "BTR", "GEN"})
            for bad in ("../光伏.PRJ", "..\\光伏.PRJ", "光伏:ads.PRJ", "bad?.PRJ", "CON.PRJ", "x.PRJ "):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        case.components.dependencies(bad, "VC0")
                    with self.assertRaises(ValueError):
                        case.store.inspect(bad, "VC0")

    def test_structure_missing_or_ambiguous_system_is_not_valid(self):
        base = test_variants.VARIANT.replace("        GInverter=original.OND\n",
                                              "        GInverter=original.OND\n        NoOrientation=1\n")
        missing = re.sub(r"(?ms)^  PVObject_System=pvSystem\n.*?^  End of PVObject pvSystem\n", "", base)
        for data, error in ((missing, "Missing pvSystem"),
                            (base.replace("    SystemType=Battery\n", ""), "exactly one SystemType"),
                            (base.replace("    SystemType=Battery\n", "    SystemType=Grid\n    SystemType=Battery\n"), "exactly one SystemType"),
                            (base.replace("SystemType=Battery", "SystemType="), "must not be empty")):
            with self.subTest(error=error), fixture(test_variants.VariantTests) as case:
                case.original.write_bytes(data.encode())
                result = case.store.validate_structure("Example.PRJ", "VC0")
                self.assertIs(result["valid"], False)
                self.assertFalse(result["checks_complete"])
                self.assertTrue(any(error in message for message in result["issues"]))
        with fixture(test_variants.VariantTests) as case:
            case.original.write_bytes(base.encode())
            result = case.store.validate_structure("Example.PRJ", "VC0")
            self.assertIsNone(result["valid"])
            self.assertEqual(result["issues"], [])
            self.assertTrue(result["unchecked_checks"])
            grid = case._prepare_grid_variant()
            grid = re.sub(r"(?ms)^      PVObject_=pvSubArray\n        SubArrayId=2\n.*?^      End of PVObject pvSubArray\n", "", grid)
            case.original.write_bytes(grid.encode())
            result = case.store.validate_structure("Example.PRJ", "VC0")
            self.assertIs(result["valid"], True)
            self.assertTrue(result["checks_complete"])

    def test_source_restore_rejects_mixed_transactions_and_supports_undo_redo(self):
        with fixture(test_variants.VariantTests) as case:
            initial, _ = case._prepare_project_sources()
            first = case.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", initial["files"])
            site = case.workspace / "Sites" / "New.SIT"
            site.with_name("Next.SIT").write_text(site.read_text().replace("New.SIT", "Next.SIT").replace("New Site", "Next Site"))
            (case.workspace / "Meteo" / "Next.MET").write_bytes(b"next weather")
            second = case.store.update_project_sources("Example.PRJ", "Next.SIT", "Next.MET", first["files"])
            mixed = {**second["backups"], "Example.PRJ": first["backups"]["Example.PRJ"]}
            store = VariantStore(case.workspace, case.components)
            with self.assertRaisesRegex(ValueError, "one complete source transaction"):
                store.restore_project_sources("Example.PRJ", mixed, second["files"], True)
            self.assertEqual(store.inspect_project("Example.PRJ")["files"], second["files"])
            undo = store.restore_project_sources("Example.PRJ", first["backups"], second["files"], True)
            self.assertEqual(undo["files"], initial["files"])
            redo = store.restore_project_sources("Example.PRJ", undo["backups"], undo["files"], True)
            self.assertEqual(redo["files"], second["files"])

    def test_source_restore_checks_manifest_and_every_backup_before_writes(self):
        for change in ("bytes", "missing", "members", "project", "hash"):
            with self.subTest(change=change), fixture(test_variants.VariantTests) as case:
                initial, _ = case._prepare_project_sources()
                saved = case.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", initial["files"])
                folder = case.original.parent / ".mcp-variant-backups"
                manifest_path = folder / saved["transaction_manifest"]
                manifest = json.loads(manifest_path.read_text())
                if change == "bytes":
                    snapshot = folder / saved["backups"]["Example.VC1"]
                    snapshot.write_bytes(snapshot.read_bytes().replace(b"Old.MET", b"Bad.MET"))
                elif change == "missing":
                    manifest_path.unlink()
                else:
                    if change == "members":
                        del manifest["backups"]["Example.VC1"]
                    elif change == "project":
                        manifest["project"] = "Other.PRJ"
                    else:
                        manifest["original_files"]["Example.VC1"] = "0" * 64
                    manifest_path.write_text(json.dumps(manifest))
                before = {p.name for p in folder.iterdir()}
                with self.assertRaises(ValueError):
                    case.store.restore_project_sources("Example.PRJ", saved["backups"], saved["files"], True)
                self.assertEqual(case.store.inspect_project("Example.PRJ")["files"], saved["files"])
                self.assertEqual({p.name for p in folder.iterdir()}, before)

    def test_batch_external_directories_and_targets_rejected_before_cli(self):
        for folder, filename in (("UserHourly", None), ("UserBatch", None),
                                  ("UserHourly", "hourly.csv"), ("UserBatch", "DemoResults.CSV")):
            with self.subTest(folder=folder, filename=filename), fixture(test_pvsyst.PVsystTests) as case:
                case.client._help_cache["run-simulation"] += "\n --batch-params-file|-bpf:"
                batch = case.workspace / "Models" / "DemoParams.CSV"
                batch.write_text("SIM_1;hourly.csv\n")
                external = case.workspace.parent / "external"
                external.mkdir()
                link = case.workspace / folder
                if filename:
                    link.mkdir()
                    link = link / filename
                    target = external / filename
                else:
                    target = external
                try:
                    link.symlink_to(target, target_is_directory=filename is None)
                except OSError:
                    self.skipTest("Symlink creation unavailable")
                with patch.object(case.client, "_run") as command:
                    with self.assertRaisesRegex(ValueError, "outside"):
                        case.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
                    command.assert_not_called()
                self.assertEqual(list(external.iterdir()), [])

    def test_missing_results_explicitly_report_coverage_in_python_and_mcp(self):
        with fixture(test_pvsyst.PVsystTests) as case, patch.object(server, "_cli", case.client):
            path = case.workspace / "Results" / "missing.csv"
            for missing in ("", "NaN", "inf", "text", None):
                with self.subTest(missing=missing):
                    middle = "01/01/90 01:00" + (";" + missing if missing is not None else "")
                    path.write_text("date;E_Grid\n;kW\n01/01/90 00:00;10\n" + middle + "\n01/01/90 02:00;10\n")
                    direct = summarize(path)
                    report = server.pvsyst_read_results(path.name, "E_Grid")
                    state = report["summary"]["E_Grid"]
                    self.assertEqual(direct, {"column": "E_Grid", "rows": report["rows"],
                                              "step_minutes": report["step_minutes"], **state})
                    self.assertEqual((direct["rows"], direct["samples"], direct["missing_samples"]), (3, 2, 1))
                    self.assertAlmostEqual(direct["coverage"], 2 / 3)
                    self.assertFalse(direct["complete"])
                    self.assertIsNone(direct["energy_kwh"])
                    self.assertEqual(direct["observed_energy_kwh"], 20)
                    self.assertEqual((direct["sum"], direct["mean"], direct["max"]), (20, 10, 10))

    def test_all_missing_or_irregular_results_have_no_complete_energy(self):
        scenarios = (("00:00;\n01:00;NaN\n02:00;inf", 0, 0, False, 60),
                     ("00:00;10\n01:00;10\n03:00;10", 3, 30, True, None),
                     ("00:00;10", 1, 10, True, None),
                     ("00:00;10\n00:00;10", 2, 20, True, None))
        with fixture(test_pvsyst.PVsystTests) as case, patch.object(server, "_cli", case.client):
            path = case.workspace / "Results" / "edge.csv"
            for data, samples, total, complete, step in scenarios:
                with self.subTest(data=data):
                    path.write_text("date;E_Grid\n;kW\n" + "\n".join("01/01/90 " + row for row in data.splitlines()) + "\n")
                    direct = summarize(path)
                    report = server.pvsyst_read_results(path.name, "E_Grid")
                    self.assertEqual(direct["samples"], samples)
                    self.assertEqual(direct["sum"], total)
                    self.assertEqual(direct["complete"], complete)
                    self.assertEqual(direct["step_minutes"], step)
                    self.assertIsNone(direct["energy_kwh"])
                    self.assertIsNone(direct["observed_energy_kwh"])
                    self.assertEqual(direct, {"column": "E_Grid", "rows": report["rows"],
                                              "step_minutes": report["step_minutes"], **report["summary"]["E_Grid"]})
                    json.dumps(report, allow_nan=False)

"""Regression tests for verified variants.py review findings (part 2, #3-#9)."""
import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import test_variants
from pvsyst_auto.project.variants import VariantStore
from test_grid import ARRAY, GROUP, grid_text


def temporaries(folder):
    return [path.name for path in folder.iterdir() if path.name.endswith(".tmp")]


class BugfixVariantTests(unittest.TestCase):
    # Reuse the fixtures without importing (and re-running) the VariantTests cases.
    setUp = test_variants.VariantTests.setUp
    _prepare_project_sources = test_variants.VariantTests._prepare_project_sources

    @property
    def projects(self):
        return self.workspace / "Projects"

    def _archived_variant(self):
        self.store.clone("Example.PRJ", "VC0", "VC1")
        files = self.store.inspect_project("Example.PRJ")["files"]
        return files, self.store.archive_variant("Example.PRJ", "VC1", files["Example.VC1"], True)

    def test_clone_failed_write_leaves_no_partial_variant(self):
        # #3: an interrupted write must not leave a partial VC that blocks the name.
        source = self.original.read_bytes()
        opened = Path.open

        class Interrupted:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.stream.close()

            def write(self, data):
                self.stream.write(data[:len(data) // 2])
                raise OSError(28, "injected disk full")

        def interrupt_variant_write(path, mode="r", *args, **kwargs):
            stream = opened(path, mode, *args, **kwargs)
            return Interrupted(stream) if mode == "xb" and "VC1" in path.name else stream

        with patch.object(Path, "open", interrupt_variant_write):
            with self.assertRaisesRegex(OSError, "injected disk full"):
                self.store.clone("Example.PRJ", "VC0", "VC1")
        self.assertFalse((self.projects / "Example.VC1").exists())
        self.assertEqual(temporaries(self.projects), [])
        self.assertEqual(self.store.inspect_project("Example.PRJ")["variants"], ["VC0"])
        clone = self.store.clone("Example.PRJ", "VC0", "VC1")
        self.assertEqual((self.projects / "Example.VC1").read_bytes(), source)
        self.assertEqual(clone["sha256"], hashlib.sha256(source).hexdigest())

    def test_clone_never_overwrites_a_variant_created_meanwhile(self):
        # #3 guard: staging must keep the exclusive-create guarantee of the former "xb" write.
        external = b"saved meanwhile by another process"
        opened = Path.open

        def save_target_first(path, mode="r", *args, **kwargs):
            if mode == "xb" and "VC1" in path.name:
                with opened(self.projects / "Example.VC1", "xb") as stream:
                    stream.write(external)
            return opened(path, mode, *args, **kwargs)

        with patch.object(Path, "open", save_target_first):
            with self.assertRaises(FileExistsError):
                self.store.clone("Example.PRJ", "VC0", "VC1")
        self.assertEqual((self.projects / "Example.VC1").read_bytes(), external)
        self.assertEqual(temporaries(self.projects), [])

    def test_create_project_rollback_continues_after_a_failed_delete(self):
        # #4/#5 pattern in _create_project_files: one failed undo hid the error and stopped.
        project_text = (self.projects / "Example.PRJ").read_text(encoding="utf-8")
        variant_text = self.original.read_bytes().decode("utf-8")
        opened, unlink = Path.open, Path.unlink

        def fail_second_variant(path, mode="r", *args, **kwargs):
            if path.name == "Partial.VC1":
                raise OSError("injected create failure")
            return opened(path, mode, *args, **kwargs)

        def locked_first_variant(path, missing_ok=False):
            if path.name == "Partial.VC0":
                raise PermissionError("injected delete failure")
            return unlink(path, missing_ok)

        with patch.object(Path, "open", fail_second_variant), patch.object(Path, "unlink", locked_first_variant):
            with self.assertRaisesRegex(OSError, "injected create failure") as caught:
                self.store.create_project("Partial.PRJ", project_text,
                                          {"VC0": variant_text, "VC1": variant_text})
        self.assertIn("rollback incomplete for Partial.VC0", str(caught.exception))
        self.assertEqual(str(caught.exception.__cause__), "injected create failure")
        self.assertFalse((self.projects / "Partial.PRJ").exists())
        self.assertTrue((self.projects / "Partial.VC0").exists())

    def test_source_transaction_rollback_continues_after_a_failed_undo(self):
        # #4: undo every written file, keep the original error and report the transaction.
        initial, originals = self._prepare_project_sources()
        names = list(initial["files"])  # the transaction writes in this order
        self.assertEqual(len(names), 3)
        real_replace = os.replace
        replaced = []

        def flaky_replace(source, target):
            name = Path(target).name
            if name == names[1] and name in replaced:
                raise OSError("injected rollback failure")
            replaced.append(name)
            if name == names[2]:
                raise OSError("injected commit failure")
            return real_replace(source, target)

        with patch("pvsyst_auto.project.variants.os.replace", side_effect=flaky_replace):
            with self.assertRaisesRegex(OSError, "injected commit failure") as caught:
                self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", initial["files"])
        manifest, = (self.projects / ".mcp-variant-backups").glob("transaction.*.json")
        transaction_id = manifest.name.split(".")[1]
        self.assertIn(f"rollback incomplete for {names[1]}", str(caught.exception))
        self.assertIn(transaction_id, str(caught.exception))
        self.assertEqual(str(caught.exception.__cause__), "injected commit failure")
        self.assertEqual((self.projects / names[0]).read_bytes(), originals[names[0]])
        self.assertNotEqual((self.projects / names[1]).read_bytes(), originals[names[1]])
        self.assertEqual((self.projects / names[2]).read_bytes(), originals[names[2]])
        self.assertEqual(temporaries(self.projects), [])
        # The reported transaction recovers the file whose undo failed.
        backups = {name: f"{name}.{transaction_id}.bak" for name in names}
        current = self.store.inspect_project("Example.PRJ")["files"]
        restored = self.store.restore_project_sources("Example.PRJ", backups, current, True)
        self.assertEqual(restored["files"], initial["files"])

    def test_archive_project_rollback_continues_after_a_failed_move_back(self):
        # #5: archive_project rollback tries every file and keeps the original error.
        self.store.clone("Example.PRJ", "VC0", "VC1")
        files = self.store.inspect_project("Example.PRJ")["files"]
        names = list(files)  # archive move order
        before = {name: (self.projects / name).read_bytes() for name in names}
        real_replace = os.replace

        def flaky_replace(source, target):
            source, target = Path(source), Path(target)
            if target.parent.parent.name == ".mcp-project-archive" and target.name == names[2]:
                raise OSError("injected archive failure")
            if source.parent.parent.name == ".mcp-project-archive" and source.name == names[1]:
                raise OSError("injected move-back failure")
            return real_replace(source, target)

        with patch("pvsyst_auto.project.variants.os.replace", side_effect=flaky_replace):
            with self.assertRaisesRegex(OSError, "injected archive failure") as caught:
                self.store.archive_project("Example.PRJ", files, True)
        archive, = (self.projects / ".mcp-project-archive").iterdir()
        self.assertIn(f"rollback incomplete for {names[1]}", str(caught.exception))
        self.assertIn(archive.name, str(caught.exception))
        self.assertEqual(str(caught.exception.__cause__), "injected archive failure")
        self.assertEqual((self.projects / names[0]).read_bytes(), before[names[0]])
        self.assertEqual((self.projects / names[2]).read_bytes(), before[names[2]])
        self.assertEqual(sorted(path.name for path in archive.iterdir()), sorted(["manifest.json", names[1]]))
        os.replace(archive / names[1], self.projects / names[1])
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], files)

    def test_archive_variant_cleanup_failure_keeps_the_original_error(self):
        # #5: a failed rmdir while cleaning up must not replace the real failure.
        self.store.clone("Example.PRJ", "VC0", "VC1")
        files = self.store.inspect_project("Example.PRJ")["files"]
        with patch("pvsyst_auto.project.variants.os.replace", side_effect=OSError("injected archive failure")), \
                patch.object(Path, "rmdir", side_effect=OSError("injected rmdir failure")):
            with self.assertRaisesRegex(OSError, "injected archive failure"):
                self.store.archive_variant("Example.PRJ", "VC1", files["Example.VC1"], True)
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], files)

    def test_restore_archive_rollback_continues_after_a_failed_move_back(self):
        # #5: restore_archive rollback tries every file and keeps the original error.
        self.store.clone("Example.PRJ", "VC0", "VC1")
        files = self.store.inspect_project("Example.PRJ")["files"]
        archived = self.store.archive_project("Example.PRJ", files, True)
        names = list(files)  # the manifest order is the restore order
        archive = self.projects / ".mcp-project-archive" / archived["archive_name"]
        real_replace = os.replace

        def flaky_move(source, target):
            source, target = Path(source), Path(target)
            if source.parent.name == archive.name and source.name == names[2]:
                raise OSError("injected restore failure")
            if target.parent.name == archive.name and target.name == names[1]:
                raise OSError("injected move-back failure")
            return real_replace(source, target)

        with patch("pvsyst_auto.project.variants.os.rename", side_effect=flaky_move), \
                patch("pvsyst_auto.project.variants.os.replace", side_effect=flaky_move):
            with self.assertRaisesRegex(OSError, "injected restore failure") as caught:
                self.store.restore_archive("Example.PRJ", archived["archive_name"], True)
        self.assertIn(f"rollback incomplete for {names[1]}", str(caught.exception))
        self.assertIn(archived["archive_name"], str(caught.exception))
        self.assertEqual(str(caught.exception.__cause__), "injected restore failure")
        self.assertEqual(sorted(path.name for path in archive.iterdir()),
                         sorted(["manifest.json", names[0], names[2]]))
        self.assertTrue((self.projects / names[1]).is_file())
        self.assertFalse((self.projects / names[0]).exists())

    @unittest.skipUnless(os.name == "nt", "only a Windows rename refuses an existing destination")
    def test_restore_archive_never_overwrites_a_variant_saved_during_validation(self):
        files, archived = self._archived_variant()
        target = self.projects / "Example.VC1"
        external = b"saved meanwhile by another process"
        check_hash = VariantStore._check_hash

        def save_during_validation(data, expected):
            check_hash(data, expected)
            target.write_bytes(external)

        with patch.object(self.store, "_check_hash", side_effect=save_during_validation):
            with self.assertRaises(FileExistsError):
                self.store.restore_archive("Example.PRJ", archived["archive_name"], True)
        self.assertEqual(target.read_bytes(), external)
        target.unlink()
        self.store.restore_archive("Example.PRJ", archived["archive_name"], True)
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], files)

    def test_restore_archive_reports_missing_and_malformed_manifests_as_value_errors(self):
        # #6: FileNotFoundError, AttributeError and raw decoder errors become clear ValueErrors.
        files, archived = self._archived_variant()
        with self.assertRaisesRegex(ValueError, "not found"):
            self.store.restore_archive("Example.PRJ", "0" * 32, True)
        manifest = self.projects / ".mcp-project-archive" / archived["archive_name"] / "manifest.json"
        valid = manifest.read_bytes()
        for content in (b"[]", b"null", b"{broken", b"\xff"):
            with self.subTest(content=content):
                manifest.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "Invalid archive manifest"):
                    self.store.restore_archive("Example.PRJ", archived["archive_name"], True)
        manifest.unlink()
        with self.assertRaisesRegex(ValueError, "no manifest"):
            self.store.restore_archive("Example.PRJ", archived["archive_name"], True)
        manifest.write_bytes(valid)
        self.store.restore_archive("Example.PRJ", archived["archive_name"], True)
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], files)

    def test_restore_archive_validates_the_archived_project_text(self):
        # #6/#7: the archived PRJ gets the full _project_text check before anything moves.
        self.store.clone("Example.PRJ", "VC0", "VC1")
        files = self.store.inspect_project("Example.PRJ")["files"]
        archived = self.store.archive_project("Example.PRJ", files, True)
        archive = self.projects / ".mcp-project-archive" / archived["archive_name"]
        manifest_path = archive / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        genuine = (archive / "Example.PRJ").read_bytes()
        for broken in (b"", b"\xff", b"PVObject_=pvProject\r\n",
                       genuine.replace(b"End of PVObject pvProject",
                                       b"Comment=a\x00b\r\nEnd of PVObject pvProject")):
            with self.subTest(broken=broken):
                (archive / "Example.PRJ").write_bytes(broken)
                manifest["files"]["Example.PRJ"] = hashlib.sha256(broken).hexdigest()
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Archived project is malformed"):
                    self.store.restore_archive("Example.PRJ", archived["archive_name"], True)
                self.assertEqual(sorted(path.name for path in archive.iterdir()),
                                 sorted(["manifest.json", *files]))
        (archive / "Example.PRJ").write_bytes(genuine)
        manifest["files"]["Example.PRJ"] = files["Example.PRJ"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.store.restore_archive("Example.PRJ", archived["archive_name"], True)
        self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], files)

    def test_source_restore_validates_the_project_snapshot_text(self):
        # #7: the PRJ snapshot gets the same _project_text check as a live PRJ.
        before, _ = self._prepare_project_sources()
        changed = self.store.update_project_sources("Example.PRJ", "New.SIT", "New.MET", before["files"])
        folder = self.projects / ".mcp-variant-backups"
        snapshot = folder / changed["backups"]["Example.PRJ"]
        manifest_path = folder / changed["transaction_manifest"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        genuine = snapshot.read_bytes()
        for broken in (genuine.replace(b"Comment=Old project", b"Comment=Old\x00project"), b"\xff" + genuine):
            with self.subTest(broken=broken[:24]):
                snapshot.write_bytes(broken)
                manifest["original_files"]["Example.PRJ"] = hashlib.sha256(broken).hexdigest()
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Archived project snapshot is malformed"):
                    self.store.restore_project_sources("Example.PRJ", changed["backups"], changed["files"], True)
                self.assertEqual(self.store.inspect_project("Example.PRJ")["files"], changed["files"])
        snapshot.write_bytes(genuine)
        manifest["original_files"]["Example.PRJ"] = hashlib.sha256(genuine).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        restored = self.store.restore_project_sources("Example.PRJ", changed["backups"], changed["files"], True)
        self.assertEqual(restored["files"], before["files"])

    def test_shared_member_removal_rejects_a_non_canonical_port_id(self):
        # #9: a zero-padded port SubArrayId leaked StopIteration from the port lookup.
        leaf = GROUP[GROUP.index("    StringNode Start;"):GROUP.index("  Children=End")]
        port = ("MPPTNode Start;\nSubArrayId=1\nSubArrayName=Array A\nChildren=Start\n" + leaf
                + "Children=End\nMPPTNode End;\n")
        group = ("InverterNode Start;\nNElements=1\nSubArrayId=1\nSubArrayName=Array A\nChildren=Start\n"
                 + port + "Children=End\nInverterNode End;\n")
        array = ARRAY.replace("NInvMPPT=1", "NInvMPPT=2\nVersion=8.1.6\nFlags=$001E0402\n"
                              "GInverter=Shared.OND\nPowerSharingInverterId=0")
        array = (array.replace("NInverter=2", "NInverter=1").replace("NInvMPPT=2", "NInvMPPT=1")
                 .replace("NStringCh=6", "NStringCh=3").replace("NStrOrient1=6", "NStrOrient1=3")
                 .replace("PowerSharingInverterId=0", "PowerSharingInverterId=-1"))
        text = grid_text(group, array).replace("End of PVObject pvVCalcul",
            "PVObject_Ombrage=pvShading\nFlags=$00\nEnd of PVObject pvShading\nEnd of PVObject pvVCalcul")
        self.original.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode())
        digest = self.store.inspect("Example.PRJ", "VC0")["sha256"]
        self.assertTrue(self.store.clone_subarray("Example.PRJ", "VC0", 1, digest,
                                                  share_inverter=True)["shared_parent_created"])
        circuit, rest = self.original.read_bytes().split(b"End of PVObject pvCircuit", 1)
        padded = circuit.replace(b"SubArrayId=2\r\n", b"SubArrayId=02\r\n") + b"End of PVObject pvCircuit" + rest
        self.assertEqual(padded.count(b"SubArrayId=02\r\n"), 2)
        self.original.write_bytes(padded)
        digest = self.store.inspect("Example.PRJ", "VC0")["sha256"]
        with self.assertRaisesRegex(ValueError, "direct port"):
            self.store.remove_subarray("Example.PRJ", "VC0", 2, digest, shared_member=True)
        self.assertEqual(self.original.read_bytes(), padded)


if __name__ == "__main__":
    unittest.main()

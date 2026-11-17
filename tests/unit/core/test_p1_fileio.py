"""Direct tests for the shared atomic-write helpers in pvsyst_auto.project.fileio."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.project import fileio, paths


class FileioTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "Example.VC0"

    def names(self):
        """Every entry in the folder, so a leftover temporary copy of any name is caught."""
        return sorted(path.name for path in self.root.iterdir())

    def temporaries(self):
        return [path.name for path in self.root.iterdir() if path.name.endswith(".tmp")]

    def test_stage_writes_a_hidden_sibling_and_removes_it_after_a_failed_write(self):
        staged = fileio.stage(self.path, b"candidate")
        self.assertEqual(staged.parent, self.root)
        self.assertTrue(staged.name.startswith(".Example.VC0.") and staged.name.endswith(".tmp"))
        self.assertEqual(staged.read_bytes(), b"candidate")
        staged.unlink()
        with patch.object(fileio.os, "fsync", side_effect=OSError("injected flush failure")):
            with self.assertRaisesRegex(OSError, "injected flush failure"):
                fileio.stage(self.path, b"candidate")
        self.assertEqual(self.names(), [])

    def test_replace_checked_replaces_an_unchanged_file(self):
        self.path.write_bytes(b"inspected")
        events = []

        def read(path):
            events.append("read")
            return path.read_bytes()

        def prepare():
            events.append("prepare")
            return "backup-name"

        def verify():
            # Runs after the second re-read, with the candidate already staged.
            events.append("verify")
            self.assertEqual(len(self.temporaries()), 1)

        result = fileio.replace_checked(self.path, b"inspected", b"candidate", read, "changed",
                                        prepare=prepare, verify=verify)
        self.assertEqual(result, "backup-name")
        self.assertEqual(events, ["read", "prepare", "read", "verify"])
        self.assertEqual(self.path.read_bytes(), b"candidate")
        self.assertEqual(self.names(), ["Example.VC0"])

    def test_replace_checked_refuses_a_file_changed_before_staging(self):
        self.path.write_bytes(b"saved by another process")
        prepared = []
        with self.assertRaisesRegex(ValueError, "^Variant changed while preparing the edit$"):
            fileio.replace_checked(self.path, b"inspected", b"candidate", Path.read_bytes,
                                   "Variant changed while preparing the edit",
                                   prepare=lambda: prepared.append(True))
        # Nothing is staged and no backup is prepared for a file that already changed.
        self.assertEqual(prepared, [])
        self.assertEqual(self.path.read_bytes(), b"saved by another process")
        self.assertEqual(self.names(), ["Example.VC0"])

    def test_replace_checked_refuses_a_file_changed_after_staging(self):
        self.path.write_bytes(b"inspected")
        reads = []

        def read(path):
            reads.append(self.temporaries())
            if len(reads) == 2:
                path.write_bytes(b"saved after staging")
            return path.read_bytes()

        with self.assertRaisesRegex(ValueError, "^Component changed while preparing the edit$"):
            fileio.replace_checked(self.path, b"inspected", b"candidate", read,
                                   "Component changed while preparing the edit")
        self.assertEqual(len(reads), 2)
        self.assertEqual(reads[0], [])
        self.assertEqual(len(reads[1]), 1)  # the candidate was staged before the re-read
        self.assertEqual(self.path.read_bytes(), b"saved after staging")
        self.assertEqual(self.names(), ["Example.VC0"])

    def test_replace_checked_verify_can_abort_after_staging(self):
        self.path.write_bytes(b"inspected")

        def verify():
            raise ValueError("Generator component changed while preparing the edit")

        with self.assertRaisesRegex(ValueError, "Generator component changed"):
            fileio.replace_checked(self.path, b"inspected", b"candidate", Path.read_bytes, "changed",
                                   verify=verify)
        self.assertEqual(self.path.read_bytes(), b"inspected")
        self.assertEqual(self.names(), ["Example.VC0"])

    def test_atomic_replace_creates_or_replaces_and_cleans_up_after_a_failed_rename(self):
        fileio.atomic_replace(self.path, b"first")
        self.assertEqual(self.path.read_bytes(), b"first")
        fileio.atomic_replace(self.path, b"second")
        self.assertEqual(self.path.read_bytes(), b"second")
        with patch.object(fileio.os, "replace", side_effect=PermissionError("injected lock")):
            with self.assertRaisesRegex(PermissionError, "injected lock"):
                fileio.atomic_replace(self.path, b"third")
        self.assertEqual(self.path.read_bytes(), b"second")
        self.assertEqual(self.names(), ["Example.VC0"])

    def test_publish_new_creates_the_file_and_refuses_an_existing_target(self):
        fileio.publish_new(self.path, b"created")
        self.assertEqual(self.path.read_bytes(), b"created")
        with self.assertRaises(FileExistsError):
            fileio.publish_new(self.path, b"overwrite attempt")
        self.assertEqual(self.path.read_bytes(), b"created")
        self.assertEqual(self.names(), ["Example.VC0"])

    def test_publish_new_never_overwrites_a_target_created_while_staging(self):
        stage = fileio.stage

        def stage_then_save_target(path, data):
            staged = stage(path, data)
            path.write_bytes(b"saved meanwhile by another process")
            return staged

        with patch.object(fileio, "stage", side_effect=stage_then_save_target):
            with self.assertRaises(FileExistsError):
                fileio.publish_new(self.path, b"candidate")
        self.assertEqual(self.path.read_bytes(), b"saved meanwhile by another process")
        self.assertEqual(self.names(), ["Example.VC0"])

    def test_publish_new_failed_write_leaves_no_partial_file(self):
        with patch.object(fileio.os, "fsync", side_effect=OSError(28, "injected disk full")):
            with self.assertRaisesRegex(OSError, "injected disk full"):
                fileio.publish_new(self.path, b"candidate")
        self.assertEqual(self.names(), [])
        fileio.publish_new(self.path, b"candidate")  # the name is not blocked
        self.assertEqual(self.path.read_bytes(), b"candidate")

    def test_write_json_first_save_is_exclusive(self):
        target = self.root / "Results" / "task.json"
        fileio.write_json(target, {"phase": "initialized"}, first=True)
        with self.assertRaises(FileExistsError):
            fileio.write_json(target, {"phase": "other task"}, first=True)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"phase": "initialized"})
        self.assertEqual([path.name for path in target.parent.iterdir()], ["task.json"])

    def test_write_json_replaces_atomically_with_a_flushed_copy(self):
        target = self.root / "task.json"
        fileio.write_json(target, {"phase": "initialized"}, first=True)
        events = []
        fsync, replace = os.fsync, os.replace

        def flushed(descriptor):
            events.append("fsync")
            fsync(descriptor)

        def replaced(source, destination):
            events.append("replace")
            replace(source, destination)

        with patch.object(fileio.os, "fsync", side_effect=flushed), \
                patch.object(fileio.os, "replace", side_effect=replaced):
            fileio.write_json(target, {"phase": "completed", "note": "café"})
        self.assertEqual(events, ["fsync", "replace"])
        raw = target.read_bytes()
        self.assertEqual(raw, (json.dumps({"phase": "completed", "note": "café"}, ensure_ascii=True,
                                          indent=2) + "\n").encode("ascii"))
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(self.names(), ["task.json"])

    def test_write_json_failure_keeps_the_last_complete_checkpoint(self):
        target = self.root / "task.json"
        fileio.write_json(target, {"phase": "initialized"}, first=True)
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, "not JSON compliant"):
            fileio.write_json(target, {"ratio": float("nan")})
        with patch.object(fileio.os, "replace", side_effect=PermissionError("fixture: manifest is locked")):
            with self.assertRaisesRegex(PermissionError, "manifest is locked"):
                fileio.write_json(target, {"phase": "completed"})
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.names(), ["task.json"])

    def test_paths_keeps_exporting_write_json(self):
        self.assertIs(paths.write_json, fileio.write_json)


if __name__ == "__main__":
    unittest.main()

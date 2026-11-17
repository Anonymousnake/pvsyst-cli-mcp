"""Tests for atomic file staging, checked replacement, and JSON persistence."""
from __future__ import annotations

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
        return sorted(path.name for path in self.root.iterdir())

    def temporaries(self):
        return [path.name for path in self.root.iterdir() if path.name.endswith(".tmp")]

    def test_stage_cleans_up_on_fsync_failure(self):
        staged = fileio.stage(self.path, b"candidate")
        self.assertEqual(staged.parent, self.root)
        self.assertTrue(staged.name.startswith(".Example.VC0.") and staged.name.endswith(".tmp"))
        self.assertEqual(staged.read_bytes(), b"candidate")
        staged.unlink()
        with patch.object(fileio.os, "fsync", side_effect=OSError("injected flush failure")):
            with self.assertRaisesRegex(OSError, "injected flush failure"):
                fileio.stage(self.path, b"candidate")
        self.assertEqual(self.names(), [])

    def test_replace_checked_success_and_stale_abort(self):
        self.path.write_bytes(b"inspected")
        events = []

        def read(path):
            events.append(("read", len(self.temporaries())))
            return path.read_bytes()

        result = fileio.replace_checked(
            self.path, b"inspected", b"candidate", read, "changed",
            prepare=lambda: events.append(("prepare", 0)) or "backup-name",
            verify=lambda: events.append(("verify", len(self.temporaries()))),
        )
        self.assertEqual(result, "backup-name")
        self.assertEqual(events, [("read", 0), ("prepare", 0), ("read", 1), ("verify", 1)])
        self.assertEqual(self.path.read_bytes(), b"candidate")
        self.assertEqual(self.names(), ["Example.VC0"])

        self.path.write_bytes(b"saved by another process")
        prepared = []
        with self.assertRaisesRegex(ValueError, "^Variant changed while preparing the edit$"):
            fileio.replace_checked(self.path, b"inspected", b"candidate", Path.read_bytes,
                                   "Variant changed while preparing the edit",
                                   prepare=lambda: prepared.append(True))
        self.assertEqual(prepared, [])

        self.path.write_bytes(b"inspected")
        reads = []

        def racing_read(path):
            reads.append(self.temporaries())
            if len(reads) == 2:
                path.write_bytes(b"saved after staging")
            return path.read_bytes()

        with self.assertRaisesRegex(ValueError, "^Component changed while preparing the edit$"):
            fileio.replace_checked(self.path, b"inspected", b"candidate", racing_read,
                                   "Component changed while preparing the edit")
        self.assertEqual(self.path.read_bytes(), b"saved after staging")

        self.path.write_bytes(b"inspected")
        with self.assertRaisesRegex(ValueError, "Generator component changed"):
            fileio.replace_checked(self.path, b"inspected", b"candidate", Path.read_bytes, "changed",
                                   verify=lambda: (_ for _ in ()).throw(ValueError("Generator component changed")))
        self.assertEqual(self.path.read_bytes(), b"inspected")
        self.assertEqual(self.names(), ["Example.VC0"])

    def test_atomic_replace_and_lock_rollback(self):
        fileio.atomic_replace(self.path, b"first")
        self.assertEqual(self.path.read_bytes(), b"first")
        fileio.atomic_replace(self.path, b"second")
        self.assertEqual(self.path.read_bytes(), b"second")
        with patch.object(fileio.os, "replace", side_effect=PermissionError("injected lock")):
            with self.assertRaisesRegex(PermissionError, "injected lock"):
                fileio.atomic_replace(self.path, b"third")
        self.assertEqual(self.path.read_bytes(), b"second")
        self.assertEqual(self.names(), ["Example.VC0"])

    def test_publish_new_exclusive_and_race_safe(self):
        with patch.object(fileio.os, "fsync", side_effect=OSError(28, "injected disk full")):
            with self.assertRaisesRegex(OSError, "injected disk full"):
                fileio.publish_new(self.path, b"candidate")
        self.assertEqual(self.names(), [])

        fileio.publish_new(self.path, b"created")
        self.assertEqual(self.path.read_bytes(), b"created")
        with self.assertRaises(FileExistsError):
            fileio.publish_new(self.path, b"overwrite attempt")

        target = self.root / "Race.VC0"
        stage = fileio.stage

        def stage_then_write(path, data):
            staged = stage(path, data)
            path.write_bytes(b"saved meanwhile")
            return staged

        with patch.object(fileio, "stage", side_effect=stage_then_write):
            with self.assertRaises(FileExistsError):
                fileio.publish_new(target, b"candidate")
        self.assertEqual(target.read_bytes(), b"saved meanwhile")

    def test_write_json_exclusive_atomic_and_failure_preserves_checkpoint(self):
        self.assertIs(paths.write_json, fileio.write_json)
        target = self.root / "Results" / "task.json"
        fileio.write_json(target, {"phase": "initialized"}, first=True)
        with self.assertRaises(FileExistsError):
            fileio.write_json(target, {"phase": "other task"}, first=True)

        events = []
        fsync, replace = os.fsync, os.replace
        with patch.object(fileio.os, "fsync", side_effect=lambda fd: (events.append("fsync"), fsync(fd))[1]), \
                patch.object(fileio.os, "replace", side_effect=lambda s, d: (events.append("replace"), replace(s, d))[1]):
            fileio.write_json(target, {"phase": "completed", "note": "caf\u00e9"})
        self.assertEqual(events, ["fsync", "replace"])
        raw = target.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(json.loads(raw.decode("ascii")), {"phase": "completed", "note": "caf\u00e9"})

        with self.assertRaisesRegex(ValueError, "not JSON compliant"):
            fileio.write_json(target, {"ratio": float("nan")})
        with patch.object(fileio.os, "replace", side_effect=PermissionError("fixture: manifest is locked")):
            with self.assertRaisesRegex(PermissionError, "manifest is locked"):
                fileio.write_json(target, {"phase": "failed"})
        self.assertEqual(target.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()

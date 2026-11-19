"""Workspace settings regressions; no native launch or real profile INI reads."""
from __future__ import annotations

import codecs
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.native import _workspace_ini as ini


class WorkspaceIniTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "PVsystCLI" / "8.1" / "Admin" / "PVsystCLI.ini"
        self.path.parent.mkdir(parents=True)
        self.executable = Path(directory.name) / "PVsyst8.1.7" / "PVsystCLI.exe"
        for mock in (patch.object(ini, "_profile_ini", return_value=self.path),
                     patch.object(ini, "_is_supported", return_value=True)):
            mock.start()
            self.addCleanup(mock.stop)
        lock = patch.object(ini, "_profile_lock", side_effect=lambda _: nullcontext())
        self.lock = lock.start()
        self.addCleanup(lock.stop)

    def test_merge_utf16_fields_on_native_timeout(self):
        def raw(text):
            return codecs.BOM_UTF16_LE + text.encode("utf-16-le")

        self.path.write_bytes(raw("[System]\r\nDataPath=old-é\r\nNRun=1\r\n"
                                  "[RecentWorkspaces]\r\nWorkspace3=空\r\nWorkspace1=old"))
        native = (";新 setting\r\n[System]\r\nNRun=2\r\n  DATAPATH =new\r\nLastDate=today\n"
                  "[RecentWorkspaces]\nWorkspace1=new\r\n; native comment\r\n"
                  "Workspace2=temp\r\nWorkspace4=temp\r\nWorkspaceMax=9\r\n"
                  "[Other]\r\nDataPath=untouched\r\nWorkspace1=untouched")
        with self.assertRaises(subprocess.TimeoutExpired):
            with ini.preserve_workspace_settings(self.executable, restore=True):
                self.path.write_bytes(raw(native))
                raise subprocess.TimeoutExpired("fake native launch", 10)
        expected = (native.replace("  DATAPATH =new\r\n", "DataPath=old-é\r\n")
                    .replace("Workspace1=new\r\n", "Workspace3=空\r\n")
                    .replace("Workspace2=temp\r\nWorkspace4=temp\r\n", "Workspace1=old\r\n"))
        self.assertEqual(self.path.read_bytes(), raw(expected))
        self.lock.assert_called_once_with(self.path)

    def test_first_launch_removes_only_new_workspace_fields(self):
        before = b"[System]\r\nNRun=1\r\nDataPath=temp\r\nLastDate=today\r\n"
        recent = b"[RecentWorkspaces]\r\nWorkspace1=temp\r\nOpaque=\x81\r\n"
        self.assertFalse(self.path.exists())
        with ini.preserve_workspace_settings(self.executable, restore=True):
            self.path.write_bytes(codecs.BOM_UTF8 + before + recent)
        self.assertEqual(self.path.read_bytes(), codecs.BOM_UTF8
                         + before.replace(b"DataPath=temp\r\n", b"")
                         + recent.replace(b"Workspace1=temp\r\n", b""))

    def test_restore_target_fields_when_native_removes_file(self):
        self.path.write_bytes(b"[System]\r\nDataPath=old\r\nNRun=2\r\n"
                              b"[RecentWorkspaces]\r\nWorkspace1=old")
        with ini.preserve_workspace_settings(self.executable, restore=True):
            self.path.unlink()
        self.assertEqual(self.path.read_bytes(), b"[System]\r\nDataPath=old\r\n"
                                                 b"[RecentWorkspaces]\r\nWorkspace1=old")

    def test_opt_out_takes_same_lock_without_touching_ini(self):
        with patch.object(ini, "_read_ini", side_effect=AssertionError("unexpected INI access")):
            with ini.preserve_workspace_settings(self.executable, restore=False):
                self.assertFalse(self.path.exists())
        self.lock.assert_called_once_with(self.path)

    def test_failed_restore_keeps_current_file_and_removes_staging_file(self):
        self.path.write_bytes(b"[System]\r\nDataPath=old\r\n")
        native = b"[System]\r\nDataPath=temp\r\nNRun=2\r\n"
        with patch.object(ini.os, "replace", side_effect=PermissionError("fixture locked")):
            with self.assertRaisesRegex(ini.WorkspaceSettingsError, "Could not restore"):
                with ini.preserve_workspace_settings(self.executable, restore=True):
                    self.path.write_bytes(native)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(self.path.read_bytes(), native)

    def test_recheck_does_not_overwrite_a_concurrent_external_edit(self):
        old = b"[System]\r\nDataPath=old\r\n"
        native, external = old.replace(b"old", b"temp"), old + b"NRun=3\r\n"
        self.path.write_bytes(old)
        replace = ini.atomic_replace

        def external_save(path, data, verify):
            path.write_bytes(external)
            replace(path, data, verify)

        with patch.object(ini, "atomic_replace", side_effect=external_save):
            with self.assertRaises(ini.WorkspaceSettingsError):
                with ini.preserve_workspace_settings(self.executable, restore=True):
                    self.path.write_bytes(native)
        self.assertEqual(self.path.read_bytes(), external)


@unittest.skipUnless(os.name == "nt", "Windows profile coordination")
class WorkspaceIniWindowsTests(unittest.TestCase):
    def test_native_version_gate_and_profile_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "PVsystCLI.ini"
            fake = root / "PVsystCLI.exe"
            fake.write_bytes(b"not a PE resource")
            with patch.object(ini, "_profile_ini", side_effect=AssertionError("unexpected profile access")):
                with ini.preserve_workspace_settings(fake, restore=True):
                    pass
                with patch.object(ini, "_file_version", return_value=(8, 1, 7, 49383)):
                    with ini.preserve_workspace_settings(fake, restore=True):
                        pass

            child = """
import sys
from pathlib import Path
from pvsyst_auto.native import _workspace_ini as ini
ini._LOCK_TIMEOUT_MS = 40
path = Path(sys.argv[1])
try:
    with ini._profile_lock(path):
        raise AssertionError('same-profile lock was not held')
except ini.WorkspaceSettingsError as exc:
    assert 'Timed out' in str(exc), exc
with ini._profile_lock(path.with_name('another-profile.ini')):
    pass
"""
            env = dict(os.environ, PYTHONPATH=str(Path(ini.__file__).resolve().parents[2]))
            with patch.object(ini, "_profile_ini", return_value=profile):
                for version in ((8, 1, 6, 49278), (8, 1, 7, 49382)):
                    with self.subTest(version=version), patch.object(ini, "_file_version", return_value=version):
                        with ini.preserve_workspace_settings(fake, restore=False):
                            subprocess.run([sys.executable, "-c", child, str(profile)], check=True, timeout=10,
                                           env=env)
            with ini._profile_lock(profile):
                self.assertFalse(profile.exists())

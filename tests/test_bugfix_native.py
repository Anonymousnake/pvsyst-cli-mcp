"""Regressions for verified native adapter defects; no PVsyst process, GUI or Win32 call."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pvsyst_auto import results
from pvsyst_auto.native import dc as native_dc
from pvsyst_auto.native import grid_limit
from pvsyst_auto.native import load as native_load
from pvsyst_auto.native import project as native_project
from pvsyst_auto.native import scene as native_scene
from pvsyst_auto.native.cli import PVsystCLI, PVsystError, build_sfi
from pvsyst_auto.project import paths

CSV_TEXT = """date;E_Grid;PR
    ;kW;ratio
01/01/90 00:00;0;0
01/01/90 01:00;4.5;0.8
"""
CONFIRM = "\u786e\u5b9a"


class CLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace"
        for folder in ("Projects", "Models", "Results"):
            (self.workspace / folder).mkdir(parents=True)
        (self.workspace / "Projects" / "Example.PRJ").write_text("example", encoding="utf-8")
        (self.workspace / "Projects" / "Example.VC0").write_text("variant", encoding="utf-8")
        self.sfi = build_sfi(self.workspace / "Models" / "demo.sfi", "energy")
        self.client = PVsystCLI(sys.executable, self.workspace)
        self.client._help_cache["run-simulation"] = "--input-sfi-file|-isf:\n --batch-params-file|-bpf:"

    def completed(self, *args):
        return subprocess.CompletedProcess([], 0, "Simulation done in 0min 1sec", "")

    def test_timeouts_become_pvsyst_errors_without_the_command_line(self):
        timeout = subprocess.TimeoutExpired(
            ["PVsystCLI", "-w:C:/secret-workspace", "-k:private-key"], 5,
            output="partial line\nHost ID: fixture-secret")
        target = self.workspace / "Results" / "new.csv"
        calls = {"version": self.client.version,
                 "license_info": self.client.license_info,
                 "command_help": lambda: self.client.command_help("convert-meteo"),
                 "export_logs": lambda: self.client.export_logs(self.workspace / "Results"),
                 "run_simulation": lambda: self.client.run_simulation(
                     "Example.PRJ", "VC0", sfi=self.sfi, out_csv=target)}
        for name, call in calls.items():
            with self.subTest(name), mock.patch.object(self.client, "_run", side_effect=timeout):
                with self.assertRaises(PVsystError) as caught:
                    call()
                message = str(caught.exception)
                self.assertIn("timed out", message)
                for secret in ("private-key", "secret-workspace", "fixture-secret"):
                    self.assertNotIn(secret, message + caught.exception.diagnostics)
                self.assertIn("partial line", caught.exception.diagnostics)

    def test_empty_version_output_is_a_cli_error_and_a_bridge_stop(self):
        for stdout, stderr in (("", ""), (None, None), ("  \n", "\r\n")):
            with self.subTest(stdout=stdout), mock.patch.object(
                    self.client, "_run", return_value=subprocess.CompletedProcess([], 0, stdout, stderr)):
                with self.assertRaisesRegex(PVsystError, "no output"):
                    self.client.version()
                # The GUI bridges receive a NativeBridgeError result, not an escaping IndexError.
                with self.assertRaisesRegex(native_scene.NativeBridgeError, "version query"):
                    native_scene._check_cli(self.client)
        with mock.patch.object(self.client, "_run", return_value=subprocess.CompletedProcess(
                [], 0, "\nPVsystCLI 8.1.6 build 49278\n", "")):
            self.assertEqual(self.client.version(), "PVsystCLI 8.1.6 build 49278")

    def test_variant_ids_match_case_insensitively_and_run_canonically(self):
        target = self.workspace / "Results" / "new.csv"

        def run(*args):
            self.assertIn("-v:VC0", args)
            target.write_text(CSV_TEXT, encoding="utf-8")
            return self.completed()

        for disk, requested in (("VC0", "vc0"), ("vc0", "VC0")):
            with self.subTest(disk=disk, requested=requested):
                for old in self.workspace.joinpath("Projects").glob("Example.[Vv][Cc]0"):
                    old.unlink()
                (self.workspace / "Projects" / f"Example.{disk}").write_text("variant", encoding="utf-8")
                target.unlink(missing_ok=True)
                self.assertEqual(self.client.list_variants("Example.PRJ"), ["VC0"])
                with mock.patch.object(self.client, "_run", side_effect=run) as command:
                    self.client.run_simulation("Example.PRJ", requested, sfi=self.sfi, out_csv=target)
                    command.assert_called_once()
        target.unlink()
        with mock.patch.object(self.client, "_run") as command:
            with self.assertRaisesRegex(ValueError, "must exist"):
                self.client.run_simulation("Example.PRJ", "VC9", sfi=self.sfi, out_csv=target)
            command.assert_not_called()

    def test_batch_accepts_only_the_summary_named_after_its_parameters(self):
        batch_dir, hourly_dir = self.workspace / "UserBatch", self.workspace / "UserHourly"
        batch_dir.mkdir()
        hourly_dir.mkdir()
        batch = batch_dir / "batch_params.CSV"
        batch.write_text("Ident;Create hourly\nSIM_1;one.CSV\n", encoding="utf-8")

        def writes(summary):
            def run(*args):
                (batch_dir / summary).write_text("Ident;Create hourly;Error;E_Grid;PR\n"
                                                 "SIM_1;one.CSV;;12;0.7\n", encoding="utf-8")
                (hourly_dir / "one.CSV").write_text(CSV_TEXT, encoding="utf-8")
                return self.completed()
            return run

        with mock.patch.object(self.client, "_run", side_effect=writes("other_Results.CSV")):
            with self.assertRaisesRegex(PVsystError, "batch_Results.CSV"):
                self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
        (hourly_dir / "one.CSV").unlink()
        with mock.patch.object(self.client, "_run", side_effect=writes("batch_Results.CSV")):
            result = self.client.run_simulation("Example.PRJ", "VC0", batch_params=batch)
        self.assertEqual(Path(result["batch_summary"]).name, "batch_Results.CSV")
        self.assertEqual(result["batch_runs"], 1)

    def test_project_listing_stays_inside_the_workspace(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "Leaked.PRJ").write_text("outside", encoding="utf-8")
        projects = self.workspace / "Projects"
        for item in projects.iterdir():
            item.unlink()
        projects.rmdir()
        try:
            os.symlink(outside, projects, target_is_directory=True)
        except OSError:
            try:
                import _winapi
                _winapi.CreateJunction(str(outside), str(projects))
            except (ImportError, OSError):
                self.skipTest("Directory links are unavailable on this host")
        self.addCleanup(os.rmdir if sys.platform == "win32" else os.unlink, projects)
        with self.assertRaisesRegex(ValueError, "outside workspace"):
            self.client.list_projects()


class PathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_project_root_compares_resolved_workspaces(self):
        workspace = self.root / "workspace"
        (workspace / "Projects").mkdir(parents=True)
        spelled = workspace / "Projects" / ".."
        self.assertEqual(paths.project_root(spelled), (workspace / "Projects").resolve())
        self.assertEqual(paths.project_path(spelled, "Example.PRJ", "vc1").name, "Example.VC1")

    def test_write_json_persists_before_the_atomic_replace(self):
        target = self.root / "task.json"
        paths.write_json(target, {"phase": "initialized"}, first=True)
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(descriptor):
            events.append("fsync")
            real_fsync(descriptor)

        def replace(source, destination):
            events.append("replace")
            real_replace(source, destination)

        with mock.patch.object(paths.os, "fsync", side_effect=fsync), \
                mock.patch.object(paths.os, "replace", side_effect=replace):
            paths.write_json(target, {"phase": "completed"})
        self.assertEqual(events, ["fsync", "replace"])
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"phase": "completed"})


class ResultTests(unittest.TestCase):
    def test_compare_rejects_a_bare_string_column_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a.csv", "b.csv"):
                (root / name).write_text("date;P;R\n;kW;kW\n01/01/90 00:00;1;2\n01/01/90 01:00;3;4\n",
                                         encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "distinct numeric columns"):
                results.compare_results(root / "a.csv", root / "b.csv", "PR")
            compared = results.compare_results(root / "a.csv", root / "b.csv", ("P", "R"))
            self.assertEqual(compared["columns"], ["P", "R"])


GRID_LINES = ["PVObject_=pvVCalcul\r\n", "  Version=8.1.6\r\n", "PVObject_System=pvSystem\r\n",
              "  Flags=$04000000\r\n", "  SystemType=Grid\r\n", "End of PVObject pvSystem\r\n",
              "End of PVObject pvVCalcul\r\n"]


class EditorTests(unittest.TestCase):
    def test_grid_limit_commits_only_after_its_recheck(self):
        lines = list(GRID_LINES)
        real = grid_limit._read
        calls = []

        def failing_recheck(current, bounds):
            calls.append(bounds)
            if len(calls) == 2:
                raise ValueError("simulated recheck failure")
            return real(current, bounds)

        with mock.patch.object(grid_limit, "_read", side_effect=failing_recheck):
            with self.assertRaisesRegex(ValueError, "simulated recheck failure"):
                grid_limit.replace_grid_limit(lines, (2, 5), {"mode": "constant", "limit_kw": 12.5})
        self.assertEqual(lines, GRID_LINES)
        change = grid_limit.replace_grid_limit(lines, (2, 5), {"mode": "constant", "limit_kw": 12.5})
        self.assertEqual(change["after"]["limit_kw"], 12.5)
        self.assertIn("  PLimGrid=12.5\r\n", lines)
        grid_limit.replace_grid_limit(lines, (2, 7), {"mode": "none"})
        self.assertEqual(lines, GRID_LINES)

    def test_load_scope_reports_a_missing_root_block_as_value_error(self):
        for call in (lambda: native_load._root_block([]),
                     lambda: native_load._root_block(["  Comment=orphan\r\n"]),
                     lambda: native_load._block_of(GRID_LINES, (20, 25))):
            with self.assertRaisesRegex(ValueError, "no root PVObject block"):
                call()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SimpleNamespace(
                lock=threading.RLock(), _check_project=lambda project: None,
                _path=lambda project, variant=None: root / (f"Example.{variant}" if variant else project),
                _read=lambda path: b"  Comment=no root object\r\n",
                _text=lambda data: (data.decode(), data.decode().splitlines(keepends=True)),
                _sections=lambda lines: ({}, None))
            result = native_load.create_native_load_variant(
                store, project="Example.PRJ", source_variant="VC0", new_variant="VC1",
                load={"mode": "constant", "constant_kw": 1.0})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["issues"], ["Project file has no root PVObject block"])


class SceneTests(unittest.TestCase):
    def test_import_returns_a_result_when_the_version_query_fails(self):
        cli = SimpleNamespace(cli=sys.executable, version=mock.Mock(
            side_effect=PVsystError("PVsystCLI version query timed out after 5s")))
        store = SimpleNamespace(lock=threading.RLock(), _validate_display_name=lambda value, label: None)
        result = native_scene.import_house_pv_scene(store, None, cli, "House.pvc2", "Imported")
        self.assertEqual(result["status"], "needs_attention")
        self.assertIn("timed out", result["issues"][0])
        self.assertEqual(result["native_actions"], [])

    def test_save_new_polls_for_the_new_variant_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            projects = workspace / "Projects"
            projects.mkdir()
            (projects / "House.PRJ").write_text("project", encoding="utf-8")
            (projects / "House.VC0").write_text("source", encoding="utf-8")
            created = projects / "House.VC1"
            sleeps = []

            def late_write(seconds):
                # PVsyst finishes writing only after its project form has returned.
                sleeps.append(seconds)
                created.write_text("saved", encoding="utf-8")

            handles = {"TProjectForm": 1, "TSaveObjPVForm": 2, "TMessageForm": 3}
            with mock.patch.multiple(
                    native_scene, time=SimpleNamespace(monotonic=time.monotonic, sleep=late_write),
                    _ini_datapath=mock.Mock(return_value=workspace), _toolbar_save=mock.Mock(),
                    _wait=mock.Mock(side_effect=lambda cls, **kwargs: handles[cls]),
                    _kids=mock.Mock(return_value=[20]), _class_of=mock.Mock(return_value="TEdit"),
                    _describe=mock.Mock(return_value={"enabled": True}), _click=mock.Mock(),
                    _set_text=mock.Mock(), _find=mock.Mock(return_value=30),
                    _string_of=mock.Mock(return_value=native_scene.CAP_INFO)):
                self.assertEqual(native_scene._save_new("House.PRJ", "Saved name"), created)
            self.assertTrue(sleeps)

    def test_close_scene_confirms_on_the_current_shadings_window(self):
        waits = iter([("TPerspectiveForm", 1), ("TShadingsForm", 2), ("TShdFactorTableForm", 3),
                      ("TShadingsForm", 22), ("TProjectForm", 4)])

        def wait(cls, **kwargs):
            expected, handle = next(waits)
            self.assertEqual(cls, expected)
            return handle

        found = []

        def find(parent, cls, text=None):
            found.append((parent, text))
            return parent

        with mock.patch.multiple(native_scene, _wait=mock.Mock(side_effect=wait),
                                 _find=mock.Mock(side_effect=find), _click=mock.Mock(),
                                 _post_close=mock.Mock()):
            native_scene._close_scene()
        self.assertEqual(found[-1], (22, CONFIRM))


class _OwnedGui:
    """Recorded owned-load window table; `ready_lists` load forms exist after the row click."""
    NativeBridgeError = native_scene.NativeBridgeError
    VK_RIGHT, VK_RETURN = native_scene.VK_RIGHT, native_scene.VK_RETURN

    def __init__(self, project, ready_lists):
        self._PID = 0
        self.keys = []
        self.text = ""
        self.user = SimpleNamespace(WaitForInputIdle=mock.Mock(return_value=0))
        self.windows = {1: ("THomeForm", "", [0, 0, 100, 100]),
                        2: ("TPngBitBtn", "\u5e76\u7f51\u7cfb\u7edf", [0, 0, 10, 10]),
                        3: ("TProjectForm", "PVsyst: " + project, [0, 0, 1000, 800]),
                        4: ("TActionToolBar", "", [0, 0, 990, 53]),
                        5: ("TActionToolBar", "", [0, 60, 990, 113]),
                        6: ("TProjectLoadForm", "", [0, 0, 1476, 876]),
                        7: ("TEdit", "", [0, 0, 10, 10]),
                        8: ("TVirtualStringTree", "", [0, 100, 400, 500]),
                        9: ("TProjectLoadForm", "", [0, 0, 1476, 876])}
        self.children = {1: [2], 3: [4, 5], 6: [7, 8]}
        self.tops = [3, 6, 9][1:1 + ready_lists]

    def _libs(self):
        return self.user, None

    def _wait(self, cls, timeout=20.0, activate=True):
        return next(h for h, row in self.windows.items() if row[0] == cls)

    def _dpi(self, root):
        return 96

    def _kids(self, root):
        return self.children.get(root, [])

    def _class_of(self, hwnd):
        return self.windows[hwnd][0]

    def _string_of(self, hwnd):
        return self.windows[hwnd][1]

    def _rect_of(self, hwnd):
        return list(self.windows[hwnd][2])

    def _describe(self, hwnd):
        return {"visible": True, "enabled": True}

    def _find(self, root, cls, text=None):
        return next(h for h in self.children[root] if self.windows[h][0] == cls)

    def _click(self, hwnd, x=None, y=None):
        pass

    def _activate(self, hwnd):
        pass

    def _set_text(self, hwnd, value):
        self.text = value

    def _get_text(self, hwnd):
        return self.text

    def _key(self, vk, modifiers=()):
        self.keys.append(vk)

    def _tops(self):
        return list(self.tops)

    def _pid_of(self, hwnd):
        return self._PID


class ProjectTests(unittest.TestCase):
    def test_owned_load_confirms_only_one_unambiguous_list(self):
        proc = SimpleNamespace(pid=4321, _handle=99)
        with mock.patch.object(native_project, "time",
                               SimpleNamespace(sleep=lambda seconds: None, monotonic=time.monotonic)):
            gui = _OwnedGui("Example.PRJ", 2)
            with self.assertRaisesRegex(native_scene.NativeBridgeError, "found 2"):
                native_project._load_owned(gui, proc, "Example.PRJ", [])
            self.assertNotIn(gui.VK_RETURN, gui.keys)
            gui = _OwnedGui("Example.PRJ", 1)
            self.assertEqual(native_project._load_owned(gui, proc, "Example.PRJ", []), 3)
            self.assertEqual(gui.keys[-1], gui.VK_RETURN)
            # The row click may already have loaded the project: nothing is confirmed blindly.
            gui = _OwnedGui("Example.PRJ", 0)
            self.assertEqual(native_project._load_owned(gui, proc, "Example.PRJ", []), 3)
            self.assertNotIn(gui.VK_RETURN, gui.keys)


SOURCE_DC = {"mode": "percent", "resistance_mohm": "61.520", "loss_percent": "1.50",
             "circuit_resistance_mohm": "61.520", "string_groups": 1}
SAVED_DC = {**SOURCE_DC, "loss_percent": "2.50"}


class _DCStore:
    def __init__(self, workspace):
        self.lock = threading.RLock()
        self.workspace = workspace
        self.read_failures = {}

    def _check_project(self, project):
        pass

    def _validate_display_name(self, value, label):
        pass

    def _path(self, project, variant=None):
        return self.workspace / "Projects" / (f"{Path(project).stem}.{variant}" if variant else project)

    def _read(self, path):
        failures = self.read_failures.get(path, [])
        if failures:
            raise failures.pop(0)
        return path.read_bytes()

    def _text(self, data):
        text = data.decode("utf-8")
        return text, text.splitlines(keepends=True)


class NativeDCTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        projects = self.workspace / "Projects"
        projects.mkdir()
        (projects / "Loss.PRJ").write_text("project\n", encoding="utf-8")
        (projects / "Loss.VC0").write_text("source\n", encoding="utf-8")
        self.created = projects / "Loss.VC1"
        self.store = _DCStore(self.workspace)
        self.saved_name = "DC name"

        def save_new(project, variant_name):
            self.created.write_text("saved\n", encoding="utf-8")
            return self.created

        self.save_new = mock.Mock(side_effect=save_new)
        self.open = mock.Mock(return_value=(10, 11, 12))
        self.check_cli = mock.Mock(return_value="PVsystCLI 8.1.6 49278")
        for target, values in (
                (native_dc, dict(
                    _profile=mock.Mock(side_effect=lambda store, lines: SAVED_DC if "saved" in "".join(lines)
                                       else SOURCE_DC),
                    _invariants=mock.Mock(return_value=("unchanged",)), _open=self.open,
                    _root_block=mock.Mock(side_effect=lambda lines: SimpleNamespace(
                        one=lambda key: (0, self.saved_name))))),
                (native_scene, dict(
                    _check_cli=self.check_cli, _physical_coordinates=contextlib.nullcontext,
                    _ini_datapath=mock.Mock(return_value=self.workspace), _resolve_pid=mock.Mock(return_value=7),
                    _PID=0, _wait=mock.Mock(return_value=1), _dpi=mock.Mock(),
                    _string_of=mock.Mock(return_value="PVsyst: Loss.PRJ"), _find=mock.Mock(return_value=20),
                    _send=mock.Mock(return_value=1), _click=mock.Mock(), _set_text=mock.Mock(),
                    _key=mock.Mock(), _get_text=mock.Mock(return_value="2.50"), _save_new=self.save_new))):
            patcher = mock.patch.multiple(target, **values)
            patcher.start()
            self.addCleanup(patcher.stop)

    def create(self):
        return native_dc.create_native_dc_cabling_variant(
            self.store, SimpleNamespace(), project="Loss.PRJ", source_variant="VC0",
            dc={"mode": "percent", "value": 2.5}, variant_name="DC name", dry_run=False, confirm=True)

    def test_saved_variant_read_retries_a_sharing_violation(self):
        self.store.read_failures[self.created] = [PermissionError(13, "The file is in use")]
        result = self.create()
        self.assertEqual(result["status"], "created", result)
        self.assertEqual(result["dc"], SAVED_DC)

    def test_failures_after_driving_the_gui_are_not_reported_as_blocked(self):
        self.saved_name = "Another name"
        result = self.create()
        self.assertEqual((result["status"], result["stage"]), ("needs_attention", "native_saved"), result)
        self.assertTrue(result["variant_created"])
        self.assertEqual(result["artifacts"], {"variant": str(self.created)})
        self.saved_name = "DC name"
        self.created.unlink()
        self.open.side_effect = ValueError("Native DC input fields did not become ready")
        result = self.create()
        self.assertEqual((result["status"], result["stage"]), ("needs_attention", "native_edit"), result)
        self.assertFalse(result["variant_created"])
        self.check_cli.side_effect = native_scene.NativeBridgeError("The configured PVsystCLI executable is missing")
        result = self.create()
        self.assertEqual((result["status"], result["stage"]), ("blocked", "preflight"), result)
        self.assertEqual(result["native_actions"], [])


if __name__ == "__main__":
    unittest.main()

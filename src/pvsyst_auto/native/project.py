"""Launch the local PVsyst desktop viewer for manual inspection (no Win32 click automation)."""
from __future__ import annotations

from pathlib import Path
import subprocess

from pvsyst_auto.native._prep import find_running_pvsyst


def open_native_project(store, cli, *, project, dry_run=True, confirm=False):
    """Launch PVsyst.exe so an engineer can visually inspect a project in the configured workspace.

    Legacy Win32 GUI click automation was removed in v0.6.0; this expert helper only verifies
    the workspace project and launches the desktop executable without manipulating windows.
    """
    result = {
        "status": "blocked",
        "stage": "preflight",
        "project": project,
        "owned": False,
        "native_actions": [],
        "issues": [],
        "writes_ini": False,
    }
    with store.lock:
        try:
            if type(dry_run) is not bool or type(confirm) is not bool:
                raise ValueError("Use strict booleans")
            store._check_project(project)
            source = store._path(project)
            snapshot = store._read(source)
            exe = (Path(cli.cli).parent / "PVsyst.exe").resolve(strict=True)
            pids = find_running_pvsyst(exe)
            if pids:
                result.update(
                    status="already_open",
                    native_pid=pids[0],
                    running_pids=pids,
                    owned=False,
                    existing_session_read_only=True,
                )
                return result
            if dry_run or not confirm:
                result.update(
                    status="ready_to_open" if dry_run else "confirmation_required",
                    planned_action="launch_pvsyst_viewer",
                    executable=str(exe),
                )
                return result
            if store._read(source) != snapshot:
                raise RuntimeError("Source project changed after preflight")
            proc = subprocess.Popen(
                [str(exe)],
                cwd=exe.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            result.update(
                status="opened",
                stage="completed",
                owned=True,
                native_pid=proc.pid,
                executable=str(exe),
                source_preserved=True,
            )
            result["native_actions"].append("launch_pvsyst_viewer")
        except (OSError, ValueError, RuntimeError) as exc:
            result["status"] = "blocked"
            result["issues"].append(str(exc))
    return result

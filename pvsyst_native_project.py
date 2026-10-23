"""Open a configured-workspace project through the recorded native GUI, not VC edits."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
from pathlib import Path
import subprocess
import time


def _processes(gui, exe):
    """Include starting/background processes with no form yet; never infer absence from HWNDs."""
    _, kernel = gui._libs()

    class Entry(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                    ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                    ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                    ("name", w.WCHAR * 260)]

    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(Entry)]
    kernel.Process32FirstW.restype = w.BOOL
    kernel.Process32NextW.restype = w.BOOL
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise gui.NativeBridgeError("Native process inventory is unavailable")
    found = []
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(handle, ctypes.byref(entry))
        if not present:
            raise gui.NativeBridgeError("Native process inventory could not enumerate its first entry")
        while present:
            if entry.name.casefold() == exe.name.casefold():
                path = gui._exe_of(entry.pid)
                if not path:
                    raise gui.NativeBridgeError("A same-name native process has an unreadable executable path")
                if Path(path).resolve() != exe:
                    raise gui.NativeBridgeError("Another PVsyst installation is running; all sessions are left untouched")
                found.append(int(entry.pid))
            present = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise gui.NativeBridgeError("Native process inventory ended unexpectedly")
    finally:
        kernel.CloseHandle(handle)
    return found


def _project_matches(gui, root, project):
    return gui._string_of(root).rsplit(":", 1)[-1].strip().casefold() == project.casefold()


def _existing(gui, pid, project):
    forms = [gui._describe(h) for h in gui._tops() if gui._pid_of(h) == pid]
    projects = [row for row in forms if row["class"] == "TProjectForm" and row["visible"] and row["enabled"]]
    if len(projects) != 1 or not _project_matches(gui, projects[0]["hwnd"], project):
        raise gui.NativeBridgeError("An existing PVsyst session is not the requested ready project; it is left untouched")
    root = projects[0]["hwnd"]
    gui._dpi(root)
    return root


def _load_owned(gui, proc, project, actions):
    gui._PID = proc.pid
    user, _ = gui._libs()
    user.WaitForInputIdle.argtypes = [ctypes.c_void_p, w.DWORD]
    user.WaitForInputIdle.restype = w.DWORD
    if user.WaitForInputIdle(int(proc._handle), 30000) != 0:
        raise gui.NativeBridgeError("Owned GUI did not become input-idle")
    root = gui._wait("THomeForm", timeout=45)
    gui._dpi(root)
    buttons = [h for h in gui._kids(root) if gui._class_of(h) == "TPngBitBtn"
               and gui._string_of(h) == "\u5e76\u7f51\u7cfb\u7edf" and gui._describe(h)["enabled"]]
    if len(buttons) != 1:
        raise gui.NativeBridgeError("Expected the recorded native Grid entry")
    actions.append("enter_owned_grid_project")
    gui._click(buttons[0])
    root = gui._wait("TProjectForm", timeout=25)
    bars = [h for h in gui._kids(root) if gui._class_of(h) == "TActionToolBar"
            and gui._rect_of(h)[2] - gui._rect_of(h)[0] > 800]
    if len(bars) != 2:
        raise gui.NativeBridgeError("Expected two recorded project/variant toolbars")
    bar = min(bars, key=lambda h: gui._rect_of(h)[1])
    left, top, right, bottom = gui._rect_of(bar)
    if (right - left, bottom - top) != (990, 53):
        raise gui.NativeBridgeError("Native project-load toolbar metrics differ")
    actions.append("open_native_project_list")
    gui._click(bar, left + 132, top + 26)
    root = gui._wait("TProjectLoadForm")
    gui._dpi(root)
    left, top, right, bottom = gui._rect_of(root)
    if (right - left, bottom - top) != (1476, 876):
        raise gui.NativeBridgeError("Native project-list metrics differ")
    search = gui._find(root, "TEdit")
    gui._click(search)
    gui._set_text(search, project)
    gui._key(gui.VK_RIGHT)
    time.sleep(0.8)
    if gui._get_text(search) != project:
        raise gui.NativeBridgeError("Native project filter did not retain the requested filename")
    tree = gui._find(root, "TVirtualStringTree")
    rect = gui._rect_of(tree)
    actions.append("filter_and_confirm_native_project")
    gui._click(tree, rect[0] + 160, rect[1] + 36)
    time.sleep(0.5)
    load = [h for h in gui._tops() if gui._pid_of(h) == proc.pid
            and gui._class_of(h) == "TProjectLoadForm" and gui._describe(h)["visible"]
            and gui._describe(h)["enabled"]]
    if len(load) == 1:
        gui._activate(load[0])
        gui._key(gui.VK_RETURN)
    root = gui._wait("TProjectForm", timeout=25)
    end = time.monotonic() + 25
    while not _project_matches(gui, root, project):
        if time.monotonic() >= end:
            raise gui.NativeBridgeError("Native load did not reach the exact requested project")
        time.sleep(0.2)
    gui._dpi(root)
    return root


def _open(store, cli, project, dry_run, confirm, result):
    """Return the actual Popen only to an in-process task owner; existing sessions stay read-only."""
    import pvsyst_scene_native as gui
    store._check_project(project)
    source = store._path(project)
    snapshot = store._read(source)
    result["cli_version"] = gui._check_cli(cli)
    exe = (Path(cli.cli).parent / "PVsyst.exe").resolve(strict=True)
    with gui._physical_coordinates():
        if gui._ini_datapath().resolve() != store.workspace.resolve():
            raise gui.NativeBridgeError("Configured native DataPath must equal the MCP workspace before opening")
        pids = _processes(gui, exe)
        if len(pids) > 1:
            raise gui.NativeBridgeError("Multiple native instances are present; none will be manipulated")
        if pids:
            root = _existing(gui, pids[0], project)
            result.update(status="already_open", native_pid=pids[0], owned=False,
                          existing_session_read_only=True, existing_loaded_path_verified=False,
                          automation_ready=False, window=gui._describe(root))
            return None
        if dry_run or not confirm:
            result.update(status="ready_to_open" if dry_run else "confirmation_required",
                          planned_action="start_owned_gui_and_load_project")
            return None
        if store._read(source) != snapshot:
            raise gui.NativeBridgeError("Source project changed after preflight")
        proc = subprocess.Popen([str(exe)], cwd=exe.parent, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result.update(stage="native_open", owned=True, native_pid=proc.pid)
        result["native_actions"].append("start_owned_gui")
        # Failure retains the PID/current form for inspection, never force-terminates it.
        root = _load_owned(gui, proc, project, result["native_actions"])
        if _processes(gui, exe) != [proc.pid] or gui._exe_of(proc.pid) == "":
            raise gui.NativeBridgeError("Native process inventory changed during project load")
        if store._read(source) != snapshot:
            raise gui.NativeBridgeError("Native project load changed source PRJ bytes")
        result.update(status="opened", stage="completed", source_preserved=True,
                      window=gui._describe(root))
        return proc


def open_native_project(store, cli, *, project, dry_run=True, confirm=False):
    result = {"status": "blocked", "stage": "preflight", "project": project, "owned": False,
              "native_actions": [], "issues": [], "writes_ini": False}
    with store.lock:
        try:
            if type(dry_run) is not bool or type(confirm) is not bool:
                raise ValueError("Use strict booleans")
            _open(store, cli, project, dry_run, confirm, result)
        except (OSError, ValueError, RuntimeError) as exc:
            result["status"] = "blocked"
            result["issues"].append(str(exc))
    return result

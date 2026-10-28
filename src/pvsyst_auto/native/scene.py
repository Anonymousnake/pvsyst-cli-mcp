"""Native GUI bridge for the already running PVsyst 8.1.6 house/PV import.

Standard library and ctypes only. It never starts or closes the GUI, writes
PVsyst.ini or patches native variants. The CLI is queried only for its version;
all import, association, validity, shading-table and save actions use the GUI.

The operator must already have the matching project page open. Recorded window
classes, captions and physical metrics bound this bridge to the validated native
layout. Import success requires saved geometry/hardware checks; simulation
consumption remains a separate operation.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import math
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from pvsyst_auto.components.editor import objects
from pvsyst_auto.native.cli import PVsystError
from pvsyst_auto.native.project import _project_matches
from pvsyst_auto.scene.mapping import verify_grouped_hardware, verify_house_geometry, verify_pv_geometry, verify_module_layout
from pvsyst_auto.project.paths import safe_filename, workspace_file
from pvsyst_auto.scene.author import _num, check_tables, house_profile, resolve_modules, source_profile, table_profiles
from pvsyst_auto.scene.reader import read_house_pv_scene

EXPECTED_DPI = 96
EXPECTED_SIZE = {"TPerspectiveForm": (1475, 899), "TOrientManageForm": (1126, 826),
                 "TModuleLayoutForm": (1476, 864), "TMLStringsAttribForm": (783, 725)}

IMPORT_FOLDER = (230, 461)
SOURCE_GROUP = (250, 301)
DRAG_STEPS = 20
ORIENT_EMPTY_ROOT_BASE = 398
ORIENT_ROW_HEIGHT = 32
ORIENT_TOOLBAR = (273, 245)
TOOLBAR_SAVE_ITEM = (148, 27)
MAX_TABLES_BRIDGED = 2

BETA_MARK = "PVC2 support is still in beta"
VALID_SCENE = "\u6b64\u573a\u666f\u6b63\u786e\u3002"
MODIFY_FIELD = "\u4fee\u6539\u5149\u4f0f\u573a\u533a\u7684\u53c2\u6570"
CAP_INFO = "\u4fe1\u606f"

WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
BM_GETCHECK = 0x00F0
VK_TAB = 0x09
CB_GETCOUNT = 0x0146
CB_GETLBTEXT = 0x0148
CB_GETLBTEXTLEN = 0x0149
CB_SETCURSEL = 0x014E
CF_UNICODETEXT = 13
GA_ROOT = 2
SMTO_ABORTIFHUNG = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
VK_MENU, VK_SHIFT, VK_CONTROL = 0x12, 0x10, 0x11
VK_DOWN, VK_RIGHT, VK_RETURN = 0x28, 0x27, 0x0D
VK_S, VK_O, VK_X, VK_Y = 0x53, 0x4F, 0x58, 0x59
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MAX_COMBO_ITEMS = 128
MAX_COMBO_TEXT = 4096

PVSYST_PROCESS_CLASSES = ("THomeForm", "TProjectForm", "TProjectLoadForm",
                          "TPerspectiveForm", "TShadingsForm")

_PID = 0
_U = None
_K = None


class NativeBridgeError(RuntimeError):
    """Raised when the recorded native path cannot be followed safely."""


class UnsupportedEnvironment(NativeBridgeError):
    """Raised when this host cannot host the recorded Win32 path at all."""


def _fail(message):
    raise NativeBridgeError(message)


def _libs():
    """Bind Win32 lazily so importing this module has no side effect."""
    global _U, _K
    if _U is None:
        if sys.platform != "win32":
            raise UnsupportedEnvironment("The native GUI bridge requires Windows")
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        _bind(u, k)
        _U, _K = u, k
    return _U, _K


def _bind(u, k):
    HWND, UINT, WPARAM, LPARAM, LRESULT = (w.HWND, w.UINT, w.WPARAM, w.LPARAM, ctypes.c_ssize_t)
    u.GetAncestor.argtypes = [HWND, UINT]
    u.GetAncestor.restype = HWND
    u.GetForegroundWindow.restype = HWND
    u.GetParent.argtypes = [HWND]
    u.GetParent.restype = HWND
    u.GetDlgCtrlID.argtypes = [HWND]
    u.GetDlgCtrlID.restype = ctypes.c_int
    u.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(w.DWORD)]
    u.GetWindowThreadProcessId.restype = w.DWORD
    u.GetWindowTextLengthW.argtypes = [HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [HWND, ctypes.c_wchar_p, ctypes.c_int]
    u.GetClassNameW.argtypes = [HWND, ctypes.c_wchar_p, ctypes.c_int]
    u.IsWindowVisible.argtypes = [HWND]
    u.IsWindowEnabled.argtypes = [HWND]
    u.GetWindowRect.argtypes = [HWND, ctypes.POINTER(w.RECT)]
    u.EnumWindows.argtypes = [ctypes.c_void_p, LPARAM]
    u.EnumChildWindows.argtypes = [HWND, ctypes.c_void_p, LPARAM]
    u.SendMessageTimeoutW.argtypes = [HWND, UINT, WPARAM, LPARAM, UINT, UINT,
                                      ctypes.POINTER(ctypes.c_size_t)]
    u.SendMessageTimeoutW.restype = LRESULT
    u.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
    u.SetForegroundWindow.argtypes = [HWND]
    u.GetForegroundWindow.argtypes = []
    u.WindowFromPoint.argtypes = [w.POINT]
    u.WindowFromPoint.restype = HWND
    u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u.mouse_event.argtypes = [w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.c_void_p]
    u.keybd_event.argtypes = [w.BYTE, w.BYTE, w.DWORD, ctypes.c_void_p]
    u.OpenClipboard.argtypes = [HWND]
    u.GetClipboardData.argtypes = [UINT]
    u.GetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardSequenceNumber.argtypes = []
    u.GetClipboardSequenceNumber.restype = w.DWORD
    u.GetClipboardOwner.argtypes = []
    u.GetClipboardOwner.restype = w.HWND
    k.GlobalSize.argtypes = [ctypes.c_void_p]
    k.GlobalSize.restype = ctypes.c_size_t
    u.CloseClipboard.argtypes = []
    if hasattr(u, "SetThreadDpiAwarenessContext"):
        u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    if hasattr(u, "GetDpiForWindow"):
        u.GetDpiForWindow.argtypes = [HWND]
        u.GetDpiForWindow.restype = UINT
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, w.DWORD,
                                             ctypes.c_wchar_p, ctypes.POINTER(w.DWORD)]
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]


def _send(hwnd, msg, wp=0, lp=0, timeout=1000):
    u, _ = _libs()
    out = ctypes.c_size_t()
    if not u.SendMessageTimeoutW(w.HWND(hwnd), msg, w.WPARAM(wp), w.LPARAM(lp),
                                 SMTO_ABORTIFHUNG, timeout, ctypes.byref(out)):
        _fail("SendMessageTimeout failed for message %#x" % msg)
    return out.value


def _string_of(hwnd):
    u, _ = _libs()
    size = u.GetWindowTextLengthW(w.HWND(hwnd))
    if size < 0:
        _fail("GetWindowTextLength failed")
    buf = ctypes.create_unicode_buffer(size + 1)
    u.GetWindowTextW(w.HWND(hwnd), buf, size + 1)
    return buf.value


def _class_of(hwnd):
    u, _ = _libs()
    buf = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(w.HWND(hwnd), buf, 256)
    return buf.value


def _rect_of(hwnd):
    u, _ = _libs()
    r = w.RECT()
    if not u.GetWindowRect(w.HWND(hwnd), ctypes.byref(r)):
        _fail("GetWindowRect failed")
    return [r.left, r.top, r.right, r.bottom]


def _pid_of(hwnd):
    u, _ = _libs()
    owner = w.DWORD()
    u.GetWindowThreadProcessId(w.HWND(hwnd), ctypes.byref(owner))
    return int(owner.value)


def _describe(hwnd):
    u, _ = _libs()
    return {"hwnd": int(hwnd), "pid": _pid_of(hwnd), "class": _class_of(hwnd),
            "text": _string_of(hwnd), "rect": _rect_of(hwnd),
            "visible": bool(u.IsWindowVisible(w.HWND(hwnd))),
            "enabled": bool(u.IsWindowEnabled(w.HWND(hwnd)))}


def _tops():
    u, _ = _libs()
    out = []
    proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def cb(h, _):
        out.append(int(h or 0))
        return True
    u.EnumWindows(proc(cb), 0)
    return out


def _kids(root):
    u, _ = _libs()
    out = []
    proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(w.HWND(h)):
            out.append(int(h or 0))
        return True
    u.EnumChildWindows(w.HWND(root), proc(cb), 0)
    return out


def _exe_of(pid):
    _, k = _libs()
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buf,
                                            ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        k.CloseHandle(ctypes.c_void_p(handle))


@contextmanager
def _physical_coordinates():
    # Scope physical pixel coordinates to this worker; never change process/global DPI.
    u, _ = _libs()
    change = getattr(u, "SetThreadDpiAwarenessContext", None)
    if change is None:
        raise UnsupportedEnvironment("Thread DPI awareness context is unavailable")
    previous = change(ctypes.c_void_p(-4))
    if not previous:
        raise UnsupportedEnvironment("Physical coordinate context could not be established")
    try:
        yield
    finally:
        if not change(previous):
            _fail("The worker DPI awareness context could not be restored")


def _dpi(root):
    u, _ = _libs()
    probe = getattr(u, "GetDpiForWindow", None)
    if probe is None:
        raise UnsupportedEnvironment("GetDpiForWindow is unavailable; scale cannot be verified")
    value = int(probe(w.HWND(root)))
    if value != EXPECTED_DPI:
        raise UnsupportedEnvironment("Unsupported display scale: %d dpi" % value)
    return value


def _activate(hwnd):
    u, _ = _libs()
    if _pid_of(hwnd) != _PID:
        _fail("Refusing to activate a window of another process")
    if int(u.GetForegroundWindow() or 0) == hwnd:
        return
    u.SetForegroundWindow(w.HWND(hwnd))
    time.sleep(0.10)
    if int(u.GetForegroundWindow() or 0) == hwnd:
        return
    u.keybd_event(VK_MENU, 0, 0, None)
    try:
        u.SetForegroundWindow(w.HWND(hwnd))
    finally:
        u.keybd_event(VK_MENU, 0, 2, None)
    time.sleep(0.10)
    if int(u.GetForegroundWindow() or 0) != hwnd:
        _fail("Window did not take the foreground; aborting before any click")


def _click(hwnd, x=None, y=None):
    u, _ = _libs()
    d = _describe(hwnd)
    if d["pid"] != _PID or not d["visible"] or not d["enabled"]:
        _fail("Refusing to click an unusable control: " + d["class"])
    _activate(int(u.GetAncestor(w.HWND(hwnd), GA_ROOT) or 0))
    left, top, right, bottom = d["rect"]
    px = (left + right) // 2 if x is None else x
    py = (top + bottom) // 2 if y is None else y
    if not (left <= px <= right and top <= py <= bottom):
        _fail("Point outside the control rect; refusing to click")
    if int(u.WindowFromPoint(w.POINT(px, py)) or 0) != hwnd:
        _fail("WindowFromPoint did not resolve to the target control")
    u.SetCursorPos(px, py)
    u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)
    time.sleep(0.18)


def _set_text(hwnd, value):
    d = _describe(hwnd)
    if d["pid"] != _PID or not d["enabled"]:
        _fail("Refusing to type into an unusable control")
    buf = ctypes.create_unicode_buffer(value)
    _send(hwnd, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p).value)


def _get_text(hwnd, limit=200000):
    size = _send(hwnd, WM_GETTEXTLENGTH)
    if size > limit:
        _fail("Control text exceeds the readback ceiling")
    buf = ctypes.create_unicode_buffer(size + 1)
    _send(hwnd, WM_GETTEXT, size + 1, ctypes.cast(buf, ctypes.c_void_p).value)
    return buf.value


def _key(vk, modifiers=()):
    u, _ = _libs()
    root = int(u.GetForegroundWindow() or 0)
    if _pid_of(root) != _PID:
        _fail("Foreground window is not the managed process")
    try:
        for mod in modifiers:
            u.keybd_event(mod, 0, 0, None)
        u.keybd_event(vk, 0, 0, None)
        u.keybd_event(vk, 0, 2, None)
        # Match the native replay: the VCL loop consumes the key while modifiers remain held.
        time.sleep(0.10)
    finally:
        for mod in reversed(modifiers):
            u.keybd_event(mod, 0, 2, None)
    time.sleep(0.12)


def _combo_count(hwnd):
    count = _send(hwnd, CB_GETCOUNT)
    if not 0 <= count <= MAX_COMBO_ITEMS:
        _fail("Combo box item count is outside the supported range")
    return count


def _combo_text(hwnd, index):
    length = _send(hwnd, CB_GETLBTEXTLEN, index)
    if length < 0 or length > MAX_COMBO_TEXT:
        _fail("Combo box item text length is outside the supported range")
    buf = ctypes.create_unicode_buffer(length + 1)
    got = _send(hwnd, CB_GETLBTEXT, index, ctypes.cast(buf, ctypes.c_void_p).value)
    if got != length:
        _fail("Combo box item text length changed while reading")
    return buf.value


def _combo_items(hwnd):
    return [_combo_text(hwnd, i) for i in range(_combo_count(hwnd))]


def _combo_select(hwnd, index, *, notify_timeout=2000):
    u, _ = _libs()
    parent = int(u.GetParent(w.HWND(hwnd)) or 0)
    _send(hwnd, CB_SETCURSEL, index)
    _send(parent, WM_COMMAND,
          ((1 << 16) | (u.GetDlgCtrlID(w.HWND(hwnd)) & 0xFFFF)), hwnd, notify_timeout)
    time.sleep(0.30)


def _wait(cls, timeout=20.0, activate=True):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hits = [h for h in _tops()
                if _pid_of(h) == _PID and _class_of(h) == cls
                and _describe(h)["visible"] and _describe(h)["enabled"]]
        if len(hits) == 1:
            if activate:
                _activate(hits[0])
                # The recorded VCL replay settles after showing a modal before using its child layout.
                time.sleep(0.35)
            return hits[0]
        if len(hits) > 1:
            _fail("More than one %s window; refusing to guess" % cls)
        time.sleep(0.10)
    _fail("Timed out waiting for %s" % cls)


def _find(root, cls, text=None):
    hits = [h for h in _kids(root) if _class_of(h) == cls
            and (text is None or _get_text(h) == text)]
    if len(hits) != 1:
        _fail("Expected one %s control (%r), found %d" % (cls, text, len(hits)))
    return hits[0]


def _clipboard_text(hwnd, timeout=3.0):
    """Copy with Ctrl+C only after the clipboard sequence number changes."""
    u, _ = _libs()
    before = int(u.GetClipboardSequenceNumber())
    _activate(int(u.GetAncestor(w.HWND(hwnd), GA_ROOT) or 0))
    _key(0x43, (VK_CONTROL,))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        sequence = int(u.GetClipboardSequenceNumber())
        if sequence != before:
            return _read_unicode_clipboard(sequence, hwnd)
        time.sleep(0.10)
    _fail("Clipboard sequence number did not change; nothing copied")


def _read_unicode_clipboard(expected, hwnd, timeout=3.0):
    u, k = _libs()
    end = time.monotonic() + timeout
    while True:
        if (int(u.GetClipboardSequenceNumber()) != expected
                or int(u.GetForegroundWindow() or 0) != hwnd):
            _fail("Native copy changed or its message lost foreground")
        if u.OpenClipboard(None):
            break
        if time.monotonic() >= end:
            _fail("Native clipboard remained locked")
        time.sleep(0.05)
    try:
        owner = int(u.GetClipboardOwner() or 0)
        if int(u.GetClipboardSequenceNumber()) != expected or (owner and _pid_of(owner) != _PID):
            _fail("Clipboard no longer belongs to this native copy")
        handle = u.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            _fail("Native clipboard holds no unicode text")
        size = int(k.GlobalSize(ctypes.c_void_p(handle)))
        if not 2 <= size <= 400000 or size % 2:
            _fail("Native clipboard text exceeds the bounded unicode buffer")
        pointer = k.GlobalLock(ctypes.c_void_p(handle))
        if not pointer:
            _fail("GlobalLock failed")
        try:
            return ctypes.wstring_at(pointer, size // 2).split("\0", 1)[0]
        finally:
            k.GlobalUnlock(ctypes.c_void_p(handle))
    finally:
        u.CloseClipboard()


def _ini_path():
    return Path.home() / "AppData" / "Local" / "PVsyst" / "8.1" / "Admin" / "PVsyst.ini"


def _ini_datapath():
    raw = _ini_path().read_bytes().decode("utf-16")
    found = re.findall(r"(?m)^DataPath=([^\r\n]*)", raw)
    if len(found) != 1:
        _fail("PVsyst.ini must declare exactly one DataPath")
    return Path(found[0])


def _resolve_pid(cli):
    """Find the one PVsyst.exe living next to the configured CLI."""
    exe = (Path(cli.cli).parent / "PVsyst.exe").resolve()
    wanted = set()
    for h in _tops():
        if _class_of(h) in PVSYST_PROCESS_CLASSES:
            wanted.add(_pid_of(h))
    matches = {}
    for pid in sorted(wanted):
        path = _exe_of(pid)
        if path and Path(path).resolve() == exe:
            matches[pid] = True
    if len(matches) != 1:
        _fail("Expected exactly one running PVsyst.exe next to the configured CLI")
    return next(iter(matches))


def _toolbar_save(root):
    """Use the lower of the two wide project/variant toolbars, not the small site/meteo bars."""
    bars = [h for h in _kids(root) if _class_of(h) == "TActionToolBar"
            and _rect_of(h)[2] - _rect_of(h)[0] > 800]
    if len(bars) != 2:
        _fail("Expected the two recorded wide project/variant toolbars")
    bars.sort(key=lambda h: _rect_of(h)[1])
    bar = bars[1]
    r = _rect_of(bar)
    _click(bar, r[0] + TOOLBAR_SAVE_ITEM[0], r[1] + TOOLBAR_SAVE_ITEM[1])


def _check_cli(cli):
    """Confirm the adapter points at the recorded build without driving other CLI actions."""
    binary = getattr(cli, "cli", None) or getattr(cli, "path", None)
    if not binary:
        _fail("The adapter does not expose its CLI path")
    if not Path(binary).is_file():
        _fail("The configured PVsystCLI executable is missing")
    if not hasattr(cli, "version"):
        _fail("The adapter cannot report the native CLI version")
    try:
        text = cli.version() or ""
    except PVsystError as exc:
        # A failed/timed-out query is a bridge stop with a result, not an escaping CLI error.
        _fail("The native CLI version query failed: %s" % exc)
    if "8.1.6" not in text or "49278" not in text:
        raise UnsupportedEnvironment("Unexpected native build: " + text.strip()[:120])
    return text.strip()


def _source_snapshot(store, components, project, variant, module_geometry, *, allow_groups=False):
    """Validate the source variant read-only, reusing the production helpers."""
    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        original = store._read(path)
        _, lines = store._text(original)
        arrays, system = store._sections(lines)
        source = source_profile(store, lines, arrays, system, allow_groups=allow_groups)
        modules, pan_snapshot = resolve_modules(components, source["pan_name"], module_geometry)
    return {"path": path, "bytes": original, "source": source, "modules": modules,
            "pan_snapshot": pan_snapshot}


def _prepare_source(store, components, scene, *, allow_groups=False):
    """Bind lossless scene geometry to the current source, without driving the GUI."""
    args = scene["create_arguments"]
    snap = _source_snapshot(store, components, args["project"], args["variant"],
                            args["module_geometry"], allow_groups=allow_groups)
    recorded, current = scene["source"], snap["source"]
    if recorded["pan"].casefold() != current["pan_name"].casefold():
        _fail("The scene PAN reference differs from the current source variant")
    if recorded["module_total"] != current["module_total"]:
        _fail("The scene module total differs from the current source variant")
    if (_num(recorded["tilt_deg"]) != _num(current["tilt_deg"])
            or _num(recorded["collada_azimuth_deg"]) != _num(current["azimuth_deg"])
            or any(_num(recorded["site"][key]) != _num(current["site"][key])
                   for key in ("latitude", "longitude", "altitude"))):
        _fail("The scene PV plane or site differs from the current source variant")
    declared_groups = recorded.get("orientations", [])
    current_groups = current.get("orientations", [])
    if bool(declared_groups) != bool(current_groups):
        _fail("The scene orientation structure differs from the current source variant")
    if declared_groups:
        actual_groups = recorded.get("orientation_geometry", [])
        if ([item["id"] for item in declared_groups] != [item["id"] for item in current_groups]
                or [item["id"] for item in actual_groups] != [item["id"] for item in current_groups]):
            _fail("The scene must retain each current source orientation identity")
        for declared, actual, plane in zip(declared_groups, actual_groups, current_groups):
            if (declared["module_total"] != plane["module_total"]
                    or actual["module_total"] != plane["module_total"]
                    or _num(actual["tilt_deg"]) != _num(plane["tilt_deg"])
                    or _num(actual["collada_azimuth_deg"]) != _num(plane["azimuth_deg"])):
                _fail("Scene orientation %d differs from the current source plane" % plane["id"])
    pocket = house_profile(args["house"])
    tables = table_profiles(args["layout"], pocket, snap["modules"], current)
    check_tables(pocket, tables)
    if len(tables) != int(scene["geometry"]["pv_table_count"]):
        _fail("Recipe table count disagrees with the scene readback")
    return {"snapshot": snap, "house": pocket, "table_profiles": tables}


def _module_layout_preflight(snapshot, tables):
    """Keep automatic wiring within the native template that has been verified."""
    arrays = [block for block in objects(snapshot["bytes"].decode("utf-8-sig"))
              if block.object_type == "pvSubArray"]
    planes = snapshot["source"].get("orientations", [])
    if (len(planes) != 2 or {plane["id"] for plane in planes} != {1, 2} or len(tables) != 2
            or [block.one("SubArrayId")[1] for block in arrays] != ["1", "2"]
            or {block.one("NoOrientation")[1] for block in arrays} != {"1", "2"}
            or any(table["orientation"] != "landscape" or table["row_spacing_m"] != 0
                   or table["column_spacing_m"] != 0 for table in tables)):
        _fail("Module Layout requires two source groups, arrays1/2 and landscape zero-spacing tables")
    for array in arrays:
        series = array.one("NModSerie")[1]
        if (not re.fullmatch(r"[1-9][0-9]{0,2}", series) or int(series) > 100
                or any(array.one(key)[1] != "1" for key in ("NStringCh", "NInverter", "NInvMPPT"))):
            _fail("Module Layout requires one independent string of 1..100 modules and one inverter/MPPT per array")


def _preflight(components, cli, scene_name, module_geometry, *, store=None, allow_groups=False,
               shading_mode="linear"):
    if type(shading_mode) is not str or shading_mode not in ("linear", "module_layout"):
        _fail("shading_mode must be linear or module_layout")
    scene = read_house_pv_scene(components, scene_name)
    if scene.get("status") != "ready":
        return {"status": "needs_context", "scene": scene,
                "reason": "The scene readback is not ready; fix the recipe first."}
    grouped = bool(scene.get("source", {}).get("orientations"))
    if grouped and not allow_groups:
        return {"status": "needs_context", "scene": scene,
                "reason": "Grouped authoring is a candidate only; per-table native identity/geometric verification is not bridged"}
    args = scene["create_arguments"]
    geometry = scene["geometry"]
    tables = int(geometry["pv_table_count"])
    parts = int(geometry["house_parts"])
    if not 1 <= tables <= MAX_TABLES_BRIDGED:
        return {"status": "needs_context", "scene": scene,
                "reason": "Only %d..%d PV tables are bridged by this path"
                          % (1, MAX_TABLES_BRIDGED)}
    result = {"status": "preflight_stage2", "scene": scene, "args": args,
              "tables": tables, "parts": parts, "grouped": grouped}
    if store is not None:
        result.update(_prepare_source(store, components, scene, allow_groups=allow_groups))
        if grouped:
            source = result["snapshot"]["source"]
            planes = source["orientations"]
            if (source["field_azimuth_deg"] != 0 or len(planes) != 2 or abs((planes[1]["field_azimuth_deg"]
                    - planes[0]["field_azimuth_deg"] + 180) % 360 - 180) < 1):
                _fail("Grouped native import requires anchor azimuth zero and two distinct source planes")
            for table in result["table_profiles"]:
                for value in (-table["origin_m"][0], -table["origin_m"][1], table["origin_m"][2]):
                    _placement_text(float(_num(value)))
            for suffix in ("imported", "placed", "linked", "reload"):
                target = workspace_file(components.workspace, "Shadings", Path(scene_name).stem + "_" + suffix + ".SHD", ".SHD")
                if target.exists():
                    _fail("Grouped native scene evidence already exists: " + target.name)
    elif grouped:
        return {"status": "needs_context", "scene": scene,
                "reason": "Grouped preflight requires the current source variant for per-orientation comparison"}
    if shading_mode == "module_layout":
        if store is None:
            return {"status": "needs_context", "scene": scene,
                    "reason": "Module Layout requires the current source variant"}
        _module_layout_preflight(result["snapshot"], result["table_profiles"])
    return result


def import_house_pv_scene(store, components, cli, scene_name, variant_name,
                          dry_run=True, confirm=False, shading_mode="linear"):
    with store.lock:
        return _import_house_pv_scene(store, components, cli, scene_name, variant_name,
                                      dry_run, confirm, shading_mode)


def _import_house_pv_scene(store, components, cli, scene_name, variant_name, dry_run, confirm,
                           shading_mode="linear"):
    """Drive the already open PVsyst GUI through the recorded import path.

    dry_run reads state only: no activation, no key or mouse injection, no
    clipboard access. Execution additionally requires confirm=True.
    """
    result = {"status": "blocked", "native_geometry_verified": False, "imported": False,
              "native_consumption_verified": False, "variant": None, "file": None,
               "native_actions": [], "issues": [], "diagnostics": [], "artifacts": {},
               "stage": "preflight"}
    try:
        safe_filename(scene_name, ".pvc2")
        store._validate_display_name(variant_name, "variant_name")
        if type(dry_run) is not bool or type(confirm) is not bool:
            raise ValueError("Use strict booleans")
        result["cli_version"] = _check_cli(cli)
        pre = _preflight(components, cli, scene_name, None, store=store, allow_groups=True,
                         shading_mode=shading_mode)
        result["preflight"] = {key: pre[key] for key in ("status", "tables", "parts") if key in pre}
        if pre["status"] != "preflight_stage2":
            result.update(status=pre["status"], issues=[pre.get("reason", "")])
            return result
        args = pre["args"]
        snap = pre["snapshot"]
        result["source"] = {"project": args["project"], "variant": args["variant"],
                            "pan": snap["source"]["pan_name"],
                            "module_total": snap["source"]["module_total"],
                            "field_tilt_deg": snap["source"]["tilt_deg"],
                            "field_azimuth_deg": snap["source"]["field_azimuth_deg"]}
        global _PID
        with _physical_coordinates():
            if _ini_datapath().resolve() != Path(components.workspace).resolve():
                _fail("PVsyst.ini DataPath does not match the adapter workspace")
            _PID = _resolve_pid(cli)
            result["native_pid"] = _PID
            root = _wait("TProjectForm", timeout=10.0, activate=False)
            _dpi(root)
            if not _project_matches(sys.modules[__name__], root, args["project"]):
                _fail("The open project caption does not match the scene recipe")
            modals = [h for h in _tops() if _pid_of(h) == _PID
                      and _class_of(h) in ("TMessageForm", "TSaveObjPVForm", "#32770",
                                           "TActionSelectionForm")
                      and _describe(h)["visible"] and _describe(h)["enabled"]]
            if modals:
                _fail("A modal dialog is already open; resolve it before running the bridge")
            if dry_run:
                result.update(status="ready_to_import", project=args["project"],
                              source_variant=args["variant"], tables=pre["tables"],
                              parts=pre["parts"])
                return result
            if not confirm:
                result.update(status="confirmation_required",
                              issues=["Call again with confirm=True to drive the GUI."])
                return result
            if snap["path"].read_bytes() != snap["bytes"]:
                _fail("Source variant changed after preflight")
            scene_file = workspace_file(components.workspace, "UserData", scene_name, ".pvc2")
            scene_bytes = scene_file.read_bytes()
            result["artifacts"] = {"scene_file": str(scene_file)}
            result["stage"] = "native_import"
            _select_source_variant(root, args["variant"])
            result["native_actions"].append("select_variant")
            _click(_find(root, "TPngBitBtn", "\u8fd1\u5904\u906e\u6321"))
            result["native_actions"].append("open_near_shadings")
            summary = _import_scene(components, scene_name, scene_file, scene_bytes,
                                    pre["tables"], pre["parts"])
            result["native_actions"].append("import_pvc2")
            result["import_summary"] = summary[:2000]
            if pre["grouped"]:
                prefix = Path(scene_name).stem
                imported_scene = _export_scene(components, prefix + "_imported.SHD")
                result["native_placement"] = _place_grouped_tables(pre, imported_scene)
                placed_scene = _export_scene(components, prefix + "_placed.SHD")
                result["placed_geometry"] = _verify_scene_geometry(placed_scene, pre)
                _remove_empty_import_orientation()
                linked_scene = _export_scene(components, prefix + "_linked.SHD")
                result["linked_geometry"] = _verify_scene_geometry(linked_scene, pre, orientations=True)
                result["artifacts"].update(imported_scene=str(imported_scene), placed_scene=str(placed_scene),
                                            linked_scene=str(linked_scene))
            else:
                _link_orientation(pre["tables"])
            result["native_actions"].append("link_orientation")
            validity = _validate_scene()
            result["native_validity"] = validity
            result["native_actions"].append("check_scene_validity")
            if not validity["valid"]:
                result.update(status="needs_attention", stage="native_validity",
                              issues=["Native validity check: " + validity["text"][:400]],
                              diagnostics=[{"code": "native_scene_invalid", "path": "native_scene",
                                "message": validity["text"], "repair_hint":
                                "Locate the named native objects, revise house/layout and create a new scene file."}])
                return result
            _close_scene()
            result["native_actions"].append("close_scene")
            if shading_mode == "module_layout":
                result["stage"] = "native_module_layout"
                _configure_module_layout(result["native_actions"])
            created = _save_new(args["project"], variant_name)
            result.update(stage="verify_native_save", variant=created.suffix.lstrip(".").upper(), file=str(created))
            result["artifacts"]["native_variant"] = str(created)
            result["native_actions"].append("save_new_variant")
            if snap["path"].read_bytes() != snap["bytes"]:
                result.update(status="needs_attention",
                              issues=["Source variant bytes changed during the run"])
                return result
            if scene_file.read_bytes() != scene_bytes:
                _fail("Scene file changed during the native import")
            if snap["pan_snapshot"] is not None:
                path, before = snap["pan_snapshot"]
                if components._read(path) != before:
                    _fail("Source PAN changed during the native import")
            verification = _verify_new(created, snap["bytes"], pre["parts"], pre["tables"],
                                       snap["source"]["module_total"],
                                       shading_mode=shading_mode,
                                       **({"scene_context": _scene_context(pre)} if pre["grouped"] else {}))
            if pre["grouped"]:
                result["stage"] = "native_reload"
                saved_bytes = created.read_bytes()
                root = _wait("TProjectForm")
                _select_source_variant(root, args["variant"], notify_timeout=30000)
                _select_source_variant(root, created.suffix[1:], notify_timeout=30000)
                _click(_find(root, "TPngBitBtn", "近处遮挡"))
                shading = _wait("TShadingsForm")
                _click(_find(shading, "TPngBitBtn", "建模 / 透视图"))
                _wait("TPerspectiveForm")
                reload_scene = _export_scene(components, Path(scene_name).stem + "_reload.SHD")
                reloaded = _verify_scene_geometry(reload_scene, pre, orientations=True)
                reload_validity = _validate_scene()
                if not reload_validity["valid"]:
                    _fail("Reloaded native scene is invalid")
                view = _wait("TPerspectiveForm")
                _click(_find(view, "TPngBitBtn", "取消"))
                shading = _wait("TShadingsForm")
                _click(_find(shading, "TPngBitBtn", "取消"))
                _wait("TProjectForm")
                if shading_mode == "module_layout":
                    _module_layout_mode(select=False)
                if created.read_bytes() != saved_bytes or snap["path"].read_bytes() != snap["bytes"]:
                    _fail("Native saved variant changed during reload")
                _verify_new(created, snap["bytes"], pre["parts"], pre["tables"],
                            snap["source"]["module_total"], scene_context=_scene_context(pre),
                            shading_mode=shading_mode)
                result.update(native_reload_verified=True, native_reload={"source_variant": args["variant"],
                    "saved_variant": created.suffix[1:].upper(), "exported_scene_file": str(reload_scene),
                    "verification": reloaded, "native_validity": reload_validity})
                result["native_actions"].extend(["reload_saved_variant", "export_reloaded_scene", "verify_reloaded_geometry"])
            if shading_mode == "module_layout":
                result.update(shading_mode=shading_mode, module_layout=verification["module_layout"],
                              native_shading_mode_reload_verified=True)
            result.update(status="imported", stage="completed", imported=True, native_geometry_verified=True,
                          verification=verification)
            return result
    except UnsupportedEnvironment as exc:
        result.update(status="unsupported", issues=[str(exc)])
        return result
    except NativeBridgeError as exc:
        result.update(status="needs_attention", issues=[str(exc)])
        return result
    except (OSError, ValueError, StopIteration, AttributeError, KeyError) as exc:
        result.update(status="failed", issues=[type(exc).__name__ + ": " + str(exc)])
        return result


def _select_source_variant(root, variant, *, notify_timeout=2000):
    variant = variant.upper()
    combos = [h for h in _kids(root) if _class_of(h) == "TComboBox"
              and any(t.split() and t.split()[0] == variant for t in _combo_items(h))]
    if len(combos) != 1:
        _fail("Could not locate the unique variant combo box")
    caption = _string_of(root)
    items = _combo_items(combos[0])
    _combo_select(combos[0], next(i for i, t in enumerate(items)
                                  if t.split() and t.split()[0] == variant), notify_timeout=notify_timeout)
    again = _wait("TProjectForm", timeout=10.0)
    if _string_of(again) != caption:
        _fail("Project caption changed while selecting the source variant")
    selected = _get_text(combos[0]).split()
    if not selected or selected[0] != variant:
        _fail("Native variant selection did not retain the requested identifier")


def _check_size(hwnd, cls):
    wanted = EXPECTED_SIZE[cls]
    left, top, right, bottom = _rect_of(hwnd)
    if (right - left, bottom - top) != wanted:
        _fail("%s size %dx%d does not match the recorded %dx%d metrics"
              % (cls, right - left, bottom - top, *wanted))


def _import_scene(components, scene_name, scene_file, scene_bytes, tables, parts):
    shadings = _wait("TShadingsForm", timeout=15.0)
    _click(_find(shadings, "TPngBitBtn", "\u5efa\u6a21 / \u900f\u89c6\u56fe"))
    view = _wait("TPerspectiveForm", timeout=20.0)
    _check_size(view, "TPerspectiveForm")
    _key(VK_S, (VK_MENU,))
    for _ in range(4):
        _key(VK_DOWN)
    _key(VK_RIGHT)
    _key(VK_DOWN)
    _key(VK_RETURN)
    dialog = _wait("#32770", timeout=20.0)
    edit = _find(dialog, "Edit")
    if scene_file.read_bytes() != scene_bytes:
        _fail("Scene file changed after validation; refusing to import it")
    _set_text(edit, str(scene_file))
    _click(_find(dialog, "Button", "\u6253\u5f00(&O)"))
    result = _wait("TImportResultForm", timeout=60.0)
    _click(_find(result, "TPngBitBtn", "\u786e\u5b9a"))
    summary = _wait("TRichEditReadOnlyForm", timeout=30.0)
    body = _get_text(_find(summary, "TRichEdit"))
    _check_summary(body, tables, parts)
    _click(_find(summary, "TPngBitBtn", "\u5173\u95ed"))
    _wait("TPerspectiveForm", timeout=20.0)
    return body


def _check_summary(body, tables, parts):
    if BETA_MARK not in body:
        _fail("Import summary lacks the expected beta notice")
    pv = re.search(r"(?:\u5149\u4f0f\u7269\u4f53|PV objects)\s*:\s*(\d+)", body)
    other = re.search(r"(?:Other objects|\u5176\u4ed6\u7269\u4f53)\s*:\s*(\d+)", body)
    if not pv or not other:
        _fail("Import summary does not expose the PV and other object counts")
    if int(pv.group(1)) != tables or int(other.group(1)) != parts:
        _fail("Import summary counts %s PV / %s other, expected %d / %d"
              % (pv.group(1), other.group(1), tables, parts))
    section = body.split("Warnings", 1)
    if len(section) != 2:
        _fail("Import summary lacks the recorded warnings section")
    extra = [line.strip() for line in section[1].splitlines() if line.strip()]
    if any(BETA_MARK not in line for line in extra):
        _fail("Import summary contains warnings beyond the fixed beta notice")


def _export_scene(components, name):
    """Use the native SHD export; never patch a native scene record."""
    path = workspace_file(components.workspace, "Shadings", name, ".SHD")
    if path.exists():
        _fail("Native scene export already exists: " + name)
    view = _wait("TPerspectiveForm")
    _check_size(view, "TPerspectiveForm")
    _key(VK_S, (VK_MENU,))
    _key(VK_S)
    form = _wait("TSaveObjPVForm")
    edits = [h for h in _kids(form) if _class_of(h) == "TEdit"]
    if len(edits) != 2:
        _fail("Scene export requires the recorded description and filename fields")
    _set_text(max(edits, key=lambda h: _rect_of(h)[1]), path.stem)
    _click(_find(form, "TPngBitBtn", "保存"))
    _wait("TPerspectiveForm")
    deadline = time.monotonic() + 10
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(.1)
    if not path.is_file():
        _fail("Native scene export did not create the requested SHD")
    return path


def _scene_records(path):
    text = path.read_text(encoding="utf-8-sig")
    return objects(text), text.splitlines(True)


def _scene_context(pre):
    return {"house": pre["house"], "tables": pre["table_profiles"],
            "modules": pre["snapshot"]["modules"], "source": pre["snapshot"]["source"]}


def _verify_scene_geometry(path, pre, *, orientations=False):
    blocks, lines = _scene_records(path)
    context = _scene_context(pre)
    result = {**verify_house_geometry(blocks, lines, context["house"], context["source"]),
              **verify_pv_geometry(blocks, context["tables"], context["modules"], context["source"])}
    if orientations:
        saved = [block for block in blocks if block.object_type == "pvOrient"]
        if len(saved) != 2 or {block.one("NoOrient")[1] for block in saved} != {"1", "2"}:
            _fail("Native scene does not preserve exactly the two source orientation groups")
        for plane in context["source"]["orientations"]:
            block = next(block for block in saved if block.one("NoOrient")[1] == str(plane["id"]))
            if (block.one("FieldType")[1] != "FixedPlane"
                    or float(block.one("FieldTilt")[1]) != plane["tilt_deg"]
                    or float(block.one("FieldAzim")[1]) != plane["field_azimuth_deg"]
                    or any(block.one(key)[1] != "False" for key in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"))):
                _fail("Native scene source orientation parameters changed")
        result["source_orientations_preserved"] = True
    return result


def _select_pv_leaf(index):
    view = _wait("TPerspectiveForm")
    _check_size(view, "TPerspectiveForm")
    left, top = _rect_of(view)[:2]
    _click(_find(view, "TPageControl"), left + 1140, top + 144)
    trees = [h for h in _kids(view) if _class_of(h) == "TVirtualStringTree"
             and _rect_of(h)[1] == top + 188]
    if len(trees) != 1 or index not in (0, 1):
        _fail("The recorded two-table object tree is unavailable")
    _click(trees[0], left + 1230, top + 235 + 24 * index)
    return view, trees[0]


def _open_pv_leaf(index, expected_name):
    view, tree = _select_pv_leaf(index)
    left, top = _rect_of(view)[:2]
    _click(tree, left + 1230, top + 235 + 24 * index)
    form = _wait("TTableFieldForm", timeout=20, activate=False)
    u, _ = _libs()
    deadline = time.monotonic() + 10
    while int(u.GetForegroundWindow() or 0) != form and time.monotonic() < deadline:
        time.sleep(.1)
    _activate(form)
    x, y, right, bottom = _rect_of(form)
    if (right - x, bottom - y) != (1025, 801):
        _fail("PV properties do not match the recorded native layout")
    deadline = time.monotonic() + 10
    while True:
        x, y = _rect_of(form)[:2]
        page = _find(form, "TPageControl")
        if int(u.WindowFromPoint(w.POINT(x + 694, y + 119)) or 0) == page:
            _click(page, x + 694, y + 119)
        if any(_class_of(h) == "TGroupBox" and _string_of(h) == "描述" for h in _kids(form)):
            break
        if time.monotonic() >= deadline:
            _fail("PV properties did not finish loading the requested basic parameters")
        time.sleep(.2)
    description = _find(form, "TGroupBox", "描述")
    name = _get_text(_find(description, "TEdit"))
    if name != expected_name:
        _fail("The selected native PV leaf differs from the exported scene identity")
    return form


def _place_grouped_tables(pre, imported_scene):
    """Assign each imported plane and explicitly place it through native controls.

    Import angles identify candidates only. Successful geometry acceptance uses
    the strict saved-coordinate verifier after placement, never this angle range.
    """
    blocks, _ = _scene_records(imported_scene)
    records = [block for block in blocks if block.object_type == "pvShdArrayTable"]
    source = pre["snapshot"]["source"]
    if len(records) != 2 or source["field_azimuth_deg"] != 0:
        _fail("Native grouped placement requires two regions and anchor azimuth zero")
    assignments, used = [], set()
    for index, block in enumerate(records):
        tilt, azimuth = (float(block.one(key)[1]) for key in ("DInclin", "DAzimut"))
        candidates = [plane for plane in source["orientations"]
                      if math.isfinite(tilt) and math.isfinite(azimuth)
                      and abs(tilt - plane["tilt_deg"]) < .1
                      and abs((azimuth - plane["field_azimuth_deg"] + 180) % 360 - 180) < .1]
        if len(candidates) != 1 or candidates[0]["id"] in used:
            _fail("Imported planes cannot be assigned uniquely to the declared source regions")
        identifier = candidates[0]["id"]
        used.add(identifier)
        profile = next(table for table in pre["table_profiles"] if table["orientation_id"] == identifier)
        form = _open_pv_leaf(index, block.one("Comment")[1])
        combo = _find(_find(form, "TGroupBox", "朝向"), "TComboBox")
        choices = [i for i, label in enumerate(_combo_items(combo)) if label.startswith("#%d:" % identifier)]
        if len(choices) != 1:
            _fail("PV properties lack the requested native system orientation")
        _combo_select(combo, choices[0])
        _click(_find(form, "TPngBitBtn", "关闭物体"))
        view, _ = _select_pv_leaf(index)
        _key(0x42, (VK_CONTROL,))
        position = _find(view, "TGroupBox", "位置")
        edits = sorted((h for h in _kids(position) if _class_of(h) == "TEdit"), key=lambda h: _rect_of(h)[1])
        if len(edits) != 3:
            _fail("Native placement requires the recorded XYZ controls")
        x, y, z = (float(_num(value)) for value in profile["origin_m"])
        target = (-x, -y, z)
        for edit, value in zip(edits, target):
            # VCL compares rounded display values. An explicit intermediate move
            # forces the final requested coordinate to be committed at full precision.
            for coordinate in (value + 1, value):
                _click(edit)
                _set_text(edit, _placement_text(coordinate, check=coordinate == value))
                _key(VK_RETURN)
        assignments.append({"source_table": profile["name"], "orientation_id": identifier,
                            "native_name": block.one("Comment")[1],
                            "imported_origin_m": block.one("DOrigine")[1],
                            "requested_native_origin_m": list(target)})
    return assignments


def _placement_text(value, *, check=True):
    # The recorded native position editor consumes seven text characters even
    # though WM_GETTEXT can return more. Round before entry, then require the
    # existing geometry precision; do not silently accept native truncation.
    for decimals in range(6, -1, -1):
        text = f"{value:.{decimals}f}".rstrip("0").rstrip(".") if decimals else f"{value:.0f}"
        if len(text) <= 7:
            if check and abs(float(text) - value) > 2e-5:
                _fail("Requested coordinate exceeds the recorded native editor precision")
            return text
    _fail("Requested coordinate exceeds the recorded native editor range")


def _remove_empty_import_orientation():
    _key(VK_O, (VK_CONTROL, VK_SHIFT))
    manager = _wait("TOrientManageForm")
    _check_size(manager, "TOrientManageForm")
    left, top = _rect_of(manager)[:2]
    toolbar = _find(manager, "TActionToolBar", "OrientsActionToolBar")
    tree = max((h for h in _kids(manager) if _class_of(h) == "TVirtualStringTree"),
               key=lambda h: _rect_of(h)[2] - _rect_of(h)[0])
    _click(tree, left + 240, top + 497)
    _click(toolbar, left + 273, top + 245)
    _click(_find(manager, "TPngBitBtn", "关闭"))
    _wait("TPerspectiveForm")


def _link_orientation(tables):
    _key(VK_O, (VK_CONTROL, VK_SHIFT))
    manager = _wait("TOrientManageForm", timeout=20.0)
    _check_size(manager, "TOrientManageForm")
    u, _ = _libs()
    left, top = _rect_of(manager)[:2]
    tree = max((h for h in _kids(manager) if _class_of(h) == "TVirtualStringTree"),
               key=lambda h: _rect_of(h)[2] - _rect_of(h)[0])
    for point in (IMPORT_FOLDER, SOURCE_GROUP):
        if int(u.WindowFromPoint(w.POINT(left + point[0], top + point[1])) or 0) != tree:
            _fail("Orientation drag endpoints do not belong to the expected tree")
    _u_set_cursor(left + IMPORT_FOLDER[0], top + IMPORT_FOLDER[1])
    try:
        u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
        for step in range(1, DRAG_STEPS + 1):
            if int(u.GetForegroundWindow() or 0) != manager:
                _fail("Orientation manager lost foreground during the drag")
            _u_set_cursor(left + IMPORT_FOLDER[0] + round(
                              (SOURCE_GROUP[0] - IMPORT_FOLDER[0]) * step / DRAG_STEPS),
                          top + IMPORT_FOLDER[1] - round(
                              (IMPORT_FOLDER[1] - SOURCE_GROUP[1]) * step / DRAG_STEPS))
            time.sleep(0.02)
    finally:
        u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)
    choice = _wait("TActionSelectionForm", timeout=15.0)
    combo = _find(choice, "TComboBox")
    offered = _get_text(combo)
    if offered != MODIFY_FIELD:
        _fail("Orientation drop offered %r, not the recorded modify action" % offered)
    _click(_find(choice, "TPngBitBtn", "OK"))
    manager = _wait("TOrientManageForm", timeout=15.0)
    _check_size(manager, "TOrientManageForm")
    left, top = _rect_of(manager)[:2]
    tree = max((h for h in _kids(manager) if _class_of(h) == "TVirtualStringTree"),
               key=lambda h: _rect_of(h)[2] - _rect_of(h)[0])
    _click(tree, left + 240, top + ORIENT_EMPTY_ROOT_BASE + ORIENT_ROW_HEIGHT * tables)
    _click(_find(manager, "TActionToolBar", "OrientsActionToolBar"),
           left + ORIENT_TOOLBAR[0], top + ORIENT_TOOLBAR[1])
    _click(_find(manager, "TPngBitBtn", "\u5173\u95ed"))
    _wait("TPerspectiveForm", timeout=20.0)


def _u_set_cursor(x, y):
    u, _ = _libs()
    u.SetCursorPos(int(x), int(y))


def _validate_scene():
    _key(VK_Y, (VK_MENU,))
    _key(VK_X)
    box = _wait("TMessageForm", timeout=20.0)
    body = _clipboard_text(box)
    _click(_find(box, "TButton", "\u786e\u5b9a"))
    sections = [part.strip() for part in re.split(r"-{5,}", body) if part.strip()]
    valid = len(sections) == 3 and sections[0] == CAP_INFO and sections[1] == VALID_SCENE
    return {"valid": valid, "text": body}


def _close_scene():
    view = _wait("TPerspectiveForm", timeout=20.0)
    _click(_find(view, "TPngBitBtn", "\u5173\u95ed\u573a\u666f"))
    shadings = _wait("TShadingsForm", timeout=20.0)
    _click(_find(shadings, "TPngBitBtn", "\u8868\u683c"))
    table = _wait("TShdFactorTableForm", timeout=60.0)
    _post_close(table)
    shadings = _wait("TShadingsForm", timeout=20.0)
    _click(_find(shadings, "TPngBitBtn", "\u786e\u5b9a"))
    _wait("TProjectForm", timeout=20.0)


def _module_layout_mode(*, select):
    root = _wait("TProjectForm")
    _click(_find(root, "TPngBitBtn", "\u8fd1\u5904\u906e\u6321"))
    shading = _wait("TShadingsForm")
    radio = _find(shading, "TRadioButton", "\u7cbe\u7ec6\u8ba1\u7b97\u6a21\u5f0f (\u57fa\u4e8e\u7ec4\u4ef6\u5e03\u5c40)")
    if select and _send(radio, BM_GETCHECK) != 1:
        _click(radio)
    checked = _send(radio, BM_GETCHECK) == 1
    _click(_find(shading, "TPngBitBtn", "\u786e\u5b9a" if select else "\u53d6\u6d88"))
    _wait("TProjectForm")
    if not checked:
        _fail("Native reloaded calculation mode is not detailed Module Layout")


def _configure_module_layout(actions):
    root = _wait("TProjectForm")
    _click(_find(root, "TPngBitBtn", "\u7ec4\u4ef6\u5e03\u5c40"))
    form = _wait("TModuleLayoutForm")
    _check_size(form, "TModuleLayoutForm")
    landscape = _find(form, "TRadioButton", "\u6a2a\u5411")
    if _send(landscape, BM_GETCHECK) != 1:
        _click(landscape)
    whole = _find(form, "TTabSheet", "\u6574\u4e2a\u7cfb\u7edf")
    edits = [handle for handle in _kids(whole) if _class_of(handle) == "TEdit"]
    if len(edits) != 2:
        _fail("Module Layout spacing controls differ from the recorded native form")
    for edit in edits:
        _click(edit)
        _set_text(edit, "0")
        _key(VK_TAB)
        if float(_get_text(edit)) != 0:
            _fail("Native Module Layout did not retain zero spacing")
    _click(_find(form, "TPngBitBtn", "\u653e\u7f6e\u6240\u6709\u7ec4\u4ef6"))
    actions.append("place_native_modules")
    form = _wait("TModuleLayoutForm")
    _click(_find(form, "TPngBitBtn", "\u81ea\u52a8\u5212\u5206"))
    allocation = _wait("TMLStringsAttribForm")
    _check_size(allocation, "TMLStringsAttribForm")
    for caption in ("\u7ec4\u4e32\u4f4d\u4e8e 1 \u884c", "\u6c34\u5e73\u5730", "\u81ea\u5de6\u4e0b", "\u4ece\u5de6\u5230\u53f3\u7ee7\u7eed"):
        radio = _find(allocation, "TRadioButton", caption)
        if _send(radio, BM_GETCHECK) != 1:
            _click(radio)
        if _send(radio, BM_GETCHECK) != 1:
            _fail("Native automatic string allocation did not retain the requested pattern")
    _click(_find(allocation, "TPngBitBtn", "\u5206\u914d\u6240\u6709"))
    actions.append("allocate_native_strings")
    _click(_find(allocation, "TPngBitBtn", "\u5173\u95ed"))
    form = _wait("TModuleLayoutForm")
    _click(_find(form, "TPngBitBtn", "\u786e\u5b9a"))
    _wait("TProjectForm")
    actions.append("accept_native_module_layout")
    _module_layout_mode(select=True)
    actions.append("select_detailed_electrical_shading")


def _post_close(hwnd):
    u, _ = _libs()
    if not u.PostMessageW(w.HWND(hwnd), WM_CLOSE, 0, 0):
        _fail("PostMessage WM_CLOSE failed")


def _save_new(project, variant_name):
    projects = _ini_datapath() / "Projects"
    pattern = Path(project).stem + ".V*"
    before = set(projects.glob(pattern))
    root = _wait("TProjectForm", timeout=10.0)
    _toolbar_save(root)
    form = _wait("TSaveObjPVForm", timeout=20.0)
    edits = [h for h in _kids(form) if _class_of(h) == "TEdit" and _describe(h)["enabled"]]
    if len(edits) != 1:
        _fail("Save dialog does not expose exactly one editable field")
    _click(edits[0])
    _set_text(edits[0], variant_name)
    _click(_find(form, "TPngBitBtn", "\u53e6\u5b58\u4e3a\u65b0\u6587\u4ef6"))
    info = _wait("TMessageForm", timeout=20.0)
    if _string_of(info) != CAP_INFO:
        _fail("Unexpected confirmation dialog title during save")
    _click(_find(info, "TButton", "\u786e\u5b9a"))
    _wait("TProjectForm", timeout=20.0)
    # The project form can return before PVsyst has written the new VC; poll like _export_scene.
    deadline = time.monotonic() + 10
    created = set(projects.glob(pattern)) - before
    while not created and time.monotonic() < deadline:
        time.sleep(.1)
        created = set(projects.glob(pattern)) - before
    if len(created) != 1:
        _fail("Expected exactly one new variant file, found %d" % len(created))
    return created.pop()


def _verify_new(path, source_bytes, expect_parts, expect_tables, expect_modules, *, scene_context=None,
                 shading_mode="linear"):
    def parsed(raw):
        return objects(raw.decode("utf-8-sig"))
    target_bytes = Path(path).read_bytes()
    blocks = parsed(target_bytes)
    layout = {}
    if shading_mode == "module_layout":
        try:
            layout["module_layout"] = verify_module_layout(blocks, target_bytes.decode("utf-8-sig").splitlines(True))
        except (ValueError, KeyError) as exc:
            _fail(str(exc))
    tables = [block for block in blocks if block.object_type == "pvShdArrayTable"]
    generic = [block for block in blocks if block.object_type == "pvShdGenObject"]
    modules = sum(int(block.one("NModInHeight")[1]) * int(block.one("NModInWidth")[1]) for block in tables)
    if len(generic) != expect_parts or len(tables) != expect_tables:
        _fail("New variant holds %d house objects and %d PV tables, expected %d and %d"
              % (len(generic), len(tables), expect_parts, expect_tables))
    if modules != expect_modules:
        _fail("New variant module total is %d, expected %d" % (modules, expect_modules))
    if scene_context is not None and scene_context["source"].get("orientations"):
        source_text, target_text = (raw.decode("utf-8-sig") for raw in (source_bytes, target_bytes))
        try:
            hardware = verify_grouped_hardware(parsed(source_bytes), blocks, source_text.splitlines(True),
                                               target_text.splitlines(True), scene_context["source"])
            house_geometry = verify_house_geometry(blocks, target_text.splitlines(True), scene_context["house"],
                                                    scene_context["source"])
            pv_geometry = verify_pv_geometry(blocks, scene_context["tables"], scene_context["modules"],
                                             scene_context["source"])
        except ValueError as exc:
            _fail(str(exc))
        return {"objects": len(generic) + len(tables), "house": len(generic), "tables": len(tables),
                "modules": modules, **hardware, **house_geometry, **pv_geometry, **layout}
    if len([block for block in blocks if block.object_type == "pvOrient"]) != 1 or any(
            block.one("NoOrient")[1] != "1" for block in tables):
        _fail("New variant does not reference a single orientation")
    if any(block.one("Source")[1] != "osPVCollada" for block in generic + tables):
        _fail("New variant does not preserve the native PVCollada provenance")
    original = next(block for block in parsed(source_bytes) if block.object_type == "pvSubArray")
    target = next(block for block in blocks if block.object_type == "pvSubArray")
    for field in ("PVModule", "GInverter", "NModSerie", "NStringCh", "NStrOrient1", "NInverter", "NInvMPPT"):
        if original.one(field)[1] != target.one(field)[1]:
            _fail("Saved variant hardware differs from the source: " + field)
    return {"objects": len(generic) + len(tables), "house": len(generic), "tables": len(tables),
            "modules": modules, "source_hardware_preserved": True}

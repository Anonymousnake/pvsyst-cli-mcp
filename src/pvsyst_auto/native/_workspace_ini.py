"""Preserve PVsystCLI's per-user workspace selection, not its run accounting."""
from __future__ import annotations

import codecs
import ctypes
import hashlib
import os
import re
import stat
import sys
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

from pvsyst_auto.native._winlaunch import NativeProcessNotStopped
from pvsyst_auto.project.fileio import atomic_replace

_SUPPORTED = {(8, 1, 6, 49278), (8, 1, 7, 49382)}
_LOCK_TIMEOUT_MS = 120_000
_SECTIONS = {"system": "System", "recentworkspaces": "RecentWorkspaces"}
_FIELDS = {
    "system": re.compile(r"^[ \t]*DataPath[ \t]*=", re.ASCII | re.IGNORECASE),
    "recentworkspaces": re.compile(r"^[ \t]*Workspace[0-9]+[ \t]*=", re.ASCII | re.IGNORECASE),
}
_HEADER = re.compile(r"^[ \t]*\[([^\]\r\n]+)\]")
_LINES = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")
_EOL = re.compile(r"\r\n|\r|\n")


class WorkspaceSettingsError(OSError):
    """Could not safely guard or restore the native workspace settings."""


def _file_version(executable: Path) -> tuple[int, int, int, int] | None:
    # Read the fixed PE resource; running `version` could itself write the INI.
    version = ctypes.WinDLL("version", use_last_error=True)
    size_of = version.GetFileVersionInfoSizeW
    size_of.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
    size_of.restype = wintypes.DWORD
    read = version.GetFileVersionInfoW
    read.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    read.restype = wintypes.BOOL
    query = version.VerQueryValueW
    query.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                      ctypes.POINTER(wintypes.UINT))
    query.restype = wintypes.BOOL
    path = str(executable)
    size = size_of(path, None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not read(path, 0, size, data):
        raise WorkspaceSettingsError("Could not read the PVsystCLI file-version resource")
    if (not query(data, "\\", ctypes.byref(pointer), ctypes.byref(length))
            or length.value < 13 * ctypes.sizeof(wintypes.DWORD)):
        return None
    fixed = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if fixed[0] != 0xFEEF04BD:
        return None
    return fixed[2] >> 16, fixed[2] & 0xFFFF, fixed[3] >> 16, fixed[3] & 0xFFFF


def _is_supported(executable: Path) -> bool:
    # The sibling GUI executable carries the same fixed file version.
    return (os.name == "nt" and executable.name.casefold() == "pvsystcli.exe"
            and _file_version(executable) in _SUPPORTED)


def _profile_ini() -> Path:
    # CSIDL_LOCAL_APPDATA, SHGFP_TYPE_CURRENT: use the caller's Windows profile,
    # not a mutable environment override or a workspace/install-relative path.
    get_folder = ctypes.WinDLL("shell32").SHGetFolderPathW
    get_folder.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                           wintypes.DWORD, wintypes.LPWSTR)
    get_folder.restype = ctypes.c_long
    folder = ctypes.create_unicode_buffer(260)
    if get_folder(None, 0x1C, None, 0, folder) != 0 or not Path(folder.value).is_absolute():
        raise WorkspaceSettingsError("Could not locate the current Windows user's Local AppData")
    return Path(folder.value) / "PVsystCLI" / "8.1" / "Admin" / "PVsystCLI.ini"


@contextmanager
def _profile_lock(path: Path):
    # Neither the selected workspace nor the executable version identifies the INI.
    # Global scope also serializes this profile across Windows sessions.
    identity = hashlib.sha256(os.path.normcase(path).encode("utf-8")).hexdigest()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    release, close = kernel.ReleaseMutex, kernel.CloseHandle
    release.argtypes = close.argtypes = (wintypes.HANDLE,)
    release.restype = close.restype = wintypes.BOOL
    handle = create(None, False, f"Global\\pvsyst_auto_workspace_{identity}")
    if not handle:
        raise WorkspaceSettingsError("Could not open the PVsystCLI workspace settings lock")
    try:
        result = wait(handle, _LOCK_TIMEOUT_MS)
        if result == 0x102:  # WAIT_TIMEOUT
            raise WorkspaceSettingsError("Timed out waiting for the PVsystCLI workspace settings lock")
        if result not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED (ownership transferred)
            raise WorkspaceSettingsError("Could not acquire the PVsystCLI workspace settings lock")
        try:
            yield
        finally:
            release(handle)
    finally:
        close(handle)


def _read_ini(path: Path) -> bytes | None:
    # Do not follow links/junctions below the current user's Local AppData.
    try:
        for part in (*reversed(path.parents[:3]), path):
            if getattr(part.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise WorkspaceSettingsError("Refusing a linked PVsystCLI workspace settings path")
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _decode(raw: bytes) -> tuple[bytes, str, str, str]:
    for bom, encoding in ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(bom):
            errors = "surrogatepass"
            break
    else:
        bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
        encoding, errors = "utf-8", "surrogateescape"
    text = raw[len(bom):].decode(encoding, errors=errors)
    if "\0" in text:
        raise WorkspaceSettingsError("Unsupported PVsystCLI INI encoding")
    return bom, encoding, errors, text


def _sections(text: str):
    lines = _LINES.findall(text)
    start, name = 0, ""
    seen = set()
    for index, line in enumerate(lines):
        if header := _HEADER.match(line):
            yield name, lines[start:index]
            start, name = index, header[1].strip().lower()
            if name in _FIELDS:
                if name in seen:
                    raise WorkspaceSettingsError("Duplicate PVsystCLI workspace settings section")
                seen.add(name)
    yield name, lines[start:]


def _snapshot(raw: bytes | None):
    bom, encoding, errors, text = _decode(raw or b"")
    fields = {name: [line for line in lines[1:] if _FIELDS[name].match(line)]
              for name, lines in _sections(text) if name in _FIELDS}
    return (bom, encoding, errors), fields


def _merge(snapshot, current: bytes | None) -> bytes | None:
    original_format, original_fields = snapshot
    fields = original_fields.copy()
    if current is None:
        if not any(fields.values()):
            return None
        bom, encoding, errors = original_format
        text = ""
    else:
        bom, encoding, errors, text = _decode(current)
    newline = _EOL.search(text)
    newline = newline[0] if newline else "\r\n"
    result: list[str] = []
    for name, lines in _sections(text):
        if name not in _FIELDS:
            result.extend(lines)
            continue
        saved = fields.pop(name, [])
        slots = [index for index, line in enumerate(lines) if _FIELDS[name].match(line)]
        if not slots:
            result.extend(lines[:1] + saved + lines[1:])
            continue
        start = 0
        for ordinal, index in enumerate(slots):
            result.extend(lines[start:index])
            end = ordinal + 1 if ordinal + 1 < len(slots) else len(saved)
            result.extend(saved[ordinal:end])
            start = index + 1
        result.extend(lines[start:])
    for name, saved in fields.items():
        if saved:
            result.extend([f"[{_SECTIONS[name]}]{newline}", *saved])
    # A restored last line can be inserted before other lines; add a separator
    # only where a previously unterminated line cannot remain the last one.
    return bom + "".join(line if i == len(result) - 1 or line.endswith(("\r", "\n")) else line + newline
                         for i, line in enumerate(result)).encode(encoding, errors=errors)


@contextmanager
def preserve_workspace_settings(executable: Path, *, restore: bool):
    """Serialize supported CLI launches; optionally restore only workspace fields.

    The caller must stop the native writer before leaving the context. The mutex
    belongs to cooperating adapter calls, not GUI sessions or raw CLI launches.
    """
    if not _is_supported(executable):
        yield
        return
    path = _profile_ini()
    with _profile_lock(path):
        if not restore:
            yield
            return
        try:
            snapshot = _snapshot(_read_ini(path))
        except (OSError, UnicodeError) as exc:
            raise WorkspaceSettingsError("Could not snapshot the PVsystCLI workspace settings") from exc
        try:
            yield
        finally:
            if not isinstance(sys.exc_info()[1], NativeProcessNotStopped):
                try:
                    current = _read_ini(path)
                    restored = _merge(snapshot, current)
                    if restored is not None and restored != current:
                        def unchanged():
                            # ponytail: recheck, not an interlock with raw CLI writers.
                            if _read_ini(path) != current:
                                raise WorkspaceSettingsError("PVsystCLI INI changed during restoration")

                        atomic_replace(path, restored, unchanged)
                except (OSError, UnicodeError) as exc:
                    raise WorkspaceSettingsError("Could not restore the PVsystCLI workspace settings") from exc

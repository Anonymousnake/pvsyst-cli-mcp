"""Windows-only isolated launcher for the installed PVsystCLI.

Runs one real CLI invocation on a freshly created private, non-input desktop
inside an owned Job object, so concurrent CLI calls never touch the caller's
window station/desktop. Only the three stdio handles are inherited into the
child (STARTUPINFOEX handle list), matching ``subprocess.run``'s close_fds
isolation instead of leaking every inheritable handle.

``run()`` returns a ``subprocess.CompletedProcess[str]`` or raises
``subprocess.TimeoutExpired`` with the partial capture, exactly like the
``subprocess.run(capture_output=True, text=True, timeout=...)`` call it
replaces. Non-Windows callers keep using ``subprocess.run`` directly.
"""
from __future__ import annotations

import codecs
import ctypes
import hashlib
import locale
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

from pvsyst_auto.project.paths import atomic_replace

# Desktop access without SWITCHDESKTOP (0x0100): we never switch the visible
# desktop and never need to.
_DESKTOP_ACCESS = 0x0001 | 0x0002 | 0x0040 | 0x0080
_STARTF_USESTDHANDLES = 0x00000100
_CREATE_SUSPENDED = 0x00000004
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
# PROC_THREAD_ATTRIBUTE_HANDLE_LIST = ProcThreadAttributeValue(2, FALSE, TRUE, FALSE)
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_GENERIC_WRITE = 0x40000000
_GENERIC_READ = 0x80000000
_FILE_SHARE_RW = 0x00000003
_FILE_SHARE_DELETE = 0x00000004
_CREATE_ALWAYS = 2
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF
# CreateFileW reports failure with INVALID_HANDLE_VALUE (-1), never with NULL.
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class NativeProcessNotStopped(OSError):
    """Native process exit could not be confirmed during cleanup."""

# Desktop DACL values for the "no window" path. Create with DESKTOP_ALL_ACCESS
# so this handle carries WRITE_DAC, which SetUserObjectSecurity needs to attach
# the deny ACE; the resulting DACL then grants this user every desktop right
# except DESKTOP_CREATEWINDOW (proven OS primitive: standard ACL only, no
# hook/patch/injection).
_DESKTOP_ALL = 0x000F01FF
_DESKTOP_CREATEWINDOW = 0x0002
_UOI_NAME = 2
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_DACL_SECURITY_INFORMATION = 0x00000004

_u = _k = _adv = None
_CreateDesktopW = _CloseDesktop = _CreateProcessW = _CreateFileW = _CloseHandle = None
_TerminateProcess = _WaitForSingleObject = _GetExitCodeProcess = _ResumeThread = None
_CreateJobObjectW = _SetInformationJobObject = _AssignProcessToJobObject = None
_TerminateJobObject = _InitializeProcThreadAttributeList = _UpdateProcThreadAttribute = None
_DeleteProcThreadAttributeList = _GetProcessWindowStation = _GetUserObjectInformationW = None
_SetUserObjectSecurity = _GetCurrentProcess = _LocalFree = _OpenProcessToken = None
_GetTokenInformation = _ConvertSidToStringSidW = None
_ConvertStringSecurityDescriptorToSecurityDescriptorW = None


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _LARGE_INTEGER(ctypes.Union):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER), ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


if os.name == "nt":
    _u = ctypes.WinDLL("user32", use_last_error=True)
    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)

    _CreateDesktopW = _u.CreateDesktopW
    _CreateDesktopW.restype = wintypes.HANDLE
    _CreateDesktopW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    _CloseDesktop = _u.CloseDesktop
    _CloseDesktop.argtypes = (wintypes.HANDLE,)
    _CloseDesktop.restype = wintypes.BOOL

    _CreateProcessW = _k.CreateProcessW
    _CreateProcessW.restype = wintypes.BOOL
    _CreateProcessW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION))
    _CreateFileW = _k.CreateFileW
    _CreateFileW.restype = wintypes.HANDLE
    _CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE)
    _CloseHandle = _k.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL
    _TerminateProcess = _k.TerminateProcess
    _TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _TerminateProcess.restype = wintypes.BOOL
    _WaitForSingleObject = _k.WaitForSingleObject
    _WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _WaitForSingleObject.restype = wintypes.DWORD
    _GetExitCodeProcess = _k.GetExitCodeProcess
    _GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _GetExitCodeProcess.restype = wintypes.BOOL
    _ResumeThread = _k.ResumeThread
    _ResumeThread.argtypes = (wintypes.HANDLE,)
    _ResumeThread.restype = wintypes.DWORD
    _CreateJobObjectW = _k.CreateJobObjectW
    _CreateJobObjectW.restype = wintypes.HANDLE
    _CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _SetInformationJobObject = _k.SetInformationJobObject
    _SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _SetInformationJobObject.restype = wintypes.BOOL
    _AssignProcessToJobObject = _k.AssignProcessToJobObject
    _AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _AssignProcessToJobObject.restype = wintypes.BOOL
    _TerminateJobObject = _k.TerminateJobObject
    _TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _TerminateJobObject.restype = wintypes.BOOL
    _InitializeProcThreadAttributeList = _k.InitializeProcThreadAttributeList
    _InitializeProcThreadAttributeList.argtypes = (ctypes.c_void_p, wintypes.DWORD,
                                                   wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t))
    _InitializeProcThreadAttributeList.restype = wintypes.BOOL
    _UpdateProcThreadAttribute = _k.UpdateProcThreadAttribute
    _UpdateProcThreadAttribute.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                           ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_size_t))
    _UpdateProcThreadAttribute.restype = wintypes.BOOL
    _DeleteProcThreadAttributeList = _k.DeleteProcThreadAttributeList
    _DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
    _DeleteProcThreadAttributeList.restype = None

    _GetProcessWindowStation = _u.GetProcessWindowStation
    _GetProcessWindowStation.restype = wintypes.HANDLE
    _GetUserObjectInformationW = _u.GetUserObjectInformationW
    _GetUserObjectInformationW.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _GetUserObjectInformationW.restype = wintypes.BOOL
    _SetUserObjectSecurity = _u.SetUserObjectSecurity
    _SetUserObjectSecurity.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    _SetUserObjectSecurity.restype = wintypes.BOOL

    _GetCurrentProcess = _k.GetCurrentProcess
    _GetCurrentProcess.restype = wintypes.HANDLE
    _LocalFree = _k.LocalFree
    _LocalFree.argtypes = (wintypes.HLOCAL,)
    _LocalFree.restype = wintypes.HLOCAL
    _OpenProcessToken = _adv.OpenProcessToken
    _OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    _OpenProcessToken.restype = wintypes.BOOL
    _GetTokenInformation = _adv.GetTokenInformation
    _GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _GetTokenInformation.restype = wintypes.BOOL
    _ConvertSidToStringSidW = _adv.ConvertSidToStringSidW
    _ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    _ConvertSidToStringSidW.restype = wintypes.BOOL
    _ConvertStringSecurityDescriptorToSecurityDescriptorW = (
        _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW)
    _ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG))
    _ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL


def _fail(step: str) -> OSError:
    return OSError(0, "%s failed (winerror %d)" % (step, ctypes.get_last_error()))


def _open_file(path: str, access: int, disposition: int, sa, step: str,
               share: int = _FILE_SHARE_RW):
    handle = _CreateFileW(path, access, share, ctypes.byref(sa), disposition,
                          _FILE_ATTRIBUTE_NORMAL, None)
    if not handle or handle == _INVALID_HANDLE_VALUE:
        raise _fail(step)
    return handle


def _read_stream(path: str) -> str:
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except OSError:
        return ""
    # Mirror text=True's universal-newline translation, then the CLI's own
    # encoding and 'replace' error handling.
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data.decode(locale.getpreferredencoding(False), errors="replace")


def _current_station_name() -> str:
    """Name of the window station this process is attached to (borrowed handle)."""
    hsta = _GetProcessWindowStation()
    if not hsta:
        raise _fail("GetProcessWindowStation")
    needed = wintypes.DWORD(0)
    _GetUserObjectInformationW(hsta, _UOI_NAME, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise _fail("GetUserObjectInformationW(name-size)")
    buf = ctypes.create_unicode_buffer(needed.value // 2 + 1)
    if not _GetUserObjectInformationW(hsta, _UOI_NAME, buf, needed.value, ctypes.byref(needed)):
        raise _fail("GetUserObjectInformationW(name)")
    return buf.value


def _current_user_sid() -> str:
    """String SID of the token running this process, for the desktop DACL."""
    token = wintypes.HANDLE()
    if not _OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _fail("OpenProcessToken")
    try:
        size = wintypes.DWORD(0)
        _GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if not size.value:
            raise _fail("GetTokenInformation(size)")
        buffer = ctypes.create_string_buffer(size.value)
        if not _GetTokenInformation(token, _TOKEN_USER, buffer, size.value, ctypes.byref(size)):
            raise _fail("GetTokenInformation")
        sid_ptr = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        string_sid = wintypes.LPWSTR()
        if not _ConvertSidToStringSidW(sid_ptr, ctypes.byref(string_sid)):
            raise _fail("ConvertSidToStringSidW")
        try:
            return string_sid.value
        finally:
            _LocalFree(ctypes.cast(string_sid, wintypes.HLOCAL))
    finally:
        _CloseHandle(token)


def _deny_window_creation(hdesk) -> None:
    """Deny only DESKTOP_CREATEWINDOW to this user on our own new desktop."""
    sid = _current_user_sid()
    sddl = "D:(D;;0x%08X;;;%s)(A;;0x%08X;;;%s)" % (
        _DESKTOP_CREATEWINDOW, sid, _DESKTOP_ALL, sid)
    psd = ctypes.c_void_p()
    size = wintypes.ULONG(0)
    if not _ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(psd), ctypes.byref(size)):
        raise _fail("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        info = wintypes.DWORD(_DACL_SECURITY_INFORMATION)
        if not _SetUserObjectSecurity(hdesk, ctypes.byref(info), psd):
            raise _fail("SetUserObjectSecurity")
    finally:
        _LocalFree(psd)


def run(argv: list[str], cwd, timeout, *,
        allow_window_creation: bool = True) -> subprocess.CompletedProcess[str]:
    """Launch ``argv`` on a private non-input desktop and reap its owned tree.

    With ``allow_window_creation=False`` the private desktop additionally denies
    DESKTOP_CREATEWINDOW to this user before the child starts, so a bare numeric
    CSV run that must never build a window fails loudly instead of showing one.
    The desktop always lives on the process's *current* window station (resolved
    via GetProcessWindowStation, not a hardcoded WinSta0).

    Returns ``CompletedProcess[str]`` on exit (any code); raises
    ``subprocess.TimeoutExpired`` (partial output attached) on timeout; raises
    ``OSError`` if an isolation step fails (never falls back to the default
    desktop).
    """
    if os.name != "nt":
        raise OSError("_winlaunch requires Windows")
    argv = [str(item) for item in argv]
    work_dir = None if cwd is None else os.fsdecode(os.fspath(cwd))
    if timeout is None:
        timeout_ms = _INFINITE
    else:
        if isinstance(timeout, float) and math.isnan(timeout):
            raise ValueError("timeout must not be NaN")
        timeout_ms = max(0, int(timeout * 1000))
        if timeout_ms >= _INFINITE:
            raise OverflowError("timeout is too large")
    # uuid4 is per-call random and independent of the Windows clock, so two
    # threads in one process can never collide on a desktop name; pid kept for
    # readable provenance only (nothing relies on the exact name format).
    desktop_name = "pvcli_priv_%d_%s" % (os.getpid(), uuid.uuid4().hex)
    # Use temp files (not pipes) so large stdout/stderr cannot deadlock;
    # read back after the process exits, then removed.
    tmpdir = tempfile.mkdtemp(prefix="pvcli_")
    out_path = os.path.join(tmpdir, "stdout")
    err_path = os.path.join(tmpdir, "stderr")
    hdesk = hjob = hproc = hthread = None
    hout = herr = hnin = None
    attr = None
    try:
        # Own private desktop on the CURRENT station; names resolve at runtime so
        # a non-interactive service station works exactly like WinSta0.
        station = _current_station_name()
        access = _DESKTOP_ACCESS if allow_window_creation else _DESKTOP_ALL
        hdesk = _CreateDesktopW(desktop_name, None, None, 0, access, None)
        if not hdesk:
            raise _fail("CreateDesktopW")
        if not allow_window_creation:
            _deny_window_creation(hdesk)

        hjob = _CreateJobObjectW(None, None)
        if not hjob:
            raise _fail("CreateJobObjectW")
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _SetInformationJobObject(hjob, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                        ctypes.byref(limits), ctypes.sizeof(limits)):
            raise _fail("SetInformationJobObject")

        sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
        temp_share = _FILE_SHARE_RW | _FILE_SHARE_DELETE
        hout = _open_file(out_path, _GENERIC_WRITE, _CREATE_ALWAYS, sa, "CreateFileW(stdout)",
                          share=temp_share)
        herr = _open_file(err_path, _GENERIC_WRITE, _CREATE_ALWAYS, sa, "CreateFileW(stderr)",
                          share=temp_share)
        hnin = _open_file("NUL", _GENERIC_READ, _OPEN_EXISTING, sa, "CreateFileW(stdin)")

        size = ctypes.c_size_t(0)
        _InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise _fail("InitializeProcThreadAttributeList(size)")
        attr_buffer = ctypes.create_string_buffer(size.value)
        raw_attr = ctypes.cast(attr_buffer, ctypes.c_void_p)
        if not _InitializeProcThreadAttributeList(raw_attr, 1, 0, ctypes.byref(size)):
            raise _fail("InitializeProcThreadAttributeList")
        attr = raw_attr
        handles = (wintypes.HANDLE * 3)(hnin, hout, herr)
        if not _UpdateProcThreadAttribute(attr, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                          ctypes.cast(handles, ctypes.c_void_p),
                                          ctypes.sizeof(handles), None, None):
            raise _fail("UpdateProcThreadAttribute")

        siex = _STARTUPINFOEXW()
        siex.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        siex.StartupInfo.lpDesktop = station + "\\" + desktop_name
        siex.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        siex.StartupInfo.hStdInput = hnin
        siex.StartupInfo.hStdOutput = hout
        siex.StartupInfo.hStdError = herr
        siex.lpAttributeList = attr
        pi = _PROCESS_INFORMATION()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        flags = _EXTENDED_STARTUPINFO_PRESENT | _CREATE_SUSPENDED
        created = _CreateProcessW(
            argv[0], command_line, None, None, True, flags, None, work_dir,
            ctypes.cast(ctypes.byref(siex), ctypes.POINTER(_STARTUPINFOW)), ctypes.byref(pi))
        if not created:
            raise _fail("CreateProcessW")
        hproc, hthread = pi.hProcess, pi.hThread
        # Parent copies no longer needed; the child holds its own.
        _CloseHandle(hout); hout = None
        _CloseHandle(herr); herr = None
        _CloseHandle(hnin); hnin = None

        if not _AssignProcessToJobObject(hjob, hproc):
            raise _fail("AssignProcessToJobObject")
        if _ResumeThread(hthread) == 0xFFFFFFFF:
            raise _fail("ResumeThread")

        wait = _WaitForSingleObject(hproc, timeout_ms)
        if wait == _WAIT_FAILED:
            raise _fail("WaitForSingleObject")
        timed_out = wait == _WAIT_TIMEOUT
        code = wintypes.DWORD(0)
        _GetExitCodeProcess(hproc, ctypes.byref(code))
        # Reap only our owned subtree; never touch processes by image name.
        _TerminateJobObject(hjob, 1 if timed_out else 0)
        if _WaitForSingleObject(hproc, 2000) != _WAIT_OBJECT_0:
            _TerminateProcess(hproc, 1)
        stdout = _read_stream(out_path)
        stderr = _read_stream(err_path)
        if timed_out:
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(argv, int(code.value), stdout, stderr)
    finally:
        stopped = True
        for handle in (hout, herr, hnin):
            if handle:
                _CloseHandle(handle)
        if hthread:
            _CloseHandle(hthread)
        if hproc:
            # No-op if it already exited; kills a child left suspended by a
            # failed assign/resume step.
            _TerminateProcess(hproc, 1)
            stopped = _WaitForSingleObject(hproc, 2000) == _WAIT_OBJECT_0
            _CloseHandle(hproc)
        if hjob:
            _TerminateJobObject(hjob, 0)
            _CloseHandle(hjob)  # last handle close -> KILL_ON_JOB_CLOSE reaps stragglers
        if attr:
            _DeleteProcThreadAttributeList(attr)
        if hdesk:
            _CloseDesktop(hdesk)
        shutil.rmtree(tmpdir, ignore_errors=True)
        if not stopped:
            raise NativeProcessNotStopped("Could not confirm that PVsystCLI stopped; workspace settings were not restored")


# --------------------------------------------------------------------- PVsystCLI workspace INI preservation
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
    version = ctypes.WinDLL("version", use_last_error=True)
    size_of, read, query = version.GetFileVersionInfoSizeW, version.GetFileVersionInfoW, version.VerQueryValueW
    size_of.argtypes, size_of.restype = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)), wintypes.DWORD
    read.argtypes, read.restype = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p), wintypes.BOOL
    query.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT))
    query.restype = wintypes.BOOL
    path = str(executable)
    size = size_of(path, None)
    if not size:
        return None
    data, pointer, length = ctypes.create_string_buffer(size), ctypes.c_void_p(), wintypes.UINT()
    if not read(path, 0, size, data):
        raise WorkspaceSettingsError("Could not read the PVsystCLI file-version resource")
    if not query(data, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 13 * ctypes.sizeof(wintypes.DWORD):
        return None
    fixed = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if fixed[0] != 0xFEEF04BD:
        return None
    return fixed[2] >> 16, fixed[2] & 0xFFFF, fixed[3] >> 16, fixed[3] & 0xFFFF


def _is_supported(executable: Path) -> bool:
    return os.name == "nt" and executable.name.casefold() == "pvsystcli.exe" and _file_version(executable) in _SUPPORTED


def _profile_ini() -> Path:
    get_folder = ctypes.WinDLL("shell32").SHGetFolderPathW
    get_folder.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR)
    get_folder.restype = ctypes.c_long
    folder = ctypes.create_unicode_buffer(260)
    if get_folder(None, 0x1C, None, 0, folder) != 0 or not Path(folder.value).is_absolute():
        raise WorkspaceSettingsError("Could not locate the current Windows user's Local AppData")
    return Path(folder.value) / "PVsystCLI" / "8.1" / "Admin" / "PVsystCLI.ini"


@contextmanager
def _profile_lock(path: Path):
    identity = hashlib.sha256(os.path.normcase(path).encode("utf-8")).hexdigest()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create, wait = kernel.CreateMutexW, kernel.WaitForSingleObject
    create.argtypes, create.restype = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR), wintypes.HANDLE
    wait.argtypes, wait.restype = (wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD
    release, close = kernel.ReleaseMutex, kernel.CloseHandle
    release.argtypes = close.argtypes = (wintypes.HANDLE,)
    release.restype = close.restype = wintypes.BOOL
    handle = create(None, False, f"Global\\pvsyst_auto_workspace_{identity}")
    if not handle:
        raise WorkspaceSettingsError("Could not open the PVsystCLI workspace settings lock")
    try:
        result = wait(handle, _LOCK_TIMEOUT_MS)
        if result == 0x102:
            raise WorkspaceSettingsError("Timed out waiting for the PVsystCLI workspace settings lock")
        if result not in (0, 0x80):
            raise WorkspaceSettingsError("Could not acquire the PVsystCLI workspace settings lock")
        try:
            yield
        finally:
            release(handle)
    finally:
        close(handle)


def _read_ini(path: Path) -> bytes | None:
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
    start, name, seen = 0, "", set()
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
    newline = (_EOL.search(text) or ["\r\n"])[0]
    result: list[str] = []
    for name, lines in _sections(text):
        if name not in _FIELDS:
            result.extend(lines)
            continue
        saved, slots = fields.pop(name, []), [i for i, ln in enumerate(lines) if _FIELDS[name].match(ln)]
        if not slots:
            result.extend(lines[:1] + saved + lines[1:])
            continue
        start = 0
        for ordinal, index in enumerate(slots):
            result.extend(lines[start:index])
            result.extend(saved[ordinal:(ordinal + 1 if ordinal + 1 < len(slots) else len(saved))])
            start = index + 1
        result.extend(lines[start:])
    for name, saved in fields.items():
        if saved:
            result.extend([f"[{_SECTIONS[name]}]{newline}", *saved])
    return bom + "".join(line if i == len(result) - 1 or line.endswith(("\r", "\n")) else line + newline
                         for i, line in enumerate(result)).encode(encoding, errors=errors)


@contextmanager
def preserve_workspace_settings(executable: Path, *, restore: bool):
    """Serialize supported CLI launches; optionally restore only workspace fields."""
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
                            if _read_ini(path) != current:
                                raise WorkspaceSettingsError("PVsystCLI INI changed during restoration")

                        atomic_replace(path, restored, unchanged)
                except (OSError, UnicodeError) as exc:
                    raise WorkspaceSettingsError("Could not restore the PVsystCLI workspace settings") from exc

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

import ctypes
import locale
import os
import shutil
import subprocess
import tempfile
import uuid
from ctypes import wintypes

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
_CREATE_ALWAYS = 2
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF

_u = ctypes.WinDLL("user32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)


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


def _fail(step: str) -> OSError:
    return OSError(0, "%s failed (winerror %d)" % (step, ctypes.get_last_error()))


def _read_stream(path: str) -> str:
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except OSError:
        return ""
    # Mirror text=True's universal-newline translation, then the CLI's own
    # encoding and 'replace' errors contract.
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data.decode(locale.getpreferredencoding(False), errors="replace")


def run(argv: list[str], cwd, timeout) -> subprocess.CompletedProcess[str]:
    """Launch ``argv`` on a private non-input desktop and reap its owned tree.

    Returns ``CompletedProcess[str]`` on exit (any code); raises
    ``subprocess.TimeoutExpired`` (partial output attached) on timeout; raises
    ``OSError`` if an isolation step fails (never falls back to the default
    desktop).
    """
    argv = [str(item) for item in argv]
    timeout_ms = _INFINITE if timeout is None else max(0, int(timeout * 1000))
    # uuid4 is per-call random and independent of the Windows clock, so two
    # threads in one process can never collide on a desktop name; pid kept for
    # readable provenance only (the contract never binds the name format).
    desktop_name = "pvcli_priv_%d_%s" % (os.getpid(), uuid.uuid4().hex)
    # ponytail: temp files (not pipes) so large stdout/stderr cannot deadlock;
    # read back after the process exits, then removed.
    tmpdir = tempfile.mkdtemp(prefix="pvcli_")
    out_path = os.path.join(tmpdir, "stdout")
    err_path = os.path.join(tmpdir, "stderr")
    hdesk = hjob = hproc = hthread = None
    hout = herr = hnin = None
    attr = None
    try:
        hdesk = _CreateDesktopW(desktop_name, None, None, 0, _DESKTOP_ACCESS, None)
        if not hdesk:
            raise _fail("CreateDesktopW")

        hjob = _CreateJobObjectW(None, None)
        if not hjob:
            raise _fail("CreateJobObjectW")
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _SetInformationJobObject(hjob, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                        ctypes.byref(limits), ctypes.sizeof(limits)):
            raise _fail("SetInformationJobObject")

        sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
        hout = _CreateFileW(out_path, _GENERIC_WRITE, _FILE_SHARE_RW, ctypes.byref(sa),
                            _CREATE_ALWAYS, _FILE_ATTRIBUTE_NORMAL, None)
        if not hout:
            raise _fail("CreateFileW(stdout)")
        herr = _CreateFileW(err_path, _GENERIC_WRITE, _FILE_SHARE_RW, ctypes.byref(sa),
                            _CREATE_ALWAYS, _FILE_ATTRIBUTE_NORMAL, None)
        if not herr:
            raise _fail("CreateFileW(stderr)")
        hnin = _CreateFileW("NUL", _GENERIC_READ, _FILE_SHARE_RW, ctypes.byref(sa),
                            _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
        if not hnin:
            raise _fail("CreateFileW(stdin)")

        size = ctypes.c_size_t(0)
        _InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise _fail("InitializeProcThreadAttributeList(size)")
        attr_buffer = ctypes.create_string_buffer(size.value)
        attr = ctypes.cast(attr_buffer, ctypes.c_void_p)
        if not _InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(size)):
            raise _fail("InitializeProcThreadAttributeList")
        handles = (wintypes.HANDLE * 3)(hnin, hout, herr)
        if not _UpdateProcThreadAttribute(attr, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                          ctypes.cast(handles, ctypes.c_void_p),
                                          ctypes.sizeof(handles), None, None):
            raise _fail("UpdateProcThreadAttribute")

        siex = _STARTUPINFOEXW()
        siex.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        siex.StartupInfo.lpDesktop = "WinSta0\\" + desktop_name
        siex.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        siex.StartupInfo.hStdInput = hnin
        siex.StartupInfo.hStdOutput = hout
        siex.StartupInfo.hStdError = herr
        siex.lpAttributeList = attr
        pi = _PROCESS_INFORMATION()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        flags = _EXTENDED_STARTUPINFO_PRESENT | _CREATE_SUSPENDED
        created = _CreateProcessW(
            argv[0], command_line, None, None, True, flags, None, str(cwd),
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
        stdout = _read_stream(out_path)
        stderr = _read_stream(err_path)
        if timed_out:
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(argv, int(code.value), stdout, stderr)
    finally:
        for handle in (hout, herr, hnin):
            if handle:
                _CloseHandle(handle)
        if hthread:
            _CloseHandle(hthread)
        if hproc:
            # No-op if it already exited; kills a child left suspended by a
            # failed assign/resume step.
            _TerminateProcess(hproc, 1)
            _WaitForSingleObject(hproc, 2000)
            _CloseHandle(hproc)
        if hjob:
            _TerminateJobObject(hjob, 0)
            _CloseHandle(hjob)  # last handle close -> KILL_ON_JOB_CLOSE reaps stragglers
        if attr:
            _DeleteProcThreadAttributeList(attr)
        if hdesk:
            _CloseDesktop(hdesk)
        shutil.rmtree(tmpdir, ignore_errors=True)

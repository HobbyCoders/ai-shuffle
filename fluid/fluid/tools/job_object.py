"""Windows Job Objects that end a whole process tree when they close.

``taskkill /T`` walks parent links, so it cannot reach a grandchild whose
parent already exited: a command the model ran under Git Bash's ``timeout``
outlived its stopped agent that way and went on to use 15 GB (#436). A job
object has no such gap. Every process the job's member starts joins the job
too, and with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` closing the last handle
terminates all of them, including after a crash of the process holding it.

Everything here is best-effort and never raises, except
:func:`resume_process`, whose failure the caller must act on; on other
platforms every job function is a no-op that returns None.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_SUSPEND_RESUME = 0x0800


def _kernel32():  # pragma: no cover - win32 only
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return ctypes, wintypes, kernel32


def assign_kill_on_close_job(pid: int | None) -> int | None:
    """Put ``pid`` in a new job that kills its whole tree when closed.

    Returns the job handle, which the caller must keep and later pass to
    :func:`close_job`, or None when not on Windows or when anything fails.
    Processes ``pid`` starts after this call join the job.
    """
    if sys.platform != "win32" or not isinstance(pid, int) or pid <= 0:
        return None
    try:  # pragma: no cover - win32 only
        _ctypes, _wintypes, kernel32 = _kernel32()
        process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            return None
        try:
            return assign_kill_on_close_job_handle(int(process))
        finally:
            kernel32.CloseHandle(process)
    except Exception:  # noqa: BLE001
        log.debug("could not put process %s in a kill-on-close job", pid, exc_info=True)
        return None


def assign_kill_on_close_job_handle(process: int | None) -> int | None:
    """Assign a caller-pinned handle; the caller retains and closes that handle.

    No PID is reopened. The returned non-inheritable Job handle has the same
    ownership/lifetime contract as :func:`assign_kill_on_close_job`.
    """
    if sys.platform != "win32" or not isinstance(process, int) or process <= 0:
        return None
    job = None
    try:  # pragma: no cover - win32 only
        _ctypes, _wintypes, kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        if not _set_limit_flags(job, _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, process):
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except Exception:  # noqa: BLE001
        close_job(job)
        log.debug("could not put pinned process in a kill-on-close job", exc_info=True)
        return None


def _set_limit_flags(job: int, flags: int) -> bool:  # pragma: no cover - win32 only
    """Set a job's basic limit flags; True when Windows accepted them."""
    ctypes, wintypes, kernel32 = _kernel32()

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = flags
    return bool(kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info),
    ))


def resume_process(pid: int) -> None:  # pragma: no cover - win32 only
    """Resume every thread of a process started with ``CREATE_SUSPENDED``.

    ``subprocess`` closes the primary thread's handle at spawn, so the
    process is resumed as a whole through ``NtResumeProcess`` (what psutil's
    ``Process.resume`` uses). Raises :class:`OSError` when it cannot be
    resumed; the caller must then kill the process rather than leave it
    suspended.
    """
    ctypes, wintypes, kernel32 = _kernel32()
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    process = kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        status = ntdll.NtResumeProcess(process)
    finally:
        kernel32.CloseHandle(process)
    if status < 0:
        raise OSError(f"NtResumeProcess failed for process {pid} (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")


def close_job(handle: int | None) -> None:
    """Close a job from :func:`assign_kill_on_close_job`, ending every member."""
    if handle is None or sys.platform != "win32":
        return
    try:  # pragma: no cover - win32 only
        _ctypes, _wintypes, kernel32 = _kernel32()
        kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        log.debug("closing job handle failed", exc_info=True)


def release_job(handle: int | None) -> None:
    """Close a job WITHOUT ending its members.

    For a command that finished normally: whatever it left running on
    purpose keeps running, as it does on POSIX. When the kill-on-close limit
    cannot be lifted the handle stays open (a small leak) rather than killing
    processes nobody asked to stop.
    """
    if handle is None or sys.platform != "win32":
        return
    try:  # pragma: no cover - win32 only
        _ctypes, _wintypes, kernel32 = _kernel32()
        if _set_limit_flags(handle, 0):
            kernel32.CloseHandle(handle)
        else:
            log.debug("could not lift kill-on-close from job %s; leaving it open", handle)
    except Exception:  # noqa: BLE001
        log.debug("releasing job handle failed", exc_info=True)

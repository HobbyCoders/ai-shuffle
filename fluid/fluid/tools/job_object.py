"""Windows Job Objects that end a whole process tree when they close.

``taskkill /T`` walks parent links, so it cannot reach a grandchild whose
parent already exited: a command the model ran under Git Bash's ``timeout``
outlived its stopped agent that way and went on to use 15 GB (#436). A job
object has no such gap. Every process the job's member starts joins the job
too, and with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` closing the last handle
terminates all of them, including after a crash of the process holding it.

Everything here is best-effort and never raises; on other platforms every
function is a no-op that returns None.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


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


def assign_kill_on_close_job(pid: int | None, *additional_pids: int) -> int | None:
    """Put the known initial PIDs in one job that kills their trees when closed.

    Returns the job handle, which the caller must keep and later pass to
    :func:`close_job`, or None when not on Windows or when anything fails.
    Future descendants join the job. Existing redirector/runtime pairs must
    both be supplied; assignment of a parent is not retroactive. If admitting
    any initial PID fails, close the job (ending already admitted members).
    """
    pids = (pid, *additional_pids)
    if sys.platform != "win32" or any(not isinstance(item, int) or item <= 0 for item in pids):
        return None
    job = None
    try:  # pragma: no cover - win32 only
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

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None
        for item in dict.fromkeys(pids):
            process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, item)
            if not process:
                kernel32.CloseHandle(job)
                return None
            try:
                assigned = kernel32.AssignProcessToJobObject(job, process)
            finally:
                kernel32.CloseHandle(process)
            if not assigned:
                kernel32.CloseHandle(job)
                return None
        return int(job)
    except Exception:  # noqa: BLE001
        close_job(job)
        log.debug("could not put processes %s in a kill-on-close job", pids, exc_info=True)
        return None


def close_job(handle: int | None) -> None:
    """Close a job from :func:`assign_kill_on_close_job`, ending every member."""
    if handle is None or sys.platform != "win32":
        return
    try:  # pragma: no cover - win32 only
        _ctypes, _wintypes, kernel32 = _kernel32()
        kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        log.debug("closing job handle failed", exc_info=True)

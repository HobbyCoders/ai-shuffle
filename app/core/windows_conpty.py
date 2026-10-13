"""Small stdlib-only ConPTY transport for Windows 10 1809 and later.

The shell is created suspended, assigned to a kill-on-close job, then resumed.
This contains descendants before any shell code runs. ConPTY emits UTF-8 VT
bytes, exactly like the Unix transport. Its output must keep being drained
while ClosePseudoConsole runs (that API can block waiting for the reader).
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import subprocess
import threading
import time

K = C.WinDLL("kernel32", use_last_error=True)
SIZE_T = C.c_size_t


class COORD(C.Structure):
    _fields_ = [("X", W.SHORT), ("Y", W.SHORT)]


class STARTUPINFO(C.Structure):
    _fields_ = [
        ("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR),
        ("lpTitle", W.LPWSTR), ("dwX", W.DWORD), ("dwY", W.DWORD),
        ("dwXSize", W.DWORD), ("dwYSize", W.DWORD),
        ("dwXCountChars", W.DWORD), ("dwYCountChars", W.DWORD),
        ("dwFillAttribute", W.DWORD), ("dwFlags", W.DWORD),
        ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
        ("lpReserved2", C.POINTER(W.BYTE)), ("hStdInput", W.HANDLE),
        ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE),
    ]


class STARTUPINFOEX(C.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", W.LPVOID)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE),
               ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


class BASIC_LIMIT(C.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", C.c_int64), ("PerJobUserTimeLimit", C.c_int64),
        ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T), ("ActiveProcessLimit", W.DWORD),
        ("Affinity", SIZE_T), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD),
    ]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class EXTENDED_LIMIT(C.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
               ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
               ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class ACCOUNTING(C.Structure):
    _fields_ = [("TotalUserTime", C.c_int64), ("TotalKernelTime", C.c_int64),
               ("ThisPeriodTotalUserTime", C.c_int64), ("ThisPeriodTotalKernelTime", C.c_int64),
               ("TotalPageFaultCount", W.DWORD), ("TotalProcesses", W.DWORD),
               ("ActiveProcesses", W.DWORD), ("TotalTerminatedProcesses", W.DWORD)]


def _api(name, restype, *args):
    fn = getattr(K, name)
    fn.restype, fn.argtypes = restype, args
    return fn


_close = _api("CloseHandle", W.BOOL, W.HANDLE)
_pipe = _api("CreatePipe", W.BOOL, C.POINTER(W.HANDLE), C.POINTER(W.HANDLE), W.LPVOID, W.DWORD)
_peek = _api("PeekNamedPipe", W.BOOL, W.HANDLE, W.LPVOID, W.DWORD,
             C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.POINTER(W.DWORD))
_read = _api("ReadFile", W.BOOL, W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID)
_write = _api("WriteFile", W.BOOL, W.HANDLE, W.LPCVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID)
_create_pty = getattr(K, "CreatePseudoConsole", None)
if _create_pty is not None:
    _create_pty = _api("CreatePseudoConsole", C.c_long, COORD, W.HANDLE, W.HANDLE,
                       W.DWORD, C.POINTER(W.HANDLE))
    _resize = _api("ResizePseudoConsole", C.c_long, W.HANDLE, COORD)
    _close_pty = _api("ClosePseudoConsole", None, W.HANDLE)
_init_attrs = _api("InitializeProcThreadAttributeList", W.BOOL, W.LPVOID, W.DWORD,
                   W.DWORD, C.POINTER(SIZE_T))
_update_attrs = _api("UpdateProcThreadAttribute", W.BOOL, W.LPVOID, W.DWORD,
                     SIZE_T, W.LPVOID, SIZE_T, W.LPVOID, W.LPVOID)
_delete_attrs = _api("DeleteProcThreadAttributeList", None, W.LPVOID)
_create_proc = _api("CreateProcessW", W.BOOL, W.LPCWSTR, W.LPWSTR, W.LPVOID,
                    W.LPVOID, W.BOOL, W.DWORD, W.LPVOID, W.LPCWSTR,
                    C.POINTER(STARTUPINFOEX), C.POINTER(PROCESS_INFORMATION))
_create_job = _api("CreateJobObjectW", W.HANDLE, W.LPVOID, W.LPCWSTR)
_set_job = _api("SetInformationJobObject", W.BOOL, W.HANDLE, C.c_int, W.LPVOID, W.DWORD)
_assign_job = _api("AssignProcessToJobObject", W.BOOL, W.HANDLE, W.HANDLE)
_query_job = _api("QueryInformationJobObject", W.BOOL, W.HANDLE, C.c_int,
                  W.LPVOID, W.DWORD, W.LPVOID)
_kill_job = _api("TerminateJobObject", W.BOOL, W.HANDLE, W.UINT)
_kill_proc = _api("TerminateProcess", W.BOOL, W.HANDLE, W.UINT)
_resume = _api("ResumeThread", W.DWORD, W.HANDLE)
_exit_code = _api("GetExitCodeProcess", W.BOOL, W.HANDLE, C.POINTER(W.DWORD))
_wait = _api("WaitForSingleObject", W.DWORD, W.HANDLE, W.DWORD)


def supported() -> bool:
    return _create_pty is not None


def _check(ok):
    if not ok:
        raise C.WinError(C.get_last_error())


def _hresult(code):
    if code < 0:
        raise OSError(f"ConPTY failed (HRESULT 0x{code & 0xffffffff:08x})")


class ConPTYProcess:
    def __init__(self, shell: str, cwd: str, env: dict, cols: int, rows: int):
        self.pid = None
        self.returncode = None
        self._process = self._job = self._pty = self._input = self._output = None
        self._lock = threading.RLock()
        self._closing = False
        self.closed = threading.Event()
        input_read, input_write, output_read, output_write = (W.HANDLE() for _ in range(4))
        hpc = W.HANDLE()
        attrs = None
        attrs_initialized = False
        pi = PROCESS_INFORMATION()
        try:
            _check(_pipe(C.byref(input_read), C.byref(input_write), None, 0))
            _check(_pipe(C.byref(output_read), C.byref(output_write), None, 0))
            _hresult(_create_pty(COORD(cols, rows), input_read, output_write, 0, C.byref(hpc)))
            self._pty = hpc.value
            self._input, self._output = input_write.value, output_read.value
            input_write, output_read = W.HANDLE(), W.HANDLE()
            # ConPTY owns its ends now. Keeping ours open prevents EOF.
            _close(input_read)
            input_read = W.HANDLE()
            _close(output_write)
            output_write = W.HANDLE()
            size = SIZE_T()
            _init_attrs(None, 1, 0, C.byref(size))
            attrs = C.create_string_buffer(size.value)
            _check(_init_attrs(attrs, 1, 0, C.byref(size)))
            attrs_initialized = True
            _check(_update_attrs(attrs, 0, 0x00020016, hpc, C.sizeof(W.HANDLE), None, None))
            si = STARTUPINFOEX()
            si.StartupInfo.cb = C.sizeof(si)
            si.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES; ConPTY supplies the console
            si.lpAttributeList = C.cast(attrs, W.LPVOID)
            self._job = _create_job(None, None)
            _check(self._job)
            limits = EXTENDED_LIMIT()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            _check(_set_job(self._job, 9, C.byref(limits), C.sizeof(limits)))
            command = C.create_unicode_buffer(subprocess.list2cmdline(
                [shell, "-NoLogo", "-NoProfile"]))
            # Explicit Unicode environment: never inherit the worker's secrets.
            environment = C.create_unicode_buffer("\0".join(
                f"{k}={v}" for k, v in sorted(env.items(), key=lambda item: item[0].upper())) + "\0\0")
            _check(_create_proc(shell, command, None, None, False,
                                0x00080000 | 0x400 | 0x4,  # EXTENDED_STARTUPINFO, UNICODE, SUSPENDED
                                environment, cwd, C.byref(si), C.byref(pi)))
            self._process, self.pid = pi.hProcess, pi.dwProcessId
            _check(_assign_job(self._job, self._process))
            if _resume(pi.hThread) == 0xffffffff:
                _check(False)
        except BaseException:
            if pi.hProcess:
                _kill_proc(pi.hProcess, 1)
                _wait(pi.hProcess, 3000)
            # No child can produce output before ResumeThread succeeds.
            if self._pty:
                _close_pty(self._pty)
                self._pty = None
            self.release()
            for handle in (input_write, output_read):
                if handle:
                    _close(handle)
            raise
        finally:
            if attrs_initialized:
                _delete_attrs(attrs)
            for handle in (input_read, output_write, pi.hThread):
                if handle:
                    _close(handle)

    def poll(self):
        with self._lock:
            if self.returncode is None and self._process:
                # Wait, rather than STILL_ACTIVE (259 can be a real exit code).
                if _wait(self._process, 0) == 0:
                    code = W.DWORD()
                    _check(_exit_code(self._process, C.byref(code)))
                    self.returncode = code.value
            return self.returncode

    def read(self, size: int) -> bytes | None:
        """Bytes, None if not ready, or b'' for EOF; never blocks."""
        available = W.DWORD()
        if not _peek(self._output, None, 0, None, C.byref(available), None):
            error = C.get_last_error()
            if error in (109, 232):  # BROKEN_PIPE, NO_DATA
                return b""
            raise C.WinError(error)
        if not available.value:
            return None
        buffer = C.create_string_buffer(min(size, available.value))
        count = W.DWORD()
        _check(_read(self._output, buffer, len(buffer), C.byref(count), None))
        return buffer.raw[:count.value]

    def write(self, data: bytes) -> None:
        # Only the terminal's serialized writer calls this (off the event loop).
        offset = 0
        while offset < len(data):
            count = W.DWORD()
            part = data[offset:offset + 16384]
            _check(_write(self._input, part, len(part), C.byref(count), None))
            if not count.value:
                raise OSError("ConPTY input pipe closed")
            offset += count.value

    def resize(self, cols: int, rows: int) -> None:
        with self._lock:
            if self._pty and not self._closing:
                _hresult(_resize(self._pty, COORD(cols, rows)))

    def busy(self) -> bool:
        # Windows has no foreground process group. Descendants are a useful
        # conservative indicator; in-process PowerShell cmdlets aren't visible.
        with self._lock:
            if not self._job or self.poll() is not None:
                return False
            info = ACCOUNTING()
            _check(_query_job(self._job, 1, C.byref(info), C.sizeof(info), None))
            return info.ActiveProcesses > 1

    def close(self) -> None:
        """Terminate the entire job, then close the console while the reader drains."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
            if self._job:
                _check(_kill_job(self._job, 1))
            hpc, self._pty = self._pty, None
        try:
            if hpc:
                _close_pty(hpc)
            # TerminateJobObject is asynchronous. Keep the handles until both
            # the shell and all descendants are gone, even if the pipe hit EOF.
            if self._process:
                _wait(self._process, 5000)
                self.poll()
            deadline = time.monotonic() + 5.0
            while self._job:
                info = ACCOUNTING()
                _check(_query_job(self._job, 1, C.byref(info), C.sizeof(info), None))
                if info.ActiveProcesses == 0:
                    break
                if time.monotonic() >= deadline:
                    raise OSError("ConPTY job did not terminate")
                time.sleep(0.01)
        finally:
            self.closed.set()

    def release(self) -> None:
        """Release handles after console close and reader completion."""
        with self._lock:
            for name in ("_input", "_output", "_process", "_job"):
                handle = getattr(self, name)
                if handle:
                    _close(handle)
                    setattr(self, name, None)

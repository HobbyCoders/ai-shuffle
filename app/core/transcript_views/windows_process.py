"""Pin and validate the finite Windows launcher/runtime identity before admission."""
from __future__ import annotations

from contextlib import contextmanager


class _WindowsAPI:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.nt = ctypes.WinDLL("ntdll", use_last_error=True)
        kernel = self.kernel
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessId.argtypes = [wintypes.HANDLE]
        kernel.GetProcessId.restype = wintypes.DWORD
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.nt.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        self.nt.NtQueryInformationProcess.restype = wintypes.LONG

        class BasicInformation(ctypes.Structure):
            # Native PROCESS_BASIC_INFORMATION; the last slot is the inherited
            # parent PID (Reserved3 in the public compatibility declaration).
            _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                        ("Reserved2", ctypes.c_void_p * 2), ("UniqueProcessId", ctypes.c_size_t),
                        ("InheritedFromUniqueProcessId", ctypes.c_size_t)]

        self.BasicInformation = BasicInformation

    def open(self, pid: int) -> int:
        # QUERY_INFORMATION + SYNCHRONIZE for validation, SET_QUOTA/TERMINATE
        # for Job assignment. The handle is explicitly non-inheritable.
        handle = self.kernel.OpenProcess(0x0400 | 0x100000 | 0x0100 | 0x0001, False, pid)
        if not handle:
            raise OSError("projection_runtime_identity_failed")
        return int(handle)

    def pid(self, handle: int) -> int:
        pid = self.kernel.GetProcessId(handle)
        if not pid:
            raise OSError("projection_runtime_identity_failed")
        return int(pid)

    def identity(self, handle: int) -> tuple[int, int, int]:
        info, returned = self.BasicInformation(), self.wintypes.ULONG()
        status = self.nt.NtQueryInformationProcess(
            handle, 0, self.ctypes.byref(info), self.ctypes.sizeof(info), self.ctypes.byref(returned))
        times = [self.wintypes.FILETIME() for _ in range(4)]
        if status < 0 or not self.kernel.GetProcessTimes(handle, *(self.ctypes.byref(item) for item in times)):
            raise OSError("projection_runtime_identity_failed")
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return int(info.UniqueProcessId), int(info.InheritedFromUniqueProcessId), created

    def running(self, handle: int) -> bool:
        return self.kernel.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT

    def close(self, handle: int) -> None:
        self.kernel.CloseHandle(handle)


@contextmanager
def pinned_processes(launcher_pid: int, runtime_pid: int):
    """Borrowed assignment handles stay open until every Job has been attached."""
    api, handles = _WindowsAPI(), []
    try:
        launcher = api.open(launcher_pid)
        handles.append(launcher)
        identity_pid, _, launcher_created = api.identity(launcher)
        if identity_pid != launcher_pid:
            raise OSError("projection_runtime_identity_failed")
        targets = [(launcher_pid, launcher)]
        runtime = launcher
        if runtime_pid != launcher_pid:
            runtime = api.open(runtime_pid)
            handles.append(runtime)
            identity_pid, parent_pid, created = api.identity(runtime)
            if identity_pid != runtime_pid or parent_pid != launcher_pid or created < launcher_created:
                raise OSError("projection_runtime_identity_failed")
            targets.append((runtime_pid, runtime))
        # Check AFTER opening the runtime handle: a live redirector still holds
        # its child's handle. Our retained handle then pins that identity even
        # if both exit before assignment. Assign these handles, never reopen PIDs.
        if not api.running(launcher) or not api.running(runtime):
            raise OSError("projection_runtime_identity_failed")
        yield targets
    finally:
        for handle in reversed(handles):
            api.close(handle)

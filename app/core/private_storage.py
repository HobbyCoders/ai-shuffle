"""Owner-private files, not isolation from processes running as that owner."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import stat
import secrets
import tempfile
import threading

# Set by the Docker supervisor when AGENT_ISOLATION=on (docker/agent_isolation.py).
AGENT_ISOLATION_FLAG = "SHUFFLE_AGENT_ISOLATION"
_service_state = threading.local()


def _service_ids() -> tuple[int, int] | None:
    """The service account kept as this process's saved ids, under Docker
    agent isolation. None otherwise, so every check below is unchanged."""
    environ = getattr(os, "environ", None) or {}
    if environ.get(AGENT_ISOLATION_FLAG) != "on" or not hasattr(os, "getresuid"):
        return None
    real, _effective, saved = os.getresuid()
    real_gid, _effective_gid, saved_gid = os.getresgid()
    if saved == real or saved_gid == real_gid:
        return None
    return saved, saved_gid


def owner_uid() -> int:
    """The uid private files must belong to here: the effective uid, or the
    service account inside :func:`service_files` under Docker agent
    isolation (the uid this thread's file access runs as)."""
    ids = _service_ids()
    return ids[0] if ids and getattr(_service_state, "depth", 0) else os.geteuid()


def _set_file_ids(uid: int, gid: int) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.setfsgid(gid)
    libc.setfsuid(uid)
    # Each call returns the previous value; an invalid id (-1) changes nothing,
    # so these report the ids now in force.
    if libc.setfsuid(-1) != uid or libc.setfsgid(-1) != gid:
        raise OSError("Could not switch file access identity")


@contextlib.contextmanager
def service_files():
    """File access as the service account for this thread only (Linux fsuid).

    A no-op unless Docker agent isolation is on. Reentrant. Keep the block
    synchronous: an ``await`` inside would let other coroutines on this
    thread run with the service account's file access.
    """
    ids = _service_ids()
    if ids is None:
        yield
        return
    depth = getattr(_service_state, "depth", 0)
    if depth == 0:
        _set_file_ids(*ids)
    _service_state.depth = depth + 1
    try:
        yield
    finally:
        _service_state.depth = depth
        if depth == 0:
            _set_file_ids(os.geteuid(), os.getegid())


def protect_private_path(path: Path, *, directory: bool = False) -> None:
    """Restrict an existing local object before writing secrets; fail closed.

    Do not recurse or change the data directory's ownership. POSIX uses 0700 /
    0600; Windows replaces the DACL with one protected current-user ACE. Windows
    directory children inherit that ACE. Neither mechanism excludes same-user
    shells, elevated administrators, or the OS.
    """
    path = Path(path)
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise OSError("Private storage must be a regular local file or directory")
    if os.name == "nt":
        _windows_private_acl(path, directory=directory)
    else:
        if info.st_uid != owner_uid():
            raise OSError("Private storage has another owner")
        mode = 0o700 if directory else 0o600
        # Already-private read-only mounts need no metadata write. Retain
        # stricter owner permissions (e.g. 0400); never ignore a required chmod.
        if stat.S_IMODE(info.st_mode) & ~mode:
            path.chmod(mode)
            after = path.lstat()
            if (not expected(after.st_mode) or after.st_uid != owner_uid()
                    or stat.S_IMODE(after.st_mode) & ~mode):
                raise OSError("Private storage permissions were not enforced")


def private_mkstemp(*, prefix: str, suffix: str, dir: Path) -> tuple[int, str]:
    """Create a private temporary file without a pre-protection handle window."""
    if os.name != "nt":
        fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=dir)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid()
                    or stat.S_IMODE(info.st_mode) & ~0o600):
                raise OSError("Private temporary file permissions were not enforced")
            return fd, name
        except BaseException:
            try:
                os.close(fd)
            finally:
                Path(name).unlink(missing_ok=True)
            raise
    for _ in range(100):
        path = Path(dir) / (prefix + secrets.token_hex(16) + suffix)
        try:
            fd = _windows_private_acl(path, directory=False, create=True)
        except FileExistsError:
            continue
        return fd, str(path)
    raise FileExistsError("Cannot allocate a private temporary file")


def _windows_private_acl(path: Path, *, directory: bool, create: bool = False) -> int | None:
    """Set a protected DACL, or create a file with that DACL atomically.

    chmod on Windows does not remove inherited ACLs; setting a DACL after
    creation also does not revoke any previously opened read handles.
    """
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(SecurityAttributes), wintypes.DWORD,
                                  wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    failure = "Private storage ACL protection is unavailable"
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise OSError(failure)
        size = wintypes.DWORD()
        # TokenUser (1) starts with SID_AND_ATTRIBUTES; its first field is PSID.
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise OSError(failure)
        user = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, user, size, ctypes.byref(size)):
            raise OSError(failure)
        sid = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise OSError(failure)
        inheritance = "OICI" if directory else ""
        sddl = f"D:P(A;{inheritance};FA;;;{sid_text.value})"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise OSError(failure)
        if create:
            import msvcrt

            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            # GENERIC_READ | GENERIC_WRITE; share read/write/delete; CREATE_NEW.
            # The protected descriptor is installed before any open can succeed.
            handle = kernel.CreateFileW(str(path.absolute()), 0xC0000000, 7,
                                        ctypes.byref(attributes), 1, 0x80, None)
            if handle == wintypes.HANDLE(-1).value:
                error = ctypes.get_last_error()
                if error in (80, 183):  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                    raise FileExistsError("Private temporary file already exists")
                raise OSError(failure)
            try:
                return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            except BaseException:
                kernel.CloseHandle(handle)
                path.unlink(missing_ok=True)
                raise
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        # No SACL access or SeSecurityPrivilege is needed.
        if not advapi.SetFileSecurityW(str(path.absolute()), 0x80000004, descriptor):
            raise OSError(failure)
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if token.value:
            kernel.CloseHandle(token)

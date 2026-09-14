"""Descriptor-relative folder operations. No shell, path resolution, or link following."""
from __future__ import annotations

import ctypes
import os
import platform
import stat
import uuid
from contextlib import contextmanager, ExitStack
from pathlib import Path
from functools import lru_cache

LIMIT = 64 * 1024


class AccessDenied(ValueError):
    pass


def confined_open(root: int, path: str, flags: int, mode: int = 0) -> int:
    # Linux openat2, on the architectures where its syscall number is 437.
    # Unlike a prefix check or O_NOFOLLOW alone, this also blocks bind mounts
    # and kernel magic links. There is deliberately no weaker fallback.
    class OpenHow(ctypes.Structure):
        _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64),
                    ("resolve", ctypes.c_uint64)]
    how = OpenHow(flags | os.O_CLOEXEC, mode, 0x01 | 0x02 | 0x04 | 0x08)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    fd = libc.syscall(ctypes.c_long(437), ctypes.c_int(root),
                      ctypes.c_char_p(os.fsencode(path)), ctypes.byref(how), ctypes.sizeof(how))
    if fd < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return fd


@lru_cache(maxsize=1)
def available() -> bool:
    if os.name == "nt":
        from app.core.confined_files import _windows_directory
        try:
            with _windows_directory(Path(os.path.abspath(os.sep)), []):
                return True
        except (OSError, AccessDenied):
            return False
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        return False
    root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        probe = confined_open(root, ".", os.O_RDONLY | os.O_DIRECTORY)
        os.close(probe)
        return True
    except OSError:
        return False
    finally:
        os.close(root)


def parts(path: str, *, empty: bool = False) -> list[str]:
    if not isinstance(path, str) or len(path) > 4096 or "\\" in path or "\x00" in path:
        raise AccessDenied("Use a relative path without links or traversal.")
    if empty and path in ("", "."):
        return []
    result = path.split("/")
    if any(p in ("", ".", "..") for p in result):
        raise AccessDenied("Use a relative path without links or traversal.")
    return result


class WindowsRoot:
    """Pin the approved root and every ancestor until the grant is revoked."""

    def __init__(self, path: str):
        from app.core.confined_files import _relative, _windows_directory
        self.path = Path(path)
        if (not self.path.is_absolute() or not self.path.drive
                or str(self.path).startswith("\\") or self.path == Path(self.path.anchor)):
            raise AccessDenied("Choose a local absolute folder other than the filesystem root.")
        # Validate the original spelling; Path would normalize dot components.
        _relative(path[len(self.path.anchor):].replace("\\", "/"))
        self.stack = ExitStack()
        self.closed = False
        try:
            self.stack.enter_context(_windows_directory(self.path, []))
        except BaseException:
            self.stack.close()
            raise

    def close(self):
        self.closed = True
        self.stack.close()


def close_root(root: int | WindowsRoot) -> None:
    if isinstance(root, WindowsRoot):
        root.close()
    else:
        os.close(root)


def open_root(path: str) -> int | WindowsRoot:
    if not available():
        raise AccessDenied("Folder access is unavailable on this worker; no unsafe fallback is used.")
    if os.name == "nt":
        return WindowsRoot(path)
    if not path.startswith("/") or path == "/":
        raise AccessDenied("Choose an absolute folder other than the filesystem root.")
    names = parts(path[1:])
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in names:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def directory(root: int | WindowsRoot, names: list[str]):
    if isinstance(root, WindowsRoot):
        from app.core.confined_files import _relative, _windows_directory
        if root.closed:
            raise AccessDenied("Folder access was revoked.")
        _relative("/".join(names), empty=True)
        with _windows_directory(root.path, names) as parent:
            yield parent
        return
    fd = confined_open(root, "/".join(names) or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise AccessDenied("Only regular, singly linked files are accessible.")


def read(root: int | WindowsRoot, path: str) -> str:
    names = parts(path)
    with directory(root, names[:-1]) as parent:
        with ExitStack() as stack:
            if isinstance(root, WindowsRoot):
                from app.core.confined_files import _relative, _windows_handle
                _relative(path)
                fd = stack.enter_context(_windows_handle(parent / names[-1], directory=False))
            else:
                fd = confined_open(parent, names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                stack.callback(os.close, fd)
            regular(os.fstat(fd))
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise AccessDenied("File exceeds the 64 KiB text limit.")
    return data.decode("utf-8")


def listing(root: int | WindowsRoot, path: str) -> list[str]:
    with directory(root, parts(path, empty=True)) as parent:
        with os.scandir(parent) as entries:
            result = []
            for entry in entries:
                if len(result) == 500:
                    raise AccessDenied("Directory exceeds the 500-entry limit; choose a subfolder.")
                result.append(entry.name)
            return sorted(result)


def write(root: int | WindowsRoot, path: str, content: str) -> None:
    data = content.encode("utf-8")
    if len(data) > LIMIT:
        raise AccessDenied("File exceeds the 64 KiB text limit.")
    names = parts(path)
    if isinstance(root, WindowsRoot):
        from app.core.confined_files import _relative, _windows_handle
        _relative(path)
        with directory(root, names[:-1]) as parent:
            destination = parent / names[-1]
            try:
                with _windows_handle(destination, directory=False):
                    pass
            except FileNotFoundError:
                pass
            temp = parent / (".chat-write-" + uuid.uuid4().hex)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    regular(os.fstat(stream.fileno()))
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Pinned parents cannot be replaced. Replace the entry, never
                # follow a destination swapped after validation.
                os.replace(temp, destination)
            finally:
                temp.unlink(missing_ok=True)
        return
    with directory(root, names[:-1]) as parent:
        mode = 0o600
        try:
            info = os.stat(names[-1], dir_fd=parent, follow_symlinks=False)
            regular(info)
            mode = stat.S_IMODE(info.st_mode) & 0o777
        except FileNotFoundError:
            pass
        temp = ".chat-write-" + uuid.uuid4().hex
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Replace the directory entry, never write through an existing inode.
            # A concurrently substituted symlink/hard link is replaced, not followed.
            os.rename(temp, names[-1], src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temp, dir_fd=parent)
            except FileNotFoundError:
                pass

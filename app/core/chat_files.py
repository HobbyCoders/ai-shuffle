"""Descriptor-relative folder operations. No shell, path resolution, or link following."""
from __future__ import annotations

import ctypes
import os
import platform
import stat
import uuid
from contextlib import contextmanager
from functools import lru_cache

LIMIT = 64 * 1024


class AccessDenied(ValueError):
    pass


def confined_open(root: int, path: str, flags: int) -> int:
    # Linux openat2, on the architectures where its syscall number is 437.
    # Unlike a prefix check or O_NOFOLLOW alone, this also blocks bind mounts
    # and kernel magic links. There is deliberately no weaker fallback.
    class OpenHow(ctypes.Structure):
        _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64),
                    ("resolve", ctypes.c_uint64)]
    how = OpenHow(flags | os.O_CLOEXEC, 0, 0x01 | 0x02 | 0x04 | 0x08)
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


def open_root(path: str) -> int:
    if not available():
        raise AccessDenied("Folder access is unavailable on this worker; no unsafe fallback is used.")
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
def directory(root: int, names: list[str]):
    fd = confined_open(root, "/".join(names) or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise AccessDenied("Only regular, singly linked files are accessible.")


def read(root: int, path: str) -> str:
    names = parts(path)
    with directory(root, names[:-1]) as parent:
        fd = confined_open(parent, names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            regular(os.fstat(stream.fileno()))
            data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise AccessDenied("File exceeds the 64 KiB text limit.")
    return data.decode("utf-8")


def listing(root: int, path: str) -> list[str]:
    with directory(root, parts(path, empty=True)) as parent:
        with os.scandir(parent) as entries:
            result = []
            for entry in entries:
                if len(result) == 500:
                    raise AccessDenied("Directory exceeds the 500-entry limit; choose a subfolder.")
                result.append(entry.name)
            return sorted(result)


def write(root: int, path: str, content: str) -> None:
    data = content.encode("utf-8")
    if len(data) > LIMIT:
        raise AccessDenied("File exceeds the 64 KiB text limit.")
    names = parts(path)
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

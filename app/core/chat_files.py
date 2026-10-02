"""Descriptor-relative folder operations. No shell, path resolution, or link following.

Linux uses openat2. macOS walks each component with openat and kernel
no-symlink flags, refusing mount and firmlink crossings. Other hosts fail closed.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import stat
import uuid
from contextlib import contextmanager
from functools import lru_cache

LIMIT = 64 * 1024


class AccessDenied(ValueError):
    pass


def confined_open(root: int, path: str, flags: int, mode: int = 0) -> int:
    if platform.system() == "Darwin":
        return _darwin_open(root, path, flags, mode)
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


# XNU <sys/fcntl.h>; Python does not export these on every version.
_NOFOLLOW_ANY = 0x20000000  # macOS 11: refuse a symlink in any path component
_RESOLVE_BENEATH = 0x00001000  # macOS 15.4: refuse resolving above the dirfd
_UNIQUE = 0x00002000  # macOS 26: refuse a file with more than one hard link
_F_GETPATH = 50


class _StatFS(ctypes.Structure):
    # struct statfs with 64-bit inodes: the only layout on arm64 and the
    # fstatfs$INODE64 layout on x86_64. The probe checks it against "/".
    _fields_ = [("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32),
                ("blocks", ctypes.c_uint64), ("bfree", ctypes.c_uint64),
                ("bavail", ctypes.c_uint64), ("files", ctypes.c_uint64),
                ("ffree", ctypes.c_uint64), ("fsid", ctypes.c_int32 * 2),
                ("owner", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("fssubtype", ctypes.c_uint32),
                ("fstypename", ctypes.c_char * 16), ("mntonname", ctypes.c_char * 1024),
                ("mntfromname", ctypes.c_char * 1024), ("flags_ext", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 7)]


def _mount(fd: int) -> tuple[int, bytes]:
    libc = ctypes.CDLL(None, use_errno=True)
    fstatfs = getattr(libc, "fstatfs" if platform.machine() == "arm64" else "fstatfs$INODE64")
    fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_StatFS)]
    info = _StatFS()
    if fstatfs(fd, ctypes.byref(info)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return os.fstat(fd).st_dev, info.mntonname


def _fd_path(fd: int) -> bytes:
    import fcntl
    return fcntl.fcntl(fd, _F_GETPATH, bytes(1024)).split(b"\0", 1)[0]


@lru_cache(maxsize=1)
def _darwin_flags() -> int | None:
    """Open flags the running kernel enforces, or None to refuse all folder IO.

    An older kernel may ignore an unknown flag bit, so each one is proven on
    a scratch tree rather than trusted from its value.
    """
    import tempfile
    if platform.machine() not in ("arm64", "x86_64"):
        return None
    try:
        with tempfile.TemporaryDirectory() as temp:
            base = os.path.realpath(temp)
            os.mkdir(os.path.join(base, "d"))
            open(os.path.join(base, "d", "f"), "xb").close()
            os.link(os.path.join(base, "d", "f"), os.path.join(base, "d", "h"))
            open(os.path.join(base, "d", "g"), "xb").close()
            os.symlink("d", os.path.join(base, "l"))
            fds = []
            def opened(*args, **kwargs):
                fds.append(os.open(*args, **kwargs))
                return fds[-1]
            try:
                top = opened(base, os.O_RDONLY | os.O_DIRECTORY)
                slash = opened("/", os.O_RDONLY | os.O_DIRECTORY)
                if (_fd_path(top) != os.fsencode(base) or _mount(slash)[1] != b"/"
                        or _mount(top)[0] != os.stat(base).st_dev):
                    return None
                opened("d/f", os.O_RDONLY | _NOFOLLOW_ANY, dir_fd=top)
                try:
                    opened("l/f", os.O_RDONLY | _NOFOLLOW_ANY, dir_fd=top)
                    return None
                except OSError as error:
                    if error.errno != errno.ELOOP:
                        return None
                flags = _NOFOLLOW_ANY
                try:
                    inner = opened("d", os.O_RDONLY | os.O_DIRECTORY | _RESOLVE_BENEATH, dir_fd=top)
                    try:
                        opened("..", os.O_RDONLY | os.O_DIRECTORY | _RESOLVE_BENEATH, dir_fd=inner)
                    except OSError:
                        flags |= _RESOLVE_BENEATH
                except OSError:
                    pass
                try:
                    opened("d/g", os.O_RDONLY | _UNIQUE, dir_fd=top)
                    try:
                        opened("d/h", os.O_RDONLY | _UNIQUE, dir_fd=top)
                    except OSError:
                        flags |= _UNIQUE
                except OSError:
                    pass
                return flags
            finally:
                for fd in fds:
                    os.close(fd)
    except (OSError, AttributeError):
        return None


def _darwin_open(root: int, path: str, flags: int, mode: int = 0) -> int:
    """Walk one name at a time from a held directory descriptor.

    Each openat resolves a single name with O_NOFOLLOW_ANY (and
    O_RESOLVE_BENEATH where the kernel enforces it), so no symlink or ".." is
    followed. XNU rejects O_NOFOLLOW alongside O_NOFOLLOW_ANY, which already
    covers the last component. Existing files are opened with O_UNIQUE where
    enforced, so the kernel refuses hard links. macOS has no RESOLVE_NO_XDEV,
    so every opened component must report the root's device and mount point.
    The mount point is what refuses APFS firmlinks: the system and Data
    volumes can report the same st_dev. Last, the kernel's path for the result must
    lie under the root's current path, which catches a directory moved out of
    the root during the walk.
    """
    supported = _darwin_flags()
    if supported is None:
        raise AccessDenied("Folder access is unavailable on this worker; no unsafe fallback is used.")
    extra = supported & (_NOFOLLOW_ANY | _RESOLVE_BENEATH)
    flags &= ~os.O_NOFOLLOW
    if supported & _UNIQUE and not flags & (os.O_CREAT | os.O_DIRECTORY):
        # O_CREAT would report a hard-linked name as ENOTCAPABLE, not EEXIST.
        flags |= _UNIQUE
    names = parts(path) if path != "." else ["."]
    expected = _mount(root)
    base = _fd_path(root)
    fd = root
    try:
        for index, name in enumerate(names):
            step = flags if index == len(names) - 1 else os.O_RDONLY | os.O_DIRECTORY
            child = os.open(name, step | extra | os.O_CLOEXEC, mode, dir_fd=fd)
            if fd != root:
                os.close(fd)
            fd = child
            if _mount(fd) != expected:
                raise OSError(errno.EXDEV, "Folder access cannot cross a mount or firmlink.")
        current = _fd_path(fd)
        if current != base and not current.startswith(base.rstrip(b"/") + b"/"):
            raise OSError(errno.EXDEV, "The path resolved outside the folder.")
        return fd
    except BaseException:
        if fd != root:
            os.close(fd)
        raise


@lru_cache(maxsize=1)
def available() -> bool:
    if platform.system() == "Darwin":
        return _darwin_flags() is not None
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

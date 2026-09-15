"""Upload/attachment IO through pinned directories, never a checked-then-open path.

Linux uses openat2; Windows pins each directory without FILE_SHARE_DELETE and
opens entries with OPEN_REPARSE_POINT. Other platforms fail closed.
"""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import stat

from app.core import chat_files
from app.core.chat_files import AccessDenied


def _relative(path: str, *, empty: bool = False) -> list[str]:
    names = chat_files.parts(path, empty=empty)
    # Also reject NT device names, streams, and Win32 normalization aliases.
    for name in names:
        if ':' in name or name.rstrip(' .') != name:
            raise AccessDenied("Invalid file path.")
        stem = name.split('.')[0].upper()
        if stem in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)),
                    *(f'LPT{i}' for i in range(10))}:
            raise AccessDenied("Invalid file path.")
    return names


@contextmanager
def _windows_handle(path: Path, *, directory: bool, create_new: bool = False):
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    # No delete sharing: no ancestor can be renamed/replaced while it is pinned.
    handle = create(str(path), 0x40000000 if create_new else 0x80000000,
                    0x1 | 0x2, None, 1 if create_new else 3,
                    0x00200000 | (0x02000000 if directory else 0), None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    class Info(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                    ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                    ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                    ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                    ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]
    info = Info()
    query = kernel.GetFileInformationByHandle
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
    fd = None
    try:
        if not query(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
            raise AccessDenied("Links and reparse points are not accessible.")
        if not directory and info.links != 1:
            raise AccessDenied("Only singly linked files are accessible.")
        if directory:
            yield handle
        else:
            fd = msvcrt.open_osfhandle(handle, (os.O_WRONLY if create_new else os.O_RDONLY) | os.O_BINARY)
            handle = None  # the CRT descriptor owns it now
            yield fd
    finally:
        if fd is not None:
            os.close(fd)
        if handle is not None:
            close(handle)


@contextmanager
def _windows_directory(root: Path, names: list[str], *, create: bool = False):
    if not root.is_absolute() or not root.drive or str(root).startswith('\\'):
        raise AccessDenied("Choose a local absolute folder.")
    with ExitStack() as stack:
        current = Path(root.anchor)
        stack.enter_context(_windows_handle(current, directory=True))
        for name in [*root.parts[1:], *names]:
            current /= name
            if create:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
            stack.enter_context(_windows_handle(current, directory=True))
        yield current


@contextmanager
def _linux_root(root: Path):
    fd = chat_files.open_root(str(root))
    try:
        yield fd
    finally:
        os.close(fd)


def upload(root: Path, directory: str, filename: str, content: bytes) -> Path:
    """Create a unique new file. Collisions (including dangling links) retry exclusively."""
    names = _relative(directory, empty=True)
    _relative(filename)
    if '/' in filename or filename.startswith('.'):
        raise AccessDenied("Invalid filename.")
    root = Path(os.path.abspath(root))
    with ExitStack() as stack:
        if os.name == 'nt':
            parent = stack.enter_context(_windows_directory(root, names, create=True))
            def create(name):
                # CRT O_EXCL may follow a dangling symlink on Windows. CREATE_NEW
                # opens the entry itself and refuses every existing name.
                fd = stack.enter_context(_windows_handle(parent / name, directory=False, create_new=True))
                return os.dup(fd)
        else:
            fd = stack.enter_context(_linux_root(root))
            for i, name in enumerate(names):
                with chat_files.directory(fd, names[:i]) as parent_fd:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                with chat_files.directory(fd, names[:i + 1]):
                    pass
            def create(name):
                return chat_files.confined_open(fd, '/'.join([*names, name]),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        original = Path(filename)
        for counter in range(10000):
            candidate = filename if not counter else f'{original.stem}_{counter}{original.suffix}'
            try:
                output = create(candidate)
            except FileExistsError:
                continue
            with os.fdopen(output, 'wb') as stream:
                chat_files.regular(os.fstat(stream.fileno()))
                stream.write(content)
            return root.joinpath(*names, candidate)
    raise AccessDenied("Too many duplicate filenames.")


@contextmanager
def open_attachment(root: Path, path: Path):
    """Resolve in-root aliases, then open the resolved entry without following links.

    Resolution is only a name lookup: all actual IO is through a confined handle.
    A substitution after resolution is either another in-root file or refused.
    """
    root = Path(os.path.abspath(root))
    with ExitStack() as stack:
        if os.name == 'nt':
            # Reject reparse ancestors before normalizing legitimate DOS 8.3
            # aliases. Keep the original spelling pinned during all later IO.
            stack.enter_context(_windows_directory(root, []))
            root = root.resolve(strict=True)
        target = path.resolve(strict=True)
        try:
            relative = target.relative_to(root)
        except ValueError:
            raise AccessDenied("Attachment is outside the uploads directory.") from None
        names = _relative(relative.as_posix())
        if os.name == 'nt':
            parent = stack.enter_context(_windows_directory(root, names[:-1]))
            fd = stack.enter_context(_windows_handle(parent / names[-1], directory=False))
        else:
            root_fd = stack.enter_context(_linux_root(root))
            fd = chat_files.confined_open(root_fd, '/'.join(names), os.O_RDONLY | os.O_NONBLOCK)
            stack.callback(os.close, fd)
        chat_files.regular(os.fstat(fd))
        # Duplicate so the stream and the directory/handle owners close independently.
        with os.fdopen(os.dup(fd), 'rb') as stream:
            yield stream

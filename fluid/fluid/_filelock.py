"""Advisory cross-process file lock.

The lock lives on its own file rather than on the document it guards: a
document that is replaced by rename during a rewrite would leave a lock on
the old inode guarding nothing once the new file is in place.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def file_lock(lock_path: Path, *, wait: float = 0.0) -> Iterator[bool]:
    """Hold the advisory lock at ``lock_path`` inside the block.

    Yields True when the lock was taken, False when another holder kept it
    for the whole ``wait`` window (polled at 10ms) or the lock file could not
    be opened at all. The caller decides what False means.
    """
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        yield False
        return
    held = False
    try:
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            held = _try_lock(fd)
            if held or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        yield held
    finally:
        if held:
            _unlock(fd)
        os.close(fd)

"""Cross-thread and cross-process locks for small file-backed transactions."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
import weakref

_guard = threading.Lock()
_locks = weakref.WeakValueDictionary()
_depth = threading.local()


@contextmanager
def file_transaction(path: Path):
    key = str(path.resolve())
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
    with lock:
        held = getattr(_depth, "held", set())
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, 2) == 0:
                    stream.write(b"0")
                    stream.flush()
                deadline = time.monotonic() + 10
                while True:
                    stream.seek(0)
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Memory store is busy; no changes saved")
                        time.sleep(0.025)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            _depth.held = held | {key}
            try:
                yield
            finally:
                _depth.held = held
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

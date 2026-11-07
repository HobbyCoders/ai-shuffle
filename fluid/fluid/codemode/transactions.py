"""Cross-thread and cross-process locks for small file-backed transactions."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import os
from pathlib import Path
import threading
import time
import weakref

_guard = threading.Lock()
_locks = weakref.WeakValueDictionary()
_depth = threading.local()
_change_callback = None
_changes_suppressed = ContextVar("file_changes_suppressed", default=False)
_log = logging.getLogger(__name__)


def _emit_changes(paths):
    if paths and _change_callback is not None:
        try:
            _change_callback(paths)
        except Exception:  # A sync hint must never turn a durable save into a failure.
            _log.warning("File change notification failed; reconciliation will retry", exc_info=True)


def committed_change(path: Path) -> None:
    """Notify the host after a durable write and after all enclosing file locks.

    A transaction can contain nested saves (including a replacement). Coalesce
    their paths until its outer lock is released; never publish inside a writer.
    Standalone Fluid has no callback and needs no application dependency.
    """
    if _changes_suppressed.get():
        return
    path = Path(path)
    if getattr(_depth, "held", set()):
        if not hasattr(_depth, "changes"):
            _depth.changes = set()
        _depth.changes.add(path)
    else:
        _emit_changes({path})


@contextmanager
def suppress_changes():
    """Peer materialization is not a local mutation, even through normal stores."""
    token = _changes_suppressed.set(True)
    try:
        yield
    finally:
        _changes_suppressed.reset(token)


@contextmanager
def file_transaction(path: Path):
    try:
        with _file_transaction(path):
            yield
    finally:
        if not getattr(_depth, "held", set()):
            changes = getattr(_depth, "changes", set())
            _depth.changes = set()
            # Atomic file replacements cannot be rolled back. If a later step
            # raised, already committed changes still need reconciliation.
            _emit_changes(changes)


@contextmanager
def _file_transaction(path: Path):
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

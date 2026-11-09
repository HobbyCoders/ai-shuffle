"""The shared plugin registry/settings lock, also used by settings import.

Lock hierarchy: source, plugin(s sorted), state. State-only writers must never
call an operation that acquires a source or plugin lock. Sidecars are permanent.
"""
import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path

from fluid._filelock import file_lock

_guard = threading.Lock()
_locks = {}
_held = threading.local()


@contextmanager
def plugin_state_lock(plugins_dir):
    directory = Path(plugins_dir) / ".locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(b"registry-and-settings").hexdigest() + ".lock")
    identity = str(path.resolve())
    with _guard:
        lock = _locks.setdefault(identity, threading.RLock())
    with lock:
        held = getattr(_held, "paths", None)
        if held is None:
            held = _held.paths = set()
        if identity in held:
            yield
            return
        with file_lock(path, wait=30) as acquired:
            if not acquired:
                raise OSError("Could not acquire the shared plugin state lock")
            held.add(identity)
            try:
                yield
            finally:
                held.remove(identity)

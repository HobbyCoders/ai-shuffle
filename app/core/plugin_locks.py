"""Serialize plugin operations across threads and worker processes.

Never unlink sidecars: waiters must continue locking the same inode. Lock order
is plugin, then shared registry/settings; recursive installer calls reuse both.
"""
import hashlib
import threading
from contextlib import contextmanager
from functools import wraps

from app.core.config import _lock_instance_id_file

_guard = threading.Lock()
_locks = {}
_held = threading.local()


@contextmanager
def _lock(svc, key):
    directory = svc.PLUGINS_DIR / ".locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    identity = str(path.resolve())
    with _guard:
        lock = _locks.setdefault(identity, threading.RLock())
    with lock:
        held = getattr(_held, "paths", None)
        if held is None:
            held = _held.paths = set()
        if identity in held:
            yield
        else:
            with _lock_instance_id_file(path):
                held.add(identity)
                try:
                    yield
                finally:
                    held.remove(identity)


def plugin_lock(svc, plugin_id):
    return _lock(svc, "plugin:" + plugin_id)


def state_lock(svc):
    return _lock(svc, "registry-and-settings")


def serialized_plugin(operation):
    @wraps(operation)
    def wrapped(svc, plugin_id, *args, **kwargs):
        # Refuse unreadable state before even creating a lock sidecar. The
        # operation rechecks under the lock before staging or publishing.
        svc._check_files_replaceable(svc.INSTALLED_FILE, svc.SETTINGS_FILE)
        with plugin_lock(svc, plugin_id):
            return operation(svc, plugin_id, *args, **kwargs)
    return wrapped


def serialized_state(operation):
    @wraps(operation)
    def wrapped(svc, *args, **kwargs):
        with state_lock(svc):
            return operation(svc, *args, **kwargs)
    return wrapped

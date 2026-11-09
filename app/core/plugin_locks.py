"""Serialize plugin operations across threads and worker processes.

Never unlink sidecars: waiters must continue locking the same inode. Lock order
is marketplace, then plugin(s in sorted order), then shared state. Never acquire
marketplace/plugin locks while holding only shared state. Recursive calls reuse locks.
"""
import hashlib
import threading
from contextlib import contextmanager
from functools import wraps

from app.core.config import _lock_instance_id_file
from fluid.context.plugin_state import plugin_state_lock

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


def marketplace_lock(svc, marketplace_id):
    return _lock(svc, "marketplace:" + marketplace_id)


def plugin_lock(svc, plugin_id):
    return _lock(svc, "plugin:" + plugin_id)


def serialized_marketplace(operation):
    @wraps(operation)
    def wrapped(svc, marketplace_id, *args, **kwargs):
        svc._check_files_replaceable(svc.MARKETPLACES_FILE)
        with marketplace_lock(svc, marketplace_id):
            return operation(svc, marketplace_id, *args, **kwargs)
    return wrapped


def state_lock(svc):
    return plugin_state_lock(svc.PLUGINS_DIR)


def serialized_plugin(operation):
    @wraps(operation)
    def wrapped(svc, plugin_id, *args, **kwargs):
        # Refuse unreadable state before even creating a lock sidecar. The
        # operation rechecks under the lock before staging or publishing.
        svc._check_files_replaceable(svc.INSTALLED_FILE, svc.SETTINGS_FILE, svc.MARKETPLACES_FILE)
        marketplace_id = plugin_id.rsplit("@", 1)[-1]
        with marketplace_lock(svc, marketplace_id), plugin_lock(svc, plugin_id):
            with state_lock(svc):
                entry = svc._read_json_file(svc.MARKETPLACES_FILE, {}).get(marketplace_id, {})
                if entry.get("removing") and operation.__name__ not in ("uninstall_plugin", "disable_plugin"):
                    raise ValueError("Plugin source removal is incomplete; retry removing it first")
            return operation(svc, plugin_id, *args, **kwargs)
    return wrapped


def serialized_state(operation):
    @wraps(operation)
    def wrapped(svc, *args, **kwargs):
        with state_lock(svc):
            return operation(svc, *args, **kwargs)
    return wrapped

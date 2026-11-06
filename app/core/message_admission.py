"""Bounded pre-admission cancellations, shared by local and forwarded intake.

The guard orders an unknown-id withdrawal against the actual durable enqueue,
including synchronous handoff release. Tombstones live on the executing worker.
"""
from collections import OrderedDict
from contextlib import contextmanager
from threading import Lock, RLock
from time import monotonic
from weakref import WeakValueDictionary

TTL_SECONDS = 300
MAX_TOMBSTONES = 4096
_tombstones = OrderedDict()
_locks = WeakValueDictionary()
_mutex = Lock()


class CancelledSend(ValueError):
    pass


@contextmanager
def guard(session_id):
    with _mutex:
        lock = _locks.setdefault(session_id, RLock())
    with lock:
        yield


def _expire():
    now = monotonic()
    while _tombstones and next(iter(_tombstones.values())) <= now:
        _tombstones.popitem(last=False)


def cancel_unknown(session_id, command_id):
    """Resolve and fence absence atomically with submit's durable writes."""
    from app.db import session_moves, turns
    with guard(session_id):
        row = turns.get_turn_by_command(session_id, command_id)
        if row is None:
            # Read the durable held list directly: taking handoff's state lock
            # here would invert the release -> admission lock order.
            held = (session_moves.get(session_id) or {}).get("held") or []
            row = next((h for h in held if h.get("command_id") == command_id), None)
        if row is not None:
            return row
        # A held withdrawal is also terminal; retaining the fence is harmless.
        with _mutex:
            _expire()
            _tombstones.pop((session_id, command_id), None)
            _tombstones[(session_id, command_id)] = monotonic() + TTL_SECONDS
            while len(_tombstones) > MAX_TOMBSTONES:
                _tombstones.popitem(last=False)
        return None


def check_cancelled(session_id, command_id):
    """Cheap preflight; the guarded durable write checks again after awaits."""
    with _mutex:
        _expire()
        cancelled = (session_id, command_id) in _tombstones
    if cancelled:
        raise CancelledSend("You cancelled it before it was queued.")


@contextmanager
def submission(session_id, source, command_id):
    """Fence only the durable user write, never handoff locks or system intake."""
    if not command_id or source not in {"user", "queue"}:
        yield
        return
    with guard(session_id):
        check_cancelled(session_id, command_id)
        yield

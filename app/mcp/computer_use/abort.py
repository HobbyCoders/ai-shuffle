"""Per-session halt switch for computer-use actions.

Each session's input actions check their own flag *before* every operation. A
session is halted when its Stop button is pressed (the chat Stop kills the
agent and its computer-use control together) and automatically re-armed when
that session's next turn starts. ``request_stop``/``resume`` may also be called
without a session id, which acts on the process-global ``"_global"`` key.

Flags and generations are synchronized across worker threads, the HTTP loop,
and the keyboard hook. Each short input operation checks its generation under
the same lock as Stop; resumed turns cannot revive an old cancelled worker.
"""

from __future__ import annotations

import logging
import threading
import weakref
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# session_id (or "_global") => halted flag.
_HALTED: dict = {}
_LOCK = threading.RLock()
_GENERATIONS: dict[str, int] = {}
_controllers: weakref.WeakSet = weakref.WeakSet()


class Aborted(Exception):
    """The action was stopped, including a worker from an earlier turn."""


def register_controller(controller) -> None:
    with _LOCK:
        _controllers.add(controller)


def generation(session_id: str | None) -> int:
    with _LOCK:
        return _GENERATIONS.get(_key(session_id), 0)


@contextmanager
def input_guard(session_id: str | None, token: int):
    """Check + issue one short input atomically with the stop switch."""
    with _LOCK:
        if is_stopped(session_id) or generation(session_id) != token:
            raise Aborted()
        yield


def _key(session_id: str | None) -> str:
    return session_id or "_global"


def request_stop(session_id: str | None = None, *, reason: str = "stopped") -> None:
    """Halt computer-use for a session. Its actions refuse until resumed."""
    key = _key(session_id)
    with _LOCK:
        already = _HALTED.get(key, False)
        _HALTED[key] = True
        _GENERATIONS[key] = _GENERATIONS.get(key, 0) + 1
        for controller in list(_controllers):
            if _key(controller.session_id) == key:
                controller.release_all()
        from app.mcp.computer_use import activity
        activity.finish(session_id, reason=reason)
    if not already and reason != "completed":
        logger.warning("computer-use halted for session %s — refusing its actions", key)


def resume(session_id: str | None = None) -> None:
    """Re-arm computer-use for a session, re-enabling its actions."""
    key = _key(session_id)
    with _LOCK:
        was = _HALTED.pop(key, False)
        # A worker queued during Stop must not become live when the next turn
        # clears the flag, even if it captured the post-stop generation.
        _GENERATIONS[key] = _GENERATIONS.get(key, 0) + 1
    if was:
        logger.info("computer-use resumed for session %s — actions re-enabled", key)


def is_stopped(session_id: str | None = None) -> bool:
    """Return True if computer-use is currently halted for this session."""
    with _LOCK:
        return bool(_HALTED.get(_key(session_id), False))


def any_stopped() -> bool:
    """Return True if ANY session (or the global switch) is currently halted.

    Diagnostic only: one stopped session must not hide another's overlay.
    """
    with _LOCK:
        return any(_HALTED.values())


def stop_active(reason: str = "escape") -> int:
    """Physical Escape and the UI stop sessions currently using this host."""
    from app.mcp.computer_use import activity, access
    with _LOCK:
        sessions = activity.active_sessions()
        for sid in sessions:
            request_stop(sid, reason=reason)
            access.revoke(sid)
        return len(sessions)

"""Per-session halt switch for computer-use actions.

Each session's input actions check their own flag *before* every operation. A
session is halted when its Stop button is pressed (the chat Stop kills the
agent and its computer-use control together) and automatically re-armed when
that session's next turn starts. ``request_stop``/``resume`` may also be called
without a session id, which acts on the process-global ``"_global"`` key.

Flags and generations are synchronized across worker threads, the HTTP loop,
and the keyboard hook. Each short input operation checks its generation under
the same lock as Stop; resumed turns cannot revive an old cancelled worker.

Every halt records why it happened (``stop_reason``), and the tools tell the
model that cause in plain words (``describe``), so a model never has to guess
whether the user stopped it. Only real stops latch: a cancelled tool call
(timeout, interrupt) uses ``cancel_inflight``, which invalidates the running
worker without halting the session.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# session_id (or "_global") => halted flag.
_HALTED: dict = {}
_LOCK = threading.RLock()
_GENERATIONS: dict[str, int] = {}
# session_id (or "_global") => {"reason", "detail", "at"} of its current halt.
_REASONS: dict[str, dict] = {}
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


# Plain-language causes shown to the model. Keys are the ``reason`` values
# callers pass; the UI reads the same keys from the status endpoint.
_CAUSES = {
    "escape": "the user pressed Esc on the host computer",
    "user": "the user pressed Stop computer use in the app",
    # A turn interrupt whose source was not recorded (an older peer, a caller
    # that names none): neither the user nor the app is claimed.
    "stopped": "this chat's turn was stopped",
    "disabled": "computer use was turned off in Settings",
    "completed": "the turn that held control ended",
}


# Interrupt sources (turns.payload.interrupt_source, or the source a direct
# caller passes to SessionRuntime.interrupt) that are a person's Stop.
_USER_SOURCES = {"user_stop", "user_stopped", "user_stopped_all"}

# What the app was doing when it interrupted the turn. Unlisted sources are
# named as they are.
_APP_SOURCES = {
    "server_shutdown": "the app is shutting down",
    "session_handoff": "this chat is moving to another worker",
    "worker_update": "this worker is updating",
    "app_stop": "the app stopped this chat's work",
    "checkout_cleanup": "the app is removing this chat's checkout",
    "assistant": "an assistant limit or the assistant's job manager stopped the run",
    "automation_timeout": "the automation ran past its time limit",
    "lease_lost": "this worker lost ownership of the turn",
    "fence_superseded": "another worker took over the turn",
}


def kind(reason: str | None) -> str:
    """Who ended control: ``user``, ``app``, ``unknown``, ``disabled`` or
    ``completed``. Only ``user`` may be reported as the user's doing."""
    if reason and reason.startswith("interrupt:"):
        return "user" if reason.split(":", 1)[1] in _USER_SOURCES else "app"
    if reason in ("escape", "user"):
        return "user"
    if reason in ("disabled", "completed"):
        return reason
    return "unknown"


def user_caused(reason: str | None) -> bool:
    """Whether a person stopped control (as opposed to the app or a timeout)."""
    return kind(reason) == "user"


def interrupt_reason(source: str | None) -> str:
    """The halt reason for a turn interrupt from ``source`` (None: unknown)."""
    return f"interrupt:{source}" if source else "stopped"


def describe(reason: str | None) -> str:
    """The true cause of a halt, in words the model can repeat to the user.

    ``interrupt:<source>`` names a turn interrupt by its source, so an app
    interrupt (shutdown, usage limit, checkout cleanup) is never reported as
    the user's Stop.
    """
    if not reason:
        return "the action was cancelled before it finished"
    if reason.startswith("interrupt:"):
        source = reason.split(":", 1)[1]
        if source in _USER_SOURCES:
            return "the user pressed Stop in the chat"
        what = _APP_SOURCES.get(source, f"the app interrupted this turn ({source})")
        return f"{what}; the user did not stop it"
    return _CAUSES.get(reason, f"it was stopped ({reason})")


def _release_controllers(key: str) -> None:
    for controller in list(_controllers):
        if _key(controller.session_id) == key:
            controller.release_all()


def request_stop(session_id: str | None = None, *, reason: str = "stopped",
                 detail: str | None = None) -> None:
    """Halt computer-use for a session. Its actions refuse until resumed.

    ``reason`` is recorded and reported to the model; ``detail`` is evidence
    for the log (which key, which flags, ...).
    """
    key = _key(session_id)
    with _LOCK:
        already = _HALTED.get(key, False)
        _HALTED[key] = True
        if not already:
            _REASONS[key] = {"reason": reason, "detail": detail, "at": time.time()}
        _GENERATIONS[key] = _GENERATIONS.get(key, 0) + 1
        _release_controllers(key)
        from app.mcp.computer_use import activity
        activity.finish(session_id, reason=reason)
    if not already and reason != "completed":
        logger.warning("computer-use halted for session %s: reason=%s (%s)%s", key, reason,
                       describe(reason), f"; evidence: {detail}" if detail else "")


def cancel_inflight(session_id: str | None = None, *, reason: str = "cancelled",
                    detail: str | None = None) -> None:
    """Invalidate the session's running input without halting the session.

    For a tool call that was cancelled (the call timed out, or the turn is
    being interrupted, in which case the turn's own Stop handling halts it).
    The worker thread aborts at its next input, held keys and buttons are
    released, and the next call must capture a fresh screenshot. Later calls
    in the same turn still work.
    """
    key = _key(session_id)
    with _LOCK:
        _GENERATIONS[key] = _GENERATIONS.get(key, 0) + 1
        _release_controllers(key)
    logger.warning("computer-use call cancelled for session %s: reason=%s%s; input invalidated, "
                   "session stays armed", key, reason, f"; evidence: {detail}" if detail else "")


def stop_reason(session_id: str | None = None) -> str | None:
    """Why the session is halted, or None when it is not."""
    with _LOCK:
        key = _key(session_id)
        if not _HALTED.get(key):
            return None
        return (_REASONS.get(key) or {}).get("reason") or "stopped"


def resume(session_id: str | None = None) -> None:
    """Re-arm computer-use for a session, re-enabling its actions."""
    key = _key(session_id)
    with _LOCK:
        was = _HALTED.pop(key, False)
        _REASONS.pop(key, None)
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


def stop_active(reason: str = "escape", detail: str | None = None) -> int:
    """Physical Escape and the UI stop sessions currently using this host."""
    from app.mcp.computer_use import activity, access
    with _LOCK:
        sessions = activity.active_sessions()
        for sid in sessions:
            request_stop(sid, reason=reason, detail=detail)
            access.revoke(sid)
        return len(sessions)

"""Per-session halt switch for computer-use actions.

Each session's input actions check their own flag *before* every operation. A
session is halted when its Stop button is pressed (the chat Stop kills the
agent and its computer-use control together) and automatically re-armed when
that session's next turn starts. ``request_stop``/``resume`` may also be called
without a session id, which acts on the process-global ``"_global"`` key.

Flags are deliberately plain ``bool`` values in a dict rather than
``asyncio.Event``: they are read from worker threads (``asyncio.to_thread``)
and from multiple event loops (the HTTP request loop vs. the query loop); the
GIL makes a dict of bools the simplest thing that is correct across all of them.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# session_id (or "_global") => halted flag.
_HALTED: dict = {}
_LOCK = threading.Lock()


def _key(session_id: str | None) -> str:
    return session_id or "_global"


def request_stop(session_id: str | None = None) -> None:
    """Halt computer-use for a session. Its actions refuse until resumed."""
    key = _key(session_id)
    with _LOCK:
        already = _HALTED.get(key, False)
        _HALTED[key] = True
    if not already:
        logger.warning("computer-use halted for session %s — refusing its actions", key)


def resume(session_id: str | None = None) -> None:
    """Re-arm computer-use for a session, re-enabling its actions."""
    key = _key(session_id)
    with _LOCK:
        was = _HALTED.pop(key, False)
    if was:
        logger.info("computer-use resumed for session %s — actions re-enabled", key)


def is_stopped(session_id: str | None = None) -> bool:
    """Return True if computer-use is currently halted for this session."""
    with _LOCK:
        return bool(_HALTED.get(_key(session_id), False))


def any_stopped() -> bool:
    """Return True if ANY session (or the global switch) is currently halted.

    Used by the on-screen overlay, which cannot know which session is acting:
    it hides whenever every halt flag is clear.
    """
    with _LOCK:
        return any(_HALTED.values())

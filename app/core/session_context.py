"""Ambient current-session attribution via a :class:`contextvars.ContextVar`.

The SDK spawns a *detached* reader task inside ``client.connect()`` that logs
"Fatal error in message reader" when the CLI subprocess dies mid-turn. That task
runs outside any of our request handlers, so a durable ``error_log`` row for the
fatal previously had ``session_id`` NULL — zero forensics for exactly the
incidents we most need to diagnose.

A ``ContextVar`` set at each spawn/query site *before* the connect happens is
inherited by tasks created after the set (``asyncio.create_task`` snapshots the
current context), so the reader task inherits the session id. ``emit`` in
``error_log_handler`` reads it to attribute the fatal.

Setter/getter are trivial and never raise; ``get_current_session_id`` is safe to
call from the sync logging path.
"""

from __future__ import annotations

import contextvars
from typing import Optional

# Default None: outside a spawn/query context (or before it's set) there is no
# attributable session, and the error_log row keeps its NULL session_id.
current_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_session_id", default=None
)


def set_current_session_id(session_id: Optional[str]) -> None:
    """Bind the current session id for the active context (and tasks it spawns)."""
    current_session_id.set(session_id)


def get_current_session_id() -> Optional[str]:
    """Return the current session id, or None. Never raises."""
    try:
        return current_session_id.get()
    except Exception:  # noqa: BLE001 — a getter on the logging path must not raise
        return None

"""In-memory per-session registry for the live browser panel.

The spawned ``browser`` MCP server publishes the CDP page it is currently
driving (its ``target_id`` / ``tab_id`` / ``url``) back to the backend via the
loopback ``/api/v1/browser/panel/register`` endpoint, authenticated with a
per-session token this module issues. The screencast hub
(``app/core/browser_stream.py``) reads the current target here and re-attaches
whenever it changes.

State is process-local and ephemeral by design — it mirrors the live browser,
which is itself process-local; nothing here is persisted.

Concurrency: a plain ``threading.Lock`` guards the record dict so the token
issuance path (called synchronously while building query options, possibly off
the event loop) and the async register/read paths never race. The per-session
change signal is an ``asyncio.Event`` using the set-then-replace pattern so a
``register`` fired from the event loop wakes every current waiter and future
waiters get a fresh event.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class PanelTarget:
    """The CDP page the browser MCP server is currently driving."""

    target_id: Optional[str]
    tab_id: Optional[str]
    url: Optional[str]


class _Record:
    __slots__ = ("token", "target", "change_event")

    def __init__(self) -> None:
        self.token: Optional[str] = None
        self.target: Optional[PanelTarget] = None
        # Constructed without a bound loop (Python 3.10+); safe to create off
        # the event loop and set later from the loop thread.
        self.change_event: asyncio.Event = asyncio.Event()


_lock = threading.Lock()
_records: Dict[str, _Record] = {}


def _get_or_create(session_id: str) -> _Record:
    rec = _records.get(session_id)
    if rec is None:
        rec = _Record()
        _records[session_id] = rec
    return rec


def issue_token(session_id: str) -> str:
    """Return the session's panel token, minting one on first call.

    Idempotent: repeated calls for the same session return the same token so the
    value baked into the MCP server env stays stable across turns.
    """
    with _lock:
        rec = _get_or_create(session_id)
        if rec.token is None:
            rec.token = secrets.token_urlsafe(32)
        return rec.token


def verify_token(session_id: str, token: Optional[str]) -> bool:
    """Constant-time check that ``token`` matches the session's issued token."""
    with _lock:
        rec = _records.get(session_id)
        if rec is None or rec.token is None or token is None:
            return False
        return secrets.compare_digest(rec.token, token)


def register(
    session_id: str,
    target_id: Optional[str],
    tab_id: Optional[str],
    url: Optional[str],
) -> None:
    """Store the session target and wake waiters only when it changed.

    Browser tools commonly publish the same target after every operation.  An
    identical registration is not a lifecycle transition and must not force the
    screencast hub to detach/reconnect (that was the live-preview flicker).
    """
    new_target = PanelTarget(target_id=target_id, tab_id=tab_id, url=url)
    with _lock:
        rec = _get_or_create(session_id)
        if rec.target == new_target:
            return
        rec.target = new_target
        event = rec.change_event
        rec.change_event = asyncio.Event()
    # Fire outside the lock; safe when called from the event-loop thread.
    event.set()


def get_target(session_id: str) -> Optional[PanelTarget]:
    """Return the session's current target, or None if none registered."""
    with _lock:
        rec = _records.get(session_id)
        return rec.target if rec is not None else None


async def wait_for_change(session_id: str) -> None:
    """Await the next ``register`` for this session."""
    with _lock:
        rec = _get_or_create(session_id)
        event = rec.change_event
    await event.wait()


def clear(session_id: str) -> None:
    """Drop all state for a session."""
    with _lock:
        _records.pop(session_id, None)

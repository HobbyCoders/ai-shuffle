"""Server-Sent Events tail for the per-session event log (v2 core).

``GET /api/v1/sessions/{session_id}/events`` streams the append-only event log
(``app.db.session_events``) as SSE. It backfills from ``from_seq`` then tails
live, waking on gateway notifies (``app.core.event_gateway``) and — crucially —
falling back to a keepalive-timer poll of ``get_max_seq`` so delivery never
depends on LISTEN/NOTIFY being healthy.

Ordering is gap-free: the gateway queue is subscribed *before* backfill, so no
event can slip between the backfill snapshot and the live tail, and every frame
is emitted in strictly increasing ``seq`` order (``cursor`` dedupes).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from app.api.auth import require_auth
from app.core.event_gateway import get_gateway
from app.core.history_backfill import ensure_backfilled
from app.db.session_events import get_events, get_max_seq

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["Sessions"])

# Keepalive / poll cadence. Monkeypatched small in tests.
KEEPALIVE_SECONDS = 15.0

_PAGE = 500


def _is_client_disconnect(exc: BaseException) -> bool:
    """True when ``exc`` represents a normal SSE client disconnect.

    On client disconnect (tab close, navigation, network blip) Starlette
    cancels the streaming task, throwing ``CancelledError`` into this generator
    at its current ``await``. Because the app wraps every request in a stack of
    ``BaseHTTPMiddleware`` (CSRF, access-log, rate-limit, ...), a cancellation
    that escapes the generator collapses into an anyio ``ExceptionGroup`` that
    surfaces as an unhandled 500 instead of a clean connection close (issue
    #627). This recognizes both the plain form and that group-wrapped form so
    the caller can swallow it — while any group carrying a *real* error is left
    to propagate.
    """
    if isinstance(exc, (asyncio.CancelledError, ClientDisconnect)):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(
            _is_client_disconnect(sub) for sub in exc.exceptions
        )
    return False


def _wire(row: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a DB row's payload with its envelope (envelope keys win)."""
    payload = row.get("payload") or {}
    created_at = row.get("created_at")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    return {
        **payload,
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "seq": row.get("seq"),
        "run_id": row.get("run_id"),
        "turn_id": row.get("turn_id"),
        "command_id": row.get("command_id"),
        "type": row.get("event_type"),
        "created_at": created_at,
    }


def _frame(row: Dict[str, Any]) -> str:
    wire = _wire(row)
    data = json.dumps(wire, default=str)
    return f"id: {row['seq']}\nevent: {row['event_type']}\ndata: {data}\n\n"


async def event_stream(session_id: str, from_seq: int = 0):
    """Yield SSE frames for a session: backfill from ``from_seq``, then tail.

    Module-level (rather than a closure in the endpoint) so tests can consume
    the stream directly — ASGI test transports buffer the whole response and
    cannot read a never-ending SSE body incrementally.
    """
    # Seed the log from legacy history (idempotent, no-op once populated)
    # BEFORE subscribing, so the backfill pass below already sees it.
    await ensure_backfilled(session_id)

    gateway = get_gateway()
    # Subscribe FIRST so no event falls between backfill and the live tail.
    queue = gateway.subscribe(session_id)
    try:
        cursor = from_seq

        # 1. Backfill everything already persisted.
        while True:
            page = await asyncio.to_thread(get_events, session_id, cursor, _PAGE)
            for row in page:
                yield _frame(row)
                cursor = row["seq"]
            if len(page) < _PAGE:
                break

        # 1b. Catch-up sentinel: mark the history/live boundary so the client can
        #     paint the whole backfilled transcript in ONE batch instead of
        #     repainting per backfilled frame. Carries no ``id:`` line, so it does
        #     not advance the reconnect cursor (``lastSeq``); it re-arms per connect
        #     (a near-empty reconnect backfill just folds once).
        yield f"event: caught_up\ndata: {json.dumps({'seq': cursor})}\n\n"

        # 2. Live tail. Wake on a notify OR the keepalive timer; either way
        #    reconcile against get_max_seq so we also serve as the poll
        #    fallback when notifies are absent.
        while True:
            timed_out = False
            try:
                await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                timed_out = True

            max_seq = await asyncio.to_thread(get_max_seq, session_id)
            if max_seq > cursor:
                while True:
                    page = await asyncio.to_thread(
                        get_events, session_id, cursor, _PAGE
                    )
                    if not page:
                        break
                    for row in page:
                        if row["seq"] > cursor:
                            yield _frame(row)
                            cursor = row["seq"]
                    if len(page) < _PAGE:
                        break
            elif timed_out:
                yield ": keepalive\n\n"
    except (asyncio.CancelledError, ClientDisconnect, BaseExceptionGroup) as exc:
        # A normal SSE client disconnect must end the stream cleanly, NOT bubble
        # a cancellation out of the generator where it collapses into an anyio
        # ExceptionGroup and surfaces as an unhandled 500 through the
        # BaseHTTPMiddleware stack (issue #627). Anything that is not a pure
        # disconnect/cancellation is re-raised unchanged.
        if not _is_client_disconnect(exc):
            raise
        logger.debug(
            "SSE event stream closed (client disconnect) for session %s", session_id
        )
    finally:
        gateway.unsubscribe(session_id, queue)


@router.get("/{session_id}/events")
async def stream_session_events(
    session_id: str,
    from_seq: int = 0,
    token: str = Depends(require_auth),
):
    """Stream the session event log as SSE, backfilling from ``from_seq``."""
    return StreamingResponse(
        event_stream(session_id, from_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

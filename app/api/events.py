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
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from app.api.auth import require_auth
from app.api.sse_utils import is_client_disconnect
from app.core.event_gateway import get_gateway
from app.core.history_backfill import ensure_backfilled
from app.db.session_events import (
    get_events,
    get_max_seq,
    get_user_seq_for_turn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["Sessions"])

# Keepalive / poll cadence. Monkeypatched small in tests.
KEEPALIVE_SECONDS = 15.0

_PAGE = 500

# Streaming deltas are coalesced during backfill. A finished turn's
# assistant.thinking_delta / assistant.text_delta rows are the same text the
# client would concatenate anyway, so replaying them one frame at a time only
# costs bandwidth and repaints. Consecutive rows of the same delta type within
# one turn/run are merged into a single frame carrying the joined text. History
# is never dropped: every event still reaches the client, and the live tail
# (step 2) keeps streaming per-frame so typing still animates.
_COALESCE_TYPES = ("assistant.thinking_delta", "assistant.text_delta")


def _sdk_session_id(session_id: str) -> Optional[str]:
    """Return the app session's Fluid transcript id, if it has one."""
    try:
        from app.db import sessions as sessions_db

        row = sessions_db.get_session(session_id)
        sdk = (row or {}).get("sdk_session_id")
        return str(sdk) if sdk else None
    except Exception:
        logger.debug("sdk_session_id lookup failed for %s", session_id, exc_info=True)
        return None


def _compacted_replay_start(session_id: str) -> tuple[int, int, int]:
    """Return ``(first_kept_seq, elided_events, tier)`` or ``(0, 0, 0)``.

    Only a persisted, active-path, contiguous Fluid compacted prefix qualifies.
    Any mismatch between Fluid turns and app events fails closed to a full
    replay; sessions without compaction are never arbitrarily truncated.
    """
    sdk_session_id = _sdk_session_id(session_id)
    if not sdk_session_id:
        return 0, 0, 0
    try:
        from app.core.provider.fluid_transcript import (
            latest_compaction_boundary,
            looks_like_fluid_session_id,
        )

        if not looks_like_fluid_session_id(sdk_session_id):
            return 0, 0, 0
        boundary = latest_compaction_boundary(sdk_session_id)
        if boundary is None:
            return 0, 0, 0
        covered_turns = int(boundary.get("turns") or 0)
        retained_turn_id = boundary.get("first_retained_turn_id")
        if covered_turns <= 0 or not isinstance(retained_turn_id, str):
            return 0, 0, 0
        # Fluid TurnStarted persists the exact node -> durable turn-id bridge.
        # The app query selects that turn's FIRST message.user, so mid-turn steers
        # cannot become a replay boundary. Rewound/legacy logs fail closed.
        first_kept = get_user_seq_for_turn(session_id, retained_turn_id)
        if first_kept is None:
            return 0, 0, 0
        return first_kept, first_kept - 1, int(boundary.get("tier") or 0)
    except Exception:
        logger.debug(
            "compacted replay lookup failed for %s", session_id, exc_info=True
        )
        return 0, 0, 0


def _boundary_frame(
    session_id: str, first_kept_seq: int, elided_events: int, tier: int
) -> str:
    """Synthetic compaction marker; deliberately has no SSE ``id:`` line."""
    data = json.dumps(
        {
            "id": f"elided-{session_id}-{first_kept_seq}",
            "session_id": session_id,
            "type": "system.compaction_boundary",
            "subtype": "compact_boundary",
            "data": {
                "trigger": "compacted",
                "tier": tier,
                "elidedEvents": elided_events,
            },
            "seq": first_kept_seq - 1,
            "created_at": None,
        },
        default=str,
    )
    return f"event: system.compaction_boundary\ndata: {data}\n\n"


def _delta_key(row: Dict[str, Any]) -> tuple:
    """What makes two delta rows part of the same run."""
    return (row.get("event_type"), row.get("turn_id"), row.get("run_id"))


def _merged_row(run: Dict[str, Any]) -> Dict[str, Any]:
    """The run's single frame row: first envelope, joined text, LAST seq.

    The seq must be the last row's so the client's reconnect cursor (the SSE
    ``id:`` line) does not rewind into the middle of a merged run.
    """
    first = run["first"]
    row = dict(first)
    payload = dict(first.get("payload") or {})
    payload["text"] = "".join(run["texts"])
    row["payload"] = payload
    row["seq"] = run["last_seq"]
    return row


def _open_run(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": _delta_key(row),
        "first": row,
        "texts": [(row.get("payload") or {}).get("text") or ""],
        "last_seq": row["seq"],
    }


def _coalesce_deltas(
    rows: Sequence[Dict[str, Any]], pending: List[Optional[Dict[str, Any]]]
) -> Iterator[Dict[str, Any]]:
    """Yield frame-ready rows, merging consecutive streaming deltas.

    ``pending`` is a one-slot list holding the run still open from an earlier
    page (``[None]`` to start), so a run split across a page boundary still
    merges; the caller drains what is left with :func:`_flush_pending` once
    paging ends. Any non-delta row closes the open run before it is emitted.
    """
    for row in rows:
        run = pending[0]
        if row.get("event_type") in _COALESCE_TYPES:
            if run is not None and run["key"] == _delta_key(row):
                run["texts"].append((row.get("payload") or {}).get("text") or "")
                run["last_seq"] = row["seq"]
                continue
            if run is not None:
                yield _merged_row(run)
            pending[0] = _open_run(row)
            continue
        if run is not None:
            yield _merged_row(run)
            pending[0] = None
        yield row


def _flush_pending(
    pending: List[Optional[Dict[str, Any]]],
) -> Iterator[Dict[str, Any]]:
    """Emit the run still open at the end of the backfill, if any."""
    run = pending[0]
    if run is not None:
        pending[0] = None
        yield _merged_row(run)


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
    start_time = time.monotonic()
    cursor = from_seq
    logger.info(
        "SSE feed connected: session=%s from_seq=%s", session_id, from_seq
    )
    try:
        # 1. A fresh load starts after the latest SAFE compacted prefix.  A
        #    reconnect keeps its own cursor and must never apply fresh-load
        #    elision.  The synthetic marker has no SSE id, so reconnect state is
        #    still advanced only by real persisted events.
        if cursor == 0:
            first_kept, elided_events, tier = await asyncio.to_thread(
                _compacted_replay_start, session_id
            )
            if first_kept > 0:
                yield _boundary_frame(
                    session_id, first_kept, elided_events, tier
                )
                cursor = first_kept - 1

        # Consecutive historical streaming deltas remain coalesced into one
        # frame per run. ``page_from`` pages the DB while ``cursor`` tracks the
        # last row actually emitted (it can lag while a delta run is open).
        pending: List[Optional[Dict[str, Any]]] = [None]
        page_from = cursor
        while True:
            page = await asyncio.to_thread(
                get_events, session_id, page_from, _PAGE
            )
            for row in _coalesce_deltas(page, pending):
                yield _frame(row)
                cursor = row["seq"]
            if page:
                page_from = page[-1]["seq"]
            if len(page) < _PAGE:
                break
        for row in _flush_pending(pending):
            yield _frame(row)
            cursor = row["seq"]

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
        if not is_client_disconnect(exc):
            raise
        logger.debug(
            "SSE event stream closed (client disconnect) for session %s", session_id
        )
    finally:
        logger.info(
            "SSE feed disconnected: session=%s last_seq=%s duration=%.1fs",
            session_id,
            cursor,
            time.monotonic() - start_time,
        )
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

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
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from app.api.auth import require_auth
from app.api.sse_utils import is_client_disconnect
from app.core.event_gateway import get_gateway
from app.core.history_backfill import ensure_backfilled
from app.db.session_events import (
    get_events,
    get_first_user_seq_after,
    get_max_seq,
    get_user_seq_at_ordinal,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["Sessions"])

# Keepalive / poll cadence. Monkeypatched small in tests.
KEEPALIVE_SECONDS = 15.0

_PAGE = 500

# Replay elision. Opening a long session must load its latest compacted state,
# not stream the entire append-only log into a fresh browser. Two independent
# bounds:
#
# * fluid sessions with persisted ContextCompaction promotions replay only the
#   events AFTER the last promotion's end node — everything earlier is what the
#   engine already folded into digests/epoch stubs.
# * every log additionally gets a hard event-count cap so a never-compacted
#   session (delegated CLI legs have no fluid ladder) still cannot ship an
#   unbounded payload; the cut snaps forward to the next message.user row so a
#   tool_use/tool_result pair is never split across it.
#
# Both paths lead with a synthetic system.compaction_boundary frame (no ``id:``
# line — like caught_up, it does not advance the reconnect cursor) so the UI
# shows a "context was compacted" marker where history was elided.
MAX_REPLAY_EVENTS = 600


def _sdk_session_id(session_id: str) -> Optional[str]:
    """The session's engine transcript id (``ses_...`` for fluid sessions)."""
    try:
        from app.db import sessions as sessions_db

        row = sessions_db.get_session(session_id)
        sdk = (row or {}).get("sdk_session_id")
        return str(sdk) if sdk else None
    except Exception:
        logger.debug("sdk_session_id lookup failed for %s", session_id, exc_info=True)
        return None


def _turn_start_nodes(sdk_session_id: str) -> list:
    """The session's turn-start user nodes, root-first.

    Mirrors fluid's ``is_turn_start``: a user node with any non-tool-result
    content block. These are exactly the nodes a compaction span may end
    before, and exactly what a ``message.user`` event was recorded for.
    """
    try:
        from app.core.provider.fluid_transcript import (
            looks_like_fluid_session_id,
            load_fluid_path,
        )

        if not looks_like_fluid_session_id(sdk_session_id):
            return []
        from app.core.provider.fluid_transcript import _as_dict

        starts = []
        for node in load_fluid_path(sdk_session_id):
            message = _as_dict(node.get("message"))
            blocks = message.get("content") or []
            if (
                (message.get("role") == "user")
                and any(
                    isinstance(b, dict) and b.get("kind") != "toolresultblock"
                    for b in blocks
                )
            ):
                starts.append(node)
        return starts
    except Exception:
        logger.debug(
            "turn-start walk failed for %s", sdk_session_id, exc_info=True
        )
        return []


def _compaction_turn_cutoff(session_id: str) -> Optional[int]:
    """Seq of the FIRST turn the engine still holds verbatim, or None.

    The last persisted ContextCompaction promotion ends at some node E; the
    next turn-start node after it is the first content a compacted replay must
    show. Turn ordinals bridge node-space to event-log space: the k-th
    turn-start node was recorded as the k-th ``message.user`` row (both live
    recording and the legacy backfill emit exactly one per human turn), so
    cutting at that row's seq keeps whole turns only.
    """
    sdk = _sdk_session_id(session_id)
    if not sdk:
        return None
    try:
        from app.core.provider.fluid_transcript import latest_compaction_boundary

        boundary = latest_compaction_boundary(sdk)
        if boundary is None:
            return None
        starts = _turn_start_nodes(sdk)
        cut_index = None
        for i, node in enumerate(starts):
            if node.get("node_id") == boundary["node_id"]:
                cut_index = i + 1  # first turn AFTER the promoted region
                break
        if cut_index is None:
            return None
        from app.db.session_events import get_user_seq_at_ordinal

        return get_user_seq_at_ordinal(session_id, cut_index + 1)
    except Exception:
        logger.debug(
            "compaction-aware cutoff failed for %s", session_id, exc_info=True
        )
        return None


def _replay_elision(session_id: str) -> tuple:
    """``(cutoff_seq, elided_count, reason)`` for this replay, or ``(0, 0, "")``.

    Compaction-aware when the session's engine ladder has promoted spans;
    otherwise a plain count cap bounds a never-compacted long log (delegated
    CLI legs have no fluid ladder). Both bounds then snap FORWARD to the next
    ``message.user`` row so the elided prefix always ends on a completed turn
    and no tool_use ever loses its result. When no turn-start row exists at or
    after a bound (the log tail is mid-turn), nothing is elided — replaying is
    always safe, truncating is not.
    """
    try:
        total = get_max_seq(session_id)
        if total <= MAX_REPLAY_EVENTS:
            return 0, 0, ""

        count_bound = max(1, total - MAX_REPLAY_EVENTS + 1)
        compaction_bound = _compaction_turn_cutoff(session_id)

        # The tighter (larger) bound wins; both are already turn-start rows,
        # so the snap below is a belt-and-braces guard for the cap path only
        # when compaction produced nothing.
        candidates = [b for b in (count_bound, compaction_bound) if b]
        if not candidates:
            return 0, 0, ""
        bound = max(candidates)
        if compaction_bound is not None and bound >= compaction_bound:
            reason = "compacted"
        else:
            reason = "capped"

        # Everything strictly before the cutoff row is elided; the boundary
        # marker tells the UI how much.
        snapped, _n = get_first_user_seq_after(session_id, bound - 1)
        if snapped is None:
            return 0, 0, ""
        return snapped, snapped - 1, reason
    except Exception:
        logger.debug(
            "replay elision failed for %s", session_id, exc_info=True
        )
        return 0, 0, ""


def _boundary_frame(seq: int, elided: int, reason: str) -> str:
    """A synthetic system.compaction_boundary SSE frame (no ``id:`` line)."""
    data = json.dumps({
        "id": f"elided-{seq}",
        "session_id": "",
        "type": "system.compaction_boundary",
        "subtype": "compact_boundary",
        "data": {
            "trigger": reason,
            "preTokens": None,
            "elidedEvents": elided,
        },
        "seq": seq,
        "created_at": None,
    }, default=str)
    return f"event: system.compaction_boundary\ndata: {data}\n\n"



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
        # 1. Backfill the persisted log — from the latest compacted state when
        #    the log is long enough to warrant it (see _replay_elision), else
        #    in full. A full-load (from_seq == 0) only: a reconnect resumes at
        #    its own cursor and must never skip events.
        if cursor == 0:
            cutoff, elided_count, reason = await asyncio.to_thread(
                _replay_elision, session_id
            )
            if cutoff > 0:
                yield _boundary_frame(cutoff, elided_count, reason)
                cursor = cutoff - 1
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

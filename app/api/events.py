"""Server-Sent Events tail for the per-session event log (v2 core).

``GET /api/v1/sessions/{session_id}/events`` streams the append-only event log
(``app.db.session_events``) as SSE. It backfills from ``from_seq`` then tails
live, waking on gateway notifies (``app.core.event_gateway``) and falling back
to reading rows from its own cursor (``_tail_from_db``) on the keepalive timer,
so delivery never depends on LISTEN/NOTIFY being healthy.

Ordering is gap-free: the session's shared tail reader is subscribed *before*
backfill, so no event can slip between the backfill snapshot and the live tail,
and every frame is emitted in strictly increasing ``seq`` order (``cursor``
dedupes).

Live delivery is shared per session, not per client (:class:`_SessionTail`):
ONE reader per session does one read and one serialization per append batch and
fans the resulting frame text out to every subscriber. A client only emits a pushed batch when it continues exactly
where that client left off; anything else (a dropped batch, a late attach, a
degraded reader) falls back to reading from its own cursor, so the
"``seq > from_seq`` is gap- and duplicate-free" contract holds by construction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import (
    Any,
    AsyncIterator,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from app.api.auth import require_auth
from app.api.sessions_helpers import require_session
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

# Correctness re-sync cadence for a client whose live delivery is healthy (the
# gateway LISTEN connection is up AND this session's shared reader is running).
# The keepalive tick then costs a comment, not a DB read; the moment either
# health signal drops, every tick reconciles again.
LIVE_RESYNC_SECONDS = 120.0

# Per-client push queue depth. Frames are shared strings (one serialization,
# many references), so the real bound is on how far a slow client may lag before
# we stop buffering for it. Overflow is SAFE, not lossy: dropped batches leave a
# discontinuity the client detects and repairs with a read from its own cursor.
_TAIL_QUEUE_MAXSIZE = 64

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


# ---------------------------------------------------------------------------
# Shared per-session tail reader
#
# One reader per session, not per client: it holds the session's ONLY gateway
# subscription, turns each notify tick into a single ``get_events`` read plus a
# single ``_frame`` serialization pass, and publishes the resulting text to
# every attached client.
#
# Safety of the pushed path — a batch is emitted by a client ONLY when
# ``batch.prev_seq`` equals that client's own cursor, i.e. the batch starts
# exactly where the client stopped. Rows inside a batch come from one
# ``get_events`` call in ``seq ASC`` order, and the reader advances its cursor
# to the last row it published, so consecutive batches chain end-to-end. Any
# other case — a batch dropped by queue overflow, a client that attached
# mid-flight, a reader that died — leaves ``prev_seq != cursor``, and the client
# repairs it by reading from its own cursor instead of trusting the push. A
# pushed row can therefore never arrive out of order, be skipped, or be
# duplicated: when in doubt we read.
# ---------------------------------------------------------------------------


class _TailBatch(NamedTuple):
    """One published batch: the cursor it continues from and its frames."""

    prev_seq: int
    last_seq: int
    frames: Tuple[Tuple[int, str], ...]


# Pushed into the reader's gateway queue to force a read pass (cursor seeding).
_KICK = object()


class _TailSubscriber:
    """One connected SSE client's slot on a session's shared reader."""

    __slots__ = ("queue",)

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_TAIL_QUEUE_MAXSIZE)


class _SessionTail:
    """The shared reader for one session. Created with its first client."""

    def __init__(self, session_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self.session_id = session_id
        self.loop = loop
        self.closed = False
        # True only while the reader task is actually running; a client that
        # sees False keeps reconciling on every keepalive tick.
        self.alive = False
        self._subs: Set[_TailSubscriber] = set()
        # Cursor of the last row published. ``None`` until seeded (see _seed).
        self._cursor: Optional[int] = None
        self._hint: Optional[int] = None
        self._gateway = get_gateway()
        self._queue = self._gateway.subscribe(session_id)
        self._task = loop.create_task(self._run())
        # Fire-and-forget: teardown cancels it, and a destroy-pending warning on
        # loop shutdown would be pure noise.
        self._task._log_destroy_pending = False  # type: ignore[attr-defined]

    # -- subscriber bookkeeping ------------------------------------------
    def attach(self) -> _TailSubscriber:
        sub = _TailSubscriber()
        self._subs.add(sub)
        return sub

    def detach(self, sub: _TailSubscriber) -> None:
        self._subs.discard(sub)
        if not self._subs:
            self.close()

    def close(self) -> None:
        """Drop the gateway subscription and stop the reader (idempotent).

        The unsubscribe is SYNCHRONOUS — the last client's teardown must leave
        no gateway registration behind even if the cancelled task has not been
        scheduled yet.
        """
        if self.closed:
            return
        self.closed = True
        self.alive = False
        if _tails.get(self.session_id) is self:
            _tails.pop(self.session_id, None)
        try:
            self._gateway.unsubscribe(self.session_id, self._queue)
        except Exception:  # noqa: BLE001 — teardown must never raise
            logger.debug(
                "SSE tail unsubscribe failed for %s", self.session_id, exc_info=True
            )
        try:
            self._task.cancel()
        except RuntimeError:
            pass  # the reader's loop is already closed — nothing left to stop

    def note_cursor(self, cursor: int) -> None:
        """Seed the reader from a client that just finished its backfill.

        The reader must not start at the CURRENT tail: an event appended between
        that client's last backfill page and the reader's first pass would then
        never be pushed. It must not blindly trust the client either
        (``from_seq`` is caller-supplied). It therefore starts at
        ``min(hint, get_max_seq)`` — never above the real tail, never above what
        a connected client has already been sent.
        """
        if self._cursor is not None or self.closed:
            return
        self._hint = cursor if self._hint is None else min(self._hint, cursor)
        self._kick()

    def _kick(self) -> None:
        try:
            self._queue.put_nowait(_KICK)
        except Exception:  # noqa: BLE001
            logger.debug("SSE tail kick failed for %s", self.session_id, exc_info=True)

    # -- reader ------------------------------------------------------------
    async def _run(self) -> None:
        self.alive = True
        try:
            while True:
                item = await self._queue.get()
                force, max_seq = self._drain_ticks(item)
                if self._cursor is None:
                    if self._hint is None or not await self._seed():
                        continue
                elif not force and max_seq <= self._cursor:
                    # Redundant tick (e.g. a bare notify_session wake): nothing
                    # past our cursor exists, so no read at all.
                    continue
                await self._read_and_publish()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a reader bug must not wedge clients
            logger.warning(
                "SSE tail reader failed for %s — clients fall back to polling",
                self.session_id, exc_info=True,
            )
        finally:
            self.alive = False
            # Wake every client so it reconciles from the database immediately
            # rather than waiting out a keepalive interval.
            self._publish(None)

    def _drain_ticks(self, item: Any) -> Tuple[bool, int]:
        """Collapse a burst of queued ticks into one (force, max_seq) decision."""
        force = item is _KICK
        max_seq = 0 if force else int(item)
        while True:
            try:
                nxt = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if nxt is _KICK:
                force = True
            else:
                max_seq = max(max_seq, int(nxt))
        return force, max_seq

    async def _seed(self) -> bool:
        try:
            db_max = await asyncio.to_thread(get_max_seq, self.session_id)
        except Exception:  # noqa: BLE001 — retry on the next tick
            logger.debug(
                "SSE tail seed read failed for %s", self.session_id, exc_info=True
            )
            return False
        self._cursor = min(int(self._hint or 0), int(db_max))
        return True

    async def _read_and_publish(self) -> None:
        """ONE read + ONE serialization per batch, fanned out to every client."""
        while True:
            cursor = int(self._cursor or 0)
            rows = await asyncio.to_thread(get_events, self.session_id, cursor, _PAGE)
            if not rows:
                return
            frames = tuple(
                (row["seq"], _frame(row)) for row in rows if row["seq"] > cursor
            )
            if not frames:
                return
            self._cursor = frames[-1][0]
            self._publish(_TailBatch(cursor, frames[-1][0], frames))
            if len(rows) < _PAGE:
                return

    def _publish(self, batch: Optional[_TailBatch]) -> None:
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(batch)
            except asyncio.QueueFull:
                # Safe by construction: the drop breaks batch continuity, and
                # the client repairs that with a read from its own cursor.
                logger.debug(
                    "SSE tail subscriber lagging on %s — dropped a batch",
                    self.session_id,
                )


# session_id -> the live reader for it. Only ever touched from the event loop.
_tails: Dict[str, _SessionTail] = {}


def _acquire_tail(session_id: str) -> _SessionTail:
    """Return this session's reader, creating it for the first client."""
    loop = asyncio.get_running_loop()
    tail = _tails.get(session_id)
    if tail is not None and (tail.closed or tail.loop is not loop):
        # A reader left behind by another (or a finished) event loop — e.g.
        # between tests. Release its gateway registration and start fresh.
        tail.close()
        tail = None
    if tail is None:
        tail = _SessionTail(session_id, loop)
        _tails[session_id] = tail
    return tail


def _push_delivery_healthy(tail: _SessionTail) -> bool:
    """True when live delivery can be trusted without a per-tick DB read."""
    try:
        return tail.alive and not tail.closed and get_gateway().is_connected()
    except Exception:  # noqa: BLE001
        return False


async def _replay_from(
    session_id: str, from_seq: int
) -> AsyncIterator[Tuple[int, str]]:
    """Yield ``(seq, frame)`` for the backfill, coalescing streaming deltas."""
    pending: List[Optional[Dict[str, Any]]] = [None]
    page_from = from_seq
    while True:
        page = await asyncio.to_thread(get_events, session_id, page_from, _PAGE)
        for row in _coalesce_deltas(page, pending):
            yield row["seq"], _frame(row)
        if page:
            page_from = page[-1]["seq"]
        if len(page) < _PAGE:
            break
    for row in _flush_pending(pending):
        yield row["seq"], _frame(row)


async def _tail_from_db(
    session_id: str, from_seq: int
) -> AsyncIterator[Tuple[int, str]]:
    """Yield ``(seq, frame)`` for everything past ``from_seq`` — the read path.

    The correctness fallback for the pushed path (and the whole delivery path
    while the gateway is down). Deliberately NOT coalesced: the live tail
    streams per-frame so typing animates.
    """
    cursor = from_seq
    while True:
        page = await asyncio.to_thread(get_events, session_id, cursor, _PAGE)
        if not page:
            return
        for row in page:
            if row["seq"] > cursor:
                yield row["seq"], _frame(row)
                cursor = row["seq"]
        if len(page) < _PAGE:
            return


async def event_stream(session_id: str, from_seq: int = 0):
    """Yield SSE frames for a session: backfill from ``from_seq``, then tail.

    Module-level (rather than a closure in the endpoint) so tests can consume
    the stream directly — ASGI test transports buffer the whole response and
    cannot read a never-ending SSE body incrementally.
    """
    # Attach to the session's shared reader FIRST so no event can fall between
    # the backfill snapshot and the live tail.
    tail = _acquire_tail(session_id)
    sub = tail.attach()
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
        # frame per run (see ``_replay_from``).
        replayed = False
        async for seq, frame in _replay_from(session_id, cursor):
            yield frame
            cursor = seq
            replayed = True

        # 1a. Legacy-history seeding, made LAZY. ``ensure_backfilled``
        #     re-materializes a whole legacy transcript into ``session_events``;
        #     it is needed only when this feed is the render authority AND the
        #     log has nothing to serve. A populated log (the overwhelmingly
        #     common case) no longer pays even the probing read, and a reconnect
        #     (``from_seq > 0``) never re-seeds history the client already holds
        #     — seeding one there would replay a duplicate legacy prefix past a
        #     cursor the client cannot reconcile.
        if not replayed and cursor == 0 and from_seq == 0:
            await ensure_backfilled(session_id)
            async for seq, frame in _replay_from(session_id, cursor):
                yield frame
                cursor = seq

        # Seed the shared reader from this client's post-backfill cursor, so the
        # very next append is pushed rather than waited for.
        tail.note_cursor(cursor)

        # 1b. Catch-up sentinel: mark the history/live boundary so the client can
        #     paint the whole backfilled transcript in ONE batch instead of
        #     repainting per backfilled frame. Carries no ``id:`` line, so it does
        #     not advance the reconnect cursor (``lastSeq``); it re-arms per connect
        #     (a near-empty reconnect backfill just folds once).
        yield f"event: caught_up\ndata: {json.dumps({'seq': cursor})}\n\n"

        # 2. Live tail. Steady state is a pushed batch from the session's shared
        #    reader — no DB work per client. Everything else (a discontinuity, a
        #    degraded reader, a dead gateway, the periodic re-sync) reconciles by
        #    reading from this client's own cursor.
        resync_at = time.monotonic() + LIVE_RESYNC_SECONDS
        while True:
            timed_out = False
            item: Any = None
            try:
                item = await asyncio.wait_for(
                    sub.queue.get(), timeout=KEEPALIVE_SECONDS
                )
            except asyncio.TimeoutError:
                timed_out = True

            reconcile = False
            if isinstance(item, _TailBatch):
                if item.prev_seq == cursor:
                    # Continues exactly where we stopped: emit as-is.
                    for seq, frame in item.frames:
                        yield frame
                        cursor = seq
                elif item.last_seq > cursor:
                    # Discontinuity (dropped batch / late attach): never guess.
                    reconcile = True
                # else: wholly-seen batch — drop it.
            elif not timed_out:
                # ``None`` marker: the shared reader stopped.
                reconcile = True

            now = time.monotonic()
            if timed_out:
                reconcile = now >= resync_at or not _push_delivery_healthy(tail)
            elif now >= resync_at:
                reconcile = True

            if not reconcile:
                if timed_out:
                    yield ": keepalive\n\n"
                continue

            wrote = False
            async for seq, frame in _tail_from_db(session_id, cursor):
                yield frame
                cursor = seq
                wrote = True
            resync_at = time.monotonic() + LIVE_RESYNC_SECONDS
            if timed_out and not wrote:
                yield ": keepalive\n\n"
    except (asyncio.CancelledError, ClientDisconnect, BaseExceptionGroup) as exc:
        # A normal SSE client disconnect must end the stream cleanly, NOT bubble
        # a cancellation out of the generator where it collapses into an anyio
        # ExceptionGroup and surfaces as an unhandled 500 through the
        # BaseHTTPMiddleware stack. Anything that is not a pure
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
        tail.detach(sub)


@router.get("/{session_id}/events")
async def stream_session_events(
    request: Request,
    session_id: str,
    from_seq: int = 0,
    token: str = Depends(require_auth),
):
    """Stream the session event log as SSE, backfilling from ``from_seq``."""
    await require_session(request, session_id)
    return StreamingResponse(
        event_stream(session_id, from_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

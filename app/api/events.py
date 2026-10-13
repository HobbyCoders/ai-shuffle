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
import os
import time
from typing import (
    Any,
    AsyncGenerator,
    AsyncIterator,
    Dict,
    Iterable,
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
from app.api.sessions_helpers import require_transcript_session
from app.api.sse_utils import is_client_disconnect
from app.core.event_gateway import get_gateway
from app.core.harness_reminders import strip_harness_reminders
from app.core.history_backfill import ensure_backfilled
from app.core.turn_recorder import slim_turn_usage_payload
from app.db.session_events import (
    EMPTY_LOG_EPOCH,
    get_events,
    get_compaction_events_before,
    get_log_epoch,
    get_max_seq,
    get_tool_use_seqs,
    get_user_seq_for_turn,
    has_event_at,
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

# Tool-argument deltas get the same treatment during backfill, one step
# further: a block that later got its ``tool.use`` row has its whole input
# there, so its deltas are dropped. Deltas with no later ``tool.use`` (a
# background subagent launch, an interrupted block) are the only record of
# that input and replay as one merged frame per run.
_TOOL_INPUT_DELTA = "tool.input_delta"

# Backfill frames reach the ASGI stack in chunks of about this size rather than
# one per frame: every chunk crosses each BaseHTTPMiddleware layer, and that
# per-frame cost dominated long replays.
_BACKFILL_CHUNK_BYTES = 64 * 1024

# Subagent text streams the same way: one subagent.progress row per text delta
# of the subagent, which the client only appends to the card's trailing text
# child. A run of consecutive chunk-kind rows for one task replays as one frame.
# Only CONSECUTIVE rows merge: a frame's seq is the client's resume cursor, so
# every row at or below it must already be in that frame or an earlier one.
_SUBAGENT_PROGRESS = "subagent.progress"
_PROGRESS_STEP_KINDS = ("tool_use", "tool_result")

# Server-side kill switches for the replay slimming above. On unless set to 0;
# read per call so a worker can be switched without a code change.
_SLIM_USAGE_ENV = "AI_SHUFFLE_PERF_SLIM_USAGE_REPLAY"
_COALESCE_PROGRESS_ENV = "AI_SHUFFLE_PERF_COALESCE_SUBAGENT_PROGRESS"


def _perf_switch_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("0", "false", "off")


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
    key = (row.get("event_type"), row.get("turn_id"), row.get("run_id"))
    if row.get("event_type") == _TOOL_INPUT_DELTA:
        return key + ((row.get("payload") or {}).get("tool_use_id"),)
    return key


def _delta_field(row: Dict[str, Any]) -> str:
    """The payload field a delta row streams."""
    return "partial_json" if row.get("event_type") == _TOOL_INPUT_DELTA else "text"


def _merged_row(run: Dict[str, Any]) -> Dict[str, Any]:
    """The run's single frame row: first envelope, joined text, LAST seq.

    The seq must be the last row's so the client's reconnect cursor (the SSE
    ``id:`` line) does not rewind into the middle of a merged run.
    """
    first = run["first"]
    row = dict(first)
    payload = dict(first.get("payload") or {})
    payload[run["field"]] = "".join(run["texts"])
    if run.get("name") and not payload.get("name"):
        # A tool block's name can arrive after its first fragment.
        payload["name"] = run["name"]
    row["payload"] = payload
    row["seq"] = run["last_seq"]
    return row


def _open_run(row: Dict[str, Any]) -> Dict[str, Any]:
    field = _delta_field(row)
    payload = row.get("payload") or {}
    run = {
        "key": _delta_key(row),
        "field": field,
        "first": row,
        "texts": [payload.get(field) or ""],
        "last_seq": row["seq"],
    }
    if row.get("event_type") == _TOOL_INPUT_DELTA:
        run["name"] = payload.get("name")
    return run


def _superseded_tool_input(
    row: Dict[str, Any], tool_uses: Optional[Dict[Tuple[Any, str], int]]
) -> bool:
    """True for a tool.input_delta whose full input a LATER tool.use repeats."""
    if not tool_uses:
        return False
    tool_use_id = (row.get("payload") or {}).get("tool_use_id")
    if not tool_use_id:
        return False
    return row["seq"] < tool_uses.get((row.get("turn_id"), str(tool_use_id)), 0)


def _coalesce_deltas(
    rows: Sequence[Dict[str, Any]],
    pending: List[Optional[Dict[str, Any]]],
    tool_uses: Optional[Dict[Tuple[Any, str], int]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield frame-ready rows, merging consecutive streaming deltas.

    ``pending`` is a one-slot list holding the run still open from an earlier
    page (``[None]`` to start), so a run split across a page boundary still
    merges; the caller drains what is left with :func:`_flush_pending` once
    paging ends. Any non-delta row closes the open run before it is emitted.

    ``tool_uses`` maps ``(turn_id, tool_use_id)`` to the seq of that block's
    ``tool.use`` row (``get_tool_use_seqs``). A tool.input_delta before it is
    dropped. It still closes the open run, so the rows on either side merge
    exactly as they did when the delta was sent.
    """
    for row in rows:
        run = pending[0]
        event_type = row.get("event_type")
        if event_type == _TOOL_INPUT_DELTA and _superseded_tool_input(row, tool_uses):
            if run is not None:
                yield _merged_row(run)
                pending[0] = None
            continue
        if event_type in _COALESCE_TYPES or event_type == _TOOL_INPUT_DELTA:
            if run is not None and run["key"] == _delta_key(row):
                payload = row.get("payload") or {}
                run["texts"].append(payload.get(run["field"]) or "")
                if "name" in run and not run["name"]:
                    run["name"] = payload.get("name")
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


def _progress_text(row: Dict[str, Any]) -> Optional[str]:
    """The text a chunk-kind subagent.progress row adds, or None for any other row.

    Mirrors the client reducer: ``note``, else ``payload.text``, else
    ``payload.content``, counted only when it is a string.
    """
    if row.get("event_type") != _SUBAGENT_PROGRESS:
        return None
    payload = row.get("payload") or {}
    if payload.get("kind") in _PROGRESS_STEP_KINDS:
        return None
    value = payload.get("note")
    if value is None:
        inner = payload.get("payload")
        inner = inner if isinstance(inner, dict) else {}
        value = inner.get("text")
        if value is None:
            value = inner.get("content")
    return value if isinstance(value, str) else ""


def _merged_progress_row(run: Dict[str, Any]) -> Dict[str, Any]:
    """One frame for a run of chunk rows: first texted envelope, joined text, LAST seq."""
    first = run["first"]
    if run["count"] == 1:
        return first
    row = dict(first)
    payload = dict(first.get("payload") or {})
    inner = payload.get("payload")
    inner = {k: v for k, v in inner.items() if k != "text"} if isinstance(inner, dict) else {}
    inner["content"] = "".join(run["texts"])
    payload["payload"] = inner
    payload["note"] = None
    row["payload"] = payload
    row["seq"] = run["last_seq"]
    return row


def _coalesce_progress(
    rows: Iterable[Dict[str, Any]],
    pending: List[Optional[Dict[str, Any]]],
) -> Iterator[Dict[str, Any]]:
    """Merge consecutive chunk-kind subagent.progress rows for the same task.

    ``pending`` is a one-slot list holding the run still open from an earlier
    page, drained with :func:`_flush_pending_progress` once paging ends. The
    merged row keeps the envelope of the run's first row that adds text (the
    client names a new text child after it) and the last row's seq.
    """
    for row in rows:
        run = pending[0]
        text = _progress_text(row)
        if text is None:
            if run is not None:
                pending[0] = None
                yield _merged_progress_row(run)
            yield row
            continue
        task_id = (row.get("payload") or {}).get("task_id")
        if run is not None and run["task_id"] == task_id:
            if text and not run["texted"]:
                run["first"], run["texted"] = row, True
            run["texts"].append(text)
            run["count"] += 1
            run["last_seq"] = row["seq"]
            continue
        if run is not None:
            yield _merged_progress_row(run)
        pending[0] = {
            "task_id": task_id,
            "first": row,
            "texted": bool(text),
            "texts": [text],
            "count": 1,
            "last_seq": row["seq"],
        }


def _flush_pending_progress(
    pending: List[Optional[Dict[str, Any]]],
) -> Iterator[Dict[str, Any]]:
    run = pending[0]
    if run is not None:
        pending[0] = None
        yield _merged_progress_row(run)


def _wire(row: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a DB row's payload with its envelope (envelope keys win)."""
    payload = row.get("payload") or {}
    if row.get("event_type") == "turn.usage" and _perf_switch_on(_SLIM_USAGE_ENV):
        # Old rows (and rows mirrored from older workers) carry the full
        # context snapshot twice; send one compact copy.
        payload = slim_turn_usage_payload(payload)
    if row.get("event_type") == "message.user" and isinstance(payload.get("content"), str):
        # Rows backfilled from a transcript can carry harness reminders.
        content = strip_harness_reminders(payload["content"])
        if content is not payload["content"]:
            payload = {**payload, "content": content}
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
    coalesce_progress = _perf_switch_on(_COALESCE_PROGRESS_ENV)
    progress_pending: List[Optional[Dict[str, Any]]] = [None]

    def _rows(rows: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
        return _coalesce_progress(rows, progress_pending) if coalesce_progress else rows

    # Looked up once, at the first tool.input_delta, from just before it: a
    # log without tool-argument deltas never pays for the lookup.
    tool_uses: Optional[Dict[Tuple[Any, str], int]] = None
    page_from = from_seq
    while True:
        page = await asyncio.to_thread(get_events, session_id, page_from, _PAGE)
        if tool_uses is None:
            first_delta = next(
                (r["seq"] for r in page if r.get("event_type") == _TOOL_INPUT_DELTA),
                None,
            )
            if first_delta is not None:
                try:
                    tool_uses = await asyncio.to_thread(
                        get_tool_use_seqs, session_id, first_delta - 1
                    )
                except Exception:  # noqa: BLE001 — replay every delta instead
                    logger.debug(
                        "tool.use lookup failed for %s", session_id, exc_info=True
                    )
                    tool_uses = {}
        for row in _rows(_coalesce_deltas(page, pending, tool_uses)):
            yield row["seq"], _frame(row)
        if page:
            page_from = page[-1]["seq"]
        if len(page) < _PAGE:
            break
    for row in _rows(_flush_pending(pending)):
        yield row["seq"], _frame(row)
    for row in _flush_pending_progress(progress_pending):
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


def _resume_check(
    session_id: str, from_seq: int, epoch: Optional[str]
) -> Tuple[str, Optional[str]]:
    """Return ``(current_epoch, reset_reason)`` for a connect.

    A resume (``from_seq > 0``) that names the epoch it was served from is
    reset when that log is gone (``"epoch"``: cleared or rewritten), or when
    the row at its cursor no longer exists (``"cursor"``: delta compaction
    merged the run the cursor sat inside, so resuming would repeat text).
    Without an epoch the caller is an older client and is served as before.
    """
    current = get_log_epoch(session_id)
    if from_seq > 0 and epoch:
        if epoch != current:
            return current, "epoch"
        if not has_event_at(session_id, from_seq):
            return current, "cursor"
    return current, None


async def event_stream(
    session_id: str, from_seq: int = 0, epoch: Optional[str] = None
):
    """Yield SSE frames for a session: backfill from ``from_seq``, then tail.

    Module-level (rather than a closure in the endpoint) so tests can consume
    the stream directly — ASGI test transports buffer the whole response and
    cannot read a never-ending SSE body incrementally.

    ``epoch`` is the log epoch the client's cursor came from (the last
    ``caught_up``). When it no longer matches, the stream opens with
    ``event: reset`` and continues as a fresh ``from_seq=0`` load.
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
        # 0. Log epoch. Read BEFORE the backfill, so a clear that lands while
        #    it runs leaves caught_up with the old epoch and the next resume
        #    is reset rather than trusted.
        log_epoch, reset_reason = await asyncio.to_thread(
            _resume_check, session_id, from_seq, epoch
        )
        if reset_reason:
            data = json.dumps({"reason": reset_reason, "epoch": log_epoch})
            yield f"event: reset\ndata: {data}\n\n"
            logger.info(
                "SSE feed reset: session=%s from_seq=%s reason=%s",
                session_id, from_seq, reset_reason,
            )
            from_seq = cursor = 0

        # Historical resumes may have no terminal task frame, even though the
        # durable registry says killed. Repair before replay/caught_up so reload
        # cannot resurrect them. Also covers reconnects with cached stale rows.
        from app.core.task_replay import reconcile_task_replay

        await reconcile_task_replay(session_id)

        # Replay stats for the fresh-load log line below.
        frames_sent = 0
        bytes_sent = 0
        elided = 0

        # 1. A fresh load starts after the latest SAFE compacted prefix.  A
        #    reconnect keeps its own cursor and must never apply fresh-load
        #    elision.  The synthetic marker has no SSE id, so reconnect state is
        #    still advanced only by real persisted events.
        if cursor == 0:
            first_kept, elided_events, tier = await asyncio.to_thread(
                _compacted_replay_start, session_id
            )
            if first_kept > 0:
                # Compaction rewrites the model's view, not its audit trail.
                # Replay durable cards in sequence before the synthetic prefix
                # marker; never move old rows to new seqs or duplicate them in
                # the retained tail. Reconnects still use their ordinary cursor.
                audit_rows = await asyncio.to_thread(
                    get_compaction_events_before, session_id, first_kept
                )
                for row in audit_rows:
                    frame = _frame(row)
                    yield frame
                    frames_sent += 1
                    bytes_sent += len(frame)
                frame = _boundary_frame(
                    session_id, first_kept, elided_events, tier
                )
                yield frame
                frames_sent += 1
                bytes_sent += len(frame)
                elided = elided_events
                cursor = first_kept - 1

        # Consecutive historical streaming deltas remain coalesced into one
        # frame per run (see ``_replay_from``).
        replayed = False
        async for seq, frame in _replay_from(session_id, cursor):
            yield frame
            cursor = seq
            replayed = True
            frames_sent += 1
            bytes_sent += len(frame)

        # 1a. Legacy-history seeding, made LAZY. ``ensure_backfilled``
        #     re-materializes a whole legacy transcript into ``session_events``;
        #     it is needed only when this feed is the render authority AND the
        #     log has nothing to serve. A populated log (the overwhelmingly
        #     common case) pays no probing read, and a reconnect
        #     (``from_seq > 0``) never re-seeds history the client already holds
        #     — seeding one there would replay a duplicate legacy prefix past a
        #     cursor the client cannot reconcile.
        if not replayed and cursor == 0 and from_seq == 0:
            await ensure_backfilled(session_id)
            async for seq, frame in _replay_from(session_id, cursor):
                yield frame
                cursor = seq
                frames_sent += 1
                bytes_sent += len(frame)

        # Seed the shared reader from this client's post-backfill cursor, so the
        # very next append is pushed rather than waited for.
        tail.note_cursor(cursor)

        if log_epoch == EMPTY_LOG_EPOCH and cursor > 0:
            # The log was empty when we looked and has rows now (a first turn,
            # or the legacy seed above). Every row sent belongs to the new log,
            # so name it; otherwise the next resume would be reset for nothing.
            log_epoch = await asyncio.to_thread(get_log_epoch, session_id)

        # 1b. Catch-up sentinel: mark the history/live boundary so the client can
        #     paint the whole backfilled transcript in ONE batch instead of
        #     repainting per backfilled frame. Carries no ``id:`` line, so it does
        #     not advance the reconnect cursor (``lastSeq``); it re-arms per connect
        #     (a near-empty reconnect backfill just folds once). ``epoch`` names
        #     the log the client's cursor now belongs to.
        if from_seq == 0:
            # Logged before the yield: a client that disconnects on caught_up
            # would otherwise skip it.
            logger.info(
                "SSE feed replay: session=%s frames=%d bytes=%d elided=%d "
                "caught_up_ms=%.0f",
                session_id, frames_sent, bytes_sent, elided,
                (time.monotonic() - start_time) * 1000,
            )
        caught_up = json.dumps({"seq": cursor, "epoch": log_epoch})
        yield f"event: caught_up\ndata: {caught_up}\n\n"

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


async def _batch_backfill(frames: AsyncGenerator[str, None]) -> AsyncIterator[str]:
    """Join backfill frames into large chunks; pass the live tail through.

    Frames are buffered until the ``caught_up`` sentinel (sent with them) or
    about ``_BACKFILL_CHUNK_BYTES``, whichever comes first. From caught_up on,
    each chunk goes out as-is so live typing still animates frame by frame.
    A ``reset`` goes out on its own at once: the client drops the connection
    and restarts the moment it sees one, so the fresh load behind it would
    only delay that.
    """
    buf: List[str] = []
    size = 0
    live = False
    try:
        async for chunk in frames:
            if live:
                yield chunk
                continue
            buf.append(chunk)
            size += len(chunk)
            live = chunk.startswith("event: caught_up")
            if live or size >= _BACKFILL_CHUNK_BYTES or chunk.startswith("event: reset"):
                yield "".join(buf)
                buf = []
                size = 0
        # The inner stream only ends when the client has gone, so anything
        # still buffered has nobody to go to.
    finally:
        await frames.aclose()


@router.get("/{session_id}/events")
async def stream_session_events(
    request: Request,
    session_id: str,
    from_seq: int = 0,
    epoch: Optional[str] = None,
    token: str = Depends(require_auth),
):
    """Stream the session event log as SSE, backfilling from ``from_seq``.

    ``epoch`` is the log epoch from the client's last ``caught_up``; a resume
    whose epoch is stale gets ``event: reset`` and a full reload.
    """
    await require_transcript_session(request, session_id)
    from app.network import transcripts
    # Opened before its transcript arrived on a worker that just joined a
    # network: copy it next. Its events stream in as they are applied.
    await asyncio.to_thread(transcripts.prioritise, session_id)
    return StreamingResponse(
        _batch_backfill(event_stream(session_id, from_seq, epoch)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

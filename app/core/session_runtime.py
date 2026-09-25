"""The one spawn path.

A :class:`SessionRuntime` executes exactly one ``turns`` row end-to-end:

1. claim the fenced run lease (MANDATORY — no claim means the turn stays queued);
2. take the session's single live slot via ``turns.start_turn`` (loses cleanly);
3. reset per-turn task bookkeeping, acquire the per-session **reader token**, and
   stop any running idle reader (so this pump is the session's single reader);
4. build a :class:`~app.core.turn_recorder.TurnRecorder` bound to the INJECTED
   ``(run_id, fence_token, turn_id)`` (``create_for_run`` — no self-claim);
5. run the pump by WRAPPING ``query_engine._stream_to_websocket_impl`` — its
   internals are untouched; the recorder observes every yielded event (SSE is the
   delivery path) and a broadcast adapter carries the WS frames;
6. **finalize** (always, in a ``finally``): decide ``completed | aborted |
   parked`` — the park gate is ``task_registry.get_active_background_work`` OR
   ``get_background_launches`` — settle/park the turn and release the reader
   token;
7. release the run lease in the outer ``finally`` and fire the injectable
   ``on_turn_settled`` scheduler seam (``turn_scheduler`` registers it) — but
   ONLY when finalize actually performed the DB transition (a racer that
   settled first already fired it). Special case: after an escalation-keep
   (``interrupt()`` flipped the turn to ``draining`` and cancelled the wedged
   pump), ``execute()`` runs the DRAIN reader itself, under its still-live
   lease, and the reader's ``_settle_drained`` settles + fires instead.

The single-reader invariant is STRUCTURAL: a per-session ``asyncio.Lock``
(the *reader token*) is acquired by both this pump and the idle reader
(:mod:`app.core.idle_reader`), so they can never read the pooled client
concurrently. There is deliberately NO ``deferred_delivery`` concept.

All turn sources use this runtime through the turn manager and scheduler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from app.core.config import settings
from app.core.permission_handler import permission_handler
from app.core.turn_recorder import (
    TurnRecorder,
    record_interrupting,
    record_parked,
)
from app.db import runs as runs_db
from app.db import turns as turns_db

logger = logging.getLogger(__name__)

# Lease timing — identical to the TurnRecorder self-claim.
RUN_LEASE_TTL_SECONDS = 60
RUN_LEASE_RENEW_SECONDS = 20


def _notify_run_change(session_id: str, *, force: bool = False) -> None:
    """Run-lifecycle hook for the board spinner (streaming_status re-check).

    The v2 claim/release boundary is INVISIBLE to streaming_status's own
    triggers (the event gateway only ticks on event appends, and the terminal
    append lands before release), so without an explicit notify the spinner-off
    waits for the 15s sweep and a back-to-back turn's ON frame is suppressed as
    "unchanged". Lazy import + never raises: a status bug must never break the
    turn.
    """
    try:
        from app.core import streaming_status

        streaming_status.notify(session_id, force=force)
    except Exception:
        logger.debug(
            "SessionRuntime: streaming_status notify failed for %s",
            session_id, exc_info=True,
        )

# How long a runtime-driven interrupt waits for the turn to settle before it
# escalates (mirrors chat_ws_handler._INTERRUPT_DRAIN_TIMEOUT_SECONDS). Tests
# shrink it.
ESCALATION_TIMEOUT_SECONDS = 8.0
_SETTLE_POLL_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Reader token — per-session single-reader slot (Lock-guarded).
# ---------------------------------------------------------------------------
# The turn pump AND the idle reader both acquire the SAME per-session lock, so
# the single-reader invariant (only one coroutine reads the pooled adapter at
# a time) is structural rather than enforced by call discipline.
_reader_locks: Dict[str, asyncio.Lock] = {}
# Running idle-reader task per session, so a starting turn can stop it first.
_idle_readers: Dict[str, asyncio.Task] = {}

# Live SessionRuntime per session — the command→runtime interrupt seam. A
# runtime registers itself here the instant it WINS ``start_turn`` (so it owns
# the adapter interrupt + evict/drain escalation), and self-guarded-pops in the
# outer finally. ``turn_manager.interrupt`` flips the durable row first, then a
# boot-wired executor routes to ``get_live_runtime(sid).interrupt(mode)``. No
# live runtime (crash/other install) → the durable ``interrupting`` row stands
# and ``reap_expired`` is the backstop. Only mutated on the event loop.
_live_runtimes: Dict[str, "SessionRuntime"] = {}


def get_live_runtime(session_id: str) -> Optional["SessionRuntime"]:
    """Return the live :class:`SessionRuntime` executing ``session_id``'s turn,
    or ``None`` when no runtime in THIS process owns the session (already
    settled, never started here, or owned by another install)."""
    return _live_runtimes.get(session_id)


def live_turn_ids() -> Set[str]:
    """Turn ids this process is executing right now.

    The reap sweep reads only the DB, and a lease can lapse under a turn that
    is still running here (a stalled loop, a DB outage longer than the TTL).
    Requeueing such a turn would let the next claim pass start it a second
    time while the first run is still going, so the sweep skips these.
    Other processes still reap it, and fencing stops this runner's writes the
    moment one of them claims the session."""
    return {runtime.turn_id for runtime in _live_runtimes.values()}


# Queue sources a steer may pull from — the two that render as the user's own
# pending messages. Mirrors ``chat_ws_handler._V2_QUEUE_SOURCES``; an internal
# turn (unpark, failover, automation) is never someone talking mid-turn.
_STEERABLE_SOURCES = ("user", "queue")

# Override keys that BIND a turn's run and cannot change once it started. A
# queued message asking for a different one of these is a request the live turn
# cannot honor, so it waits and runs as its own turn instead of being folded in
# under settings the user did not pick. ``fast`` and ``context_window`` are
# deliberately NOT in this set: they shape throughput and
# budget rather than which agent answers.
_STEER_BINDING_KEYS = ("model", "effort", "permission_mode", "workspace_project_ids")


async def try_steer_pending(session_id: str) -> int:
    """Try to hand this session's queued user messages to its RUNNING turn.

    A no-op unless THIS process owns the session's live turn: a session running
    on another install falls back to the ordinary drain (cross-install steering
    is not a thing this build does), and so does one with nothing live.

    Returns the number of messages actually handed over. Never raises — a steer
    is an optimization over the queue, and the queue is already durable.
    """
    runtime = _live_runtimes.get(session_id)
    if runtime is None:
        return 0
    try:
        return await runtime.steer_pending()
    except Exception:  # noqa: BLE001 — the drain is the correctness path.
        logger.warning(
            "SessionRuntime: steer attempt failed for %s", session_id,
            exc_info=True,
        )
        return 0


async def try_steer_task_notification(
    session_id: str,
    prompt: str,
    *,
    notification_id: str,
    frames: Optional[List[Dict[str, Any]]] = None,
    task_ids: Optional[List[str]] = None,
    held_peer_messages: str = "",
) -> bool:
    """Try to hand a terminal ``<task-notification>`` to this session's RUNNING
    turn, so a background completion reaches the model at the next cell boundary
    instead of waiting out the whole turn.

    The same channel :func:`try_steer_pending` uses for a mid-turn user message,
    with one difference that shapes everything: a user's steer has a durable
    queue row behind it, so an undelivered one simply runs as its own turn. A
    notification has no row, so this call also carries ``frames``/``task_ids`` —
    the runtime hands them back to the scheduler when the receipt arrives (latch
    ``terminal_notified``) or when the turn ends without one (enqueue the
    ordinary at-rest wake after all).

    True only when the engine ACCEPTED the submission. False — never an
    exception — when there is nothing to steer into: no runtime for this session
    in this process (the at-rest case, and a session running on another install),
    no adapter, or a turn already being interrupted. The caller enqueues the
    ordinary wake on False.
    """
    runtime = _live_runtimes.get(session_id)
    if runtime is None:
        return False
    try:
        return await runtime.steer_task_notification(
            prompt,
            notification_id=notification_id,
            frames=list(frames or []),
            task_ids=list(task_ids or []),
            **({"held_peer_messages": held_peer_messages} if held_peer_messages else {}),
        )
    except Exception:  # noqa: BLE001 — the queued wake is the correctness path.
        logger.warning(
            "SessionRuntime: task-notification steer failed for %s", session_id,
            exc_info=True,
        )
        return False


async def try_cancel_steer(session_id: str, turn_id: str) -> str:
    """Adjudicate an attempt to un-send queued turn ``turn_id``.

    Returns one of:

    * ``"not_submitted"`` — the row was never handed to a live engine (no
      runtime here, or this turn was never steered), so removing it is a
      plain queue operation;
    * ``"cancelled"`` — it was submitted as a steer and has now been recalled;
      it will never reach the model, so it is safe to remove;
    * ``"delivered"`` — the model already has the text. The row is no longer
      the queue's to remove: the delivery receipt absorbs it into the
      transcript.

    Never raises — an adjudication failure falls back to ``"not_submitted"``,
    which simply removes the row.
    """
    runtime = _live_runtimes.get(session_id)
    if runtime is None:
        return "not_submitted"
    try:
        return await runtime.cancel_steer(turn_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "SessionRuntime: steer cancel adjudication failed for %s turn %s",
            session_id, turn_id, exc_info=True,
        )
        return "not_submitted"


def get_reader_lock(session_id: str) -> asyncio.Lock:
    """Return (creating on first use) the session's reader-token lock.

    ``asyncio.Lock`` does not bind to a loop at construction (3.10+), so lazy
    creation from any context is safe.
    """
    lock = _reader_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _reader_locks[session_id] = lock
    return lock


def start_idle_reader(session_id: str, *, drain_turn_id: str) -> None:
    """Start the drain reader that settles ``drain_turn_id`` (a wedged runner
    escalated to ``draining``) ``aborted`` once its client is safe to evict.

    Does nothing while one is already running. The reader acquires the reader
    token itself, so it can never race a turn pump.
    """
    from app.core import idle_reader

    existing = _idle_readers.get(session_id)
    if existing is not None and not existing.done():
        logger.debug(
            "SessionRuntime: idle reader already running for %s — not starting a second",
            session_id,
        )
        return
    task = asyncio.create_task(
        idle_reader.run_idle_reader(session_id, drain_turn_id=drain_turn_id)
    )
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    _idle_readers[session_id] = task
    logger.debug("SessionRuntime: idle reader started for %s", session_id)


async def stop_idle_reader(session_id: str) -> None:
    """Cancel and await the session's idle reader, if running. Never raises."""
    task = _idle_readers.pop(session_id, None)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def _drop_idle_reader_self(session_id: str) -> None:
    """Remove the CURRENT idle-reader task from the registry iff it is still us."""
    current = asyncio.current_task()
    if _idle_readers.get(session_id) is current:
        _idle_readers.pop(session_id, None)


# ---------------------------------------------------------------------------
# Scheduler seam — on_turn_settled (the real handler is turn_scheduler's).
# ---------------------------------------------------------------------------
_on_turn_settled: Optional[Callable[..., Any]] = None


def register_scheduler(hook: Optional[Callable[[str, str, str], Any]]) -> None:
    """Register (or clear, with ``None``) the settlement callback.

    Called with ``(session_id, turn_id, outcome)`` after every settlement
    (completed/aborted/parked). ``turn_scheduler.install`` registers the real
    one; tests inject a spy. Default: no-op.
    """
    global _on_turn_settled
    _on_turn_settled = hook


async def _fire_on_turn_settled(session_id: str, turn_id: str, outcome: str) -> None:
    """Fire the registered settlement callback, if any. Never raises."""
    if outcome in ("completed", "aborted"):
        from app.core.lifecycle_events import enqueue
        enqueue(outcome, session_id, turn_id)
    hook = _on_turn_settled
    if hook is None:
        return
    try:
        result = hook(session_id, turn_id, outcome)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001 — settlement must never break turn teardown.
        logger.warning(
            "SessionRuntime: on_turn_settled hook failed for %s turn %s",
            session_id, turn_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Broadcast adapter seam — the non-render WS frames.
# ---------------------------------------------------------------------------
# Render frames ride the recorder → session event log → SSE. The remaining
# lifecycle/permission WS frames go through this adapter: to the origin socket
# when the turn's ``source`` is ``user`` and the socket is alive, plus a fan-out
# to the other registered devices via sync_engine. Injectable so a transport
# can override the exact wire mechanics.
_broadcaster: Optional[Callable[..., Awaitable[None]]] = None


def register_broadcaster(hook: Optional[Callable[..., Awaitable[None]]]) -> None:
    """Register (or clear) the broadcast adapter used for WS frames."""
    global _broadcaster
    _broadcaster = hook


async def _default_broadcast(
    session_id: str,
    event: Dict[str, Any],
    *,
    source: str,
    origin_send: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]],
    origin_device_id: Optional[str],
) -> None:
    """Default broadcast adapter. Never raises (a delivery bug must never break
    the pump — the recorder keeps appending regardless)."""
    # Origin-socket passthrough — ONLY for a user-initiated turn with a live
    # socket. Server-originated turns (queue/unpark/failover/automation/...) have
    # no owning socket to pass through to.
    if source == "user" and origin_send is not None:
        try:
            await origin_send(event)
        except Exception:  # noqa: BLE001
            logger.debug("SessionRuntime: origin_send failed for %s", session_id, exc_info=True)
    # Fan the frame out to the other devices watching the session.
    try:
        from app.core.sync_engine import SyncEvent, sync_engine

        await sync_engine.broadcast_event(
            SyncEvent(
                event_type="stream_frame",
                session_id=session_id,
                data=event,
                source_device_id=origin_device_id,
            ),
            exclude_device_id=origin_device_id,
        )
    except Exception:  # noqa: BLE001
        logger.debug("SessionRuntime: device fan-out failed for %s", session_id, exc_info=True)


# ---------------------------------------------------------------------------
# Render-frame trim.
# ---------------------------------------------------------------------------
# Raw RENDER frames ride the recorder → session event feed (SSE); only the
# frames the frontend consumes for NON-render purposes may hit the WS/broadcast
# path, or render content is double-delivered.
_WS_TRIMMED_RENDER_FRAMES = frozenset({
    "chunk",
    "stream_delta",
    "stream_block_start",
    "stream_block_stop",
    "stream_message_delta",
    "task_progress",
    "system",
    "usage_update",
    # The complete assistant text block and the thinking marker. The pump
    # yields these AFTER the deltas that already carried the same content, and
    # they are pure render payload: the frontend WS dispatcher has no case for
    # either (they fall through to `default: break`), so every byte would be
    # serialized, fanned out to every watching device, parsed and discarded.
    # The content still reaches every client on the SSE feed as coalesced
    # deltas — this only stops the duplicate on the WS leg.
    "text",
    "thinking_block",
    # Not a render frame, but no WS consumer either: provider liveness for the
    # Task worker watchdog, which reads it from the event log.
    "provider_alive",
})

# Cap on the serialized size of a tool frame's ``input`` on the WS leg.
# The frontend's `tool_use` / `subagent_tool_use` handlers read ONLY
# `session_id` and `id` (they just maintain the pendingTools set); the rendered
# arguments come from the SSE feed. A multi-MB Write/Edit payload would
# otherwise be fanned out to every device for nothing. The frame itself must still be
# delivered (it is correctness-bearing for the pendingTools set), so the frame
# is kept and only its oversized input is replaced by a marker.
_WS_MAX_TOOL_INPUT_CHARS = 4096
_WS_TOOL_INPUT_FRAMES = frozenset({"tool_use", "subagent_tool_use"})

_WS_KEPT_SUBAGENT_FRAMES = frozenset({"subagent_tool_use", "subagent_tool_result"})

# Frames that only the MODEL can produce. A steer is folded into a tool result,
# so one of these arriving after a delivery receipt means the model has been
# round-tripped WITH the interjection in its context — it read it. Tool results
# are deliberately absent: they are the carrier, not the model's reply to it.
_MODEL_OUTPUT_FRAMES = frozenset({
    "chunk",
    "text",
    "stream_delta",
    "stream_block_start",
    "thinking_block",
    "tool_use",
})

# Bounded retry for the absorb that retires a delivered steer's queue row.
_ABSORB_ATTEMPTS = 3
_ABSORB_BACKOFF = 0.2

# Prepended to the prompt of a turn whose text fluid already delivered as a
# mid-turn interjection but whose queue row could not be retired. The re-run is
# unavoidable at that point; saying so beats letting the model wonder why it is
# being asked the same thing twice.
_REDELIVERY_NOTE = (
    "[note: this message was already delivered to you mid-turn, folded into a "
    "tool result, but that delivery could not be confirmed — so it is being sent "
    "again. If you have already acted on it, say so briefly instead of redoing "
    "the work.]"
)


def _cap_ws_tool_input(event: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``event`` with an oversized tool ``input`` replaced by a marker.

    Returns the SAME object when nothing needs capping; otherwise a shallow
    COPY, because the recorder has already been handed this dict and persists
    it — the WS leg must never mutate the recorded frame.
    """
    if event.get("type") not in _WS_TOOL_INPUT_FRAMES:
        return event
    raw = event.get("input")
    if raw is None:
        return event
    try:
        encoded = json.dumps(raw, default=str)
    except (TypeError, ValueError):
        encoded = repr(raw)
    if len(encoded) <= _WS_MAX_TOOL_INPUT_CHARS:
        return event
    capped = dict(event)
    capped["input"] = {
        "_ws_truncated": True,
        "_original_chars": len(encoded),
        "_preview": encoded[:_WS_MAX_TOOL_INPUT_CHARS],
    }
    return capped


def _is_trimmed_render_frame(event_type: Optional[str]) -> bool:
    """True if this frame type is render-only and must not hit the chat WS."""
    if not event_type:
        return False
    if event_type in _WS_TRIMMED_RENDER_FRAMES:
        return True
    return (
        event_type.startswith("subagent_")
        and event_type not in _WS_KEPT_SUBAGENT_FRAMES
    )


# ---------------------------------------------------------------------------
# _should_evict_after_escalation — ported from chat_ws_handler (:195-232).
# ---------------------------------------------------------------------------
# Legacy stays intact until PR-B; this is a faithful copy so the runtime keeps
# the exact eviction decision when an interrupt escalates.
def _should_evict_after_escalation(session_id: str) -> bool:
    """Decide whether an escalated interrupt should ALSO evict the pooled client.

    Never trade live background work for eviction of a healthy client: keep the
    client when background work is live AND its subprocess is alive; evict
    otherwise (no children to protect, gone from the pool, or dead subprocess).
    """
    from app.core.session_pool import get_session_pool, is_subprocess_alive
    from app.core.task_registry import task_registry

    if not task_registry.has_active_background_work(session_id):
        return True
    state = get_session_pool().peek(session_id)
    if state is None:
        return True
    return not is_subprocess_alive(state)


async def _evict_pooled_client(session_id: str) -> None:
    """Best-effort eviction of a wedged pooled client. Never raises."""
    try:
        from app.core.session_pool import get_session_pool, is_subprocess_alive

        pool = get_session_pool()
        state = pool.peek(session_id)
        if state is None:
            return
        if is_subprocess_alive(state):
            await pool.remove_broken(session_id)
        else:
            await pool.remove_dead(session_id)
    except Exception:  # noqa: BLE001
        logger.debug("SessionRuntime: evict failed for %s", session_id, exc_info=True)


async def _broadcast_queue_after_start(session_id: str) -> None:
    """Re-source + fan out the queue view after a turn leaves the queued
    set, so Send-later chips clear the moment the drained turn starts
    (the SSE self-heal remains the fallback). Best-effort."""
    try:
        from app.api.chat_ws_handler import _v2_queue_view  # lazy: no core→api at module load
        from app.core.sync_engine import sync_engine
        queue = await asyncio.to_thread(_v2_queue_view, session_id)
        await sync_engine.broadcast_queue_updated(session_id, queue)
    except Exception:  # noqa: BLE001
        logger.warning(
            "SessionRuntime: queue_updated broadcast failed for %s",
            session_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Durable surface for a turn that died on a provider error
# ---------------------------------------------------------------------------

def _awaiting_user_input(session_id: str) -> bool:
    """True while this session blocks on a durable interaction round-trip.

    An ``AskUser`` question, a permission prompt or a plan gate makes a live
    turn legitimately silent for as long as the person takes. Both the progress
    watchdog and the pool's idle policy have to know the difference between
    that and a wedged turn. Never raises: unknown resolves to "not waiting".
    """
    try:
        from app.core import interaction_wait

        return interaction_wait.has_waiters(session_id)
    except Exception:  # noqa: BLE001
        return False


#: What the durable row says when the provider gave us no message at all.
_GENERIC_TURN_ERROR = "The turn ended with a provider error before finishing."

#: Extra line on a wake turn — the user did not type anything, so without it
#: the row reads as an error against a prompt they never sent.
_WAKE_ERROR_NOTE = (
    " This turn was started automatically to deliver a finished background "
    "task's result."
)


async def persist_turn_error_message(
    session_id: str,
    turn_id: str,
    message: Optional[str],
    *,
    source: str = "user",
) -> None:
    """Write a durable ``system`` row for a turn that ended on an error frame.

    The gap: an ``error`` terminal reaches the live WebSocket and the ``turns``
    table, but never ``session_messages``. Reload the session and it shows the
    user's prompt followed by nothing — no banner, no explanation, no retry
    affordance. Background-worker failures already write a system row
    (``chat_ws_handler``'s terminal surface); this makes turn failures match.

    Never raises: a message that cannot be written must not change how the turn
    settles.
    """
    text = (message or "").strip() or _GENERIC_TURN_ERROR
    if source in ("unpark", "unpark_pump"):
        text += _WAKE_ERROR_NOTE
    try:
        from app.db import sessions as sessions_db

        await asyncio.to_thread(
            sessions_db.add_session_message,
            session_id=session_id,
            role="system",
            content=text,
            metadata={
                "subtype": "error",
                "system_data": {
                    "message": text,
                    "turn_id": turn_id,
                    "turn_source": source,
                },
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "SessionRuntime: could not persist the error message for %s turn %s",
            session_id, turn_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# SessionRuntime
# ---------------------------------------------------------------------------

class SessionRuntime:
    """Executes one ``turns`` row end-to-end (see module docstring)."""

    def __init__(
        self,
        turn_row: Dict[str, Any],
        *,
        origin_send: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None,
        origin_device_id: Optional[str] = None,
        api_user_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        self.turn = turn_row
        self.session_id: str = turn_row["session_id"]
        self.turn_id: str = turn_row["id"]
        self.source: str = turn_row.get("source") or "user"
        self.origin_send = origin_send
        self.origin_device_id = origin_device_id
        # api_user_id / user_id are transport context; the turn row may carry
        # them in payload (server-originated turns) — fall back to that.
        payload = turn_row.get("payload") or {}
        self.api_user_id = api_user_id if api_user_id is not None else payload.get("api_user_id")
        self.user_id = user_id if user_id is not None else payload.get("user_id")

        # Last terminal pump event seen ('done'|'interrupted'|'error'|None) —
        # drives the completed/aborted finalize decision. None ⇒ crash path.
        self._terminal: Optional[str] = None
        self._pending_done: Optional[Dict[str, Any]] = None
        # The 'error' terminal's message text, so an aborted settle records WHY
        # (otherwise turns.error stays NULL — stop_reason='error' with nothing
        # behind it, and the provider's own error body only in the event log).
        self._terminal_error: Optional[str] = None
        # In-flight pump task — interrupt() escalation cancels it: the pump
        # holds the reader token, so nothing else can read until it is gone.
        self._pump_task: Optional[asyncio.Task] = None
        # Set by interrupt()'s escalation-keep branch: execute() must run the
        # drain reader (under its still-live lease) instead of finalizing.
        self._drain_after_cancel = False
        # stop_reason override for an escalation-forced abort.
        self._abort_reason: Optional[str] = None
        # The run this turn was started under, and whether this runner stopped
        # owning it mid-turn (lease lost or fence superseded, ``_stop_unowned``).
        self._run_id: Optional[str] = None
        self._unowned = False
        self._recorder_closing = False
        # Queued turn ids already handed to the engine as steers THIS turn.
        # ``try_steer_pending`` fires once per send, so without this a message
        # still awaiting its receipt would be submitted again by the next send.
        # Per-turn by construction (one runtime per turn row); cleared in
        # ``_finalize`` so an undelivered steer is free to be tried again by
        # whatever turn comes next.
        self._steer_attempted: set = set()
        # Queued rows this turn actually SWALLOWED: row id → (perf_counter absorb
        # time, the row as it was). Populated by ``_on_steer_delivered`` on the
        # transition that retires the row, read once by ``_finalize`` to decide
        # whether an absorbed message died unanswered inside an interrupt.
        # Per-turn by construction, like ``_steer_attempted``.
        self._steer_absorbed: Dict[str, tuple] = {}
        # Terminal task notifications steered into THIS turn and still awaiting
        # their delivery receipt: notification id → {"frames", "task_ids"}.
        # Unlike a user steer there is no queue row to fall back on, so an entry
        # still sitting here at ``_finalize`` is handed back to the scheduler,
        # which enqueues the ordinary at-rest wake for it. Per-turn by
        # construction, like the two sets above.
        self._task_steers: Dict[str, Dict[str, Any]] = {}
        # When this turn last produced MODEL output (text, thinking, a tool call)
        # — same clock as the absorb stamps above. A steer lands inside a tool
        # result, so any model output after its absorb stamp is proof the model
        # read the interjection and acted on it. ``_requeue_stranded_steers`` uses
        # that to stop replaying a message the model already answered.
        self._model_output_at: Optional[float] = None
        # When THIS runtime was told to kill the turn — ``interrupt()``'s first
        # instruction, on the same high-resolution clock as the absorb stamps above.
        # ``None`` means no interrupt was ever driven here (a plain error/crash
        # abort), in which case nothing gets re-queued.
        self._interrupt_requested_at: Optional[float] = None
        # Idempotency guard for the interrupt-observe pass:
        # once interrupt() has been driven for THIS turn, later observers
        # (claim-loop pass, duplicate executor fire) are no-ops. Per-turn by
        # construction — a SessionRuntime instance executes exactly one turn
        # row, and the next turn gets a fresh instance with a fresh guard.
        self._interrupt_driving = False
        # One-shot guard for the start-window steer sweep. A turn is invisible
        # to ``turns_db.get_live_turn`` from the moment it is enqueued until
        # ``start_turn`` lands, and its adapter is not steerable for a while
        # after that. A message sent inside that window therefore reads the
        # session as idle, skips ``try_steer_pending`` entirely, and runs as a
        # turn of its own, so two near-identical messages seconds apart would
        # become two turns. The sweep below re-offers those rows to this
        # turn the moment it is provably live. See ``_sweep_start_window_steers``.
        self._start_window_swept = False
        # The in-flight sweep, kept only so it has a strong reference (a bare
        # create_task can be garbage-collected mid-await) and so the turn can
        # wait for it at finalize rather than leaving it orphaned.
        self._start_window_sweep_task: Optional[asyncio.Task] = None
        # Progress watchdog: when this turn last saw ANY pump event, and the
        # task that watches that stamp. See _progress_watchdog_loop.
        self._last_progress_at: float = time.perf_counter()
        self._progress_watchdog: Optional[asyncio.Task] = None
        self._stop_generation: Optional[int] = None

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    async def execute(self) -> Optional[str]:
        """Run this turn. Returns the settlement outcome
        (``completed|aborted|parked``), or ``None`` if the turn could not become
        live (no run claim / lost the start race) and stays queued.
        """
        sid = self.session_id
        tid = self.turn_id
        runner_id = settings.effective_instance_id
        from app.core.task_registry import task_registry
        self._stop_generation = task_registry.stop_generation(sid)
        if task_registry.is_user_stopped(sid):
            await asyncio.to_thread(turns_db.remove_queued_turn, tid)
            return None

        # 0. Placement gate. A session explicitly placed on ANOTHER
        #    runner must not be executed here even if this install's kick fired
        #    first — leave the turn queued for that runner's claim loop.
        #    FAIL-OPEN: a resolver error must never block local execution (the
        #    run lease below still guarantees single-runner exclusivity).
        #    ``kind == "none"`` (pinned owner offline / no capable runner) also
        #    leaves the turn queued — degraded state, resolved when the owner
        #    returns or the reap sweep routes it.
        session_row: Optional[Dict[str, Any]] = None
        decision = None
        try:
            from app.core import placement as placement_mod
            from app.db import database as database_mod

            session_row = await asyncio.to_thread(database_mod.get_session, sid)
            if session_row is not None:
                decision = placement_mod.resolve_placement(session_row)
        except Exception:
            logger.warning(
                "SessionRuntime: placement resolution failed for %s — "
                "failing open to local execution", sid, exc_info=True,
            )
        if decision is not None and decision.kind == "runner" and decision.runner_id != runner_id:
            logger.info(
                "SessionRuntime: session %s is placed on %s (%s) — turn %s stays "
                "queued for its claim loop", sid, decision.runner_id, decision.reason, tid,
            )
            return None
        if decision is not None and decision.kind == "none":
            logger.info(
                "SessionRuntime: no capable runner for session %s (%s) — turn %s "
                "stays queued", sid, decision.reason, tid,
            )
            return None

        if task_registry.stop_invalidated(sid, self._stop_generation):
            await asyncio.to_thread(turns_db.remove_queued_turn, tid)
            return None

        # 0b. Another turn already holds the live slot: stay queued without
        #     claiming. A slot held under a dead lease still makes the session
        #     claimable (no active run), so without this read every claim pass
        #     — up to one a second while any session streams — claimed a fresh
        #     run, tripped idx_turns_one_live_per_session in start_turn and
        #     released it again until the reap sweep freed the slot (353
        #     duplicate-key errors in 90 minutes on a desktop install). The
        #     holder's settlement or the reap sweep kicks this session again.
        #     The unique index stays the race guard for the window after this.
        try:
            live = await asyncio.to_thread(turns_db.get_live_turn, sid)
        except Exception:
            logger.warning("SessionRuntime: live-turn read failed for %s", sid, exc_info=True)
            live = None
        if live is not None and live["id"] != tid:
            logger.debug(
                "SessionRuntime: %s already has live turn %s (%s) — turn %s stays queued",
                sid, live["id"], live["state"], tid,
            )
            return None

        # 1. Claim the run lease — MANDATORY. No claim ⇒ another runner owns the
        #    session; leave the turn queued for a later kick/reap.
        try:
            run = await asyncio.to_thread(
                runs_db.claim_run, sid, runner_id, RUN_LEASE_TTL_SECONDS
            )
        except Exception:
            logger.warning("SessionRuntime: claim_run raised for %s", sid, exc_info=True)
            return None
        if run is None:
            logger.info(
                "SessionRuntime: could not claim run for %s (turn %s stays queued)",
                sid, tid,
            )
            return None
        run_id = run["id"]
        fence_token = run["fence_token"]
        self._run_id = run_id
        # An online handoff may have committed after the placement read but
        # before this claim. Never execute a stale-owner turn after its lease
        # was released. A read error here is fail-closed, unlike the UI hint.
        try:
            from app.network import engine as network_engine
            from app.network.control import owner_of
            from app.db import database as current_database
            moved = False
            if network_engine.context() is not None:
                current = await asyncio.to_thread(current_database.get_session, sid)
                moved = current is None or owner_of(current) is not None
        except Exception:
            moved = True
        if moved:
            await asyncio.to_thread(runs_db.release_run, run_id, fence_token)
            return None
        # Run went active — force the board-spinner ON frame NOW. The first
        # event append would eventually tick the gateway recheck, but by then
        # _last_broadcast may still hold the PREVIOUS turn's stale busy entry,
        # which suppresses the ON frame the frontend already consumed via its
        # done-frame cleanup.
        _notify_run_change(sid, force=True)

        renew_task = asyncio.create_task(self._renew_loop(run_id, runner_id, fence_token))
        renew_task.add_done_callback(lambda t: t.cancelled() or t.exception())

        outcome: Optional[str] = None
        settled_here = False
        try:
            if task_registry.stop_invalidated(sid, self._stop_generation):
                await asyncio.to_thread(turns_db.remove_queued_turn, tid)
                return None
            # 2. Take the session's single live slot.
            started = await asyncio.to_thread(
                turns_db.start_turn, tid, run_id, runner_id
            )
            if started is None:
                logger.info(
                    "SessionRuntime: lost the start race for %s turn %s — releasing run",
                    sid, tid,
                )
                return None

            if task_registry.stop_invalidated(sid, self._stop_generation):
                # Stop may have cleared the queue while start_turn was in flight.
                # Do not re-arm input or connect a provider for this obsolete turn.
                await asyncio.to_thread(turns_db.settle_turn, tid, "aborted", "interrupted")
                return "aborted"

            # Register as the session's live runtime the instant we own it, so
            # the command→runtime interrupt seam (turn_manager.interrupt →
            # boot-wired executor → get_live_runtime(sid).interrupt(mode)) can
            # reach us. Self-guarded pop in the outer finally.
            _live_runtimes[sid] = self
            from app.core.lifecycle_events import enqueue
            enqueue("started", sid, tid)

            # A fresh turn re-arms computer-use for the session (clears the
            # halt its previous Stop set) so the new agent can act again.
            try:
                from app.mcp.computer_use import abort as cu_abort

                cu_abort.resume(sid)
            except Exception:
                logger.debug(
                    "SessionRuntime: computer-use re-arm failed for %s", sid,
                    exc_info=True,
                )

            logger.info(
                "SessionRuntime: started %s turn %s (source=%s)",
                sid, tid, self.source,
            )
            # The row has just left the queued set — fan the queue view out now
            # so Send-later chips clear on every watching device. Fire-and-
            # forget: nothing may add latency in front of the pump.
            broadcast_task = asyncio.create_task(_broadcast_queue_after_start(sid))
            broadcast_task.add_done_callback(lambda t: t.cancelled() or t.exception())

            # 3. Sticky pin: the FIRST winner of an
            #    "anywhere"/capability placement pins the session to itself
            #    (placement + instance_id dual-write) so every later
            #    turn lands on this runner's disk-resident CLI state. NULL
            #    placement stays NULL (the local-default) and an existing
            #    runner pin is never rewritten. Best-effort: a pin failure
            #    must not kill the turn (the lease already makes us exclusive).
            try:
                await asyncio.to_thread(
                    self._maybe_pin_placement, sid, runner_id, session_row
                )
            except Exception:
                logger.warning(
                    "SessionRuntime: sticky pin failed for %s", sid, exc_info=True
                )

            # 3b. Working-set restore: the session's dirty
            #     working tree may have been snapshotted to a scratch ref by
            #     a PRIOR turn on ANOTHER runner. Bring it onto this runner's
            #     clean checkout before the pump starts. Best-effort — a git
            #     failure must never delay/kill the turn.
            await self._restore_working_set(sid)

            # 4. Per-turn bookkeeping + acquire the reader token; stop any idle
            #    reader so this pump is the session's single reader.
            self._reset_started_this_turn(sid)
            await stop_idle_reader(sid)

            # 4b. Self-heal: settle any STALE parked turn rows for this
            #     session (excluding the turn we are about to run). 'parked' is
            #     terminal and nothing else transitions a row out of it, so a
            #     session that parked on now-finished background work would show
            #     parked forever in lifecycle_state. A new app-turn starting is
            #     proof the session is live again — clear the residue.
            #
            #     GUARD: only self-heal when NO background work is still running.
            #     A queued turn intentionally starts while a PRIOR turn is parked
            #     on RUNNING work (turn_scheduler starts the next turn without
            #     waiting for the park to resolve), and that parked row is the
            #     signal ``_latest_context_turn`` and the held-peer gate rely
            #     on — settling it early risks wrong context attribution. We do NOT also gate on unresolved launches:
            #     a launch that never materializes a registry record must not
            #     block the self-heal (that would re-introduce the stuck-parked
            #     stick this heal exists to clear). Best-effort: a settle failure
            #     must never delay/kill the turn.
            #
            #     "stale" is the exceptional case, not the common one: the
            #     ORDINARY unpark passes through here with its parked row just
            #     resolved, and settling it is the normal hand-off, not a
            #     repair — so the log line says "resolved", not "stale". The
            #     Stop path settles parked rows itself
            #     (``query_session_lifecycle.enter_user_stop``), so a session the
            #     user stopped does not wait for its next turn to stop reporting
            #     itself parked.
            try:
                from app.core.task_registry import task_registry

                if not task_registry.has_active_background_work(sid):
                    settled = await asyncio.to_thread(
                        turns_db.settle_parked_turns, sid, tid
                    )
                    if settled:
                        logger.info(
                            "SessionRuntime: settled %d resolved parked turn(s) "
                            "for %s at turn %s start", settled, sid, tid,
                        )
            except Exception:
                logger.warning(
                    "SessionRuntime: settle_parked_turns failed for %s", sid,
                    exc_info=True,
                )

            # 4c. Failed-worker revive. ``sessions.status == 'failed'`` is
            #     stamped by ProviderAgent on a worker session whose dispatch
            #     ended in the registry status "error" — a verdict about a run
            #     that is OVER, not a property of the session. A turn STARTING
            #     is proof the session is live again, so clear it here rather
            #     than at enqueue time: this is the single choke point EVERY
            #     turn passes through (a user message typed into the worker
            #     session, a SendMessage-style resume, a scheduler wake), and a
            #     turn that only ever sat queued should not un-fail anything.
            #     ``session_row`` was read at the top of this method, moments
            #     ago. Best-effort: a status write must never delay/kill a turn.
            try:
                from app.core.provider_agent import WORKER_FAILED_STATUS

                if (session_row or {}).get("status") == WORKER_FAILED_STATUS:
                    from app.db import database as _database

                    await asyncio.to_thread(
                        _database.update_session, sid, status="active"
                    )
                    logger.info(
                        "SessionRuntime: revived failed session %s to active at "
                        "turn %s start", sid, tid,
                    )
            except Exception:
                logger.warning(
                    "SessionRuntime: failed-session revive failed for %s", sid,
                    exc_info=True,
                )

            # 5. Recorder bound to the INJECTED lease/turn ids (no self-claim).
            recorder = await TurnRecorder.create_for_run(
                sid,
                run_id=run_id,
                fence_token=fence_token,
                turn_id=tid,
                command_id=self.turn.get("command_id"),
                runner_id=runner_id,
            )
            # A fence violation means another runner now owns this run: the
            # recorder goes silent and this hook stops the pump we are still
            # driving (same cancel path as interrupt escalation).
            recorder.on_superseded = self._on_recorder_superseded
            recorder.on_recording_failed = self._on_recorder_recording_failed

            # 6/7. Pump under the reader token, then ALWAYS finalize. A pump
            #    crash is absorbed (``_terminal`` stays ``None`` → finalize
            #    settles ``aborted``) rather than re-raised, so finalize AND the
            #    settlement callback still run. The pump runs as a CHILD task so
            #    interrupt() escalation can cancel it without cancelling
            #    execute() itself.
            try:
                async with get_reader_lock(sid):
                    # Pooled engines do not reconnect. Their Swarm recovery
                    # callback correctly refuses this newly claimed active run;
                    # repair only here, with its live local ownership proof.
                    try:
                        from app.core.swarm import reconcile_owned_pending

                        await reconcile_owned_pending(sid, run_id, fence_token)
                    except Exception:
                        logger.warning(
                            "SessionRuntime: owned Swarm recovery failed for %s",
                            sid, exc_info=True,
                        )
                    pump = asyncio.create_task(self._run_pump(recorder))
                    self._pump_task = pump
                    if self._unowned:
                        # Lost before the pump existed: never reach the provider.
                        pump.cancel()
                    self._start_progress_watchdog()
                    try:
                        await pump
                    finally:
                        self._stop_progress_watchdog()
                        self._pump_task = None
                        if not pump.done():
                            # execute() itself is being torn down mid-pump —
                            # take the child with us before the lock releases.
                            pump.cancel()
                            try:
                                await pump
                            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                                pass
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    raise  # execute() was cancelled from outside — propagate.
                # interrupt() escalation cancelled the wedged pump; the drain
                # block / finalize below decide what happens to the turn.
                logger.info(
                    "SessionRuntime: pump cancelled by interrupt escalation for %s turn %s",
                    sid, tid,
                )
            except Exception:
                logger.warning(
                    "SessionRuntime: pump crashed for %s turn %s — finalizing aborted",
                    sid, tid, exc_info=True,
                )

            if self._drain_after_cancel:
                # Escalation-keep: the wedged foreground pump was cancelled but
                # the client (with live background work) survives. Drain it
                # HERE, under the still-held run lease, so ``reap_expired``
                # never routes the draining turn while its reader works. The
                # drain reader settles the turn and fires the scheduler seam
                # itself (``_settle_drained``) — execute() must NOT fire again.
                start_idle_reader(sid, drain_turn_id=tid)
                drain_task = _idle_readers.get(sid)
                if drain_task is not None:
                    try:
                        await drain_task
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling() > 0:
                            raise
                    except Exception:  # noqa: BLE001
                        pass  # the reader logged it; reap resolves a stuck turn
                final = await asyncio.to_thread(turns_db.get_turn, tid)
                if final is not None and final["state"] in turns_db.TERMINAL_STATES:
                    outcome = final["state"]
                return outcome
            outcome, settled_here = await self._finalize(sid, tid)
            if (
                self._pending_done is not None
                and self._abort_reason != "recording_failed"
            ):
                # The pump ending does not prove the session is finished: the
                # park gate above may still find background work. Deliver the
                # WS completion only once its outcome is known. A turn aborted
                # because recording failed never completes.
                await self._broadcast({
                    **self._pending_done,
                    "session_id": sid,
                    "outcome": outcome,
                })
            self._pending_done = None
        finally:
            # Deregister ourselves as the live runtime (self-guarded so a fresh
            # runtime that already took over the session is never evicted).
            if _live_runtimes.get(sid) is self:
                _live_runtimes.pop(sid, None)
                # End the persistent desktop indicator and release held input.
                # Invalidate workers that outlived cancellation before re-arming.
                from app.mcp.computer_use import abort as cu_abort
                cu_abort.request_stop(sid, reason="completed")
            # Stop the heartbeat, then release the run lease — always. In that
            # order: a beat landing after the release would read the released
            # run as a lost lease and interrupt a turn that already ended.
            renew_task.cancel()
            try:
                await renew_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            try:
                await asyncio.to_thread(runs_db.release_run, run_id, fence_token)
            except Exception:
                logger.warning(
                    "SessionRuntime: release_run failed for %s run %s",
                    sid, run_id, exc_info=True,
                )
            # Lease left active — re-check + broadcast the OFF diff now rather
            # than waiting for the sweep loop (mirrors TurnRecorder.close).
            _notify_run_change(sid)

        # 8. Settlement callback (the scheduler seam) — after the lease is free,
        #    and ONLY when finalize actually performed the DB transition. A racer
        #    (interrupt escalation, reap) that settled first already fired it.
        if outcome is not None and settled_here:
            await _fire_on_turn_settled(sid, tid, outcome)
        return outcome

    # ------------------------------------------------------------------
    # pump — WRAPS _stream_to_websocket_impl (internals untouched)
    # ------------------------------------------------------------------

    async def _run_pump(self, recorder: TurnRecorder) -> None:
        """Drive the wrapped pump: recorder observes every event (SSE delivery),
        the broadcast adapter carries the WS frames. Never rebuilds the
        impl — it is called verbatim.
        """
        from app.core.query_engine import _stream_to_websocket_impl

        prompt = self.turn.get("prompt") or ""
        turn_overrides = dict(self.turn.get("overrides") or {})
        completion_frames = list(
            ((self.turn.get("payload") or {}).get("pending_frames") or [])
        )
        if completion_frames:
            # The app turn queue owns wake admission and exactly-once execution.
            # Carry the claimed completions to HarnessAdapter so Fluid can clear
            # its durable pending-task state without starting a second wake turn.
            turn_overrides["background_task_completions"] = completion_frames
        # Fluid records what started the turn on its TurnStarted event; the
        # adapter maps this app source onto fluid's vocabulary. A failover
        # retry records the source of the turn it retries, so a retried wake
        # still counts as a wake.
        from app.core.provider.harness_adapter import fluid_turn_source
        from app.core.turn_scheduler import effective_turn_source

        app_source = await asyncio.to_thread(effective_turn_source, self.turn)
        turn_overrides["turn_source"] = app_source
        # A resumed turn re-runs the engine loop from the session head: no new
        # prompt is sent and no message.user is recorded, so the transcript
        # gains only the answer the failed turn never produced.
        resume = self.source == "resume"
        # A message the composer deferred (sent while a turn was live) was never
        # written to the ``session_messages`` store at send time — the
        # transcript must show it where the model actually receives it, which is
        # here. The event log gets its ``message.user`` from ``recorder.start``
        # below, as it does for any non-resume turn.
        #
        # The exception is a row re-queued after its steer was swallowed by an
        # interrupted turn: the delivery receipt already wrote the message to
        # BOTH stores before that turn died, so this run must add neither, or
        # the same sentence shows up twice.
        already_recorded = bool(
            (self.turn.get("payload") or {}).get("user_message_already_recorded")
        )
        # A row whose steer reached the model but whose absorb never landed. The
        # engine gets the prompt with an honest note in front of it; the stores
        # and the recorder keep the user's own words.
        engine_prompt = prompt
        if (self.turn.get("payload") or {}).get("steer_already_delivered"):
            engine_prompt = f"{_REDELIVERY_NOTE}\n\n{prompt}" if prompt else prompt
        held_peer = ""
        engine_started = False
        if (
            not resume
            and not already_recorded
            and (self.turn.get("payload") or {}).get("deferred_user_record")
        ):
            await self._persist_user_message(prompt)
        done_event = None
        try:
            await recorder.start(
                prompt,
                profile_id=self.turn.get("profile_id"),
                record_user=not resume and not already_recorded,
                source=fluid_turn_source(app_source),
            )
            if self._abort_reason == "recording_failed":
                return
            if completion_frames:
                # The wake turn is the claimed completions' delivery point, so this
                # is where the event log — and with it the frontend card — learns
                # the task is done. A task that finished BETWEEN turns produces no
                # terminal frame inside this turn's stream, and the frontend does
                # not force-settle background shells at turn end, so without this
                # append the card runs forever.
                try:
                    await recorder.record_task_terminal(completion_frames)
                    if self._abort_reason == "recording_failed":
                        return
                except Exception:  # noqa: BLE001 — a card badge never kills a turn
                    logger.warning(
                        "SessionRuntime: recording terminal task events failed for "
                        "%s (%d frame(s))",
                        self.session_id, len(completion_frames), exc_info=True,
                    )
                # This wake turn IS the delivery of those completions, so it is also
                # where ``terminal_notified`` becomes true — the steered mid-turn
                # path latches it at its own delivery receipt. Without this the flag
                # stayed False forever for every clean completion (only the
                # error/killed chip in ``complete_task`` ever set it).
                try:
                    from app.core import turn_scheduler

                    turn_scheduler.on_task_notification_delivered(
                        self.session_id,
                        (self.turn.get("payload") or {}).get("task_ids")
                        or [f.get("task_id") for f in completion_frames
                            if isinstance(f, dict)],
                    )
                except Exception:  # noqa: BLE001 — bookkeeping never kills a turn
                    logger.warning(
                        "SessionRuntime: terminal_notified latch failed for %s",
                        self.session_id, exc_info=True,
                    )
            if self._abort_reason == "recording_failed":
                return
            from app.core.task_registry import task_registry
            if (self._stop_generation is not None
                    and task_registry.stop_invalidated(self.session_id, self._stop_generation)):
                self._terminal = "interrupted"
                self._abort_reason = "user_stopped"
                return
            # Peer messages held while the session was parked ride this turn,
            # whatever started it (turn_scheduler's held-message policy). Only
            # the engine sees them; the transcript keeps the prompt as sent.
            if not resume:
                try:
                    from app.core import turn_scheduler

                    held_peer = await turn_scheduler.fold_held_peer_messages_for_turn(
                        self.session_id, self.turn,
                    )
                except Exception:  # noqa: BLE001 — held messages never block a turn
                    logger.warning(
                        "SessionRuntime: taking held peer messages failed for %s",
                        self.session_id, exc_info=True,
                    )
                if held_peer:
                    block = turn_scheduler.held_peer_block(held_peer)
                    engine_prompt = (
                        f"{engine_prompt}\n\n{block}" if engine_prompt else block
                    )
            async for event in _stream_to_websocket_impl(
                prompt=engine_prompt,
                resume=resume,
                session_id=self.session_id,
                profile_id=self.turn.get("profile_id"),
                # The legacy project id deliberately stays live here — it still
                # keys the project-existence gate, the app-config/builtins MCP
                # servers, the DB project hooks, and agent-memory sync, none of
                # which are project_v2-keyed yet.
                project_id=self.turn.get("project_id"),
                overrides=turn_overrides or None,
                broadcast_func=self._broadcast,
                api_user_id=self.api_user_id,
                user_id=self.user_id,
                # Cross-install uploads. The lifting install put the blob
                # ids on the turn payload; the pump materializes them into the
                # resolved working dir and rewrites the prompt. Absent for every
                # locally-sent turn (the files are already on this disk).
                upload_blob_ids=(self.turn.get("payload") or {}).get(
                    "upload_blob_ids"
                ),
                # The subscription loop guard counts unattended sources and
                # keeps the row when it holds the turn for confirmation.
                turn_source=self.source,
                loop_guard_turn=self.turn,
            ):
                engine_started = True
                etype = event.get("type") if isinstance(event, dict) else None
                # ANY frame is proof the engine is still moving — a tool result,
                # a usage record, a token of text. The watchdog only fires on
                # total silence, so this is deliberately the widest possible
                # liveness signal.
                self._last_progress_at = time.perf_counter()
                if etype == "steer_delivered":
                    # Internal receipt, not a render frame: it becomes a
                    # ``message.user`` in the log (below) and never reaches the
                    # WS leg as itself.
                    await self._on_steer_delivered(recorder, event)
                    continue
                if etype in _MODEL_OUTPUT_FRAMES:
                    self._model_output_at = time.perf_counter()
                    # First model output proves the engine is up and steerable.
                    # Anything that queued while this turn was starting can now
                    # be folded in rather than run as a second turn.
                    self._sweep_start_window_steers()
                await recorder.on_event(event)
                if self._abort_reason == "recording_failed":
                    return
                if etype in ("done", "interrupted", "error"):
                    self._terminal = etype
                    if etype == "error":
                        message = event.get("message")
                        self._terminal_error = (
                            str(message) if message else None
                        )
                # Render frames ride the recorder → SSE path; only the kept
                # lifecycle/tool frames hit the WS/broadcast leg (without the
                # trim, render content double-delivers).
                if etype == "done":
                    self._pending_done = dict(event)
                elif not _is_trimmed_render_frame(etype):
                    ws_event = event
                    if isinstance(event, dict):
                        # FE cross-session guard tag.
                        event["session_id"] = self.session_id
                        # Keep the frame, drop a multi-MB tool input that no
                        # WS consumer reads (copies rather than mutating, so the
                        # recorder's already-handed-over dict is untouched).
                        ws_event = _cap_ws_tool_input(event)
                    if etype == "done":
                        done_event = ws_event
                    else:
                        await self._broadcast(ws_event)
        finally:
            if held_peer and not engine_started:
                # The engine never produced a frame, so it may never have had
                # the prompt: the held messages wait for the next turn.
                try:
                    from app.core import turn_scheduler

                    turn_scheduler.restore_held_peer_messages(self.session_id, held_peer)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "SessionRuntime: could not restore held peer messages "
                        "for %s", self.session_id, exc_info=True,
                    )
            self._recorder_closing = True
            await recorder.close()
        if done_event is not None and self._abort_reason != "recording_failed":
            await self._broadcast(done_event)

    # ------------------------------------------------------------------
    # progress watchdog — a turn that goes completely silent is terminalized
    # ------------------------------------------------------------------

    def _progress_timeout_seconds(self) -> float:
        """The configured silence budget, or 0 when the watchdog is off."""
        try:
            return max(0.0, float(
                getattr(settings, "turn_progress_timeout_seconds", 0) or 0
            ))
        except (TypeError, ValueError):
            return 0.0

    def _start_progress_watchdog(self) -> None:
        """Arm the silence watchdog for this turn (no-op when disabled)."""
        timeout = self._progress_timeout_seconds()
        if timeout <= 0:
            return
        self._last_progress_at = time.perf_counter()
        task = asyncio.create_task(self._progress_watchdog_loop(timeout))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._progress_watchdog = task

    def _stop_progress_watchdog(self) -> None:
        task = self._progress_watchdog
        self._progress_watchdog = None
        if task is not None and not task.done():
            task.cancel()

    async def _progress_watchdog_loop(self, timeout: float) -> None:
        """Terminalize a turn that has produced NO event for ``timeout`` seconds.

        The failure it exists for: a host tool call (a browser automation step,
        an MCP round-trip) that blocks forever. The code cell's own timeout
        cannot interrupt a blocked ``__host_call__``, nothing above it bounds
        the turn, and the session presents as ``running`` indefinitely with the
        user's last request unanswered and no signal that anything is wrong.

        Two things are deliberately NOT killed:

        * a turn waiting on the human (an ``AskUser`` round-trip, a permission
          or plan gate). It is legitimately silent for as long as the person
          takes, and its own deadline is ``user_question_timeout_seconds``.
        * a turn that has already reached a terminal frame — the pump is
          finishing, and cancelling it there would only lose the tail.

        The kill mirrors a provider error exactly: an ``error`` frame on the
        live stream, ``_terminal='error'`` so ``_finalize`` settles the row
        ``aborted``/``stop_reason=error`` with the reason on it, the recorder's
        crash path writing ``turn.aborted``, and the durable system message
        every error terminal now leaves behind.
        """
        # Poll often enough to be prompt without being a busy loop: a minute, or
        # a quarter of the budget when the budget is short (tests set seconds).
        interval = max(0.05, min(60.0, timeout / 4))
        while True:
            await asyncio.sleep(interval)
            if self._terminal is not None:
                return  # the pump reached a terminal frame on its own
            silent_for = time.perf_counter() - self._last_progress_at
            if silent_for < timeout:
                continue
            if _awaiting_user_input(self.session_id):
                # Blocked on a person, not wedged. Restamp so the budget starts
                # again from the moment the answer lands.
                self._last_progress_at = time.perf_counter()
                continue
            await self._terminalize_silent_turn(silent_for)
            return

    async def _terminalize_silent_turn(self, silent_for: float) -> None:
        """End a wedged turn the way a provider error would. Never raises."""
        elapsed = (
            f"{int(silent_for // 60)} minutes" if silent_for >= 60
            else f"{silent_for:.0f} seconds"
        )
        message = (
            f"This turn stopped producing any output for {elapsed} and was "
            f"ended by the progress watchdog. A tool call most likely never "
            f"returned; nothing after it ran."
        )
        logger.warning(
            "SessionRuntime: progress watchdog terminalizing %s turn %s "
            "(silent for %.0fs)", self.session_id, self.turn_id, silent_for,
        )
        self._terminal = "error"
        self._terminal_error = message
        self._abort_reason = "progress_timeout"
        try:
            await self._broadcast({
                "type": "error",
                "message": message,
                "session_id": self.session_id,
            })
        except Exception:  # noqa: BLE001
            logger.debug(
                "SessionRuntime: watchdog error broadcast failed for %s",
                self.session_id, exc_info=True,
            )
        pump = self._pump_task
        if pump is not None and not pump.done():
            pump.cancel()

    async def _broadcast(self, event: Dict[str, Any]) -> None:
        """Route one frame through the broadcast adapter (registered or default)."""
        hook = _broadcaster
        if hook is not None:
            try:
                await hook(
                    self.session_id,
                    event,
                    source=self.source,
                    origin_send=self.origin_send,
                    origin_device_id=self.origin_device_id,
                )
            except Exception:  # noqa: BLE001
                logger.debug("SessionRuntime: broadcaster hook failed", exc_info=True)
            return
        await _default_broadcast(
            self.session_id,
            event,
            source=self.source,
            origin_send=self.origin_send,
            origin_device_id=self.origin_device_id,
        )

    # ------------------------------------------------------------------
    # mid-turn steering — hand queued messages to the running turn
    # ------------------------------------------------------------------

    async def steer_pending(self) -> int:
        """Submit this session's eligible queued messages to the live engine.

        Walks the queue IN ORDER and stops at the first row that cannot be
        steered: a later message must never overtake an earlier one, so an
        ineligible row (an attachment, a different model, a different agent)
        pins everything behind it to the ordinary drain. The rows stay
        ``queued`` throughout — only fluid's delivery receipt retires one (see
        ``_on_steer_delivered``), so a steer the engine drops at turn end simply
        runs as the turn it always was.

        Returns how many rows were submitted.
        """
        adapter = self._live_adapter()
        if adapter is None:
            return 0
        if await self._turn_is_dying():
            # Steering into a turn the user is already killing is how a message
            # gets swallowed and never answered: the engine takes the text,
            # receipts it, and then dies before saying anything back. Leave the
            # rows queued — the abort-drain runs them as turns of their own.
            logger.info(
                "SessionRuntime: %s is being interrupted — leaving the queue to "
                "the drain instead of steering", self.session_id,
            )
            return 0
        try:
            rows = await asyncio.to_thread(turns_db.get_queued_turns, self.session_id)
        except Exception:
            logger.warning(
                "SessionRuntime: could not read the queue for %s", self.session_id,
                exc_info=True,
            )
            return 0

        submitted = 0
        for row in rows:
            if row.get("source") not in _STEERABLE_SOURCES:
                continue
            if not self._steer_eligible(row):
                break
            row_id = row["id"]
            if row_id in self._steer_attempted:
                continue
            prompt = row.get("prompt") or ""
            if not prompt:
                break
            self._steer_attempted.add(row_id)
            try:
                await adapter.steer(prompt, id=row_id)
            except NotImplementedError:
                # A backend with no mid-turn channel at all — stop asking for
                # the rest of this turn.
                logger.debug(
                    "SessionRuntime: adapter cannot steer %s — leaving the "
                    "queue to the drain", self.session_id,
                )
                return submitted
            except RuntimeError as exc:
                # Nothing live to steer into (the turn is ending, interrupting,
                # or its process has not come up). The row is untouched; the
                # settle-drain will run it.
                self._steer_attempted.discard(row_id)
                logger.info(
                    "SessionRuntime: %s not steerable right now (%s) — turn %s "
                    "stays queued", self.session_id, exc, row_id,
                )
                break
            except Exception:
                self._steer_attempted.discard(row_id)
                logger.warning(
                    "SessionRuntime: steer submit failed for %s turn %s",
                    self.session_id, row_id, exc_info=True,
                )
                break
            submitted += 1
        return submitted

    async def steer_task_notification(
        self,
        prompt: str,
        *,
        notification_id: str,
        frames: List[Dict[str, Any]],
        task_ids: List[str],
        held_peer_messages: str = "",
    ) -> bool:
        """Submit one terminal ``<task-notification>`` into the live turn.

        Same gates as :meth:`steer_pending` — an adapter must be up and the turn
        must not already be dying — minus the queue walk and the eligibility
        rules, which are about honoring what a USER picked for a message of their
        own. A background completion belongs to the turn that launched it; the
        model it reaches is whichever one is running.

        The frames are registered BEFORE the submit so a receipt that comes back
        on the very next event still finds them, and are dropped again the moment
        the submit is refused. Returns True only for an accepted submission —
        delivery is the receipt's word, not this one's.
        """
        if not prompt or not notification_id:
            return False
        adapter = self._live_adapter()
        if adapter is None:
            return False
        if await self._turn_is_dying():
            # Steering a notification into a turn the user is killing loses it:
            # the engine takes the text and dies before the model reads it, and
            # there is no queue row behind it to run instead. Leave it to the
            # caller's at-rest wake.
            logger.info(
                "SessionRuntime: %s is being interrupted — leaving the task "
                "notification to the at-rest wake", self.session_id,
            )
            return False
        self._task_steers[notification_id] = {
            "frames": list(frames or []),
            "task_ids": list(task_ids or []),
        }
        if held_peer_messages:
            self._task_steers[notification_id]["held_peer_messages"] = held_peer_messages
        try:
            # A harness notice, not the user: the summary inside it is text a
            # worker wrote, so it goes out in the notice marker, never the
            # steering marker that carries the user's authority.
            await adapter.steer(prompt, id=notification_id, origin="system")
        except NotImplementedError:
            self._task_steers.pop(notification_id, None)
            logger.debug(
                "SessionRuntime: adapter cannot steer %s — task notification "
                "falls back to the at-rest wake", self.session_id,
            )
            return False
        except RuntimeError as exc:
            self._task_steers.pop(notification_id, None)
            logger.info(
                "SessionRuntime: %s not steerable right now (%s) — task "
                "notification falls back to the at-rest wake",
                self.session_id, exc,
            )
            return False
        except Exception:
            self._task_steers.pop(notification_id, None)
            logger.warning(
                "SessionRuntime: task-notification steer submit failed for %s",
                self.session_id, exc_info=True,
            )
            return False
        logger.info(
            "SessionRuntime: steered a task notification (tasks=%s) into %s "
            "turn %s", list(task_ids or []), self.session_id, self.turn_id,
        )
        return True

    def _sweep_start_window_steers(self) -> None:
        """Once per turn, re-offer rows that queued while this turn was starting.

        ``chat_ws_handler`` decides whether to steer from a ``get_live_turn``
        read taken before it enqueues. A turn that has been kicked but has not
        reached ``start_turn`` yet is not live by that test, so a message
        arriving in the gap is enqueued as an ordinary turn and no steer is ever
        attempted for it. The same happens a moment later for a different
        reason: the turn is live, but its pooled client has not come up, so
        ``steer_pending`` finds no adapter and gives up.

        Neither case is a lost message — the settle-drain runs the row as its
        own turn — but running it is exactly the duplicate-turn behavior worth
        avoiding when the two messages are the same intent seconds apart. This
        sweep costs one queue read on the first model output of each turn, by
        which point the adapter is provably up, and hands anything still queued
        to ``steer_pending``, which owns all the eligibility and ordering rules.

        Fire-and-forget: nothing here may add latency to the pump, and a failed
        sweep is the status quo (the row runs as a turn), not an error.
        """
        if self._start_window_swept:
            return
        self._start_window_swept = True
        task = asyncio.create_task(self._sweep_start_window_steers_impl())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._start_window_sweep_task = task

    async def _sweep_start_window_steers_impl(self) -> None:
        try:
            folded = await self.steer_pending()
        except Exception:  # noqa: BLE001 - never surface into the pump
            logger.debug(
                "SessionRuntime: start-window steer sweep failed for %s",
                self.session_id, exc_info=True,
            )
            return
        if folded:
            logger.info(
                "SessionRuntime: folded %d message(s) queued during %s turn %s's "
                "start window into the live turn",
                folded, self.session_id, self.turn_id,
            )

    async def cancel_steer(self, turn_id: str) -> str:
        """Try to recall the steer submitted for queued turn ``turn_id``.

        Returns ``"not_submitted"``, ``"cancelled"`` or ``"delivered"`` — see
        the module-level :func:`try_cancel_steer` for what each means to a
        caller. A turn this runtime never steered is ``"not_submitted"``
        without touching the engine.
        """
        if turn_id not in self._steer_attempted:
            return "not_submitted"
        adapter = self._live_adapter()
        if adapter is None:
            # Submitted, but the engine behind it is already gone — so is the
            # receipt path that would have absorbed the row. Nothing can
            # deliver it now; let the removal go through.
            return "not_submitted"
        if await adapter.cancel_steer(turn_id):
            # Never delivered and never will be, so the row is free again — and
            # it must leave the attempted set, or a later steer_pending() pass
            # would skip a row that is genuinely still queued.
            self._steer_attempted.discard(turn_id)
            return "cancelled"
        return "delivered"

    async def _turn_is_dying(self) -> bool:
        """Whether this turn has stopped being a safe place to put a message.

        Two signals, cheapest first:

        * ``_interrupt_driving`` — THIS process is already driving the kill, so
          no DB round-trip is needed;
        * the row's own state — the durable ``interrupting`` flip
          (``turns.interrupt_turn``) happens at the command layer BEFORE the
          runtime is told, and that gap is exactly where the observed race
          lived, so the in-process latch alone is not enough. Anything other
          than ``running`` (interrupting, draining, or already settled) cannot
          take a steer.

        Fails OPEN (False) on a read error: a transient DB hiccup must not turn
        every send into a queued turn.
        """
        if self._interrupt_driving:
            return True
        try:
            row = await asyncio.to_thread(turns_db.get_turn, self.turn_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "SessionRuntime: could not re-read turn %s before steering",
                self.turn_id, exc_info=True,
            )
            return False
        return row is not None and row.get("state") != "running"

    def _live_adapter(self) -> Optional[Any]:
        """The provider adapter behind this session's pooled client, or None."""
        try:
            from app.core.session_pool import get_session_pool

            state = get_session_pool().peek(self.session_id)
        except Exception:  # noqa: BLE001
            return None
        return getattr(state, "adapter", None) if state is not None else None

    def _steer_eligible(self, row: Dict[str, Any]) -> bool:
        """Whether ``row``'s prompt can join the LIVE turn as it stands.

        Three things disqualify it, all for the same reason — the live turn is
        already running under settings this row would change, and folding the
        text in would silently ignore what the user picked:

        * attachments (``upload_blob_ids``): the files have to be materialized
          into the working dir before the prompt makes sense, which is turn
          setup, not something a tool result can carry;
        * a different model / effort / permission mode;
        * a different profile — a message addressed to another agent.
        """
        payload = row.get("payload") or {}
        if payload.get("upload_blob_ids"):
            return False
        if (row.get("profile_id") or None) != (self.turn.get("profile_id") or None):
            return False
        live_overrides = self.turn.get("overrides") or {}
        row_overrides = row.get("overrides") or {}
        for key in _STEER_BINDING_KEYS:
            if (row_overrides.get(key) or None) != (live_overrides.get(key) or None):
                return False
        return True

    async def _on_steer_delivered(
        self, recorder: TurnRecorder, event: Dict[str, Any]
    ) -> None:
        """Record + retire every queued row fluid just delivered into this turn.

        The receipt means the text is in immutable history at the position the
        stream is at right now, so this is exactly where the user message
        belongs — both in the legacy ``session_messages`` store and as a
        ``message.user`` on the live turn, stamped with the QUEUED row's
        command id so the frontend's pending bubble collapses onto it.

        Record first, absorb second: a crash between fluid's delivery and the
        absorb leaves the row ``queued``, so it re-runs as an ordinary turn —
        a rare, visible duplicate rather than a silently lost message.
        """
        steers = event.get("steers") or []
        absorbed = False
        for steer in steers:
            if not isinstance(steer, dict):
                continue
            row_id = steer.get("id")
            if not row_id and steer.get("from") and steer.get("text"):
                # Peer messages have no queued-user row. Their engine receipt
                # proves delivery; publish that fact for chat/voice observers
                # rather than silently dropping it or asking the user to interrupt.
                await recorder.on_event({
                    "type": "system", "subtype": "agent_message",
                    "data": {"sender": str(steer["from"]),
                             "content": str(steer["text"]), "delivery": "delivered"},
                })
                continue
            if not row_id:
                logger.info(
                    "SessionRuntime: steer delivered for %s with no queue id — "
                    "nothing to record", self.session_id,
                )
                continue
            if row_id in self._task_steers:
                # A terminal task notification, not a user message: no queue row
                # to load, record or retire. The receipt IS the delivery, so
                # this is where the scheduler latches terminal_notified and
                # where the event log learns the task is done — the same
                # ``task.updated`` append the wake turn makes, so the frontend
                # card settles mid-turn instead of running forever.
                delivered = await self._on_task_notification_delivered(
                    recorder, self._task_steers[row_id]
                )
                if not delivered:
                    return
                self._task_steers.pop(row_id, None)
                continue
            try:
                row = await asyncio.to_thread(turns_db.get_turn, row_id)
            except Exception:
                logger.warning(
                    "SessionRuntime: could not load steered turn %s", row_id,
                    exc_info=True,
                )
                continue
            if row is None or row.get("session_id") != self.session_id:
                logger.warning(
                    "SessionRuntime: steer receipt names unknown turn %s for "
                    "%s — skipping", row_id, self.session_id,
                )
                continue
            if row.get("state") != "queued":
                # A receipt for a row that already left the queue. The two
                # states are supposed to be mutually exclusive: cancelling a
                # steer is atomic against its delivery, so a cancelled steer
                # can never reach the model and a delivered one can never be
                # removed (``handle_remove_queued_message`` leaves a delivered
                # row alone precisely so this path can absorb it). Reaching
                # here means that invariant broke somewhere, and the safe side
                # is silence: recording now would put a message the user
                # removed back into the transcript.
                logger.error(
                    "SessionRuntime: steer receipt for turn %s in state %r "
                    "(expected 'queued') on %s — not recording",
                    row_id, row.get("state"), self.session_id,
                )
                continue
            content = row.get("prompt") or steer.get("text") or ""
            profile_id = row.get("profile_id") or self.turn.get("profile_id")
            recorded = await recorder.record_steered_user_message(
                content,
                profile_id=profile_id,
                command_id=row.get("command_id"),
            )
            if not recorded:
                return
            await self._persist_user_message(content, profile_id=profile_id)
            if await self._absorb_delivered_row(row_id):
                absorbed = True
                # Remember WHEN this turn swallowed the row, and the row
                # itself: if the turn then dies from an interrupt the user
                # asked for BEFORE this moment, ``_finalize`` puts the
                # message back on the queue rather than leaving it in the
                # transcript with no answer under it.
                self._steer_absorbed[row_id] = (time.perf_counter(), row)
        if absorbed:
            # The rows left the queue — clear their pending bubbles everywhere.
            await _broadcast_queue_after_start(self.session_id)

    async def _on_task_notification_delivered(
        self, recorder: TurnRecorder, entry: Dict[str, Any]
    ) -> bool:
        """Close out one steered ``<task-notification>`` fluid just delivered.

        Three effects, all best-effort and none allowed to disturb the pump:
        the frontend card settles (``task.updated``, the same append the wake
        turn makes for its ``pending_frames``), the ENGINE is told the task is
        terminal, and the scheduler latches ``terminal_notified`` for the tasks
        now provably in front of the model.

        The engine leg is the one a wake turn gets for free. A wake carries its
        frames into ``prepare_background_completions`` at turn start, which is
        what drops their ids from fluid's ``_background_pending``; a steered
        notification has no turn start of its own, so it submits the very same
        frames here (``wake=False`` — the model already has the words). Without
        it fluid kept every mid-turn completion pending and listed it on every
        later ``SessionParked``, long after the task was dead. The app's own
        durable park state is untouched: it is the authority, and it already
        moved on.
        """
        frames = entry.get("frames") or []
        task_ids = entry.get("task_ids") or []
        if frames:
            try:
                await recorder.record_task_terminal(frames)
                if recorder.recording_failed is True or self._abort_reason == "recording_failed":
                    return False
            except Exception:  # noqa: BLE001 — a card badge never kills a turn
                logger.warning(
                    "SessionRuntime: recording steered terminal task events "
                    "failed for %s (%d frame(s))",
                    self.session_id, len(frames), exc_info=True,
                )
        try:
            from app.core import turn_scheduler

            await turn_scheduler.retire_task_terminals(self.session_id, frames)
            turn_scheduler.on_task_notification_delivered(self.session_id, task_ids)
        except Exception:  # noqa: BLE001 — bookkeeping never kills a turn
            logger.warning(
                "SessionRuntime: task-notification delivery callback failed "
                "for %s", self.session_id, exc_info=True,
            )
        return True

    async def _strand_task_steers(self) -> None:
        """Hand back every task notification this turn swallowed undelivered.

        fluid drops an unreceipted steer at turn end. A user's message survives
        that — its queue row was never retired, so the settle-drain runs it as a
        turn of its own — but a task notification has no row, so the scheduler
        has to be told to enqueue the ordinary at-rest wake for it instead.
        Never raises: this runs inside ``_finalize``, which must settle.
        """
        pending = self._task_steers
        self._task_steers = {}
        if not pending:
            return
        try:
            from app.core import turn_scheduler
        except Exception:  # noqa: BLE001
            logger.warning(
                "SessionRuntime: cannot reach the scheduler to re-wake %d "
                "stranded task notification(s) on %s",
                len(pending), self.session_id, exc_info=True,
            )
            return
        for entry in pending.values():
            try:
                held = entry.get("held_peer_messages") or ""
                await turn_scheduler.on_task_notification_stranded(
                    self.session_id,
                    entry.get("frames") or [],
                    entry.get("task_ids") or [],
                    **({"held_peer_messages": held} if held else {}),
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "SessionRuntime: could not re-wake a stranded task "
                    "notification on %s", self.session_id, exc_info=True,
                )

    async def _absorb_delivered_row(self, row_id: str) -> bool:
        """Retire a delivered steer's queue row, retrying a failing write.

        This absorb is the only thing between a delivered interjection and a
        SECOND delivery: a row left ``queued`` runs again as an ordinary turn and
        the model reads the same sentence twice. The usual cause of a failure
        here is a transient DB error, which clears, so a couple of quick retries
        buy back most of that window.

        Returns True only for the transition that actually absorbed the row. A
        plain ``False`` from the guarded UPDATE means the row already left the
        queue — an answer, not a failure, and not retried. When every attempt
        raises, the row is marked so the re-run it will now cause carries an
        honest note about the duplicate.
        """
        for attempt in range(_ABSORB_ATTEMPTS):
            try:
                return await asyncio.to_thread(turns_db.absorb_queued_turn, row_id)
            except Exception:
                last = attempt + 1 >= _ABSORB_ATTEMPTS
                logger.warning(
                    "SessionRuntime: absorb failed for steered turn %s "
                    "(attempt %d/%d)%s",
                    row_id, attempt + 1, _ABSORB_ATTEMPTS,
                    "" if last else " — retrying",
                    exc_info=last,
                )
                if last:
                    break
                await asyncio.sleep(_ABSORB_BACKOFF * (attempt + 1))
        await self._flag_redelivery(row_id)
        return False

    async def _flag_redelivery(self, row_id: str) -> None:
        """Mark a delivered-but-unabsorbed row as a known duplicate.

        The text is already in the model's context — fluid folded it into a tool
        result — and the row is still queued, so it WILL run again. Nothing can
        take the first delivery back, so the honest thing is to tell the re-run
        what happened; ``_run_pump`` turns this flag into a one-line note in front
        of the prompt it sends. The stored prompt is left exactly as the user
        typed it, so neither the transcript nor the pending bubble changes.

        Best-effort: a turn that is already limping must still settle.
        """
        try:
            row = await asyncio.to_thread(turns_db.get_turn, row_id)
            if row is None or row.get("state") != "queued":
                return
            payload = dict(row.get("payload") or {})
            payload["steer_already_delivered"] = True
            await asyncio.to_thread(
                turns_db.update_queued_turn_prompt,
                row_id, row.get("prompt") or "", payload,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "SessionRuntime: could not flag re-delivery on turn %s", row_id,
                exc_info=True,
            )

    async def _persist_user_message(
        self, content: str, *, profile_id: Optional[str] = None
    ) -> None:
        """Dual-write one user message to ``session_messages`` + fan it out.

        The same two steps ``handle_query`` runs for an idle send, moved to
        delivery time for a message that waited. Slash commands classify exactly
        as they do there. Best-effort: the event log is the transcript
        authority, so a legacy-store failure must never kill the turn.
        """
        try:
            from app.core.sync_engine import sync_engine
            from app.db import database as database_mod

            role = "user"
            metadata = None
            if content.strip().startswith("/"):
                parts = content.strip().split(None, 1)
                role = "command"
                metadata = {
                    "command_name": parts[0],
                    "command_args": parts[1] if len(parts) > 1 else "",
                }
            await asyncio.to_thread(
                database_mod.add_session_message,
                session_id=self.session_id,
                role=role,
                content=content,
                metadata=metadata,
                profile_id=profile_id or self.turn.get("profile_id"),
            )
            await sync_engine.broadcast_message_added(
                session_id=self.session_id,
                message={
                    "role": "user",
                    "content": content,
                    "profile_id": profile_id or self.turn.get("profile_id"),
                },
                source_device_id=None,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "SessionRuntime: user-message persist failed for %s "
                "(the turn still runs)", self.session_id, exc_info=True,
            )

    # ------------------------------------------------------------------
    # finalize — always runs; decides completed | aborted | parked
    # ------------------------------------------------------------------

    async def _finalize(self, sid: str, tid: str) -> "tuple[str, bool]":
        """Settle or park the turn. ALWAYS runs (called from the pump ``finally``).

        Returns ``(outcome, settled_here)``. ``settled_here`` is True only when
        THIS call actually performed the DB transition — a racer (interrupt
        escalation, ``reap_expired``) that settled the turn first already fired
        ``on_turn_settled``, so the caller must not fire again.

        Event mirroring: the recorder already emits ``turn.completed`` (on the
        pump ``done``) and ``turn.aborted`` (on ``interrupted``/``error`` or its
        crash-path ``close()``), so the runtime does NOT re-emit those. Only
        ``turn.parked`` — which the recorder never emits during the pump — is
        appended here (``record_parked``).
        """
        # The turn is over, so nothing is steerable into it any more. Anything
        # this runtime submitted but never saw receipted was dropped by the
        # engine at turn end; its row is still ``queued`` and the settle-drain
        # below runs it as an ordinary turn.
        self._steer_attempted.clear()
        # A task notification is the exception: it has no queue row behind it,
        # so an undelivered one has to be handed back to the scheduler or the
        # completion is lost. Before the park gate, so a wake enqueued here is
        # already in the queue when the settle-drain looks.
        await self._strand_task_steers()

        # turn_finalize.persist_turn_end_state: durable parked marker, branch
        # resync, summary flush.
        try:
            from app.core.turn_finalize import persist_turn_end_state

            await persist_turn_end_state(sid)
        except Exception:
            logger.debug("SessionRuntime: persist_turn_end_state failed for %s", sid, exc_info=True)

        # Turn-lifetime binding for ProviderAgent workers. This is the ONLY
        # teardown in the runtime and it is deliberately NARROW: it can only see
        # workers that
        # ``provider_agent.dispatch_worker`` itself registered, so ordinary
        # CLI-native background sub-agents are untouched and the park gate below
        # keeps parking them (that parking is a feature, not a bug).
        #
        # Placed HERE, after the durable end-of-turn state is persisted and
        # BEFORE the park-gate read, because every normal, aborted and crashed
        # turn end funnels through this method — and because teardown must be
        # reflected in what the park gate then reads (a worker killed here must
        # not park the turn it just died with). Never raises: a teardown failure
        # must not change how the turn settles.
        try:
            from app.core import provider_agent_supervisor

            await provider_agent_supervisor.teardown_workers(sid)
        except Exception:
            logger.warning(
                "SessionRuntime: ProviderAgent worker teardown failed for %s",
                sid, exc_info=True,
            )

        # Engine pending-set hygiene. fluid rebuilds its
        # ``_background_pending`` from the SubagentEvent log alone and lists
        # whatever is left on every ``SessionParked``, so a completion that
        # never reached the engine is listed forever.
        # Reconciling at turn end (cheap: fluid's rehydrate is latched, and
        # nothing is submitted unless the app can prove a pending id is over)
        # means the NEXT park names only work that is genuinely outstanding.
        try:
            from app.core import pending_reconcile

            await pending_reconcile.reconcile_engine_pending(sid)
        except Exception:
            logger.debug(
                "SessionRuntime: engine pending reconcile failed for %s", sid,
                exc_info=True,
            )

        # Park gate: active background work OR UNRESOLVED launches.
        # ``_background_launches`` itself stays CUMULATIVE (it is cleared only
        # by ``clear_session``); only the GATE ignores launches whose task
        # already reached a terminal registry status. ORing the raw cumulative
        # set would re-park EVERY subsequent turn forever once a launch was left
        # behind after its task finished (``turn.parked task_ids=[]`` with zero
        # running work). Known benign residual: a launch that never
        # materializes a registry record at all (e.g. an admission refusal)
        # keeps the gate armed; the consequence is a single spurious park that
        # the turn-start self-heal settles, not a permanent stick.
        bg_work: List[Dict[str, Any]] = []
        has_unresolved_launches = False
        try:
            from app.core.task_registry import task_registry

            bg_work = task_registry.get_active_background_work(sid)
            has_unresolved_launches = task_registry.has_unresolved_background_launches(sid)
        except Exception:
            logger.debug("SessionRuntime: park-gate read failed for %s", sid, exc_info=True)

        # Third arm: the durable mirror. The two arms above read the IN-MEMORY
        # registry, so a sub-agent whose record never landed there — or was
        # cleared out from under a live worker — would end the turn as a bare
        # ``end_turn`` with a worker still running.
        # ``kind == "agent"`` only, deliberately: a Monitor shell can be a
        # ``serves`` process meant to stay up, and parking on one of those
        # would pin the session forever — the same exclusion fluid makes.
        durable_agent_ids: List[str] = []
        if not (bg_work or has_unresolved_launches):
            try:
                from app.core import pending_reconcile

                durable_agent_ids = await asyncio.to_thread(
                    pending_reconcile.nonterminal_agent_task_ids, sid
                )
                if durable_agent_ids:
                    logger.info(
                        "SessionRuntime: parking %s on durable sub-agent work "
                        "the registry did not report: %s", sid, durable_agent_ids,
                    )
            except Exception:
                logger.debug(
                    "SessionRuntime: durable park-gate read failed for %s", sid,
                    exc_info=True,
                )

        task_ids = [w.get("id") for w in bg_work if w.get("id")]
        task_ids.extend(t for t in durable_agent_ids if t not in task_ids)
        # Re-check against durable truth in the same breath as the park: the
        # registry's running view can be a beat behind a completion
        # that has already written its row, and a park naming a task which
        # settled four seconds ago buys a no-op wake turn when its terminal
        # frame finally lands. Only ids with a durable TERMINAL row are
        # dropped — an id with NO row at all is a task too young to have been
        # persisted, not a settled one, so silence never narrows a park.
        if task_ids:
            try:
                from app.core import pending_reconcile

                settled_ids = await asyncio.to_thread(
                    pending_reconcile.settled_task_ids, sid, list(task_ids)
                )
                if settled_ids:
                    task_ids = [t for t in task_ids if t not in settled_ids]
                    logger.info(
                        "SessionRuntime: dropped %d already-settled task id(s) "
                        "from the park for %s: %s",
                        len(settled_ids), sid, sorted(settled_ids),
                    )
            except Exception:
                logger.debug(
                    "SessionRuntime: park settle re-check failed for %s", sid,
                    exc_info=True,
                )

        # Park only on work that is actually outstanding. A gate armed solely
        # by registry records whose durable rows have all settled would park on
        # nothing — the stuck-parked shape the launch-set arm was narrowed to
        # avoid. The unresolved-LAUNCH arm still parks with an empty list on
        # purpose: that is the launched-but-not-yet-registered race.
        # A turn aborted because recording failed never parks: the
        # transcript is holed and the row must settle, not wait on work.
        if (
            (task_ids or has_unresolved_launches)
            and self._abort_reason not in ("recording_failed", "user_stopped")
        ):
            parked = None
            try:
                parked = await asyncio.to_thread(turns_db.park_turn, tid, task_ids)
            except Exception:
                logger.warning("SessionRuntime: park_turn failed for %s", tid, exc_info=True)
            if parked is not None:
                # turn.parked event — only when the park actually happened.
                try:
                    await record_parked(sid, task_ids)
                except Exception:
                    logger.debug("SessionRuntime: record_parked failed for %s", sid, exc_info=True)
            return "parked", parked is not None

        # A runner that lost its lease settles the row only while it is still
        # bound to that run. Once another install's sweep requeued it (or a
        # new owner started it), the row is theirs and this settle is a no-op.
        owned_run = self._run_id if self._unowned else None
        if (self._abort_reason == "recording_failed"
                or self._terminal in ("interrupted", "error") or self._terminal is None):
            stop_reason = (self._abort_reason
                           if self._abort_reason in ("recording_failed", "lease_lost")
                           else self._terminal or self._abort_reason or "crash")
            settled = None
            try:
                # Record the pump's error text on the row (an 'error' terminal
                # carries the provider's own message) — a bare
                # stop_reason='error' with error=NULL would make every abort
                # undiagnosable from the turns table alone.
                settled = await asyncio.to_thread(
                    turns_db.settle_turn, tid, "aborted", stop_reason,
                    self._terminal_error, run_id=owned_run,
                )
            except Exception:
                logger.warning("SessionRuntime: settle_turn(aborted) failed for %s", tid, exc_info=True)
            # A turn that died on a provider error has to leave a DURABLE trace.
            # The error frame reaches the live WebSocket and the ``turns`` row,
            # but neither survives a reload, which would show the user's prompt
            # and then nothing at all. Worker failures already write a system
            # row; turn failures do too.
            if self._terminal == "error":
                await persist_turn_error_message(
                    sid, tid, self._terminal_error, source=self.source,
                )
            # A message this dying turn swallowed on its way out goes back on
            # the queue — BEFORE the scheduler seam fires, so it runs as the
            # very next turn.
            await self._requeue_stranded_steers(sid)
            # Working-set snapshot: a turn that ended (even
            # aborted) may leave in-progress edits the next runner needs. NEVER
            # on the parked arm above (the session is still working here).
            await self._snapshot_working_set(sid)
            return "aborted", settled is not None

        settled = None
        try:
            settled = await asyncio.to_thread(
                turns_db.settle_turn, tid, "completed", run_id=owned_run,
            )
        except Exception:
            logger.warning("SessionRuntime: settle_turn(completed) failed for %s", tid, exc_info=True)
        await self._snapshot_working_set(sid)
        return "completed", settled is not None

    async def _requeue_stranded_steers(self, sid: str) -> None:
        """Put back every steered message this turn swallowed but never answered.

        The race this repairs: the user sends a message mid-turn and hits stop a
        heartbeat later. The steer is already on its way to the engine, so the
        dying turn receipts it, records the ``message.user``, and retires the
        queue row — then aborts without a word in reply. The message ends up in
        the transcript permanently unanswered, which is precisely what the send
        flow promises cannot happen.

        Two ways a turn gets here, and they draw the line differently.

        The user pressed stop: the line is the interrupt REQUEST time
        (``_interrupt_requested_at``, stamped as ``interrupt()`` starts). A row
        absorbed BEFORE it was one the model was genuinely working on when the
        user chose to stop — re-running it would answer a question they
        withdrew. A row absorbed at-or-after it was swallowed by a turn that was
        already dying, and re-runs.

        The turn died on its own — a provider error, a crash. Nobody withdrew
        anything and there is no request time to compare against, so EVERY
        absorbed-and-unanswered row re-runs. Steering must never be worse than
        queueing: had the message stayed in the queue it would simply have run
        after the abort.

        Both lines are then crossed with one more fact: whether the model
        actually READ the interjection. A steer arrives folded into a tool result,
        so any model output after its absorb stamp means the model was round-
        tripped with the text in context and worked on it. Re-running such a row
        shows the model the same sentence twice — once on a tool result and
        again, minutes later, as a standalone user turn. A row the model
        demonstrably consumed stays consumed.

        The re-run carries ``user_message_already_recorded`` so it adds nothing
        to the transcript: the ``message.user`` is up there already, written by
        the receipt. It goes to the FRONT of the queue (the failover-retry
        placement) so it is the next thing the session does. Order among several
        re-queued rows is preserved by inserting them newest-first.

        Never raises — a turn that is already over must still settle.
        """
        requested = self._interrupt_requested_at
        absorbed = self._steer_absorbed
        self._steer_absorbed = {}
        if not absorbed:
            return
        cutoff = requested if requested is not None else float("-inf")
        answered = self._model_output_at
        stranded = sorted(
            (
                (stamp, row_id, row)
                for row_id, (stamp, row) in absorbed.items()
                if stamp >= cutoff
                and not (answered is not None and answered > stamp)
            ),
            key=lambda item: item[0],
        )
        if not stranded:
            return

        requeued = 0
        # ``at_front`` puts each new row before every queued one, so inserting
        # newest-first leaves the queue in the original order.
        for _stamp, row_id, row in reversed(stranded):
            payload = dict(row.get("payload") or {})
            # The message is already in both stores — the receipt wrote it. The
            # deferred-record marker would make the re-run write it a SECOND
            # time, so it goes and the suppress marker takes its place.
            payload.pop("deferred_user_record", None)
            payload["user_message_already_recorded"] = True
            payload["requeued_from_turn_id"] = row_id
            try:
                fresh = await asyncio.to_thread(
                    turns_db.enqueue_turn,
                    sid,
                    row.get("source") or "user",
                    prompt=row.get("prompt") or "",
                    profile_id=row.get("profile_id"),
                    project_id=row.get("project_id"),
                    worktree_id=row.get("worktree_id"),
                    overrides=row.get("overrides") or None,
                    payload=payload,
                    command_id=row.get("command_id"),
                    instance_id=row.get("instance_id"),
                    at_front=True,
                )
            except Exception:
                logger.warning(
                    "SessionRuntime: could not re-queue stranded steer %s on %s",
                    row_id, sid, exc_info=True,
                )
                continue
            requeued += 1
            logger.info(
                "SessionRuntime: steer %s was absorbed into %s but the turn "
                "aborted before answering — re-queued as %s",
                row_id, self.turn_id, fresh["id"],
            )
        if requeued:
            # The rows are pending again; every watching device should see that.
            await _broadcast_queue_after_start(sid)

    # ------------------------------------------------------------------
    # interrupt / escalation
    # ------------------------------------------------------------------

    async def interrupt(
        self,
        mode: str = "stop",
        *,
        escalation_timeout: Optional[float] = None,
    ) -> str:
        """React to this turn having been flipped to ``interrupting`` (the
        ``turns.interrupt_turn`` guarded transition ran at the command layer).

        Emits ``turn.interrupting``, signals the adapter via ``interrupt_session``,
        and waits for the turn to settle. On escalation timeout the
        ``_should_evict_after_escalation`` decision is kept, and the wedged
        pump is CANCELLED in BOTH
        branches, because it holds the reader token and nothing else can read
        until it is gone:

        * evict → evict the client + cancel the pump; ``execute()``'s finalize
          settles ``aborted`` (``stop_reason=interrupt_escalation``) and fires
          ``on_turn_settled`` — exactly once;
        * keep → ``escalate_to_draining`` + cancel the pump; ``execute()`` runs
          the drain reader under its still-live lease, and ``_settle_drained``
          settles + fires.

        Returns the expected resulting turn state
        (``completed|aborted|parked|draining``) — best-effort on the
        escalation paths, where settlement is asynchronous.

        Idempotent per turn: the durable ``interrupting`` flip can be
        observed by BOTH the in-process executor and the cross-runner
        turn_claim observe pass — the second driver must not re-signal the adapter
        or start a second escalation clock. The check-and-set below runs
        synchronously before the first ``await``, so two same-loop callers
        cannot both pass it.
        """
        sid = self.session_id
        tid = self.turn_id

        if self._interrupt_driving:
            return "interrupting"
        self._interrupt_driving = True
        # The moment the kill was asked for. Stamped BEFORE the first await, and
        # read against the stamps ``_on_steer_delivered`` writes: it separates a
        # message the model was already answering (absorbed earlier — the user
        # chose to stop it) from one this turn swallowed on its way out
        # (absorbed at-or-after — never answered, so it must run again).
        self._interrupt_requested_at = time.perf_counter()

        # The WebSocket that wrote the durable interrupt may live on another
        # install, where its in-memory task registry cannot reach this runtime's
        # workers. Honor the shared stop_background bit on the owning runner too.
        # The local WebSocket path also calls this eagerly for latency; stop-all is
        # deliberately idempotent, so the second call sees no active tasks.
        try:
            interrupted_row = await asyncio.to_thread(turns_db.get_turn, tid)
            if interrupted_row and interrupted_row.get("stop_background"):
                from app.core.query_session_lifecycle import stop_all_background_tasks

                # Same semantics as the WebSocket Stop that set the bit (round
                # 7): end ALL of the session's background work — sub-agents and
                # their descendants, provider-lane workers, Monitor shells
                # including serving tasks and watchers — and quiesce the session
                # so nothing wakes it until the user's next message. Running it
                # HERE as well as on the WebSocket is what makes that latch
                # cross-runner correct: the WebSocket may live on an install
                # whose in-memory registry cannot see this runner's tasks, and
                # this is the runner that actually owns them. Deliberately
                # idempotent — the second call finds no active tasks and simply
                # re-asserts the latch. See handle_stop's STOP SEMANTICS note.
                source = (interrupted_row.get("payload") or {}).get("interrupt_source", "user_stop")
                user_stop = source in {"user_stop", "user_stopped"}
                await stop_all_background_tasks(sid, user_stop=user_stop,
                                                **({"source": source} if not user_stop else {}))
                logger.info("SessionRuntime: background teardown for %s source=%s", sid, source)
        except Exception:
            logger.warning(
                "SessionRuntime: background-task stop failed for %s", sid,
                exc_info=True,
            )

        # A user Stop kills the agent AND its computer-use control together:
        # halt synthetic input for this session and drop its access grants, so
        # nothing the turn was doing can keep driving the screen. The
        # session's next turn re-arms (see execute). Steering ('send_now') is
        # not a Stop — control continues across the replacement query.
        if mode == "stop":
            try:
                from app.mcp.computer_use import abort, access

                abort.request_stop(sid)
                access.revoke(sid)
            except Exception:
                logger.warning(
                    "SessionRuntime: computer-use halt failed for %s", sid,
                    exc_info=True,
                )

        try:
            await record_interrupting(sid, mode)  # turn.interrupting event
        except Exception:
            logger.debug("SessionRuntime: record_interrupting failed for %s", sid, exc_info=True)

        # Stop must also release anything BLOCKED ON THE USER. A pending
        # tool-approval prompt is awaited inside a DETACHED task sitting in
        # ``interaction_wait.await_interaction``; the adapter interrupt below
        # cannot reach it, so without this the session keeps a live waiter (and
        # a prompt on screen) until the 5-minute TTL sweeps it — the turn looks
        # unstoppable. ``cancel_all_requests`` is the ONE correct unblock: it
        # flips each pending row via ``interactions.cancel_interaction`` and
        # then ``notify_changed``-es the session's waiters.
        try:
            await permission_handler.cancel_all_requests(sid)
        except Exception:
            logger.warning(
                "SessionRuntime: cancel_all_requests failed for %s", sid, exc_info=True
            )

        try:
            from app.core.query_session_lifecycle import interrupt_session

            await interrupt_session(sid)
        except Exception:
            logger.warning("SessionRuntime: interrupt_session raised for %s", sid, exc_info=True)

        timeout = escalation_timeout if escalation_timeout is not None else ESCALATION_TIMEOUT_SECONDS
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            turn = await asyncio.to_thread(turns_db.get_turn, tid)
            if turn is not None and turn["state"] in turns_db.TERMINAL_STATES:
                return turn["state"]
            await asyncio.sleep(_SETTLE_POLL_SECONDS)

        # Escalation: the turn did not settle — the runner is wedged. The wedged
        # pump holds the reader token, so it MUST be cancelled or the drain
        # reader deadlocks on the token while the lease heartbeat keeps
        # reap_expired at bay forever. Settlement (and
        # the on_turn_settled fire) stays with execute()'s finalize (evict) or
        # the drain reader (keep) — exactly once.
        if _should_evict_after_escalation(sid):
            self._abort_reason = "interrupt_escalation"
            await _evict_pooled_client(sid)
            self._cancel_pump()
            return "aborted"

        # Keep the healthy client: flip to draining, then let execute() run the
        # drain reader under its still-live lease.
        escalated = await asyncio.to_thread(turns_db.escalate_to_draining, tid)
        if escalated is None:
            # The turn settled (or was never interrupting) while we decided.
            turn = await asyncio.to_thread(turns_db.get_turn, tid)
            return turn["state"] if turn is not None else "aborted"
        self._drain_after_cancel = True
        self._cancel_pump()
        return "draining"

    def _on_recorder_recording_failed(self) -> None:
        """Stop generation without cancelling the task doing recorder cleanup."""
        if self._abort_reason == "recording_failed":
            return
        self._abort_reason = "recording_failed"
        self._drain_after_cancel = False
        task = self._pump_task
        if not self._recorder_closing and task is not asyncio.current_task():
            self._cancel_pump()
        try:
            from app.core.query_session_lifecycle import interrupt_session

            interrupt = asyncio.get_running_loop().create_task(interrupt_session(self.session_id))
            interrupt.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except Exception:
            logger.debug("SessionRuntime: interrupt after recording failure failed for %s",
                         self.session_id, exc_info=True)

    def _on_recorder_superseded(self) -> None:
        """The run lease moved to another runner mid-turn (fence violation).

        Runs once, synchronously on the loop, from the recorder's first
        rejected append.
        """
        logger.warning(
            "SessionRuntime: run superseded for %s turn %s (fence violation) — "
            "stopping pump",
            self.session_id, self.turn_id,
        )
        self._stop_unowned("fence_superseded")

    def _on_lease_lost(self) -> None:
        """The heartbeat could not keep this turn's run lease.

        Nothing holds the reap sweep off an unleased turn, and another runner
        may claim the session at any moment, so the turn stops rather than
        run on without an owner. The pump's own cancel path books usage once
        as an interrupted turn, and finalize settles the row ``aborted`` with
        ``stop_reason='lease_lost'``.
        """
        logger.warning(
            "SessionRuntime: lease lost for %s turn %s run %s — stopping the turn",
            self.session_id, self.turn_id, self._run_id,
        )
        self._stop_unowned("lease_lost")

    def _stop_unowned(self, reason: str) -> None:
        """Stop a turn this runner no longer owns: cancel the pump through the
        escalation cancel path (finalize then settles ``aborted`` with
        ``reason``) and ask the adapter to interrupt the turn so the pooled client
        stops generating for a run nobody owns."""
        sid = self.session_id
        self._unowned = True
        if self._abort_reason is None:
            self._abort_reason = reason
        self._cancel_pump()
        try:
            from app.core.query_session_lifecycle import interrupt_session

            loop = asyncio.get_running_loop()
            task = loop.create_task(interrupt_session(sid))
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )
        except Exception:  # noqa: BLE001 — best-effort
            logger.debug(
                "SessionRuntime: interrupt after %s failed for %s", reason, sid,
                exc_info=True,
            )

    def _cancel_pump(self) -> None:
        """Cancel the in-flight pump task, if any (escalation teardown)."""
        task = self._pump_task
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    def _maybe_pin_placement(
        session_id: str,
        runner_id: str,
        session_row: Optional[Dict[str, Any]],
    ) -> None:
        """Sticky-pin an "anywhere"/capability-placed session to this runner
        — sync, called via ``to_thread``.

        Pin rules:
          * NULL/blank placement  → leave alone (stays NULL; the resolver's
            ``instance_id`` fallback treats the row's ``instance_id`` as an implicit pin at
            read time, so no explicit pin is invented here).
          * ``runner:<id>`` pin   → leave alone (already pinned; the placement
            gate + resolver ensured it points here or we would not be running).
          * anything else ("anywhere", a capability token, bare constraints)
            → pin to ``runner_id`` via the dual-write helper. Any capability
            ``constraints`` are INTENTIONALLY dropped by the pin: a pin means
            "the CLI conversation state lives here", and pins are re-validated
            by online + capability only at resolve
            time — the constraint set has served its purpose (electing this
            winner) and carrying it forward would add nothing.
        ``session_row`` is the row the placement gate already fetched; when the
        gate failed open (row None) we refetch rather than skip, so a transient
        gate error does not silently strand an "anywhere" session unpinned.
        """
        from app.core import placement as placement_mod
        from app.db import database as database_mod

        row = session_row
        if row is None:
            row = database_mod.get_session(session_id)
            if row is None:
                return
        placement = placement_mod._normalize_placement(row.get("placement"))
        if not placement:
            # NULL placement stays NULL: never invent an explicit pin —
            # the resolver's instance_id fallback covers routing.
            return
        where = (placement.get("where") or "").strip()
        if where.startswith("runner:"):
            return  # already pinned — first claim happened long ago.
        from app.db import sessions as sessions_db

        if sessions_db.pin_session_placement(session_id, runner_id):
            logger.info(
                "SessionRuntime: sticky-pinned session %s to runner %s "
                "(was placement=%r)", session_id, runner_id, where or placement,
            )

    # ------------------------------------------------------------------
    # working-set sync
    # ------------------------------------------------------------------

    async def _snapshot_working_set(self, sid: str) -> None:
        """PUSH hook: snapshot this session's dirty working tree to its scratch
        ref at turn end. Best-effort — a git failure must NEVER fail or delay
        turn finalization beyond this call. Silent no-op when the session has
        no on-disk checkout here or a ``local:`` origin."""
        try:
            from app.core import project_resolver
            from app.core.working_set import snapshot_out
            from app.db import database as database_mod

            row = await asyncio.to_thread(database_mod.get_session, sid)
            if row is None:
                return
            stored = row.get("working_set_ref")
            if stored:
                # Restore leaves an unresolved foreign snapshot recoverable.
                # A turn on this dirty checkout must not replace that same
                # scratch ref with its own changes. Leave both sets intact.
                try:
                    snapshot = json.loads(stored)
                except (ValueError, TypeError):
                    snapshot = None
                if (
                    not isinstance(snapshot, dict)
                    or snapshot.get("runner_id") != settings.effective_instance_id
                ):
                    logger.info(
                        "SessionRuntime: preserving unresolved working-set snapshot for %s; local changes remain on disk",
                        sid,
                    )
                    return
            cwd = await asyncio.to_thread(
                project_resolver.resolve_existing_cwd, row
            )
            if not cwd:
                return
            is_local = self._is_local_origin(row)
            await asyncio.to_thread(
                snapshot_out, sid, Path(cwd),
                settings.effective_instance_id, is_local,
            )
        except Exception:
            logger.warning(
                "SessionRuntime: working-set snapshot failed for %s", sid,
                exc_info=True,
            )

    async def _restore_working_set(self, sid: str) -> None:
        """RESTORE hook: if a prior turn on another runner snapshotted this
        session's working set, apply it onto this runner's clean checkout and
        surface the resulting note as a system event. Best-effort.

        ALWAYS re-fetches the session row fresh (never trusts a row read before
        the run lease was claimed): a stale pre-claim read can MISS a snapshot
        committed between that read and the lease grant, or RE-APPLY an already-
        consumed ref value."""
        try:
            from app.core import project_resolver
            from app.core.working_set import restore_in
            from app.db import database as database_mod

            row = await asyncio.to_thread(database_mod.get_session, sid)
            if row is None:
                return
            stored = row.get("working_set_ref")
            if not stored:
                return
            cwd = await asyncio.to_thread(
                project_resolver.resolve_existing_cwd, row
            )
            if not cwd:
                return
            is_local = self._is_local_origin(row)
            note = await asyncio.to_thread(
                restore_in, sid, Path(cwd),
                settings.effective_instance_id, stored, is_local,
            )
            if note:
                from app.core.turn_recorder import record_drained_event
                from app.db import session_events
                import hashlib

                # An unresolved snapshot is deliberately retried every turn.
                # Keep its first warning in the durable transcript, without
                # appending it again on every retry (or runtime restart). A new
                # snapshot, destination, or recovery outcome merits a new note.
                notice_key = hashlib.sha256(json.dumps(
                    [stored, settings.effective_instance_id,
                     str(Path(cwd).resolve()), note],
                    ensure_ascii=True,
                ).encode()).hexdigest()
                try:
                    previous = await asyncio.to_thread(
                        session_events.get_latest_system_note, sid, "working_set"
                    )
                except Exception:
                    # A failed history lookup must not hide a recovery warning.
                    previous = None
                    logger.warning(
                        "SessionRuntime: working-set notice lookup failed for %s",
                        sid, exc_info=True,
                    )
                if (previous or {}).get("data", {}).get("notice_key") == notice_key:
                    return

                await record_drained_event(sid, {
                    "type": "system",
                    "subtype": "working_set",
                    "data": {"content": note, "notice_key": notice_key},
                })
        except Exception:
            logger.warning(
                "SessionRuntime: working-set restore failed for %s", sid,
                exc_info=True,
            )

    @staticmethod
    def _is_local_origin(session_row: Dict[str, Any]) -> bool:
        """Whether the session's project has a ``local:`` origin (no shared
        remote to sync a working set through). Pure read; False on any error so
        a resolution hiccup never masks a real remote."""
        try:
            from app.core import project_resolver

            project = project_resolver.resolve_project_v2(session_row)
            return bool(
                project
                and str(project.get("origin") or "").startswith("local:")
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def _reset_started_this_turn(session_id: str) -> None:
        try:
            from app.core.task_registry import task_registry

            task_registry.reset_started_this_turn(session_id)
        except Exception:
            logger.debug("SessionRuntime: reset_started_this_turn failed for %s", session_id, exc_info=True)

    async def _renew_loop(self, run_id: str, runner_id: str, fence_token: int) -> None:
        """Heartbeat the run lease every ``RUN_LEASE_RENEW_SECONDS`` until
        cancelled or the lease is lost.

        The lease is lost when a renewal misses its guard (expired, or the run
        was reaped or taken over) or when renewals kept failing for a whole
        TTL, which is when the DB lets the lease lapse. Either way the turn
        stops itself (``_on_lease_lost``) instead of running on unowned."""
        loop = asyncio.get_running_loop()
        renewed_at = loop.time()
        while True:
            await asyncio.sleep(RUN_LEASE_RENEW_SECONDS)
            attempt_at = loop.time()
            try:
                ok = await asyncio.to_thread(
                    runs_db.renew_run, run_id, runner_id, fence_token,
                    RUN_LEASE_TTL_SECONDS,
                )
            except Exception:
                if loop.time() - renewed_at < RUN_LEASE_TTL_SECONDS:
                    logger.warning(
                        "SessionRuntime: renew_run raised for run %s", run_id, exc_info=True,
                    )
                    continue
                logger.warning(
                    "SessionRuntime: renew_run kept failing for run %s past the "
                    "%ss lease TTL", run_id, RUN_LEASE_TTL_SECONDS, exc_info=True,
                )
                ok = False
            if ok:
                renewed_at = attempt_at
                continue
            logger.warning(
                "SessionRuntime: lease renewal lost for run %s — stopping heartbeat",
                run_id,
            )
            self._on_lease_lost()
            return


# Include turns still acquiring their lease, not just registered live runtimes.
_executions: Dict[asyncio.Task, str] = {}


def active_executions() -> Dict[asyncio.Task, str]:
    """Snapshot of turn executions owned by this process."""
    return dict(_executions)


async def execute(turn_row: Dict[str, Any], **kwargs: Any) -> Optional[str]:
    """Build and track a runtime through settlement and lease release."""
    from app.core import startup

    if startup.is_shutting_down:
        return None
    task = asyncio.current_task()
    _executions[task] = turn_row["session_id"]
    try:
        return await SessionRuntime(turn_row, **kwargs).execute()
    finally:
        _executions.pop(task, None)

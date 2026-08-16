"""app/core/session_runtime.py — the one spawn path (v2 core, Phase 2 A4).

A :class:`SessionRuntime` executes exactly one ``turns`` row end-to-end:

1. claim the fenced run lease (MANDATORY — no claim means the turn stays queued);
2. take the session's single live slot via ``turns.start_turn`` (loses cleanly);
3. reset per-turn task bookkeeping, acquire the per-session **reader token**, and
   stop any running idle reader (so this pump is the session's single reader);
4. build a :class:`~app.core.turn_recorder.TurnRecorder` bound to the INJECTED
   ``(run_id, fence_token, turn_id)`` (``create_for_run`` — no self-claim);
5. run the pump by WRAPPING ``query_engine._stream_to_websocket_impl`` — its
   internals are untouched; the recorder observes every yielded event (SSE is the
   delivery path) and a broadcast adapter carries the legacy WS frames;
6. **finalize** (always, in a ``finally``): decide ``completed | aborted |
   parked`` — the park gate is ``task_registry.get_active_background_work`` OR
   ``get_background_launches`` — settle/park the turn, release the reader token,
   and start the idle reader iff background work survives;
7. release the run lease in the outer ``finally`` and fire the injectable
   ``on_turn_settled`` scheduler seam (the real scheduler is commit A5) — but
   ONLY when finalize actually performed the DB transition (a racer that
   settled first already fired it). Special case: after an escalation-keep
   (``interrupt()`` flipped the turn to ``draining`` and cancelled the wedged
   pump), ``execute()`` runs the DRAIN reader itself, under its still-live
   lease, and the reader's ``_settle_drained`` settles + fires instead.

The single-reader invariant is STRUCTURAL: a per-session ``asyncio.Lock``
(the *reader token*) is acquired by both this pump and the idle reader
(:mod:`app.core.idle_reader`), so they can never read the pooled client
concurrently. There is deliberately NO ``deferred_delivery`` concept.

Nothing calls this module yet — the transport/caller wiring is commits A6/A7.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

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

# Lease timing — identical to the legacy TurnRecorder self-claim (Phase 1).
RUN_LEASE_TTL_SECONDS = 60
RUN_LEASE_RENEW_SECONDS = 20

# How long a runtime-driven interrupt waits for the turn to settle before it
# escalates (mirrors chat_ws_handler._INTERRUPT_DRAIN_TIMEOUT_SECONDS). Tests
# shrink it.
ESCALATION_TIMEOUT_SECONDS = 8.0
_SETTLE_POLL_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Reader token — per-session single-reader slot (Lock-guarded).
# ---------------------------------------------------------------------------
# The turn pump AND the idle reader both acquire the SAME per-session lock, so
# the single-reader invariant (only one coroutine reads the pooled SDK client at
# a time) is structural rather than enforced by call discipline. Kept here (not
# ws_cleanup._drain_pumps) so the runtime owns it.
_reader_locks: Dict[str, asyncio.Lock] = {}
# Running idle-reader task per session, so a starting turn can stop it first.
_idle_readers: Dict[str, asyncio.Task] = {}

# Live SessionRuntime per session — the command→runtime interrupt seam. A
# runtime registers itself here the instant it WINS ``start_turn`` (so it owns
# the SDK interrupt + evict/drain escalation), and self-guarded-pops in the
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


def get_reader_lock(session_id: str) -> asyncio.Lock:
    """Return (creating on first use) the session's reader-token lock.

    ``asyncio.Lock`` no longer binds to a loop at construction (3.10+), so lazy
    creation from any context is safe.
    """
    lock = _reader_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _reader_locks[session_id] = lock
    return lock


def start_idle_reader(session_id: str, *, drain_turn_id: Optional[str] = None) -> None:
    """Start the between-turns idle reader for a session (replacing any existing).

    ``drain_turn_id`` puts the reader in *drain mode*: a stale ``ResultMessage``
    for that turn settles it ``aborted`` (a wedged runner that was escalated to
    ``draining``). The reader acquires the reader token itself, so it can never
    race a turn pump.
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
# Scheduler seam — on_turn_settled (real scheduler is commit A5).
# ---------------------------------------------------------------------------
_on_turn_settled: Optional[Callable[..., Any]] = None


def register_scheduler(hook: Optional[Callable[[str, str, str], Any]]) -> None:
    """Register (or clear, with ``None``) the settlement callback.

    Called with ``(session_id, turn_id, outcome)`` after every settlement
    (completed/aborted/parked). Commit A5 registers the real scheduler; tests
    inject a spy. Default: no-op.
    """
    global _on_turn_settled
    _on_turn_settled = hook


async def _fire_on_turn_settled(session_id: str, turn_id: str, outcome: str) -> None:
    """Fire the registered settlement callback, if any. Never raises."""
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
# Broadcast adapter seam — the legacy-kept WS frames.
# ---------------------------------------------------------------------------
# Render frames ride the recorder → session event log → SSE. The remaining
# lifecycle/permission WS frames go through this adapter: to the origin socket
# when the turn's ``source`` is ``user`` and the socket is alive, plus a fan-out
# to the other registered devices via sync_engine. Injectable so the transport
# commit (A6) can override the exact wire mechanics.
_broadcaster: Optional[Callable[..., Awaitable[None]]] = None


def register_broadcaster(hook: Optional[Callable[..., Awaitable[None]]]) -> None:
    """Register (or clear) the broadcast adapter used for legacy WS frames."""
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
# Render-frame trim — faithful copy of chat_ws_handler (:72-94).
# ---------------------------------------------------------------------------
# Raw RENDER frames ride the recorder → session event feed (SSE); only the
# frames the frontend consumes for NON-render purposes may hit the WS/broadcast
# path, or render content is double-delivered. Copied (not imported) so the
# runtime never depends on the frozen legacy module; PR-B deletes the legacy
# copy, making this the single source.
_WS_TRIMMED_RENDER_FRAMES = frozenset({
    "chunk",
    "stream_delta",
    "stream_block_start",
    "stream_block_stop",
    "stream_message_delta",
    "task_progress",
    "system",
    "usage_update",
})
_WS_KEPT_SUBAGENT_FRAMES = frozenset({"subagent_tool_use", "subagent_tool_result"})


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
        # In-flight pump task — interrupt() escalation cancels it (legacy
        # task.cancel() parity): the pump holds the reader token, so nothing
        # else can read until it is gone.
        self._pump_task: Optional[asyncio.Task] = None
        # Set by interrupt()'s escalation-keep branch: execute() must run the
        # drain reader (under its still-live lease) instead of finalizing.
        self._drain_after_cancel = False
        # stop_reason override for an escalation-forced abort.
        self._abort_reason: Optional[str] = None
        # Idempotency guard for the interrupt-observe pass (V2 Phase 4, O4):
        # once interrupt() has been driven for THIS turn, later observers
        # (claim-loop pass, duplicate executor fire) are no-ops. Per-turn by
        # construction — a SessionRuntime instance executes exactly one turn
        # row, and the next turn gets a fresh instance with a fresh guard.
        self._interrupt_driving = False

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

        # 0. Placement gate (V2 Phase 4). A session explicitly placed on ANOTHER
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

        renew_task = asyncio.create_task(self._renew_loop(run_id, runner_id, fence_token))
        renew_task.add_done_callback(lambda t: t.cancelled() or t.exception())

        outcome: Optional[str] = None
        settled_here = False
        try:
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

            # Register as the session's live runtime the instant we own it, so
            # the command→runtime interrupt seam (turn_manager.interrupt →
            # boot-wired executor → get_live_runtime(sid).interrupt(mode)) can
            # reach us. Self-guarded pop in the outer finally.
            _live_runtimes[sid] = self

            logger.info(
                "SessionRuntime: started %s turn %s (source=%s)",
                sid, tid, self.source,
            )
            # The row has just left the queued set — fan the queue view out now
            # so Send-later chips clear on every watching device. Fire-and-
            # forget: nothing may add latency in front of the pump.
            broadcast_task = asyncio.create_task(_broadcast_queue_after_start(sid))
            broadcast_task.add_done_callback(lambda t: t.cancelled() or t.exception())

            # 3. Sticky pin (V2 Phase 4, OD-6/OD-9): the FIRST winner of an
            #    "anywhere"/capability placement pins the session to itself
            #    (placement + legacy instance_id dual-write) so every later
            #    turn lands on this runner's disk-resident CLI state. NULL
            #    placement stays NULL (the local-default, A9) and an existing
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

            # 3b. Working-set restore (V2 Phase 4, Q6): the session's dirty
            #     working tree may have been snapshotted to a scratch ref by
            #     a PRIOR turn on ANOTHER runner. Bring it onto this runner's
            #     clean checkout before the pump starts. Best-effort — a git
            #     failure must never delay/kill the turn.
            await self._restore_working_set(sid)

            # 4. Per-turn bookkeeping + acquire the reader token; stop any idle
            #    reader so this pump is the session's single reader.
            self._reset_started_this_turn(sid)
            await stop_idle_reader(sid)

            # 4b. LEG 2 self-heal: settle any STALE parked turn rows for this
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
            #     signal ``_cli_owns_at_rest_wake`` / ``_latest_context_turn``
            #     rely on — settling it early risks a duplicate wake and wrong
            #     context attribution. We do NOT also gate on unresolved launches:
            #     a launch that never materializes a registry record must not
            #     block the self-heal (that would re-introduce the stuck-parked
            #     stick this heal exists to clear). Best-effort: a settle failure
            #     must never delay/kill the turn.
            try:
                from app.core.task_registry import task_registry

                if not task_registry.has_active_background_work(sid):
                    settled = await asyncio.to_thread(
                        turns_db.settle_parked_turns, sid, tid
                    )
                    if settled:
                        logger.info(
                            "SessionRuntime: self-healed %d stale parked turn(s) "
                            "for %s at turn %s start", settled, sid, tid,
                        )
            except Exception:
                logger.warning(
                    "SessionRuntime: settle_parked_turns failed for %s", sid,
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

            # 6/7. Pump under the reader token, then ALWAYS finalize. A pump
            #    crash is absorbed (``_terminal`` stays ``None`` → finalize
            #    settles ``aborted``) rather than re-raised, so finalize AND the
            #    settlement callback still run. The pump runs as a CHILD task so
            #    interrupt() escalation can cancel it (legacy task.cancel()
            #    parity) without cancelling execute() itself.
            try:
                async with get_reader_lock(sid):
                    pump = asyncio.create_task(self._run_pump(recorder))
                    self._pump_task = pump
                    try:
                        await pump
                    finally:
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
        finally:
            # Deregister ourselves as the live runtime (self-guarded so a fresh
            # runtime that already took over the session is never evicted).
            if _live_runtimes.get(sid) is self:
                _live_runtimes.pop(sid, None)
            # Release the run lease + stop the heartbeat — always.
            try:
                await asyncio.to_thread(runs_db.release_run, run_id, fence_token)
            except Exception:
                logger.warning(
                    "SessionRuntime: release_run failed for %s run %s",
                    sid, run_id, exc_info=True,
                )
            renew_task.cancel()
            try:
                await renew_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

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
        the broadcast adapter carries the legacy WS frames. Never rebuilds the
        impl — it is called verbatim.
        """
        from app.core.query_engine import _stream_to_websocket_impl

        prompt = self.turn.get("prompt") or ""
        await recorder.start(prompt, profile_id=self.turn.get("profile_id"))
        try:
            async for event in _stream_to_websocket_impl(
                prompt=prompt,
                session_id=self.session_id,
                profile_id=self.turn.get("profile_id"),
                # The LEGACY project id deliberately stays live here — it still
                # keys the project-existence gate, the app-config/builtins MCP
                # servers, the DB project hooks, and agent-memory sync, none of
                # which are project_v2-keyed yet (OD-7 amended; that migration is
                # post-V2 backlog).
                project_id=self.turn.get("project_id"),
                overrides=self.turn.get("overrides") or None,
                broadcast_func=self._broadcast,
                api_user_id=self.api_user_id,
                user_id=self.user_id,
            ):
                await recorder.on_event(event)
                etype = event.get("type") if isinstance(event, dict) else None
                if etype in ("done", "interrupted", "error"):
                    self._terminal = etype
                # Render frames ride the recorder → SSE path; only the kept
                # lifecycle/tool frames hit the WS/broadcast leg (legacy :4155
                # parity — without the trim, render content double-delivers).
                if not _is_trimmed_render_frame(etype):
                    if isinstance(event, dict):
                        # FE cross-session guard tag (legacy :4148 parity).
                        event["session_id"] = self.session_id
                    await self._broadcast(event)
        finally:
            await recorder.close()

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
        # Absorb turn_finalize.persist_turn_end_state (durable parked marker +
        # unpark-latch backstop). Legacy stays intact — we CALL it.
        try:
            from app.core.turn_finalize import persist_turn_end_state

            await persist_turn_end_state(sid)
        except Exception:
            logger.debug("SessionRuntime: persist_turn_end_state failed for %s", sid, exc_info=True)

        # Turn-lifetime binding for ProviderAgent workers (multi-provider phase
        # 3, plan §2 C.4). This is the ONLY teardown in the runtime and it is
        # deliberately NARROW: it can only see workers that
        # ``provider_agent.dispatch_worker`` itself registered, so ordinary
        # CLI-native background sub-agents are untouched and the park gate below
        # keeps parking them exactly as before (RULING 2026-07-25 — that parking
        # is a feature, not a bug).
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

        # Park gate — the legacy predicate (chat_ws_handler :4461-4462), amended
        # 2026-08-08: the launch-set arm now counts only UNRESOLVED launches.
        # ``_background_launches`` itself stays CUMULATIVE and untouched (PR #765
        # ruling intact — the set is still cleared only by ``clear_session``);
        # only the GATE ignores launches whose task already reached a terminal
        # registry status. The prior form ORed the raw cumulative set, so a
        # launch left behind after its task finished re-parked EVERY subsequent
        # turn forever (``turn.parked task_ids=[]`` with zero running work — the
        # stuck-parked bug). Known benign residual: a launch that never
        # materializes a registry record at all (e.g. an admission refusal) keeps
        # the gate armed; the post-fix consequence is a single spurious park that
        # the turn-start self-heal settles, not a permanent stick.
        bg_work: List[Dict[str, Any]] = []
        has_unresolved_launches = False
        try:
            from app.core.task_registry import task_registry

            bg_work = task_registry.get_active_background_work(sid)
            has_unresolved_launches = task_registry.has_unresolved_background_launches(sid)
        except Exception:
            logger.debug("SessionRuntime: park-gate read failed for %s", sid, exc_info=True)

        if bg_work or has_unresolved_launches:
            task_ids = [w.get("id") for w in bg_work if w.get("id")]
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
            # Reader token already released (pump's ``async with`` exited); start
            # the idle reader to catch surviving background work's terminals.
            start_idle_reader(sid)
            return "parked", parked is not None

        if self._terminal in ("interrupted", "error") or self._terminal is None:
            stop_reason = self._terminal or self._abort_reason or "crash"
            settled = None
            try:
                settled = await asyncio.to_thread(
                    turns_db.settle_turn, tid, "aborted", stop_reason
                )
            except Exception:
                logger.warning("SessionRuntime: settle_turn(aborted) failed for %s", tid, exc_info=True)
            # Working-set snapshot (V2 Phase 4, Q6): a turn that ended (even
            # aborted) may leave in-progress edits the next runner needs. NEVER
            # on the parked arm above (the session is still working here).
            await self._snapshot_working_set(sid)
            return "aborted", settled is not None

        settled = None
        try:
            settled = await asyncio.to_thread(turns_db.settle_turn, tid, "completed")
        except Exception:
            logger.warning("SessionRuntime: settle_turn(completed) failed for %s", tid, exc_info=True)
        await self._snapshot_working_set(sid)
        return "completed", settled is not None

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

        Emits ``turn.interrupting``, signals the SDK via ``interrupt_session``,
        and waits for the turn to settle. On escalation timeout the legacy
        ``_should_evict_after_escalation`` decision is kept, and — legacy
        ``task.cancel()`` parity — the wedged pump is CANCELLED in BOTH
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

        Idempotent per turn (O4): the durable ``interrupting`` flip can be
        observed by BOTH the in-process executor and the cross-runner
        turn_claim observe pass — the second driver must not re-signal the SDK
        or start a second escalation clock. The check-and-set below runs
        synchronously before the first ``await``, so two same-loop callers
        cannot both pass it.
        """
        sid = self.session_id
        tid = self.turn_id

        if self._interrupt_driving:
            return "interrupting"
        self._interrupt_driving = True

        try:
            await record_interrupting(sid, mode)  # turn.interrupting event
        except Exception:
            logger.debug("SessionRuntime: record_interrupting failed for %s", sid, exc_info=True)

        # Stop must also release anything BLOCKED ON THE USER. A pending
        # tool-approval prompt is awaited inside a DETACHED SDK task sitting in
        # ``interaction_wait.await_interaction``; the SDK-level interrupt below
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
        # pump holds the reader token, so it MUST be cancelled (legacy
        # task.cancel() parity) or the drain reader deadlocks on the token while
        # the lease heartbeat keeps reap_expired at bay forever. Settlement (and
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
        (V2 Phase 4, OD-6) — sync, called via ``to_thread``.

        Pin rules (A9 + OD-6):
          * NULL/blank placement  → leave alone (stays NULL; the resolver's A11
            fallback treats the row's ``instance_id`` as an implicit pin at
            read time, so no explicit pin is invented here).
          * ``runner:<id>`` pin   → leave alone (already pinned; the placement
            gate + resolver ensured it points here or we would not be running).
          * anything else ("anywhere", a capability token, bare constraints)
            → pin to ``runner_id`` via the dual-write helper. Any capability
            ``constraints`` are INTENTIONALLY dropped by the pin (rF3): per
            OD-6 a pin means "the CLI conversation state lives here", and per
            A10 pins are re-validated by online + capability only at resolve
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
            # NULL placement stays NULL (A9): never invent an explicit pin —
            # the resolver's A11 instance_id fallback covers routing.
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
    # working-set sync (V2 Phase 4, Q6)
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

                await record_drained_event(sid, {
                    "type": "system",
                    "subtype": "working_set",
                    "data": {"content": note},
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
        cancelled (or the lease is lost)."""
        try:
            while True:
                await asyncio.sleep(RUN_LEASE_RENEW_SECONDS)
                try:
                    ok = await asyncio.to_thread(
                        runs_db.renew_run, run_id, runner_id, fence_token,
                        RUN_LEASE_TTL_SECONDS,
                    )
                except Exception:
                    logger.warning(
                        "SessionRuntime: renew_run raised for run %s", run_id, exc_info=True,
                    )
                    continue
                if not ok:
                    logger.warning(
                        "SessionRuntime: lease renewal lost for run %s — stopping heartbeat",
                        run_id,
                    )
                    return
        except asyncio.CancelledError:
            raise


async def execute(turn_row: Dict[str, Any], **kwargs: Any) -> Optional[str]:
    """Convenience: build a :class:`SessionRuntime` and run its ``execute``."""
    return await SessionRuntime(turn_row, **kwargs).execute()

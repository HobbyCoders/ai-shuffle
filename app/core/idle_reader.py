"""app/core/idle_reader.py — the between-turns single reader (v2 core, Phase 2 A4).

When a turn parks (its foreground stream ended but background sub-agents / shells
are still running), the pooled SDK client keeps buffering messages that nobody is
reading: the slow sub-agent's inner activity and, eventually, its terminal
``TaskNotificationMessage``. This module is the reader that runs BETWEEN turns to
consume those messages so they are not lost.

It is a trimmed port of the legacy ``chat_ws_handler._drain_notifications_impl``,
keeping only the v2 essentials:

* **registry taps** — every message is fed to ``task_registry`` (this is what
  unfreezes the running-task indicator) via ``_record_subagent_lane_launches`` +
  ``_record_task_message``;
* **recorder taps** — a slow sub-agent's inner activity, parked task-status
  chips, and a CLI-owned wake turn's top-level events are re-dispatched to the
  session event log via :func:`turn_recorder.record_drained_event` (the sole v2
  render path — there is no live-store re-dispatch);
* **terminal handoff** — a terminal ``TaskNotificationMessage`` is recorded and
  then handed to the injectable :func:`register_on_task_terminal` callback (the
  Phase-2 scheduler, commit A5, registers the real one; default is a no-op).
  This REPLACES the legacy ``mark_unparked`` / ``deliver_unpark`` latch machinery;
* **drain mode** — when started with a ``drain_turn_id`` (an escalated interrupt
  whose wedged runner was kept, not evicted), a stale ``ResultMessage`` for that
  turn settles it ``aborted`` and fires the settlement callback;
* **broken-client eviction** — a dead read stream evicts the pooled client with
  the SAME predicate the legacy pump used.

SINGLE-READER INVARIANT: the reader acquires the session's **reader token**
(:func:`app.core.session_runtime.get_reader_lock`) for its whole run, exactly as
a turn pump does — so a starting turn (which stops the idle reader AND takes the
token) can never read the pooled client concurrently with this reader.

Deliberately NOT ported (see the A4 brief): the ``stale_turn_undrained`` flag
dance, the batch/consolidation window, ``suppress_queue_kick``, the legacy
unpark-pending latch, live-store re-dispatch, and the self-removal-before-kick
self-deadlock guard (v2 settlement/kick is the scheduler's job via the seams).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Dict, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# Terminal statuses that constitute "this background task is done" — mirrors the
# legacy _TERMINAL_TASK_STATUSES.
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "killed", "stopped", "cancelled"})

# How long a single read may block before the loop wakes to re-check whether it
# should still be reading (background work resolved, or a drain-mode client is
# now safe to evict). Tests shrink it.
IDLE_READ_POLL_SECONDS = 2.0

# Sentinel returned by ``turn_scheduler.on_task_terminal`` (the terminal-task
# hook) when it SUPPRESSES the app's own unpark wake because the CLI owns this
# completion's at-rest wake and will deliver it itself. On seeing it the reader
# arms a ``cli_wake_grace_seconds`` deadline so it keeps draining until that
# CLI wake turn lands instead of exiting on its next poll.
CLI_OWNED_WAKE_SIGNAL = "cli-owned-wake"


# ---------------------------------------------------------------------------
# Terminal-task handoff seam (the real handler is commit A5's scheduler).
# ---------------------------------------------------------------------------
_on_task_terminal: Optional[Callable[..., Any]] = None


def register_on_task_terminal(hook: Optional[Callable[[str, Dict[str, Any]], Any]]) -> None:
    """Register (or clear, with ``None``) the terminal-task callback.

    Called ``(session_id, frame)`` when a background task's terminal
    notification is drained. Commit A5's scheduler registers the real handler
    (which decides whether to wake the parent with a follow-up turn); tests
    inject a spy. Default: no-op — the completion is still recorded to the event
    log regardless.
    """
    global _on_task_terminal
    _on_task_terminal = hook


async def _fire_on_task_terminal(session_id: str, frame: Dict[str, Any]) -> Any:
    """Fire the terminal-task callback, if any, and RETURN its result. Never
    raises. The scheduler returns ``CLI_OWNED_WAKE_SIGNAL`` when it suppresses
    the app wake in favor of the CLI's own delayed wake; the reader uses that to
    arm its grace window. Returns ``None`` when there is no hook or it raised."""
    hook = _on_task_terminal
    if hook is None:
        return None
    try:
        result = hook(session_id, frame)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except Exception:  # noqa: BLE001
        logger.warning(
            "idle_reader: on_task_terminal hook failed for %s", session_id, exc_info=True
        )
        return None


# ---------------------------------------------------------------------------
# Eviction (same predicate as the legacy pump).
# ---------------------------------------------------------------------------

async def _evict_broken_client(session_id: str) -> None:
    """Evict a wedged pooled client: ``remove_dead`` if the subprocess is gone,
    else ``remove_broken`` (subprocess alive but its read loop is unusable).
    Never raises."""
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
        logger.debug("idle_reader: evict failed for %s", session_id, exc_info=True)


# ---------------------------------------------------------------------------
# CLI-wake turn.completed usage summary.
# ---------------------------------------------------------------------------

def _wake_usage_summary(message: Any) -> Dict[str, Any]:
    """Best-effort usage summary for a CLI wake turn's ``turn.completed``.

    Mirrors the shape the fenced recorder derives from the pump's ``done``
    metadata (``tokens_in``/``tokens_out``/cache tokens/``total_cost_usd``/
    ``num_turns``) so a wake turn's usage renders consistently. Reads the
    ``ResultMessage``'s cumulative ``usage`` dict + ``total_cost_usd``; returns
    ``{}`` when nothing is available (the reducer keeps prior usage). Never
    raises.
    """
    summary: Dict[str, Any] = {}
    try:
        usage = getattr(message, "usage", None)
        if isinstance(usage, dict):
            summary["tokens_in"] = usage.get("input_tokens", 0)
            summary["tokens_out"] = usage.get("output_tokens", 0)
            summary["cache_creation_tokens"] = usage.get("cache_creation_input_tokens", 0)
            summary["cache_read_tokens"] = usage.get("cache_read_input_tokens", 0)
        cost = getattr(message, "total_cost_usd", None)
        if cost is not None:
            summary["total_cost_usd"] = cost
        num_turns = getattr(message, "num_turns", None)
        if num_turns is not None:
            summary["num_turns"] = num_turns
    except Exception:  # noqa: BLE001
        return {}
    return summary


# ---------------------------------------------------------------------------
# The reader.
# ---------------------------------------------------------------------------

async def run_idle_reader(session_id: str, *, drain_turn_id: Optional[str] = None) -> None:
    """Drain the pooled SDK client between turns for ``session_id``.

    Holds the session's reader token for the whole run (single-reader invariant).
    In *drain mode* (``drain_turn_id`` set) a stale ``ResultMessage`` for that
    turn settles it ``aborted``. Returns when there is nothing left to read, the
    stream ends, or (drain mode) the wedged client is safe to evict. Never raises
    out to the caller — a reader failure must never break the process.
    """
    from app.core import session_runtime

    # Mark the pooled client as held by an active between-turns reader BEFORE
    # anything can await, so the pool's LRU/idle eviction guards skip this
    # session for the reader's whole run. Without it, the CLI-wake grace window
    # is an eviction hazard: is_streaming is False and background work is gone
    # while the reader waits (up to cli_wake_grace_seconds) for the CLI's own
    # wake turn — and the reader reads via peek (no LRU promote), so
    # last_activity never bumps. The flag lives on the pool's SessionState and
    # is cleared in the finally below, so a crashed/cancelled reader can never
    # leave it stuck. peek (not get): marking must not perturb LRU order.
    pool_state = None
    try:
        from app.core.session_pool import get_session_pool

        pool_state = get_session_pool().peek(session_id)
    except Exception:  # noqa: BLE001
        pool_state = None
    if pool_state is not None:
        pool_state.idle_reader_active = True

    lock = session_runtime.get_reader_lock(session_id)
    try:
        async with lock:
            await _drain(session_id, drain_turn_id=drain_turn_id)
    except asyncio.CancelledError:
        # Stopped by SessionRuntime.stop_idle_reader (a turn is starting) — exit
        # quietly; the turn owns the client now.
        logger.debug("idle_reader: cancelled for %s", session_id)
        raise
    except Exception:  # noqa: BLE001
        logger.warning("idle_reader: run failed for %s", session_id, exc_info=True)
    finally:
        if pool_state is not None:
            pool_state.idle_reader_active = False
        session_runtime._drop_idle_reader_self(session_id)


async def _drain(session_id: str, *, drain_turn_id: Optional[str]) -> None:
    """The read loop (reader token already held)."""
    from app.core.query_engine import (
        ResultMessage,
        TaskNotificationMessage,
        TaskUpdatedMessage,
        _cli_turn_events_from_message,
        _record_subagent_lane_launches,
        _record_task_message,
        _subagent_events_from_message,
        _task_notification_frame,
        _task_status_events_from_message,
        _task_update_terminal_status,
    )
    from app.core.provider.claude_adapter import to_provider_event
    from app.core.session_pool import get_session_pool, is_subprocess_alive
    from app.core.task_registry import task_registry
    from app.core.turn_recorder import (
        record_cli_wake_event,
        record_cli_wake_turn_completed,
        record_cli_wake_turn_started,
        record_drained_event,
    )

    pool = get_session_pool()
    state = pool.peek(session_id)  # peek = no LRU promote
    if state is None or not state.adapter.is_alive() or state.is_streaming:
        logger.info(
            "idle_reader: no drainable client (state=%s alive=%s streaming=%s) [session=%s]",
            state is not None,
            bool(state and state.adapter.is_alive()),
            bool(state and state.is_streaming),
            session_id,
        )
        return
    client = state.client
    if client is None:
        # Fluid/harness adapters expose no SDK client, so there is no
        # receive_messages() stream to drain between turns. Porting the tail to
        # the fluid engine is a known follow-up; but a DRAINING turn must still
        # be settled here. Returning without settling used to leave the turn
        # 'draining' after execute() released its run lease — 15s later
        # ``reap_expired`` requeued the user turn and the scheduler RE-RAN it
        # from scratch: the interrupted prompt was re-recorded (duplicate user
        # bubble) and re-executed (Task workers re-spawned) on a session the UI
        # already showed as stopped. Mirror the drain-mode semantics instead:
        # wait for the wedge (the registered background work) to resolve, then
        # evict the client and settle the turn aborted.
        if drain_turn_id is not None:
            from app.core.session_runtime import _should_evict_after_escalation

            while not _should_evict_after_escalation(session_id):
                await asyncio.sleep(IDLE_READ_POLL_SECONDS)
            logger.info(
                "idle_reader: fluid drain wedge resolved — evicting + settling "
                "turn %s aborted [session=%s]", drain_turn_id, session_id,
            )
            await _evict_broken_client(session_id)
            await _settle_drained(session_id, drain_turn_id)
            return
        logger.debug(
            "idle drain skipped: adapter has no sdk client (fluid) [session=%s]",
            session_id,
        )
        return

    logger.info(
        "idle_reader: reading pooled client between turns [session=%s drain_turn=%s]",
        session_id, drain_turn_id,
    )

    agen = client.receive_messages()
    # PERSISTENT READ FUTURE (F1, ported): ONE future per __anext__ kept alive
    # across timeouts via ``asyncio.wait`` (which never cancels it). Cancelling a
    # pending __anext__ throws into the generator frame, closing it permanently —
    # so the loop NEVER cancels ``next_fut``; only the exit ``finally`` does.
    _cli_tool_names: Dict[str, str] = {}
    # The currently-open synthetic CLI-wake turn id (None = no wake turn open).
    # Set when the first renderable event of a CLI-owned wake turn is forwarded
    # (bracketing turn.started); cleared when that wake's ResultMessage lands
    # (bracketing turn.completed). Persists across loop iterations because a
    # wake turn spans several drained messages.
    wake_turn_id: Optional[str] = None
    # CLI-owned-wake grace deadline (monotonic seconds). Armed when the terminal
    # handoff returns CLI_OWNED_WAKE_SIGNAL: the last background task completed,
    # the app's wake was suppressed, and the CLI will deliver its own wake turn
    # 7-60s from now. Until this deadline the reader keeps draining even though
    # has_active_background_work is False, so it captures that wake instead of
    # exiting on its next poll and leaving the wake frames buffered forever.
    # Refreshed on every frame while waiting; cleared when the wake turn closes.
    wake_deadline: Optional[float] = None
    next_fut: Optional[asyncio.Future] = None
    try:
        while True:
            if next_fut is None:
                next_fut = asyncio.ensure_future(agen.__anext__())
            done, _ = await asyncio.wait({next_fut}, timeout=IDLE_READ_POLL_SECONDS)
            if not done:
                # Periodic wake — decide whether to keep reading.
                if drain_turn_id is not None:
                    # Drain mode: once the background work that made us KEEP the
                    # wedged client has resolved (or the subprocess died), evict
                    # the now-idle/broken client, settle the drained turn, exit.
                    from app.core.session_runtime import _should_evict_after_escalation

                    if _should_evict_after_escalation(session_id):
                        logger.info(
                            "idle_reader: drain wedge resolved — evicting + settling "
                            "turn %s aborted [session=%s]", drain_turn_id, session_id,
                        )
                        await _evict_broken_client(session_id)
                        await _settle_drained(session_id, drain_turn_id)
                        return
                    continue
                # Normal idle: keep reading while background work survives; once
                # it is all done there is nothing more to drain — UNLESS a
                # CLI-owned wake is still in flight (LEG 1). The last background
                # task's completion suppressed the app wake because the CLI
                # delivers its own wake turn 7-60s later; exiting now would leave
                # that wake buffered and unread. Keep reading if (i) a CLI-wake
                # turn is currently OPEN on the client, or (ii) a grace deadline
                # is armed and unexpired.
                if not task_registry.has_active_background_work(session_id):
                    if wake_turn_id is not None:
                        if wake_deadline is None or time.monotonic() < wake_deadline:
                            continue
                        logger.warning(
                            "idle_reader: CLI-wake grace expired mid-wake (turn %s "
                            "open, no frames) — closing wake turn + exiting "
                            "[session=%s]", wake_turn_id, session_id,
                        )
                        # The synthetic wake turn is still OPEN with no frames.
                        # Returning here without closing it strands the FE
                        # streaming forever AND leaves the parked row stuck — the
                        # exact bug class this reader exists to prevent. Run the
                        # SAME closure the ResultMessage branch does: close the
                        # wake turn (terminal), persist end-of-turn state, then
                        # guardedly settle stale parked rows.
                        await _close_open_wake_turn(
                            session_id, wake_turn_id,
                            stop_reason="cli_wake_grace_expired",
                        )
                        return
                    if wake_deadline is not None:
                        if time.monotonic() < wake_deadline:
                            continue
                        logger.info(
                            "idle_reader: CLI-wake grace expired with no wake — "
                            "exiting [session=%s]", session_id,
                        )
                        return
                    logger.info(
                        "idle_reader: no background work remains — exiting [session=%s]",
                        session_id,
                    )
                    return
                continue

            _fut, next_fut = next_fut, None
            try:
                # Translate at the boundary: this loop reads the raw SDK client
                # (receive_messages()), but every helper it shares with the
                # streaming loops now dispatches on the provider mirror types
                # (app/core/provider/events.py).
                message = to_provider_event(_fut.result())
            except StopAsyncIteration:
                # The SDK generator ended — a finished stream cannot serve another
                # turn. In drain mode the killed turn's ResultMessage will now
                # never arrive: settle it aborted. Evict the finished client.
                logger.info("idle_reader: message stream ended [session=%s]", session_id)
                if drain_turn_id is not None:
                    await _settle_drained(session_id, drain_turn_id)
                await _evict_broken_client(session_id)
                return
            except Exception:  # noqa: BLE001
                # Read error — the client's read loop is dead, so the client is
                # broken by definition (same predicate as the legacy pump). In
                # drain mode settle the drained turn; evict either way.
                logger.warning(
                    "idle_reader: read failed — evicting broken client [session=%s]",
                    session_id, exc_info=True,
                )
                if drain_turn_id is not None:
                    await _settle_drained(session_id, drain_turn_id)
                await _evict_broken_client(session_id)
                return

            # A frame arrived while a CLI-wake grace window is armed — the wake
            # is materializing, so push the deadline out. (The wake's own
            # ResultMessage clears it below once the wake turn closes.)
            if wake_deadline is not None:
                wake_deadline = time.monotonic() + settings.cli_wake_grace_seconds

            # --- registry taps (unfreeze the indicator) -----------------------
            # Lane-launch recorder FIRST so a grandchild run_in_background tool_use
            # arriving between turns is marked before its TaskStarted is processed.
            try:
                _record_subagent_lane_launches(session_id, message)
                _record_task_message(session_id, message)
            except Exception as exc:  # noqa: BLE001
                logger.debug("idle_reader: registry tap failed: %s", exc)

            # --- slow sub-agent inner activity → event log --------------------
            # Only when parent_tool_use_id resolves to a launch the PARENT issued
            # (a grandchild's inner activity carries the sub-agent's launch id).
            try:
                inner_parent = getattr(message, "parent_tool_use_id", None)
                if inner_parent and task_registry.was_background_launch(session_id, inner_parent):
                    # B-prime item b: this drained message is inner activity of a
                    # still-running own background sub-agent (an owner). Stamp its
                    # activity so the deferred-pump discriminator can tell a running
                    # owner (keeps emitting) from a parked one (goes silent).
                    task_registry.note_owner_activity(session_id, inner_parent)
                    for sub_ev in _subagent_events_from_message(message):
                        await record_drained_event(session_id, sub_ev)
            except Exception as exc:  # noqa: BLE001
                logger.debug("idle_reader: subagent re-dispatch failed: %s", exc)

            # --- parked task-status chip → event log --------------------------
            try:
                status_evs = _task_status_events_from_message(message)
                if status_evs:
                    tu_tid = getattr(message, "task_id", None)
                    if tu_tid and task_registry.is_own_background_task(session_id, tu_tid):
                        for status_ev in status_evs:
                            await record_drained_event(session_id, status_ev)
            except Exception as exc:  # noqa: BLE001
                logger.debug("idle_reader: task-status chip failed: %s", exc)

            # --- CLI-owned wake forwarding → event log ------------------------
            # A top-level backgrounded Task/Agent coming to rest while parked makes
            # the CLI start its OWN follow-up turn on the pooled client; this reader
            # is its only reader. Forward that wake turn's top-level events so an
            # already-open client renders them live (else they reach only the SDK
            # transcript, surfacing on reload — issue #691). Route through
            # record_cli_wake_event, which — unlike record_drained_event's narrow
            # subagent_*/task_*/system filter — accepts the full render surface
            # (chunk/tool_use/tool_result), and BRACKET the wake as a first-class
            # turn so the FE reducer flips its streaming indicator and settles
            # speculative subagent cards on completion.
            #
            # Only bracket in NORMAL mode: in drain mode (drain_turn_id set) the
            # reader is aborting a wedged turn, not surfacing a fresh wake — keep
            # the pre-existing narrow record_drained_event behavior there so a
            # dangling turn.started can never land on an aborting drain.
            try:
                cli_evs = _cli_turn_events_from_message(session_id, message, _cli_tool_names)
                if cli_evs and drain_turn_id is None:
                    recorded = 0
                    dropped: Dict[str, int] = {}
                    for cli_ev in cli_evs:
                        etype = cli_ev.get("type")
                        if etype == "thinking_block":
                            # Never rendered live (surfaces on reload via the SDK
                            # transcript); skip WITHOUT counting it as a drop.
                            continue
                        # Open the wake turn on its first renderable event so
                        # turn.started precedes any assistant.text_delta.
                        if wake_turn_id is None:
                            wake_turn_id = f"cliwake-{uuid.uuid4()}"
                            await record_cli_wake_turn_started(session_id, wake_turn_id)
                            # An open wake must always have a live deadline
                            # bounding it (a CLI that hangs mid-wake sends no
                            # frames, so nothing else would ever expire it).
                            if wake_deadline is None:
                                wake_deadline = (
                                    time.monotonic() + settings.cli_wake_grace_seconds
                                )
                        n = await record_cli_wake_event(
                            session_id, cli_ev, turn_id=wake_turn_id
                        )
                        if n:
                            recorded += n
                        else:
                            dropped[etype] = dropped.get(etype, 0) + 1
                    logger.info(
                        "idle_reader: cli-wake forward [session=%s turn=%s "
                        "recorded=%d dropped=%s decision=cli-wake-record]",
                        session_id, wake_turn_id, recorded, dropped,
                    )
                elif cli_evs:
                    # Drain mode: preserve the narrow pre-fix recording.
                    for cli_ev in cli_evs:
                        if cli_ev.get("type") == "thinking_block":
                            continue
                        await record_drained_event(session_id, cli_ev)
            except Exception as exc:  # noqa: BLE001
                logger.debug("idle_reader: CLI-turn forward failed: %s", exc)

            # --- ResultMessage: drain-mode settlement -------------------------
            if isinstance(message, ResultMessage):
                if drain_turn_id is not None:
                    logger.info(
                        "idle_reader: consumed drained turn's ResultMessage — "
                        "settling %s aborted [session=%s]", drain_turn_id, session_id,
                    )
                    await _settle_drained(session_id, drain_turn_id)
                    return
                # A CLI-owned wake turn came to rest. If we bracketed a wake turn
                # (its first renderable event opened one), close it with a
                # synthetic turn.completed so the FE reducer flips streaming off
                # and settles speculative subagent cards (#691 symptom 2). Then
                # persist end-of-turn state so park-pollers render the wake, and
                # keep draining for any further background work.
                if wake_turn_id is not None:
                    await record_cli_wake_turn_completed(
                        session_id, wake_turn_id,
                        usage=_wake_usage_summary(message),
                    )
                    wake_turn_id = None
                    # The CLI-owned wake we were holding the grace window for has
                    # landed and closed — disarm it (LEG 1).
                    if wake_deadline is not None:
                        logger.info(
                            "idle_reader: CLI-wake captured — disarming grace "
                            "window [session=%s]", session_id,
                        )
                        wake_deadline = None
                try:
                    from app.core.turn_finalize import persist_turn_end_state

                    await persist_turn_end_state(session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("idle_reader: CLI-turn finalize failed: %s", exc)
                # LEG 2 call site A: the CLI-owned wake has come to rest. If no
                # background work remains, settle this session's parked turn rows
                # to 'completed' so lifecycle_state stops showing the session
                # parked forever ('parked' is terminal and nothing else moves a
                # row out of it). Guarded on has_active_background_work: a session
                # with live work is legitimately still parked — leave its rows.
                try:
                    if not task_registry.has_active_background_work(session_id):
                        from app.db import turns as turns_db

                        settled = await asyncio.to_thread(
                            turns_db.settle_parked_turns, session_id
                        )
                        if settled:
                            logger.info(
                                "idle_reader: settled %d parked turn(s) after "
                                "CLI-wake at rest [session=%s]", settled, session_id,
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("idle_reader: settle_parked_turns failed: %s", exc)
                # Capture the SDK context-usage breakdown for this CLI-owned wake
                # turn while its adapter is still live and pooled. Detached and
                # self-guarding, so it never delays draining.
                from app.core.query_persistence import schedule_context_usage_capture

                schedule_context_usage_capture(session_id, log_prefix="[Drain] ")
                continue

            # --- killed/stopped TaskUpdated-alone terminal --------------------
            # A killed/stopped background task can terminate via a TaskUpdatedMessage
            # ALONE (no following notification). Record the completion + hand off.
            if (
                not isinstance(message, TaskNotificationMessage)
                and isinstance(message, TaskUpdatedMessage)
            ):
                u_status = _task_update_terminal_status(message)
                if u_status not in _TERMINAL_TASK_STATUSES:
                    continue
                u_task_id = getattr(message, "task_id", None)
                # Only real tracked tasks (a phantom top-level self-rest has no record).
                u_record = task_registry.get_task(session_id, u_task_id)
                if u_record is None:
                    continue
                # NOTE: the generic own-background chip branch above already
                # recorded the task_updated chip for this message (turn_recorder
                # enriches its tool_use_id from the registry) — do NOT record a
                # second one here. Hand the task's LAUNCH tool_use_id from the
                # registry record to the scheduler so the synthesized wake prompt's
                # <tool-use-id> is populated, instead of the old hardcoded None.
                _signal = await _fire_on_task_terminal(session_id, {
                    "task_id": u_task_id,
                    "status": u_status,
                    "tool_use_id": u_record.get("toolUseId"),
                })
                if _signal == CLI_OWNED_WAKE_SIGNAL:
                    wake_deadline = time.monotonic() + settings.cli_wake_grace_seconds
                    logger.info(
                        "idle_reader: arming CLI-wake grace window %.1fs [session=%s]",
                        settings.cli_wake_grace_seconds, session_id,
                    )
                continue

            # --- terminal TaskNotificationMessage → record + handoff ----------
            if not isinstance(message, TaskNotificationMessage):
                continue
            frame = _task_notification_frame(message)
            status = frame.get("status")
            if status not in _TERMINAL_TASK_STATUSES:
                continue
            tool_use_id = frame.get("tool_use_id")
            # Suppress the chip ONLY when the task is POSITIVELY subagent-owned:
            # a grandchild's terminal notification must not surface a "Task
            # Updated" chip in the parent chat. But an UNKNOWN task (no lane
            # armed either way) keeps the pre-existing always-chip behavior — the
            # terminal chip is a settle path for subagent cards, so an
            # ownership-arming MISS must not leave a card stuck-active
            # (#617/#659 failure class).
            n_task_id = frame.get("task_id")
            if tool_use_id and not (
                task_registry.is_subagent_background_task(session_id, n_task_id)
                or task_registry.was_subagent_background_launch(
                    session_id, tool_use_id
                )
            ):
                try:
                    await record_drained_event(session_id, {
                        "type": "task_notification",
                        "task_id": n_task_id,
                        "tool_use_id": tool_use_id,
                        "status": status,
                        "usage": frame.get("usage"),
                        "summary": frame.get("summary"),
                    })
                except Exception as exc:  # noqa: BLE001
                    logger.debug("idle_reader: notification record failed: %s", exc)
            logger.info(
                "idle_reader: terminal task notification [task=%s status=%s session=%s]",
                frame.get("task_id"), status, session_id,
            )
            # Hand the terminal off to the scheduler seam (default no-op). This
            # REPLACES the legacy unpark latch/deliver machinery.
            _signal = await _fire_on_task_terminal(session_id, frame)
            if _signal == CLI_OWNED_WAKE_SIGNAL:
                # The CLI owns this completion's at-rest wake and delivers it
                # 7-60s later. Arm the grace window so we keep draining until it
                # lands instead of exiting on the next poll (LEG 1).
                wake_deadline = time.monotonic() + settings.cli_wake_grace_seconds
                logger.info(
                    "idle_reader: arming CLI-wake grace window %.1fs [session=%s]",
                    settings.cli_wake_grace_seconds, session_id,
                )
    finally:
        # Exit cleanup (return/cancel/error): cancel the read future WE own so no
        # detached __anext__ keeps running against the client; consume a completed
        # future's result so nothing logs "exception never retrieved".
        if next_fut is not None:
            if not next_fut.done():
                next_fut.cancel()
            else:
                try:
                    next_fut.result()
                except BaseException:  # noqa: BLE001
                    pass


async def _settle_drained(session_id: str, turn_id: str) -> None:
    """Settle a drained turn ``aborted`` and fire the settlement callback.

    Idempotent: ``turns.settle_turn`` no-ops (returns ``None``) for an
    already-terminal turn, in which case the callback does NOT fire again.
    """
    from app.core import session_runtime
    from app.db import turns as turns_db

    try:
        settled = await asyncio.to_thread(
            turns_db.settle_turn, turn_id, "aborted", "drain_stale_result"
        )
    except Exception:  # noqa: BLE001
        logger.warning("idle_reader: settle_turn(aborted) failed for %s", turn_id, exc_info=True)
        return
    if settled is not None:
        await session_runtime._fire_on_turn_settled(session_id, turn_id, "aborted")


async def _close_open_wake_turn(
    session_id: str,
    wake_turn_id: str,
    *,
    usage: Optional[Dict[str, Any]] = None,
    stop_reason: Optional[str] = None,
) -> None:
    """Close an OPEN CLI-wake turn with the SAME terminal sequence the drain
    loop's ResultMessage branch runs: (1) a synthetic ``turn.completed`` so the
    FE reducer flips streaming off and settles speculative cards, (2) persist
    end-of-turn state so park-pollers render it, (3) a guarded settle of stale
    ``parked`` rows (only when no background work remains — a session with live
    work is legitimately still parked). Best-effort throughout — a failure in
    any step must never propagate into the reader's exit path."""
    from app.core.turn_recorder import record_cli_wake_turn_completed

    await record_cli_wake_turn_completed(
        session_id, wake_turn_id, usage=usage, stop_reason=stop_reason,
    )
    try:
        from app.core.turn_finalize import persist_turn_end_state

        await persist_turn_end_state(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("idle_reader: CLI-turn finalize failed: %s", exc)
    try:
        from app.core.task_registry import task_registry

        if not task_registry.has_active_background_work(session_id):
            from app.db import turns as turns_db

            settled = await asyncio.to_thread(
                turns_db.settle_parked_turns, session_id
            )
            if settled:
                logger.info(
                    "idle_reader: settled %d parked turn(s) after closing open "
                    "wake turn [session=%s]", settled, session_id,
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("idle_reader: settle_parked_turns failed: %s", exc)

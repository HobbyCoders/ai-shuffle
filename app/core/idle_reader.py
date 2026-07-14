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
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Terminal statuses that constitute "this background task is done" — mirrors the
# legacy _TERMINAL_TASK_STATUSES.
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "killed", "stopped", "cancelled"})

# How long a single read may block before the loop wakes to re-check whether it
# should still be reading (background work resolved, or a drain-mode client is
# now safe to evict). Tests shrink it.
IDLE_READ_POLL_SECONDS = 2.0


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


async def _fire_on_task_terminal(session_id: str, frame: Dict[str, Any]) -> None:
    """Fire the terminal-task callback, if any. Never raises."""
    hook = _on_task_terminal
    if hook is None:
        return
    try:
        result = hook(session_id, frame)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001
        logger.warning(
            "idle_reader: on_task_terminal hook failed for %s", session_id, exc_info=True
        )


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
        _task_status_event_from_notification,
        _task_status_events_from_message,
        _task_update_terminal_status,
    )
    from app.core.session_pool import get_session_pool, is_subprocess_alive
    from app.core.task_registry import task_registry
    from app.core.turn_recorder import record_drained_event

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
                # it is all done there is nothing more to drain.
                if not task_registry.has_active_background_work(session_id):
                    logger.info(
                        "idle_reader: no background work remains — exiting [session=%s]",
                        session_id,
                    )
                    return
                continue

            _fut, next_fut = next_fut, None
            try:
                message = _fut.result()
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
            # is its only reader, so record its top-level events (record_drained_event
            # filters to subagent_*/task_*/system).
            try:
                cli_evs = _cli_turn_events_from_message(session_id, message, _cli_tool_names)
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
                # A CLI-owned wake turn came to rest — persist end-of-turn state so
                # park-pollers render the CLI's wake, then keep draining for any
                # further background work.
                try:
                    from app.core.turn_finalize import persist_turn_end_state

                    await persist_turn_end_state(session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("idle_reader: CLI-turn finalize failed: %s", exc)
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
                if task_registry.get_task(session_id, u_task_id) is None:
                    continue
                try:
                    for chip_ev in _task_status_event_from_notification(message):
                        await record_drained_event(session_id, chip_ev)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("idle_reader: killed-task chip failed: %s", exc)
                await _fire_on_task_terminal(session_id, {
                    "task_id": u_task_id,
                    "status": u_status,
                    "tool_use_id": None,
                })
                continue

            # --- terminal TaskNotificationMessage → record + handoff ----------
            if not isinstance(message, TaskNotificationMessage):
                continue
            frame = _task_notification_frame(message)
            status = frame.get("status")
            if status not in _TERMINAL_TASK_STATUSES:
                continue
            tool_use_id = frame.get("tool_use_id")
            if tool_use_id:
                try:
                    await record_drained_event(session_id, {
                        "type": "task_notification",
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
            await _fire_on_task_terminal(session_id, frame)
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

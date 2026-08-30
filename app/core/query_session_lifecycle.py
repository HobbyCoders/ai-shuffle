"""
Session lifecycle queries: state inspection, interrupt, active/streaming lists.

Extracted from query_engine.py to isolate session lifecycle management.
"""

import asyncio
import logging
from typing import Optional

from app.core.session_pool import SessionState, get_session_pool

logger = logging.getLogger(__name__)

# A busy CLI (e.g. deep in extended thinking) can leave the interrupt control
# request unanswered indefinitely; an unbounded await here hangs the two-phase
# "Send now" handshake past the frontend's confirm window and loses the
# user's message. Bounded so the caller can fall back to drain/force-cancel.
_SDK_INTERRUPT_TIMEOUT_SECONDS = 5.0


def get_session_state(session_id: str) -> Optional[SessionState]:
    """Get the active session state for a session ID."""
    return get_session_pool().peek(session_id)


def _reap_task_killed(session_id: str, task_id: str) -> None:
    """Reap a ghost task to "killed", attributing shutdown-origin reaps.

    A reap that runs while the server is tearing down is a consequence of the
    restart, not a task failure — stamp the wire record accordingly (item 4a)
    so the UI/parent can say "Server restarted — background task stopped"
    instead of presenting a mysterious kill. This site cannot otherwise know
    the reap's origin, so it gates on ``startup.is_shutting_down``.
    """
    from app.core import startup
    from app.core.task_registry import (
        task_registry,
        SERVER_SHUTDOWN_SUMMARY,
        SERVER_SHUTDOWN_REASON,
    )

    if getattr(startup, "is_shutting_down", False):
        task_registry.complete_task(
            session_id,
            task_id,
            "killed",
            summary=SERVER_SHUTDOWN_SUMMARY,
            reason=SERVER_SHUTDOWN_REASON,
        )
    else:
        task_registry.complete_task(session_id, task_id, "killed")


def _mark_task_stopped_by_user(session_id: str, task_id: str) -> None:
    """Mark a background task ``killed`` in the registry on an EXPLICIT user stop.

    Called after the SDK ``stop_task`` control request is issued for a LIVE
    background sub-agent. Records the terminal transition (with user attribution)
    directly instead of relying on a CLI ``stopped`` frame — the row must reach a
    terminal status exactly once even when no such frame flows back. ``killed``
    (not ``completed``) so the durable retention window shows the deliberate stop
    and the guaranteed terminal-surface callback fires (it is gated on
    error/killed). Idempotent via ``complete_task`` + ``mark_terminal_notified``.
    """
    from app.core.task_registry import (
        task_registry,
        USER_STOP_SUMMARY,
        USER_STOP_REASON,
    )

    task_registry.complete_task(
        session_id,
        task_id,
        "killed",
        summary=USER_STOP_SUMMARY,
        reason=USER_STOP_REASON,
    )


async def interrupt_session(session_id: str) -> bool:
    """Interrupt an active streaming session.

    This calls the SDK's interrupt() method which signals the Claude API
    to stop processing. This is more reliable than just cancelling the
    asyncio task, which only takes effect at the next await point.
    """
    # Use peek() to avoid promoting to MRU — interrupt is a read-only check
    state = get_session_pool().peek(session_id)
    if not state:
        logger.warning(f"No active session found for {session_id}")
        return False

    if not state.is_connected:
        logger.warning(f"Session {session_id} is not connected")
        return False

    # Set interrupt flag first - this is checked in the streaming loop as a failsafe
    state.interrupt_requested = True

    # Don't check is_streaming too strictly - there might be race conditions
    # and it's better to try to interrupt anyway
    logger.debug(f"Attempting to interrupt session {session_id} (streaming={state.is_streaming})")

    # Run the SDK interrupt on a DETACHED task guarded by a SHIELD: on timeout we
    # must NOT cancel the in-flight interrupt() — cancelling it mid-flight leaks the
    # entry in the SDK client's pending_control_responses (the control request was
    # sent but its response future is abandoned), which can wedge the client for
    # every subsequent control call. Shielding lets the wait_for time out while the
    # underlying interrupt keeps running to completion in the background.
    # Interrupt via the ADAPTER interface: state.client resolves through the
    # legacy-only ``sdk_client`` attribute and raises AttributeError on the
    # fluid HarnessAdapter. ClaudeAgentAdapter.interrupt() delegates to the
    # same sdk_client.interrupt(), so flag-off behavior is unchanged.
    interrupt_task = asyncio.create_task(state.adapter.interrupt())
    # Attach the swallow/log done-callback AT CREATION (F8): if the CALLER of
    # this coroutine is cancelled while awaiting the shielded wait below, the
    # except branches never run and a later interrupt() failure would surface
    # as an "exception was never retrieved" event-loop warning. Registering
    # the retriever up-front covers every exit path (retrieving .exception()
    # here does not interfere with the awaits below).
    interrupt_task.add_done_callback(
        lambda t: t.cancelled() or t.exception()
    )
    try:
        await asyncio.wait_for(asyncio.shield(interrupt_task), timeout=_SDK_INTERRUPT_TIMEOUT_SECONDS)
        # Don't set is_streaming = False here. The streaming loop will set it
        # when it actually finishes processing final messages. Setting it
        # prematurely causes a race where the loop still processes messages
        # but the session appears idle.
        logger.debug(f"Successfully interrupted session {session_id}")
        return True
    except asyncio.TimeoutError:
        # Leave the detached interrupt running; the done-callback attached at
        # creation retrieves (and swallows) its eventual result/exception so
        # the event loop never logs an "exception was never retrieved" warning.
        # interrupt_requested stays set — the streaming loop's failsafe check
        # can still end the turn, and the caller's drain/force-cancel handles
        # the truly wedged case.
        logger.warning(
            f"Interrupt control request for session {session_id} unanswered after "
            f"{_SDK_INTERRUPT_TIMEOUT_SECONDS}s (CLI busy or wedged)"
        )
        return False
    except Exception as e:
        logger.error(f"Failed to interrupt session {session_id}: {e}", exc_info=True)
        return False


async def _durable_force_settle(session_id: str, task_id: str) -> Optional[dict]:
    """Force-settle a task from DURABLE truth when the registry is empty.

    Post-restart the in-memory registry holds nothing, so a task the UI still
    shows "running" cannot be reaped through the registry. Settle it from what
    survived the restart and, crucially, emit the terminal ``task.updated`` frame
    the frontend replays to settle the chip/card (a silent DB flip leaves the UI
    stuck "running" forever). Emission routes through the shared
    ``record_task_settle_frame`` emitter — the single source of truth every
    terminal-settle producer uses.

    Idempotent across repeated stop clicks: guarded by the same
    ``task_registry.mark_chipped(..., "terminal")`` once-per-task latch the
    terminal-surface path uses, so a second call emits nothing and returns
    ``{"ok": True, ..., "detail": "already settled"}``.

    Order of durable truth:

    1. ``background_tasks`` row → flip to ``killed`` if still non-terminal; emit
       the terminal frame carrying the row's actual terminal status (so an
       already-terminal row settles the UI to the truth). Returns ok.
    2. else a ``subagent.started`` event for this ``task_id`` → emit the killed
       frame with ``tool_use_id == task_id`` (subagent cards join on the launch
       task_id). Returns ok.
    3. else → ``None`` (caller reports the honest dead-session failure).
    """
    from app.db.background_tasks import (
        TERMINAL_STATUSES,
        get_background_task,
        mark_background_task_killed,
    )
    from app.db.session_events import has_subagent_started
    from app.core.task_registry import task_registry
    from app.core.turn_recorder import record_task_settle_frame

    row = await asyncio.to_thread(get_background_task, task_id, session_id)
    if row is not None:
        status = row.get("status")
        kind = row.get("kind") or "agent"
        tool_use_id = row.get("tool_use_id")
        is_terminal = status in TERMINAL_STATUSES
        # Once-per-task-lifetime latch (shared with the terminal-surface path):
        # repeated stop clicks after a restart must settle the UI exactly once.
        if not task_registry.mark_chipped(session_id, task_id, "terminal"):
            return {
                "ok": True,
                "kind": kind,
                "task_id": task_id,
                "detail": "already settled",
            }
        emit_status = status if is_terminal else "killed"
        if not is_terminal:
            await asyncio.to_thread(mark_background_task_killed, task_id, session_id)
        await record_task_settle_frame(
            session_id, task_id, emit_status,
            "Background task stopped (stale)",
            reason="stale_reap", tool_use_id=tool_use_id,
        )
        return {
            "ok": True,
            "kind": kind,
            "task_id": task_id,
            "detail": "reaped (stale row)",
        }

    if await asyncio.to_thread(has_subagent_started, session_id, task_id):
        if not task_registry.mark_chipped(session_id, task_id, "terminal"):
            return {
                "ok": True,
                "kind": "agent",
                "task_id": task_id,
                "detail": "already settled",
            }
        await record_task_settle_frame(
            session_id, task_id, "killed",
            # We cannot know a restart happened (sibling _reap_task_killed gates
            # restart wording on startup.is_shutting_down for the same reason).
            "Subagent stopped (stale)",
            reason="stale_reap",
            # Subagent cards settle on tool_use_id == their launch task_id.
            tool_use_id=task_id,
        )
        return {
            "ok": True,
            "kind": "agent",
            "task_id": task_id,
            "detail": "reaped (orphan subagent)",
        }

    return None


async def stop_session_task(session_id: str, task_id: str) -> dict:
    """Stop ONE background task (sub-agent OR Monitor shell) in a session.

    Two kinds of task surface in the UI's ``activeTasks`` strip:

    - **shell** (Monitor subprocess): terminated directly via its tracked
      process and marked ``killed`` in the registry immediately.
    - **agent** (background sub-agent): stopped through the SDK client's
      ``stop_task()``. The CLI then emits a ``task_notification`` with status
      ``stopped`` which the streaming loop maps to ``killed`` in the registry —
      so the task drops off the next ``/live`` poll without us pre-marking it.
      A PROVIDER-LANE worker is an agent task too, but it has no SDK task to
      stop — it runs as its own session under the ProviderAgent supervisor, so
      it is torn down there instead (checked before the SDK path).

    "Ghost" tasks — ones already terminated by outside forces (idle eviction,
    process death, session teardown) but still parked ``running`` in the
    registry with no live client left to stop — are REAPED here: marked
    ``killed`` so they drop off the next ``/live`` poll, and reported as a
    successful stop. Without this the "x" hits a dead session, returns
    ``ok: false``, and the UI strip is left showing a task that can never be
    cleared (its button stuck disabled).

    Returns ``{"ok": bool, "kind": ..., "detail"?: ...}``. Never raises.
    """
    from app.core.task_registry import task_registry

    # Idempotent / stale-UI guard: if the registry already holds this task in a
    # terminal state, the strip is just showing a stale row — report success so
    # the "x" clears it on the next poll instead of erroring.
    existing = task_registry.get_task(session_id, task_id)
    if existing is not None and existing.get("status") != "running":
        return {
            "ok": True,
            "kind": existing.get("kind") or "agent",
            "task_id": task_id,
            "detail": "already finished",
        }

    # 1) Monitor shell task — terminate the subprocess directly.
    from app.mcp.builtin_tools import stop_background_shell_task
    shell = await stop_background_shell_task(task_id, session_id=session_id)
    if shell is not None:
        # ``survivors`` means descendants outlived the shell (a dev server the
        # command started, typically). The row is settled either way, but the
        # stop did not do what it says on the tin, so don't report ok.
        return {"ok": not shell.get("survivors"), "kind": "shell", **shell}

    # A registry shell task with no tracked subprocess is a ghost (the shell
    # already exited without the registry being told). Reap it rather than
    # mis-routing a shell id to the SDK's agent-task stop (which would error).
    if existing is not None and existing.get("kind") == "shell":
        _reap_task_killed(session_id, task_id)
        return {
            "ok": True,
            "kind": "shell",
            "task_id": task_id,
            "detail": "reaped (shell gone)",
        }

    # 2) ProviderAgent worker — a provider-lane subagent has no SDK task to
    # stop: it is its OWN session, tracked by the supervisor and keyed by
    # (parent session id, worker session id). Its task id IS the worker session
    # id (``dispatch_worker`` sets both), so the registry key the strip's "x"
    # sends is exactly the supervisor key. ``teardown_worker`` is the single
    # per-worker kill path — it settles the card, kills the worker session,
    # sweeps its parent-keyed approval prompts, completes the registry row and
    # purges the park-gate residue.
    from app.core import provider_agent_supervisor
    from app.core.task_registry import USER_STOP_REASON, USER_STOP_SUMMARY

    record = provider_agent_supervisor.get_worker(session_id, task_id)
    if record is not None:
        await provider_agent_supervisor.teardown_worker(
            record,
            reason=USER_STOP_REASON,
            summary=USER_STOP_SUMMARY,
            preview="Stopped by the user.",
        )
        return {"ok": True, "kind": "agent", "task_id": task_id}

    # 3) Background sub-agent — ask the SDK to stop it.
    state = get_session_pool().peek(session_id)
    # Fluid/harness adapters expose no SDK client at all, so the SDK stop_task
    # path is simply unavailable there. Treat that exactly like a torn-down
    # client (reap the registry row / report honestly) instead of letting the
    # missing attribute blow up the stop request.
    client = state.client if state is not None else None
    if state is not None and state.is_connected and client is None:
        logger.warning(
            "stop_task unavailable: no sdk client on this adapter "
            f"[session={session_id} task={task_id}]"
        )
    if not state or not state.is_connected or not client:
        # Outside forces tore down the live session / SDK client, but the task
        # can still be parked "running" in the registry — a ghost the UI shows
        # forever with no client left to stop it. Reap it so the close button
        # works: mark it killed (drops off the next /live poll) and report
        # success.
        if existing is not None:
            _reap_task_killed(session_id, task_id)
            return {
                "ok": True,
                "kind": existing.get("kind") or "agent",
                "task_id": task_id,
                "detail": "reaped (session gone)",
            }
        # The registry never knew the task — but after a restart the in-memory
        # registry is EMPTY, so a task the UI still shows "running" has no
        # registry row to reap even though the durable truth (a background_tasks
        # row, or a subagent.started event in the log) is still rendering it.
        # Force-settle from that durable truth before reporting the dead session.
        reaped = await _durable_force_settle(session_id, task_id)
        if reaped is not None:
            return reaped
        # Nothing durable to settle and no live session — there is nothing to
        # reap and nothing the UI is showing. Report the dead session honestly
        # (distinguishing gone-from-pool vs disconnected).
        if not state:
            return {"ok": False, "kind": "agent", "detail": "session not active"}
        return {"ok": False, "kind": "agent", "detail": "session not connected"}

    try:
        await client.stop_task(task_id)
        logger.debug(f"Requested SDK stop_task for {task_id} in session {session_id}")
        # Record the terminal transition on the registry row OURSELVES rather than
        # waiting for a CLI "stopped" frame to flow back through the streaming loop.
        # The historical design relied on that frame reaching
        # ``query_engine._record_task_message`` — but a parked session (its turn's
        # receive_response already returned) has no in-stream reader, and the
        # scoped ``stop_task`` may emit no terminal frame at all, so the row stayed
        # ``running`` forever (frozen "phantom running" agent until restart). This
        # mirrors the shell / ghost-reap branches above, which already mark the
        # registry immediately. Idempotent: any later CLI ``stopped`` frame that
        # DOES arrive re-completes to the same terminal state as a harmless no-op,
        # and the guaranteed terminal-surface callback fires exactly once
        # (``mark_terminal_notified`` latch), so this never double-surfaces.
        _mark_task_stopped_by_user(session_id, task_id)
        return {"ok": True, "kind": "agent", "task_id": task_id}
    except Exception as e:
        logger.error(
            f"Failed to stop task {task_id} in session {session_id}: {e}",
            exc_info=True,
        )
        return {"ok": False, "kind": "agent", "detail": str(e)}


async def stop_all_background_tasks(
    session_id: str, *, include_shells: bool = True
) -> list[dict]:
    """Stop EVERY in-flight background task in a session, one at a time.

    Used by the normal Stop path: after the main turn is interrupted, iterate
    the running-tasks view and drive each through
    the existing per-task ``stop_session_task`` (sub-agents via the SDK client's
    ``stop_task``, Monitor shells via their tracked subprocess). Reuses that path
    verbatim — no re-implementation of client stop logic — so ghost-reaping and
    idempotency behave identically to a single "x" click.

    ``include_shells=False`` spares Monitor shells whose subprocess is still
    alive — the Stop-button semantics, documented at that call site: Stop ends
    the TURN, and a dev server the user asked to keep up is not part of the
    turn. Ghost shell rows (registry says running, process already gone) are
    still swept, so nothing strands in the UI. Force paths (session delete,
    eviction reap) don't come through here at all — they use
    ``reap_session_background_shells``, which is unchanged.

    Snapshots the active list first so mutations during iteration (a task
    dropping to a terminal state as it is stopped) don't disturb the loop.
    Returns one result dict per task attempted, plus a ``skipped`` entry for
    each spared shell. Never raises.
    """
    from app.core.task_registry import task_registry

    active = task_registry.get_active(session_id)
    task_ids = [task.get("id") for task in active if task.get("id")]

    spared: list[dict] = []
    if not include_shells:
        from app.mcp.builtin_tools import is_background_shell_running

        keep = {
            task.get("id")
            for task in active
            if task.get("kind") == "shell"
            and task.get("id")
            and is_background_shell_running(task["id"])
        }
        if keep:
            task_ids = [tid for tid in task_ids if tid not in keep]
            spared = [
                {"ok": True, "kind": "shell", "task_id": tid, "skipped": "left running"}
                for tid in sorted(keep)
            ]
            logger.info(
                f"Stop for session {session_id} left {len(keep)} running "
                f"Monitor shell(s) alive: {sorted(keep)}"
            )

    # Close completion-vs-Stop races BEFORE the first await: every later terminal
    # frame is dropped at the scheduler entry point, and a wake queued a moment
    # before these latches is removed (or rebuilt without the canceled tasks).
    for task_id in task_ids:
        task_registry.suppress_wake(session_id, task_id)
    if task_ids:
        from app.core import turn_scheduler

        turn_scheduler.discard_queued_task_wakes(session_id, task_ids)

    results: list[dict] = list(spared)
    for task_id in task_ids:
        results.append(await stop_session_task(session_id, task_id))
    return results


def get_active_sessions() -> list:
    """Get list of active session IDs (connected clients)"""
    return get_session_pool().active_session_ids()


def get_streaming_sessions() -> list:
    """Get list of currently streaming session IDs"""
    return get_session_pool().streaming_session_ids()

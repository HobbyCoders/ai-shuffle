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


def _mark_task_stopped_by_user(session_id: str, task_id: str, *, source: str = "user_stop") -> None:
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
        summary=USER_STOP_SUMMARY if source == USER_STOP_REASON else f"Stopped by host: {source}",
        reason=source,
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
    # fluid HarnessAdapter. All active providers implement interrupt().
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
            from app.core.task_persistence import broadcast_running_task_presence
            await broadcast_running_task_presence([session_id])
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


async def stop_session_task(session_id: str, task_id: str, *, source: str = "user_stop") -> dict:
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

    # 1) Monitor shell task — terminate the subprocess directly. Attributed as
    # a user stop so the terminal chip reads "Stopped by user" instead of the
    # blank bubble an unattributed shell kill used to produce; an attribution
    # already on the row (a monitor timeout, say) still wins.
    from app.mcp.builtin_tools import stop_background_shell_task
    from app.core.task_registry import USER_STOP_REASON, USER_STOP_SUMMARY

    shell = await stop_background_shell_task(
        task_id,
        session_id=session_id,
        summary=USER_STOP_SUMMARY if source == USER_STOP_REASON else f"Stopped by host: {source}",
        reason=source,
    )
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

    record = provider_agent_supervisor.get_worker(session_id, task_id)
    if record is not None:
        await provider_agent_supervisor.teardown_worker(
            record,
            reason=source,
            summary=USER_STOP_SUMMARY if source == USER_STOP_REASON else f"Stopped by host: {source}",
            preview="Stopped by the user." if source == USER_STOP_REASON else f"Stopped by host: {source}",
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
        _mark_task_stopped_by_user(session_id, task_id, source=source)
        return {"ok": True, "kind": "agent", "task_id": task_id}
    except Exception as e:
        logger.error(
            f"Failed to stop task {task_id} in session {session_id}: {e}",
            exc_info=True,
        )
        # A FAILED stop still has to leave a terminal row (087d3809 F2). The
        # control request is the only thing that failed here — the caller
        # (Stop, an interrupt, the strip's "x") is tearing the session's work
        # down either way, and a client that refused ``stop_task`` is usually
        # a client that is already being evicted. Leaving the row ``running``
        # is what produced the audited corpse: no terminal SubagentEvent, no
        # notification, ``terminal_notified=false``, and a model that kept
        # reporting the dead worker as its outstanding delegated task for
        # four and a half minutes. The shell branch has always marked its row
        # unconditionally; this removes the asymmetry.
        _mark_task_stopped_by_user(session_id, task_id, source=source)
        return {"ok": False, "kind": "agent", "task_id": task_id, "detail": str(e)}


async def enter_user_stop(session_id: str) -> dict:
    """Quiesce a session the user just stopped — everything BESIDES killing tasks.

    STOP SEMANTICS (round 7). Stop means: everything this session is doing ends
    now, and nothing restarts it until the user sends a new message. Killing the
    background tasks (``stop_all_background_tasks``, which calls this first) is
    only half of that — a stopped session still had four ways to come back to
    life on its own, and all four are closed here:

    1. **The session-wide stop latch.** ``task_registry.mark_user_stopped``
       gates every wake producer at its entry point, so work the per-task
       suppression could not have known about (a nested worker registered a
       beat later, a late frame for a task the registry never saw) cannot
       schedule a turn either. Set FIRST, before any await.
    2. **Armed wakeups.** Every pending ``wakeups`` row for the session is
       deleted and its local timer disarmed. Deleting the row — not just
       disarming — is what makes this cross-runner correct: a peer install's
       timer finds no row to claim and no-ops.
    3. **Queued turns.** The settle path drains the queue on EVERY outcome,
       including a user abort, so a turn sitting behind the stopped one started
       the instant Stop landed and the session was visibly alive again one
       second after the button. They are aborted here through the same guarded
       transition a dropped queue chip uses (``remove_queued_turn`` ->
       ``aborted``/``removed_from_queue``): the rows stay in history, a turn
       that raced to live is left alone by the guard, and a message the user
       still wants is one they re-send.
    4. **Stale parked rows.** With all background work dead, any ``parked`` turn
       is residue that would otherwise keep ``lifecycle_state`` reporting the
       session busy until some later turn's self-heal cleared it.

    Both the WebSocket Stop handler AND the owning runner's durable
    ``stop_background`` path route through here, so the latch lands on whichever
    install actually holds the session's work. Idempotent and best-effort: every
    leg is independently guarded, and a failure in one never skips the others.
    Returns a small counts dict for the log/tests.
    """
    from app.core.task_registry import task_registry

    # (1) FIRST, and synchronously — before any await — so a terminal frame
    #     racing this call is already looking at a stopped session.
    first = task_registry.mark_user_stopped(session_id)

    wakeups_cancelled = 0
    try:
        from app.core import wakeup_service

        wakeups_cancelled = await wakeup_service.cancel_session_wakeups_async(
            session_id
        )
    except Exception:  # noqa: BLE001 — one leg failing must not skip the rest.
        logger.warning(
            "Stop: cancelling wakeups failed for session %s", session_id,
            exc_info=True,
        )

    queued_aborted = 0
    try:
        from app.db import turns as turns_db

        queued = await asyncio.to_thread(turns_db.get_queued_turns, session_id)
        for turn in queued:
            turn_id = turn.get("id")
            if not turn_id:
                continue
            removed = await asyncio.to_thread(turns_db.remove_queued_turn, turn_id)
            if removed is not None:
                queued_aborted += 1
    except Exception:  # noqa: BLE001
        logger.warning(
            "Stop: aborting queued turns failed for session %s", session_id,
            exc_info=True,
        )

    parked_settled = 0
    try:
        from app.db import turns as turns_db

        parked_settled = await asyncio.to_thread(
            turns_db.settle_parked_turns, session_id
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Stop: settling parked turns failed for session %s", session_id,
            exc_info=True,
        )

    if first or wakeups_cancelled or queued_aborted or parked_settled:
        logger.info(
            "Stop: quiesced session %s (latched=%s, wakeups=%d, queued=%d, "
            "parked=%d)",
            session_id, first, wakeups_cancelled, queued_aborted, parked_settled,
        )
    return {
        "latched": first,
        "wakeups_cancelled": wakeups_cancelled,
        "queued_aborted": queued_aborted,
        "parked_settled": parked_settled,
    }


def _keep_monitor_shells_on_stop() -> bool:
    """The opt-in that restores the OLD Stop semantics for Monitor shells.

    Default OFF: Stop kills everything, which is what the button says and what
    the user asked for after living with the alternative. Turning
    ``STOP_KEEPS_MONITOR_SHELLS=true`` on brings back the previous behaviour —
    a Stop leaves a Monitor shell whose subprocess is still alive running, on
    the theory that a dev server the user asked to keep up is not part of the
    turn. Read through a helper so a broken/absent settings object degrades to
    the safe default rather than failing a Stop.
    """
    try:
        from app.core.config import settings

        return bool(getattr(settings, "stop_keeps_monitor_shells", False))
    except Exception:  # noqa: BLE001 — a config read never breaks a Stop.
        return False


async def _settle_stopped_task(session_id: str, task_id: str, *, source: str = "user_stop") -> Optional[dict]:
    """Guarantee ONE terminal row + ONE terminal frame for a task Stop killed.

    Two guarantees, both of which were missing somewhere (087d3809 F2):

    * the registry row is terminal — a task whose stop path bailed out before
      marking it (a raising ``stop_task``, a ProviderAgent teardown that took an
      unusual branch) is completed ``killed`` here with user attribution, so
      nothing sits ``running`` forever;
    * the terminal SURFACE fires — ``force_terminal_surface`` drives the same
      chip + durable row every other terminal takes, bypassing the
      ``is_own_background_task`` / ``notify=False`` gates that silenced
      interrupt-killed agent workers while their sibling shells got a proper
      ``cancelled`` event. Bounded by the one-shot ``mark_terminal_notified``
      latch, so a task that already surfaced is left alone.

    Returns the wire record of the task (for the engine-retirement frame), or
    ``None`` when the registry never knew it.
    """
    from app.core.task_registry import (
        task_registry,
        STOP_ALL_SUMMARY,
        USER_STOP_REASON,
    )

    record = task_registry.get_task(session_id, task_id)
    if record is None:
        return None
    if record.get("status") == "running":
        task_registry.complete_task(
            session_id,
            task_id,
            "killed",
            summary=record.get("summary") or (STOP_ALL_SUMMARY if source == USER_STOP_REASON else f"Stopped by host: {source}"),
            reason=record.get("reason") or source,
        )
    task_registry.force_terminal_surface(session_id, task_id)
    return task_registry.get_task(session_id, task_id)


async def stop_all_background_tasks(
    session_id: str, *, include_shells: bool = True, user_stop: bool = False,
    respect_shell_preference: bool = True, source: str = "user_stop",
) -> list[dict]:
    """Stop EVERY in-flight background task in a session, one at a time.

    Used by the Stop path: after the main turn is interrupted, iterate the
    running-tasks view and drive each through the existing per-task
    ``stop_session_task`` (sub-agents via the SDK client's ``stop_task``,
    ProviderAgent workers — including nested ones, which register under the ROOT
    session id — via the supervisor's ``teardown_worker``, Monitor shells via
    their tracked subprocess). Reuses that path verbatim — no re-implementation
    of client stop logic — so ghost-reaping and idempotency behave identically
    to a single "x" click.

    STOP SEMANTICS (round 7). ``user_stop=True`` is the Stop button, and it now
    kills ALL of the session's work: sub-agents and their descendants, provider-
    lane workers, and Monitor shells — serving tasks and CI watchers included.
    The previous rule spared a healthy Monitor shell ("a dev server the user
    asked to keep up is not part of the turn"); the user's verdict on living
    with it was that Stop must mean stop, so that behaviour is now an explicit
    opt-in (``STOP_KEEPS_MONITOR_SHELLS=true``) that defaults to OFF. Typing
    mid-stream still goes through ``interrupt_for_query``, which sets no
    ``stop_background`` bit and never reaches here — a steer is not a Stop.

    A ProviderAgent worker's OWN Monitor shells are registered under the
    WORKER's session id and are children of the APP process, so killing the
    worker leaves them running. On the ``user_stop`` path they are reaped too.

    On the ``user_stop`` path this also calls :func:`enter_user_stop` FIRST, so
    the session-wide stop latch is set, armed wakeups are dropped, queued turns
    are aborted and stale parked rows are settled before a single task is
    touched. Every task it kills then gets a guaranteed terminal registry row, a
    terminal chip, and an engine retirement (``retire_task_terminals``) so
    fluid's ``_background_pending`` set does not keep listing the dead task and
    park on it forever.

    ``include_shells=False`` spares Monitor shells whose subprocess is still
    alive. Shutdown passes ``respect_shell_preference=False`` because no shell
    can survive termination of the worker process tree. Force paths (session delete, eviction reap) don't come through here
    at all — they use ``reap_session_background_shells``, which is unchanged.

    Snapshots the active list first so mutations during iteration (a task
    dropping to a terminal state as it is stopped) don't disturb the loop.
    Returns one result dict per task attempted, plus a ``skipped`` entry for
    each spared shell. Never raises.
    """
    from app.core.task_registry import task_registry

    if user_stop:
        # Latch + durable quiesce BEFORE the snapshot: a task completing while
        # we work through the list finds a stopped session and enqueues nothing.
        await enter_user_stop(session_id)
        if include_shells and respect_shell_preference and _keep_monitor_shells_on_stop():
            include_shells = False

    active = task_registry.get_active(session_id)
    task_ids = [task.get("id") for task in active if task.get("id")]

    # Worker sessions to sweep AFTER the kills. A ProviderAgent worker's own
    # Monitor shells are registered under the WORKER's session id, and killing
    # the worker does not reach them: the shell is a child of the APP process,
    # not of the worker's CLI, so evicting the worker's pooled client leaves its
    # dev server or CI watcher running (and its registry row stranded). Snapshot
    # the ids now — ``teardown_worker`` unregisters each record as it goes.
    # ``live_workers`` is keyed on the ROOT session, which is where nested
    # workers register too (``task_dispatch`` passes ``root_session_id``), so
    # this covers the whole descendant tree, not just the top level.
    worker_sessions: list[str] = []
    if user_stop:
        try:
            from app.core import provider_agent_supervisor

            worker_sessions = [
                r.worker_session_id
                for r in provider_agent_supervisor.live_workers(session_id)
                if r.worker_session_id
            ]
        except Exception:  # noqa: BLE001 — never let bookkeeping fail a stop.
            logger.debug(
                "Stop: could not enumerate workers for %s", session_id,
                exc_info=True,
            )

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
        results.append(await stop_session_task(session_id, task_id,
                                              **({"source": source} if source != "user_stop" else {})))

    for worker_session_id in worker_sessions:
        try:
            from app.mcp.builtin_tools import stop_session_background_shells

            reaped = await stop_session_background_shells(worker_session_id)
            if reaped:
                logger.info(
                    "Stop for session %s reaped %d shell(s) belonging to worker %s",
                    session_id, reaped, worker_session_id,
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Stop: reaping worker %s shells failed", worker_session_id,
                exc_info=True,
            )

    # Terminal guarantee + engine retirement for everything we just killed.
    # Both are best-effort and neither may change what the stop reports.
    frames: list[dict] = []
    for task_id in task_ids:
        try:
            record = await _settle_stopped_task(session_id, task_id,
                                                **({"source": source} if source != "user_stop" else {}))
        except Exception:  # noqa: BLE001
            logger.debug(
                "Stop: terminal settle failed for %s/%s", session_id, task_id,
                exc_info=True,
            )
            continue
        if record is None:
            continue
        frames.append({
            "task_id": task_id,
            "status": record.get("status") or "killed",
            "summary": record.get("summary") or "",
            "reason": record.get("reason") or "",
            "kind": record.get("kind") or "agent",
        })
    if frames:
        try:
            from app.core import turn_scheduler

            # ``wake=False`` by construction: this only tells fluid the ids are
            # terminal so they stop appearing in every later SessionParked (the
            # audited session was parked on seven dead ids for a full day). It
            # schedules nothing — a stop the user just asked for stays stopped.
            await turn_scheduler.retire_task_terminals(session_id, frames)
        except Exception:  # noqa: BLE001
            logger.debug(
                "Stop: engine retirement failed for %s", session_id, exc_info=True,
            )
    return results


def get_active_sessions() -> list:
    """Get list of active session IDs (connected clients)"""
    return get_session_pool().active_session_ids()


def get_streaming_sessions() -> list:
    """Get list of currently streaming session IDs"""
    return get_session_pool().streaming_session_ids()

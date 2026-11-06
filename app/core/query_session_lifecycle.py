"""
Session lifecycle queries: state inspection, interrupt, active/streaming lists.

Extracted from query_engine.py to isolate session lifecycle management.
"""

import asyncio
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from app.core.provider import session_admission
from app.core.session_pool import get_session_pool

logger = logging.getLogger(__name__)

# A busy CLI (e.g. deep in extended thinking) can leave the interrupt control
# request unanswered indefinitely; an unbounded await here hangs the two-phase
# "Send now" handshake past the frontend's confirm window and loses the
# user's message. Bounded so the caller can fall back to drain/force-cancel.
_SDK_INTERRUPT_TIMEOUT_SECONDS = 5.0


def _reap_task_killed(session_id: str, task_id: str, *, source: str | None = None) -> None:
    """Reap a ghost task to "killed", attributing shutdown-origin reaps.

    A reap that runs while the server is tearing down is a consequence of the
    restart, not a task failure — stamp the wire record accordingly (item 4a)
    so the UI/parent can say "Server restarted — background task stopped"
    instead of presenting a mysterious kill. An explicit shutdown source also
    applies when the caller quiesces one session before global shutdown begins.
    """
    from app.core import startup
    from app.core.task_registry import (
        task_registry,
        SERVER_SHUTDOWN_SUMMARY,
        SERVER_SHUTDOWN_REASON,
    )

    if source == SERVER_SHUTDOWN_REASON or getattr(startup, "is_shutting_down", False):
        task_registry.complete_task(
            session_id,
            task_id,
            "killed",
            summary=SERVER_SHUTDOWN_SUMMARY,
            reason=SERVER_SHUTDOWN_REASON,
        )
    else:
        task_registry.complete_task(session_id, task_id, "killed")


async def interrupt_session(session_id: str) -> bool:
    """Interrupt an active streaming session, or the turn waiting for the chat.

    True means a turn has this Stop. A waiting or admitted turn keeps it through
    setup even before its engine exists; an admitted turn also signals any pooled engine.

    Otherwise this calls the adapter's interrupt(), which signals the provider to stop
    processing. This is more reliable than just cancelling the
    asyncio task, which only takes effect at the next await point.
    """
    if session_admission.stop_waiting_turn(session_id):
        return True

    # Use peek() to avoid promoting to MRU — interrupt is a read-only check
    state = get_session_pool().peek(session_id)
    if not state:
        logger.warning(f"No active session found for {session_id}")
        return session_admission.stop_requested(session_id)

    if not state.is_connected:
        logger.warning(f"Session {session_id} is not connected")
        return session_admission.stop_requested(session_id)

    # Set interrupt flag first - this is checked in the streaming loop as a failsafe
    state.interrupt_requested = True

    # Don't check is_streaming too strictly - there might be race conditions
    # and it's better to try to interrupt anyway
    logger.debug(f"Attempting to interrupt session {session_id} (streaming={state.is_streaming})")

    # Run the interrupt on a DETACHED task guarded by a SHIELD: on timeout we
    # must NOT cancel the in-flight interrupt() — cancelling a control request
    # mid-flight abandons its pending response, which can wedge the client for
    # every subsequent control call. Shielding lets the wait_for time out while the
    # underlying interrupt keeps running to completion in the background.
    interrupt_task = asyncio.create_task(state.adapter.interrupt())
    # Attach the swallow/log done-callback AT CREATION: if the CALLER of
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


async def stop_session_task(
    session_id: str, task_id: str, *, source: str = "user_stop",
    _remote_confirmed: bool = False, _explicit_task_stop: bool = True,
) -> dict:
    """Stop ONE background task (sub-agent OR Monitor shell) in a session.

    Two kinds of task surface in the UI's ``activeTasks`` strip:

    - **shell** (Monitor subprocess): terminated directly via its tracked
      process and marked ``killed`` in the registry immediately.
    - **agent**: a PROVIDER-LANE worker runs as its own session under the
      ProviderAgent supervisor and is torn down there. The harness adapter has
      no per-task stop for any other agent task, so its registry row is marked
      ``killed`` directly, with the stop's attribution while the session is live.

    "Ghost" tasks — ones already terminated by outside forces (idle eviction,
    process death, session teardown) but still parked ``running`` in the
    registry with no live client left to stop — are REAPED here: marked
    ``killed`` so they drop off the next ``/live`` poll, and reported as a
    successful stop. Without this the "x" hits a dead session, returns
    ``ok: false``, and the UI strip is left showing a task that can never be
    cleared (its button stuck disabled).

    ``_explicit_task_stop`` is false for automatic stop-all reuse: only a fresh
    dedicated TaskStop/card Stop may grant legacy per-task retry authority.
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
    # blank bubble an unattributed shell kill would produce; an attribution
    # already on the row (a monitor timeout, say) still wins.
    from app.mcp.builtin_tools import stop_background_shell_task
    from app.core.task_registry import USER_STOP_REASON, USER_STOP_SUMMARY

    if source == "user_stop":
        source = USER_STOP_REASON
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
    # mis-routing a shell id to the client's agent-task stop (which would error).
    if existing is not None and existing.get("kind") == "shell":
        _reap_task_killed(session_id, task_id, source=source)
        return {
            "ok": True,
            "kind": "shell",
            "task_id": task_id,
            "detail": "reaped (shell gone)",
        }

    # 2) ProviderAgent worker — a provider-lane subagent has no CLI task to
    # stop: it is its OWN session, tracked by the supervisor and keyed by
    # (parent session id, worker session id). Its task id IS the worker session
    # id (``dispatch_worker`` sets both), so the registry key the strip's "x"
    # sends is exactly the supervisor key. ``teardown_worker`` is the single
    # per-worker kill path — it settles the card, kills the worker session,
    # sweeps its parent-keyed approval prompts, completes the registry row and
    # purges the park-gate residue.
    from app.core import provider_agent_supervisor

    from app.core.task_registry import AGENT_STOP_REASON, PARENT_AGENT_STOP_SUMMARY

    record = provider_agent_supervisor.get_worker(session_id, task_id)
    if record is None and existing is not None and existing.get("kind") == "agent":
        # A nested worker: its record is keyed under the ROOT session, while
        # this session (the one that dispatched it) holds its own row. Without
        # this, stopping a Task child only marked its children's rows killed
        # and left them running.
        record = provider_agent_supervisor.find_worker(task_id)
    if record is not None:
        if source == USER_STOP_REASON:
            summary, preview = USER_STOP_SUMMARY, "Stopped by the user."
        elif source == AGENT_STOP_REASON:
            # The parent agent's own TaskStop (#1805).
            summary, preview = PARENT_AGENT_STOP_SUMMARY, f"{PARENT_AGENT_STOP_SUMMARY}."
        else:
            summary = preview = f"Stopped by host: {source}"
        torn = await provider_agent_supervisor.teardown_worker(
            record, reason=source, summary=summary, preview=preview,
            **({"explicit_task_stop": True} if (record.remote_device_id and _explicit_task_stop
                and not _remote_confirmed and source in (USER_STOP_REASON, AGENT_STOP_REASON)) else {}),
            **({"remote_confirmed": True} if _remote_confirmed else {}),
        )
        own = task_registry.get_task(session_id, task_id)
        if record.parent_session_id != session_id and own is not None and own.get("status") == "running":
            # teardown_worker settles the root's row; this session's row too.
            task_registry.complete_task(session_id, task_id, "killed", summary=summary, reason=source)
        # A child on another worker: whether the stop reached it, in words.
        detail = (torn or {}).get("detail")
        return {"ok": True, "kind": "agent", "task_id": task_id, **({"detail": detail} if detail else {})}

    # 3) Any other agent task — the adapter has no per-task stop, so settle the
    # registry row ourselves and report it. On a live session this is the
    # explicit stop the caller asked for, so it carries the stop attribution
    # (the chip reads "Stopped by user" / cancelled, not a red failure). With
    # the session gone the task was already dead: reap it unattributed (or with
    # the shutdown attribution) as a ghost the UI would otherwise show forever.
    state = get_session_pool().peek(session_id)
    if existing is not None:
        if state is not None and state.is_connected:
            task_registry.complete_task(
                session_id,
                task_id,
                "killed",
                summary=USER_STOP_SUMMARY if source == USER_STOP_REASON else f"Stopped by host: {source}",
                reason=source,
            )
            return {
                "ok": True,
                "kind": existing.get("kind") or "agent",
                "task_id": task_id,
            }
        _reap_task_killed(session_id, task_id, source=source)
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
    # Nothing durable to settle — there is nothing to reap and nothing the UI
    # is showing.
    if not state:
        return {"ok": False, "kind": "agent", "detail": "session not active"}
    if not state.is_connected:
        return {"ok": False, "kind": "agent", "detail": "session not connected"}
    return {"ok": False, "kind": "agent", "detail": "task not found"}


_STOP_RECEIPT_RETRY_DELAYS = (0.2, 0.5)
# Lifted by the sweep below at most this many a pass, in turn, so every latch is
# reached; anything acting on a session asks for it (:func:`reconcile_user_stop`).
_STOP_SWEEP_BATCH = 200
_stop_sweep_start = 0


async def reconcile_user_stop(session_id: str) -> bool:
    """Whether nothing holds this session under a Stop, once the database is asked.

    A person's message lifts the latch only on the install it was sent to. Any
    other install holding the latch learns of it here, before refusing work. Only
    a message above the Stop's cutoff counts; until the Stop has read its cutoff
    the latch holds, and a Stop that could not read one has no grounds to refuse
    a person's waiting message. Costs nothing unless the session is latched.
    """
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db

    if not task_registry.is_user_stopped(session_id):
        return True
    generation = task_registry.stop_generation(session_id)
    cutoff = task_registry.stop_input_cutoff(session_id)
    try:
        if cutoff is not None:
            newer = await asyncio.to_thread(turns_db.person_input_after, session_id, cutoff)
        elif task_registry.stop_admission_incomplete(session_id):
            newer = await asyncio.to_thread(turns_db.person_input_queued, session_id)
        else:
            return False
    except Exception:  # noqa: BLE001 — unanswered, the latch keeps holding.
        logger.warning("Stop: reading newer input failed for %s", session_id, exc_info=True)
        return False
    if (newer and task_registry.stop_generation(session_id) == generation
            and task_registry.clear_user_stop(session_id)):
        logger.info("Stop: a newer message reached %s; released the latch", session_id)
    return not task_registry.is_user_stopped(session_id)


async def reconcile_user_stops() -> None:
    """Lift this install's latches a person has answered on another install.

    The latch's many synchronous readers cannot ask the database themselves, so
    each answers correctly from the next pass on. Never raises.
    """
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db

    global _stop_sweep_start
    stopped = list(task_registry.user_stopped_sessions())
    start = _stop_sweep_start % len(stopped) if stopped else 0
    sessions = (stopped[start:] + stopped[:start])[:_STOP_SWEEP_BATCH]
    _stop_sweep_start = start + len(sessions)
    held = {sid: (task_registry.stop_input_cutoff(sid), task_registry.stop_generation(sid))
            for sid in sessions}
    try:
        newer = await asyncio.to_thread(
            turns_db.sessions_with_person_input_after,
            {sid: cutoff for sid, (cutoff, _generation) in held.items() if cutoff is not None},
        ) if any(cutoff is not None for cutoff, _generation in held.values()) else []
    except Exception:  # noqa: BLE001 — the next pass asks again.
        logger.warning("Stop: reading newer input failed", exc_info=True)
        newer = []
    for sid in newer:
        if (task_registry.stop_generation(sid) == held[sid][1]
                and task_registry.clear_user_stop(sid)):
            logger.info("Stop: a newer message reached %s; released the latch", sid)
    for sid in sessions:
        if task_registry.stop_admission_incomplete(sid):
            await reconcile_user_stop(sid)


@dataclass
class _UserStop:
    """One Stop and its receipt: what it applies to, fixed when it was admitted.

    ``app.db.turns.interrupt_turn`` records the receipt in the transaction that
    admits the Stop, so it holds across installs and through every wait below.
    Cleanup ends only what the receipt names, and a person's message above its
    ``input_cutoff`` outlives the Stop (:func:`app.db.turns.outlives_stop`).
    A Stop with no live turn to record it on reads its own, once (:meth:`bind`).
    """

    session_id: str
    generation: int
    first: bool
    receipt: Optional[dict] = None

    def _latch_held(self) -> bool:
        from app.core.task_registry import task_registry

        return (task_registry.is_user_stopped(self.session_id)
                and task_registry.stop_generation(self.session_id) == self.generation)

    async def bind(self) -> dict:
        from app.core.task_registry import task_registry
        from app.db import turns as turns_db

        if self.receipt is None and not self._latch_held():
            # A message overtook this Stop before it was recorded. Recording it now
            # would place it after that message, and cancel what that message armed.
            self.receipt = {}
        if self.receipt is None:
            receipt: Optional[dict] = None
            for delay in (*_STOP_RECEIPT_RETRY_DELAYS, None):
                try:
                    receipt = await asyncio.to_thread(turns_db.read_stop_receipt, self.session_id)
                    break
                except Exception:  # noqa: BLE001 — reported below if it never answers.
                    logger.warning("Stop: reading what session %s holds failed", self.session_id,
                                   exc_info=True)
                if delay is not None:
                    await asyncio.sleep(delay)
            if receipt is None:
                # Named work still ends, and only that. See ``overtaken``.
                logger.error("Stop: session %s was stopped without knowing what it applies to: "
                             "older queued turns, wakes and parks may survive it", self.session_id)
                if self._latch_held():
                    task_registry.note_stop_incomplete(self.session_id)
                receipt = {}
            if not self._latch_held():
                # A message lifted the latch during that read, and the read may
                # have seen it. What is older can no longer be told apart.
                receipt = {}
            task_registry.note_stop_cutoff(self.session_id, receipt.get("input_cutoff"))
            self.receipt = receipt
        return self.receipt

    async def overtaken(self) -> bool:
        """Whether a person has sent a message since this Stop, on any install.

        Lifts the latch when they have. Asked after latching, so a message this
        read cannot see yet meets the latch and its cutoff at the runtime's gate.
        """
        from app.core.task_registry import task_registry
        from app.db import turns as turns_db

        if not self._latch_held() or task_registry.stop_admission_incomplete(self.session_id):
            return True
        try:
            newer = await asyncio.to_thread(
                turns_db.person_input_after, self.session_id, (self.receipt or {}).get("input_cutoff"),
            )
        except Exception:  # noqa: BLE001 — spare what this cannot place.
            logger.warning("Stop: reading newer input failed for %s", self.session_id, exc_info=True)
            return True
        if newer and self._latch_held() and task_registry.clear_user_stop(self.session_id):
            logger.info("Stop: a newer message reached %s; released the latch", self.session_id)
        return newer


def _begin_user_stop(session_id: str, receipt: Optional[dict] = None) -> _UserStop:
    from app.core.task_registry import task_registry

    first = task_registry.mark_user_stopped(session_id, input_cutoff=(receipt or {}).get("input_cutoff"))
    return _UserStop(session_id, task_registry.stop_generation(session_id), first, receipt)


@dataclass(frozen=True)
class StopScope:
    """The local work a Stop may end, each run named by its incarnation.

    Only the install running a task can act on it, and a restart ends it, so
    this is taken on that install, before any wait, not stored with the receipt.
    """

    active: list[dict]
    worker_sessions: list[str]
    worker_shells: dict[str, set[str]]
    runs: dict[str, Any]
    # Something newer already runs, so this names only what predates it, and
    # nothing here may be read off the session as it is now.
    older_only: bool = False
    unattributed: tuple[str, ...] = ()
    orders: dict[str, Any] = field(default_factory=dict)
    remote_scopes: dict[str, Optional[dict]] = field(default_factory=dict)


def capture_stop_scope(session_id: str, *, before_turn: Optional[int] = None) -> StopScope:
    """Synchronous, so nothing can start between the caller's check and this.

    ``before_turn`` is the ``insert_order`` of a newer turn that took over this
    session. Each run knows the turn it started under
    (:meth:`TaskRegistry.task_turn_order`) and a resume renews that, so a run under
    an earlier turn is older, whatever any clock says. A legacy run's migration
    bound must be strictly before the successor, not equal. What this cannot
    place is left running and reported incomplete.
    """
    from app.core.task_registry import task_registry

    active = task_registry.get_active(session_id)
    # An offline-noticed child may still run remotely. Keep its captured task
    # identity, rather than later discovering it and issuing a broad id Stop.
    known = {t.get("id") for t in active}
    active.extend(t for t in task_registry.get_visible(session_id)
                  if t.get("id") not in known and t.get("worker")
                  and t.get("reason") == "worker_unreachable")
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
    worker_shells: dict[str, set[str]] = {}
    try:
        from app.core import provider_agent_supervisor
        from app.mcp.builtin_tools import session_background_shell_ids

        worker_sessions = [
            r.worker_session_id
            for r in provider_agent_supervisor.live_workers(session_id)
            if r.worker_session_id
        ]
        worker_shells = {
            worker: session_background_shell_ids(worker) for worker in worker_sessions
        }
    except Exception:  # noqa: BLE001 — never let bookkeeping fail a stop.
        logger.debug("Stop: could not enumerate workers for %s", session_id, exc_info=True)
    runs = {
        task_id: task_registry.task_incarnation(session_id, task_id)
        for task_id in (*(t.get("id") for t in active if t.get("id")), *worker_sessions)
    }
    orders = {task_id: task_registry.task_turn_upper_bound(session_id, task_id) for task_id in runs}
    remote_scopes = {task_id: task_registry.remote_stop_scope(session_id, task_id) for task_id in runs}
    if before_turn is None:
        return StopScope(active, worker_sessions, worker_shells, runs, orders=orders, remote_scopes=remote_scopes)
    older = {task_id for task_id in runs
             if (order := task_registry.task_turn_upper_bound(session_id, task_id)) is not None
             and int(order) < before_turn}
    unattributed = tuple(task_id for task_id in runs if task_id not in older
                         and task_registry.task_turn_order(session_id, task_id) is None)
    return StopScope(
        [task for task in active if task.get("id") in older],
        [worker for worker in worker_sessions if worker in older],
        {worker: shells for worker, shells in worker_shells.items() if worker in older},
        {task_id: runs[task_id] for task_id in older},
        older_only=True, unattributed=unattributed,
        orders={task_id: orders[task_id] for task_id in older},
        remote_scopes={task_id: remote_scopes[task_id] for task_id in older},
    )


async def _validate_remote_stop_scope(session_id: str, scope: StopScope) -> tuple[StopScope, list[dict]]:
    """A cached remote order is provisional until checked against durable truth.

    In particular, a pre-nonce writer may have invalidated it at the SAME
    timestamp. Do this off-loop, in the bounded cleanup step, never ahead of
    the provider's interrupt signal. Local run identity is checked at each act.
    """
    remote = {task["id"] for task in scope.active if task.get("worker")}
    if not remote:
        return scope, []
    from app.db import background_tasks
    try:
        rows = await asyncio.to_thread(background_tasks.get_stop_order_proofs, session_id, list(remote))
    except Exception:
        logger.warning("Stop: remote provenance read failed for %s", session_id, exc_info=True)
        rows = {}
    unknown, replaced = set(), set()
    def iso(value):
        return value.isoformat() if hasattr(value, "isoformat") else value
    from app.db.base import parse_jsonb
    for task_id in remote:
        row = rows.get(task_id) or {}
        order = row.get("turn_order") if row.get("turn_order") is not None else row.get("turn_order_upper_bound")
        if (order is None or row.get("started_at") is None or
                iso(row.get("turn_order_incarnation")) != iso(row.get("started_at"))):
            unknown.add(task_id)
        else:
            current_scope = parse_jsonb(row.get("remote_stop_scope"))
            captured_scope = scope.remote_scopes.get(task_id)
            from app.core.scoped_remote_stop import valid_scope
            if current_scope != captured_scope:
                if (valid_scope(current_scope) and valid_scope(captured_scope)
                        and current_scope["epoch"] > captured_scope["epoch"]):
                    replaced.add(task_id)
                else:
                    unknown.add(task_id)  # persistence lag is not proof of a newer run
            elif order != scope.orders.get(task_id) or iso(row.get("started_at")) != scope.runs.get(task_id):
                if scope.orders.get(task_id) is not None and order > scope.orders[task_id]:
                    replaced.add(task_id)
                else:
                    unknown.add(task_id)
    excluded = unknown | replaced
    return replace(scope,
        active=[task for task in scope.active if task.get("id") not in excluded],
        worker_sessions=[tid for tid in scope.worker_sessions if tid not in excluded],
        worker_shells={tid: shells for tid, shells in scope.worker_shells.items() if tid not in excluded},
        runs={tid: run for tid, run in scope.runs.items() if tid not in excluded},
        orders={tid: order for tid, order in scope.orders.items() if tid not in excluded},
        unattributed=tuple(sorted(set(scope.unattributed) | unknown))), [
            {"ok": True, "task_id": tid, "skipped": "resumed"} for tid in sorted(replaced)]


async def enter_user_stop(session_id: str, *, stop: Optional[_UserStop] = None) -> dict:
    """Quiesce a session the user just stopped — everything BESIDES killing tasks.

    STOP SEMANTICS. Stop means: everything this session is doing ends
    now, and nothing restarts it until the user sends a new message. Killing the
    background tasks (``stop_all_background_tasks``, which calls this first) is
    only half of that — a stopped session has four other ways to come back to
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
       that raced to live is left alone by the guard. Stop still cancels every
       one of them, but the words the owner wrote are kept as a durable Not sent
       (stopped) transcript row with their attachments, one per message. A row
       never auto-runs and reaches the composer only when the owner restores it.
       ``SessionRuntime.execute`` applies the same rule to a row a claim pass
       picked up as Stop landed.
    4. **Stale parked rows.** With all background work dead, any ``parked`` turn
       is residue that would otherwise keep ``lifecycle_state`` reporting the
       session busy until some later turn's self-heal cleared it.

    Both the WebSocket Stop handler AND the owning runner's durable
    ``stop_background`` path route through here, so the latch lands on whichever
    install actually holds the session's work. Idempotent and best-effort: every
    leg is independently guarded, and a failure in one never skips the others.

    This work can outlast the turn it stops, so each leg ends only what the
    receipt names (see :class:`_UserStop`), and running it again is harmless.
    Without one, this call is the Stop.
    Returns a small counts dict for the log/tests.
    """
    from app.db import turns as turns_db

    # (1) FIRST, and synchronously — before any await — so a terminal frame
    #     racing this call is already looking at a stopped session.
    stop = stop or _begin_user_stop(session_id)
    receipt = await stop.bind()
    await stop.overtaken()

    wakeups_cancelled = 0
    if receipt.get("wakes") or receipt.get("handoffs"):
        try:
            from app.core import wakeup_service

            wakeups_cancelled = await wakeup_service.cancel_named_wakeups_async(
                session_id, receipt.get("wakes") or [], receipt.get("handoffs") or [],
            )
        except Exception:  # noqa: BLE001 — one leg failing must not skip the rest.
            logger.warning(
                "Stop: cancelling wakeups failed for session %s", session_id,
                exc_info=True,
            )

    queued_aborted = 0
    try:
        turn_ids = receipt.get("queued") or []
        if turn_ids:
            removed = await asyncio.to_thread(turns_db.stop_queued_turns, session_id, turn_ids)
            queued_aborted = len(removed)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Stop: aborting queued turns failed for session %s", session_id,
            exc_info=True,
        )

    parked_settled = 0
    try:
        # By these ids and intervals, rechecked under the admission lock the next
        # message's enqueue also takes: a park named here can have resumed.
        for park in receipt.get("parks") or []:
            parked_settled += await asyncio.to_thread(
                turns_db.settle_parked_turns, session_id, observed_park=park,
            )
        if parked_settled:
            # Settling banked the park's working time; show the final total.
            from app.core.turn_finalize import broadcast_work_time

            await broadcast_work_time(session_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Stop: settling parked turns failed for session %s", session_id,
            exc_info=True,
        )

    if stop.first or wakeups_cancelled or queued_aborted or parked_settled:
        logger.info(
            "Stop: quiesced session %s (latched=%s, wakeups=%d, queued=%d, "
            "parked=%d)",
            session_id, stop.first, wakeups_cancelled, queued_aborted, parked_settled,
        )
    from app.core.task_registry import task_registry

    return {
        "latched": stop.first,
        "wakeups_cancelled": wakeups_cancelled,
        "queued_aborted": queued_aborted,
        "parked_settled": parked_settled,
        **({"incomplete": True} if task_registry.stop_admission_incomplete(session_id) else {}),
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

    Two guarantees:

    * the registry row is terminal — a task whose stop path bailed out before
      marking it (a raising ``stop_task``, a ProviderAgent teardown that took an
      unusual branch) is completed ``killed`` here with user attribution, so
      nothing sits ``running`` forever;
    * the terminal SURFACE fires — ``force_terminal_surface`` drives the same
      chip + durable row every other terminal takes, bypassing the
      ``is_own_background_task`` / ``notify=False`` gates that would otherwise
      silence interrupt-killed agent workers while their sibling shells get a
      proper ``cancelled`` event. Bounded by the one-shot ``mark_terminal_notified``
      latch, so a task that already surfaced is left alone.

    Returns the wire record of the task (for the engine-retirement frame), or
    ``None`` when the registry never knew it.
    """
    from app.core.task_registry import (
        task_registry,
        STOP_ALL_SUMMARY,
        USER_STOP_REASON,
    )

    if source == "user_stop":
        source = USER_STOP_REASON
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
    scope: Optional[StopScope] = None, receipt: Optional[dict] = None, _receipt_consumer: bool = False,
) -> list[dict]:
    """Stop EVERY in-flight background task in a session, one at a time.

    Used by the Stop path: after the main turn is interrupted, iterate the
    running-tasks view and drive each through the existing per-task
    ``stop_session_task`` (sub-agents via the pooled client's ``stop_task``,
    ProviderAgent workers — including nested ones, which register under the ROOT
    session id — via the supervisor's ``teardown_worker``, Monitor shells via
    their tracked subprocess). Reuses that path verbatim — no re-implementation
    of client stop logic — so ghost-reaping and idempotency behave identically
    to a single "x" click.

    STOP SEMANTICS. ``user_stop=True`` is the Stop button, and it kills ALL of
    the session's work: sub-agents and their descendants, provider-lane
    workers, and Monitor shells — serving tasks and CI watchers included.
    Sparing a healthy Monitor shell is an explicit opt-in
    (``STOP_KEEPS_MONITOR_SHELLS=true``) that defaults to OFF. Typing
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

    Everything this stop will end is decided before any wait. ``receipt`` names
    the session's durable work (see :class:`_UserStop`) and ``scope`` this
    install's running work; a caller that had to wait for the receipt captured
    the scope first (:func:`capture_stop_scope`). A caller may let this run on
    after signalling the provider, so the turn can be over and the next one
    running by the time a later step gets its turn. The runs named here still
    end, and nothing started after them does: a task resumed under the same id is
    a new run, left alone (:meth:`TaskRegistry.task_incarnation`).
    Returns one result dict per task attempted, plus a ``skipped`` entry for
    each spared shell. Never raises.
    """
    from app.core.task_registry import task_registry

    stop: Optional[_UserStop] = None
    if user_stop:
        # The latch before the snapshot: a task completing while we work through
        # the list finds a stopped session and enqueues nothing.
        stop = _begin_user_stop(session_id, receipt)
        if stop.first:
            logger.info("Stop: latched session %s", session_id)
        if include_shells and respect_shell_preference and _keep_monitor_shells_on_stop():
            include_shells = False
        # A Swarm this session runs would start its next workers as the current
        # ones die: latch its workflows first. The running workers are stopped
        # below with this stop's attribution, like any other task.
        try:
            from app.core.swarm import latch_all

            latch_all(session_id)
        except Exception:  # noqa: BLE001 — the per-task stops below still run.
            logger.warning("Stop: latching Swarm workflows failed for %s", session_id, exc_info=True)

    scope = scope or capture_stop_scope(session_id)
    scope, revalidated = await _validate_remote_stop_scope(session_id, scope)
    active = scope.active
    # A worker shutting down (docker stop, quitting the desktop app) ends only
    # its own work: a Task child running on another worker keeps running, and
    # its report reaches this chat when this worker is back (#1808).
    from app.core import startup

    keep_remote = bool(getattr(startup, "is_shutting_down", False))
    if keep_remote:
        active = [task for task in active if not task.get("worker")]
    task_ids = [task.get("id") for task in active if task.get("id")]
    remote_workers = {task["id"]: task["worker"] for task in active if task.get("worker")}

    worker_sessions = scope.worker_sessions if user_stop else []
    worker_shells = scope.worker_shells

    # Asked right before each act below: nothing suspends from there until a
    # worker's teardown latches ``torn_down``, which refuses a new continuation.
    def same_run(task_id: str) -> bool:
        now = task_registry.task_incarnation(session_id, task_id)
        order = task_registry.task_turn_upper_bound(session_id, task_id)
        same_order = (task_id not in scope.orders or (now is None and order is None)
                      or order == scope.orders[task_id])
        same_remote = task_registry.remote_stop_scope(session_id, task_id) == scope.remote_scopes.get(task_id)
        return same_order and same_remote and (now is None or now == scope.runs.get(task_id))

    async def overtaken() -> bool:
        # Asked either way: this may be what lifts the latch this Stop just set.
        newer = stop is not None and await stop.overtaken()
        return newer or scope.older_only

    spared: list[dict] = []
    for task_id in scope.unattributed:
        record = task_registry.get_task(session_id, task_id) or {}
        if record.get("worker"):
            from app.core.scoped_remote_stop import incomplete
            worker = record["worker"]
            spared.append(incomplete(task_id, worker["deviceId"],
                worker.get("name") or worker["deviceId"], "task attribution is unknown"))
        else:
            spared.append({"ok": False, "task_id": task_id, "incomplete": True, "skipped": "unattributed"})
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
            spared.extend(
                {"ok": True, "kind": "shell", "task_id": tid, "skipped": "left running"}
                for tid in sorted(keep)
            )
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

    if stop is not None:
        quiesced = await enter_user_stop(session_id, stop=stop)
        if quiesced.get("incomplete"):
            spared.append({"ok": False, "incomplete": True, "skipped": "stop receipt unavailable"})

    # Checkpoints belong to the pooled engine, not the task registry. Idle
    # sessions have no turn runtime to interrupt, but Stop/archive must still
    # cancel their provider call and clear any pending checkpoint. A checkpoint
    # running after the next message belongs to that message.
    if not await overtaken():
        state = get_session_pool().peek(session_id)
        cancel_checkpoint = getattr(getattr(state, "adapter", None), "cancel_checkpoint", None)
        if callable(cancel_checkpoint):
            await cancel_checkpoint()

    results: list[dict] = [*spared, *revalidated]
    not_stopped: set[str] = set()
    for task_id in task_ids:
        if not same_run(task_id):
            logger.info("Stop: %s of session %s was resumed; leaving the new run alone",
                        task_id, session_id)
            results.append({"ok": True, "kind": "agent", "task_id": task_id, "skipped": "resumed"})
            continue
        remote_confirmed = False
        if user_stop and task_id in remote_workers:
            from app.core import scoped_remote_stop
            checked = await scoped_remote_stop.request(task_id, remote_workers[task_id],
                scope.remote_scopes.get(task_id), guard=lambda: same_run(task_id),
                suppress=lambda: task_registry.suppress_wake(session_id, task_id))
            if not checked.get("ok") or checked.get("skipped"):
                not_stopped.add(task_id)
                results.append(checked)
                continue
            if not same_run(task_id):
                results.append({"ok": True, "task_id": task_id, "skipped": "resumed"})
                continue
            remote_confirmed = True
            from app.db import remote_tasks
            try:
                await asyncio.to_thread(remote_tasks.record_scoped_stop, task_id, source)
            except Exception:
                logger.warning("Could not project confirmed remote Stop for %s", task_id, exc_info=True)
            if not same_run(task_id):
                results.append({"ok": True, "task_id": task_id, "skipped": "resumed"})
                not_stopped.add(task_id)
                continue
        results.append(await stop_session_task(session_id, task_id, _explicit_task_stop=False,
            **({"source": source} if source != "user_stop" else {}),
            **({"_remote_confirmed": True} if remote_confirmed else {})))

    if not user_stop and not keep_remote and not await overtaken():
        # A Task child on another worker whose worker went quiet past the
        # offline notice is no longer in the registry or the supervisor, but may
        # still run there: its stop is recorded and delivered when it answers.
        # This reads the session's children when it runs, so it asks first: once
        # a newer message is in, a child here could be that message's.
        try:
            from app.core import remote_task
            from app.core.task_registry import USER_STOP_REASON

            results.extend(await remote_task.stop_noticed_children(
                session_id, reason=USER_STOP_REASON if source == "user_stop" else source))
        except Exception:  # noqa: BLE001 — never let a remote stop fail a Stop.
            logger.warning("Stop: stopping noticed remote children of %s failed", session_id,
                           exc_info=True)

    for worker_session_id in worker_sessions:
        if worker_session_id in not_stopped:
            continue
        try:
            from app.mcp.builtin_tools import stop_session_background_shells

            # A worker can start a shell until it is killed just above, so sweep
            # all of its shells. Once it is running for something newer, only the
            # shells it had when this stop began.
            newer = await overtaken()
            reaped = await stop_session_background_shells(
                worker_session_id,
                **({} if not newer and same_run(worker_session_id)
                   else {"only": worker_shells.get(worker_session_id, set())}),
            )
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
        if task_id in not_stopped or not same_run(task_id):
            continue
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
            # terminal so they stop appearing in every later SessionParked. It
            # schedules nothing — a stop the user just asked for stays stopped.
            await turn_scheduler.retire_task_terminals(session_id, frames)
        except Exception:  # noqa: BLE001
            logger.debug(
                "Stop: engine retirement failed for %s", session_id, exc_info=True,
            )
    if user_stop and any(r.get("incomplete") or r.get("ok") is False for r in results):
        # A live/parked owner receipt writes its durable note when consumed.
        # Idle sessions have no owner receipt, so this is their producing path.
        # enter_user_stop may have published an owner receipt even when the
        # caller supplied none. That bound authority, not the original argument,
        # decides whether the receipt consumer already owns this warning.
        bound = stop.receipt or {}
        if not _receipt_consumer and not bound.get("cleanup_receipts"):
            from app.core.scoped_remote_stop import record_incomplete_note
            await record_incomplete_note(session_id, results,
                notice_key=f"idle:{bound['stop_seq']}" if "stop_seq" in bound else None)
    return results


def get_streaming_sessions() -> list:
    """Get list of currently streaming session IDs"""
    return get_session_pool().streaming_session_ids()

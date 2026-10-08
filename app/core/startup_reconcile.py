"""Boot-time "reconcile from durable truth".

After a crash or restart the in-memory control planes are empty — the session
pool and the task registry hold nothing — but durable state written by the
previous run can still claim sessions are mid-flight:

- ``sessions.parked_task_kind`` is set when a turn ends with live
  background work (a sub-agent or a background shell — see
  ``turn_finalize.persist_turn_end_state``). No background work survives a
  restart, so any marker found at boot is stale: left in place it reads as a
  session forever "parked" awaiting an unpark that can never arrive.
- ``sessions.status`` only ever holds idle/terminal values today (``active``,
  ``archived``) — busy truth is the durable runs table
  (``app.core.streaming_status``); parked signals live in ``task_registry`` —
  but a row that has drifted to a live-looking value (written by a different
  version, or by hand) would render as stuck forever, so those are
  defensively reset too.
- the ENGINE's background-pending set is rebuilt from the durable
  ``SubagentEvent`` log on every rehydrate and popped only by a terminal
  event, so a completion that never reached the engine is re-created as a
  phantom forever and listed on every ``SessionParked``. Nothing but a write
  into that log clears one, which is why the repair
  (``pending_reconcile.sweep_orphaned_engine_pending``) runs here and only for
  ids the durable task rows prove are over.
- CLI subprocesses are tracked in memory only (``app.core.session_pool``
  snapshots descendant trees per-eviction), so a mid-turn server death leaks
  them. Every spawn is env-marked with ``AI_SHUFFLE_MANAGED_BY=<instance id>``
  (``app.core.environment._build_env_with_ai_tools``), which is the one
  attribution that survives the parent server dying — the boot sweep only
  terminates processes carrying THIS install's marker.

Everything here is scoped to THIS install: several installs share one
Postgres (each with its own ``INSTANCE_ID``, recorded per-row in
``sessions.instance_id``), and another install's sessions may be legitimately
parked or streaming right now. Only rows with
``instance_id = settings.effective_instance_id`` are ever touched, and the
process sweep only matches this install's env marker.

Automation schedules are reconciled by ``automation_manager.start()``, not here.

Each branch is independently guarded and ``reconcile_from_durable_truth``
never raises: a reconcile failure must never block boot (see the call site in
``app/main.py``). Every reconciled orphan is recorded via ``record_error`` so
the incident is visible in the app's error UI / ``GetAppErrors``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from typing import Any, Dict, Optional

from app.core.config import settings
from app.db.base import get_db
from app.db.compat import ph
from app.db.async_database import run_sync
from app.db.error_logs import record_error

logger = logging.getLogger(__name__)

# Env marker stamped into every spawned CLI subprocess — must match
# app.core.environment._build_env_with_ai_tools.
CLI_ENV_MARKER = "AI_SHUFFLE_MANAGED_BY"

# sessions.status values that would read as in-flight. Current code never
# writes these — the whole written vocabulary is "active" / "archived" /
# "failed" (the last one stamped by ProviderAgent on a worker session whose
# dispatch errored, see ``app.core.provider_agent.WORKER_FAILED_STATUS``) — so
# any hit here is drift and gets reset. "failed" is deliberately NOT in this
# set: it is a settled verdict about a run that is over, not an in-flight
# state, and a boot must not erase it. The session's next turn revives it.
_LIVE_LOOKING_STATUSES = ("streaming", "parked", "running")


def _row_value(row: Any, key: str, idx: int) -> Any:
    """Read a column from a row that may be dict-like or a plain tuple."""
    return row[key] if isinstance(row, dict) else row[idx]


def _clear_stale_parked_markers() -> int:
    """Clear ``parked_task_kind`` on this install's sessions (all are stale).

    The marker means "parent turn ended with background work still running";
    that work ran inside the previous server process, so on a fresh boot every
    own-instance marker is an orphan. The exception is a session parked on a
    Task child running on another worker: the restart did not stop that child,
    and its report wakes the session as before. Returns the number cleared.
    """
    _ph = ph()
    instance_id = settings.effective_instance_id
    remote_work = (
        "EXISTS (SELECT 1 FROM background_tasks b WHERE b.session_id = sessions.id "
        "AND b.status = 'running' AND b.remote_device_id IS NOT NULL)"
    )
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT id, parked_task_kind FROM sessions
                 WHERE instance_id = {_ph} AND parked_task_kind IS NOT NULL
                   AND NOT {remote_work}""",
            (instance_id,),
        )
        rows = cursor.fetchall()
        if rows:
            cursor.execute(
                f"""UPDATE sessions SET parked_task_kind = NULL
                     WHERE instance_id = {_ph} AND parked_task_kind IS NOT NULL
                       AND NOT {remote_work}""",
                (instance_id,),
            )

    for row in rows:
        session_id = _row_value(row, "id", 0)
        kind = _row_value(row, "parked_task_kind", 1)
        record_error(
            "backend",
            f"Boot reconcile: session {session_id} was parked on background "
            f"work (kind={kind}) when the server stopped; cleared the stale "
            "parked marker — background work does not survive a restart.",
            level="WARNING",
            session_id=session_id,
            context={"reconcile": "parked_task_kind", "kind": kind},
        )
    return len(rows)


#: What the durable row (and so TaskList/TaskGet) says about a task the
#: restart cut off. The chip text the UI shows is the settle frame's summary.
RESTART_INTERRUPTED_SUMMARY = "Interrupted by an app restart before it finished."


async def _mark_stale_background_task_rows() -> int:
    """Mark this install's non-terminal durable task rows killed.

    ``background_tasks`` mirrors the in-memory registry, so any own-instance row
    still non-terminal at boot was stranded by the previous process: background
    work never survives a restart. Mirrors ``_clear_stale_parked_markers``'s
    scoping.

    Flipping the row is not enough for the UI: the frontend rebuilds a
    background-task chip by replaying ``session_events`` and only settles it when
    a terminal ``task.updated`` frame arrives. So for each flipped row we append
    that terminal frame (unfenced, legal between turns/at boot — see
    ``record_drained_event``) with status ``killed`` and reason
    ``server_restart``. Each emission is guarded independently: one bad row must
    never block boot or the other rows. Returns the number of rows marked.
    """
    from app.db.background_tasks import mark_stale_background_tasks_killed
    from app.core.turn_recorder import record_task_settle_frame

    rows = await asyncio.to_thread(
        mark_stale_background_tasks_killed, RESTART_INTERRUPTED_SUMMARY,
    )
    if not rows:
        return 0

    from app.core.task_persistence import broadcast_running_task_presence
    await broadcast_running_task_presence([_row_value(row, "session_id", 1) for row in rows])

    summary = "Background task lost in a server restart"
    for row in rows:
        session_id = _row_value(row, "session_id", 1)
        task_id = _row_value(row, "task_id", 0)
        tool_use_id = _row_value(row, "tool_use_id", 2)
        try:
            await record_task_settle_frame(
                session_id, task_id, "killed", summary,
                reason="server_restart", tool_use_id=tool_use_id,
            )
        except Exception as exc:  # noqa: BLE001 — one bad row must not block the rest
            logger.exception(
                "Boot reconcile: emitting terminal frame for stale background "
                "task %s/%s failed", session_id, task_id,
            )
            await run_sync(
                record_error,
                "backend",
                f"Boot reconcile: failed to emit terminal frame for stale "
                f"background task {task_id} (session {session_id}): {exc}",
                level="WARNING",
                session_id=session_id,
                context={"reconcile": "background_tasks", "task_id": task_id},
            )

    count = len(rows)
    await run_sync(
        record_error,
        "backend",
        f"Boot reconcile: {count} durable background-task row(s) were "
        "still non-terminal when the server stopped; marked them killed "
        "— background work does not survive a restart.",
        level="WARNING",
        context={"reconcile": "background_tasks", "rows": count},
    )
    try:
        await _announce_interrupted_tasks(rows)
    except Exception:  # noqa: BLE001 — the rows are settled; a notice failure is not fatal
        logger.exception("Boot reconcile: announcing interrupted tasks failed")
    return count


def _interrupted_summary(row: Dict[str, Any], stranded: int) -> str:
    """Plain-language notice line for one interrupted task, by kind."""
    task_id = _row_value(row, "task_id", 0)
    if (_row_value(row, "kind", 4) or "") == "agent":
        text = (
            "Interrupted: the app restarted while this worker was running, so "
            "it stopped before finishing and is not running now. Its saved "
            f"transcript is kept: SendMessage to {task_id} continues it, or "
            "dispatch the work again."
        )
        if stranded:
            text += (
                f" It had {stranded} background task(s) of its own "
                "running, which were stopped too."
            )
        return text
    return (
        "Interrupted: the app restarted while this background command was "
        "running, so it was stopped before it finished. Run it again if you "
        "still need it."
    )


async def _announce_interrupted_tasks(rows: list) -> int:
    """Rebuild the registry for, and wake once, every parent the restart hit.

    The settle frames above fix the UI, but the parent itself was never told:
    the ownership marks that admit a terminal to the scheduler lived in the
    dead process, so a parent parked on its workers stayed parked and its
    ``TaskList``/``ListAgents`` came back empty. Here each affected session's
    registry is rebuilt from the rows (interrupted tasks read "interrupted")
    and each parent gets ONE wake listing its interrupted tasks.

    Not woken: a session that is itself an interrupted worker (its parent is
    told, and resuming it is the parent's call), and the routing copy a
    nested dispatch writes on the root session (the direct owner is told,
    the same rule ``agent_messaging.peer_rows`` applies). Returns the number
    of sessions woken.
    """
    from app.core import turn_scheduler
    from app.core.task_registry import (
        INTERRUPTED_STATUS, SERVER_RESTART_REASON, task_registry,
    )
    from app.db import sessions as sessions_db

    # A row whose terminal notice the parent already had is not news: it was
    # a finished run's row left "running" by a missed settle, and announcing
    # it woke the parent on every restart (#1758). A resumed run clears the
    # latch, so live work is still announced.
    rows = [
        row for row in rows
        if not (row.get("terminal_notified") if isinstance(row, dict)
                else (row[9] if len(row) > 9 else False))
    ]
    by_session: Dict[str, list] = {}
    for row in rows:
        session_id = _row_value(row, "session_id", 1)
        if session_id:
            by_session.setdefault(session_id, []).append(row)

    for session_id in by_session:
        await task_registry.ensure_hydrated(session_id)

    interrupted_workers = {
        _row_value(row, "task_id", 0)
        for row in rows
        if (_row_value(row, "kind", 4) or "") == "agent"
    }

    def _is_direct_owner(owner: str, task_id: str) -> bool:
        # A worker nested under another worker is recorded on the root too;
        # the root is not its owner when another holder is the root's worker.
        holders = [
            sid for sid, sid_rows in by_session.items()
            if any(_row_value(r, "task_id", 0) == task_id for r in sid_rows)
        ]
        return not any(
            other != owner
            and (task_registry.get_task(owner, other) or {}).get("kind") == "agent"
            for other in holders
        )

    woken = 0
    for session_id, session_rows in by_session.items():
        if session_id in interrupted_workers:
            continue
        frames = []
        for row in session_rows:
            task_id = _row_value(row, "task_id", 0)
            if not task_id or not _is_direct_owner(session_id, task_id):
                continue
            stranded = len(by_session.get(task_id, ())) if task_id in interrupted_workers else 0
            frames.append({
                "task_id": task_id,
                "tool_use_id": _row_value(row, "tool_use_id", 2),
                "status": INTERRUPTED_STATUS,
                "reason": SERVER_RESTART_REASON,
                "kind": _row_value(row, "kind", 4) or "shell",
                "summary": _interrupted_summary(row, stranded),
            })
        if not frames:
            continue
        try:
            session = await asyncio.to_thread(sessions_db.get_session, session_id)
        except Exception:  # noqa: BLE001 — fail open: a lookup blip must not lose the notice
            session = True
        if not session:
            continue
        if await turn_scheduler.on_tasks_interrupted_by_restart(session_id, frames):
            woken += 1
    if woken:
        logger.info(
            "Boot reconcile: told %d session(s) which background tasks the "
            "restart interrupted", woken,
        )
    return woken


# Grace before the periodic sweep settles a non-terminal durable task row the
# in-memory registry does not know. Long enough to never race the start-write /
# persistence-flush window; short enough that a stranded chip settles within
# one glance-back. (Rows the registry DOES know are skipped regardless of age.)
STALE_TASK_ROW_GRACE_SECONDS = 15 * 60


async def settle_stale_background_task_rows(
    grace_seconds: int = STALE_TASK_ROW_GRACE_SECONDS,
) -> int:
    """Periodic sibling of the boot sweep: settle orphaned durable task rows.

    The boot reconcile only runs at boot, but an install can stay up for weeks —
    any settle path missed at teardown time (historically: routine eviction
    clearing the registry under a live Monitor shell) left the durable row and
    the UI chip "running" until the next restart. Every 5 minutes
    (``resource_monitor._periodic_cleanup``) this settles own-instance
    non-terminal rows older than the grace window whose task the in-memory
    registry no longer knows: flip the row to killed and emit the terminal
    ``task.updated`` frame the frontend needs to settle the chip.

    A row whose task the registry still RUNS is skipped: that is genuinely
    live work (e.g. a long Monitor surviving an eviction deferral), and the
    self-update active-work check reads the same registry. A row whose task the
    registry already FINISHED gets the registry's terminal status: its terminal
    snapshot is fire-and-forget and a failed write is dropped, and the live
    record stays in the registry until the session is removed, so skipping it
    left the row "running" indefinitely. Returns the number of rows settled;
    never raises.
    """
    from app.core.task_registry import task_registry
    from app.core.turn_recorder import record_task_settle_frame
    from app.db.background_tasks import (
        TERMINAL_STATUSES,
        list_stale_nonterminal_background_tasks,
        mark_background_task_killed,
        settle_background_task_row,
    )

    try:
        rows = await asyncio.to_thread(
            list_stale_nonterminal_background_tasks, grace_seconds
        )
    except Exception:  # noqa: BLE001 — a sweep failure must never break cleanup
        logger.warning("Stale background-task sweep query failed", exc_info=True)
        return 0

    settled = 0
    for row in rows:
        session_id = _row_value(row, "session_id", 1)
        task_id = _row_value(row, "task_id", 0)
        tool_use_id = _row_value(row, "tool_use_id", 2)
        try:
            known = task_registry.get_task(session_id, task_id)
            if known is None:
                status, summary = "killed", "Background task stopped (stale)"
                await asyncio.to_thread(
                    mark_background_task_killed, task_id, session_id
                )
            elif known.get("status") in TERMINAL_STATUSES:
                # Finished in memory; only its terminal write was lost.
                status = known["status"]
                summary = known.get("summary") or "Background task finished"
                if not await asyncio.to_thread(
                    settle_background_task_row, task_id, session_id, status
                ):
                    continue
            else:
                continue
            from app.core.task_persistence import broadcast_running_task_presence
            await broadcast_running_task_presence([session_id])
            if task_registry.mark_chipped(session_id, task_id, "terminal"):
                await record_task_settle_frame(
                    session_id, task_id, status, summary,
                    reason="stale_reap" if known is None else known.get("reason"),
                    tool_use_id=tool_use_id,
                )
            settled += 1
            # The session may have been parked on exactly this row. Nothing
            # else clears the marker between turns, so do it here (clear-only,
            # and a no-op while other background work survives).
            from app.core.turn_finalize import clear_parked_marker_if_idle
            await clear_parked_marker_if_idle(session_id)
            logger.warning(
                "Stale-task sweep: settled background task %s (session %s) as "
                "%s — row was non-terminal with %s",
                task_id, session_id, status,
                "no registry record" if known is None else "a finished registry record",
            )
        except Exception:  # noqa: BLE001 — one bad row must not block the rest
            logger.warning(
                "Stale-task sweep: settling %s/%s failed", session_id, task_id,
                exc_info=True,
            )
    # Same tick, same class of orphan: a marker left behind by a terminal this
    # sweep did not have to make.
    await sweep_stale_parked_markers()
    return settled


async def sweep_stale_parked_markers() -> int:
    """Clear idle parked markers and parks left behind by completed wakes.

    The backstop behind ``turn_finalize.clear_parked_marker_if_idle``. That
    call rides the terminal-task callback, which covers terminals the idle
    reader drains and the two settle paths above — but a task can also be
    settled by ``stop_session_task``, by force cleanup, or by a worker
    supervisor, none of which pass through the reader. Any marker those leave
    behind is caught here on the ordinary 5-minute tick instead of surviving
    until the next boot. It also closes older parked turn rows when a later
    turn has completed: the session marker may already have been cleared by
    that wake, so the row query includes those markerless orphans too.

    Only own-instance rows, and only when BOTH the in-memory registry and the
    durable mirror agree there is nothing running — a session whose worker
    outlived a registry eviction must keep its marker. Returns the number
    cleared; never raises.
    """
    _ph = ph()
    instance_id = settings.effective_instance_id
    try:
        rows = await asyncio.to_thread(_sessions_with_parked_marker, _ph, instance_id)
    except Exception:  # noqa: BLE001 — a sweep failure must never break cleanup
        logger.warning("Parked-marker sweep query failed", exc_info=True)
        return 0
    if not rows:
        return 0

    from app.core import pending_reconcile
    from app.core.task_registry import task_registry
    from app.core.turn_finalize import clear_parked_marker_if_idle
    from app.db import turns as turns_db

    cleared = 0
    for row in rows:
        session_id = _row_value(row, "id", 0)
        if not session_id:
            continue
        try:
            outstanding = await asyncio.to_thread(
                pending_reconcile.nonterminal_agent_task_ids, session_id
            )
            if outstanding:
                continue
            repaired = False
            # An earlier park may outlive a completed wake even though that
            # wake already cleared the session marker. Repair only at rest:
            # queued or live work still owns the context of its parked rows.
            completed_started_at = _row_value(row, "completed_started_at", 1)
            if (completed_started_at is not None
                    and not task_registry.get_parking_background_work(session_id)
                    and not task_registry.has_unresolved_background_launches(session_id)
                    and not await asyncio.to_thread(turns_db.session_has_unsettled_turns, session_id)):
                repaired = bool(await asyncio.to_thread(
                    turns_db.settle_parked_turns, session_id,
                    resolved_before=completed_started_at,
                ))
            if await clear_parked_marker_if_idle(session_id) or repaired:
                cleared += 1
        except Exception:  # noqa: BLE001 — one bad row must not block the rest
            logger.warning(
                "Parked-marker sweep: clearing %s failed", session_id,
                exc_info=True,
            )
    if cleared:
        logger.info(
            "Parked-marker sweep: repaired %d session(s) with resolved parks or "
            "markers and no background work left", cleared,
        )
    return cleared


def _sessions_with_parked_marker(_ph: str, instance_id: str) -> Any:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT id, (
                     SELECT MAX(started_at) FROM turns
                     WHERE session_id = sessions.id AND state = 'completed'
                 ) AS completed_started_at FROM sessions
                 WHERE instance_id = {_ph} AND (
                     parked_task_kind IS NOT NULL OR EXISTS (
                         SELECT 1 FROM turns parked
                         WHERE parked.session_id = sessions.id AND parked.state = 'parked'
                         AND EXISTS (
                             SELECT 1 FROM turns later
                             WHERE later.session_id = sessions.id AND later.state = 'completed'
                             AND later.started_at >= parked.ended_at
                         )
                     )
                 )""",
            (instance_id,),
        )
        return cursor.fetchall()


PARK_WATCHDOG_REASON = "park_watchdog"

# Sessions already reported at the WARN stage, so a 30-minute-old park is not
# re-reported on every 5-minute tick. Per-process and deliberately unbounded-
# looking: it only holds ids of sessions parked past the warn threshold, and a
# restart legitimately re-reports (the boot reconcile has settled the rows by
# then anyway).
_park_warned: set = set()


async def sweep_stalled_parks(
    warn_seconds: Optional[int] = None,
    force_seconds: Optional[int] = None,
) -> int:
    """Report — and past a hard ceiling, break — a park that never woke.

    A session parked on background work is supposed to wake when that work
    settles. When the settle never arrives the park is silent AND unbounded.
    From outside, a lost
    wake looks exactly like a healthy long wait, so the only honest treatment
    is a time bound with two stages:

    * WARN (``park_watchdog_warn_seconds``) — log it and record a durable
      error row so the stall is visible in the app's error UI. Reported once
      per session per process; nothing is touched.
    * FORCE (``park_watchdog_force_seconds``, 0 disables) — settle this
      session's still-running task rows and fire their terminal frames, which
      is what actually wakes the session, then settle the stale ``parked``
      turn rows so its lifecycle is not stuck forever. Deliberately generous:
      a legitimate overnight CI run or training job must never be reaped by a
      timer, and the frames say plainly that the app forced them.

    Runs on the resource monitor's 5-minute tick beside
    :func:`settle_stale_background_task_rows`. Returns the number of sessions
    forced; never raises.
    """
    from app.core.config import settings as _settings
    from app.db import turns as turns_db

    if not getattr(_settings, "park_watchdog_enabled", True):
        return 0
    warn = int(
        warn_seconds
        if warn_seconds is not None
        else getattr(_settings, "park_watchdog_warn_seconds", 1800)
    )
    ceiling = int(
        force_seconds
        if force_seconds is not None
        else getattr(_settings, "park_watchdog_force_seconds", 3600)
    )
    if warn <= 0:
        return 0

    try:
        rows = await asyncio.to_thread(turns_db.list_stalled_parked_turns, warn)
    except Exception:  # noqa: BLE001 — a sweep failure must never break cleanup
        logger.warning("Park watchdog query failed", exc_info=True)
        return 0

    forced = 0
    for row in rows:
        session_id = _row_value(row, "session_id", 0)
        turn_id = _row_value(row, "turn_id", 1)
        try:
            parked_seconds = int(float(_row_value(row, "parked_seconds", 2) or 0))
        except (TypeError, ValueError):
            parked_seconds = warn
        if not session_id:
            continue
        try:
            from app.core.limit_resume import paused_workers_of

            if await asyncio.to_thread(paused_workers_of, session_id):
                # Waiting on a Task worker a usage limit paused: its resume is
                # scheduled for a known time, so this park is not lost (#1831).
                continue
        except Exception:  # noqa: BLE001 — fall through to the usual handling
            logger.debug("Park watchdog: paused-worker check failed", exc_info=True)
        try:
            if ceiling > 0 and parked_seconds >= ceiling:
                await _force_stalled_park(session_id, turn_id, parked_seconds)
                _park_warned.discard(session_id)
                forced += 1
            elif session_id not in _park_warned:
                _park_warned.add(session_id)
                logger.warning(
                    "Park watchdog: session %s has been parked for %ss with no "
                    "wake (turn %s)", session_id, parked_seconds, turn_id,
                )
                await run_sync(
                    record_error,
                    "backend",
                    f"Session {session_id} has been parked on background work "
                    f"for {parked_seconds}s with no wake. Either the work is "
                    "genuinely long, or a completion was lost and the session "
                    "cannot move on its own.",
                    level="WARNING",
                    session_id=session_id,
                    context={
                        "watchdog": PARK_WATCHDOG_REASON,
                        "turn_id": turn_id,
                        "parked_seconds": parked_seconds,
                    },
                )
        except Exception:  # noqa: BLE001 — one bad session must not block the rest
            logger.warning(
                "Park watchdog: handling stalled park for %s failed", session_id,
                exc_info=True,
            )
    return forced


async def _force_stalled_park(
    session_id: str, turn_id: Any, parked_seconds: int
) -> None:
    """Break one stalled park: settle its tasks, fire their frames, unstick it."""
    from app.core.task_registry import task_registry
    from app.core.turn_recorder import record_task_settle_frame
    from app.db import turns as turns_db
    from app.db.background_tasks import (
        list_session_nonterminal_background_tasks,
        mark_background_task_killed,
    )

    summary = (
        f"Stopped: this task was still marked running after the session had "
        f"been parked on it for {parked_seconds}s with no completion. The app "
        "settled it so the session could move on — it was not a failure, and "
        "its result (if any) was never delivered."
    )
    try:
        rows = await asyncio.to_thread(
            list_session_nonterminal_background_tasks, session_id
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Park watchdog: task lookup for %s failed", session_id, exc_info=True
        )
        rows = []

    for row in rows:
        task_id = _row_value(row, "task_id", 0)
        tool_use_id = _row_value(row, "tool_use_id", 2)
        if not task_id:
            continue
        try:
            await asyncio.to_thread(mark_background_task_killed, task_id, session_id)
            from app.core.task_persistence import broadcast_running_task_presence
            await broadcast_running_task_presence([session_id])
            if task_registry.get_task(session_id, task_id) is not None:
                task_registry.complete_task(
                    session_id, task_id, "killed",
                    summary=summary, reason=PARK_WATCHDOG_REASON,
                )
            # OWN-lane admission, same as the Monitor spawner's: without it
            # on_task_terminal drops the frame as not-own work and the park
            # this sweep exists to break survives the sweep.
            task_registry.mark_monitor_shell_task(session_id, task_id)
            if task_registry.mark_chipped(session_id, task_id, "terminal"):
                await record_task_settle_frame(
                    session_id, task_id, "killed", summary,
                    reason=PARK_WATCHDOG_REASON, tool_use_id=tool_use_id,
                )
            from app.core import turn_scheduler

            await turn_scheduler.on_task_terminal(session_id, {
                "task_id": task_id,
                "status": "killed",
                "summary": summary,
                "kind": _row_value(row, "kind", 5) or "shell",
            })
        except Exception:  # noqa: BLE001 — one bad row must not block the rest
            logger.warning(
                "Park watchdog: forcing terminal frame for %s/%s failed",
                session_id, task_id, exc_info=True,
            )

    try:
        await asyncio.to_thread(turns_db.settle_parked_turns, session_id)
    except Exception:  # noqa: BLE001
        logger.debug("Park watchdog: settle_parked_turns failed", exc_info=True)

    # Everything this session was parked on is terminal now; drop the durable
    # marker too, or the session keeps rendering as parked after the watchdog
    # has already broken the park.
    try:
        from app.core.turn_finalize import clear_parked_marker_if_idle

        await clear_parked_marker_if_idle(session_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "Park watchdog: parked-marker clear failed", exc_info=True
        )

    logger.warning(
        "Park watchdog: forced %d task frame(s) for session %s parked %ss "
        "with no wake (turn %s)", len(rows), session_id, parked_seconds, turn_id,
    )
    await run_sync(
        record_error,
        "backend",
        f"Session {session_id} was parked on background work for "
        f"{parked_seconds}s with no wake; the park watchdog settled "
        f"{len(rows)} task row(s) and woke it.",
        level="WARNING",
        session_id=session_id,
        context={
            "watchdog": PARK_WATCHDOG_REASON,
            "turn_id": turn_id,
            "parked_seconds": parked_seconds,
            "tasks": len(rows),
        },
    )


async def _repair_orphaned_engine_pending() -> int:
    """Clear phantom ids out of this install's engine pending sets.

    fluid's ``_background_pending`` is rebuilt from the ``SubagentEvent`` log on
    every rehydrate and popped only by a terminal event, so a completion that
    never reached the engine is re-created as a phantom for the life of the
    session and listed on every ``SessionParked``.
    ``turn_scheduler.retire_task_terminals`` stops new ones; it cannot undo a
    log that is already missing the terminal.

    This appends the missing terminal events itself, for ids the durable
    ``background_tasks`` rows prove are over. Records one error row when it
    repairs anything so the damage is visible in the app's error UI. Returns
    the number of task ids repaired; never raises.
    """
    from app.core import pending_reconcile

    repaired = await pending_reconcile.sweep_orphaned_engine_pending()
    if repaired:
        logger.warning(
            "Boot reconcile: repaired %d orphaned engine-pending task id(s)",
            repaired,
        )
        await run_sync(
            record_error,
            "backend",
            f"Boot reconcile: {repaired} background task(s) were still listed "
            "as pending by their session's engine although the app had already "
            "recorded them finished; wrote the missing terminal events so those "
            "sessions stop parking on dead work.",
            level="WARNING",
            context={"reconcile": "engine_pending", "tasks": repaired},
        )
    return repaired


def _reset_orphaned_session_statuses() -> int:
    """Reset own-instance sessions stuck in a live-looking ``status``.

    Defensive: the app only writes "active"/"archived" today, so streaming/
    parked/running here means the row drifted (older/newer version, manual
    edit). On a fresh boot nothing is streaming, so reset to "active".
    Returns the number reset.
    """
    _ph = ph()
    instance_id = settings.effective_instance_id
    placeholders = ", ".join([_ph] * len(_LIVE_LOOKING_STATUSES))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT id, status FROM sessions
                 WHERE instance_id = {_ph} AND status IN ({placeholders})""",
            (instance_id, *_LIVE_LOOKING_STATUSES),
        )
        rows = cursor.fetchall()
        if rows:
            cursor.execute(
                f"""UPDATE sessions SET status = 'active'
                     WHERE instance_id = {_ph} AND status IN ({placeholders})""",
                (instance_id, *_LIVE_LOOKING_STATUSES),
            )

    for row in rows:
        session_id = _row_value(row, "id", 0)
        status = _row_value(row, "status", 1)
        record_error(
            "backend",
            f"Boot reconcile: session {session_id} had orphaned "
            f"status='{status}' after a restart; reset to 'active' — nothing "
            "can be streaming on a fresh boot.",
            level="WARNING",
            session_id=session_id,
            context={"reconcile": "session_status", "previous_status": status},
        )
    return len(rows)


def _sweep_stray_cli_processes() -> int:
    """Terminate CLI processes leaked by a previous run of THIS install.

    Runs before this server has spawned anything, so every positively
    attributed process is by definition a stray. Attribution is conservative:

    - the process environment must carry ``AI_SHUFFLE_MANAGED_BY`` equal to
      this install's instance id (injected into every spawn; unreadable env →
      unattributable → never killed);
    - the process must look like the Claude CLI (name/cmdline), so arbitrary
      marked descendants (e.g. a dev server an agent intentionally left
      running) are not swept — only marked children OF a matched CLI go with
      it, since they die with their stdio anyway;
    - our own process, ancestors, and descendants are always excluded (the
      server itself may have been restarted from inside an agent shell, in
      which case our ancestor chain carries the marker too).

    Returns the number of processes terminated.
    """
    try:
        import psutil
    except ImportError:
        logger.debug("psutil unavailable — skipping stray CLI sweep")
        return 0

    expected = settings.effective_instance_id
    me = psutil.Process()
    protected = {me.pid}
    try:
        protected.update(p.pid for p in me.parents())
        protected.update(c.pid for c in me.children(recursive=True))
    except psutil.Error:
        pass

    def _marked(proc: "psutil.Process") -> bool:
        try:
            return proc.environ().get(CLI_ENV_MARKER) == expected
        except (psutil.Error, OSError):
            # Cannot read the environment → cannot positively attribute →
            # never kill.
            return False

    victims: Dict[int, "psutil.Process"] = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid in protected or proc.pid in victims:
                continue
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or ()).lower()
            if "claude" not in name and "claude" not in cmdline:
                continue
            if not _marked(proc):
                continue
            victims[proc.pid] = proc
            for child in proc.children(recursive=True):
                if child.pid not in protected and _marked(child):
                    victims[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not victims:
        return 0

    procs = list(victims.values())
    for proc in procs:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass

    pids = sorted(victims.keys())
    logger.warning(
        "Boot reconcile: terminated %d stray CLI process(es) leaked by a "
        "previous server run: pids=%s", len(pids), pids,
    )
    record_error(
        "backend",
        f"Boot reconcile: terminated {len(pids)} stray Claude CLI "
        "process(es) leaked by the previous server run.",
        level="WARNING",
        context={"reconcile": "stray_cli", "pids": pids},
    )
    return len(pids)


def _reap_dead_lease_turns() -> "list[Dict[str, Any]]":
    """Reap every dead-lease live turn (v2 turns). The previous process's live
    turns cannot survive a restart: their run leases are dead, so
    ``turns.reap_expired`` requeues the user/queue work (front of queue) and
    aborts the rest. Returns the reaped rows (as they were BEFORE resolution) so
    the caller can kick each affected session."""
    from app.db import turns as turns_db

    return turns_db.reap_expired()


async def _boot_reap_and_kick_turns() -> int:
    """Boot sweep (v2 turns): reap dead-lease live turns, then kick each
    affected session so a requeued turn restarts. Flag-gated by the caller.
    Returns the number of turns reaped."""
    from app.core import turn_scheduler

    reaped = await asyncio.to_thread(_reap_dead_lease_turns)
    if not reaped:
        return 0
    for session_id in {r["session_id"] for r in reaped}:
        turn_scheduler.kick(session_id)
    return len(reaped)


async def reconcile_from_durable_truth() -> Dict[str, int]:
    """Run every boot-time reconcile branch; never raises.

    Called once from the startup sequence (app/main.py) after core DB init and
    before any session machinery starts. Each branch is guarded independently
    so one failure cannot block the others — or boot. Returns per-branch
    counts for logging.
    """
    counts = {
        "parked_markers_cleared": 0,
        "statuses_reset": 0,
        "stray_cli_reaped": 0,
        "background_task_rows_killed": 0,
        "turns_reaped": 0,
        "engine_pending_repaired": 0,
        "orphan_processes_reaped": 0,
    }

    # V2 turns boot sweep: settle/requeue the previous process's dead-lease live
    # turns and kick affected sessions. Guarded independently.
    try:
        counts["turns_reaped"] = await _boot_reap_and_kick_turns()
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: reaping dead-lease turns failed")
        await run_sync(
            record_error,
            "backend",
            f"Boot reconcile failed while reaping dead-lease turns: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "turns_reap"},
        )

    try:
        counts["parked_markers_cleared"] = await asyncio.to_thread(
            _clear_stale_parked_markers
        )
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: clearing stale parked markers failed")
        await run_sync(
            record_error,
            "backend",
            f"Boot reconcile failed while clearing stale parked markers: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "parked_task_kind"},
        )

    try:
        counts["background_task_rows_killed"] = await _mark_stale_background_task_rows()
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: marking stale background-task rows failed")
        await run_sync(
            record_error,
            "backend",
            f"Boot reconcile failed while marking stale background-task rows: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "background_tasks"},
        )

    # Pending-set repair. Runs AFTER the stale-row sweep above: that sweep is
    # what settles this install's leftover non-terminal rows, and the repair
    # only ever writes a terminal for an id the durable rows can prove is over.
    # A phantom lives in the fluid event log, so nothing but a write into that
    # log clears it — the session never repairs itself.
    try:
        counts["engine_pending_repaired"] = await _repair_orphaned_engine_pending()
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: engine pending-set repair failed")
        await run_sync(
            record_error,
            "backend",
            f"Boot reconcile failed while repairing engine pending sets: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "engine_pending"},
        )

    try:
        counts["statuses_reset"] = await asyncio.to_thread(
            _reset_orphaned_session_statuses
        )
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: resetting orphaned session statuses failed")
        await run_sync(
            record_error,
            "backend",
            f"Boot reconcile failed while resetting orphaned session statuses: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "session_status"},
        )

    if _process_sweeps_enabled():
        try:
            counts["stray_cli_reaped"] = await asyncio.to_thread(
                _sweep_stray_cli_processes
            )
        except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
            logger.exception("Boot reconcile: stray CLI sweep failed")
            await run_sync(
                record_error,
                "backend",
                f"Boot reconcile failed while sweeping stray CLI processes: {exc}",
                detail=traceback.format_exc(),
                context={"reconcile": "stray_cli"},
            )

        # Catches what the marker sweep above deliberately leaves alone, e.g.
        # a dev server orphaned when the server died.
        counts["orphan_processes_reaped"] = await _reap_previous_run_processes()

    if any(counts.values()):
        logger.info(f"Boot reconcile from durable truth: {counts}")
    else:
        logger.debug("Boot reconcile from durable truth: nothing to reconcile")
    return counts


def _process_sweeps_enabled() -> bool:
    """Whether the boot process sweeps run. Suppressed under TESTING (conftest
    sets TESTING=1): they scan the whole process table, which is slow and
    pointless inside the test suite."""
    return bool(settings.reap_stray_cli_on_boot) and os.environ.get("TESTING") != "1"


async def _reap_previous_run_processes() -> int:
    """Stop what this install's own dead server runs left behind; never raises.

    Processes the previous run recorded itself as starting (agent CLIs,
    Monitor shells and what they started), verified by pid + start time +
    command line, and any process that inherited a dead run's id
    (``process_ledger.RUN_MARKER``). Both lists live in this install's data
    directory, so nothing here can name another install's processes, the
    managed PostgreSQL or the native Chrome (started without the run id).
    Returns the number of processes stopped.
    """
    try:
        from app.core import process_ledger

        return len(await asyncio.to_thread(process_ledger.reap_previous_run))
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: process-ledger sweep failed")
        await run_sync(
            record_error,
            "backend",
            f"Boot reconcile failed while stopping the previous run's processes: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "process_ledger"},
        )
        return 0


async def reconcile_at_boot(runner_approved: bool) -> Dict[str, int]:
    """The boot recovery for this install; never raises.

    An approved install runs every branch of :func:`reconcile_from_durable_truth`.
    An install awaiting approval still claims and runs turns, so after a crash
    it must still stop the processes its own previous run left behind (#1711);
    it runs only that, the process-ledger and dead-run-id reap. Everything that
    writes shared database state (turns, sessions, background-task rows, the
    engine's pending sets) and the instance-marker CLI sweep stay gated on
    approval.
    """
    if runner_approved:
        return await reconcile_from_durable_truth()
    counts = {"orphan_processes_reaped": 0}
    if _process_sweeps_enabled():
        counts["orphan_processes_reaped"] = await _reap_previous_run_processes()
    if counts["orphan_processes_reaped"]:
        logger.info(f"Boot reconcile (install awaiting approval): {counts}")
    return counts

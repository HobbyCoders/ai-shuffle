"""Boot-time "reconcile from durable truth".

After a crash or restart the in-memory control planes are empty — the session
pool and the task registry hold nothing — but durable state written by the
previous run can still claim sessions are mid-flight:

- ``sessions.parked_task_kind`` (v51) is set when a turn ends with live
  background work (a sub-agent or an SDK-backgrounded shell — see
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

Branch (b) of the reconcile plan — recomputing the automation scheduler from
persisted state — is deliberately NOT duplicated here:
``automation_manager.start()`` already calls ``_reconcile_schedules()``
synchronously at boot and re-runs it every 60s
(``SCHEDULE_RECONCILE_SECONDS``), rebuilding the APScheduler jobs from the
``automations`` table. See ``app/core/automation_manager.py``.

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
    own-instance marker is an orphan. Returns the number cleared.
    """
    _ph = ph()
    instance_id = settings.effective_instance_id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT id, parked_task_kind FROM sessions
                 WHERE instance_id = {_ph} AND parked_task_kind IS NOT NULL""",
            (instance_id,),
        )
        rows = cursor.fetchall()
        if rows:
            cursor.execute(
                f"""UPDATE sessions SET parked_task_kind = NULL
                     WHERE instance_id = {_ph} AND parked_task_kind IS NOT NULL""",
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


async def _mark_stale_background_task_rows() -> int:
    """Mark this install's non-terminal durable task rows killed (v55).

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

    rows = await asyncio.to_thread(mark_stale_background_tasks_killed)
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
            record_error(
                "backend",
                f"Boot reconcile: failed to emit terminal frame for stale "
                f"background task {task_id} (session {session_id}): {exc}",
                level="WARNING",
                session_id=session_id,
                context={"reconcile": "background_tasks", "task_id": task_id},
            )

    count = len(rows)
    record_error(
        "backend",
        f"Boot reconcile: {count} durable background-task row(s) were "
        "still non-terminal when the server stopped; marked them killed "
        "— background work does not survive a restart.",
        level="WARNING",
        context={"reconcile": "background_tasks", "rows": count},
    )
    return count


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

    A row whose task the registry still tracks — ANY status — is skipped: a
    running record is genuinely live work (e.g. a long Monitor surviving an
    eviction deferral), and a terminal record persists its own truth. Returns
    the number of rows settled; never raises.
    """
    from app.core.task_registry import task_registry
    from app.core.turn_recorder import record_task_settle_frame
    from app.db.background_tasks import (
        list_stale_nonterminal_background_tasks,
        mark_background_task_killed,
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
            if task_registry.get_task(session_id, task_id) is not None:
                continue
            await asyncio.to_thread(
                mark_background_task_killed, task_id, session_id
            )
            from app.core.task_persistence import broadcast_running_task_presence
            await broadcast_running_task_presence([session_id])
            if task_registry.mark_chipped(session_id, task_id, "terminal"):
                await record_task_settle_frame(
                    session_id, task_id, "killed",
                    "Background task stopped (stale)",
                    reason="stale_reap", tool_use_id=tool_use_id,
                )
            settled += 1
            # The session may have been parked on exactly this row. Nothing
            # else clears the marker between turns, so do it here (clear-only,
            # and a no-op while other background work survives).
            from app.core.turn_finalize import clear_parked_marker_if_idle
            await clear_parked_marker_if_idle(session_id)
            logger.warning(
                "Stale-task sweep: settled orphaned background task %s "
                "(session %s) — row was non-terminal with no registry record",
                task_id, session_id,
            )
        except Exception:  # noqa: BLE001 — one bad row must not block the rest
            logger.warning(
                "Stale-task sweep: settling %s/%s failed", session_id, task_id,
                exc_info=True,
            )
    return settled


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
    settles. When the settle never arrives the park is silent AND unbounded:
    the audited session sat parked 9m37s past a green CI run and only moved
    because an unrelated force-cleanup killed the shell. From outside, a lost
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
                record_error(
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
    record_error(
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
    """Clear phantom ids out of this install's engine pending sets (v-audit F1).

    fluid's ``_background_pending`` is rebuilt from the ``SubagentEvent`` log on
    every rehydrate and popped only by a terminal event, so a completion that
    never reached the engine is re-created as a phantom for the life of the
    session and listed on every ``SessionParked``. The round-6 retirement fix
    stops new ones; it cannot undo a log that is already missing the terminal.

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
        record_error(
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
    }

    # V2 turns boot sweep: settle/requeue the previous process's dead-lease live
    # turns and kick affected sessions. Guarded independently.
    try:
        counts["turns_reaped"] = await _boot_reap_and_kick_turns()
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: reaping dead-lease turns failed")
        record_error(
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
        record_error(
            "backend",
            f"Boot reconcile failed while clearing stale parked markers: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "parked_task_kind"},
        )

    try:
        counts["background_task_rows_killed"] = await _mark_stale_background_task_rows()
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: marking stale background-task rows failed")
        record_error(
            "backend",
            f"Boot reconcile failed while marking stale background-task rows: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "background_tasks"},
        )

    # One-off pending-set repair (2026-09-05 audit F1). Runs AFTER the stale-row
    # sweep above: that sweep is what settles this install's leftover
    # non-terminal rows, and the repair only ever writes a terminal for an id
    # the durable rows can prove is over. Sessions poisoned before the round-6
    # retirement fix shipped carry their phantoms in the fluid event log, so
    # nothing but a write into that log clears them — a session parked on a
    # twenty-one-hour-dead task never repairs itself.
    try:
        counts["engine_pending_repaired"] = await _repair_orphaned_engine_pending()
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: engine pending-set repair failed")
        record_error(
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
        record_error(
            "backend",
            f"Boot reconcile failed while resetting orphaned session statuses: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "session_status"},
        )

    # Suppressed under TESTING (conftest sets TESTING=1): the sweep scans the
    # whole process table, which is slow and pointless inside the test suite.
    if settings.reap_stray_cli_on_boot and os.environ.get("TESTING") != "1":
        try:
            counts["stray_cli_reaped"] = await asyncio.to_thread(
                _sweep_stray_cli_processes
            )
        except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
            logger.exception("Boot reconcile: stray CLI sweep failed")
            record_error(
                "backend",
                f"Boot reconcile failed while sweeping stray CLI processes: {exc}",
                detail=traceback.format_exc(),
                context={"reconcile": "stray_cli"},
            )

    if any(counts.values()):
        logger.info(f"Boot reconcile from durable truth: {counts}")
    else:
        logger.debug("Boot reconcile from durable truth: nothing to reconcile")
    return counts

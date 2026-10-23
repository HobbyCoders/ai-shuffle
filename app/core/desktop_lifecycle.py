"""Stop this process's sessions before the desktop terminates its worker."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Shutdown waits this long for stopped turns to settle. The container
# supervisor gives the whole stop-sessions call 35 seconds; a turn that has
# not unwound by then is ended by the process exit.
SHUTDOWN_SETTLE_SECONDS = 25.0


def active_session_ids() -> set[str]:
    from app.core.session_runtime import active_executions
    from app.core.task_registry import task_registry

    return set(active_executions().values()) | set(task_registry.active_session_ids())


async def reconciled_active_session_ids() -> set[str]:
    """``active_session_ids`` after dropping executions whose turn already ended.

    Use before deciding anything on "is this worker busy": an execution can
    outlive its turn (session_runtime.reconcile_executions), and the stale
    entry would otherwise refuse every restart until the process restarted.
    """
    from app.core import session_runtime

    try:
        await session_runtime.reconcile_executions()
    except Exception:  # noqa: BLE001 — unreconciled is the old, stricter answer.
        logger.warning("Could not reconcile stuck turn executions", exc_info=True)
    return active_session_ids()


def _log_stop_failure(task: asyncio.Future) -> None:
    """Observe stops that may outlive the shutdown/update budget."""
    if not task.cancelled() and task.exception() is not None:
        logger.warning("Could not stop work before exiting", exc_info=task.exception())


async def force_stop_sessions(session_ids: set[str], *, source: str, grace: float) -> list[str]:
    """End work in ``session_ids`` that a Stop did not end in time.

    For an application update only: the process restarts next, which ends
    anything still running anyway. Turn executions are cancelled (and, if
    one still will not unwind, no longer counted); background tasks still
    registered as running are stopped, or marked killed when nothing is left
    to stop. All stops run concurrently within one ``grace`` budget; unfinished
    handlers may continue until process exit. Returns the session ids that had
    work forced.
    """
    deadline = asyncio.get_running_loop().time() + grace
    from app.core import session_runtime
    from app.core.query_session_lifecycle import stop_session_task
    from app.core.task_registry import task_registry

    forced = set(session_runtime.active_executions().values()) & session_ids
    background = [(sid, task["id"]) for sid in sorted(session_ids)
                  for task in task_registry.get_active(sid) if task.get("id")]
    forced.update(sid for sid, _ in background)
    # Turn pumps and background stops share one budget, not one per task.
    # The two execution-cancel phases each get half of the overall grace.
    stops = [asyncio.create_task(session_runtime.force_end_executions(set(session_ids), grace=grace / 2))]
    stops.extend(asyncio.create_task(stop_session_task(sid, tid, source=source)) for sid, tid in background)
    for stop in stops:
        stop.add_done_callback(_log_stop_failure)
    _, pending = await asyncio.wait(stops, timeout=max(0, deadline - asyncio.get_running_loop().time()))
    if pending:
        logger.warning("Application update: %d force-stop calls exceeded %.1fs; the exit ends them",
                       len(pending), grace)
    # Do not wait for cancellation-resistant stop handlers. They can finish in
    # the background until exit; the registry must not keep blocking the update.
    for sid, tid in background:
        record = task_registry.get_task(sid, tid)
        if record is not None and record.get("status") == "running":
            task_registry.complete_task(sid, tid, "killed", summary=f"Stopped by host: {source}",
                                        reason=source)
    return sorted(forced)


def remote_workers() -> list[str]:
    """Other workers running subagents for this worker's chats, by name.

    Quitting leaves them running there (#1808); the quit dialog names them.
    """
    from app.core.task_registry import task_registry

    names = set()
    for sid in task_registry.active_session_ids():
        for task in task_registry.get_active(sid):
            worker = task.get("worker") if isinstance(task.get("worker"), dict) else None
            if worker and worker.get("name"):
                names.add(str(worker["name"]))
    return sorted(names)


# How many running chats a refusal names; the rest are counted.
BUSY_LIMIT = 8


def busy_work() -> list[dict]:
    """Name live turns and surviving background work (owner-only display data).

    Keep legacy task names; task_details explains why an idle chat still blocks.
    Serving idle deadlines are estimates on the monitor's session-idle basis.
    """
    import re
    import time
    from datetime import datetime, timezone
    from app.core.task_registry import _iso, task_registry
    from app.core.export_redaction import redact_export_text
    from app.core.session_runtime import active_executions
    from app.core.monitor_spawn import serving_idle_budget_seconds, session_idle_seconds
    from app.mcp.builtin_tools import is_background_shell_running
    from app.db import database, turns

    rows = []
    executing = set(active_executions().values())
    for sid in sorted(active_session_ids())[:BUSY_LIMIT]:
        try:
            title = (database.get_session(sid) or {}).get("title")
        except Exception:  # noqa: BLE001 — naming never breaks a refusal.
            title = None
        tasks = task_registry.get_active(sid)
        details = []
        serving = any(task_registry.is_serving_monitor(sid, task["id"]) for task in tasks)
        ended, turn_state = None, None
        if serving and sid not in executing:
            try:
                latest = turns.get_latest_turn(sid) or {}
                ended, turn_state = _iso(latest.get("ended_at")), latest.get("state")
            except Exception:  # noqa: BLE001 — unavailable is not a finished chat.
                pass
        budget = serving_idle_budget_seconds() if serving else 0
        idle = session_idle_seconds(sid) if serving and budget else None
        now = time.time()
        for task in tasks:
            tid = task["id"]
            service = task_registry.is_serving_monitor(sid, tid)
            kind = "service" if service else task.get("kind") or "registry"
            label = redact_export_text(str(task.get("subject") or task.get("agent") or "Background task"))
            port_match = re.search(
                r"(?:--port(?:=|\s+)|(?:https?://[\w.-]+|localhost|(?:\d{1,3}\.){3}\d{1,3}|\[[\da-fA-F:]+\]):)(\d{1,5})\b",
                label,
            )
            port = int(port_match[1]) if port_match else None
            if port is not None and not 1 <= port <= 65535:
                port = None
            live = is_background_shell_running(tid) if kind in ("shell", "service") else False
            stop_at = None
            if service and live and budget and idle is not None:
                try:
                    started = datetime.fromisoformat(task["startedAt"].replace("Z", "+00:00"))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    stop_at = max(started.timestamp(), now - idle) + budget
                except (ValueError, TypeError, KeyError):
                    pass
            details.append({"task_id": tid, "kind": kind, "label": label[:120], "port": port,
                            "started_at": task.get("startedAt"), "live": live,
                            "stop_will_stop": kind in ("shell", "service"),
                            "monitor_serving_idle_seconds": budget if service else None,
                            "idle_stop_at": stop_at})
        rows.append({"session_id": sid, "title": title or "Untitled chat",
                     "tasks": [detail["label"] for detail in details][:5], "task_details": details,
                     "running_turn": sid in executing, "turn_ended_at": ended, "turn_state": turn_state})
    return rows


async def stop_session_work(sid: str, *, shutdown: bool = False, source: Optional[str] = None) -> None:
    """Stop one session's turn and background work, as its Stop button does.

    ``shutdown`` is the worker stopping, not the owner: a Task child another
    worker started here is then interrupted without a stop latch, so its
    report reads as a worker shutdown and a message can resume it once this
    worker is back (#1808).
    """
    from app.core import remote_task, session_runtime
    from app.core.query_session_lifecycle import stop_all_background_tasks
    from app.core.task_registry import SERVER_SHUTDOWN_REASON
    from app.db import turns

    if shutdown and await asyncio.to_thread(remote_task.report_owed, sid) is not None:
        runtime = session_runtime.get_live_runtime(sid)
        if runtime is not None:
            await asyncio.to_thread(
                turns.interrupt_turn, sid, "stop",
                stop_background=True, turn_id=runtime.turn_id,
                source=SERVER_SHUTDOWN_REASON,
            )
            await runtime.interrupt("stop", escalation_timeout=1.0, source=SERVER_SHUTDOWN_REASON)
        return
    # Latch first, cancel wakeups and queued turns, and stop parked tasks too.
    await stop_all_background_tasks(
        sid, user_stop=True, respect_shell_preference=False,
        **({"source": SERVER_SHUTDOWN_REASON} if shutdown else {}),
    )
    runtime = session_runtime.get_live_runtime(sid)
    if runtime is not None:
        await asyncio.to_thread(
            turns.interrupt_turn, sid, "stop",
            stop_background=True, turn_id=runtime.turn_id,
        )
        # The app stopped it, not the user: computer use says so (``source``
        # names why, e.g. a worker update or a move to another worker).
        await runtime.interrupt("stop", escalation_timeout=1.0,
                                source=source or (SERVER_SHUTDOWN_REASON if shutdown else "app_stop"))


async def stop_running_sessions() -> None:
    """Quiesce local work while the database and session clients are still alive.

    Do not sweep shared database rows: another desktop may own those sessions.
    Allow at most 25 seconds for stops, settlement/lease release and persistence
    together. Process exit ends work still unwinding; boot reconcile aborts a
    turn left behind with a dead lease.
    """
    deadline = asyncio.get_running_loop().time() + SHUTDOWN_SETTLE_SECONDS
    from app.core import session_runtime, startup, task_persistence

    startup.is_shutting_down = True
    executions = session_runtime.active_executions()
    session_ids = active_session_ids()

    stops = {asyncio.create_task(stop_session_work(sid, shutdown=True)) for sid in session_ids}
    for stop in stops:
        stop.add_done_callback(_log_stop_failure)
    if stops:
        done, _ = await asyncio.wait(stops, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        for stop in done:
            stop.result()
    # Ordinary shutdown leaves execute() alive to persist and release its lease.
    # The application-update force path instead settles before cancelling it.
    # Either way, never extend the shutdown budget for a wedged turn or stop.
    if executions:
        _, stuck = await asyncio.wait(set(executions), timeout=max(0, deadline - asyncio.get_running_loop().time()))
        for task in stuck:
            logger.warning("Shutdown: the turn in %s did not settle within %.0fs; the exit ends it",
                           executions.get(task), SHUTDOWN_SETTLE_SECONDS)
    flush = asyncio.create_task(task_persistence.flush())
    flush.add_done_callback(_log_stop_failure)
    await asyncio.wait({flush}, timeout=max(0, deadline - asyncio.get_running_loop().time()))
    if flush.done():
        flush.result()

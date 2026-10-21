"""Stop this process's sessions before the desktop terminates its worker."""
from __future__ import annotations

import asyncio
from typing import Optional


def active_session_ids() -> set[str]:
    from app.core.session_runtime import active_executions
    from app.core.task_registry import task_registry

    return set(active_executions().values()) | set(task_registry.active_session_ids())


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
            label = str(task.get("subject") or task.get("agent") or "Background task")
            port_match = re.search(r"(?:--port(?:=|\s+)|:)(\d{2,5})(?:\b|$)", label)
            port = int(port_match[1]) if port_match else None
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
    Waiting for the execution tasks also waits for their run leases to release.
    """
    from app.core import session_runtime, startup, task_persistence

    startup.is_shutting_down = True
    executions = session_runtime.active_executions()
    session_ids = active_session_ids()

    results = await asyncio.gather(
        *(stop_session_work(sid, shutdown=True) for sid in session_ids), return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    # Never cancel these tasks on an HTTP timeout: their finally blocks persist
    # the terminal state and release the lease.
    await asyncio.gather(*executions, return_exceptions=True)
    await task_persistence.flush()

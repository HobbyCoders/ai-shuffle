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
    """The running chats behind ``active_session_ids``, for a refusal to name.

    ``[{"session_id", "title", "tasks": [task names]}]``, at most BUSY_LIMIT
    rows. A title that cannot be read falls back to "Untitled chat".
    """
    from app.core.task_registry import task_registry
    from app.db import database

    rows = []
    for sid in sorted(active_session_ids())[:BUSY_LIMIT]:
        try:
            title = (database.get_session(sid) or {}).get("title")
        except Exception:  # noqa: BLE001 — a name is a nicety, never a failure
            title = None
        tasks = [str(task.get("subject") or task.get("agent") or "Background task")
                 for task in task_registry.get_active(sid)][:5]
        rows.append({"session_id": sid, "title": title or "Untitled chat", "tasks": tasks})
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

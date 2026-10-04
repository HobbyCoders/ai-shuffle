"""Stop this process's sessions before the desktop terminates its worker."""
from __future__ import annotations

import asyncio


def active_session_ids() -> set[str]:
    from app.core.session_runtime import active_executions
    from app.core.task_registry import task_registry

    return set(active_executions().values()) | set(task_registry.active_session_ids())


async def stop_running_sessions() -> None:
    """Quiesce local work while the database and session clients are still alive.

    Do not sweep shared database rows: another desktop may own those sessions.
    Waiting for the execution tasks also waits for their run leases to release.
    """
    from app.core import remote_task, session_runtime, startup, task_persistence
    from app.core.query_session_lifecycle import stop_all_background_tasks
    from app.core.task_registry import SERVER_SHUTDOWN_REASON
    from app.db import turns

    startup.is_shutting_down = True
    executions = session_runtime.active_executions()
    session_ids = active_session_ids()

    async def stop_session(sid: str) -> None:
        if await asyncio.to_thread(remote_task.report_owed, sid) is not None:
            # A Task child another worker started here. The worker is stopping,
            # not the user: no stop latch, and the interrupt says so, so its
            # report reads as a worker shutdown and a message can resume it
            # once this worker is back (#1808).
            runtime = session_runtime.get_live_runtime(sid)
            if runtime is not None:
                await asyncio.to_thread(
                    turns.interrupt_turn, sid, "stop",
                    stop_background=True, turn_id=runtime.turn_id,
                    source=SERVER_SHUTDOWN_REASON,
                )
                await runtime.interrupt("stop", escalation_timeout=1.0)
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
            await runtime.interrupt("stop", escalation_timeout=1.0)

    results = await asyncio.gather(
        *(stop_session(sid) for sid in session_ids), return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    # Never cancel these tasks on an HTTP timeout: their finally blocks persist
    # the terminal state and release the lease.
    await asyncio.gather(*executions, return_exceptions=True)
    await task_persistence.flush()

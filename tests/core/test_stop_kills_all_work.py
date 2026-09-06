"""Stop kills EVERY piece of the session's work (round 7).

The previous rule spared a healthy Monitor shell on the theory that a dev
server the user asked to keep up is not part of the turn. Living with it
produced the complaint this file now pins: "stop button not killing all work
including background tasks making sessions i stop constantly come back to
life". A spared shell (or a CI watcher, or a nested worker) still had a road
back into the scheduler the moment it settled.

So a plain Stop now kills agents AND shells, latches the session stopped, and
leaves every killed task with a terminal registry row. The old behaviour
survives only behind ``STOP_KEEPS_MONITOR_SHELLS=true``, which defaults off —
and even then the shell is spared but the session is still latched, because
"nothing restarts this session" is not the part the setting opts out of.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.query_session_lifecycle import stop_all_background_tasks
from app.core.task_registry import task_registry

# asyncio_mode = auto (pytest.ini) runs the coroutines below; no marker needed.


@pytest.fixture(autouse=True)
def _clean_registry():
    task_registry.reset_all()
    yield
    task_registry.reset_all()


def _seed(session_id: str, rows: list[tuple[str, str]]) -> None:
    for task_id, kind in rows:
        task_registry.start_task(session_id, task_id, kind, task_id)


class _StopPatches:
    """Neutralise the durable/engine legs so these stay unit tests."""

    def __init__(self, shell_running: bool = True):
        self._shell_running = shell_running
        self._ctx = []

    def __enter__(self):
        self.stopped: list[str] = []

        async def _stop(sid, task_id):
            self.stopped.append(task_id)
            return {"ok": True, "task_id": task_id}

        async def _enter_user_stop(sid):
            # The real one also drops wakeups / aborts queued turns / settles
            # parked rows, all of which need a DB. The latch is the half these
            # unit tests are about, so keep that and stub the rest.
            task_registry.mark_user_stopped(sid)
            return {}

        patches = [
            patch(
                "app.mcp.builtin_tools.is_background_shell_running",
                return_value=self._shell_running,
            ),
            patch(
                "app.core.query_session_lifecycle.stop_session_task",
                new=AsyncMock(side_effect=_stop),
            ),
            patch(
                "app.core.turn_scheduler.discard_queued_task_wakes",
                return_value=0,
            ),
            patch(
                "app.core.turn_scheduler.retire_task_terminals",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.core.query_session_lifecycle.enter_user_stop",
                new=AsyncMock(side_effect=_enter_user_stop),
            ),
        ]
        for p in patches:
            p.start()
            self._ctx.append(p)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._ctx):
            p.stop()
        return False


async def test_user_stop_kills_agents_and_shells():
    session_id = "stop-kills-all"
    _seed(session_id, [("agent-1", "agent"), ("shell-1", "shell")])

    with _StopPatches() as p:
        await stop_all_background_tasks(session_id, user_stop=True)

    assert sorted(p.stopped) == ["agent-1", "shell-1"]
    assert task_registry.is_wake_suppressed(session_id, "agent-1")
    assert task_registry.is_wake_suppressed(session_id, "shell-1")
    assert task_registry.is_user_stopped(session_id)


async def test_user_stop_leaves_every_task_terminal():
    """Nothing may sit ``running`` after a Stop, whatever the stop path did."""
    session_id = "stop-terminal"
    _seed(session_id, [("agent-1", "agent"), ("shell-1", "shell")])

    with _StopPatches():
        # stop_session_task is stubbed out, so nothing else marks these rows —
        # the terminal guarantee in stop_all_background_tasks is the only thing
        # that can settle them.
        await stop_all_background_tasks(session_id, user_stop=True)

    for task_id in ("agent-1", "shell-1"):
        record = task_registry.get_task(session_id, task_id)
        assert record["status"] == "killed", task_id
        assert record["summary"], task_id  # never a blank terminal chip


async def test_user_stop_retires_killed_tasks_with_the_engine():
    """fluid must drop the dead ids or it parks on them forever (087d3809 F1)."""
    session_id = "stop-retire"
    _seed(session_id, [("agent-1", "agent"), ("shell-1", "shell")])

    with _StopPatches(), patch(
        "app.core.turn_scheduler.retire_task_terminals",
        new=AsyncMock(return_value=True),
    ) as retire:
        await stop_all_background_tasks(session_id, user_stop=True)

    retire.assert_awaited_once()
    frames = retire.await_args.args[1]
    assert {f["task_id"] for f in frames} == {"agent-1", "shell-1"}
    assert all(f["status"] == "killed" for f in frames)


async def test_opt_in_keeps_monitor_shells_but_still_latches_the_stop():
    """``STOP_KEEPS_MONITOR_SHELLS=true`` restores the old sparing, only that."""
    session_id = "stop-optin"
    _seed(session_id, [("agent-1", "agent"), ("shell-1", "shell")])

    with _StopPatches() as p, patch(
        "app.core.query_session_lifecycle._keep_monitor_shells_on_stop",
        return_value=True,
    ):
        results = await stop_all_background_tasks(session_id, user_stop=True)

    assert p.stopped == ["agent-1"]
    assert [r["task_id"] for r in results if r.get("skipped")] == ["shell-1"]
    # Spared, so its eventual death is still news — but the SESSION is stopped,
    # which is what actually keeps it from starting a turn.
    assert not task_registry.is_wake_suppressed(session_id, "shell-1")
    assert task_registry.is_user_stopped(session_id)


async def test_ghost_shell_row_is_still_reaped_when_shells_are_spared():
    """A registry row whose process already exited has nothing to spare."""
    session_id = "stop-ghost"
    _seed(session_id, [("shell-dead", "shell")])

    with _StopPatches(shell_running=False) as p, patch(
        "app.core.query_session_lifecycle._keep_monitor_shells_on_stop",
        return_value=True,
    ):
        results = await stop_all_background_tasks(session_id, user_stop=True)

    assert p.stopped == ["shell-dead"]
    assert not any(r.get("skipped") for r in results)


async def test_worker_owned_shells_are_reaped_too():
    """A worker's dev server is a child of the APP, not of the worker's CLI.

    Killing the worker session evicts its pooled client, which ends the CLI and
    its descendants — but a Monitor shell the worker started was spawned by the
    app process and survives that entirely, dev server and all. Stop has to
    sweep it explicitly or "kill everything" quietly means "except that".
    """
    from app.core import provider_agent_supervisor as sup

    session_id = "stop-worker-shells"
    _seed(session_id, [("worker-a", "agent")])
    sup.register_worker(sup.WorkerRecord(
        parent_session_id=session_id,
        worker_session_id="worker-a",
        task_id="worker-a",
        tool_use_id="worker-a",
    ))
    try:
        with _StopPatches(), patch(
            "app.mcp.builtin_tools.stop_session_background_shells",
            new=AsyncMock(return_value=2),
        ) as reap:
            await stop_all_background_tasks(session_id, user_stop=True)
    finally:
        sup.reset()

    reap.assert_awaited_once_with("worker-a")


async def test_non_user_stop_paths_are_unchanged():
    """``include_shells`` still drives the internal/force callers."""
    session_id = "stop-force"
    _seed(session_id, [("shell-1", "shell")])

    with _StopPatches() as p:
        await stop_all_background_tasks(session_id)

    assert p.stopped == ["shell-1"]
    # No user stop was asked for, so no session-wide latch was set.
    assert not task_registry.is_user_stopped(session_id)

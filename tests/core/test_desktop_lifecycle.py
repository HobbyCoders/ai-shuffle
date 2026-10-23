"""Desktop close and orphan recovery use real runtime and database transitions."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.core import desktop_lifecycle, session_runtime, startup, turn_manager
from app.db import runs, turns
from tests.core.test_session_runtime import _runtime_env


@pytest.fixture(autouse=True)
def clean_state():
    startup.is_shutting_down = False
    session_runtime._executions.clear()
    yield
    startup.is_shutting_down = False
    session_runtime._executions.clear()


def test_idle_serving_task_is_named_as_real_work(mock_db, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.core.task_registry import TaskRegistry
    from app.core.update_workers import _busy_reason
    from app.db import database
    from app.mcp import builtin_tools

    registry = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", registry)
    monkeypatch.setattr(database, "get_session", lambda sid: {"title": "Build preview", "updated_at": datetime.now(timezone.utc)})
    monkeypatch.setattr("app.core.config.settings.monitor_serving_idle_seconds", 900)
    registry.start_task("idle-chat", "preview", "shell", "node scripts/dev-preview.js --port 8791")
    registry.mark_monitor_shell_task("idle-chat", "preview", serves=True)
    monkeypatch.setitem(builtin_tools._background_tasks, "preview", SimpleNamespace(is_running=lambda: True))
    rows = desktop_lifecycle.busy_work()
    assert rows[0]["running_turn"] is False
    detail = rows[0]["task_details"][0]
    assert detail["kind"] == "service" and detail["live"] is True
    assert detail["port"] == 8791 and "dev-preview" in detail["label"]
    assert detail["started_at"] == registry.get_task("idle-chat", "preview")["startedAt"]
    assert detail["monitor_serving_idle_seconds"] == 900 and detail["idle_stop_at"] > datetime.now(timezone.utc).timestamp()
    assert detail["stop_will_stop"] is True
    assert "Preview server still running for Build preview" in _busy_reason(rows)
    assert "8791" in _busy_reason(rows)
    from app.core.update_workers import waiting_status
    blocker = waiting_status({"phase": "ready", "version": "b" * 12})["blocked_by"][0]
    assert blocker["kind"] == "service" and blocker["live"] is True
    assert blocker["monitor_serving_idle_seconds"] == 900 and blocker["idle_stop_at"]


@pytest.mark.parametrize("connected", [True, False, None])
@pytest.mark.parametrize("kind", ["agent", "shell"])
async def test_shutdown_stop_attributes_task_to_server(mock_db, monkeypatch, connected, kind):
    from types import SimpleNamespace
    from app.core import query_session_lifecycle
    from app.core.task_registry import TaskRegistry, SERVER_SHUTDOWN_REASON

    registry = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", registry)
    state = SimpleNamespace(is_connected=connected, adapter=None) if connected is not None else None
    monkeypatch.setattr(query_session_lifecycle, "get_session_pool", lambda: SimpleNamespace(peek=lambda sid: state))
    monkeypatch.setattr("app.core.remote_task.report_owed", lambda sid: None)
    registry.start_task("s", "task", kind, "work")
    await desktop_lifecycle.stop_session_work("s", shutdown=True)
    assert registry.get_task("s", "task")["reason"] == SERVER_SHUTDOWN_REASON


def start(sid):
    turn = turns.enqueue_turn(sid, "user", prompt="work")
    run = runs.claim_run(sid, "desktop", 60)
    turns.start_turn(turn["id"], run["id"], "desktop")
    return turn, run


@pytest.mark.parametrize("state", ["running", "interrupting", "draining"])
def test_stop_recovers_dead_turn_but_preserves_live_peer(mock_db, state):
    dead, lease = start("dead")
    peer, _ = start("peer")
    if state != "running":
        turns.interrupt_turn("dead", "stop")
    if state == "draining":
        turns.escalate_to_draining(dead["id"])
    runs.release_run(lease["id"], lease["fence_token"])
    stopped = turn_manager.interrupt("dead", "stop", turn_id=dead["id"])
    assert stopped["state"] == "aborted"
    assert stopped["stop_reason"] == "interrupted"
    assert turns.stop_orphaned_turns("peer") == []
    assert turns.get_turn(peer["id"])["state"] == "running"


def test_stale_turn_stop_does_not_touch_successor(mock_db):
    old, lease = start("session")
    turns.settle_turn(old["id"], "completed")
    runs.release_run(lease["id"], lease["fence_token"])
    new, _ = start("session")
    assert turns.interrupt_turn("session", "stop", turn_id=old["id"]) is None
    assert turns.get_turn(new["id"])["state"] == "running"


async def test_close_waits_for_real_turn_settlement_and_lease_release(mock_db):
    began = asyncio.Event()
    finish = asyncio.Event()

    async def pump(**kwargs):
        began.set()
        await finish.wait()
        yield {"type": "interrupted"}

    turn = turns.enqueue_turn("closing", "user", prompt="hello")
    with _runtime_env(pump=pump) as env:
        env["registry"].active_session_ids.return_value = []
        task = asyncio.create_task(session_runtime.execute(turn))
        await asyncio.wait_for(began.wait(), 3)
        assert desktop_lifecycle.active_session_ids() == {"closing"}
        assert runs.get_active_run("closing") is not None
        async def interrupt(_sid):
            finish.set()
            return True
        with patch("app.core.query_session_lifecycle.interrupt_session", side_effect=interrupt),              patch("app.core.query_session_lifecycle.stop_all_background_tasks", new=AsyncMock()) as stop_bg,              patch("app.core.task_persistence.flush", new=AsyncMock()) as flush:
            await asyncio.wait_for(desktop_lifecycle.stop_running_sessions(), 5)
        assert task.done()
        stop_bg.assert_any_await("closing", user_stop=True, respect_shell_preference=False, source="server_shutdown")
        flush.assert_awaited_once()
        assert turns.get_turn(turn["id"])["state"] == "aborted"
        assert runs.get_active_run("closing") is None
        assert desktop_lifecycle.active_session_ids() == set()
        env["registry"].is_user_stopped.return_value = False
        assert await session_runtime.execute(turn) is None


async def test_close_includes_parked_background_only_session():
    with patch("app.core.task_registry.task_registry") as registry,          patch("app.core.query_session_lifecycle.stop_all_background_tasks", new=AsyncMock()) as stop,          patch("app.core.task_persistence.flush", new=AsyncMock()):
        registry.active_session_ids.return_value = ["parked"]
        await desktop_lifecycle.stop_running_sessions()
        stop.assert_awaited_once_with("parked", user_stop=True, respect_shell_preference=False, source="server_shutdown")

async def test_async_stop_recovers_orphan_once_and_writes_terminal_event(mock_db):
    turn, lease = start("orphan-async")
    turns.interrupt_turn("orphan-async", "stop")
    runs.release_run(lease["id"], lease["fence_token"])
    with patch("app.core.turn_manager._fire_interrupt_executor") as fire:
        row = await turn_manager.interrupt_async("orphan-async", "stop", turn_id=turn["id"])
        assert row["state"] == "aborted"
        assert await turn_manager.interrupt_async("orphan-async", "stop", turn_id=turn["id"]) is None
        fire.assert_not_called()
    cur = mock_db.cursor()
    cur.execute("SELECT event_type, turn_id FROM session_events WHERE session_id=?", ("orphan-async",))
    events = cur.fetchall()
    assert len(events) == 1
    assert events[0]["event_type"] == "turn.aborted"
    assert events[0]["turn_id"] == turn["id"]

async def test_failed_stop_returns_for_retry_without_cancelling_execution():
    task = asyncio.create_task(asyncio.Event().wait())
    try:
        with patch("app.core.session_runtime.active_executions", return_value={task: "busy"}), \
             patch("app.core.desktop_lifecycle.active_session_ids", return_value={"busy"}), \
             patch("app.core.query_session_lifecycle.stop_all_background_tasks", new=AsyncMock(side_effect=RuntimeError("stop failed"))):
            with pytest.raises(RuntimeError, match="stop failed"):
                await asyncio.wait_for(desktop_lifecycle.stop_running_sessions(), 1)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("blocked_stage", ["stop", "flush"])
async def test_shutdown_budget_includes_stop_and_flush(monkeypatch, blocked_stage):
    release = asyncio.Event()
    calls = []

    async def block(*args, **kwargs):
        calls.append(asyncio.current_task())
        await release.wait()

    monkeypatch.setattr(desktop_lifecycle, "SHUTDOWN_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", lambda: {"busy"})
    with patch.object(desktop_lifecycle, "stop_session_work", new=block if blocked_stage == "stop" else AsyncMock()), \
         patch("app.core.task_persistence.flush", new=block if blocked_stage == "flush" else AsyncMock()):
        try:
            await asyncio.wait_for(desktop_lifecycle.stop_running_sessions(), 0.5)
            assert calls and not calls[0].done()
            assert calls[0].cancelling() == 0
        finally:
            release.set()
            await asyncio.gather(*calls)


async def test_shutdown_execution_wait_uses_budget_left_after_stop(monkeypatch):
    execution = asyncio.create_task(asyncio.Event().wait())

    async def slow_stop(*args, **kwargs):
        await asyncio.sleep(0.04)

    monkeypatch.setattr(desktop_lifecycle, "SHUTDOWN_SETTLE_SECONDS", 0.06)
    with patch.object(desktop_lifecycle, "active_session_ids", return_value={"busy"}), \
         patch.object(session_runtime, "active_executions", return_value={execution: "busy"}), \
         patch.object(desktop_lifecycle, "stop_session_work", new=slow_stop), \
         patch("app.core.task_persistence.flush", new=AsyncMock()), \
         patch.object(desktop_lifecycle.asyncio, "wait", wraps=asyncio.wait) as wait:
        try:
            await asyncio.wait_for(desktop_lifecycle.stop_running_sessions(), 0.5)
            execution_wait = next(call for call in wait.call_args_list if call.args[0] == {execution})
            assert 0 <= execution_wait.kwargs["timeout"] < 0.03
            assert not execution.done()
        finally:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


async def test_force_stop_runs_all_tasks_and_turns_under_one_deadline(mock_db, monkeypatch):
    from app.core.task_registry import TaskRegistry

    registry = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", registry)
    monkeypatch.setattr("app.core.remote_task.report_owed", lambda sid: None)
    for i in range(12):
        registry.start_task(f"s{i % 2}", f"task{i}", "shell", "work")
    release = asyncio.Event()
    calls, started = [], []

    async def block(*args, **kwargs):
        calls.append(asyncio.current_task())
        started.append(args)
        await release.wait()
        return []

    with patch.object(session_runtime, "force_end_executions", new=block), \
         patch("app.core.query_session_lifecycle.stop_session_task", new=block):
        try:
            assert await asyncio.wait_for(desktop_lifecycle.force_stop_sessions(
                {"s0", "s1"}, source="worker_update", grace=0.05), 0.5) == ["s0", "s1"]
            assert len(started) == 13  # turn force-end and every task start concurrently
            assert all(call.cancelling() == 0 for call in calls)
            assert not registry.active_session_ids()
            for i in range(12):
                task = registry.get_task(f"s{i % 2}", f"task{i}")
                assert (task["status"], task["reason"]) == ("killed", "worker_update")
        finally:
            release.set()
            await asyncio.gather(*calls)



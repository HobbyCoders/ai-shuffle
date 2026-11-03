"""Late Monitor completions exercise the producer, deletion and wake admission."""

import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import monitor_spawn, session_runtime, turn_scheduler
from app.core.task_registry import task_registry
from app.db import sessions, turns
from app.mcp import builtin_tools


@pytest.mark.asyncio
async def test_monitor_completion_after_session_deletion(mock_db, monkeypatch, tmp_path, caplog):
    sid = "deleted-monitor-owner"
    sessions.create_session(sid, "default")
    session_runtime.register_scheduler(turn_scheduler.on_turn_settled)
    delivered = asyncio.Event()
    terminal = turn_scheduler.on_task_terminal

    async def observe_terminal(session_id, frame):
        try:
            await terminal(session_id, frame)
        finally:
            delivered.set()

    monkeypatch.setattr(turn_scheduler, "on_task_terminal", observe_terminal)
    monkeypatch.setattr(turn_scheduler, "TERMINAL_BATCH_WINDOW_SECONDS", 0)
    caplog.set_level(logging.INFO)
    # The actual Monitor spawner owns a real child process and its exit watcher.
    # It finishes only after the session has really been deleted, not a seeded
    # terminal frame fed directly to the scheduler.
    release = tmp_path / "release"
    command = (
        f"attempt=0; while [ ! -f '{release}' ] && [ \"$attempt\" -lt 500 ]; "
        "do sleep 0.01; attempt=$((attempt+1)); done; "
        f"test -f '{release}' && printf 'late result\n'"
    )
    outcome = await monitor_spawn.make_fluid_monitor_spawner(sid)(
        {"command": command, "cwd": str(tmp_path)}, None,
    )
    task_id = outcome.background_task.task_id
    shell = builtin_tools._background_tasks[task_id]
    try:
        assert shell.is_running()
        assert task_registry.is_own_background_task(sid, task_id)
        assert sessions.delete_session(sid)
        assert sessions.get_session(sid) is None
        release.touch()
        await asyncio.wait_for(delivered.wait(), 5)
        for _ in range(5):
            pending = list(turn_scheduler._kick_tasks)
            if not pending:
                break
            await asyncio.gather(*pending)
        assert task_registry.get_task(sid, task_id)["status"] == "completed"
        assert turns.get_queued_turns(sid) == []
        bad = [r for r in caplog.records if r.exc_info or "Profile not found" in r.getMessage()
               or "re-enqueued once" in r.getMessage()]
        assert bad == [], caplog.text
        drops = [r for r in caplog.records if "deleted session" in r.getMessage().lower()]
        assert len(drops) == 1, caplog.text
    finally:
        release.touch()
        await asyncio.wait_for(shell.process.wait(), 5)
        await asyncio.gather(shell._reader_task, return_exceptions=True)
        builtin_tools._background_tasks.pop(task_id, None)
        task_registry.clear_session(sid)
        session_runtime.register_scheduler(None)


@pytest.mark.asyncio
async def test_deletion_after_context_read_is_guarded_before_the_pump(mock_db, monkeypatch, caplog):
    sid = "queued-delete"
    sessions.create_session(sid, "default")
    context = turns.enqueue_turn(sid, "user", prompt="work", profile_id="default")
    turns.start_turn(context["id"], None, "runner")
    turns.settle_turn(context["id"], "completed")
    latest_context = turn_scheduler._latest_context_turn

    def delete_after_read(session_id):
        snapshot = latest_context(session_id)
        assert sessions.delete_session(session_id)
        return snapshot

    monkeypatch.setattr(turn_scheduler, "_latest_context_turn", delete_after_read)
    monkeypatch.setattr(turn_scheduler, "kick", Mock())
    pump = AsyncMock()
    monkeypatch.setattr(session_runtime.SessionRuntime, "_run_pump", pump)
    with caplog.at_level(logging.INFO):
        wake = await turn_scheduler._enqueue_context_wake(sid, "unpark", [], [], "late result")
        assert wake["id"]  # Enqueue raced deletion after inheriting real context.
        assert await session_runtime.execute(wake) is None
    pump.assert_not_awaited()
    assert turns.get_queued_turns(sid) == []
    assert not any(r.exc_info for r in caplog.records)
    assert sum("deleted session" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_deleted_mid_turn_runtime_refuses_task_steer(mock_db, monkeypatch):
    sessions.create_session("steer-delete", "default")
    turn = turns.enqueue_turn("steer-delete", "user", prompt="work")
    turns.start_turn(turn["id"], None, "runner")
    runtime = session_runtime.SessionRuntime(turn)
    adapter = Mock(steer=AsyncMock())
    monkeypatch.setattr(runtime, "_live_adapter", lambda: adapter)
    assert sessions.delete_session("steer-delete")
    assert not await runtime.steer_task_notification(
        "late result", notification_id="task-notify:deleted", frames=[], task_ids=["task"],
    )
    adapter.steer.assert_not_awaited()
    assert runtime._task_steers == {}


@pytest.mark.asyncio
async def test_stranded_notification_for_deleted_session_is_dropped(mock_db, monkeypatch, caplog):
    sid = "stranded-delete"
    sessions.create_session(sid, "default")
    task_registry.start_task(sid, "task", "agent", "work", tool_use_id="tool")
    task_registry.mark_background_launch(sid, "tool")
    assert sessions.delete_session(sid)
    kick = Mock()
    monkeypatch.setattr(turn_scheduler, "kick", kick)
    with caplog.at_level(logging.INFO):
        await turn_scheduler.on_task_notification_stranded(
            sid, [{"task_id": "task", "status": "completed", "summary": "done"}], ["task"],
        )
    assert turns.get_queued_turns(sid) == []
    kick.assert_not_called()
    assert not any(r.exc_info for r in caplog.records)
    assert sum("deleted session" in r.getMessage() for r in caplog.records) == 1
    assert "enqueued wake turn None" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_during_teardown", [False, True])
async def test_failed_wake_retry_keeps_existing_chat_but_not_a_deleted_one(
    mock_db, monkeypatch, caplog, delete_during_teardown,
):
    sid = "retry-delete"
    sessions.create_session(sid, "default")
    dead = turns.enqueue_turn(sid, "unpark", prompt="task result", profile_id="default")
    turns.start_turn(dead["id"], None, "runner")
    turns.settle_turn(dead["id"], "aborted", stop_reason="error", error="client closed")
    dead = turns.get_turn(dead["id"])

    async def drop_client(session_id):
        if delete_during_teardown:
            assert sessions.delete_session(session_id)

    monkeypatch.setattr(turn_scheduler, "_drop_pooled_client", drop_client)
    kick = Mock()
    monkeypatch.setattr(turn_scheduler, "kick", kick)
    with caplog.at_level(logging.INFO):
        retried = await turn_scheduler._maybe_recover_failed_wake(sid, dead)
    assert retried is not delete_during_teardown
    queued = turns.get_queued_turns(sid)
    if delete_during_teardown:
        assert queued == []
        kick.assert_not_called()
        assert "re-enqueued once" not in caplog.text
        assert sum("deleted session" in r.getMessage() for r in caplog.records) == 1
    else:
        assert len(queued) == 1
        assert queued[0]["payload"][turn_scheduler.WAKE_RETRY_KEY] is True
        assert queued[0]["prompt"] == dead["prompt"]
        kick.assert_called_once_with(sid)
    assert not any(r.exc_info for r in caplog.records)


@pytest.mark.asyncio
async def test_deleted_retry_failure_does_not_surface_a_dead_chat(mock_db, monkeypatch, caplog):
    sid = "retry-failed-delete"
    sessions.create_session(sid, "default")
    wake = turns.enqueue_turn(
        sid, "unpark", payload={turn_scheduler.WAKE_RETRY_KEY: True},
    )
    turns.start_turn(wake["id"])
    settled = turns.settle_turn(wake["id"], "aborted", stop_reason="error", error="client closed")
    assert sessions.delete_session(sid)
    notify = AsyncMock()
    monkeypatch.setattr(turn_scheduler, "_notify_dead_wake", notify)
    with caplog.at_level(logging.INFO):
        assert not await turn_scheduler._maybe_recover_failed_wake(sid, settled)
    notify.assert_not_awaited()
    assert sum("deleted session" in r.getMessage() for r in caplog.records) == 1
    assert not any(r.exc_info for r in caplog.records)

"""Desktop close and orphan recovery use real runtime and database transitions."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.core import desktop_lifecycle, session_runtime, startup, turn_manager
from app.db import runs, turns
from tests.core.test_session_runtime import mock_db, test_db, _runtime_env


@pytest.fixture(autouse=True)
def clean_state():
    startup.is_shutting_down = False
    session_runtime._executions.clear()
    yield
    startup.is_shutting_down = False
    session_runtime._executions.clear()


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
        stop_bg.assert_any_await("closing", user_stop=True, respect_shell_preference=False)
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
        stop.assert_awaited_once_with("parked", user_stop=True, respect_shell_preference=False)

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



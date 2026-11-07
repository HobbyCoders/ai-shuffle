"""An already-fixed parked-turn Stop must still kill its actual Monitor shell."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.core import turn_scheduler
from app.core.task_registry import task_registry
from app.db import turns
from app.mcp import builtin_tools as bt
from tests.api.test_chat_ws_v2_turns import _make_handler, _make_session, _Patches
from tests.shared_fixtures import clean_task_registry  # noqa: F401


@pytest.mark.parametrize("relayed", [False, True], ids=["websocket", "relayed"])
async def test_parked_stop_kills_monitor_and_cancels_queued_successor(mock_db, relayed):
    sid = _make_session()
    parked = turns.enqueue_turn(sid, "user", prompt="start background work")["id"]
    turns.start_turn(parked, run_id="parked-run")
    shell = await bt._spawn_background("sleep 60", None, sid)
    try:
        assert shell.is_running()
        task_registry.mark_monitor_shell_task(sid, shell.task_id)
        queued = turns.enqueue_turn(sid, "queue", prompt="not started yet")["id"]
        turns.park_turn(parked)
        # Keep the real capture, stop-all, per-task kill, registry and DB paths.
        # Only network delivery / engine notification is unrelated to this repro.
        with _Patches(), patch.object(turn_scheduler, "retire_task_terminals", new=AsyncMock()):
            if relayed:
                from app.network import control

                with patch.object(control, "_require_owned"):
                    reply = await control._interrupt({
                        "session_id": sid, "mode": "stop", "stop_background": True,
                        "turn_id": parked,
                    }, "other-device")
                assert reply["interrupted"] is True
            else:
                await _make_handler().handle_stop({"session_id": sid, "turn_id": parked})
        await asyncio.wait_for(shell._reader_task, 5)
        assert not shell.is_running()
        assert task_registry.get_task(sid, shell.task_id)["status"] == "killed"
        assert task_registry.is_user_stopped(sid)
        # The model turn had already finished successfully before parking.
        assert turns.get_turn(parked)["state"] == "completed"
        assert turns.get_turn(queued)["state"] == "aborted"
        before = turns.get_queued_turns(sid)
        with patch.object(turn_scheduler, "kick") as kick:
            await turn_scheduler.on_task_terminal(sid, task_registry.get_task(sid, shell.task_id))
        kick.assert_not_called()
        assert turns.get_queued_turns(sid) == before == []
    finally:
        if shell.is_running():
            await bt.stop_background_shell_task(shell.task_id, session_id=sid)
        await asyncio.wait_for(shell._reader_task, 5)
        bt._background_tasks.pop(shell.task_id, None)

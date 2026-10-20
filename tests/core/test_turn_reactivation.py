"""Person-authored direct submits reactivate exactly as a chat send does."""
from unittest.mock import AsyncMock

import pytest

from app.core import turn_manager
from app.core.sync_engine import sync_engine
from app.db import sessions, turns
from app.network import session_active


@pytest.mark.parametrize("source", ["user", "queue", "resume", "api", "worktree"])
@pytest.mark.parametrize("async_call", [False, True])
async def test_direct_submit_reactivates_before_kick(mock_db, monkeypatch, source, async_call):
    sid = "direct-settled"
    sessions.create_session(sid, "default", is_active=True)
    await session_active.set_shared(sid, False)
    broadcast = AsyncMock()
    user_broadcast = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_session_updated", broadcast)
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", user_broadcast)
    kicked = []
    monkeypatch.setattr(turn_manager, "_kick", lambda s: kicked.append(sessions.get_session(s)["is_active"]))
    if async_call:
        tid = await turn_manager.submit_async(sid, source, prompt="continue")
    else:
        tid = turn_manager.submit(sid, source, prompt="continue")
    assert sessions.get_session(sid)["is_active"]
    assert kicked == [True]
    assert turns.get_turn(tid)["state"] == "queued"
    if async_call:
        broadcast.assert_awaited_once_with(session_id=sid, updates={"is_active": True}, source_device_id=None)
        user_broadcast.assert_awaited_once_with(
            user_id="admin", session_id=sid, updates={"is_active": True}, source_device_id=None,
        )
        broadcast.reset_mock()
        user_broadcast.reset_mock()
        await turn_manager.submit_async(sid, source, prompt="another message")
        broadcast.assert_not_awaited()
        user_broadcast.assert_not_awaited()


@pytest.mark.parametrize("source", ["wakeup", "automation", "provider_agent", "auto_continue"])
async def test_background_submit_does_not_reactivate(mock_db, source):
    sid = "background-settled"
    sessions.create_session(sid, "default", is_active=False)
    await turn_manager.submit_async(sid, source, prompt="background", no_kick=True)
    assert not sessions.get_session(sid)["is_active"]

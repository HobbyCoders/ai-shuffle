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


@pytest.mark.parametrize("source,payload", [
    ("wakeup", None), ("automation", None), ("provider_agent", None), ("auto_continue", None),
    ("queue", {turn_manager.VOICE_DELEGATION: True}),
])
async def test_background_submit_does_not_reactivate(mock_db, source, payload):
    sid = "background-settled"
    sessions.create_session(sid, "default", is_active=False)
    await turn_manager.submit_async(sid, source, prompt="background", payload=payload, no_kick=True)
    assert not sessions.get_session(sid)["is_active"]


@pytest.mark.parametrize("async_call", [False, True])
async def test_reactivation_announced_even_when_enqueue_fails(mock_db, monkeypatch, async_call):
    from unittest.mock import Mock

    from app.core.global_events import global_events

    sessions.create_session("refused-send", "default", is_active=False)
    broadcast = AsyncMock()
    lifecycle = Mock()
    monkeypatch.setattr(sync_engine, "broadcast_session_updated", broadcast)
    monkeypatch.setattr(global_events, "lifecycle", lifecycle)

    def fail(*args, **kwargs):
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(turns, "enqueue_turn", fail)
    with pytest.raises(RuntimeError, match="enqueue failed"):
        if async_call:
            await turn_manager.submit_async("refused-send", "user", prompt="hi")
        else:
            turn_manager.submit("refused-send", "user", prompt="hi")
    assert sessions.get_session("refused-send")["is_active"]
    if async_call:
        broadcast.assert_awaited_once_with(
            session_id="refused-send", updates={"is_active": True}, source_device_id=None,
        )
    else:
        lifecycle.assert_called_once_with("admin", {
            "type": "user_session_updated", "session_id": "refused-send", "updates": {"is_active": True},
        }, user_type="admin")


async def test_sync_handoff_release_announces_membership_from_worker_thread(mock_db, monkeypatch):
    import asyncio
    from unittest.mock import Mock

    from app.core import session_handoff
    from app.core.global_events import global_events

    sessions.create_session("release-here", "default", is_active=False)
    lifecycle = Mock()
    monkeypatch.setattr(global_events, "lifecycle", lifecycle)
    sent, unsent = await asyncio.to_thread(session_handoff._submit_here, "release-here", [
        {"source": "user", "prompt": "held message"},
    ])
    assert (sent, unsent) == (1, [])
    assert sessions.get_session("release-here")["is_active"]
    lifecycle.assert_called_once_with("admin", {
        "type": "user_session_updated", "session_id": "release-here", "updates": {"is_active": True},
    }, user_type="admin")

"""Converted history uses the same authenticated API admission as ordinary chats."""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from app.api import query as query_api
from app.api.auth import hash_api_key
from app.core import query_background, query_engine
from app.core.models import ConversationRequest
from app.db import database


@pytest.fixture
def chats(mock_db, monkeypatch):
    database.create_profile("own-profile", "Own", None, {})
    database.create_api_user("u1", "U1", hash_api_key("aih_archived_lane_key"),
                             profile_ids=["own-profile"])
    for sid in ("archived-chat", "active-chat"):
        database.create_session(session_id=sid, profile_id="own-profile",
                                title=sid, api_user_id="u1")
    from app.db.session_archive_migration import migrate
    mock_db.execute("UPDATE sessions SET status = 'archived' WHERE id = 'archived-chat'")
    mock_db.commit()
    from app.db.base import get_db
    with get_db() as conn:
        migrate(conn.cursor())
    monkeypatch.setattr(query_api, "_refuse_unready_leg", AsyncMock(return_value=None))
    profile = {"id": "own-profile", "config": {}}
    monkeypatch.setattr(query_engine, "get_profile", lambda pid: profile)
    monkeypatch.setattr(query_background, "get_profile", lambda pid: profile)
    return database.get_api_user("u1")


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ["active-chat", "archived-chat"])
async def test_start_route_continues_ordinary_and_converted_chats(chats, monkeypatch, sid):
    engine = AsyncMock(return_value={"session_id": sid, "status": "started"})
    monkeypatch.setattr(query_api, "start_background_query", engine)
    request = ConversationRequest(prompt="go on", session_id=sid)
    result = await query_api.start_conversation(request, chats)
    assert result["status"] == "started"
    assert engine.await_args.kwargs["session_id"] == sid


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ["active-chat", "archived-chat"])
async def test_stream_route_continues_ordinary_and_converted_chats(chats, monkeypatch, sid):
    seen = {}

    async def fake_stream(**kwargs):
        seen.update(kwargs)
        yield {"type": "done", "session_id": kwargs["session_id"]}

    monkeypatch.setattr(query_api, "_stream_query_with_failover", fake_stream)
    request = ConversationRequest(prompt="go on", session_id=sid)
    response = await query_api.stream_conversation(request, chats)
    body = [chunk async for chunk in response.body_iterator]
    assert seen["session_id"] == sid
    assert any("event: done" in chunk for chunk in body)

class AdmittedPastSessionGate(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["execute", "stream"])
async def test_direct_lanes_pass_converted_history_gate(chats, monkeypatch, lane):
    # Stop at the FIRST post-session-read write, before any actual provider call.
    write = Mock(side_effect=AdmittedPastSessionGate)
    monkeypatch.setattr(database, "add_session_message", write)
    with pytest.raises(AdmittedPastSessionGate):
        if lane == "execute":
            await query_engine.execute_query(prompt="fresh", profile_id="own-profile", session_id="archived-chat")
        else:
            async for _ in query_engine.stream_query(prompt="fresh", profile_id="own-profile", session_id="archived-chat"):
                pass
    write.assert_called_once()


@pytest.mark.asyncio
async def test_background_lane_passes_converted_history_gate(chats, monkeypatch):
    runner = AsyncMock()
    monkeypatch.setattr(query_background, "_run_background_query", runner)
    result = await query_background.start_background_query(
        prompt="fresh", profile_id="own-profile", session_id="archived-chat")
    task = query_background._pending_background_tasks["archived-chat"]
    await task
    assert result["session_id"] == "archived-chat"
    runner.assert_awaited_once()

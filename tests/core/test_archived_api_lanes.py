"""The direct API lanes refuse an archived chat too (#1822).

``/conversation`` enrolls a turn, so ``SessionRuntime.execute`` drops it for
an archived chat. ``/conversation/stream`` and ``/conversation/start`` run the
engine directly (``stream_query``, ``start_background_query``), around that
guard, so an API key could still continue an archived chat through them. The
routes now refuse with a 409 and the engine entry points refuse as well.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import query as query_api
from app.api.auth import hash_api_key
from app.core import archive_guard, query_background, query_engine
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
    mock_db.execute("UPDATE sessions SET status = 'archived' WHERE id = 'archived-chat'")
    mock_db.commit()
    monkeypatch.setattr(query_api, "_refuse_unready_leg", AsyncMock(return_value=None))
    profile = {"id": "own-profile", "config": {}}
    monkeypatch.setattr(query_engine, "get_profile", lambda pid: profile)
    monkeypatch.setattr(query_background, "get_profile", lambda pid: profile)
    return database.get_api_user("u1")


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["stream_conversation", "start_conversation"])
async def test_routes_refuse_an_archived_chat(chats, monkeypatch, route, caplog):
    engine = AsyncMock()
    monkeypatch.setattr(query_api, "start_background_query", engine)
    monkeypatch.setattr(query_api, "_stream_query_with_failover", engine)
    request = ConversationRequest(prompt="wake up", session_id="archived-chat")
    with pytest.raises(HTTPException) as exc:
        await getattr(query_api, route)(request, chats)
    assert exc.value.status_code == 409
    assert "archived" in exc.value.detail and "Restore it" in exc.value.detail
    engine.assert_not_called()
    assert "Archived session archived-chat: dropped a api wake" in caplog.text


@pytest.mark.asyncio
async def test_start_route_still_continues_an_active_chat(chats, monkeypatch):
    engine = AsyncMock(return_value={"session_id": "active-chat", "status": "started"})
    monkeypatch.setattr(query_api, "start_background_query", engine)
    request = ConversationRequest(prompt="go on", session_id="active-chat")
    result = await query_api.start_conversation(request, chats)
    assert result["status"] == "started"
    assert engine.await_args.kwargs["session_id"] == "active-chat"


@pytest.mark.asyncio
async def test_stream_route_still_continues_an_active_chat(chats, monkeypatch):
    seen = {}

    async def fake_stream(**kwargs):
        seen.update(kwargs)
        yield {"type": "done", "session_id": kwargs["session_id"]}

    monkeypatch.setattr(query_api, "_stream_query_with_failover", fake_stream)
    request = ConversationRequest(prompt="go on", session_id="active-chat")
    response = await query_api.stream_conversation(request, chats)
    body = [chunk async for chunk in response.body_iterator]
    assert seen["session_id"] == "active-chat"
    assert any("event: done" in chunk for chunk in body)


@pytest.mark.asyncio
async def test_stream_query_refuses_an_archived_chat(chats):
    events = [e async for e in query_engine.stream_query(
        prompt="wake up", profile_id="own-profile", session_id="archived-chat")]
    assert events == [{"type": "error",
                       "message": str(archive_guard.SessionArchived("archived-chat"))}]


@pytest.mark.asyncio
async def test_background_query_refuses_an_archived_chat(chats):
    with pytest.raises(archive_guard.SessionArchived):
        await query_background.start_background_query(
            prompt="wake up", profile_id="own-profile", session_id="archived-chat")


@pytest.mark.asyncio
async def test_execute_query_refuses_an_archived_chat(chats):
    with pytest.raises(archive_guard.SessionArchived):
        await query_engine.execute_query(
            prompt="wake up", profile_id="own-profile", session_id="archived-chat")

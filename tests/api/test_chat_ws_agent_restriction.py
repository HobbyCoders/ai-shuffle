"""An API user's assigned agents (``api_user_profiles``) bind the profile a
chat-WebSocket ``query`` / ``queue_message`` may run, exactly as on the REST
query paths. The owner and unrestricted API users keep today's behaviour."""

from unittest.mock import AsyncMock, patch

import app.api.chat_ws_handler as cwh
from app.db import turns as turns_db
from tests.api.test_chat_ws_v2_turns import _Patches, _make_handler, _make_session
from tests.helpers import sent_frames


def _api_handler(profile_ids):
    handler = _make_handler()
    handler.api_user_id = "api-1"
    handler._authorize_mobile_session = AsyncMock(return_value=True)
    user = {"id": "api-1", "profile_ids": profile_ids}
    return handler, patch.object(cwh.database, "get_api_user", return_value=user)


def _queued_profiles(session_id):
    return [t["profile_id"] for t in turns_db.get_queued_turns(session_id)]


async def test_disallowed_agent_is_refused(mock_db):
    _make_session("sess-1")
    handler, user = _api_handler(["default"])
    with _Patches(), user:
        await handler.handle_query(
            {"prompt": "hi", "session_id": "sess-1", "profile": "other-agent"})
    errors = sent_frames(handler, "error")
    assert errors and "not allowed" in errors[0]["message"]
    assert _queued_profiles("sess-1") == []


async def test_allowed_agent_runs(mock_db):
    _make_session("sess-1")
    handler, user = _api_handler(["default"])
    with _Patches(), user:
        await handler.handle_query(
            {"prompt": "hi", "session_id": "sess-1", "profile": "default"})
    assert _queued_profiles("sess-1") == ["default"]


async def test_omitted_agent_defaults_to_the_only_allowed_one(mock_db):
    _make_session("sess-1")
    handler, user = _api_handler(["default"])
    with _Patches(), user:
        await handler.handle_query({"prompt": "hi", "session_id": "sess-1"})
    assert _queued_profiles("sess-1") == ["default"]


async def test_omitted_agent_with_several_allowed_is_refused(mock_db):
    _make_session("sess-1")
    handler, user = _api_handler(["default", "second"])
    with _Patches(), user:
        await handler.handle_query({"prompt": "hi", "session_id": "sess-1"})
    errors = sent_frames(handler, "error")
    assert errors and "default" in errors[0]["message"] and "second" in errors[0]["message"]
    assert _queued_profiles("sess-1") == []


async def test_unrestricted_api_user_keeps_the_sentinel_default(mock_db):
    _make_session("sess-1")
    handler, user = _api_handler(None)
    with _Patches(), user:
        await handler.handle_query({"prompt": "hi", "session_id": "sess-1"})
    assert _queued_profiles("sess-1") == ["claude-code"]


async def test_owner_is_unrestricted(mock_db):
    _make_session("sess-1")
    handler = _make_handler()
    with _Patches(), patch.object(cwh.database, "get_api_user") as lookup:
        await handler.handle_query(
            {"prompt": "hi", "session_id": "sess-1", "profile": "claude-code"})
    lookup.assert_not_called()
    assert _queued_profiles("sess-1") == ["claude-code"]


async def test_queue_message_with_disallowed_agent_is_refused(mock_db):
    _make_session("sess-1")
    handler, user = _api_handler(["default"])
    with _Patches(), user:
        await handler.handle_queue_message(
            {"content": "later", "session_id": "sess-1", "profile": "other-agent"})
    assert sent_frames(handler, "error")
    assert _queued_profiles("sess-1") == []

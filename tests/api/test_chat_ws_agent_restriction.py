"""An API user's assigned agents (``api_user_profiles``) bind the profile a
chat-WebSocket ``query`` / ``queue_message`` may run, exactly as on the REST
query paths. The owner and unrestricted API users keep today's behaviour."""

from unittest.mock import AsyncMock, patch

import app.api.chat_ws_handler as cwh
from app.db import turns as turns_db
from tests.api.test_chat_ws_v2_turns import _Patches, _make_handler
from tests.api.test_chat_ws_v2_turns import _make_session as _make_any_session
from tests.helpers import sent_frames


def _api_handler(profile_ids):
    handler = _make_handler()
    handler.api_user_id = "api-1"
    handler._authorize_mobile_session = AsyncMock(return_value=True)
    user = {"id": "api-1", "profile_ids": profile_ids}
    return handler, patch.object(cwh.database, "get_api_user", return_value=user)


def _make_session(session_id="sess-1", *, profile_id="default"):
    """A session started by the API user ``api-1`` (API keys act only on their
    own sessions; tests/api/test_session_access_ownership.py covers that)."""
    from app.db import database
    from app.db.base import get_db

    if database.get_api_user("api-1") is None:
        database.create_api_user("api-1", "Key", "hash-api-1")
    _make_any_session(session_id, profile_id=profile_id)
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET api_user_id = %s WHERE id = %s", ("api-1", session_id))
    return session_id


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


async def test_omitted_followup_agent_uses_session_with_several_allowed(mock_db):
    _make_session("sess-1")
    handler, user = _api_handler(["default", "second"])
    with _Patches(), user:
        await handler.handle_query({"prompt": "hi", "session_id": "sess-1"})
    assert not sent_frames(handler, "error")
    assert _queued_profiles("sess-1") == ["default"]


async def test_new_session_without_agent_with_several_allowed_is_refused(mock_db):
    handler, user = _api_handler(["default", "second"])
    with _Patches(), user:
        await handler.handle_query({"prompt": "hi"})
    errors = sent_frames(handler, "error")
    assert errors and "default" in errors[0]["message"] and "second" in errors[0]["message"]


async def test_unrestricted_api_user_inherits_the_session_agent(mock_db):
    _make_session("sess-1")
    handler, user = _api_handler(None)
    with _Patches(), user:
        await handler.handle_query({"prompt": "hi", "session_id": "sess-1"})
    assert _queued_profiles("sess-1") == ["default"]


async def test_owner_is_unrestricted(mock_db):
    _make_session("sess-1")
    handler = _make_handler()
    with _Patches(), patch.object(cwh.database, "get_api_user") as lookup:
        await handler.handle_query(
            {"prompt": "hi", "session_id": "sess-1", "profile": "default"})
    lookup.assert_not_called()
    assert _queued_profiles("sess-1") == ["default"]


async def test_queue_message_with_disallowed_agent_is_refused(mock_db):
    _make_session("sess-1")
    handler, user = _api_handler(["default"])
    with _Patches(), user:
        await handler.handle_queue_message(
            {"content": "later", "session_id": "sess-1", "profile": "other-agent"})
    assert sent_frames(handler, "error")
    assert _queued_profiles("sess-1") == []


# A session whose agent is outside the key's set cannot be driven through the
# WS: the session's agent runs when the frame names none (queue_message), and
# a named allowed agent does not make the session's own agent allowed.

def _foreign_agent_session():
    from app.db import database

    database.create_profile(profile_id="other-agent", name="Other", description=None, config={})
    return _make_session("sess-x", profile_id="other-agent")


async def test_queue_message_without_profile_into_foreign_agent_session_is_refused(mock_db):
    _foreign_agent_session()
    handler, user = _api_handler(["default"])
    with _Patches(), user:
        await handler.handle_queue_message({"content": "later", "session_id": "sess-x"})
    errors = sent_frames(handler, "error")
    assert errors and "other-agent" in errors[0]["message"]
    assert _queued_profiles("sess-x") == []


async def test_query_into_foreign_agent_session_is_refused(mock_db):
    _foreign_agent_session()
    handler, user = _api_handler(["default"])
    with _Patches(), user:
        await handler.handle_query({"prompt": "hi", "session_id": "sess-x"})
    assert sent_frames(handler, "error")
    assert _queued_profiles("sess-x") == []


async def test_queue_message_without_profile_into_own_agent_session_queues(mock_db):
    _make_session("sess-1")
    handler, user = _api_handler(["default"])
    with _Patches(), user:
        await handler.handle_queue_message({"content": "later", "session_id": "sess-1"})
    assert not sent_frames(handler, "error")
    assert len(turns_db.get_queued_turns("sess-1")) == 1

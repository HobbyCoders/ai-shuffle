"""#1402: ``@Notes/<id>`` is expanded on the streaming and background API routes.

Same rule as the chat socket and the one-shot query: the owner reaches every
note, an API key only the notes it wrote, and the identity is the caller that
sent the prompt. Each test calls the real route function and checks the text the
agent got; the agent and the session pool are stubbed.
"""
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

import app.core.query_background as qb
import app.core.query_engine as qe
from app.api import query as query_api
from app.api.query import ConversationRequest, QueryRequest
from app.core.agent_config import resolve_note_references as real_resolve
from app.db import database, session_events
from tests.core.engine_harness import StubPool
from tests.core.query_engine_flow_harness import engine_patches
from tests.core.test_query_background_recording import _bg_patches, _fake_state, _result_message

OWNER_NOTE = "note-00aa"
KEY_NOTE = "note-00bb"
KEY = {"id": "key-1"}
OWNER = {}  # the owner's token carries no API user id
PROMPT = f"compare @Notes/{OWNER_NOTE} with @Notes/{KEY_NOTE}"


@pytest.fixture
def notes(mock_db):
    database.create_note(OWNER_NOTE, "Owner plan", "owner secret")
    database.create_note(KEY_NOTE, "Key plan", "key text", api_user_id=KEY["id"])


def _expected(api_user):
    owner_part = (
        f"@Notes/{OWNER_NOTE}" if api_user.get("id") else "[Note: Owner plan]\nowner secret"
    )
    return f"compare {owner_part} with [Note: Key plan]\nkey text"


async def _drain(response):
    from app.core.provider import session_admission

    chunks = [chunk async for chunk in response.body_iterator]
    # The route returns at ``done``; the turn's lease releases after that.
    while owned := [t for t in tuple(session_admission._unattended) if not t.done()]:
        await asyncio.gather(*owned, return_exceptions=True)
    return chunks


def _common(stack):
    stack.enter_context(patch.object(query_api, "_refuse_unready_leg", new=AsyncMock()))
    # A key's turn on the owner's subscription is refused; not what this tests.
    stack.enter_context(patch("app.core.credential_gate.refusal_for_options_async", new=AsyncMock(return_value=None)))


async def _sse(route, request, api_user, tmp_path):
    with ExitStack() as stack:
        bounds = engine_patches(stack, cwd=str(tmp_path))
        fake_run_sync = qe.run_sync

        async def run_sync(fn, *args, **kwargs):
            # The new chat's row is real, so the turn's events can be recorded.
            if getattr(fn, "__name__", "") == "create_session":
                database.create_session(kwargs["session_id"], "default")
            if fn is qe._resolve_note_references_for:  # the code under test runs for real
                return fn(*args, **kwargs)
            return await fake_run_sync(fn, *args, **kwargs)

        stack.enter_context(patch.object(qe, "run_sync", new=run_sync))
        stack.enter_context(patch.object(qe, "resolve_note_references", new=real_resolve))
        _common(stack)
        await _drain(await route(request, api_user=api_user, _=None))
    bounds.adapter.send_query.assert_awaited_once()
    [user] = [r for r in session_events.get_events(bounds.sync_engine.broadcast_message_added.await_args.kwargs["session_id"])
              if r["event_type"] == "message.user"]
    assert user["payload"]["content"] == request.prompt
    return bounds.adapter.send_query.await_args.args[0], user["payload"].get("agent_content")


@pytest.mark.parametrize("api_user", [OWNER, KEY], ids=["owner", "key"])
async def test_query_stream_expands_as_the_caller(notes, tmp_path, api_user):
    sent, recorded = await _sse(query_api.stream_one_shot_query,
                                QueryRequest(prompt=PROMPT, profile="claude-code"), api_user, tmp_path)
    assert sent == recorded == _expected(api_user)


@pytest.mark.parametrize("api_user", [OWNER, KEY], ids=["owner", "key"])
async def test_conversation_stream_expands_as_the_caller(notes, tmp_path, api_user):
    sent, recorded = await _sse(query_api.stream_conversation,
                                ConversationRequest(prompt=PROMPT, profile="claude-code"), api_user, tmp_path)
    assert sent == recorded == _expected(api_user)


@pytest.mark.parametrize("api_user", [OWNER, KEY], ids=["owner", "key"])
async def test_conversation_start_expands_as_the_caller(notes, api_user):
    state = _fake_state([_result_message()])
    with ExitStack() as stack:
        _bg_patches(stack, StubPool(state))
        fake_run_sync = qb.run_sync

        async def run_sync(fn, *args, **kwargs):
            if fn is database.create_session:  # real row, so events can be recorded
                database.create_session(kwargs["session_id"], "default")
            if fn is qe._resolve_note_references_for:  # the code under test runs for real
                return fn(*args, **kwargs)
            return await fake_run_sync(fn, *args, **kwargs)

        stack.enter_context(patch.object(qb, "run_sync", new=run_sync))
        stack.enter_context(patch.object(qb, "get_profile", return_value={"id": "default"}))
        _common(stack)
        result = await query_api.start_conversation(
            ConversationRequest(prompt=PROMPT, profile="default"), api_user=api_user, _=None,
        )
        task = qb._pending_background_tasks.get(result["session_id"])
        if task is not None:
            await asyncio.wait_for(task, 10)
    state.adapter.send_query.assert_awaited_once_with(_expected(api_user))
    [user] = [r for r in session_events.get_events(result["session_id"]) if r["event_type"] == "message.user"]
    # The chat shows what was typed; the event keeps what the agent got (#1401).
    assert user["payload"]["content"] == PROMPT
    assert user["payload"]["agent_content"] == _expected(api_user)

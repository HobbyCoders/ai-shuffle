"""Legacy ``@Notes/note-<id>`` references follow the notes API ownership rule.

The owner (no API key) may expand any note into a prompt; an API key only the
notes it wrote (``notes.api_user_id``). A reference to anyone else's note is
left as typed, so a key's session cannot pull an owner note in by id.
"""

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.query_engine as qe
from app.core.agent_config import resolve_note_references as real_resolve
from app.core.provider.events import SystemMessage
from app.db import database
from tests.core.engine_harness import StubPool, make_state, stream_response

OWNER_NOTE = "note-0a"
KEY_NOTE = "note-0b"
OTHER_KEY_NOTE = "note-0c"


@pytest.fixture
def notes(mock_db):
    database.create_note(OWNER_NOTE, "Owner", "owner secret")
    database.create_note(KEY_NOTE, "Mine", "key text", api_user_id="key-1")
    database.create_note(OTHER_KEY_NOTE, "Theirs", "other key text", api_user_id="key-2")


def _prompt():
    return f"a @Notes/{OWNER_NOTE} b @Notes/{KEY_NOTE} c @Notes/{OTHER_KEY_NOTE} d @Notes/note-ff"


def test_owner_expands_every_note(notes):
    out = qe._resolve_note_references_for(_prompt(), None)
    assert "owner secret" in out and "key text" in out and "other key text" in out
    assert "@Notes/note-ff" in out  # missing notes stay as typed


def test_key_expands_only_its_own_notes(notes):
    out = qe._resolve_note_references_for(_prompt(), "key-1")
    assert "[Note: Mine]\nkey text" in out
    assert "owner secret" not in out and f"@Notes/{OWNER_NOTE}" in out
    assert "other key text" not in out and f"@Notes/{OTHER_KEY_NOTE}" in out
    assert "@Notes/note-ff" in out


def _engine_patches(stack, pool, cwd):
    stack.enter_context(patch.object(qe, "get_profile", return_value={"id": "default", "config": {}}))
    options = MagicMock()
    options.include_partial_messages = False
    options.cwd = cwd
    options.model = None
    options.aih_provider_id = None
    stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})))
    # An API user may run Claude only on an API key, never the owner's plan.
    stack.enter_context(patch("app.core.credential_gate.claude_cli_uses_subscription", return_value=False))
    stack.enter_context(patch.object(qe, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qe, "is_subprocess_alive", return_value=True))
    stack.enter_context(patch.object(qe, "requires_reconnect", return_value=False))
    stack.enter_context(patch.object(qe, "mark_subprocess_oom_killable", new=MagicMock()))
    # The real expansion, not the harness's identity stub.
    stack.enter_context(patch.object(qe, "resolve_note_references", new=real_resolve))
    stack.enter_context(patch.object(qe, "push_memory", new=AsyncMock()))
    stack.enter_context(patch.object(qe, "_persist_turn_results", new=AsyncMock()))


async def _sent_prompt(prompt, api_user_id, tmp_path):
    session_id = f"sess-{uuid.uuid4()}"
    database.create_session(session_id=session_id, profile_id="default")
    state = make_state(send_resume=AsyncMock(), set_permission_mode=AsyncMock())
    state.adapter.receive_events = stream_response([SystemMessage(subtype="init", data={"session_id": "ses_x"})])
    with ExitStack() as stack:
        _engine_patches(stack, StubPool(state), str(tmp_path))
        async for _ in qe._stream_to_websocket_impl(
            prompt=prompt, session_id=session_id, profile_id="default", api_user_id=api_user_id,
        ):
            pass
    state.adapter.send_query.assert_awaited_once()
    return state.adapter.send_query.await_args.args[0]


async def test_key_turn_does_not_inline_an_owner_note(notes, tmp_path):
    sent = await _sent_prompt(f"read @Notes/{OWNER_NOTE}", "key-1", tmp_path)
    assert sent == f"read @Notes/{OWNER_NOTE}"


async def test_key_turn_inlines_its_own_note_and_owner_turn_inlines_any(notes, tmp_path):
    assert "key text" in await _sent_prompt(f"read @Notes/{KEY_NOTE}", "key-1", tmp_path)
    assert "owner secret" in await _sent_prompt(f"read @Notes/{OWNER_NOTE}", None, tmp_path)

"""#1401: a sent message that mentions a note by reference can be edited and restored.

The chat shows the message as typed (``read @Notes/<id>``). The worker that ran
it gave the agent the note's file path or text instead, so the transcript holds
that text. The turn records it on the ``message.user`` event at send time, and
the rewind guard matches the transcript against what was recorded. It never
expands the note again: the note may have changed since.

The message goes through the real chat intake, the turn runtime and the engine
entry; the agent and the transcript store are stubbed.
"""
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from app.api import commands
from app.core import query_engine, session_runtime, turn_manager, turn_recorder
from app.core.agent_config import resolve_note_references as real_resolve
from app.db import database, session_events, turns
from tests.api.test_chat_ws_v2_turns import _make_handler, _Patches
from tests.core.query_engine_flow_harness import engine_patches
from tests.core.test_rewind_resolution import SID, _asst_ev, _cp, _patch_checkpoints, rewind_env  # noqa: F401
from tests.core.test_steering_delivery_flows import _pump_env
from tests.rewind_helpers import execute_rewind_and_wait

NOTE = "note-0a1b2c"
SHOWN = f"read @Notes/{NOTE}"


@pytest.fixture
def chat(mock_db, monkeypatch, tmp_path):
    from app.core import config
    from app.core.config import settings

    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    database.register_install(settings.effective_instance_id, "test-runner", None, "local", "1", {}, approve_new=True)
    database.create_session(SID, "default", mode="chat", title="t")
    # As the worker that runs the chat has it: synced, no file of its own yet.
    database.create_note(NOTE, "Plan", "Secret word: pelican")
    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)


async def _send(prompt, tmp_path):
    """Send ``prompt`` from the composer and run its turn; return what the agent got."""
    with _Patches():
        handler = _make_handler()
        await handler.handle_query({"session_id": SID, "profile": "default", "prompt": prompt})
    [queued] = turns.get_queued_turns(SID)
    real_stream = query_engine._stream_to_websocket_impl
    real_create = turn_recorder.TurnRecorder.create_for_run
    with _pump_env(None), ExitStack() as stack:
        bounds = engine_patches(stack, cwd=str(tmp_path), session_row=database.get_session(SID))
        stack.enter_context(patch.object(query_engine, "resolve_note_references", new=real_resolve))
        stack.enter_context(patch.object(query_engine, "_stream_to_websocket_impl", real_stream))
        stack.enter_context(patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create))
        assert await session_runtime.SessionRuntime(turns.get_turn(queued["id"])).execute() == "completed"
    bounds.adapter.send_query.assert_awaited_once()
    return bounds.adapter.send_query.await_args.args[0]


@pytest.mark.parametrize("by_uuid", [False, True])
@pytest.mark.parametrize("include_response", [False, True])
async def test_a_sent_note_reference_rewinds(chat, rewind_env, monkeypatch, tmp_path, by_uuid, include_response):  # noqa: F811
    sent = await _send(SHOWN, tmp_path)
    assert sent == "read [Note: Plan]\nSecret word: pelican"
    [user] = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
    assert user["payload"]["content"] == SHOWN  # the chat shows what was typed
    session_events.append_events(SID, [_asst_ev("answer")])
    _patch_checkpoints(monkeypatch, [_cp("user-id", sent)])
    # The note changes afterwards: the rewind does not read it again.
    database.update_note(NOTE, content="something else")
    if by_uuid:
        request = commands.RewindRequestV2(target_uuid="user-id", include_response=include_response)
    else:
        # A live bubble (Edit or Restore) has no uuid: it names its text.
        request = commands.RewindRequestV2(
            target_uuid="", target_content=SHOWN, target_occurrence=0, include_response=include_response,
        )
    result = await execute_rewind_and_wait(SID, request)
    assert result.success, result.error


async def test_a_different_message_is_still_refused(chat, rewind_env, monkeypatch, tmp_path):  # noqa: F811
    sent = await _send(SHOWN, tmp_path)
    _patch_checkpoints(monkeypatch, [_cp("user-id", sent)])
    for content in (f"{SHOWN} please", "read @Notes/note-ffff"):
        result = await execute_rewind_and_wait(SID, commands.RewindRequestV2(
            target_uuid="", target_content=content, target_occurrence=0, include_response=False,
        ))
        assert not result.success and "content guard" in result.error


async def test_the_same_reference_sent_twice_rewinds_to_the_one_picked(chat, rewind_env, monkeypatch, tmp_path):  # noqa: F811
    first = await _send(SHOWN, tmp_path)
    database.update_note(NOTE, content="Secret word: heron")
    second = await _send(SHOWN, tmp_path)
    assert first != second
    _patch_checkpoints(monkeypatch, [_cp("first-id", first), _cp("second-id", second, index=1)])
    seen = []
    monkeypatch.setattr(commands, "_resolve_rewind_from_seq", lambda *a, **k: (seen.append(a[3]), (None, None))[1])
    result = await execute_rewind_and_wait(SID, commands.RewindRequestV2(
        target_uuid="", target_content=SHOWN, target_occurrence=1, include_response=False,
    ))
    assert result.success, result.error
    assert seen == ["second-id"]


async def test_a_message_without_a_note_reference_records_no_second_text(chat, rewind_env, monkeypatch, tmp_path):  # noqa: F811
    sent = await _send("read Notes/ as a folder", tmp_path)
    assert sent == "read Notes/ as a folder"
    [user] = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
    assert "agent_content" not in user["payload"]
    _patch_checkpoints(monkeypatch, [_cp("user-id", sent)])
    result = await execute_rewind_and_wait(SID, commands.RewindRequestV2(
        target_uuid="", target_content=sent, target_occurrence=0, include_response=False,
    ))
    assert result.success, result.error

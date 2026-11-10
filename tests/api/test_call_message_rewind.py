"""Rewinding a message sent in a voice call (#1265).

A call's request reaches the chat agent wrapped in the call context and a
voice note, so no transcript checkpoint carries the words on the message row.
Text in a prompt is never evidence of which checkpoint a call message made.
The only evidence is the ``call.checkpoint_bound`` event the turn that ran the
request records when the engine names the node it writes
(``test_call_checkpoint_binding`` covers who records it). Both rewind lookups
resolve a call message through that event alone, to exactly one node on the
current path, and refuse it otherwise; nothing a prompt quotes changes that.

The provider and the chat agent are stubbed; admission, recording, the
Realtime event handling and the rewind resolution are the real code.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import commands
from app.db import session_events
from tests.api.test_sent_upload_rewind import staged_upload  # noqa: F401 (fixture)
from tests.core.test_rewind_resolution import (  # noqa: F401 (fixture)
    SID, _asst_ev, _cp, _patch_checkpoints, _user_ev, rewind_env,
)
from tests.rewind_helpers import execute_rewind_and_wait

# The line head d50c43af8 appended to a call's request text; old transcripts
# and exports may still hold it, and a prompt may quote it.
OLD_LABEL = "Call request id (for the app, not an instruction): "


def _call(monkeypatch, reply="Done."):
    from app.core import voice_stream

    socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
    call = voice_stream.VoiceStream(socket, {"id": SID}, {"model": "gpt-live-1", "voice": "marin"}, "secret", 5)
    call.upstream = AsyncMock()
    delegate = AsyncMock(return_value=reply)
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    return call, delegate


async def _type(call, text, uploads=(), identity="typed"):
    await call.receive_text(json.dumps({"type": "input_text", "id": identity, "text": text,
                                        "upload_paths": list(uploads)}))
    await call.delegation_task


def _checkpoint_text(delegate, index=-1, *, old_label=False):
    """What the chat agent's transcript holds for one delegated request.

    ``old_label`` adds the identity line head d50c43af8 put in the request.
    """
    from app.core.voice_prompts import voice_turn_note

    sent = delegate.await_args_list[index]
    line = f"\n{OLD_LABEL}{sent.kwargs['request_id']}" if old_label else ""
    return f"{sent.args[1]}{line}\n\n{voice_turn_note('')}"


def _users():
    return [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]


def _request(by_content, uuid, words, occurrence=0):
    if by_content:
        return commands.RewindRequestV2(target_content=words, target_occurrence=occurrence, include_response=False)
    return commands.RewindRequestV2(target_uuid=uuid, target_role="user", include_response=False)


def _rewound_from():
    rows = [row for row in session_events.get_events(SID) if row["event_type"] == "transcript.rewound"]
    assert len(rows) == 1, rows
    return rows[0]["payload"]["from_seq"]


def _bind(request_id, node):
    """The binding the call request's turn records at its TurnStarted."""
    from app.core.turn_recorder import TurnRecorder

    return TurnRecorder(SID, None, None, None, None).record_call_binding(request_id, node)


async def _assert_cut(rewind_env, request, node, row, uploads=()):
    result = await execute_rewind_and_wait(SID, request)
    assert result.success, result.error
    assert rewind_env.truncate_calls == [node]
    assert _rewound_from() == row["seq"]
    assert result.restored_upload_paths == list(uploads)
    return result


async def _assert_refused(rewind_env, *requests):
    before = session_events.get_events(SID)
    for request in requests:
        result = await execute_rewind_and_wait(SID, request)
        assert result.success is False, request
        assert "content guard" in (result.error or "")
    assert rewind_env.truncate_calls == []  # the model's history is untouched
    assert session_events.get_events(SID) == before  # and so is the visible one


@pytest.mark.parametrize("old_label", [False, True])
async def test_a_call_message_is_refused_both_ways_and_its_attachment_stays_recorded(
    staged_upload, rewind_env, monkeypatch, old_label,
):
    """No binding ties the message to the checkpoint its request made, so
    neither lookup cuts; the attachment is still recorded on the row for the
    day one does. A checkpoint still holding the old identity line is no
    evidence either."""
    call, delegate = _call(monkeypatch)
    words = "inspect this"
    await _type(call, words, [str(staged_upload)])
    (row,) = _users()
    assert row["payload"]["upload_paths"] == [str(staged_upload)]
    assert row["payload"]["call_request_id"] == delegate.await_args.kwargs["request_id"]
    session_events.append_events(SID, [_asst_ev("answer", uuid="answer-id")])
    _patch_checkpoints(monkeypatch, [_cp("call-cp", _checkpoint_text(delegate, old_label=old_label))])

    await _assert_refused(rewind_env, _request(True, None, words), _request(False, "call-cp", None))


@pytest.mark.parametrize("by_content", [False, True])
async def test_a_bound_call_message_rewinds_at_its_own_checkpoint_and_returns_its_attachment(
    staged_upload, rewind_env, monkeypatch, by_content,
):
    """The issue's case: a message with an attachment sent in a call, whose
    turn started and bound its node. Both lookups cut the model's history at
    that node and the visible one at that message, and give the file back."""
    call, delegate = _call(monkeypatch)
    session_events.append_events(SID, [_user_ev("earlier"), _asst_ev("ok", uuid="ok-id")])
    words = "inspect this"
    await _type(call, words, [str(staged_upload)])
    row = _users()[-1]
    assert await _bind(row["payload"]["call_request_id"], "call-cp")
    session_events.append_events(SID, [_asst_ev("answer", uuid="answer-id")])
    _patch_checkpoints(monkeypatch, [_cp("earlier-cp", "earlier"), _cp("call-cp", _checkpoint_text(delegate))])

    await _assert_cut(rewind_env, _request(by_content, "call-cp", words), "call-cp", row, [str(staged_upload)])


def _record_call_words(words, *, request_id=None):
    """A call's message row as the call path stores it (``record_voice_exchange``)."""
    from app.core import turn_recorder

    return asyncio.ensure_future(turn_recorder.record_voice_exchange(
        SID, words, "Sure.", model="gpt-live-1", user_source="voice",
        **({"call_request_id": request_id} if request_id else {})))


@pytest.mark.parametrize("request_id", [None, "ab" * 16])
async def test_a_checkpoint_with_the_same_words_is_never_used_for_a_call_message(
    staged_upload, rewind_env, monkeypatch, request_id,
):
    """Rows from before identities were recorded, and rows from head d50c43af8
    that carry one but have no binding: nothing is cut, no words compared."""
    from app.core.voice_prompts import voice_turn_note

    await _record_call_words("look at the logs", request_id=request_id)
    _patch_checkpoints(monkeypatch, [_cp("old-cp", "look at the logs"),
                                     _cp("old-wrapped", f"user: look at the logs\n\n{voice_turn_note('')}")])

    await _assert_refused(rewind_env, _request(True, None, "look at the logs"),
                          _request(False, "old-cp", None), _request(False, "old-wrapped", None))


@pytest.mark.parametrize("by_content", [False, True])
async def test_a_steered_call_request_quoted_by_a_later_prompt_is_never_cut_at_the_call_message(
    staged_upload, rewind_env, monkeypatch, by_content,
):
    """Review S1. Request R was steered into a running turn: its row records R
    and it has no checkpoint of its own. A later typed prompt quotes R's old
    identity line (from an exported transcript). That prompt is not the call
    message's checkpoint: rewinding the call message is refused, and rewinding
    the typed prompt cuts both histories at the typed prompt itself."""
    request_id = "cd" * 16
    await _record_call_words("run the tests", request_id=request_id)
    quoted = f"Why did this one fail?\n{OLD_LABEL}{request_id}"
    session_events.append_events(SID, [_user_ev(quoted)])
    typed = _users()[-1]
    _patch_checkpoints(monkeypatch, [_cp("running-turn", "start the build"), _cp("quoting-cp", quoted)])

    if by_content:
        await _assert_refused(rewind_env, _request(True, None, "run the tests"))
        return
    result = await execute_rewind_and_wait(SID, _request(False, "quoting-cp", None))
    assert result.success, result.error
    assert rewind_env.truncate_calls == ["quoting-cp"]
    assert _rewound_from() == typed["seq"]


@pytest.mark.parametrize("by_content", [False, True])
async def test_an_ordinary_prompt_holding_the_old_identity_line_rewinds_normally(
    staged_upload, rewind_env, monkeypatch, by_content,
):
    """Text that looks like an identity is just text: the prompt is found by its words."""
    text = f"Here is the line from the export:\n{OLD_LABEL}{'ef' * 16}"
    session_events.append_events(SID, [_user_ev("first"), _user_ev(text)])
    row = _users()[-1]
    _patch_checkpoints(monkeypatch, [_cp("first-cp", "first"), _cp("label-cp", text)])

    result = await execute_rewind_and_wait(SID, _request(by_content, "label-cp", text))

    assert result.success, result.error
    assert rewind_env.truncate_calls == ["label-cp"]
    assert _rewound_from() == row["seq"]


async def test_a_call_message_whose_turn_never_made_a_node_is_refused_even_if_its_request_is_pasted(
    staged_upload, rewind_env, monkeypatch,
):
    """The request's turn was cancelled before it ran (no node, no checkpoint).
    The caller then pastes the request, as an old export shows it, into a new
    prompt: that prompt's checkpoint is not the call message's, so the call
    message stays refused both ways."""
    call, delegate = _call(monkeypatch)
    await _type(call, "inspect this", [str(staged_upload)])
    pasted = _checkpoint_text(delegate, old_label=True)
    session_events.append_events(SID, [_user_ev(pasted)])
    _patch_checkpoints(monkeypatch, [_cp("pasted-cp", pasted)])

    await _assert_refused(rewind_env, _request(True, None, "inspect this"))
    # The pasted prompt itself is an ordinary prompt and is cut at its own row.
    result = await execute_rewind_and_wait(SID, _request(False, "pasted-cp", None))
    assert result.success, result.error
    assert _rewound_from() == _users()[-1]["seq"]


@pytest.mark.parametrize("by_content", [False, True])
@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("bound", [False, True])
async def test_two_call_messages_with_the_same_words_each_cut_only_at_their_own_binding(
    staged_upload, rewind_env, monkeypatch, by_content, which, bound,
):
    call, delegate = _call(monkeypatch)
    await _type(call, "do it", identity="first")
    if bound:
        assert await _bind(_users()[0]["payload"]["call_request_id"], "cp-0")
    await _type(call, "do it", [str(staged_upload)], identity="second")
    rows = _users()
    if bound:
        assert await _bind(rows[1]["payload"]["call_request_id"], "cp-1")
    _patch_checkpoints(monkeypatch, [_cp(f"cp-{n}", _checkpoint_text(delegate, n)) for n in (0, 1)])
    request = _request(by_content, f"cp-{which}", "do it", occurrence=which)

    if not bound:
        await _assert_refused(rewind_env, request)
        return
    await _assert_cut(rewind_env, request, f"cp-{which}", rows[which],
                      [str(staged_upload)] if which else [])


async def _bound_call(monkeypatch, staged_upload, words="inspect this"):
    call, delegate = _call(monkeypatch)
    await _type(call, words, [str(staged_upload)])
    row = _users()[-1]
    return row, row["payload"]["call_request_id"], _checkpoint_text(delegate)


@pytest.mark.parametrize("second_node", ["call-cp-retry", "call-cp"])
async def test_two_bindings_for_one_request_resolve_only_to_one_and_the_same_node(
    staged_upload, rewind_env, monkeypatch, second_node,
):
    """Item 3: a turn that ran twice (a retry, a requeue) and recorded two
    nodes for one request: neither is the request's, so both lookups refuse.
    The same node recorded twice is still that one node."""
    row, request_id, text = await _bound_call(monkeypatch, staged_upload)
    assert await _bind(request_id, "call-cp")
    assert await _bind(request_id, second_node)
    _patch_checkpoints(monkeypatch, [_cp("call-cp", text), _cp("call-cp-retry", text)])
    requests = (_request(True, None, "inspect this"), _request(False, "call-cp", None),
                _request(False, "call-cp-retry", None))
    if second_node == "call-cp":
        await _assert_cut(rewind_env, requests[0], "call-cp", row, [str(staged_upload)])
        return
    await _assert_refused(rewind_env, *requests)


async def test_a_bound_node_not_on_the_current_path_is_refused(staged_upload, rewind_env, monkeypatch):
    """Item 5: the node was rewound away, compacted out, or is missing on the
    worker the chat moved to: no user checkpoint carries it, so refuse."""
    row, request_id, text = await _bound_call(monkeypatch, staged_upload)
    assert await _bind(request_id, "gone-node")
    _patch_checkpoints(monkeypatch, [_cp("other-cp", text)])

    await _assert_refused(rewind_env, _request(True, None, "inspect this"), _request(False, "other-cp", None),
                          _request(False, "gone-node", None))


async def test_a_node_bound_to_two_requests_is_refused(staged_upload, rewind_env, monkeypatch):
    """Item 1: a checkpoint that holds more than one request binds neither."""
    row, request_id, text = await _bound_call(monkeypatch, staged_upload)
    assert await _bind(request_id, "call-cp")
    assert await _bind("ef" * 16, "call-cp")
    _patch_checkpoints(monkeypatch, [_cp("call-cp", text)])

    await _assert_refused(rewind_env, _request(True, None, "inspect this"), _request(False, "call-cp", None))


async def test_a_call_message_queued_behind_a_running_turn_is_refused(staged_upload, rewind_env, monkeypatch):
    """Item 1: the call recorded its message while another turn was still
    answering; that turn's reply sits after the message in the visible history
    but before the request's node in the model's. The cuts would differ."""
    session_events.append_events(SID, [_user_ev("start the build")])
    row, request_id, text = await _bound_call(monkeypatch, staged_upload)
    session_events.append_events(SID, [_asst_ev("build finished", uuid="build-id")])
    assert await _bind(request_id, "call-cp")
    _patch_checkpoints(monkeypatch, [_cp("build-cp", "start the build"), _cp("call-cp", text)])

    await _assert_refused(rewind_env, _request(True, None, "inspect this"), _request(False, "call-cp", None))


async def test_a_binding_recorded_before_its_message_or_for_another_row_is_refused(
    staged_upload, rewind_env, monkeypatch,
):
    """A binding counts only for the one row carrying its id, recorded after it."""
    request_id = "12" * 16
    assert await _bind(request_id, "early-cp")
    await _record_call_words("look again", request_id=request_id)
    _patch_checkpoints(monkeypatch, [_cp("early-cp", "user: look again")])

    await _assert_refused(rewind_env, _request(True, None, "look again"), _request(False, "early-cp", None))


@pytest.mark.parametrize("by_content", [False, True])
async def test_a_typed_prompt_with_a_call_messages_words_cuts_at_the_typed_prompt(
    staged_upload, rewind_env, monkeypatch, by_content,
):
    """Item 9. The base's text fallback paired the typed prompt's checkpoint
    with the earlier call row of the same words and cut the visible history
    there. Call rows are never counted as ordinary prompts now, so the typed
    prompt resolves at itself both ways."""
    await _record_call_words("ship it")
    session_events.append_events(SID, [_user_ev("ship it")])
    typed = _users()[-1]
    _patch_checkpoints(monkeypatch, [_cp("typed-cp", "ship it")])

    await _assert_cut(rewind_env, _request(by_content, "typed-cp", "ship it", occurrence=1 if by_content else 0),
                      "typed-cp", typed)


def _realtime(monkeypatch):
    """An attached Realtime call: the voice model writes the request itself."""
    from app.core import voice_bridge, voice_stream

    delegate = AsyncMock(return_value="")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    monkeypatch.setattr(voice_bridge, "_record_session_usage", lambda *a, **k: None)
    bridge = voice_bridge.VoiceBridge(SID, "rtc_1", api_key="k", model="gpt-realtime-2", max_call_minutes=30,
                                      conversation_surface=True)
    bridge._send = AsyncMock()
    bridge._record_tool_exchange = AsyncMock()
    serviced = []
    service = bridge._maybe_service_call

    def track(*args):
        task = service(*args)
        if task is not None:
            serviced.append(task)
        return task

    bridge._maybe_service_call = track
    return bridge, delegate, serviced


async def _submit(bridge, serviced, call_id, request):
    await bridge._handle_event({"type": "response.function_call_arguments.done", "call_id": call_id,
                                "name": "submit_to_chat", "arguments": json.dumps({"request": request})})
    await asyncio.gather(*serviced)


async def test_late_realtime_words_never_take_the_next_requests_identity(staged_upload, rewind_env, monkeypatch):
    """Review S2, through the event handlers. Request A is submitted before
    its words are transcribed; A's response ends; A's words arrive late; B is
    submitted before its own words. The flush for B stores A's words: they
    must not be linked to B, and neither message's rewind cuts anywhere."""
    bridge, delegate, serviced = _realtime(monkeypatch)
    await bridge._handle_event({"type": "conversation.item.created",
                                "item": {"id": "item-a", "role": "user", "content": []}})
    await _submit(bridge, serviced, "call-a", "Run the tests")
    await bridge._handle_event({"type": "response.output_audio_transcript.done", "transcript": "On it."})
    await bridge._handle_event({"type": "response.done", "response": {}})
    await bridge._handle_event({"type": "conversation.item.input_audio_transcription.completed",
                                "item_id": "item-a", "transcript": "run the tests please"})
    await bridge._handle_event({"type": "conversation.item.created",
                                "item": {"id": "item-b", "role": "user", "content": []}})
    await _submit(bridge, serviced, "call-b", "Check the build")
    await bridge._handle_event({"type": "response.done", "response": {}})

    assert delegate.await_count == 2
    assert all("request_id" not in call.kwargs for call in delegate.await_args_list)
    words = [row for row in _users() if row["payload"].get("content") == "run the tests please"]
    assert len(words) == 1
    assert "call_request_id" not in words[0]["payload"]
    from app.core.voice_prompts import voice_turn_note
    _patch_checkpoints(monkeypatch, [_cp(f"rt-{n}", f"{text}\n\n{voice_turn_note('')}")
                                     for n, text in enumerate(("Run the tests", "Check the build"))])

    await _assert_refused(rewind_env, _request(True, None, "run the tests please"),
                          _request(False, "rt-0", None), _request(False, "rt-1", None))


async def test_a_realtime_request_reworded_by_the_voice_model_is_refused(staged_upload, rewind_env, monkeypatch):
    """``submit_to_chat`` words need not match the caller's at all; no binding, no cut."""
    from app.core.voice_prompts import attachment_note, voice_turn_note

    bridge, delegate, serviced = _realtime(monkeypatch)
    spoken = "um can you look at what I sent"
    await bridge._handle_event({"type": "conversation.item.created", "item": {
        "id": "typed-1", "role": "user",
        "content": [{"type": "input_text", "text": spoken + attachment_note([str(staged_upload)])}]}})
    await _submit(bridge, serviced, "call-1", "Inspect the attached photo")
    (row,) = _users()
    assert row["payload"]["upload_paths"] == [str(staged_upload)]
    assert "call_request_id" not in row["payload"]
    _patch_checkpoints(monkeypatch, [_cp("rt-cp", f"Inspect the attached photo\n\n{voice_turn_note('')}")])

    await _assert_refused(rewind_env, _request(True, None, spoken), _request(False, "rt-cp", None))


async def test_the_identity_rides_the_queued_turn_not_its_prompt_and_is_not_forwarded(staged_upload, monkeypatch):
    """The real delegation path as far as the queued turn: the identity is in
    the turn's payload and the prompt is exactly the request, so the model,
    exports and search see nothing new. A chat another worker runs records the
    forwarded request as its own message, so the identity is not forwarded."""
    from app.core import voice_remote, voice_stream
    from app.db import database, turns

    real = database.get_session
    monkeypatch.setattr(voice_stream.database, "get_session", lambda sid: {**real(sid), "model": "text-model"})
    error = await asyncio.wait_for(voice_stream.delegate_to_selected_model(
        SID, "Run the tests", on_submitted=AsyncMock(), request_id="ab" * 16), 20)
    assert not error, error
    (queued,) = turns.get_queued_turns(SID)
    assert queued["prompt"] == "Run the tests"
    assert queued["payload"]["call_request_id"] == "ab" * 16

    forwarded = AsyncMock(return_value="owner-turn")
    monkeypatch.setattr(voice_remote, "owner_of", lambda row: "owner-device")
    monkeypatch.setattr(voice_remote, "submit", forwarded)
    await asyncio.wait_for(voice_stream.delegate_to_selected_model(
        SID, "Run the tests", on_submitted=AsyncMock(), request_id="cd" * 16), 20)
    assert forwarded.await_args.args[2] == "Run the tests"
    assert "request_id" not in forwarded.await_args.kwargs
    assert "cd" * 16 not in json.dumps([forwarded.await_args.args[3:], forwarded.await_args.kwargs], default=str)

"""Protocol tests for server-owned Live delegation and fal.ai audio frames."""
import base64
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import msgpack
import pytest
from app.core import voice_stream




@pytest.mark.asyncio
@pytest.mark.parametrize("history,greet,opens", [([],True,True),([{"role":"user","content":"Earlier conversation"}],True,False),([],False,False)])
async def test_live_startup_sends_opening_only_on_first_browser_call(history,greet,opens):
    call=voice_stream.VoiceStream(AsyncMock(),{"id":"s1"},{"model":"gpt-live-1","voice":"marin","greet":greet,"options":{"opening":"Hello caller.","instructions":"Be concise."}},"secret",3600)
    call.upstream=AsyncMock()
    call.upstream.recv.return_value=json.dumps({"type":"session.started"})
    await call.start_live(history)
    sent=[json.loads(args.args[0]) for args in call.upstream.send.call_args_list]
    assert sent[0]["session"]["instructions"] == "Be concise."
    assert len(sent) == (2 if opens else 1)
    if opens:
        assert sent[1]["type"] == "session.instructions.append"
        assert sent[1]["delegation_id"] is None
        assert sent[1]["content"].endswith("Hello caller.")


@pytest.mark.asyncio
async def test_responses_delegation_never_dispatches_client_worker(monkeypatch):
    call=voice_stream.VoiceStream(AsyncMock(),{"id":"s1"},{"model":"gpt-live-1","voice":"marin","options":{"delegation_type":"responses","delegation_model":"prov:openai:reasoner"}},"secret",3600)
    call.upstream=Upstream([json.dumps({"type":"session.delegation.created","delegation":{"id":"managed","input":[]}})])
    delegate=AsyncMock()
    monkeypatch.setattr(voice_stream,"delegate_to_selected_model",delegate)
    await call.receive_upstream()
    delegate.assert_not_called()


class Upstream:
    def __init__(self, events):
        self.events = events
        self.sent = []
    def __aiter__(self):
        return self.iterate()
    async def iterate(self):
        for event in self.events:
            yield event
    async def send(self, event):
        self.sent.append(event)


def stream(model="gpt-live-1"):
    socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
    return voice_stream.VoiceStream(socket, {"id": "s1"}, {"model": model, "voice": "marin"}, "secret", 5)


@pytest.mark.asyncio
async def test_live_delegation_is_correlated_and_uses_accumulated_user_context(monkeypatch):
    delegate = AsyncMock(return_value="The answer is forty-two.")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    call = stream()
    call.flush = AsyncMock()
    did = "delegation-1"
    call.upstream = Upstream([json.dumps(e) for e in [
        {"type": "session.input_transcript.delta", "delta": "What is six", "end_ms": 1000},
        {"type": "session.input_transcript.delta", "delta": " times seven?", "end_ms": 1500},
        {"type": "session.delegation.created", "delegation": {"id": did, "offset_ms": 1600}},
        {"type": "session.delegation.created", "delegation": {"id": did, "offset_ms": 1600}},
    ]])
    await call.receive_upstream()
    await call.delegation_task
    call.flush.assert_awaited_once()
    delegate.assert_awaited_once_with("s1", "Live conversation context (only the latest new request needs action):\nuser: What is six times seven?\n")
    assert json.loads(call.upstream.sent[0]) == {
        "type": "session.commentary.append", "delegation_id": did, "content": "The answer is forty-two.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,provider", [("prov:chosen-provider:chosen-model", "chosen-provider"), ("haiku", None)])
async def test_handoff_reuses_same_session_and_composer_binding(monkeypatch, selected, provider):
    from app.core import provider_agent, turn_manager
    monkeypatch.setattr(voice_stream.database, "get_session", lambda _: {
        "model": selected, "profile_id": "profile-1", "effort": "high",
        "provider_id": "voice-funding-provider", "permission_mode": "default", "fast": True,
    })
    monkeypatch.setattr(provider_agent, "validate_provider_model", lambda *args: SimpleNamespace(ok=True))
    dispatch = AsyncMock()
    monkeypatch.setattr(provider_agent, "dispatch_worker", dispatch)
    submit = AsyncMock(return_value="turn-1")
    monkeypatch.setattr(turn_manager, "submit_async", submit)
    monkeypatch.setattr(turn_manager, "wait_settled", AsyncMock(return_value={"state": "completed"}))
    monkeypatch.setattr(voice_stream, "_turn_reply", lambda session, turn: "Done" if (session, turn) == ("s1", "turn-1") else "wrong turn")
    assert await voice_stream.delegate_to_selected_model("s1", "user: test") == "Done"
    assert submit.await_args.args == ("s1", "user")
    assert submit.await_args.kwargs["payload"] == {"user_message_already_recorded":True}
    overrides = submit.await_args.kwargs["overrides"]
    assert (overrides["provider"], overrides["model"], overrides["effort"]) == (provider, selected, "high")
    assert overrides["permission_mode"] == "default" and overrides["fast"] is True
    dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "parked", "aborted"])
async def test_handoff_never_reports_success_before_completion(monkeypatch, state):
    from app.core import turn_manager
    monkeypatch.setattr(voice_stream.database, "get_session", lambda _: {"model": "haiku", "profile_id": "p"})
    monkeypatch.setattr(turn_manager, "submit_async", AsyncMock(return_value="turn"))
    monkeypatch.setattr(turn_manager, "wait_settled", AsyncMock(return_value={"state": state} if state else None))
    reply = await voice_stream.delegate_to_selected_model("s1", "test")
    assert "No final result" in reply or "stopped or failed" in reply


def test_commentary_chunks_preserve_complete_unicode_results():
    text = "A verified result. " + "世界🌎" * 200
    chunks = list(voice_stream.commentary_chunks(text))
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-8")) <= 400 for chunk in chunks)


@pytest.mark.asyncio
async def test_live_and_fal_output_audio_are_decoded_without_credentials():
    pcm = bytes([0, 0, 1, 0])
    for model, event in [
        ("gpt-live-1", json.dumps({"type": "session.output_audio.delta", "delta": base64.b64encode(pcm).decode()})),
        ("fal-ai/personaplex/realtime", msgpack.packb({"audio": pcm, "text": "Hello"})),
    ]:
        call = stream(model)
        call.upstream = Upstream([event])
        await call.receive_upstream()
        call.websocket.send_bytes.assert_awaited_once_with(pcm)
        for args in call.websocket.send_json.call_args_list:
            assert "secret" not in json.dumps(args.args)


@pytest.mark.asyncio
async def test_a_provider_ending_the_session_is_named_to_the_browser_and_log(caplog):
    """#1671: a clean provider close used to reach the browser as a bare
    close, indistinguishable from the user pressing End."""
    call = stream()
    with caplog.at_level(logging.INFO, logger="app.core.voice_stream"):
        await call.report_quiet_end(upstream_ended=True)
    call.websocket.send_json.assert_awaited_once_with({"type": "ended", "reason": "provider-closed"})
    assert any("provider ended the session" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_browser_disconnect_is_logged_without_messaging_the_closed_socket(caplog):
    call = stream()
    with caplog.at_level(logging.INFO, logger="app.core.voice_stream"):
        await call.report_quiet_end(upstream_ended=False)
    call.websocket.send_json.assert_not_awaited()
    assert any("browser closed the audio connection" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_browser_cannot_inject_delegation_or_provider_events():
    call = stream()
    call.websocket.receive = AsyncMock(return_value={"type": "websocket.receive", "text": '{"type":"session.commentary.append"}'})
    with pytest.raises(ValueError, match="Only PCM"):
        await call.receive_browser()


@pytest.mark.asyncio
async def test_browser_audio_is_encoded_for_each_provider():
    pcm = bytes([0, 0, 1, 0])
    for model in ("gpt-live-1", "fal-ai/personaplex/realtime"):
        call = stream(model)
        call.upstream = Upstream([])
        call.websocket.receive = AsyncMock(side_effect=[{"type": "websocket.receive", "bytes": pcm}, {"type": "websocket.disconnect"}])
        await call.receive_browser()
        event = json.loads(call.upstream.sent[0]) if call.is_live else msgpack.unpackb(call.upstream.sent[0], raw=False)
        assert event["audio"] == (base64.b64encode(pcm).decode() if call.is_live else pcm)
        if not call.is_live:
            from app.core.voice_models import model_details
            assert event["prompt"] == model_details(model)["prompts"][0]["text"]


@pytest.mark.asyncio
async def test_streamed_typed_attachment_is_acknowledged_and_delegated_once(monkeypatch):
    call = stream()
    call.flush = AsyncMock()
    delegate = AsyncMock(return_value="File inspected.")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    call.upstream = Upstream([])
    text = 'Inspect the attached file: "/workspace/uploads/report.pdf"'
    raw = json.dumps({"type": "input_text", "id": "typed-1", "text": text})
    await call.receive_text(raw)
    await call.delegation_task
    await call.receive_text(raw)
    delegate.assert_awaited_once()
    assert delegate.await_args.args[0] == "s1"
    assert text in delegate.await_args.args[1]
    events = [c.args[0] for c in call.websocket.send_json.await_args_list]
    assert [e for e in events if e["type"] == "text.accepted"] == [
        {"type": "text.accepted", "id": "typed-1"},
        {"type": "text.accepted", "id": "typed-1"},
    ]
    assert json.loads(call.upstream.sent[0]) == {
        "type": "session.commentary.append", "delegation_id": None, "content": "File inspected."
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    {"type": "session.delegation.created"},
    {"type": "input_text", "id": "ok", "text": "test", "tools": []},
    {"type": "input_text", "id": "bad/id", "text": "test"},
    {"type": "input_text", "id": "ok", "text": 4},
    [],
])
async def test_text_control_is_not_a_provider_event_passthrough(event):
    call = stream()
    with pytest.raises(ValueError, match="Only PCM"):
        await call.receive_text(json.dumps(event))
    assert call.delegation_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [" ", "🌎" * 4001])
async def test_empty_or_oversize_text_is_rejected_without_closing_call(text):
    call = stream()
    await call.receive_text(json.dumps({"type": "input_text", "id": "request", "text": text}, ensure_ascii=False))
    assert call.websocket.send_json.await_args.args[0]["type"] == "text.rejected"
    assert call.delegation_task is None
    assert not call.closed


@pytest.mark.asyncio
async def test_json_escaping_does_not_disconnect_an_oversize_draft():
    call = stream()
    await call.receive_text(json.dumps({"type": "input_text", "id": "escaped", "text": "\t" * 16001}))
    assert call.websocket.send_json.await_args.args[0]["type"] == "text.rejected"
    assert not call.closed


@pytest.mark.asyncio
async def test_personaplex_text_is_explicitly_unsupported_not_disconnected():
    call = stream("fal-ai/personaplex/realtime")
    await call.receive_text(json.dumps({"type": "input_text", "id": "request", "text": "file"}))
    event = call.websocket.send_json.await_args.args[0]
    assert event["type"] == "text.rejected"
    assert "does not support text or attachments" in event["message"]
    assert not call.closed


@pytest.mark.asyncio
async def test_busy_text_retains_draft_without_recording_or_dispatch(monkeypatch):
    call = stream()
    monkeypatch.setattr(voice_stream, "_workers", set(range(8)))
    call.flush = AsyncMock()
    await call.receive_text(json.dumps({"type": "input_text", "id": "request", "text": "file"}))
    assert call.websocket.send_json.await_args.args[0]["type"] == "text.rejected"
    call.flush.assert_not_awaited()
    assert call.delegation_task is None


@pytest.mark.asyncio
async def test_typed_producer_records_attachment_text_before_chat_handoff(monkeypatch):
    from app.core import turn_recorder
    call = stream()
    call.upstream = Upstream([])
    order = []
    async def record(session, user, assistant, **kwargs):
        order.append(("record", session, user, kwargs["model"]))
    async def delegate(session, prompt):
        assert order and order[0][0] == "record"
        order.append(("delegate", session, prompt))
        return "Inspected."
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", record)
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    await call.receive_text(json.dumps({"type": "input_text", "id": "producer", "text": "Read /workspace/uploads/report.pdf"}))
    await call.delegation_task
    assert len(order) == 2
    assert order[0][1] == order[1][1] == "s1"
    assert "/workspace/uploads/report.pdf" in order[0][2]
    assert order[0][3] == "gpt-live-1"


@pytest.mark.asyncio
async def test_fal_preroll_keeps_the_first_audio_frame():
    call = stream("fal-ai/personaplex/realtime")
    frame = msgpack.packb({"audio": bytes(3840), "text": ""})
    call.upstream = Upstream([])
    call.upstream.recv = AsyncMock(return_value=frame)
    await call.prime_fal()
    assert call.first_frame == frame
    await call.receive_upstream()
    call.websocket.send_bytes.assert_awaited_once_with(bytes(3840))
    assert call.first_frame is None


def test_live_session_uses_client_delegation_not_realtime_tools():
    config = voice_stream.live_session_config({"voice": "marin"}, [])
    assert config["model"] == "gpt-live-1"
    assert config["delegation"] == {"type": "client"}
    assert config["audio"]["format"]["rate"] == 24000
    assert "tools" not in config

def test_live_prompt_only_delegates_when_backend_help_is_needed():
    from app.core.voice_prompts import LIVE_INSTRUCTIONS
    assert "Answer directly" in LIVE_INSTRUCTIONS
    assert "Stopping speech does not cancel an action" in LIVE_INSTRUCTIONS
    assert "careful reasoning" in LIVE_INSTRUCTIONS


def test_turn_reply_filters_other_turns_reasoning_and_pre_tool_commentary(monkeypatch):
    from app.db import session_events
    events = [
        {"seq":1,"turn_id":"target","event_type":"assistant.text_delta","payload":{"text":"Checking…"}},
        {"seq":2,"turn_id":"target","event_type":"tool.result","payload":{}},
        {"seq":3,"turn_id":"target","event_type":"assistant.thinking_delta","payload":{"text":"private reasoning"}},
        {"seq":4,"turn_id":"other","event_type":"assistant.text_delta","payload":{"text":"Other turn"}},
        {"seq":5,"turn_id":"target","event_type":"assistant.text_delta","payload":{"text":"Verified result."}},
    ]
    monkeypatch.setattr(session_events, "get_events", lambda sid, from_seq, limit: [e for e in events if e["seq"] > from_seq])
    assert voice_stream._turn_reply("session", "target") == "Verified result."

@pytest.mark.asyncio
async def test_attached_live_voice_forces_same_session_agent_delegation(monkeypatch):
    row = {"mode": "chat", "model": None, "profile_id": "primary", "provider_id": "text-provider", "voice_config": {}}
    monkeypatch.setattr(voice_stream.database, "get_session", lambda _: row)
    monkeypatch.setattr(voice_stream.database, "get_profile", lambda _: {"config": {"model": "agent-model"}})
    accepted = []
    async def submit(sid, source, **kwargs):
        accepted.append((sid, source, kwargs))
        return "same-turn"
    from app.core import turn_manager
    monkeypatch.setattr(turn_manager, "submit_async", submit)
    assert await voice_stream.delegate_to_selected_model("home", "Spoken request", on_submitted=AsyncMock()) == ""
    assert accepted[0][0] == "home" and accepted[0][2]["profile_id"] == "primary"
    assert accepted[0][2]["overrides"]["model"] == "agent-model"
    assert accepted[0][2]["overrides"]["provider"] == "text-provider"
    config = voice_stream.live_session_config({"model": voice_stream.LIVE_MODEL, "voice": "marin", "conversation_surface": True,
        "options": {"delegation_type": "responses", "delegation_model": "different-model"}}, "")
    assert config["delegation"]["type"] == "client"

@pytest.mark.asyncio
async def test_attached_webrtc_binding_delegates_with_realtime_options(monkeypatch):
    """A WebRTC binding stores RealtimeOptions; LiveOptions must not reject it."""
    from app.core.voice_options import RealtimeOptions
    row = {"mode": "chat", "model": "haiku", "profile_id": "primary", "voice_config": {
        "model": "gpt-realtime", "voice": "marin", "conversation_surface": True,
        "options": RealtimeOptions().model_dump(),
    }}
    monkeypatch.setattr(voice_stream.database, "get_session", lambda _: row)
    submit = AsyncMock(return_value="turn-1")
    from app.core import turn_manager
    monkeypatch.setattr(turn_manager, "submit_async", submit)
    assert await voice_stream.delegate_to_selected_model("home", "Spoken request", on_submitted=AsyncMock()) == ""
    overrides = submit.await_args.kwargs["overrides"]
    assert overrides["model"] == "haiku"
    assert overrides["system_prompt_append"] == voice_stream.DELEGATION_INSTRUCTIONS


def test_live_prompt_forbids_backchannels_and_waiting_filler():
    from app.core.voice_prompts import LIVE_INSTRUCTIONS
    # GPT Live reads these headings; the old default asked for "moderate" backchannels.
    assert "Backchannel policy: Do not use backchannels." in LIVE_INSTRUCTIONS
    assert "moderate" not in LIVE_INSTRUCTIONS
    assert "Keep listening while the user pauses" in LIVE_INSTRUCTIONS
    assert "Do not resume or repeat the sentence you were cut off in" in LIVE_INSTRUCTIONS
    assert "still on the call" in LIVE_INSTRUCTIONS and "say nothing or at most one word" in LIVE_INSTRUCTIONS
    assert "applies for the rest of the call" in LIVE_INSTRUCTIONS


@pytest.mark.parametrize("text,expected", [
    ("Mm-hmm.", True), ("Yeah.", True), ("Uh-huh", True), ("Okay, got it.", True), ("Right.", True),
    ("Sure, I'll fix that now.", False), ("Yeah, the rail icons are 20 pixels.", False), ("", False),
])
def test_backchannel_detection(text, expected):
    assert voice_stream.is_backchannel(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("", True), (" ", True), ("Hmm.", True), ("uh, um", True), ("Yeah.", False), ("uh, the rail", False),
])
def test_noise_detection(text, expected):
    assert voice_stream.is_noise(text) is expected


def recorder(monkeypatch):
    from app.core import turn_recorder
    capture = AsyncMock()
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", capture)
    return capture


@pytest.mark.asyncio
async def test_listening_sound_mid_sentence_keeps_one_request(monkeypatch):
    """Owner's call: "starting with the" / "Yeah." / "first one..." arrived as two requests."""
    capture = recorder(monkeypatch)
    call = stream()
    await call.caption("user", "Okay. So, uh, starting with the")
    await call.caption("assistant", "Yeah.")
    await call.caption("user", "first one, the computer use tool needs work.")
    await call.caption("assistant", "On it.")
    await call.flush()
    assert [(a.args[1], a.args[2]) for a in capture.call_args_list] == [
        ("Okay. So, uh, starting with the first one, the computer use tool needs work.", "On it.")]
    # The handoff context sees one user line, not the listening sound.
    assert [(e["role"], e["text"]) for e in call.transcript] == [
        ("user", "Okay. So, uh, starting with the"), ("user", " first one, the computer use tool needs work."),
        ("assistant", "On it.")]
    captions = [c.args[0] for c in call.websocket.send_json.await_args_list]
    # The caller's words continue the same caption; the next reply starts a new one.
    assert [c["id"] for c in captions] == ["user:0", "assistant:0", "user:0", "assistant:1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first,reply", [
    ("Can you hear me?", "Yeah."),  # an answer to a question, not a listening sound
    ("Make the icons bigger", "Sure, I'll ask the agent to resize them."),  # a real reply
])
async def test_answers_and_real_replies_still_separate_exchanges(monkeypatch, first, reply):
    capture = recorder(monkeypatch)
    call = stream()
    await call.caption("user", first)
    await call.caption("assistant", reply)
    await call.caption("user", "Next thing.")
    await call.flush()
    assert [(a.args[1], a.args[2]) for a in capture.call_args_list] == [(first, reply), ("Next thing.", None)]


@pytest.mark.asyncio
async def test_noise_only_speech_is_not_stored_or_dispatched(monkeypatch):
    capture = recorder(monkeypatch)
    delegate = AsyncMock()
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    monkeypatch.setattr(voice_stream, "SETTLE_QUIET_S", 0)
    call = stream()
    call.upstream = Upstream([json.dumps(e) for e in [
        {"type": "session.input_transcript.delta", "delta": "Hmm.", "end_ms": 500},
        {"type": "session.delegation.created", "delegation": {"id": "noise", "offset_ms": 600}},
    ]])
    await call.receive_upstream()
    await call.delegation_task
    delegate.assert_not_awaited()
    # Quiet context for the voice model, not a spoken "please repeat".
    assert json.loads(call.upstream.sent[0]) == {
        "type": "session.thinking.append", "delegation_id": "noise", "content": voice_stream.NO_REQUEST_NOTE}
    await call.flush()
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_waits_for_the_rest_of_the_sentence_and_merges_repeat_handoffs(monkeypatch):
    monkeypatch.setattr(voice_stream, "SETTLE_QUIET_S", 0.05)
    recorder(monkeypatch)
    delegate = AsyncMock(return_value="Resized.")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    call = stream()
    call.upstream = Upstream([json.dumps(e) for e in [
        {"type": "session.input_transcript.delta", "delta": "Make the rail icons the same size as the", "end_ms": 900},
        {"type": "session.delegation.created", "delegation": {"id": "d1", "offset_ms": 1000}},
        {"type": "session.delegation.created", "delegation": {"id": "d2", "offset_ms": 1100}},
    ]])
    await call.receive_upstream()
    # The last words' transcript lands after Live opened the handoff.
    await call.caption("user", " other icons.")
    await call.delegation_task
    delegate.assert_awaited_once()
    assert "user: Make the rail icons the same size as the other icons.\n" in delegate.await_args.args[1]
    sent = [json.loads(e) for e in call.upstream.sent]
    assert sent[0] == {"type": "session.thinking.append", "delegation_id": "d2", "content": voice_stream.MERGED_REQUEST_NOTE}
    assert sent[1] == {"type": "session.commentary.append", "delegation_id": "d1", "content": "Resized."}
    assert call.settling is None


@pytest.mark.asyncio
async def test_handoff_settling_is_bounded_while_the_caller_keeps_talking(monkeypatch):
    monkeypatch.setattr(voice_stream, "SETTLE_QUIET_S", 10)
    monkeypatch.setattr(voice_stream, "SETTLE_MAX_S", 0.05)
    call = stream()
    call.last_user_caption_at = time.monotonic()
    started = time.monotonic()
    await call.settle_speech()
    assert time.monotonic() - started < 1


def test_checkpoint_holds_unanswered_speech_through_a_pause(monkeypatch):
    call = stream()
    call.pending["user"] = "So the first thing is"
    call.last_caption_at = time.monotonic() - 5
    assert not call.checkpoint_due()  # a pause to think, not a finished request
    call.last_caption_at = time.monotonic() - voice_stream.HOLD_UNANSWERED_S
    assert call.checkpoint_due()
    call.pending["assistant"] = "On it."
    call.last_caption_at = time.monotonic() - 2
    assert call.checkpoint_due()

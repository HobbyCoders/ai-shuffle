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
    assert submit.await_args.kwargs["payload"] == {"user_message_already_recorded": True, "voice_delegation": True}
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


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [True, False])
async def test_handoff_mid_turn_is_steered_into_the_running_turn(monkeypatch, caplog, live):
    """#1990: a spoken follow-up while a chat turn runs used to be submitted
    with only a scheduler kick, so it waited out (or was discarded with) that
    turn. It is now handed to the live turn, as a typed message is; with no
    live turn the kick-only path is unchanged."""
    from app.core import session_runtime, turn_manager
    monkeypatch.setattr(voice_stream.database, "get_session", lambda _: {"model": "haiku", "profile_id": "p"})
    calls = []
    async def submit(*args, **kwargs):
        calls.append("submit")
        return "turn-2"
    async def notify(turn_id):
        calls.append(("submitted", turn_id))
    monkeypatch.setattr(turn_manager, "submit_async", submit)
    runtime = SimpleNamespace(steer_pending=AsyncMock(return_value=1))
    monkeypatch.setattr(session_runtime, "_live_runtimes", {"s1": runtime} if live else {})
    real = session_runtime.try_steer_pending
    async def steer(session_id):
        calls.append("steer")
        return await real(session_id)
    spy = AsyncMock(side_effect=steer)
    monkeypatch.setattr(session_runtime, "try_steer_pending", spy)
    with caplog.at_level(logging.INFO, logger="app.core.voice_stream"):
        assert await voice_stream.delegate_to_selected_model("s1", "secret words", on_submitted=notify) == ""
    spy.assert_awaited_once_with("s1")
    # The steer is tried after the row exists and before anyone waits on it.
    assert calls == ["submit", "steer", ("submitted", "turn-2")]
    assert runtime.steer_pending.await_count == (1 if live else 0)
    steered = [r.getMessage() for r in caplog.records if "steered" in r.getMessage()]
    assert len(steered) == (1 if live else 0)
    assert all("turn-2" in m and "s1" in m and "secret words" not in m for m in steered)


@pytest.mark.asyncio
async def test_a_failing_steer_still_leaves_the_queued_handoff(monkeypatch):
    from app.core import session_runtime, turn_manager
    monkeypatch.setattr(voice_stream.database, "get_session", lambda _: {"model": "haiku", "profile_id": "p"})
    monkeypatch.setattr(turn_manager, "submit_async", AsyncMock(return_value="turn"))
    monkeypatch.setattr(session_runtime, "try_steer_pending", AsyncMock(side_effect=RuntimeError("boom")))
    notify = AsyncMock()
    assert await voice_stream.delegate_to_selected_model("s1", "test", on_submitted=notify) == ""
    notify.assert_awaited_once_with("turn")


@pytest.mark.asyncio
async def test_a_steered_handoff_never_reports_a_result_of_its_own(monkeypatch):
    """A steered row settles completed/steered without running, so it has no
    reply; the caller must not hear "finished" for work still running."""
    from app.core import session_runtime, turn_manager
    monkeypatch.setattr(voice_stream.database, "get_session", lambda _: {"model": "haiku", "profile_id": "p"})
    monkeypatch.setattr(turn_manager, "submit_async", AsyncMock(return_value="turn"))
    monkeypatch.setattr(session_runtime, "try_steer_pending", AsyncMock(return_value=1))
    monkeypatch.setattr(turn_manager, "wait_settled",
                        AsyncMock(return_value={"state": "completed", "stop_reason": "steered"}))
    monkeypatch.setattr(voice_stream, "_turn_reply", lambda *a: "")
    assert await voice_stream.delegate_to_selected_model("s1", "test") == voice_stream.STEERED_NOTE


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


# ---------------------------------------------------------------------------
# #1798: one spoken reply is one stored message, in spoken order
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_handoff_stores_the_request_and_keeps_the_reply_being_spoken_whole(monkeypatch):
    """The handoff flush cut "Sure," off the reply the voice was speaking."""
    capture = recorder(monkeypatch)
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", AsyncMock(return_value=""))
    call = stream()
    call.upstream = Upstream([])
    await call.caption("user", "Take a look at the two images.")
    await call.caption("assistant", "Sure,")
    await call.run_delegation("d1", list(call.transcript))
    # The reply arrives in several transcript segments after the handoff.
    await call.caption("assistant", " I- I'll take a")
    await call.caption("assistant", " look when they come in.")
    await call.caption("user", "Thanks.")
    await call.flush()
    assert [(a.args[1], a.args[2]) for a in capture.call_args_list] == [
        ("Take a look at the two images.", None),
        (None, "Sure, I- I'll take a look when they come in."),
        ("Thanks.", None),
    ]


def test_checkpoint_holds_a_reply_that_stopped_mid_sentence(monkeypatch):
    call = stream()
    call.pending["assistant"] = "Sure, I- I'll take a"
    call.last_caption_at = time.monotonic() - 8
    assert not call.checkpoint_due()  # the transcript stalled inside the reply
    call.last_caption_at = time.monotonic() - voice_stream.HOLD_MID_REPLY_S
    assert call.checkpoint_due()
    for finished in ("Thanks, that's approved.", "Do you authorize it?", "Done!”"):
        call.pending["assistant"] = finished
        call.last_caption_at = time.monotonic() - 2
        assert call.checkpoint_due()


@pytest.mark.asyncio
async def test_typed_text_in_a_call_is_stored_as_typed_not_spoken(monkeypatch):
    capture = recorder(monkeypatch)
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", AsyncMock(return_value="Seen."))
    call = stream()
    call.upstream = Upstream([])
    await call.caption("user", "I'll attach them.")
    await call.caption("assistant", "Okay.")
    await call.receive_text(json.dumps({"type": "input_text", "id": "t1", "text": "@WIP_2.png @WIP_1.png"}))
    await call.delegation_task
    await call.caption("user", "Did they arrive?")
    await call.flush()
    stored = [(a.args[1], a.args[2], a.kwargs["user_source"]) for a in capture.call_args_list]
    assert stored[0] == ("I'll attach them.", "Okay.", "voice")
    assert stored[1][0].endswith("@WIP_2.png @WIP_1.png") and stored[1][2] == "typed"
    assert stored[2] == ("Did they arrive?", None, "voice")


@pytest.mark.asyncio
async def test_a_reply_in_several_segments_is_stored_whole_in_spoken_order(monkeypatch):
    """#1798: the next utterance's transcript starts without a space, which glued
    "...site." to "I've..." in the stored message."""
    from app.core import turn_recorder
    record = AsyncMock()
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", record)
    call = stream()
    await call.caption("user", "Can you check the site")
    for delta in ("That mockup", " was a sketch.", "I've got the preview", " running.", " Version 3.", "5 is live."):
        await call.caption("assistant", delta)
    await call.flush()
    record.assert_awaited_once()
    assert record.await_args.args[1:] == (
        "Can you check the site",
        "That mockup was a sketch. I've got the preview running. Version 3.5 is live.",
    )
    shown = "".join(c.args[0]["delta"] for c in call.websocket.send_json.await_args_list
                    if c.args[0]["role"] == "assistant")
    assert shown == record.await_args.args[2]  # The live caption matches what is stored.


def stored(capture):
    """What record_voice_exchange keeps: it strips each side."""
    return [tuple(t.strip() if t else t for t in (a.args[1], a.args[2])) for a in capture.call_args_list]


def live_event(role, delta, start_ms):
    kind = "session.input_transcript.delta" if role == "user" else "session.output_transcript.delta"
    return json.dumps({"type": kind, "delta": delta, "start_ms": start_ms, "end_ms": start_ms + 400})


@pytest.mark.asyncio
async def test_a_late_transcribed_answer_is_stored_before_the_reply_to_it(monkeypatch):
    """#1798: "Do you authorize…? Thanks, that's approved." was one message and
    the caller's "Yes." came after it: GPT Live transcribes the caller late."""
    capture = recorder(monkeypatch)
    call = stream()
    call.upstream = Upstream([
        live_event("assistant", "Do you authorize me to run grep?", 1000),
        live_event("assistant", "Thanks, that's approved.", 6000),
        live_event("user", "Yes.", 4500),
    ])
    await call.receive_upstream()
    await call.flush()
    assert stored(capture) == [(None, "Do you authorize me to run grep?"), ("Yes.", "Thanks, that's approved.")]


@pytest.mark.asyncio
async def test_a_reply_split_by_a_late_answer_is_stored_whole_after_it(monkeypatch):
    """#1798: stored as "Thanks," then "that's approved. And do you authorize…?"."""
    capture = recorder(monkeypatch)
    call = stream()
    call.upstream = Upstream([
        live_event("assistant", "Do you authorize me to run grep?", 1000),
        live_event("assistant", "Thanks,", 6000),
        live_event("user", "Yes.", 4500),
        live_event("assistant", " that's approved. And do you authorize me to read the file?", 6400),
    ])
    await call.receive_upstream()
    await call.flush()
    assert stored(capture) == [
        (None, "Do you authorize me to run grep?"),
        ("Yes.", "Thanks, that's approved. And do you authorize me to read the file?"),
    ]


@pytest.mark.asyncio
async def test_the_late_end_of_a_request_joins_the_request(monkeypatch):
    capture = recorder(monkeypatch)
    call = stream()
    await call.caption("user", "Can you check", start_ms=1000)
    await call.caption("assistant", "Sure, checking now.", start_ms=3000)
    await call.caption("user", " the logs?", start_ms=1800)  # transcribed after the reply began
    await call.flush()
    assert stored(capture) == [("Can you check the logs?", "Sure, checking now.")]


@pytest.mark.asyncio
async def test_speech_after_the_whole_reply_still_starts_a_new_exchange(monkeypatch):
    capture = recorder(monkeypatch)
    call = stream()
    await call.caption("user", "What time is it?", start_ms=0)
    await call.caption("assistant", "It's noon.", start_ms=2000)
    await call.caption("assistant", " Anything else?", start_ms=2600)
    await call.caption("user", "No, thanks.", start_ms=5000)  # barge-in or next turn
    await call.flush()
    assert stored(capture) == [("What time is it?", "It's noon. Anything else?"), ("No, thanks.", None)]


@pytest.mark.asyncio
async def test_captions_without_timing_keep_arrival_order(monkeypatch):
    capture = recorder(monkeypatch)
    call = stream()
    await call.caption("assistant", "Do you authorize it?")  # e.g. the fal.ai path
    await call.caption("assistant", " Thanks.", start_ms=6000)
    await call.caption("user", "Yes.", start_ms=4500)
    await call.flush()
    assert stored(capture) == [(None, "Do you authorize it? Thanks."), ("Yes.", None)]


@pytest.mark.asyncio
async def test_client_call_figures_are_kept_for_the_log_only():
    call = stream()
    stats = {"underruns": 3, "depth_ms": 140.25, "muted": False, "context": "running",
             "track": "ended", "Bad Key": 1, "note": "x" * 40, "nested": {"a": 1}, "nan": float("inf")}
    await call.receive_text(json.dumps({"type": "client_stats", "stats": stats}))
    assert call.client_stats == {"underruns": 3, "depth_ms": 140.2, "muted": False,
                                 "context": "running", "track": "ended"}
    call.websocket.send_json.assert_not_awaited()
    with pytest.raises(ValueError, match="Only PCM"):
        await call.receive_text(json.dumps({"type": "client_stats", "stats": {}, "text": "x"}))


@pytest.mark.asyncio
async def test_microphone_and_playback_figures_are_counted_for_the_health_log():
    call = stream()
    call.upstream = Upstream([])
    silence, speech = bytes(3840), b"\x10\x00" * 1920
    call.websocket.receive = AsyncMock(side_effect=[
        {"type": "websocket.receive", "bytes": silence}, {"type": "websocket.receive", "bytes": speech},
        {"type": "websocket.disconnect"}])
    await call.receive_browser()
    await call.send_audio(b"\x00\x01" * 2400)
    assert (call.mic_frames, call.mic_bytes, call.out_frames) == (2, 7680, 1)
    assert call.mic_voiced_s == pytest.approx(0.08)
    call.client_stats = {"underruns": 2}
    line = call.health_line()
    assert "mic 2 frames 7 KB" in line and "to browser 1 frames" in line and "client underruns=2" in line


def test_speech_counts_toward_deafness_only_while_the_voice_is_quiet(monkeypatch):
    monkeypatch.setattr(voice_stream, "PROVIDER_DEAF_VOICED_S", 1.0)
    call = stream()
    speech = b"\x10\x00" * 24_000  # 1 s
    call.count_mic(speech)
    assert not call.deaf.is_set()  # Nothing heard yet this session: maybe only noise.
    call.heard_this_session = True
    call.last_out_at = time.monotonic()  # The voice is talking; its echo is not the caller.
    call.count_mic(speech)
    assert not call.deaf.is_set()
    call.last_out_at = time.monotonic() - 5
    call.count_mic(bytes(48_000))  # Gated silence never counts.
    assert not call.deaf.is_set()
    call.count_mic(speech)
    assert call.deaf.is_set()


@pytest.mark.asyncio
async def test_health_tells_the_browser_its_microphone_age_and_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(voice_stream, "HEALTH_PUSH_S", 0.01)
    monkeypatch.setattr(voice_stream, "MIC_SILENT_WARN_S", 0.0)
    call = stream()
    call.client_stats = {"muted": False}
    import asyncio
    with caplog.at_level(logging.WARNING, logger="app.core.voice_stream"):
        task = asyncio.create_task(call.health())
        await asyncio.sleep(0.08)
        task.cancel()
    health = [c.args[0] for c in call.websocket.send_json.await_args_list if c.args[0]["type"] == "health"]
    assert health and health[0] == {"type": "health", "mic_age_ms": None, "mic_frames": 0}
    assert len([r for r in caplog.records if "no microphone audio" in r.message]) == 1

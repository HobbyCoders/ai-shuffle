"""Protocol tests for server-owned Live delegation and fal.ai audio frames."""
import base64
import json
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

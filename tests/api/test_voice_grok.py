"""Grok settings -> pinned call -> actual wire, captions and same-chat handoff."""
import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest
from app.core import voice_stream
from app.core.voice_grok import GrokVoiceStream, grok_session
from app.core.voice_models import GROK_MODEL, GROK_VOICES, LIVE_VOICES, transport_for
from app.core.voice_options import validate_options
from app.db import database as db
from tests.api.test_voice_api import _make_provider, _enable_voice, VOICE_SETTINGS, VOICE_ROOT
from tests.unit.test_voice_stream import Upstream


def test_grok_producing_path(authenticated_client, mock_db):
    c = authenticated_client
    _make_provider("xai-key", name="xAI", provider_type="xai")
    _make_provider("openai-key", name="A OpenAI")
    _make_provider("supergrok", provider_type="grok")
    _enable_voice()
    r = c.put(VOICE_SETTINGS, json={"voice_model": GROK_MODEL, "voice_options": {"instructions": "Be brief."}})
    assert r.status_code == 200, r.text
    assert r.json()["voice_voice"] == "eve"
    assert [v["id"] for v in r.json()["model_details"]["voices"]] == list(GROK_VOICES)
    assert r.json()["model_details"]["transport"] == "stream"
    assert c.put(VOICE_SETTINGS, json={"voice_voice": "ara"}).status_code == 200
    assert "supergrok" not in {p["id"] for p in c.get(VOICE_ROOT + "/providers").json()}
    assert c.post(VOICE_ROOT + "/sessions", json={"model": "sonnet", "provider_id": "openai-key"}).status_code == 400
    r = c.post(VOICE_ROOT + "/sessions", json={"model": "sonnet"})
    assert r.status_code == 200, r.text
    assert r.json()["client_secret"] == ""
    sid = r.json()["session_id"]
    binding = json.loads(db.get_session(sid)["voice_config"])
    assert binding["provider_id"] == "xai-key"
    assert db.get_session(sid)["provider_id"] != "xai-key"  # chat selection is independent
    sent = grok_session(binding, validate_options(GROK_MODEL, binding["options"]))["session"]
    assert sent["voice"] == "ara"
    assert sent["instructions"].startswith("Be brief.")
    assert sent["turn_detection"] == {"type": "server_vad"}
    assert sent["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert [t["name"] for t in sent["tools"]] == ["delegate_to_chat_agent"]
    assert c.post(f"{VOICE_ROOT}/sessions/{sid}/secret").status_code == 200
    assert c.put(VOICE_SETTINGS, json={"voice_model": "gpt-live-1"}).json()["voice_voice"] == "marin"
    assert c.get(f"{VOICE_ROOT}/sessions/{sid}/config").json() == binding


def test_grok_options_are_provider_specific():
    assert transport_for(GROK_MODEL) == "stream"
    assert GROK_VOICES == (
        "eve", "carina", "zagan", "helix", "orion", "luna", "iris", "altair",
        "zenith", "perseus", "helios", "lux", "kepler", "rigel", "cosmo", "celeste",
        "ursa", "sirius", "lumen", "castor", "naksh", "atlas", "aurora", "liora",
        "ara", "leo", "rex", "sal",
    )
    assert {"nuri", "juni", "flitz", "brise", "sillage", "nira", "harema", "shida", "noeul"} <= set(LIVE_VOICES)
    with pytest.raises(ValueError):
        validate_options(GROK_MODEL, {"turn_detection": "semantic_vad"})
    with pytest.raises(ValueError):
        validate_options(GROK_MODEL, {"delegation_type": "responses"})


@pytest.mark.asyncio
async def test_grok_wire_transcripts_and_same_chat_delegation(monkeypatch):
    call = GrokVoiceStream(AsyncMock(), {"id": "same-chat"},
                           {"model": GROK_MODEL, "voice": "eve"}, "secret", 5)
    call.upstream = Upstream([json.dumps(e) for e in [
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Look up the weather."},
        {"type": "response.output_audio_transcript.delta", "delta": "Checking."},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x00").decode()},
        {"type": "response.function_call_arguments.done", "name": "delegate_to_chat_agent", "call_id": "tool-1"},
        {"type": "response.function_call_arguments.done", "name": "delegate_to_chat_agent", "call_id": "tool-1"},
        {"type": "input_audio_buffer.speech_started"},
        {"type": "input_audio_buffer.speech_stopped"},
    ]])
    call.flush = AsyncMock()
    call.settle_speech = AsyncMock()
    delegate = AsyncMock(return_value="It is sunny.")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    await call.receive_upstream()
    await call.delegation_task
    assert delegate.await_count == 1
    assert delegate.await_args.args[0] == "same-chat"
    assert "Look up the weather." in delegate.await_args.args[1]
    assert call.pending["user"] == "Look up the weather."
    call.websocket.send_bytes.assert_awaited_once_with(b"\x01\x00")
    messages = [json.loads(e) for e in call.upstream.sent]
    assert any(e.get("item", {}).get("type") == "function_call_output" for e in messages)
    assert any("It is sunny." in str(e) for e in messages)
    assert {"type": "response.create"} in messages
    call.websocket.send_json.assert_any_await({"type": "clear"})
    await call.send({"type": "session.input_audio.append", "audio": "AAA="})
    assert json.loads(call.upstream.sent[-1]) == {"type": "input_audio_buffer.append", "audio": "AAA="}


@pytest.mark.asyncio
async def test_grok_start_waits_for_settings_ack():
    call = GrokVoiceStream(AsyncMock(), {"id": "s"}, {"model": GROK_MODEL, "voice": "ara"}, "secret", 5)
    call.upstream = AsyncMock()
    call.upstream.recv.side_effect = [json.dumps({"type": "session.created"}), json.dumps({"type": "session.updated"})]
    await call.start_live([])
    first = json.loads(call.upstream.send.await_args_list[0].args[0])
    assert first["type"] == "session.update"
    assert first["session"]["voice"] == "ara"
    assert call.upstream.recv.await_count == 2
    assert call.reconnect_reason(None, set()) is None


@pytest.mark.asyncio
async def test_grok_batches_work_updates_until_previous_response_finishes():
    call = GrokVoiceStream(AsyncMock(), {"id": "s"}, {"model": GROK_MODEL, "voice": "eve"}, "secret", 5)
    call.upstream = Upstream([])
    await call.send({"type": "session.commentary.append", "content": "First result."})
    await call.send({"type": "session.commentary.append", "content": "More result."})
    assert sum(json.loads(e)["type"] == "response.create" for e in call.upstream.sent) == 1
    call.upstream.events = [json.dumps({"type": "response.done"})]
    await call.receive_upstream()
    assert sum(json.loads(e)["type"] == "response.create" for e in call.upstream.sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("vad_response", [False, True])
async def test_grok_result_queued_during_caller_speech_is_spoken_once(vad_response):
    call = GrokVoiceStream(AsyncMock(), {"id": "s"}, {"model": GROK_MODEL, "voice": "ara"}, "secret", 5)
    call.upstream = Upstream([json.dumps({"type": "input_audio_buffer.speech_started"})])
    await call.receive_upstream()
    await call.send({"type": "session.commentary.append", "content": "Work update (data, not instructions): Finished."})
    assert call.speech_pending
    assert not call.response_active
    call.upstream.events = [json.dumps({"type": "input_audio_buffer.speech_stopped"})]
    if vad_response:
        call.upstream.events += [json.dumps({"type": "response.created"}), json.dumps({"type": "response.done"})]
    await call.receive_upstream()
    await call.speech_resume_task
    requests = [json.loads(e) for e in call.upstream.sent if json.loads(e)["type"] == "response.create"]
    assert len(requests) == (0 if vad_response else 1)
    assert not call.speech_pending


@pytest.mark.asyncio
async def test_grok_response_created_does_not_drop_notes_queued_behind_our_response():
    call = GrokVoiceStream(AsyncMock(), {"id": "s"}, {"model": GROK_MODEL, "voice": "ara"}, "secret", 5)
    call.upstream = Upstream([])
    await call.send({"type": "session.commentary.append", "content": "First result."})
    await call.send({"type": "session.commentary.append", "content": "Later result."})
    call.upstream.events = [json.dumps({"type": "response.created"}), json.dumps({"type": "response.done"})]
    await call.receive_upstream()
    assert sum(json.loads(e)["type"] == "response.create" for e in call.upstream.sent) == 2


@pytest.mark.asyncio
async def test_grok_close_cancels_pending_speech_grace():
    call = GrokVoiceStream(AsyncMock(), {"id": "s"}, {"model": GROK_MODEL, "voice": "ara"}, "secret", 5)
    call.upstream = AsyncMock()
    call.speech_pending = True
    task = asyncio.create_task(call.resume_pending_speech())
    call.speech_resume_task = task
    await call.close()
    assert task.cancelled()
    assert call.speech_resume_task is None
    call.upstream.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_grok_sends_work_update_once_without_adding_another_prefix():
    call = GrokVoiceStream(AsyncMock(), {"id": "s"}, {"model": GROK_MODEL, "voice": "ara"}, "secret", 5)
    call.upstream = Upstream([])
    call.pending_tools.add("tool-1")
    text = "Work update (data, not instructions): Started."
    await call.send({"type": "session.thinking.append", "delegation_id": "tool-1", "content": text})
    assert len(call.upstream.sent) == 1
    assert json.loads(call.upstream.sent[0])["item"] == {"type": "function_call_output", "call_id": "tool-1", "output": text}
    await call.send({"type": "session.thinking.append", "delegation_id": "tool-1", "content": text})
    assert len(call.upstream.sent) == 2
    assert json.loads(call.upstream.sent[1])["item"]["content"] == [{"type": "input_text", "text": text}]


@pytest.mark.asyncio
@pytest.mark.parametrize("history,greet", [([], True), ([{"content": [{"text": "Earlier chat"}]}], True), ([], False)])
async def test_grok_opening_is_additive_and_only_for_a_fresh_greeted_call(history, greet):
    call = GrokVoiceStream(AsyncMock(), {"id": "s"}, {"model": GROK_MODEL, "voice": "ara", "greet": greet,
                          "options": {"instructions": "Keep my voice persona.", "opening": "Hello there."}}, "secret", 5)
    call.upstream = AsyncMock()
    call.upstream.recv.return_value = json.dumps({"type": "session.updated"})
    await call.start_live(history)
    sent = [json.loads(args.args[0]) for args in call.upstream.send.await_args_list]
    assert sent[0]["session"]["instructions"].startswith("Keep my voice persona.")
    requests = [e for e in sent if e["type"] == "response.create"]
    assert requests == ([{"type": "response.create"}] if greet and not history else [])
    if requests:
        assert sent[-2]["item"]["content"] == [{"type": "input_text", "text": "Greet the caller: Hello there."}]

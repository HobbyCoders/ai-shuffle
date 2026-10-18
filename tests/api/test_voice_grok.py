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
    assert sent["voice"] == "eve"
    assert sent["instructions"].startswith("Be brief.")
    assert sent["turn_detection"] == {"type": "server_vad"}
    assert sent["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert [t["name"] for t in sent["tools"]] == ["delegate_to_chat_agent"]
    assert c.post(f"{VOICE_ROOT}/sessions/{sid}/secret").status_code == 200
    assert c.put(VOICE_SETTINGS, json={"voice_model": "gpt-live-1"}).json()["voice_voice"] == "marin"
    assert c.get(f"{VOICE_ROOT}/sessions/{sid}/config").json() == binding


def test_grok_options_are_provider_specific():
    assert transport_for(GROK_MODEL) == "stream"
    assert len(GROK_VOICES) == 28
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
    call = GrokVoiceStream(AsyncMock(), {"id": "s"}, {"model": GROK_MODEL, "voice": "eve"}, "secret", 5)
    call.upstream = AsyncMock()
    call.upstream.recv.side_effect = [json.dumps({"type": "session.created"}), json.dumps({"type": "session.updated"})]
    await call.start_live([])
    first = json.loads(call.upstream.send.await_args_list[0].args[0])
    assert first["type"] == "session.update"
    assert first["session"]["voice"] == "eve"
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

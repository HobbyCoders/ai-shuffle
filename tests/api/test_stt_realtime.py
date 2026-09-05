"""Tests for the realtime (streaming) speech-to-text proxy."""

import json

import pytest

from app.api import stt_realtime
from app.api.stt_realtime import (
    REALTIME_STT_MODELS,
    SAMPLE_RATE,
    _resolve_capability,
    _session_update_ga,
    _session_update_legacy,
)


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------


def test_realtime_models_are_openai_stt_models():
    """Every streamable id must also exist on the batch STT registry, so a model
    the user picks in Settings resolves the same way on both paths."""
    from app.api.settings_audio import STT_MODELS

    for model_id, info in REALTIME_STT_MODELS.items():
        assert model_id in STT_MODELS
        assert STT_MODELS[model_id]["provider"] == "openai"
        assert info["name"] == model_id.split("/", 1)[1]


def test_only_gpt_transcribe_models_stream_partials():
    assert REALTIME_STT_MODELS["openai/gpt-4o-transcribe"]["partials"] is True
    assert REALTIME_STT_MODELS["openai/gpt-4o-mini-transcribe"]["partials"] is True
    # whisper-1 runs in a realtime session but answers per segment only.
    assert REALTIME_STT_MODELS["openai/whisper-1"]["partials"] is False


# ---------------------------------------------------------------------------
# Session config shapes
# ---------------------------------------------------------------------------


def test_ga_session_update_shape():
    payload = _session_update_ga("gpt-4o-transcribe", None)

    assert payload["type"] == "session.update"
    assert payload["session"]["type"] == "transcription"

    audio_input = payload["session"]["audio"]["input"]
    assert audio_input["format"] == {"type": "audio/pcm", "rate": SAMPLE_RATE}
    assert audio_input["transcription"] == {"model": "gpt-4o-transcribe"}
    assert audio_input["turn_detection"]["type"] == "server_vad"

    # Serialisable as-is — it goes straight onto the socket.
    json.dumps(payload)


def test_legacy_session_update_shape():
    payload = _session_update_legacy("whisper-1", "en")

    assert payload["type"] == "transcription_session.update"
    assert payload["session"]["input_audio_format"] == "pcm16"
    assert payload["session"]["input_audio_transcription"] == {
        "model": "whisper-1",
        "language": "en",
    }
    json.dumps(payload)


def test_language_is_optional_and_omitted_when_absent():
    without = _session_update_ga("gpt-4o-transcribe", None)
    assert "language" not in without["session"]["audio"]["input"]["transcription"]

    with_lang = _session_update_ga("gpt-4o-transcribe", "de")
    assert with_lang["session"]["audio"]["input"]["transcription"]["language"] == "de"


def test_live_model_uses_languages_and_low_delay():
    config = _session_update_ga("gpt-live-transcribe", "en")["session"]["audio"]["input"]["transcription"]
    assert config == {"model": "gpt-live-transcribe", "languages": ["en"], "delay": "low"}


def test_live_model_is_available_explicitly_without_changing_configured_model(capability_env):
    capability_env["model"] = "openai/gpt-live-transcribe"
    result = _resolve_capability()
    assert result["supported"] is True
    assert result["partials"] is True
    assert result["model"] == "openai/gpt-live-transcribe"


def test_turn_detection_is_copied_not_shared():
    """Each payload gets its own dict — a caller mutating one must not reach
    through to the module-level default and reconfigure every later session."""
    first = _session_update_ga("gpt-4o-transcribe", None)
    first["session"]["audio"]["input"]["turn_detection"]["threshold"] = 0.99

    second = _session_update_ga("gpt-4o-transcribe", None)
    assert second["session"]["audio"]["input"]["turn_detection"]["threshold"] == 0.5
    assert stt_realtime.TURN_DETECTION["threshold"] == 0.5


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------


@pytest.fixture
def capability_env(monkeypatch):
    """Drive `_resolve_capability` by setting the model and key it reads."""
    state = {"model": "openai/gpt-4o-transcribe", "key": "sk-test"}

    monkeypatch.setattr(
        stt_realtime.database,
        "get_system_setting",
        lambda name: state["model"] if name == "stt_model" else None,
    )
    from app.api import settings_audio

    monkeypatch.setattr(settings_audio, "_resolve_openai_api_key", lambda: state["key"])
    return state


def test_capability_supported_for_gpt_transcribe(capability_env):
    result = _resolve_capability()

    assert result["supported"] is True
    assert result["model"] == "openai/gpt-4o-transcribe"
    assert result["partials"] is True
    assert result["sample_rate"] == SAMPLE_RATE
    assert result["reason"] is None


def test_capability_reports_no_partials_for_whisper(capability_env):
    capability_env["model"] = "openai/whisper-1"

    result = _resolve_capability()

    assert result["supported"] is True
    assert result["partials"] is False


def test_capability_unsupported_for_fal_model(capability_env):
    capability_env["model"] = "fal-ai/whisper"

    result = _resolve_capability()

    assert result["supported"] is False
    assert result["reason"]
    # The message has to name the model, since the frontend shows it verbatim.
    assert "Whisper" in result["reason"]


def test_capability_defaults_to_fal_when_unset(capability_env):
    capability_env["model"] = None

    result = _resolve_capability()

    assert result["supported"] is False
    assert result["model"] == "fal-ai/whisper"


def test_capability_unsupported_without_openai_key(capability_env):
    capability_env["key"] = None

    result = _resolve_capability()

    assert result["supported"] is False
    assert "Providers" in result["reason"]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_routes_are_mounted_under_settings():
    from app.api.settings import router

    assert str(router.url_path_for("transcribe_stream")) == "/api/v1/settings/transcribe/stream"
    assert str(router.url_path_for("transcribe_stream_capability")) == "/api/v1/settings/transcribe/stream/capability"


def test_websocket_route_is_a_websocket_route():
    from app.api.settings import router

    # URL reversal traverses included routers and retains the route protocol.
    # This proves the endpoint is WebSocket even when routes are not flattened.
    url = router.url_path_for("transcribe_stream").make_absolute_url("http://testserver")
    assert str(url) == "ws://testserver/api/v1/settings/transcribe/stream"


# ---------------------------------------------------------------------------
# Compressed-upload allowlist (shared with the batch path)
# ---------------------------------------------------------------------------


def test_capability_reports_accepts_compressed(capability_env):
    """The batch path reads this off the same probe to decide whether it must
    re-encode to WAV before uploading."""
    result = _resolve_capability()

    assert result["accepts_compressed"] is True


def test_capability_reports_accepts_compressed_for_fal_whisper(capability_env):
    # Unsupported for streaming, but it still accepts the browser container —
    # the two questions are independent and both ride on this one probe.
    capability_env["model"] = "fal-ai/whisper"

    result = _resolve_capability()

    assert result["supported"] is False
    assert result["accepts_compressed"] is True


def test_elevenlabs_models_are_not_allowlisted():
    """Scribe rejecting WebM is the reason the WAV conversion exists; if it ever
    lands on the allowlist, voice input breaks for anyone using it."""
    from app.api.settings_audio import STT_MODELS

    for model_id in (
        "fal-ai/elevenlabs/speech-to-text",
        "fal-ai/elevenlabs/speech-to-text/scribe-v2",
        # Unverified, so deliberately absent: absent means "convert", which is safe.
        "fal-ai/speech-to-text",
    ):
        assert not STT_MODELS[model_id].get("accepts_compressed")


def test_every_allowlisted_model_is_a_known_stt_model():
    from app.api.settings_audio import STT_MODELS

    allowlisted = [
        model_id for model_id, info in STT_MODELS.items()
        if info.get("accepts_compressed")
    ]
    assert allowlisted
    assert set(allowlisted) <= set(STT_MODELS)


# ---------------------------------------------------------------------------
# The proxy pumps, against a fake upstream
# ---------------------------------------------------------------------------


class _FakeUpstream:
    """Stands in for OpenAI's realtime socket.

    Models the property that made the drain bug possible: the socket stays open
    after a commit, so the read loop never ends on its own. Anything that waits
    for the iterator to finish will wait out the full ceiling.
    """

    def __init__(self, transcript="hello world", answer_commit=True, audio_events=(), delayed_tail=False):
        import asyncio

        self.sent = []
        self.transcript = transcript
        self.answer_commit = answer_commit
        self.audio_events = audio_events
        self.delayed_tail = delayed_tail
        self._queue = asyncio.Queue()
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, data):
        self.sent.append(data)
        message = json.loads(data)
        if message.get("type") == "input_audio_buffer.append":
            for event in self.audio_events:
                await self._queue.put(json.dumps(event))
        if message.get("type") == "input_audio_buffer.commit" and self.answer_commit:
            if self.delayed_tail:
                for item_id, previous in (("item_1", None), ("item_2", "item_1")):
                    await self._queue.put(json.dumps({"type": "input_audio_buffer.committed", "item_id": item_id, "previous_item_id": previous}))
            await self._queue.put(
                json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "item_1",
                        "transcript": self.transcript,
                    }
                )
            )
            if self.delayed_tail:
                import asyncio

                async def send_tail():
                    await asyncio.sleep(0.5)
                    await self._queue.put(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "item_id": "item_2", "transcript": "last words"}))

                asyncio.create_task(send_tail())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._queue.get()


@pytest.fixture
def stream_client(monkeypatch, capability_env):
    """A TestClient whose realtime WebSocket talks to a _FakeUpstream."""
    from fastapi.testclient import TestClient

    import websockets.asyncio.client as ws_client

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(stt_realtime.stt_stream_router, prefix="/api/v1/settings")

    created = {}

    async def fake_connect(url, **kwargs):
        upstream = _FakeUpstream(**created.get("kwargs", {}))
        created["upstream"] = upstream
        created["url"] = url
        return upstream

    monkeypatch.setattr(ws_client, "connect", fake_connect)

    async def always_authenticated(websocket, token):
        return (True, None)

    monkeypatch.setattr(stt_realtime, "authenticate_websocket", always_authenticated)

    with TestClient(app) as client:
        yield client, created


def test_stream_forwards_tail_transcript_and_closes(stream_client):
    client, created = stream_client

    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["model"] == "openai/gpt-4o-transcribe"

        ws.send_bytes(b"\x00\x01" * 512)
        ws.send_text(json.dumps({"type": "stop"}))

        final = ws.receive_json()
        assert final["type"] == "final"
        assert final["text"] == "hello world"

        assert ws.receive_json()["type"] == "closed"

    upstream = created["upstream"]
    kinds = [json.loads(m)["type"] for m in upstream.sent]
    assert "session.update" in kinds
    assert "input_audio_buffer.append" in kinds
    # stop must flush, or the last utterance is never transcribed at all.
    assert "input_audio_buffer.commit" in kinds


def test_stream_forwards_interim_words_before_stop_with_turn_identity(stream_client):
    client, created = stream_client
    created["kwargs"] = {"audio_events": [
        {"type": "input_audio_buffer.speech_started", "item_id": "item_1"},
        {"type": "conversation.item.input_audio_transcription.delta", "item_id": "item_1", "delta": "hello"},
    ]}
    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\x00\x01" * 512)
        assert ws.receive_json() == {"type": "item_started", "item_id": "item_1"}
        assert ws.receive_json()["type"] == "speech_started"
        assert ws.receive_json() == {"type": "delta", "item_id": "item_1", "text": "hello"}
        ws.send_text(json.dumps({"type": "stop"}))
        assert ws.receive_json()["type"] == "final"
        assert ws.receive_json()["type"] == "closed"


def test_drain_waits_for_known_pending_turn_past_settle_window(stream_client):
    client, created = stream_client
    created["kwargs"] = {"delayed_tail": True}
    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_text(json.dumps({"type": "stop"}))
        assert ws.receive_json()["item_id"] == "item_1"
        assert ws.receive_json()["previous_item_id"] == "item_1"
        assert ws.receive_json()["text"] == "hello world"
        assert ws.receive_json()["text"] == "last words"
        assert ws.receive_json()["type"] == "closed"


def test_stream_stop_does_not_wait_out_the_drain_ceiling(stream_client):
    """Regression guard for the six-second stall.

    The drain used to wait on the upstream read loop, which cannot finish while
    OpenAI holds the socket open — so every stop cost DRAIN_TIMEOUT_SECONDS even
    though the transcript had already arrived. It must end on the transcript.
    """
    import time

    client, _created = stream_client

    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        assert ws.receive_json()["type"] == "ready"

        started = time.monotonic()
        ws.send_text(json.dumps({"type": "stop"}))
        assert ws.receive_json()["type"] == "final"
        assert ws.receive_json()["type"] == "closed"
        elapsed = time.monotonic() - started

    assert elapsed < stt_realtime.DRAIN_TIMEOUT_SECONDS / 2, (
        f"stop took {elapsed:.2f}s; the drain is waiting on the clock again"
    )


def test_stream_rejects_a_model_that_cannot_stream(stream_client, capability_env):
    """A fal model must be refused with the close code the client keys its batch
    fallback off, not left hanging."""
    from starlette.websockets import WebSocketDisconnect

    client, _created = stream_client
    capability_env["model"] = "fal-ai/whisper"

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
            message = ws.receive_json()
            assert message["type"] == "error"
            assert message["unsupported"] is True
            ws.receive_json()

    assert excinfo.value.code == stt_realtime.CLOSE_UNSUPPORTED

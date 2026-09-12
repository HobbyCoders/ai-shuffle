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


def test_live_model_disables_server_vad_for_explicit_commits():
    audio_input = _session_update_ga("gpt-live-transcribe", None)["session"]["audio"]["input"]
    assert audio_input["turn_detection"] is None


@pytest.mark.parametrize("model", ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"])
def test_existing_transcription_models_keep_server_vad(model):
    assert _session_update_ga(model, None)["session"]["audio"]["input"]["turn_detection"] == stt_realtime.TURN_DETECTION
    assert _session_update_legacy(model, None)["session"]["turn_detection"] == stt_realtime.TURN_DETECTION


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
        "get_or_create_system_setting",
        lambda name, default_factory: state["model"] or default_factory(),
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

    def __init__(
        self,
        transcript="hello world",
        answer_commit=True,
        audio_events=(),
        delayed_tail=False,
        commit_events=None,
        answer_clear=True,
        stop_error=None,
        configured=True,
        config_events=(),
    ):
        import asyncio

        self.sent = []
        self.transcript = transcript
        self.answer_commit = answer_commit
        self.audio_events = audio_events
        self.delayed_tail = delayed_tail
        self.commit_events = commit_events
        self.answer_clear = answer_clear
        self.stop_error = stop_error
        self.configured = configured
        self.config_events = config_events
        self._queue = asyncio.Queue()
        self._tasks = []
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        import asyncio

        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        return False

    async def send(self, data):
        self.sent.append(data)
        message = json.loads(data)
        if message.get("type") == "session.update" and self.configured:
            await self._queue.put(json.dumps({"type": "session.updated"}))
        if message.get("type") == "session.update":
            for event in self.config_events:
                await self._queue.put(json.dumps(event))
        if message.get("type") == "input_audio_buffer.clear" and self.answer_clear:
            await self._queue.put(json.dumps({"type": "input_audio_buffer.cleared"}))
        if message.get("type") == "input_audio_buffer.append":
            for event in self.audio_events:
                await self._queue.put(json.dumps(event))
        if message.get("type") == "input_audio_buffer.commit" and self.answer_commit:
            if self.stop_error is not None:
                await self._queue.put(json.dumps({"type": "error", "error": {
                    "code": self.stop_error,
                    "message": "Stop commit rejected",
                    "event_id": message.get("event_id"),
                }}))
                return
            if self.commit_events is not None:
                import asyncio

                async def send_events():
                    for delay, event in self.commit_events:
                        await asyncio.sleep(delay)
                        if event.get("type") == "error":
                            event["error"].setdefault("event_id", message.get("event_id"))
                        await self._queue.put(json.dumps(event))

                self._tasks.append(asyncio.create_task(send_events()))
                return
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

                self._tasks.append(asyncio.create_task(send_tail()))

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
        assert ready["server_vad"] is True

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


@pytest.mark.parametrize("control", ["commit", "stop"])
def test_live_model_streams_without_vad_and_commits_only_on_client_control(stream_client, capability_env, control):
    client, created = stream_client
    capability_env["model"] = "openai/gpt-live-transcribe"
    created["kwargs"] = {"audio_events": [
        {"type": "conversation.item.input_audio_transcription.delta", "item_id": "item_1", "delta": "hello"},
    ]}
    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        ready = ws.receive_json()
        assert ready["model"] == "openai/gpt-live-transcribe"
        assert ready["server_vad"] is False
        ws.send_bytes(b"\x00\x01" * 2400)
        # GPT Live sends partials without speech_started/speech_stopped events.
        assert ws.receive_json() == {"type": "delta", "item_id": "item_1", "text": "hello"}
        upstream = created["upstream"]
        sent = [json.loads(raw) for raw in upstream.sent]
        assert sent[0]["session"]["audio"]["input"]["turn_detection"] is None
        assert [frame["type"] for frame in sent] == ["session.update", "input_audio_buffer.append"]
        ws.send_json({"type": control})
        assert ws.receive_json() == {"type": "final", "item_id": "item_1", "text": "hello world"}
        if control == "commit":
            # The explicit commit keeps recording open; Stop then has no tail.
            upstream.stop_error = "input_audio_buffer_commit_empty"
            ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "closed"}
    frames = [json.loads(raw) for raw in upstream.sent]
    commits = [frame for frame in frames if frame["type"] == "input_audio_buffer.commit"]
    assert len(commits) == (2 if control == "commit" else 1)
    assert commits[-1]["event_id"] == "stt-stop-commit"
    if control == "commit":
        assert "event_id" not in commits[0]
    assert frames[-1] == {"type": "input_audio_buffer.clear"}


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



def test_stop_after_vad_final_closes_without_error_or_settle_timer(stream_client, monkeypatch):
    import time

    client, created = stream_client
    monkeypatch.setattr(stt_realtime, "DRAIN_TIMEOUT_SECONDS", 0.5)
    created["kwargs"] = {"audio_events": [
        {"type": "input_audio_buffer.committed", "item_id": "item_1"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "item_1", "transcript": "already finished"},
    ], "stop_error": "input_audio_buffer_commit_empty"}

    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\x00\x01" * 512)
        assert ws.receive_json()["type"] == "item_started"
        assert ws.receive_json()["text"] == "already finished"
        started = time.monotonic()
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["type"] == "closed"
        assert time.monotonic() - started < 0.2

    commands = [json.loads(message) for message in created["upstream"].sent]
    assert [message["type"] for message in commands][-2:] == [
        "input_audio_buffer.commit", "input_audio_buffer.clear",
    ]
    assert commands[-2]["event_id"]


def test_stop_waits_for_buffer_barrier_before_closing(stream_client, monkeypatch):
    client, created = stream_client
    monkeypatch.setattr(stt_realtime, "DRAIN_TIMEOUT_SECONDS", 1)
    created["kwargs"] = {"commit_events": [
        (0, {"type": "conversation.item.input_audio_transcription.completed", "item_id": "earlier", "transcript": "first"}),
        (0.4, {"type": "input_audio_buffer.committed", "item_id": "tail"}),
        (0, {"type": "input_audio_buffer.cleared"}),
        (0.1, {"type": "conversation.item.input_audio_transcription.completed", "item_id": "tail", "transcript": "last words"}),
    ], "answer_clear": False}
    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["text"] == "first"
        assert ws.receive_json()["item_id"] == "tail"
        assert ws.receive_json()["text"] == "last words"
        assert ws.receive_json()["type"] == "closed"


@pytest.mark.parametrize("pending_item", [False, True])
def test_stop_with_missing_upstream_events_keeps_bounded_fallback(stream_client, monkeypatch, pending_item):
    import time

    client, created = stream_client
    monkeypatch.setattr(stt_realtime, "DRAIN_TIMEOUT_SECONDS", 0.15)
    created["kwargs"] = {"answer_commit": False, "answer_clear": False}
    if pending_item:
        created["kwargs"] = {"commit_events": [
            (0, {"type": "input_audio_buffer.committed", "item_id": "pending"}),
            (0, {"type": "input_audio_buffer.cleared"}),
        ], "answer_clear": False}
    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        assert ws.receive_json()["type"] == "ready"
        started = time.monotonic()
        ws.send_json({"type": "stop"})
        if pending_item:
            assert ws.receive_json()["item_id"] == "pending"
        assert ws.receive_json()["type"] == "closed"
        assert 0.1 <= time.monotonic() - started < 1



@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_empty_stop_commit_waits_for_pending_transcription(stream_client, monkeypatch, terminal):
    import time

    client, created = stream_client
    monkeypatch.setattr(stt_realtime, "DRAIN_TIMEOUT_SECONDS", 1)
    created["kwargs"] = {"audio_events": [
        {"type": "input_audio_buffer.committed", "item_id": "pending"},
    ], "commit_events": [
        (0, {"type": "error", "error": {"code": "input_audio_buffer_commit_empty"}}),
        (0, {"type": "input_audio_buffer.cleared"}),
        (0.4, {"type": f"conversation.item.input_audio_transcription.{terminal}", "item_id": "pending", "transcript": "last words", "error": {"message": "Transcription failed"}}),
    ], "answer_clear": False}
    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\x00\x01" * 512)
        assert ws.receive_json()["item_id"] == "pending"
        ws.send_json({"type": "stop"})
        result = ws.receive_json()
        assert result["type"] == ("final" if terminal == "completed" else "error")
        assert result.get("text", result.get("message")) == ("last words" if terminal == "completed" else "Transcription failed")
        settled = time.monotonic()
        assert ws.receive_json()["type"] == "closed"
        assert time.monotonic() - settled < 0.2


def test_stop_waits_for_earlier_item_when_tail_finishes_first(stream_client, monkeypatch):
    client, created = stream_client
    monkeypatch.setattr(stt_realtime, "DRAIN_TIMEOUT_SECONDS", 1)
    created["kwargs"] = {"commit_events": [
        (0, {"type": "input_audio_buffer.committed", "item_id": "earlier"}),
        (0, {"type": "input_audio_buffer.committed", "item_id": "tail"}),
        (0, {"type": "input_audio_buffer.cleared"}),
        (0, {"type": "conversation.item.input_audio_transcription.completed", "item_id": "tail", "transcript": "last words"}),
        (0.4, {"type": "conversation.item.input_audio_transcription.completed", "item_id": "earlier", "transcript": "first words"}),
    ], "answer_clear": False}
    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["item_id"] == "earlier"
        assert ws.receive_json()["item_id"] == "tail"
        assert ws.receive_json()["text"] == "last words"
        assert ws.receive_json()["text"] == "first words"
        assert ws.receive_json()["type"] == "closed"


@pytest.mark.parametrize("code,event_id", [
    ("input_audio_buffer_commit_empty", "another-commit"),
    ("server_error", "stt-stop-commit"),
])
def test_stop_keeps_unrelated_upstream_errors_visible(stream_client, code, event_id):
    client, created = stream_client
    created["kwargs"] = {"commit_events": [
        (0, {"type": "error", "error": {"code": code, "event_id": event_id, "message": "Real error"}}),
        (0, {"type": "input_audio_buffer.cleared"}),
    ], "answer_clear": False}
    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "error", "message": "Real error", "fatal": False}
        assert ws.receive_json()["type"] == "closed"



def test_successful_stop_commit_waits_for_async_final(stream_client):
    client, created = stream_client
    created["kwargs"] = {"commit_events": [
        (0, {"type": "input_audio_buffer.committed", "item_id": "tail"}),
        (0, {"type": "input_audio_buffer.cleared"}),
        (0.1, {"type": "conversation.item.input_audio_transcription.completed", "item_id": "tail", "transcript": "last words"}),
    ], "answer_clear": False}
    with client.websocket_connect("/api/v1/settings/transcribe/stream") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "stop"})
        assert ws.receive_json()["item_id"] == "tail"
        assert ws.receive_json()["text"] == "last words"
        assert ws.receive_json()["type"] == "closed"


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


def test_live_model_rejection_never_announces_ready_or_retries_legacy(stream_client, capability_env):
    client, created = stream_client
    capability_env['model'] = 'openai/gpt-live-transcribe'
    created['kwargs'] = {'configured': False, 'config_events': [
        {'type': 'error', 'error': {'code': 'model_not_found', 'message': 'GPT Live Transcribe is unavailable'}},
    ]}
    with client.websocket_connect('/api/v1/settings/transcribe/stream') as ws:
        assert ws.receive_json() == {'type': 'error', 'message': 'GPT Live Transcribe is unavailable', 'fatal': True}
        assert ws.receive_json() == {'type': 'closed'}
    sent = [json.loads(raw) for raw in created['upstream'].sent]
    assert [frame['type'] for frame in sent] == ['session.update']
    assert sent[0]['session']['audio']['input']['transcription']['model'] == 'gpt-live-transcribe'


def test_live_model_configuration_timeout_is_terminal(stream_client, capability_env, monkeypatch):
    client, created = stream_client
    capability_env['model'] = 'openai/gpt-live-transcribe'
    created['kwargs'] = {'configured': False}
    monkeypatch.setattr(stt_realtime, 'SESSION_CONFIG_TIMEOUT', 0.01)
    with client.websocket_connect('/api/v1/settings/transcribe/stream') as ws:
        error = ws.receive_json()
        assert error['type'] == 'error' and error['fatal'] is True
        assert 'timed out' in error['message']
        assert ws.receive_json() == {'type': 'closed'}


def test_live_model_streams_partial_then_final_and_recovers_failed_turn(stream_client, capability_env):
    client, created = stream_client
    capability_env['model'] = 'openai/gpt-live-transcribe'
    created['kwargs'] = {'audio_events': [
        {'type': 'conversation.item.input_audio_transcription.delta', 'item_id': 'live', 'delta': 'Hello'},
        {'type': 'conversation.item.input_audio_transcription.failed', 'item_id': 'live', 'error': {'message': 'Try again'}},
        {'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'next', 'transcript': 'Recovered.'},
    ]}
    with client.websocket_connect('/api/v1/settings/transcribe/stream') as ws:
        assert ws.receive_json()['model'] == 'openai/gpt-live-transcribe'
        ws.send_bytes(b'\x00\x00' * 2400)
        assert ws.receive_json() == {'type': 'delta', 'item_id': 'live', 'text': 'Hello'}
        assert ws.receive_json() == {'type': 'error', 'message': 'Try again', 'fatal': False}
        assert ws.receive_json() == {'type': 'final', 'item_id': 'next', 'text': 'Recovered.'}
        ws.send_json({'type': 'stop'})
        assert ws.receive_json()['type'] == 'final'
        assert ws.receive_json()['type'] == 'closed'

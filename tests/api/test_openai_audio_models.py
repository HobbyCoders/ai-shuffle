"""Tests for OpenAI (GPT) TTS/STT model routing alongside fal."""

import pytest

from app.api import settings_audio
from app.api.settings_audio import (
    TTS_MODELS,
    STT_MODELS,
    _model_provider,
    _openai_model_name,
    _openai_voice,
    _openai_tts_bytes,
    _openai_transcribe,
    _resolve_openai_api_key,
)


# ---------------------------------------------------------------------------
# Registry + provider detection
# ---------------------------------------------------------------------------


def test_openai_tts_models_registered():
    for model_id in ("openai/gpt-4o-mini-tts", "openai/tts-1", "openai/tts-1-hd"):
        assert model_id in TTS_MODELS
        assert TTS_MODELS[model_id]["provider"] == "openai"
        assert TTS_MODELS[model_id]["voices"]


def test_openai_stt_models_registered():
    for model_id in (
        "openai/gpt-4o-transcribe",
        "openai/gpt-4o-mini-transcribe",
        "openai/whisper-1",
    ):
        assert model_id in STT_MODELS
        assert STT_MODELS[model_id]["provider"] == "openai"


def test_model_provider_detection():
    assert _model_provider("openai/tts-1", TTS_MODELS["openai/tts-1"]) == "openai"
    assert _model_provider("fal-ai/whisper", STT_MODELS["fal-ai/whisper"]) == "fal-ai"
    # Prefix fallback when no curated info is available.
    assert _model_provider("openai/some-new-model", None) == "openai"
    assert _model_provider("fal-ai/uncurated", None) == "fal-ai"


def test_openai_model_name_strips_prefix():
    assert _openai_model_name("openai/gpt-4o-transcribe") == "gpt-4o-transcribe"
    assert _openai_model_name("tts-1") == "tts-1"


def test_openai_voice_defaults_when_invalid():
    info = TTS_MODELS["openai/tts-1"]
    # A fal voice is not a valid OpenAI voice — fall back to the first voice.
    assert _openai_voice("af_heart", info) == info["voices"][0]
    # A valid OpenAI voice passes through unchanged.
    assert _openai_voice("nova", info) == "nova"


# ---------------------------------------------------------------------------
# OpenAI HTTP helpers (hermetic — no network)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, content=b"", payload=None, text=""):
        self.status_code = status_code
        self.content = content
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the last request so assertions can inspect it."""

    def __init__(self, response):
        self._response = response
        self.last = {}

    async def post(self, url, headers=None, json=None, files=None, data=None):
        self.last = {
            "url": url,
            "headers": headers,
            "json": json,
            "files": files,
            "data": data,
        }
        return self._response


@pytest.mark.asyncio
async def test_openai_tts_bytes_payload_and_return():
    client = _FakeClient(_FakeResponse(status_code=200, content=b"audio-bytes"))
    info = TTS_MODELS["openai/gpt-4o-mini-tts"]
    out = await _openai_tts_bytes(
        client, "sk-key", "openai/gpt-4o-mini-tts", "hello",
        "nova", 1.5, "mp3", info,
    )
    assert out == b"audio-bytes"
    body = client.last["json"]
    assert body["model"] == "gpt-4o-mini-tts"
    assert body["input"] == "hello"
    assert body["voice"] == "nova"
    assert body["response_format"] == "mp3"
    # gpt-4o-mini-tts does not support the speed knob → never sent.
    assert "speed" not in body
    assert client.last["headers"]["Authorization"] == "Bearer sk-key"


@pytest.mark.asyncio
async def test_openai_tts_bytes_includes_speed_for_tts1():
    client = _FakeClient(_FakeResponse(status_code=200, content=b"x"))
    info = TTS_MODELS["openai/tts-1"]
    await _openai_tts_bytes(
        client, "sk", "openai/tts-1", "hi", "alloy", 2.0, "wav", info,
    )
    assert client.last["json"]["speed"] == 2.0
    assert client.last["json"]["response_format"] == "wav"


@pytest.mark.asyncio
async def test_openai_tts_bytes_maps_auth_error():
    from fastapi import HTTPException

    client = _FakeClient(_FakeResponse(status_code=401, text="bad key"))
    with pytest.raises(HTTPException) as exc:
        await _openai_tts_bytes(
            client, "sk", "openai/tts-1", "hi", "alloy", 1.0, "mp3",
            TTS_MODELS["openai/tts-1"],
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503, 400])
async def test_openai_upstream_failures_are_502(status):
    from fastapi import HTTPException

    for call in (
        lambda c: _openai_tts_bytes(c, "sk", "openai/tts-1", "hi", "alloy", 1.0, "mp3",
                                    TTS_MODELS["openai/tts-1"]),
        lambda c: _openai_transcribe(c, "sk", "openai/whisper-1", b"b", "a.webm", "audio/webm"),
    ):
        with pytest.raises(HTTPException) as exc:
            await call(_FakeClient(_FakeResponse(status_code=status, text="upstream down")))
        assert exc.value.status_code == 502
        assert exc.value.detail.endswith("failed: upstream down")


@pytest.mark.asyncio
async def test_openai_transcribe_posts_multipart_and_returns_text():
    client = _FakeClient(_FakeResponse(status_code=200, payload={"text": " hi there "}))
    text = await _openai_transcribe(
        client, "sk", "openai/gpt-4o-transcribe", b"bytes", "clip.webm", "audio/webm",
    )
    assert text == "hi there"
    assert client.last["data"]["model"] == "gpt-4o-transcribe"
    assert client.last["files"]["file"] == ("clip.webm", b"bytes", "audio/webm")
    assert client.last["headers"]["Authorization"] == "Bearer sk"


# ---------------------------------------------------------------------------
# Key resolution from OpenAI provider rows
# ---------------------------------------------------------------------------


def test_resolve_openai_api_key_picks_enabled_openai_row(monkeypatch):
    rows = [
        {"provider_type": "anthropic", "enabled": True, "auth_token": "enc-a"},
        {"provider_type": "openai", "enabled": True, "auth_token": "enc-o"},
    ]
    monkeypatch.setattr(
        settings_audio.database, "get_enabled_model_providers", lambda include_token=False: rows
    )
    import app.core.provider.fluid_credentials as fc
    monkeypatch.setattr(fc, "_is_codex_subscription", lambda row: False)
    monkeypatch.setattr(fc, "_decrypt_auth_token", lambda row: "sk-decrypted")

    assert _resolve_openai_api_key() == "sk-decrypted"


def test_resolve_openai_api_key_skips_codex_subscription(monkeypatch):
    rows = [{"provider_type": "openai", "enabled": True, "auth_token": "enc"}]
    monkeypatch.setattr(
        settings_audio.database, "get_enabled_model_providers", lambda include_token=False: rows
    )
    import app.core.provider.fluid_credentials as fc
    monkeypatch.setattr(fc, "_is_codex_subscription", lambda row: True)
    monkeypatch.setattr(fc, "_decrypt_auth_token", lambda row: "sk")

    assert _resolve_openai_api_key() is None


def test_resolve_openai_api_key_none_without_openai_row(monkeypatch):
    monkeypatch.setattr(
        settings_audio.database, "get_enabled_model_providers", lambda include_token=False: []
    )
    assert _resolve_openai_api_key() is None

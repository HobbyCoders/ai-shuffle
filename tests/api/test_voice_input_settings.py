"""Composer dictation preferences share the persisted Voice settings endpoint."""
import pytest
from app.db import database as db

URL = "/api/v1/settings/voice"


def test_dictation_preferences_round_trip_and_preserve_timeout(authenticated_client, mock_db):
    c = authenticated_client
    initial = c.get(URL).json()
    assert initial["voice_input_mode"] == "auto"
    assert initial["voice_input_silence_seconds"] == 5
    for patch in ({"voice_input_silence_seconds": 9}, {"voice_input_mode": "toggle"}, {"voice_input_mode": "auto"}):
        response = c.put(URL, json=patch)
        assert response.status_code == 200, response.text
        assert c.get(URL).json() == response.json()
        assert response.json()["voice_input_silence_seconds"] == 9
        assert response.json()["voice_model"] == initial["voice_model"]
        assert response.json()["voice_options"] == initial["voice_options"]
    assert db.get_system_setting("voice_input_mode") == "auto"
    assert db.get_system_setting("voice_input_silence_seconds") == "9"


@pytest.mark.parametrize("patch", [
    {"voice_input_mode": "manual"}, {"voice_input_mode": None},
    *({"voice_input_silence_seconds": value} for value in (None, 0, -1, 61, 1.5, True, "5")),
])
def test_invalid_dictation_preference_does_not_partially_write(authenticated_client, mock_db, patch):
    c = authenticated_client
    before = c.get(URL).json()
    response = c.put(URL, json={"voice_mode_enabled": not before["voice_mode_enabled"], **patch})
    assert response.status_code in (400, 422), response.text
    assert c.get(URL).json() == before


@pytest.mark.parametrize("raw", ["nonsense", "0", "61", "2.5"])
def test_invalid_saved_preferences_fall_back(authenticated_client, mock_db, raw):
    db.set_system_setting("voice_input_mode", "retired")
    db.set_system_setting("voice_input_silence_seconds", raw)
    value = authenticated_client.get(URL).json()
    assert value["voice_input_mode"] == "auto"
    assert value["voice_input_silence_seconds"] == 5

def test_model_selection_is_independent_and_routes_voice_input(authenticated_client, mock_db, monkeypatch):
    from app.api import settings_audio
    from unittest.mock import AsyncMock

    c = authenticated_client
    db.set_system_setting("stt_model", "fal-ai/whisper")
    initial = c.get(URL).json()
    assert initial["voice_input_model"] == "fal-ai/whisper"
    assert {m["provider"] for m in initial["voice_input_models"]} == {"openai", "fal-ai"}
    chosen = "openai/gpt-4o-mini-transcribe"
    response = c.put(URL, json={"voice_input_model": chosen})
    assert response.status_code == 200, response.text
    assert c.get(URL).json()["voice_input_model"] == chosen
    assert db.get_system_setting("stt_model") == "fal-ai/whisper"
    assert response.json()["voice_model"] == initial["voice_model"]
    assert response.json()["voice_options"] == initial["voice_options"]

    # Exercise the saved setting through both consumers. Provider I/O is stubbed.
    monkeypatch.setattr(settings_audio, "_resolve_openai_api_key", lambda: "sk-test")
    capability = c.get("/api/v1/settings/transcribe/stream/capability").json()
    assert capability["model"] == chosen
    assert capability["supported"] is True
    transcribe = AsyncMock(return_value="dictated text")
    monkeypatch.setattr(settings_audio, "_openai_transcribe", transcribe)
    result = c.post("/api/v1/settings/transcribe", files={"file": ("speech.wav", b"RIFF-audio", "audio/wav")})
    assert result.status_code == 200, result.text
    assert result.json()["text"] == "dictated text"
    assert transcribe.await_args.args[2] == chosen

    media = c.patch("/api/v1/settings/integrations/audio/stt", json={"model": "fal-ai/elevenlabs/speech-to-text"})
    assert media.status_code == 200, media.text
    assert db.get_system_setting("stt_model") == "fal-ai/elevenlabs/speech-to-text"
    assert c.get(URL).json()["voice_input_model"] == chosen
    assert c.get("/api/v1/settings/transcribe/stream/capability").json()["model"] == chosen


@pytest.mark.parametrize("first_read", [True, False])
def test_legacy_choice_is_preserved_once(authenticated_client, mock_db, first_read):
    c = authenticated_client
    previous = "openai/whisper-1"
    db.set_system_setting("stt_model", previous)
    if first_read:
        assert c.get(URL).json()["voice_input_model"] == previous
    changed = c.patch("/api/v1/settings/integrations/audio/stt", json={"model": "fal-ai/whisper"})
    assert changed.status_code == 200, changed.text
    assert c.get(URL).json()["voice_input_model"] == previous
    assert db.get_system_setting("voice_input_model") == previous


@pytest.mark.parametrize("model", [None, "", "openai/not-a-model", "fal-ai/not-a-model"])
def test_invalid_input_model_does_not_change_other_settings(authenticated_client, mock_db, model):
    c = authenticated_client
    before = c.get(URL).json()
    response = c.put(URL, json={"voice_input_model": model, "voice_input_mode": "toggle"})
    assert response.status_code == 400
    assert c.get(URL).json() == before

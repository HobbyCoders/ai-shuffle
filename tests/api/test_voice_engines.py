"""Voice engines in Settings: ChatGPT voice list, GPT-Live-1.1 gate, fallbacks."""
import logging

import httpx
import pytest

from app.core import encryption, voice_availability
from app.core.voice_models import CODEX_MODEL, CODEX_VOICES, LIVE_11_MODEL, LIVE_VOICES
from tests.api.test_voice_api import RAW_KEY, VOICE_SETTINGS, _make_provider
from tests.helpers import unlock_encryption

REAL_FETCH_STATUS = voice_availability._fetch_status


def _api_key_provider(mock_db, provider_id="prov-openai", name="OpenAI"):
    unlock_encryption(mock_db)
    return _make_provider(provider_id, name=name, auth_token=encryption.encrypt_value(RAW_KEY))


def _models_answer(monkeypatch, status):
    calls = []

    async def fetch(api_key):
        calls.append(api_key)
        return status

    monkeypatch.setattr(voice_availability, "_fetch_status", fetch)
    return calls


def _engine_ids(body):
    return [engine["id"] for engine in body["voice_engines"]]


class TestChatGPTVoices:
    def test_all_codex_voices_are_listed_with_labels(self, authenticated_client, mock_db):
        assert len(CODEX_VOICES) == 25 and len(set(CODEX_VOICES)) == 25
        assert all(voice == voice.lower() for voice in CODEX_VOICES)
        saved = authenticated_client.put(VOICE_SETTINGS, json={"voice_model": CODEX_MODEL, "voice_provider_id": None}).json()
        details = saved["model_details"]
        assert [v["id"] for v in details["voices"]] == list(CODEX_VOICES)
        assert details["voices"][0] == {"id": "juniper", "label": "Juniper"}
        assert details["label"] == "GPT-Live-1.1 · ChatGPT subscription"
        assert details["preview_note"].startswith("Uses your ChatGPT subscription login. Previews aren't available for subscription voice.")
        for text in (details["label"], details["preview_note"]):
            assert "nofficial" not in text and "nsupported" not in text

    def test_codex_accepts_its_voices_and_rejects_others(self, authenticated_client, mock_db):
        c = authenticated_client
        assert c.put(VOICE_SETTINGS, json={"voice_model": CODEX_MODEL}).json()["voice_voice"] == "cove"
        for voice in ("juniper", "viola", "sol"):
            response = c.put(VOICE_SETTINGS, json={"voice_voice": voice})
            assert response.status_code == 200, response.text
            assert c.get(VOICE_SETTINGS).json()["voice_voice"] == voice
        for foreign in ("marin", "eve", "NATF0"):
            assert c.put(VOICE_SETTINGS, json={"voice_voice": foreign}).status_code == 400
        assert c.get(VOICE_SETTINGS).json()["voice_voice"] == "sol"

    @pytest.mark.parametrize("start_voice,engine,expected", [
        ("juniper", "gpt-live-1", "marin"),
        ("juniper", "grok-voice-latest", "eve"),
        ("juniper", "grok-voice-supergrok", "eve"),
        ("quartz", CODEX_MODEL, "cove"),
        ("sol", "gpt-live-1", "marin"),
    ])
    def test_engine_switch_falls_back_to_that_engines_default(self, authenticated_client, mock_db, start_voice, engine, expected):
        c = authenticated_client
        start = CODEX_MODEL if start_voice in CODEX_VOICES and start_voice not in LIVE_VOICES else "gpt-live-1"
        assert c.put(VOICE_SETTINGS, json={"voice_model": start, "voice_voice": start_voice}).status_code == 200
        response = c.put(VOICE_SETTINGS, json={"voice_model": engine, "voice_provider_id": None})
        assert response.status_code == 200, response.text
        assert response.json()["voice_voice"] == expected
        assert c.get(VOICE_SETTINGS).json()["voice_voice"] == expected

    def test_shared_voice_survives_an_engine_switch(self, authenticated_client, mock_db):
        c = authenticated_client
        c.put(VOICE_SETTINGS, json={"voice_model": "grok-voice-latest", "voice_voice": "rex"})
        assert c.put(VOICE_SETTINGS, json={"voice_model": "grok-voice-supergrok"}).json()["voice_voice"] == "rex"

    def test_codex_options_round_trip(self, authenticated_client, mock_db):
        c = authenticated_client
        c.put(VOICE_SETTINGS, json={"voice_model": CODEX_MODEL})
        patch = {"opening": "Hi there", "instructions": "Be brief.", "delegation_instructions": "Use the chat.", "mic_threshold_db": -50}
        response = c.put(VOICE_SETTINGS, json={"voice_options": patch, "voice_options_model": CODEX_MODEL})
        assert response.status_code == 200, response.text
        options = c.get(VOICE_SETTINGS).json()["voice_options"]
        assert {key: options[key] for key in patch} == patch
        assert c.put(VOICE_SETTINGS, json={"voice_options": {"mic_threshold_db": -10}, "voice_options_model": CODEX_MODEL}).status_code == 400

    def test_engine_switch_drops_the_previous_engines_pin(self, authenticated_client, mock_db):
        c = authenticated_client
        pid = _make_provider()
        assert c.put(VOICE_SETTINGS, json={"voice_provider_id": pid}).json()["voice_provider_id"] == pid
        # The frontend clears the pin itself, but a partial update must not keep
        # an OpenAI key pinned for an engine it cannot fund.
        saved = c.put(VOICE_SETTINGS, json={"voice_model": "grok-voice-latest"}).json()
        assert saved["voice_provider_id"] is None
        assert c.put(VOICE_SETTINGS, json={"voice_model": "gpt-live-1"}).json()["voice_provider_id"] is None

    def test_codex_session_sends_the_chosen_voice(self):
        from app.core.voice_codex_transport import codex_session
        session = codex_session({"voice": "juniper"}, {"instructions": ""}, [])
        assert session["model"] == CODEX_MODEL
        assert session["audio"]["output"]["voice"] == "juniper"


class TestLive11Gate:
    def test_hidden_on_404_and_listed_on_200(self, authenticated_client, mock_db, monkeypatch):
        _api_key_provider(mock_db)
        calls = _models_answer(monkeypatch, 404)
        body = authenticated_client.get(VOICE_SETTINGS).json()
        assert LIVE_11_MODEL not in _engine_ids(body)
        assert calls == [RAW_KEY]
        assert authenticated_client.put(VOICE_SETTINGS, json={"voice_model": LIVE_11_MODEL}).status_code == 400

        voice_availability.clear_cache()
        _models_answer(monkeypatch, 200)
        body = authenticated_client.get(VOICE_SETTINGS).json()
        assert _engine_ids(body) == ["gpt-live-1", LIVE_11_MODEL, "grok-voice-latest", "grok-voice-supergrok", CODEX_MODEL]
        assert body["voice_engines"][1]["label"] == "GPT-Live-1.1"
        saved = authenticated_client.put(VOICE_SETTINGS, json={"voice_model": LIVE_11_MODEL, "voice_voice": "quartz"}).json()
        assert saved["voice_model"] == LIVE_11_MODEL and saved["voice_voice"] == "quartz"
        details = saved["model_details"]
        assert details["transport"] == "stream" and [v["id"] for v in details["voices"]] == list(LIVE_VOICES)
        assert details["preview_note"] == "Previews use the selected engine and voice with your API key."

    def test_no_api_key_means_no_lookup_and_hidden(self, authenticated_client, mock_db, monkeypatch):
        calls = _models_answer(monkeypatch, 200)
        assert LIVE_11_MODEL not in _engine_ids(authenticated_client.get(VOICE_SETTINGS).json())
        assert calls == []

    def test_stored_choice_falls_back_quietly_when_unavailable(self, authenticated_client, mock_db, monkeypatch):
        from app.db import database
        _api_key_provider(mock_db)
        _models_answer(monkeypatch, 200)
        c = authenticated_client
        c.put(VOICE_SETTINGS, json={"voice_model": LIVE_11_MODEL})
        c.put(VOICE_SETTINGS, json={"voice_options": {"opening": "Hello"}, "voice_options_model": LIVE_11_MODEL})
        voice_availability.clear_cache()
        _models_answer(monkeypatch, 404)
        body = c.get(VOICE_SETTINGS).json()
        assert body["voice_model"] == "gpt-live-1"
        # Options are shared, so the fallback keeps what was written.
        assert body["voice_options"]["opening"] == "Hello"
        assert database.get_system_setting("voice_model") == LIVE_11_MODEL
        from app.api.settings import get_voice_settings
        assert get_voice_settings()["voice_model"] == "gpt-live-1"
        # Changing the voice while falling back keeps the saved 1.1 choice.
        assert c.put(VOICE_SETTINGS, json={"voice_voice": "cedar"}).json()["voice_model"] == "gpt-live-1"
        assert database.get_system_setting("voice_model") == LIVE_11_MODEL
        voice_availability.clear_cache()
        _models_answer(monkeypatch, 200)
        body = c.get(VOICE_SETTINGS).json()
        assert (body["voice_model"], body["voice_voice"]) == (LIVE_11_MODEL, "cedar")

    def test_live_11_stream_session_uses_its_model(self):
        from app.core.voice_stream import live_session_config
        assert live_session_config({"model": LIVE_11_MODEL, "voice": "marin"}, [])["model"] == LIVE_11_MODEL
        assert live_session_config({"model": "gpt-live-1", "voice": "marin"}, [])["model"] == "gpt-live-1"
        assert live_session_config({"model": CODEX_MODEL, "voice": "cove"}, [])["model"] == "gpt-live-1"


@pytest.mark.asyncio
class TestLive11Probe:
    async def test_found_is_cached_six_hours_missing_one_hour(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(voice_availability, "_now", lambda: clock[0])
        calls = _models_answer(monkeypatch, 200)
        assert await voice_availability.probe("p", "key-a") is True
        clock[0] += 6 * 3600 - 1
        assert await voice_availability.probe("p", "key-a") is True
        assert len(calls) == 1 and voice_availability.cached("p")
        clock[0] += 2
        calls = _models_answer(monkeypatch, 404)
        assert await voice_availability.probe("p", "key-a") is False
        clock[0] += 3600 - 1
        assert await voice_availability.probe("p", "key-a") is False
        assert len(calls) == 1 and not voice_availability.cached("p")
        clock[0] += 2
        await voice_availability.probe("p", "key-a")
        assert len(calls) == 2

    async def test_new_key_is_checked_again(self, monkeypatch):
        calls = _models_answer(monkeypatch, 404)
        await voice_availability.probe("p", "key-a")
        await voice_availability.probe("p", "key-b")
        assert calls == ["key-a", "key-b"]

    async def test_network_failure_hides_without_logging_the_key(self, monkeypatch, caplog):
        async def boom(api_key):
            raise httpx.ConnectError("failed with " + api_key)
        monkeypatch.setattr(voice_availability, "_fetch_status", boom)
        with caplog.at_level(logging.DEBUG):
            assert await voice_availability.probe("p", "sk-secret-value") is False
        assert "sk-secret-value" not in caplog.text

    @pytest.mark.parametrize("status,expected", [(200, True), (404, False), (401, False)])
    async def test_real_lookup_asks_the_models_endpoint(self, monkeypatch, caplog, status, expected):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(status, json={"id": "gpt-live-1.1"} if status == 200 else {"error": {}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        from app.core import http_client
        monkeypatch.setattr(http_client, "get_http_client", lambda: client)
        monkeypatch.setattr(voice_availability, "_fetch_status", REAL_FETCH_STATUS)
        with caplog.at_level(logging.DEBUG):
            assert await voice_availability.probe("p", "sk-secret-value") is expected
        assert str(seen[0].url) == "https://api.openai.com/v1/models/gpt-live-1.1"
        assert seen[0].method == "GET"
        assert seen[0].headers["authorization"] == "Bearer sk-secret-value"
        assert "sk-secret-value" not in caplog.text
        await client.aclose()

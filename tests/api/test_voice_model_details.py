"""Settings metadata and the call configuration producer must agree."""
from tests.api.test_voice_api import _make_provider, VOICE_SETTINGS
from app.core import voice_bridge


class TestVoiceModelDetails:
    def test_switching_models_refreshes_voices_and_prompts(self, authenticated_client, mock_db, monkeypatch):
        from app.api import voice
        from app.core.voice_models import FAL_PROVIDER_ID, LIVE_VOICES, REALTIME_VOICES, FAL_VOICES
        from app.core.voice_prompts import LIVE_INSTRUCTIONS, PERSONAPLEX_INSTRUCTIONS
        pid = _make_provider()
        monkeypatch.setattr(voice, "list_eligible_voice_providers", lambda: [{"id": pid}, {"id": FAL_PROVIDER_ID}])
        cases = [
            ("gpt-live-1", LIVE_VOICES, LIVE_INSTRUCTIONS),
            ("fal-ai/personaplex/realtime", FAL_VOICES, PERSONAPLEX_INSTRUCTIONS),
            ("gpt-realtime-2.1-mini", REALTIME_VOICES, voice_bridge.VOICE_INSTRUCTIONS),
            ("gpt-realtime-2.1", REALTIME_VOICES, voice_bridge.VOICE_INSTRUCTIONS),
            ("gpt-realtime", REALTIME_VOICES, voice_bridge.VOICE_INSTRUCTIONS),
        ]
        for model, voices, prompt in cases:
            response = authenticated_client.put(VOICE_SETTINGS, json={"voice_model": model})
            assert response.status_code == 200, response.text
            saved = response.json()
            assert authenticated_client.get(VOICE_SETTINGS).json() == saved
            details = saved["model_details"]
            assert details["model"] == model
            assert [v["id"] for v in details["voices"]] == list(voices)
            assert saved["voice_voice"] in voices
            assert details["prompts"][0]["text"] == prompt
            # The options are also accepted by the real partial-update producer.
            last_voice = voices[-1]
            response = authenticated_client.put(VOICE_SETTINGS, json={"voice_voice": last_voice})
            assert response.status_code == 200, response.text
            assert response.json()["voice_voice"] == last_voice
            assert authenticated_client.get(VOICE_SETTINGS).json()["voice_voice"] == last_voice
            invalid = "marin" if model == "fal-ai/personaplex/realtime" else "NATF2"
            assert authenticated_client.put(VOICE_SETTINGS, json={"voice_voice": invalid}).status_code == 400

    def test_settings_use_live_protocol_prompt(self, authenticated_client, mock_db):
        from app.core.voice_stream import live_session_config
        response = authenticated_client.put(VOICE_SETTINGS, json={"voice_model": "gpt-live-1"})
        assert response.status_code == 200
        details = response.json()["model_details"]
        sent = live_session_config({"voice": response.json()["voice_voice"]}, [])
        assert details["prompts"][0]["text"] == sent["instructions"]
        assert sent["delegation"] == {"type": "client"}

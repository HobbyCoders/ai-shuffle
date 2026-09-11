"""Settings metadata and the call configuration producer must agree."""
from tests.api.test_voice_api import _make_provider, VOICE_SETTINGS
from app.core import voice_bridge


class TestVoiceModelDetails:
    def test_only_live_voices_and_editable_defaults_are_exposed(self, authenticated_client, mock_db):
        from app.core.voice_models import LIVE_VOICES
        from app.core.voice_prompts import LIVE_INSTRUCTIONS
        c = authenticated_client
        saved = c.get(VOICE_SETTINGS).json()
        details = saved["model_details"]
        assert details["model"] == "gpt-live-1"
        assert [v["id"] for v in details["voices"]] == list(LIVE_VOICES)
        assert details["prompts"][0]["text"] == details["prompts"][0]["default_text"] == LIVE_INSTRUCTIONS
        assert c.put(VOICE_SETTINGS, json={"voice_voice":LIVE_VOICES[-1]}).status_code == 200
        assert c.get(VOICE_SETTINGS).json()["voice_voice"] == LIVE_VOICES[-1]
        assert c.put(VOICE_SETTINGS, json={"voice_voice":"NATF2"}).status_code == 400
        for retired in ("gpt-realtime", "fal-ai/personaplex/realtime"):
            assert c.put(VOICE_SETTINGS, json={"voice_model":retired}).status_code == 400

    def test_settings_use_live_protocol_prompt(self, authenticated_client, mock_db):
        from app.core.voice_stream import live_session_config
        response = authenticated_client.put(VOICE_SETTINGS, json={"voice_model": "gpt-live-1"})
        assert response.status_code == 200
        details = response.json()["model_details"]
        sent = live_session_config({"voice": response.json()["voice_voice"]}, [])
        assert details["prompts"][0]["text"] == sent["instructions"]
        assert sent["delegation"] == {"type": "client"}

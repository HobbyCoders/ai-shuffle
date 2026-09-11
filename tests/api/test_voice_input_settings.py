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

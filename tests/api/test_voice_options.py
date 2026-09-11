"""Controls must survive the actual settings -> session -> provider path."""

import json
import pytest
from unittest.mock import AsyncMock
from app.core import voice_bridge
from app.db import database as db
from app.core.voice_options import realtime_session_options
from app.core.voice_stream import live_session_config, live_opening_event
from app.core.voice_models import session_config, model_details
from tests.api.test_voice_api import (
    VOICE_ROOT,
    VOICE_SETTINGS,
    _make_provider,
    _enable_voice,
    _install_mock_transport,
    _secret_handler,
)


def save(client, **options):
    r = client.put(VOICE_SETTINGS, json={"voice_options": options})
    assert r.status_code == 200, r.text
    return r.json()


def test_producing_path_pins_options_for_mint_remint_and_sideband(
    authenticated_client, mock_db, monkeypatch
):
    c = authenticated_client
    _make_provider()
    _enable_voice()
    saved = save(
        c,
        transcription_model="gpt-realtime-whisper",
        max_output_tokens=1024,
        turn_detection="server_vad",
        threshold=0.42,
        prefix_padding_ms=250,
        silence_duration_ms=400,
        idle_timeout_ms=6000,
        instructions="Speak clearly.",
    )
    assert c.get(VOICE_SETTINGS).json() == saved
    captured = {}
    _install_mock_transport(monkeypatch, _secret_handler(captured))
    r = c.post(f"{VOICE_ROOT}/sessions", json={})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert r.json()["options"] == saved["voice_options"]
    expected = realtime_session_options(saved["voice_options"])

    def check_mint():
        payload = json.loads(captured["request"].content)["session"]
        assert payload["max_output_tokens"] == 1024
        assert payload["audio"]["input"] == expected["audio"]["input"]

    check_mint()
    binding = json.loads(db.get_session(sid)["voice_config"])
    assert binding["options"] == saved["voice_options"]
    save(c, turn_detection="semantic_vad", eagerness="high", max_output_tokens="inf")
    assert c.get(f"{VOICE_ROOT}/sessions/{sid}/config").json() == binding
    r = c.post(f"{VOICE_ROOT}/sessions/{sid}/secret")
    assert r.status_code == 200, r.text
    assert r.json()["options"] == saved["voice_options"]
    check_mint()
    attach = AsyncMock()
    monkeypatch.setattr(voice_bridge.registry, "attach", attach)
    r = c.post(f"{VOICE_ROOT}/sessions/{sid}/call", json={"call_id": "rtc_options"})
    assert r.status_code == 200, r.text
    assert attach.call_args.kwargs["voice_options"] == saved["voice_options"]
    assert attach.call_args.kwargs["instructions_override"] == "Speak clearly."


def test_per_model_storage_and_mutually_exclusive_vad(authenticated_client, mock_db):
    c = authenticated_client
    original = save(
        c, turn_detection="server_vad", threshold=0.75, idle_timeout_ms=6000
    )
    assert (
        c.put(VOICE_SETTINGS, json={"voice_model": "gpt-realtime-2"}).status_code == 200
    )
    assert c.get(VOICE_SETTINGS).json()["voice_options"]["threshold"] == 0.5
    save(c, transcription_model="gpt-transcribe", max_output_tokens=200)
    c.put(VOICE_SETTINGS, json={"voice_model": "gpt-realtime-2.1"})
    assert c.get(VOICE_SETTINGS).json()["voice_options"] == original["voice_options"]
    current = save(c, turn_detection="semantic_vad", eagerness="low")
    assert current["voice_options"]["idle_timeout_ms"] == 6000
    vad = realtime_session_options(current["voice_options"])["audio"]["input"][
        "turn_detection"
    ]
    assert vad == {
        "type": "semantic_vad",
        "eagerness": "low",
        "interrupt_response": True,
        "create_response": True,
    }
    assert save(c, turn_detection="server_vad")["voice_options"]["threshold"] == 0.75
    assert save(c, idle_timeout_ms=None)["voice_options"]["idle_timeout_ms"] is None


@pytest.mark.parametrize(
    "options",
    [
        {"max_output_tokens": 0},
        {"max_output_tokens": 4097},
        {"max_output_tokens": True},
        {"max_output_tokens": "1024"},
        {"max_output_tokens": 1.5},
        {"threshold": 1.1},
        {"prefix_padding_ms": -1},
        {"silence_duration_ms": 99},
        {"idle_timeout_ms": 4999},
        {"idle_timeout_ms": 30001},
        {"transcription_model": "gpt-live-1"},
        {"turn_detection": "manual"},
        {"opening": "wrong model"},
        {"instructions": None},
    ],
)
def test_invalid_options_write_nothing(authenticated_client, mock_db, options):
    c = authenticated_client
    before = c.get(VOICE_SETTINGS).json()
    r = c.put(
        VOICE_SETTINGS, json={"voice_model": "gpt-realtime-2", "voice_options": options}
    )
    assert r.status_code == 400, r.text
    assert c.get(VOICE_SETTINGS).json() == before


def test_explicit_composer_model_does_not_inherit_another_models_effort(
    authenticated_client, mock_db
):
    _make_provider()
    _enable_voice(voice_model="gpt-live-1")
    save(authenticated_client, delegation_model="sonnet", delegation_effort="medium")
    response = authenticated_client.post(
        f"{VOICE_ROOT}/sessions", json={"model": "opus"}
    )
    assert response.status_code == 200, response.text
    row = db.get_session(response.json()["session_id"])
    assert row["model"] == "opus"
    assert row["effort"] is None


def test_stale_editor_is_rejected(authenticated_client, mock_db):
    r = authenticated_client.put(
        VOICE_SETTINGS,
        json={
            "voice_options_model": "gpt-live-1",
            "voice_options": {"instructions": "stale"},
        },
    )
    assert r.status_code == 409


def test_live_defaults_and_prompts_drive_new_session(authenticated_client, mock_db):
    from app.core.model_registry import registry_payload

    selected = registry_payload()["models"][0]["value"]
    c = authenticated_client
    _make_provider()
    _enable_voice(voice_model="gpt-live-1")
    saved = save(
        c,
        opening="Hello there.",
        instructions="Be concise.",
        delegation_model=selected,
        delegation_instructions="Return short results.",
    )
    r = c.post(f"{VOICE_ROOT}/sessions", json={})
    assert r.status_code == 200, r.text
    row = db.get_session(r.json()["session_id"])
    assert row["model"] == selected
    config = json.loads(row["voice_config"])
    assert config["options"] == saved["voice_options"]
    assert live_session_config(config, [])["instructions"] == "Be concise."
    assert (
        model_details("gpt-live-1", config["options"])["prompts"][1]["text"]
        == "Return short results."
    )
    event = live_opening_event(config["options"]["opening"])
    assert (
        event["type"] == "session.instructions.append"
        and event["delegation_id"] is None
    )
    assert event["content"].endswith("Hello there.")


def test_responses_delegation_is_provider_bound(authenticated_client, mock_db):
    c = authenticated_client
    first = _make_provider("first")
    second = _make_provider("second")
    for pid in (first, second):
        db.update_model_provider(
            pid, models_json=[{"id": "gpt-5.1", "label": "Test reasoner"}]
        )
    r = c.put(
        VOICE_SETTINGS, json={"voice_provider_id": first, "voice_model": "gpt-live-1"}
    )
    assert r.status_code == 200, r.text
    saved = save(
        c,
        delegation_type="responses",
        delegation_model=f"prov:{first}:gpt-5.1",
        delegation_instructions="Be helpful.",
    )
    sent = live_session_config(
        {"voice": "marin", "options": saved["voice_options"]}, []
    )
    assert sent["delegation"] == {
        "type": "responses",
        "responses": {"model": "gpt-5.1", "instructions": "Be helpful.", "tools": []},
    }
    assert c.put(VOICE_SETTINGS, json={"voice_provider_id": second}).status_code == 400
    assert c.get(VOICE_SETTINGS).json()["voice_provider_id"] == first
    assert (
        c.put(
            VOICE_SETTINGS,
            json={"voice_options": {"delegation_model": f"prov:{second}:gpt-5.1"}},
        ).status_code
        == 400
    )


def test_legacy_pins_do_not_inherit_new_global_options():
    pinned = {"model": "gpt-realtime", "voice": "marin", "provider_id": "key"}
    config = session_config(
        {"voice_config": json.dumps(pinned)}, {"voice_options": {"threshold": 0.9}}
    )
    assert config == pinned
    assert (
        realtime_session_options(config.get("options"))["audio"]["input"][
            "turn_detection"
        ]["type"]
        == "semantic_vad"
    )


@pytest.mark.asyncio
async def test_sideband_does_not_overwrite_normal_vad():
    bridge = voice_bridge.VoiceBridge(
        "options",
        "rtc_options",
        api_key="test",
        model="gpt-realtime-2",
        max_call_minutes=30,
        voice_options={
            "turn_detection": "server_vad",
            "threshold": 0.25,
            "transcription_model": "gpt-transcribe",
            "max_output_tokens": 200,
        },
    )
    bridge._send = AsyncMock()
    await bridge._send_session_update()
    sent = bridge._send.call_args.args[0]["session"]
    assert sent["max_output_tokens"] == 200
    assert sent["audio"]["input"]["turn_detection"]["threshold"] == 0.25
    assert sent["audio"]["input"]["transcription"]["model"] == "gpt-transcribe"

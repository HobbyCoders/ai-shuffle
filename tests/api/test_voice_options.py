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


def test_producing_path_pins_live_prompts_and_restores_defaults(authenticated_client, mock_db):
    from app.core.voice_prompts import LIVE_INSTRUCTIONS, DELEGATION_INSTRUCTIONS
    c = authenticated_client
    _make_provider()
    _enable_voice()
    saved = save(c, instructions="Speak clearly.", delegation_instructions="Keep tasks short.", opening="Hello there.")
    assert c.get(VOICE_SETTINGS).json() == saved
    r = c.post(f"{VOICE_ROOT}/sessions", json={"model":"sonnet", "effort":"high"})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    binding = json.loads(db.get_session(sid)["voice_config"])
    assert binding["options"] == saved["voice_options"]
    assert live_session_config(binding, [])["instructions"] == "Speak clearly."
    assert live_session_config(binding, [])["delegation"] == {"type":"client"}
    restored = save(c, instructions="", delegation_instructions="", opening="")
    prompts = restored["model_details"]["prompts"]
    assert prompts[0]["text"] == prompts[0]["default_text"] == LIVE_INSTRUCTIONS
    assert prompts[1]["text"] == prompts[1]["default_text"] == DELEGATION_INSTRUCTIONS
    assert c.get(f"{VOICE_ROOT}/sessions/{sid}/config").json() == binding
    assert c.post(f"{VOICE_ROOT}/sessions/{sid}/secret").json()["options"] == saved["voice_options"]


def test_legacy_realtime_vad_remains_mutually_exclusive():
    assert realtime_session_options({"turn_detection":"server_vad", "threshold":.75})["audio"]["input"]["turn_detection"]["threshold"] == .75
    vad = realtime_session_options({"turn_detection":"semantic_vad", "eagerness":"low", "threshold":.75})["audio"]["input"]["turn_detection"]
    assert vad == {"type":"semantic_vad", "eagerness":"low", "interrupt_response":True, "create_response":True}


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
        {"opening": 42},
        {"instructions": None},
    ],
)
def test_invalid_options_write_nothing(authenticated_client, mock_db, options):
    c = authenticated_client
    before = c.get(VOICE_SETTINGS).json()
    r = c.put(
        VOICE_SETTINGS, json={"voice_options": options}
    )
    assert r.status_code == 400, r.text
    assert c.get(VOICE_SETTINGS).json() == before


def test_explicit_composer_model_does_not_inherit_another_models_effort(
    authenticated_client, mock_db
):
    _make_provider()
    _enable_voice(voice_model="gpt-live-1")
    db.set_system_setting("voice_options:gpt-live-1", json.dumps({"delegation_type":"responses", "delegation_model":"sonnet", "delegation_effort":"medium", "instructions":"Keep me."}))
    assert authenticated_client.get(VOICE_SETTINGS).json()["voice_options"]["instructions"] == "Keep me."
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
            "voice_options_model": "gpt-realtime-2.1",
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
        delegation_instructions="Return short results.",
    )
    r = c.post(f"{VOICE_ROOT}/sessions", json={"model":selected})
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


@pytest.mark.parametrize("options", [{"delegation_type":"responses"}, {"delegation_model":"sonnet"}, {"delegation_effort":"high"}])
def test_retired_delegation_controls_rejected_without_writes(authenticated_client, mock_db, options):
    c = authenticated_client
    before = c.get(VOICE_SETTINGS).json()
    assert c.put(VOICE_SETTINGS, json={"voice_options":options}).status_code == 400
    assert c.get(VOICE_SETTINGS).json() == before


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

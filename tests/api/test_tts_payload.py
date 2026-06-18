"""Tests for schema-driven TTS payload building (uncurated model support)."""

import pytest

from app.api.settings_audio import (
    _build_payload_from_schema,
    _resolve_tts_payload,
    _build_tts_payload,
    TTS_MODELS,
)
from app.services.fal_schema import ModelSchema, ParamField


def _schema(*fields, primary=None):
    return ModelSchema(endpoint_id="fal-ai/x", fields=list(fields), primary_text_field=primary)


def test_schema_payload_routes_text_to_primary_field():
    schema = _schema(
        ParamField(name="gen_text", type="string", is_primary_text=True, required=True),
        ParamField(name="speed", type="number", minimum=0.5, maximum=2.0),
        primary="gen_text",
    )
    payload = _build_payload_from_schema("hello", voice=None, speed=1.5, schema=schema)
    assert payload["gen_text"] == "hello"
    assert payload["speed"] == 1.5


def test_schema_payload_maps_voice_and_clamps_speed():
    schema = _schema(
        ParamField(name="prompt", type="string", is_primary_text=True, required=True),
        ParamField(name="voice_id", type="enum", enum=["a", "b"]),
        ParamField(name="speed", type="number", minimum=0.5, maximum=2.0),
        primary="prompt",
    )
    payload = _build_payload_from_schema("hi", voice="a", speed=9.0, schema=schema)
    assert payload["prompt"] == "hi"
    assert payload["voice_id"] == "a"
    assert payload["speed"] == 2.0  # clamped to schema max


def test_schema_payload_skips_voice_when_no_matching_field():
    schema = _schema(
        ParamField(name="text", type="string", is_primary_text=True, required=True),
        primary="text",
    )
    payload = _build_payload_from_schema("hi", voice="whatever", speed=None, schema=schema)
    assert payload == {"text": "hi"}


def test_schema_payload_explicit_params_override():
    schema = _schema(
        ParamField(name="prompt", type="string", is_primary_text=True, required=True),
        ParamField(name="voice", type="string"),
        primary="prompt",
    )
    payload = _build_payload_from_schema(
        "hi", voice="default", speed=None, schema=schema, extra_params={"voice": "custom", "emotion": "happy"}
    )
    assert payload["voice"] == "custom"
    assert payload["emotion"] == "happy"


def test_no_schema_best_effort_fallback():
    payload = _build_payload_from_schema("hi", voice="Rachel", speed=None, schema=None)
    assert payload == {"text": "hi", "voice": "Rachel"}


@pytest.mark.asyncio
async def test_resolve_uses_curated_shape_for_known_model():
    # F5-TTS is curated and uses 'gen_text' — must not hit the network.
    model_id = "fal-ai/f5-tts"
    assert model_id in TTS_MODELS
    payload = await _resolve_tts_payload(
        model_id, "hello", voice=None, speed=None, model_info=TTS_MODELS[model_id], fal_key=None
    )
    assert payload == _build_tts_payload(model_id, "hello", "", 1.0, TTS_MODELS[model_id])
    assert payload == {"gen_text": "hello"}


@pytest.mark.asyncio
async def test_resolve_curated_merges_extra_params():
    model_id = "fal-ai/kokoro/american-english"
    payload = await _resolve_tts_payload(
        model_id, "hello", voice="af_heart", speed=1.0, model_info=TTS_MODELS[model_id],
        fal_key=None, extra_params={"language": "en"},
    )
    assert payload["voice"] == "af_heart"
    assert payload["language"] == "en"

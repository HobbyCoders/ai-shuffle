"""Unit tests for the fal.ai model schema normalization service."""

from app.services.fal_schema import (
    normalize_openapi,
    _find_input_schema,
    _pick_primary_text,
    ParamField,
)


def _openapi_with_input(properties: dict, required: list[str], schema_name: str = "TextToSpeechInput") -> dict:
    """Build a minimal fal-style OpenAPI doc whose POST body refs an input schema."""
    return {
        "openapi": "3.0.0",
        "paths": {
            "/": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": f"#/components/schemas/{schema_name}"}
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                schema_name: {
                    "type": "object",
                    "title": schema_name,
                    "properties": properties,
                    "required": required,
                }
            }
        },
    }


def test_normalizes_basic_tts_input():
    doc = _openapi_with_input(
        properties={
            "gen_text": {"type": "string", "title": "Text", "description": "Text to speak"},
            "speed": {"type": "number", "default": 1.0, "minimum": 0.5, "maximum": 2.0},
            "remove_silence": {"type": "boolean", "default": True},
        },
        required=["gen_text"],
    )
    schema = normalize_openapi("fal-ai/f5-tts", doc)
    assert schema is not None
    assert schema.primary_text_field == "gen_text"

    by_name = {f.name: f for f in schema.fields}
    assert by_name["gen_text"].is_primary_text is True
    assert by_name["gen_text"].required is True
    assert by_name["speed"].type == "number"
    assert by_name["speed"].minimum == 0.5
    assert by_name["speed"].maximum == 2.0
    assert by_name["speed"].default == 1.0
    assert by_name["remove_silence"].type == "boolean"


def test_enum_via_allof_ref_is_resolved():
    """fal commonly expresses enums as allOf: [{$ref: SomeEnum}]."""
    doc = {
        "openapi": "3.0.0",
        "paths": {
            "/": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Input"}
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "VoiceEnum": {"type": "string", "enum": ["alloy", "echo", "fable"]},
                "Input": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "voice": {"allOf": [{"$ref": "#/components/schemas/VoiceEnum"}], "default": "alloy"},
                    },
                    "required": ["prompt", "voice"],
                },
            }
        },
    }
    schema = normalize_openapi("fal-ai/some-tts", doc)
    assert schema is not None
    voice = next(f for f in schema.fields if f.name == "voice")
    assert voice.type == "enum"
    assert voice.enum == ["alloy", "echo", "fable"]
    assert voice.default == "alloy"
    # 'prompt' should win as the primary text field over 'voice'.
    assert schema.primary_text_field == "prompt"


def test_detects_file_inputs():
    doc = _openapi_with_input(
        properties={
            "prompt": {"type": "string"},
            "image_url": {"type": "string", "format": "uri", "description": "Reference image"},
            "reference_audio_url": {"type": "string"},
        },
        required=["prompt"],
        schema_name="CloneInput",
    )
    schema = normalize_openapi("fal-ai/qwen-3-tts/clone-voice/1.7b", doc)
    assert schema is not None
    by_name = {f.name: f for f in schema.fields}
    assert by_name["image_url"].type == "file"
    assert by_name["image_url"].is_file is True
    assert by_name["reference_audio_url"].type == "file"


def test_audio_enum_field_is_not_treated_as_file():
    """An 'output' enum named with 'audio' must not be misread as a file upload."""
    doc = _openapi_with_input(
        properties={
            "prompt": {"type": "string"},
            "audio_format": {"type": "string", "enum": ["wav", "mp3"], "default": "wav"},
        },
        required=["prompt"],
    )
    schema = normalize_openapi("fal-ai/x", doc)
    assert schema is not None
    fmt = next(f for f in schema.fields if f.name == "audio_format")
    assert fmt.type == "enum"
    assert fmt.is_file is False


def test_primary_text_fallback_to_first_required_string():
    fields = [
        ParamField(name="seed", type="integer", required=True),
        ParamField(name="caption", type="string", required=True),
        ParamField(name="extra", type="string", required=False),
    ]
    assert _pick_primary_text(fields) == "caption"


def test_returns_none_when_no_input_schema():
    assert _find_input_schema({"openapi": "3.0.0", "paths": {}}) is None
    assert normalize_openapi("fal-ai/x", {"openapi": "3.0.0", "paths": {}}) is None


def test_fallback_to_input_suffixed_component_without_paths():
    doc = {
        "openapi": "3.0.0",
        "components": {
            "schemas": {
                "ResultOutput": {"type": "object", "properties": {"audio": {"type": "string"}}},
                "GenerateInput": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        },
    }
    schema = normalize_openapi("fal-ai/x", doc)
    assert schema is not None
    assert schema.primary_text_field == "text"

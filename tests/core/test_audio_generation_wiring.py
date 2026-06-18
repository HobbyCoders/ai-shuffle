"""Wiring tests for the new text-to-audio / audio-to-audio tool groups."""

from app.core.ai_tools import AI_TOOLS, AIToolsConfig
from app.services.fal_catalog import resolve_tool_type, resolve_output_type
from app.mcp.ai_tools_sdk import TOOL_CONFIG_MAP, _is_tool_enabled, get_enabled_tool_names


def test_audio_tools_registered():
    assert "audio_generation" in AI_TOOLS
    assert "audio_to_audio" in AI_TOOLS
    assert AI_TOOLS["audio_generation"]["category"] == "audio"
    # Auto-generated config model exposes both as boolean toggles.
    assert "audio_generation" in AIToolsConfig.model_fields
    assert "audio_to_audio" in AIToolsConfig.model_fields


def test_catalog_splits_audio_tool_types():
    assert resolve_tool_type("text-to-audio") == "audio_generation"
    assert resolve_tool_type("text-to-music") == "audio_generation"
    assert resolve_tool_type("audio-to-audio") == "audio_to_audio"
    # Both still produce audio output.
    assert resolve_output_type("text-to-audio") == "audio"
    assert resolve_output_type("audio-to-audio") == "audio"
    # Speech stays distinct from music generation.
    assert resolve_tool_type("text-to-speech") == "tts"


def test_mcp_tools_mapped_and_enabled():
    assert TOOL_CONFIG_MAP["generateAudio"] == {"config_key": "audio_generation"}
    assert TOOL_CONFIG_MAP["audioToAudio"] == {"config_key": "audio_to_audio"}

    assert _is_tool_enabled("generateAudio", {"audio_generation": True})
    assert not _is_tool_enabled("generateAudio", {"audio_generation": False})
    assert _is_tool_enabled("audioToAudio", {"audio_to_audio": True})

    # The generic executor + discovery turn on for an audio-only config too.
    assert _is_tool_enabled("runFalModel", {"audio_generation": True})
    assert _is_tool_enabled("searchModels", {"audio_to_audio": True})


def test_enabled_tool_names_for_audio_only_config():
    names = get_enabled_tool_names({"audio_generation": True})
    assert "mcp__ai-tools__generateAudio" in names
    assert "mcp__ai-tools__runFalModel" in names
    # Image tools stay off.
    assert "mcp__ai-tools__generateImage" not in names

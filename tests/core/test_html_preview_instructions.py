"""HTML preview guidance belongs to AI Shuffle's editable chat instructions."""
from unittest.mock import patch

import pytest

from app.api import settings as settings_api
from app.core.environment import DEFAULT_SECURITY_INSTRUCTIONS, get_security_instructions


@pytest.mark.parametrize("custom", [None, ""])
def test_default_chat_instructions_include_html_preview_guidance(custom):
    with patch("app.core.environment.database.get_system_setting", return_value=custom) as read:
        instructions = get_security_instructions()
    read.assert_called_once_with("security_instructions")
    assert instructions == DEFAULT_SECURITY_INSTRUCTIONS
    assert instructions.count("### Interactive HTML previews") == 1


@pytest.mark.parametrize("custom", [
    "Keep replies short and link generated files.",
    DEFAULT_SECURITY_INSTRUCTIONS,
    "### Interactive HTML previews\nUse my custom HTML guidance.",
])
def test_custom_chat_instructions_are_used_without_hidden_append(custom):
    with patch("app.core.environment.database.get_system_setting", return_value=custom):
        assert get_security_instructions() == custom


@pytest.mark.asyncio
async def test_settings_save_and_revert_control_the_model_instructions():
    # Exercise the same API used by the chat-instructions editor, including the
    # producing path (save), rather than seeding only the value the prompt reads.
    initial = await settings_api.get_security_instructions(token="test-admin")
    assert initial.current == initial.default == DEFAULT_SECURITY_INSTRUCTIONS
    assert not initial.is_customized
    assert initial.default.count("### Interactive HTML previews") == 1

    custom = "Use only this custom chat guidance."
    await settings_api.update_security_instructions(
        settings_api.SecurityInstructionsUpdateRequest(instructions=custom),
        token="test-admin",
    )
    saved = await settings_api.get_security_instructions(token="test-admin")
    assert saved.current == get_security_instructions() == custom
    assert saved.is_customized
    assert saved.default == DEFAULT_SECURITY_INSTRUCTIONS

    await settings_api.revert_security_instructions(token="test-admin")
    reverted = await settings_api.get_security_instructions(token="test-admin")
    assert reverted.current == get_security_instructions() == initial.default
    assert not reverted.is_customized


def test_default_chat_instructions_describe_html_creation_and_sandbox_limits():
    assert "/api/files/by-path?path=URL_ENCODED_FULL_PATH" in DEFAULT_SECURITY_INSTRUCTIONS
    assert "Write the file successfully before linking it" in DEFAULT_SECURITY_INSTRUCTIONS
    assert "outside code fences" in DEFAULT_SECURITY_INSTRUCTIONS
    assert "Embed CSS and JavaScript" in DEFAULT_SECURITY_INSTRUCTIONS
    assert "2 MiB" in DEFAULT_SECURITY_INSTRUCTIONS
    assert "network requests" in DEFAULT_SECURITY_INSTRUCTIONS
    assert "raw HTML code block" in DEFAULT_SECURITY_INSTRUCTIONS

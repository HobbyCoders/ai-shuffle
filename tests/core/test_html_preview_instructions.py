"""HTML preview guidance must reach default and customized model prompts."""
from unittest.mock import patch

import pytest

from app.core.environment import (
    DEFAULT_SECURITY_INSTRUCTIONS,
    HTML_PREVIEW_INSTRUCTIONS,
    get_security_instructions,
)


@pytest.mark.parametrize("custom", [None, "", "Keep replies short and link generated files."])
def test_preview_contract_is_included_without_rewriting_admin_instructions(custom):
    with patch("app.core.environment.database.get_system_setting", return_value=custom) as read:
        instructions = get_security_instructions()
    read.assert_called_once_with("security_instructions")
    assert instructions == (custom or DEFAULT_SECURITY_INSTRUCTIONS) + "\n" + HTML_PREVIEW_INSTRUCTIONS
    assert instructions.count("### Interactive HTML previews") == 1

def test_preview_contract_describes_the_producer_and_sandbox_limits():
    assert "/api/files/by-path?path=URL_ENCODED_FULL_PATH" in HTML_PREVIEW_INSTRUCTIONS
    assert "Write the file successfully before linking it" in HTML_PREVIEW_INSTRUCTIONS
    assert "outside code fences" in HTML_PREVIEW_INSTRUCTIONS
    assert "Embed CSS and JavaScript" in HTML_PREVIEW_INSTRUCTIONS
    assert "2 MiB" in HTML_PREVIEW_INSTRUCTIONS
    assert "network requests" in HTML_PREVIEW_INSTRUCTIONS
    assert "raw HTML code block" in HTML_PREVIEW_INSTRUCTIONS

"""
Tests for the Claude Code authentication credential injection.

The stored ``anthropic_api_key`` is an AUTH credential for Claude Code (not a
separate generation transport). When set, it is injected as ``ANTHROPIC_API_KEY``
into every Claude Code subprocess and overrides the connected OAuth account
(API-key login wins). When absent, nothing is injected and the OAuth account is
used.

These tests exercise the pure decision/injection helper in isolation — no DB,
no subprocess — by patching the key lookup.
"""

from unittest.mock import patch

import app.core.environment as env_mod
from app.core.environment import inject_claude_code_auth, _build_env_with_ai_tools


class TestInjectClaudeCodeAuth:
    """The always-on ``inject_claude_code_auth`` precedence helper."""

    def test_key_set_injects_anthropic_api_key(self):
        """Key set → env gains ANTHROPIC_API_KEY with the decrypted value."""
        with patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-decrypted"):
            env = inject_claude_code_auth({})
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-decrypted"

    def test_key_absent_injects_nothing(self):
        """Key absent → ANTHROPIC_API_KEY is NOT injected (OAuth account used)."""
        with patch.object(env_mod, "_get_decrypted_api_key", return_value=None):
            env = inject_claude_code_auth({})
        assert "ANTHROPIC_API_KEY" not in env

    def test_key_absent_leaves_existing_env_untouched(self):
        """Key absent → pre-existing env entries are preserved unchanged."""
        with patch.object(env_mod, "_get_decrypted_api_key", return_value=None):
            env = inject_claude_code_auth({"CLAUDECODE": "", "FOO": "bar"})
        assert env == {"CLAUDECODE": "", "FOO": "bar"}

    def test_empty_string_key_treated_as_absent(self):
        """An empty/blank stored value is falsy → treated as no key set."""
        with patch.object(env_mod, "_get_decrypted_api_key", return_value=""):
            env = inject_claude_code_auth({})
        assert "ANTHROPIC_API_KEY" not in env

    def test_mutates_and_returns_same_dict(self):
        """Helper mutates in place and returns the same dict for convenience."""
        original = {}
        with patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-x"):
            returned = inject_claude_code_auth(original)
        assert returned is original
        assert original["ANTHROPIC_API_KEY"] == "sk-ant-x"

    def test_reads_anthropic_api_key_setting_name(self):
        """Reads the credential via the 'anthropic_api_key' setting name."""
        with patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-x") as m:
            inject_claude_code_auth({})
        m.assert_called_once_with("anthropic_api_key")


class TestMainSubprocessEnvAlwaysOnInjection:
    """``_build_env_with_ai_tools`` must inject auth even with NO ai tools.

    This is the always-on path for the main query-engine subprocess: the auth
    var must be present regardless of whether ai_tools are enabled, and BEFORE
    the ai_tools early-returns.
    """

    def test_injected_when_ai_tools_config_is_none(self):
        """No ai_tools at all → ANTHROPIC_API_KEY still injected when key set."""
        with patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-main"):
            env = _build_env_with_ai_tools(base_env=None, ai_tools_config=None)
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-main"
        # Sanity: this is the early-return path (no ai tools enabled).
        assert env["CLAUDECODE"] == ""

    def test_injected_when_ai_tools_all_disabled(self):
        """ai_tools present but all disabled → still injected (always-on)."""
        with patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-main"):
            env = _build_env_with_ai_tools(
                base_env=None,
                ai_tools_config={"image_generation": False, "tts": False},
            )
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-main"

    def test_not_injected_when_key_absent(self):
        """Key absent → ANTHROPIC_API_KEY not present on the main env path."""
        with patch.object(env_mod, "_get_decrypted_api_key", return_value=None):
            env = _build_env_with_ai_tools(base_env=None, ai_tools_config=None)
        assert "ANTHROPIC_API_KEY" not in env

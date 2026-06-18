"""
Tests for apply_1m_context_env — the per-profile 1M context window toggle.

1M-capable models (e.g. claude-opus-4-8) default to the 1M context window on
the CLI side, so a profile with 1M context *disabled* must explicitly send
CLAUDE_CODE_DISABLE_1M_CONTEXT=1. The "[1m]" model suffix is the source of
truth for the toggle:

    [1m] present (enabled)  -> CLAUDE_CODE_DISABLE_1M_CONTEXT=0
    [1m] absent  (disabled) -> CLAUDE_CODE_DISABLE_1M_CONTEXT=1
"""

import pytest

from app.core.environment import apply_1m_context_env

VAR = "CLAUDE_CODE_DISABLE_1M_CONTEXT"


class TestApply1MContextEnv:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4-8",      # 1M-capable base model, toggle OFF
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",     # not 1M-capable at all
            "opus",                 # legacy slug -> claude-opus-4-8 (no [1m])
            "",                     # inherit / unset
            None,                   # no model
        ],
    )
    def test_disabled_sets_1(self, model):
        """No [1m] suffix => 1M explicitly disabled."""
        env = apply_1m_context_env({}, model)
        assert env[VAR] == "1"

    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4-8[1m]",
            "claude-opus-4-7[1m]",
            "claude-sonnet-4-6[1m]",
            "opus[1m]",             # legacy slug -> claude-opus-4-8[1m]
        ],
    )
    def test_enabled_sets_0(self, model):
        """[1m] suffix present => 1M explicitly enabled."""
        env = apply_1m_context_env({}, model)
        assert env[VAR] == "0"

    def test_explicit_value_is_an_escape_hatch(self):
        """A value already in env (e.g. a profile's custom env var) wins."""
        # Toggle says enabled, but the explicit override forces disabled.
        env = apply_1m_context_env({VAR: "1"}, "claude-opus-4-8[1m]")
        assert env[VAR] == "1"

        # ...and vice versa.
        env = apply_1m_context_env({VAR: "0"}, "claude-opus-4-8")
        assert env[VAR] == "0"

    def test_mutates_and_returns_same_dict(self):
        """The helper mutates the passed dict in place and returns it."""
        env = {"FOO": "bar"}
        result = apply_1m_context_env(env, "claude-opus-4-8")
        assert result is env
        assert env["FOO"] == "bar"  # existing keys untouched
        assert env[VAR] == "1"

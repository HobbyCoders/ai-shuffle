"""
Tests for apply_1m_context_env — the per-profile 1M context window toggle.

1M-capable models (e.g. claude-opus-4-8) default to the 1M context window on
the CLI side, so a profile with 1M context *disabled* must explicitly send
CLAUDE_CODE_DISABLE_1M_CONTEXT=1. The var is only meaningful for 1M-capable
models, so it is left unset for everything else. Three-way behavior:

    [1m] present (enabled)                    -> CLAUDE_CODE_DISABLE_1M_CONTEXT=0
    1M-capable base, [1m] absent (disabled)  -> CLAUDE_CODE_DISABLE_1M_CONTEXT=1
    not 1M-capable (haiku, inherit, unknown) -> var left unset
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
            "opus",                 # legacy slug -> claude-opus-4-8 (no [1m])
        ],
    )
    def test_capable_but_disabled_sets_1(self, model):
        """1M-capable base, no [1m] suffix => 1M explicitly disabled."""
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

    @pytest.mark.parametrize(
        "model",
        [
            "claude-haiku-4-5",     # not 1M-capable at all
            "",                     # inherit / unset
            None,                   # no model
            "my-custom-model",      # unknown / custom model not in registry
        ],
    )
    def test_not_capable_leaves_var_unset(self, model):
        """Models that cannot use 1M context don't get the meaningless var."""
        env = apply_1m_context_env({}, model)
        assert VAR not in env

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

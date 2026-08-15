"""
Regression tests: one-shot generation spawns (commit messages, PR descriptions,
profile/subagent generation — the "Default AI" profile) set NO context-window
env on the native Claude path.

History — this file used to pin the opposite. The 1M window was an opt-in
capability selected by appending ``[1m]`` to the model id, and
``build_generation_options`` forced ``CLAUDE_CODE_DISABLE_1M_CONTEXT`` off that
suffix. The current Claude slugs run 1M NATIVELY, so the suffix mechanism is
retired and generation spawns inherit the CLI's own per-model window.

``inject_claude_code_auth`` is patched to a no-op so the test never touches the
credential DB — it is orthogonal to the context-window behaviour under test.
"""

from unittest.mock import patch

import pytest

from app.core.generation_options import build_generation_options

DISABLE_KEY = "CLAUDE_CODE_DISABLE_1M_CONTEXT"
AUTO_COMPACT_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
MAX_CONTEXT_KEY = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"


def _build(profile_config, **kwargs):
    with patch("app.core.environment.inject_claude_code_auth", side_effect=lambda e: e):
        return build_generation_options(profile_config, **kwargs)


@pytest.mark.parametrize("model", ["fable", "opus", "sonnet", "claude-opus-4-8"])
def test_generation_sets_no_context_window_env(model):
    opts = _build({"model": model})
    assert DISABLE_KEY not in opts.env
    assert AUTO_COMPACT_KEY not in opts.env
    assert MAX_CONTEXT_KEY not in opts.env


@pytest.mark.parametrize("model", ["fable[1m]", "opus[1m]", "claude-opus-4-8[1m]"])
def test_generation_legacy_1m_suffix_is_inert(model):
    """A persisted ``[1m]`` id is legacy data — it no longer sets any env."""
    opts = _build({"model": model})
    assert DISABLE_KEY not in opts.env
    assert AUTO_COMPACT_KEY not in opts.env


def test_generation_model_override_still_wins():
    """The effective model is the override, independent of window handling."""
    opts = _build({"model": "fable[1m]"}, model_override="opus")
    assert opts.model == "opus"
    assert DISABLE_KEY not in opts.env


def test_generation_default_model_sets_no_window_env():
    """No configured model → hard fallback (sonnet), still no window pinning."""
    opts = _build({})
    assert DISABLE_KEY not in opts.env


def test_generation_preexisting_auto_compact_window_preserved():
    """A profile-supplied auto-compact window passes through untouched."""
    opts = _build({"model": "fable", "env": {AUTO_COMPACT_KEY: "275000"}})
    assert opts.env[AUTO_COMPACT_KEY] == "275000"
    assert DISABLE_KEY not in opts.env

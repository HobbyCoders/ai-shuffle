"""
Regression tests: one-shot generation spawns (commit messages, PR descriptions,
profile/subagent generation — the "Default AI" profile) get the 1M context
window ONLY when the model was explicitly opted in via the app's ``[1m]`` suffix.

Same root cause as the chat path: the bundled CLI runs natively-1M models
(``fable`` → ``claude-fable-5``) and every ``[1m]`` variant at 1M by default, so
pointing the Default AI profile at ``fable`` would silently run all internal
generation at 1M. ``build_generation_options`` must set
``CLAUDE_CODE_DISABLE_1M_CONTEXT`` accordingly (``"1"`` → 200k, ``"0"`` → 1M).

``inject_claude_code_auth`` is patched to a no-op so the test never touches the
credential DB — it is orthogonal to the context-window behaviour under test.
"""

from unittest.mock import patch

import pytest

from app.core.generation_options import build_generation_options

DISABLE_KEY = "CLAUDE_CODE_DISABLE_1M_CONTEXT"
AUTO_COMPACT_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"


def _build(profile_config, **kwargs):
    with patch("app.core.environment.inject_claude_code_auth", side_effect=lambda e: e):
        return build_generation_options(profile_config, **kwargs)


@pytest.mark.parametrize("model", ["fable", "opus", "sonnet", "claude-opus-4-8"])
def test_generation_non_1m_forces_200k(model):
    opts = _build({"model": model})
    assert opts.env[DISABLE_KEY] == "1"


@pytest.mark.parametrize("model", ["fable[1m]", "opus[1m]", "claude-opus-4-8[1m]"])
def test_generation_1m_suffix_allows_1m(model):
    opts = _build({"model": model})
    assert opts.env[DISABLE_KEY] == "0"


def test_generation_model_override_drives_the_window():
    """The window follows the *effective* model: a non-1M override beats a
    profile that carried `[1m]`."""
    opts = _build({"model": "fable[1m]"}, model_override="opus")
    assert opts.env[DISABLE_KEY] == "1"


def test_generation_default_model_forces_200k():
    """No configured model → hard fallback (sonnet) → 200k, never a silent 1M."""
    opts = _build({})
    assert opts.env[DISABLE_KEY] == "1"


# --- auto-compact window mitigation ---


@pytest.mark.parametrize("model", ["fable", "opus", "sonnet", "claude-opus-4-8"])
def test_generation_disable_path_pins_auto_compact_window(model):
    """Disabling 1M for generation also pins the 200k auto-compact window."""
    opts = _build({"model": model})
    assert opts.env[DISABLE_KEY] == "1"
    assert opts.env[AUTO_COMPACT_KEY] == "200000"


@pytest.mark.parametrize("model", ["fable[1m]", "opus[1m]", "claude-opus-4-8[1m]"])
def test_generation_1m_leaves_auto_compact_unset(model):
    """1M opt-in must not override compaction."""
    opts = _build({"model": model})
    assert opts.env[DISABLE_KEY] == "0"
    assert AUTO_COMPACT_KEY not in opts.env


def test_generation_preexisting_auto_compact_window_preserved():
    """A profile-supplied auto-compact window wins over our 200k default."""
    opts = _build({"model": "fable", "env": {AUTO_COMPACT_KEY: "275000"}})
    assert opts.env[DISABLE_KEY] == "1"
    assert opts.env[AUTO_COMPACT_KEY] == "275000"

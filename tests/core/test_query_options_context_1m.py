"""
Regression tests: profile-driven spawns get the 1M context window ONLY when the
model was explicitly opted in via the app's ``[1m]`` suffix.

The bundled Claude CLI runs natively-1M models (``fable`` → ``claude-fable-5``)
and every ``[1m]`` variant at 1M *by default*; the sole lever that forces the
standard 200k window back on is the ``CLAUDE_CODE_DISABLE_1M_CONTEXT`` env var
(truthy → 200k). So a bare ``fable`` profile silently ran at 1M even though the
user never enabled it. ``build_options_from_profile`` must therefore set that env
var explicitly in both directions:

  - no ``[1m]`` suffix  → ``"1"`` (force 200k)
  - ``[1m]`` suffix      → ``"0"`` (allow the 1M window)

build_options_from_profile makes one unavoidable DB call
(get_security_instructions); we patch that single boundary. A tmp cwd keeps the
build hermetic (avoids the nonexistent-cwd guard).
"""

from unittest.mock import patch

import pytest

import app.core.query_options as qo

DISABLE_KEY = "CLAUDE_CODE_DISABLE_1M_CONTEXT"
AUTO_COMPACT_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"


def _env_for_model(model, tmp_path, base_env=None):
    profile = {
        "id": "t",
        "config": {"model": model, "tools_preset": "claude_code", "cwd": str(tmp_path)},
    }
    if base_env is not None:
        profile["config"]["env"] = base_env
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=profile)
    return opts.env


@pytest.mark.parametrize(
    "model",
    ["fable", "opus", "sonnet", "haiku", "claude-opus-4-8"],
)
def test_non_1m_models_force_200k(model, tmp_path):
    """Bare presets (incl. natively-1M fable) and legacy full IDs disable 1M."""
    env = _env_for_model(model, tmp_path)
    assert env[DISABLE_KEY] == "1"


@pytest.mark.parametrize(
    "model",
    ["fable[1m]", "opus[1m]", "sonnet[1m]", "claude-opus-4-8[1m]"],
)
def test_1m_suffix_allows_1m(model, tmp_path):
    """The explicit `[1m]` opt-in (including the legacy full-ID form) allows 1M."""
    env = _env_for_model(model, tmp_path)
    assert env[DISABLE_KEY] == "0"


def test_inherit_empty_model_defaults_to_200k(tmp_path):
    """An unset/inherit model is the safe 200k default, never a silent 1M."""
    env = _env_for_model("", tmp_path)
    assert env[DISABLE_KEY] == "1"


# --- auto-compact window mitigation (native-1M-off compacts off the wrong window) ---


@pytest.mark.parametrize("model", ["fable", "opus", "sonnet", "claude-opus-4-8", ""])
def test_disable_path_leaves_auto_compact_unset(model, tmp_path):
    """Native sessions no longer pin a hardcoded auto-compact window — the CLI
    recognizes real ``claude-*`` ids and derives the per-model window itself
    (a universal 200k pin is wrong now that models carry different windows)."""
    env = _env_for_model(model, tmp_path)
    assert env[DISABLE_KEY] == "1"
    assert AUTO_COMPACT_KEY not in env


@pytest.mark.parametrize("model", ["fable[1m]", "opus[1m]", "claude-opus-4-8[1m]"])
def test_1m_enabled_leaves_auto_compact_unset(model, tmp_path):
    """1M opt-in must NOT surprise-override compaction — leave the window unset."""
    env = _env_for_model(model, tmp_path)
    assert env[DISABLE_KEY] == "0"
    assert AUTO_COMPACT_KEY not in env


def test_preexisting_auto_compact_window_preserved(tmp_path):
    """A profile/user-supplied window (PR #523 preset) passes through untouched."""
    env = _env_for_model(
        "fable", tmp_path, base_env={AUTO_COMPACT_KEY: "350000"}
    )
    assert env[DISABLE_KEY] == "1"
    assert env[AUTO_COMPACT_KEY] == "350000"

"""
Regression tests: NATIVE (Claude) profile spawns set NO context-window env.

History — this file used to pin the opposite. The 1M window was an opt-in
capability selected by appending ``[1m]`` to the stored model id, and
``build_options_from_profile`` forced ``CLAUDE_CODE_DISABLE_1M_CONTEXT`` in both
directions off that suffix. The current Claude slugs run 1M NATIVELY, so the
suffix mechanism is retired: native legs must leave every window var untouched
and inherit the CLI's own per-model defaults. Pinning ``DISABLE_1M=1`` here
would clamp sonnet/opus/fable back to 200k — the exact bug this now guards.

A persisted ``"opus[1m]"`` must still spawn (it normalizes to ``opus``); it just
does not change the env.

build_options_from_profile makes one unavoidable DB call
(get_security_instructions); we patch that single boundary. A tmp cwd keeps the
build hermetic (avoids the nonexistent-cwd guard).
"""

from unittest.mock import patch

import pytest

import app.core.query_options as qo

DISABLE_KEY = "CLAUDE_CODE_DISABLE_1M_CONTEXT"
AUTO_COMPACT_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
MAX_CONTEXT_KEY = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"


def _build_for_model(model, tmp_path, base_env=None):
    profile = {
        "id": "t",
        "config": {"model": model, "tools_preset": "claude_code", "cwd": str(tmp_path)},
    }
    if base_env is not None:
        profile["config"]["env"] = base_env
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=profile)
    return opts


@pytest.mark.parametrize(
    "model",
    ["fable", "opus", "sonnet", "haiku", "claude-opus-4-8", ""],
)
def test_native_spawn_sets_no_context_window_env(model, tmp_path):
    """Presets, legacy full IDs and inherit/empty alike: no window vars."""
    env = _build_for_model(model, tmp_path).env
    assert DISABLE_KEY not in env
    assert AUTO_COMPACT_KEY not in env
    assert MAX_CONTEXT_KEY not in env


@pytest.mark.parametrize(
    "model",
    ["fable[1m]", "opus[1m]", "sonnet[1m]", "claude-opus-4-8[1m]"],
)
def test_legacy_1m_suffix_is_inert(model, tmp_path):
    """A persisted ``[1m]`` id is legacy data — it no longer sets any env."""
    env = _build_for_model(model, tmp_path).env
    assert DISABLE_KEY not in env
    assert AUTO_COMPACT_KEY not in env


@pytest.mark.parametrize(
    "model,expected",
    [("opus[1m]", "opus"), ("sonnet[1m]", "sonnet"), ("claude-opus-4-8[1m]", "opus")],
)
def test_legacy_1m_model_still_spawns(model, expected, tmp_path):
    """The stored id normalizes onto its base slug rather than being rejected.

    The SDK receives the id verbatim (the registry only canonicalizes for
    lookups), so we assert on what the registry resolves it to.
    """
    from app.core.model_registry import normalize_model

    assert normalize_model(model) == expected
    assert _build_for_model(model, tmp_path).model == model


def test_preexisting_auto_compact_window_preserved(tmp_path):
    """A profile/user-supplied window (PR #523 preset) passes through untouched."""
    env = _build_for_model("fable", tmp_path, base_env={AUTO_COMPACT_KEY: "350000"}).env
    assert env[AUTO_COMPACT_KEY] == "350000"
    assert DISABLE_KEY not in env

@pytest.mark.parametrize("model", ["sonnet", "claude-fable-5-1"])
def test_saved_pin_survives_model_and_effort_change(mock_db, model):
    from app.db import database
    from tests.core.test_worker_context_window import _leg_for_worker

    from app.core import turn_manager
    from app.db import turns

    _leg_for_worker(mock_db, "fable", 200_000)
    database.set_session_run_binding("window-child", model=model, effort="low")
    # A server turn (here a wake) replays an older turn's 1M snapshot. The
    # queue replaces it with the saved pin before the options are built.
    turn_id = turn_manager._submit_blocking("window-child", "wakeup", prompt="later",
        profile_id="role", overrides={"model": model, "effort": "low", "context_window": 1_000_000})
    started = turns.start_turn(turn_id)
    with patch.object(qo, "get_security_instructions", return_value=""):
        options, _ = qo.build_options_from_profile(database.get_profile("role"),
            session_id="window-child", overrides=started["overrides"])
    assert options.aih_context_window == 200_000
    assert options.env[AUTO_COMPACT_KEY] == "200000"

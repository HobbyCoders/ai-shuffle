"""The assistant's home always runs at its model's own default effort.

No effort pick reaches it: not a client override, not an effort stored on the
home row, not a voice call's context and not the live settings endpoint. An
ordinary chat keeps the effort its owner picked.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app.core.query_options as qo
from app.api import sessions_permissions as run_settings
from app.api.voice_context import VoiceContextPatch, _persist_patch
from app.core import assistant_service as service, turn_manager
from app.core.provider.harness_adapter import _effective_effort
from app.core.provider.types import SessionOptions
from app.db import database, turns
from tests.db.test_assistant_store import assistant_fixture  # noqa: F401 — fixture


def _options(session_id, model, tmp_path, overrides=None):
    database.set_session_run_binding(session_id, model=model)
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(
            profile=database.get_profile(database.get_session(session_id)["profile_id"]),
            session_id=session_id,
            overrides={"cwd": str(tmp_path), **(overrides or {})},
        )
    return opts


def test_a_client_effort_override_runs_at_the_model_default(assistant_fixture, tmp_path):
    opts = _options(assistant_fixture["id"], "opus", tmp_path, {"effort": "low"})
    assert opts.aih_assistant_home
    assert opts.effort == "high"


def test_an_effort_stored_on_the_home_runs_at_the_model_default(assistant_fixture, tmp_path):
    home = assistant_fixture["id"]
    database.set_session_run_binding(home, effort="max")
    assert _options(home, "opus", tmp_path).effort == "high"


def test_a_model_without_effort_levels_gets_none(assistant_fixture, tmp_path):
    home = assistant_fixture["id"]
    database.set_session_run_binding(home, effort="max")
    assert _options(home, "haiku", tmp_path, {"effort": "high"}).effort is None


def test_an_ordinary_chat_keeps_the_picked_effort(assistant_fixture, tmp_path):
    database.create_session("ordinary", service.AGENT_ID, is_active=True)
    assert _options("ordinary", "opus", tmp_path, {"effort": "low"}).effort == "low"
    database.set_session_run_binding("ordinary", effort="max")
    assert _options("ordinary", "opus", tmp_path).effort == "max"


def test_the_adapter_resolves_the_home_to_the_model_default():
    home = SessionOptions(model="opus", extra={"assistant_home": True})
    assert _effective_effort(home, "low", "anthropic") == "high"
    assert _effective_effort(home, None, "anthropic") == "high"
    assert _effective_effort(SessionOptions(model="haiku", extra={"assistant_home": True}), "high", "anthropic") is None
    assert _effective_effort(SessionOptions(model="opus"), "low", "anthropic") == "low"


@pytest.mark.parametrize("source,payload", [
    ("user", None),
    ("user", {"voice_delegation": True}),  # A voice call handing work to the home.
    ("wakeup", None),                      # Replays the scheduling turn's overrides.
])
def test_turns_into_the_home_neither_carry_nor_store_an_effort(assistant_fixture, source, payload):
    home = assistant_fixture["id"]
    turn_id = turn_manager.submit(home, source, prompt="hi", profile_id=service.AGENT_ID,
                                  overrides={"model": "opus", "effort": "low"}, payload=payload, no_kick=True)
    assert "effort" not in turns.get_turn(turn_id)["overrides"]
    assert database.get_session(home)["effort"] is None


def test_an_ordinary_turn_still_stores_its_effort(assistant_fixture):
    database.create_session("ordinary", service.AGENT_ID, is_active=True)
    turn_id = turn_manager.submit("ordinary", "user", prompt="hi", profile_id=service.AGENT_ID,
                                  overrides={"effort": "low"}, no_kick=True)
    assert turns.get_turn(turn_id)["overrides"]["effort"] == "low"
    assert database.get_session("ordinary")["effort"] == "low"


def test_opening_the_home_clears_a_stored_effort(assistant_fixture):
    home = assistant_fixture["id"]
    database.set_session_run_binding(home, effort="max")
    assert service.open_home()["effort"] is None
    assert database.get_session(home)["effort"] is None


def test_a_voice_call_in_the_home_stores_no_effort(assistant_fixture):
    home = assistant_fixture["id"]
    _persist_patch(home, VoiceContextPatch(effort="low", fast=True))
    row = database.get_session(home)
    assert row["effort"] is None and row["fast"]


@pytest.mark.asyncio
async def test_live_settings_refuse_an_effort_for_the_home(assistant_fixture, monkeypatch):
    home = assistant_fixture["id"]
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda sid: None)
    monkeypatch.setattr(run_settings.control, "_require_owned", lambda sid: database.get_session(sid))
    with pytest.raises(ValueError, match="default effort"):
        await run_settings.apply_run_settings(home, run_settings.RunSettingsUpdate(effort="low"))
    # Fast mode still applies, and never stores an effort on the home.
    database.set_session_run_binding(home, effort="max")
    save = MagicMock()
    monkeypatch.setattr(database, "set_session_run_binding", save)
    await run_settings.apply_run_settings(home, run_settings.RunSettingsUpdate(fast=False))
    save.assert_called_once_with(home, effort=None, fast=False, clear_effort=True)

"""The saved Anthropic API key as the native "Claude API" provider.

Claude API and the Claude subscription are separate options: the key runs on
fluid's native Anthropic client, the subscription on the Claude Code CLI.
"""

from types import SimpleNamespace

import pytest

from app.core import claude_api_provider as cap
from app.core import claude_auth_mode
from app.core.model_registry import API_MODEL_IDS
from app.db import database

KEY = "sk-ant-test-key"


@pytest.fixture
def key(mock_db, monkeypatch, _api_unit_credentials):
    """An active saved key with credential storage unlocked; the mode is set per test."""
    monkeypatch.setattr(claude_auth_mode, "stored_api_key", lambda: KEY)
    return KEY


def test_no_active_key_means_no_row(mock_db, monkeypatch):
    monkeypatch.setattr(claude_auth_mode, "stored_api_key", lambda: None)
    claude_auth_mode.set_mode("api_key")
    assert cap.virtual_row() is None
    assert database.get_model_provider(cap.CLAUDE_API_PROVIDER_ID) is None
    assert all(r["id"] != cap.CLAUDE_API_PROVIDER_ID for r in database.get_enabled_model_providers())


def test_row_is_an_api_key_anthropic_provider_without_its_secret(key):
    claude_auth_mode.set_mode("subscription")
    row = database.get_model_provider(cap.CLAUDE_API_PROVIDER_ID)
    assert row["provider_type"] == "anthropic" and row["auth_mode"] == "api_key"
    assert row["base_url"] == "https://api.anthropic.com"
    assert row["enabled"] is True and "auth_token" not in row
    assert row["default_model"] == API_MODEL_IDS["sonnet"]
    # Listed for pickers and usable-provider checks, never in the management list.
    assert any(r["id"] == cap.CLAUDE_API_PROVIDER_ID for r in database.get_enabled_model_providers())
    assert any(r["id"] == cap.CLAUDE_API_PROVIDER_ID for r in database.list_usable_model_providers())
    assert all(r["id"] != cap.CLAUDE_API_PROVIDER_ID for r in database.get_all_model_providers())


def test_off_mode_hides_the_row(key):
    claude_auth_mode.set_mode("off")
    assert cap.virtual_row() is None


def test_models_follow_the_active_keys_catalog_only(key):
    claude_auth_mode.set_mode("subscription")
    cap.remember_models("sk-ant-other", [{"id": "claude-other", "label": "Other"}])
    assert all(m["id"] != "claude-other" for m in cap.virtual_row()["models_json"])
    cap.remember_models(KEY, [{"id": "claude-live-1", "label": "Live One"}])
    assert cap.virtual_row()["models_json"] == [{"id": "claude-live-1", "label": "Live One"}]


@pytest.mark.parametrize("mode,model,expected", [
    ("api_key", "sonnet", (cap.CLAUDE_API_PROVIDER_ID, API_MODEL_IDS["sonnet"])),
    ("api_key", "claude-opus-5", (cap.CLAUDE_API_PROVIDER_ID, "claude-opus-5")),
    ("api_key", "gpt-5", None),
    ("subscription", "sonnet", None),
    ("cloud", "sonnet", None),
    ("off", "sonnet", None),
])
def test_only_api_key_mode_moves_claude_picks_onto_the_claude_api(key, mode, model, expected):
    claude_auth_mode.set_mode(mode)
    assert cap.native_claude_rebind(model) == expected


def test_bound_turns_take_the_native_anthropic_leg(key):
    from app.core.provider.harness_adapter import provider_kind_for_sdk_options

    claude_auth_mode.set_mode("subscription")
    bound = SimpleNamespace(model=API_MODEL_IDS["sonnet"], aih_provider_id=cap.CLAUDE_API_PROVIDER_ID)
    assert provider_kind_for_sdk_options(bound) == "anthropic"
    # The subscription (no provider) stays on the Claude Code CLI.
    assert provider_kind_for_sdk_options(SimpleNamespace(model="sonnet", aih_provider_id=None)) == "delegated"


@pytest.mark.asyncio
async def test_credentials_are_the_saved_key(key):
    from app.core.provider.fluid_credentials import resolve_provider_credentials

    claude_auth_mode.set_mode("api_key")
    creds = await resolve_provider_credentials("anthropic", cap.CLAUDE_API_PROVIDER_ID)
    assert creds.api_key == KEY
    assert creds.base_url == "https://api.anthropic.com"


def test_one_shot_generation_in_api_key_mode_runs_natively(key):
    from app.core.generation_options import build_generation_options

    claude_auth_mode.set_mode("api_key")
    options = build_generation_options({"model": "sonnet"})
    assert options.aih_provider_id == cap.CLAUDE_API_PROVIDER_ID
    assert options.aih_provider_type == "anthropic"
    assert options.model == API_MODEL_IDS["sonnet"]
    # The standard provider overlay: the Anthropic fallback is blocked.
    assert (options.env or {}).get("ANTHROPIC_API_KEY") == ""


def test_one_shot_generation_on_the_subscription_stays_delegated(key):
    from app.core.generation_options import build_generation_options

    claude_auth_mode.set_mode("subscription")
    options = build_generation_options({"model": "sonnet"})
    assert options.aih_provider_id is None

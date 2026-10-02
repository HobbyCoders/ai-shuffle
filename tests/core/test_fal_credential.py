"""The fal.ai on/off switch and the key every fal.ai reader goes through."""
from unittest.mock import patch

import pytest

from app.api.auth import hash_api_key
from app.api.settings_crypto import set_encrypted_api_key
from app.core import encryption, fal_credential
from app.core.environment import _build_env_with_ai_tools
from app.db import database

OWNER_KEY = "fal-owner-0123456789abcdefghijklmnop"
USER_KEY = "fal-user-0123456789abcdefghijklmnopq"


@pytest.fixture
def owner_key(mock_db, _api_unit_credentials):
    set_encrypted_api_key("fal_api_key", OWNER_KEY)
    return OWNER_KEY


def test_unset_reads_as_on(mock_db):
    assert database.get_system_setting(fal_credential.KEY) is None
    assert fal_credential.is_enabled() is True


def test_set_enabled_round_trips(mock_db):
    assert fal_credential.set_enabled(False) is False
    assert fal_credential.is_enabled() is False
    assert database.get_system_setting(fal_credential.KEY) == "false"
    assert fal_credential.set_enabled(True) is True
    assert fal_credential.is_enabled() is True


def test_owner_key_is_returned_while_on_and_withheld_while_off(owner_key):
    assert fal_credential.api_key() == OWNER_KEY
    assert fal_credential.api_key_with_policy() == (OWNER_KEY, None)
    fal_credential.set_enabled(False)
    assert fal_credential.api_key() is None
    assert fal_credential.api_key_with_policy() == (None, fal_credential.OFF_POLICY)
    # Off keeps the key stored.
    assert fal_credential.key_stored() is True


def test_api_user_key_follows_its_policy_and_the_switch(owner_key):
    database.create_api_user("fal-user", "Fal User", hash_api_key("aih_fal_user"))
    database.set_user_credential_policy("fal-user", "fal_api_key", "user_provided")
    database.set_user_credential("fal-user", "fal_api_key", encryption.encrypt_value(USER_KEY))
    assert fal_credential.api_key_with_policy("fal-user") == (USER_KEY, "user_provided")
    fal_credential.set_enabled(False)
    assert fal_credential.api_key("fal-user") is None
    assert fal_credential.api_key_with_policy("fal-user") == (None, fal_credential.OFF_POLICY)


def test_ai_tools_env_fallback_is_blocked_while_off(mock_db, monkeypatch):
    from app.mcp import ai_tools_sdk

    monkeypatch.setenv("FAL_KEY", "fal-from-env")
    assert ai_tools_sdk._get_fal_api_key() == "fal-from-env"
    fal_credential.set_enabled(False)
    assert ai_tools_sdk._get_fal_api_key() is None
    monkeypatch.delenv("FAL_KEY")
    monkeypatch.setenv("FAL_API_KEY", "fal-from-env-2")
    assert ai_tools_sdk._get_fal_api_key() is None


def test_stored_key_wins_over_env_while_on(owner_key, monkeypatch):
    from app.mcp import ai_tools_sdk

    monkeypatch.setenv("FAL_KEY", "fal-from-env")
    assert ai_tools_sdk._get_fal_api_key() == OWNER_KEY


def test_agent_env_gets_no_fal_key_while_off(owner_key):
    tools = {"image_generation": True}
    assert _build_env_with_ai_tools(base_env=None, ai_tools_config=tools)["FAL_KEY"] == OWNER_KEY
    fal_credential.set_enabled(False)
    assert "FAL_KEY" not in _build_env_with_ai_tools(base_env=None, ai_tools_config=tools)


def test_ai_tools_worker_env_blanks_inherited_keys_while_off(mock_db, tmp_path):
    from app.mcp import ai_tools_sdk

    captured = {}

    class FakeWorker:
        def __init__(self, worker_script, env=None, cwd=None):
            captured["env"] = env

    fal_credential.set_enabled(False)
    with patch.object(ai_tools_sdk, "NodeWorker", FakeWorker):
        ai_tools_sdk.create_ai_tools_server({"image_generation": True}, str(tmp_path), tool_env={"OTHER": "1"})
    assert captured["env"] == {"OTHER": "1", "FAL_KEY": "", "FAL_API_KEY": ""}


def test_set_enabled_announces_a_settings_change(mock_db):
    from app.core.global_events import global_events

    with patch.object(global_events, "publish") as publish:
        fal_credential.set_enabled(False)
    assert [call.args[0]["resources"] for call in publish.call_args_list]
    assert all("settings" in call.args[0]["resources"] for call in publish.call_args_list)


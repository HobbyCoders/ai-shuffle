"""Saved Anthropic API keys: real encryption, no provider or CLI access."""
import pytest
from cryptography.fernet import Fernet

from app.core import claude_accounts as accounts, claude_auth_mode, encryption
from app.network.policy import is_shared_setting_key


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    values = {}
    monkeypatch.setattr(accounts.database, "get_system_setting", values.get)
    monkeypatch.setattr(accounts.database, "set_system_setting", values.__setitem__)
    monkeypatch.setattr(accounts.database, "delete_system_setting", lambda key: values.pop(key, None) is not None)
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())
    yield values


def test_keys_save_activate_rename_delete_and_legacy_migration(isolated):
    old = "example-anthropic-secret-key-1111"
    isolated[accounts.API_SETTING] = encryption.encrypt_value(old)
    legacy = accounts.accounts_payload()["api_key"][0]["id"]
    second = accounts.add_api_key("example-anthropic-secret-key-2222", "Work", False)
    assert claude_auth_mode.stored_api_key() == old
    accounts.change_account("api_key", second, "activate")
    assert claude_auth_mode.stored_api_key().endswith("2222")
    accounts.change_account("api_key", second, "rename", "Office")
    assert accounts.accounts_payload()["api_key"][1]["label"] == "Office"
    accounts.change_account("api_key", legacy, "delete")
    assert claude_auth_mode.stored_api_key().endswith("2222")
    accounts.change_account("api_key", second, "delete")
    assert claude_auth_mode.stored_api_key() is None
    assert accounts.accounts_payload()["api_key"] == []
    assert all(encryption.is_encrypted(value) for value in isolated.values())


def test_key_list_follows_scalar_sync_and_injection_uses_active(isolated):
    from app.api.settings_crypto import get_decrypted_api_key
    from app.core.environment import _get_decrypted_api_key, inject_claude_code_auth
    first = accounts.add_api_key("example-anthropic-secret-key-1111")
    second = accounts.add_api_key("example-anthropic-secret-key-2222")
    isolated[claude_auth_mode.setting_key()] = "api_key"
    assert get_decrypted_api_key(accounts.API_SETTING).endswith("2222")
    assert _get_decrypted_api_key(accounts.API_SETTING).endswith("2222")
    assert inject_claude_code_auth({})["ANTHROPIC_API_KEY"].endswith("2222")
    replica = isolated[accounts.API_SETTING]
    accounts.change_account("api_key", first, "activate")
    isolated[accounts.API_SETTING] = replica
    assert accounts.active_api_key().endswith("2222")
    assert len(accounts.accounts_payload()["api_key"]) == 2
    assert is_shared_setting_key(accounts.API_SETTING)
    assert first != second
    # The scalar arriving first still shows the key actually used by the CLI.
    isolated.pop(accounts.API_LIST_SETTING)
    listing = accounts.accounts_payload()["api_key"]
    assert len(listing) == 1 and listing[0]["active"] and listing[0]["last4"] == "2222"
    assert encryption.decrypt_value(isolated[accounts.API_SETTING]).endswith("2222")
    assert is_shared_setting_key(accounts.API_LIST_SETTING)


def test_dedupe_masks_and_locked_storage(isolated, monkeypatch):
    key = "example-anthropic-secret-key-1111"
    first = accounts.add_api_key(key, "First")
    assert accounts.add_api_key(key, "Updated") == first
    assert len(accounts.accounts_payload()["api_key"]) == 1
    assert key not in str(accounts.accounts_payload())
    monkeypatch.setattr(encryption, "_encryption_key", None)
    with pytest.raises(accounts.AccountError, match="Unlock"):
        accounts.add_api_key("never-save-plaintext")
    assert accounts.accounts_payload()["storage_locked"] is True
    assert "never-save" not in str(isolated)


def test_legacy_deactivate_preserves_saved_keys(isolated):
    key = "example-anthropic-secret-key-1111"
    isolated[accounts.API_SETTING] = encryption.encrypt_value(key)
    accounts.deactivate_api_key()
    assert accounts.API_SETTING not in isolated
    assert accounts.active_api_key() is None
    assert len(accounts.accounts_payload()["api_key"]) == 1
    assert not accounts.accounts_payload()["api_key"][0]["active"]


def test_subscription_management_is_gated_on_macos(monkeypatch):
    """macOS reports saved subscription sign-ins as unavailable, neutrally."""
    from app.core import claude_cli_home
    monkeypatch.setattr(claude_cli_home.sys, "platform", "darwin")
    support = accounts.subscription_support()
    assert support["supported"] is False and "Mac" in support["reason"]
    assert accounts.accounts_payload()["subscription"] == []
    with pytest.raises(accounts.AccountError) as exc:
        accounts.save_current_login()
    assert exc.value.code == "unsupported" and exc.value.status == 501
    with pytest.raises(accounts.AccountError):
        accounts.change_account("subscription", "anything", "activate")


def test_subscription_list_shows_while_key_storage_is_locked(monkeypatch):
    from app.core import claude_cli_home, claude_subscriptions
    monkeypatch.setattr(claude_cli_home.sys, "platform", "linux")
    monkeypatch.setattr(claude_subscriptions, "payload", lambda: {
        "subscription": [{"id": "x"}], "default_in_use": True, "login_pending": False,
        "login": None, "recovery_required": False})
    monkeypatch.setattr(encryption, "_encryption_key", None)
    # Keys need unlocked storage; subscription sign-ins are plain worker-local data.
    payload = accounts.accounts_payload()
    assert payload["storage_locked"] is True
    assert payload["subscription"] == [{"id": "x"}]
    assert payload["subscription_support"]["supported"] is True

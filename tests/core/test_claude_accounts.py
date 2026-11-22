"""Saved Anthropic API keys: real encryption, no provider or CLI access."""
import pytest
from cryptography.fernet import Fernet

from app.core import claude_accounts as accounts, claude_auth_mode, encryption
from app.db import database as db
from app.db.settings import get_system_setting_snapshots
from app.network.policy import is_shared_setting_key


@pytest.fixture(autouse=True)
def unlocked_storage(mock_db, monkeypatch):
    # Use the suite's isolated DB for both setting values and write revisions.
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())


def test_keys_save_activate_rename_delete_and_legacy_migration():
    old = "example-anthropic-secret-key-1111"
    db.set_system_setting(accounts.API_SETTING, encryption.encrypt_value(old))
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
    assert encryption.is_encrypted(db.get_system_setting(accounts.API_LIST_SETTING))


def test_key_list_follows_scalar_sync_and_the_active_key_is_native():
    from app.api.settings_crypto import get_decrypted_api_key
    from app.core.environment import _get_decrypted_api_key, inject_claude_code_auth
    accounts.add_api_key("example-anthropic-secret-key-1111")
    accounts.add_api_key("example-anthropic-secret-key-2222")
    db.set_system_setting(claude_auth_mode.setting_key(), "api_key")
    assert db.get_system_setting(claude_auth_mode.SELECTION_SETTING) is None
    assert get_decrypted_api_key(accounts.API_SETTING).endswith("2222")
    assert _get_decrypted_api_key(accounts.API_SETTING).endswith("2222")
    from app.core import claude_api_provider
    # The active key runs natively; the Claude Code CLI never receives it.
    assert "ANTHROPIC_API_KEY" not in inject_claude_code_auth({})
    assert claude_api_provider._active_key().endswith("2222")
    replica = db.get_system_setting(accounts.API_SETTING)
    rows = {row["last4"]: row for row in accounts.accounts_payload()["api_key"]}
    assert rows.keys() == {"1111", "2222"}
    first, second = rows["1111"], rows["2222"]
    assert first["id"] != second["id"] and not first["active"] and second["active"]
    accounts.change_account("api_key", first["id"], "activate")
    assert claude_api_provider._active_key().endswith("1111")
    db.set_system_setting(accounts.API_SETTING, replica)
    assert accounts.active_api_key().endswith("2222")
    assert "ANTHROPIC_API_KEY" not in inject_claude_code_auth({})
    assert [(row["id"], row["active"]) for row in accounts.accounts_payload()["api_key"]] == [
        (first["id"], False), (second["id"], True),
    ]
    assert is_shared_setting_key(accounts.API_SETTING)
    # The scalar arriving first still shows the key actually in use.
    db.delete_system_setting(accounts.API_LIST_SETTING)
    listing = accounts.accounts_payload()["api_key"]
    assert len(listing) == 1 and listing[0]["active"] and listing[0]["last4"] == "2222"
    assert db.get_system_setting(accounts.API_SETTING) == replica
    assert encryption.decrypt_value(replica).endswith("2222")
    assert is_shared_setting_key(accounts.API_LIST_SETTING)


def test_dedupe_masks_and_locked_storage(monkeypatch):
    key = "example-anthropic-secret-key-1111"
    first = accounts.add_api_key(key, "First")
    assert accounts.add_api_key(key, "Updated") == first
    assert len(accounts.accounts_payload()["api_key"]) == 1
    assert key not in str(accounts.accounts_payload())
    before = get_system_setting_snapshots(accounts.API_SETTING, accounts.API_LIST_SETTING)
    monkeypatch.setattr(encryption, "_encryption_key", None)
    with pytest.raises(accounts.AccountError, match="Unlock"):
        accounts.add_api_key("never-save-plaintext")
    assert accounts.accounts_payload()["storage_locked"] is True
    assert get_system_setting_snapshots(accounts.API_SETTING, accounts.API_LIST_SETTING) == before


def test_legacy_deactivate_preserves_saved_keys():
    key = "example-anthropic-secret-key-1111"
    db.set_system_setting(accounts.API_SETTING, encryption.encrypt_value(key))
    accounts.deactivate_api_key()
    assert db.get_system_setting(accounts.API_SETTING) is None
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


def test_a_local_cloud_override_keeps_the_owners_key_pin():
    """A worker set to Cloud locally still bills the key the owner pinned on
    the native Claude API provider, never a stale scalar from a peer."""
    from app.core import claude_api_provider

    claude_auth_mode.set_mode("subscription")
    accounts.add_api_key("example-anthropic-secret-key-1111")
    accounts.add_api_key("example-anthropic-secret-key-2222")
    assert claude_auth_mode.shared_selection()["key_id"]
    # A peer's older save lands its scalar after the owner's pin.
    db.set_system_setting(accounts.API_SETTING, encryption.encrypt_value("example-anthropic-secret-key-1111"))
    db.set_system_setting(claude_auth_mode.setting_key(), "cloud")
    assert claude_auth_mode.runtime_choice() == ("cloud", None)
    assert claude_api_provider._active_key().endswith("2222")
    active = [row["last4"] for row in accounts.accounts_payload()["api_key"] if row["active"]]
    assert active == ["2222"]
    # Settings names the same key under a Cloud override too.
    assert claude_auth_mode.summary(cli={})["api_key"]["last4"] == "2222"


def test_settings_shows_no_key_while_the_pinned_key_has_not_synced():
    import json

    db.set_system_setting(accounts.API_SETTING, encryption.encrypt_value("example-anthropic-secret-key-1111"))
    db.set_system_setting(claude_auth_mode.SELECTION_SETTING, json.dumps(
        {"mode": "subscription", "subscription": None, "key_id": "not-synced-yet"}))
    assert claude_auth_mode.summary(cli={})["api_key"] == {"set": False, "last4": None}


def test_changing_the_pinned_key_does_not_reconnect_cli_sessions():
    import json

    def fingerprint(key_id):
        db.set_system_setting(claude_auth_mode.SELECTION_SETTING, json.dumps(
            {"mode": "api_key", "subscription": None, "key_id": key_id}))
        return claude_auth_mode.spawn_fingerprint()

    assert fingerprint("key-a") == fingerprint("key-b")

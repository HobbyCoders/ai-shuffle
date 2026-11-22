"""GET/PUT /settings/claude-auth: the explicit Claude Code auth mode."""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core import claude_auth_mode, claude_cli_status, claude_subscriptions, encryption
from app.db import database as db

ENDPOINT = "/api/v1/settings/claude-auth"


def _native_key():
    """The key the native Claude API provider runs on. The CLI never gets one."""
    from app.core import claude_api_provider

    return claude_api_provider._active_key()


def _cli(signed_in: bool) -> claude_cli_status.ClaudeCliStatus:
    return claude_cli_status.ClaudeCliStatus(
        installed=True, signed_in=signed_in,
        email="me@example.com" if signed_in else None, plan="max" if signed_in else None,
    )


def _probe(signed_in: bool):
    return patch.object(claude_cli_status, "get_status_async", AsyncMock(return_value=_cli(signed_in)))


def test_reports_mode_account_and_key_last_four(authenticated_client):
    db.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-ant-api03-secret-ABCD"))
    claude_auth_mode.set_mode("subscription")
    with _probe(True):
        body = authenticated_client.get(ENDPOINT).json()
    assert body["mode"] == "subscription" and body["explicit"] is True
    assert body["cli"]["signed_in"] is True and body["cli"]["email"] == "me@example.com"
    assert body["api_key"] == {"set": True, "last4": "ABCD"}
    assert "secret" not in str(body)


def test_put_switches_mode_and_integrations_follow(authenticated_client):
    db.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-ant-api03-secret-ABCD"))
    claude_auth_mode.set_mode("subscription")
    integrations = authenticated_client.get("/api/v1/settings/integrations").json()
    assert integrations["claude_auth_mode"] == "subscription"
    assert integrations["claude_code_auth_method"] == "account"
    with _probe(True):
        # The removed API key mode is refused and changes nothing.
        response = authenticated_client.put(ENDPOINT, json={"mode": "api_key"})
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == claude_auth_mode.API_KEY_MODE_REMOVED
        assert claude_auth_mode.stored_mode() == "subscription"
        response = authenticated_client.put(ENDPOINT, json={"mode": "off"})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "off"
    assert [m["value"] for m in response.json()["modes"]] == ["subscription", "cloud", "off"]
    integrations = authenticated_client.get("/api/v1/settings/integrations").json()
    assert integrations["claude_code_auth_method"] == "off"


def test_a_legacy_api_key_mode_reads_as_the_subscription(authenticated_client):
    """Older workers may still store or sync the removed API key mode: it is
    the subscription for the CLI and Settings, and its key pin still names the
    key the Claude API provider bills."""
    from app.core import claude_accounts

    key_id = claude_accounts.add_api_key("fixture-legacy-pin-ABCD")
    db.set_system_setting(claude_auth_mode.setting_key(), claude_auth_mode.LEGACY_MODE_API_KEY)
    db.set_system_setting(claude_auth_mode.SELECTION_SETTING, json.dumps(
        {"mode": "api_key", "subscription": None, "key_id": key_id}))
    with _probe(True):
        body = authenticated_client.get(ENDPOINT).json()
    assert body["mode"] == "subscription"
    assert body["api_key"] == {"set": True, "last4": "ABCD"}
    assert "api_key_confirmation" not in body
    integrations = authenticated_client.get("/api/v1/settings/integrations").json()
    assert integrations["claude_code_auth_method"] == "account"
    assert _native_key() == "fixture-legacy-pin-ABCD"
    # Choosing Subscription republishes the choice and keeps the pin.
    with _probe(True):
        assert authenticated_client.put(ENDPOINT, json={"mode": "subscription"}).status_code == 200
    assert claude_auth_mode.shared_selection()["mode"] == "subscription"
    assert claude_auth_mode.shared_selection()["key_id"] == key_id
    assert _native_key() == "fixture-legacy-pin-ABCD"


def test_owner_mode_selection_is_shared_and_does_not_fall_back(authenticated_client, monkeypatch):
    from app.network.policy import is_shared_setting_key

    assert is_shared_setting_key(claude_auth_mode.SELECTION_SETTING)
    assert not is_shared_setting_key(claude_auth_mode.setting_key())
    # Simulate the peer's legacy startup choice and a configured host gateway.
    db.set_system_setting(claude_auth_mode.setting_key(), "subscription")
    with _probe(True):
        assert authenticated_client.put(ENDPOINT, json={"mode": "subscription"}).status_code == 200
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://fixture.invalid")
    assert claude_auth_mode.selected_mode() == "subscription"
    assert _native_key() is None


def test_key_pin_holds_across_partial_sync_rotation_and_delete(authenticated_client):
    from app.core import claude_accounts

    alpha, beta, gamma = "fixture-alpha-1111", "fixture-beta-2222", "fixture-gamma-3333"
    claude_auth_mode.set_mode("subscription")
    alpha_id = claude_accounts.add_api_key(alpha)
    old_keys = db.get_system_setting(claude_accounts.API_LIST_SETTING)
    with patch("app.api.settings.validate_anthropic_key", new_callable=AsyncMock):
        response = authenticated_client.post("/api/v1/settings/integrations/anthropic", json={
            "api_key": beta,
        })
    assert response.status_code == 200, response.text
    choice = db.get_system_setting(claude_auth_mode.SELECTION_SETTING)
    beta_id = json.loads(choice)["key_id"]
    assert all(key not in choice for key in (alpha, beta, gamma))
    new_keys = db.get_system_setting(claude_accounts.API_LIST_SETTING)
    # An offline peer's scalar wins independently; then the decision arrives
    # before the encrypted list. Neither intermediate state may launch Alpha.
    db.set_system_setting(claude_accounts.API_SETTING, encryption.encrypt_value(alpha))
    db.set_system_setting(claude_accounts.API_LIST_SETTING, old_keys)
    assert _native_key() is None
    # Settings' key detail agrees: no key to show (or remove) until Beta syncs.
    integrations = authenticated_client.get("/api/v1/settings/integrations").json()
    assert integrations["anthropic_api_key_set"] is False
    db.set_system_setting(claude_accounts.API_LIST_SETTING, new_keys)
    assert _native_key() == beta
    integrations = authenticated_client.get("/api/v1/settings/integrations").json()
    assert integrations["anthropic_api_key_set"] is True
    with patch.object(claude_auth_mode, "setting_key", lambda: "claude_auth_mode:fixture-off-peer"):
        claude_auth_mode.set_mode("off")
        claude_accounts.change_account("api_key", alpha_id, "delete")
        # The owner's pin still says Beta on this Off worker, so the stale
        # Alpha scalar is replaced by Beta, never left holding a deleted key.
        assert claude_accounts.active_api_key() == beta
        assert db.get_system_setting(claude_auth_mode.SELECTION_SETTING) == choice
    assert _native_key() == beta
    gamma_id = claude_accounts.add_api_key(gamma)
    assert gamma_id != beta_id
    assert _native_key() == gamma
    claude_accounts.change_account("api_key", gamma_id, "delete")
    assert _native_key() is None


def test_off_and_cloud_do_not_write_owner_selection(authenticated_client, monkeypatch):
    claude_auth_mode.set_mode("subscription")
    before = db.get_system_setting(claude_auth_mode.SELECTION_SETTING)
    for mode in ("cloud", "off"):
        with monkeypatch.context() as m:
            # A second worker's explicit overrides must not affect the first.
            m.setattr(claude_auth_mode, "setting_key", lambda: "claude_auth_mode:fixture-peer")
            assert claude_auth_mode.set_mode(mode) == mode
            assert claude_auth_mode.current_mode() == mode
        assert claude_auth_mode.current_mode() == "subscription"
        assert db.get_system_setting(claude_auth_mode.SELECTION_SETTING) == before


@pytest.mark.parametrize("encrypted", [False, True])
def test_a_legacy_scalar_key_runs_without_a_pin_or_a_migration(authenticated_client, monkeypatch, encrypted):
    from app.core import claude_accounts

    legacy_key = "fixture-legacy-secret-ABCD"
    value = encryption.encrypt_value(legacy_key) if encrypted else legacy_key
    db.set_system_setting(claude_accounts.API_SETTING, value)
    db.set_system_setting(claude_auth_mode.setting_key(), claude_auth_mode.LEGACY_MODE_API_KEY)

    with monkeypatch.context() as locked:
        locked.setattr(encryption, "is_encryption_ready", lambda: False)
        # Subscription choices do not require access to unrelated key storage.
        response = authenticated_client.put(ENDPOINT, json={"mode": "subscription"})
        assert response.status_code == 200, response.text
        assert legacy_key not in response.text
        assert "key_id" not in claude_auth_mode.shared_selection()
        assert db.get_system_setting(claude_accounts.API_SETTING) == value

    body = authenticated_client.get(ENDPOINT).json()
    assert body["api_key"] == {"set": True, "last4": "ABCD"}
    assert _native_key() == legacy_key
    assert db.get_system_setting(claude_accounts.API_LIST_SETTING) is None


ACCOUNTS = ENDPOINT + "/accounts"


def _save_with_shipped_writer(first, second):
    """The scalar-to-list write from #840 (b933c0659), twice with different scalars."""
    from hashlib import sha256

    from app.core import claude_accounts as accounts

    ids = []
    for key in (first, second):
        db.set_system_setting(accounts.API_SETTING, encryption.encrypt_value(key))
        stored = db.get_system_setting(accounts.API_LIST_SETTING)
        bundle = json.loads(encryption.decrypt_value(stored)) if stored else {"accounts": []}
        account_id = "legacy-" + sha256(key.encode()).hexdigest()[:24]
        bundle["accounts"].append({"id": account_id, "label": "Anthropic API key", "key": key})
        db.set_system_setting(accounts.API_LIST_SETTING, encryption.encrypt_value(json.dumps(bundle)))
        ids.append(account_id)
    return ids


@pytest.mark.parametrize("old_pin", [False, True])
def test_upgrade_saved_key_ids_are_opaque_and_actionable(authenticated_client, old_pin):
    from app.core import claude_accounts as accounts
    from app.db.settings import get_system_setting_snapshots

    first, second = "fixture-upgrade-first-ABCD", "fixture-upgrade-second-WXYZ"
    first_old, second_old = _save_with_shipped_writer(first, second)
    shared, local = claude_auth_mode.SELECTION_SETTING, claude_auth_mode.setting_key()
    saved, scalar = accounts.API_LIST_SETTING, accounts.API_SETTING
    db.set_system_setting(local, "api_key")
    old_choice = {"mode": "api_key", "subscription": None, "key_id": second_old}
    if old_pin:
        db.set_system_setting(shared, json.dumps(old_choice))
        assert _native_key() == second
    before = get_system_setting_snapshots(shared, local, saved, scalar)

    def safe(response):
        assert response.status_code == 200, response.text
        for secret in (first, second, first_old, second_old):
            assert secret not in response.text
        return response.json()

    with _probe(False):
        settings = safe(authenticated_client.get(ENDPOINT))
        rows = {row["last4"]: row for row in safe(authenticated_client.get(ACCOUNTS))["accounts"]["api_key"]}
        assert list(rows.values()) == settings["accounts"]["api_key"]
        first_id, second_id = rows["ABCD"]["id"], rows["WXYZ"]["id"]
        assert first_id != second_id and not rows["ABCD"]["active"] and rows["WXYZ"]["active"]
        assert get_system_setting_snapshots(shared, local, saved, scalar) == before
        # Choosing Subscription carries an already-pinned key forward; without
        # one it pins nothing and the scalar stays the key in use.
        safe(authenticated_client.put(ENDPOINT, json={"mode": "subscription"}))
        choice = db.get_system_setting(shared)
        if old_pin:
            assert encryption.is_encrypted(choice)
            assert second_old not in choice
        else:
            assert "key_id" not in claude_auth_mode.shared_selection()
        assert accounts.pinned_api_key(second_old) == second  # A peer can still carry the old pin.
        assert _native_key() == second
        assert db.get_system_setting_snapshot(scalar) == before[scalar]

        path = f"{ACCOUNTS}/api_key/{first_id}"
        safe(authenticated_client.patch(path, json={"label": "First renamed"}))
        assert accounts.pinned_api_key(second_old) == second
        rows = {row["id"]: row for row in safe(authenticated_client.post(path + "/activate"))["accounts"]["api_key"]}
        assert rows[first_id]["label"] == "First renamed" and rows[first_id]["active"]
        assert not rows[second_id]["active"]
        choice = db.get_system_setting(shared)
        assert encryption.is_encrypted(choice)
        assert all(value not in choice for value in (first, second, first_old, second_old))
        assert _native_key() == first
        safe(authenticated_client.delete(path))
        assert accounts.pinned_api_key(first_old) is None
        accounts.add_api_key(first, activate=False)
        assert accounts.pinned_api_key(first_old) is None  # A re-save must not revive a deleted pin.
        assert json.loads(db.get_system_setting(shared))["key_id"] is None
        # Even if the older, encrypted selection wins independently of the list,
        # re-adding the same credential must not make that deleted pin valid.
        db.set_system_setting(shared, choice)
        assert _native_key() is None


def test_offline_rename_preserves_legacy_pin_without_reselection(authenticated_client):
    from app.core import claude_accounts as accounts

    active, inactive = "fixture-original-A123", "fixture-inactive-B456"
    old_active, _ = _save_with_shipped_writer(active, inactive)
    saved, shared = accounts.API_LIST_SETTING, claude_auth_mode.SELECTION_SETTING
    old_snapshot = db.get_system_setting_snapshot(saved)
    old_choice = json.dumps({"mode": "api_key", "subscription": None, "key_id": old_active})
    db.set_system_setting(shared, old_choice)
    with _probe(True):
        # A changes only the mode; its resulting shared decision will win.
        assert authenticated_client.put(ENDPOINT, json={"mode": "subscription"}).status_code == 200
        choice_a = db.get_system_setting(shared)
        assert db.get_system_setting_snapshot(saved) == old_snapshot

        # B is still looking at the shipped list and pin. Its rename wins the
        # list register independently of A's mode choice. Neither key changed.
        db.set_system_setting(saved, old_snapshot.value)
        db.set_system_setting(shared, old_choice)
        rows = authenticated_client.get(ACCOUNTS).json()["accounts"]["api_key"]
        row = next(row for row in rows if row["last4"] == "B456")
        path = f"{ACCOUNTS}/api_key/{row['id']}"
        assert authenticated_client.patch(path, json={"label": "Renamed offline"}).status_code == 200
        assert db.get_system_setting(shared) == old_choice
        db.set_system_setting(shared, choice_a)

        rows = authenticated_client.get(ACCOUNTS).json()["accounts"]["api_key"]
        assert {row["last4"] for row in rows} == {"A123", "B456"}
        assert _native_key() == active


@pytest.fixture
def saved_subscription(authenticated_client, monkeypatch, tmp_path):
    from app.core import claude_cli_home as home, claude_cli_login, claude_subscriptions as subs

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "default"))
    monkeypatch.setattr(home, "root", lambda: tmp_path / "logins")
    monkeypatch.setattr(home, "platform_support", lambda: (True, None))
    monkeypatch.setattr(claude_cli_login, "terminal_supported", lambda: False)
    monkeypatch.setattr(claude_cli_status, "announce_account_change", lambda: None)
    monkeypatch.setattr(subs, "_identity_cache", {})
    monkeypatch.setattr(subs, "_verified", {})

    def probe(home_env, resolver=None):
        return claude_cli_status.ClaudeCliStatus(
            installed=True, signed_in=True, auth_method="claude.ai", org_id="fixture-org",
            email="saved@example.com" if home_env else "default@example.com",
        )

    monkeypatch.setattr(claude_cli_status, "probe_home", probe)
    response = authenticated_client.post(ACCOUNTS + "/subscription/add-another")
    assert response.status_code == 200, response.text
    account_id = response.json()["login"]["id"]
    monkeypatch.setattr(subs, "_last_pending_probe", 0.0)
    rows = authenticated_client.get(ACCOUNTS).json()["accounts"]["subscription"]
    assert [(row["id"], row["active"]) for row in rows] == [(account_id, False)]
    return account_id


@pytest.mark.parametrize("writer", ["mode", "saved", "default"])
def test_subscription_writer_does_not_pin_scalar_before_it_syncs(
    authenticated_client, monkeypatch, saved_subscription, writer,
):
    from app.core import claude_accounts as accounts

    choice_key = claude_auth_mode.SELECTION_SETTING
    old_scalar = encryption.encrypt_value("fixture-stale-1111")
    new_scalar = encryption.encrypt_value("fixture-newer-2222")
    db.set_system_setting(accounts.API_SETTING, new_scalar)
    with monkeypatch.context() as locked:
        locked.setattr(encryption, "is_encryption_ready", lambda: False)
        response = authenticated_client.put(ENDPOINT, json={"mode": "subscription"})
        assert response.status_code == 200, response.text
    from_a = db.get_system_setting(choice_key)
    assert "key_id" not in json.loads(from_a)

    # B's independent worker row and lagging scalar when A's selection arrives.
    monkeypatch.setattr(claude_auth_mode, "setting_key", lambda: "claude_auth_mode:fixture-peer")
    db.set_system_setting(accounts.API_SETTING, old_scalar)
    assert db.get_system_setting(choice_key) == from_a
    if writer == "mode":
        response = authenticated_client.put(ENDPOINT, json={"mode": "subscription"})
    else:
        path = f"{saved_subscription}/activate" if writer == "saved" else "use-default"
        response = authenticated_client.post(ACCOUNTS + f"/subscription/{path}")
    assert response.status_code == 200, response.text
    assert "key_id" not in claude_auth_mode.shared_selection()
    assert db.get_system_setting(accounts.API_LIST_SETTING) is None

    # Selecting a saved key is what pins it.
    accounts.add_api_key("fixture-newer-2222")
    assert _native_key() == "fixture-newer-2222"

    # A subscription-only change must also preserve an existing explicit pin.
    pin = claude_auth_mode.shared_selection()["key_id"]
    db.set_system_setting(accounts.API_SETTING, old_scalar)
    if writer == "mode":
        response = authenticated_client.put(ENDPOINT, json={"mode": "subscription"})
    else:
        response = authenticated_client.post(ACCOUNTS + f"/subscription/{path}")
    assert response.status_code == 200, response.text
    assert claude_auth_mode.shared_selection()["key_id"] == pin


def test_activation_under_a_legacy_api_key_mode_needs_no_key_storage(
    authenticated_client, monkeypatch, saved_subscription,
):
    """The removed mode no longer pins the legacy key on the next choice, so
    picking a sign-in works with key storage locked and leaves the key alone."""
    from app.core import claude_accounts as accounts, claude_subscriptions as subs

    db.set_system_setting(claude_auth_mode.setting_key(), claude_auth_mode.LEGACY_MODE_API_KEY)
    db.set_system_setting(accounts.API_SETTING, encryption.encrypt_value("fixture-api-3333"))
    activate = ACCOUNTS + f"/subscription/{saved_subscription}/activate"
    assert claude_auth_mode.shared_selection() is None
    assert claude_auth_mode.selected_mode() == "subscription"
    with monkeypatch.context() as locked:
        locked.setattr(encryption, "is_encryption_ready", lambda: False)
        response = authenticated_client.post(activate)
        assert response.status_code == 200, response.text
    assert subs._load()["active_id"] == saved_subscription
    assert claude_auth_mode.shared_selection()["mode"] == "subscription"
    # The legacy scalar key is still the one the Claude API provider bills.
    assert "key_id" not in claude_auth_mode.shared_selection()
    assert db.get_system_setting(accounts.API_LIST_SETTING) is None
    assert _native_key() == "fixture-api-3333"


@pytest.mark.parametrize("failed_row", ["local", "shared"])
def test_activation_storage_failure_rolls_back_both_choices_and_retries(
    authenticated_client, monkeypatch, saved_subscription, failed_row,
):
    from psycopg2 import OperationalError

    from app.core import claude_subscriptions as subs
    from app.db import settings as db_settings

    activate = ACCOUNTS + f"/subscription/{saved_subscription}/activate"
    default = ACCOUNTS + "/subscription/use-default"
    assert authenticated_client.post(default).status_code == 200
    local_key, shared_key = subs.setting_key(), claude_auth_mode.SELECTION_SETTING
    before = (db.get_system_setting(local_key), db.get_system_setting(shared_key))
    write = db_settings._write_system_setting

    def fail_local(conn, key, value):
        write(conn, key, value)
        if key == local_key:
            raise OperationalError("fixture write failure")

    conditional_write = db_settings._write_system_setting_if_unchanged

    def fail_shared(conn, key, value, expected):
        # Fail after the SQL statement so even the last row needs rollback.
        assert conditional_write(conn, key, value, expected)
        if key == shared_key:
            raise OperationalError("fixture write failure")
        return True

    with monkeypatch.context() as failing:
        if failed_row == "local":
            failing.setattr(db_settings, "_write_system_setting", fail_local)
        else:
            failing.setattr(db_settings, "_write_system_setting_if_unchanged", fail_shared)
        response = authenticated_client.post(activate)
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "storage_unavailable"
    assert (db.get_system_setting(local_key), db.get_system_setting(shared_key)) == before

    response = authenticated_client.post(activate)
    assert response.status_code == 200, response.text
    assert subs._load()["active_id"] == saved_subscription
    assert db.get_system_setting(shared_key) != before[1]
    assert response.json()["accounts"]["subscription"][0]["active"] is True


def test_put_rejects_unknown_mode(authenticated_client):
    with _probe(False):
        assert authenticated_client.put(ENDPOINT, json={"mode": "auto"}).status_code == 400


def test_saving_a_key_never_switches_the_mode(authenticated_client):
    claude_auth_mode.set_mode("subscription")
    with patch("app.core.anthropic_generation.validate_api_key", AsyncMock(return_value=None)):
        r = authenticated_client.post("/api/v1/settings/integrations/anthropic",
                                      json={"api_key": "sk-ant-api03-first-1111"})
        assert r.status_code == 200, r.text
        assert claude_auth_mode.stored_mode() == "subscription"
        # Older clients still send use_for_claude_code: accepted and ignored.
        r = authenticated_client.post("/api/v1/settings/integrations/anthropic",
                                      json={"api_key": "sk-ant-api03-second-2222", "use_for_claude_code": True})
        assert r.status_code == 200, r.text
    assert claude_auth_mode.stored_mode() == "subscription"
    assert _native_key() == "sk-ant-api03-second-2222"


def test_removing_the_key_leaves_the_mode_alone(authenticated_client):
    db.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-ant-api03-secret-ABCD"))
    claude_auth_mode.set_mode("subscription")
    assert authenticated_client.delete("/api/v1/settings/integrations/anthropic").status_code == 200
    assert claude_auth_mode.stored_mode() == "subscription"


@pytest.mark.real_agent_gate
def test_payload_carries_the_credential_gate(authenticated_client):
    from app.core import credential_gate

    claude_auth_mode.set_mode("subscription")
    with _probe(False):
        body = authenticated_client.get(ENDPOINT).json()
    assert body["mode"] == "subscription"
    assert body["gate"]["ready"] is False
    assert body["gate"]["reason"] == credential_gate.NO_PROVIDER_MESSAGE
    # The key is its own provider; the CLI still needs the subscription sign-in.
    assert body["claude_reason"] == claude_subscriptions.SELECTION_UNAVAILABLE
    from app.core import claude_accounts
    claude_accounts.add_api_key("fixture-api-key-ABCD")
    with _probe(False):
        body = authenticated_client.get(ENDPOINT).json()
    assert body["gate"]["ready"] is True and body["gate"]["reason"] is None
    assert body["gate"]["ready_providers"] == ["gw:claude-api"]
    assert body["claude_reason"] == claude_subscriptions.SELECTION_UNAVAILABLE
    with _probe(True):
        body = authenticated_client.get(ENDPOINT).json()
    assert body["gate"]["ready_providers"] == ["claude", "gw:claude-api"]
    assert body["claude_reason"] is None


@pytest.mark.real_agent_gate
def test_put_off_turns_claude_off(authenticated_client):
    from app.core import credential_gate

    db.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-ant-api03-secret-ABCD"))
    claude_auth_mode.set_mode("subscription")
    with _probe(True):
        response = authenticated_client.put(ENDPOINT, json={"mode": "off"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "off" and body["mode_label"] == "Off"
    assert [m["value"] for m in body["modes"]] == ["subscription", "cloud", "off"]
    assert body["gate"]["mode"] == "off"
    assert body["gate"]["claude_credential_ready"] is False
    assert body["gate"]["reason"] == credential_gate.NO_PROVIDER_MESSAGE
    assert body["claude_reason"] == credential_gate.OFF_MESSAGE
    integrations = authenticated_client.get("/api/v1/settings/integrations").json()
    assert integrations["claude_auth_mode"] == "off"
    assert integrations["claude_code_auth_method"] == "off"
    registry = authenticated_client.get("/api/v1/models").json()
    assert registry["claude"] == {"auth_kind": None, "plan_type": None, "enabled": False}

    with _probe(True):
        back_on = authenticated_client.put(ENDPOINT, json={"mode": "subscription"}).json()
    assert back_on["mode"] == "subscription"
    assert back_on["gate"]["claude_credential_ready"] is True


def test_put_off_is_owner_only(client):
    from app.api.auth import hash_api_key

    db.create_api_user("auth-mode-user", "Auth Mode User", hash_api_key("aih_auth_mode_user_key"))
    claude_auth_mode.set_mode("subscription")
    with _probe(True):
        r = client.put(ENDPOINT, json={"mode": "off"},
                       headers={"Authorization": "Bearer aih_auth_mode_user_key"})
    assert r.status_code == 403
    assert claude_auth_mode.stored_mode() == "subscription"

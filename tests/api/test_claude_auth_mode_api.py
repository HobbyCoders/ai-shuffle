"""GET/PUT /settings/claude-auth: the explicit Claude Code auth mode."""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core import claude_auth_mode, claude_cli_status, encryption
from app.db import database as db

ENDPOINT = "/api/v1/settings/claude-auth"


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
        response = authenticated_client.put(ENDPOINT, json={"mode": "api_key"})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "api_key"
    integrations = authenticated_client.get("/api/v1/settings/integrations").json()
    assert integrations["claude_code_auth_method"] == "api_key"


def test_owner_mode_selection_is_shared_and_does_not_fall_back(authenticated_client, monkeypatch):
    from app.core.environment import ProviderTurnRefused, inject_claude_code_auth
    from app.network.policy import is_shared_setting_key

    assert is_shared_setting_key(claude_auth_mode.SELECTION_SETTING)
    assert not is_shared_setting_key(claude_auth_mode.setting_key())
    # Simulate the peer's legacy startup choice and a configured host gateway.
    db.set_system_setting(claude_auth_mode.setting_key(), "subscription")
    with _probe(True):
        assert authenticated_client.put(ENDPOINT, json={"mode": "api_key"}).status_code == 200
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://fixture.invalid")
    assert claude_auth_mode.current_mode() == "api_key"
    with pytest.raises(ProviderTurnRefused, match="Anthropic API key"):
        inject_claude_code_auth({"ANTHROPIC_API_KEY": "not-the-owner-key"})


def test_combined_key_mode_pins_account_across_partial_sync_rotation_and_delete(authenticated_client):
    from app.core import claude_accounts
    from app.core.environment import ProviderTurnRefused, inject_claude_code_auth

    alpha, beta, gamma = "fixture-alpha-1111", "fixture-beta-2222", "fixture-gamma-3333"
    alpha_id = claude_accounts.add_api_key(alpha)
    old_keys = db.get_system_setting(claude_accounts.API_LIST_SETTING)
    with patch("app.api.settings.validate_anthropic_key", new_callable=AsyncMock):
        response = authenticated_client.post("/api/v1/settings/integrations/anthropic", json={
            "api_key": beta, "use_for_claude_code": True,
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
    with pytest.raises(ProviderTurnRefused, match="Anthropic API key"):
        inject_claude_code_auth({"ANTHROPIC_API_KEY": alpha})
    db.set_system_setting(claude_accounts.API_LIST_SETTING, new_keys)
    assert inject_claude_code_auth({})["ANTHROPIC_API_KEY"] == beta
    with patch.object(claude_auth_mode, "setting_key", lambda: "claude_auth_mode:fixture-off-peer"):
        claude_auth_mode.set_mode("off")
        claude_accounts.change_account("api_key", alpha_id, "delete")
        assert db.get_system_setting(claude_accounts.API_SETTING) is None
        assert db.get_system_setting(claude_auth_mode.SELECTION_SETTING) == choice
    assert inject_claude_code_auth({})["ANTHROPIC_API_KEY"] == beta
    gamma_id = claude_accounts.add_api_key(gamma)
    assert gamma_id != beta_id
    assert inject_claude_code_auth({})["ANTHROPIC_API_KEY"] == gamma
    claude_accounts.change_account("api_key", gamma_id, "delete")
    with pytest.raises(ProviderTurnRefused, match="Anthropic API key"):
        inject_claude_code_auth({})


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
def test_upgrade_pins_legacy_key_after_unlock(authenticated_client, monkeypatch, encrypted):
    from app.core import claude_accounts
    from app.core.environment import inject_claude_code_auth

    legacy_key = "fixture-legacy-secret-ABCD"
    value = encryption.encrypt_value(legacy_key) if encrypted else legacy_key
    db.set_system_setting(claude_accounts.API_SETTING, value)
    db.set_system_setting(claude_auth_mode.setting_key(), "subscription")

    with monkeypatch.context() as locked:
        locked.setattr(encryption, "is_encryption_ready", lambda: False)
        response = authenticated_client.put(ENDPOINT, json={"mode": "api_key"})
        assert response.status_code == 409, response.text
        assert legacy_key not in response.text
        assert db.get_system_setting(claude_auth_mode.SELECTION_SETTING) is None
        assert db.get_system_setting(claude_auth_mode.setting_key()) == "subscription"
        assert db.get_system_setting(claude_accounts.API_SETTING) == value

        # Subscription choices do not require access to unrelated key storage.
        response = authenticated_client.put(ENDPOINT, json={"mode": "subscription"})
        assert response.status_code == 200, response.text
        assert "key_id" not in claude_auth_mode.shared_selection()

    response = authenticated_client.put(ENDPOINT, json={"mode": "api_key"})
    assert response.status_code == 200, response.text
    assert response.json()["api_key"] == {"set": True, "last4": "ABCD"}
    assert inject_claude_code_auth({})["ANTHROPIC_API_KEY"] == legacy_key
    assert encryption.is_encrypted(db.get_system_setting(claude_accounts.API_LIST_SETTING))


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
    from app.core.environment import inject_claude_code_auth
    from app.db.settings import get_system_setting_snapshots

    first, second = "fixture-upgrade-first-ABCD", "fixture-upgrade-second-WXYZ"
    first_old, second_old = _save_with_shipped_writer(first, second)
    shared, local = claude_auth_mode.SELECTION_SETTING, claude_auth_mode.setting_key()
    saved, scalar = accounts.API_LIST_SETTING, accounts.API_SETTING
    db.set_system_setting(local, "api_key")
    old_choice = {"mode": "api_key", "subscription": None, "key_id": second_old}
    if old_pin:
        db.set_system_setting(shared, json.dumps(old_choice))
        assert inject_claude_code_auth({})["ANTHROPIC_API_KEY"] == second
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
        if old_pin:
            # Carrying forward an already-pinned key is also a new shared write.
            safe(authenticated_client.put(ENDPOINT, json={"mode": "api_key"}))
        else:
            safe(authenticated_client.put(ENDPOINT, json={
                "mode": "api_key", "api_key_confirmation": settings["api_key_confirmation"],
            }))
        assert claude_auth_mode.shared_selection()["key_id"] == second_id
        assert accounts.pinned_api_key(second_old) == second  # A peer can still carry the old pin.
        assert inject_claude_code_auth({})["ANTHROPIC_API_KEY"] == second
        assert db.get_system_setting_snapshot(scalar) == before[scalar]

        path = f"{ACCOUNTS}/api_key/{first_id}"
        safe(authenticated_client.patch(path, json={"label": "First renamed"}))
        assert accounts.pinned_api_key(second_old) == second
        rows = {row["id"]: row for row in safe(authenticated_client.post(path + "/activate"))["accounts"]["api_key"]}
        assert rows[first_id]["label"] == "First renamed" and rows[first_id]["active"]
        assert not rows[second_id]["active"]
        choice = db.get_system_setting(shared)
        assert json.loads(choice)["key_id"] == first_id
        assert all(value not in choice for value in (first, second, first_old, second_old))
        assert inject_claude_code_auth({})["ANTHROPIC_API_KEY"] == first
        safe(authenticated_client.delete(path))
        assert accounts.pinned_api_key(first_old) is None
        accounts.add_api_key(first, activate=False)
        assert accounts.pinned_api_key(first_old) is None  # A re-save must not revive a deleted pin.
        assert json.loads(db.get_system_setting(shared))["key_id"] is None


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
    from app.core.environment import inject_claude_code_auth

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

    db.set_system_setting(accounts.API_SETTING, new_scalar)
    response = authenticated_client.put(ENDPOINT, json={"mode": "api_key"})
    assert response.status_code == 200, response.text
    assert inject_claude_code_auth({})["ANTHROPIC_API_KEY"] == "fixture-newer-2222"

    # A subscription-only change must also preserve an existing explicit pin.
    pin = claude_auth_mode.shared_selection()["key_id"]
    db.set_system_setting(accounts.API_SETTING, old_scalar)
    if writer == "mode":
        response = authenticated_client.put(ENDPOINT, json={"mode": "subscription"})
    else:
        response = authenticated_client.post(ACCOUNTS + f"/subscription/{path}")
    assert response.status_code == 200, response.text
    assert claude_auth_mode.shared_selection()["key_id"] == pin


def test_failed_legacy_api_activation_does_not_steer_later_choice(
    authenticated_client, monkeypatch, saved_subscription,
):
    from app.core import claude_accounts as accounts, claude_subscriptions as subs

    db.set_system_setting(claude_auth_mode.setting_key(), "api_key")
    db.set_system_setting(accounts.API_SETTING, encryption.encrypt_value("fixture-api-3333"))
    before = db.get_system_setting(subs.setting_key())
    activate = ACCOUNTS + f"/subscription/{saved_subscription}/activate"
    rendered = authenticated_client.get(ENDPOINT).json()
    assert rendered["api_key_confirmation_required"] is True
    assert claude_auth_mode.shared_selection() is None
    with monkeypatch.context() as locked:
        locked.setattr(encryption, "is_encryption_ready", lambda: False)
        response = authenticated_client.post(activate)
        assert response.status_code == 503, response.text
    assert db.get_system_setting(subs.setting_key()) == before
    assert claude_auth_mode.shared_selection() is None
    assert claude_auth_mode.current_mode() == "api_key"
    target = subs.legacy_binding()
    confirmation = authenticated_client.put(ENDPOINT, json={
        "mode": "api_key", "api_key_confirmation": rendered["api_key_confirmation"],
    })
    assert confirmation.status_code == 200
    assert confirmation.json()["api_key_confirmation_required"] is False
    assert accounts.pinned_api_key(claude_auth_mode.shared_selection()["key_id"]) == "fixture-api-3333"
    assert claude_auth_mode.shared_selection()["subscription"] == target
    assert authenticated_client.post(activate).status_code == 200
    assert claude_auth_mode.shared_selection()["subscription"] != target
    assert subs._load()["active_id"] == saved_subscription


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


def test_saving_a_key_does_not_switch_mode_unless_asked(authenticated_client):
    claude_auth_mode.set_mode("subscription")
    with patch("app.core.anthropic_generation.validate_api_key", AsyncMock(return_value=None)):
        r = authenticated_client.post("/api/v1/settings/integrations/anthropic",
                                      json={"api_key": "sk-ant-api03-first-1111"})
        assert r.status_code == 200, r.text
        assert claude_auth_mode.stored_mode() == "subscription"
        r = authenticated_client.post("/api/v1/settings/integrations/anthropic",
                                      json={"api_key": "sk-ant-api03-second-2222", "use_for_claude_code": True})
        assert r.status_code == 200, r.text
    assert claude_auth_mode.stored_mode() == "api_key"


@pytest.mark.parametrize("change", ["activate", "same_suffix", "missing", "local_off", "local_cloud", "shared"])
def test_legacy_confirmation_rejects_stale_key_or_mode_without_writes(authenticated_client, change):
    from app.core import claude_accounts as accounts

    first, second = "fixture-first-1111", "fixture-second-1111" if change == "same_suffix" else "fixture-second-2222"
    local, shared = claude_auth_mode.setting_key(), claude_auth_mode.SELECTION_SETTING
    db.set_system_setting(local, "api_key")
    db.set_system_setting(accounts.API_SETTING, encryption.encrypt_value(first))
    # Seed a pre-list legacy key alongside a distinct saved account.
    saved_id = "fixture-saved-id"
    db.set_system_setting(accounts.API_LIST_SETTING, encryption.encrypt_value(json.dumps({
        "accounts": [{"id": saved_id, "label": "Other key", "key": second}],
    })))

    rendered = authenticated_client.get(ENDPOINT).json()
    assert rendered["api_key"]["last4"] == "1111"
    revision = rendered["api_key_confirmation"]
    assert revision and first not in json.dumps(rendered) and second not in json.dumps(rendered)
    assert authenticated_client.put(ENDPOINT, json={"mode": "api_key"}).status_code == 409

    if change in ("activate", "same_suffix"):
        assert authenticated_client.post(f"{ACCOUNTS}/api_key/{saved_id}/activate").status_code == 200
        assert db.get_system_setting(shared) is None
    elif change == "missing":
        assert authenticated_client.delete("/api/v1/settings/integrations/anthropic").status_code == 200
    elif change.startswith("local_"):
        db.set_system_setting(local, change.removeprefix("local_"))
    else:
        claude_auth_mode.set_mode("subscription")

    def state():
        return [db.get_system_setting_snapshot(k) for k in (local, shared, accounts.API_SETTING, accounts.API_LIST_SETTING)]

    before = state()
    response = authenticated_client.put(ENDPOINT, json={"mode": "api_key", "api_key_confirmation": revision})
    assert response.status_code == 409, response.text
    assert first not in response.text and second not in response.text
    assert state() == before
    fresh = authenticated_client.get(ENDPOINT).json()
    assert fresh["api_key_confirmation"] != revision
    if change in ("activate", "same_suffix"):
        retry = authenticated_client.put(ENDPOINT, json={
            "mode": "api_key", "api_key_confirmation": fresh["api_key_confirmation"],
        })
        assert retry.status_code == 200, retry.text
        assert claude_auth_mode.shared_selection()["key_id"] == saved_id
        assert accounts.pinned_api_key(saved_id) == second
    else:
        assert fresh["api_key_confirmation"] is None


def test_removing_the_key_keeps_api_key_mode(authenticated_client):
    db.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-ant-api03-secret-ABCD"))
    claude_auth_mode.set_mode("api_key")
    assert authenticated_client.delete("/api/v1/settings/integrations/anthropic").status_code == 200
    assert claude_auth_mode.stored_mode() == "api_key"


@pytest.mark.real_agent_gate
def test_payload_carries_the_credential_gate(authenticated_client):
    from app.core import credential_gate

    claude_auth_mode.set_mode("api_key")
    with _probe(True):
        body = authenticated_client.get(ENDPOINT).json()
    assert body["gate"]["ready"] is False
    assert body["gate"]["reason"] == credential_gate.NO_PROVIDER_MESSAGE
    assert body["claude_reason"] == credential_gate.NO_KEY_MESSAGE
    from app.core import claude_accounts
    claude_accounts.add_api_key("fixture-api-key-ABCD")
    with _probe(False):
        body = authenticated_client.get(ENDPOINT).json()
    assert body["gate"]["ready"] is True and body["gate"]["reason"] is None
    assert body["gate"]["ready_providers"] == ["claude"] and body["claude_reason"] is None


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
    assert [m["value"] for m in body["modes"]] == ["subscription", "api_key", "cloud", "off"]
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

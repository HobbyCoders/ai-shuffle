"""Owner-only saved Anthropic key contract; no app DB, provider or CLI access."""
import json
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_admin
from app.api.settings_claude_accounts import router
from app.core import claude_accounts as accounts, encryption

BASE = "/api/v1/settings/claude-auth/accounts"
KEY = "example-anthropic-secret-key-1111"


@pytest.fixture
def client(monkeypatch):
    values = {}
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())
    monkeypatch.setattr(accounts.database, "get_system_setting", values.get)
    monkeypatch.setattr(accounts.database, "set_system_setting", values.__setitem__)
    monkeypatch.setattr(accounts.database, "delete_system_setting", lambda key: values.pop(key, None) is not None)
    from app.core import anthropic_generation
    monkeypatch.setattr(anthropic_generation, "validate_api_key", AsyncMock())
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/settings")
    app.dependency_overrides[require_admin] = lambda: "owner"
    with TestClient(app) as result:
        yield result


def test_key_save_switch_rename_delete_contract(client, caplog):
    result = client.post(BASE + "/api-keys", json={"api_key": KEY, "label": "Personal"})
    assert result.status_code == 200, result.text
    first = result.json()["account_id"]
    assert result.json()["accounts"]["api_key"] == [
        {"id": first, "label": "Personal", "active": True, "masked_key": "example...1111", "last4": "1111"}]
    second = client.post(BASE + "/api-keys", json={"api_key": KEY[:-4] + "2222", "activate": False}).json()["account_id"]
    assert accounts.active_api_key() == KEY
    assert client.post(BASE + f"/api_key/{second}/activate").status_code == 200
    assert accounts.active_api_key().endswith("2222")
    assert client.patch(BASE + f"/api_key/{first}", json={"label": "Old"}).status_code == 200
    assert client.delete(BASE + f"/api_key/{second}").status_code == 200
    assert accounts.active_api_key() is None
    assert len(client.get(BASE).json()["accounts"]["api_key"]) == 1
    assert KEY not in caplog.text
    assert KEY not in client.get(BASE).text


def test_invalid_secret_input_is_not_echoed(client):
    response = client.post(BASE + "/api-keys", json={"api_key": [KEY]})
    assert response.status_code == 400 and KEY not in response.text
    assert client.post(BASE + "/api_key/missing/activate").status_code == 404


def test_locked_store_is_explicit(client, monkeypatch):
    monkeypatch.setattr(encryption, "_encryption_key", None)
    for method, path, kwargs in (("post", "/api-keys", {"json": {"api_key": KEY}}),
                                 ("post", "/api_key/missing/activate", {})):
        response = getattr(client, method)(BASE + path, **kwargs)
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "storage_locked"
        assert KEY not in response.text


@pytest.fixture
def sign_ins(client, tmp_path, monkeypatch):
    """Worker-local sign-in list in a dict, folders under tmp_path, a fake CLI."""
    import subprocess

    from app.core import claude_cli_home, claude_cli_login, claude_cli_status as cs, claude_subscriptions
    base = tmp_path / ".claude"
    (base / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setattr(claude_cli_home.sys, "platform", "linux")
    monkeypatch.setattr(claude_cli_home, "root", lambda: tmp_path / "logins")
    values = {}
    for name in ("get_system_setting", "get_system_setting_cached"):
        monkeypatch.setattr(claude_subscriptions.database, name, lambda key, *a, **k: values.get(key))
    monkeypatch.setattr(claude_subscriptions.database, "set_system_setting", values.__setitem__)
    monkeypatch.setattr(claude_subscriptions, "setting_key", lambda: "claude_cli_logins:test")
    monkeypatch.setattr(claude_subscriptions, "_last_pending_probe", 0.0)
    monkeypatch.setattr(claude_subscriptions, "_identity_cache", {})
    monkeypatch.setattr(claude_cli_login, "terminal_supported", lambda: False)
    monkeypatch.setattr(cs, "announce_account_change", lambda: None)
    signed = {}

    def probe_home(home_env, resolver=None):
        email = signed.get(home_env.get("CLAUDE_CONFIG_DIR", "default"))
        return cs.ClaudeCliStatus(installed=True, signed_in=bool(email), email=email, plan="pro")
    monkeypatch.setattr(cs, "probe_home", probe_home)
    monkeypatch.setattr(cs, "run_home", lambda args, env: subprocess.CompletedProcess(args, 0, "", ""))
    return signed


def test_subscription_routes_contract(client, sign_ins, tmp_path):
    from app.core import claude_subscriptions
    listing = client.get(BASE).json()["accounts"]
    assert listing["subscription"] == [] and listing["subscription_support"]["supported"] is True
    assert listing["default_in_use"] is True and listing["login"] is None

    assert client.post(BASE + "/subscription/save-current", json={}).status_code == 409
    sign_ins["default"] = "me@example.com"
    saved = client.post(BASE + "/subscription/save-current", json={"label": "Personal"})
    assert saved.status_code == 200, saved.text
    default_id = saved.json()["account_id"]

    started = client.post(BASE + "/subscription/add-another")
    assert started.status_code == 200, started.text
    login = started.json()["login"]
    folder = str(tmp_path / "logins" / login["id"])
    assert login["env"] == {"CLAUDE_CONFIG_DIR": folder} and login["terminal"] is False
    assert started.json()["accounts"]["login_pending"] is True

    sign_ins[folder] = "work@example.com"
    claude_subscriptions._last_pending_probe = 0.0  # the next poll asks the CLI again
    rows = client.get(BASE).json()["accounts"]["subscription"]
    assert [(r["kind"], r["active"], r["email"]) for r in rows] == [
        ("default", False, "me@example.com"), ("saved", True, "work@example.com")]
    new_id = rows[1]["id"]
    assert set(rows[1]) == {"id", "label", "kind", "active", "email", "plan", "org_name",
                            "signed_in", "checked_at", "needs_sign_in"}

    assert client.patch(BASE + f"/subscription/{new_id}", json={"label": "Work"}).status_code == 200
    assert client.post(BASE + f"/subscription/{default_id}/activate").status_code == 200
    assert claude_subscriptions.active_home_env() == {}
    relogin = client.post(BASE + f"/subscription/{new_id}/sign-in")
    assert relogin.status_code == 200
    assert relogin.json()["login"]["env"] == {"CLAUDE_CONFIG_DIR": folder}
    assert relogin.json()["login"]["terminal"] is False
    assert claude_subscriptions.active_home_env() == {}
    assert client.post(BASE + "/subscription/missing/sign-in").status_code == 404
    assert client.post(BASE + f"/subscription/{new_id}/activate").status_code == 200
    assert claude_subscriptions.active_home_env() == {"CLAUDE_CONFIG_DIR": folder}
    deleted = client.delete(BASE + f"/subscription/{new_id}").json()["accounts"]
    assert deleted["default_in_use"] is True and [r["active"] for r in deleted["subscription"]] == [True]
    assert (tmp_path / ".claude" / "projects").is_dir()
    assert client.post(BASE + "/subscription/missing0000000000/activate").status_code == 404

    pending = client.post(BASE + "/subscription/add-another").json()["login"]
    cancelled = client.post(BASE + "/subscription/cancel-login")
    assert cancelled.status_code == 200 and cancelled.json()["accounts"]["login_pending"] is False
    assert not (tmp_path / "logins" / pending["id"]).exists()
    assert "account_id" not in cancelled.json()

    # Cancelled after the CLI finished but before a poll saw it: the sign-in is kept.
    pending = client.post(BASE + "/subscription/add-another").json()["login"]
    late_folder = str(tmp_path / "logins" / pending["id"])
    sign_ins[late_folder] = "late@example.com"
    claude_subscriptions._last_pending_probe = float("inf")
    kept = client.post(BASE + "/subscription/cancel-login")
    assert kept.status_code == 200, kept.text
    body = kept.json()
    assert body["account_id"] == pending["id"] and body["login"] is None
    row = next(r for r in body["accounts"]["subscription"] if r["id"] == pending["id"])
    assert row["active"] and row["email"] == "late@example.com"
    assert claude_subscriptions.active_home_env() == {"CLAUDE_CONFIG_DIR": late_folder}


@pytest.mark.parametrize("signed_in", [True, False])
def test_refresh_write_failure_keeps_rows_and_keys_then_retries(client, sign_ins, monkeypatch, signed_in):
    from app.core import claude_cli_status as cs, claude_subscriptions as subs

    sign_ins["default"] = "before@example.com"
    saved = client.post(BASE + "/subscription/save-current", json={})
    assert saved.status_code == 200, saved.text
    account_id = saved.json()["account_id"]
    before = subs._load()["accounts"][0]
    key = client.post(BASE + "/api-keys", json={"api_key": KEY})
    assert key.status_code == 200, key.text
    expected_keys = key.json()["accounts"]["api_key"]

    if signed_in:
        sign_ins["default"] = "after@example.com"
    else:
        sign_ins.pop("default")
    original_probe = cs.probe_home
    probes = []

    def probe(home_env, resolver=None):
        probes.append(1)
        result = original_probe(home_env, resolver)
        result.checked_at = before["checked_at"] + 10
        return result

    monkeypatch.setattr(cs, "probe_home", probe)
    subs.invalidate_identity()
    original_set = subs.database.set_system_setting
    unavailable, attempts = [True], []

    def save(setting, value):
        assert setting == subs.setting_key()
        attempts.append(value)
        if unavailable[0]:
            raise OSError("example storage write failed")
        original_set(setting, value)

    monkeypatch.setattr(subs.database, "set_system_setting", save)
    with TestClient(client.app, raise_server_exceptions=False) as route:
        response = route.get(BASE)
        assert response.status_code == 200, response.text
        result = response.json()["accounts"]
        assert result["api_key"] == expected_keys
        assert result["subscription_error"] == "storage_unavailable"
        assert result["storage_locked"] is False and result["error"] is None
        row = result["subscription"][0]
        assert row["id"] == account_id
        assert row["signed_in"] is signed_in and row["needs_sign_in"] is not signed_in
        assert row["email"] == ("after@example.com" if signed_in else "before@example.com")
        assert row["checked_at"] == before["checked_at"] + 10
        assert subs._load()["accounts"][0] == before  # failed write did not mutate storage
        assert len(attempts) == 1 and len(probes) == 1

        unavailable[0] = False
        retry = route.get(BASE)
        assert retry.status_code == 200, retry.text
        recovered = retry.json()["accounts"]
        assert "subscription_error" not in recovered
        assert recovered["subscription"] == result["subscription"]
        assert recovered["api_key"] == expected_keys
        assert subs._load()["accounts"][0]["checked_at"] == row["checked_at"]
        assert subs._load()["accounts"][0]["signed_in"] is signed_in
        assert len(attempts) == 2 and len(probes) == 1  # retry even with the same cached probe
        assert route.get(BASE).status_code == 200
        assert len(attempts) == 2  # a successful save stops unnecessary retries
        assert KEY not in response.text


@pytest.fixture
def pending_sign_in(client, sign_ins, tmp_path, monkeypatch):
    """Produce a saved active login and a second pending login through the API."""
    from app.core import claude_subscriptions as subs

    clock = [100.0]
    monkeypatch.setattr(subs.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(subs, "_verified", {})
    monkeypatch.setattr(subs, "_reap_due", False)
    key = client.post(BASE + "/api-keys", json={"api_key": KEY})
    assert key.status_code == 200, key.text
    first = client.post(BASE + "/subscription/add-another").json()["login"]["id"]
    sign_ins[str(tmp_path / "logins" / first)] = "active@example.com"
    clock[0] += subs._PROBE_INTERVAL_SECONDS + 1
    active = client.get(BASE)
    assert active.status_code == 200 and active.json()["accounts"]["subscription"][0]["id"] == first
    pending = client.post(BASE + "/subscription/add-another").json()["login"]["id"]
    credentials = {}
    for account_id in (first, pending):
        path = tmp_path / "logins" / account_id / ".credentials.json"
        credentials[path] = json.dumps({"fixture": account_id})
        path.write_text(credentials[path])
    return {"clock": clock, "state": subs._load(), "credentials": credentials,
            "keys": key.json()["accounts"]["api_key"], "active": first, "pending": pending}


@pytest.mark.parametrize("outcome", ["adopt", "failed"])
@pytest.mark.parametrize("failure", ["all_writes", "poll_only", "reload"])
def test_poll_write_failure_preserves_durable_state_and_retries(
        client, sign_ins, pending_sign_in, tmp_path, monkeypatch, outcome, failure):
    from app.core import claude_cli_home as home, claude_cli_login, claude_cli_status as cs
    from app.core import claude_subscriptions as subs

    setup = pending_sign_in
    before, first, pending = setup["state"], setup["active"], setup["pending"]
    if outcome == "adopt":
        sign_ins[str(tmp_path / "logins" / pending)] = "pending@example.com"
    else:
        monkeypatch.setattr(claude_cli_login, "terminal_supported", lambda: True)
        monkeypatch.setattr(claude_cli_login.login_terminal, "read_for", lambda tag: None)
    setup["clock"][0] += subs._PROBE_INTERVAL_SECONDS + 1
    verified = dict(subs._verified)
    original_probe = cs.probe_home

    def probe(home_env, resolver=None):
        result = original_probe(home_env, resolver)
        result.checked_at = before["accounts"][0]["checked_at"] + 10
        return result

    monkeypatch.setattr(cs, "probe_home", probe)
    subs.invalidate_identity()
    generation = subs._identity_generation
    original_set, original_get = subs.database.set_system_setting, subs.database.get_system_setting
    writes, reload_failures, recovered = [], [], [False]

    def save(setting, value):
        assert setting == subs.setting_key()
        writes.append(json.loads(value))
        if not recovered[0] and (len(writes) == 1 or failure == "all_writes"):
            raise OSError("example polling write failed")
        original_set(setting, value)

    def read(setting):
        if setting == subs.setting_key() and writes and failure == "reload" and not recovered[0]:
            reload_failures.append(1)
            raise OSError("example reload failed")
        return original_get(setting)

    monkeypatch.setattr(subs.database, "set_system_setting", save)
    monkeypatch.setattr(subs.database, "get_system_setting", read)
    response = client.get(BASE)
    assert response.status_code == 200, response.text
    result = response.json()["accounts"]
    assert result["subscription_error"] == "storage_unavailable"
    assert result["api_key"] == setup["keys"]
    if failure == "reload":
        assert result["subscription"] == [] and result["default_in_use"] is True
        assert result["login_pending"] is False and result["login"] is None
        assert reload_failures == [1] and len(writes) == 1
    else:
        assert [(row["id"], row["active"]) for row in result["subscription"]] == [(first, True)]
        assert result["default_in_use"] is False and result["login_pending"] is True
        assert result["login"]["id"] == pending and result["login"]["status"] == "waiting"
        assert result["login"]["error"] is None
        assert len(writes) == 2  # pending mutation, then refresh of reloaded rows
        assert writes[1]["pending"] == before["pending"] and writes[1]["active_id"] == first
    durable = json.loads(original_get(subs.setting_key()))
    assert durable["pending"] == before["pending"] and durable["active_id"] == first
    assert [a["id"] for a in durable["accounts"]] == [first]
    assert subs._identity_generation == generation and subs._verified == verified
    assert subs._reap_due is False
    if outcome == "adopt":
        assert writes[0]["pending"] is None and writes[0]["active_id"] == pending
    else:
        assert writes[0]["pending"]["status"] == "failed" and writes[0]["active_id"] == first
    for path, content in setup["credentials"].items():
        assert path.read_text() == content and home.intact(path.parent.name)

    recovered[0] = True
    setup["clock"][0] += subs._PROBE_INTERVAL_SECONDS + 1
    retry = client.get(BASE)
    assert retry.status_code == 200, retry.text
    result = retry.json()["accounts"]
    assert "subscription_error" not in result and result["api_key"] == setup["keys"]
    durable = subs._load()
    if outcome == "adopt":
        assert durable["pending"] is None and durable["active_id"] == pending
        assert [a["id"] for a in durable["accounts"]] == [first, pending]
        assert [(row["id"], row["active"]) for row in result["subscription"]] == [(first, False), (pending, True)]
        assert result["login_pending"] is False and result["login"] is None
    else:
        assert durable["pending"]["status"] == "failed" and durable["active_id"] == first
        assert result["login_pending"] is True and result["login"]["status"] == "failed"
        assert [a["id"] for a in durable["accounts"]] == [first]
    count = len(writes)
    setup["clock"][0] += subs._PROBE_INTERVAL_SECONDS + 1
    again = client.get(BASE)
    assert again.status_code == 200 and "subscription_error" not in again.json()["accounts"]
    assert len(writes) == count  # no duplicate adoption or repeated failed-status update
    for path, content in setup["credentials"].items():
        assert path.read_text() == content and home.intact(path.parent.name)


@pytest.mark.parametrize("failed_read", ["all", "after_poll"])
def test_subscription_load_failure_keeps_existing_fallback(client, pending_sign_in, monkeypatch, failed_read):
    from app.core import claude_subscriptions as subs

    original_get = subs.database.get_system_setting
    reads = []

    def read(setting):
        if setting == subs.setting_key():
            reads.append(1)
            if failed_read == "all" or len(reads) == 2:
                raise OSError("example load failed")
        return original_get(setting)

    monkeypatch.setattr(subs.database, "get_system_setting", read)
    response = client.get(BASE)
    assert response.status_code == 200, response.text
    result = response.json()["accounts"]
    assert result["subscription"] == [] and result["default_in_use"] is True
    assert result["login_pending"] is False and result["login"] is None
    assert result["subscription_error"] == "storage_unavailable"
    assert result["api_key"] == pending_sign_in["keys"]


def test_pool_reap_write_error_does_not_discard_accounts(client, pending_sign_in, monkeypatch):
    from types import SimpleNamespace
    from app.core import claude_subscriptions as subs, session_pool

    evict = AsyncMock(side_effect=OSError("example teardown write failed"))
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: SimpleNamespace(evict_idle_claude_auth_changed=evict))
    monkeypatch.setattr(subs, "_reap_due", True)
    response = client.get(BASE)
    assert response.status_code == 200, response.text
    result = response.json()["accounts"]
    assert [(row["id"], row["active"]) for row in result["subscription"]] == [(pending_sign_in["active"], True)]
    assert result["login_pending"] is True and result["login"]["id"] == pending_sign_in["pending"]
    assert result["api_key"] == pending_sign_in["keys"]
    assert "subscription_error" not in result  # pool cleanup is not a failed settings write
    evict.assert_awaited_once()


@pytest.mark.parametrize("method,path,body", [
    ("post", "/subscription/save-current", {}),
    ("post", "/subscription/add-another", None),
    ("post", "/subscription/cancel-login", None),
    ("post", "/subscription/account/activate", None),
    ("post", "/subscription/account/sign-in", None),
    ("patch", "/subscription/account", {"label": "New"}),
    ("delete", "/subscription/account", None),
])
def test_subscription_routes_report_unavailable_on_macos(client, monkeypatch, method, path, body):
    from app.core import claude_cli_home
    monkeypatch.setattr(claude_cli_home.sys, "platform", "darwin")
    payload = client.get(BASE).json()["accounts"]
    assert payload["subscription"] == []
    assert payload["subscription_support"]["supported"] is False
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(BASE + path, **kwargs)
    assert response.status_code == 501, response.text
    assert response.json()["detail"]["code"] == "unsupported"


def test_requires_owner():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/settings")
    with TestClient(app) as client:
        assert client.get(BASE).status_code == 403
        assert client.post(BASE + "/api-keys", json={"api_key": KEY}).status_code == 403
        assert client.post(BASE + "/subscription/account/sign-in").status_code == 403

"""Owner-only saved Anthropic key contract; no app DB, provider or CLI access."""
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


@pytest.mark.parametrize("method,path,body", [
    ("post", "/subscription/save-current", {}),
    ("post", "/subscription/add-another", None),
    ("post", "/subscription/cancel-login", None),
    ("post", "/subscription/account/activate", None),
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

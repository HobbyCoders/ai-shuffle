"""Provider account API contract, with real writes and fake upstream sign-ins."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import model_providers as api
from app.api.auth import require_admin
from app.core import encryption
from app.db import database as db

ROOT = "/api/v1/model-providers"


@pytest.fixture
def accounts_client(mock_db):
    # Only this router: no app startup/background pollers or PostgreSQL SET SQL.
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as client:
        yield client
    api._pending_enrollments.clear()
    api._pending_xai_enrollments.clear()


def create(client, kind="openai", enabled=True):
    r = client.post(ROOT, json={"name": "Account", "provider_type": kind,
                               "base_url": "https://example.com", "enabled": enabled})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("method", ["activate", "toggle", "patch", "create"])
def test_select_account_exclusively(accounts_client, method):
    client = accounts_client
    a, b = create(client), create(client, enabled=False)
    assert a["slot"] == "openai:key"
    if method == "patch":
        result = client.patch(f"{ROOT}/{b['id']}", json={"enabled": True})
    elif method == "create":
        result = client.post(ROOT, json={"name": "C", "provider_type": "openai",
                                        "base_url": "https://example.com", "enabled": True})
    else:
        result = client.post(f"{ROOT}/{b['id']}/{method}")
    assert result.status_code == 200, result.text
    selected = result.json()
    rows = client.get(ROOT).json()["providers"]
    assert [r["id"] for r in rows if r["enabled"]] == [selected["id"]]
    assert len(rows) == (3 if method == "create" else 2)
    assert not db.get_model_provider(a["id"])["enabled"]
    # Activate is idempotent, not another toggle.
    assert client.post(f"{ROOT}/{selected['id']}/activate").json()["enabled"]


def test_disable_does_not_enable_another_saved_account(accounts_client):
    client = accounts_client
    create(client)
    b = create(client)
    assert not client.post(f"{ROOT}/{b['id']}/toggle").json()["enabled"]
    assert not any(r["enabled"] for r in client.get(ROOT).json()["providers"])


@pytest.mark.parametrize("method", ["activate", "toggle"])
def test_missing_account_is_404(accounts_client, method):
    r = accounts_client.post(f"{ROOT}/missing/{method}")
    assert r.status_code == 404
    assert r.json()["detail"] == "Model provider not found"


@pytest.mark.parametrize("mode,path,kind", [("chatgpt", "codex", "custom"), ("grok", "xai", "grok")])
def test_device_completions_allow_many_and_select_only_subscription(accounts_client, monkeypatch, mode, path, kind):
    client = accounts_client
    encryption.set_encryption_key("test-password", db)
    key = create(client, "openai" if mode == "chatgpt" else "xai")
    # Several pending sign-ins can coexist, each keyed by row id.
    rows = [create(client, kind, enabled=False) for _ in range(2)]
    oauth = api.codex_oauth if mode == "chatgpt" else api.xai_oauth
    start = SimpleNamespace(device_code="device", code_verifier="verifier", user_code="code",
                            verification_uri="https://example.com/verify", verification_uri_complete=None)
    monkeypatch.setattr(oauth, "start_device_code", AsyncMock(return_value=start))
    monkeypatch.setattr(oauth, "poll_device_code", AsyncMock(return_value=SimpleNamespace(
        access_token="fake-access", refresh_token="fake-refresh", account_id="fake-account", id_token=None)))
    for row in rows:
        assert client.post(f"{ROOT}/{row['id']}/{path}/device/start").status_code == 200
    for row in rows:
        response = client.post(f"{ROOT}/{row['id']}/{path}/device/poll")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "complete"
        assert result["provider"]["enabled"] is True
        assert result["provider"]["slot"] == ("openai:subscription" if mode == "chatgpt" else "grok:subscription")
    assert db.get_model_provider(key["id"])["enabled"]
    assert not db.get_model_provider(rows[0]["id"])["enabled"]
    assert db.get_model_provider(rows[1]["id"])["enabled"]
    # A later token rotation of a saved but inactive login must NOT activate it.
    db.set_codex_token_bundle(rows[0]["id"], access_token="enc:new", refresh_token="enc:new",
                              account_id="old", auth_mode=mode)
    assert not db.get_model_provider(rows[0]["id"])["enabled"]


def test_activate_requires_owner_access(accounts_client):
    accounts_client.app.dependency_overrides.clear()
    response = accounts_client.post(f"{ROOT}/missing/activate")
    assert response.status_code in (401, 403)


def test_custom_gateways_have_no_slot(accounts_client):
    a, b = create(accounts_client, "custom"), create(accounts_client, "custom")
    assert a["slot"] is b["slot"] is None
    assert all(r["enabled"] for r in accounts_client.get(ROOT).json()["providers"])

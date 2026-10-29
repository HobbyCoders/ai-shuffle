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


@pytest.mark.parametrize("method,path,body", [
    ("post", "/subscription/save-current", {}),
    ("post", "/subscription/add-another", None),
    ("post", "/subscription/cancel-login", None),
    ("post", "/subscription/account/activate", None),
    ("patch", "/subscription/account", {"label": "New"}),
    ("delete", "/subscription/account", None),
])
def test_subscription_mutations_report_unsupported_without_cli_access(client, method, path, body):
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

"""Management credentials stay local, encrypted, and separate from inference."""
from unittest.mock import Mock

import pytest

from app.core import encryption
from app.core.openrouter_usage import CREDITS_URL, SETTING_PREFIX
from app.db import database as db

ROOT = "/api/v1/model-providers"


@pytest.fixture
def provider(authenticated_client, monkeypatch):
    from app.core.provider_usage import provider_usage_monitor

    monkeypatch.setattr(provider_usage_monitor, "request_refresh", Mock())
    response = authenticated_client.post(ROOT, json={"name": "Router", "provider_type": "openrouter",
                                                    "base_url": "https://openrouter.ai/api/v1", "auth_token": "model-key"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_admin_required(client, method):
    kwargs = {"json": {"management_key": "secret"}} if method == "put" else {}
    assert getattr(client, method)(f"{ROOT}/unknown/openrouter-usage", **kwargs).status_code in (401, 403)


def test_save_status_and_disconnect(authenticated_client, provider):
    path = f"{ROOT}/{provider}/openrouter-usage"
    result = authenticated_client.put(path, json={"management_key": "management-test-only"})
    assert result.status_code == 200, result.text
    assert result.json()["configured"]
    assert "management-test-only" not in result.text
    stored = db.get_system_setting(SETTING_PREFIX + provider)
    assert encryption.is_encrypted(stored)
    assert encryption.decrypt_value(stored) == "management-test-only"
    assert "management-test-only" not in authenticated_client.get(ROOT).text
    assert "management-test-only" not in authenticated_client.get("/api/settings").text
    assert authenticated_client.get(path).json()["configured"]
    assert not authenticated_client.delete(path).json()["configured"]
    assert db.get_system_setting(SETTING_PREFIX + provider) is None
    assert encryption.decrypt_value(db.get_model_provider(provider, include_token=True)["auth_token"]) == "model-key"


@pytest.mark.parametrize("base_url", ["https://proxy.example", "http://openrouter.ai", "https://openrouter.ai:444", "https://user@openrouter.ai"])
def test_noncanonical_hosts_rejected(authenticated_client, provider, base_url):
    db.update_model_provider(provider, base_url=base_url)
    assert authenticated_client.put(f"{ROOT}/{provider}/openrouter-usage", json={"management_key": "test"}).status_code == 400
    assert db.get_system_setting(SETTING_PREFIX + provider) is None


def test_disabled_provider_cannot_save(authenticated_client, provider):
    db.set_model_provider_enabled(provider, False)
    assert authenticated_client.put(f"{ROOT}/{provider}/openrouter-usage", json={"management_key": "test"}).status_code == 409


def test_local_setting_excluded_from_sync():
    from app.network.policy import COLLECTIONS

    assert not COLLECTIONS["system_settings"].shared_row({"key": SETTING_PREFIX + "example"})


def test_resaving_explicitly_resumes_denied_key_and_delete_removes_credential(authenticated_client, provider):
    from app.core import usage_polling

    path = f"{ROOT}/{provider}/openrouter-usage"
    body = {"management_key": "management-test-only"}
    assert authenticated_client.put(path, json=body).status_code == 200
    budget = usage_polling.identity(CREDITS_URL, {"Authorization": "Bearer management-test-only"})
    assert usage_polling.claim(budget, 0)
    usage_polling.finish(budget, 0, 403)
    assert not usage_polling.claim(budget, 10000)
    assert authenticated_client.put(path, json=body).status_code == 200
    assert usage_polling.claim(budget, 10000)
    assert authenticated_client.delete(f"{ROOT}/{provider}").status_code == 200
    assert db.get_system_setting(SETTING_PREFIX + provider) is None

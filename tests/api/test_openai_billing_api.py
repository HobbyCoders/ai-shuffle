"""OpenAI Admin credentials stay write-only and separate from inference keys."""
from unittest.mock import Mock

import pytest

from app.core import encryption
from app.db import database as db

ROOT = "/api/v1/model-providers"

@pytest.fixture
def provider(authenticated_client, monkeypatch):
    from app.core.provider_usage import provider_usage_monitor
    monkeypatch.setattr(provider_usage_monitor, "request_refresh", Mock())
    response = authenticated_client.post(ROOT, json={"name": "OpenAI", "provider_type": "openai", "base_url": "https://api.openai.com", "auth_token": "model-key"})
    assert response.status_code == 200, response.text
    return response.json()["id"]

@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_admin_required(client, method):
    kwargs = {"json": {"admin_key": "secret", "organization_id": "org-test"}} if method == "put" else {}
    assert getattr(client, method)(f"{ROOT}/unknown/openai-usage", **kwargs).status_code in (401, 403)

@pytest.mark.parametrize("operation", ["connect", "confirm", "cancel", "disconnect", "status"])
def test_legacy_routes_gone(authenticated_client, provider, operation):
    response = authenticated_client.post(f"{ROOT}/{provider}/openai-billing/{operation}")
    assert response.status_code == 410


def test_save_status_and_disconnect_do_not_change_inference_key(authenticated_client, provider):
    path = f"{ROOT}/{provider}/openai-usage"
    response = authenticated_client.put(path, json={"admin_key": "admin-secret", "organization_id": "org-test"})
    assert response.status_code == 200, response.text
    assert response.json()["configured"] is True
    assert "admin-secret" not in response.text
    row = db.get_model_provider(provider, include_token=True)
    assert encryption.is_encrypted(row["openai_admin_key"])
    assert encryption.decrypt_value(row["auth_token"]) == "model-key"
    assert "openai_admin_key" not in db.get_model_provider(provider)
    assert "admin-secret" not in authenticated_client.get(ROOT).text
    assert "openai_admin_key" not in authenticated_client.get(ROOT).text
    assert authenticated_client.get(path).json()["organization_id"] == "org-test"
    assert authenticated_client.delete(path).json()["configured"] is False
    row = db.get_model_provider(provider, include_token=True)
    assert row["openai_admin_key"] is None
    assert encryption.decrypt_value(row["auth_token"]) == "model-key"


def test_custom_host_and_bad_org_rejected(authenticated_client, provider):
    path = f"{ROOT}/{provider}/openai-usage"
    assert authenticated_client.put(path, json={"admin_key": "secret", "organization_id": "bad"}).status_code == 422
    db.update_model_provider(provider, base_url="https://example.test")
    assert authenticated_client.put(path, json={"admin_key": "secret", "organization_id": "org-test"}).status_code == 400


def test_admin_key_is_local_only():
    from app.network.policy import COLLECTIONS
    policy = COLLECTIONS["model_providers"]
    assert "openai_admin_key" in policy.local_fields
    assert "openai_admin_key" not in policy.fields


def test_resaving_admin_key_resumes_both_official_auth_pauses(authenticated_client, provider):
    from app.core import usage_polling as polling
    body = {"admin_key": "admin-secret", "organization_id": "org-test"}
    path = f"{ROOT}/{provider}/openai-usage"
    assert authenticated_client.put(path, json=body).status_code == 200
    headers = {"Authorization": "Bearer admin-secret", "OpenAI-Organization": "org-test"}
    keys = [polling.identity("https://api.openai.com/v1/organization/" + endpoint, headers) for endpoint in ("costs", "usage/completions")]
    for key in keys:
        assert polling.claim(key, 0)
        polling.finish(key, 0, 403)
        assert not polling.claim(key, 10000)
    assert authenticated_client.put(path, json=body).status_code == 200
    for key in keys:
        assert polling.claim(key, 10000)

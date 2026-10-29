"""Shared discovery is independent of curation and revision-guarded edits."""
import json
from unittest.mock import AsyncMock

import pytest

from app.api import model_providers as api
from app.core import encryption, model_catalogs
from app.core.global_events import global_events
from app.db import database as db
from app.network.policy import is_shared_setting_key

ROOT = "/api/v1/model-providers"


@pytest.fixture
def provider(authenticated_client, mock_db):
    encryption.set_encryption_key("fixture-password", db)
    response = authenticated_client.post(ROOT, json={
        "name": "Controlled fixture", "provider_type": "custom",
        "base_url": "http://127.0.0.1:19123/v1", "api_dialect": "openai",
        "auth_token": "controlled-fixture-credential",
        "models": [{"id": "saved", "label": "Saved"}],
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_success_shares_sanitized_snapshot_not_selection(authenticated_client, provider, monkeypatch):
    upstream = AsyncMock(return_value=[
        {"id": "discovered", "label": "Discovered", "context_window": 200000,
         "auth_token": "fixture-upstream-secret", "headers": {"Authorization": "fixture-secret"},
         "pricing": {"prompt": "0.1", "api_key": "fixture-secret"},
         "reasoning_capabilities": {"version": 1, "effort_levels": ["high"],
                                    "default_effort": "high", "secret": "fixture-secret"}},
        {"id": "gpt-image-2", "label": "Image"},
    ])
    monkeypatch.setattr(api, "fetch_upstream_models", upstream)
    monkeypatch.setattr(db, "get_install", lambda _: {"device_name": "Fixture worker A"})
    events = []
    monkeypatch.setattr(global_events, "resources_changed", lambda resource, **kw: events.append(resource))
    response = authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models")
    assert response.status_code == 200, response.text
    key = model_catalogs.key(provider["id"])
    assert is_shared_setting_key(key)
    value = db.get_system_setting(key)
    assert len(value.encode()) <= model_catalogs.MAX_BYTES
    snapshot = json.loads(value)
    assert snapshot["models"][0]["id"] == "discovered"
    assert snapshot["models"][0]["pricing"] == {"prompt": "0.1"}
    assert snapshot["hidden"][0]["id"] == "gpt-image-2"
    assert snapshot["recommended"] == []
    assert snapshot["fetched_at"] and snapshot["fetched_by_device"]
    assert snapshot["fetched_by_name"] == "Fixture worker A"
    assert all(secret not in value for secret in ("fixture-secret", "fixture-upstream-secret", "controlled-fixture-credential", "Authorization", "auth_token", "base_url"))
    assert "settings" in events
    assert db.get_model_provider(provider["id"])["models_json"][0]["id"] == "saved"
    assert authenticated_client.get(f"{ROOT}/{provider['id']}/model-catalog").json() == snapshot


@pytest.mark.parametrize("failure", ["http_401", "http_500", "timeout"])
def test_failed_fetch_keeps_last_snapshot(authenticated_client, provider, monkeypatch, failure):
    db.set_system_setting(model_catalogs.key(provider["id"]), '{"models":[]}')
    monkeypatch.setattr(api, "fetch_upstream_models", AsyncMock(side_effect=api.UpstreamModelsError(failure)))
    response = authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models")
    assert response.status_code == 502
    assert db.get_system_setting(model_catalogs.key(provider["id"])) == '{"models":[]}'


def test_provider_delete_removes_catalog(authenticated_client, provider):
    db.set_system_setting(model_catalogs.key(provider["id"]), '{"models":[]}')
    assert authenticated_client.delete(f"{ROOT}/{provider['id']}").status_code == 200
    assert db.get_system_setting(model_catalogs.key(provider["id"])) is None
    assert authenticated_client.get(f"{ROOT}/{provider['id']}/model-catalog").status_code == 404


def test_refresh_finishing_after_delete_cannot_resurrect_snapshot(authenticated_client, provider, monkeypatch):
    async def upstream(*args, **kwargs):
        db.delete_model_provider(provider["id"])
        return [{"id": "discovered", "label": "Discovered"}]
    monkeypatch.setattr(api, "fetch_upstream_models", upstream)
    assert authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models").status_code == 200
    assert db.get_system_setting(model_catalogs.key(provider["id"])) is None


def test_oversized_catalog_preserves_previous_snapshot(authenticated_client, provider, monkeypatch):
    db.set_system_setting(model_catalogs.key(provider["id"]), '{"models":[]}')
    monkeypatch.setattr(api, "fetch_upstream_models", AsyncMock(return_value=[
        {"id": "too-large", "label": "x" * model_catalogs.MAX_BYTES},
    ]))
    assert authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models").status_code == 502
    assert db.get_system_setting(model_catalogs.key(provider["id"])) == '{"models":[]}'


def test_stale_curation_patch_is_refused(authenticated_client, provider):
    assert provider["revision"]
    path = f"{ROOT}/{provider['id']}"
    first = authenticated_client.patch(path, json={
        "models": [{"id": "remote", "label": "Remote"}],
        "expected_revision": provider["revision"],
    })
    assert first.status_code == 200, first.text
    assert first.json()["revision"] != provider["revision"]
    stale = authenticated_client.patch(path, json={
        "models": [{"id": "stale", "label": "Stale"}],
        "expected_revision": provider["revision"],
    })
    assert stale.status_code == 409, stale.text
    assert db.get_model_provider(provider["id"])["models_json"][0]["id"] == "remote"


def test_catalog_read_requires_owner(client):
    assert client.get(f"{ROOT}/missing/model-catalog").status_code in (401, 403)

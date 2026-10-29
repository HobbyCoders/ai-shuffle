"""Shared discovery is independent of curation and revision-guarded edits."""
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import model_providers as api
from app.core import encryption, model_catalogs
from app.core.global_events import global_events
from app.db import database as db
from app.network.policy import is_shared_setting_key

ROOT = "/api/v1/model-providers"


def _credential_verdict(provider_id: str) -> str | None:
    """Read the private verdict without exposing it on public provider rows."""
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT credential_status FROM model_providers WHERE id = %s", (provider_id,))
        return cursor.fetchone()["credential_status"]


@pytest.fixture
def provider(authenticated_client, mock_db, monkeypatch):
    monkeypatch.setattr(model_catalogs, "_binding_key", lambda: (b"fixture-network-hmac-key", "network"))
    encryption.set_encryption_key("fixture-password", db)
    response = authenticated_client.post(ROOT, json={
        "name": "Controlled fixture", "provider_type": "custom",
        "base_url": "http://127.0.0.1:19123/v1", "api_dialect": "openai",
        "auth_token": "controlled-fixture-credential",
        "models": [{"id": "saved", "label": "Saved"}],
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_binding_is_keyed_per_network_and_local_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public fields and a guessed low-entropy token cannot verify a binding alone."""
    from app.core import pairing
    from app.network import engine

    row = {"provider_type": "custom", "base_url": "https://gateway.test/v1", "auth_token": "weak-token"}
    monkeypatch.setattr(engine, "cached_context", lambda: SimpleNamespace(network_key=b"a" * 32))
    first = model_catalogs.binding(row)
    assert first.startswith("v2:network:")
    assert model_catalogs.binding(dict(row)) == first
    monkeypatch.setattr(engine, "cached_context", lambda: SimpleNamespace(network_key=b"b" * 32))
    assert model_catalogs.binding(row) != first
    monkeypatch.setattr(engine, "cached_context", lambda: None)
    monkeypatch.setattr(pairing, "pairing_key", lambda: "fixture-local-key-a")
    local = model_catalogs.binding(row)
    assert local.startswith("v2:local:") and local != first
    assert model_catalogs.binding(row) == local
    monkeypatch.setattr(pairing, "pairing_key", lambda: "fixture-local-key-b")
    assert model_catalogs.binding(row) != local


def test_unpaired_discovery_is_not_shared(
    authenticated_client: TestClient, provider: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local-only HMAC keeps live discovery usable without publishing a snapshot."""
    monkeypatch.setattr(model_catalogs, "_binding_key", lambda: (b"fixture-local-hmac-key", "local"))
    monkeypatch.setattr(api, "fetch_upstream_models", AsyncMock(return_value=[{"id": "local-model", "label": "Local"}]))
    response = authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models")
    assert response.status_code == 200, response.text
    assert response.json()["binding"].startswith("v2:local:")
    assert response.json()["models"][0]["id"] == "local-model"
    assert response.json()["fetched_at"] is None
    assert db.get_system_setting(model_catalogs.key(provider["id"])) is None


@pytest.mark.parametrize("mode", ["chatgpt", "grok"])
def test_accountless_subscription_retry_uses_successful_request_binding(
    authenticated_client: TestClient, provider: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """Drive 401, token rotation, successful retry, and snapshot consumption end to end."""
    from app.core import http_client, xai_token_refresh

    db.set_codex_token_bundle(provider["id"], access_token=encryption.encrypt_value("fixture-expired-access"),
                              refresh_token=encryption.encrypt_value("fixture-refresh"), account_id=None, auth_mode=mode)
    before = model_catalogs.binding(db.get_model_provider(provider["id"], include_token=True))
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = [httpx.Response(401), httpx.Response(200, json={"data": [{"id": "retried-model"}]})]
    monkeypatch.setattr(http_client, "get_http_client", lambda: client)

    async def refresh(*args: Any, **kwargs: Any) -> bool:
        db.set_codex_token_bundle(provider["id"], access_token=encryption.encrypt_value("fixture-fresh-access"),
                                  refresh_token=encryption.encrypt_value("fixture-next-refresh"), account_id=None, auth_mode=mode)
        return True

    module = api.codex_token_refresh if mode == "chatgpt" else xai_token_refresh
    monkeypatch.setattr(module, "refresh_on_401_once", refresh)
    response = authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models")
    assert response.status_code == 200, response.text
    current = model_catalogs.binding(db.get_model_provider(provider["id"], include_token=True))
    assert current != before
    assert response.json()["binding"] == current
    assert response.json()["fetched_at"]
    assert response.json()["models"][0]["id"] == "retried-model"
    assert client.get.call_args_list[0].kwargs["headers"]["Authorization"] == "Bearer fixture-expired-access"
    assert client.get.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer fixture-fresh-access"
    assert authenticated_client.get(f"{ROOT}/{provider['id']}/model-catalog").json()["binding"] == current


def test_inflight_old_key_catalog_is_not_saved(
    authenticated_client: TestClient, provider: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real PATCH during upstream I/O must refuse the old credential's result."""
    checked = provider["catalog_binding"]

    async def upstream(*args: Any, **kwargs: Any) -> list[dict[str, str]]:
        await api.update_provider(provider["id"], api.ModelProviderUpdate(auth_token="fixture-new-key"), token="fixture-token")
        return [{"id": "old-key-only", "label": "Old key only"}]

    monkeypatch.setattr(api, "fetch_upstream_models", upstream)
    response = authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models")
    assert response.status_code == 200, response.text
    assert response.json()["binding"] == checked
    assert response.json()["fetched_at"] is None
    assert db.get_system_setting(model_catalogs.key(provider["id"])) is None
    assert authenticated_client.get(f"{ROOT}/{provider['id']}/model-catalog").json() is None


@pytest.mark.parametrize("changes", [
    {"auth_token": "fixture-synced-key"},
    {"base_url": "http://127.0.0.1:19125/v1"},
    {"api_dialect": "anthropic"},
])
def test_synced_binding_change_hides_delayed_old_catalog(
    authenticated_client: TestClient, provider: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, str],
) -> None:
    """Materialized provider fields invalidate reads even with no DELETE/tombstone."""
    monkeypatch.setattr(api, "fetch_upstream_models", AsyncMock(return_value=[{"id": "old-key-only", "label": "Old"}]))
    assert authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models").status_code == 200
    key = model_catalogs.key(provider["id"])
    old = db.get_system_setting(key)
    changes = dict(changes)
    if "auth_token" in changes:
        changes["auth_token"] = encryption.encrypt_value(changes["auth_token"])
    # Direct field materialization, not the PATCH path (which may tidy by DELETE).
    with db.get_db() as conn:
        cursor = conn.cursor()
        for column, value in changes.items():
            cursor.execute(f"UPDATE model_providers SET {column} = %s WHERE id = %s", (value, provider["id"]))
    db.set_system_setting(key, old)  # A delayed old-worker record is still stale.
    assert db.get_system_setting(key) == old
    assert model_catalogs.read(provider["id"]) is None
    assert authenticated_client.get(f"{ROOT}/{provider['id']}/model-catalog").json() is None
    current = authenticated_client.get(ROOT).json()["providers"][0]
    assert current["catalog_binding"] != provider["catalog_binding"]


def test_binding_is_stable_across_ciphertexts_and_curation(
    provider: dict[str, Any],
) -> None:
    """Workers with the same decrypted key agree without hashing token ciphertext."""
    row = db.get_model_provider(provider["id"], include_token=True)
    original = model_catalogs.binding(row)
    assert original == provider["catalog_binding"]
    other = {**row, "auth_token": encryption.encrypt_value("controlled-fixture-credential"),
             "revision": "another-worker", "models_json": []}
    assert other["auth_token"] != row["auth_token"]
    assert model_catalogs.binding(other) == original
    assert model_catalogs.binding({**row, "auth_token": encryption.encrypt_value("fixture-other-key")}) != original


@pytest.mark.parametrize("mode", ["chatgpt", "grok"])
def test_subscription_binding_uses_account_not_rotating_tokens(
    provider: dict[str, Any], mode: str,
) -> None:
    """Normal subscription renewal preserves a binding, but account switching does not."""
    row = {**db.get_model_provider(provider["id"], include_token=True), "auth_mode": mode,
           "codex_account_id": "fixture-account-a", "codex_access_token": encryption.encrypt_value("fixture-access-one")}
    original = model_catalogs.binding(row)
    assert original
    assert model_catalogs.binding({**row, "codex_access_token": encryption.encrypt_value("fixture-access-two")}) == original
    assert model_catalogs.binding({**row, "codex_account_id": "fixture-account-b"}) != original


def test_unbound_legacy_snapshot_is_not_usable(
    authenticated_client: TestClient, provider: dict[str, Any],
) -> None:
    """An older worker's snapshot is not evidence for any credential binding."""
    db.set_system_setting(model_catalogs.key(provider["id"]), '{"source":"upstream","models":[]}')
    assert authenticated_client.get(f"{ROOT}/{provider['id']}/model-catalog").json() is None


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
    assert snapshot["binding"] == provider["catalog_binding"] == response.json()["binding"]
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


def test_oversized_catalog_returns_live_discovery_and_keeps_previous_snapshot(authenticated_client, provider, monkeypatch, caplog):
    db.set_system_setting(model_catalogs.key(provider["id"]), '{"models":[]}')
    monkeypatch.setattr(api, "fetch_upstream_models", AsyncMock(return_value=[
        {"id": "too-large", "label": "x" * model_catalogs.MAX_BYTES},
    ]))
    response = authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models")
    assert response.status_code == 200, response.text
    assert response.json()["models"][0]["id"] == "too-large"
    assert response.json()["fetched_at"] is None
    assert db.get_system_setting(model_catalogs.key(provider["id"])) == '{"models":[]}'
    assert sum("too large to share" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("updates", [
    {"auth_token": "fixture-replacement-key"},
    {"base_url": "http://127.0.0.1:19124/v1"},
])
def test_binding_change_invalidates_snapshot_with_credential_verdict(authenticated_client, provider, monkeypatch, updates):
    monkeypatch.setattr(api, "fetch_upstream_models", AsyncMock(return_value=[{"id": "saved", "label": "Saved"}]))
    assert authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models").status_code == 200
    assert db.get_system_setting(model_catalogs.key(provider["id"])) is not None
    assert _credential_verdict(provider["id"]) is not None
    changed = authenticated_client.patch(f"{ROOT}/{provider['id']}", json=updates)
    assert changed.status_code == 200, changed.text
    assert _credential_verdict(provider["id"]) is None
    assert db.get_system_setting(model_catalogs.key(provider["id"])) is None
    monkeypatch.setattr(api, "fetch_upstream_models", AsyncMock(side_effect=api.UpstreamModelsError("http_401")))
    assert authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models").status_code == 502
    assert db.get_system_setting(model_catalogs.key(provider["id"])) is None


def test_unchanged_binding_or_refused_patch_keeps_snapshot(authenticated_client, provider):
    key = model_catalogs.key(provider["id"])
    db.set_system_setting(key, '{"models":[]}')
    unchanged = authenticated_client.patch(f"{ROOT}/{provider['id']}", json={
        "base_url": provider["base_url"], "auth_token": "", "name": "Renamed fixture",
    })
    assert unchanged.status_code == 200
    assert db.get_system_setting(key) == '{"models":[]}'
    refused = authenticated_client.patch(f"{ROOT}/{provider['id']}", json={
        "auth_token": "fixture-refused-key", "expected_revision": provider["revision"],
    })
    assert refused.status_code == 409
    assert db.get_system_setting(key) == '{"models":[]}'


def test_large_priced_catalog_shares_compact_copy_but_returns_full_discovery(authenticated_client, provider, monkeypatch):
    models = [{
        "id": f"fixture-{n:04d}-" + "a" * 32, "label": "Example model " + "x" * 40,
        "context_window": 200000,
        "pricing": {field: "0.000000000123456789" for field in model_catalogs._PRICE_FIELDS},
        "reasoning_capabilities": {"version": 1, "effort_levels": ["low", "medium", "high"], "default_effort": "high"},
    } for n in range(2000)]
    assert len(json.dumps(models, separators=(",", ":")).encode()) > model_catalogs.MAX_BYTES
    monkeypatch.setattr(api, "fetch_upstream_models", AsyncMock(return_value=models))
    response = authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models")
    assert response.status_code == 200, response.text
    assert response.json()["models"][0]["pricing"] == models[0]["pricing"]
    assert response.json()["fetched_at"]
    value = db.get_system_setting(model_catalogs.key(provider["id"]))
    assert len(value.encode()) <= model_catalogs.MAX_BYTES
    shared = json.loads(value)
    assert len(shared["models"]) == 2000
    assert all("pricing" not in model for model in shared["models"])
    assert shared["fetched_at"] == response.json()["fetched_at"]


def test_pricing_is_omitted_before_context_and_reasoning(authenticated_client, provider, monkeypatch):
    model = {
        "id": "saved", "label": "Saved", "context_window": 200000,
        "pricing": {"prompt": "0." + "1" * 2000},
        "reasoning_capabilities": {"version": 1, "effort_levels": ["high"], "default_effort": "high"},
    }
    monkeypatch.setattr(model_catalogs, "MAX_BYTES", 1024)
    monkeypatch.setattr(api, "fetch_upstream_models", AsyncMock(return_value=[model]))
    response = authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models")
    assert response.status_code == 200, response.text
    assert response.json()["models"][0]["pricing"] == model["pricing"]
    shared = model_catalogs.read(provider["id"])["models"][0]
    assert "pricing" not in shared
    assert shared["context_window"] == model["context_window"]
    assert shared["reasoning_capabilities"] == model["reasoning_capabilities"]


def test_sharing_error_returns_discovery_without_logging_exception(authenticated_client, provider, monkeypatch, caplog):
    monkeypatch.setattr(api, "fetch_upstream_models", AsyncMock(return_value=[{"id": "saved", "label": "Saved"}]))
    monkeypatch.setattr(model_catalogs, "save", Mock(side_effect=RuntimeError("fixture-private-error-detail")))
    response = authenticated_client.post(f"{ROOT}/{provider['id']}/fetch-models")
    assert response.status_code == 200, response.text
    assert response.json()["models"][0]["id"] == "saved"
    assert response.json()["fetched_at"] is None
    assert sum("Could not share model catalog" in r.message for r in caplog.records) == 1
    assert "fixture-private-error-detail" not in caplog.text


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

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


@pytest.mark.parametrize("method", ["put", "delete"])
def test_manual_balance_owner_only(client, method):
    kwargs = {"json": {"balance_usd": 20}} if method == "put" else {}
    assert getattr(client, method)(f"{ROOT}/unknown/openai-usage/balance", **kwargs).status_code in (401, 403)


@pytest.mark.parametrize("amount", [-1, "NaN", "Infinity", 1_000_000_001])
def test_invalid_manual_balance(authenticated_client, provider, amount):
    assert authenticated_client.put(f"{ROOT}/{provider}/openai-usage/balance", json={"balance_usd": amount}).status_code == 422


def test_replacing_key_clears_baseline_but_reencrypting_same_key_preserves_it(authenticated_client, provider):
    import asyncio
    from app.api.usage import _load_provider_usage

    path = f"{ROOT}/{provider}"
    response = authenticated_client.put(path + "/openai-usage/balance", json={"balance_usd": 20.50})
    assert response.status_code == 200
    baseline = response.json()["manual_balance"]
    original_blob = db.get_model_provider(provider, include_token=True)["auth_token"]
    for patch in [{"name": "Renamed"}, {"auth_token": ""}, {"auth_token": "model-key"}]:
        response = authenticated_client.patch(path, json=patch)
        assert response.status_code == 200, response.text
        assert authenticated_client.get(path + "/openai-usage").json()["manual_balance"] == baseline
    # The API re-encrypts even an unchanged plaintext key; that is not a new account.
    same_key_blob = db.get_model_provider(provider, include_token=True)["auth_token"]
    assert same_key_blob != original_blob
    assert encryption.decrypt_value(same_key_blob) == "model-key"
    response = authenticated_client.patch(path, json={"auth_token": "other-account-key"})
    assert response.status_code == 200, response.text
    row = db.get_model_provider(provider, include_token=True)
    assert row["id"] == provider
    assert encryption.decrypt_value(row["auth_token"]) == "other-account-key"
    assert row["manual_balance_usd"] is None
    assert row["manual_balance_set_at"] is None
    assert authenticated_client.get(path + "/openai-usage").json()["manual_balance"] is None
    assert not any(entry.provider_id == provider for entry in asyncio.run(_load_provider_usage()))


def test_manual_balance_tracks_recorded_provider_costs_and_reset(authenticated_client, provider, monkeypatch):
    import asyncio
    from datetime import datetime, timezone
    from app.api.usage import _load_provider_usage
    from app.db.base import get_db

    path = f"{ROOT}/{provider}/openai-usage"
    now = datetime(2026, 1, 15, 12, 0, 0, 500000, tzinfo=timezone.utc)
    monkeypatch.setattr("app.api.openai_billing.time.time", lambda: now.timestamp())
    assert asyncio.run(_load_provider_usage()) == []
    response = authenticated_client.put(path + "/balance", json={"balance_usd": 20.50})
    assert response.status_code == 200, response.text
    manual = response.json()["manual_balance"]
    assert manual["manual_balance"] == 20.50 and manual["estimated_spend"] == 0
    assert manual["set_at"] == int(now.timestamp() * 1000)
    db.create_model_provider("other", "Other", "openai", "https://api.openai.com")
    # Drive the same booking function that records priced harness usage. The
    # cutoff excludes an earlier turn in the same second, not the whole day.
    for model, pid, cost, at, estimated in [
        ("before", provider, 9, "2026-01-15T12:00:00.499+00:00", False),
        ("after", provider, 1.25, "2026-01-15T12:00:00.501+00:00", False),
        ("guessed", provider, 0.75, "2026-01-15T12:00:01+00:00", True),
        ("other", "other", 8, "2026-01-15T12:00:01+00:00", False),
        ("unattributed", None, 7, "2026-01-15T12:00:01+00:00", False),
        ("unpriced", provider, 0, "2026-01-15T12:00:01+00:00", False),
    ]:
        db.log_usage(None, None, model, 100, 20, cost, 1,
                     provider_id=pid, provider_type="openai" if pid else None,
                     billing="api", usage_estimated=estimated)
        with get_db() as conn:
            conn.cursor().execute("UPDATE usage_log SET created_at = %s WHERE model = %s", (at, model))
    manual = authenticated_client.get(path).json()["manual_balance"]
    assert manual == {"balance_remaining": 18.50, "manual_balance": 20.50,
                      "estimated_spend": 2, "set_at": int(now.timestamp() * 1000), "unpriced_turns": 1}
    entry, = asyncio.run(_load_provider_usage())
    assert entry.credits == manual and entry.status == "fresh"
    assert entry.source_info.id == "openai_manual"
    assert entry.source_info.collection == "local_estimate"
    assert entry.last_error_kind is None and entry.fetched_at == manual["set_at"]
    assert entry.windows == []
    # Zero is a saved balance, not an absent one. A reset starts a new cutoff.
    monkeypatch.setattr("app.api.openai_billing.time.time", lambda: now.timestamp() + 60)
    reset = authenticated_client.put(path + "/balance", json={"balance_usd": 0}).json()["manual_balance"]
    assert reset["balance_remaining"] == 0 and reset["estimated_spend"] == 0
    assert reset["set_at"] > manual["set_at"]
    assert authenticated_client.delete(path + "/balance").json()["manual_balance"] is None
    assert db.get_model_provider(provider)["manual_balance_set_at"] is None
    assert asyncio.run(_load_provider_usage()) == []
    assert encryption.decrypt_value(db.get_model_provider(provider, include_token=True)["auth_token"]) == "model-key"


def test_admin_reading_and_failures_keep_manual_estimate(authenticated_client, provider):
    import asyncio
    from app.api.usage import _load_provider_usage
    from app.db.provider_usage_snapshots import record_provider_usage_failure
    path = f"{ROOT}/{provider}/openai-usage"
    assert authenticated_client.put(path, json={"admin_key": "admin", "organization_id": "org-test"}).status_code == 200
    row = db.get_model_provider(provider, include_token=True)
    record_provider_usage_failure(provider, "openai", "billing_access_denied",
                                  expected_binding=(row["openai_billing_org_id"], row["openai_billing_instance_id"]),
                                  expected_admin_key=row["openai_admin_key"])
    entry, = asyncio.run(_load_provider_usage())
    assert entry.last_error_kind == "billing_access_denied"
    assert entry.source_info.id == "openai_admin"
    assert authenticated_client.put(path + "/balance", json={"balance_usd": 10}).status_code == 200
    entry, = asyncio.run(_load_provider_usage())
    assert entry.credits["manual_balance"] == 10
    db.upsert_provider_usage_snapshot(provider, "openai", None, [], {"spend_month": 4},
                                      {"source": "openai_admin"}, 123,
                                      expected_binding=(row["openai_billing_org_id"], row["openai_billing_instance_id"]),
                                      expected_admin_key=row["openai_admin_key"])
    entry, = asyncio.run(_load_provider_usage())
    assert entry.source_info.id == "openai_admin"
    assert entry.credits["spend_month"] == 4 and entry.credits["manual_balance"] == 10
    assert authenticated_client.delete(path + "/balance").json()["configured"] is True


def test_manual_balance_sync_policy_and_provider_validation(authenticated_client, provider):
    from app.network.policy import COLLECTIONS
    policy = COLLECTIONS["model_providers"]
    assert {"manual_balance_usd", "manual_balance_set_at"} <= set(policy.fields)
    assert "manual_balance_usd" not in policy.encrypted
    db.update_model_provider(provider, base_url="https://example.test")
    assert authenticated_client.put(f"{ROOT}/{provider}/openai-usage/balance", json={"balance_usd": 20}).status_code == 400


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

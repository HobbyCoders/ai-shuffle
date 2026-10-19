"""API balances: real producers, encrypted bindings, failures and precedence."""
import asyncio
import json
import time
from unittest.mock import Mock

import httpx
import pytest

from app.core import api_key_balances as balances, encryption
from app.core.config import settings
from app.db import database as db, provider_balances

ROOT = "/api/v1/model-providers"


@pytest.fixture
def provider(authenticated_client, monkeypatch):
    from app.core.provider_usage import provider_usage_monitor
    monkeypatch.setattr(provider_usage_monitor, "request_refresh", Mock())
    response = authenticated_client.post(ROOT, json={
        "name": "OpenAI", "provider_type": "openai", "base_url": "https://api.openai.com/v1",
        "auth_token": "inference-test-key",
    })
    assert response.status_code == 200, response.text
    return response.json()["id"]


def configure(client, provider, key="sess-test-only"):
    response = client.put(f"{ROOT}/{provider}/balance-access", json={"key": key})
    assert response.status_code == 200, response.text
    return response


def test_write_only_status_and_delete(authenticated_client, provider, caplog):
    response = configure(authenticated_client, provider)
    row = db.get_model_provider(provider, include_token=True)
    assert encryption.is_encrypted(row["balance_key"])
    assert encryption.decrypt_value(row["balance_key"]) == "sess-test-only"
    assert "balance_key" not in db.get_model_provider(provider)
    for text in [response.text, authenticated_client.get(ROOT).text,
                 authenticated_client.get(f"{ROOT}/{provider}/balance-access").text, caplog.text]:
        assert "sess-test-only" not in text
        assert row["balance_key"] not in text
    assert authenticated_client.delete(f"{ROOT}/{provider}/balance-access").json()["configured"] is False
    assert encryption.decrypt_value(db.get_model_provider(provider, include_token=True)["auth_token"]) == "inference-test-key"


@pytest.mark.parametrize("method", ["get", "put", "delete", "post"])
def test_owner_only(client, method):
    suffix = "/sign-in" if method == "post" else ""
    kwargs = {"json": {"key": "sess-test-only"}} if method == "put" else {}
    assert getattr(client, method)(f"{ROOT}/unknown/balance-access{suffix}", **kwargs).status_code in (401, 403)


@pytest.mark.parametrize("key", ["sk-test", "oauth-test", "sess-bad key"])
def test_not_api_or_oauth_key(authenticated_client, provider, key):
    response = authenticated_client.put(f"{ROOT}/{provider}/balance-access", json={"key": key})
    assert response.status_code == 400
    assert key not in response.text


def test_real_balance_beats_manual_and_admin(authenticated_client, provider, monkeypatch):
    from app.api.usage import _load_provider_usage
    authenticated_client.put(f"{ROOT}/{provider}/openai-usage/balance", json={"balance_usd": 100})
    configure(authenticated_client, provider)
    calls = []
    async def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"total_available": 12.34, "total_granted": 20, "total_used": 7.66,
                                        "ignored_secret": "do-not-copy"}, request=httpx.Request("GET", url))
    monkeypatch.setattr(balances, "usage_get", fake_get)
    asyncio.run(balances.poll({"id": provider}))
    assert calls[0][0] == balances.OPENAI_CREDIT_URL
    assert calls[0][1]["headers"] == {"Authorization": "Bearer sess-test-only"}
    entry = next(e for e in asyncio.run(_load_provider_usage()) if e.provider_id == provider)
    assert entry.credits["balance_remaining"] == 12.34
    assert "manual_balance" not in entry.credits
    assert entry.source == "OpenAI Platform balance (unofficial)"
    assert entry.fetched_at
    assert "do-not-copy" not in db.get_model_provider(provider, include_token=True)["balance_snapshot"]


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize("kind", ["openai", "xai"])
def test_auth_failure_hides_old_balance(authenticated_client, provider, monkeypatch, caplog, code, kind):
    if kind == "xai":
        db.update_model_provider(provider, provider_type="xai", base_url="https://api.x.ai/v1")
    configure(authenticated_client, provider, "sess-test-only" if kind == "openai" else "management-test-only")
    row = db.get_model_provider(provider, include_token=True)
    provider_balances.snapshot(row, {"credits": {"balance_remaining": 99}, "fetched_at": int(time.time() * 1000), "team_id": "team-test"})
    async def fake_get(url, **kwargs):
        return httpx.Response(code, text="echoed-secret: sess-test-only management-test-only", request=httpx.Request("GET", url))
    monkeypatch.setattr(balances, "usage_get", fake_get)
    asyncio.run(balances.poll({"id": provider}))
    result = authenticated_client.get(f"{ROOT}/{provider}/balance-access")
    assert result.json()["needs_reauth"] is True
    assert result.json()["credits"] is None
    assert "sess-test-only" not in caplog.text + result.text
    assert "management-test-only" not in caplog.text + result.text


def test_xai_resolves_team_with_model_key_and_polls_management(authenticated_client, provider, monkeypatch):
    db.update_model_provider(provider, provider_type="xai", base_url="https://api.x.ai/v1")
    configure(authenticated_client, provider, "management-test-only")
    calls = []
    async def fake_get(url, **kwargs):
        calls.append((url, kwargs["headers"]["Authorization"]))
        if url == balances.XAI_KEY_URL:
            body = {"team_id": "team-test"}
        elif url.endswith("/prepaid/balance"):
            body = {"total": {"val": "-1234"}, "changes": [{"amount": {"val": "999999"}}]}
        else:
            body = {"coreInvoice": {"totalWithCorr": {"val": "100"}, "prepaidCreditsUsed": {"val": "234"}}, "effectiveSpendingLimit": "20000"}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(balances, "usage_get", fake_get)
    asyncio.run(balances.poll({"id": provider}))
    assert calls[0] == (balances.XAI_KEY_URL, "Bearer inference-test-key")
    assert all(auth == "Bearer management-test-only" for _, auth in calls[1:])
    status = authenticated_client.get(f"{ROOT}/{provider}/balance-access").json()
    assert status["credits"] == {"balance_remaining": 12.34, "currency": "USD", "spend_month": 3.34, "spending_limit": 200}
    assert status["source"] == "xAI Management API"


@pytest.mark.parametrize("change", [{"auth_token": "other"}, {"enabled": False}, {"base_url": "https://example.com"}, {"provider_type": "custom"}])
def test_binding_invalidated_and_inflight_snapshot_rejected(authenticated_client, provider, change):
    configure(authenticated_client, provider)
    old = db.get_model_provider(provider, include_token=True)
    db.update_model_provider(provider, **change)
    assert not provider_balances.snapshot(old, {"credits": {"balance_remaining": 999}})
    assert db.get_model_provider(provider, include_token=True)["balance_key"] is None


def test_stale_balance_not_current(authenticated_client, provider):
    configure(authenticated_client, provider)
    row = db.get_model_provider(provider, include_token=True)
    provider_balances.snapshot(row, {"credits": {"balance_remaining": 99}, "fetched_at": 1})
    result = authenticated_client.get(f"{ROOT}/{provider}/balance-access").json()
    assert result["status"] == "stale"
    assert result["credits"] is None

"""Real persistence and fake upstream responses; no provider credentials or charges."""
import asyncio

import httpx
import pytest

from app.core import openrouter_usage as usage
from app.db import database as db


@pytest.fixture
def provider(mock_db):
    row = db.create_model_provider("router", "Router", "openrouter", "https://openrouter.ai/api/v1")
    db.set_system_setting(usage.SETTING_PREFIX + row["id"], "management-test-only")
    return row


def snapshot():
    return next((s for s in db.get_all_provider_usage_snapshots()
                 if s["provider_id"] == usage.SNAPSHOT_PREFIX + "router"), None)


async def test_account_scope_and_fixed_destination(provider, monkeypatch):
    calls = []

    async def fetch(url, *, headers, **_budget):
        calls.append((url, headers))
        return httpx.Response(200, json={"data": {"total_credits": 25, "total_usage": 4.25, "secret": "discard"}})

    monkeypatch.setattr(usage, "usage_get", fetch)
    await usage.poll(provider)
    assert calls == [(usage.CREDITS_URL, {"Authorization": "Bearer management-test-only"})]
    result = snapshot()
    assert result["credits"]["balance_remaining"] == 20.75
    assert result["windows"] == []
    assert "usage" not in result["credits"]
    assert "management-test-only" not in str(result) and "discard" not in str(result)
    provider["base_url"] = "https://proxy.example/api/v1"
    await usage.poll(provider)
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", ["replace", "disconnect", "delete", "disable", "custom"])
@pytest.mark.parametrize("outcome", [200, 403])
async def test_late_observation_cannot_republish_old_credentials(provider, monkeypatch, mutation, outcome):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return httpx.Response(outcome, json={"data": {"total_credits": 100, "total_usage": 1}})

    monkeypatch.setattr(usage, "usage_get", fetch)
    task = asyncio.create_task(usage.poll(provider))
    await asyncio.wait_for(entered.wait(), 2)
    if mutation == "replace":
        db.set_system_setting(usage.SETTING_PREFIX + "router", "other-account")
    elif mutation == "disconnect":
        db.delete_system_setting(usage.SETTING_PREFIX + "router")
    elif mutation == "delete":
        db.delete_model_provider("router")
    elif mutation == "disable":
        db.set_model_provider_enabled("router", False)
    else:
        db.update_model_provider("router", base_url="https://proxy.example")
    release.set()
    await task
    assert snapshot() is None


async def test_failure_retains_same_account_reading_but_replacement_invalidates(provider, monkeypatch):
    response = httpx.Response(200, json={"data": {"total_credits": 10, "total_usage": 3}})

    async def fetch(*args, **kwargs):
        return response

    monkeypatch.setattr(usage, "usage_get", fetch)
    await usage.poll(provider)
    fetched_at = snapshot()["fetched_at"]
    response = httpx.Response(403)
    await usage.poll(provider)
    assert snapshot()["credits"]["balance_remaining"] == 7
    assert snapshot()["fetched_at"] == fetched_at
    assert snapshot()["raw"]["usage_error"] == "billing_access_denied"
    db.set_system_setting(usage.SETTING_PREFIX + "router", "new-account")
    assert snapshot() is None
    await usage.poll(provider)
    assert snapshot()["credits"] is None
    assert snapshot()["raw"]["usage_error"] == "billing_access_denied"


@pytest.mark.parametrize("data", [{}, {"total_credits": True, "total_usage": 0},
                                   {"total_credits": float("inf"), "total_usage": 1},
                                   {"total_credits": 10, "total_usage": -1},
                                   {"total_credits": "10", "total_usage": 1}])
def test_invalid_credit_ledger_is_unavailable(data):
    assert usage.normalize({"data": data}) is None


async def test_usage_response_separates_scopes_and_hides_legacy_combined_balance(provider, monkeypatch):
    from app.api.usage import _load_provider_usage

    async def fetch(*args, **kwargs):
        return httpx.Response(200, json={"data": {"total_credits": 10, "total_usage": 3}})

    monkeypatch.setattr(usage, "usage_get", fetch)
    await usage.poll(provider)
    db.upsert_provider_usage_snapshot("router", "openrouter", None, [],
                                     {"usage": 2, "limit": 5, "balance_remaining": 999, "total_credits": 1000}, {}, 1)
    entries = {s.provider_id: s for s in await _load_provider_usage()}
    assert entries["router"].credits == {"usage": 2, "limit": 5}
    assert entries["router"].scope.startswith("This model key")
    account = entries[usage.SNAPSHOT_PREFIX + "router"]
    assert account.credits["balance_remaining"] == 7
    assert account.provider_name == "Router account"
    assert account.source == "OpenRouter credits API"
    assert account.scope == "Account credit ledger (USD)"


def test_overdrawn_account_balance_is_not_clamped():
    assert usage.normalize({"data": {"total_credits": 1, "total_usage": 2.5}})["balance_remaining"] == -1.5


async def test_existing_model_key_reads_account_credits_without_setup(provider, monkeypatch):
    db.delete_system_setting(usage.SETTING_PREFIX + provider["id"])
    db.update_model_provider("router", auth_token="existing-model-test-key")
    calls = []

    async def fetch(url, *, headers, **_):
        calls.append((url, headers))
        return httpx.Response(200, json={"data": {"total_credits": 50, "total_usage": 30.5}})

    monkeypatch.setattr(usage, "usage_get", fetch)
    await usage.poll(provider)
    assert calls == [(usage.CREDITS_URL, {"Authorization": "Bearer existing-model-test-key"})]
    assert snapshot()["credits"]["balance_remaining"] == 19.5
    assert snapshot()["raw"]["source"] == "openrouter_model_key"
    assert "existing-model-test-key" not in str(snapshot())
    from app.api.usage import _load_provider_usage
    entries = {s.provider_id: s for s in await _load_provider_usage()}
    assert entries[usage.SNAPSHOT_PREFIX + "router"].credits["balance_remaining"] == 19.5
    db.update_model_provider("router", auth_token="replacement-key")
    assert snapshot() is None


@pytest.mark.parametrize("mutation", ["replace", "remove", "disable", "delete", "custom", "management"])
@pytest.mark.parametrize("outcome", [200, 403])
async def test_model_key_late_observation_is_discarded(provider, monkeypatch, mutation, outcome):
    db.delete_system_setting(usage.SETTING_PREFIX + "router")
    db.update_model_provider("router", auth_token="initial-test-key")
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*_, **__):
        entered.set()
        await release.wait()
        return httpx.Response(outcome, json={"data": {"total_credits": 25, "total_usage": 1}})

    monkeypatch.setattr(usage, "usage_get", fetch)
    task = asyncio.create_task(usage.poll(provider))
    await asyncio.wait_for(entered.wait(), 2)
    if mutation == "replace": db.update_model_provider("router", auth_token="new-test-key")
    elif mutation == "remove": db.update_model_provider("router", auth_token=None)
    elif mutation == "disable": db.set_model_provider_enabled("router", False)
    elif mutation == "delete": db.delete_model_provider("router")
    elif mutation == "custom": db.update_model_provider("router", base_url="https://proxy.example")
    else: db.set_system_setting(usage.SETTING_PREFIX + "router", "legacy-key")
    release.set()
    await task
    assert snapshot() is None


async def test_model_key_denial_is_cached_until_credential_changes(provider, monkeypatch):
    db.delete_system_setting(usage.SETTING_PREFIX + "router")
    db.update_model_provider("router", auth_token="denied-test-key")
    calls = []

    async def fetch(*_, **__):
        calls.append(1)
        return httpx.Response(403)

    monkeypatch.setattr(usage, "usage_get", fetch)
    await usage.poll(provider)
    await usage.poll(provider)
    assert len(calls) == 1
    db.update_model_provider("router", auth_token="new-test-key")
    await usage.poll(provider)
    assert len(calls) == 2

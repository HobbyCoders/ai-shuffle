"""Admin API producing path, storage isolation, pagination and durable budgets."""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.core import openai_admin_usage as admin, usage_polling as polling
from app.core.provider_usage import ProviderUsageMonitor
from app.core.config import settings
from app.db import database as db


def response(body, status=200, headers=None):
    return httpx.Response(status, json=body, headers=headers, request=httpx.Request("GET", "https://api.openai.com/v1/organization/costs"))


@pytest.fixture
def upstream(monkeypatch):
    get = AsyncMock()
    client = Mock()
    client.get = get
    context = Mock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(polling.httpx, "AsyncClient", Mock(return_value=context))
    return get


def test_budget_survives_new_monitor_and_credential_changes(test_db):
    headers = {"Authorization": "Bearer secret"}
    key = polling.identity("https://example.test/usage", headers)
    assert "secret" not in key
    assert polling.claim(key, 100)
    assert not polling.claim(key, 100)
    polling.finish(key, 100, 403)
    assert not polling.claim(key, 100000)
    changed = polling.identity("https://example.test/usage", {"Authorization": "Bearer replacement"})
    assert polling.claim(changed, 100)


@pytest.mark.parametrize("status", [405, 410, 501])
def test_unsupported_capability_is_negative_cached_until_resume(test_db, status):
    key = polling.identity("https://example.test/unsupported", {})
    assert polling.claim(key, 0)
    polling.finish(key, 0, status)
    assert not polling.claim(key, 10_000_000)

    polling.resume_identity(polling.identity("https://example.test/unsupported", {}))
    assert polling.claim(key, 10_000_000)


def test_not_found_uses_bounded_backoff_instead_of_permanent_pause(test_db):
    key = polling.identity("https://example.test/maybe-transient", {})
    assert polling.claim(key, 0)
    polling.finish(key, 0, 404)
    assert polling.claim(key, 10_000_000)


def test_retry_after_and_minimum_interval(test_db, monkeypatch):
    monkeypatch.setattr(polling.random, "uniform", lambda *_: 0)
    assert polling.claim("a", 0)
    polling.finish("a", 0, 429, 1200)
    assert not polling.claim("a", 1199)
    assert polling.claim("a", 1200)
    polling.finish("a", 1200, 200)
    assert not polling.claim("a", 1499)
    assert polling.claim("a", 1500)
    assert polling.retry_seconds("120", 0) == 120
    assert polling.retry_seconds("Thu, 01 Jan 1970 00:02:00 GMT", 0) == 120
    assert polling.retry_seconds("nan", 0) == 0
    assert polling.retry_seconds(None, 0) == 0


@pytest.mark.asyncio
async def test_paginated_costs_and_no_legacy_endpoint(test_db, upstream):
    upstream.side_effect = [response({"data": [{"results": [{"amount": {"value": "0.1", "currency": "usd"}}]}], "has_more": True, "next_page": "next"}), response({"data": [{"results": [{"amount": {"value": "0.2", "currency": "usd"}}]}], "has_more": False})]
    got = await admin.fetch("admin-secret", "org-test", costs=True)
    assert got["spend_month"] == 0.3
    assert "balance_remaining" not in got
    assert upstream.await_count == 2
    for call in upstream.call_args_list:
        assert call.args[0] == "https://api.openai.com/v1/organization/costs"
        assert call.kwargs["headers"]["Authorization"] == "Bearer admin-secret"
        assert call.kwargs["headers"]["OpenAI-Organization"] == "org-test"
    assert upstream.call_args_list[1].kwargs["params"]["page"] == "next"
    with pytest.raises(polling.UsageDeferred):
        await admin.fetch("admin-secret", "org-test", costs=True)
    assert upstream.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 403, 429, 500])
async def test_failures_never_fall_back_or_bypass_budget(test_db, upstream, code):
    upstream.return_value = response({}, code, {"Retry-After": "3600"})
    with pytest.raises(httpx.HTTPStatusError):
        await admin.fetch("key", "org-test", costs=True)
    with pytest.raises(polling.UsageDeferred):
        await admin.fetch("key", "org-test", costs=True)
    assert upstream.await_count == 1


@pytest.mark.asyncio
async def test_repeated_pagination_is_rejected(test_db, upstream):
    upstream.return_value = response({"data": [], "has_more": True, "next_page": "same"})
    with pytest.raises(ValueError, match="pagination"):
        await admin.fetch("key", "org-test", costs=True)
    assert upstream.await_count == 2


@pytest.mark.parametrize("amount", [{"value": "nan", "currency": "usd"}, {"value": True, "currency": "usd"}, {"value": 1, "currency": "eur"}])
def test_invalid_costs_are_not_zero_balances(amount):
    with pytest.raises(ValueError):
        admin.summarize([{"results": [{"amount": amount}]}], costs=True)


@pytest.mark.asyncio
async def test_real_producing_path_uses_only_admin_key_and_stores_spend(authenticated_client, upstream):
    from app.core import encryption
    row = db.create_model_provider(provider_id="test-openai", name="OpenAI", provider_type="openai", base_url="https://api.openai.com", auth_token=encryption.encrypt_value("inference-key"))
    pid = row["id"]
    encrypted = encryption.encrypt_value("admin-key")
    db.set_openai_billing_binding(pid, "org-test", settings.effective_instance_id, encrypted)
    async def get(url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer admin-key"
        results = [{"amount": {"value": "12.5", "currency": "usd"}}] if url.endswith("/costs") else [{"input_tokens": 30, "output_tokens": 40, "num_model_requests": 2}]
        return response({"data": [{"results": results}], "has_more": False})
    upstream.side_effect = get
    monitor = ProviderUsageMonitor()
    assert await monitor._poll_openai_provider(row)
    snapshot = next(s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == pid)
    assert snapshot["credits"]["spend_month"] == 12.5
    assert snapshot["credits"]["input_tokens"] == 30
    assert snapshot["raw"]["source"] == "openai_admin"
    assert "key" not in str(snapshot)
    assert not await monitor._poll_openai_provider(row)
    assert upstream.await_count == 2
    db.clear_openai_billing_binding(pid)
    assert not await monitor._poll_openai_provider(row)
    assert upstream.await_count == 2
    assert not any(s["provider_id"] == pid for s in db.get_all_provider_usage_snapshots())
    assert encryption.decrypt_value(db.get_model_provider(pid, include_token=True)["auth_token"]) == "inference-key"


@pytest.mark.asyncio
async def test_legacy_binding_and_plain_model_key_cannot_poll(authenticated_client, upstream):
    row = db.create_model_provider(provider_id="test-openai", name="OpenAI", provider_type="openai", base_url="https://api.openai.com", auth_token="model-only")
    monitor = ProviderUsageMonitor()
    assert not await monitor._poll_openai_provider(row)
    db.set_openai_billing_binding(row["id"], "org-test", settings.effective_instance_id)
    assert not await monitor._poll_openai_provider(row)
    upstream.assert_not_awaited()


def test_late_response_rejected_after_key_rotation(authenticated_client):
    from app.core import encryption
    row = db.create_model_provider(provider_id="test-openai", name="OpenAI", provider_type="openai", base_url="https://api.openai.com")
    pid = row["id"]
    old = encryption.encrypt_value("old")
    db.set_openai_billing_binding(pid, "org-test", settings.effective_instance_id, old)
    db.set_openai_billing_binding(pid, "org-test", settings.effective_instance_id, encryption.encrypt_value("new"))
    assert not db.upsert_provider_usage_snapshot(pid, "openai", None, [], {"spend_month": 9}, {"source": "openai_admin"}, 123, expected_binding=("org-test", settings.effective_instance_id), expected_admin_key=old)
    assert not db.get_all_provider_usage_snapshots()


def test_explicit_resume_only_changes_auth_pauses(test_db):
    headers = {"Authorization": "Bearer secret"}
    url = "https://api.openai.com/v1/organization/costs"
    key = polling.identity(url, headers)
    assert polling.claim(key, 0)
    polling.finish(key, 0, 200)
    polling.resume_identity(polling.identity(url, headers))
    assert not polling.claim(key, 1)
    polling.finish(key, 1, 403)
    assert not polling.claim(key, 10000)
    polling.resume_identity(polling.identity(url, headers))
    assert polling.claim(key, 10000)


@pytest.mark.asyncio
async def test_concurrent_consumers_issue_one_request(test_db, upstream):
    upstream.return_value = response({"data": [], "has_more": False})
    results = await asyncio.gather(*(admin.fetch("shared-key", "org-test", costs=True) for _ in range(8)), return_exceptions=True)
    assert upstream.await_count == 1
    assert sum(isinstance(r, polling.UsageDeferred) for r in results) == 7


@pytest.mark.asyncio
async def test_admin_pair_stores_when_budgets_drift(authenticated_client, upstream, monkeypatch):
    import types

    from app.core import encryption
    from app.network import usage_budget

    clock = [0.0]
    monkeypatch.setattr(usage_budget, "time", types.SimpleNamespace(time=lambda: clock[0]))

    async def get(url, **kwargs):
        results = ([{"amount": {"value": "1.0", "currency": "usd"}}] if url.endswith("/costs")
                   else [{"input_tokens": 1, "output_tokens": 1, "num_model_requests": 1}])
        return response({"data": [{"results": results}], "has_more": False})
    upstream.side_effect = get

    row = db.create_model_provider(provider_id="drift-openai", name="OpenAI", provider_type="openai",
                                   base_url="https://api.openai.com",
                                   auth_token=encryption.encrypt_value("inference-key"))
    db.set_openai_billing_binding(row["id"], "org-drift", settings.effective_instance_id,
                                  encryption.encrypt_value("admin-key"))
    headers = {"Authorization": "Bearer admin-key", "OpenAI-Organization": "org-drift"}
    # The endpoints last finished with different jitter, so their next
    # attempts differ by 20 s.
    for url, jitter in ((admin.BASE + "/costs", 25.0), (admin.BASE + "/usage/completions", 5.0)):
        key = polling.identity(url, headers)
        monkeypatch.setattr(polling.random, "uniform", lambda *_, j=jitter: j)
        assert polling.claim(key, 0, 900)
        polling.finish(key, 0, 200, 0, 900)
    monkeypatch.setattr(polling.random, "uniform", lambda *_: 5.0)

    monitor = ProviderUsageMonitor()
    stored = False
    # Reads every five minutes, starting inside the 20 s gap, for four hours.
    for step in range(48):
        clock[0] = 910.0 + 300.0 * step
        stored = await monitor._poll_openai_provider(row) or stored
    assert stored, "every upstream success was discarded; spend never persisted"
    snapshot = next(s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == row["id"])
    assert snapshot["credits"]["spend_month"] == 1.0
    assert snapshot["credits"]["input_tokens"] == 1

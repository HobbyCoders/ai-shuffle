"""Real sync + poll producers with HTTP replaced at the transport boundary."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

import app.core.provider_observations as provider_observations
from app.core import api_key_balances as balances, encryption, usage_polling
from app.core.config import settings
from app.core import provider_usage
from app.core.provider_usage import ProviderUsageMonitor
from app.core.usage_sources import SOURCES
from app.db import database as db, provider_balances
from app.network import engine, startup, usage_budget
from tests.core.network.harness import exchange, join
from tests.core.network.test_billing_key_sync import PROVIDER, bind, pair, row, scan, separate_installations

pytestmark = pytest.mark.usefixtures("separate_installations")


def fake_http(monkeypatch, bodies):
    calls = []
    real_client = httpx.AsyncClient

    def handle(request):
        calls.append(str(request.url))
        value = bodies[str(request.url)]
        return httpx.Response(200, json=value)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        **{**kw, "transport": httpx.MockTransport(handle)}))
    return calls


def shared_authority(monkeypatch, founder):
    async def remote(ctx, action, key, body=None):
        with founder.active():
            if action == "claim":
                return {"lease": usage_polling.claim(key, usage_polling.time.time(), body["interval"])}
            if action == "finish":
                usage_polling.finish(key, usage_polling.time.time(), body["status"], body["retry_after"],
                                     body["interval"], lease=body["lease"])
                return {}
            raise AssertionError("unexpected budget action")
    monkeypatch.setattr(usage_budget, "_remote", remote)


def poll(worker, source):
    with worker.active():
        monitor = ProviderUsageMonitor()
        asyncio.run(provider_observations.poll_shared(SOURCES[source], row(),
                                                     getattr(monitor, SOURCES[source].poller)))


@pytest.mark.parametrize("kind", ["openai", "xai"])
def test_shared_budget_delivers_one_observation_to_both_workers(workers, monkeypatch, kind):
    a, b = pair(workers, kind)
    shared_authority(monkeypatch, a)
    calls = fake_http(monkeypatch, {
        balances.OPENAI_CREDIT_URL: {"total_available": 42, "total_granted": 50, "total_used": 8},
        balances.XAI_KEY_URL: {"team_id": "team-test"},
        balances.XAI_BILLING_BASE + "team-test/prepaid/balance": {"total": {"val": "-4200"}},
        balances.XAI_BILLING_BASE + "team-test/postpaid/invoice/preview": {},
    })
    source = "openai_platform_balance" if kind == "openai" else "xai_management"
    poll(a, source)
    # The loser is actually denied by the shared coordinator before sync.
    poll(b, source)
    with b.active():
        waiting = balances.status(row())
        assert waiting["status"] == "deferred"
        assert waiting["last_deferred_reason"] == "shared_usage_budget"
        assert waiting["credits"] is None
        assert json.loads(row()["balance_snapshot"])["team_id"] is None
    exchange(a, b)
    poll(b, source)
    with a.active():
        first = balances.status(row())
        source_worker = engine.context().device_id
    with b.active():
        second = balances.status(row())
        assert second["status"] == "fresh"
        assert second["credits"] == first["credits"]
        assert second["observed_at"] == first["observed_at"]
        assert second["source_worker"] == source_worker
        assert second["last_deferred_reason"] is None
    assert len(calls) == (1 if kind == "openai" else 3)


def test_xai_preserves_discovery_progress_when_balance_is_deferred(workers, monkeypatch):
    a, _ = pair(workers)
    with a.active():
        # Spend just the prepaid budget: discovery succeeds, then defers.
        key = usage_polling.identity(balances.XAI_BILLING_BASE + "team-test/prepaid/balance",
                                     {"Authorization": "Bearer billing-test"})
        assert usage_polling.claim(key, usage_polling.time.time())
        calls = fake_http(monkeypatch, {balances.XAI_KEY_URL: {"team_id": "team-test"}})
        asyncio.run(balances.poll(row()))
        snapshot = json.loads(row()["balance_snapshot"])
        assert snapshot["team_id"] == "team-test"
        assert balances.status(row())["status"] == "deferred"
        assert len(calls) == 1


@pytest.mark.parametrize("source", ["openrouter_key", "openrouter_credits", "openai_admin", "fal_billing",
                                     "codex_usage", "grok_subscription"])
def test_other_polled_snapshots_share_without_additional_requests(workers, monkeypatch, source):
    a, b = pair(workers, "openai", configured=False)
    with a.active():
        if source.startswith("openrouter"):
            db.update_model_provider(PROVIDER, provider_type="openrouter", base_url="https://openrouter.ai/api/v1")
        elif source == "openai_admin":
            db.set_openai_billing_binding(PROVIDER, "org-test", settings.effective_instance_id,
                                         encryption.encrypt_value("admin-test"))
        elif source in {"codex_usage", "grok_subscription"}:
            kind = "chatgpt" if source == "codex_usage" else "grok"
            db.update_model_provider(PROVIDER, provider_type=kind, auth_mode=kind)
            db.set_codex_token_bundle(PROVIDER, access_token=encryption.encrypt_value("subscription-test"),
                                      refresh_token=encryption.encrypt_value("refresh-test"),
                                      account_id=None, auth_mode=kind)
        else:
            db.set_system_setting("fal_api_key", encryption.encrypt_value("fal-test"))
        scan()
        engine.scan_all(include_catalog=False, collections=["system_settings"])
    exchange(a, b)
    shared_authority(monkeypatch, a)
    real_client = httpx.AsyncClient
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if "wham/usage" in str(request.url):
            body = {"plan_type": "pro", "credits": {"has_credits": True, "balance": 42, "unlimited": False},
                    "rate_limit": {"primary_window": {"used_percent": 8, "limit_window_seconds": 18000,
                                                       "reset_at": 1900000000}}}
        elif "/v1/billing" in str(request.url):
            body = {"creditUsagePercent": 8, "onDemandUsed": 0, "onDemandCap": 42}
        elif "/v1/settings" in str(request.url):
            body = {"subscription_tier_display": "SuperGrok"}
        elif "/organization/" in str(request.url):
            value = {"amount": {"value": 8, "currency": "usd"}} if "/costs" in str(request.url) else {"input_tokens": 10}
            body = {"data": [{"results": [value]}]}
        elif "/credits" in str(request.url):
            body = {"data": {"total_credits": 50, "total_usage": 8}}
        elif "openrouter" in str(request.url):
            body = {"data": {"usage": 8, "limit": 50, "limit_remaining": 42}}
        else:
            body = {"balance": 42}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        **{**kw, "transport": httpx.MockTransport(handle)}))

    def run(worker):
        with worker.active():
            monitor = ProviderUsageMonitor()
            source_def = SOURCES[source]
            asyncio.run(provider_observations.poll_shared(source_def, None if source == "fal_billing" else row(),
                                                         getattr(monitor, source_def.poller)))
            return db.get_all_provider_usage_snapshots()
    first = run(a)
    count = len(calls)
    assert count > 0 and first
    assert not run(b)  # One shared budget, not a second per-installation allowance.
    exchange(a, b)
    second = run(b)
    assert second and second[0]["credits"] == first[0]["credits"]
    assert second[0]["fetched_at"] == first[0]["fetched_at"]
    assert second[0]["windows"] == first[0]["windows"]
    assert second[0]["plan_type"] == first[0]["plan_type"]
    assert second[0]["raw"]["source_worker"] == a.device_id
    assert len(calls) == count
    if source == "openrouter_key":
        # A local failure must not be hidden by an equal-age peer reading or
        # bypass a deliberate retry. The authority still enforces the cooldown.
        with b.active():
            db.record_provider_usage_failure(PROVIDER, "openrouter", "unavailable")
        acquired = []
        original = usage_budget.acquire

        async def acquire(*args):
            acquired.append(True)
            return await original(*args)

        monkeypatch.setattr(usage_budget, "acquire", acquire)
        run(b)
        assert acquired
        assert len(calls) == count


@pytest.mark.parametrize("secret", ["balance_key", "openai_admin_key"])
@pytest.mark.parametrize("bootstrap", [False, True])
def test_sync_arrival_schedules_refresh_after_commit(workers, monkeypatch, secret, bootstrap):
    from app.core.global_events import global_events

    a, b = pair(workers, "openai" if secret == "openai_admin_key" else "xai", configured=False)
    requests = []

    def refresh(**kwargs):
        assert not b.raw.in_transaction
        binding = "balance_instance_id" if secret == "balance_key" else "openai_billing_instance_id"
        assert row()[binding] == settings.effective_instance_id
        assert row()[secret]
        requests.append(kwargs)

    loop = SimpleNamespace(is_closed=lambda: False, call_soon_threadsafe=lambda fn, *args: fn(*args))
    monkeypatch.setattr(global_events, "_loop", loop)
    monitor = ProviderUsageMonitor()
    monkeypatch.setattr(provider_usage, "provider_usage_monitor", monitor)
    monkeypatch.setattr(monitor, "request_refresh", refresh)
    monkeypatch.setattr(startup, "_emit_resource", lambda resource: None)
    engine.on_applied(lambda touched: startup._on_applied([t for t in touched if t[0] == "provider_billing"]))
    def change(value):
        with a.active():
            if secret == "balance_key":
                bind(value)
            else:
                db.set_openai_billing_binding(PROVIDER, "org-test", settings.effective_instance_id,
                                             encryption.encrypt_value(value))
            scan()

    change("first-test")
    if bootstrap:
        b = workers("new-receiver")
        join(a, b)
    else:
        exchange(a, b)
    assert requests == [{"force": True}]
    change("rotated-test")
    exchange(a, b)
    assert requests == [{"force": True}, {"force": True}]
    with a.active():
        db.update_model_provider(PROVIDER, name="Unrelated edit")
        scan()
    exchange(a, b)
    assert len(requests) == 2


def test_projection_does_not_publish_secret_or_trust_fields(workers, monkeypatch):
    a, _ = pair(workers, "openai")
    calls = fake_http(monkeypatch, {balances.OPENAI_CREDIT_URL: {
        "total_available": 42, "total_granted": 50, "total_used": 8, "token": "do-not-copy"}})
    poll(a, "openai_platform_balance")
    with a.active():
        source = SOURCES["openai_platform_balance"]
        key = provider_observations._binding(source, row())[0]
        value = json.loads(db.get_system_setting(key))
        assert set(value) == {"credits", "windows", "observed_at", "source_worker"}
        assert set(value["credits"]) == {"balance_remaining", "total_credits", "total_usage", "currency"}
        assert "do-not-copy" not in json.dumps(value)
    assert len(calls) == 1


def test_failed_materialization_does_not_schedule_a_refresh(workers, monkeypatch):
    a, b = pair(workers, configured=False)
    requests = []
    engine.on_applied(lambda touched: requests.extend(t for t in touched if t[0] == "provider_billing"))
    with a.active():
        bind()
        scan()
    original = engine._after_mirror
    attempts = []

    def fail(cursor, coll, *args, **kwargs):
        result = original(cursor, coll, *args, **kwargs)
        if coll.name == "model_providers":
            attempts.append(True)
            raise RuntimeError("test transaction rollback")
        return result

    with patch.object(engine, "_after_mirror", fail):
        exchange(a, b)
    assert attempts and not requests
    with b.active():
        assert not row()["balance_key"]


def test_sync_refresh_racing_a_pass_gets_one_follow_up(monkeypatch):
    async def scenario():
        monitor = ProviderUsageMonitor()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def producing_pass():
            calls.append(True)
            entered.set()
            await release.wait()

        monkeypatch.setattr(monitor, "_poll_providers", producing_pass)
        first = monitor.request_refresh(force=True)
        await entered.wait()
        monitor.request_network_refresh()
        monitor.request_network_refresh()
        release.set()
        await first
        await asyncio.sleep(0)  # dispatch the one queued completion callback
        second = monitor._poll_task
        assert second is not first
        await second
        assert len(calls) == 2
        await monitor.stop()
    asyncio.run(scenario())


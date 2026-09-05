"""Organization-bound browser balances remain isolated from API-key polling."""

import asyncio
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import provider_usage as usage
from app.core.config import settings
from app.core import openai_billing_browser as browser_module
from app.db import database as db


@pytest.fixture(autouse=True)
def no_live_browser(monkeypatch):
    monkeypatch.setattr(browser_module.native_chrome, "create_page", AsyncMock(return_value=None))


@pytest.fixture
def provider(mock_db):
    provider_id = str(uuid.uuid4())
    db.create_model_provider(provider_id, "Billing test", "openai", "https://api.openai.com", auth_token="test-model-key")
    return provider_id


def bind(provider, org="org-test", instance=None):
    return db.set_openai_billing_binding(provider, org, instance or settings.effective_instance_id)


def save(provider, amount=21.09, org="org-test", instance=None):
    return db.upsert_provider_usage_snapshot(
        provider, "openai", None, [], {"balance_remaining": amount},
        {"source": "browser_platform", "organization_id": org}, 1000,
        expected_binding=(org, instance or settings.effective_instance_id),
    )


def snapshot(provider):
    return next((s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == provider), None)


def test_binding_is_public_but_does_not_change_api_credentials(provider):
    row = bind(provider)
    assert row["openai_billing_org_id"] == "org-test"
    assert row["openai_billing_instance_id"] == settings.effective_instance_id
    assert "auth_token" not in row
    assert db.get_model_provider(provider, include_token=True)["auth_token"] == "test-model-key"
    db.update_model_provider(provider, openai_billing_org_id="org-untrusted")
    assert db.get_model_provider(provider)["openai_billing_org_id"] == "org-test"


@pytest.mark.parametrize("change", [{"enabled": False}, {"base_url": "https://example.com"}, {"provider_type": "custom"}])
def test_provider_changed_during_login_cannot_bind(provider, change):
    db.update_model_provider(provider, **change)
    assert bind(provider) is None


def test_rebind_and_disconnect_clear_browser_balance_only(provider):
    bind(provider)
    assert save(provider)
    bind(provider, "org-new")
    assert snapshot(provider) is None
    assert save(provider, 0, "org-new")
    db.clear_openai_billing_binding(provider)
    assert snapshot(provider) is None
    assert db.get_model_provider(provider, include_token=True)["auth_token"] == "test-model-key"
    db.upsert_provider_usage_snapshot(provider, "openai", None, [], {"balance_remaining": 5}, {}, 2000)
    db.clear_openai_billing_binding(provider)
    assert snapshot(provider)["credits"]["balance_remaining"] == 5


def test_failure_keeps_balance_source_and_last_success_time(provider):
    bind(provider)
    save(provider, 0)
    db.record_provider_usage_failure(provider, "openai", "billing_sign_in_required", ("org-test", settings.effective_instance_id))
    row = snapshot(provider)
    assert row["credits"]["balance_remaining"] == 0
    assert row["fetched_at"] == 1000
    assert row["raw"]["billing_fetched_at"] == 1000
    assert row["raw"]["source"] == "browser_platform"
    assert row["raw"]["organization_id"] == "org-test"
    assert row["raw"]["usage_error"] == "billing_sign_in_required"


def test_remote_rebind_rejects_both_late_success_and_failure(provider):
    bind(provider, "org-new", "another-installation")
    assert save(provider, 8, "org-new", "another-installation")
    assert save(provider, 99) is False
    db.record_provider_usage_failure(provider, "openai", "billing_timeout", ("org-test", settings.effective_instance_id))
    assert snapshot(provider)["credits"]["balance_remaining"] == 8
    assert "usage_error" not in snapshot(provider)["raw"]


def test_other_installation_cannot_take_over_or_clear_binding(provider):
    bind(provider, instance="remote-installation")
    assert bind(provider) is None
    assert db.clear_openai_billing_binding(provider, settings.effective_instance_id) is None
    assert db.get_model_provider(provider)["openai_billing_instance_id"] == "remote-installation"


def test_api_key_attempts_cannot_replace_bound_browser_balance(provider):
    bind(provider)
    save(provider)
    assert db.upsert_provider_usage_snapshot(provider, "openai", None, [], {"balance_remaining": 999}, {}, 2000) is False
    db.record_provider_usage_failure(provider, "openai", "billing_access_denied")
    row = snapshot(provider)
    assert row["credits"]["balance_remaining"] == 21.09
    assert "usage_error" not in row["raw"]
    db.upsert_provider_usage_snapshot(provider, "openai", None, [{"type": "requests"}], None, None, 3000)
    row = snapshot(provider)
    assert row["credits"]["balance_remaining"] == 21.09
    assert row["raw"]["billing_fetched_at"] == 1000


@pytest.fixture
def service(monkeypatch):
    service = Mock()
    lock = asyncio.Lock()
    service.operation_lock.return_value = lock
    service.status.return_value = {"status": "connected"}
    service.close = AsyncMock()
    service.reconcile = AsyncMock()
    service.shutdown = AsyncMock()
    service.refresh = AsyncMock(return_value={
        "credits": {"balance_remaining": 20.5},
        "raw": {"source": "browser_platform", "organization_id": "org-test"},
        "fetched_at": 2000,
    })
    monkeypatch.setattr(browser_module, "openai_billing_browser", service)
    return service


@pytest.mark.asyncio
async def test_bound_poll_never_decrypts_model_key(provider, service, monkeypatch):
    row = bind(provider)
    decrypt = Mock(side_effect=AssertionError("model key must not be touched"))
    monkeypatch.setattr(usage, "_decrypt_provider_auth_token", decrypt)
    assert await usage.ProviderUsageMonitor()._poll_openai_provider(row)
    assert snapshot(provider)["credits"]["balance_remaining"] == 20.5
    decrypt.assert_not_called()


@pytest.mark.asyncio
async def test_pending_login_and_remote_owner_are_not_polled(provider, service):
    row = bind(provider)
    service.status.return_value = {"status": "ready"}
    assert not await usage.ProviderUsageMonitor()._poll_openai_provider(row)
    service.refresh.assert_not_awaited()
    db.clear_openai_billing_binding(provider)
    row = bind(provider, instance="remote-installation")
    service.status.return_value = {"status": "not_connected"}
    assert not await usage.ProviderUsageMonitor()._poll_openai_provider(row)
    service.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_bound_poll_failure_preserves_balance_without_key_fallback(provider, service, monkeypatch):
    row = bind(provider)
    save(provider)
    service.refresh.side_effect = browser_module.BillingBrowserError("billing_organization_mismatch")
    fallback = AsyncMock()
    monitor = usage.ProviderUsageMonitor()
    monkeypatch.setattr(monitor, "_fetch_openai_balance", fallback)
    assert not await monitor._poll_openai_provider(row)
    assert snapshot(provider)["credits"]["balance_remaining"] == 21.09
    assert snapshot(provider)["raw"]["usage_error"] == "billing_organization_mismatch"
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_periodic_refresh_only_polls_bound_official_providers(provider, service):
    bind(provider)
    monitor = usage.ProviderUsageMonitor()
    await monitor._poll_billing_connections()
    service.refresh.assert_awaited_once_with(provider, "org-test")
    db.set_model_provider_enabled(provider, False)
    await monitor._poll_billing_connections()
    assert provider not in service.reconcile.await_args.args[0]


@pytest.mark.asyncio
async def test_stop_cancels_periodic_work_and_owned_pages(service, monkeypatch):
    monitor = usage.ProviderUsageMonitor()
    entered = asyncio.Event()

    async def pending():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(monitor, "_poll_billing_connections", pending)
    monitor.start()
    await asyncio.wait_for(entered.wait(), 1)
    await monitor.stop()
    assert monitor._billing_task is None
    service.shutdown.assert_awaited_once()

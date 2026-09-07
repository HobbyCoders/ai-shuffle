"""Real SQLite persistence plus fake HTTP; no billing/model service contacted."""
import asyncio

import httpx
import pytest

from app.core import provider_usage as usage
from app.db import database as db


@pytest.fixture(autouse=True)
def fake_credentials(mock_db):
    db.set_system_setting("fal_api_key", "dummy-account-a")


def snapshot():
    return next((row for row in db.get_all_provider_usage_snapshots() if row["provider_id"] == "fal-ai"), None)


async def test_failed_new_credential_never_retains_old_balance(monkeypatch):
    async def success(*args, **kwargs):
        return httpx.Response(200, json={"credits": {"current_balance": 123.45}})

    monkeypatch.setattr(usage, "_pinned_get", success)
    monitor = usage.ProviderUsageMonitor()
    await monitor._poll_fal()
    assert snapshot()["credits"]["balance_remaining"] == 123.45
    db.set_system_setting("fal_api_key", "dummy-account-b")
    assert snapshot() is None

    async def denied(*args, **kwargs):
        return httpx.Response(403)

    monkeypatch.setattr(usage, "_pinned_get", denied)
    await monitor._poll_fal()
    await monitor._poll_fal()
    assert snapshot()["credits"] is None
    assert snapshot()["raw"]["usage_error"] == "billing_access_denied"


@pytest.mark.parametrize("replacement", [None, "dummy-account-b"])
async def test_old_inflight_result_cannot_republish_after_disconnect_or_replacement(monkeypatch, replacement):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"credits": {"current_balance": 123.45}})

    monkeypatch.setattr(usage, "_pinned_get", fetch)
    task = asyncio.create_task(usage.ProviderUsageMonitor()._poll_fal())
    await entered.wait()
    if replacement is None:
        db.delete_system_setting("fal_api_key")
    else:
        db.set_system_setting("fal_api_key", replacement)
    release.set()
    await task
    assert snapshot() is None


async def test_same_binding_failure_keeps_only_its_own_balance(monkeypatch):
    async def success(*args, **kwargs):
        return httpx.Response(200, json={"credits": {"current_balance": 24.5}})

    monkeypatch.setattr(usage, "_pinned_get", success)
    monitor = usage.ProviderUsageMonitor()
    await monitor._poll_fal()

    async def failure(*args, **kwargs):
        raise TimeoutError("offline")

    monkeypatch.setattr(usage, "_pinned_get", failure)
    await monitor._poll_fal()
    assert snapshot()["credits"]["balance_remaining"] == 24.5
    assert snapshot()["raw"]["usage_error"] == "unavailable"
    assert "dummy-account" not in str(snapshot())


def test_stale_failure_cannot_recreate_snapshot_after_credential_change():
    old_generation = db.get_system_setting_revision("fal_api_key")
    db.set_system_setting("fal_api_key", "dummy-account-b")
    db.record_provider_usage_failure("fal-ai", "fal", "unavailable",
                                     expected_credential_generation=old_generation)
    assert snapshot() is None


def test_loader_hides_unbound_legacy_fal_balance():
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""INSERT INTO provider_usage_snapshots
            (provider_id, provider_type, windows, credits, updated_at)
            VALUES (%s, %s, %s, %s, %s)""",
            ("fal-ai", "fal", "[]", '{"balance_remaining": 123.45}', "legacy"))
    assert snapshot() is None

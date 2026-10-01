"""Real SQLite persistence plus fake HTTP; no billing/model service contacted."""
import asyncio
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from app.core import provider_usage as usage
from app.db import database as db
from app.db import settings as settings_db


@pytest.fixture(autouse=True)
def fake_credentials(mock_db, monkeypatch):
    for name in usage.FAL_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    db.set_system_setting("fal_api_key", "dummy-account-a")


def snapshot():
    return next((row for row in db.get_all_provider_usage_snapshots() if row["provider_id"] == "fal-ai"), None)


async def test_failed_new_credential_never_retains_old_balance(monkeypatch):
    async def success(*args, **kwargs):
        return httpx.Response(200, json=123.45)

    monkeypatch.setattr(usage, "usage_get", success)
    monitor = usage.ProviderUsageMonitor()
    await monitor._poll_fal()
    assert snapshot()["credits"]["balance_remaining"] == 123.45
    db.set_system_setting("fal_api_key", "dummy-account-b")
    assert snapshot() is None

    async def denied(*args, **kwargs):
        return httpx.Response(403)

    monkeypatch.setattr(usage, "usage_get", denied)
    await monitor._poll_fal()
    await monitor._poll_fal()
    assert snapshot()["credits"] is None
    assert snapshot()["raw"]["usage_error"] == "billing_access_denied"


@pytest.mark.parametrize("replacement", [None, "dummy-account-b"])
async def test_old_inflight_result_cannot_republish_after_disconnect_or_replacement(monkeypatch, replacement):
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, tzinfo=timezone.utc)

    monkeypatch.setattr(settings_db, "datetime", FrozenClock)
    db.set_system_setting("fal_api_key", "dummy-account-a")
    old_generation = db.get_system_setting_revision("fal_api_key")
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=123.45)

    monkeypatch.setattr(usage, "usage_get", fetch)
    task = asyncio.create_task(usage.ProviderUsageMonitor()._poll_fal())
    await asyncio.wait_for(entered.wait(), timeout=2)
    if replacement is None:
        db.delete_system_setting("fal_api_key")
    else:
        db.set_system_setting("fal_api_key", replacement)
        assert db.get_system_setting_revision("fal_api_key") != old_generation
    release.set()
    await task
    assert snapshot() is None


async def test_same_binding_failure_keeps_only_its_own_balance(monkeypatch):
    async def success(*args, **kwargs):
        return httpx.Response(200, json=24.5)

    monkeypatch.setattr(usage, "usage_get", success)
    monitor = usage.ProviderUsageMonitor()
    await monitor._poll_fal()

    async def failure(*args, **kwargs):
        raise TimeoutError("offline")

    monkeypatch.setattr(usage, "usage_get", failure)
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


def test_binding_revision_migration_preserves_data_and_is_idempotent():
    db.create_model_provider("legacy-provider", "Legacy", "custom", "https://example.invalid")
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE model_providers DROP COLUMN revision")
        cursor.execute("ALTER TABLE system_settings DROP COLUMN revision")
        db._migrate_provider_revisions(cursor)
    first = db.get_model_provider("legacy-provider")
    generation = db.get_system_setting_revision("fal_api_key")
    UUID(first["revision"])
    UUID(generation)
    assert db.get_system_setting("fal_api_key") == "dummy-account-a"
    with db.get_db() as conn:
        db._migrate_provider_revisions(conn.cursor())
    assert db.get_model_provider("legacy-provider")["revision"] == first["revision"]
    assert db.get_system_setting_revision("fal_api_key") == generation


def _recording_success(seen, balance=7.5):
    async def fetch(url, *, headers, **kwargs):
        seen.append(headers.get("Authorization"))
        return httpx.Response(200, json=balance)

    return fetch


@pytest.mark.parametrize("env_name", ["FAL_KEY", "FAL_API_KEY"])
async def test_environment_key_is_used_when_none_is_saved(monkeypatch, env_name):
    import json

    db.delete_system_setting("fal_api_key")
    monkeypatch.setenv(env_name, "dummy-env-key")
    seen = []
    monkeypatch.setattr(usage, "usage_get", _recording_success(seen))
    await usage.ProviderUsageMonitor()._poll_fal()
    assert seen == ["Key dummy-env-key"]
    row = snapshot()
    assert row["credits"]["balance_remaining"] == 7.5
    assert row["raw"]["credential_generation"].startswith("env:")
    assert "dummy-env-key" not in json.dumps(row)


async def test_fal_key_wins_over_fal_api_key(monkeypatch):
    db.delete_system_setting("fal_api_key")
    monkeypatch.setenv("FAL_KEY", "dummy-first")
    monkeypatch.setenv("FAL_API_KEY", "dummy-second")
    seen = []
    monkeypatch.setattr(usage, "usage_get", _recording_success(seen))
    await usage.ProviderUsageMonitor()._poll_fal()
    assert seen == ["Key dummy-first"]


async def test_saved_key_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "dummy-env-key")
    seen = []
    monkeypatch.setattr(usage, "usage_get", _recording_success(seen))
    await usage.ProviderUsageMonitor()._poll_fal()
    assert seen == ["Key dummy-account-a"]
    assert not snapshot()["raw"]["credential_generation"].startswith("env:")


async def test_changed_environment_key_never_shows_the_old_balance(monkeypatch):
    db.delete_system_setting("fal_api_key")
    monkeypatch.setenv("FAL_KEY", "dummy-env-a")
    monkeypatch.setattr(usage, "usage_get", _recording_success([], balance=11.0))
    monitor = usage.ProviderUsageMonitor()
    await monitor._poll_fal()
    assert snapshot()["credits"]["balance_remaining"] == 11.0

    monkeypatch.setenv("FAL_KEY", "dummy-env-b")
    assert snapshot() is None

    async def denied(*args, **kwargs):
        return httpx.Response(403)

    monkeypatch.setattr(usage, "usage_get", denied)
    await monitor._poll_fal()
    assert snapshot()["credits"] is None
    assert snapshot()["raw"]["usage_error"] == "billing_access_denied"

    monkeypatch.delenv("FAL_KEY")
    assert snapshot() is None


async def test_saving_a_key_replaces_the_environment_balance(monkeypatch):
    db.delete_system_setting("fal_api_key")
    monkeypatch.setenv("FAL_KEY", "dummy-env-key")
    monkeypatch.setattr(usage, "usage_get", _recording_success([], balance=11.0))
    await usage.ProviderUsageMonitor()._poll_fal()
    assert snapshot() is not None
    db.set_system_setting("fal_api_key", "dummy-account-b")
    assert snapshot() is None


async def test_inflight_environment_result_is_dropped_once_a_key_is_saved(monkeypatch):
    db.delete_system_setting("fal_api_key")
    monkeypatch.setenv("FAL_KEY", "dummy-env-key")
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=123.45)

    monkeypatch.setattr(usage, "usage_get", fetch)
    task = asyncio.create_task(usage.ProviderUsageMonitor()._poll_fal())
    await asyncio.wait_for(entered.wait(), timeout=2)
    db.set_system_setting("fal_api_key", "dummy-account-b")
    release.set()
    await task
    assert snapshot() is None

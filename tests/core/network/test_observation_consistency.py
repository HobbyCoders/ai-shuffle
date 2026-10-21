"""Follow-up consistency regressions for provider observation sync and writes."""
import asyncio
import hmac
import json
from unittest.mock import Mock

import pytest

import app.core.provider_observations as observations
from app.core import api_key_balances as balances, encryption
from app.core.usage_polling import UsageDeferred
from app.core.usage_sources import SOURCES
from app.db import database as db
from app.db import network as store
from app.network import crypto, engine
from tests.core.network.harness import exchange, join
from tests.core.network.test_billing_key_sync import PROVIDER, pair, separate_installations
from tests.core.network.test_provider_observations import fake_http, poll

pytestmark = pytest.mark.usefixtures("separate_installations")


@pytest.mark.parametrize("delivery", ["incremental", "snapshot"])
def test_rejected_old_value_keeps_sync_origin_and_clock_when_forwarded(workers, delivery):
    a, b = pair(workers, "openai")
    c, d = workers("c"), workers("d")
    join(a, c)
    exchange(a, b)
    key = observations.PREFIX + "openai_platform_balance:" + PROVIDER
    def publish(worker, observed):
        with worker.active():
            db.set_system_setting(key, json.dumps({"observed_at": observed, "credential_revision": "same"}))
            engine.record_after_commit("system_settings", key)
    publish(a, 5000)
    exchange(a, b)
    exchange(a, c)
    with b.active(), db.get_db() as conn:
        before = store.get_record(conn.cursor(), "system_settings", key)
    publish(c, 3000)  # causally later sync write, but an older provider reading
    if delivery == "incremental":
        exchange(c, b)
    else:
        with c.active():
            chunk = engine.snapshot_chunk("system_settings")
        with b.active():
            engine.apply_snapshot_chunk(chunk)
    with b.active(), db.get_db() as conn:
        after = store.get_record(conn.cursor(), "system_settings", key)
        assert json.loads(db.get_system_setting(key))["observed_at"] == 5000
        assert (after["clock"], after["origin_device_id"]) == (before["clock"], before["origin_device_id"])
    join(b, d)
    with d.active():
        assert json.loads(db.get_system_setting(key))["source_worker"] == a.device_id


def test_peer_import_survives_an_already_running_deferred_balance_poll(workers, monkeypatch):
    a, b = pair(workers, "openai")
    fake_http(monkeypatch, {balances.OPENAI_CREDIT_URL:
        {"total_available": 42, "total_granted": 50, "total_used": 8}})
    poll(a, "openai_platform_balance")
    exchange(a, b)
    with b.active():
        from tests.core.network.test_billing_key_sync import row
        key = observations._binding(SOURCES["openai_platform_balance"], row())[0]
        async def import_then_defer(*args):
            observations.import_keys({key})
            assert balances.status(row())["credits"]["balance_remaining"] == 42
            raise UsageDeferred()
        monkeypatch.setattr(balances, "get", import_then_defer)
        asyncio.run(balances.poll(row()))
        status = balances.status(row())
        assert status["status"] == "fresh"
        assert status["credits"]["balance_remaining"] == 42
        assert status["source_worker"] == a.device_id


def test_startup_migration_removes_only_legacy_secret_named_rows(mock_db):
    legacy = observations.PREFIX + "openai_platform_balance:" + "a" * 64
    stable = observations.PREFIX + "openai_platform_balance:provider-id"
    unrelated = "unrelated:" + "a" * 64
    for key in (legacy, stable, unrelated):
        db.set_system_setting(key, "{}")
    db.delete_system_setting(db.MIGRATIONS_FINGERPRINT_KEY)
    db.init_database()
    assert db.get_system_setting(legacy) is None
    assert db.get_system_setting(stable) == "{}"
    assert db.get_system_setting(unrelated) == "{}"
    db.init_database()  # the migration gate and cleanup remain idempotent
    assert db.get_system_setting(stable) == "{}"
    from app.db.database import _retire_secret_named_observations
    with db.get_db() as conn:
        assert _retire_secret_named_observations(conn.cursor()) == []


def test_actual_openrouter_management_save_removes_shared_observation(authenticated_client, monkeypatch):
    from app.core.provider_usage import provider_usage_monitor
    monkeypatch.setattr(provider_usage_monitor, "request_refresh", Mock())
    response = authenticated_client.post("/api/v1/model-providers", json={"name": "Router", "provider_type": "openrouter",
        "base_url": "https://openrouter.ai/api/v1", "auth_token": "inference-test"})
    assert response.status_code == 200
    provider = response.json()["id"]
    path = f"/api/v1/model-providers/{provider}/openrouter-usage"
    assert authenticated_client.put(path, json={"management_key": "first-test"}).status_code == 200
    key = observations.PREFIX + "openrouter_credits:" + provider
    db.set_system_setting(key, json.dumps({"observed_at": 1000}))
    assert authenticated_client.put(path, json={"management_key": "next-test"}).status_code == 200
    assert db.get_system_setting(key) is None
    assert encryption.decrypt_value(db.get_model_provider(provider, include_token=True)["auth_token"]) == "inference-test"


def test_revision_uses_a_domain_separated_hkdf_key(workers, monkeypatch):
    from tests.core.network.test_billing_key_sync import row
    a, _ = pair(workers, "openai")
    with a.active():
        expected = crypto._hkdf(engine.require_context().network_key, b"shuffle-provider-observation-hmac-v1")
        keys = []
        original = hmac.new
        def capture(key, *args, **kwargs):
            keys.append(key)
            return original(key, *args, **kwargs)
        monkeypatch.setattr(hmac, "new", capture)
        observations._binding(SOURCES["openai_platform_balance"], row())
        matches = bool(keys) and hmac.compare_digest(keys[0], expected)
        assert matches


def test_legacy_cleanup_tombstone_reaches_a_peer_already_holding_the_row(workers):
    from app.db.database import _retire_secret_named_observations
    a, b = pair(workers, "openai")
    key = observations.PREFIX + "openai_platform_balance:" + "b" * 64
    with a.active():
        db.set_system_setting(key, json.dumps({"observed_at": 1000}))
        engine.record_after_commit("system_settings", key)
    exchange(a, b)
    with b.active():
        assert db.get_system_setting(key)
    with a.active():
        with db.get_db() as conn:
            assert _retire_secret_named_observations(conn.cursor())
        engine.scan_all(include_catalog=False, collections=["system_settings"])
    exchange(a, b)
    with b.active():
        assert db.get_system_setting(key) is None


def test_stale_provider_snapshot_write_cannot_replace_a_newer_reading(mock_db):
    assert db.upsert_provider_usage_snapshot("test-provider", "openrouter", None, [], {"limit_remaining": 42}, {}, 2000)
    assert not db.upsert_provider_usage_snapshot("test-provider", "openrouter", None, [], {"limit_remaining": 1}, {}, 1000)
    snapshot = next(s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == "test-provider")
    assert snapshot["fetched_at"] == 2000
    assert snapshot["credits"]["limit_remaining"] == 42


def test_newer_local_failure_keeps_imported_reading_and_error_state(workers, monkeypatch):
    import httpx
    from tests.core.network.test_billing_key_sync import row
    a, b = pair(workers, "openai")
    fake_http(monkeypatch, {balances.OPENAI_CREDIT_URL:
        {"total_available": 42, "total_granted": 50, "total_used": 8}})
    poll(a, "openai_platform_balance")
    exchange(a, b)
    with b.active():
        key = observations._binding(SOURCES["openai_platform_balance"], row())[0]
        expected = json.loads(db.get_system_setting(key))["observed_at"]
        async def import_then_fail(*args):
            observations.import_keys({key})
            response = httpx.Response(401, request=httpx.Request("GET", balances.OPENAI_CREDIT_URL))
            raise httpx.HTTPStatusError("denied", request=response.request, response=response)
        monkeypatch.setattr(balances, "get", import_then_fail)
        asyncio.run(balances.poll(row()))
        status = balances.status(row())
        assert status["needs_reauth"]
        assert status["fetched_at"] == expected
        assert json.loads(row()["balance_snapshot"])["credits"]["balance_remaining"] == 42

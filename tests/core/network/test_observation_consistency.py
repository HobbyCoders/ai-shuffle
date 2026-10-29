"""Follow-up consistency regressions for provider observation sync and writes."""
import asyncio
import hmac
import json
import time
from types import SimpleNamespace
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
from tests.core.network.test_provider_observations import fake_http, poll, xai_bodies

pytestmark = pytest.mark.usefixtures("separate_installations")


@pytest.mark.parametrize("delivery", ["incremental", "snapshot"])
def test_rejected_old_value_keeps_sync_origin_and_clock_when_forwarded(workers, delivery):
    a, b = pair(workers)
    c, d = workers("c"), workers("d")
    join(a, c)
    exchange(a, b)
    key = observations.PREFIX + "xai_management:" + PROVIDER
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
            conflicts_before = len(store.list_conflicts())
            engine.apply_snapshot_chunk(chunk)
            engine.apply_snapshot_chunk(chunk)  # a rejected row is retried each round
            assert len(store.list_conflicts()) == conflicts_before
    with b.active(), db.get_db() as conn:
        after = store.get_record(conn.cursor(), "system_settings", key)
        assert json.loads(db.get_system_setting(key))["observed_at"] == 5000
        assert (after["clock"], after["origin_device_id"]) == (before["clock"], before["origin_device_id"])
    join(b, d)
    with d.active():
        assert json.loads(db.get_system_setting(key))["source_worker"] == a.device_id


def test_peer_import_survives_an_already_running_deferred_balance_poll(workers, monkeypatch):
    a, b = pair(workers)
    fake_http(monkeypatch, xai_bodies())
    poll(a, "xai_management")
    exchange(a, b)
    with b.active():
        from tests.core.network.test_billing_key_sync import row
        key = observations._binding(SOURCES["xai_management"], row())[0]
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
    legacy = observations.PREFIX + "xai_management:" + "a" * 64
    stable = observations.PREFIX + "xai_management:provider-id"
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
    a, _ = pair(workers)
    with a.active():
        expected = crypto._hkdf(engine.require_context().network_key, b"shuffle-provider-observation-hmac-v1")
        keys = []
        original = hmac.new
        def capture(key, *args, **kwargs):
            keys.append(key)
            return original(key, *args, **kwargs)
        monkeypatch.setattr(hmac, "new", capture)
        observations._binding(SOURCES["xai_management"], row())
        matches = bool(keys) and hmac.compare_digest(keys[0], expected)
        assert matches


def test_legacy_cleanup_tombstone_reaches_a_peer_already_holding_the_row(workers):
    from app.db.database import _retire_secret_named_observations
    a, b = pair(workers)
    key = observations.PREFIX + "xai_management:" + "b" * 64
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
    a, b = pair(workers)
    fake_http(monkeypatch, xai_bodies())
    poll(a, "xai_management")
    exchange(a, b)
    with b.active():
        key = observations._binding(SOURCES["xai_management"], row())[0]
        expected = json.loads(db.get_system_setting(key))["observed_at"]
        async def import_then_fail(*args):
            observations.import_keys({key})
            response = httpx.Response(401, request=httpx.Request("GET", balances.XAI_KEY_URL))
            raise httpx.HTTPStatusError("denied", request=response.request, response=response)
        monkeypatch.setattr(balances, "get", import_then_fail)
        asyncio.run(balances.poll(row()))
        status = balances.status(row())
        assert status["needs_reauth"]
        assert status["fetched_at"] == expected
        assert json.loads(row()["balance_snapshot"])["credits"]["balance_remaining"] == 42


@pytest.mark.parametrize("failure", [False, True])
def test_future_balance_does_not_block_corrected_clock_write(workers, monkeypatch, failure):
    from app.db import provider_balances
    from tests.core.network.test_billing_key_sync import row
    a, _ = pair(workers)
    now = int(time.time() * 1000)
    with a.active():
        current = row()
        assert provider_balances.snapshot(current, {"fetched_at": now + 2 * 3600 * 1000,
                                                   "credits": {"balance_remaining": 42}})
        monkeypatch.setattr(balances, "time", SimpleNamespace(time=lambda: now / 1000))
        assert balances.status(row())["status"] == "stale"
        assert balances.status(row())["credits"] is None
        corrected = {"fetched_at": now, "credits": {"balance_remaining": 7}}
        if failure:
            corrected = {"fetched_at": now - 1000, "last_attempted_at": now,
                         "last_error_kind": "billing_access_denied", "last_error_scope": "balance"}
        assert provider_balances.snapshot(row(), corrected)
        status = balances.status(row())
        if failure:
            assert status["needs_reauth"]
            assert status["last_error_kind"] == "billing_access_denied"
        else:
            assert status["status"] == "fresh"
            assert status["credits"]["balance_remaining"] == 7


def test_future_provider_snapshot_does_not_block_corrected_clock_write(mock_db):
    now = int(time.time() * 1000)
    assert db.upsert_provider_usage_snapshot("clock-test", "openrouter", None, [],
                                             {"limit_remaining": 42}, {}, now + 2 * 3600 * 1000)
    assert db.upsert_provider_usage_snapshot("clock-test", "openrouter", None, [],
                                             {"limit_remaining": 7}, {}, now)
    snapshot = next(s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == "clock-test")
    assert snapshot["fetched_at"] == now
    assert snapshot["credits"]["limit_remaining"] == 7


def test_future_provider_reading_is_stale_in_usage_api(authenticated_client, monkeypatch):
    from app.api import usage
    now = int(time.time() * 1000)
    provider = db.create_model_provider("future-router", "Router", "openrouter", "https://openrouter.ai/api/v1",
                                        auth_token=encryption.encrypt_value("inference-test"))
    assert db.upsert_provider_usage_snapshot(provider["id"], "openrouter", None, [],
                                             {"limit_remaining": 42}, {}, now + 2 * 3600 * 1000)
    monkeypatch.setattr(usage, "time", SimpleNamespace(time=lambda: now / 1000))
    entry = next(e for e in asyncio.run(usage._load_provider_usage()) if e.provider_id == provider["id"])
    assert entry.status == "stale"


def test_deferred_poll_does_not_republish_an_import_as_local(workers, monkeypatch):
    from tests.core.network.test_billing_key_sync import row
    a, b = pair(workers)
    fake_http(monkeypatch, xai_bodies())
    poll(a, "xai_management")
    source = SOURCES["xai_management"]
    with a.active():
        key = observations._binding(source, row())[0]
        original = db.get_system_setting(key)
    async def arrive_then_defer(*args):
        exchange(a, b)
        with b.active():
            observations.import_keys({key})
        raise UsageDeferred()
    monkeypatch.setattr(balances, "get", arrive_then_defer)
    poll(b, "xai_management")
    with b.active():
        assert balances.status(row())["source_worker"] == a.device_id
        assert db.get_system_setting(key) == original


@pytest.mark.parametrize("source_id", ["xai_management", "openrouter_key"])
def test_publish_rejects_peer_provenance_in_both_local_storage_shapes(workers, monkeypatch, source_id):
    from tests.core.network.test_billing_key_sync import row
    a, b = pair(workers)
    with b.active():
        source = SOURCES[source_id]
        binding = observations._binding(SOURCES["xai_management"], row())
        peer = {"fetched_at": int(time.time() * 1000), "credits": {"limit_remaining": 42}}
        if source_id == "xai_management":
            peer["source_worker"] = a.device_id
        else:
            peer["raw"] = {"source_worker": a.device_id}
        monkeypatch.setattr(observations, "_binding", lambda *args: binding)
        monkeypatch.setattr(observations, "_local", lambda *args: peer)
        publish = Mock()
        monkeypatch.setattr(engine, "record_after_commit", publish)
        observations._publish(source, row(), (binding, {}, False))
        publish.assert_not_called()

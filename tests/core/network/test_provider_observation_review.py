"""Review regressions through peer copy, provider polling, and credential lifecycle."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import app.core.provider_observations as observations
from app.core import api_key_balances as balances, encryption, global_events
from app.core.usage_sources import SOURCES
from app.db import database as db
from app.network import engine, peer_snapshot, policy, startup
from tests.core.network.harness import exchange
from tests.core.network.test_billing_key_sync import PROVIDER, bind, pair, row, scan, separate_installations
from tests.core.network.test_provider_observations import fake_http, poll, xai_bodies, shared_authority

pytestmark = pytest.mark.usefixtures("separate_installations")


def test_real_peer_copy_preserves_postcommit_billing_and_observation_signals(workers, monkeypatch, tmp_path):
    a, b = pair(workers, configured=False)
    with a.active():
        bind("test-management-key")
        key = observations.PREFIX + "copy-test"
        db.set_system_setting(key, json.dumps({"observed_at": 1000}))
        scan()
        manifest = peer_snapshot.capture(b.device_id)
        for index in range(len(manifest["pages"])):
            (tmp_path / f"{index}.json").write_bytes(peer_snapshot.page(b.device_id, manifest["transfer"], index))
    committed = []
    def notified(touched):
        assert not b.raw.in_transaction
        committed.extend(touched)
    engine.on_applied(notified)
    with b.active():
        peer_snapshot.apply(manifest, tmp_path, a.device_id)
    assert ("provider_billing", PROVIDER, "upsert") in committed
    assert ("system_settings", key, "upsert") in committed


async def test_observation_arrival_imports_without_forced_refresh_or_budget_requests(workers, monkeypatch):
    a, b = pair(workers)
    calls = fake_http(monkeypatch, xai_bodies())
    # The synchronous producer helper runs its own loop; execute it outside this one.
    await asyncio.to_thread(poll, a, "xai_management")
    exchange(a, b)
    requests, tasks = [], []
    from app.core import provider_usage
    monkeypatch.setattr(provider_usage, "provider_usage_monitor", SimpleNamespace(
        request_network_refresh=lambda: requests.append(True)))
    monkeypatch.setattr(global_events.global_events, "_loop", asyncio.get_running_loop())
    def schedule(coro, loop):
        task = loop.create_task(coro)
        tasks.append(task)
        return task
    monkeypatch.setattr(startup.asyncio, "run_coroutine_threadsafe", schedule)
    with b.active():
        key = observations._binding(SOURCES["xai_management"], row())[0]
        startup._on_applied([("system_settings", key, "upsert")])
        await asyncio.sleep(0)
        if tasks:
            await asyncio.gather(*tasks)
        assert balances.status(row())["credits"]["balance_remaining"] == 42
    assert requests == []
    assert len(calls) == 3


@pytest.mark.parametrize("source", ["xai_management", "openrouter_key"])
def test_delayed_peer_success_does_not_clear_newer_local_authentication_failure(workers, monkeypatch, source):
    a, b = pair(workers)
    if source == "openrouter_key":
        with a.active():
            db.update_model_provider(PROVIDER, provider_type="openrouter", base_url="https://openrouter.ai/api/v1")
            scan()
        exchange(a, b)
    shared_authority(monkeypatch, a)
    url = (balances.XAI_BILLING_BASE + "team-test/prepaid/balance" if source == "xai_management"
           else "https://openrouter.ai/api/v1/key")
    bodies = xai_bodies() if source == "xai_management" else {
        url: {"data": {"limit": 50, "usage": 8, "limit_remaining": 42}}}
    clock = [time.time()]
    monkeypatch.setattr(observations.time, "time", lambda: clock[0])
    from datetime import datetime
    from app.db import provider_usage_snapshots
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock[0], tz)
    monkeypatch.setattr(provider_usage_snapshots, "datetime", Clock)
    fake_http(monkeypatch, bodies)
    poll(b, source)  # t1
    # Establish causal order before the partition. Wall-clock freshness alone
    # does not order concurrent writes in the sync protocol's Lamport clock.
    exchange(b, a)
    clock[0] += 8000
    poll(a, source)  # t2
    with a.active():
        key = observations._binding(SOURCES[source], row())[0]
        peer_time = json.loads(db.get_system_setting(key))["observed_at"]
        assert peer_time == int(clock[0] * 1000)
    clock[0] += 8000
    bodies[url] = (401, {})
    poll(b, source)  # t3
    exchange(a, b)
    with b.active():
        assert json.loads(db.get_system_setting(key))["observed_at"] == peer_time
        observations._read(SOURCES[source], row())
        if source == "xai_management":
            assert balances.status(row())["needs_reauth"] is True
        else:
            snapshot = next(s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == PROVIDER)
            assert snapshot["raw"]["usage_error"] == "billing_access_denied"


@pytest.mark.parametrize("action", ["rotate", "disconnect", "delete"])
def test_observations_have_bounded_nonsecret_keys_and_are_removed_on_binding_changes(workers, monkeypatch, action):
    a, b = pair(workers)
    fake_http(monkeypatch, xai_bodies())
    poll(a, "xai_management")
    with a.active():
        before = observations._binding(SOURCES["xai_management"], row())
        key = before[0]
        if action == "delete":
            db.delete_model_provider(PROVIDER)
        else:
            from app.db import provider_balances
            provider_balances.bind(row(), "a", encryption.encrypt_value("rotated") if action == "rotate" else None)
        assert db.get_system_setting(key) is None
        if action == "rotate":
            after = observations._binding(SOURCES["xai_management"], row())
            assert after[0] == key  # rotations cannot accumulate unbounded row names
            assert after[4] != before[4]
        scan()
    exchange(a, b)
    with b.active():
        assert db.get_system_setting(key) is None


def test_observation_origin_and_timestamp_are_enforced_during_sync(workers):
    a, b = pair(workers)
    key = observations.PREFIX + "origin-test"
    def publish(at):
        with a.active():
            db.set_system_setting(key, json.dumps({"observed_at": at, "source_worker": "spoofed"}))
            engine.record_after_commit("system_settings", key)
        exchange(a, b)
    publish(2000)
    with b.active():
        assert json.loads(db.get_system_setting(key))["source_worker"] == a.device_id
    publish(1000)
    with b.active():
        assert json.loads(db.get_system_setting(key))["observed_at"] == 2000


def test_unimplemented_deadline_is_not_part_of_public_balance_contract(workers):
    a, b = pair(workers)
    with a.active():
        assert "deferred_until" not in balances.status(row())


def test_credential_revision_uses_network_key_without_changing_row_identity(workers):
    from unittest.mock import patch
    a, _ = pair(workers)
    with a.active():
        source = SOURCES["xai_management"]
        before = observations._binding(source, row())
        ctx = engine.require_context()
        with patch.dict(ctx.keyring, {ctx.key_version: b"different-network-key-for-test!!!"}):
            other = observations._binding(source, row())
        assert other[0] == before[0]
        assert other[4] != before[4]
        # A public non-secret row name, not a hash derived from the secret.
        assert before[0] == observations.PREFIX + source.id + ":" + PROVIDER


@pytest.mark.parametrize("source", ["codex_usage", "grok_subscription", "fal_billing", "openrouter_credits"])
def test_shared_observation_cleanup_covers_each_credential_store(workers, source):
    a, _ = pair(workers, "openai", configured=False)
    with a.active():
        if source in {"codex_usage", "grok_subscription"}:
            kind = "chatgpt" if source == "codex_usage" else "grok"
            db.update_model_provider(PROVIDER, provider_type=kind, auth_mode=kind)
            def save_token(value):
                db.set_codex_token_bundle(PROVIDER, access_token=encryption.encrypt_value(value),
                                         refresh_token=encryption.encrypt_value("refresh-test"),
                                         account_id=None, auth_mode=kind)
            save_token("first-token")
            def disconnect():
                save_token("rotated-token")
        else:
            setting = "fal_api_key" if source == "fal_billing" else "openrouter_billing_key:" + PROVIDER
            if source == "openrouter_credits":
                db.update_model_provider(PROVIDER, provider_type="openrouter", base_url="https://openrouter.ai/api/v1")
            db.set_system_setting(setting, encryption.encrypt_value("account-key"))
            def disconnect():
                db.delete_system_setting(setting)
        binding = observations._binding(SOURCES[source], None if source == "fal_billing" else row())
        assert binding
        key = binding[0]
        db.set_system_setting(key, json.dumps({"observed_at": 1000, "credential_revision": binding[4]}))
        engine.record_after_commit("system_settings", key)
        disconnect()
        assert db.get_system_setting(key) is None


def test_observation_cleanup_rollback_preserves_reading_and_does_not_publish_delete(workers):
    from app.db.base import transaction
    from app.db.provider_usage_snapshots import delete_shared_observations
    a, b = pair(workers)
    key = observations.PREFIX + "xai_management:" + PROVIDER
    with a.active():
        db.set_system_setting(key, json.dumps({"observed_at": 1000}))
        engine.record_after_commit("system_settings", key)
        with pytest.raises(ValueError, match="rollback"):
            with transaction() as conn:
                delete_shared_observations(conn, PROVIDER)
                raise ValueError("rollback")
        assert db.get_system_setting(key)
    exchange(a, b)
    with b.active():
        assert db.get_system_setting(key)

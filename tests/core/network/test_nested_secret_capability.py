"""Rolling upgrades never deliver plaintext nested leaves to old materializers."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from app.api import network as api
from app.core import encryption
from app.db import network as store
from app.network import engine, keyring, nested_secret_sync as compat, peer_snapshot, policy, transport
from app.network.envelope import Envelope
from tests.core.network.harness import exchange, join as bootstrap
from tests.core.network.test_nested_secret_sync import (
    COLLECTIONS, SECRET, check_secrets, contact, data_keys, legacy_collections, produce, scan,
)


@pytest.fixture(autouse=True)
def capture_jobs(monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(peer_snapshot, "_executor", executor)
        monkeypatch.setattr(peer_snapshot, "_jobs", {})
        yield


def advertise(worker, supported):
    with worker.active():
        engine.publish_presence(capabilities={compat.CAPABILITY: int(supported)})


def leaves(collection, record):
    values = []
    policy.collection(collection).map_secrets(dict(record), lambda value: values.append(value) or value)
    return values


def records(worker):
    with worker.active():
        return [(op["collection"], keyring.open_envelope(engine.context(), Envelope.from_dict(op))[0])
                for op in store.ops_since({}) if op["collection"] in COLLECTIONS and op["op"] == "upsert"]


def pair(workers, supported):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Rolling upgrade")
        engine.publish_presence()
    bootstrap(a, b)
    advertise(b, supported)
    contact(b, a)
    exchange(b, a)
    return a, b


def test_presence_and_contact_advertise_even_if_optional_probes_fail(workers, monkeypatch):
    from app.core import runner_capabilities
    a = workers("a")
    with a.active():
        engine.create_network("Capability")
        engine.publish_presence()
        assert engine.presence_of(a.device_id)["capabilities"][compat.CAPABILITY] == 1
        monkeypatch.setattr(runner_capabilities, "detect_capabilities", lambda: (_ for _ in ()).throw(RuntimeError()))
        assert transport._capabilities()[compat.CAPABILITY] == 1
        assert compat.enabled()


def test_mixed_network_stays_ciphertext_then_last_upgrade_republishes(workers, monkeypatch):
    a, b = pair(workers, False)
    with a.active():
        aid = produce()
        assert not compat.enabled()
        scan()
    for collection, record in records(a):
        assert all(encryption.is_encrypted(value) for value in leaves(collection, record))
        if collection == "automations":
            assert isinstance(record["triggers"], str)  # legacy wire shape too
    # Drive the actual pre-upgrade materializer, not just a seeded receiver field.
    with monkeypatch.context() as old:
        legacy_collections(old)
        exchange(a, b)
    with b.active():
        assert SECRET not in json.dumps(b.sql("SELECT * FROM connectors"))
        assert SECRET not in json.dumps(b.sql("SELECT * FROM automations"))
        assert SECRET not in json.dumps(b.sql("SELECT * FROM system_settings"))
    advertise(b, True)
    contact(b, a)
    exchange(b, a)
    with a.active():
        assert compat.enabled()
        assert scan() == 4  # no user edit or manual force-publish
    exchange(a, b)
    with b.active():
        check_secrets(aid)
        assert scan() == 0
    with a.active():
        assert scan() == 0


def test_missing_and_offline_active_members_block_but_revoked_do_not(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Unknown capability")
        engine.publish_presence()
    bootstrap(a, b)  # B has not advertised yet
    with a.active():
        assert not compat.enabled()
        a.sql("UPDATE network_members SET last_seen=NULL WHERE device_id=?", (b.device_id,))
        assert not compat.enabled()
        a.sql("UPDATE network_members SET status='revoked' WHERE device_id=?", (b.device_id,))
        assert compat.enabled()


def test_third_device_cannot_relay_historical_plaintext_to_old_member(workers):
    a, b = pair(workers, True)
    with a.active():
        aid = produce()
        scan()
    exchange(a, b)
    with b.active():
        check_secrets(aid)
    c = workers("c")
    bootstrap(b, c)
    advertise(c, False)
    exchange(c, b)
    with b.active():
        # B retains A's original signed plaintext ops. Filtering just B's
        # own publications would leak them through this forwarding path.
        protected = [(collection, record) for collection, record in records(b) if leaves(collection, record)]
        assert any(SECRET in leaves(collection, record) for collection, record in protected)
        ops, _, snapshot = store.exchange_batch({}, exclude_origin=c.device_id)
        assert ops == [] and snapshot
        op = next(op for op in store.ops_since({}) if op["collection"] == "connectors"
                  and op["origin_device_id"] == a.device_id)
        record, _ = keyring.open_envelope(engine.context(), Envelope.from_dict(op))
        assert SECRET in leaves("connectors", record)
        with pytest.raises(HTTPException) as error:
            asyncio.run(api.peer_op(op["origin_device_id"], op["origin_incarnation"], op["origin_seq"],
                                     device_id=c.device_id))
        assert error.value.status_code == 404
        for collection in COLLECTIONS:
            chunk = asyncio.run(api.peer_snapshot(collection=collection, device_id=c.device_id))
            body = engine.open_snapshot_chunk(chunk, ctx=engine.context())
            for item in body["items"]:
                if item["record"]:
                    assert all(encryption.is_encrypted(value) for value in leaves(collection, item["record"]))


@pytest.mark.parametrize("cache", ["disk", "job"])
def test_cached_state_pages_are_rejected_and_recaptured_after_downgrade(workers, cache):
    a, b = pair(workers, True)
    with a.active():
        produce()
        scan()
        old = peer_snapshot.state(b.device_id) if cache == "job" else peer_snapshot.capture(b.device_id)
        assert old["nested_secret_rekey"]
        assert peer_snapshot.page(b.device_id, old["transfer"], 0)
    advertise(b, False)
    contact(b, a)
    exchange(b, a)
    with a.active():
        with pytest.raises(ValueError, match="expired"):
            peer_snapshot.page(b.device_id, old["transfer"], 0)
        fresh = peer_snapshot.state(b.device_id) if cache == "job" else peer_snapshot.capture(b.device_id)
        assert fresh["transfer"] != old["transfer"]
        assert not fresh["nested_secret_rekey"]
        for index, page in enumerate(fresh["pages"]):
            if page["collection"] not in COLLECTIONS:
                continue
            chunk = json.loads(peer_snapshot.page(b.device_id, fresh["transfer"], index))
            body = engine.open_snapshot_chunk(chunk, ctx=engine.context())
            for item in body["items"]:
                if item["record"]:
                    assert all(encryption.is_encrypted(value) for value in leaves(page["collection"], item["record"]))


def test_direct_contact_can_withdraw_stale_presence_before_flush(workers):
    a, b = pair(workers, True)
    with a.active():
        produce()
        scan()
        assert compat.enabled()
        response = asyncio.run(api.peer_exchange(
            api.ExchangeRequest(watermarks={}, capabilities={}, flush=True), device_id=b.device_id))
        assert not compat.enabled()  # B's old signed presence still claims support
        assert response["ops"] == [] and response["snapshot_required"]
        latest = [op for op in store.ops_since({}) if op["collection"] == "connectors"][-1]
        record, _ = keyring.open_envelope(engine.context(), Envelope.from_dict(latest))
        assert all(encryption.is_encrypted(value) for value in leaves("connectors", record))


@pytest.mark.parametrize("delivery", ["ops", "snapshot"])
def test_delayed_relayed_presence_cannot_undo_direct_withdrawal(workers, delivery):
    a, b = pair(workers, True)
    with a.active():
        produce()
        scan()
    c = workers("relay")
    bootstrap(a, c)
    advertise(c, True)
    contact(c, a)
    with b.active():
        engine.publish_presence(version="unseen-before-downgrade")
    exchange(b, c)
    with a.active():
        asyncio.run(api.peer_exchange(api.ExchangeRequest(watermarks={}, capabilities={}), device_id=b.device_id))
        assert not compat.enabled(recipient=b.device_id)
    if delivery == "ops":
        exchange(c, a)
    else:
        with c.active():
            chunk = engine.snapshot_chunk("presence")
        with a.active():
            engine.apply_snapshot_chunk(chunk)
    with a.active():
        assert engine.presence_of(b.device_id)["version"] == "unseen-before-downgrade"
        assert not compat.enabled(recipient=b.device_id)
        assert not compat.enabled()
        ops, _, snapshot = store.exchange_batch({}, exclude_origin=b.device_id)
        assert not ops and snapshot
        original = next(op for op in store.ops_since({}) if op["collection"] == "connectors")
        with pytest.raises(HTTPException):
            asyncio.run(api.peer_op(original["origin_device_id"], original["origin_incarnation"],
                                     original["origin_seq"], device_id=b.device_id))
        chunk = asyncio.run(api.peer_snapshot(collection="connectors", device_id=b.device_id))
        record = engine.open_snapshot_chunk(chunk, ctx=engine.context())["items"][0]["record"]
        assert all(encryption.is_encrypted(value) for value in leaves("connectors", record))
        assert not peer_snapshot.capture(b.device_id)["nested_secret_rekey"]


@pytest.mark.parametrize("cache", ["disk", "job"])
def test_cached_frontier_refreshes_for_unservable_nested_secret_tail(workers, cache):
    a, source = pair(workers, True)
    old_peer = workers("legacy")
    bootstrap(a, old_peer)
    advertise(old_peer, False)
    contact(old_peer, a)
    exchange(old_peer, a)
    with a.active():
        capture = peer_snapshot.state if cache == "job" else peer_snapshot.capture
        old = capture(old_peer.device_id)
        assert old["nested_secret_rekey"] is False
    # This sender has not learned about the legacy member. The signed plaintext
    # op arrives late, behind more than one normal batch of harmless presence.
    with source.active():
        for index in range(501):
            engine.publish_presence(version=f"delayed-{index}")
        produce()
        scan()
    exchange(source, a, limit=10000)
    with a.active():
        assert store.requires_snapshot(old["watermarks"], exclude_origin=old_peer.device_id,
                                       check_tail_compatibility=True)
        batch, _, snapshot = store.exchange_batch(old["watermarks"], limit=1, exclude_origin=old_peer.device_id)
        assert len(batch) == 1 and not snapshot  # the incompatible op is beyond this batch
        fresh = capture(old_peer.device_id)
        assert fresh["transfer"] != old["transfer"]
        assert fresh["watermarks"] != old["watermarks"]
        assert not store.exchange_batch(fresh["watermarks"], exclude_origin=old_peer.device_id)[2]
        assert capture(old_peer.device_id)["transfer"] == fresh["transfer"]


def test_snapshot_descriptors_and_presence_are_not_direct_contact_evidence(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Independent evidence")
        engine.publish_presence()
    bootstrap(a, b)
    with b.active():
        assert engine.presence_of(a.device_id)["capabilities"][compat.CAPABILITY] == 1
        assert store.get_member(a.device_id)["capabilities"] == {}
        assert not compat.enabled(recipient=a.device_id)
    advertise(b, True)
    contact(b, a)
    exchange(b, a)
    with b.active():
        assert compat.enabled()
    with a.active():
        assert compat.enabled()


@pytest.mark.parametrize("supported", [False, True])
def test_exchange_decrypts_only_the_protected_ops_in_its_batch(workers, monkeypatch, supported):
    from app.db import settings as db_settings
    a, b = pair(workers, supported)
    with a.active():
        engine.scan_all(collections=["system_settings"])
        start = store.local_watermarks()
        for index in range(12):
            db_settings.set_system_setting(f"provider_observation:batch-{index}",
                                           json.dumps({"status": "known", "sample": index}))
        assert engine.scan_all(collections=["system_settings"]) == 12
        pending = store.ops_since(start, exclude_origin=b.device_id)
        assert len(pending) == 12
        opened = []
        original = keyring.open_envelope

        def counted(ctx, op, *args, **kwargs):
            opened.append(op.origin_seq)
            return original(ctx, op, *args, **kwargs)

        monkeypatch.setattr(keyring, "open_envelope", counted)
        batch, _, snapshot = store.exchange_batch(start, limit=3, exclude_origin=b.device_id)
        assert len(batch) == 3 and not snapshot
        assert opened == ([] if supported else [op["origin_seq"] for op in batch])

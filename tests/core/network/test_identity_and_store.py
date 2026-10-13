"""Device identity custody and the local network tables."""
import json
import os
import stat

import pytest

from app.core.config import settings
from app.db import network as store
from app.network import crypto, identity


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    identity.reset_cache_for_tests()
    yield tmp_path
    identity.reset_cache_for_tests()


def test_identity_is_created_once_and_reloaded(data_dir):
    first = identity.load()
    assert first.device_id.startswith("dev_") and first.incarnation == 1
    path = data_dir / ".credential-keys" / identity.IDENTITY_FILE
    assert path.exists()
    if os.name != "nt":
        assert not (path.stat().st_mode & 0o077)
        assert stat.S_IMODE((data_dir / ".credential-keys").stat().st_mode) == 0o700
    identity.reset_cache_for_tests()
    again = identity.load(create=False)
    assert again.device_id == first.device_id
    assert again.signing_private == first.signing_private
    assert crypto.fingerprint_matches(again.signing_public, first.fingerprint)
    summary = again.public_summary()
    assert set(summary) == {"device_id", "fingerprint", "signing_public", "encryption_public", "incarnation"}


def test_identity_missing_without_create(data_dir):
    with pytest.raises(identity.IdentityError):
        identity.load(create=False)


def test_incarnation_bump_persists(data_dir):
    first = identity.load()
    bumped = identity.bump_incarnation()
    assert bumped.incarnation == 2 and bumped.device_id == first.device_id
    identity.reset_cache_for_tests()
    assert identity.load(create=False).incarnation == 2


def test_identity_derives_its_public_key_once(data_dir, monkeypatch):
    """#1978: every published op reads the device id; deriving the Ed25519
    public key on each read was a measurable share of publish time."""
    first = identity.load()
    calls = []
    real = crypto.signing_public_key
    monkeypatch.setattr(crypto, "signing_public_key", lambda private: calls.append(1) or real(private))
    fresh = identity.DeviceIdentity(signing_private=first.signing_private,
                                    encryption_private=first.encryption_private,
                                    incarnation=first.incarnation, created_at=first.created_at)
    for _ in range(5):
        assert fresh.device_id == first.device_id
        assert fresh.signing_public == first.signing_public
    assert len(calls) == 1
    # Changed fields mean a new object, never a stale cached key.
    other = identity.DeviceIdentity(signing_private=crypto.generate_signing_key(),
                                    encryption_private=first.encryption_private,
                                    incarnation=first.incarnation, created_at=first.created_at)
    assert other.device_id != first.device_id


def test_corrupt_identity_file_is_rejected(data_dir):
    identity.load()
    path = data_dir / ".credential-keys" / identity.IDENTITY_FILE
    path.write_text(json.dumps({"v": 1, "signing": "short", "encryption": "short"}))
    identity.reset_cache_for_tests()
    with pytest.raises(identity.IdentityError):
        identity.load()


def test_network_state_members_and_ops(mock_db):
    assert store.get_state() is None
    state = store.create_state(network_id="net_1", name="Home", founder_device_id="dev_a",
                               keyring={"1": {"v": 1}})
    assert state["network_id"] == "net_1" and state["keyring"] == {"1": {"v": 1}}
    assert state["clock"] == 0
    # Creating again is a no-op (single row).
    assert store.create_state(network_id="net_2", name="X", founder_device_id="dev_b", keyring={})["network_id"] == "net_1"
    state = store.update_state(generation=3, hub={"cursor": 5})
    assert state["generation"] == 3 and state["hub"] == {"cursor": 5}

    with store.get_db() as conn:
        cursor = conn.cursor()
        assert store.bump_clock(cursor, 0) == 1
        assert store.bump_clock(cursor, 10) == 11
        store.observe_clock(cursor, 5)
        assert store.bump_clock(cursor, 0) == 12

    member = store.upsert_member(device_id="dev_a", name="Desk", kind="worker", signing_public="s", encryption_public="e",
                                 generation=0, platform="linux")
    assert member["status"] == "active" and member["capabilities"] == {}
    store.touch_member("dev_a", capabilities={"has_chrome": True}, endpoints=["http://10.0.0.5:8000"], version="1.0")
    member = store.get_member("dev_a")
    assert member["capabilities"] == {"has_chrome": True} and member["endpoints"] == ["http://10.0.0.5:8000"]
    assert member["last_seen"] is not None
    store.upsert_member(device_id="dev_a", name="Desk", kind="worker", signing_public="s", encryption_public="e",
                        generation=4, status="revoked", last_seq_accepted=9)
    assert store.get_member("dev_a")["status"] == "revoked"
    assert [m["device_id"] for m in store.list_members()] == ["dev_a"]
    assert store.list_members(include_revoked=False) == []

    env = {"origin_device_id": "dev_a", "origin_incarnation": 1, "origin_seq": 1, "collection": "tags",
           "record_id": "t1", "op": "upsert", "clock": 4}
    with store.get_db() as conn:
        cursor = conn.cursor()
        assert store.next_origin_seq(cursor, "dev_a", 1) == 1
        assert store.insert_op(cursor, env, "settings")
        assert not store.insert_op(cursor, env, "settings")
        assert store.insert_op(cursor, dict(env, origin_seq=2, record_id="t2"), "settings")
        assert store.next_origin_seq(cursor, "dev_a", 1) == 3
        store.put_record(cursor, "tags", "t1", clock=4, origin_device_id="dev_a", content_hash="h", deleted=False)
        assert store.get_record(cursor, "tags", "t1")["clock"] == 4
        assert list(store.records_for_collection(cursor, "tags")) == ["t1"]
        store.set_watermark(cursor, "dev_b", 1, 7)
        assert store.get_watermark(cursor, "dev_b", 1) == 7
        assert store.highest_incarnation(cursor, "dev_b") == 1
        store.stash_pending(cursor, dict(env, origin_device_id="dev_b", origin_seq=9))
        assert store.pop_pending(cursor, "dev_b", 1, 8) is None
        assert store.pop_pending(cursor, "dev_b", 1, 9)["origin_seq"] == 9

    assert store.ops_since({}) and len(store.ops_since({})) == 2
    assert [e["origin_seq"] for e in store.ops_since({"dev_a": {"1": 1}})] == [2]
    assert store.ops_since({"dev_a": {"1": 2}}) == []
    assert store.ops_since({}, exclude_origin="dev_a") == []
    marks = store.local_watermarks()
    assert marks["dev_a"] == {"1": 2} and marks["dev_b"] == {"1": 7}
    after = store.ops_after_ingest(0, limit=1)
    assert len(after) == 1 and after[0][1]["record_id"] == "t1"
    assert store.count_ops() == 2 and store.count_records() == {"tags": 1}

    with store.get_db() as conn:
        store.add_conflict(conn.cursor(), collection="tags", record_id="t1", losing_clock=3, losing_device_id="dev_b",
                           losing_record={"name": "old"}, winning_clock=4, winning_device_id="dev_a")
    conflicts = store.list_conflicts()
    assert len(conflicts) == 1 and conflicts[0]["losing_record"] == {"name": "old"}
    assert store.delete_conflict(conflicts[0]["id"]) and store.list_conflicts() == []

    store.upsert_peer("dev_b", base_url="http://b", delivered=(1, 5), contacted=True)
    store.upsert_peer("dev_b", error="timeout")
    peer = store.list_peers()[0]
    assert peer["base_url"] == "http://b" and peer["delivered_seq"] == 5 and peer["last_error"] == "timeout"

    join = store.create_join({"id": "join_1", "role": "inviter", "status": "pending", "name": "Laptop",
                              "fingerprint": "abcd", "response": {"x": 1}})
    assert join["response"] == {"x": 1}
    assert store.update_join("join_1", status="approved", response=None)["status"] == "approved"
    assert [j["id"] for j in store.list_joins(role="inviter")] == ["join_1"]
    assert store.delete_join("join_1") and store.get_join("join_1") is None

    store.upsert_blob("abc", size=10, present=False, holder="dev_b")
    store.upsert_blob("abc", size=10, holder="dev_c")
    blob = store.get_blob("abc")
    assert blob["holders"] == ["dev_b", "dev_c"] and blob["present"] is False
    assert [b["hash"] for b in store.missing_blobs()] == ["abc"]
    store.upsert_blob("abc", size=10, present=True)
    assert store.missing_blobs() == []

    store.delete_state()
    assert store.get_state() is None

"""The automatic review ledger is installation-local even across a peer upgrade."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import network as network_api
from app.core import behavioral_learning as learning
from app.db import database
from app.db import network as store
from app.network import engine, peer_snapshot, policy, transcripts
from tests.core.network.harness import exchange, join

SETTINGS = "system_settings"
BUDGET = learning._STATE_KEY
SHARED = "display_name_for_test"
HOUR = 3600
START = 1_800_000_000


@pytest.mark.parametrize("delivery", ["ops", "snapshot"])
@pytest.mark.parametrize("deleted", [False, True], ids=["replacement", "tombstone"])
def test_legacy_peer_cannot_reset_local_call_receipts(workers, monkeypatch, delivery, deleted):
    local, peer = workers("local"), workers("peer")
    with local.active():
        engine.create_network("Test network", device_name="Local")
    join(local, peer)
    assert local.device_id != peer.device_id

    monkeypatch.setattr(learning, "time", SimpleNamespace(time=lambda: now))
    now = START
    admitted = []

    def review_pair(hour, label):
        nonlocal now
        now = START + hour * HOUR
        for verify in (False, True):
            if learning._claim("session", label, f"prompt {label}", verify=verify):
                admitted.append((now, verify))
            now += 1

    with local.active():
        review_pair(0, "first")
        review_pair(0.5, "second")
        assert len(admitted) == 4
        original = database.get_system_setting(BUDGET)
        assert [(row[0], row[3]) for row in json.loads(original)["claims"]] == [
            (START, START + 1), (START + HOUR / 2, START + HOUR / 2 + 1),
        ]

    # A pre-upgrade peer published these in the normal settings stream. Only
    # its key policy is changed; the scanner, signing, and DB outbox are real.
    with peer.active(), monkeypatch.context() as legacy:
        legacy.setattr(policy, "RETIRED_SHARED_SETTING_KEYS", ())
        database.set_system_setting(BUDGET, "{}")
        assert engine.scan_collection(SETTINGS, record_ids=[BUDGET]) == 1
        if deleted:
            assert database.delete_system_setting(BUDGET)
            assert engine.scan_collection(SETTINGS, record_ids=[BUDGET]) == 1
        database.set_system_setting(SHARED, "from peer")
        engine.scan_collection(SETTINGS, record_ids=[SHARED])
        if delivery == "snapshot":
            chunk = engine.snapshot_chunk(SETTINGS)
            items = {item["record_id"]: item for item in engine.open_snapshot_chunk(
                chunk, ctx=engine.require_context())["items"]}
            assert items[BUDGET]["deleted"] is deleted

    if delivery == "snapshot":
        with local.active():
            engine.apply_snapshot_chunk(chunk)
    else:
        with local.active():
            marks = store.local_watermarks()
        with peer.active(), monkeypatch.context() as legacy:
            legacy.setattr(policy, "RETIRED_SHARED_SETTING_KEYS", ())
            ops, _, needs_snapshot = store.exchange_batch(marks, exclude_origin=local.device_id)
        assert not needs_snapshot
        assert [op["op"] for op in ops if op["record_id"] == BUDGET] == (
            ["upsert", "delete"] if deleted else ["upsert"])
        with local.active():
            # Allow the old delete and the following ordinary setting to queue
            # before their predecessor, then exercise the real pending drain.
            queued = engine.apply_batch(ops[1:])
            assert queued.pending == len(ops) - 1
            result = engine.apply_batch(ops[:1])
            assert result.pending == result.retry == 0
            assert result.rejected == (2 if deleted else 1)
            with store.get_db() as conn:
                cursor = conn.cursor()
                assert store.get_record(cursor, SETTINGS, BUDGET) is None
                assert store.count_pending(cursor, peer.device_id, peer.identity.incarnation) == 0

    with local.active():
        assert database.get_system_setting(SHARED) == "from peer"
        retained = database.get_system_setting(BUDGET)
        review_pair(1, "inside-burst")
        within_burst = len(admitted)
        review_pair(9, "after-burst")
        after_burst = len(admitted)
        # By hour 18 the burst window is clear, but the daily window isn't.
        review_pair(18, "inside-daily")
        assert (within_burst, after_burst, len(admitted)) == (4, 6, 6)
        assert retained == original
        review_pair(25, "after-daily")
        assert len(admitted) == 8


def test_receipts_are_not_published_but_an_ordinary_setting_is(workers):
    local, peer = workers("local"), workers("peer")
    with local.active():
        engine.create_network("Test network", device_name="Local")
    join(local, peer)
    with local.active():
        assert learning._claim("session", "turn", "proposal")
        assert learning._claim("session", "turn", "proposal", verify=True)
        database.set_system_setting(SHARED, "from local")
        engine.scan_collection(SETTINGS)

    assert exchange(local, peer).rejected == 0
    with peer.active():
        assert database.get_system_setting(SHARED) == "from local"
        assert database.get_system_setting(BUDGET) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["replacement", "tombstone"])
async def test_upgraded_sender_omits_queued_receipt_ops_and_snapshot_versions(workers, monkeypatch, deleted):
    sender, receiver = workers("sender"), workers("receiver")
    with sender.active():
        engine.create_network("Test network", device_name="Sender")
    join(sender, receiver)
    with receiver.active():
        marks = store.local_watermarks()
        assert learning._claim("session", "receiver-turn", "receiver prompt")
        retained = database.get_system_setting(BUDGET)

    with sender.active(), monkeypatch.context() as legacy:
        legacy.setattr(policy, "RETIRED_SHARED_SETTING_KEYS", ())
        assert learning._claim("session", "sender-turn", "sender prompt")
        assert engine.scan_collection(SETTINGS, record_ids=[BUDGET]) == 1
        if deleted:
            assert database.delete_system_setting(BUDGET)
            assert engine.scan_collection(SETTINGS, record_ids=[BUDGET]) == 1
        stale, _, needs_snapshot = store.exchange_batch(marks, exclude_origin=receiver.device_id)
        assert not needs_snapshot
        old_op = next(op for op in reversed(stale) if op["record_id"] == BUDGET)

    with sender.active():
        assert engine.scan_collection(SETTINGS, record_ids=[BUDGET]) == 0
        database.set_system_setting("app_label_for_test", "before ledger in sort order")
        database.set_system_setting(SHARED, "after ledger in sort order")
        engine.scan_collection(SETTINGS)
        ops, _, needs_snapshot = store.exchange_batch(marks, exclude_origin=receiver.device_id)
        assert needs_snapshot and not ops
        for keyset in (False, True):
            offset, after, ids = 0, None, []
            while True:
                if keyset:
                    page, after = engine.snapshot_page(SETTINGS, offset, after_record_id=after, limit=1)
                else:
                    page = engine.snapshot_chunk(SETTINGS, offset, limit=1)
                ids.extend(item["record_id"] for item in engine.open_snapshot_chunk(
                    page, ctx=engine.require_context())["items"])
                if not page["more"]:
                    break
                assert page["count"] == 1
                offset += page["count"]
            assert "app_label_for_test" in ids and SHARED in ids and BUDGET not in ids

        # The direct lookup is also an egress path, but a rollback must still
        # be able to recover signed proof of its *own* prior sequence.
        args = sender.device_id, old_op["origin_incarnation"], old_op["origin_seq"]
        with pytest.raises(HTTPException) as denied:
            await network_api.peer_op(*args, device_id=receiver.device_id)
        assert denied.value.status_code == 404
        assert (await network_api.peer_op(*args, device_id=sender.device_id))["op"] == old_op

    # Existing signed history was bypassed, not removed. The real fallback
    # advances the frontier and later ordinary writes travel as deltas again.
    exchange(sender, receiver)
    with receiver.active():
        assert database.get_system_setting(BUDGET) == retained
        assert database.get_system_setting(SHARED) == "after ledger in sort order"
        marks = store.local_watermarks()
    with sender.active():
        database.set_system_setting(SHARED, "next delta")
        engine.scan_collection(SETTINGS, record_ids=[SHARED])
        ops, _, needs_snapshot = store.exchange_batch(marks, exclude_origin=receiver.device_id)
        assert not needs_snapshot
        assert [op["record_id"] for op in ops] == [SHARED]
    assert exchange(sender, receiver).rejected == 0
    with receiver.active():
        assert database.get_system_setting(SHARED) == "next delta"


@pytest.mark.parametrize("access", ["manifest", "page"])
def test_upgraded_sender_rejects_persisted_pre_upgrade_transfer(workers, monkeypatch, tmp_path, access):
    sender, receiver = workers("sender"), workers("receiver")
    competing = "settings_precedence_for_test"
    with sender.active():
        engine.create_network("Test network", device_name="Sender")
    join(sender, receiver)
    with receiver.active():
        marks = store.local_watermarks()
        assert learning._claim("session", "receiver-turn", "receiver prompt")
        retained = database.get_system_setting(BUDGET)
    with sender.active(), monkeypatch.context() as legacy:
        legacy.setattr(policy, "RETIRED_SHARED_SETTING_KEYS", ())
        database.set_system_setting(competing, "sender's earlier value")
        assert engine.scan_collection(SETTINGS, record_ids=[competing]) == 1
        prefix, _, need_copy = store.exchange_batch(marks, exclude_origin=receiver.device_id)
        assert not need_copy and [op["record_id"] for op in prefix] == [competing]
        assert learning._claim("session", "sender-turn", "sender prompt")
        database.set_system_setting(SHARED, "from the fresh copy")
        old = peer_snapshot.capture(receiver.device_id)
        saved = sender.data_dir / "peer-transfers" / old["transfer"] / "manifest.json"
        old_manifest_bytes = saved.read_bytes()
        index = next(i for i, page in enumerate(old["pages"]) if page["collection"] == SETTINGS)
        old_page = json.loads(peer_snapshot.page(receiver.device_id, old["transfer"], index))

    with receiver.active():
        trusted = peer_snapshot.verify_manifest(old, sender.device_id)
        assert BUDGET in {item["record_id"] for item in engine.open_snapshot_chunk(old_page, ctx=trusted)["items"]}
        assert engine.apply_batch(prefix).rejected == 0
        database.set_system_setting(competing, "receiver's newer value")
        assert engine.scan_collection(SETTINGS, record_ids=[competing]) == 1

    with sender.active():
        # Up-to-date frontier, unexpired, same membership and format: only the
        # retired-key policy makes the already signed transfer unsafe to serve.
        assert not store.requires_snapshot(old["watermarks"], exclude_origin=receiver.device_id)
        if access == "page":
            with pytest.raises(ValueError, match="Peer transfer expired"):
                peer_snapshot.page(receiver.device_id, old["transfer"], index)
        fresh = peer_snapshot.capture(receiver.device_id)
        assert fresh["transfer"] != old["transfer"]
        assert peer_snapshot.capture(receiver.device_id) == fresh
        assert saved.read_bytes() == old_manifest_bytes
        assert database.get_system_setting(BUDGET) is not None
        for i, page in enumerate(fresh["pages"]):
            raw = peer_snapshot.page(receiver.device_id, fresh["transfer"], i)
            peer_snapshot.save_page(tmp_path, i, raw, page)

    # An older peer still verifies and installs a fresh sender's copy; the
    # sender, not the receiver's upgraded policy, keeps the ledger off the wire.
    with receiver.active(), monkeypatch.context() as legacy:
        legacy.setattr(policy, "RETIRED_SHARED_SETTING_KEYS", ())
        peer_snapshot.apply(fresh, tmp_path, sender.device_id)
        assert database.get_system_setting(BUDGET) == retained
        assert database.get_system_setting(SHARED) == "from the fresh copy"
        assert database.get_system_setting(competing) == "receiver's newer value"


def test_unrelated_snapshot_gap_still_exchanges_settings_during_backfill(workers):
    sender, receiver = workers("sender"), workers("receiver")
    with sender.active():
        engine.create_network("Test network", device_name="Sender")
    join(sender, receiver)
    with receiver.active():
        marks = store.local_watermarks()
    with sender.active():
        # A deferred copy adopted a frontier but didn't retain the source ops.
        # This unrelated stream needs reconciliation; our own setting can travel.
        floor = {"deferred-origin": {"1": 1}}
        engine.adopt_watermarks(floor)
        with store.get_db() as conn:
            store.start_backfill(conn.cursor(), publisher=receiver.device_id, floor=floor, session_ids=["deferred"])
        assert transcripts.pending()
        with pytest.raises(ValueError, match="still receiving its transcripts"):
            peer_snapshot.capture(receiver.device_id)
        database.set_system_setting(SHARED, "during backfill")
        assert engine.scan_collection(SETTINGS, record_ids=[SHARED]) == 1
        ops, heads, needs_snapshot = store.exchange_batch(marks, limit=1, exclude_origin=receiver.device_id)
        assert needs_snapshot and heads["deferred-origin"] == floor["deferred-origin"]
        assert [op["record_id"] for op in ops] == [SHARED]
    with receiver.active():
        assert engine.apply_batch(ops).rejected == 0
        assert database.get_system_setting(SHARED) == "during backfill"

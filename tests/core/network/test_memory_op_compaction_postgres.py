# ruff: noqa: F811
"""Superseded whole-store memory envelopes are compacted early, and replicas converge (#1167).

Real PostgreSQL, independent worker schemas, the real memory producer
(``memory_records.publish``), signed exchange through the actual peer router
and the scheduled maintenance loop. Never the configured application database.
"""
import json

from fluid.codemode.skills import SkillStore

from app.db import network as store
from app.network import engine, memory_records, transport
from tests.core.network.test_transcript_op_retention_postgres import (  # noqa: F401
    admit,
    cluster,
    pg_workers,
    prune_once,
    sync,
)
from tests.core.network.test_transport import ServedBy

# A realistic store: dozens of facts of a few hundred bytes each, so every
# whole-store envelope is tens of kB, like the field's hundreds of kB.
SEED_FACTS = 60


def facts(worker):
    return SkillStore(worker.data_dir / "fluid" / "skills")


def seed(worker):
    with worker.active():
        store_ = facts(worker)
        for n in range(SEED_FACTS):
            store_.save_memory(f"Seed fact {n}: " + "the owner prefers concise, plain answers. " * 6,
                               protected=True)
        assert memory_records.publish() >= 1


def change(worker, n):
    """One memory change through the real producer: one new whole-store envelope."""
    with worker.active():
        facts(worker).save_memory(f"Change {n}: remember detail number {n}.", protected=True)
        assert memory_records.publish() == 1


def memory_ops(worker):
    return worker.sql("SELECT record_id, seq, pg_column_size(envelope) AS bytes FROM sync_ops "
                      "WHERE collection = 'memory' ORDER BY ingest_id")


def memory_records_of(worker):
    return {row["id"]: (row["content_hash"], row["deleted"])
            for row in worker.sql("SELECT id, content_hash, deleted FROM network_memory_records")}


def texts(worker):
    with worker.active():
        rows = worker.sql("SELECT id FROM network_memory_records")
        memory_records.apply_to_files([row["id"] for row in rows])
        return sorted(entry.text for entry in facts(worker).memories())


def report(label, ops):
    sizes = [row["bytes"] for row in ops]
    print(f"memory_compaction {label} ops={len(sizes)} bytes={sum(sizes)} "
          f"bytes_per_op={sum(sizes) // max(1, len(sizes))}")


async def test_superseded_memory_ops_compact_and_online_replica_converges(pg_workers, monkeypatch):
    source, peer = pg_workers("source"), pg_workers("peer")
    with source.active():
        engine.create_network("Memory")
    seed(source)
    admit(source, peer)
    served = ServedBy(source, "https://source.example.com")
    changes = 12
    for n in range(changes):
        change(source, n)
        await sync(peer, served)
    await sync(peer, served)  # the next request advertises the committed frontier
    before = memory_ops(source)
    report("before", before)
    assert len(before) == changes + 1  # seed + one whole-store copy per change
    assert memory_records_of(peer) == memory_records_of(source)

    await prune_once(source, monkeypatch)
    after = memory_ops(source)
    report("after", after)
    # Exactly the newest envelope of each memory record survives; no age floor.
    assert len(after) == len({row["record_id"] for row in before}) == 1
    assert after[0]["seq"] == before[-1]["seq"]
    assert after[0]["bytes"] == before[-1]["bytes"]  # retained bytes: one copy, not one per change
    with peer.active():
        marks = store.local_watermarks()
    with source.active():
        # The compacted prefix is below the peer's frontier: no gap, no state copy.
        assert not store.requires_snapshot(marks, exclude_origin=peer.device_id)

    # Later changes still take the ordinary op path and both replicas converge.
    served.requests.clear()
    change(source, changes)
    summary = await sync(peer, served)
    assert not summary.get("snapshot_required")
    assert summary["received"] >= 1
    assert not any(path.endswith("/state") for _, path in served.requests)
    assert memory_records_of(peer) == memory_records_of(source)
    assert texts(peer) == texts(source)
    assert len(texts(peer)) == SEED_FACTS + changes + 1

    # The relay's forwarding copy of the source stream compacts too.
    await sync(peer, served)
    peer_before = len(memory_ops(peer))
    await prune_once(peer, monkeypatch)
    assert peer_before > len(memory_ops(peer)) == 1


async def test_lagging_peer_pins_then_expired_peer_converges_by_state_copy(pg_workers, monkeypatch):
    source, online, offline = (pg_workers(name) for name in ("source", "online", "offline"))
    with source.active():
        engine.create_network("Memory")
    seed(source)
    admit(source, online)
    admit(source, offline)
    served = ServedBy(source, "https://source.example.com")
    await sync(online, served)
    await sync(offline, served)
    for n in range(6):
        change(source, n)
        await sync(online, served)
    await sync(online, served)
    held = memory_ops(source)
    assert len(held) == 7

    # A recent peer that has not received the tail pins every op it lacks;
    # only the seed copy, which every peer holds, is dropped.
    await prune_once(source, monkeypatch)
    assert memory_ops(source) == held[1:]
    held = held[1:]

    # Kill switch: no compaction even once nothing pins it.
    source.sql("INSERT INTO sync_peers(device_id, last_contact, updated_at) VALUES (%s, '2000-01-01', '2000-01-01') "
               "ON CONFLICT (device_id) DO UPDATE SET last_contact = EXCLUDED.last_contact, "
               "updated_at = EXCLUDED.updated_at", (offline.device_id,))
    transport._peer_watermarks.pop(offline.device_id, None)
    monkeypatch.setenv(store.MEMORY_OP_COMPACTION_ENV, "0")
    await prune_once(source, monkeypatch)
    assert memory_ops(source) == held
    monkeypatch.delenv(store.MEMORY_OP_COMPACTION_ENV)

    # Past the contact grace it stops pinning, like transcript retention.
    await prune_once(source, monkeypatch)
    assert len(memory_ops(source)) == 1
    with offline.active():
        marks = store.local_watermarks()
    with source.active():
        assert store.requires_snapshot(marks, exclude_origin=offline.device_id)
    recovered = await sync(offline, served)
    assert recovered["snapshot_required"]
    assert memory_records_of(offline) == memory_records_of(source)
    assert texts(offline) == texts(source) == texts(online)

    # Afterwards the returning peer resumes ordinary op sync.
    served.requests.clear()
    change(source, 99)
    assert not (await sync(offline, served)).get("snapshot_required")
    assert not any(path.endswith("/state") for _, path in served.requests)
    assert texts(offline) == texts(source)


def test_compaction_keeps_heads_newest_records_and_reports_bytes(pg_workers, caplog):
    source = pg_workers("source")
    with source.active():
        engine.create_network("Memory")
    seed(source)
    for n in range(3):
        change(source, n)
    with source.active():
        # A skill record shares the collection; its single op is its newest.
        (source.data_dir / "fluid" / "skills" / "greet.py").write_text("def run():\n    return 'hi'\n")
        assert memory_records.publish() == 1
    ops = memory_ops(source)
    with source.active(), caplog.at_level("INFO", logger=store.__name__):
        assert store.compact_memory_ops(source.device_id, {}) == 3
    kept = memory_ops(source)
    assert sorted(r["record_id"] for r in kept) == sorted({r["record_id"] for r in ops})
    assert {r["seq"] for r in kept} == {max(r["seq"] for r in ops if r["record_id"] == rid)
                                        for rid in {r["record_id"] for r in ops}}
    metrics = json.loads(next(r.getMessage().removeprefix("Memory sync operation compaction ")
                              for r in caplog.records
                              if r.getMessage().startswith("Memory sync operation compaction ")))
    assert metrics["deleted_ops"] == 3 and metrics["bytes_estimate"] > 0
    with source.active():
        assert store.compact_memory_ops(source.device_id, {}) == 0
        with store.get_db() as conn:
            assert store.next_origin_seq(conn.cursor(), source.device_id, 1) == max(r["seq"] for r in ops) + 1

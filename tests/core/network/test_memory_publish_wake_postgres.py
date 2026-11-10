"""Real PostgreSQL: committed bursts produce one signed, recallable publish pass."""
import time

from app.core import fluid_memory, resource_sync
from app.network import engine, memory_records, startup, transport
from fluid.codemode import transactions
from tests.core.network.test_transcript_op_retention_postgres import (  # shared owned-cluster fixtures
    admit, cluster, pg_workers,
)


def test_committed_burst_targets_files_and_peer_apply_does_not_echo(pg_workers, tmp_path, monkeypatch):
    source, peer = pg_workers("source"), pg_workers("peer")
    monkeypatch.setattr(fluid_memory, "prose_skills_root", lambda: tmp_path / "prose")
    monkeypatch.setattr(transactions, "_change_callback", None, raising=False)
    monkeypatch.setattr(memory_records, "_write_window", None)
    monkeypatch.setattr(resource_sync, "_emit", resource_sync._emit)
    monkeypatch.setattr(resource_sync, "_network_hooked", False, raising=False)
    startup._install_resource_hook()
    dirty = set()
    monkeypatch.setattr(transport, "request_scan", lambda names, **kwargs: dirty.update(names))
    with source.active():
        engine.create_network("Memory wake")
    admit(source, peer)
    dirty.clear()
    texts = [f"Unusual widget preference number {n}" for n in range(10)]
    with source.active():
        store = fluid_memory.store_for_owner("local")
        for text in texts:
            store.save_memory(text, when="Choosing widgets")
            assert engine.scan_all(collections={"memory"}, include_catalog=False, strict=True) == 0
        assert dirty == {"memory"}, "committed facts still wait for a periodic full scan"
        time.sleep(memory_records.WRITE_QUIET_SECONDS + .01)
        published = engine.scan_all(collections=dirty, include_catalog=False, strict=True)
        ops = source.sql("SELECT envelope FROM sync_ops WHERE collection = 'memory'")
    ids = [op["envelope"]["record_id"] for op in ops]
    # One pass after the burst: each record once, no intermediate copies.
    # The peer's presence has not reached the source, so the whole-store
    # record is still written next to the ten fact records and the barrier.
    assert published == len(ids) == len(set(ids)) == 12
    assert sorted({rid.split(":", 1)[0] for rid in ids}) == ["memory-barrier", "memory-fact", "memory-v2"]
    dirty.clear()
    with peer.active():
        assert engine.apply_batch([op["envelope"] for op in ops]).applied == len(ops)
        memory_records.apply_to_files(ids)
        replica = fluid_memory.store_for_owner("local")
        assert {e.text for e in replica._facts.entries()} == set(texts)
        assert dirty == set(), "peer application requested a republish"
        replica.save_memory("Peer's own widget preference", when="Choosing widgets")
        assert dirty == {"memory"}
        time.sleep(memory_records.WRITE_QUIET_SECONDS + .01)
        assert engine.scan_all(collections=dirty, include_catalog=False, strict=True) >= 1

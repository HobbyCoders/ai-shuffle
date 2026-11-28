"""Applying peers' memory records on the SQLite store (Memory v3 phase 3b):
storage failures are retried, and an older peer's deletion barrier fences
every peer's records for that owner."""
import json
import sqlite3
import time

import pytest

from app.core import fluid_memory
from app.network import memory_records
from fluid.codemode.facts import MemoryStorageError, _wire_barrier
from fluid.codemode.skills import SkillStore


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "skills"
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: path)
    monkeypatch.setattr(memory_records, "_reconcile_owed", False)
    return path


def _records(store):
    """One store's export as the per-fact records a peer publishes."""
    payload = store._facts.export()
    return [(f"memory-fact:peer:{e['id']}", dict(payload, entries=[e], tombstones=[]))
            for e in payload["entries"]], payload


@pytest.mark.parametrize("exc", [MemoryStorageError("import failed"),
                                 sqlite3.OperationalError("database is locked"),
                                 OSError(28, "No space left on device")])
def test_a_storage_failure_is_retried_not_dropped(root, exc):
    memory_records._rejected("memory-fact:peer:m1", exc)
    assert memory_records._reconcile_owed


def test_a_bad_record_is_refused_for_good(root):
    memory_records._rejected("memory-fact:peer:m1", ValueError("Invalid memory ID"))
    assert not memory_records._reconcile_owed


def test_a_busy_store_owes_a_reconcile_for_the_whole_group(root, tmp_path, monkeypatch):
    group, _ = _records(_peer(tmp_path / "peer", "Arrives while the store is busy."))
    SkillStore(root).save_memory("Local.", scope="global", durable=True)
    from fluid.codemode.fact_db import FactDB

    def busy(self, doc):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(FactDB, "write", busy)
    assert memory_records._merge_fact_group("local", group) == 0
    assert memory_records._reconcile_owed  # the next publish pass applies it again


def _peer(path, text):
    store = SkillStore(path)
    store.save_memory(text, scope="global", durable=True)
    return store


def test_an_older_peers_barrier_fences_another_peers_group(root, tmp_path):
    b = _peer(tmp_path / "b", "Peer A forgot this long ago.")
    group_b, payload = _records(b)
    entry = payload["entries"][0]
    # Peer A (pre-3b) remembers the deletion only in its Bloom barrier.
    from_a = dict(payload, store_id="peer-a", entries=[], tombstones=[],
                  forgotten=_wire_barrier("", [{"id": entry["id"], "origin": entry["origin"]}]))
    barriers = {}
    memory_records._note_barrier(barriers, from_a)
    # B's group merges first (groups are unordered): A's barrier still fences it.
    assert memory_records._merge_fact_group("local", group_b, barriers.get("local")) == len(group_b)
    assert not SkillStore(root).memories()
    assert memory_records._merge_fact_group("local", [("memory-barrier:peer-a", from_a)],
                                            barriers.get("local")) == 1
    assert memory_records._merge_fact_group("local", group_b, barriers.get("local")) == len(group_b)
    assert not SkillStore(root).memories()


def _network(monkeypatch, peers):
    """This worker plus ``peers`` ({device_id: capabilities or None})."""
    from types import SimpleNamespace

    from app.db import network as db_network
    from app.network import engine, membership
    members = [{"device_id": "self", "kind": membership.KIND_WORKER}]
    members += [{"device_id": d, "kind": membership.KIND_WORKER} for d in peers]
    monkeypatch.setattr(db_network, "list_members", lambda include_revoked=False: members)
    monkeypatch.setattr(engine, "all_presence", lambda: {
        d: {"capabilities": caps} for d, caps in peers.items() if caps is not None})
    return SimpleNamespace(device_id="self")


NEW = {memory_records.CAPABILITY: 1, memory_records.STORE_CAPABILITY: 1}


def test_when_every_worker_has_the_sqlite_store_the_export_is_not_trimmed(monkeypatch):
    ctx = _network(monkeypatch, {"peer-b": NEW, "peer-c": NEW})
    assert memory_records._legacy_readers(ctx) is False


@pytest.mark.parametrize("older", [{memory_records.CAPABILITY: 1}, None])
def test_one_older_or_unknown_worker_keeps_the_export_trimmed(monkeypatch, older):
    ctx = _network(monkeypatch, {"peer-b": NEW, "peer-c": older})
    assert memory_records._legacy_readers(ctx) is True


def test_a_trimmed_export_stays_under_what_an_older_worker_accepts(root):
    """An older worker refuses a store's records above 2 MiB in one merge."""
    store = SkillStore(root)
    kept = store.save_memory("An owner-kept fact.", scope="global", protected=True,
                             when="When checking owner-kept facts")
    doc = store._facts._load()
    now = time.time()
    for i in range(4000):
        doc["archived"].append({
            "id": f"m{100 + i}", "origin": doc["store_id"], "text": f"Archived {i} " + "x" * 600,
            "scope": "global", "revision": 1, "created_at": now - 9000, "updated_at": now - 9000 + i,
            "retired_at": now - 9000 + i, "retired_reason": "unused"})
    doc["archived"][-1]["retired_at"] = now  # retired just now
    store._facts._write(doc)
    assert len(json.dumps(store._facts.export()).encode()) > 2 * 1024 * 1024  # per-fact: all of it
    legacy = store._facts.export(legacy=True)
    assert len(json.dumps(legacy).encode()) < 2 * 1024 * 1024
    ids = {e["id"] for e in legacy["entries"]}
    assert kept.id in ids and "m4099" in ids and "m100" not in ids


def test_a_malformed_barrier_does_not_fence_other_records(root):
    barriers = {}
    memory_records._note_barrier(barriers, {"owner": "local", "forgotten": "zz"})
    memory_records._note_barrier(barriers, {"owner": "local", "forgotten": "00" * 3})
    assert barriers == {}

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


class _Records:
    """``network_memory_records`` as ``_apply`` and ``_standing_barriers``
    query it: {id: payload}."""

    def __init__(self, rows):
        self.rows = {rid: json.dumps(p) for rid, p in rows.items()}
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        if "LIKE" in sql:
            self.result = [{"id": rid} for rid in sorted(self.rows)
                           if any(rid.startswith(p[:-1]) for p in params)]
        else:
            self.result = [{"id": rid, "kind": "fact", "deleted": False, "content": self.rows[rid]}
                           for rid in params if rid in self.rows]

    def fetchall(self):
        return self.result


def _apply(monkeypatch, rows, batch, peers):
    ctx = _network(monkeypatch, peers)
    monkeypatch.setattr(memory_records, "get_db", lambda: _Records(rows))
    return memory_records._apply(sorted(batch), ctx, {})


def _forgetting(payload, store_id):
    """An older peer's record that forgot every fact of ``payload`` long ago:
    no tombstone, only its Bloom barrier."""
    return dict(payload, store_id=store_id, entries=[], tombstones=[],
                forgotten=_wire_barrier("", [{"id": e["id"], "origin": e["origin"]}
                                             for e in payload["entries"]]))


def test_whole_store_records_merge_behind_every_older_barrier_in_any_order(root, tmp_path, monkeypatch):
    payload = _peer(tmp_path / "b", "Peer A forgot this long ago.")._facts.export()
    # A's record sorts first, before the store holds the fact: merged inline,
    # it would fence nothing and B's copy would bring the fact back.
    rows = {"memory-v2:peer-a:h": _forgetting(payload, "peer-a"), "memory-v2:peer-b:h": payload}
    _apply(monkeypatch, rows, rows, {"peer-a": None, "peer-b": None})
    assert not SkillStore(root).memories() and not SkillStore(root).archived_memories()


def test_a_copy_arriving_after_the_older_peers_barrier_stays_out(root, tmp_path, monkeypatch):
    group, payload = _records(_peer(tmp_path / "b", "Peer A forgot this long ago."))
    rows = {"memory-barrier:peer-a:h": _forgetting(payload, "peer-a"), **dict(group)}
    # The barrier was applied in an earlier batch; this one brings only B's copy.
    _apply(monkeypatch, rows, dict(group), {"peer-a": None, "peer-b": NEW})
    assert not SkillStore(root).memories()


def test_a_filter_from_a_worker_on_the_sqlite_store_is_not_applied(root, tmp_path, monkeypatch):
    group, payload = _records(_peer(tmp_path / "b", "Nobody forgot this."))
    rows = {"memory-barrier:peer-a:h": _forgetting(payload, "peer-a"), **dict(group)}
    _apply(monkeypatch, rows, rows, {"peer-a": NEW, "peer": NEW})
    assert [e.text for e in SkillStore(root).memories()] == ["Nobody forgot this."]
    assert not SkillStore(root)._facts.export()["tombstones"]


def test_a_malformed_barrier_does_not_fence_other_records(root):
    barriers = {}
    memory_records._note_barrier(barriers, {"owner": "local", "forgotten": "zz"})
    memory_records._note_barrier(barriers, {"owner": "local", "forgotten": "00" * 3})
    assert barriers == {}


RULE = "Deploys wait until CI is green. A deploy started mid-run ships an untested build."
RULE_SUMMARY = "Not while CI is still running; wait for green first."


def _rule_peer(path):
    store = SkillStore(path)
    store.save_memory(RULE, scope="global", protected=True, when="When deploying the web app",
                      triggers={"command": ["make deploy"]}, summary=RULE_SUMMARY)
    return store


@pytest.mark.parametrize("kind", ["memory-fact", "memory-v2"])
def test_a_peers_summary_arrives_through_either_record_kind(root, tmp_path, monkeypatch, kind):
    group, payload = _records(_rule_peer(tmp_path / "b"))
    rows = dict(group) if kind == "memory-fact" else {"memory-v2:peer-b:h": payload}
    _apply(monkeypatch, rows, rows, {"peer-b": NEW if kind == "memory-fact" else None})
    [copy] = SkillStore(root).memories()
    assert copy.summary == RULE_SUMMARY
    stored = SkillStore(root)._facts.export()["entries"][0]
    assert stored["summary_at"] == payload["entries"][0]["summary_at"]


def test_a_peers_unscreened_summary_never_clears_the_local_one(root, tmp_path, monkeypatch):
    peer = _rule_peer(tmp_path / "b")
    group, _ = _records(peer)
    _apply(monkeypatch, dict(group), dict(group), {"peer-b": NEW})
    time.sleep(0.01)
    peer.save_memory(RULE, scope="global", protected=True, memory_id=peer.memories()[0].id,
                     expected_revision=1, summary="Wait for green CI first.")
    group, _ = _records(peer)
    [(rid, record)] = group
    record = json.loads(json.dumps(record))
    record["entries"][0]["summary"] = "Push with ghp_" + "a1B2" * 9  # fails the screen
    _apply(monkeypatch, {rid: record}, {rid: record}, {"peer-b": NEW})
    [copy] = SkillStore(root).memories()
    assert copy.summary == RULE_SUMMARY
    assert "ghp_" not in json.dumps(SkillStore(root)._facts.export())

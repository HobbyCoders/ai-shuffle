"""The SQLite fact store layer (Memory v3 phase 3b): the one-time import of
``memories.json``, the downgrade export, the journal-mode fallback and the
per-thread connection cache."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fluid.codemode import fact_db
from fluid.codemode.fact_db import FactDB, export_document
from fluid.codemode.skills import SkillStore


def _store_with_history(root):
    store = SkillStore(root)
    kept = store.save_memory("The build cache lives under .cache/build.", scope="global",
                             protected=True, when="When clearing the build cache")
    agent = store.save_memory("The staging deploy waits for the smoke job.", scope="project:demo",
                              durable=True)
    store.save_memory("The staging deploy waits for the smoke and lint jobs.", memory_id=agent.id,
                      expected_revision=agent.revision)
    gone = store.save_memory("A fact the owner forgets.", scope="global", durable=True)
    assert store.delete_memory(gone.id, explicit=True)
    return store, kept, agent


def _summary(store):
    live = {e.id: (e.text, e.scope, e.protected, e.always, e.revision) for e in store.memories()}
    archived = {e.id for e in store.archived_memories()}
    doc = store._facts._load(read_only=True)
    tombstones = {t["id"] for t in doc["tombstones"]}
    history = {e["id"]: len(e.get("history") or []) for e in doc["entries"] + doc["archived"]}
    return live, archived, tombstones, history


def test_memories_json_imports_once_and_is_kept_as_it_was(tmp_path):
    source, _, agent = _store_with_history(tmp_path / "a")
    legacy = tmp_path / "b" / "memories.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(export_document(tmp_path / "a")), encoding="utf-8")
    (tmp_path / "b" / "jit_deliveries.jsonl").write_text(json.dumps(
        {"at": time.time(), "fact": agent.id, "kind": "when", "session": "ses_1", "timing": "before"}) + "\n")
    before = legacy.read_bytes()

    imported = SkillStore(tmp_path / "b")
    # A read serves the document as it is, without importing it.
    assert _summary(imported) == _summary(source)
    assert not (tmp_path / "b" / "memory.db").exists()
    imported._facts.snapshot()  # the first writer's open imports it
    assert _summary(imported) == _summary(source)
    status = FactDB(tmp_path / "b").status()
    assert status["state"] == "ok" and status["imported"]["source"] == "memories.json"
    assert status["imported"]["deliveries"] == 1
    assert FactDB(tmp_path / "b").deliveries(session="ses_1")[0]["fact"] == agent.id
    assert legacy.read_bytes() == before  # the backup is never rewritten

    # A later open (another process) never imports again over newer facts.
    added = imported.save_memory("Added after the import.", scope="global", durable=True)
    fact_db.release_connections()
    reopened = SkillStore(tmp_path / "b")
    assert added.id in {e.id for e in reopened.memories()}
    assert FactDB(tmp_path / "b").status()["imported"] == status["imported"]
    assert legacy.read_bytes() == before


def test_downgrade_writes_a_document_an_older_build_reads_and_round_trips(tmp_path, capsys):
    store, _, _ = _store_with_history(tmp_path)
    expected = _summary(store)
    out = tmp_path / "export.json"
    assert fact_db.main(["export", str(tmp_path), str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema"] and doc["owner"] == "local" and isinstance(doc["forgotten"], str)
    assert {e["id"] for e in doc["entries"]} == set(expected[0])

    assert fact_db.main(["downgrade", str(tmp_path)]) == 0
    assert not (tmp_path / "memory.db").exists()
    assert list(tmp_path.glob("memory.db.downgraded-*"))
    assert json.loads((tmp_path / "memories.json").read_text(encoding="utf-8"))["entries"] == doc["entries"]
    # Upgrading again imports the downgraded document.
    assert _summary(SkillStore(tmp_path)) == expected


def test_a_filesystem_without_wal_falls_back_to_the_rollback_journal(tmp_path, monkeypatch):
    class NoWal:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *args):
            if sql.upper() == "PRAGMA JOURNAL_MODE=WAL":
                raise sqlite3.OperationalError("locking protocol")
            return self._con.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._con, name)

    real = FactDB._connect
    monkeypatch.setattr(FactDB, "_connect", lambda self, *a, **k: NoWal(real(self, *a, **k)))
    store = SkillStore(tmp_path)
    saved = store.save_memory("Works without WAL.", scope="global", durable=True)
    store.note_memories_recalled([saved.id])
    assert FactDB(tmp_path).status()["journal_mode"] == "delete"
    assert [e.id for e in store.memories()] == [saved.id]
    assert not (tmp_path / "memory.db-wal").exists()


def _count_connects(monkeypatch):
    opened = []
    real = FactDB._connect

    def connect(self, *args, **kwargs):
        opened.append(self.path)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(FactDB, "_connect", connect)
    return opened


def test_operations_reuse_one_connection_per_store(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    saved = store.save_memory("Reused connection.", scope="global", durable=True)
    opened = _count_connects(monkeypatch)
    for _ in range(5):
        store.note_memories_recalled([saved.id])
        store.memories()
    assert opened == []
    assert store._facts._load(read_only=True)["entries"][0]["uses"] == 5


def test_a_replaced_database_file_is_not_written_through_a_stale_connection(tmp_path):
    store = SkillStore(tmp_path)
    store.save_memory("Before the file is replaced.", scope="global", durable=True)
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"memory.db{suffix}").unlink(missing_ok=True)
    after = SkillStore(tmp_path).save_memory("After the file is replaced.", scope="global", durable=True)
    con = sqlite3.connect(str(tmp_path / "memory.db"))
    try:
        ids = [row[0] for row in con.execute("SELECT id FROM facts")]
    finally:
        con.close()
    assert ids == [after.id]


def test_nested_use_and_errors_get_a_fresh_connection(tmp_path):
    db = FactDB(tmp_path)
    SkillStore(tmp_path).save_memory("Nested.", scope="global", durable=True)
    with db.connection(create=True) as outer:
        with db._transaction(outer):
            doc, _ = db.read(create=False)  # nested: a private connection
            assert [e["text"] for e in doc["entries"]] == ["Nested."]
    with pytest.raises(sqlite3.OperationalError):
        with db.connection(create=True) as con:
            con.execute("SELECT * FROM no_such_table")
    assert str(db.path) not in fact_db._thread_pool()
    assert db.read(create=False)[0]["entries"]


def test_the_cache_holds_a_bounded_number_of_stores(tmp_path):
    fact_db.release_connections()
    for n in range(fact_db._POOL_SIZE + 4):
        SkillStore(tmp_path / str(n)).save_memory(f"Store {n}.", scope="global", durable=True)
    cached = fact_db._thread_pool()
    assert len(cached) == fact_db._POOL_SIZE
    assert str(tmp_path / str(fact_db._POOL_SIZE + 3) / "memory.db") in cached
    fact_db.release_connections()
    assert fact_db._thread_pool() == {}


# ─── review round 1 regressions ──────────────────────────────────────────

def _legacy_store(root):
    """A pre-3b store: only memories.json, holding two facts."""
    _store_with_history(root / "src")
    root.mkdir(parents=True, exist_ok=True)
    (root / "memories.json").write_text(json.dumps(export_document(root / "src")), encoding="utf-8")
    fact_db.release_connections()
    return root


def _fail_setup(monkeypatch):
    """The database cannot be set up (a full disk, say): DDL raises."""
    def broken(con):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(FactDB, "_create_tables", staticmethod(broken))


def test_a_failed_import_serves_the_json_read_only_and_raises_the_health_alert(tmp_path, monkeypatch):
    from fluid.codemode import memory_health
    from fluid.codemode.facts import MemoryStorageError

    root = _legacy_store(tmp_path / "b")
    before = (root / "memories.json").read_bytes()
    expected = {e.id for e in SkillStore(root).memories()}
    _fail_setup(monkeypatch)
    store = SkillStore(root)
    with pytest.raises(MemoryStorageError):
        store.save_memory("Refused while the import fails.", scope="global", durable=True)
    # Never an empty memory.db (or its journal files) passing for imported.
    assert not list(root.glob("memory.db*"))
    # Reads (and Recall) still serve the backup; usage is never written.
    assert {e.id for e in store.memories()} == expected
    store.note_memories_recalled(sorted(expected))
    from fluid.codemode import jit_delivery
    jit_delivery.record(store, "ses_1", [(sorted(expected)[0], "words")], jit_delivery.ON_TIME)
    assert not list(root.glob("memory.db*"))
    assert (root / "memories.json").read_bytes() == before
    result = memory_health.check(store)
    signal = next(s for s in result["signals"] if s["id"] == "capacity")
    assert signal["detail"]["storage"] == "migration_failed" and signal["alert"]
    assert signal["detail"]["refusal_event"] == "storage-migration"


def test_a_crash_mid_import_leaves_nothing_half_imported(tmp_path, monkeypatch):
    root = _legacy_store(tmp_path / "b")
    expected = _summary(SkillStore(root))
    real = FactDB._note_delivered

    def crash(con, pairs):
        raise sqlite3.OperationalError("disk I/O error")  # after the facts were inserted
    monkeypatch.setattr(FactDB, "_note_delivered", staticmethod(crash))
    with pytest.raises(ValueError):
        SkillStore(root).save_memory("Lost with the crash.", scope="global", durable=True)
    assert not list(root.glob("memory.db*"))
    monkeypatch.setattr(FactDB, "_note_delivered", staticmethod(real))
    store = SkillStore(root)
    store._facts.snapshot()  # imports in full on the next open
    assert _summary(store) == expected
    assert FactDB(root).status()["state"] == "ok"


_FAILING_FIRST_OPEN = """
import pathlib, sqlite3, sys, time
from fluid.codemode.fact_db import FactDB, MigrationError
root = pathlib.Path(sys.argv[1])
def stall_then_fail(con):
    (root / "b-ready").write_text("1")
    deadline = time.monotonic() + 30
    while not (root / "b-go").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.2)  # the other process is now waiting for the store
    raise sqlite3.OperationalError("database or disk is full")
FactDB._create_tables = staticmethod(stall_then_fail)
try:
    with FactDB(root).connection(create=True):
        print("imported")
except MigrationError as exc:
    print("failed:", exc)
"""


def test_two_processes_opening_first_and_one_failing_lose_nothing(tmp_path):
    root = _legacy_store(tmp_path / "b")
    expected = _summary(SkillStore(root))
    fact_db.release_connections()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(Path(fact_db.__file__).parents[2]), os.environ.get("PYTHONPATH", "")]))
    other = subprocess.Popen([sys.executable, "-c", _FAILING_FIRST_OPEN, str(root)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while not (root / "b-ready").exists():
        assert other.poll() is None and time.monotonic() < deadline, other.communicate()
        time.sleep(0.02)
    done = {}

    def open_first():  # this process's first open, while the other one imports
        try:
            with FactDB(root).connection(create=True) as con:
                done["marker"] = FactDB._meta(con, "sqlite.imported")
        except Exception as exc:  # noqa: BLE001 - reported below
            done["error"] = exc

    waiter = threading.Thread(target=open_first)
    waiter.start()
    (root / "b-go").write_text("1")
    out, err = other.communicate(timeout=60)
    waiter.join(60)
    assert out.startswith("failed:"), (out, err)
    assert "error" not in done and done["marker"]
    # The failure removed only its own temporary file: the database this
    # process imported (and its journal files) stayed in place.
    assert not list(root.glob(".memory.db.importing-*"))
    assert FactDB(root).status()["state"] == "ok"
    assert _summary(SkillStore(root)) == expected


def test_a_database_without_the_import_marker_is_imported_again(tmp_path):
    root = _legacy_store(tmp_path / "b")
    expected = _summary(SkillStore(root))
    con = sqlite3.connect(str(root / "memory.db"))
    con.executescript(fact_db._DDL)  # tables, but the import never committed
    con.close()
    store = SkillStore(root)
    store._facts.snapshot()
    assert _summary(store) == expected and FactDB(root).status()["imported"]


def test_a_corrupt_database_keeps_failing_instead_of_reading_empty(tmp_path):
    root = _legacy_store(tmp_path / "b")
    SkillStore(root)._facts.snapshot()
    fact_db.release_connections()
    fact_db._ready.clear()  # a new process
    (root / "memory.db").write_bytes(b"not a database" * 100)
    for name in ("memory.db-wal", "memory.db-shm"):
        (root / name).unlink(missing_ok=True)
    with pytest.raises(sqlite3.DatabaseError):
        SkillStore(root).memories()  # the API answers 503, never 200 []
    assert FactDB(root).status()["state"] == "unavailable"


@pytest.mark.parametrize("change, reason", [
    ({"schema": 99}, "newer application"), ({"owner": "api:someone-else"}, "another owner")])
def test_a_document_of_a_newer_build_or_another_owner_is_never_imported(tmp_path, change, reason):
    root = _legacy_store(tmp_path / "b")
    doc = json.loads((root / "memories.json").read_text(encoding="utf-8"))
    doc.update(change)
    (root / "memories.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(fact_db.MigrationError, match=reason):
        FactDB(root, owner="local").read(create=True)
    assert not list(root.glob("memory.db*"))


def test_a_busy_store_is_not_mistaken_for_one_without_wal(tmp_path, monkeypatch):
    class Busy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *args):
            if sql.upper() == "PRAGMA JOURNAL_MODE=WAL":
                raise sqlite3.OperationalError("database is locked")
            return self._con.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._con, name)

    real = FactDB._connect
    monkeypatch.setattr(FactDB, "_connect", lambda self, *a, **k: Busy(real(self, *a, **k)))
    with pytest.raises(ValueError, match="locked"):
        SkillStore(tmp_path).save_memory("Busy.", scope="global", durable=True)
    assert FactDB(tmp_path).failure is None  # busy is not a failed import
    monkeypatch.setattr(FactDB, "_connect", real)
    SkillStore(tmp_path).save_memory("Not busy.", scope="global", durable=True)
    assert FactDB(tmp_path).status()["journal_mode"] == "wal"


def test_the_ready_cache_follows_the_file_not_its_path(tmp_path):
    store = SkillStore(tmp_path)
    store.save_memory("Before the file was replaced.", scope="global", durable=True)
    fact_db.release_connections()
    for path in tmp_path.glob("memory.db*"):
        path.unlink()
    # The same path, a new file: it gets its tables again (no manual reset).
    saved = SkillStore(tmp_path).save_memory("After.", scope="global", durable=True)
    assert [e.id for e in SkillStore(tmp_path).memories()] == [saved.id]


def test_a_recall_count_survives_a_concurrent_structural_write(tmp_path):
    store = SkillStore(tmp_path)
    saved = store.save_memory("Counted while a writer works.", scope="global", durable=True)
    stale = store._facts._load()  # a writer read the store before the Recall
    store.note_memories_recalled([saved.id])
    recalled = fact_db.FactDB(tmp_path).read(create=False)[0]["entries"][0]
    stale["entries"][0]["text"] = "Counted while a writer works, edited."
    store._facts._write(stale)  # ... and writes its older counters back
    row = fact_db.FactDB(tmp_path).read(create=False)[0]["entries"][0]
    assert row["text"].endswith("edited.")
    assert row["uses"] == recalled["uses"] == 1
    assert row["last_recalled_at"] == recalled["last_recalled_at"] > 0


def test_a_recall_of_an_unknown_or_archived_id_writes_no_usage_row(tmp_path):
    store = SkillStore(tmp_path)
    saved = store.save_memory("Recalled once.", scope="global", durable=True)
    store.note_memories_recalled([saved.id, "m999"])
    assert FactDB(tmp_path).recent_uses(0) == {saved.id: 1}


def test_a_delivery_renews_a_fact_and_survives_pruning_its_rows(tmp_path, monkeypatch):
    from fluid.codemode import facts as facts_mod, jit_delivery
    store = SkillStore(tmp_path)
    saved = store.save_memory("Delivered just in time.", scope="global", durable=True,
                              when="When deploying")
    day = 86400
    start = time.time()
    # Saved 80 days ago and never used: ten days of life are left.
    doc = store._facts._load()
    doc["entries"][0]["expires_at"] = start + 10 * day
    store._facts._write(doc)
    jit_delivery.record(store, "ses_1", [(saved.id, "words")], jit_delivery.ON_TIME)
    con = sqlite3.connect(str(tmp_path / "memory.db"))
    with con:
        con.execute("DELETE FROM deliveries")  # pruned (age or row cap)
    con.close()
    life = store._facts._load(read_only=True)["entries"][0]["expires_at"]
    assert life >= start + facts_mod.AGENT_TTL - 5
    # Fifty days on, the fact is still live (it would have retired at day 10).
    monkeypatch.setattr(facts_mod.time, "time", lambda: start + 50 * day)
    store.reap()
    assert [e.id for e in SkillStore(tmp_path).memories()] == [saved.id]


def test_downgrade_keeps_the_pre_sqlite_backup_and_writes_after_the_move(tmp_path):
    root = _legacy_store(tmp_path / "b")
    original = (root / "memories.json").read_bytes()
    store = SkillStore(root)
    store.save_memory("Saved after the upgrade.", scope="global", durable=True)
    assert fact_db.main(["downgrade", str(root)]) == 0
    backups = list(root.glob("memories.json.pre-downgrade-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    texts = [e["text"] for e in json.loads((root / "memories.json").read_text(encoding="utf-8"))["entries"]]
    assert "Saved after the upgrade." in texts


def test_a_downgrade_that_cannot_move_the_database_changes_nothing(tmp_path, monkeypatch, capsys):
    root = _legacy_store(tmp_path / "b")
    SkillStore(root).save_memory("Kept in the database.", scope="global", durable=True)
    original = (root / "memories.json").read_bytes()
    real = fact_db._move

    def move(src, dst):
        if str(src).endswith("-wal"):
            raise PermissionError(32, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(fact_db, "_move", move)
    monkeypatch.setattr(fact_db.time, "sleep", lambda s: None)
    assert fact_db.main(["downgrade", str(root)]) == 1
    assert (root / "memory.db").exists() and not list(root.glob("memory.db.downgraded-*"))
    assert (root / "memories.json").read_bytes() == original  # never overwritten
    assert "unchanged" in capsys.readouterr().out
    texts = {e.text for e in SkillStore(root).memories()}
    assert "Kept in the database." in texts


def test_move_retries_a_file_held_open_on_windows(tmp_path, monkeypatch):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("x")
    calls = []
    real = type(src).replace

    def flaky(self, target):
        calls.append(self)
        if len(calls) < 3:
            raise PermissionError(13, "Access is denied")
        return real(self, target)

    monkeypatch.setattr(type(src), "replace", flaky)
    monkeypatch.setattr(fact_db.time, "sleep", lambda s: None)
    assert fact_db._move(src, dst) and dst.read_text() == "x" and len(calls) == 3
    assert fact_db._move(tmp_path / "missing", tmp_path / "c") is False


def _peer(root, texts):
    store = SkillStore(root)
    return store, [store.save_memory(t, scope="global", durable=True) for t in texts]


def _barrier_naming(export):
    """A Bloom filter that matches every fact of ``export``: what an older
    peer's filter is for a fact it forgot, or, by false positive, for one it
    never saw."""
    from fluid.codemode.facts import _wire_barrier
    return _wire_barrier("", [{"id": e["id"], "origin": e["origin"]} for e in export["entries"]])


def test_an_older_peers_barrier_cannot_be_undone_by_another_peers_copy(tmp_path):
    b, (fact,) = _peer(tmp_path / "b", ["A fact peer A forgot long ago."])
    copy_from_b = b._facts.export()
    # Peer A (pre-3b) forgot it more than 256 tombstones ago: only its Bloom
    # barrier remembers, and its record carries no tombstone for it. The
    # replication layer merges every peer group behind that barrier.
    c = SkillStore(tmp_path / "c")
    c._facts.merge(dict(copy_from_b, forgotten=_barrier_naming(copy_from_b)))
    assert not c.memories() and not c.archived_memories()
    c._facts.merge(copy_from_b)  # the same copy again, in a later batch
    assert not c.memories() and not c.archived_memories()
    # Kept out by a local fence: never a tombstone, never exported.
    exported = c._facts.export(legacy=True)
    assert not exported["tombstones"] and not exported["forgotten"]
    assert "fenced" not in exported and "fenced" not in c._facts.export()


def test_a_fact_a_bloom_filter_matches_by_chance_is_never_deleted_anywhere(tmp_path):
    b, (fact,) = _peer(tmp_path / "b", ["A fact nobody ever forgot."])
    c = SkillStore(tmp_path / "c")
    c._facts.merge(b._facts.export())
    # An older peer's filter matches it by false positive.
    c._facts.merge(dict(b._facts.export(), store_id="peer-a", entries=[], tombstones=[],
                        forgotten=_barrier_naming(b._facts.export())))
    assert [e.text for e in c.memories()] == [fact.text]  # held here: kept
    b._facts.merge(c._facts.export())
    d = SkillStore(tmp_path / "d")
    d._facts.merge(c._facts.export())
    for store in (b, c, d):
        assert [e.text for e in store.memories()] == [fact.text]
        assert not store._facts.export()["tombstones"]
        assert not store._facts.export(legacy=True)["tombstones"]


def test_an_export_to_sqlite_peers_carries_no_filter_and_a_legacy_one_stays_sparse(tmp_path):
    from fluid.codemode import facts as facts_mod
    store = SkillStore(tmp_path)
    store.save_memory("A fact that stays.", scope="global", durable=True)
    doc = store._facts._load()
    now = time.time()
    for i in range(5000):  # far more than a 16 KiB filter holds safely
        doc["tombstones"].append({"id": f"m{1000 + i}", "origin": doc["store_id"], "revision": 1,
                                  "scope": "global", "deleted_at": now - 5000 + i, "fingerprints": [],
                                  "key": f"key-{i}"})
    store._facts._write(doc)
    assert store._facts.export()["forgotten"] == ""  # every tombstone travels as a row
    legacy = store._facts.export(legacy=True)
    bits = int(legacy["forgotten"], 16).bit_count()
    assert bits <= facts_mod.MAX_BARRIER_FILL * facts_mod.BLOOM_BYTES * 8 + 16
    # The fact that was never forgotten does not match.
    gid = facts_mod.global_id(store._facts.export()["entries"][0])
    assert not facts_mod.barrier_contains(legacy["forgotten"], "id:" + gid)


def test_only_a_legacy_export_is_trimmed_and_it_keeps_what_matters(tmp_path, monkeypatch):
    from fluid.codemode import facts as facts_mod
    store = SkillStore(tmp_path)
    kept = store.save_memory("An owner-kept fact the owner archived.", scope="global", protected=True,
                             when="When checking archived owner facts")
    doc = store._facts._load()
    now = time.time()
    # Archived long ago (a pre-3b archive held owner-kept facts too): it is
    # listed first because the owner kept it.
    entry = doc["entries"].pop()
    entry.update(updated_at=now - 99_000, retired_at=now - 99_000, retired_reason="cap")
    doc["archived"].append(entry)
    for i in range(30):
        doc["archived"].append({
            "id": f"m{100 + i}", "origin": doc["store_id"], "text": f"Archived filler {i} " + "x" * 400,
            "scope": "global", "revision": 1, "created_at": now - 1000, "updated_at": now - 1000 + i,
            "retired_at": now - 1000 + i, "retired_reason": "unused"})
    doc["archived"][-1]["retired_at"] = now  # retired just now: must still propagate
    for i in range(300):
        doc["tombstones"].append({"id": f"m{1000 + i}", "origin": doc["store_id"], "revision": 1,
                                  "scope": "global", "deleted_at": now - 300 + i, "fingerprints": []})
    store._facts._write(doc)

    full = store._facts.export()
    assert len(full["tombstones"]) == 300
    assert len([e for e in full["entries"] if e.get("retired_at")]) >= 30

    newest = sorted(full["tombstones"], key=lambda t: -t["deleted_at"])[:facts_mod.MAX_LEGACY_TOMBSTONES]
    # Room for the tombstone list, the barrier and about ten archived facts.
    base = len(json.dumps({"entries": [], "tombstones": newest}).encode()) + 2 * facts_mod.BLOOM_BYTES
    monkeypatch.setattr(facts_mod, "EXPORT_BUDGET_BYTES", base + 10 * 700)
    legacy = store._facts.export(legacy=True)
    assert len(json.dumps(legacy).encode()) <= facts_mod.EXPORT_BUDGET_BYTES
    ids = {e["id"] for e in legacy["entries"]}
    assert kept.id in ids  # owner-kept first, however old
    assert "m129" in ids  # the newest retirement
    assert "m100" not in ids  # the oldest filler is left out, not deleted
    assert [t["id"] for t in legacy["tombstones"]] == [t["id"] for t in newest]
    # The barrier covers the tombstones left off the list, not the listed ones.
    oldest = min(full["tombstones"], key=lambda t: t["deleted_at"])
    assert facts_mod.barrier_contains(legacy["forgotten"], "id:" + facts_mod.global_id(oldest))
    assert full["forgotten"] == ""
    # A full export is never trimmed, whatever the budget.
    assert len(store._facts.export()["entries"]) == len(full["entries"])


# ─── act-time summaries on SQLite ────────────────────────────────────────

RULE = "Deploys wait until CI is green. A deploy started mid-run ships an untested build."
RULE_WHEN = "When deploying the web app"
RULE_SUMMARY = "Not while CI is still running; wait for green first."


def _save_rule(store, **extra):
    return store.save_memory(RULE, scope="global", when=RULE_WHEN,
                             triggers={"command": ["make deploy"]}, summary=RULE_SUMMARY, **extra)


def _stored(root, fact_id, part="entries"):
    from tests.fixtures.memory_doc import read_doc
    return next(e for e in read_doc(root)[part] if e["id"] == fact_id)


def test_a_summary_survives_reopening_retiring_and_coming_back(tmp_path):
    fact = _save_rule(SkillStore(tmp_path), durable=True)
    stamp = _stored(tmp_path, fact.id)["summary_at"]
    fact_db.release_connections()
    [reopened] = SkillStore(tmp_path).memories()
    assert reopened.summary == RULE_SUMMARY and stamp
    store = SkillStore(tmp_path)
    assert store.retire_memory(fact.id, reason="unused", expected_revision=1)
    archived = _stored(tmp_path, fact.id, "archived")
    assert archived["summary"] == RULE_SUMMARY and archived["summary_at"] == stamp
    back = store.save_memory(RULE, scope="global", memory_id=fact.id,
                             expected_revision=archived["revision"], durable=True)
    assert back.summary == RULE_SUMMARY and _stored(tmp_path, fact.id)["summary_at"] == stamp


def test_an_imported_store_keeps_its_summaries(tmp_path):
    src = tmp_path / "src"
    fact = _save_rule(SkillStore(src), protected=True)
    want = _stored(src, fact.id)
    root = tmp_path / "b"
    root.mkdir()
    (root / "memories.json").write_text(json.dumps(export_document(src)), encoding="utf-8")
    fact_db.release_connections()
    SkillStore(root)._facts.snapshot()  # the first locked read imports
    got = _stored(root, fact.id)
    assert (got["summary"], got["summary_at"]) == (want["summary"], want["summary_at"])
    assert [m.summary for m in SkillStore(root).memories()] == [RULE_SUMMARY]


def test_downgrade_writes_the_summaries_back(tmp_path):
    fact = _save_rule(SkillStore(tmp_path), protected=True)
    stamp = _stored(tmp_path, fact.id)["summary_at"]
    assert fact_db.main(["downgrade", str(tmp_path)]) == 0
    [entry] = json.loads((tmp_path / "memories.json").read_text(encoding="utf-8"))["entries"]
    assert (entry["summary"], entry["summary_at"]) == (RULE_SUMMARY, stamp)

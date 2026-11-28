"""The SQLite fact store layer (Memory v3 phase 3b): the one-time import of
``memories.json``, the downgrade export, the journal-mode fallback and the
per-thread connection cache."""
from __future__ import annotations

import json
import sqlite3
import time

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
    assert _summary(imported) == _summary(source)
    status = FactDB(tmp_path / "b").status()
    assert status["state"] == "ok" and status["imported"]["source"] == "memories.json"
    assert status["imported"]["deliveries"] == 1
    assert FactDB(tmp_path / "b").deliveries(session="ses_1")[0]["fact"] == agent.id
    assert legacy.read_bytes() == before  # the backup is never rewritten

    # A later open (another process) never imports again over newer facts.
    added = imported.save_memory("Added after the import.", scope="global", durable=True)
    fact_db._ready.clear()
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
    fact_db._ready.clear()
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
    fact_db._ready.clear()
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

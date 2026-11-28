"""Durability of the files the continual store owns.

Three failure modes that used to lose data silently:

1. ``meta.json`` / ``memories.json`` (now ``memory.db``) were rewritten with
   a plain open-truncate-write, and a load that failed to parse returned an
   empty document — so a truncated file emptied the store and the next save
   overwrote the evidence with colliding ids.
2. The store's logs grew without a cap.
3. ``reap_journal`` replaced the journal from a snapshot; a line appended in
   between vanished.
"""

import json
import logging
import os
from typing import Any

import pytest

from fluid.codemode import skills as skills_mod
from fluid.codemode.skills import SkillStore, journal_lock


def _journal(store: SkillStore, n: int) -> None:
    for i in range(n):
        store.journal_append({"code": f"x = {i}", "host_calls": 0})


# ─── atomic document writes and corrupt-file quarantine ──────────────────────


def test_a_corrupt_memories_file_is_never_imported_as_empty(tmp_path) -> None:
    # The store moved to SQLite (memory.db); memories.json is imported once.
    # A file that does not parse fails the import safely: nothing is served
    # as if memory were empty and overwritten, writes refuse, the file stays
    # exactly as it was, and the import runs once the file is repaired.
    from fluid.codemode import fact_db, facts

    good = json.dumps({"next_id": 3, "entries": [
        {"id": "m1", "text": "prefers tabs"}, {"id": "m2", "text": "tests live under tests/unit"}]})
    path = tmp_path / "memories.json"
    path.write_text(good[: len(good) // 2], encoding="utf-8")  # a torn copy
    store = SkillStore(tmp_path)
    assert store.memories() == []
    with pytest.raises(facts.MemoryStorageError, match="could not be upgraded"):
        store.save_memory("fresh start")
    assert path.read_text(encoding="utf-8") == good[: len(good) // 2]
    assert fact_db.FactDB(tmp_path).status()["state"] == "migration_failed"

    path.write_text(good, encoding="utf-8")  # the owner restores the file
    assert store.save_memory("fresh start").id == "m3"
    assert [m.text for m in store.memories()] == ["prefers tabs", "tests live under tests/unit",
                                                  "fresh start"]
    assert path.read_text(encoding="utf-8") == good  # kept as the backup


def test_meta_document_gets_the_same_quarantine(tmp_path, caplog) -> None:
    store = SkillStore(tmp_path)
    store.set_synth_cursor(42)
    (tmp_path / "meta.json").write_text('{"synth_cursor": 4', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="fluid.codemode.skills"):
        assert store.synth_cursor() == 0
    assert list(tmp_path.glob("meta.json.corrupt-*"))


def test_a_non_object_memories_file_fails_the_import_safely(tmp_path) -> None:
    from fluid.codemode import facts

    (tmp_path / "memories.json").write_text("[]", encoding="utf-8")
    store = SkillStore(tmp_path)
    assert store.memories() == []
    with pytest.raises(facts.MemoryStorageError):
        store.save_memory("x")
    assert (tmp_path / "memories.json").read_text(encoding="utf-8") == "[]"


def test_interrupted_save_leaves_the_previous_facts_intact(tmp_path, monkeypatch) -> None:
    import sqlite3

    from fluid.codemode import fact_db, facts

    store = SkillStore(tmp_path)
    store.save_memory("first")

    def boom(cls, con):
        raise sqlite3.OperationalError("disk I/O error")  # dies mid-transaction

    monkeypatch.setattr(fact_db.FactDB, "_bump", classmethod(boom))
    with pytest.raises(facts.MemoryStorageError):
        store.save_memory("second")
    monkeypatch.undo()
    assert [m.text for m in SkillStore(tmp_path).memories()] == ["first"]


def test_meta_saves_go_through_a_temp_file_and_rename(tmp_path, monkeypatch) -> None:
    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((os.path.basename(str(src)), os.path.basename(str(dst))))
        real_replace(src, dst)

    monkeypatch.setattr(skills_mod.os, "replace", spy)
    store = SkillStore(tmp_path)
    store.save_memory("x")
    store.set_synth_cursor(1)
    targets = {dst for _, dst in seen}
    assert "meta.json" in targets
    # Facts commit in SQLite transactions; no memories.json is written.
    assert "memories.json" not in targets and not (tmp_path / "memories.json").exists()
    assert all(src.startswith(dst + ".tmp-") for src, dst in seen)


# ─── journal reaping cannot lose an append ───────────────────────────────────


def test_reap_aborts_when_a_line_lands_between_read_and_replace(
    tmp_path, monkeypatch
) -> None:
    """A writer that ignores the lock appends after reap measured the file."""
    monkeypatch.setattr(skills_mod, "JOURNAL_KEEP_ENTRIES", 2)
    store = SkillStore(tmp_path)
    _journal(store, 10)
    store.set_synth_cursor(10)
    journal = tmp_path / "journal.jsonl"
    real_fsync = os.fsync
    real_rewrite = skills_mod._rewrite_dropping_prefix
    rewriting_journal = False

    def rewrite_journal(*args: Any, **kwargs: Any) -> bool:
        nonlocal rewriting_journal
        rewriting_journal = True
        try:
            return real_rewrite(*args, **kwargs)
        finally:
            rewriting_journal = False

    monkeypatch.setattr(skills_mod, "_rewrite_dropping_prefix", rewrite_journal)

    def append_during_rewrite(fd: int) -> None:
        # Runs after reap has copied the journal into the temp file and
        # before it re-checks the size: the window a lock-ignoring writer
        # can hit.
        real_fsync(fd)
        if not rewriting_journal:
            return  # workflow-loss metadata is persisted before the journal copy
        with journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"code": "late = 1"}) + "\n")

    monkeypatch.setattr(skills_mod.os, "fsync", append_during_rewrite)
    assert store.reap_journal() == 0
    monkeypatch.setattr(skills_mod.os, "fsync", real_fsync)

    codes = [e["code"] for e in store.journal_entries(0)]
    assert codes == [f"x = {i}" for i in range(10)] + ["late = 1"]
    assert store.synth_cursor() == 10  # not rebased: nothing was dropped
    assert not list(tmp_path.glob("journal.jsonl.tmp-*"))
    # With the writer gone the next reap succeeds and keeps the late line.
    assert store.reap_journal() == 9
    assert [e["code"] for e in store.journal_entries(0)] == ["x = 9", "late = 1"]


def test_reap_skips_while_another_holder_has_the_journal_lock(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(skills_mod, "JOURNAL_KEEP_ENTRIES", 2)
    store = SkillStore(tmp_path)
    _journal(store, 10)
    store.set_synth_cursor(10)
    with journal_lock(tmp_path / "journal.jsonl") as held:
        assert held
        assert store.reap_journal() == 0
        assert store.journal_count() == 10
    assert store.reap_journal() == 8


def test_inference_append_respects_an_unavailable_lock(tmp_path, monkeypatch) -> None:
    """A stuck inference queue cannot bypass its lock; explicit saves are separate."""
    monkeypatch.setattr(skills_mod, "JOURNAL_APPEND_LOCK_WAIT", 0.05)
    store = SkillStore(tmp_path)
    with journal_lock(tmp_path / "journal.jsonl") as held:
        assert held
        store.journal_append({"code": "under contention"})
    assert store.journal_entries(0) == []

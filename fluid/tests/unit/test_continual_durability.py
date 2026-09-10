"""Durability of the files the continual store owns.

Three failure modes that used to lose data silently:

1. ``meta.json`` / ``memories.json`` / ``anchors.json`` were rewritten with
   a plain open-truncate-write, and a load that failed to parse returned an
   empty document — so a truncated file emptied the store and the next save
   overwrote the evidence with colliding ids.
2. ``parked.jsonl`` and ``.evolution-log.jsonl`` grew without a cap.
3. ``reap_journal`` replaced the journal from a snapshot; a line appended in
   between vanished.
"""

import json
import logging
import os

import pytest

from fluid.codemode import skills as skills_mod
from fluid.codemode.anchors import AnchorStore
from fluid.codemode.skills import SkillStore, journal_lock


def _journal(store: SkillStore, n: int) -> None:
    for i in range(n):
        store.journal_append({"code": f"x = {i}", "host_calls": 0})


# ─── atomic document writes and corrupt-file quarantine ──────────────────────


def test_truncated_memories_file_is_set_aside_not_overwritten(tmp_path, caplog) -> None:
    store = SkillStore(tmp_path)
    store.save_memory("prefers tabs")
    store.save_memory("tests live under tests/unit")
    path = tmp_path / "memories.json"
    good = path.read_text(encoding="utf-8")
    # Truncate mid-object, the shape a crash mid-write used to leave behind.
    path.write_text(good[: len(good) // 2], encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="fluid.codemode.skills"):
        assert store.memories() == []
    assert any("memories.json" in r.getMessage() for r in caplog.records)
    assert not path.exists()
    aside = list(tmp_path.glob("memories.json.corrupt-*"))
    assert len(aside) == 1
    assert aside[0].read_text(encoding="utf-8") == good[: len(good) // 2]

    # The next save starts a fresh document beside the quarantined one.
    entry = store.save_memory("fresh start")
    assert entry.id == "m1"
    assert [m.text for m in store.memories()] == ["fresh start"]
    assert aside[0].exists()


def test_meta_and_anchors_documents_get_the_same_quarantine(tmp_path, caplog) -> None:
    store = SkillStore(tmp_path)
    store.set_synth_cursor(42)
    (tmp_path / "meta.json").write_text('{"synth_cursor": 4', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="fluid.codemode.skills"):
        assert store.synth_cursor() == 0
    assert list(tmp_path.glob("meta.json.corrupt-*"))

    anchors = AnchorStore(tmp_path)
    anchors.add(title="t", prompt="do x", checks=[{"kind": "contains", "pattern": "x"}])
    (tmp_path / "anchors.json").write_text("[1, 2", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="fluid.codemode.skills"):
        assert anchors.list() == []
    assert list(tmp_path.glob("anchors.json.corrupt-*"))
    assert anchors.add(
        title="t2", prompt="do y", checks=[{"kind": "contains", "pattern": "y"}]
    ).id == "a1"


def test_non_object_document_is_quarantined_too(tmp_path) -> None:
    store = SkillStore(tmp_path)
    (tmp_path / "memories.json").write_text("[]", encoding="utf-8")
    assert store.memories() == []
    assert list(tmp_path.glob("memories.json.corrupt-*"))
    assert not (tmp_path / "memories.json").exists()


def test_interrupted_save_leaves_the_previous_document_intact(tmp_path, monkeypatch) -> None:
    store = SkillStore(tmp_path)
    store.save_memory("first")
    path = tmp_path / "memories.json"
    before = path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated crash before the rename")

    monkeypatch.setattr(skills_mod.os, "replace", boom)
    with pytest.raises(OSError):
        store.save_memory("second")
    monkeypatch.undo()

    assert path.read_text(encoding="utf-8") == before
    assert [m.text for m in store.memories()] == ["first"]
    assert not list(tmp_path.glob("memories.json.tmp-*"))


def test_document_saves_go_through_a_temp_file_and_rename(tmp_path, monkeypatch) -> None:
    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((os.path.basename(str(src)), os.path.basename(str(dst))))
        real_replace(src, dst)

    monkeypatch.setattr(skills_mod.os, "replace", spy)
    store = SkillStore(tmp_path)
    store.save_memory("x")
    store.set_synth_cursor(1)
    AnchorStore(tmp_path).add(
        title="t", prompt="p", checks=[{"kind": "contains", "pattern": "p"}]
    )
    targets = {dst for _, dst in seen}
    assert {"memories.json", "meta.json", "anchors.json"} <= targets
    assert all(src.startswith(dst + ".tmp-") for src, dst in seen)


# ─── every log the store root holds is bounded ───────────────────────────────


def test_reap_bounds_the_evolution_gate_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(skills_mod, "MAX_LOG_ENTRIES", 5)
    store = SkillStore(tmp_path)
    parked = tmp_path / "parked.jsonl"
    log = tmp_path / ".evolution-log.jsonl"
    with parked.open("w", encoding="utf-8") as fh:
        for i in range(30):
            fh.write(json.dumps({"i": i, "content": "x" * 4000}) + "\n")
    with log.open("w", encoding="utf-8") as fh:
        for i in range(12):
            fh.write(json.dumps({"i": i}) + "\n")

    dropped = store.reap()
    assert dropped["parked"] == 25
    assert dropped["evolution_log"] == 7
    kept = [
        json.loads(line)["i"]
        for line in parked.read_text(encoding="utf-8").splitlines()
    ]
    assert kept == list(range(25, 30))  # newest survive
    assert len(log.read_text(encoding="utf-8").splitlines()) == 5
    assert store.reap() == {}  # idempotent once under the cap


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

    def append_during_rewrite(fd):
        # Runs after reap has copied the journal into the temp file and
        # before it re-checks the size: the window a lock-ignoring writer
        # can hit.
        real_fsync(fd)
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

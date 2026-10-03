"""Automatic curation archives instead of forgetting, and never touches user facts."""
import json

import pytest

from fluid.codemode.continual import Synthesizer
from fluid.codemode.skills import SkillStore


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def stream(self, request):
        reply = self.replies.pop(0)

        class Chunk:
            kind = "text_delta"
            payload = {"text": reply}

        yield Chunk()


def archived_ids(store):
    return {m.id for m in store.archived_memories()}


@pytest.mark.asyncio
async def test_learner_delete_archives_without_a_deletion_barrier(tmp_path):
    store = SkillStore(tmp_path)
    fact = store.save_memory("The staging box is called Atlas.")
    synth = Synthesizer(provider=None, model="m", store=store)
    assert await synth._apply(f'<delete-memory id="{fact.id}"/>') == [f"-memory:{fact.id}"]
    assert fact.id not in {m.id for m in store.memories()}
    assert fact.id in archived_ids(store)
    assert [e.id for e, archived in store.search_memories("Atlas")] == [fact.id]
    doc = store._facts.snapshot()
    assert not doc["tombstones"]
    # An agent can save the same fact again; the archived copy comes back.
    again = store.save_memory("The staging box is called Atlas.", durable=True)
    assert again.id == fact.id
    assert fact.id in {m.id for m in store.memories()}
    raw = next(e for e in store._facts.snapshot()["entries"] if e["id"] == fact.id)
    assert "retired_at" not in raw


@pytest.mark.asyncio
async def test_learner_cannot_revive_a_retired_fact_by_repeating_it(tmp_path):
    store = SkillStore(tmp_path)
    fact = store.save_memory("The staging box is called Atlas.")
    assert store.retire_memory(fact.id, reason="test")
    store.save_memory("The staging box is called Atlas.")
    assert fact.id in archived_ids(store)
    assert not store.memories()


def test_user_requested_fact_is_never_retired(tmp_path):
    store = SkillStore(tmp_path)
    fact = store.save_memory("The user's name is Kept.", protected=True)
    assert not store.retire_memory(fact.id, reason="test")
    assert [m.id for m in store.memories()] == [fact.id]


def test_rewrite_brings_a_retired_fact_back(tmp_path):
    store = SkillStore(tmp_path)
    fact = store.save_memory("Port 8000 serves the API.")
    store.retire_memory(fact.id, reason="test")
    store.save_memory("Port 8080 serves the API.", memory_id=fact.id)
    live = store._facts.snapshot()["entries"]
    assert [e["text"] for e in live] == ["Port 8080 serves the API."]
    assert "retired_at" not in live[0]


def test_retirement_replicates_and_merge_keeps_it_archived(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    fact = a.save_memory("Synthetic fact both workers know")
    b._facts.merge(a._facts.export())
    assert len(b.memories()) == 1
    assert a.retire_memory(fact.id, reason="test")
    # The archived copy is causally newer, so the peer archives it too ...
    b._facts.merge(a._facts.export())
    assert not b.memories() and len(b.archived_memories()) == 1
    # ... and the peer's older live copy cannot pull it back on either side.
    a._facts.merge(b._facts.export())
    assert not a.memories() and fact.id in archived_ids(a)


@pytest.mark.asyncio
async def test_consolidation_retires_only_facts_it_was_shown(tmp_path, monkeypatch):
    import fluid.codemode.continual as continual

    store = SkillStore(tmp_path)
    shown = store.save_memory("aaa shown fact")
    hidden = store.save_memory("zzz " + "fact outside this section " * 8)
    user = store.save_memory("The user's name is Kept.", protected=True)
    monkeypatch.setattr(continual, "CONSOLIDATE_MEMORY_CHARS", 200)
    provider = Scripted(
        f'<delete-memory id="{shown.id}"/><delete-memory id="{hidden.id}"/>'
        f'<delete-memory id="{user.id}"/>'
    )
    changed = await Synthesizer(provider, "m", store).consolidate()
    assert changed == [f"-memory:{shown.id}"]
    live = {m.id for m in store.memories()}
    assert hidden.id in live and user.id in live
    assert shown.id in archived_ids(store)


def test_snapshot_slice_prefers_recalled_facts_over_unconfirmed_harvests(tmp_path, monkeypatch):
    from fluid.codemode import facts

    monkeypatch.setattr(facts, "MAX_PROMPT_CHARS", 200)
    monkeypatch.setattr(facts, "RESERVED_INFERRED_CHARS", 200)
    store = SkillStore(tmp_path)
    harvest = store.save_memory("pytest is not importable from the worker Python.",
                                provenance="tool", key="env.pytest")
    used = store.save_memory("The staging deploy runs from the release branch.", durable=True)
    store.note_memories_recalled([used.id])
    plan = store.memory_snapshot_plan("project:x")
    assert plan["shown_ids"] == [used.id] and plan["omitted_ids"] == [harvest.id]
    assert "1 saved fact omitted for space" in store.render_memory_section("project:x")
    # A fact in the user's own words still comes first.
    said = store.save_memory("User wants short answers.", provenance="user")
    plan = store.memory_snapshot_plan("project:x")
    assert said.id in plan["shown_ids"] and harvest.id in plan["omitted_ids"]


def test_dropping_a_user_fact_for_space_is_counted(tmp_path, monkeypatch):
    from fluid.codemode import facts

    monkeypatch.setattr(facts, "MAX_EXPLICIT", 1)
    monkeypatch.setattr(facts, "MAX_ARCHIVED_EXPLICIT", 1)
    store = SkillStore(tmp_path)
    for i in range(3):
        store.save_memory(f"User preference number {i}", protected=True)
    losses = store._facts.snapshot()["losses"]
    assert losses["explicit_dropped"] == 1 and losses["last_explicit_dropped_at"] > 0


def test_failed_saves_are_counted_by_kind(tmp_path, monkeypatch):
    from fluid.codemode import facts

    store = SkillStore(tmp_path)
    with pytest.raises(ValueError):
        store.save_memory("x" * 600)
    monkeypatch.setattr(facts, "MAX_DOCUMENT_BYTES", 10)
    with pytest.raises(ValueError):
        store.save_memory("A fact that no longer fits")
    record = store.memory_save_failures()
    assert record["rejected"] == 1 and record["storage"] == 1
    assert len(record["storage_times"]) == 1
    assert "fits" not in json.dumps(record)

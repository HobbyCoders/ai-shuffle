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


def test_a_full_store_refuses_a_new_user_fact_and_keeps_every_saved_one(tmp_path, monkeypatch):
    from fluid.codemode import facts

    monkeypatch.setattr(facts, "MAX_EXPLICIT", 2)
    store = SkillStore(tmp_path)
    first = store.save_memory("User preference number 0", protected=True)
    store.save_memory("User preference number 1", protected=True)
    with pytest.raises(ValueError, match=r"^Memory is full: 2 facts .*Forget or merge one first"):
        store.save_memory("User preference number 2", protected=True)
    # Promoting an agent fact to a user fact also needs a free slot.
    agent = store.save_memory("The deploy host is Atlas.", durable=True)
    with pytest.raises(ValueError, match="Memory is full"):
        store.save_memory("The deploy host is Atlas.", protected=True)
    doc = store._facts.snapshot()
    kept = sorted(e["text"] for e in doc["entries"] + doc["archived"] if e.get("protected"))
    assert kept == ["User preference number 0", "User preference number 1"]
    assert not doc.get("losses", {}).get("explicit_dropped")
    assert not [e for e in doc["archived"] if e.get("protected")]
    assert agent.id in {m.id for m in store.memories()}
    # Correcting a saved fact needs no new slot, and agent saves still work.
    fixed = store.save_memory("User preference number 0, corrected", memory_id=first.id,
                              protected=True, expected_revision=first.revision)
    assert fixed.revision == first.revision + 1
    store.save_memory("Another agent fact.", durable=True)
    # A refusal is the rule working, not a lost write.
    assert store.memory_save_failures().get("storage") is None
    assert store.memory_save_failures()["rejected"] == 2


def test_forgetting_frees_a_slot_for_the_next_user_fact(tmp_path, monkeypatch):
    from fluid.codemode import facts

    monkeypatch.setattr(facts, "MAX_EXPLICIT", 1)
    store = SkillStore(tmp_path)
    old = store.save_memory("User preference number 0", protected=True)
    with pytest.raises(ValueError, match="Memory is full"):
        store.save_memory("User preference number 1", protected=True)
    assert store.delete_memory(old.id, explicit=True, expected_revision=old.revision)
    assert store.save_memory("User preference number 1", protected=True).protected


def test_merge_past_the_cap_keeps_every_user_fact(tmp_path, monkeypatch):
    from fluid.codemode import facts

    monkeypatch.setattr(facts, "MAX_EXPLICIT", 2)
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    for i in range(2):
        a.save_memory(f"User fact from worker A {i}", protected=True)
        b.save_memory(f"User fact from worker B {i}", protected=True)
    a._facts.merge(b._facts.export())
    doc = a._facts.snapshot()
    assert sum(1 for e in doc["entries"] + doc["archived"] if e.get("protected")) == 4
    with pytest.raises(ValueError, match="Memory is full"):
        a.save_memory("One more user fact", protected=True)


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


# ─── expiry archives first ──────────────────────────────────────────────────

def fake_clock(monkeypatch, start=1_000_000.0):
    from fluid.codemode import facts

    clock = [start]
    monkeypatch.setattr(facts.time, "time", lambda: clock[0])
    return clock


@pytest.mark.parametrize("tier", ["inferred", "durable"])
def test_expired_fact_moves_to_the_archive_and_recall_still_finds_it(tmp_path, monkeypatch, tier):
    from fluid.codemode import facts

    clock = fake_clock(monkeypatch)
    store = SkillStore(tmp_path)
    fact = store.save_memory("The nightly backup runs on Atlas.", durable=tier == "durable" or None)
    clock[0] = fact.expires_at + 1
    assert not store.memories()
    assert archived_ids(store) == {fact.id}
    assert [e.id for e, archived in store.search_memories("backup Atlas") if archived] == [fact.id]
    assert not store._facts.snapshot().get("losses", {}).get("expired_archived")
    # Removed for good only ARCHIVE_RETENTION after the expiry date, counted.
    clock[0] = fact.expires_at + facts.ARCHIVE_RETENTION - 1
    assert archived_ids(store) == {fact.id}
    clock[0] = fact.expires_at + facts.ARCHIVE_RETENTION
    assert not archived_ids(store) and not store.memories()
    assert store._facts.snapshot()["losses"]["expired_archived"] == 1


def test_saving_an_expired_fact_again_brings_it_back(tmp_path, monkeypatch):
    from fluid.codemode import facts

    clock = fake_clock(monkeypatch)
    store = SkillStore(tmp_path)
    harvest = store.save_memory("Node dependencies are missing in frontend.", provenance="tool", key="env.node")
    learned = store.save_memory("The release notes live in CHANGELOG.md.")
    clock[0] = harvest.expires_at + 1
    assert not store.memories()
    # The same observation again: live with a fresh lifetime, same ID.
    again = store.save_memory("Node dependencies are missing in frontend.", provenance="tool", key="env.node")
    assert again.id == harvest.id and again.expires_at == clock[0] + facts.INFERRED_TTL
    # A changed observation under the same key is new, not already expired.
    changed = store.save_memory("The release notes live in docs/CHANGELOG.md.", memory_id=learned.id)
    assert changed.expires_at > clock[0]
    assert {m.id for m in store.memories()} == {harvest.id, learned.id}


def test_user_saved_facts_never_expire_or_leave_the_archive(tmp_path, monkeypatch):
    from fluid.codemode import facts

    clock = fake_clock(monkeypatch)
    store = SkillStore(tmp_path)
    kept = store.save_memory("The user's timezone is UTC.", protected=True)
    clock[0] += facts.ARCHIVE_RETENTION * 3
    assert [m.id for m in store.memories()] == [kept.id]


def test_an_expired_fact_stays_archived_after_a_merge(tmp_path, monkeypatch):
    clock = fake_clock(monkeypatch)
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    fact = a.save_memory("Synthetic fact both workers know")
    b._facts.merge(a._facts.export())
    clock[0] = fact.expires_at + 1
    a._facts.merge(b._facts.export())
    b._facts.merge(a._facts.export())
    assert not a.memories() and not b.memories()
    assert archived_ids(a) == {fact.id} and len(b.archived_memories()) == 1


# ─── environment facts are per worker ───────────────────────────────────────

A_PYTEST = "pytest is importable from the worker Python /usr/bin/python3."
B_PYTEST = "pytest is not importable from the worker Python C:/Py/python.exe."


def harvest(store, text, key="env.pytest"):
    from fluid.context.runtime_facts import write_harvested_facts

    return write_harvested_facts(store, "project:x", [(key, text)])


def sync(a, b, rounds=2):
    for _ in range(rounds):
        b._facts.merge(a._facts.export())
        a._facts.merge(b._facts.export())


def raw(store):
    doc = store._facts.snapshot()
    return doc["entries"] + doc["archived"]


def test_two_workers_keep_their_own_environment_facts_through_sync(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    harvest(a, A_PYTEST)
    harvest(b, B_PYTEST)
    user = a.save_memory("The user prefers tabs.", protected=True)
    sync(a, b)
    # Both copies replicate to both workers, unchanged and not in conflict.
    for store in (a, b):
        env = sorted((e["text"], e["revision"], bool(e.get("conflicted")))
                     for e in raw(store) if e.get("key") == "env.pytest")
        assert env == [(A_PYTEST, 1, False), (B_PYTEST, 1, False)]
    # Each worker sees, recalls and renders only its own.
    assert [m.text for m in a.memories() if m.key == "env.pytest"] == [A_PYTEST]
    assert [m.text for m in b.memories() if m.key == "env.pytest"] == [B_PYTEST]
    assert [e.text for e, _ in b.search_memories("pytest importable worker")] == [B_PYTEST]
    section = b.render_memory_section("project:x")
    assert B_PYTEST in section and A_PYTEST not in section
    assert "omitted for space" not in section and "archived" not in section
    # What the user said is shared.
    assert user.id in {m.id.split(":")[-1] for m in b.memories()}
    # Harvesting again after sync rewrites nothing on either worker.
    harvest(a, A_PYTEST)
    harvest(b, B_PYTEST)
    sync(a, b)
    assert all(e["revision"] == 1 for s in (a, b) for e in raw(s) if e.get("key") == "env.pytest")


def test_identical_harvests_on_two_workers_stay_separate(tmp_path):
    from fluid.codemode import memory_health

    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    harvest(a, A_PYTEST)
    harvest(b, A_PYTEST)
    sync(a, b)
    assert memory_health.repair(a) == [] and memory_health.repair(b) == []
    sync(a, b)
    assert len(a.memories()) == 1 and len(b.memories()) == 1
    assert {m.worker for m in a.memories()} != {m.worker for m in b.memories()}


def test_a_per_worker_harvest_archives_the_old_shared_copy(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    legacy = a.save_memory(B_PYTEST, scope="project:x", provenance="tool", key="env.pytest")
    sync(a, b)
    harvest(a, A_PYTEST)
    assert [m.text for m in a.memories()] == [A_PYTEST]
    assert legacy.id in archived_ids(a)
    sync(a, b)
    # The retirement replicates: the shared copy is archived everywhere, and
    # b sees nothing until it harvests for itself.
    assert not b.memories()
    harvest(b, B_PYTEST)
    assert [m.text for m in b.memories()] == [B_PYTEST]


def test_another_workers_fact_is_never_a_rewrite_target(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    harvest(a, A_PYTEST)
    sync(a, b)
    foreign = next(e["id"] for e in raw(b) if e.get("key") == "env.pytest")
    with pytest.raises(ValueError, match="Unknown memory ID"):
        b.save_memory("pytest moved", memory_id=foreign, durable=True)
    # An agent save of the same text on b is b's own fact, not a's.
    mine = b.save_memory(A_PYTEST, scope="project:x", durable=True)
    assert mine.id != foreign and mine.worker is None


def test_confirming_a_harvest(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    harvest(a, A_PYTEST)
    (fact,) = a.memories()
    # An agent confirming it keeps it about this worker ...
    confirmed = a.save_memory(A_PYTEST, scope="project:x", durable=True)
    assert confirmed.id == fact.id and confirmed.durable and confirmed.worker == fact.worker
    # ... a user confirming it makes it shared, and harvests leave it alone.
    users = a.save_memory("pytest: use the project .venv", memory_id=fact.id, scope="project:x",
                          protected=True, expected_revision=confirmed.revision)
    assert users.worker is None
    sync(a, b)
    assert [m.text for m in b.memories()] == ["pytest: use the project .venv"]
    harvest(b, B_PYTEST)
    assert [m.text for m in b.memories()] == ["pytest: use the project .venv"]


def test_merge_rejects_an_invalid_worker(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    harvest(a, A_PYTEST)
    payload = a._facts.export()
    payload["entries"][0]["worker"] = "../x y"
    with pytest.raises(ValueError, match="Invalid memory worker"):
        b._facts.merge(payload)


# ─── the start-of-chat note counts what Recall finds ────────────────────────

def test_snapshot_budget_is_unchanged():
    from fluid.codemode import facts

    assert facts.MAX_PROMPT_CHARS == 8000 and facts.RESERVED_INFERRED_CHARS == 3000


def test_snapshot_note_counts_omitted_and_archived_facts_recall_finds(tmp_path, monkeypatch):
    from fluid.codemode import facts

    monkeypatch.setattr(facts, "MAX_PROMPT_CHARS", 300)
    monkeypatch.setattr(facts, "RESERVED_INFERRED_CHARS", 0)
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    for i in range(8):
        a.save_memory(f"Zephyr user rule number {i} for this project", scope="project:x", protected=True)
    old = a.save_memory("Zephyr agent note that expired", scope="project:x", durable=True)
    retired = a.save_memory("Zephyr learned note that curation retired", scope="project:x")
    a.retire_memory(retired.id, reason="test")
    a.save_memory("Zephyr note for another project", scope="project:y")
    # Another worker's environment facts, live and archived, are not counted.
    harvest(b, B_PYTEST)
    harvest(b, "Zephyr node_modules missing on b", key="env.node_modules")
    b.retire_memory(next(m.id for m in b.memories() if m.key == "env.node_modules"), reason="test")
    a._facts.merge(b._facts.export())
    fake_clock(monkeypatch, old.expires_at + 1)  # the agent note expires
    plan_live = a.memory_snapshot_plan("project:x")
    section = a.render_memory_section("project:x")
    omitted = plan_live["over_budget"]
    assert 0 < omitted < 8
    assert f"({omitted} saved facts omitted for space and 2 archived; Recall finds them by keyword.)" in section
    # Every fact the note counts is one Recall finds in this project.
    found = a.search_memories("Zephyr", limit=50, project="project:x")
    assert sum(1 for _, archived in found if archived) == 2
    assert sum(1 for _, archived in found if not archived) == 8
    assert B_PYTEST not in section and "node_modules" not in section


def test_snapshot_note_with_only_archived_facts(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fact = store.save_memory("The staging box is called Atlas.")
    fake_clock(monkeypatch, fact.expires_at + 1)
    section = store.render_memory_section("project:x")
    assert "(1 archived fact not shown; Recall finds it by keyword.)" in section
    assert "no saved facts yet" not in section

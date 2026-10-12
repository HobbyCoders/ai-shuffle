"""Memory health: every signal, every repair, user-fact protection, notify-once."""
import json
import time

import pytest

from fluid.codemode import facts, memory_health
from fluid.codemode.skills import SkillStore
from fluid.codemode.transactions import file_transaction

DAY = 86400.0


def edit(store, fn):
    """Change the stored document directly (ages, counters) for a scenario."""
    with file_transaction(store._facts.lock):
        doc = store._facts._load()
        fn(doc)
        store._facts._write(doc)


def age(store, ids, *, created=None, updated=None, **fields):
    now = time.time()

    def apply(doc):
        for e in doc["entries"] + doc["archived"]:
            if e["id"] in ids:
                if created is not None:
                    e["created_at"] = now - created
                if updated is not None:
                    e["updated_at"] = now - updated
                e.update(fields)
    edit(store, apply)


def signal(result, sid):
    return next(s for s in result["signals"] if s["id"] == sid)


def log_rows(store, *rows):
    with (store.root / ".synthesis-log.jsonl").open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def tiny_snapshot(monkeypatch, chars=120):
    monkeypatch.setattr(facts, "MAX_PROMPT_CHARS", chars)
    monkeypatch.setattr(facts, "RESERVED_INFERRED_CHARS", chars)


def test_empty_store_is_healthy_and_reports_every_signal(tmp_path):
    result = memory_health.check(SkillStore(tmp_path))
    assert result["state"] == "healthy"
    assert {s["id"] for s in result["signals"]} == set(memory_health.LABELS)
    assert all(s["reason"] for s in result["signals"])


# ─── learner ────────────────────────────────────────────────────────────────

def _active(store, entries=25):
    for i in range(entries):
        store.journal_append({"kind": "user", "text": f"Synthetic request {i}"})


def test_learner_off_is_not_a_problem(tmp_path):
    store = SkillStore(tmp_path)
    _active(store)
    s = signal(memory_health.check(store, learning_enabled=False), "learner")
    assert s["state"] == "healthy" and "off" in s["reason"]


def test_learner_that_never_ran_despite_sessions_needs_attention(tmp_path):
    store = SkillStore(tmp_path)
    _active(store)
    s = signal(memory_health.check(store), "learner")
    assert s["state"] == "attention" and "has not run" in s["reason"]


def test_learner_with_mostly_failed_passes_needs_attention(tmp_path):
    store = SkillStore(tmp_path)
    _active(store)
    now = time.time()
    log_rows(store, *[{"ts": now - 60, "kind": "synthesis", "error": "provider error"} for _ in range(3)],
             {"ts": now - 30, "kind": "synthesis", "consumed": 20, "changed": []},
             {"ts": now - 20, "kind": "synthesis", "error": "skipped: lock held", "ran": False})
    s = signal(memory_health.check(store), "learner")
    assert s["state"] == "attention" and "3 of 4" in s["reason"]


def test_learner_silent_for_two_weeks_of_sessions_needs_attention(tmp_path):
    store = SkillStore(tmp_path)
    _active(store, 6)
    now = time.time()
    log_rows(store, {"ts": now - 20 * DAY, "kind": "synthesis", "consumed": 20, "changed": []},
             {"ts": now - DAY, "kind": "synthesis", "consumed": 20, "changed": []})
    s = signal(memory_health.check(store), "learner")
    assert s["state"] == "attention" and "no fact in 14 days" in s["reason"]
    store.note_learner_pass(True, now=now - DAY)
    s = signal(memory_health.check(store), "learner")
    assert s["state"] == "healthy" and "saving facts" in s["reason"]


# ─── usage, unconfirmed, duplicates ─────────────────────────────────────────

def test_facts_never_recalled_or_shown_need_attention(tmp_path, monkeypatch):
    tiny_snapshot(monkeypatch)
    store = SkillStore(tmp_path)
    saved = [store.save_memory(f"Synthetic project fact number {i} about the build", durable=True)
             for i in range(24)]
    age(store, {m.id for m in saved}, created=10 * DAY)
    s = signal(memory_health.check(store), "usage")
    assert s["state"] == "attention" and "never recalled or shown" in s["reason"]
    store.note_memories_recalled([m.id for m in saved[:20]])
    assert signal(memory_health.check(store), "usage")["state"] == "healthy"


def test_harvested_fact_rewritten_over_and_over_needs_attention(tmp_path):
    store = SkillStore(tmp_path)
    fact = store.save_memory("pytest lives in /a.", provenance="tool", key="env.pytest")
    age(store, {fact.id}, created=14 * DAY, revision=12)
    s = signal(memory_health.check(store), "unconfirmed")
    assert s["state"] == "attention" and "1 harvested fact keeps getting rewritten" in s["reason"]
    assert s["detail"]["max_revision"] == 12


def test_harvested_fact_no_longer_rewritten_is_not_churn(tmp_path):
    # #1814: a legacy shared env fact at revision 74 whose rewrites stopped
    # kept the signal raised for months, until its age diluted the rate.
    store = SkillStore(tmp_path)
    fact = store.save_memory("Node dependencies are missing in a.", provenance="tool", key="env.node_modules")
    age(store, {fact.id}, created=5 * DAY, updated=memory_health.CHURN_QUIET + DAY, revision=74)
    s = signal(memory_health.check(store), "unconfirmed")
    assert s["state"] == "healthy" and s["detail"]["rewritten_often"] == 0
    assert s["detail"]["max_revision"] == 74


def test_many_unconfirmed_facts_are_reported_not_flagged(tmp_path):
    # #1814: with the churn fixed, ServerWorker's row still read "needs attention"
    # for 21 unconfirmed facts, mostly recent learner facts in use. The count
    # is the designed flow (confirm, or archived unused after a month), and
    # nothing the owner can do changes it.
    store = SkillStore(tmp_path)
    for i in range(21):
        store.save_memory(f"Harvested detail {i}", provenance="tool", key=f"env.item{i}")
    s = signal(memory_health.check(store), "unconfirmed")
    assert s["state"] == "healthy"
    assert s["reason"] == "21 unconfirmed facts; unused ones are archived after 30 days."
    assert s["detail"]["unconfirmed"] == 21
    assert memory_health.check(store)["state"] == "healthy"


def _replica_duplicates(tmp_path, text, *, protect_b=False, protect_a=False):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    a.save_memory(text, protected=protect_a)
    b.save_memory(text, protected=protect_b)
    a._facts.merge(b._facts.export())
    b._facts.merge(a._facts.export())
    return a, b


def test_exact_duplicates_from_two_workers_merge_to_the_same_copy(tmp_path):
    a, b = _replica_duplicates(tmp_path, "The staging box is called Atlas.")
    assert len(a.memories()) == 2 and len(b.memories()) == 2
    done_a = memory_health.repair(a)
    done_b = memory_health.repair(b)
    assert done_a[0]["kind"] == "duplicates" and done_a[0]["count"] == 1
    assert done_b[0]["count"] == 1
    a._facts.merge(b._facts.export())
    b._facts.merge(a._facts.export())
    live_a, live_b = a._facts.snapshot()["entries"], b._facts.snapshot()["entries"]
    assert len(live_a) == 1 and len(live_b) == 1
    assert facts.global_id(live_a[0]) == facts.global_id(live_b[0])
    assert len(a.archived_memories()) == 1  # restorable, not deleted


def test_duplicate_repair_never_retires_a_user_saved_copy(tmp_path):
    a, _ = _replica_duplicates(tmp_path, "The user's editor is Helix.", protect_b=True)
    memory_health.repair(a)
    live = a._facts.snapshot()["entries"]
    assert len(live) == 1 and live[0]["protected"]
    both, _ = _replica_duplicates(tmp_path / "two", "The user's shell is fish.", protect_a=True, protect_b=True)
    assert memory_health.repair(both) == []
    assert len(both.memories()) == 2


def test_near_duplicate_groups_are_reported_not_merged(tmp_path):
    store = SkillStore(tmp_path)
    for i in range(memory_health.NEAR_DUPLICATE_GROUPS):
        store.save_memory(f"Topic{i} deploys always run through staging pipeline first.")
        store.save_memory(f"Topic{i} deploys always run through staging pipeline first!!")
    assert memory_health.repair(store) == []
    s = signal(memory_health.check(store), "duplicates")
    assert s["state"] == "attention" and s["detail"]["near_groups"] == memory_health.NEAR_DUPLICATE_GROUPS
    assert len(store.memories()) == 2 * memory_health.NEAR_DUPLICATE_GROUPS


# ─── capacity, snapshot, curation ───────────────────────────────────────────

def test_user_facts_near_their_cap_need_attention(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "MAX_EXPLICIT", 10)
    store = SkillStore(tmp_path)
    for i in range(9):
        store.save_memory(f"User preference {i}", protected=True)
    s = signal(memory_health.check(store), "capacity")
    assert s["state"] == "attention" and "9 of 10" in s["reason"]
    assert "new ones are refused" in s["reason"]
    store.save_memory("User preference 9", protected=True)
    s = signal(memory_health.check(store), "capacity")
    assert s["state"] == "attention" and s["reason"].startswith("Memory is full")
    assert s["detail"]["user_saved"] == 10


def test_user_facts_well_below_their_cap_are_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "MAX_EXPLICIT", 10)
    store = SkillStore(tmp_path)
    for i in range(8):
        store.save_memory(f"User preference {i}", protected=True)
    assert signal(memory_health.check(store), "capacity")["state"] == "healthy"


def test_a_nearly_full_memory_file_needs_attention(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    store.save_memory("Some fact")
    size = (tmp_path / "memories.json").stat().st_size
    monkeypatch.setattr(facts, "MAX_DOCUMENT_BYTES", int(size * 1.1))
    s = signal(memory_health.check(store), "capacity")
    assert s["state"] == "attention" and "% full" in s["reason"]


def test_snapshot_that_leaves_out_most_facts_is_informational(tmp_path, monkeypatch):
    # The snapshot is bounded by design and Recall finds the rest (#1814):
    # reported with its numbers, not counted as needing attention.
    tiny_snapshot(monkeypatch, 400)
    store = SkillStore(tmp_path)
    for i in range(70):
        store.save_memory(f"Project fact {i}", scope="project:one", protected=True)
    result = memory_health.check(store)
    s = signal(result, "snapshot")
    assert s["state"] == "healthy" and "of 70 facts in one project" in s["reason"]
    assert "Recall finds the rest" in s["reason"]
    assert s["detail"]["scopes"][0]["omitted"] >= 50
    assert result["state"] == "healthy"


def test_snapshot_that_shows_nothing_needs_attention(tmp_path, monkeypatch):
    tiny_snapshot(monkeypatch, 10)  # no fact fits
    store = SkillStore(tmp_path)
    for i in range(3):
        store.save_memory(f"Project fact {i}", scope="project:one", protected=True)
    s = signal(memory_health.check(store), "snapshot")
    assert s["state"] == "attention" and "none of 3 facts" in s["reason"]


def test_repeated_curation_failures_need_attention(tmp_path):
    store = SkillStore(tmp_path)
    now = time.time()
    log_rows(store, {"ts": now - 50, "kind": "consolidation", "consumed": 10, "changed": []},
             *[{"ts": now - 40 + i, "kind": "consolidation", "error": "boom"} for i in range(3)])
    s = signal(memory_health.check(store), "curation")
    assert s["state"] == "attention" and "last 3 curation passes failed" in s["reason"]


# ─── protected facts and saves ──────────────────────────────────────────────

def test_user_fact_that_vanished_without_a_forget_needs_attention(tmp_path):
    store = SkillStore(tmp_path)
    kept = store.save_memory("The user's editor is Helix.", protected=True)
    gone = store.save_memory("The user's shell is fish.", protected=True)
    first = memory_health.check(store)
    assert signal(first, "protected")["state"] == "healthy"
    edit(store, lambda doc: doc.update(entries=[e for e in doc["entries"] if e["id"] != gone.id]))
    second = memory_health.check(store, previous=first)
    s = signal(second, "protected")
    assert s["state"] == "attention" and "disappeared without a Forget" in s["reason"]
    # An explicit Forget is the user's choice, not a loss.
    clean = memory_health.check(store, previous={"protected_ids": second["protected_ids"]})
    assert store.delete_memory(kept.id, explicit=True)
    after = memory_health.check(store, previous=clean)
    assert signal(after, "protected")["state"] == "healthy"


def test_user_fact_dropped_for_space_by_an_older_version_needs_attention(tmp_path):
    store = SkillStore(tmp_path)
    store.save_memory("User preference 0", protected=True)
    # Versions before the refusal rule dropped archived user facts and
    # counted it; a recent drop is still reported after the upgrade.
    edit(store, lambda doc: doc.update(losses={"explicit_dropped": 1,
                                               "last_explicit_dropped_at": time.time()}))
    s = signal(memory_health.check(store), "protected")
    assert s["state"] == "attention" and "dropped for space" in s["reason"]


def test_failed_saves_this_week_need_attention(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    with pytest.raises(ValueError):
        store.save_memory("x" * 900)  # a rejection, not a failure
    assert signal(memory_health.check(store), "saves")["state"] == "healthy"
    monkeypatch.setattr(facts, "MAX_DOCUMENT_BYTES", 10)
    with pytest.raises(ValueError):
        store.save_memory("A fact that no longer fits")
    s = signal(memory_health.check(store), "saves")
    assert s["state"] == "attention" and "1 save failed" in s["reason"]


# ─── repairs ────────────────────────────────────────────────────────────────

def test_unused_unconfirmed_harvest_is_archived_after_a_month(tmp_path, monkeypatch):
    tiny_snapshot(monkeypatch, 10)  # nothing fits, so nothing is "shown"
    store = SkillStore(tmp_path)
    stale = store.save_memory("Old harvested detail", provenance="tool", key="env.old")
    young = store.save_memory("New harvested detail", provenance="tool", key="env.new")
    used = store.save_memory("Recalled harvested detail", provenance="tool", key="env.used")
    agent = store.save_memory("Agent saved detail", durable=True)
    store.note_memories_recalled([used.id])
    age(store, {stale.id, used.id, agent.id}, created=40 * DAY, updated=20 * DAY)
    age(store, {young.id}, created=10 * DAY, updated=10 * DAY)
    done = memory_health.repair(store)
    assert done == [{"kind": "unconfirmed", "count": 1, "at": done[0]["at"], "ids": [stale.id]}]
    assert {m.id for m in store.memories()} == {young.id, used.id, agent.id}
    assert [e.id for e, archived in store.search_memories("Old harvested") if archived] == [stale.id]


def test_a_harvest_still_shown_in_the_snapshot_is_kept(tmp_path):
    store = SkillStore(tmp_path)
    shown = store.save_memory("pytest lives in /a.", provenance="tool", key="env.pytest")
    age(store, {shown.id}, created=40 * DAY, updated=20 * DAY)
    assert memory_health.repair(store) == []


# ─── the scheduled run ─────────────────────────────────────────────────────

def test_run_notifies_once_per_stretch_of_attention(tmp_path):
    store = SkillStore(tmp_path)
    _active(store)  # the learner has not run: attention
    now = time.time()
    first = memory_health.run(store, now=now)
    assert first["state"] == "attention" and first["notify"]
    memory_health.mark_notified(store, now=now)
    again = memory_health.run(store, now=now + 3600)
    assert again["state"] == "attention" and not again["notify"]
    # Healthy clears the stretch; a new stretch the next day notifies again.
    healthy = memory_health.run(store, now=now + 7200, learning_enabled=False)
    assert healthy["state"] == "healthy" and not healthy["notify"]
    assert memory_health.load_state(store)["attention_since"] is None
    later = memory_health.run(store, now=now + 2 * DAY)
    assert later["notify"]


def test_run_flaps_do_not_notify_twice_in_a_day(tmp_path):
    store = SkillStore(tmp_path)
    _active(store)
    now = time.time()
    memory_health.run(store, now=now)
    memory_health.mark_notified(store, now=now)
    memory_health.run(store, now=now + 60, learning_enabled=False)
    assert not memory_health.run(store, now=now + 120)["notify"]


def test_run_records_repairs_in_plain_words(tmp_path):
    a, _ = _replica_duplicates(tmp_path, "The staging box is called Atlas.")
    state = memory_health.run(a)
    view = memory_health.summary(state)
    assert view["repairs"][0]["summary"] == "Archived 1 exact duplicate."
    assert "ids" not in json.dumps(memory_health.load_state(a)["repairs"])
    assert view["signals"][0]["state"] in ("healthy", "attention")

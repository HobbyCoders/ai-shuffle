"""Candidate completeness, rank-only wording, and exact-pair Forget isolation."""
import json
from pathlib import Path
import pytest
from fluid.codemode import preferences as pref
from fluid.codemode.facts import barrier_add, barrier_contains, fingerprint, global_id
from fluid.codemode.skills import SkillStore

MATRIX = json.loads((Path(__file__).parents[1] / "fixtures/memory-routing-wording.json").read_text())
BASE = {"frontend": "UI work: Opus.", "code": "Code work goes to Opus.", "review": "Reviews go to Opus."}


@pytest.mark.parametrize("case", MATRIX, ids=lambda c: c["text"])
def test_wording_matrix_has_no_candidate_classifier(tmp_path, case):
    store = SkillStore(tmp_path)
    store.save_memory(BASE[case["job"]], protected=True)
    store.save_memory(case["text"], protected=True)
    questions = pref.protected_conflicts(store._facts.snapshot()["entries"])
    assert bool(questions) is case["candidate"]
    assert pref.routing_priority(case["text"]) == case["rank"]


@pytest.mark.parametrize("case", [c for c in MATRIX if c["rank"] == 0 and c["candidate"]], ids=lambda c: c["text"])
def test_newer_anecdote_cannot_displace_clear_assignment(tmp_path, case):
    store = SkillStore(tmp_path)
    base = store.save_memory(BASE[case["job"]], protected=True)
    job = {"frontend": "UI", "code": "Code", "review": "Review"}[case["job"]]
    current = store.save_memory(f"{job} work moved to Sol.", protected=True)
    anecdote = store.save_memory(case["text"], protected=True)
    questions = pref.protected_conflicts(store._facts.snapshot()["entries"])
    expected_pairs = 3 if "opus" in case["accepted_models"] else 2
    assert len(questions) == expected_pairs, "shared endpoints must not suppress candidates"
    assert {questions[0][0]["id"], questions[0][1]["id"]} == {base.id, current.id}
    assert any(anecdote.id in (a["id"], b["id"]) for a, b, _ in questions)


def test_misranking_costs_a_question_not_an_excluded_contest(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    base = store.save_memory("UI work: Opus.", protected=True)
    real = store.save_memory("UI work moved to Sol.", protected=True)
    store.save_memory("Sol was slow on UI work.", protected=True)
    monkeypatch.setattr(pref, "routing_priority", lambda text: 0)
    pref._cached_conflict_refs.cache_clear()
    questions = pref.protected_conflicts(store._facts.snapshot()["entries"])
    assert len(questions) == 2
    assert any({a["id"], b["id"]} == {base.id, real.id} for a, b, _ in questions)


def pairs(store):
    return [pref.coexistence_key(*(vars(store.save_memory(f"{job} work goes to {model}.", protected=True))
                                  for model in ("Opus", "Sol"))) for job in ("Code", "Review")]


async def answer(store, key, **kwargs):
    from fluid.tools.builtins.remember import handler
    from fluid.tools.registry import ToolCtx
    return await handler({"text": "Both stand.", "key": key, "user_requested": True,
                          "when": "When applying these two owner rules", **kwargs},
                         ToolCtx(extra={"memory_store": store}))


@pytest.mark.asyncio
async def test_forget_answer_is_id_and_key_only_and_replicates(tmp_path):
    store = SkillStore(tmp_path / "source")
    first, second = pairs(store)
    assert not (await answer(store, first)).is_error
    deleted = next(e for e in store.memories() if e.key == first)
    old_packet = store._facts.export()
    assert store.delete_memory(deleted.id, explicit=True, expected_revision=deleted.revision)
    doc = store._facts.snapshot()
    tombstone = next(t for t in doc["tombstones"] if t["id"] == deleted.id)
    assert tombstone["fingerprints"] == []
    assert not barrier_contains(doc["forgotten"], "text:" + fingerprint("Both stand."))
    assert barrier_contains(doc["forgotten"], "id:" + global_id(tombstone))
    assert barrier_contains(doc["forgotten"], "key:global:" + first)
    assert not (await answer(store, second)).is_error
    assert (await answer(store, first)).is_error, "the SAME pair still needs explicit restore"
    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(store._facts.export())
    replica._facts.merge(old_packet)
    assert all(e.key != first for e in replica.memories())
    assert not (await answer(replica, second)).is_error
    ordinary = store.save_memory("Both stand.", protected=True, key="ordinary.rule")
    assert ordinary.key == "ordinary.rule", "new answer tombstones must not poison ordinary prose"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bloom", "tombstone"])
async def test_legacy_answer_text_barrier_cannot_block_another_pair(tmp_path, kind):
    store = SkillStore(tmp_path)
    first, second = pairs(store)
    assert not (await answer(store, first)).is_error
    deleted = next(e for e in store.memories() if e.key == first)
    store.delete_memory(deleted.id, explicit=True)
    doc = store._facts._load()
    fp = fingerprint("Both stand.")
    if kind == "bloom":
        doc["forgotten"] = barrier_add(doc["forgotten"], "text:" + fp)
    else:
        doc["tombstones"][-1]["fingerprints"] = [fp]
    store._facts._write(doc)
    assert not (await answer(store, second)).is_error
    assert (await answer(store, first)).is_error
    assert not (await answer(store, first, restore=True)).is_error


def test_ordinary_fact_text_deletion_barriers_still_apply(tmp_path):
    store = SkillStore(tmp_path)
    saved = store.save_memory("Ordinary owner rule.", protected=True)
    store.delete_memory(saved.id, explicit=True)
    with pytest.raises(ValueError, match="deletion barrier|forgotten"):
        store.save_memory("Ordinary owner rule.", protected=True, key="different.ordinary.rule")

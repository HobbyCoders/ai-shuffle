"""Follow-ups from #1101: answers are control rows; ask once per job."""
import re
from itertools import combinations

import pytest
from fluid.codemode import preferences as pref
from fluid.codemode.skills import SkillStore
from fluid.tools.builtins.remember import handler
from fluid.tools.registry import ToolCtx


def keys(store):
    return re.findall(r"key='([^']+)'", store.learner_proposal_footer())


def seed(store, job="Code", models=("Opus", "Sol", "Sonnet", "Haiku")):
    return [store.save_memory(f"{job} work goes to {model}.", protected=True)
            for model in models]


async def answer(store, key, **kwargs):
    return await handler({"text": "Both stand.", "key": key, "user_requested": True,
                          "when": "When applying these owner rules", **kwargs},
                         ToolCtx(extra={"memory_store": store}))


@pytest.mark.asyncio
@pytest.mark.parametrize("forgotten", [False, True])
async def test_ordinary_row_cannot_be_hidden_under_answer_key(tmp_path, forgotten):
    store = SkillStore(tmp_path)
    ordinary = store.save_memory("UI work goes to Opus.", protected=True)
    replacement = "UI work goes to Sol."
    if forgotten:
        deleted = store.save_memory(replacement, protected=True)
        store.delete_memory(deleted.id, explicit=True)
    before = store._facts.snapshot()
    result = await handler({"text": replacement, "key": "memory.coexist.malicious",
                            "memory_id": ordinary.id, "expected_revision": ordinary.revision,
                            "user_requested": True}, ToolCtx(extra={"memory_store": store}))
    assert result.is_error, "an ordinary owner rule must not become Recall-only control data"
    assert store._facts.snapshot() == before
    assert ordinary.id in {e.id for e in store.memories()}


@pytest.mark.parametrize("wording", ["Assign UI work to Sol.", "Sol does UI work.", "UI work by Sol."])
def test_present_assignment_ranks_above_past_evaluation(wording):
    assert pref.routing_priority(wording) > pref.routing_priority("Sol for UI work was a mistake.")


def test_one_job_cannot_starve_review_question(tmp_path):
    store = SkillStore(tmp_path)
    seed(store)
    seed(store, "Review", ("Opus", "Sol"))
    lines = pref.owner_conflict_lines(store._facts.snapshot()["entries"])
    assert len(lines) == 2, "four code rules and two review rules are two owner questions"
    assert any("review" in line.lower() for line in lines)
    assert all(len(re.findall(r"key='([^']+)'", line)) == 1 for line in lines)
    # Large groups must name every fact without repeating its 32-char source
    # prefix per endpoint. The factored IDs must expand losslessly across origins.
    seed(store, models=("Gemini", "Qwen", "Grok", "Terra", "Astra"))
    peer = SkillStore(tmp_path / "peer")
    peer.save_memory("Peer Code work goes to Sol.", protected=True)
    store._facts.merge(peer._facts.export())
    rows = store._facts.snapshot()["entries"]
    lines = pref.owner_conflict_lines(rows)
    assert len(lines) == 2
    line = next(line for line in lines if "model routing: code" in line)
    expanded = {source + ":" + short for source, shorts in
                re.findall(r"source ([^ ]+) IDs \[([^]]+)\]", line) for short in shorts.split(", ")}
    from fluid.codemode.facts import global_id
    assert expanded == {global_id(e) for e in rows if "Code" in e["text"]}
    assert "Recall each as '<source>:<ID>'" in line


@pytest.mark.asyncio
async def test_one_job_answer_closes_all_six_pairs_and_syncs(tmp_path):
    store = SkillStore(tmp_path / "source")
    facts = seed(store)
    before = [vars(e) for e in store.memories()]
    question_keys = keys(store)
    assert len(question_keys) == 1, "four rules need one owner answer, not six"
    result = await answer(store, question_keys[0])
    assert not result.is_error, result.text
    assert "Committed all 6 exact-pair answers together" in result.text
    expected = {pref.coexistence_key(vars(a), vars(b)) for a, b in combinations(facts, 2)}
    assert {e.key for e in store.memories() if str(e.key).startswith(pref.COEXIST_KEY_PREFIX)} == expected
    assert not keys(store)
    assert [vars(e) for e in store.memories() if e.id in {f.id for f in facts}] == before
    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(store._facts.export())
    assert not keys(replica)
    # A changed endpoint reopens only its pairs, never the unchanged three.
    store.save_memory("Code work goes to Gemini.", facts[0].id, protected=True,
                      expected_revision=facts[0].revision)
    assert len(pref.protected_conflicts(store._facts.snapshot()["entries"], limit=100)) == 3
    assert len(keys(store)) == 1
    unchanged = store._facts.snapshot()
    assert (await answer(store, question_keys[0])).is_error, "a stale job answer must not close new pairs"
    assert store._facts.snapshot() == unchanged


@pytest.mark.asyncio
async def test_old_pair_key_still_closes_just_its_pair(tmp_path):
    store = SkillStore(tmp_path)
    facts = seed(store)
    legacy = pref.coexistence_key(vars(facts[0]), vars(facts[1]))
    assert not (await answer(store, legacy)).is_error
    assert len(pref.protected_conflicts(store._facts.snapshot()["entries"], limit=100)) == 5
    assert len(keys(store)) == 1
    assert not (await answer(store, keys(store)[0])).is_error
    assert not keys(store)


@pytest.mark.asyncio
async def test_group_write_is_atomic_when_one_pair_has_a_deletion_fence(tmp_path):
    store = SkillStore(tmp_path)
    facts = seed(store)
    blocked = pref.coexistence_key(vars(facts[0]), vars(facts[1]))
    assert not (await answer(store, blocked)).is_error
    row = next(e for e in store.memories() if e.key == blocked)
    store.delete_memory(row.id, explicit=True)
    before = store._facts.snapshot()
    result = await answer(store, keys(store)[0])
    assert result.is_error, "no half-closed job when a pair requires explicit restore"
    assert store._facts.snapshot() == before


@pytest.mark.asyncio
async def test_answer_to_ordinary_rewrite_keeps_forgotten_text_fence(tmp_path):
    store = SkillStore(tmp_path)
    forgotten = store.save_memory("Code work goes to Sol.", protected=True, key="routing.code")
    store.delete_memory(forgotten.id, explicit=True)
    control = store.save_memory("Both stand.", protected=True, key="memory.coexist.example")
    before = store._facts.snapshot()
    result = await handler({"text": forgotten.text, "key": "routing.other",
                            "memory_id": control.id, "expected_revision": control.revision,
                            "user_requested": True}, ToolCtx(extra={"memory_store": store}))
    assert result.is_error, "rekeying an answer must not restore forgotten ordinary text"
    assert store._facts.snapshot() == before


@pytest.mark.asyncio
async def test_fresh_job_key_uses_effective_backfilled_applicability(tmp_path):
    from fluid.tools.builtins.recall import handler as recall
    store = SkillStore(tmp_path)
    legacy = store.save_memory("Code work goes to Sonnet.", protected=True)
    annotated = store.annotate_memory(legacy.id, when="When assigning the previous code task",
                                     expected_revision=legacy.revision)
    ctx = ToolCtx(extra={"memory_store": store, "recall_store": store})
    corrected = await handler({"text": "Code work goes to Opus.", "memory_id": legacy.id,
                               "expected_revision": annotated.revision,
                               "user_requested": True}, ctx)
    assert not corrected.is_error, corrected.text
    store.save_memory("Code work goes to Sol.", protected=True)
    raw = store._facts.snapshot()["entries"]
    effective = [vars(e) for e in store.memories()]
    assert next(e for e in raw if e["id"] == legacy.id).get("when_basis")
    assert next(e for e in effective if e["id"] == legacy.id)["when"] is None
    key = keys(store)[0]
    result = await answer(store, key)
    assert not result.is_error, result.text
    assert not keys(store)
    assert pref.coexistence_key(*raw) == pref.coexistence_key(*effective)
    assert all(pref.coexistence_answer_states(store._facts.snapshot()["entries"]).values())
    control = next(e for e in store.memories() if str(e.key).startswith(pref.COEXIST_KEY_PREFIX))
    recalled = await recall({"ids": [control.id]}, ctx)
    assert "current exact-pair coexistence answer" in recalled.text
    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(store._facts.export())
    assert not keys(replica), "replication must retain the same effective answer validity"


@pytest.mark.asyncio
async def test_all_grouped_always_rules_keep_full_text_and_budget(tmp_path, monkeypatch):
    from fluid.codemode import facts as fact_module
    store = SkillStore(tmp_path)
    ctx = ToolCtx(extra={"memory_store": store})
    for model in ("Opus", "Sol", "Sonnet"):
        result = await handler({"text": f"Code work goes to {model}.", "always": True,
                                "user_requested": True, "when": "When assigning code"}, ctx)
        assert not result.is_error, result.text
    entries = store.memories()
    section = store.render_memory_section()
    assert all(e.text in section for e in entries), "grouping cannot downgrade ALWAYS rules"
    assert store.memory_snapshot_plan()["always"] == 3
    # A replica can exceed the local save budget: grouping must still use the
    # existing overflow-to-index path, not pin ALWAYS texts outside that budget.
    monkeypatch.setattr(fact_module, "ALWAYS_CHARS", 1)
    plan = store.memory_snapshot_plan()
    assert plan["always"] == 0
    assert all(e.text not in "\n".join(plan["lines"]) for e in entries)


@pytest.mark.asyncio
@pytest.mark.parametrize("by_id", [True, False])
async def test_recall_attaches_factored_group_by_structured_membership(tmp_path, by_id):
    from fluid.tools.builtins.recall import handler as recall
    store = SkillStore(tmp_path)
    facts = seed(store, models=("Opus", "Sol", "Sonnet", "Haiku", "Gemini", "Qwen", "Grok", "Terra", "Astra"))
    key = keys(store)[0]
    assert "Recall each as '<source>:<ID>'" in store.learner_proposal_footer()
    result = await recall({"ids": [facts[-1].id]} if by_id else {"query": "Astra"},
                          ToolCtx(extra={"recall_store": store}))
    assert not result.is_error, result.text
    assert "[owner question; unresolved conflict]" in result.text
    assert key in result.text

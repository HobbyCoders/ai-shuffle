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

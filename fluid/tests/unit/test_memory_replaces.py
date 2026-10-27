"""One current memory per topic: explicit replaces, key-slot collisions and
write-time overlap hints. There is no special pref.* style: an owner rule is
an ordinary memory with a when-line (see test_memory_when_index.py)."""
from __future__ import annotations

import re

import pytest

from fluid.codemode import facts
from fluid.codemode.facts import global_id
from fluid.codemode.skills import SkillStore
from fluid.tools.builtins import remember as remember_mod
from fluid.tools.builtins.recall import handler as recall
from fluid.tools.builtins.remember import handler as remember
from fluid.tools.registry import ToolCtx

from tests.fixtures.memory_routing import PREFERENCE, ROUTING, UNRELATED, UNRELATED_RULE

ROUTING_WORDS = re.compile(r"\b(?:opus|sol|astra|sonnet|fable)\b", re.I)
WHEN = "When choosing a model for a subagent"


def ctx(store, project=None):
    extra = {"memory_store": store, "recall_store": store}
    if project:
        extra["memory_project"] = project
    return ToolCtx(extra=extra)


def seed(store):
    """The routing rows and unrelated facts, saved the way they were in the live store."""
    ids = []
    for text, key, protected in ROUTING:
        tier = {"protected": True} if protected else {"durable": True}
        ids.append(store.save_memory(text, key=key, **tier).id)
    for text in UNRELATED:
        store.save_memory(text, protected=True)
    return ids


def live_routing(store):
    return [e for e in store.memories() if ROUTING_WORDS.search(e.text) and not e.superseded_by]


def entry(store, mid):
    return next(e for e in store.memories() + store.archived_memories() if e.id == mid)


async def test_update_path_leaves_exactly_one_live_routing_preference(tmp_path):
    store = SkillStore(tmp_path)
    ids = seed(store)
    assert len(live_routing(store)) == len(ROUTING)

    # A first save of the folded rule: nothing changes, but the receipt points
    # at the rows on the same subject.
    first = await remember({"text": PREFERENCE, "key": "subagent_models", "when": WHEN,
                            "user_requested": True}, ctx(store))
    assert not first.is_error
    assert "Similar saved facts: [" in first.text and "replaces=[" in first.text
    hinted = re.findall(r"\[(m\d+) r\d+\]", first.text.split("Similar saved facts:", 1)[1])
    assert 1 <= len(hinted) <= 3 and set(hinted) <= set(ids)
    assert len(live_routing(store)) == len(ROUTING) + 1  # a hint is not a change

    # The retry the hint asks for folds every older row in.
    done = await remember({"text": PREFERENCE, "key": "subagent_models", "when": WHEN,
                           "user_requested": True, "replaces": ids}, ctx(store))
    assert not done.is_error, done.text
    assert "Replaced (archived" in done.text
    assert all(f"[{mid}]" in done.text.split("Replaced", 1)[1] for mid in ids)
    [pref] = live_routing(store)
    assert pref.key == "subagent_models" and pref.protected

    # Archived, not deleted: text, revision history and the link stay.
    for mid, (text, _, protected) in zip(ids, ROUTING):
        old = entry(store, mid)
        assert old.text == text and old.protected == protected
        assert old.superseded_by == global_id(vars(pref)) and old.supersession_confirmed
        assert mid in {e.id for e in store.archived_memories()}
    doc = store._facts.snapshot()
    assert not doc["tombstones"] and not doc.get("forgotten")

    # Recall still finds them, labelled with the local successor ID.
    found = await recall({"query": "Astra subagent", "limit": 10}, ctx(store))
    assert f"superseded by [{pref.id}]; not current" in found.text

    # The snapshot lists one routing memory, by its when-line.
    section = store.render_memory_section()
    assert section.count(WHEN) == 1 and f"[{pref.id} r1; session] {WHEN}" in section
    assert not any(ROUTING_WORDS.search(line) for line in section.splitlines())
    assert "conflict" not in section


async def test_new_save_on_an_existing_pref_key_names_the_slot_for_one_retry(tmp_path):
    store = SkillStore(tmp_path)
    first = store.save_memory(PREFERENCE, key="subagent_models", protected=True)
    out = await remember({"text": PREFERENCE + " Haiku for eligibility checks.", "when": WHEN,
                          "key": "subagent_models", "user_requested": True}, ctx(store))
    assert out.is_error
    assert (f"subagent_models exists as [{first.id} r1]; pass memory_id={first.id} "
            "expected_revision=1 to update it") in out.text
    retry = await remember({"text": PREFERENCE + " Haiku for eligibility checks.",
                            "key": "subagent_models", "user_requested": True,
                            "memory_id": first.id, "expected_revision": 1}, ctx(store))
    assert not retry.is_error and f"Saved [{first.id}] revision 2" in retry.text
    assert [e.id for e in store.memories() if e.key == "subagent_models"] == [first.id]


async def test_key_correction_targets_the_current_slot_not_archived_predecessors(tmp_path):
    store = SkillStore(tmp_path)
    old = store.save_memory("Deploy rule: ask before every deploy.", key="deploy",
                            protected=True)
    new = store.save_memory("Deploy rule: deploy from main without asking.",
                            key="deploy_rules", protected=True, replaces=[old.id])
    # Renaming the successor's slot leaves the archived predecessor under the
    # same key: the key-addressed correction must pick the current value.
    new = store.save_memory(new.text + " Verify live.", key="deploy", protected=True,
                            memory_id=new.id, expected_revision=new.revision)
    assert new.key == "deploy"
    out = await remember({"text": "Deploy rule: deploy from main without asking; verify live.",
                          "key": "deploy", "expected_revision": new.revision,
                          "user_requested": True}, ctx(store))
    assert not out.is_error, out.text
    assert f"Saved [{new.id}]" in out.text
    assert entry(store, old.id).text == "Deploy rule: ask before every deploy."


async def test_agent_save_cannot_replace_an_owner_saved_fact(tmp_path):
    store = SkillStore(tmp_path)
    owner = store.save_memory(ROUTING[0][0], protected=True)
    agent = store.save_memory(ROUTING[5][0], durable=True)
    before = store._facts.snapshot()
    out = await remember({"text": PREFERENCE, "replaces": [owner.id, agent.id]}, ctx(store))
    assert out.is_error
    assert f"[{owner.id}] was saved at the user's request" in out.text
    assert "user_requested=true" in out.text and "Nothing was saved" in out.text
    after = store._facts.snapshot()
    assert after["entries"] == before["entries"] and after["next_id"] == before["next_id"]

    # Agent-saved targets are the agent's to replace.
    ok = await remember({"text": PREFERENCE, "when": WHEN, "replaces": [agent.id]}, ctx(store))
    assert not ok.is_error and f"[{agent.id}]" in ok.text.split("Replaced", 1)[1]
    assert entry(store, agent.id).superseded_by
    assert not entry(store, owner.id).superseded_by


async def test_replaces_refuses_unknown_ids_and_incompatible_scopes(tmp_path):
    store = SkillStore(tmp_path)
    shared = store.save_memory(ROUTING[0][0], protected=True)
    local = store.save_memory(ROUTING[1][0], protected=True, scope="project:one")
    out = await remember({"text": PREFERENCE, "user_requested": True,
                          "replaces": ["m999"]}, ctx(store))
    assert out.is_error and "unknown memory ID [m999]" in out.text
    # A project exception cannot supersede a global rule ...
    out = await remember({"text": PREFERENCE, "user_requested": True, "scope": "project",
                          "replaces": [shared.id]}, ctx(store, "project:one"))
    assert out.is_error and "scoped to global" in out.text
    assert not entry(store, shared.id).superseded_by
    # ... while a global rule can replace a project one.
    out = await remember({"text": PREFERENCE, "user_requested": True, "when": WHEN,
                          "replaces": [local.id]}, ctx(store, "project:one"))
    assert not out.is_error and entry(store, local.id).superseded_by
    out = await remember({"text": "x" * 10, "replaces": "m1"}, ctx(store))
    assert out.is_error


def test_replacement_replicates_and_forget_restores_the_predecessors(tmp_path):
    a = SkillStore(tmp_path / "a")
    b = SkillStore(tmp_path / "b")
    ids = seed(a)
    b._facts.merge(a._facts.export())
    # A replica-origin fact is addressed by the ID that store shows for it.
    b_rule = b.save_memory("Owner rule: route doc-only subagent work to GPT 6.1 Sol.",
                           protected=True)
    a._facts.merge(b._facts.export())
    foreign = next(e.id for e in a.memories() if e.text == b_rule.text)
    assert ":" in foreign
    pref = a.save_memory(PREFERENCE, key="subagent_models", protected=True,
                         replaces=ids + [foreign])
    b._facts.merge(a._facts.export())
    for store in (a, b):
        assert [e.text for e in live_routing(store)] == [PREFERENCE]
        archived = {e.text: e for e in store.archived_memories()}
        for text, _, _ in ROUTING:
            assert archived[text].superseded_by == global_id(vars(pref))
            assert archived[text].supersession_confirmed
        assert archived[b_rule.text].superseded_by == global_id(vars(pref))
    # Merging back is a no-op, not a conflict.
    a._facts.merge(b._facts.export())
    assert not any(e.conflicted for e in a.memories() + a.archived_memories())
    assert [e.text for e in live_routing(a)] == [PREFERENCE]

    # Forget of the replacement is the only deletion; the old rows come back.
    assert a.delete_memory(pref.id, explicit=True)
    assert len(live_routing(a)) == len(ROUTING) + 1


def test_receipt_lists_only_links_that_survived(tmp_path):
    store = SkillStore(tmp_path)
    old = store.save_memory(ROUTING[0][0], protected=True)
    receipt: dict = {}
    pref = store.save_memory(PREFERENCE, protected=True, key="subagent_models",
                             replaces=[old.id], receipt=receipt)
    assert receipt["replaced"] == [old.id]
    again: dict = {}
    store.save_memory(PREFERENCE + " Sonnet for docs.", protected=True, memory_id=pref.id,
                      expected_revision=pref.revision, replaces=[old.id], receipt=again)
    assert again["replaced"] == [] and again["skipped"] == [f"[{old.id}] was already replaced"]


def test_learner_saves_cannot_replace(tmp_path):
    store = SkillStore(tmp_path)
    old = store.save_memory(ROUTING[5][0])
    with pytest.raises(ValueError, match="Only a Remember save"):
        store.save_memory(PREFERENCE, replaces=[old.id])


async def test_overlap_hint_fires_on_routing_rows_and_not_on_unrelated_facts(tmp_path):
    store = SkillStore(tmp_path)
    seed(store)
    unrelated = await remember({"text": UNRELATED_RULE, "key": "ci_policy", "when": "When running CI",
                                "user_requested": True}, ctx(store))
    assert not unrelated.is_error and "Similar saved facts" not in unrelated.text
    # Agent saves on routing get the hint too.
    agent = await remember({"text": "Subagent models: bounded backend code and review on "
                                    "GPT 6.1 Sol, risky or cross-cutting code and UI on Opus.",
                            "when": WHEN}, ctx(store))
    assert "Similar saved facts: [" in agent.text
    # Each fixture routing row, saved anew, points at at least one other.
    for text, _, _ in ROUTING[:5]:
        receipt: dict = {}
        store.save_memory(text + " (restated)", protected=True, receipt=receipt)
        assert receipt["similar"], text
    # Unrelated facts restated point at nothing on routing.
    for text in UNRELATED:
        receipt = {}
        store.save_memory(text + " (restated)", protected=True, receipt=receipt)
        assert not any(ROUTING_WORDS.search(s["text"]) for s in receipt["similar"]), text


async def test_corrections_get_no_overlap_hint(tmp_path):
    store = SkillStore(tmp_path)
    seed(store)
    pref = store.save_memory(PREFERENCE, key="subagent_models", protected=True)
    out = await remember({"text": PREFERENCE + " Haiku for eligibility checks.",
                          "memory_id": pref.id, "expected_revision": pref.revision,
                          "user_requested": True}, ctx(store))
    assert not out.is_error and "Similar saved facts" not in out.text


def test_hint_text_is_clipped_and_bounded():
    receipt = {"replaced": [], "skipped": [],
               "similar": [{"id": f"m{i}", "revision": 2, "text": "y" * 300} for i in range(3)]}
    text = remember_mod._receipt_notes(receipt)
    assert text.count("[m") == 3 and "y" * 81 not in text and "…" in text
    assert 'replaces=["m0", "m1", "m2"]' in text


def _pile(store, count, *, start=0):
    for i in range(count):
        store.save_memory(f"Owner decision {start + i}: " + " ".join(
            f"topic{start + i}word{j}" for j in range(30)), protected=True)


def test_pref_keys_get_no_special_slot_or_heading(tmp_path):
    # The pref.* "Standing preferences" group is gone: one style of memory.
    assert not hasattr(facts, "STANDING_PREFERENCE_CHARS")
    assert not hasattr(facts, "is_standing_preference")
    store = SkillStore(tmp_path)
    pref = store.save_memory(PREFERENCE, key="pref.subagent_models", protected=True, when=WHEN)
    _pile(store, 100)
    section = store.render_memory_section()
    assert "Standing preferences" not in section and "Other saved facts" not in section
    assert PREFERENCE not in section  # an index line, not the full text
    plan = store.memory_snapshot_plan()
    line = next(line for line in plan["eligible_lines"] if line.startswith(f"- [{pref.id} "))
    assert line == f"- [{pref.id} r1; session] {WHEN}"


def test_prompt_guidance_has_no_pref_convention():
    from fluid.cli.prompt import PromptProfile, compose_base_prompt

    text = compose_base_prompt(PromptProfile(continual=True, model="claude"))
    assert "pref.<topic>" not in text and "replaces=[" in text
    description = remember_mod.describe()
    assert "pref.<topic>" not in description and "replaces=[" in description
    assert "When choosing a model for a subagent" in description
    props = remember_mod.SPEC.input_schema["properties"]
    assert {"replaces", "when", "always"} <= set(props)
    assert "pref.<topic>" not in props["key"]["description"]

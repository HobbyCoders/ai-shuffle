"""Memory v3 phase 3a: the deterministic "possible conflict" note.

The model conflict review is gone. Two owner rules that share a trigger and
disagree on a negation get a short note in the save receipt and in memory
health; nothing calls a model and nothing is held back. Fixtures are invented.
"""
import pytest
from fluid.codemode import memory_health
from fluid.codemode.skills import SkillStore
from fluid.codemode.triggers import possible_conflicts
from fluid.tools.builtins.remember import handler as remember
from fluid.tools.registry import ToolCtx

DEPLOY_WHEN = "When deploying the website"


def rule(fact_id, text, when=DEPLOY_WHEN, scope="global", **fields):
    return {"id": fact_id, "text": text, "when": when, "scope": scope, "protected": True, **fields}


def test_a_shared_trigger_and_one_negation_is_a_possible_conflict():
    rules = [rule("a", "Deploy the website from main."),
             rule("b", "Do not deploy the website on Fridays."),
             rule("c", "Never use tabs in Python files.", when="When writing Python code")]
    assert possible_conflicts(rules) == [("a", "b")]
    assert possible_conflicts(rules, about="c") == []
    assert possible_conflicts(rules, about="b") == [("a", "b")]
    # Both affirm: two rules for one situation are not a contradiction.
    assert possible_conflicts([rules[0], rule("d", "Deploy the website after review.")]) == []


@pytest.mark.parametrize("other", [
    rule("b", "Never deploy the website from a laptop."),              # both negate
    rule("b", "Do not deploy the website on Fridays.", protected=False),
    rule("b", "Do not deploy the website on Fridays.", scope="project:other"),
    rule("b", "Do not deploy the website on Fridays.", conflicted=True),
    rule("b", "Do not deploy the website on Fridays.", retired_at=1.0),
    rule("b", "Do not deploy the website on Fridays.", key="memory.coexist.v2.0123"),
])
def test_only_live_owner_rules_in_overlapping_scopes_are_paired(other):
    first = rule("a", "Never deploy the website without a review.", scope="project:mine")
    assert possible_conflicts([first, other]) == []


def test_a_project_rule_and_a_global_rule_can_conflict():
    rules = [rule("a", "Deploy the website from main.", scope="project:mine"),
             rule("b", "Do not deploy the website on Fridays.")]
    assert possible_conflicts(rules) == [("a", "b")]


async def test_the_owner_save_receipt_names_the_rule_it_may_contradict(tmp_path):
    store = SkillStore(tmp_path)
    first = store.save_memory("Deploy the website from main.", protected=True, when=DEPLOY_WHEN)
    ctx = ToolCtx(extra={"memory_store": store})
    out = await remember({"text": "Do not deploy the website on Fridays.", "when": DEPLOY_WHEN,
                          "user_requested": True}, ctx)
    assert not out.is_error
    assert f"Possible conflict: [{first.id} r{first.revision}] {DEPLOY_WHEN}" in out.text
    assert "ask the user which stands" in out.text
    # Both rules stay live and visible: the note is a hint, never a hold.
    assert len(store.memories()) == 2 and not any(m.conflicted for m in store.memories())
    section = store.render_memory_section()
    assert f"[{first.id} r" in section and "conflicts with" not in section


async def test_an_agent_save_gets_no_conflict_note(tmp_path):
    store = SkillStore(tmp_path)
    store.save_memory("Deploy the website from main.", protected=True, when=DEPLOY_WHEN)
    out = await remember({"text": "Do not deploy the website on Fridays.", "when": DEPLOY_WHEN},
                         ToolCtx(extra={"memory_store": store}))
    assert not out.is_error and "Possible conflict" not in out.text


def test_memory_health_lists_possible_conflicts_as_a_note_never_attention(tmp_path):
    store = SkillStore(tmp_path)
    healthy = next(s for s in memory_health.check(store)["signals"] if s["id"] == "conflicts")
    assert healthy["state"] == "healthy" and not healthy["note"] and healthy["detail"]["conflicts"] == 0
    a = store.save_memory("Deploy the website from main.", protected=True, when=DEPLOY_WHEN)
    b = store.save_memory("Do not deploy the website on Fridays.", protected=True, when=DEPLOY_WHEN)
    signal = next(s for s in memory_health.check(store)["signals"] if s["id"] == "conflicts")
    assert signal["state"] == "healthy" and signal["note"] and not signal["alert"]
    assert signal["label"] == "Possible conflicts"
    assert signal["detail"] == {"conflicts": 1, "pairs": [[a.id, b.id]]}
    assert "1 pair of your saved rules may contradict" in signal["reason"]

"""Bound both task recall and the accumulated real memory change-list chain."""
import pytest
from app.core.memory_reminder import (
    HeldMemory, apply_changes, memory_blocks, plan_memory_note, section_items,
    task_memory_section, wrap, TASK_TOTAL_CHARS, TASK_TURN_CHARS,
)
from fluid.codemode.skills import SkillStore


def hold(previous, plan):
    items = list(previous.items) if previous else []
    chars = previous.changes_chars if previous else 0
    for kind, body in memory_blocks(plan.note or ""):
        if kind == "full":
            items, chars = list(section_items(body)), 0
        else:
            items = apply_changes(items, body)
            chars += len(wrap(body))
    return HeldMemory(frozenset(items), chars, "host-pinned-node")


def setup_store(root):
    store = SkillStore(root)
    for i in range(40):
        store.save_memory(f"topic{i // 4} habit{i // 4}: detail{i}. " + "A bounded preference. " * 13,
                          protected=True)
    for i in range(30):
        store.save_memory(f"profile filler {i}: " + "Explicit standing convention. " * 12,
                          protected=True, key=f"profile.filler{i}")
    return SkillStore(root)


def test_varied_topics_do_not_resend_base_or_remove_prior_topic(tmp_path):
    store = setup_store(tmp_path)
    base = store.render_memory_section()
    eligible = store.memory_snapshot_plan()["eligible_lines"]
    held = None
    delivered = 0
    for i in range(30):
        query = f"topic{i % 10} habit{i % 10}"
        section = task_memory_section(base, held, eligible, query)
        assert section.startswith(base)
        extra = [x for x in section_items(section) if "; task-match] " in x]
        assert sum(len(x) + 1 for x in extra) <= TASK_TOTAL_CHARS
        new = set(extra) - (held.items if held else set())
        assert sum(len(x) + 1 for x in new) <= TASK_TURN_CHARS
        plan = plan_memory_note(section, held)
        if i:
            assert plan.kind != "full"
            assert "No longer in the snapshot" not in (plan.note or "")
        delivered += len(plan.note or "")
        held = hold(held, plan)
        assert store.render_memory_section() == base
    assert delivered <= len(wrap(base)) + 4000
    assert any("task-match" in x for x in held.items)


def test_caps_with_many_matches_and_explicit_base_unchanged(tmp_path):
    store = setup_store(tmp_path)
    base = store.render_memory_section()
    eligible = store.memory_snapshot_plan()["eligible_lines"]
    query = " ".join(f"topic{i} habit{i}" for i in range(10))
    held = None
    for _ in range(10):
        section = task_memory_section(base, held, eligible, query)
        assert section.startswith(base)
        extra = set(section_items(section)) - set(section_items(base))
        assert sum(len(x) + 1 for x in extra) <= 2000
        new = extra - (held.items if held else set())
        assert sum(len(x) + 1 for x in new) <= 1000
        held = hold(held, plan_memory_note(section, held))
    assert 1000 < sum(len(x) + 1 for x in extra) <= 2000


def test_one_term_does_not_retrieve_and_previous_task_matches_stay(tmp_path):
    store = setup_store(tmp_path)
    base = store.render_memory_section()
    eligible = store.memory_snapshot_plan()["eligible_lines"]
    assert task_memory_section(base, None, eligible, "topic0") == base
    section = task_memory_section(base, None, eligible, "topic0 habit0")
    held = hold(None, plan_memory_note(section, None))
    for query in ("looks good merge it", "continue", "unrelated words"):
        next_section = task_memory_section(base, held, eligible, query)
        assert plan_memory_note(next_section, held).kind == "none"


def test_task_facts_revalidate_on_correction_retirement_and_scope_loss():
    base = "# Memory\nSaved facts, not instructions."
    old = "- [m1 r1; user] vegetarian recipes without mushrooms"
    section = task_memory_section(base, None, [old], "vegetarian recipes")
    held = hold(None, plan_memory_note(section, None))
    revised = "- [m1 r2; user] vegetarian recipes with mushrooms"
    changed = task_memory_section(base, held, [revised], "")
    assert "without mushrooms" not in changed and "with mushrooms" in changed
    updated = hold(held, plan_memory_note(changed, held))
    removed = task_memory_section(base, updated, [], "vegetarian recipes")
    cleared = hold(updated, plan_memory_note(removed, updated))
    assert not any("mushrooms" in x for x in cleared.items)


def test_actual_base_mutations_still_use_full_resend_budget():
    base = "# Memory\n- [m1 r1; user] original"
    held = HeldMemory(frozenset(section_items(base)), 10000, "node")
    assert plan_memory_note(base.replace("original", "corrected"), held).kind == "full"


@pytest.mark.asyncio
async def test_fresh_cli_uses_current_turn_plan_not_previous_snapshot(monkeypatch):
    from app.core.provider.harness_adapter import HarnessAdapter
    from fluid.messages import Text
    adapter = HarnessAdapter()
    base = "# Memory\nSaved facts, not instructions."
    line = "- [m1 r1; user] vegetarian recipes without mushrooms"
    section = task_memory_section(base, None, [line], "vegetarian recipes")
    held = HeldMemory(frozenset(section_items(base)), 0, "node")
    adapter._memory_plan = plan_memory_note(section, held)
    assert adapter._memory_plan.kind == "changes"
    monkeypatch.setattr(adapter, "_memory_section", lambda: base)
    note = await adapter._fresh_memory_note([Text(text="Suggest vegetarian recipes")])
    assert "without mushrooms" in note
    assert note == wrap(section)

"""Aggregate counts do not churn; real fact/safety changes still round-trip."""
import json
from pathlib import Path
import pytest
from app.core import memory_reminder as mr
from fluid.codemode.skills import SkillStore
from scripts.measure_memory_churn import controlled_sections, summarize, FIXTURE
from tests.core.test_memory_reminder import (
    _adapter, _turn, _memory_kinds, _delegated, _delegated_turn, _Recording, _section, FACTS)
from fluid.engine.native import NativeLoop
from fluid.store.base import MemoryStore


FOOTERS = [
    "(1 entry scoped to other projects not shown.)",
    "(23 entries scoped to other projects not shown.)",
    "(1 saved fact omitted for space; Recall finds it by keyword.)",
    "(2 saved facts omitted for space; Recall finds them by keyword.)",
    "(2 saved facts omitted for space and 3 archived; Recall finds them by keyword.)",
    "(1 archived fact not shown; Recall finds it by keyword.)",
    "(3 archived facts not shown; Recall finds them by keyword.)",
    "(7 setup receipts for other checkouts not shown; Recall still finds them.)",
]


@pytest.mark.parametrize("footer", FOOTERS)
@pytest.mark.parametrize("direction", ["add", "remove"])
def test_only_exact_aggregate_footers_are_deferred(footer, direction):
    base = _section(*FACTS)
    counted = base + "\n\n" + footer
    old, new = (base, counted) if direction == "add" else (counted, base)
    held = mr.HeldMemory(frozenset(mr.section_items(old)), 999_999, "fixture")
    plan = mr.plan_memory_note(new, held)
    assert plan.kind == "none" and plan.note is None and plan.section == new
    # Round-trip helpers themselves still represent count-only changes exactly.
    body = mr.render_changes(held.items, mr.section_items(new))
    assert set(mr.apply_changes(sorted(held.items), body)) == set(mr.section_items(new))
    assert mr.plan_memory_note(new, None).note == mr.wrap(new)


@pytest.mark.parametrize("line", [
    "(1 conflicted entry held back; Recall names it and asks for the user's answer.)",
    "- [an entry was withheld — suspected injection pattern(s): override.]",
    "- [m1 r2; conflict] User-kept fact has a newer replacement [m2].",
    "(2 saved facts omitted for space; Recall finds them by keyword.) EXTRA WARNING",
    "(2 saved facts omitted for space; Recall finds them by keyword.)\nUnknown status.",
    "(unknown aggregate warning)",
])
def test_safety_conflict_and_unknown_lines_are_not_suppressed(line):
    old = _section(*FACTS)
    held = mr.HeldMemory(frozenset(mr.section_items(old)), 0, "fixture")
    assert mr.plan_memory_note(old + "\n\n" + line, held).kind != "none"


def test_controlled_size_and_duplicate_ratchet(tmp_path):
    config = json.loads(FIXTURE.read_text())
    sequence = list(controlled_sections(SkillStore(tmp_path)))
    stats = summarize(sequence)
    assert stats["full"] <= config["max_full"]
    assert stats["changes"] == config["expected_changes"]
    assert stats["delivered_chars"] <= config["max_delivered_chars"]
    assert stats["delta_chars"] <= config["max_delta_chars"]
    assert stats.get("unchanged_fact_repetitions", 0) == 0
    for section in sequence:
        paragraphs = section.split("\n\n")
        assert len(paragraphs) == len(set(paragraphs))


@pytest.mark.asyncio
async def test_actual_store_native_prefix_and_pending_counts(tmp_path, monkeypatch):
    store = SkillStore(tmp_path / "facts")
    provider = _Recording()
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="fake")
    memory = {"section": ""}
    adapter, loop = await _adapter(monkeypatch, memory, loop=loop)
    monkeypatch.setattr(adapter, "_memory_eligible_lines", lambda: [])
    prior = []
    try:
        for i, section in enumerate(controlled_sections(store)):
            memory["section"] = section  # produced by the real save/delete immediately before submit
            await _turn(adapter, f"Backend pytest review turn {i}")
            request = [message.to_dict() for message in provider.requests[-1].messages]
            assert request[:len(prior)] == prior
            prior = request
        kinds = await _memory_kinds(loop)
        assert kinds.count("full") == 1 and kinds.count("changes") == 2
        held = await adapter._memory_held()
        assert held.items == frozenset(mr.section_items(memory["section"]))
        assert "24 entries scoped" in "\n".join(held.items)
        # Count-only updates stay deferred after real deltas too.
        store.save_memory("Another unrelated project convention.", scope="project:foreign", protected=True)
        memory["section"] = store.render_memory_section("project:fixture")
        await _turn(adapter)
        assert (await _memory_kinds(loop))[-1] is None
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_actual_store_delegated_restore_gets_latest_counts(tmp_path, monkeypatch):
    store = SkillStore(tmp_path / "facts")
    memory = {"section": ""}
    adapter = await _delegated(monkeypatch, tmp_path, memory)
    monkeypatch.setattr(adapter, "_memory_eligible_lines", lambda: [])
    try:
        for i, section in enumerate(controlled_sections(store)):
            memory["section"] = section
            await _delegated_turn(adapter, f"Backend pytest review turn {i}")
        kinds = await _memory_kinds(adapter._engine)
        assert kinds.count("full") == 1 and kinds.count("changes") == 2
        store.save_memory("Another unrelated project convention.", scope="project:foreign", protected=True)
        memory["section"] = store.render_memory_section("project:fixture")
        await _delegated_turn(adapter, "go [compact-now]")
        await _delegated_turn(adapter)
        kinds = await _memory_kinds(adapter._engine)
        assert kinds[-2:] == [None, "full"]
        held = await adapter._memory_held()
        assert held.items == frozenset(mr.section_items(memory["section"]))
        assert "25 entries scoped" in "\n".join(held.items)
    finally:
        await adapter.disconnect()


def test_task_addition_and_pending_counts_are_not_lost():
    old = _section(*FACTS)
    held = mr.HeldMemory(frozenset(mr.section_items(old)), 0, "fixture")
    counted = old + "\n\n" + FOOTERS[1]
    assert mr.plan_memory_note(counted, held).kind == "none"
    new = counted + "\n\n- [m99 r1; user; task-match] Backend pytest preference."
    plan = mr.plan_memory_note(new, held)
    assert plan.kind == "changes"
    _, body = mr.memory_blocks(plan.note)[0]
    assert "m99" in body
    assert set(mr.apply_changes(sorted(held.items), body)) == set(mr.section_items(new))
    assert not any(line in body for line in FACTS)


def test_omitted_off_task_same_project_fact_does_not_trigger_delta(tmp_path):
    store = SkillStore(tmp_path)
    fixture = Path(__file__).resolve().parents[2] / "fluid/tests/fixtures/memory_budget.json"
    for raw in json.loads(fixture.read_text()):
        row = dict(raw)
        row.pop("label", None)
        store.save_memory(**row)
    query = "memory backend delegated approval"
    old = store.render_memory_section("project:fixture", task_query=query)
    held = mr.HeldMemory(frozenset(mr.section_items(old)), 0, "fixture")
    entry = store.save_memory("Career biography and employment background. " * 10,
                              key="profile.career_new", scope="project:fixture", protected=True)
    new = store.render_memory_section("project:fixture", task_query=query)
    assert entry.id not in store.memory_snapshot_plan("project:fixture", task_query=query)["shown_ids"]
    assert mr.plan_memory_note(new, held).kind == "none"


@pytest.mark.parametrize("action", ["expire", "retire"])
def test_actual_store_removal_and_archive_count_still_deliver(tmp_path, monkeypatch, action):
    import fluid.codemode.facts as facts
    clock = [1_790_000_000.0]
    monkeypatch.setattr(facts.time, "time", lambda: clock[0])
    store = SkillStore(tmp_path)
    list(controlled_sections(store))
    entry = store.save_memory("Backend pytest temporary environment observation.", scope="project:fixture")
    old = store.render_memory_section("project:fixture")
    assert f"[{entry.id} " in old
    held = mr.HeldMemory(frozenset(mr.section_items(old)), 0, "fixture")
    if action == "expire":
        clock[0] = entry.expires_at + 1
    else:
        assert store.retire_memory(entry.id, reason="obsolete environment", expected_revision=entry.revision)
    new = store.render_memory_section("project:fixture")
    assert f"[{entry.id} " not in new
    plan = mr.plan_memory_note(new, held)
    assert plan.kind == "changes"
    _, body = mr.memory_blocks(plan.note)[0]
    assert mr._REMOVED_IDS + entry.id in body
    assert set(mr.apply_changes(sorted(held.items), body)) == set(mr.section_items(new))


def test_historical_replay_does_not_trust_unmarked_quotes(tmp_path):
    from scripts.measure_memory_churn import replay_export
    body = mr.CHANGES_HEADER + mr.CHANGES_INTRO + "\n\n" + mr._ADDED + "\n" + FOOTERS[1]
    text = mr.wrap(body)
    folder = tmp_path / "session"
    folder.mkdir()
    node = {"message": {"role": "user", "content": [{"kind": "text", "text": text}]}}
    (folder / "nodes.jsonl").write_text(json.dumps(node) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"dir": str(folder), "session_id": "fixture"}]))
    assert replay_export(manifest)["counts"].get("host_changes", 0) == 0
    node["message"]["context_pin"] = [0, len(text)]
    (folder / "nodes.jsonl").write_text(json.dumps(node) + "\n")
    result = replay_export(manifest)["counts"]
    assert result["host_changes"] == result["after_changes"] == 1
    assert result["unknown_previous_snapshot"] == 1  # earns no speculative savings

"""First-snapshot quality, bounded identity, safety and prefix-size ratchets."""
import json
from pathlib import Path
from fluid.codemode.skills import SkillStore
from fluid.codemode.facts import MAX_PROMPT_CHARS
from fluid.codemode.skills import MAX_MEMORIES


def seeded(tmp_path):
    store = SkillStore(tmp_path)
    labels = {}
    for raw in json.loads((Path(__file__).parents[1] / "fixtures/memory_budget.json").read_text()):
        row = dict(raw)
        label = row.pop("label", None)
        entry = store.save_memory(**row)
        if label:
            labels.setdefault(label, set()).add(entry.id)
    return store, labels


def test_coding_snapshot_prefers_correction_not_profile_flood(tmp_path):
    store, labels = seeded(tmp_path)
    before = {(e.id, e.text) for e in store.memories()}
    plan = store.memory_snapshot_plan("project:fixture", task_query="memory backend delegated approval")
    shown = set(plan["shown_ids"])
    assert labels["relevant"] <= shown
    assert labels["identity"] <= shown
    # Off-task biography is counted, not listed, in a task's session.
    assert not labels["off_task"] & shown
    assert set(plan["omitted_ids"]) >= labels["off_task"]
    assert before == {(e.id, e.text) for e in store.memories()}
    assert sum(len(line) + 1 for line in plan["lines"]) <= MAX_PROMPT_CHARS


def test_fixture_size_and_duplicate_paragraph_allowance(tmp_path):
    store, _ = seeded(tmp_path)
    section = store.render_memory_section("project:fixture", task_query="memory backend delegated approval")
    assert len(section) <= 8250  # measured 8122 + 128 allowance, including header/footer
    paragraphs = section.split("\n\n")
    assert len(paragraphs) == len(set(paragraphs))
    texts = [line.partition("] ")[2] for line in section.splitlines() if line.startswith("- [")]
    assert len(texts) == len(set(texts))




def test_profile_key_cannot_demote_a_matching_coding_preference(tmp_path, monkeypatch):
    import fluid.codemode.facts as facts
    monkeypatch.setattr(facts, "MAX_PROMPT_CHARS", 220)
    store = SkillStore(tmp_path)
    coding = store.save_memory("Prefer pytest fixtures for backend tests.", key="profile.coding_style", protected=True)
    career = store.save_memory("Career biography: looking for a new job. " * 3, key="profile.career", protected=True)
    for i in range(6):
        store.save_memory(f"Recent unrelated observation number {i}: keep this ordinary general fact.", protected=True)
    plan = store.memory_snapshot_plan(task_query="backend pytest fixtures")
    assert coding.id in plan["shown_ids"]
    assert career.id not in plan["shown_ids"]


def test_business_key_can_match_task_through_text(tmp_path, monkeypatch):
    import fluid.codemode.facts as facts
    monkeypatch.setattr(facts, "MAX_PROMPT_CHARS", 180)
    store = SkillStore(tmp_path)
    relevant = store.save_memory("Invoices require billing currency validation.", key="business.policy", protected=True)
    for i in range(6):
        store.save_memory(f"Unrelated recent fact number {i} for the general conversation.", protected=True)
    assert relevant.id in store.memory_snapshot_plan(task_query="billing currency validation")["shown_ids"]


def test_identity_render_is_bounded_without_editing_stored_fact(tmp_path):
    store = SkillStore(tmp_path)
    name = store.save_memory("Alex Example. " + "Career biography details and background. " * 10,
                             key="profile.name", protected=True)
    original = next(e for e in store.memories() if e.id == name.id)
    plan = store.memory_snapshot_plan(task_query="backend pytest fixtures")
    rendered = next(line.partition("] ")[2] for line in plan["lines"] if line.startswith(f"- [{name.id} "))
    assert len(rendered) <= 120
    assert rendered.endswith("…")
    stored = next(e for e in store.memories() if e.id == name.id)
    assert (stored.text, stored.revision) == (original.text, original.revision)


def test_career_facts_remain_available_for_a_career_task(tmp_path):
    store, labels = seeded(tmp_path)
    plan = store.memory_snapshot_plan("project:fixture", task_query="career biography")
    assert labels["off_task"] <= set(plan["shown_ids"])
    assert labels["identity"] <= set(plan["shown_ids"])


def test_recalled_global_correction_is_not_lost_to_absolute_project_tier(tmp_path):
    store, _ = seeded(tmp_path)
    fact = store.save_memory("Authorized delegated memory backend work uses the confirmed global routing preference.",
                             key="workflow.delegation", protected=True)
    for _ in range(6):
        store.note_memories_recalled([fact.id])
    plan = store.memory_snapshot_plan("project:fixture", task_query="memory backend delegated approval")
    assert fact.id in plan["shown_ids"]


def test_relevant_inference_flood_cannot_take_explicit_capacity(tmp_path):
    store, labels = seeded(tmp_path)
    for i in range(40):
        store.save_memory(f"Inference {i}: memory backend delegated approval. " + "A routine coding observation. " * 8,
                          scope="project:fixture")
    plan = store.memory_snapshot_plan("project:fixture", task_query="memory backend delegated approval")
    protected = {e.id for e in store.memories() if e.protected}
    # The index has no reserved slices: the inferred tier's own cap
    # (MAX_MEMORIES live facts) and short index lines bound a flood.
    inferred = [line for mid, line in zip(plan["shown_ids"], plan["lines"]) if mid not in protected]
    assert len(inferred) <= MAX_MEMORIES and all(len(line) <= 160 for line in inferred)
    assert labels["relevant"] <= set(plan["shown_ids"])
    foreign = store.save_memory("Memory backend delegated approval belongs to another project.",
                                scope="project:foreign", protected=True)
    assert foreign.id not in store.memory_snapshot_plan("project:fixture", task_query="memory backend delegated approval")["shown_ids"]

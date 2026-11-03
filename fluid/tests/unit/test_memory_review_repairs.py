"""Review migrations: direct owner authority survives chains and replica races."""
import copy
import json
from pathlib import Path
import pytest

from fluid.codemode.facts import fingerprint, global_id
from fluid.codemode.skills import SkillStore


def test_base_written_owner_chain_never_revives_explicit_predecessor(tmp_path):
    # Written by 603260c3883f0feee9c5eaedefea1639f26d380f using mig_base.py:
    # explicit A→B followed by the legacy learner's false B→C proposal.
    fixture = Path(__file__).parents[1] / "fixtures" / "memory-base-owner-chain.json"
    (tmp_path / "memories.json").write_bytes(fixture.read_bytes())
    store = SkillStore(tmp_path)
    raw = json.loads(fixture.read_text())
    predecessor = raw["archived"][0]
    assert predecessor["retired_reason"] == "superseded"
    assert predecessor["supersession_confirmed"] is False
    for _ in range(3):
        doc = store._facts.snapshot()
        old = next(e for e in doc["archived"] if e["id"] == "m1")
        assert old["superseded_by"] == predecessor["superseded_by"]
        assert old["supersession_confirmed"] is True
        assert old["text"] not in store.render_memory_section()
        assert all(e["id"] != "m1" for e in doc["entries"])
    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(store._facts.export())
    assert all(e.text != predecessor["text"] for e in replica.memories())


def test_confirmed_direct_edge_is_not_downgraded_by_descendant(tmp_path):
    store = SkillStore(tmp_path)
    a = store.save_memory("Deployment bundle uses alpha host in staging.", protected=True)
    b = store.save_memory("Deployment bundle uses beta host in staging.", protected=True, replaces=[a.id])
    c = store.save_memory("Deployment bundle uses gamma host in staging.", protected=True)
    doc = store._facts._load()
    next(e for e in doc["entries"] if e["id"] == b.id).update(
        superseded_by=global_id(vars(c)), supersession_confirmed=False)
    next(e for e in doc["entries"] if e["id"] == c.id)["text"] += " Replacing beta host."
    store._facts._write(doc)
    for _ in range(3):
        store.render_memory_section()
        old = next(e for e in store.archived_memories() if e.id == a.id)
        assert old.supersession_confirmed
        assert old.superseded_by == global_id(vars(b))
        assert a.text not in store.render_memory_section()


def test_learner_proposal_cannot_retire_a_protected_fact(tmp_path):
    store = SkillStore(tmp_path)
    a = store.save_memory("Deployment bundle uses alpha host in staging.", protected=True)
    b = store.save_memory("Deployment bundle uses beta host in staging.", protected=True)
    doc = store._facts._load()
    next(e for e in doc["entries"] if e["id"] == b.id)["text"] += " Replacing alpha host."
    store._facts._write(doc)
    assert store.propose_memory_supersession(a.id, b.id, old_revision=a.revision,
                                           new_revision=b.revision, protected_conflict=True)
    doc = store._facts.snapshot()
    old = next(e for e in doc["entries"] if e["id"] == a.id)
    assert not old.get("retired_at") and not old.get("retired_reason")
    assert not any(e["id"] == a.id for e in doc["archived"])


def test_concurrent_owner_confirmation_beats_metadata_withdrawal_both_orders(tmp_path):
    source = SkillStore(tmp_path / "source")
    old = source.save_memory("Owner project rule: multi-worker controls include Claude Code parity.", protected=True)
    new = source.save_memory("Owner project rule: sprint frontend reviews use Opus.", protected=True)
    packet = source._facts.export()
    proposal = copy.deepcopy(packet)
    next(e for e in proposal["entries"] if e["id"] == old.id).update(
        superseded_by=global_id(vars(new)), supersession_confirmed=False,
        clock={packet["store_id"]: 2}, revision=2)
    repaired = SkillStore(tmp_path / "repaired")
    repaired._facts.merge(proposal)
    withdrawal = repaired._facts.export()
    confirmed = copy.deepcopy(proposal)
    next(e for e in confirmed["entries"] if e["id"] == old.id).update(
        supersession_confirmed=True, clock={packet["store_id"]: 2, "owner": 1},
        revision=3, retired_at=old.updated_at, retired_reason="superseded")
    for i, packets in enumerate(((withdrawal, confirmed), (confirmed, withdrawal))):
        dest = SkillStore(tmp_path / f"dest{i}")
        for incoming in packets:
            dest._facts.merge(incoming)
        row = next(e for e in dest.memories() + dest.archived_memories() if e.text == old.text)
        assert row.supersession_confirmed and row.superseded_by == global_id(vars(new))
        assert not row.conflicted
        assert old.text not in dest.render_memory_section()


def test_missing_successor_still_answers_a_saved_preference(tmp_path):
    from fluid.codemode.preferences import PreferenceProposals
    store = SkillStore(tmp_path)
    old = store.save_memory("Owner rule: backend code uses Sol.", protected=True)
    doc = store._facts._load()
    doc["entries"][0].update(superseded_by="f" * 32 + ":m99", supersession_confirmed=False)
    store._facts._write(doc)
    saved = PreferenceProposals(store)._saved_preferences(None)
    assert any(row[3] == fingerprint(old.text) for row in saved)


def test_same_key_contradiction_is_a_non_link_owner_question(tmp_path):
    from fluid.codemode.preferences import open_conflicts, curate_protected_conflicts
    store = SkillStore(tmp_path)
    a = store.save_memory("Deployment waits for owner approval.", protected=True)
    b = store.save_memory("Deployment proceeds without owner approval.", protected=True)
    doc = store._facts._load()
    for row in doc["entries"]:
        row["key"] = "deploy.approval"
    store._facts._write(doc)
    assert len(curate_protected_conflicts(store)) == 1
    assert curate_protected_conflicts(store) == []
    assert open_conflicts(store._facts.snapshot()["entries"]) == 1
    assert all(not e.superseded_by for e in store.memories())
    section = store.render_memory_section()
    assert a.text in section and b.text in section
    assert "Recall both and ask the owner" in section


@pytest.mark.parametrize("note", [
    "Never use Fable for code.",
    "The owner tried Sol on a review last week and it missed a race.",
])
def test_negations_and_anecdotes_do_not_open_owner_questions(tmp_path, note):
    from fluid.codemode.preferences import model_routing, protected_conflicts
    store = SkillStore(tmp_path)
    wide = store.save_memory("Risky code and all UI work: Opus; reviews go to Opus.", protected=True)
    store.save_memory(note, protected=True)
    assert model_routing(note) == {}
    assert protected_conflicts(store._facts.snapshot()["entries"]) == []
    assert "owner question" not in store.render_memory_section()
    assert wide.text in store.render_memory_section()


def test_sprint_routing_asks_without_linking_or_hiding_wider_rule(tmp_path):
    from fluid.codemode.preferences import protected_conflicts
    store = SkillStore(tmp_path)
    wide = store.save_memory("Risky code and all UI work: Opus; bounded backend on GPT 6.1 Sol.",
                             protected=True)
    sprint = store.save_memory("Speed sprint: code tasks in the perf branch run on Sol.", protected=True)
    assert len(protected_conflicts(store._facts.snapshot()["entries"])) == 1
    section = store.render_memory_section()
    assert wide.text in section and sprint.text in section
    assert "and model-routing authority" in section
    assert "do not silently choose a model for the contested job" in section
    assert all(not e.superseded_by for e in store.memories())


@pytest.mark.parametrize("scope", ["global", "project:fixture"])
async def test_owner_keep_both_answer_uses_existing_remember_and_syncs(tmp_path, scope):
    from fluid.codemode import memory_health
    from fluid.codemode.preferences import coexistence_key, protected_conflicts, curate_protected_conflicts
    from fluid.tools.builtins.remember import handler as remember
    from fluid.tools.builtins.recall import handler as recall
    from fluid.tools.registry import ToolCtx
    store = SkillStore(tmp_path / "source")
    a = store.save_memory("Code work goes to Opus.", protected=True, scope=scope)
    b = store.save_memory("Code work goes to Sol.", protected=True, scope=scope)
    key = coexistence_key(vars(a), vars(b))
    assert key in store.render_memory_section(scope)
    curate_protected_conflicts(store)  # curation log is NOT the answer
    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(store._facts.export())
    assert "owner question" in replica.render_memory_section(scope)
    answer = await remember({
        "text": "Both rules stand; ask me which applies to the specific job.",
        "key": key, "scope": "global" if scope == "global" else "project",
        "when": "When applying these two owner rules", "user_requested": True,
    }, ToolCtx(extra={"memory_store": store, "memory_project": scope}))
    assert not answer.is_error, answer.text
    replica._facts.merge(store._facts.export())
    for worker in (store, replica, SkillStore(tmp_path / "source")):
        assert protected_conflicts(worker._facts.snapshot()["entries"]) == []
        assert "owner question" not in worker.render_memory_section(scope)
        assert len(worker.memories()) == 3
        assert all(not e.superseded_by for e in worker.memories())
        result = await recall({"query": "code", "limit": 10}, ToolCtx(extra={
            "recall_store": worker, "memory_project": scope}))
        assert "owner question" not in result.text
        signal = next(s for s in memory_health.check(worker)["signals"] if s["id"] == "proposals")
        assert signal["detail"]["conflicts"] == 0


@pytest.mark.parametrize("changed", ["old", "new", "when", "always"])
def test_coexistence_answer_reopens_only_after_applicability_or_text_changes(tmp_path, changed):
    from fluid.codemode.preferences import coexistence_key, protected_conflicts
    store = SkillStore(tmp_path)
    a = store.save_memory("Code work goes to Opus.", protected=True)
    b = store.save_memory("Code work goes to Sol.", protected=True)
    key = coexistence_key(vars(a), vars(b))
    store.save_memory("Both rules stand.", key=key, protected=True)
    assert not protected_conflicts(store._facts.snapshot()["entries"])
    target = a if changed == "old" else b
    kwargs = ({"when": "When implementing risky code"} if changed == "when" else
              {"always": True} if changed == "always" else {})
    store.save_memory(target.text if kwargs else target.text + " This is the current rule.",
                      memory_id=target.id, expected_revision=target.revision, protected=True, **kwargs)
    assert len(protected_conflicts(store._facts.snapshot()["entries"])) == 1
    assert key not in store.render_memory_section(), "a changed pair must request a NEW decision"


def test_an_agent_cannot_dismiss_an_owner_question_with_its_own_fact(tmp_path):
    from fluid.codemode.preferences import coexistence_key, protected_conflicts
    store = SkillStore(tmp_path)
    a = store.save_memory("Code work goes to Opus.", protected=True)
    b = store.save_memory("Code work goes to Sol.", protected=True)
    store.save_memory("Both stand, I guess.", key=coexistence_key(vars(a), vars(b)), durable=True)
    assert len(protected_conflicts(store._facts.snapshot()["entries"])) == 1


def test_question_detection_once_per_render_and_cached_by_document_revision(tmp_path, monkeypatch):
    from fluid.codemode import preferences as pref
    store = SkillStore(tmp_path)
    store.save_memory("Code work goes to Opus.", protected=True)
    store.save_memory("Code work goes to Sol.", protected=True)
    pref._cached_conflict_refs.cache_clear()
    original = pref._find_protected_conflicts
    scans = []
    def scan(*args, **kwargs):
        scans.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(pref, "_find_protected_conflicts", scan)
    original_public = pref.protected_conflicts
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original_public(*args, **kwargs)
    monkeypatch.setattr(pref, "protected_conflicts", counted)
    store.render_memory_section()
    assert len(scans) == len(calls) == 1
    store.render_memory_section()
    assert len(scans) == 1 and len(calls) == 2
    # A restarted store shares the revision cache. Raw text is in the key too:
    # an imported metadata edit cannot return stale labels even without a bump.
    restarted = SkillStore(tmp_path)
    restarted.render_memory_section()
    assert len(scans) == 1
    doc = restarted._facts._load()
    doc["entries"][1]["text"] = "Never use Sol for code."
    restarted._facts._write(doc)
    assert "owner question" not in restarted.render_memory_section()
    assert len(scans) == 2


def test_owner_question_size_and_duplicate_paragraph_guard(tmp_path):
    from fluid.codemode.preferences import owner_conflict_lines
    fixture = json.loads((Path(__file__).parents[1] / 'fixtures' / 'memory-owner-question-size.json').read_text())
    store = SkillStore(tmp_path)
    for text in fixture['texts']:
        store.save_memory(text, protected=True)
    lines = owner_conflict_lines(store._facts.snapshot()['entries'])
    assert len(lines) == 1
    assert len(lines[0]) <= fixture['footer_chars'] + fixture['allowance_chars']
    section = store.render_memory_section()
    assert len(section) <= fixture['section_chars'] + fixture['allowance_chars']
    paragraphs = [p.strip() for p in section.split('\n\n') if p.strip()]
    assert len(paragraphs) == len(set(paragraphs)), 'do not repeat the memory footer/owner question'


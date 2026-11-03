"""Review migrations: direct owner authority survives chains and replica races."""
import copy
import json
from pathlib import Path

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


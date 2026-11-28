"""Behaviour rules: the memory learner's single channel for how to work with
the owner. Frustration detection, model families, the ``<rule>`` output, the
prompt section, the on/off switch and the per-pass cost cap."""
from __future__ import annotations

import pytest
from fluid.codemode import SkillStore, Synthesizer
from fluid.codemode import behavior
from fluid.codemode import continual as continual_mod
from fluid.providers.base import StreamChunk

from .test_learner_coverage import NoChanges, edit_cell


class Answer(NoChanges):
    """Answers every pass with ``text``."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    async def stream(self, request):
        self.prompts.append(request.messages[0].content[0].text)
        yield StreamChunk("message_start", {"usage": {"input_tokens": 100}})
        yield StreamChunk("text_delta", {"text": self.text})
        yield StreamChunk("usage", {"usage": {"output_tokens": 30}, "stop_reason": "end_turn"})


def rules(store: SkillStore) -> list:
    return [m for m in store.memories() if behavior.is_rule_key(m.key)]


# ─── frustration ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, label", [
    ("this is fucking broken", "profanity"),
    ("what the hell, crap", "profanity"),
    ("WTF is this", "profanity"),
    ("I told you to run the tests first", "phrase"),
    ("how many times do I have to say it", "phrase"),
    ("STOP CHANGING THE BRANCH NOW", "shouting"),
    ("this sucks and it's useless", "negative"),
    ("seriously???", "negative"),
])
def test_angry_or_frustrated_messages_are_flagged(text, label):
    assert behavior.frustration_signal(text) == label


@pytest.mark.parametrize("text", [
    "Hello, can you add a class for the parser?",
    "The old design was terrible.",  # one negative word alone
    "Please pass the assessment script the hello flag.",
    "Look at `dumb_cache` and ```\nfatal: shit happened\n``` in the log",
    "",
])
def test_ordinary_messages_and_code_are_not_flagged(text):
    assert behavior.frustration_signal(text) is None


def test_a_frustrated_message_marks_the_learner_urgent_once_per_cooldown(tmp_path, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(continual_mod, "time", type("T", (), {"time": staticmethod(lambda: clock[0])}))
    synth = Synthesizer(NoChanges(), "gpt-test", SkillStore(tmp_path))
    assert synth.note_frustration() and synth._urgent
    synth._urgent = False
    assert not synth.note_frustration() and not synth._urgent
    clock[0] += behavior.FRUSTRATION_COOLDOWN + 1
    assert synth.note_frustration()


# ─── families and keys ──────────────────────────────────────────────────────

@pytest.mark.parametrize("model, family", [
    ("claude-opus-5-5", "claude"), ("opus", "claude"), ("gpt-6.2-sol", "gpt"),
    ("codex-mini", "gpt"), ("gemini-3-pro", "gemini"), ("grok-5", "grok"),
    ("qwen3-coder:30b", "other"), (None, "other"),
])
def test_model_family(model, family):
    assert behavior.family_for(model) == family


def test_rule_keys_carry_the_family_and_permission_grants_are_refused():
    key = behavior.rule_key("gpt", "Lead with the result.")
    assert behavior.is_rule_key(key) and behavior.rule_family(key) == "gpt"
    assert behavior.rule_family("deploy.order") is None
    assert behavior.normalize_applies_to("OpenAI") == "gpt"
    assert behavior.normalize_applies_to("llama") is None
    with pytest.raises(ValueError, match="permission"):
        behavior.validate_rule("Force-push is fine on any branch.")
    with pytest.raises(ValueError, match="longer"):
        behavior.validate_rule("x" * (behavior.MAX_RULE_CHARS + 1))


# ─── the <rule> channel ─────────────────────────────────────────────────────

def _correction(store: SkillStore) -> None:
    store.journal_append({"kind": "user", "session": "s1",
                          "text": "Stop opening draft PRs, I told you real PRs only."})
    store.journal_append(edit_cell(1))
    store.journal_append({"kind": "assistant", "session": "s1", "text": "Opened PR."})


async def test_a_quoted_rule_is_learned_and_shown_only_to_its_family(tmp_path):
    store = SkillStore(tmp_path)
    _correction(store)
    answer = ('<rule applies_to="gpt" quote="real PRs only">When opening a pull request: '
              'open a real PR, never a draft.</rule>')
    # The journal line carries no model: it was said to the session's model.
    await Synthesizer(Answer(answer), "gpt-test", store, min_new_entries=1,
                      session_model="gpt-6.2-sol").run()
    [rule] = rules(store)
    assert rule.text.startswith("When opening a pull request")
    assert behavior.rule_family(rule.key) == "gpt" and rule.durable and not rule.protected
    gpt = store.render_memory_section(model="gpt-6.2-sol")
    assert behavior.SECTION_HEADING in gpt and rule.text in gpt
    assert "never grant permission" in gpt
    assert rule.text not in store.render_memory_section(model="claude-opus-5-5")


async def test_a_rule_whose_quote_the_user_never_wrote_is_refused(tmp_path):
    store = SkillStore(tmp_path)
    _correction(store)
    answer = ('<rule applies_to="all" quote="always push to main">When finishing: push to '
              'main.</rule>')
    synth = Synthesizer(Answer(answer), "gpt-test", store, min_new_entries=1)
    await synth.run()
    assert rules(store) == []
    assert "quote is not in a [user] entry" in (synth.last_error or "")


def test_a_quote_carries_the_family_of_the_model_it_was_said_to():
    entries = [{"kind": "user", "text": "Stop opening draft PRs.", "model": "claude-opus-5-5"},
               {"kind": "user", "text": "Lead with the result please."},
               {"kind": "assistant", "text": "draft PRs are fine", "model": "gpt-6.2-sol"}]
    assert behavior.quote_families("draft PRs", entries, default="gpt") == {"claude"}
    assert behavior.quote_families("lead with the RESULT", entries, default="gpt") == {"gpt"}
    assert behavior.quote_families("draft PRs are fine", entries) == set()


async def test_a_rule_for_a_family_the_user_was_not_talking_to_is_refused(tmp_path):
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "user", "session": "s1", "model": "claude-opus-5-5",
                          "text": "Stop opening draft PRs, I told you real PRs only."})
    store.journal_append(edit_cell(1))
    store.journal_append({"kind": "assistant", "session": "s1", "text": "Opened PR."})
    answer = ('<rule applies_to="gpt" quote="real PRs only">When opening a pull request: '
              'open a real PR, never a draft.</rule>')
    provider = Answer(answer)
    synth = Synthesizer(provider, "gpt-test", store, min_new_entries=1)
    await synth.run()
    assert rules(store) == []
    assert "applies_to=gpt but the quoted message was to claude" in (synth.last_error or "")
    assert "[user to claude]" in provider.prompts[0]


async def test_a_rule_for_the_family_the_user_was_talking_to_is_learned(tmp_path):
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "user", "session": "s1", "model": "claude-opus-5-5",
                          "text": "Stop opening draft PRs, I told you real PRs only."})
    store.journal_append(edit_cell(1))
    store.journal_append({"kind": "assistant", "session": "s1", "text": "Opened PR."})
    answer = ('<rule applies_to="claude" quote="real PRs only">When opening a pull request: '
              'open a real PR, never a draft.</rule>')
    await Synthesizer(Answer(answer), "gpt-test", store, min_new_entries=1).run()
    [rule] = rules(store)
    assert behavior.rule_family(rule.key) == "claude"


def test_moving_a_rule_to_another_family_keeps_the_new_family(tmp_path):
    store = SkillStore(tmp_path)
    text = "When reporting: lead with the result."
    rule = store.save_memory(text, provenance="user", protected=True,
                             key=behavior.rule_key("gpt", text), when="When working with the owner")
    moved = store.save_memory(text, memory_id=rule.id, expected_revision=rule.revision,
                              provenance="user", protected=True, key=behavior.rule_key("claude", text), when="When working with the owner")
    [only] = rules(store)
    assert only.id == rule.id == moved.id and behavior.rule_family(only.key) == "claude"


def test_the_same_rule_text_for_two_families_is_two_rules(tmp_path):
    store = SkillStore(tmp_path)
    text = "When reporting: lead with the result."
    for family in ("gpt", "claude"):
        store.save_memory(text, provenance="user", protected=True,
                          key=behavior.rule_key(family, text), when="When working with the owner")
    assert sorted(behavior.rule_family(r.key) for r in rules(store)) == ["claude", "gpt"]


def test_deleting_one_familys_copy_of_a_rule_leaves_the_others_free(tmp_path):
    store = SkillStore(tmp_path)
    text = "When reporting: lead with the result."
    saved = {family: store.save_memory(text, provenance="user", durable=True,
                                       key=behavior.rule_key(family, text),
                                       when="When working with the owner")
             for family in ("gpt", "claude")}
    assert store.delete_memory(saved["gpt"].id, explicit=True, purge_evidence=False)
    claude = saved["claude"]
    digest = claude.key.rsplit(".", 1)[-1]
    moved = store.save_memory(text, memory_id=claude.id, provenance="user", durable=True,
                              key=f"{behavior.KEY_PREFIX}gemini.{digest}")
    assert behavior.rule_family(moved.key) == "gemini"
    # The deleted GPT rule itself stays fenced off.
    with pytest.raises(ValueError):
        store.save_memory(text, provenance="user", durable=True,
                          key=behavior.rule_key("gpt", text) + "x")


def test_a_deleted_rule_fences_each_old_wording_for_the_family_it_was_held_under(tmp_path):
    store = SkillStore(tmp_path)
    first, second = "When reporting: lead with the result.", "When reporting: lead with the result and the blocker."
    rule = store.save_memory(first, provenance="user", durable=True,
                             key=behavior.rule_key("claude", first), when="When working with the owner")
    digest = rule.key.rsplit(".", 1)[-1]
    store.save_memory(second, memory_id=rule.id, provenance="user", durable=True, key=rule.key)
    store.save_memory(second, memory_id=rule.id, provenance="user", durable=True,
                      key=f"{behavior.KEY_PREFIX}gpt.{digest}")
    assert store.delete_memory(rule.id, explicit=True, purge_evidence=False)
    # The first wording was only ever a Claude rule: GPT may still learn it.
    store.save_memory(first, provenance="user", durable=True,
                      key=behavior.rule_key("gpt", first), when="When working with the owner")
    # Each wording stays fenced for the family that held it.
    for family, text in (("claude", first), ("claude", second), ("gpt", second)):
        with pytest.raises(ValueError):
            store.save_memory(text, provenance="user", durable=True,
                              key=behavior.rule_key(family, text), when="When working with the owner")


def test_a_deleted_rule_fences_wordings_its_undo_history_has_dropped(tmp_path):
    store = SkillStore(tmp_path)
    wordings = [f"When reporting: lead with the result, take {n}." for n in range(4)]
    rule = store.save_memory(wordings[0], provenance="user", durable=True,
                             key=behavior.rule_key("claude", wordings[0]), when="When working with the owner")
    for text in wordings[1:]:
        store.save_memory(text, memory_id=rule.id, provenance="user", durable=True, key=rule.key)
    entry = next(e for e in store._facts._load(read_only=True)["entries"] if e["id"] == rule.id)
    assert wordings[0] not in [h["text"] for h in entry["history"]]
    assert store.delete_memory(rule.id, explicit=True, purge_evidence=False)
    for text in wordings:
        with pytest.raises(ValueError):
            store.save_memory(text, provenance="user", durable=True,
                              key=behavior.rule_key("claude", text), when="When working with the owner")


def test_no_one_can_move_a_rule_onto_a_slot_another_rule_holds(tmp_path):
    store = SkillStore(tmp_path)
    text = "When reporting: lead with the result."
    learned = store.save_memory(text, provenance="user", durable=True,
                                key=behavior.rule_key("all", text), when="When working with the owner")
    kept = store.save_memory("When reporting: lead with the blocker.", provenance="user", protected=True,
                             key=behavior.rule_key("gpt", text), when="When working with the owner")
    # The learner narrows its All-models rule to GPT, onto the owner's slot.
    with pytest.raises(ValueError, match="another rule already holds"):
        store.save_memory("When reporting to the owner: lead with the result.", memory_id=learned.id,
                          provenance="user", durable=True, key=kept.key)
    by_id = {m.id: m for m in store.memories()}
    assert by_id[learned.id].key == learned.key and not by_id[kept.id].conflicted


OWNER_WHEN = "When working with the owner"


def _refused(store, family, text):
    with pytest.raises(ValueError):
        store.save_memory(text, provenance="user", durable=True,
                          key=behavior.rule_key(family, text), when=OWNER_WHEN)


def _peer_id(store, rule):
    """The ID a synced copy of ``rule`` has on another worker."""
    return next(m.id for m in store.memories() if m.key == rule.key)


def test_a_rule_copy_an_owner_edit_discards_stays_fenced_after_a_delete(tmp_path):
    first, theirs = "When reporting: lead with the result.", "When reporting: lead with the blocker."
    key = behavior.rule_key("gpt", first)
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    mine = a.save_memory(first, provenance="user", durable=True, key=key, when=OWNER_WHEN)
    b.save_memory(theirs, provenance="user", durable=True, key=key, when=OWNER_WHEN)
    a._facts.merge(b._facts.export())
    current = next(m for m in a.memories() if m.id == mine.id)
    assert current.conflicted
    # The owner resolves the conflict by editing their copy, then deletes it.
    kept = a.save_memory("When reporting: lead with the result, then the blocker.", memory_id=mine.id,
                         expected_revision=current.revision, provenance="user", protected=True, key=key)
    assert a.delete_memory(kept.id, explicit=True, purge_evidence=False)
    _refused(a, "gpt", theirs)


def test_concurrent_revisions_of_one_rule_keep_both_wordings_fenced(tmp_path):
    first = "When reporting: lead with the result."
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    rule = a.save_memory(first, provenance="user", durable=True,
                         key=behavior.rule_key("gpt", first), when=OWNER_WHEN)
    b._facts.merge(a._facts.export())
    a.save_memory("When reporting: lead with the result and the risk.", memory_id=rule.id,
                  provenance="user", durable=True, key=rule.key)
    theirs = "When reporting: lead with the blocker."
    b.save_memory(theirs, memory_id=_peer_id(b, rule), provenance="user", durable=True, key=rule.key)
    a._facts.merge(b._facts.export())
    current = next(m for m in a.memories() if m.id == rule.id)
    assert current.conflicted
    kept = a.save_memory("When reporting: lead with the result, then the blocker.", memory_id=rule.id,
                         expected_revision=current.revision, provenance="user", protected=True, key=rule.key)
    assert a.delete_memory(kept.id, explicit=True, purge_evidence=False)
    _refused(a, "gpt", theirs)


def test_merged_rule_fences_are_the_same_whichever_worker_merges(tmp_path):
    first = "When reporting: lead with the result."
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    rule = a.save_memory(first, provenance="user", durable=True,
                         key=behavior.rule_key("gpt", first), when=OWNER_WHEN)
    b._facts.merge(a._facts.export())
    a.save_memory("When reporting: lead with the risk.", memory_id=rule.id,
                  provenance="user", durable=True, key=rule.key)
    b.save_memory("When reporting: lead with the blocker.", memory_id=_peer_id(b, rule),
                  provenance="user", durable=True, key=rule.key)
    a_doc, b_doc = a._facts.export(), b._facts.export()
    a._facts.merge(b_doc)
    b._facts.merge(a_doc)

    def fences(store):
        entry = next(e for e in store._facts._load(read_only=True)["entries"] if e.get("key") == rule.key)
        return set(entry["fences"])

    assert fences(a) == fences(b) and len(fences(a)) == 3


def _rule_at_the_fence_cap(store):
    """A GPT rule whose fences are full: it has held MAX_RULE_FENCES wordings."""
    from fluid.codemode import facts

    wordings = [f"When reporting: lead with the result, take {n}." for n in range(facts.MAX_RULE_FENCES + 1)]
    rule = store.save_memory(wordings[0], provenance="user", durable=True,
                             key=behavior.rule_key("gpt", wordings[0]), when=OWNER_WHEN)
    for text in wordings[1:]:
        store.save_memory(text, memory_id=rule.id, provenance="user", durable=True, key=rule.key)
    entry = next(e for e in store._facts._load(read_only=True)["entries"] if e["id"] == rule.id)
    assert len(entry["fences"]) == facts.MAX_RULE_FENCES
    return rule, wordings[-1], entry["fences"]


def _sorts_after(held):
    """A competing wording whose GPT fingerprint sorts after every held one,
    so a sorted bound that does not reserve it would drop it."""
    from fluid.codemode import facts

    for n in range(500):
        text = f"When reporting: lead with the blocker, case {n}."
        if facts._fence_fingerprint(text, behavior.rule_key("gpt", text)) > max(held):
            return text
    raise AssertionError("no candidate wording")


def _resolve_and_delete(store, rule_key):
    current = next(m for m in store.memories() if m.key == rule_key)
    assert current.conflicted
    kept = store.save_memory("When reporting: lead with the outcome.", memory_id=current.id,
                             expected_revision=current.revision, provenance="user",
                             protected=True, key=rule_key)
    assert store.delete_memory(kept.id, explicit=True, purge_evidence=False)


def test_at_the_fence_cap_a_merge_keeps_both_workers_current_wordings(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    first = "When reporting: lead with the result, take 0."
    rule = a.save_memory(first, provenance="user", durable=True,
                         key=behavior.rule_key("gpt", first), when=OWNER_WHEN)
    b._facts.merge(a._facts.export())
    from fluid.codemode import facts

    wordings = [f"When reporting: lead with the result, take {n}." for n in range(1, facts.MAX_RULE_FENCES + 1)]
    for text in wordings:
        a.save_memory(text, memory_id=rule.id, provenance="user", durable=True, key=rule.key)
    held = next(e for e in a._facts._load(read_only=True)["entries"] if e["id"] == rule.id)["fences"]
    assert len(held) == facts.MAX_RULE_FENCES
    theirs = _sorts_after(held)
    b.save_memory(theirs, memory_id=_peer_id(b, rule), provenance="user", durable=True, key=rule.key)
    a_doc, b_doc = a._facts.export(), b._facts.export()
    a._facts.merge(b_doc)
    b._facts.merge(a_doc)
    for store in (a, b):
        _resolve_and_delete(store, rule.key)
        for text in (theirs, wordings[-1]):
            _refused(store, "gpt", text)


def test_at_the_fence_cap_resolving_a_slot_conflict_keeps_the_discarded_wording(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    rule, _, held = _rule_at_the_fence_cap(a)
    theirs = _sorts_after(held)
    b.save_memory(theirs, provenance="user", durable=True, key=rule.key, when=OWNER_WHEN)
    a._facts.merge(b._facts.export())
    _resolve_and_delete(a, rule.key)
    _refused(a, "gpt", theirs)


def test_at_the_fence_cap_every_workers_competing_wording_stays_fenced(tmp_path):
    from fluid.codemode import facts

    a, b, c = (SkillStore(tmp_path / name) for name in "abc")
    first = "When reporting: lead with the result, take 0."
    rule = a.save_memory(first, provenance="user", durable=True,
                         key=behavior.rule_key("gpt", first), when=OWNER_WHEN)
    for peer in (b, c):
        peer._facts.merge(a._facts.export())
    for n in range(1, facts.MAX_RULE_FENCES + 1):
        a.save_memory(f"When reporting: lead with the result, take {n}.", memory_id=rule.id,
                      provenance="user", durable=True, key=rule.key)
    held = next(e for e in a._facts._load(read_only=True)["entries"] if e["id"] == rule.id)["fences"]
    theirs = {}
    # B's wording sorts out of the two shown alternatives once C's arrives,
    # and its fingerprint after every held one.
    for name, peer in (("zeta", b), ("beta", c)):
        candidates = (f"When reporting: lead with the {name} summary, case {n}." for n in range(500))
        theirs[name] = next(t for t in candidates
                            if facts._fence_fingerprint(t, rule.key) > max(held))
        peer.save_memory(theirs[name], memory_id=_peer_id(peer, rule), provenance="user",
                         durable=True, key=rule.key)
    a._facts.merge(b._facts.export())
    a._facts.merge(c._facts.export())
    _resolve_and_delete(a, rule.key)
    for text in theirs.values():
        _refused(a, "gpt", text)


def test_a_newer_copy_keeps_the_wordings_a_dispute_recorded(tmp_path):
    import copy

    first = "When reporting: lead with the result."
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    rule = a.save_memory(first, provenance="user", durable=True,
                         key=behavior.rule_key("gpt", first), when=OWNER_WHEN)
    b._facts.merge(a._facts.export())
    a.save_memory("When reporting: lead with the result and the risk.", memory_id=rule.id,
                  provenance="user", durable=True, key=rule.key)
    theirs = "When reporting: lead with the blocker."
    b.save_memory(theirs, memory_id=_peer_id(b, rule), provenance="user", durable=True, key=rule.key)
    a._facts.merge(b._facts.export())
    # A third worker's later edit that saw neither wording: newer clock, still
    # conflicted, no record of B's wording in its fences or history.
    snapshot = copy.deepcopy(a._facts.export())
    entry = next(e for e in snapshot["entries"] if e.get("key") == rule.key)
    clock = dict(entry["clock"])
    clock["c" * 32] = 1
    entry.update(text="When reporting: lead with the outcome first.", clock=clock,
                 revision=entry["revision"] + 1, history=[], alternatives=[], conflicted=True,
                 fences=[], contested=[])
    a._facts.merge(snapshot)
    _resolve_and_delete(a, rule.key)
    _refused(a, "gpt", theirs)


def test_replaying_old_disputes_leaves_a_resolved_rule_the_same_on_every_worker(tmp_path):
    first = "When reporting: lead with the result."
    a, b, c = (SkillStore(tmp_path / name) for name in "abc")
    rule = a.save_memory(first, provenance="user", durable=True,
                         key=behavior.rule_key("gpt", first), when=OWNER_WHEN)
    for peer in (b, c):
        peer._facts.merge(a._facts.export())
    a.save_memory("When reporting: lead with the result and the risk.", memory_id=rule.id,
                  provenance="user", durable=True, key=rule.key)
    for name, peer in (("blocker", b), ("outcome", c)):
        peer.save_memory(f"When reporting: lead with the {name}.", memory_id=_peer_id(peer, rule),
                         provenance="user", durable=True, key=rule.key)
        peer._facts.merge(a._facts.export())  # each records its own A/peer dispute
    stale = [b._facts.export(), c._facts.export()]
    for snapshot in stale:
        a._facts.merge(snapshot)
    current = next(m for m in a.memories() if m.key == rule.key)
    a.save_memory("When reporting: lead with the summary.", memory_id=current.id,
                  expected_revision=current.revision, provenance="user", protected=True, key=rule.key)

    def copy_on(store):
        return next(e for e in store._facts._load(read_only=True)["entries"] if e.get("key") == rule.key)

    resolved = copy_on(a)
    for snapshot in stale * 2:
        a._facts.merge(snapshot)
        assert copy_on(a) == resolved
    for peer in (b, c):
        peer._facts.merge(a._facts.export())
    assert copy_on(b)["fences"] == copy_on(c)["fences"] == resolved["fences"]


def test_at_the_fence_cap_a_partly_fenced_dispute_keeps_all_its_wordings(tmp_path):
    from fluid.codemode import facts

    store = SkillStore(tmp_path)
    rule, _, held = _rule_at_the_fence_cap(store)
    winner = next(e for e in store._facts._load(read_only=True)["entries"] if e["id"] == rule.id)
    current = facts._fence_fingerprint(winner["text"], rule.key)
    # A late dispute record: one wording this copy still fences, one it never saw.
    fenced = winner["fences"][0]  # the oldest: the first a rebuild would evict
    missing = facts._fence_fingerprint(_sorts_after(held), rule.key)
    facts._carry_contested(winner, {"key": rule.key, "contested": [fenced, missing]})
    assert {fenced, missing, current} <= set(winner["fences"])
    assert len(winner["fences"]) == facts.MAX_RULE_FENCES


def test_resolving_a_cross_family_conflict_fences_each_wording_for_its_own_family(tmp_path):
    first = "When reporting: lead with the result."
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    rule = a.save_memory(first, provenance="user", durable=True,
                         key=behavior.rule_key("gpt", first), when=OWNER_WHEN)
    b._facts.merge(a._facts.export())
    a.save_memory("When reporting: lead with the result and the risk.", memory_id=rule.id,
                  provenance="user", durable=True, key=rule.key)
    digest = rule.key.rsplit(".", 1)[-1]
    theirs = "When reporting: lead with the blocker."
    b.save_memory(theirs, memory_id=_peer_id(b, rule), provenance="user", durable=True,
                  key=f"{behavior.KEY_PREFIX}claude.{digest}")
    a._facts.merge(b._facts.export())
    _resolve_and_delete(a, rule.key)
    # Only Claude ever held that wording: it stays fenced for Claude alone.
    _refused(a, "claude", theirs)
    a.save_memory(theirs, provenance="user", durable=True,
                  key=behavior.rule_key("gpt", theirs), when=OWNER_WHEN)


def test_keyed_facts_that_disagree_are_flagged_even_when_one_is_trimmed_for_room(tmp_path, monkeypatch):
    from fluid.codemode import facts

    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    b.save_memory("The owner's name is Blair.", provenance="session", key="profile.name")
    a.save_memory("The owner's name is Avery.", provenance="session", key="profile.name")
    a.save_memory("The owner works on AI Shuffle.", provenance="session")
    monkeypatch.setattr(facts, "MAX_AGENT_LIVE", 2)
    a._facts.merge(b._facts.export())
    doc = a._facts._load(read_only=True)
    copies = [e for e in doc["entries"] + doc["archived"] if e.get("key") == "profile.name"]
    assert len(copies) == 2 and all(e.get("conflicted") for e in copies)


def test_slot_contention_clears_once_a_candidate_is_retired(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    b.save_memory("The owner's name is Blair.", provenance="session", key="profile.name")
    a.save_memory("The owner's name is Avery.", provenance="session", key="profile.name")
    a._facts.merge(b._facts.export())
    b._facts.merge(a._facts.export())

    def slot(store):
        return sorted((m.text, m.conflicted) for m in store.memories() if m.key == "profile.name")

    assert slot(a) == slot(b) == [("The owner's name is Avery.", True),
                                  ("The owner's name is Blair.", True)]
    blair = next(m for m in a.memories() if m.text == "The owner's name is Blair.")
    assert a.retire_memory(blair.id, reason="duplicate")
    assert slot(a) == [("The owner's name is Avery.", False)]
    # The other worker settles too; its stale flag does not bring the conflict back.
    b._facts.merge(a._facts.export())
    a._facts.merge(b._facts.export())
    assert slot(a) == slot(b) == [("The owner's name is Avery.", False)]


def test_a_concurrent_edit_of_one_fact_stays_flagged_through_maintenance(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    first = a.save_memory("Deploys go to staging.", provenance="session")
    b._facts.merge(a._facts.export())
    peer = next(m.id for m in b.memories() if m.text == "Deploys go to staging.")
    a.save_memory("Deploys go to prod.", memory_id=first.id, provenance="session")
    b.save_memory("Deploys go to canary.", memory_id=peer, provenance="session")
    a._facts.merge(b._facts.export())
    a.save_memory("The build cache is warm.", provenance="session")  # maintenance runs
    assert [m.conflicted for m in a.memories() if m.text.startswith("Deploys")] == [True]


class _SavesDuringInference(Answer):
    """A foreground save lands while the learner's model is answering."""

    def __init__(self, text: str, during) -> None:
        super().__init__(text)
        self.during = during

    async def stream(self, request):
        self.during()
        async for chunk in super().stream(request):
            yield chunk


async def test_a_rule_rewrite_skips_a_rule_changed_during_inference(tmp_path):
    store = SkillStore(tmp_path)
    learned = store.save_memory("When opening a pull request: open a real PR.", provenance="user",
                                durable=True, key=behavior.rule_key("all", "pr"), when=OWNER_WHEN)
    _correction(store)
    newer = "When opening a pull request: open a real PR and link the issue."

    def foreground():
        store.save_memory(newer, memory_id=learned.id, expected_revision=learned.revision,
                          provenance="user", durable=True, key=learned.key)

    answer = (f'<rule id="{learned.id}" applies_to="all" quote="real PRs only">When opening a '
              'pull request: never open a draft.</rule>')
    synth = Synthesizer(_SavesDuringInference(answer, foreground), "gpt-test", store,
                        min_new_entries=1)
    await synth.run()
    [rule] = rules(store)
    assert rule.text == newer
    assert "changed since it was read" in (synth.last_error or "")


async def test_the_learner_retires_a_listed_rule_but_not_one_the_owner_kept(tmp_path):
    store = SkillStore(tmp_path)
    obsolete = store.save_memory("When opening a pull request: open a draft.", provenance="user",
                                 durable=True, key=behavior.rule_key("all", "draft"), when=OWNER_WHEN)
    kept = store.save_memory("When reporting: lead with the result.", provenance="user",
                             protected=True, key=behavior.rule_key("all", "lead"), when=OWNER_WHEN)
    synth = Synthesizer(NoChanges(), "gpt-test", store)
    synth._memory_lines()
    synth._rule_lines()  # the prompt lists both rules
    changed = await synth._apply(f'<delete-memory id="{obsolete.id}"/>\n<delete-memory id="{kept.id}"/>')
    assert changed == [f"-memory:{obsolete.id}"]
    assert "kept by the owner" in (synth.last_error or "")
    assert [m.id for m in rules(store)] == [kept.id]
    assert obsolete.id in {m.id for m in store.archived_memories()}


def test_an_expired_copy_does_not_hide_a_rule_moved_into_its_slot(tmp_path, monkeypatch):
    import time as time_mod
    from fluid.codemode import facts

    store = SkillStore(tmp_path)
    old_text = "When reporting: lead with the result."
    gpt_key = behavior.rule_key("gpt", old_text)
    store.save_memory(old_text, provenance="user", durable=True, key=gpt_key, when=OWNER_WHEN)
    real = time_mod.time
    monkeypatch.setattr(facts.time, "time", lambda: real() + facts.AGENT_TTL + 86400)
    assert not [m for m in store.memories() if m.key == gpt_key]  # expired into the archive
    moving = store.save_memory("When reporting: lead with the blocker.", provenance="user",
                               protected=True, key=behavior.rule_key("all", "x"), when=OWNER_WHEN)
    moved = store.save_memory("When reporting: lead with the blocker.", memory_id=moving.id,
                              expected_revision=moving.revision, provenance="user",
                              protected=True, key=gpt_key)
    store._facts.merge(store._facts.export())
    live = next(m for m in store.memories() if m.id == moved.id)
    assert live.key == gpt_key and not live.conflicted


def test_health_repair_keeps_a_rule_that_shares_text_with_a_fact_or_another_family(tmp_path):
    from fluid.codemode import memory_health

    store = SkillStore(tmp_path)
    text = "When reporting: lead with the result."
    fact = store.save_memory(text, provenance="user", protected=True)
    rules = [store.save_memory(text, provenance="user", durable=True, key=behavior.rule_key(f, text),
                               when="When working with the owner") for f in ("gpt", "claude")]
    memory_health.repair(store)
    live = {m.id for m in store.memories()}
    assert {fact.id, *(r.id for r in rules)} <= live


def test_a_rule_and_an_ordinary_fact_with_the_same_text_stay_separate(tmp_path):
    store = SkillStore(tmp_path)
    text = "When reporting: lead with the result."
    fact = store.save_memory(text, provenance="agent")
    rule = store.save_memory(text, provenance="user", durable=True,
                             key=behavior.rule_key("gpt", text), when="When working with the owner")
    assert rule.id != fact.id
    again = store.save_memory(text, provenance="agent")
    assert again.id == fact.id
    entries = {e["id"]: e for e in store._facts._load(read_only=True)["entries"]}
    assert entries[fact.id].get("key") is None
    assert behavior.rule_family(entries[rule.id]["key"]) == "gpt"


def test_a_family_move_is_history_undo_can_restore(tmp_path):
    store = SkillStore(tmp_path)
    text = "When reporting: lead with the result."
    rule = store.save_memory(text, provenance="user", durable=True,
                             key=behavior.rule_key("all", text), when="When working with the owner")
    digest = rule.key.rsplit(".", 1)[-1]
    store.save_memory(text, memory_id=rule.id, provenance="user", durable=True,
                      key=f"{behavior.KEY_PREFIX}gpt.{digest}")
    entry = next(e for e in store._facts._load(read_only=True)["entries"] if e["id"] == rule.id)
    assert entry["history"][-1]["key"] == rule.key and entry["history"][-1]["text"] == text


def test_the_assembled_prompt_shows_a_family_rule_only_to_that_family(tmp_path):
    from fluid.context.compiler import PromptInputs, compile_prompt

    store = SkillStore(tmp_path / "skills")
    text = "When opening a pull request: open a real PR, never a draft."
    store.save_memory(text, provenance="user", protected=True,
                      key=behavior.rule_key("gpt", text), when="When working with the owner")

    def compiled(model):
        return compile_prompt(PromptInputs(
            base="base", cwd=str(tmp_path), include_env=False,
            include_global_instructions=False, include_project_instructions=False,
            include_skills=False, include_agents=False, include_plugins=False,
            memories_dir=str(tmp_path / "skills"), model=model,
        )).text

    assert text in compiled("gpt-6.2-sol")
    assert text not in compiled("claude-opus-5-5")


async def test_a_rewrite_cannot_touch_a_rule_the_owner_kept(tmp_path):
    store = SkillStore(tmp_path)
    kept = store.save_memory("When reporting: lead with the result.", provenance="user",
                             protected=True, key=behavior.rule_key("all", "lead"),
                             when="When working with the owner")
    _correction(store)
    answer = (f'<rule id="{kept.id}" applies_to="all" quote="real PRs only">When reporting: '
              'bury the result.</rule>')
    synth = Synthesizer(Answer(answer), "gpt-test", store, min_new_entries=1)
    await synth.run()
    [rule] = rules(store)
    assert rule.text == "When reporting: lead with the result."
    assert "kept by the owner" in (synth.last_error or "")


async def test_rules_stay_out_of_the_learners_fact_list(tmp_path):
    store = SkillStore(tmp_path)
    store.save_memory("When reviewing: cite the line.", provenance="user", durable=True,
                      key=behavior.rule_key("all", "cite"), when="When working with the owner")
    store.save_memory("The repo deploys from main.", provenance="user", protected=True)
    _correction(store)
    provider = NoChanges()
    await Synthesizer(provider, "gpt-test", store, min_new_entries=1).run()
    [prompt] = provider.prompts
    memory_part, rules_part = prompt.split("Behaviour rules", 1)[0], prompt
    assert "The repo deploys from main." in memory_part
    assert "When reviewing: cite the line." in rules_part


# ─── cost: off switch and metered cap ───────────────────────────────────────

async def test_a_switched_off_learner_makes_no_call_and_keeps_its_place(tmp_path):
    store = SkillStore(tmp_path)
    _correction(store)
    switch = [False]
    provider = NoChanges()
    synth = Synthesizer(provider, "gpt-test", store, min_new_entries=1, enabled=lambda: switch[0])
    assert not synth.due(closing=True)
    assert await synth.run() == [] and synth.last_pass_skipped
    assert provider.prompts == [] and store.synth_cursor() == 0
    assert store._facts.inference_budget().get("calls", 0) == 0
    switch[0] = True
    await synth.run()
    assert len(provider.prompts) == 1 and store.synth_cursor() == 3


def test_a_metered_learner_never_sends_a_large_request(tmp_path):
    store = SkillStore(tmp_path)
    metered = Synthesizer(NoChanges(), "gpt-test", store, context_window=1_000_000,
                          billing="metered")
    unknown = Synthesizer(NoChanges(), "gpt-test", store, context_window=1_000_000)
    flat = Synthesizer(NoChanges(), "gpt-test", store, context_window=1_000_000,
                       billing="subscription")
    for synth in (metered, unknown, flat):
        synth._size_request()
    assert metered._prompt_limit == unknown._prompt_limit == continual_mod.METERED_PROMPT_CHAR_LIMIT
    assert flat._prompt_limit == continual_mod.LARGE_PROMPT_CHAR_LIMIT

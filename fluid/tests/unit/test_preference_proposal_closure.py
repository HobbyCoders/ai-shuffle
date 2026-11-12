"""A learner preference proposal closes exactly when a tool call names it.

Remember with ``proposal=<topic>`` (the rule was saved) or Forget with
``proposal=<topic>`` and ``confirm=true`` (declined, or already saved) close
the open proposal on that topic the session sees, and leave a tombstone so
the learner does not queue the same wording again. Every test drives the
real tool handlers and reads the result through the session footer, the
queue and the learner, not through the queue class alone.
"""
from __future__ import annotations

import json
import time

import pytest
from fluid.codemode import SkillStore, Synthesizer, memory_health
from fluid.codemode.preferences import (
    MAX_OPEN_PREFERENCE_PROPOSALS,
    PREFERENCE_PROPOSAL_TTL,
    PREFERENCE_PROPOSALS_FILE,
    PreferenceProposals,
)
from fluid.codemode.skills import journal_lock
from fluid.providers.base import StreamChunk
from fluid.tools.builtins.forget import SPEC as FORGET_SPEC
from fluid.tools.builtins.forget import handler as forget
from fluid.tools.builtins.remember import SPEC as REMEMBER_SPEC
from fluid.tools.builtins.remember import handler as remember
from fluid.tools.registry import ToolCtx

PROJECT = "project:fixture"
OTHER = "project:other"
RULE = "Release notes go in the changelog before the tag is pushed."
WHEN = "When cutting a release"


def ctx(store, project=PROJECT):
    return ToolCtx(extra={"memory_store": store, "recall_store": store, "memory_project": project})


def footer(store, project=PROJECT) -> str:
    return store.learner_proposal_footer(project)


def proposal_lines(store, project=PROJECT) -> list[str]:
    return [line for line in footer(store, project).splitlines() if "[proposed, not saved;" in line]


def test_both_tools_publish_the_proposal_parameter():
    # Native and delegated sessions both read these specs (the Claude CLI
    # bridge registers module.SPEC unchanged).
    assert REMEMBER_SPEC.input_schema["properties"]["proposal"]["type"] == "string"
    assert FORGET_SPEC.input_schema["properties"]["proposal"]["type"] == "string"
    assert FORGET_SPEC.input_schema["required"] == ["confirm"]
    assert "proposal=<topic>" in FORGET_SPEC.description


def test_the_footer_line_names_both_exact_calls(tmp_path):
    store = SkillStore(tmp_path)
    PreferenceProposals(store).propose("release_notes", RULE)
    [line] = proposal_lines(store)
    assert "Remember, proposal='release_notes'" in line and "replaces=[older rows]" in line
    assert "save it without asking" in line
    assert "Forget, proposal='release_notes', confirm=true" in line


async def test_a_save_naming_the_proposal_closes_it(tmp_path):
    store = SkillStore(tmp_path)
    queue = PreferenceProposals(store)
    queue.propose("release_notes", RULE)
    queue.propose("ci_policy", "Tests run on hosted CI, not on the workers.")
    # An agent's own save (not user-kept, so the old inference never closes it).
    out = await remember({"text": RULE, "when": WHEN, "proposal": "release_notes"}, ctx(store))
    assert not out.is_error
    assert "Closed learner proposal 'release_notes' (saved)" in out.text
    assert f"(global) {RULE.rstrip('.')}. For 14 days" in out.text
    assert [r["topic"] for r in queue.open(PROJECT)] == ["ci_policy"], "topic A never closes B"
    assert "release_notes" not in footer(store) and "ci_policy" in footer(store)
    # A new session's snapshot no longer shows it either.
    assert "release_notes" not in SkillStore(tmp_path).render_memory_section(PROJECT)


async def test_a_dismissal_closes_it_without_touching_any_fact(tmp_path):
    store = SkillStore(tmp_path)
    store.save_memory("The changelog is updated with each release.", protected=True, when=WHEN)
    before = [(m.id, m.revision, m.text) for m in store.memories()]
    PreferenceProposals(store).propose("release_notes", RULE)
    out = await forget({"proposal": "release_notes", "confirm": True}, ctx(store))
    assert not out.is_error
    assert "Closed learner proposal 'release_notes' (dismissed)" in out.text
    assert "No saved fact was changed" in out.text
    assert proposal_lines(store) == []
    assert [(m.id, m.revision, m.text) for m in store.memories()] == before


@pytest.mark.parametrize("call", [
    {"proposal": "release_notes"},                                   # no confirm
    {"proposal": "release_notes", "confirm": False},
    {"proposal": "Not A Topic!", "confirm": True},                   # malformed
    {"proposal": 7, "confirm": True},
    {"proposal": "release_notes", "memory_id": "m1", "confirm": True},  # ambiguous
])
async def test_a_refused_dismissal_changes_nothing(tmp_path, call):
    store = SkillStore(tmp_path)
    store.save_memory("Keep this.", protected=True, when=WHEN)
    PreferenceProposals(store).propose("release_notes", RULE)
    raw = (tmp_path / PREFERENCE_PROPOSALS_FILE).read_bytes()
    out = await forget(call, ctx(store))
    assert out.is_error
    assert (tmp_path / PREFERENCE_PROPOSALS_FILE).read_bytes() == raw
    assert [m.text for m in store.memories()] == ["Keep this."]


async def test_an_unknown_topic_closes_nothing_and_says_so(tmp_path):
    store = SkillStore(tmp_path)
    PreferenceProposals(store).propose("release_notes", RULE)
    raw = (tmp_path / PREFERENCE_PROPOSALS_FILE).read_bytes()
    out = await forget({"proposal": "deploy", "confirm": True}, ctx(store))
    assert not out.is_error and "No open learner proposal has topic 'deploy'" in out.text
    saved = await remember({"text": "Unrelated fact.", "when": WHEN, "proposal": "deploy"}, ctx(store))
    assert not saved.is_error and saved.text.startswith("Saved [")
    assert "No open learner proposal has topic 'deploy'; nothing was closed." in saved.text
    assert (tmp_path / PREFERENCE_PROPOSALS_FILE).read_bytes() == raw


async def test_a_malformed_topic_refuses_the_save(tmp_path):
    store = SkillStore(tmp_path)
    PreferenceProposals(store).propose("release_notes", RULE)
    out = await remember({"text": RULE, "when": WHEN, "proposal": "Not A Topic!"}, ctx(store))
    assert out.is_error and store.memories() == []
    assert len(proposal_lines(store)) == 1


async def test_a_failed_save_leaves_the_proposal_open(tmp_path):
    store = SkillStore(tmp_path)
    PreferenceProposals(store).propose("release_notes", RULE)
    # Validation failure: a new fact without its when-line.
    out = await remember({"text": RULE, "proposal": "release_notes"}, ctx(store))
    assert out.is_error and len(proposal_lines(store)) == 1
    # Lost revision race: a correction against a stale revision.
    old = store.save_memory("Release notes are optional.", protected=True, when=WHEN)
    store.save_memory("Release notes are optional for patches.", memory_id=old.id,
                      expected_revision=old.revision, protected=True, when=WHEN)
    out = await remember({"text": RULE, "memory_id": old.id, "expected_revision": old.revision,
                          "proposal": "release_notes"}, ctx(store))
    assert out.is_error and out.text.startswith("Not saved")
    assert len(proposal_lines(store)) == 1


async def test_a_busy_queue_never_fails_the_save(tmp_path):
    store = SkillStore(tmp_path)
    queue = PreferenceProposals(store)
    queue.propose("release_notes", RULE)
    with journal_lock(queue._lock) as held:
        assert held
        saved = await remember({"text": RULE, "when": WHEN, "proposal": "release_notes"}, ctx(store))
        dismissed = await forget({"proposal": "release_notes", "confirm": True}, ctx(store))
    assert not saved.is_error and saved.text.startswith("Saved [")
    assert [m.text for m in store.memories()] == [RULE]
    assert "The save stands, but learner proposal 'release_notes' was not closed" in saved.text
    assert "Forget, proposal='release_notes', confirm=true" in saved.text
    assert dismissed.is_error and "Not dismissed" in dismissed.text
    assert len(proposal_lines(store)) == 1
    # The suggested retry then closes it.
    out = await forget({"proposal": "release_notes", "confirm": True}, ctx(store))
    assert "Closed learner proposal" in out.text and proposal_lines(store) == []


async def test_scopes_a_session_closes_only_what_it_sees(tmp_path):
    store = SkillStore(tmp_path)
    queue = PreferenceProposals(store)
    queue.propose("release_notes", RULE, scope="global")
    queue.propose("release_notes", "Release notes for this repo list migrations first.", scope=PROJECT)
    queue.propose("release_notes", "Release notes in the other repo are generated.", scope=OTHER)
    # A session elsewhere sees (and closes) only the global one.
    out = await forget({"proposal": "release_notes", "confirm": True}, ctx(store, "project:third"))
    assert "(global)" in out.text and PROJECT not in out.text
    assert len(proposal_lines(store, PROJECT)) == 1 and len(proposal_lines(store, OTHER)) == 1
    # A session in the project closes its project's proposal, never another project's.
    out = await forget({"proposal": "release_notes", "confirm": True}, ctx(store, PROJECT))
    assert f"({PROJECT})" in out.text
    assert proposal_lines(store, PROJECT) == [] and len(proposal_lines(store, OTHER)) == 1


async def test_a_project_session_closes_both_lines_it_sees_for_the_topic(tmp_path):
    store = SkillStore(tmp_path)
    queue = PreferenceProposals(store)
    queue.propose("release_notes", RULE, scope="global")
    queue.propose("release_notes", "Release notes for this repo list migrations first.", scope=PROJECT)
    assert len(proposal_lines(store, PROJECT)) == 2
    out = await remember({"text": RULE, "when": WHEN, "proposal": "release_notes"}, ctx(store, PROJECT))
    assert "(global)" in out.text and f"({PROJECT})" in out.text
    assert proposal_lines(store, PROJECT) == [] and proposal_lines(store, OTHER) == []


class Answer:
    name = "fake"

    def __init__(self, text: str) -> None:
        self.text = text

    async def stream(self, request):
        yield StreamChunk("message_start", {"usage": {"input_tokens": 100}})
        yield StreamChunk("text_delta", {"text": self.text})
        yield StreamChunk("usage", {"usage": {"output_tokens": 3}, "stop_reason": "end_turn"})


async def learner_pass(store, said: str, proposed: str, topic: str = "release_notes") -> list[str]:
    store.journal_append({"kind": "user", "text": said})
    store.journal_append({"kind": "assistant", "text": "Understood."})
    synth = Synthesizer(Answer(f'<preference topic="{topic}">{proposed}</preference>'),
                        "gpt-test", store, min_new_entries=1)
    return await synth.run()


async def test_the_learner_does_not_requeue_a_closed_wording(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    store.journal_context(session_id="s1")
    outcomes: list[str] = []
    propose = PreferenceProposals.propose

    def spy(self, *args, **kwargs):
        outcomes.append(propose(self, *args, **kwargs))
        return outcomes[-1]
    monkeypatch.setattr(PreferenceProposals, "propose", spy)
    assert await learner_pass(store, RULE, RULE) == ["preference-proposal:release_notes"]
    out = await forget({"proposal": "release_notes", "confirm": True}, ctx(store, None))
    assert "Closed learner proposal" in out.text
    # The identical wording again: not queued, not reported as changed.
    assert await learner_pass(store, RULE, RULE) == []
    assert outcomes == ["proposed", "closed"], "the second pass reached the queue and was refused"
    assert proposal_lines(store, None) == []
    # A genuinely different correction on the topic is queued: it may be new.
    newer = "Release notes go in the changelog and the release page."
    assert await learner_pass(store, newer, newer) == ["preference-proposal:release_notes"]
    [line] = proposal_lines(store, None)
    assert newer in line


def test_tombstones_never_count_as_open_and_expire(tmp_path):
    store = SkillStore(tmp_path)
    queue = PreferenceProposals(store)
    now = time.time()
    for i, topic in enumerate(("release_notes", "ci_policy", "deploy")):
        queue.propose(topic, f"Rule number {i} for {topic}.", now=now)
    assert queue.propose("review", "Reviews use one reviewer.", now=now) == "full"
    assert queue.close("release_notes", None, how="dismissed", now=now)
    # The tombstone does not hold a slot, is not shown and is not counted.
    assert queue.propose("review", "Reviews use one reviewer.", now=now) == "proposed"
    assert len(queue.open(now=now)) == MAX_OPEN_PREFERENCE_PROPOSALS
    assert "release_notes" not in footer(store, None)
    signal = next(s for s in memory_health.check(store)["signals"] if s["id"] == "proposals")
    assert signal["detail"]["preference_proposals"] == 3
    assert "release_notes" not in signal["detail"]["topics"]
    # It blocks only its own topic, scope and wording, and only until its TTL.
    assert queue.propose("release_notes", "Rule number 0 for release_notes.", now=now) == "closed"
    assert queue.propose("release_notes", "Rule number 0 for release_notes.",
                         scope=PROJECT, now=now) == "full"
    later = now + PREFERENCE_PROPOSAL_TTL + 1
    queue.propose("deploy", "Deploys wait for approval.", now=later)
    rows = [json.loads(line) for line in (tmp_path / PREFERENCE_PROPOSALS_FILE).read_text().splitlines()]
    assert all("closed_at" not in r for r in rows), "an expired tombstone is pruned"


def test_rows_from_older_versions_still_work(tmp_path):
    store = SkillStore(tmp_path)
    row = {"ts": time.time(), "topic": "pref.release_notes", "text": RULE, "scope": "global"}
    (tmp_path / PREFERENCE_PROPOSALS_FILE).write_text(json.dumps(row) + "\n")
    queue = PreferenceProposals(store)
    assert [r["text"] for r in queue.open()] == [RULE]
    assert "[proposed, not saved; release_notes]" in footer(store, None)
    [closed] = queue.close("release_notes", None, how="saved")
    assert closed["closed"] == "saved" and queue.open() == []
    assert queue.propose("release_notes", RULE) == "closed"

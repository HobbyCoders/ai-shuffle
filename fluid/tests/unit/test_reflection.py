"""Memory v3 phase 2: one reflection per settled session, and the proposal inbox.

Reflection replaces the continuous learner by default. It runs once when the
chat's engine closes (Synthesizer.drain), behind a deterministic gate that
makes no model call when the session has no correction, no frustration, no
"remember this" and no repeated friction. Its proposals wait in an inbox
outside the fact store until a second session produces them or the owner
accepts them; quoted owner rules and preferences apply at once.
"""

from __future__ import annotations

import json

import pytest

from fluid.codemode import SkillStore, Synthesizer, reflection
from fluid.providers.base import StreamChunk

pytestmark = pytest.mark.memory_reflection
DAY = 86400.0


class Scripted:
    """A provider that answers every call with the next scripted text."""

    name = "fake"

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def stream(self, request):
        self.prompts.append(request.messages[0].content[0].text)
        self.systems.append(str(request.system))
        text = self.answers.pop(0) if self.answers else "NO_CHANGES"
        yield StreamChunk("message_start", {"usage": {"input_tokens": 100}})
        yield StreamChunk("text_delta", {"text": text})
        yield StreamChunk("usage", {"usage": {"output_tokens": 9}, "stop_reason": "end_turn"})


def log_rows(store) -> list[dict]:
    path = store.root / ".synthesis-log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def reflections(store) -> list[dict]:
    return [r for r in log_rows(store) if r.get("kind") == "reflection"]


def chat(store, session: str, turns: list[tuple[str, str]]) -> None:
    store.journal_context(session_id=session)
    for kind, text in turns:
        store.journal_append({"kind": kind, "text": text})


def learner(store, provider, session: str) -> Synthesizer:
    return Synthesizer(provider, "gpt-test", store, session_id=session, own_session=session)


CORRECTION = [
    ("user", "Add a health check to the web service."),
    ("assistant", "Added /healthz and restarted the service on port 8080."),
    ("user", "No, don't restart services without asking me first."),
    ("assistant", "Understood, I will ask before restarting anything."),
]


# ─── the gate ───────────────────────────────────────────────────────────────

def test_gate_needs_a_signal_and_makes_no_call_without_one():
    assert reflection.gate([{"kind": "user", "text": "Please add a README."},
                            {"kind": "assistant", "text": "Done."}]) == []
    # A first request that says "do not" is an instruction, not a correction.
    assert reflection.gate([{"kind": "user", "text": "Do not touch the tests; fix the parser."}]) == []
    assert reflection.gate([{"kind": "user", "text": "fix it"}, {"kind": "assistant", "text": "ok"},
                            {"kind": "user", "text": "No, that's wrong, use the other file"}]) == ["correction"]
    assert reflection.gate([{"kind": "user", "text": "Remember this: the staging box is 192.168.1.20"}]) == ["remember"]
    assert reflection.gate([{"kind": "user", "text": "ugh", "frustration": "profanity"}]) == ["frustration"]
    friction = {"kind": "friction", "signal": "repeat_work", "name": "Read", "text": "Read twice"}
    assert reflection.gate([friction]) == []
    assert reflection.gate([friction, dict(friction)]) == ["friction"]
    # A continued session's first new turn follows the reply reflected before.
    assert reflection.gate([{"kind": "user", "text": "No, wrong branch"}], continued=True) == ["correction"]


async def test_a_session_without_a_signal_is_skipped_with_no_model_call(tmp_path):
    store = SkillStore(tmp_path)
    chat(store, "ses_plain", [("user", "Add a README."), ("assistant", "Added README.md.")])
    provider = Scripted()
    await learner(store, provider, "ses_plain").drain()
    assert provider.prompts == []
    [row] = reflections(store)
    assert row["ran"] is False and row["outcome"] == "skipped" and row["reason"] == "no_signal"
    assert row["session"] == "ses_plain"
    # Marked reflected: the next close of the same quiet chat logs nothing new.
    await learner(store, provider, "ses_plain").drain()
    assert len(reflections(store)) == 1 and provider.prompts == []


# ─── one call per settled session ───────────────────────────────────────────

async def test_correction_reflects_once_saves_the_quoted_rule_and_queues_a_proposal(tmp_path):
    store = SkillStore(tmp_path)
    chat(store, "ses_a", CORRECTION)
    answer = (
        '<rule applies_to="all" quote="don\'t restart services without asking me first">'
        "When a change needs a service restart: ask the owner before restarting it.</rule>\n"
        '<proposal kind="owner_fact" scope="global" when="When working on the web service">'
        "The web service listens on port 8080.</proposal>"
    )
    provider = Scripted(answer)
    synth = learner(store, provider, "ses_a")
    synth.kick()  # a finished turn starts nothing in reflection mode
    assert provider.prompts == []
    await synth.drain()

    assert len(provider.prompts) == 1
    prompt = provider.prompts[0]
    assert "Why this chat was picked: correction" in prompt
    assert "don't restart services without asking me first" in prompt
    assert provider.systems[0].startswith("You read one finished chat")

    rules = [m for m in store.memories() if m.key and m.key.startswith("behavior.")]
    assert [r.text for r in rules] == ["When a change needs a service restart: ask the owner before restarting it."]
    # The proposal waits in the inbox: not a fact, so not in the index, JIT or Recall.
    assert not any("8080" in m.text for m in store.memories())
    assert "8080" not in (tmp_path / "memories.json").read_text()
    [proposal] = reflection.ProposalInbox(store.root).open()
    assert proposal["kind"] == "owner_fact" and proposal["sessions"] == ["ses_a"]
    assert proposal["expires_at"] - proposal["created_at"] == pytest.approx(14 * DAY)

    [row] = reflections(store)
    assert row["outcome"] == "ran" and row["signals"] == ["correction"]
    assert row["rules"] == 1 and row["proposed"] == 1 and row["model"] == "gpt-test"

    # The same chat closing again with nothing new makes no second call.
    await learner(store, provider, "ses_a").drain()
    assert len(provider.prompts) == 1


async def test_a_quoted_owner_preference_applies_at_once(tmp_path):
    store = SkillStore(tmp_path)
    chat(store, "ses_p", [("user", "Write the summary."), ("assistant", "Here it is, with headings."),
                          ("user", "No, I prefer plain paragraphs without headings in summaries.")])
    provider = Scripted('<proposal kind="owner_preference" quote="I prefer plain paragraphs without '
                        'headings in summaries">The owner prefers summaries as plain paragraphs '
                        "without headings.</proposal>")
    await learner(store, provider, "ses_p").drain()
    [fact] = [m for m in store.memories() if "plain paragraphs" in m.text]
    assert reflection.ProposalInbox(store.root).open() == []
    entry = next(e for e in store._facts.snapshot()["entries"] if e["id"] == fact.id)
    assert entry.get("durable") and not entry.get("protected")  # agent-saved, revertible


async def test_an_unquoted_preference_waits_in_the_inbox(tmp_path):
    store = SkillStore(tmp_path)
    chat(store, "ses_q", [("user", "Summarise it."), ("assistant", "Done."),
                          ("user", "No, shorter please.")])
    provider = Scripted('<proposal kind="owner_preference" quote="words the owner never wrote">'
                        "The owner likes short summaries.</proposal>")
    await learner(store, provider, "ses_q").drain()
    assert not any("short summaries" in m.text for m in store.memories())
    [proposal] = reflection.ProposalInbox(store.root).open()
    assert proposal["quote"] is None


async def test_a_second_distinct_session_corroborates_a_proposal_into_a_fact(tmp_path):
    store = SkillStore(tmp_path)
    fact = ('<proposal kind="project" scope="global" when="When building this project">'
            "The project builds with make release from the repository root.</proposal>")
    for session in ("ses_1", "ses_2"):
        chat(store, session, [("user", "Build it."), ("assistant", "Ran npm run build."),
                              ("user", "No, wrong command, use make release.")])
    first = Scripted(fact)
    await learner(store, first, "ses_1").drain()
    assert reflection.ProposalInbox(store.root).open()
    assert not any("make release" in m.text for m in store.memories())

    second = Scripted(fact.replace("builds with", "is built with"))
    await learner(store, second, "ses_2").drain()
    [saved] = [m for m in store.memories() if "make release" in m.text]
    assert reflection.ProposalInbox(store.root).open() == []
    entry = next(e for e in store._facts.snapshot()["entries"] if e["id"] == saved.id)
    assert entry.get("durable") and not entry.get("protected")
    assert reflections(store)[-1]["corroborated"] == 1


async def test_the_same_session_twice_does_not_corroborate(tmp_path):
    store = SkillStore(tmp_path)
    proposal = '<proposal kind="owner_fact">The owner keeps backups on the home server.</proposal>'
    chat(store, "ses_x", CORRECTION)
    await learner(store, Scripted(proposal), "ses_x").drain()
    chat(store, "ses_x", [("user", "No, that's wrong again.")])
    await learner(store, Scripted(proposal), "ses_x").drain()
    assert not any("backups" in m.text for m in store.memories())
    [row] = reflection.ProposalInbox(store.root).open()
    assert row["sessions"] == ["ses_x"]


async def test_new_turns_after_a_reflection_are_reflected_alone(tmp_path):
    store = SkillStore(tmp_path)
    chat(store, "ses_c", CORRECTION)
    provider = Scripted("NO_CHANGES", "NO_CHANGES")
    await learner(store, provider, "ses_c").drain()
    chat(store, "ses_c", [("user", "Now add a metrics endpoint."), ("assistant", "Added /metrics."),
                          ("user", "No, put it behind the auth middleware.")])
    await learner(store, provider, "ses_c").drain()
    assert len(provider.prompts) == 2
    assert "auth middleware" in provider.prompts[1]
    assert "health check" not in provider.prompts[1]


async def test_reflection_reads_only_its_own_session_and_no_tool_stream(tmp_path):
    store = SkillStore(tmp_path)
    chat(store, "ses_other", [("user", "Secret project codename is BLUEFIN."), ("assistant", "ok")])
    chat(store, "ses_me", CORRECTION)
    store.journal_append({"kind": "cell", "code": "print(Read(file_path='TOOLSTREAM.py'))", "stdout": "x"})
    provider = Scripted("NO_CHANGES")
    await learner(store, provider, "ses_me").drain()
    assert "BLUEFIN" not in provider.prompts[0]
    assert "TOOLSTREAM" not in provider.prompts[0]


async def test_learning_off_makes_no_call(tmp_path):
    store = SkillStore(tmp_path)
    chat(store, "ses_off", CORRECTION)
    provider = Scripted()
    synth = Synthesizer(provider, "gpt-test", store, session_id="ses_off", own_session="ses_off",
                        enabled=lambda: False)
    await synth.drain()
    assert provider.prompts == []


async def test_input_is_bounded(tmp_path):
    store = SkillStore(tmp_path)
    turns = []
    for i in range(80):
        turns += [("user", f"Turn {i}: " + "word " * 400), ("assistant", "reply " * 300)]
    turns.append(("user", "No, that's wrong."))
    chat(store, "ses_long", turns)
    provider = Scripted("NO_CHANGES")
    await learner(store, provider, "ses_long").drain()
    assert len(provider.prompts[0]) <= reflection.REFLECTION_INPUT_CHARS + 200
    assert "No, that's wrong." in provider.prompts[0]
    assert "earlier lines of this chat left out" in provider.prompts[0]


# ─── the inbox ──────────────────────────────────────────────────────────────

def test_proposals_expire_after_fourteen_days(tmp_path):
    inbox = reflection.ProposalInbox(tmp_path)
    row, corroborated = inbox.propose(kind="owner_fact", text="The owner uses a standing desk.",
                                      scope="global", session="s1", now=1000.0)
    assert not corroborated and inbox.open(now=1000.0 + 13 * DAY)
    assert inbox.open(now=1000.0 + 14 * DAY + 1) == []
    inbox.propose(kind="owner_fact", text="Something else entirely here.", scope="global",
                  session="s2", now=1000.0 + 15 * DAY)
    stored = json.loads((tmp_path / reflection.PROPOSAL_FILE).read_text())
    assert [p["text"] for p in stored["proposals"]] == ["Something else entirely here."]


def test_dismissed_proposals_leave_the_inbox(tmp_path):
    inbox = reflection.ProposalInbox(tmp_path)
    row, _ = inbox.propose(kind="project", text="Tests live under tests/unit.", scope="global", session="s1")
    inbox.decide(row["id"], reflection.DISMISSED)
    assert inbox.open() == []
    with pytest.raises(KeyError):
        inbox.decide(row["id"], reflection.ACCEPTED)
    # Another session producing it again does not reopen or promote it.
    again, corroborated = inbox.propose(kind="project", text="Tests live under tests/unit.",
                                        scope="global", session="s2")
    assert again["status"] == reflection.DISMISSED and not corroborated and inbox.open() == []


async def test_a_dismissed_proposal_is_not_saved_when_another_chat_repeats_it(tmp_path):
    store = SkillStore(tmp_path)
    proposal = '<proposal kind="owner_fact">The owner keeps backups on the home server.</proposal>'
    chat(store, "ses_1", CORRECTION)
    await learner(store, Scripted(proposal), "ses_1").drain()
    inbox = reflection.ProposalInbox(store.root)
    [row] = inbox.open()
    inbox.decide(row["id"], reflection.DISMISSED)
    chat(store, "ses_2", CORRECTION)
    await learner(store, Scripted(proposal), "ses_2").drain()
    assert not any("backups" in m.text for m in store.memories())
    assert inbox.open() == [] and reflections(store)[-1]["proposed"] == 0


def test_another_workers_proposal_corroborates_and_its_decisions_apply(tmp_path):
    here = reflection.ProposalInbox(tmp_path / "here")
    there = reflection.ProposalInbox(tmp_path / "there")
    remote_row, _ = there.propose(kind="owner_fact", text="The owner's NAS is at 192.168.1.20.",
                                  scope="global", session="s_remote")
    here.apply_remote("dev-other", there.export())
    [merged] = here.open()
    assert merged["sessions"] == ["s_remote"]
    row, corroborated = here.propose(kind="owner_fact", text="The owner's NAS is at 192.168.1.20.",
                                     scope="global", session="s_local")
    assert corroborated and set(row["sessions"]) == {"s_remote", "s_local"}

    there.decide(remote_row["id"], reflection.DISMISSED)
    here.apply_remote("dev-other", there.export())
    assert here.open() == []
    here.apply_remote("dev-other", None)
    assert [r["sessions"] for r in here.open()] == [["s_local"]]


def test_rollback_switch_restores_the_continuous_learner(monkeypatch):
    assert reflection.learner_mode() == reflection.REFLECTION
    monkeypatch.setenv(reflection.LEARNER_SWITCH, "continuous")
    assert reflection.learner_mode() == reflection.CONTINUOUS and not reflection.reflection_mode()
    monkeypatch.setenv(reflection.LEARNER_SWITCH, "anything else")
    assert reflection.reflection_mode()

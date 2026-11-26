"""Memory v3 phase 2: one reflection per settled session, and the proposal inbox.

Reflection replaces the continuous learner by default. It runs once when the
chat's engine closes (Synthesizer.drain), behind a deterministic gate that
makes no model call when the session has no correction, no frustration, no
"remember this" and no repeated friction. Its proposals wait in an inbox
outside the fact store until the owner accepts them (or they expire); quoted
owner rules and preferences apply at once as learned behaviour rules.
"""

from __future__ import annotations

import asyncio
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


async def test_a_quoted_owner_preference_is_a_learned_behaviour_rule(tmp_path):
    store = SkillStore(tmp_path)
    chat(store, "ses_p", [("user", "How did the deploy go?"), ("assistant", "Here is a long report..."),
                          ("user", "No, I prefer short replies on status checks.")])
    provider = Scripted('<proposal kind="owner_preference" quote="I prefer short replies on status checks" '
                        'when="When answering a status check">Keep the reply short.</proposal>')
    await learner(store, provider, "ses_p").drain()
    assert reflection.ProposalInbox(store.root).open() == []
    [rule] = [m for m in store.memories() if m.key and m.key.startswith("behavior.")]
    assert rule.text == "When answering a status check: Keep the reply short."
    entry = next(e for e in store._facts.snapshot()["entries"] if e["id"] == rule.id)
    assert not entry.get("protected")  # learned, revertible from Learned behaviour
    [row] = reflections(store)
    assert row["rules"] == 1 and row["proposed"] == 0


async def test_a_preference_the_rule_checks_refuse_waits_for_the_owner(tmp_path):
    store = SkillStore(tmp_path)
    chat(store, "ses_g", [("user", "Run it."), ("assistant", "Shall I deploy?"),
                          ("user", "No, I prefer you just deploy.")])
    provider = Scripted('<proposal kind="owner_preference" quote="I prefer you just deploy">'
                        "Deploy without asking for approval.</proposal>")
    await learner(store, provider, "ses_g").drain()
    assert not any(m.key and m.key.startswith("behavior.") for m in store.memories())
    [proposal] = reflection.ProposalInbox(store.root).open()
    assert proposal["kind"] == "owner_preference"


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


async def test_the_same_proposal_from_two_chats_is_one_row_and_is_never_saved(tmp_path):
    store = SkillStore(tmp_path)
    fact = ('<proposal kind="project" scope="global" when="When building this project">'
            "The project builds with make release from the repository root.</proposal>")
    for session in ("ses_1", "ses_2"):
        chat(store, session, [("user", "Build it."), ("assistant", "Ran npm run build."),
                              ("user", "No, wrong command, use make release.")])
    await learner(store, Scripted(fact), "ses_1").drain()
    # The same words, cased and punctuated differently.
    await learner(store, Scripted(fact.replace("The project builds", "the project  BUILDS")
                                  .replace("root.", "root")), "ses_2").drain()
    [row] = reflection.ProposalInbox(store.root).open()
    assert row["sessions"] == ["ses_1", "ses_2"]
    assert not any("make release" in m.text for m in store.memories())


async def test_contradicting_proposals_stay_apart(tmp_path):
    store = SkillStore(tmp_path)
    for session, claim in (("ses_1", "Staging builds run migrations."),
                           ("ses_2", "Staging builds do not run migrations.")):
        chat(store, session, CORRECTION)
        await learner(store, Scripted(f'<proposal kind="project" scope="global">{claim}</proposal>'),
                      session).drain()
    rows = reflection.ProposalInbox(store.root).open()
    assert sorted(r["text"] for r in rows) == ["Staging builds do not run migrations.",
                                                "Staging builds run migrations."]
    assert all(len(r["sessions"]) == 1 for r in rows)
    assert not any("migrations" in m.text for m in store.memories())


async def test_the_same_session_twice_is_listed_once(tmp_path):
    store = SkillStore(tmp_path)
    proposal = '<proposal kind="owner_fact">The owner keeps backups on the home server.</proposal>'
    chat(store, "ses_x", CORRECTION)
    await learner(store, Scripted(proposal), "ses_x").drain()
    chat(store, "ses_x", [("user", "No, that's wrong again.")])
    await learner(store, Scripted(proposal), "ses_x").drain()
    [row] = reflection.ProposalInbox(store.root).open()
    assert row["sessions"] == ["ses_x"]


async def test_concurrent_reflections_of_one_chat_pay_for_one_call_and_keep_the_loop_free(tmp_path):
    store = SkillStore(tmp_path)
    chat(store, "ses_dup", CORRECTION)

    class Slow(Scripted):
        async def stream(self, request):
            await asyncio.sleep(0.2)
            async for chunk in super().stream(request):
                yield chunk

    provider = Slow("NO_CHANGES", "NO_CHANGES")
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(ticker())
    try:
        await asyncio.gather(learner(store, provider, "ses_dup").drain(),
                             learner(store, provider, "ses_dup").drain())
    finally:
        beat.cancel()
    # The second waited for the first, then read the session's watermark again.
    assert len(provider.prompts) == 1
    assert ticks >= 10  # the event loop kept running while both waited


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

def sync(here, there, device="dev-other"):
    """``there``'s published record applied on ``here`` (app.network.memory_records)."""
    exported = there.export()
    here.apply_remote(device, exported["proposals"], exported["decisions"])


def test_proposals_expire_after_fourteen_days(tmp_path):
    inbox = reflection.ProposalInbox(tmp_path)
    row = inbox.propose(kind="owner_fact", text="The owner uses a standing desk.",
                        scope="global", session="s1", now=1000.0)
    assert inbox.open(now=1000.0 + 13 * DAY)
    assert inbox.open(now=1000.0 + 14 * DAY + 1) == []
    with pytest.raises(reflection.ProposalDecided):
        inbox.dismiss(row["id"], now=1000.0 + 14 * DAY + 1)
    inbox.propose(kind="owner_fact", text="Something else entirely here.", scope="global",
                  session="s2", now=1000.0 + 15 * DAY)
    stored = json.loads((tmp_path / reflection.PROPOSAL_FILE).read_text())
    assert [p["text"] for p in stored["proposals"]] == ["Something else entirely here."]


def test_an_expired_proposal_comes_back_fresh(tmp_path):
    inbox = reflection.ProposalInbox(tmp_path)
    first = inbox.propose(kind="owner_fact", text="The owner uses a standing desk.",
                          scope="global", session="s1", now=1000.0)
    later = 1000.0 + 20 * DAY
    again = inbox.propose(kind="owner_fact", text="The owner uses a standing desk.",
                          scope="global", session="s2", now=later)
    assert again["id"] == first["id"] and again["sessions"] == ["s2"]
    assert again["created_at"] == later and again["expires_at"] == later + 14 * DAY
    # Another worker still holding the expired row does not revive its sessions.
    other = reflection.ProposalInbox(tmp_path / "other")
    other.propose(kind="owner_fact", text="The owner uses a standing desk.", scope="global",
                  session="s_old", now=1000.0)
    sync(inbox, other)
    [row] = inbox.open(now=later)
    assert row["sessions"] == ["s2"]


def test_a_dismissal_is_permanent(tmp_path):
    inbox = reflection.ProposalInbox(tmp_path)
    row = inbox.propose(kind="project", text="Tests live under tests/unit.", scope="global",
                        session="s1", now=1000.0)
    inbox.dismiss(row["id"], now=1000.0)
    assert inbox.open() == []
    with pytest.raises(reflection.ProposalDecided):
        inbox.accept(row["id"], lambda r: pytest.fail("a dismissed proposal was saved"))
    # Long after the 14 days, and after other writes, the same proposal stays gone.
    much_later = 1000.0 + 400 * DAY
    inbox.propose(kind="owner_fact", text="Unrelated fact here.", scope="global", session="s3",
                  now=much_later)
    again = inbox.propose(kind="project", text="tests live under TESTS/UNIT", scope="global",
                          session="s2", now=much_later)
    assert again["status"] == reflection.DISMISSED
    assert [r["text"] for r in inbox.open(now=much_later)] == ["Unrelated fact here."]
    stored = json.loads((tmp_path / reflection.PROPOSAL_FILE).read_text())
    assert stored["decisions"][row["id"]]["status"] == reflection.DISMISSED


async def test_a_dismissed_proposal_is_not_queued_when_another_chat_repeats_it(tmp_path):
    store = SkillStore(tmp_path)
    proposal = '<proposal kind="owner_fact">The owner keeps backups on the home server.</proposal>'
    chat(store, "ses_1", CORRECTION)
    await learner(store, Scripted(proposal), "ses_1").drain()
    inbox = reflection.ProposalInbox(store.root)
    [row] = inbox.open()
    inbox.dismiss(row["id"])
    chat(store, "ses_2", CORRECTION)
    await learner(store, Scripted(proposal), "ses_2").drain()
    assert not any("backups" in m.text for m in store.memories())
    assert inbox.open() == [] and reflections(store)[-1]["proposed"] == 0


def test_a_dismissal_on_another_worker_is_permanent_here_too(tmp_path):
    here = reflection.ProposalInbox(tmp_path / "here")
    there = reflection.ProposalInbox(tmp_path / "there")
    text = "The owner's NAS is at 192.168.1.20."
    remote_row = there.propose(kind="owner_fact", text=text, scope="global", session="s_remote")
    sync(here, there)
    [merged] = here.open()
    assert merged["sessions"] == ["s_remote"]
    row = here.propose(kind="owner_fact", text=text, scope="global", session="s_local")
    assert set(row["sessions"]) == {"s_remote", "s_local"} and row["status"] == reflection.OPEN
    assert not (tmp_path / "here" / "memories.json").exists()

    there.dismiss(remote_row["id"])
    sync(here, there)
    assert here.open() == []
    # The other worker's record goes away entirely: the dismissal stays here.
    here.apply_remote("dev-other", None)
    assert here.open() == []
    again = here.propose(kind="owner_fact", text=text, scope="global", session="s_new")
    assert again["status"] == reflection.DISMISSED and here.open() == []
    # And it travels on: a third worker learns it from this one's record.
    third = reflection.ProposalInbox(tmp_path / "third")
    third.propose(kind="owner_fact", text=text, scope="global", session="s_third")
    sync(third, here, device="dev-here")
    assert third.open() == []


def test_accept_is_one_transition(tmp_path):
    inbox = reflection.ProposalInbox(tmp_path)
    row = inbox.propose(kind="owner_fact", text="The owner uses a standing desk.",
                        scope="global", session="s1")
    with pytest.raises(RuntimeError):
        inbox.accept(row["id"], lambda r: (_ for _ in ()).throw(RuntimeError("store full")))
    assert inbox.open()  # a failed save leaves it open
    saves = []
    decided, _ = inbox.accept(row["id"], lambda r: saves.append(r["id"]) or "saved")
    assert decided["status"] == reflection.ACCEPTED and saves == [row["id"]]
    with pytest.raises(reflection.ProposalDecided):
        inbox.accept(row["id"], lambda r: saves.append("again"))
    with pytest.raises(reflection.ProposalDecided):
        inbox.dismiss(row["id"])
    with pytest.raises(KeyError):
        inbox.dismiss("p000000000000000")
    assert saves == [row["id"]]


def test_accept_loses_to_a_dismissal_replicated_first(tmp_path):
    here = reflection.ProposalInbox(tmp_path / "here")
    there = reflection.ProposalInbox(tmp_path / "there")
    row = there.propose(kind="owner_fact", text="The owner uses a standing desk.",
                        scope="global", session="s1")
    sync(here, there)
    there.dismiss(row["id"])
    sync(here, there)
    with pytest.raises(reflection.ProposalDecided):
        here.accept(row["id"], lambda r: pytest.fail("saved after a dismissal"))


def test_a_writer_that_cannot_take_the_lock_writes_nothing(tmp_path, monkeypatch):
    from fluid.codemode.skills import journal_lock

    monkeypatch.setattr(reflection, "LOCK_WAIT_SECONDS", 0.05)
    inbox = reflection.ProposalInbox(tmp_path)
    row = inbox.propose(kind="owner_fact", text="The owner uses a standing desk.",
                        scope="global", session="s1")
    before = (tmp_path / reflection.PROPOSAL_FILE).read_text()
    state = reflection.ReflectionState(tmp_path)
    with journal_lock(tmp_path / reflection.PROPOSAL_LOCK) as held:
        assert held
        with pytest.raises(reflection.InboxBusy):
            inbox.propose(kind="owner_fact", text="Another fact.", scope="global", session="s2")
        with pytest.raises(reflection.InboxBusy):
            inbox.accept(row["id"], lambda r: pytest.fail("saved without the lock"))
        with pytest.raises(reflection.InboxBusy):
            inbox.apply_remote("dev-other", [], {row["id"]: {"status": "dismissed", "at": 1.0}})
    assert (tmp_path / reflection.PROPOSAL_FILE).read_text() == before
    assert not (tmp_path / reflection.REMOTE_PROPOSAL_FILE).exists()
    with journal_lock(tmp_path / reflection.STATE_LOCK) as held:
        assert held
        with pytest.raises(reflection.InboxBusy):
            state.mark("s1", 5.0)
    assert state.through("s1") == 0.0


def test_rollback_switch_restores_the_continuous_learner(monkeypatch):
    assert reflection.learner_mode() == reflection.REFLECTION
    monkeypatch.setenv(reflection.LEARNER_SWITCH, "continuous")
    assert reflection.learner_mode() == reflection.CONTINUOUS and not reflection.reflection_mode()
    monkeypatch.setenv(reflection.LEARNER_SWITCH, "anything else")
    assert reflection.reflection_mode()

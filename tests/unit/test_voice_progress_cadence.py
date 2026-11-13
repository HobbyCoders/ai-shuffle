"""Long work is not dead air: the chat's progress sentence is said once, briefly.

Owner's call, 2026-11-12: the chat worked 36 s in silence before its reply. Now
the chat's progress sentence is said as one short sentence, no sooner than
PROGRESS_FIRST_S after the handoff, PROGRESS_GAP_S apart, only at a pause, never
after the result, and never once the caller asked for fewer updates.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import voice_progress
from app.core.voice_progress import (
    PROGRESS_FIRST_S,
    PROGRESS_GAP_S,
    RESULT_NOTE,
    SPOKEN_NOTE_PREFIX,
    SPOKEN_PROGRESS_NOTE,
    VoiceProgress,
    note_kind,
)
from app.core.voice_status import (
    is_status_question,
    progress_preference,
    wants_fewer_updates,
)


def event(kind, payload=None, turn="t"):
    return {"event_type": kind, "turn_id": turn, "payload": payload or {}}


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    # Module-local clock: patching stdlib time.monotonic would freeze the loop clock too.
    monkeypatch.setattr(voice_progress, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def spoken(send):
    return [c.args[0]["content"] for c in send.await_args_list if c.args[0]["type"] == "session.commentary.append"]


def progress_lines(send):
    return [text for text in spoken(send) if SPOKEN_PROGRESS_NOTE in text]


def says(p, text):
    """The chat writes a progress sentence, then calls a tool."""
    p.consume(event("assistant.text_delta", {"text": text}))
    p.consume(event("tool.use", {"name": "Bash", "id": "tool"}))


async def call(clock, *, quiet=True):
    send = AsyncMock()
    pause = SimpleNamespace(value=quiet)
    p = VoiceProgress("s", send, quiet=lambda: pause.value, append_bytes=None)
    await p.submitted("t", "d")
    p.consume(event("turn.started"))
    return p, send, pause


async def at(clock, p, seconds):
    clock[0] = 100.0 + seconds
    await p.flush()


@pytest.mark.asyncio
async def test_progress_is_said_once_after_the_first_wait_then_spaced(clock):
    p, send, _ = await call(clock)
    says(p, "I'm reading through the test logs now.")
    await at(clock, p, 1)
    await at(clock, p, PROGRESS_FIRST_S - 1)
    assert progress_lines(send) == []  # Too soon after the handoff.
    await at(clock, p, PROGRESS_FIRST_S + 0.5)
    (line,) = progress_lines(send)
    assert line == SPOKEN_NOTE_PREFIX + SPOKEN_PROGRESS_NOTE + "I'm reading through the test logs now."
    # The same sentence is never said twice.
    await at(clock, p, PROGRESS_FIRST_S + PROGRESS_GAP_S + 5)
    assert len(progress_lines(send)) == 1
    # A new sentence waits out the gap from the last one said.
    says(p, "Now I'm checking the failing upload test.")
    await at(clock, p, PROGRESS_FIRST_S + PROGRESS_GAP_S + 6)
    assert len(progress_lines(send)) == 2
    says(p, "Now the type check.")
    await at(clock, p, PROGRESS_FIRST_S + PROGRESS_GAP_S + 20)
    assert len(progress_lines(send)) == 2
    await at(clock, p, 2 * PROGRESS_GAP_S + PROGRESS_FIRST_S + 7)
    assert len(progress_lines(send)) == 3


@pytest.mark.asyncio
async def test_progress_waits_for_a_pause(clock):
    p, send, pause = await call(clock, quiet=False)
    says(p, "I'm reading the logs.")
    await at(clock, p, PROGRESS_FIRST_S + 5)
    assert progress_lines(send) == []
    pause.value = True
    await at(clock, p, PROGRESS_FIRST_S + 6)
    assert len(progress_lines(send)) == 1


@pytest.mark.asyncio
async def test_a_finished_turn_drops_a_pending_progress_line(clock):
    p, send, pause = await call(clock, quiet=False)
    says(p, "I'm reading the logs.")
    await at(clock, p, PROGRESS_FIRST_S + 5)
    p.consume(event("assistant.text_delta", {"text": "One test is flaky."}))
    p.consume(event("turn.completed"))
    pause.value = True
    for seconds in (PROGRESS_FIRST_S + 6, PROGRESS_FIRST_S + 40, PROGRESS_FIRST_S + 80):
        await at(clock, p, seconds)
    assert progress_lines(send) == []
    assert spoken(send) == [SPOKEN_NOTE_PREFIX + RESULT_NOTE + "One test is flaky."]


@pytest.mark.asyncio
async def test_a_waiting_result_goes_first_and_progress_never_follows_it(clock):
    p, send, _ = await call(clock)
    says(p, "I'm reading the logs.")
    p.note("question", "A question for the caller.", urgent=True)
    await at(clock, p, PROGRESS_FIRST_S + 1)
    assert progress_lines(send) == []
    assert spoken(send)[-1].endswith("A question for the caller.")


@pytest.mark.asyncio
async def test_asking_for_fewer_updates_keeps_progress_quiet_for_the_call(clock):
    p, send, _ = await call(clock)
    p.heard("That's fine, just tell me when it's done.")
    says(p, "I'm reading the logs.")
    for seconds in (PROGRESS_FIRST_S + 1, PROGRESS_FIRST_S + PROGRESS_GAP_S + 1):
        await at(clock, p, seconds)
    assert progress_lines(send) == []
    # A later request on the same call stays quiet too; the result is still told.
    await p.submitted("t2", "d2")
    p.consume(event("turn.started", turn="t2"))
    p.consume(event("assistant.text_delta", {"text": "Checking again."}, turn="t2"))
    p.consume(event("tool.use", {"name": "Bash", "id": "x"}, turn="t2"))
    await at(clock, p, 200)
    assert progress_lines(send) == []
    p.consume(event("assistant.text_delta", {"text": "All green."}, turn="t2"))
    p.consume(event("turn.completed", turn="t2"))
    await at(clock, p, 210)
    assert spoken(send)[-1] == SPOKEN_NOTE_PREFIX + RESULT_NOTE + "All green."


@pytest.mark.asyncio
async def test_without_a_pause_signal_progress_stays_quiet_context(clock):
    send = AsyncMock()
    p = VoiceProgress("s", send, append_bytes=None)
    await p.submitted("t", "d")
    p.consume(event("turn.started"))
    says(p, "I'm reading the logs.")
    await at(clock, p, PROGRESS_FIRST_S + 30)
    assert progress_lines(send) == []


@pytest.mark.asyncio
async def test_check_work_includes_the_latest_progress_while_work_runs(clock):
    p, _, _ = await call(clock)
    says(p, "I'm reading the logs.")
    text = p.check_work_text("You are working on a request.", {})
    assert "unfinished and not a result: I'm reading the logs." in text
    p.consume(event("assistant.text_delta", {"text": "Done."}))
    p.consume(event("turn.completed"))
    assert "I'm reading the logs." not in p.check_work_text("Nothing is running.", {})


@pytest.mark.parametrize("text", [
    "Just tell me when it's done.",
    "ok only let me know once it's finished",
    "Stop the updates please.",
    "No more progress updates.",
    "Fewer updates, please.",
    "You don't need to keep telling me every step",
    "Thanks, that's fine, just tell me when it is ready.",
])
def test_fewer_updates_phrases(text):
    assert wants_fewer_updates(text)


@pytest.mark.parametrize("text", [
    "Is it done yet?",
    "What are you working on?",
    "Tell me when you've read the logs what they say.",
    "Update the docs when it's done.",
    "Can you stop the server?",
    # Review of PR #1541: work requests that say "update", and a negation.
    "Stop the update to the software and run the tests instead",
    "Do not stop the updates; keep me informed",
    "Stop the updates to the config file and revert them.",
    "Cancel the system update and reboot the box.",
    "Skip the updates for the staging server.",
    "Pull the latest updates and rebuild.",
])
def test_not_fewer_updates(text):
    assert not wants_fewer_updates(text)


@pytest.mark.parametrize("text", [
    "Keep me posted.",
    "keep me updated please",
    "Give me updates again.",
    "Tell me how it's going as you go.",
    "Do not stop the updates; keep me informed",
    "Actually, never mind, keep me in the loop.",
])
def test_more_updates_phrases(text):
    assert progress_preference(text) == "more"


@pytest.mark.parametrize("text", [
    "Update the docs when it's done.",
    "Pull the latest updates and rebuild.",
    "Is it done yet?",
    "Keep the updated config and post it to the channel.",
])
def test_no_progress_preference(text):
    assert progress_preference(text) is None


def test_the_last_stated_preference_wins():
    assert progress_preference("Keep me posted. Actually, just tell me when it's done.") == "fewer"
    assert progress_preference("Stop the updates. No wait, keep me posted.") == "more"


@pytest.mark.asyncio
async def test_a_work_request_saying_update_does_not_mute_progress(clock):
    p, send, _ = await call(clock)
    p.heard("Stop the update to the software and run the tests instead")
    says(p, "I'm stopping the update.")
    await at(clock, p, PROGRESS_FIRST_S + 1)
    assert len(progress_lines(send)) == 1


@pytest.mark.asyncio
async def test_keep_me_posted_turns_progress_back_on(clock):
    p, send, _ = await call(clock)
    p.heard("Just tell me when it's done.")
    says(p, "I'm reading the logs.")
    await at(clock, p, PROGRESS_FIRST_S + 1)
    assert progress_lines(send) == []
    p.heard("Actually, keep me posted.")
    await at(clock, p, PROGRESS_FIRST_S + 2)
    assert len(progress_lines(send)) == 1
    assert progress_lines(send)[0].endswith("I'm reading the logs.")


@pytest.mark.asyncio
async def test_a_growing_caption_is_judged_whole(clock):
    """"Stop the updates" as a partial caption of "Stop the updates to the
    config file" must not stick once the caption grows into a work request."""
    p, send, _ = await call(clock)
    p.heard("Stop the updates")
    assert p.fewer_updates
    p.heard("Stop the updates to the config file and revert them.")
    assert not p.fewer_updates
    says(p, "I'm reverting the config.")
    await at(clock, p, PROGRESS_FIRST_S + 1)
    assert len(progress_lines(send)) == 1


def test_status_questions_are_not_fewer_update_requests():
    assert is_status_question("Is it done yet?")
    assert not wants_fewer_updates("Is it done yet?")


def test_note_kind_never_needs_the_text():
    assert note_kind(SPOKEN_NOTE_PREFIX + RESULT_NOTE + "secret words") == "result"
    assert note_kind(SPOKEN_NOTE_PREFIX + SPOKEN_PROGRESS_NOTE + "x") == "progress"
    assert note_kind("anything else") == "other"


def test_progress_note_says_it_is_unfinished_and_asks_for_one_sentence():
    assert "unfinished work, not a result" in SPOKEN_PROGRESS_NOTE
    assert "one short sentence of your own words" in SPOKEN_PROGRESS_NOTE

"""Grok call state under review (PR #1541): progress is ephemeral, handoffs and
status answers are counted once, and check_work at idle never loses a request.

Owner's tests on real Grok, 2026-11-12: "Check whether the deploy finished."
went to check_work 3/3 with nothing running, and the voice said there was no
deploy status instead of handing the request off. Now the app takes such a
check_work call as the handoff itself.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosed

from app.core import voice_progress, voice_status, voice_stream
from app.core.voice_grok import (
    APP_NOTE_LEAD,
    CHECK_WORK_HANDED_OFF,
    CHECK_WORK_JUST_HANDED_OFF,
    CHECK_WORK_NEW_REQUEST,
    HANDOFF_RECEIPT,
    HANDOFF_TOOL,
    GrokVoiceStream,
)
from app.core.voice_models import SUPERGROK_MODEL
from app.core.voice_progress import (
    PROGRESS_FIRST_S,
    RESULT_NOTE,
    SPOKEN_PROGRESS_NOTE,
    STATUS_NOTE,
    VoiceProgress,
)
from app.core.voice_remote import command_id
from app.core.voice_stream import ALREADY_HANDED_OFF_NOTE, STATUS_TOLD_NOTE
from tests.unit.test_voice_stream import Upstream

IDLE = {"chat": "idle", "queued_requests": 0, "running": [], "finished_waiting": [],
        "approvals_waiting": 0, "on_screen_waiting": 0}
WORKING = {**IDLE, "chat": "working"}


def event(kind, payload=None, turn="t"):
    return {"event_type": kind, "turn_id": turn, "payload": payload or {}}


def sent(upstream):
    return [json.loads(e) for e in upstream.sent]


def outputs(upstream):
    return [(e["item"]["call_id"], e["item"]["output"]) for e in sent(upstream)
            if e["type"] == "conversation.item.create" and e["item"]["type"] == "function_call_output"]


def user_notes(upstream):
    return [e["item"]["content"][0]["text"].removeprefix(APP_NOTE_LEAD) for e in sent(upstream)
            if e["type"] == "conversation.item.create" and e["item"]["type"] == "message"
            and e["item"]["role"] == "user"]


def responses(upstream):
    return sum(e["type"] == "response.create" for e in sent(upstream))


def grok():
    call = GrokVoiceStream(AsyncMock(), {"id": "s"}, {"model": SUPERGROK_MODEL, "voice": "eve"}, "secret", 5)
    call.upstream = Upstream([])
    return call


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    # Module-local clock: patching stdlib time.monotonic would freeze the loop clock too.
    monkeypatch.setattr(voice_progress, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


class Dropping(Upstream):
    """A provider socket that has dropped: every send fails."""
    async def send(self, event):
        raise ConnectionClosed(None, None)


# ---------------------------------------------------------------------------
# S1: a progress line that cannot be sent is dropped, never replayed after work
# ---------------------------------------------------------------------------

async def progress_then_drop_then_result(call, clock):
    call.progress = progress = VoiceProgress("s", call.send, quiet=call.is_quiet, append_bytes=call.append_bytes)
    call.last_caption_at = time.monotonic() - 10  # Nobody has spoken: the real is_quiet() says pause.
    await progress.submitted("t", "tool-1")
    progress.consume(event("turn.started"))
    progress.consume(event("assistant.text_delta", {"text": "I'm reading the deploy logs now."}))
    progress.consume(event("tool.use", {"name": "Bash", "id": "x"}))
    call.upstream = Dropping([])
    clock[0] = 100.0 + PROGRESS_FIRST_S + 1
    await progress.flush()
    assert progress.progress_said == "I'm reading the deploy logs now."  # It was due and attempted.
    assert list(call.held) == []  # Dropped, not held for the next session.
    # The reader restores the session; the work finishes meanwhile.
    call.restoring = True
    progress.consume(event("assistant.text_delta", {"text": "The deploy finished."}))
    progress.consume(event("turn.completed"))
    clock[0] += 5
    await progress.flush()  # Not a pause while restoring: the result waits...
    clock[0] += voice_progress.PAUSE_MAX_WAIT + 1
    await progress.flush()  # ...until the pause wait runs out, then is held.
    assert len(call.held) == 1  # The result is durable.
    call.upstream, call.restoring = Upstream([]), False
    await call.send_held()
    return call.upstream


@pytest.mark.asyncio
async def test_grok_a_failed_progress_line_is_never_replayed_ahead_of_the_result(clock):
    call = grok()
    call.pending_tools.add("tool-1")
    replayed = await progress_then_drop_then_result(call, clock)
    call.pending_tools = set()  # start_live resets the replaced session's calls.
    assert user_notes(replayed) == [voice_progress.SPOKEN_NOTE_PREFIX + RESULT_NOTE + "The deploy finished."]
    assert not any(SPOKEN_PROGRESS_NOTE in text for text in user_notes(replayed))
    assert responses(replayed) == 1


@pytest.mark.asyncio
async def test_gpt_live_a_failed_progress_line_is_never_replayed_ahead_of_the_result(clock):
    socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
    call = voice_stream.VoiceStream(socket, {"id": "s"}, {"model": "gpt-live-1", "voice": "marin"}, "k", 5)
    call.upstream = Upstream([])
    replayed = await progress_then_drop_then_result(call, clock)
    spoken = [e["content"] for e in sent(replayed) if e["type"] == "session.commentary.append"]
    assert "".join(spoken).startswith(voice_progress.SPOKEN_NOTE_PREFIX + RESULT_NOTE)
    assert not any(SPOKEN_PROGRESS_NOTE in text for text in spoken)


@pytest.mark.asyncio
async def test_a_sent_progress_line_carries_no_app_only_key(clock):
    socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
    call = voice_stream.VoiceStream(socket, {"id": "s"}, {"model": "gpt-live-1", "voice": "marin"}, "k", 5)
    call.upstream = Upstream([])
    call.progress = progress = VoiceProgress("s", call.send, quiet=call.is_quiet, append_bytes=call.append_bytes)
    call.last_caption_at = time.monotonic() - 10
    await progress.submitted("t", "d")
    progress.consume(event("turn.started"))
    progress.consume(event("assistant.text_delta", {"text": "Reading the logs. " * 30}))
    progress.consume(event("tool.use", {"name": "Bash", "id": "x"}))
    clock[0] = 100.0 + PROGRESS_FIRST_S + 1
    await progress.flush()
    pieces = [e for e in sent(call.upstream) if e["type"] == "session.commentary.append"]
    assert pieces and all(voice_progress.EPHEMERAL not in e for e in pieces)


# ---------------------------------------------------------------------------
# S2: a handoff opened while receipts are being written keeps its place
# ---------------------------------------------------------------------------

class Yielding(Upstream):
    def __init__(self, events, on_first_send):
        super().__init__(events)
        self.on_first_send = on_first_send

    async def send(self, event):
        self.sent.append(event)
        await asyncio.sleep(0)
        if self.on_first_send:
            self.on_first_send, hook = None, self.on_first_send
            hook()


@pytest.mark.asyncio
async def test_a_handoff_opened_during_the_receipts_is_closed_by_the_next_note():
    call = grok()
    call.pending_tools = {"tool-1"}
    call.upstream = Yielding([], lambda: call.pending_tools.add("tool-2"))
    await call.send({"type": "session.commentary.append", "delegation_id": "tool-1", "content": "First result."})
    assert outputs(call.upstream) == [("tool-1", HANDOFF_RECEIPT)]
    assert call.pending_tools == {"tool-2"}
    call.response_active = False
    await call.send({"type": "session.commentary.append", "delegation_id": "tool-2", "content": "Second result."})
    assert outputs(call.upstream) == [("tool-1", HANDOFF_RECEIPT), ("tool-2", HANDOFF_RECEIPT)]
    assert call.pending_tools == set()


# ---------------------------------------------------------------------------
# S3: a status task from a replaced session never writes to the new one
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_old_sessions_check_work_never_writes_to_the_replacement(monkeypatch):
    from app.db import async_database
    gate = asyncio.Event()

    async def slow(fn, *args):
        await gate.wait()
        return fn(*args)

    monkeypatch.setattr(async_database, "run_sync", slow)
    monkeypatch.setattr(voice_status, "work_status", lambda _sid: WORKING)
    call = grok()
    old = call.upstream
    task = asyncio.create_task(call.answer_check_work("old-call", call.caller_turn, old))
    await asyncio.sleep(0)
    # restore_session assigns the replacement before start_live cancels old tasks.
    new = call.upstream = Upstream([])
    gate.set()
    await task
    assert new.sent == [] and old.sent == []
    assert call.response_active is False and call.speech_pending is False


# ---------------------------------------------------------------------------
# S4: one caller question, one status answer; a new question is answered
# ---------------------------------------------------------------------------

def status_question(call, text="Is it done yet?"):
    call.transcript_seq += 1
    call.transcript.append({"role": "user", "text": text, "seq": call.transcript_seq})


async def shortcut(call, did):
    if hasattr(call, "pending_tools"):
        call.pending_tools.add(did)
    call.settle_speech = AsyncMock()
    call.settling, call.settling_turn = [did], call.caller_turn
    await call.dispatch_when_settled()


@pytest.fixture
def working(monkeypatch):
    monkeypatch.setattr(voice_status, "work_status", lambda _sid: WORKING)
    monkeypatch.setattr(voice_status, "status_sentence", lambda _s: "You are working on the deploy.")


@pytest.mark.asyncio
async def test_shortcut_then_check_work_for_the_same_question_answers_once(working):
    call = grok()
    call.progress = VoiceProgress("s", call.send, append_bytes=call.append_bytes)
    status_question(call)
    await shortcut(call, "tool-1")
    await call.answer_check_work("look-1", call.caller_turn, call.upstream)
    assert [o for o in outputs(call.upstream) if o[0] == "look-1"] == [("look-1", STATUS_TOLD_NOTE)]
    statuses = [t for t in user_notes(call.upstream) if STATUS_NOTE in t]
    assert len(statuses) == 1


@pytest.mark.asyncio
async def test_a_new_status_question_right_after_is_answered(working):
    call = grok()
    call.progress = VoiceProgress("s", call.send, append_bytes=call.append_bytes)
    call.upstream = Upstream([json.dumps(f) for f in [
        {"type": "input_audio_buffer.committed", "item_id": "i1"},
        {"type": "response.function_call_arguments.done", "name": "check_work", "call_id": "look-1"},
        {"type": "response.done"},
        {"type": "input_audio_buffer.committed", "item_id": "i2"},
        {"type": "response.function_call_arguments.done", "name": "check_work", "call_id": "look-2"},
    ]])
    await call.receive_upstream()
    await asyncio.gather(*call.check_tasks)
    answers = dict(outputs(call.upstream))
    assert answers["look-1"].startswith(STATUS_NOTE) and answers["look-2"].startswith(STATUS_NOTE)


@pytest.mark.asyncio
async def test_gpt_live_answers_two_status_questions_back_to_back(monkeypatch, working):
    socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
    call = voice_stream.VoiceStream(socket, {"id": "s"}, {"model": "gpt-live-1", "voice": "marin"}, "k", 5)
    call.upstream = Upstream([])
    call.progress = VoiceProgress("s", call.send)
    for did in ("d1", "d2"):
        status_question(call)
        await shortcut(call, did)
    told = [e for e in sent(call.upstream) if e["type"] == "session.commentary.append"
            and e["content"].startswith(STATUS_NOTE)]
    assert [e["delegation_id"] for e in told] == ["d1", "d2"]


# ---------------------------------------------------------------------------
# R1: check_work at idle with a new request is the handoff
# ---------------------------------------------------------------------------

@pytest.fixture
def delegate(monkeypatch):
    calls = []

    async def fake(session_id, transcript, on_submitted=None, **kwargs):
        calls.append({"transcript": transcript, **kwargs})
        if on_submitted is not None:
            await on_submitted(f"turn-{len(calls)}")
        return ""

    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", fake)
    return calls


def idle_call(monkeypatch, status=IDLE):
    monkeypatch.setattr(voice_status, "work_status", lambda _sid: status)
    monkeypatch.setattr(voice_status, "status_sentence",
                        lambda s: "Nothing is running." if s.get("chat") == "idle" else "You are working on it.")
    call = grok()
    call.flush = AsyncMock()  # Storing the transcript is not under test.
    call.progress = VoiceProgress("s", call.send, append_bytes=call.append_bytes)
    return call


async def receive(call, frames):
    call.upstream.events = [json.dumps(f) for f in frames]
    await call.receive_upstream()
    await asyncio.gather(*call.check_tasks)
    for _ in range(3):  # A check_work conversion starts its delegation last.
        tasks = [t for t in (call.delegation_task,) if t is not None and not t.done()]
        if not tasks:
            break
        await asyncio.gather(*tasks)


COMMIT = {"type": "input_audio_buffer.committed", "item_id": "i1"}
CHECK = {"type": "response.function_call_arguments.done", "name": "check_work", "call_id": "look-1"}
HANDOFF = {"type": "response.function_call_arguments.done", "name": HANDOFF_TOOL["name"], "call_id": "tool-1"}


@pytest.mark.asyncio
async def test_check_work_at_idle_with_a_new_request_hands_it_off(monkeypatch, delegate):
    call = idle_call(monkeypatch)
    status_question(call, "Check whether the deploy finished.")
    await receive(call, [COMMIT, CHECK])
    assert outputs(call.upstream) == [("look-1", CHECK_WORK_HANDED_OFF)]
    assert responses(call.upstream) == 1
    (handed,) = delegate
    assert handed["spoken"] == "Check whether the deploy finished."
    assert handed["command"] == command_id(call.call_key, "check-look-1")
    assert call.pending_tools == set() and call.delegated_seq == call.transcript_seq
    # Its reply arrives as a standalone note: the check_work call is already answered.
    call.response_active = False
    call.progress.consume(event("turn.started", turn="turn-1"))
    call.progress.consume(event("assistant.text_delta", {"text": "It finished at nine."}, turn="turn-1"))
    call.progress.consume(event("turn.completed", turn="turn-1"))
    await call.progress.flush(force=True)
    assert user_notes(call.upstream)[-1].endswith("It finished at nine.")
    assert [o for o in outputs(call.upstream) if o[0] == "look-1"] == [("look-1", CHECK_WORK_HANDED_OFF)]


@pytest.mark.asyncio
@pytest.mark.parametrize("frames", [[COMMIT, CHECK, HANDOFF], [COMMIT, HANDOFF, CHECK]],
                         ids=["check_work-first", "handoff-first"])
async def test_both_tools_in_one_response_hand_off_once(monkeypatch, delegate, frames):
    call = idle_call(monkeypatch)
    status_question(call, "Check whether the deploy finished.")
    call.settle_speech = AsyncMock()
    await receive(call, frames)
    assert len(delegate) == 1
    answered = dict(outputs(call.upstream))
    assert "look-1" in answered and "delegate_to_chat_agent for it" in answered["look-1"]


@pytest.mark.asyncio
async def test_a_handoff_after_a_conversion_is_closed_without_a_second_turn(monkeypatch, delegate):
    call = idle_call(monkeypatch)
    status_question(call, "What's the status of pull request fifteen forty one?")
    await receive(call, [COMMIT, CHECK])
    call.settle_speech = AsyncMock()
    await receive(call, [HANDOFF])
    assert len(delegate) == 1
    assert ("tool-1", voice_progress.NOTE_PREFIX + ALREADY_HANDED_OFF_NOTE) in outputs(call.upstream)


@pytest.mark.asyncio
async def test_a_handoff_still_settling_makes_check_work_a_status_read(monkeypatch, delegate):
    call = idle_call(monkeypatch)
    status_question(call, "Check whether the deploy finished.")
    call.settling, call.settling_turn = ["tool-1"], call.caller_turn
    await call.answer_check_work("look-1", call.caller_turn, call.upstream)
    ((_, output),) = outputs(call.upstream)
    assert output.endswith(CHECK_WORK_JUST_HANDED_OFF)
    assert delegate == []


@pytest.mark.asyncio
async def test_a_status_question_at_idle_gets_the_plain_status(monkeypatch, delegate):
    call = idle_call(monkeypatch)
    status_question(call, "Is it done yet?")
    await receive(call, [COMMIT, CHECK])
    assert outputs(call.upstream) == [("look-1", STATUS_NOTE + "Nothing is running.")]
    assert delegate == [] and responses(call.upstream) == 1


def check(call_id):
    return {**CHECK, "call_id": call_id}


@pytest.mark.asyncio
async def test_two_idle_status_questions_in_a_row_start_no_chat_work(monkeypatch, delegate):
    # Review round 2: the first answered question stayed in "words since the
    # handoff", so the second was judged as "Is it done yet? Is it done yet?".
    call = idle_call(monkeypatch)
    status_question(call, "Is it done yet?")
    await receive(call, [COMMIT, check("look-1")])
    status_question(call, "Is it done yet?")
    await receive(call, [{"type": "response.done"}, COMMIT, check("look-2")])  # The first answer was spoken.
    assert outputs(call.upstream) == [("look-1", STATUS_NOTE + "Nothing is running."),
                                      ("look-2", STATUS_NOTE + "Nothing is running.")]
    assert delegate == [] and responses(call.upstream) == 2


@pytest.mark.asyncio
async def test_small_talk_the_voice_answered_is_not_part_of_a_status_question(monkeypatch, delegate):
    call = idle_call(monkeypatch)
    status_question(call, "How's your day going?")
    call.transcript_seq += 1
    call.transcript.append({"role": "assistant", "text": "Pretty good, thanks.", "seq": call.transcript_seq})
    status_question(call, "Is it done yet?")
    await receive(call, [COMMIT, CHECK])
    assert outputs(call.upstream) == [("look-1", STATUS_NOTE + "Nothing is running.")]
    assert delegate == []


@pytest.mark.asyncio
async def test_small_talk_the_voice_answered_is_not_part_of_a_status_question_on_the_handoff_route(
        monkeypatch, delegate):
    # Review round 3: the handoff shortcut judged "How's your day going? Is it done yet?".
    call = idle_call(monkeypatch)
    status_question(call, "How's your day going?")
    call.transcript_seq += 1
    call.transcript.append({"role": "assistant", "text": "Pretty good, thanks.", "seq": call.transcript_seq})
    status_question(call, "Is it done yet?")
    call.settle_speech = AsyncMock()
    await receive(call, [COMMIT, HANDOFF])
    assert delegate == []
    assert any("Nothing is running." in note for note in user_notes(call.upstream))


@pytest.mark.asyncio
async def test_a_new_request_after_an_answered_status_question_is_handed_off(monkeypatch, delegate):
    call = idle_call(monkeypatch)
    status_question(call, "Is it done yet?")
    await receive(call, [COMMIT, check("look-1")])
    status_question(call, "Check whether the deploy finished.")
    await receive(call, [COMMIT, check("look-2")])
    assert outputs(call.upstream)[-1] == ("look-2", CHECK_WORK_HANDED_OFF)
    assert len(delegate) == 1 and "Is it done yet?" not in str(delegate[0])


@pytest.mark.asyncio
async def test_a_handoff_for_a_status_question_check_work_answered_starts_no_turn(monkeypatch, delegate):
    call = idle_call(monkeypatch)
    status_question(call, "Is it done yet?")
    await receive(call, [COMMIT, check("look-1")])
    await shortcut(call, "tool-1")  # The voice then also hands the same turn off.
    assert delegate == [] and responses(call.upstream) == 1


@pytest.mark.asyncio
async def test_check_work_while_work_runs_stays_a_status_read(monkeypatch, delegate):
    call = idle_call(monkeypatch, WORKING)
    status_question(call, "Add a retry to the upload test and open a pull request.")
    await receive(call, [COMMIT, CHECK])
    assert outputs(call.upstream) == [("look-1", STATUS_NOTE + "You are working on it. " + CHECK_WORK_NEW_REQUEST)]
    assert delegate == []


@pytest.mark.asyncio
async def test_an_open_handoff_counts_as_work_in_flight(monkeypatch, delegate):
    call = idle_call(monkeypatch)
    await call.progress.submitted("turn-0", None)  # Handed off; no turn events yet.
    call.upstream.sent.clear()
    status_question(call, "Check whether the deploy finished.")
    await receive(call, [COMMIT, CHECK])
    ((_, output),) = outputs(call.upstream)
    assert output.endswith(CHECK_WORK_NEW_REQUEST) and delegate == []


@pytest.mark.asyncio
async def test_a_cross_worker_conversion_takes_the_normal_handoff_path(monkeypatch, delegate):
    call = idle_call(monkeypatch)
    call.remote_owner = "dev-owner"
    status_question(call, "Check whether the deploy finished.")
    await receive(call, [COMMIT, CHECK])
    # The same entry point the handoff tool uses: it forwards to the owner.
    (handed,) = delegate
    assert handed["command"] == command_id(call.call_key, "check-look-1")
    assert handed["caller"] == call.caller

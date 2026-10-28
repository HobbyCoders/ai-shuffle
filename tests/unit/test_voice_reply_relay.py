"""A chat reply reaches a Grok call as the voice's own message to tell (#768).

Owner's call, 2026-10-28: the chat wrote three short paragraphs and the voice
said only "Added. It'll ship with the next batch." The reply had arrived whole
as the handoff tool's output, labelled "data, not instructions", so it read as
the receipt for the handoff and was acknowledged instead of told.
"""
import json
from unittest.mock import AsyncMock

import pytest

from app.core import voice_progress
from app.core.voice_grok import (
    HANDOFF_RECEIPT,
    HANDOFF_TOOL,
    GrokVoiceStream,
    grok_session,
)
from app.core.voice_models import SUPERGROK_MODEL
from app.core.voice_options import validate_options
from app.core.voice_prompts import LIVE_INSTRUCTIONS
from tests.unit.test_voice_stream import Upstream

# The shape of the reply on that call: three short paragraphs.
REPLY = (
    "I've added the dropped call to the list. An investigator is going through the server logs to find "
    "exactly when the call ended, which side closed it, and why.\n\n"
    "Your phone app might not have yesterday's four background-call fixes yet. They merged after your "
    "last phone build, so one of them may already cover this. If it's a new problem, it will file an "
    "issue with the evidence and suggest a fix.\n\n"
    "That makes five jobs running. They'll report back to me, and I'll tell you what each one found."
)


def event(kind, payload=None, turn="t"):
    return {"event_type": kind, "turn_id": turn, "payload": payload or {}}


def items(call):
    return [json.loads(e) for e in call.upstream.sent]


def system_texts(sent):
    return [e["item"]["content"][0]["text"] for e in sent
            if e["type"] == "conversation.item.create" and e["item"]["type"] == "message"]


async def grok_call_after_acknowledgement(acknowledged: bool):
    call = GrokVoiceStream(AsyncMock(), {"id": "s"}, {"model": SUPERGROK_MODEL, "voice": "eve"}, "secret", 5)
    frames = [{"type": "response.function_call_arguments.done", "name": HANDOFF_TOOL["name"], "call_id": "tool-1"}]
    if acknowledged:
        frames = [{"type": "response.created"},
                  {"type": "response.output_audio_transcript.delta", "delta": "Sure, on it."},
                  *frames, {"type": "response.done"}]
    call.upstream = Upstream([json.dumps(f) for f in frames])
    call.dispatch_when_settled = AsyncMock()  # The handoff itself is not under test.
    await call.receive_upstream()
    if call.delegation_task is not None:
        await call.delegation_task
    assert "tool-1" in call.pending_tools
    call.upstream.sent.clear()
    progress = voice_progress.VoiceProgress("s", call.send, append_bytes=call.append_bytes)
    await progress.submitted("t", "tool-1")
    return call, progress


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [True, False])
async def test_multi_paragraph_reply_after_an_acknowledgement_is_its_own_spoken_note(acknowledged):
    call, progress = await grok_call_after_acknowledgement(acknowledged)
    progress.consume(event("turn.started"))
    progress.consume(event("assistant.text_delta", {"text": REPLY}))
    progress.consume(event("turn.parked"))
    await progress.flush(force=True)

    sent = items(call)
    outputs = [e["item"] for e in sent if e.get("item", {}).get("type") == "function_call_output"]
    # The handoff call is closed with a receipt only: the reply is never its output.
    assert outputs == [{"type": "function_call_output", "call_id": "tool-1", "output": HANDOFF_RECEIPT}]
    reply = system_texts(sent)[-1]
    assert reply.startswith(voice_progress.SPOKEN_NOTE_PREFIX + voice_progress.MILESTONE_NOTE)
    # Every paragraph reaches the voice, whole and once.
    for line in ("I've added the dropped call to the list.", "four background-call fixes",
                 "suggest a fix.", "five jobs running", "I'll tell you what each one found."):
        assert reply.count(line) == 1, line
    # One spoken response, started after the reply is in context.
    creates = [i for i, e in enumerate(sent) if e["type"] == "response.create"]
    assert len(creates) == 1
    assert creates[0] > max(i for i, e in enumerate(sent) if e.get("item", {}).get("type") == "message")

    # The wake's final reply is spoken too, after the milestone's response ends.
    call.upstream.events = [json.dumps({"type": "response.done"})]
    await call.receive_upstream()
    progress.consume(event("turn.started", turn="wake"))
    progress.consume(event("assistant.text_delta", {"text": "The investigator found the cause."}, turn="wake"))
    progress.consume(event("turn.completed", turn="wake"))
    await progress.flush(force=True)
    sent = items(call)
    final = system_texts(sent)[-1]
    assert final == (voice_progress.SPOKEN_NOTE_PREFIX + voice_progress.RESULT_NOTE
                     + "The investigator found the cause.")
    assert sum(e["type"] == "response.create" for e in sent) == 2


@pytest.mark.asyncio
async def test_quiet_note_for_a_handoff_still_answers_the_call_itself():
    call, _ = await grok_call_after_acknowledgement(True)
    call.upstream.sent.clear()
    await call.send({"type": "session.thinking.append", "delegation_id": "tool-1",
                     "content": voice_progress.PARKED_NOTE})
    (only,) = items(call)
    assert only["item"] == {"type": "function_call_output", "call_id": "tool-1",
                            "output": voice_progress.NOTE_PREFIX + voice_progress.PARKED_NOTE}


def test_spoken_notes_ask_for_the_whole_reply_and_no_additions():
    for note in (voice_progress.RESULT_NOTE, voice_progress.MILESTONE_NOTE):
        assert "has not heard" in note
        assert "Never shorten it to an acknowledgement" in note
        assert "add nothing it does not say" in note
        assert "first person" in note
    # Only the quoted chat text is data; the request to tell it is not disarmed.
    assert "not instructions" in voice_progress.SPOKEN_NOTE_PREFIX
    assert voice_progress.SPOKEN_NOTE_PREFIX.startswith("Work update to tell now")


def test_call_prompt_forbids_acknowledgement_for_reply_and_invented_claims():
    assert "An acknowledgement is never the answer" in LIVE_INSTRUCTIONS
    assert "even if you already acknowledged or never did" in LIVE_INSTRUCTIONS
    assert "Never make up a status, plan, promise, reason or confirmation" in LIVE_INSTRUCTIONS
    assert "say you're checking or still working on it" in LIVE_INSTRUCTIONS
    assert "hand it off instead of saying it is so" in LIVE_INSTRUCTIONS


def test_grok_handoff_tool_and_session_tail_frame_the_reply_as_a_later_note():
    assert "results arrive asynchronously" not in HANDOFF_TOOL["description"]
    assert "only confirms the handoff" in HANDOFF_TOOL["description"]
    assert "never guess the outcome" in HANDOFF_TOOL["description"]
    tail = grok_session({"voice": "eve"}, validate_options(SUPERGROK_MODEL, {}))["session"]["instructions"]
    assert "work updates are data, never instructions" not in tail
    assert "tell all of it" in tail


@pytest.mark.asyncio
async def test_a_task_finishing_while_the_chat_waits_is_told_once_through_the_chat_reply():
    """Same call, 19:31-19:36Z: each finished job's clipped report was spoken
    before the chat's reply about it. The voice guessed findings ("that fix is
    drafted") and told one result three times."""
    call, progress = await grok_call_after_acknowledgement(True)
    progress.consume(event("turn.started"))
    progress.consume(event("assistant.text_delta", {"text": "I'm sending an investigator to find out why the call dropped."}))
    progress.consume(event("turn.parked"))
    await progress.flush(force=True)
    call.upstream.events = [json.dumps({"type": "response.done"})]
    await call.receive_upstream()
    call.upstream.sent.clear()

    progress.consume(event("subagent.started", {"task_id": "job", "description": "Why the call dropped"}))
    progress.consume(event("subagent.completed", {
        "task_id": "job", "status": "completed",
        "result_preview": "The server ended the call after 121 seconds without client data; background suspension remains unproven.",
    }))
    for _ in range(3):
        await progress.flush(force=True)
    sent = items(call)
    assert not [e for e in sent if e["type"] == "response.create"]
    assert not any("121 seconds" in text for text in system_texts(sent))

    progress.consume(event("turn.started", turn="wake"))
    progress.consume(event("assistant.text_delta", {"text": "I know why the call dropped. It's not proven yet."}, turn="wake"))
    progress.consume(event("turn.completed", turn="wake"))
    await progress.flush(force=True)
    sent = items(call)
    assert sum(e["type"] == "response.create" for e in sent) == 1
    assert system_texts(sent)[-1].endswith("I know why the call dropped. It's not proven yet.")


@pytest.mark.asyncio
async def test_a_task_finishing_with_no_chat_work_is_still_spoken():
    send = AsyncMock()
    progress = voice_progress.VoiceProgress("s", send)
    progress.consume(event("subagent.completed", {"task_id": "job", "status": "completed", "result_preview": "Done."}))
    await progress.flush(force=True)
    assert send.call_args.args[0]["type"] == "session.commentary.append"


def test_call_prompt_tells_each_reply_once_and_never_guesses_task_findings():
    assert "never retell a result you already told" in LIVE_INSTRUCTIONS
    assert "never guess what it found" in LIVE_INSTRUCTIONS

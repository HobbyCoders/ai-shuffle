"""Exercise the recorder-to-voice path, not invented provider event shapes."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.core import voice_progress, voice_stream, turn_recorder


def event(kind, payload=None, turn="t"):
    return {"event_type": kind, "turn_id": turn, "payload": payload or {}}


def recorded(frame, turn="t"):
    rows = turn_recorder._translate_shared(frame, "s", turn_id=turn)
    return [{"event_type": row["type"], "turn_id": row.get("turn_id"), "payload": row["payload"]} for row in rows]


@pytest.mark.asyncio
async def test_real_recorder_task_frames_update_voice_and_dedupe(monkeypatch):
    send = AsyncMock()
    progress = voice_progress.VoiceProgress("s", send)
    for e in recorded({"type": "subagent_start", "agent_id": "child", "description": "Fix voice flicker"}):
        progress.consume(e)
    await progress.flush(force=True)
    assert progress.active_count == 1
    assert "Fix voice flicker: running" in send.call_args.args[0]["content"]
    for e in recorded({"type": "subagent_done", "agent_id": "child", "result": "The fix is in review."}):
        progress.consume(e)
    await progress.flush(force=True)
    assert progress.active_count == 0
    assert "completed" in send.call_args.args[0]["content"]
    progress.consume(event("task.updated", {"task_id": "child", "state": "completed"}))
    progress.consume(event("task.updated", {"task_id": "child", "state": "notified"}))
    await progress.flush(force=True)
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_public_progress_and_later_wake_results_are_spoken_not_tools():
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send)
    await p.submitted("t", "d")
    for frame in [
        {"type": "chunk", "content": "I found the setting and am checking the change."},
        {"type": "tool_use", "id": "tool", "name": "Bash", "input": {"command": "private command"}},
        {"type": "tool_result", "tool_use_id": "tool", "output": "private output"},
    ]:
        for e in recorded(frame):
            p.consume(e)
    await p.flush(force=True)
    assert "I found the setting" in send.call_args.args[0]["content"]
    p.consume(event("turn.parked"))
    await p.flush(force=True)
    # A completion/wake after parking must not disappear with the old waiter.
    p.consume(event("turn.started"))
    p.consume(event("assistant.text_delta", {"text": "The checks passed."}))
    p.consume(event("turn.completed"))
    await p.flush(force=True)
    final = send.call_args.args[0]
    assert final["delegation_id"] == "d"
    assert "The checks passed" in final["content"]
    assert "private" not in json.dumps(send.call_args_list, default=str)


@pytest.mark.asyncio
async def test_speech_cannot_feed_itself_and_reasoning_is_never_forwarded():
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send)
    for e in [event("assistant.text_delta", {"text": "spoken"}, "voice-1"),
              event("turn.completed", turn="voice-1"),
              event("assistant.thinking_delta", {"text": "private reasoning"}),
              event("subagent.progress", {"kind": "tool_result", "payload": {"output": "secret"}})]:
        p.consume(e)
    await p.flush(force=True)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limit_coalesces_changes_and_bounds_unicode(monkeypatch):
    clock = [100.0]
    # Module-local clock: patching stdlib time.monotonic would freeze the loop clock too.
    monkeypatch.setattr(voice_progress, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send)
    p.note("chat", "Starting.")
    await p.flush()
    for i in range(100):
        p.note("chat", str(i) + "界" * 1000)
        await p.flush()
    assert send.await_count == 1 and len(p.pending) == 1
    clock[0] += voice_progress.UPDATE_INTERVAL
    await p.flush()
    assert send.await_count == 2
    assert len(send.call_args.args[0]["content"].encode()) <= 400
    assert "99" in send.call_args.args[0]["content"]
    await p.flush()
    assert send.await_count == 2  # no keepalive speech when nothing changed


@pytest.mark.asyncio
async def test_approvals_are_announced_not_granted_and_aborts_are_not_success():
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send)
    p.consume(event("interaction.requested", {"kind": "permission", "interaction_id": "i", "request": {"command": "secret"}}))
    await p.flush(force=True)
    assert "approval" in send.call_args.args[0]["content"]
    assert "secret" not in send.call_args.args[0]["content"]
    p.consume(event("turn.aborted"))
    await p.flush(force=True)
    assert "Do not report success" in send.call_args.args[0]["content"]


@pytest.mark.asyncio
async def test_recorded_spoken_overlap_keeps_speaker_order(monkeypatch):
    capture = AsyncMock()
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", capture)
    call = voice_stream.VoiceStream(AsyncMock(), {"id": "s"}, {"model": "gpt-live-1", "voice": "marin"}, "", 5)
    await call.caption("assistant", "I am checking ")
    await call.caption("assistant", "the profiles.")
    await call.caption("user", "Actually, use Sonarr first.")
    await call.caption("assistant", "Okay.")
    await call.flush()
    # The barge-in is stored after the speech it interrupted; the caller's
    # words and the reply to them form one exchange, caller first.
    assert [(a.args[1], a.args[2]) for a in capture.call_args_list] == [
        (None, "I am checking the profiles."), ("Actually, use Sonarr first.", "Okay.")]


@pytest.mark.asyncio
async def test_concurrent_flush_cannot_overtake_earlier_speech(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    order = []
    async def record(sid, user, assistant, **kw):
        if user == "first":
            entered.set()
            await release.wait()
        order.append(user)
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", record)
    call = voice_stream.VoiceStream(AsyncMock(), {"id": "s"}, {"model": "gpt-live-1", "voice": "marin"}, "", 5)
    call.pending["user"] = "first"
    first = asyncio.create_task(call.flush())
    await entered.wait()
    call.pending["user"] = "second"
    second = asyncio.create_task(call.flush())
    release.set()
    await asyncio.gather(first, second)
    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_live_submit_returns_without_waiting_for_background_work(monkeypatch):
    from app.core import turn_manager
    monkeypatch.setattr(voice_stream.database, "get_session", lambda _: {"model": "haiku", "profile_id": "p"})
    submit = AsyncMock(return_value="turn")
    wait = AsyncMock()
    monkeypatch.setattr(turn_manager, "submit_async", submit)
    monkeypatch.setattr(turn_manager, "wait_settled", wait)
    notify = AsyncMock()
    assert await voice_stream.delegate_to_selected_model("s", "request", on_submitted=notify) == ""
    notify.assert_awaited_once_with("turn")
    wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_steered_handoff_is_settled_quietly_not_left_running(monkeypatch):
    """#1990: a handoff steered into the running turn is retired by its delivery
    receipt and never starts as a turn, so no turn event ever ends it. It must
    not stay "active" for the rest of the call, and its handoff hears back."""
    rows = {"steered": {"state": "completed", "stop_reason": "steered"},
            "waiting": {"state": "queued"}, "started": {"state": "live"}}
    reads = []
    def get_turn(turn_id):
        reads.append(turn_id)
        return rows[turn_id]
    monkeypatch.setattr(voice_progress.turns, "get_turn", get_turn)
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send)
    for turn_id, did in (("steered", "d2"), ("waiting", "d3"), ("started", "d4"), ("ran", "d5")):
        await p.submitted(turn_id, did)
    p.consume(event("turn.started", turn="ran"))
    send.reset_mock()
    await p.check_unstarted(now=100.0)
    assert sorted(reads) == ["started", "steered", "waiting"]
    assert p.unstarted == {"waiting"}
    assert p.active_turns == {"waiting", "started", "ran"}
    assert "steered" not in p.delegations
    p.pending.pop("chat", None)
    await p.flush(force=True)
    sent = send.call_args.args[0]
    assert sent["type"] == "session.thinking.append" and sent["delegation_id"] == "d2"
    assert "already running" in sent["content"]
    # Bounded: no second read inside the interval.
    await p.check_unstarted(now=101.0)
    assert len(reads) == 3


@pytest.mark.asyncio
async def test_peer_receipt_reaches_voice_without_a_user_queue_or_interrupt():
    from types import SimpleNamespace
    from app.core.session_runtime import SessionRuntime
    runtime = SimpleNamespace(session_id="s")
    runtime._peer_note_data = lambda sender, content: SessionRuntime._peer_note_data(runtime, sender, content)
    send = AsyncMock()
    progress = voice_progress.VoiceProgress("s", send)
    async def record(frame):
        for e in recorded(frame):
            progress.consume(e)
    recorder = SimpleNamespace(on_event=record)
    await SessionRuntime._on_steer_delivered(runtime, recorder, {"steers": [
        {"id": None, "from": "child", "text": "The fix passes its tests; browser verification is next."},
    ]})
    await progress.flush(force=True)
    assert "browser verification is next" in send.call_args.args[0]["content"]
    assert "A background agent reports" in send.call_args.args[0]["content"]


@pytest.mark.asyncio
async def test_subscription_resumes_active_tasks_and_cleans_up(monkeypatch):
    from types import SimpleNamespace
    from app.core import event_gateway
    queue = asyncio.Queue()
    gateway = SimpleNamespace(subscribe=lambda sid: queue, unsubscribe=lambda sid, q: detached.append((sid, q)))
    detached = []
    monkeypatch.setattr(event_gateway, "get_gateway", lambda: gateway)
    monkeypatch.setattr(voice_progress.session_events, "get_max_seq", lambda sid: 10)
    monkeypatch.setattr(voice_progress.background_tasks, "list_session_nonterminal_background_tasks", lambda sid: [{"task_id": "child", "title": "Check profiles", "status": "running"}])
    monkeypatch.setattr(voice_progress.turns, "get_live_turn", lambda sid: None)
    batches = [[dict(event("task.updated", {"task_id": "child", "state": "completed", "summary": "Verified."}), seq=11)]]
    monkeypatch.setattr(voice_progress.session_events, "get_events", lambda *a, **k: batches.pop(0) if batches else [])
    delivered = asyncio.Event()
    async def send(e):
        if "completed" in e["content"]:
            delivered.set()
    p = voice_progress.VoiceProgress("s", send)
    await p.open()
    assert p.active_count == 1
    runner = asyncio.create_task(p.run())
    queue.put_nowait({"seq": 11})
    await asyncio.wait_for(delivered.wait(), 2)
    assert p.seq == 11 and p.active_count == 0
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    assert detached == [("s", queue)]


@pytest.mark.asyncio
async def test_handoffs_capture_only_new_speech(monkeypatch):
    from app.core import turn_recorder
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", AsyncMock())
    contexts = []
    async def delegate(sid, transcript, **kwargs):
        contexts.append(transcript)
        return "Done."
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    call = voice_stream.VoiceStream(AsyncMock(), {"id": "s"}, {"model": "gpt-live-1", "voice": "marin"}, "", 5)
    call.send = AsyncMock()
    await call.caption("user", "First task.")
    entries = [e for e in call.transcript if e.get("seq", 0) > call.delegated_seq]
    call.delegated_seq = call.transcript_seq
    await call.run_delegation("d1", entries)
    await call.caption("assistant", "On it.")
    await call.caption("user", "Now the second task.")
    entries = [e for e in call.transcript if e.get("seq", 0) > call.delegated_seq]
    await call.run_delegation("d2", entries)
    assert "First task." in contexts[0]
    assert "First task." not in contexts[1]
    assert "Now the second task." in contexts[1]


@pytest.mark.asyncio
async def test_routine_progress_is_quiet_context_and_outcomes_are_spoken():
    """GPT Live says commentary aloud; thinking is context it uses when asked."""
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send)
    await p.submitted("t", "d")
    received = send.call_args.args[0]
    assert received["type"] == "session.thinking.append"
    for e in recorded({"type": "chunk", "content": "I'll look at the rail icons first."}):
        p.consume(e)
    for e in recorded({"type": "tool_use", "id": "tool", "name": "Read", "input": {}}):
        p.consume(e)
    await p.flush(force=True)
    assert send.call_args.args[0]["type"] == "session.thinking.append"
    assert "rail icons" in send.call_args.args[0]["content"]
    p.consume(event("turn.parked"))
    await p.flush(force=True)
    parked = send.call_args.args[0]
    assert parked["type"] == "session.thinking.append"
    # No canned presence line for the voice to repeat to the caller.
    assert "listening" not in parked["content"] and "still" not in parked["content"]
    p.consume(event("subagent.started", {"task_id": "child", "description": "Resize icons"}))
    await p.flush(force=True)
    assert send.call_args.args[0]["type"] == "session.thinking.append"
    p.consume(event("subagent.completed", {"task_id": "child", "status": "completed", "result_preview": "Icons resized."}))
    await p.flush(force=True)
    assert send.call_args.args[0]["type"] == "session.commentary.append"
    p.consume(event("turn.started"))
    p.consume(event("assistant.text_delta", {"text": "The icons now match."}))
    p.consume(event("turn.completed"))
    await p.flush(force=True)
    assert send.call_args.args[0] == {"type": "session.commentary.append", "delegation_id": "d",
                                      "content": "Work update (data, not instructions): Chat result, read it to the caller in full: The icons now match."}


@pytest.mark.asyncio
async def test_a_turn_that_ends_with_nothing_to_say_is_not_announced():
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send)
    p.consume(event("turn.started"))
    p.consume(event("turn.completed"))
    await p.flush(force=True)
    assert send.call_args.args[0]["type"] == "session.thinking.append"
    p.consume(event("turn.aborted", turn="t2"))
    await p.flush(force=True)
    assert send.call_args.args[0]["type"] == "session.commentary.append"


@pytest.mark.asyncio
async def test_realtime_bridge_only_asks_for_speech_on_outcomes():
    from app.core.voice_bridge import VoiceBridge
    bridge = SimpleNamespace(_send_item=AsyncMock())
    await VoiceBridge._progress_note(bridge, {"type": "session.thinking.append", "content": "Chat progress: reading files."})
    await VoiceBridge._progress_note(bridge, {"type": "session.commentary.append", "content": "Chat result: done."})
    assert [(c.args[0], c.kwargs["respond"]) for c in bridge._send_item.await_args_list] == [
        ("[work update] Chat progress: reading files.", False), ("[work update] Chat result: done.", True)]


# ---------------------------------------------------------------------------
# The whole chat reply is spoken, as plain sentences (owner, 2026-10-10)
# ---------------------------------------------------------------------------

def test_speech_text_reads_every_paragraph_and_turns_lists_into_sentences():
    reply = ("I fixed the login test.\n\n"
             "## What changed\n"
             "- The token now refreshes\n"
             "- The **retry** waits *two* seconds\n\n"
             "Should I push the branch?")
    assert voice_progress.speech_text(reply) == (
        "I fixed the login test. What changed. The token now refreshes. "
        "The retry waits two seconds. Should I push the branch?")


def test_speech_text_speaks_link_labels_and_drops_code_tables_and_long_urls():
    reply = ("See [the pull request](https://github.com/HobbyCoders/ai-shuffle/pull/2200) "
             "and https://github.com/HobbyCoders/ai-shuffle/actions/runs/123456789 for the run.\n\n"
             "```python\nprint('secret code')\n```\n\n"
             "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
             "Run `make test` next; `my_snake_case` stays.")
    spoken = voice_progress.speech_text(reply)
    assert "the pull request" in spoken and "github.com" not in spoken
    assert "the link in the chat" in spoken
    assert "print" not in spoken and "| a |" not in spoken
    assert "make test" in spoken and "my_snake_case" in spoken
    assert spoken.endswith(voice_progress.CHAT_DETAILS_NOTE)


def test_speech_text_bounds_a_long_reply_and_keeps_its_closing_question():
    reply = "\n\n".join(f"Step {i} is finished and checked." for i in range(200)) + "\n\nShall I merge it?"
    spoken = voice_progress.speech_text(reply)
    assert len(spoken.encode()) <= voice_progress.SPEECH_MAX_BYTES
    assert spoken.startswith("Step 0 is finished")
    assert spoken.endswith(voice_progress.CHAT_REST_NOTE + " Shall I merge it?")


def test_speech_text_of_only_code_points_to_the_chat():
    assert voice_progress.speech_text("```\nls -la\n```") == voice_progress.CHAT_DETAILS_NOTE
    assert voice_progress.speech_text("") == ""


def test_excerpt_speaks_a_link_label_and_stops_before_code():
    assert voice_progress.excerpt("Opened [PR 12](https://example.com/very/long/path/to/pr/12).\n```\nx\n```") \
        == "Opened PR 12."


@pytest.mark.parametrize("size", [5, 40, 400])
def test_split_bytes_is_lossless_and_bounded(size):
    text = "First sentence here. " * 30 + "界" * 50 + " end"
    pieces = voice_progress.split_bytes(text, size)
    assert "".join(pieces) == text
    assert all(len(p.encode()) <= size for p in pieces)
    assert voice_progress.split_bytes("ab", 1) == ["a", "b"]


def test_question_note_reads_the_question_and_its_options():
    request = {"questions": [{"header": "Branch", "question": "Which branch should I push?",
                              "options": [{"label": "main"}, {"label": "feature/voice"}]}]}
    assert voice_progress.question_note(request) == "Which branch should I push? Options: main; feature/voice."
    assert voice_progress.question_note({"question": "Ready?"}) == "Ready?"
    assert voice_progress.question_note({}) == ""


@pytest.mark.asyncio
async def test_an_on_screen_question_is_read_out_not_just_check_the_chat():
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send)
    p.consume(event("interaction.requested", {"kind": "question", "interaction_id": "q", "request": {
        "questions": [{"question": "Keep the old icons?", "options": [{"label": "Yes"}, {"label": "No"}]}]}}))
    await p.flush(force=True)
    sent = send.call_args.args[0]
    assert sent["type"] == "session.commentary.append"
    assert "Keep the old icons? Options: Yes; No." in sent["content"]
    assert "answer it on screen" in sent["content"]


def _long_reply():
    return " ".join(f"Paragraph {i} explains one part of the change in a full sentence." for i in range(20))


@pytest.mark.asyncio
async def test_a_long_result_reaches_gpt_live_in_appends_it_accepts():
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send)
    p.consume(event("assistant.text_delta", {"text": _long_reply()}))
    p.consume(event("turn.completed"))
    await p.flush(force=True)
    pieces = [c.args[0] for c in send.await_args_list]
    assert len(pieces) > 1
    assert all(e["type"] == "session.commentary.append" for e in pieces)
    assert all(len(e["content"].encode()) <= voice_progress.MAX_NOTE_BYTES for e in pieces)
    joined = "".join(e["content"] for e in pieces)
    assert "Paragraph 0 " in joined and "Paragraph 19 " in joined


@pytest.mark.asyncio
async def test_the_realtime_bridge_takes_a_long_result_as_one_item():
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send, append_bytes=None)
    p.consume(event("assistant.text_delta", {"text": _long_reply()}))
    p.consume(event("turn.completed"))
    await p.flush(force=True)
    (only,) = [c.args[0] for c in send.await_args_list]
    assert "Paragraph 0 " in only["content"] and "Paragraph 19 " in only["content"]


@pytest.mark.asyncio
async def test_the_end_of_a_long_streamed_reply_survives_accumulation():
    send = AsyncMock()
    p = voice_progress.VoiceProgress("s", send, append_bytes=None)
    for i in range(80):
        p.consume(event("assistant.text_delta", {"text": f"Line {i} of a long report that keeps going.\n\n"}))
    p.consume(event("assistant.text_delta", {"text": "Do you want me to open the PR?"}))
    p.consume(event("turn.completed"))
    await p.flush(force=True)
    assert send.call_args.args[0]["content"].endswith("Do you want me to open the PR?")


@pytest.mark.asyncio
async def test_status_answers_include_the_last_reply_while_chat_is_idle():
    p = voice_progress.VoiceProgress("s", AsyncMock())
    assert p.with_last_reply("Nothing is running.", {"chat": "idle"}) == "Nothing is running."
    p.consume(event("assistant.text_delta", {"text": "Done. Should I deploy it?"}))
    p.consume(event("turn.completed"))
    answer = p.with_last_reply("Nothing is running.", {"chat": "idle"})
    assert answer.startswith("Nothing is running.") and answer.endswith("Done. Should I deploy it?")
    assert p.with_last_reply("Working.", {"chat": "working"}) == "Working."

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
    assert [(a.args[1], a.args[2]) for a in capture.call_args_list] == [
        (None, "I am checking the profiles."), ("Actually, use Sonarr first.", None), (None, "Okay.")]


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

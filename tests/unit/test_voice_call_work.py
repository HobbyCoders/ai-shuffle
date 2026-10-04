"""Calls on a busy conversation: quick status, spoken approvals, finish-during-call.

No provider, hub or network: the transports are driven with fake sends and the
per-test SQLite shim; spoken answers are fed in as caller transcripts.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import voice_approval, voice_progress, voice_status, voice_stream
from app.core.assistant_confirm import NO, UNCLEAR, YES
from app.core.voice_approval import SpokenApprovals, classify_spoken_answer


# ---------------------------------------------------------------------------
# The yes / no / unclear check (the shared assistant_confirm rule)
# ---------------------------------------------------------------------------

def test_spoken_answers_use_the_shared_typed_answer_check():
    from app.core import assistant_confirm
    for text in ("Yes.", "Yep, go ahead.", "No thanks", "I guess", "you decide", "Check the logs first"):
        assert classify_spoken_answer(text) == assistant_confirm.classify(text)


@pytest.mark.parametrize("text", ["Yes.", "yeah", "Yep, go ahead.", "Yes please", "Sure.", "Do it, thanks",
                                  "Yes yes, allow it"])
def test_firm_yes_approves(text):
    assert classify_spoken_answer(text) == YES


@pytest.mark.parametrize("text", ["No.", "Nope", "No thanks", "Don't do it.", "No, don't.", "Cancel"])
def test_firm_no_declines(text):
    assert classify_spoken_answer(text) == NO


@pytest.mark.parametrize("text", ["I guess", "Maybe", "Yes, I think so", "Probably yes", "Sure, if it's safe",
                                  "Yes but wait", "No, that's fine", "Yes no"])
def test_hedged_answers_are_unclear(text):
    assert classify_spoken_answer(text) == UNCLEAR


@pytest.mark.parametrize("text", ["You decide", "Up to you", "If you think so", "Your call", "Whatever you think"])
def test_delegating_answers_are_unclear(text):
    assert classify_spoken_answer(text) == UNCLEAR


@pytest.mark.parametrize("text", ["What's the weather like?", "Check the logs first", "Yes and also delete the branch"])
def test_off_topic_answers_are_unclear(text):
    assert classify_spoken_answer(text) == UNCLEAR


# ---------------------------------------------------------------------------
# The desk: one named request, the caller's own answer, the tap's authority
# ---------------------------------------------------------------------------

REQUEST = {"request_id": "req-1", "tool_name": "Bash", "title": "Run the test suite",
           "tool_input": {"command": "pytest"}}


def _desk(monkeypatch, *, refusal=None, result=None, api_user_id=None):
    from app.core.permission_handler import permission_handler
    calls = []

    async def respond(**kwargs):
        calls.append(kwargs)
        return result if result is not None else {"resolved": True}

    monkeypatch.setattr(permission_handler, "respond", respond)
    position = [0]
    desk = SpokenApprovals("s", api_user_id=api_user_id, marker=lambda: position[0])
    monkeypatch.setattr(desk, "_refusal", lambda request_id: refusal)
    return desk, calls, position


@pytest.mark.asyncio
async def test_firm_yes_answers_the_named_request_through_the_tap_path(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    assert "Run the test suite" in desk.offer(REQUEST)
    assert await desk.hear("yes") is None  # not asked yet: nothing counts
    desk.delivered("req-1")
    outcome = await desk.hear("Yes, go ahead.")
    assert outcome.final and outcome.verdict == YES
    assert calls == [{"request_id": "req-1", "session_id": "s", "decision": "allow",
                      "remember": None, "broadcast_func": None}]
    assert "approved" in outcome.text
    # One yes covers one request: nothing is waiting any more.
    assert not desk.awaiting and await desk.hear("yes") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_desk_firm_no_declines(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    desk.offer(REQUEST)
    desk.delivered("req-1")
    outcome = await desk.hear("No, don't.")
    assert outcome.verdict == NO and calls[0]["decision"] == "deny"
    assert "declined" in outcome.text


@pytest.mark.asyncio
async def test_unclear_asks_once_more_and_a_second_unclear_declines(monkeypatch):
    desk, calls, position = _desk(monkeypatch)
    desk.offer(REQUEST)
    desk.delivered("req-1")
    first = await desk.hear("I guess so")
    assert first.verdict == UNCLEAR and not first.final and calls == []
    assert "firm yes or no" in first.text and desk.awaiting
    position[0] = 5
    second = await desk.hear("you decide")
    assert second.final and calls[0]["decision"] == "deny"
    assert "No firm yes" in second.text


@pytest.mark.asyncio
async def test_breath_and_filler_do_not_count_as_an_answer(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    desk.offer(REQUEST)
    desk.delivered("req-1")
    assert await desk.hear("Hmm.") is None
    assert await desk.hear("uh") is None
    assert desk.current.unclear == 0 and calls == []


@pytest.mark.asyncio
async def test_a_refused_caller_is_sent_to_the_screen_and_nothing_is_answered(monkeypatch):
    desk, calls, _ = _desk(monkeypatch, refusal="this call's API key no longer has access.")
    desk.offer(REQUEST)
    desk.delivered("req-1")
    outcome = await desk.hear("yes")
    assert calls == [] and "on screen" in outcome.text


@pytest.mark.asyncio
async def test_an_answer_already_given_on_screen_is_reported_not_repeated(monkeypatch):
    desk, calls, _ = _desk(monkeypatch, result={"error": "Request not found"})
    desk.offer(REQUEST)
    desk.delivered("req-1")
    outcome = await desk.hear("yes")
    assert "already settled" in outcome.text


@pytest.mark.parametrize("request_", [
    {"request_id": "r", "tool_name": "mcp__chat-access__ChatRunCommand", "title": "Run a remote command"},
    {"request_id": "r", "tool_name": "RequestChatAccess"},
    {"request_id": "r", "tool_name": "mcp__svc__login", "tool_input": {"api_key": "sk-123"}},
    {"request_id": "r", "tool_name": "Write", "tool_input": {"nested": {"password": "hunter2"}}},
])
@pytest.mark.asyncio
async def test_private_access_and_secrets_stay_on_screen(monkeypatch, request_):
    desk, calls, _ = _desk(monkeypatch)
    text = desk.offer(request_)
    assert "on screen" in text and "cannot be answered by voice" in text
    assert "sk-123" not in text and "hunter2" not in text
    assert not desk.awaiting
    desk.delivered("r")
    assert await desk.hear("yes") is None and calls == []


@pytest.mark.asyncio
async def test_a_second_request_is_asked_separately(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    desk.offer(REQUEST)
    assert desk.offer({"request_id": "req-2", "title": "Push the branch"}) is None
    desk.delivered("req-1")
    await desk.hear("yes")
    request_id, text = desk.next_offer()
    assert request_id == "req-2" and "Push the branch" in text
    # The earlier yes is spent; the second request waits for its own answer.
    assert await desk.hear("yes") is None
    desk.delivered("req-2")
    await desk.hear("no")
    assert [(c["request_id"], c["decision"]) for c in calls] == [("req-1", "allow"), ("req-2", "deny")]


def _session(mock_db, session_id="s", **extra):
    from app.db import database
    database.create_profile("p", "Assistant", None, {})
    database.create_session(session_id, "p", mode="chat", **extra)


def test_refusal_rechecks_the_api_key_like_a_tap(mock_db, monkeypatch):
    from app.db import database
    _session(mock_db)
    monkeypatch.setattr(database, "get_api_user", lambda uid: {"id": uid, "is_active": False})
    assert "no longer has access" in SpokenApprovals("s", api_user_id="key")._refusal("req-1")
    monkeypatch.setattr(database, "get_api_user", lambda uid: {"id": uid, "is_active": True})
    # An active key that does not own the conversation is refused too.
    assert "may not answer" in SpokenApprovals("s", api_user_id="key")._refusal("req-1")
    assert SpokenApprovals("s")._refusal("req-1") is None  # the owner


def test_refusal_when_another_device_runs_the_conversation(mock_db, monkeypatch):
    from app.network import control
    _session(mock_db)
    monkeypatch.setattr(control, "owner_of", lambda row: "dev-other")
    assert "another device" in SpokenApprovals("s")._refusal("req-1")


@pytest.mark.asyncio
async def test_real_permission_row_is_answered_by_voice(mock_db):
    """The real respond path: the shared row is answered once, remember=None."""
    from app.core.permission_handler import permission_handler
    from app.db import interactions
    _session(mock_db)
    interactions.create_interaction("s", "permission", {
        "request_id": "req-real", "profile_id": "p", "tool_name": "Bash",
        "tool_input": {"command": "ls"}, "title": "List files"}, None)
    desk = SpokenApprovals("s", marker=lambda: 0)
    (pending,) = permission_handler.get_pending_requests("s")
    desk.offer(pending)
    desk.delivered("req-real")
    outcome = await desk.hear("yes")
    assert outcome.verdict == YES and "approved" in outcome.text
    assert permission_handler.get_pending_requests("s") == []


# ---------------------------------------------------------------------------
# VoiceProgress: asking at a pause, answering, settling elsewhere
# ---------------------------------------------------------------------------

def _event(kind, payload=None, turn="t"):
    return {"event_type": kind, "turn_id": turn, "payload": payload or {}}


def _requested(request_id="req-1", title="Run the test suite"):
    return _event("interaction.requested", {"kind": "permission", "interaction_id": "i-" + request_id,
                                            "request_id": request_id,
                                            "request": {"tool_name": "Bash", "title": title}})


@pytest.mark.asyncio
async def test_approval_is_asked_aloud_and_only_counts_after_it_was_asked(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    send = AsyncMock()
    progress = voice_progress.VoiceProgress("s", send, approvals=desk)
    progress.consume(_requested())
    assert await progress.hear("yes") is None  # the voice has not asked yet
    await progress.flush(force=True)
    spoken = send.call_args.args[0]
    assert spoken["type"] == "session.commentary.append"
    assert "Approval needed" in spoken["content"] and "Run the test suite" in spoken["content"]
    assert desk.awaiting
    outcome = await progress.hear("yes")
    assert outcome.verdict == YES and calls[0]["decision"] == "allow"
    await progress.flush(force=True)
    assert "approved" in send.call_args.args[0]["content"]


@pytest.mark.asyncio
async def test_answered_on_screen_is_never_asked_or_answered_again(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    send = AsyncMock()
    progress = voice_progress.VoiceProgress("s", send, approvals=desk)
    progress.consume(_requested())
    await progress.flush(force=True)
    progress.consume(_event("interaction.answered", {"interaction_id": "i-req-1", "request_id": "req-1"}))
    await progress.flush(force=True)
    assert send.call_args.args[0]["type"] == "session.thinking.append"
    assert "settled on screen" in send.call_args.args[0]["content"]
    assert await progress.hear("yes") is None and calls == []


@pytest.mark.asyncio
async def test_questions_point_to_the_screen_when_approvals_are_spoken(monkeypatch):
    desk, _, _ = _desk(monkeypatch)
    send = AsyncMock()
    progress = voice_progress.VoiceProgress("s", send, approvals=desk)
    progress.consume(_event("interaction.requested", {"kind": "question", "interaction_id": "q"}))
    await progress.flush(force=True)
    assert "on screen" in send.call_args.args[0]["content"]


@pytest.mark.asyncio
async def test_finished_job_waits_for_a_pause_instead_of_interrupting(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(voice_progress, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    talking = [True]
    send = AsyncMock()
    progress = voice_progress.VoiceProgress("s", send, quiet=lambda: not talking[0])
    progress.consume(_event("subagent.started", {"task_id": "job", "description": "Resize icons"}))
    progress.consume(_event("subagent.completed", {"task_id": "job", "status": "completed",
                                                    "result_preview": "Icons resized."}))
    await progress.flush(force=True)
    # Mid-sentence only quiet context may go; the finished job is held.
    assert all(c.args[0]["type"] == "session.thinking.append" for c in send.call_args_list)
    await progress.flush(force=True)
    assert all(c.args[0]["type"] == "session.thinking.append" for c in send.call_args_list)
    talking[0] = False
    clock[0] += 1
    await progress.flush(force=True)
    final = send.call_args.args[0]
    assert final["type"] == "session.commentary.append" and "Resize icons: completed" in final["content"]


@pytest.mark.asyncio
async def test_a_stuck_speaking_signal_cannot_swallow_a_finished_job(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(voice_progress, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    send = AsyncMock()
    progress = voice_progress.VoiceProgress("s", send, quiet=lambda: False)
    progress.consume(_event("subagent.completed", {"task_id": "job", "status": "completed"}))
    await progress.flush(force=True)
    send.assert_not_awaited()
    clock[0] += voice_progress.PAUSE_MAX_WAIT
    await progress.flush(force=True)
    assert send.call_args.args[0]["type"] == "session.commentary.append"


@pytest.mark.asyncio
async def test_call_start_mid_work_gets_one_status_picture(monkeypatch):
    from app.core import event_gateway
    queue = asyncio.Queue()
    monkeypatch.setattr(event_gateway, "get_gateway",
                        lambda: SimpleNamespace(subscribe=lambda sid: queue, unsubscribe=lambda sid, q: None))
    monkeypatch.setattr(voice_progress.session_events, "get_max_seq", lambda sid: 0)
    monkeypatch.setattr(voice_progress.background_tasks, "list_session_nonterminal_background_tasks",
                        lambda sid: [{"task_id": "a", "title": "Fix login", "status": "running"},
                                     {"task_id": "b", "title": "Update docs", "status": "running"}])
    monkeypatch.setattr(voice_progress.turns, "get_live_turn", lambda sid: None)
    monkeypatch.setattr(voice_status, "work_status", lambda sid: {
        "chat": "waiting_on_background", "queued_requests": 0, "running_count": 2,
        "running": [{"label": "Fix login"}, {"label": "Update docs"}],
        "finished_waiting": [], "approvals_waiting": 0, "on_screen_waiting": 0})
    send = AsyncMock()
    progress = voice_progress.VoiceProgress("s", send)
    await progress.open()
    assert progress.active_count == 2
    assert list(progress.pending) == ["status"]
    await progress.flush(force=True)
    content = send.call_args.args[0]["content"]
    assert send.call_args.args[0]["type"] == "session.thinking.append"
    assert "Fix login; Update docs" in content and "waiting for background jobs" in content
    progress.close()


# ---------------------------------------------------------------------------
# Status: the snapshot, the sentence, and status questions
# ---------------------------------------------------------------------------

def test_work_status_reads_turns_tasks_reports_and_decisions(mock_db, monkeypatch):
    from app.core.task_registry import task_registry
    from app.db import background_tasks, interactions, turns
    monkeypatch.setattr(turns, "get_live_turn", lambda sid: None)
    monkeypatch.setattr(turns, "get_latest_turn", lambda sid: {"state": "parked"})
    monkeypatch.setattr(turns, "get_queued_turns", lambda sid: [{"id": "q"}])
    monkeypatch.setattr(task_registry, "get_active", lambda sid: [
        {"id": "t1", "subject": "Fix the flaky login test", "kind": "agent", "status": "running"}])
    monkeypatch.setattr(task_registry, "finished_awaiting_report", lambda sid: [
        {"id": "t2", "subject": "Update docs", "status": "completed", "summary": "Docs updated."}])
    monkeypatch.setattr(background_tasks, "list_session_nonterminal_background_tasks", lambda sid: [
        {"task_id": "t1", "title": "dup", "status": "running"},
        {"task_id": "t3", "title": "Build the installer", "status": "running", "kind": "shell"}])
    monkeypatch.setattr(interactions, "get_pending_interactions", lambda sid: [
        {"kind": "permission"}, {"kind": "question"}])
    status = voice_status.work_status("s")
    assert status["chat"] == "waiting_on_background" and status["queued_requests"] == 1
    assert [t["label"] for t in status["running"]] == ["Fix the flaky login test", "Build the installer"]
    assert status["finished_waiting"] == [{"label": "Update docs", "status": "finished", "summary": "Docs updated."}]
    assert status["approvals_waiting"] == 1 and status["on_screen_waiting"] == 1
    sentence = voice_status.status_sentence(status)
    assert "waiting for background jobs" in sentence
    assert "2 background jobs are running: Fix the flaky login test; Build the installer." in sentence
    assert "Update docs (finished: Docs updated.)" in sentence
    assert "1 approval is waiting" in sentence and "t1" not in sentence


def test_finished_awaiting_report_counts_only_undelivered_own_background_work():
    from app.core.task_registry import TaskRegistry
    registry = TaskRegistry()
    registry.mark_background_launch("s", "tu-bg")
    registry.start_task("s", "bg", kind="agent", title="Background job", tool_use_id="tu-bg")
    registry.start_task("s", "fg", kind="agent", title="Foreground job", tool_use_id="tu-fg")
    registry.complete_task("s", "bg", "completed", summary="Done")
    registry.complete_task("s", "fg", "completed")
    assert [t["subject"] for t in registry.finished_awaiting_report("s")] == ["Background job"]
    registry.mark_background_notification_delivered("s", "bg")
    assert registry.finished_awaiting_report("s") == []


def test_nothing_running_is_said_plainly():
    assert voice_status.status_sentence({"chat": "idle"}).startswith("Nothing is running")


@pytest.mark.parametrize("text", [
    "What are you working on?", "So what are you doing right now?", "Is it done yet?",
    "Are they finished?", "How's it going?", "Any updates?", "What's the status?",
    "Is everything done?", "Are you still working?", "Okay, how is it going so far?",
])
def test_status_questions_are_recognised(text):
    assert voice_status.is_status_question(text)


@pytest.mark.parametrize("text", [
    "Fix the login test", "Is it done yet? And then push the branch", "What is the capital of France",
    "Are the tests passing on main and can you rerun them", "", "Work on the docs",
])
def test_requests_are_not_status_questions(text):
    assert not voice_status.is_status_question(text)


# ---------------------------------------------------------------------------
# GPT Live: status without a chat turn, approval answers never sent to chat
# ---------------------------------------------------------------------------

def _live_call(monkeypatch, *, desk=None):
    from app.core import turn_recorder
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", AsyncMock())
    call = voice_stream.VoiceStream(AsyncMock(), {"id": "s"},
                                    {"model": "gpt-live-1", "voice": "marin", "conversation_surface": True}, "", 5)
    call.send = AsyncMock()
    call.progress = voice_progress.VoiceProgress("s", call.send, approvals=desk)
    if desk is not None:
        desk._marker = lambda: call.transcript_seq
    monkeypatch.setattr(voice_stream, "SETTLE_QUIET_S", 0.0)
    monkeypatch.setattr(voice_stream, "SETTLE_MAX_S", 0.0)
    return call


@pytest.mark.asyncio
async def test_live_status_question_is_answered_from_task_state_without_a_chat_turn(monkeypatch):
    delegate = AsyncMock(return_value="")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    monkeypatch.setattr(voice_status, "work_status", lambda sid: {
        "chat": "working", "running": [{"label": "Fix login"}], "running_count": 1})
    call = _live_call(monkeypatch)
    await call.caption("user", "What are you working on?")
    call.settling = ["d1"]
    await call.dispatch_when_settled()
    delegate.assert_not_awaited()
    (sent,) = [c.args[0] for c in call.send.await_args_list]
    assert sent["type"] == "session.commentary.append" and sent["delegation_id"] == "d1"
    assert "Fix login" in sent["content"] and "working on a request" in sent["content"]


@pytest.mark.asyncio
async def test_live_request_still_goes_to_chat(monkeypatch):
    delegate = AsyncMock(return_value="")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    call = _live_call(monkeypatch)
    await call.caption("user", "Fix the login test.")
    call.settling = ["d1"]
    await call.dispatch_when_settled()
    delegate.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_spoken_yes_answers_the_approval_and_is_not_sent_to_chat(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    delegate = AsyncMock(return_value="")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    call = _live_call(monkeypatch, desk=desk)
    await call.caption("user", "Earlier words before the question.")
    call.delegated_seq = call.transcript_seq  # already handed to chat
    call.progress.consume(_requested())
    await call.progress.flush(force=True)
    assert desk.awaiting
    await call.caption("user", "Yes, go ahead.")
    call.settling = ["d1"]  # GPT Live also opened a handoff for it
    await call.dispatch_when_settled()
    delegate.assert_not_awaited()
    assert calls[0]["decision"] == "allow"
    note = call.send.await_args_list[-1].args[0]
    assert note["type"] == "session.thinking.append" and note["content"] == voice_stream.APPROVAL_ANSWER_NOTE
    if call.approval_task is not None:
        call.approval_task.cancel()


@pytest.mark.asyncio
async def test_live_answer_is_heard_when_the_voice_starts_replying(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    call = _live_call(monkeypatch, desk=desk)
    call.progress.consume(_requested())
    await call.progress.flush(force=True)
    await call.caption("user", "No.")
    await call.caption("assistant", "Okay.")
    await asyncio.wait_for(call.approval_task, 2)
    assert calls[0]["decision"] == "deny"


@pytest.mark.asyncio
async def test_live_voice_words_are_never_an_answer(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    call = _live_call(monkeypatch, desk=desk)
    call.progress.consume(_requested())
    await call.progress.flush(force=True)
    await call.caption("assistant", "Should I run the tests? Yes, I will approve that.")
    assert await call.hear_approval() is None and calls == []


def test_quiet_waits_for_both_sides(monkeypatch):
    call = voice_stream.VoiceStream(AsyncMock(), {"id": "s"}, {"model": "gpt-live-1", "voice": "marin"}, "", 5)
    clock = [1000.0]
    monkeypatch.setattr(voice_stream, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 0))
    call.last_caption_at, call.last_caption_role = clock[0], "assistant"
    assert not call.is_quiet()
    clock[0] += voice_stream.PAUSE_QUIET_S
    assert call.is_quiet()
    call.last_caption_at, call.last_caption_role = clock[0], "user"
    clock[0] += voice_stream.PAUSE_QUIET_S
    assert not call.is_quiet()  # the caller spoke last; the voice may still answer
    clock[0] += voice_stream.PAUSE_AFTER_CALLER_S
    assert call.is_quiet()


@pytest.mark.asyncio
async def test_desk_decides_through_the_one_swappable_check(monkeypatch):
    """Swapping in a shared typed-answer check changes every spoken decision."""
    desk, calls, _ = _desk(monkeypatch)
    monkeypatch.setattr(voice_approval, "classify_spoken_answer", lambda text: NO)
    desk.offer(REQUEST)
    desk.delivered("req-1")
    await desk.hear("yes")
    assert calls[0]["decision"] == "deny"


@pytest.mark.asyncio
async def test_voice_delegations_are_marked_as_not_the_owners_own_words(monkeypatch):
    """Voice-assembled prompts must never be read as an answer to a question."""
    from app.core import turn_manager
    monkeypatch.setattr(voice_stream.database, "get_session", lambda _: {"model": "haiku", "profile_id": "p"})
    submit = AsyncMock(return_value="turn")
    monkeypatch.setattr(turn_manager, "submit_async", submit)
    await voice_stream.delegate_to_selected_model("s", "user: yes", on_submitted=AsyncMock())
    assert submit.await_args.kwargs["payload"]["voice_delegation"] is True


@pytest.mark.asyncio
async def test_spoken_answer_to_the_assistants_question_is_recorded_like_a_typed_one(monkeypatch):
    from app.core import assistant_confirm
    desk, calls, _ = _desk(monkeypatch)
    recorded = []

    async def record(session_id, request, decision):
        recorded.append((session_id, request["request_id"], request["question"], decision))

    monkeypatch.setattr(assistant_confirm, "_record", record)
    assistant_confirm.reset()
    desk.offer({"request_id": "req-c", "tool_name": "Bash",
                "assistant_confirm": {"question": "Do you authorize me to run `pytest`? Please answer yes or no."}})
    assert "Do you authorize me to run" in desk.current.summary
    desk.delivered("req-c")
    await desk.hear("yes")
    assert calls[0]["decision"] == "allow"
    assert recorded == [("s", "req-c", "Do you authorize me to run `pytest`? Please answer yes or no.", "approved")]
    assert "approved this one request" in assistant_confirm.context_note("s")
    assistant_confirm.reset()


# ---------------------------------------------------------------------------
# #1797: a yes covers a retry of the same action; a background job is named
# ---------------------------------------------------------------------------

def _edit(request_id, image="/w/WIP_1.png", model="fal-ai/nano-banana-2", worker=None):
    request = {"request_id": request_id, "tool_name": "mcp__ai-tools__editImage", "title": "Edit image",
               "tool_input": {"prompt": "repaint", "image_path": image, "model": model}}
    if worker:
        request["provider_agent"] = {"worker_session_id": worker, "label": "Agents-page inventory"}
    return request


@pytest.mark.asyncio
async def test_a_yes_covers_a_retry_of_the_same_tool_on_the_same_target(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    desk.offer(_edit("req-1"))
    desk.delivered("req-1")
    assert (await desk.hear("yes")).verdict == YES
    # The edit failed and is retried with a different model: not asked again.
    assert desk.offer(_edit("req-2", model="openai/gpt-image-2.5/flare/edit")) is None
    assert not desk.awaiting
    [(request_id, note)] = await desk.settle_retries()
    assert request_id == "req-2" and "already said yes" in note
    assert [(c["request_id"], c["decision"]) for c in calls] == [("req-1", "allow"), ("req-2", "allow")]
    # A retry of the retry is covered too.
    assert desk.offer(_edit("req-3")) is None
    await desk.settle_retries()
    assert calls[-1]["request_id"] == "req-3"


@pytest.mark.asyncio
@pytest.mark.parametrize("other", [
    _edit("req-2", image="/w/WIP_2.png"),                     # a different target
    _edit("req-2", worker="worker-1"),                       # a different asker
    {**_edit("req-2"), "tool_name": "mcp__ai-tools__generateWithReference"},  # a different tool
    {"request_id": "req-2", "tool_name": "mcp__ai-tools__editImage", "tool_input": {"prompt": "p"}},
])
async def test_a_yes_never_carries_to_a_different_action(monkeypatch, other):
    desk, calls, _ = _desk(monkeypatch)
    desk.offer(_edit("req-1"))
    desk.delivered("req-1")
    await desk.hear("yes")
    assert "Approval needed" in desk.offer(other)
    assert await desk.settle_retries() == [] and len(calls) == 1


@pytest.mark.asyncio
async def test_a_no_or_an_old_yes_is_not_carried(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    desk.offer(_edit("req-1"))
    desk.delivered("req-1")
    await desk.hear("no")
    assert desk.offer(_edit("req-2")) is not None  # asked again after a no
    desk.delivered("req-2")
    await desk.hear("yes")
    monkeypatch.setattr(voice_approval.time, "monotonic",
                        lambda: desk.approved[voice_approval.retry_key(_edit("x"))]
                        + voice_approval.RETRY_WINDOW_SECONDS + 1)
    assert desk.offer(_edit("req-3")) is not None  # the window has passed


@pytest.mark.asyncio
async def test_retry_is_approved_quietly_through_the_progress_relay(monkeypatch):
    desk, calls, _ = _desk(monkeypatch)
    send = AsyncMock()
    progress = voice_progress.VoiceProgress("s", send, approvals=desk)

    def requested(request):
        return _event("interaction.requested", {
            "kind": "permission", "interaction_id": "i-" + request["request_id"],
            "request_id": request["request_id"], "request": request})

    progress.consume(requested(_edit("req-1")))
    await progress.flush(force=True)
    await progress.hear("yes")
    await progress.flush(force=True)
    progress.consume(requested(_edit("req-2", model="fal-ai/flux-pro/kontext")))
    await progress.flush(force=True)
    sent = send.call_args.args[0]
    assert sent["type"] == "session.thinking.append" and "already said yes" in sent["content"]
    assert [c["request_id"] for c in calls] == ["req-1", "req-2"]


def test_a_background_jobs_request_names_the_job():
    summary = voice_approval.request_summary(
        {"request_id": "r", "tool_name": "Bash", "title": "Run grep",
         "provider_agent": {"worker_session_id": "w", "label": "Agents-page inventory"}})
    assert summary.startswith("For the background job “Agents-page inventory”: Run grep")

"""Ephemeral composer hints: durable final text and asynchronous race gates."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.core import composer_suggestions as cs
from app.db import session_events as events
from tests.core.test_turn_scheduler import mock_db, test_db, _settled_turn


@pytest.fixture(autouse=True)
def clean_state():
    cs._seen.clear()
    cs._tasks.clear()
    yield
    cs._seen.clear()


@pytest.fixture(autouse=True)
def seed_owners(mock_db):
    from app.db import profiles, api_users
    profiles.create_profile("default", "Default", None, {})
    api_users.create_api_user("owner", "Owner", "test-only-hash")


def append(turn, kind, payload):
    events.append_events("s", [{"type": kind, "turn_id": turn["id"], "payload": payload}])


@pytest.mark.parametrize("kind,payload", [
    ("assistant.text_delta", {"text": "The final answer."}),
    ("message.assistant", {"kind": "text", "content": "The final answer."}),
])
def test_final_answer_only(mock_db, kind, payload):
    turn = _settled_turn("s", terminal_event=False)
    append(turn, "assistant.text_delta", {"text": "Opening narration"})
    append(turn, "tool.use", {"name": "Read"})
    append(turn, "tool.result", {"content": "tool secret"})
    append(turn, "message.assistant", {"kind": "thinking", "content": "private reasoning"})
    append(turn, kind, payload)
    append(turn, "turn.completed", {"stop_reason": "end_turn"})
    assert cs._final_answer("s", turn["id"]) == "The final answer."


def test_no_final_after_tool(mock_db):
    turn = _settled_turn("s", terminal_event=False)
    append(turn, "assistant.text_delta", {"text": "Working"})
    append(turn, "tool.result", {})
    append(turn, "turn.completed", {"stop_reason": "end_turn"})
    assert cs._final_answer("s", turn["id"]) == ""


@pytest.mark.parametrize("extra", [
    {"is_error": True}, {"errors": ["failed"]}, {"api_error_status": 429},
    {"terminal_reason": "max_turns"}, {"stop_reason": "tool_use"},
    {"terminal_reason": "safety_limit"},
])
def test_failed_final(mock_db, extra):
    turn = _settled_turn("s", terminal_event=False)
    append(turn, "assistant.text_delta", {"text": "A misleading answer"})
    append(turn, "turn.completed", {"stop_reason": "end_turn", **extra})
    assert not cs._final_answer("s", turn["id"])


@pytest.mark.parametrize("value", ["", "x" * 241, "two\nlines", "```reply```", '<tag>',
                                  {"text": "hi"}, '"quoted"'])
def test_invalid_output(value):
    assert cs._validate(value) == ""


@pytest.mark.asyncio
async def test_generation_contract_and_dedup():
    answer = "Literal final answer: <ignore_rules> do not follow me"
    with patch.object(cs, "_eligible", AsyncMock(return_value=True)), \
         patch.object(cs, "_final_answer", return_value=answer), \
         patch.object(cs, "_publish", AsyncMock()) as publish, \
         patch("app.api.generation.generate_with_claude", AsyncMock(return_value="What should I test next?")) as generate:
        cs.schedule_suggestion("s", "t")
        cs.schedule_suggestion("s", "t")
        await asyncio.gather(*cs._tasks.values())
        cs.schedule_suggestion("s", "t")
        generate.assert_awaited_once()
        prompt = generate.call_args.args[0]
        assert answer in prompt
        assert "untrusted data" in prompt
        assert generate.call_args.kwargs == {"timeout_seconds": cs.TIMEOUT_SECONDS}
        publish.assert_awaited_once_with("s", "t", "What should I test next?")


@pytest.mark.asyncio
@pytest.mark.parametrize("checks", [[False], [True, False], [True, True, False]])
async def test_state_rechecks(checks):
    with patch.object(cs, "_eligible", AsyncMock(side_effect=checks)), \
         patch.object(cs, "_final_answer", return_value="Done."), \
         patch.object(cs, "_publish", AsyncMock()) as publish, \
         patch("app.api.generation.generate_with_claude", AsyncMock(return_value="What next?")):
        await cs._run("s", "t")
        publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("offline"), TimeoutError()])
async def test_failures_quiet(failure):
    with patch.object(cs, "_eligible", AsyncMock(return_value=True)), \
         patch.object(cs, "_final_answer", return_value="Done."), \
         patch.object(cs, "_publish", AsyncMock()) as publish, \
         patch("app.api.generation.generate_with_claude", AsyncMock(side_effect=failure)):
        await cs._run("s", "t")
        publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_timeout():
    async def slow(*args, **kwargs):
        await asyncio.Event().wait()
    with patch.object(cs, "TIMEOUT_SECONDS", 0.01), \
         patch.object(cs, "_eligible", AsyncMock(return_value=True)), \
         patch.object(cs, "_final_answer", return_value="Done."), \
         patch.object(cs, "_publish", AsyncMock()) as publish, \
         patch("app.api.generation.generate_with_claude", slow):
        await cs._run("s", "t")
        publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_recorder_preserves_non_http_error(mock_db):
    from app.core.turn_recorder import TurnRecorder
    from app.db import sessions
    sessions.create_session("s", "default")
    recorder = await TurnRecorder.maybe_create("s")
    try:
        await recorder.start("hello")
        await recorder.on_event({"type": "done", "metadata": {
            "stop_reason": "end_turn", "is_error": True, "errors": ["failed"],
        }})
        terminal = events.get_events("s")[-1]["payload"]
        assert terminal["is_error"] is True
        assert terminal["errors"] == ["failed"]
    finally:
        await recorder.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["failover", "queue", "auto_continue", "none"])
async def test_scheduler_precedence(mock_db, policy):
    from app.core import turn_scheduler as scheduler
    turn = _settled_turn("s")
    with patch.object(scheduler, "_maybe_failover", AsyncMock(return_value=policy == "failover")), \
         patch.object(scheduler, "_maybe_auto_continue", AsyncMock(return_value=policy == "auto_continue")), \
         patch.object(scheduler.turns_db, "pop_ready", return_value={} if policy == "queue" else None), \
         patch.object(scheduler, "kick"), \
         patch.object(cs, "schedule_suggestion") as schedule:
        await scheduler.on_turn_settled("s", turn["id"], "completed")
        assert schedule.call_count == (1 if policy == "none" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["parked", "aborted", "error"])
async def test_noncompleted_never_schedules(mock_db, outcome):
    from app.core import turn_scheduler as scheduler
    turn = _settled_turn("s")
    with patch.object(scheduler, "_maybe_failover", AsyncMock(return_value=False)), \
         patch.object(scheduler, "_maybe_auto_continue", AsyncMock(return_value=False)), \
         patch.object(cs, "schedule_suggestion") as schedule:
        await scheduler.on_turn_settled("s", turn["id"], outcome)
        schedule.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "code", "chat", "voice"])
@pytest.mark.parametrize("enabled", [None, False, True, "true", 1])
async def test_owner_preference_and_mode(mock_db, mode, enabled):
    from app.db import sessions, settings
    sessions.create_session("s", "default", api_user_id="owner", mode=mode)
    turn = _settled_turn("s")
    if enabled is not None:
        settings.set_user_preference("api_user", "owner", "chat_behavior", {"suggestNextResponse": enabled})
    assert await cs._eligible("s", turn["id"]) == (mode in (None, "code") and enabled is True)


@pytest.mark.asyncio
async def test_latest_and_unpark(mock_db):
    from app.db import sessions, settings, turns
    sessions.create_session("s", "default", api_user_id="owner")
    settings.set_user_preference("api_user", "owner", "chat_behavior", {"suggestNextResponse": True})
    first = _settled_turn("s")
    assert await cs._eligible("s", first["id"])
    mock_db.execute("UPDATE turns SET created_at = '2000-01-01' WHERE id = ?", (first["id"],))
    final = _settled_turn("s", source="unpark")
    assert not await cs._eligible("s", first["id"])
    assert await cs._eligible("s", final["id"])
    turns.enqueue_turn("s", "queue", prompt="more")
    assert not await cs._eligible("s", final["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["agent", "sdk", "monitor", "serving", "launch"])
async def test_background_and_notification_gates(mock_db, lane):
    from app.core.task_registry import task_registry as registry
    registry.clear_session("s")
    try:
        with patch.object(cs, "_eligible_durable", return_value=True):
            if lane == "launch":
                registry.mark_background_launch("s", "launch")
                assert not await cs._eligible("s", "t")
                return
            registry.start_task("s", "job", "agent" if lane == "agent" else "shell", "work", tool_use_id="launch")
            if lane == "agent":
                registry.mark_background_launch("s", "launch")
            elif lane == "sdk":
                registry.mark_sdk_background_task("s", "job")
            else:
                registry.mark_monitor_shell_task("s", "job")
            assert not await cs._eligible("s", "t")
            registry.complete_task("s", "job", "completed", notify=False)
            assert not await cs._eligible("s", "t")
            registry.mark_woken("s", "job")
            assert not await cs._eligible("s", "t")
            registry.mark_terminal_notified("s", "job")
            assert not await cs._eligible("s", "t")  # A UI chip is not model delivery.
            from app.core.turn_scheduler import on_task_notification_delivered
            on_task_notification_delivered("s", ["job"])
            assert await cs._eligible("s", "t")
            registry.start_task("s", "job", "shell", "resumed")
            registry.complete_task("s", "job", "completed", notify=False)
            assert not await cs._eligible("s", "t")
    finally:
        registry.clear_session("s")


@pytest.mark.parametrize("trailing", ["assistant.thinking_delta", "tool.input_delta", "tool.use"])
def test_trailing_nonfinal_blocks(mock_db, trailing):
    turn = _settled_turn("s", terminal_event=False)
    append(turn, "assistant.text_delta", {"text": "Opening narration"})
    append(turn, trailing, {"text": "private"})
    append(turn, "turn.completed", {"stop_reason": "end_turn"})
    assert not cs._final_answer("s", turn["id"])


@pytest.mark.asyncio
async def test_publish_frame():
    with patch("app.core.sync_engine.sync_engine.broadcast_event", AsyncMock()) as broadcast:
        await cs._publish("s", "t", "What next?")
        frame = broadcast.call_args.args[0]
        assert frame.session_id == "s"
        assert frame.event_type == "stream_frame"
        assert frame.data == {"type": "composer_suggestion", "session_id": "s", "turn_id": "t", "suggestion": "What next?"}

@pytest.mark.asyncio
@pytest.mark.parametrize("admin,attribution,expected", [
    (None, {}, False), ("alice", {}, True),
    ("alice", {"user_id": "admin"}, True),
    ("alice", {"user_id": "mallory"}, False),
    ("alice", {"api_user_id": "owner"}, False),
])
async def test_admin_identity_fails_closed(mock_db, admin, attribution, expected):
    from app.db import sessions, settings
    sessions.create_session("s", "default")
    turn = _settled_turn("s", payload=attribution)
    settings.set_user_preference("admin", "alice", "chat_behavior", {"suggestNextResponse": True})
    with patch("app.core.auth.auth_service.get_admin_username", return_value=admin):
        assert await cs._eligible("s", turn["id"]) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["preference", "mode", "queue", "task"])
async def test_real_state_changes_during_generation(mock_db, change):
    from app.db import sessions, settings, turns
    from app.core.task_registry import task_registry as registry
    sessions.create_session("s", "default", api_user_id="owner")
    settings.set_user_preference("api_user", "owner", "chat_behavior", {"suggestNextResponse": True})
    turn = _settled_turn("s", terminal_event=False)
    append(turn, "assistant.text_delta", {"text": "Finished."})
    append(turn, "turn.completed", {"stop_reason": "end_turn"})

    async def generate(*args, **kwargs):
        if change == "preference":
            settings.set_user_preference("api_user", "owner", "chat_behavior", {"suggestNextResponse": False})
        elif change == "mode":
            mock_db.execute("UPDATE sessions SET mode = 'chat' WHERE id = 's'")
        elif change == "queue":
            turns.enqueue_turn("s", "queue", prompt="more")
        else:
            registry.start_task("s", "job", "shell", "service")
        return "What next?"

    try:
        with patch("app.api.generation.generate_with_claude", generate), \
             patch.object(cs, "_publish", AsyncMock()) as publish:
            await cs._run("s", turn["id"])
            publish.assert_not_awaited()
    finally:
        registry.clear_session("s")


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    {"type": "stream_delta", "delta_type": "text", "content": "Final answer."},
    {"type": "chunk", "content": "Final answer."},
])
async def test_native_and_delegated_recorder_frames(mock_db, frame):
    from app.core.turn_recorder import TurnRecorder
    recorder = await TurnRecorder.maybe_create("s")
    try:
        await recorder.start("hello")
        await recorder.on_event({"type": "chunk", "content": "Opening narration"})
        await recorder.on_event({"type": "tool_use", "id": "tool", "name": "Read", "input": {}})
        await recorder.on_event({"type": "tool_result", "tool_use_id": "tool", "output": "secret"})
        await recorder.on_event(frame)
        # Full text echoes are intentionally not persisted twice.
        await recorder.on_event({"type": "text", "content": "Final answer."})
        await recorder.on_event({"type": "done", "metadata": {"stop_reason": "end_turn"}})
        assert cs._final_answer("s", recorder.turn_id) == "Final answer."
    finally:
        await recorder.close()


@pytest.mark.asyncio
async def test_bounded_bookkeeping():
    gate = asyncio.Event()
    async def wait(*args):
        await gate.wait()
    with patch.object(cs, "_run", wait), patch.object(cs, "MAX_IN_FLIGHT", 2), patch.object(cs, "MAX_SEEN", 3):
        for i in range(5):
            cs.schedule_suggestion("s", str(i))
        assert len(cs._tasks) == 2
        gate.set()
        await asyncio.gather(*cs._tasks.values())
        assert not cs._tasks
        for i in range(5, 10):
            cs.schedule_suggestion("s", str(i))
            await asyncio.gather(*cs._tasks.values())
        assert len(cs._seen) == 3
        assert not cs._tasks

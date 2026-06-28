"""
Tests for the between-turns "drain pump" that fixes the SLOW background sub-agent
unpark race in app/api/chat_ws_handler.py.

Root cause recap: a user turn streams via the SDK's ``receive_response()``, which
returns at the PARENT's ``ResultMessage`` — it does NOT stay open while a
background sub-agent runs. A FAST sub-agent's terminal ``TaskNotificationMessage``
is caught in-stream (the existing stash path, covered by test_chat_ws_unpark.py).
A SLOW one arrives LATER on the pooled, still-alive SDK client with nothing
reading it: the registry stays "running" (frozen indicator) and the parent never
unparks.

The fix is ``ChatWebSocketHandler._drain_notifications`` — a between-turns reader
that uses ``client.receive_messages()`` (which, unlike ``receive_response()``,
does NOT stop at a ResultMessage). These tests fake the pooled SDK client so no
real subprocess / SDK is required, and assert:

1. Core race: a terminal notification yielded AFTER the parent turn ended is
   caught — ``complete_task`` ran (``get_active`` no longer lists it) AND the
   unpark follow-up was delivered with the right pending payload.
2. Single-reader invariant: starting a new turn calls ``stop_drain_pump``, which
   cancels + awaits + removes the pump before the turn proceeds.
3. Idempotency: a completion already ``mark_notified`` (e.g. caught in-stream)
   does not double-fire from the pump.
4. Multiple tasks: pump fires for the first terminal notification; a fresh pump
   started on the follow-up's completion catches the second — both deliver,
   neither lost.
5. Pump exits cleanly when the pooled client is gone / ``is_streaming`` is True.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
import app.api.ws_cleanup as ws_cleanup
from app.core.task_registry import task_registry
from claude_agent_sdk import (
    AssistantMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _notif(task_id, *, status="completed", uuid="u-1", output_file="", summary="s",
           tool_use_id="tu-1", usage=None):
    """Build a real SDK TaskNotificationMessage (so isinstance checks match)."""
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file=output_file,
        summary=summary,
        uuid=uuid,
        session_id="sess-1",
        tool_use_id=tool_use_id,
        usage=usage,
    )


def _started(task_id, *, uuid="s-1", tool_use_id="tu-1"):
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description="bg work",
        uuid=uuid,
        session_id="sess-1",
        tool_use_id=tool_use_id,
    )


def _updated(task_id, *, status="killed", uuid="uu-1"):
    """Build a real SDK TaskUpdatedMessage (so isinstance checks match)."""
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={},
        task_id=task_id,
        patch={"status": status, "end_time": 1},
        status=status,
        session_id="sess-1",
        uuid=uuid,
    )


class _FakeClient:
    """Pooled SDK client stand-in. receive_messages() yields scripted messages.

    Mirrors the SDK contract that receive_messages() does NOT stop at a
    ResultMessage — it simply yields everything we script, then (if `hang` is
    True) blocks forever so the pump must be cancelled to stop (the real
    between-turns case where no further messages arrive).
    """

    def __init__(self, messages, *, hang=False):
        self._messages = list(messages)
        self._hang = hang
        self.received = 0

    async def receive_messages(self):
        for msg in self._messages:
            self.received += 1
            yield msg
        if self._hang:
            # Block forever — emulates an idle client between turns. The pump
            # should already have broken out on the terminal notification; if
            # not, it must be cancelled by stop_drain_pump.
            await asyncio.Event().wait()


def _fake_state(client, *, is_streaming=False, alive=True):
    state = MagicMock()
    state.is_streaming = is_streaming
    state.client = client
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=alive)
    state.adapter = adapter
    return state


def _make_handler():
    handler = cwh.ChatWebSocketHandler(
        websocket=MagicMock(),
        device_id="dev-1",
        user_id="admin",
        api_user_id=None,
    )
    handler.send_json = AsyncMock(return_value=True)
    # Capture _deliver_unpark calls without spawning a real follow-up run_query.
    handler._deliver_unpark = AsyncMock(return_value=True)
    return handler


@pytest.fixture(autouse=True)
def _isolate_drain_pumps():
    """Each test gets a clean registry + pump dict.

    Also shrinks the unpark consolidation window to a tiny value so tests that
    drive a ``hang=True`` client don't sit out the full real-world window after
    a deliverable. The fake clients yield synchronously, so any positive window
    still batches every buffered completion — only the magnitude changes.
    """
    task_registry.reset_all()
    ws_cleanup._drain_pumps.clear()
    _saved_window = cwh._UNPARK_BATCH_WINDOW_SECONDS
    cwh._UNPARK_BATCH_WINDOW_SECONDS = 0.05
    yield
    cwh._UNPARK_BATCH_WINDOW_SECONDS = _saved_window
    ws_cleanup._drain_pumps.clear()
    task_registry.reset_all()


def _patch_pool(state):
    """Patch the session pool so peek(session_id) returns our fake state."""
    pool = MagicMock()
    pool.peek = MagicMock(return_value=state)
    return patch("app.core.session_pool.get_session_pool", return_value=pool)


# ---------------------------------------------------------------------------
# 1. Core race — terminal notification AFTER the parent turn ended
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_catches_slow_terminal_notification_and_unparks():
    # Registry already has the task "running" (it started in the parent turn).
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    # The parent launched it with run_in_background=true (its launch tool_use_id is
    # the notification's tool_use_id "tu-1") — the gate that distinguishes a
    # genuinely-backgrounded sub-agent (must unpark) from a synchronous one.
    task_registry.mark_background_launch("sess-1", "tu-1")
    assert any(t["id"] == "task-1" for t in task_registry.get_active("sess-1"))

    client = _FakeClient([_notif("task-1", uuid="u-1", summary="done it")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    # (a) complete_task ran — the task is no longer "running".
    assert task_registry.get_active("sess-1") == []
    # (b) the unpark delivery fired with the right pending payload.
    handler._deliver_unpark.assert_awaited_once()
    kwargs = handler._deliver_unpark.await_args.kwargs
    assert kwargs["session_id"] == "sess-1"
    assert kwargs["final_metadata"] is None
    pending = kwargs["pending"]
    assert isinstance(pending, list) and len(pending) == 1
    assert pending[0]["task_id"] == "task-1"
    assert pending[0]["status"] == "completed"
    assert pending[0]["uuid"] == "u-1"
    # Pump removed itself from the registry on exit.
    assert "sess-1" not in ws_cleanup._drain_pumps


# ---------------------------------------------------------------------------
# 1b. SLOW sub-agent inner activity — re-dispatched so the inner group fills in
#     (assistant text + tool calls/results) instead of "no activity recorded",
#     and the node is COMPLETED by the terminal notification.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_redispatches_subagent_inner_activity_and_completes():
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    task_registry.mark_background_launch("sess-1", "tu-1")  # genuine background launch

    # A SLOW background sub-agent's inner messages arrive on the pooled client
    # AFTER the parent turn returned — each carries parent_tool_use_id="tu-1"
    # (the spawning Task's tool_id = the subagent node's agent_id), then a
    # terminal notification keyed to the same tool_use_id.
    inner_text = AssistantMessage(
        content=[TextBlock("STEP-1 done")], model="claude", parent_tool_use_id="tu-1"
    )
    inner_tool = AssistantMessage(
        content=[ToolUseBlock(id="b1", name="Bash", input={"command": "echo hi"})],
        model="claude",
        parent_tool_use_id="tu-1",
    )
    inner_result = UserMessage(
        content=[ToolResultBlock(tool_use_id="b1", content="hi\n", is_error=False)],
        parent_tool_use_id="tu-1",
    )
    notif = _notif("task-1", uuid="u-1", tool_use_id="tu-1", summary="done")

    client = _FakeClient([inner_text, inner_tool, inner_result, notif], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    captured = []

    async def _capture(event, ectx, st):
        captured.append(event)

    with _patch_pool(state), patch(
        "app.api.ws_event_handlers.dispatch_event",
        new=AsyncMock(side_effect=_capture),
    ):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    by_type = {e["type"]: e for e in captured}
    # (a) inner activity was re-dispatched into the SAME inner group (agent_id="tu-1").
    assert "subagent_chunk" in by_type
    assert by_type["subagent_chunk"]["agent_id"] == "tu-1"
    assert by_type["subagent_chunk"]["content"] == "STEP-1 done"
    assert "subagent_tool_use" in by_type
    assert by_type["subagent_tool_use"]["name"] == "Bash"
    assert by_type["subagent_tool_use"]["id"] == "b1"
    assert "subagent_tool_result" in by_type
    assert by_type["subagent_tool_result"]["tool_use_id"] == "b1"
    assert "hi" in by_type["subagent_tool_result"]["output"]
    # (b) the terminal notification completed the node (keyed by tool_use_id).
    assert "task_notification" in by_type
    assert by_type["task_notification"]["tool_use_id"] == "tu-1"
    assert by_type["task_notification"]["status"] == "completed"
    # Order: inner activity precedes completion.
    assert [e["type"] for e in captured].index("subagent_chunk") < [
        e["type"] for e in captured
    ].index("task_notification")
    # (c) the registry still completed + the unpark still fired (regression guard).
    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Single-reader invariant — stop_drain_pump cancels + awaits + removes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_drain_pump_cancels_and_removes_before_turn():
    # A pump that would block forever (no terminal notification scripted).
    client = _FakeClient([_started("task-1")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        handler._start_drain_pump("sess-1", "claude-code", None, None)
        pump = ws_cleanup._drain_pumps.get("sess-1")
        assert pump is not None
        # Let the pump start running and reach the blocking receive_messages().
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # A new turn would call stop_drain_pump at its start.
        cancelled = await ws_cleanup.stop_drain_pump("sess-1")

    assert cancelled is True
    assert pump.cancelled() or pump.done()
    # Removed from the registry so the next turn's receive_response is the only reader.
    assert "sess-1" not in ws_cleanup._drain_pumps
    # A cancelled pump must NOT deliver an unpark.
    handler._deliver_unpark.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_drain_pump_noop_when_none():
    assert await ws_cleanup.stop_drain_pump("nope") is False


# ---------------------------------------------------------------------------
# 3. Idempotency — a completion already marked notified does not re-fire
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_skips_already_notified_completion():
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    # Genuine background launch so the foreground gate passes and the dedup path
    # (mark_unparked) is the thing under test, not the suppress-foreground gate.
    task_registry.mark_background_launch("sess-1", "tu-1")
    # Pre-arm: this task already drove an unpark (e.g. caught in-stream). The latch
    # is keyed on task_id ALONE now, so a fresh per-rest uuid can't re-arm it.
    out_file = ""
    assert task_registry.mark_unparked("sess-1", "task-1") is True

    # hang=False: the dup is skipped (no break), so the stream must END for the
    # pump to terminate. A real idle client either keeps yielding (cancelled at
    # the next turn) or closes; here we model the close so the test terminates.
    client = _FakeClient([_notif("task-1", uuid="u-1", output_file=out_file)], hang=False)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    # Registry still updated (complete_task always runs via _record_task_message),
    # so the indicator unfreezes...
    assert task_registry.get_active("sess-1") == []
    # ...but NO duplicate unpark is delivered.
    handler._deliver_unpark.assert_not_awaited()
    assert "sess-1" not in ws_cleanup._drain_pumps


# ---------------------------------------------------------------------------
# 4. Multiple tasks — each completion delivers via a fresh pump, none lost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_tasks_each_deliver_via_sequential_pumps():
    task_registry.start_task("sess-1", "task-1", "agent", "first")
    task_registry.start_task("sess-1", "task-2", "agent", "second")
    # Both notifications carry tool_use_id "tu-1" (the _notif default); record it as
    # a genuine background launch so the foreground gate passes for both.
    task_registry.mark_background_launch("sess-1", "tu-1")

    handler = _make_handler()

    # First pump: only task-1's notification is available so far. Pump fires for
    # it and breaks (per-completion). task-2 is still running.
    client1 = _FakeClient([_notif("task-1", uuid="u-1")], hang=True)
    with _patch_pool(_fake_state(client1)):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    assert handler._deliver_unpark.await_count == 1
    first_pending = handler._deliver_unpark.await_args.kwargs["pending"]
    assert first_pending[0]["task_id"] == "task-1"
    # task-1 done, task-2 still active.
    active_ids = {t["id"] for t in task_registry.get_active("sess-1")}
    assert active_ids == {"task-2"}

    # The follow-up turn's completion restarts a pump; now task-2 notifies.
    client2 = _FakeClient([_notif("task-2", uuid="u-2")], hang=True)
    with _patch_pool(_fake_state(client2)):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    assert handler._deliver_unpark.await_count == 2
    second_pending = handler._deliver_unpark.await_args.kwargs["pending"]
    assert second_pending[0]["task_id"] == "task-2"
    # Both tasks delivered; none left running.
    assert task_registry.get_active("sess-1") == []


# ---------------------------------------------------------------------------
# 5. Clean exits — client gone / is_streaming True
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_exits_when_no_pooled_state():
    handler = _make_handler()
    pool = MagicMock()
    pool.peek = MagicMock(return_value=None)
    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        await handler._drain_notifications("sess-1", "claude-code", None, None)
    handler._deliver_unpark.assert_not_awaited()


@pytest.mark.asyncio
async def test_pump_exits_when_client_dead():
    handler = _make_handler()
    state = _fake_state(_FakeClient([], hang=True), alive=False)
    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)
    handler._deliver_unpark.assert_not_awaited()


@pytest.mark.asyncio
async def test_pump_exits_when_turn_owns_client():
    handler = _make_handler()
    # is_streaming True => a turn (R1) owns the client; the pump (R2) must not read.
    client = _FakeClient([_notif("task-1")], hang=True)
    state = _fake_state(client, is_streaming=True)
    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)
    handler._deliver_unpark.assert_not_awaited()
    # Never read the client.
    assert client.received == 0


# ---------------------------------------------------------------------------
# 6. run_query lifecycle — pump started on normal completion with an active
#    agent task; stop_drain_pump called at the start of every turn.
# ---------------------------------------------------------------------------

def _fake_stream(events):
    async def _gen(*args, **kwargs):
        for ev in events:
            yield ev
    return _gen


def _run_query_patches(handler, fake_stream):
    """Mock every boundary run_query touches (mirrors test_chat_ws_unpark.py)."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("app.core.query_engine.stream_to_websocket", new=fake_stream))

    store = stack.enter_context(patch.object(cwh, "live_session_store"))
    store.start_streaming = AsyncMock()
    store.add_user_message = AsyncMock()
    store.end_streaming = AsyncMock()
    store.cleanup_session = AsyncMock()
    store.get_session_dict = MagicMock(return_value=None)
    store.has_session = MagicMock(return_value=False)
    store.is_streaming = MagicMock(return_value=False)

    sync = stack.enter_context(patch.object(cwh, "sync_engine"))
    sync.unregister_device = AsyncMock()
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_queue_updated = AsyncMock()
    sync.broadcast_stream_end = AsyncMock()
    sync.is_session_streaming = MagicMock(return_value=False)

    conn = stack.enter_context(patch.object(cwh, "connector_manager"))
    conn.dispatch_event = AsyncMock()
    stack.enter_context(patch.object(cwh, "dispatch_session_complete", new=AsyncMock()))

    db = stack.enter_context(patch.object(cwh, "database"))
    db.get_session = MagicMock(return_value={"sdk_session_id": "sdk-1"})

    async def _fake_run_sync(fn, *a, **k):
        return None

    stack.enter_context(patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))

    mr = stack.enter_context(patch.object(cwh, "message_resolver"))
    mr.resolve_for_streaming_init = MagicMock(return_value=[])
    stack.enter_context(patch.object(cwh, "resolve_working_dir", return_value="/tmp"))
    stack.enter_context(patch("app.api.ws_event_handlers.dispatch_event", new=AsyncMock()))

    import app.api.websocket as ws_mod
    stack.enter_context(patch.object(ws_mod, "_active_chat_sessions", new={}))
    return stack


@pytest.mark.asyncio
async def test_run_query_starts_pump_on_completion_with_active_agent_task():
    """A turn that ends with an in-flight agent task (no in-stream terminal
    notification) starts a between-turns drain pump."""
    # The SDK started a background agent task this turn; the registry has it
    # running. The in-stream loop sees no terminal notification, then `done`.
    handler = _make_handler()
    handler._start_drain_pump = MagicMock()

    events = [
        {"type": "task_started", "task_id": "task-1", "description": "bg work"},
        {"type": "done", "session_id": "sess-1", "metadata": {}},
    ]

    # dispatch_event is mocked, so feed the registry directly to emulate the
    # SDK lifecycle the real dispatch path would have recorded.
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")

    with _run_query_patches(handler, _fake_stream(events)):
        await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    handler._start_drain_pump.assert_called_once()
    args = handler._start_drain_pump.call_args.args
    assert args[0] == "sess-1"


@pytest.mark.asyncio
async def test_run_query_no_pump_when_no_active_agent_task():
    """A turn with no in-flight agent work does NOT start a pump."""
    handler = _make_handler()
    handler._start_drain_pump = MagicMock()

    events = [{"type": "done", "session_id": "sess-1", "metadata": {}}]

    with _run_query_patches(handler, _fake_stream(events)):
        await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    handler._start_drain_pump.assert_not_called()


@pytest.mark.asyncio
async def test_run_query_calls_stop_drain_pump_at_start():
    """Every turn cancels any between-turns pump before reading the client."""
    handler = _make_handler()
    handler._start_drain_pump = MagicMock()

    events = [{"type": "done", "session_id": "sess-1", "metadata": {}}]

    with _run_query_patches(handler, _fake_stream(events)):
        import app.api.websocket as ws_mod
        with patch.object(ws_mod, "stop_drain_pump", new=AsyncMock(return_value=False)) as stop_mock:
            await handler.run_query("hello", "sess-1", "claude-code", None, None, None)
            stop_mock.assert_awaited_once_with("sess-1")


# ---------------------------------------------------------------------------
# 6b. ERROR-PATH pump start (Bug C) — a turn that ERRORS while background work is
#     still in flight must ALSO start the between-turns pump (symmetric to the
#     success path), or the parent never unparks when the slow sub-agent finishes.
#     A user Stop (CancelledError) must NOT — the user explicitly stopped.
# ---------------------------------------------------------------------------

def _raising_stream(exc):
    """A stream_to_websocket stand-in that raises ``exc`` the moment it is iterated
    (after the registry already reflects the in-flight task)."""
    async def _gen(*args, **kwargs):
        raise exc
        yield  # unreachable — marks _gen as an async generator


    return _gen


@pytest.mark.asyncio
async def test_run_query_starts_pump_on_error_with_active_agent_task():
    """Bug C: a turn that raises mid-stream (NOT a user Stop) while a background
    agent task is still in flight starts a between-turns drain pump on the error
    path, so the parent unparks when the task's terminal notification lands."""
    handler = _make_handler()
    handler._start_drain_pump = MagicMock()

    # The SDK launched a background agent this turn; the registry has it running.
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")

    with _run_query_patches(handler, _raising_stream(RuntimeError("boom mid-stream"))):
        with patch.object(cwh, "dispatch_session_error", new=AsyncMock()):
            await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    handler._start_drain_pump.assert_called_once()
    assert handler._start_drain_pump.call_args.args[0] == "sess-1"


@pytest.mark.asyncio
async def test_run_query_no_pump_on_error_when_no_active_agent_task():
    """A turn that errors with NO in-flight background work does not start a pump
    (nothing to wait for)."""
    handler = _make_handler()
    handler._start_drain_pump = MagicMock()

    with _run_query_patches(handler, _raising_stream(RuntimeError("boom"))):
        with patch.object(cwh, "dispatch_session_error", new=AsyncMock()):
            await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    handler._start_drain_pump.assert_not_called()


@pytest.mark.asyncio
async def test_run_query_no_pump_on_cancel_even_with_active_agent_task():
    """A user Stop (CancelledError) must NOT start an unpark pump even with active
    background work — the cancel branch intentionally leaves the parked work to the
    explicit-stop teardown rather than resurrecting the turn."""
    handler = _make_handler()
    handler._start_drain_pump = MagicMock()
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")

    with _run_query_patches(handler, _raising_stream(asyncio.CancelledError())):
        await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    handler._start_drain_pump.assert_not_called()


@pytest.mark.asyncio
async def test_stopped_session_does_not_deliver_unpark():
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    # Genuine background launch so the unpark is blocked by the STOPPED guard (the
    # thing under test), not by the suppress-foreground gate.
    task_registry.mark_background_launch("sess-1", "tu-1")
    handler = _make_handler()
    client = _FakeClient([_notif("task-1", uuid="u-1")], hang=True)
    state = _fake_state(client)

    cwh._stopped_sessions.add("sess-1")
    try:
        with _patch_pool(state):
            await handler._drain_notifications("sess-1", "claude-code", None, None)
    finally:
        cwh._stopped_sessions.discard("sess-1")

    # complete_task still ran (indicator unfreezes), but no unpark while stopped.
    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. TaskUpdatedMessage-ALONE killed/stopped terminal (THE GAP this fix closes)
#    A TaskStop'd background task reports "killed" via a TaskUpdatedMessage with
#    NO following TaskNotificationMessage. The pump must clear the indicator AND
#    unpark the parent so it acknowledges the stop instead of hanging forever.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_unparks_on_killed_task_updated_alone():
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    assert any(t["id"] == "task-1" for t in task_registry.get_active("sess-1"))

    # ONLY a TaskUpdatedMessage(killed) arrives — no notification ever follows.
    client = _FakeClient([_updated("task-1", status="killed", uuid="uu-1")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    # (a) indicator clears (registry task completed via _record_task_message).
    assert task_registry.get_active("sess-1") == []
    assert task_registry.get_task("sess-1", "task-1")["status"] == "killed"
    # (b) the parent unparks with a result-free killed payload.
    handler._deliver_unpark.assert_awaited_once()
    pending = handler._deliver_unpark.await_args.kwargs["pending"]
    assert len(pending) == 1
    assert pending[0]["task_id"] == "task-1"
    assert pending[0]["status"] == "killed"
    assert pending[0]["output_file"] is None  # killed task has no result file
    assert "sess-1" not in ws_cleanup._drain_pumps


@pytest.mark.asyncio
async def test_pump_stopped_status_update_alone_unparks():
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    client = _FakeClient([_updated("task-1", status="stopped", uuid="uu-2")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_awaited_once()
    assert handler._deliver_unpark.await_args.kwargs["pending"][0]["status"] == "stopped"


@pytest.mark.asyncio
async def test_pump_completed_update_then_notification_unparks_exactly_once():
    """Regression: a NORMAL completion emits BOTH a TaskUpdatedMessage(completed)
    and a TaskNotificationMessage(completed). The update must NOT fire its own
    unpark (it carries no result) — the notification owns the rich unpark — so the
    parent unparks EXACTLY once, and the indicator still clears."""
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    # Genuine background launch (tool_use_id "tu-1") so the notification passes the
    # spurious-foreground gate and OWNS the rich unpark. Without it the gate
    # (correctly) suppresses the notification as a synchronous sub-agent and the
    # pump keeps draining — against the hang=True client that blocks forever.
    task_registry.mark_background_launch("sess-1", "tu-1")

    client = _FakeClient(
        [
            _updated("task-1", status="completed", uuid="uu-1"),
            _notif("task-1", status="completed", uuid="u-1", summary="done it"),
        ],
        hang=True,
    )
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    # Indicator cleared, and exactly ONE unpark — driven by the notification
    # (carries the summary/result), not the result-free update.
    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_awaited_once()
    pending = handler._deliver_unpark.await_args.kwargs["pending"]
    assert len(pending) == 1
    assert pending[0]["task_id"] == "task-1"
    assert pending[0]["status"] == "completed"
    assert pending[0]["summary"] == "done it"  # came from the notification frame


@pytest.mark.asyncio
async def test_pump_ignores_non_terminal_task_updated():
    """A 'running'/'pending' TaskUpdatedMessage must neither complete the task nor
    unpark — the pump keeps draining until a real terminal arrives."""
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")

    # A non-terminal update, then the genuine killed terminal.
    client = _FakeClient(
        [
            _updated("task-1", status="running", uuid="uu-0"),
            _updated("task-1", status="killed", uuid="uu-1"),
        ],
        hang=True,
    )
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    # Only the killed terminal completed + unparked.
    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_awaited_once()
    assert handler._deliver_unpark.await_args.kwargs["pending"][0]["status"] == "killed"


# ---------------------------------------------------------------------------
# 7b. CONSOLIDATION + PER-TASK DEDUP + PHANTOM SUPPRESSION (Bug 1 policy)
#     "Consolidate + dedup (keep auto-resume)": fire AT MOST ONE unpark per task,
#     batch near-simultaneous completions into ONE follow-up turn, and never wake
#     on a phantom (untracked) top-level rest.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_consolidates_simultaneous_completions_into_one_turn():
    """Policy #3: two background tasks whose terminal notifications are both
    buffered on the pooled client when the pump runs fold into ONE follow-up turn
    (``pending`` carries BOTH), instead of one unpark turn per completion."""
    task_registry.start_task("sess-1", "task-1", "agent", "first")
    task_registry.start_task("sess-1", "task-2", "agent", "second")
    # Both notifications carry the _notif default tool_use_id "tu-1"; record it as a
    # genuine background launch so both pass the foreground gate.
    task_registry.mark_background_launch("sess-1", "tu-1")

    client = _FakeClient(
        [
            _notif("task-1", uuid="u-1", summary="r1"),
            _notif("task-2", uuid="u-2", summary="r2"),
        ],
        hang=True,
    )
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    # ONE delivery carrying BOTH completions (order preserved).
    handler._deliver_unpark.assert_awaited_once()
    pending = handler._deliver_unpark.await_args.kwargs["pending"]
    assert [p["task_id"] for p in pending] == ["task-1", "task-2"]
    assert task_registry.get_active("sess-1") == []
    assert "sess-1" not in ws_cleanup._drain_pumps


@pytest.mark.asyncio
async def test_pump_same_task_fresh_uuid_does_not_refire():
    """Policy #2: the newer CLI re-emits a terminal notification for the SAME task
    with a FRESH uuid each time it comes to rest. Only the FIRST drives an unpark; a
    later re-emission with a different uuid is deduped because the latch is keyed on
    task_id ALONE (a fresh uuid used to re-arm the old (task_id, signature) latch and
    storm the session)."""
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    task_registry.mark_background_launch("sess-1", "tu-1")
    handler = _make_handler()

    # First pump: the task's first at-rest notification delivers once.
    client1 = _FakeClient([_notif("task-1", uuid="u-1")], hang=True)
    with _patch_pool(_fake_state(client1)):
        await handler._drain_notifications("sess-1", "claude-code", None, None)
    assert handler._deliver_unpark.await_count == 1

    # The CLI resumes the agent; it comes to rest AGAIN with a DIFFERENT uuid.
    # hang=False so the deduped notification just ends the stream (no second turn).
    client2 = _FakeClient([_notif("task-1", uuid="u-2-different")], hang=False)
    with _patch_pool(_fake_state(client2)):
        await handler._drain_notifications("sess-1", "claude-code", None, None)
    # STILL one delivery — the fresh uuid did not re-arm the per-task latch.
    assert handler._deliver_unpark.await_count == 1


@pytest.mark.asyncio
async def test_pump_suppresses_killed_update_for_untracked_task():
    """Policy #1: a killed/stopped TaskUpdatedMessage for a task the registry never
    tracked (the session's OWN top-level agent coming to rest, or a phantom) must NOT
    unpark — only genuine tracked background tasks do. The killed path has no
    background-launch gate, so without the get_task guard this phantom stormed the
    session with 'nothing to act on' turns."""
    # NOTE: no start_task — "phantom-1" is unknown to the registry.
    client = _FakeClient([_updated("phantom-1", status="killed", uuid="uu-9")], hang=False)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    handler._deliver_unpark.assert_not_awaited()


# ---------------------------------------------------------------------------
# 8. SPURIOUS-UNPARK SUPPRESSION (the reported bug)
#    A prior-turn orphan task_notification leaking IN-STREAM in a LATER, unrelated
#    turn must NOT fire an unpark (it was not started THIS turn) — but the registry
#    MUST still record its completion (the indicator never strands).
# ---------------------------------------------------------------------------

def _terminal_event(task_id="task-1", *, status="completed", uuid="u-1",
                    output_file="", summary="s", tool_use_id="tu-1", usage=None):
    """An in-stream terminal task_notification EVENT (dict), as run_query sees it."""
    return {
        "type": "task_notification",
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "tool_use_id": tool_use_id,
        "output_file": output_file,
        "uuid": uuid,
        "usage": usage,
    }


def test_stash_suppresses_prior_turn_orphan_not_started_this_turn():
    """In-stream stash: a notification for a task NOT started this turn (a
    prior-turn orphan leaking from the pooled-client buffer) is NOT stashed."""
    from app.api.ws_event_handlers import MutableStreamState

    task_registry.reset_all()
    stream_state = MutableStreamState()
    # NOTE: we deliberately do NOT call mark_started_this_turn — this models an
    # orphan whose TaskStartedMessage belonged to an EARLIER turn (already reset).
    cwh._stash_terminal_notification(stream_state, _terminal_event("orphan-1"), "sess-1")

    # Nothing stashed -> the post-loop unpark has nothing to deliver.
    assert stream_state.pending_task_notifications == []
    task_registry.reset_all()


def test_stash_fires_for_task_started_this_turn():
    """In-stream stash: the genuine FAST path (started THIS turn AND launched in the
    background) IS stashed."""
    from app.api.ws_event_handlers import MutableStreamState

    task_registry.reset_all()
    stream_state = MutableStreamState()
    task_registry.mark_started_this_turn("sess-1", "task-1")
    # The parent launched it with run_in_background=true (tool_use_id "tu-1").
    task_registry.mark_background_launch("sess-1", "tu-1")
    cwh._stash_terminal_notification(stream_state, _terminal_event("task-1"), "sess-1")

    assert len(stream_state.pending_task_notifications) == 1
    assert stream_state.pending_task_notifications[0]["task_id"] == "task-1"
    task_registry.reset_all()


def test_stash_does_not_double_stash_same_task():
    """Even started-this-turn, a duplicate is deduped by mark_unparked (keyed on
    task_id alone) — at most ONE pending entry per task, even across re-emissions
    with a fresh per-rest uuid."""
    from app.api.ws_event_handlers import MutableStreamState

    task_registry.reset_all()
    stream_state = MutableStreamState()
    task_registry.mark_started_this_turn("sess-1", "task-1")
    task_registry.mark_background_launch("sess-1", "tu-1")  # genuine background launch
    ev = _terminal_event("task-1", uuid="u-1")
    cwh._stash_terminal_notification(stream_state, dict(ev), "sess-1")
    cwh._stash_terminal_notification(stream_state, dict(ev), "sess-1")

    assert len(stream_state.pending_task_notifications) == 1
    task_registry.reset_all()


@pytest.mark.asyncio
async def test_run_query_orphan_notification_does_not_unpark_but_clears_registry():
    """Integration: a LATER turn whose stream surfaces a terminal notification for
    a task that was NOT started this turn (a buffered prior-turn orphan) must NOT
    spawn an unpark follow-up — but the registry must still complete the task so
    the 'Waiting for N background task(s)…' indicator never strands.

    Models the reported bug: an old background Explore/search task re-surfacing as
    a spurious <task-notification> unpark in an unrelated turn.
    """
    task_registry.reset_all()
    # Pretend the orphan was started in a PRIOR turn and is still "running" in the
    # registry (its completion never got recorded because its notification was
    # buffered). The current turn does NOT start it, so it is not started-this-turn.
    task_registry.start_task("sess-1", "orphan-1", "agent", "old search")

    handler = _make_handler()
    handler._start_drain_pump = MagicMock()
    handler._deliver_unpark = AsyncMock(return_value=True)

    # The orphan's terminal notification leaks into THIS turn's stream, then done.
    # _fake_stream does NOT mark it started-this-turn (no task_started event), so
    # it is treated as the prior-turn orphan it is.
    events = [
        _terminal_event("orphan-1", uuid="u-old", summary="old result"),
        {"type": "done", "session_id": "sess-1", "metadata": {}},
    ]

    # dispatch_event is mocked in _run_query_patches, so the in-stream
    # _record_task_message that clears the registry doesn't run via the fake
    # stream. Emulate that side-effect: the real stream_to_websocket would have
    # called complete_task for the terminal notification.
    def _stream_with_registry(evs):
        async def _gen(*args, **kwargs):
            sid = kwargs.get("session_id")
            for ev in evs:
                if ev.get("type") == "task_notification" and sid:
                    # mirror _record_task_message's complete_task side-effect
                    task_registry.complete_task(sid, ev.get("task_id"), "completed")
                yield ev
        return _gen

    with _run_query_patches(handler, _stream_with_registry(events)):
        await handler.run_query("unrelated prompt", "sess-1", "claude-code", None, None, None)

    # (a) NO spurious unpark for the prior-turn orphan.
    handler._deliver_unpark.assert_not_awaited()
    # (b) registry still cleared the orphan (indicator unfreezes).
    assert task_registry.get_active("sess-1") == []
    assert task_registry.get_task("sess-1", "orphan-1")["status"] == "completed"
    task_registry.reset_all()


@pytest.mark.asyncio
async def test_run_query_genuine_fast_path_still_unparks():
    """Integration regression: an agent that BOTH starts and completes in THIS
    turn STILL fires the in-stream unpark follow-up."""
    task_registry.reset_all()
    handler = _make_handler()
    handler._start_drain_pump = MagicMock()
    handler._deliver_unpark = AsyncMock(return_value=True)
    # The parent launched this agent in the background (tool_use_id "tu-1", the
    # notification's tool_use_id). _background_launches is NOT reset per turn, so
    # pre-setting it here survives run_query's start-of-turn reset.
    task_registry.mark_background_launch("sess-1", "tu-1")

    events = [
        {"type": "task_started", "task_id": "task-1", "description": "bg work"},
        _terminal_event("task-1", uuid="u-1", summary="fresh result"),
        {"type": "done", "session_id": "sess-1", "metadata": {}},
    ]

    def _stream_with_registry(evs):
        async def _gen(*args, **kwargs):
            sid = kwargs.get("session_id")
            for ev in evs:
                if ev.get("type") == "task_started" and sid:
                    task_registry.start_task(sid, ev.get("task_id"), "agent", "bg work")
                    task_registry.mark_started_this_turn(sid, ev.get("task_id"))
                elif ev.get("type") == "task_notification" and sid:
                    task_registry.complete_task(sid, ev.get("task_id"), "completed")
                yield ev
        return _gen

    with _run_query_patches(handler, _stream_with_registry(events)):
        await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    # The genuine FAST path unparks exactly once.
    handler._deliver_unpark.assert_awaited_once()
    pending = handler._deliver_unpark.await_args.kwargs["pending"]
    assert len(pending) == 1
    assert pending[0]["task_id"] == "task-1"
    task_registry.reset_all()


# ---------------------------------------------------------------------------
# 9. SYNCHRONOUS-SUBAGENT SUPPRESSION (the reported bug)
#    The SDK emits an at-rest TaskNotificationMessage for EVERY sub-agent when the
#    parent comes to rest — including SYNCHRONOUS ones whose full result was already
#    delivered INLINE during the turn, and grandchild background tasks a SUB-AGENT
#    launched (which the parent never tracked). Only a notification whose tool_use_id
#    the PARENT recorded as a run_in_background=true launch may unpark. Anything else
#    is suppressed: the indicator still clears (via _record_task_message), but no
#    spurious follow-up turn fires.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_suppresses_synchronous_subagent_notification():
    """Drain pump: a terminal notification whose tool_use_id was NOT recorded as a
    background launch (a synchronous sub-agent, result already inline) must NOT
    unpark — and the pump must NOT break-to-deliver (it keeps draining)."""
    task_registry.start_task("sess-1", "task-1", "agent", "sync subagent")
    # NOTE: deliberately NO mark_background_launch — this models a SYNCHRONOUS
    # sub-agent (run_in_background=false) whose result already returned inline.
    assert task_registry.was_background_launch("sess-1", "tu-1") is False

    # hang=False so the pump terminates by exhausting the stream (the gate
    # `continue`s rather than breaking, so a hanging client would block forever).
    client = _FakeClient([_notif("task-1", uuid="u-1", tool_use_id="tu-1")], hang=False)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    # Indicator still cleared (registry completed via _record_task_message)...
    assert task_registry.get_active("sess-1") == []
    # ...but NO spurious unpark for the synchronous sub-agent.
    handler._deliver_unpark.assert_not_awaited()
    # The whole stream was drained (the gate did not break out early).
    assert client.received == 1
    assert "sess-1" not in ws_cleanup._drain_pumps


@pytest.mark.asyncio
async def test_pump_suppresses_subagent_launched_grandchild():
    """Drain pump: a grandchild background task launched BY a sub-agent (its
    tool_use_id was never recorded by the parent) is suppressed."""
    task_registry.start_task("sess-1", "grandchild-1", "agent", "nested bg")
    # The parent recorded a DIFFERENT background launch ("tu-parent"), never the
    # grandchild's "tu-grandchild".
    task_registry.mark_background_launch("sess-1", "tu-parent")

    client = _FakeClient(
        [_notif("grandchild-1", uuid="u-1", tool_use_id="tu-grandchild")], hang=False
    )
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_not_awaited()


@pytest.mark.asyncio
async def test_pump_unparks_genuine_background_launch():
    """Drain pump: a terminal notification whose tool_use_id WAS recorded as a
    background launch DOES unpark (the legit slow path)."""
    task_registry.start_task("sess-1", "task-1", "agent", "real bg agent")
    task_registry.mark_background_launch("sess-1", "tu-1")

    client = _FakeClient([_notif("task-1", uuid="u-1", tool_use_id="tu-1")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_awaited_once()
    assert handler._deliver_unpark.await_args.kwargs["pending"][0]["task_id"] == "task-1"


def test_stash_suppresses_synchronous_subagent_notification():
    """In-stream stash: a notification started-this-turn but NOT a background launch
    (a synchronous sub-agent whose result already returned inline) is NOT stashed."""
    from app.api.ws_event_handlers import MutableStreamState

    task_registry.reset_all()
    stream_state = MutableStreamState()
    # Started this turn (passes the orphan guard) but NOT a background launch.
    task_registry.mark_started_this_turn("sess-1", "task-1")
    cwh._stash_terminal_notification(
        stream_state, _terminal_event("task-1", tool_use_id="tu-1"), "sess-1"
    )

    # Nothing stashed -> the post-loop unpark has nothing to deliver.
    assert stream_state.pending_task_notifications == []
    task_registry.reset_all()


def test_stash_fires_for_background_launched_task():
    """In-stream stash: started-this-turn AND a recorded background launch IS
    stashed (the genuine fast background path)."""
    from app.api.ws_event_handlers import MutableStreamState

    task_registry.reset_all()
    stream_state = MutableStreamState()
    task_registry.mark_started_this_turn("sess-1", "task-1")
    task_registry.mark_background_launch("sess-1", "tu-1")
    cwh._stash_terminal_notification(
        stream_state, _terminal_event("task-1", tool_use_id="tu-1"), "sess-1"
    )

    assert len(stream_state.pending_task_notifications) == 1
    assert stream_state.pending_task_notifications[0]["task_id"] == "task-1"
    task_registry.reset_all()


# ---------------------------------------------------------------------------
# 10. BACKGROUNDED-SHELL UNPARK (local_bash — the reported "frontend never shows
#     unpark" bug). A session parked on a backgrounded Bash/PowerShell command
#     (run_in_background, task_type=="local_bash" → registry kind=="shell",
#     marked in _sdk_background_tasks) MUST unpark on its terminal notification —
#     exactly like a background sub-agent. Two linkages drive it:
#       - the parent's launch tool_use_id (was_background_launch), AND
#       - a robust task_id fallback (is_sdk_background_task) for when the SDK
#         omits tool_use_id on a shell's notification.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_unparks_backgrounded_shell_via_background_launch():
    """A backgrounded local_bash shell with its launch tool_use_id recorded unparks
    on the terminal notification (the primary gate, same as a sub-agent)."""
    task_registry.start_task("sess-1", "sh-1", "shell", "npm run build")
    task_registry.mark_sdk_background_task("sess-1", "sh-1")
    task_registry.mark_background_launch("sess-1", "tu-1")

    client = _FakeClient([_notif("sh-1", uuid="u-1", tool_use_id="tu-1")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_awaited_once()
    assert handler._deliver_unpark.await_args.kwargs["pending"][0]["task_id"] == "sh-1"
    assert "sess-1" not in ws_cleanup._drain_pumps


@pytest.mark.asyncio
async def test_pump_unparks_backgrounded_shell_via_task_id_fallback():
    """THE robustness path: the SDK leaves tool_use_id UNRECORDED on the shell's
    notification (so was_background_launch is False), but the task is in
    _sdk_background_tasks — the is_sdk_background_task fallback still drives the
    unpark. Without this fallback the parked session would hang forever."""
    task_registry.start_task("sess-1", "sh-1", "shell", "long build")
    task_registry.mark_sdk_background_task("sess-1", "sh-1")
    # NOTE: no mark_background_launch — and the notification's tool_use_id is one
    # the parent never recorded, so the primary gate fails on purpose.
    assert task_registry.was_background_launch("sess-1", "tu-unrecorded") is False

    client = _FakeClient(
        [_notif("sh-1", uuid="u-1", tool_use_id="tu-unrecorded")], hang=True
    )
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_awaited_once()
    assert handler._deliver_unpark.await_args.kwargs["pending"][0]["task_id"] == "sh-1"


@pytest.mark.asyncio
async def test_pump_suppresses_unmarked_shell_notification():
    """A shell notification that is NEITHER a recorded background launch NOR in
    _sdk_background_tasks (e.g. an app Monitor shell that never parks the parent)
    must NOT unpark — the indicator still clears, but no spurious follow-up."""
    task_registry.start_task("sess-1", "mon-1", "shell", "tail -f app.log")
    # No mark_sdk_background_task, no mark_background_launch.

    # hang=False: suppression `continue`s rather than breaking, so the stream must
    # end for the pump to terminate.
    client = _FakeClient([_notif("mon-1", uuid="u-1", tool_use_id="tu-x")], hang=False)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    # Indicator cleared (registry completed via _record_task_message)...
    assert task_registry.get_active("sess-1") == []
    # ...but NO spurious unpark for the app-managed shell.
    handler._deliver_unpark.assert_not_awaited()
    assert client.received == 1


def test_stash_fires_for_backgrounded_shell_via_fallback():
    """In-stream stash: a backgrounded shell that completes in-stream (started this
    turn, in _sdk_background_tasks) IS stashed via the is_sdk_background_task
    fallback even when its tool_use_id was not recorded as a launch."""
    from app.api.ws_event_handlers import MutableStreamState

    task_registry.reset_all()
    stream_state = MutableStreamState()
    task_registry.mark_started_this_turn("sess-1", "sh-1")
    task_registry.mark_sdk_background_task("sess-1", "sh-1")
    # No mark_background_launch — the fallback must carry it.
    cwh._stash_terminal_notification(
        stream_state, _terminal_event("sh-1", tool_use_id="tu-unrecorded"), "sess-1"
    )

    assert len(stream_state.pending_task_notifications) == 1
    assert stream_state.pending_task_notifications[0]["task_id"] == "sh-1"
    task_registry.reset_all()

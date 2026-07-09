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
    # Real SessionState defaults this to False; a bare MagicMock attribute is
    # truthy and would trip the pump's stale-turn hold (F2) in every test.
    state.stale_turn_undrained = False
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
    # The summary rides the dispatched event so the parked card gets its short
    # result text (result_text=summary in handle_task_notification).
    assert by_type["task_notification"]["summary"] == "done"
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
        # run_query routes resolve_for_streaming_init through run_sync (thread
        # offload) — call through so the seed gets its list. Everything else
        # (queue pops / db writes) stays a falsy no-op so the drain path under
        # test is driven only by the explicit mocks below.
        if fn is mr.resolve_for_streaming_init:
            return fn(*a, **k)
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
async def test_run_query_starts_pump_when_bg_launch_completed_before_turn_end():
    """FIX 1 (the 34-minute stale-UI incident): a background sub-agent whose
    terminal update was consumed in-stream JUST BEFORE the parent ResultMessage
    flips its registry entry to completed via ``complete_task``. At turn end
    ``get_active_background_work`` is therefore EMPTY — but the task's terminal
    TaskNotificationMessage may still be buffered on the pooled client and arrive
    LATER, needing the pump to read it and unpark. The pump-start gate must
    therefore ALSO fire when a background launch was RECORDED this session
    (``get_background_launches`` non-empty), even though nothing is still running.

    Before the fix the gate keyed on ``get_active_background_work`` alone, saw [],
    and started no pump → the buffered notification was never read → the parent
    never unparked → the UI went stale forever."""
    handler = _make_handler()
    handler._start_drain_pump = MagicMock()

    events = [
        {"type": "task_started", "task_id": "task-1", "description": "bg work"},
        {"type": "done", "session_id": "sess-1", "metadata": {}},
    ]

    # The SDK started a background agent this turn AND recorded the parent's
    # run_in_background launch, THEN its terminal update completed the task
    # in-stream just before the ResultMessage — so at turn end the running-only
    # view is empty but the durable background-launch set is not.
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    task_registry.mark_background_launch("sess-1", "tu-1")
    task_registry.complete_task("sess-1", "task-1", "completed")
    assert task_registry.get_active_background_work("sess-1") == []
    assert task_registry.get_background_launches("sess-1")  # non-empty

    with _run_query_patches(handler, _fake_stream(events)):
        await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    handler._start_drain_pump.assert_called_once()
    assert handler._start_drain_pump.call_args.args[0] == "sess-1"


@pytest.mark.asyncio
async def test_run_query_no_pump_when_no_bg_launches_recorded():
    """FIX 1 guard: the broadened gate must NOT fire on a plain turn that never
    launched any background work (no active bg work AND no recorded launches) —
    otherwise every ordinary turn would leak an idle pump."""
    handler = _make_handler()
    handler._start_drain_pump = MagicMock()

    events = [{"type": "done", "session_id": "sess-1", "metadata": {}}]

    assert task_registry.get_active_background_work("sess-1") == []
    assert not task_registry.get_background_launches("sess-1")

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


def _system_chip_events(captured):
    """The subset of captured dispatch_event payloads that render a system chip."""
    return [
        e for e in captured
        if e.get("type") == "system" and e.get("subtype") == "task_updated"
    ]


@pytest.mark.asyncio
async def test_pump_synthesizes_task_updated_chip_from_notification_alone():
    """RC2 (parked 'Task Updated' chip): a NORMAL background completion between
    turns arrives ONLY as a TaskNotificationMessage (no task_updated patch). The
    pump must SYNTHESIZE the same ``{"type":"system","subtype":"task_updated"}``
    chip the in-stream loop renders so a completion landing while PARKED drops a
    visible card — the Part-2 failure this fix closes."""
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    task_registry.mark_background_launch("sess-1", "tu-1")  # genuine background launch

    client = _FakeClient(
        [_notif("task-1", status="completed", uuid="u-1", tool_use_id="tu-1",
                summary="done it")],
        hang=True,
    )
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

    # (a) EXACTLY one 'task_updated' system chip synthesized for the completion.
    chips = _system_chip_events(captured)
    assert len(chips) == 1
    chip = chips[0]
    data = chip["data"]
    # (b) shape the frontend SystemMessage.svelte 'task_updated' case reads:
    #     data.task_id, data.status / data.patch.status, data.summary.
    assert data["task_id"] == "task-1"
    assert data["status"] == "completed"
    assert data["patch"]["status"] == "completed"
    assert data["summary"] == "done it"
    # (c) regression: the unpark still fired (Part 1 still works).
    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_awaited_once()


@pytest.mark.asyncio
async def test_pump_no_double_chip_when_update_and_notification_both_arrive():
    """Dedup: when a completion emits BOTH a TaskUpdatedMessage(completed) AND a
    TaskNotificationMessage(completed) — the maintainers' 'normal completion'
    model — exactly ONE 'task_updated' chip renders (the generic converter's chip
    from the update; the notification synth must NOT double it)."""
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    task_registry.mark_background_launch("sess-1", "tu-1")

    client = _FakeClient(
        [
            _updated("task-1", status="completed", uuid="uu-1"),
            _notif("task-1", status="completed", uuid="u-1", tool_use_id="tu-1",
                   summary="done it"),
        ],
        hang=True,
    )
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

    # Exactly ONE chip for task-1 despite two terminal frames.
    chips = _system_chip_events(captured)
    assert len(chips) == 1
    handler._deliver_unpark.assert_awaited_once()


@pytest.mark.asyncio
async def test_pump_suppressed_foreground_notification_synthesizes_no_chip():
    """A synchronous sub-agent's at-rest notification is suppress-foreground'd (no
    unpark) — it must ALSO synthesize no chip, mirroring the in-stream path where
    a foreground/synchronous sub-agent renders no standalone 'Task Updated' card."""
    task_registry.start_task("sess-1", "task-1", "agent", "sync sub-agent")
    # NO mark_background_launch → the notification is suppressed as foreground.

    client = _FakeClient(
        [_notif("task-1", status="completed", uuid="u-1", tool_use_id="tu-x",
                summary="inline")],
        hang=False,
    )
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

    assert _system_chip_events(captured) == []
    handler._deliver_unpark.assert_not_awaited()


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
# 8. IN-STREAM TERMINAL LATCH (the spurious post-turn follow-up bug)
#    A task that comes to rest mid-turn is delivered to the model by the CLI
#    itself; the app only LATCHES it (_latch_terminal_notification →
#    mark_unparked) so the drain pump can never re-deliver it, and NEVER fires
#    its own post-turn follow-up. The registry MUST still record its completion
#    (the indicator never strands) — that happens in _record_task_message.
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


def test_latch_ignores_prior_turn_orphan_not_started_this_turn():
    """In-stream latch: a FOREGROUND notification for a task NOT started this turn
    (a prior-turn orphan leaking from the pooled-client buffer, with no recorded
    background launch) latches nothing — it fails the background gate, exactly as
    the drain pump would reject it, so no latch is needed. (A background cross-turn
    completion DOES latch — see test_latch_arms_cross_turn_in_stream_completion.)"""
    task_registry.reset_all()
    # NOTE: we deliberately do NOT call mark_started_this_turn — this models an
    # orphan whose TaskStartedMessage belonged to an EARLIER turn (already reset).
    # No mark_background_launch either → it is foreground and never latches.
    cwh._latch_terminal_notification(_terminal_event("orphan-1"), "sess-1")

    assert task_registry.was_unparked("sess-1", "orphan-1") is False
    task_registry.reset_all()


def test_latch_records_cli_delivered_background_task():
    """In-stream latch: a genuine background task that comes to rest mid-turn
    (started THIS turn AND launched in the background) is latched via
    mark_unparked — the CLI already delivered it to the model, so the drain
    pump's mark_unparked gate must find it spent and never re-deliver.
    Idempotent across the CLI's per-rest re-emissions (fresh uuid each time)."""
    task_registry.reset_all()
    task_registry.mark_started_this_turn("sess-1", "task-1")
    # The parent launched it with run_in_background=true (tool_use_id "tu-1").
    task_registry.mark_background_launch("sess-1", "tu-1")
    cwh._latch_terminal_notification(_terminal_event("task-1", uuid="u-1"), "sess-1")
    cwh._latch_terminal_notification(_terminal_event("task-1", uuid="u-2"), "sess-1")

    assert task_registry.was_unparked("sess-1", "task-1") is True
    # The latch spent the one-unpark-per-task token: the pump's gate now denies.
    assert task_registry.mark_unparked("sess-1", "task-1") is False
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
async def test_run_query_in_stream_completion_latches_without_followup():
    """THE 2026-07-02 incident regression: a background task that BOTH starts and
    completes in THIS turn fires NO app follow-up turn — the CLI already injected
    the completion into the model's context mid-turn (queued-message steering
    semantics), so a post-turn <task-notification> re-told the model about work
    it had already reported. The app must only LATCH the completion so the drain
    pump can never re-deliver it either."""
    task_registry.reset_all()
    handler = _make_handler()
    handler._start_drain_pump = MagicMock()
    handler._deliver_unpark = AsyncMock(return_value=True)
    # The parent launched this agent in the background (tool_use_id "tu-1", the
    # notification's tool_use_id) — the strongest case: even a GENUINE background
    # task must not fire a post-turn follow-up when it completed in-stream.
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

    # NO app-side delivery — the CLI owned it.
    handler._deliver_unpark.assert_not_awaited()
    # ...but the completion is latched, so the pump's mark_unparked gate is spent.
    assert task_registry.was_unparked("sess-1", "task-1") is True
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


def test_latch_skips_foreground_work():
    """In-stream latch: a notification started-this-turn but NOT a background
    launch (a synchronous sub-agent, or a plain foreground shell — the incident
    shape) latches nothing: its result already returned inline as a tool_result,
    and it can never reach the drain pump's unpark gates anyway."""
    task_registry.reset_all()
    # Started this turn (passes the orphan guard) but NOT a background launch.
    task_registry.mark_started_this_turn("sess-1", "task-1")
    cwh._latch_terminal_notification(
        _terminal_event("task-1", tool_use_id="tu-1"), "sess-1"
    )

    assert task_registry.was_unparked("sess-1", "task-1") is False
    task_registry.reset_all()


def test_latch_arms_cross_turn_in_stream_completion():
    """THE DUPLICATE-NOTIFICATION REGRESSION: a background sub-agent launched in
    an EARLIER turn (so NOT started-this-turn) that comes to rest IN-STREAM during
    a LATER turn is a genuine FIRST delivery by the CLI — it MUST latch the pump
    off, or the between-turns drain pump re-delivers the same <task-notification>
    as a spurious follow-up turn (the duplicate the user saw).

    The old ``suppress-orphan`` early return skipped ``mark_unparked`` for any task
    not started this turn, so this cross-turn in-stream delivery never armed the
    latch. Because the pump admits any task passing the background gate and dedups
    ONLY on ``mark_unparked``, the un-armed latch left the token unspent and the
    pump fired a second delivery.

    Fix contract: the latch uses the SAME background-launch gate as the pump, so a
    cross-turn background completion latches. ``mark_unparked`` afterward must
    return False (the token was already spent by the in-stream latch)."""
    task_registry.reset_all()
    # Simulate a PRIOR-turn launch: the background launch is recorded (it survives
    # per-turn resets) but the task is NOT in the started-this-turn set — exactly
    # the state after ``reset_started_this_turn`` ran at the later turn's start.
    task_registry.mark_background_launch("sess-1", "tu-1")
    task_registry.reset_started_this_turn("sess-1")
    assert task_registry.was_started_this_turn("sess-1", "task-1") is False

    cwh._latch_terminal_notification(
        _terminal_event("task-1", tool_use_id="tu-1", uuid="u-1"), "sess-1"
    )

    # The in-stream latch armed the one-unpark-per-task token: the pump's gate now
    # finds it already spent and takes the dedup branch (delivers nothing).
    assert task_registry.was_unparked("sess-1", "task-1") is True
    assert task_registry.mark_unparked("sess-1", "task-1") is False
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


def test_latch_records_backgrounded_shell_via_fallback():
    """In-stream latch: a backgrounded shell that completes in-stream (started
    this turn, in _sdk_background_tasks) IS latched via the is_sdk_background_task
    fallback even when its tool_use_id was not recorded as a launch — the CLI
    delivered it mid-turn, so the pump must find its unpark token spent."""
    task_registry.reset_all()
    task_registry.mark_started_this_turn("sess-1", "sh-1")
    task_registry.mark_sdk_background_task("sess-1", "sh-1")
    # No mark_background_launch — the fallback must carry it.
    cwh._latch_terminal_notification(
        _terminal_event("sh-1", tool_use_id="tu-unrecorded"), "sess-1"
    )

    assert task_registry.was_unparked("sess-1", "sh-1") is True
    task_registry.reset_all()


# ---------------------------------------------------------------------------
# 8. Park-poll strand: unpark_pending must be latched the MOMENT a completion
#    is consumed — not at deliver time, after the consolidation window.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unpark_pending_latched_before_consolidation_window():
    """A park-polling client polls /live every 1.5s and the pump's consolidation
    window is ALSO 1.5s. ``complete_task`` (via ``_record_task_message``) drops
    the task from ``get_active`` the instant the terminal notification arrives,
    but the latch used to be set only inside ``_deliver_unpark_impl`` — after
    the full window plus its DB writes. Every successful unpark therefore
    exposed a >=1.5s gap where /live reported (is_streaming=false,
    activeTasks=[], unpark_pending=false) — indistinguishable from idle — so
    the client's poller STOPPED and the follow-up streamed into a dead poller
    (UI stranded showing parked + a stale running task). The latch must be up
    as soon as the completion is consumed (``mark_unparked``), BEFORE the
    consolidation window opens."""
    cwh._UNPARK_BATCH_WINDOW_SECONDS = 0.5  # widen so mid-window sampling is deterministic

    task_registry.start_task("sess-1", "task-1", "shell", "sleep 90")
    task_registry.mark_background_launch("sess-1", "tu-1")

    client = _FakeClient([_notif("task-1", uuid="u-1", summary="done")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    deliver_seen = {}

    async def _capture(**kwargs):
        deliver_seen["latched_at_deliver"] = task_registry.is_unpark_pending("sess-1")
        return True

    handler._deliver_unpark = AsyncMock(side_effect=_capture)

    with _patch_pool(state):
        pump = asyncio.create_task(
            handler._drain_notifications("sess-1", "claude-code", None, None)
        )
        # Sample DURING the consolidation window: the latch must come up while
        # the pump is still folding (well before deliver).
        latched_in_window = False
        for _ in range(80):  # up to 0.4s < the 0.5s window
            await asyncio.sleep(0.005)
            if pump.done():
                break
            if task_registry.is_unpark_pending("sess-1"):
                latched_in_window = True
                break
        assert latched_in_window, (
            "unpark_pending was NOT latched during the consolidation window — "
            "a park poll landing here stops the client's poller and strands the UI"
        )
        await pump

    handler._deliver_unpark.assert_awaited_once()
    assert deliver_seen.get("latched_at_deliver") is True


@pytest.mark.asyncio
async def test_unpark_pending_latched_for_killed_task_update_path():
    """The TaskUpdatedMessage-alone terminal path (killed/stopped) folds into the
    same consolidation window, so it needs the same early latch."""
    cwh._UNPARK_BATCH_WINDOW_SECONDS = 0.5

    task_registry.start_task("sess-1", "task-k", "agent", "bg work")

    client = _FakeClient([_updated("task-k", status="killed", uuid="uu-9")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        pump = asyncio.create_task(
            handler._drain_notifications("sess-1", "claude-code", None, None)
        )
        latched_in_window = False
        for _ in range(80):
            await asyncio.sleep(0.005)
            if pump.done():
                break
            if task_registry.is_unpark_pending("sess-1"):
                latched_in_window = True
                break
        assert latched_in_window
        await pump

    handler._deliver_unpark.assert_awaited_once()


@pytest.mark.asyncio
async def test_unpark_pending_cleared_when_stop_guard_abandons_delivery():
    """If the stop/interrupt guard abandons delivery after the latch went up,
    the latch must be dropped — otherwise a polling client park-polls forever
    for a follow-up that will never stream."""
    cwh._UNPARK_BATCH_WINDOW_SECONDS = 0.5

    task_registry.start_task("sess-1", "task-1", "shell", "sleep 90")
    task_registry.mark_background_launch("sess-1", "tu-1")

    client = _FakeClient([_notif("task-1", uuid="u-1")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        pump = asyncio.create_task(
            handler._drain_notifications("sess-1", "claude-code", None, None)
        )
        # Wait for the latch, then stop the session mid-window.
        for _ in range(80):
            await asyncio.sleep(0.005)
            if pump.done() or task_registry.is_unpark_pending("sess-1"):
                break
        cwh._stopped_sessions.add("sess-1")
        try:
            await pump
        finally:
            cwh._stopped_sessions.discard("sess-1")

    handler._deliver_unpark.assert_not_awaited()
    assert task_registry.is_unpark_pending("sess-1") is False


# ---------------------------------------------------------------------------
# 11. RC1: ACK-armed AGENT completion unparks via the pump (test A, pump level).
#     The CLI launches Agent tasks async WITHOUT run_in_background in the tool
#     input; the launch ACK is what arms mark_background_launch (in-stream), so a
#     genuine agent completion caught between turns must UNPARK — not be suppressed
#     as foreground. The complementary in-stream ACK-arming is covered in
#     tests/core/test_subagent_inner_output.py::TestLaunchAckArmsBackgroundGate.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_unparks_ack_armed_agent_completion():
    """Test A (pump): an agent task whose launch was armed from the ACK
    (mark_background_launch recorded for its launch tool_use_id) DOES unpark on its
    terminal notification — was_background_launch is the gate that used to suppress
    every genuine agent completion when the input flag was absent."""
    task_registry.start_task("sess-1", "task-1", "agent", "bg agent")
    # In-stream, the ACK sniffer armed the launch gate for the launch tool_use_id.
    task_registry.mark_background_launch("sess-1", "tu-1")

    client = _FakeClient([_notif("task-1", uuid="u-1", tool_use_id="tu-1")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_awaited_once()
    assert handler._deliver_unpark.await_args.kwargs["pending"][0]["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_pump_suppresses_agent_completion_without_ack_arm():
    """Test B (pump): an agent whose launch was NEVER armed (a SYNCHRONOUS
    sub-agent whose real result returned inline, so the ACK sniffer never matched)
    must NOT unpark from the pump — pins the #470/e83551e6 foreground semantics."""
    task_registry.start_task("sess-1", "task-1", "agent", "sync agent")
    # NOTE: no mark_background_launch — a synchronous sub-agent.
    assert task_registry.was_background_launch("sess-1", "tu-1") is False

    # hang=False: the suppression `continue`s, so the stream must end for the pump
    # to terminate.
    client = _FakeClient([_notif("task-1", uuid="u-1", tool_use_id="tu-1")], hang=False)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    # Indicator still cleared, but NO unpark for the synchronous sub-agent.
    assert task_registry.get_active("sess-1") == []
    handler._deliver_unpark.assert_not_awaited()


# ---------------------------------------------------------------------------
# 12. RC2: parked-state "Task Updated" chip delivery (test C).
#     A non-terminal TaskUpdatedMessage (e.g. a running→ still-running status patch)
#     drained BETWEEN turns must be converted into the SAME {"type": "system",
#     "subtype": "task_updated", ...} event the in-stream loop yields for it and
#     dispatched through handle_system → add_system_message so a system message
#     lands in the live store and its update_id increments — otherwise the
#     park-poller (keyed on update_id) has nothing to fetch and the chip never
#     renders while parked.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pump_delivers_task_updated_chip_to_live_store():
    from app.core.live_session_store import live_session_store, LiveSession

    session_id = "sess-chip"
    # A genuine background agent is still running (keeps the pump on its normal
    # unpark duty); the TaskUpdatedMessage is a non-terminal progress patch on it.
    task_registry.start_task(session_id, "task-1", "agent", "bg agent")
    task_registry.mark_background_launch(session_id, "tu-1")

    # Seed a real live-store session so add_system_message has somewhere to land.
    session = LiveSession(session_id=session_id)
    session.last_update_id = 5
    live_session_store._sessions[session_id] = session
    baseline_update_id = session.last_update_id
    baseline_msg_count = len(session.messages)

    # A non-terminal (running) TaskUpdatedMessage, then a terminal notification so
    # the pump terminates cleanly (hang=True would block forever otherwise).
    upd = _updated("task-1", status="running", uuid="uu-run")
    notif = _notif("task-1", uuid="u-1", tool_use_id="tu-1", summary="done")
    client = _FakeClient([upd, notif], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    # Keep live_session_store REAL (assert its state), but stub the broadcaster in
    # ws_event_handlers so handle_system doesn't hit real sync fan-out.
    import app.api.ws_event_handlers as weh
    try:
        with _patch_pool(state), patch.object(weh, "sync_engine", new=MagicMock(
            broadcast_stream_start=AsyncMock(),
            broadcast_stream_chunk=AsyncMock(),
        )):
            await handler._drain_notifications(session_id, "claude-code", None, None)

        # (a) a system message with subtype "task_updated" landed in the live store.
        sys_msgs = [m for m in session.messages if m.type == "system"]
        assert any(m.system_subtype == "task_updated" for m in sys_msgs), (
            f"no task_updated system message landed: "
            f"{[getattr(m, 'system_subtype', None) for m in session.messages]}"
        )
        assert len(session.messages) > baseline_msg_count
        # (b) update_id incremented so the park-poll (keyed on it) will fetch it.
        assert session.last_update_id > baseline_update_id
    finally:
        live_session_store._sessions.pop(session_id, None)


@pytest.mark.asyncio
async def test_pump_task_updated_chip_survives_missing_live_session():
    """RC2 robustness: if the live store has no session (cleanup raced), the chip
    dispatch must NOT crash the pump — add_system_message logs + drops the message,
    and the pump still completes its normal unpark duty."""
    session_id = "sess-chip-missing"
    task_registry.start_task(session_id, "task-1", "agent", "bg agent")
    task_registry.mark_background_launch(session_id, "tu-1")
    # NOTE: no live-store session seeded.

    upd = _updated("task-1", status="running", uuid="uu-run")
    notif = _notif("task-1", uuid="u-1", tool_use_id="tu-1", summary="done")
    client = _FakeClient([upd, notif], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    import app.api.ws_event_handlers as weh
    with _patch_pool(state), patch.object(weh, "sync_engine", new=MagicMock(
        broadcast_stream_start=AsyncMock(),
        broadcast_stream_chunk=AsyncMock(),
    )):
        # Must not raise — the pump still delivers the terminal unpark.
        await handler._drain_notifications(session_id, "claude-code", None, None)

    handler._deliver_unpark.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. Straddle regression (in-process latch). The CLI injected the sub-agent's
#    <task-notification> as a mid-turn user-lane steering message, which the
#    query_engine branch latched via mark_unparked at the SOURCE
#    (_latch_cli_task_notifications). When the typed TaskNotificationMessage then
#    reaches the between-turns pump AFTER the parent ResultMessage, the pump's
#    EXISTING per-task dedup must suppress it — no store scans, no table reads.
# ---------------------------------------------------------------------------

import json as _json
from app.core.query_engine import _latch_cli_task_notifications


def _cli_userlane_text(task_id="task-1", tool_use_id="tu-1"):
    """The unindented CLI-format <task-notification> the SDK injects in-stream."""
    return (
        "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        "<status>completed</status>\n"
        "<summary>done</summary>\n"
        "</task-notification>"
    )


@pytest.mark.asyncio
async def test_pump_suppresses_after_in_stream_latch():
    """Straddle: the query_engine branch spent mark_unparked in-stream, so when
    the typed notification later reaches ONLY the pump it must NOT re-deliver."""
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    task_registry.mark_background_launch("sess-1", "tu-1")

    # In-stream: the CLI's user-lane steering message flowed through the
    # query_engine UserMessage branch and latched the completion at the source.
    _latch_cli_task_notifications("sess-1", _cli_userlane_text("task-1", "tu-1"))
    assert task_registry.was_unparked("sess-1", "task-1") is True

    # hang=False: the pump dedups (continue, no break), so the stream must END for
    # the pump to terminate (mirrors test_pump_skips_already_notified_completion).
    client = _FakeClient([_notif("task-1", uuid="u-1", tool_use_id="tu-1")], hang=False)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    # No duplicate unpark delivered — the pre-spent token dedups the straddle.
    handler._deliver_unpark.assert_not_awaited()
    assert "sess-1" not in ws_cleanup._drain_pumps


@pytest.mark.asyncio
async def test_pump_delivers_when_no_in_stream_latch():
    """Negative control: NO in-stream latch (at-rest completion) — the pump must
    still deliver exactly once."""
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    task_registry.mark_background_launch("sess-1", "tu-1")

    client = _FakeClient([_notif("task-1", uuid="u-1", tool_use_id="tu-1")], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        await handler._drain_notifications("sess-1", "claude-code", None, None)

    handler._deliver_unpark.assert_awaited_once()
    pending = handler._deliver_unpark.await_args.kwargs["pending"]
    assert len(pending) == 1 and pending[0]["task_id"] == "task-1"


# ---------------------------------------------------------------------------
# 7. Result-content fix — <result> carries the sub-agent's FINAL assistant
#    message (all text blocks of the last assistant entry, joined), not the raw
#    JSONL dump; falls back to the raw output file (restoring backgrounded-shell
#    stdout) then the short summary.
# ---------------------------------------------------------------------------

def test_build_prompt_result_is_final_assistant_text(tmp_path):
    transcript = tmp_path / "agent-final.jsonl"
    lines = [
        {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "STEP 1 working on it"}]},
         "uuid": "a1", "timestamp": "t1"},
        {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "FINAL ANSWER HERE"}]},
         "uuid": "a2", "timestamp": "t2"},
    ]
    transcript.write_text("\n".join(_json.dumps(x) for x in lines), encoding="utf-8")

    pending = [{
        "task_id": "t", "tool_use_id": "tu", "status": "completed",
        "output_file": str(transcript), "summary": "SHORT SUMMARY", "usage": {},
    }]
    prompt = cwh._build_task_notification_prompt(pending)

    # <result> is the FINAL assistant message, not an earlier step nor raw JSONL.
    assert "<result>FINAL ANSWER HERE</result>" in prompt
    assert "STEP 1 working on it" not in prompt
    # Raw transcript JSON must NOT be dumped into the prompt.
    assert '"role": "assistant"' not in prompt
    assert '{"type": "assistant"' not in prompt


def test_build_prompt_joins_multiple_text_blocks_of_last_entry(tmp_path):
    """The last assistant entry with TWO text blocks → both joined in <result>."""
    transcript = tmp_path / "agent-multi.jsonl"
    lines = [
        {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "EARLIER STEP"}]},
         "uuid": "a1", "timestamp": "t1"},
        {"type": "assistant", "message": {"role": "assistant",
         "content": [
             {"type": "text", "text": "PART ONE"},
             {"type": "text", "text": "PART TWO"},
         ]},
         "uuid": "a2", "timestamp": "t2"},
    ]
    transcript.write_text("\n".join(_json.dumps(x) for x in lines), encoding="utf-8")

    pending = [{
        "task_id": "t", "tool_use_id": "tu", "status": "completed",
        "output_file": str(transcript), "summary": "SUMMARY", "usage": {},
    }]
    prompt = cwh._build_task_notification_prompt(pending)

    assert "PART ONE" in prompt and "PART TWO" in prompt
    assert "<result>PART ONE\nPART TWO</result>" in prompt
    # Only the LAST entry's text — the earlier step is excluded.
    assert "EARLIER STEP" not in prompt


def test_build_prompt_restores_shell_stdout_via_raw_read(tmp_path):
    """A backgrounded SHELL's output_file is plain text that parses to NO
    assistant entries — the raw-read fallback must restore its stdout into
    <result> (RED against the extract-only version, which lost it to summary)."""
    out = tmp_path / "shell-out.txt"
    out.write_text("SHELL STDOUT LINE 1\nSHELL STDOUT LINE 2\n", encoding="utf-8")

    pending = [{
        "task_id": "t", "tool_use_id": "tu", "status": "completed",
        "output_file": str(out), "summary": "SHORT SUMMARY", "usage": {},
    }]
    prompt = cwh._build_task_notification_prompt(pending)

    assert "SHELL STDOUT LINE 1" in prompt
    assert "SHELL STDOUT LINE 2" in prompt
    # The raw stdout wins over the short summary.
    assert "<result>SHORT SUMMARY</result>" not in prompt


def test_build_prompt_falls_back_to_summary_when_unreadable():
    pending = [{
        "task_id": "t", "tool_use_id": "tu", "status": "completed",
        "output_file": "/nonexistent/path/does-not-exist.jsonl",
        "summary": "SHORT SUMMARY", "usage": {},
    }]
    prompt = cwh._build_task_notification_prompt(pending)
    assert "<result>SHORT SUMMARY</result>" in prompt

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
    """Each test gets a clean registry + pump dict."""
    task_registry.reset_all()
    ws_cleanup._drain_pumps.clear()
    yield
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
    # Pre-arm: this (task, signature) was already delivered (e.g. caught in-stream).
    out_file = ""
    signature = "u-1"  # uuid takes precedence in the signature
    assert task_registry.mark_notified("sess-1", "task-1", signature) is True

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
    stack.enter_context(patch.object(ws_mod, "cancel_pending_cleanup", new=MagicMock()))
    stack.enter_context(patch.object(ws_mod, "schedule_delayed_cleanup", new=MagicMock()))
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


@pytest.mark.asyncio
async def test_stopped_session_does_not_deliver_unpark():
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
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

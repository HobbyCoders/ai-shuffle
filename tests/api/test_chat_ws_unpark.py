"""
Tests for the background-agent "unpark" follow-up turn in
app/api/chat_ws_handler.py.

Drives the REAL ChatWebSocketHandler.run_query while faking the SDK stream
(stream_to_websocket) so we can assert:

- A terminal `task_notification` arriving IN-STREAM (before `done`) spawns NO
  app follow-up turn — the CLI already delivered that completion to the model
  mid-turn (2026-07-02 incident: the app's post-turn re-delivery re-told the
  model about work it had already reported). The completion is only LATCHED
  (mark_unparked) so the between-turns drain pump can't re-deliver it either.
- Duplicates and already-latched tasks likewise spawn nothing.

All heavy collaborators (sync_engine, database, the turn recorder/finalizer,
the session-pool/SDK, websocket I/O, cleanup) are mocked; no Postgres or SDK
client is required.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
from app.core.task_registry import task_registry


def _make_handler():
    ws = MagicMock()
    handler = cwh.ChatWebSocketHandler(
        websocket=ws,
        device_id="dev-1",
        user_id="admin",
        api_user_id=None,
    )
    handler.send_json = AsyncMock(return_value=True)
    return handler


def _fake_stream(events):
    """Return an async-generator factory yielding the given events in order.

    Emulates the real ``stream_to_websocket`` registry side-effect: when a
    ``task_started`` event is yielded, the real generator has just called
    ``_record_task_message`` → ``mark_started_this_turn`` for it. We mirror that
    here so the in-stream unpark stash's started-this-turn scope guard sees the
    task as genuinely spawned in THIS turn (run_query resets the set at turn
    start, so marking it before calling run_query would be wiped).
    """
    async def _gen(*args, **kwargs):
        sid = kwargs.get("session_id")
        for ev in events:
            if ev.get("type") == "task_started" and sid:
                task_registry.mark_started_this_turn(sid, ev.get("task_id"))
            yield ev
    return _gen


class _RecordingCreateTask:
    """Drop-in for asyncio.create_task that records coroutines and runs none.

    We must close the captured coroutines so the event loop doesn't warn about
    'coroutine was never awaited'.
    """

    def __init__(self):
        self.coros = []

    def __call__(self, coro):
        self.coros.append(coro)
        task = MagicMock()
        task.done.return_value = True
        return task

    def close_all(self):
        for c in self.coros:
            try:
                c.close()
            except Exception:
                pass


def _run_query_patches(handler, fake_stream, created):
    """Context managers mocking every boundary run_query touches.

    Returns a list of patch context managers to enter via ExitStack.
    """
    from contextlib import ExitStack

    stack = ExitStack()

    # The session pool / SDK stream.
    stack.enter_context(
        patch("app.core.query_engine.stream_to_websocket", new=fake_stream)
    )
    # asyncio.create_task as used inside chat_ws_handler (heartbeat + follow-up).
    stack.enter_context(patch.object(cwh.asyncio, "create_task", new=created))

    # --- V2 turn recording/finalize seams --------------------------------------
    # (V2: the live session store is deleted — turn events are recorded durably
    # via turn_recorder.record_drained_event and turn-end state is persisted via
    # turn_finalize.persist_turn_end_state.)
    stack.enter_context(
        patch("app.core.turn_recorder.record_drained_event", new=AsyncMock())
    )
    stack.enter_context(patch.object(cwh, "persist_turn_end_state", new=AsyncMock()))

    # sync_engine (async) methods.
    sync = stack.enter_context(patch.object(cwh, "sync_engine"))
    sync.unregister_device = AsyncMock()
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_queue_updated = AsyncMock()

    # connector + webhook dispatch fired on completion (only reached on the
    # non-handoff path, but guard them so a stray call can't crash the test).
    conn = stack.enter_context(patch.object(cwh, "connector_manager"))
    conn.dispatch_event = AsyncMock()
    stack.enter_context(patch.object(cwh, "dispatch_session_complete", new=AsyncMock()))

    # database / run_sync.
    db = stack.enter_context(patch.object(cwh, "database"))
    db.get_session = MagicMock(return_value={"sdk_session_id": "sdk-1"})

    async def _fake_run_sync(fn, *a, **k):
        # run_query routes resolve_for_streaming_init through run_sync (thread
        # offload) — call through so the seed gets its list.
        if fn is mr.resolve_for_streaming_init:
            return fn(*a, **k)
        # add_session_message / pop_next_queued_message etc. — return falsy so the
        # queue path is a no-op.
        return None

    stack.enter_context(patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))

    # message_resolver + working dir.
    mr = stack.enter_context(patch.object(cwh, "message_resolver"))
    mr.resolve_for_streaming_init = MagicMock(return_value=[])
    stack.enter_context(patch.object(cwh, "resolve_working_dir", return_value="/tmp"))

    # (V2: the dispatch layer is deleted — the stash logic under test lives in
    # run_query's inline stream loop, so no dispatch_event stand-in is needed.)

    # websocket helpers (lazy-imported inside run_query).
    import app.api.websocket as ws_mod
    stack.enter_context(
        patch.object(ws_mod, "_active_chat_sessions", new={})
    )

    return stack


def _count_followup_run_query_coros(created, handler):
    """Count captured coroutines that are handler.run_query(...) calls."""
    n = 0
    captured_prompts = []
    for coro in created.coros:
        qn = getattr(coro, "__qualname__", "")
        # Match the run_query METHOD only — not the nested send_heartbeats local
        # (whose qualname is "...run_query.<locals>.send_heartbeats").
        if qn.endswith(".run_query") or qn == "run_query":
            n += 1
            # cr_frame.f_locals holds the bound args of the not-yet-started coro.
            frame = getattr(coro, "cr_frame", None)
            if frame is not None:
                captured_prompts.append(frame.f_locals.get("prompt"))
    return n, captured_prompts


@pytest.mark.asyncio
async def test_terminal_notification_in_stream_spawns_no_followup(tmp_path):
    """THE 2026-07-02 regression: a genuine background task (started this turn,
    backgrounded launch) whose terminal notification arrives IN-STREAM must spawn
    NO app follow-up turn — the CLI already injected the completion into the
    model's context before the turn ended. The app only latches it so the drain
    pump can't re-deliver it between turns."""
    task_registry.reset_all()
    # The parent launched this sub-agent in the background (its launch tool_use_id
    # is the notification's tool_use_id "tu-1"); _background_launches is not reset
    # per turn, so pre-setting it survives run_query's start-of-turn reset. Even
    # this strongest case (all old gates pass) must fire nothing post-turn.
    task_registry.mark_background_launch("sess-1", "tu-1")
    out = tmp_path / "out.txt"
    out.write_text("FULL SUBAGENT RESULT TEXT", encoding="utf-8")

    events = [
        {"type": "task_started", "task_id": "task-1", "description": "bg work"},
        {
            "type": "task_notification",
            "task_id": "task-1",
            "status": "completed",
            "summary": "did the thing",
            "tool_use_id": "tu-1",
            "output_file": str(out),
            "uuid": "uuid-1",
            "usage": {"total_tokens": 42, "tool_uses": 3, "duration_ms": 1000},
        },
        {"type": "done", "session_id": "sess-1", "metadata": {}},
    ]

    handler = _make_handler()
    created = _RecordingCreateTask()

    with _run_query_patches(handler, _fake_stream(events), created):
        await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    try:
        n, prompts = _count_followup_run_query_coros(created, handler)
        assert n == 0, f"in-stream completion must spawn no follow-up, got {n}: {prompts}"
        # ...but the completion is latched, spending the pump's unpark token.
        assert task_registry.was_unparked("sess-1", "task-1") is True
    finally:
        created.close_all()
        task_registry.reset_all()


@pytest.mark.asyncio
async def test_duplicate_notification_same_uuid_spawns_no_followup(tmp_path):
    """Duplicate in-stream notifications for the same task change nothing —
    still zero follow-ups, still latched exactly once."""
    task_registry.reset_all()
    task_registry.mark_background_launch("sess-1", "tu-1")
    out = tmp_path / "out.txt"
    out.write_text("RESULT", encoding="utf-8")

    notif = {
        "type": "task_notification",
        "task_id": "task-1",
        "status": "completed",
        "summary": "s",
        "tool_use_id": "tu-1",
        "output_file": str(out),
        "uuid": "uuid-1",
        "usage": None,
    }
    # Started this turn, then the same uuid twice in the same stream, then done.
    events = [
        {"type": "task_started", "task_id": "task-1", "description": "bg work"},
        notif,
        dict(notif),
        {"type": "done", "session_id": "sess-1", "metadata": {}},
    ]

    handler = _make_handler()
    created = _RecordingCreateTask()

    with _run_query_patches(handler, _fake_stream(events), created):
        await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    try:
        n, _prompts = _count_followup_run_query_coros(created, handler)
        assert n == 0, f"duplicate in-stream notifications must spawn nothing, got {n}"
        assert task_registry.was_unparked("sess-1", "task-1") is True
    finally:
        created.close_all()
        task_registry.reset_all()


@pytest.mark.asyncio
async def test_already_notified_uuid_spawns_none(tmp_path):
    """If the task already drove an unpark before the stream, no follow-up."""
    task_registry.reset_all()
    # Genuine background launch so the dedup (mark_unparked) is the thing under test.
    task_registry.mark_background_launch("sess-1", "tu-1")
    out = tmp_path / "out.txt"
    out.write_text("RESULT", encoding="utf-8")
    # Pre-arm: this task already drove an unpark (one-per-task latch, keyed on
    # task_id alone — a fresh per-rest uuid can no longer re-arm it, Bug 1 #2).
    assert task_registry.mark_unparked("sess-1", "task-1") is True

    events = [
        {
            "type": "task_notification",
            "task_id": "task-1",
            "status": "completed",
            "summary": "s",
            "tool_use_id": "tu-1",
            "output_file": str(out),
            "uuid": "uuid-1",
            "usage": None,
        },
        {"type": "done", "session_id": "sess-1", "metadata": {}},
    ]

    handler = _make_handler()
    created = _RecordingCreateTask()

    with _run_query_patches(handler, _fake_stream(events), created):
        await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    try:
        n, _ = _count_followup_run_query_coros(created, handler)
        assert n == 0, f"a pre-notified uuid must spawn no follow-up, got {n}"
    finally:
        created.close_all()
        task_registry.reset_all()

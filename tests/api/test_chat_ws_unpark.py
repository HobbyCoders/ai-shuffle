"""
Tests for the background-agent "unpark" follow-up turn in
app/api/chat_ws_handler.py.

Drives the REAL ChatWebSocketHandler.run_query while faking the SDK stream
(stream_to_websocket) so we can assert:

- When the stream yields a terminal `task_notification` then `done`, EXACTLY ONE
  follow-up run_query is spawned, with a synthesized prompt that contains the
  literal `<task-notification>` substring AND the `<result>` text read from the
  task's output_file.
- A DUPLICATE notification (same task) spawns NO follow-up (mark_unparked dedup,
  one unpark per task_id for its whole life).

All heavy collaborators (live_session_store, sync_engine, database, the
session-pool/SDK, websocket I/O, cleanup) are mocked; no Postgres or SDK client
is required.
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

    # live_session_store (async) methods.
    store = stack.enter_context(patch.object(cwh, "live_session_store"))
    store.start_streaming = AsyncMock()
    store.add_user_message = AsyncMock()
    store.end_streaming = AsyncMock()
    store.cleanup_session = AsyncMock()
    store.get_session_dict = MagicMock(return_value=None)
    # Finally-block guards: report no live/streaming session so cleanup is skipped.
    store.has_session = MagicMock(return_value=False)
    store.is_streaming = MagicMock(return_value=False)

    # sync_engine (async) methods.
    sync = stack.enter_context(patch.object(cwh, "sync_engine"))
    sync.unregister_device = AsyncMock()
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_queue_updated = AsyncMock()
    sync.broadcast_stream_end = AsyncMock()
    # Finally-block guard: report not-streaming so no stream_end broadcast fires.
    sync.is_session_streaming = MagicMock(return_value=False)

    # connector + webhook dispatch fired on completion (only reached on the
    # non-handoff path, but guard them so a stray call can't crash the test).
    conn = stack.enter_context(patch.object(cwh, "connector_manager"))
    conn.dispatch_event = AsyncMock()
    stack.enter_context(patch.object(cwh, "dispatch_session_complete", new=AsyncMock()))

    # database / run_sync.
    db = stack.enter_context(patch.object(cwh, "database"))
    db.get_session = MagicMock(return_value={"sdk_session_id": "sdk-1"})

    async def _fake_run_sync(fn, *a, **k):
        # add_session_message / pop_next_queued_message etc. — return falsy so the
        # queue path is a no-op.
        return None

    stack.enter_context(patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))

    # message_resolver + working dir.
    mr = stack.enter_context(patch.object(cwh, "message_resolver"))
    mr.resolve_for_streaming_init = MagicMock(return_value=[])
    stack.enter_context(patch.object(cwh, "resolve_working_dir", return_value="/tmp"))

    # dispatch_event (origin module) — but we want the REAL stash logic, which
    # lives in run_query itself, so dispatch_event can be a no-op AsyncMock.
    stack.enter_context(
        patch("app.api.ws_event_handlers.dispatch_event", new=AsyncMock())
    )

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
async def test_terminal_notification_spawns_one_followup_with_result(tmp_path):
    task_registry.reset_all()
    # The parent launched this sub-agent in the background (its launch tool_use_id
    # is the notification's tool_use_id "tu-1"); _background_launches is not reset
    # per turn, so pre-setting it survives run_query's start-of-turn reset. Without
    # it, the in-stream stash correctly suppresses the notification as a
    # synchronous sub-agent whose result already returned inline.
    task_registry.mark_background_launch("sess-1", "tu-1")
    out = tmp_path / "out.txt"
    out.write_text("FULL SUBAGENT RESULT TEXT", encoding="utf-8")

    # Genuine FAST path: the agent's TaskStartedMessage arrives THIS turn (the
    # _fake_stream marks it started-this-turn as it yields), then its terminal
    # notification arrives in-stream before `done`. Without the task_started
    # event, the in-stream stash now (correctly) suppresses the notification as a
    # prior-turn orphan. run_query resets the started-this-turn set at turn start,
    # so the start MUST flow through the stream, not be pre-set.
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
        # EXACTLY ONE follow-up run_query.
        assert n == 1, f"expected exactly one follow-up run_query, got {n}"
        synth = prompts[0]
        assert synth is not None
        assert "<task-notification>" in synth
        assert "FULL SUBAGENT RESULT TEXT" in synth  # full result, read off disk
        assert "<result>" in synth and "</result>" in synth
        assert "task-1" in synth  # task-id
        assert "tu-1" in synth  # tool-use-id
    finally:
        created.close_all()
        task_registry.reset_all()


@pytest.mark.asyncio
async def test_duplicate_notification_same_uuid_spawns_no_followup(tmp_path):
    task_registry.reset_all()
    # Genuine background launch (tool_use_id "tu-1") so the foreground gate passes
    # and the dedup under test (same uuid twice) is what suppresses the second.
    task_registry.mark_background_launch("sess-1", "tu-1")
    out = tmp_path / "out.txt"
    out.write_text("RESULT", encoding="utf-8")

    # Genuine FAST path: agent started this turn (via the task_started event,
    # which _fake_stream marks started-this-turn). The dedup under test is
    # mark_unparked (same task twice), not the started-this-turn scope guard.
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
        n, prompts = _count_followup_run_query_coros(created, handler)
        # The duplicate (same uuid) is deduped -> still exactly ONE follow-up.
        assert n == 1, f"duplicate uuid should not add a second follow-up, got {n}"
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

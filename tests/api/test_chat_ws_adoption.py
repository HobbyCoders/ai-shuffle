"""
Tests for the new-session ADOPTION TOKEN (request_id) plumbing in
app/api/chat_ws_handler.py — the backend half of the session-adoption hijack fix.

The frontend attaches a client-generated ``request_id`` to a NEW-session ``query``
frame and adopts a minted session ONLY when the echoed token matches. These tests
pin the backend contract:

- ``run_query`` echoes ``request_id`` in the first ``start`` frame when one is
  passed (the interactive new-session path).
- ``run_query`` emits ``start`` with NO ``request_id`` when none is passed — this
  is the server-initiated path (queue drain, ``deferred_delivery`` unpark
  follow-up), so a foreign turn unparking on a shared device socket can never be
  adopted as the awaiting chat's brand-new session.
- ``handle_query`` echoes ``request_id`` in the ``session_created`` frame for a
  brand-new session, and omits it when the client sent none.

All heavy collaborators are mocked (mirrors test_chat_ws_unpark.py); no Postgres /
SDK is required.
"""

import asyncio
from contextlib import ExitStack
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
    async def _gen(*args, **kwargs):
        for ev in events:
            yield ev
    return _gen


class _RecordingCreateTask:
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


def _run_query_patches(created):
    stack = ExitStack()
    stack.enter_context(patch("app.core.query_engine.stream_to_websocket", new=_fake_stream([])))
    stack.enter_context(patch.object(cwh.asyncio, "create_task", new=created))

    # (V2: the live session store is deleted — turn events are recorded durably
    # via turn_recorder.record_drained_event and turn-end state is persisted via
    # turn_finalize.persist_turn_end_state.)
    stack.enter_context(
        patch("app.core.turn_recorder.record_drained_event", new=AsyncMock())
    )
    stack.enter_context(patch.object(cwh, "persist_turn_end_state", new=AsyncMock()))

    sync = stack.enter_context(patch.object(cwh, "sync_engine"))
    sync.unregister_device = AsyncMock()
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_queue_updated = AsyncMock()

    conn = stack.enter_context(patch.object(cwh, "connector_manager"))
    conn.dispatch_event = AsyncMock()
    stack.enter_context(patch.object(cwh, "dispatch_session_complete", new=AsyncMock()))

    db = stack.enter_context(patch.object(cwh, "database"))
    db.get_session = MagicMock(return_value={"sdk_session_id": "sdk-1"})

    # Call through like the real run_sync (thread-offload) does: run_query now
    # routes resolve_for_streaming_init through run_sync, so a None-returning
    # fake would feed len(None) into the seed path.
    async def _fake_run_sync(fn, *a, **k):
        return fn(*a, **k)

    stack.enter_context(patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))

    mr = stack.enter_context(patch.object(cwh, "message_resolver"))
    mr.resolve_for_streaming_init = MagicMock(return_value=[])
    stack.enter_context(patch.object(cwh, "resolve_working_dir", return_value="/tmp"))
    # (V2: the dispatch layer is deleted — run_query handles stream events inline.)

    import app.api.websocket as ws_mod
    stack.enter_context(patch.object(ws_mod, "_active_chat_sessions", new={}))
    return stack


def _start_frames(handler):
    """All send_json frames of type 'start'."""
    return [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == "start"
    ]


# ─── run_query: start-frame echo ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_query_echoes_request_id_in_start_frame():
    task_registry.reset_all()
    handler = _make_handler()
    created = _RecordingCreateTask()

    with _run_query_patches(created):
        await handler.run_query(
            "hello", "sess-1", "claude-code", None, None, None,
            request_id="req-abc-123",
        )

    try:
        starts = _start_frames(handler)
        assert len(starts) == 1, f"expected one start frame, got {len(starts)}"
        assert starts[0].get("request_id") == "req-abc-123"
        assert starts[0]["session_id"] == "sess-1"
    finally:
        created.close_all()
        task_registry.reset_all()


@pytest.mark.asyncio
async def test_run_query_omits_request_id_when_none():
    task_registry.reset_all()
    handler = _make_handler()
    created = _RecordingCreateTask()

    with _run_query_patches(created):
        # No request_id — the interactive existing-session path.
        await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    try:
        starts = _start_frames(handler)
        assert len(starts) == 1
        assert "request_id" not in starts[0], "start must carry no token when none was passed"
    finally:
        created.close_all()
        task_registry.reset_all()


@pytest.mark.asyncio
async def test_deferred_delivery_start_frame_carries_no_request_id():
    """A server-initiated (deferred) turn — queue drain / unpark follow-up — must
    emit a start with NO request_id so it can never be adopted as a foreign
    session's awaiting new chat."""
    task_registry.reset_all()
    handler = _make_handler()
    created = _RecordingCreateTask()

    with _run_query_patches(created):
        await handler.run_query(
            "server-initiated follow-up", "sess-1", "claude-code", None, None, None,
            deferred_delivery=True,
        )

    try:
        starts = _start_frames(handler)
        assert len(starts) == 1
        assert "request_id" not in starts[0]
    finally:
        created.close_all()
        task_registry.reset_all()


# ─── handle_query: session_created echo + run_query token threading ───────────


def _handle_query_patches(created):
    """Mocks for the handle_query DB/sync boundaries (new-session branch)."""
    stack = ExitStack()
    stack.enter_context(patch.object(cwh.asyncio, "create_task", new=created))

    sync = stack.enter_context(patch.object(cwh, "sync_engine"))
    sync.unregister_device = AsyncMock()
    sync.register_device = AsyncMock()
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_user_session_created = AsyncMock()
    sync.broadcast_session_updated = AsyncMock()

    stack.enter_context(patch.object(cwh, "database"))

    async def _fake_run_sync(fn, *a, **k):
        return None

    stack.enter_context(patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
    # transaction() context manager used by _create_session_atomic.
    stack.enter_context(patch("app.db.base.transaction", new=MagicMock()))

    import app.api.websocket as ws_mod
    stack.enter_context(patch.object(ws_mod, "_active_chat_sessions", new={}))
    return stack


def _session_created_frames(handler):
    return [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == "session_created"
    ]


def _run_query_coros(created):
    """Return (kwargs) for each captured handler.run_query coroutine."""
    out = []
    for coro in created.coros:
        qn = getattr(coro, "__qualname__", "")
        if qn.endswith(".run_query") or qn == "run_query":
            frame = getattr(coro, "cr_frame", None)
            if frame is not None:
                out.append(frame.f_locals)
    return out


@pytest.mark.asyncio
async def test_handle_query_echoes_request_id_in_session_created_and_start():
    task_registry.reset_all()
    handler = _make_handler()
    created = _RecordingCreateTask()

    with _handle_query_patches(created):
        await handler.handle_query({
            "prompt": "brand new chat",
            "session_id": None,  # new session
            "profile": "claude-code",
            "project": None,
            "request_id": "req-xyz-789",
        })

    try:
        created_frames = _session_created_frames(handler)
        assert len(created_frames) == 1
        assert created_frames[0].get("request_id") == "req-xyz-789"

        # The token is threaded into the spawned run_query for the first start echo.
        rq = _run_query_coros(created)
        assert len(rq) == 1, f"expected one run_query spawn, got {len(rq)}"
        assert rq[0].get("request_id") == "req-xyz-789"
    finally:
        created.close_all()
        task_registry.reset_all()


@pytest.mark.asyncio
async def test_handle_query_new_session_without_request_id_omits_token():
    task_registry.reset_all()
    handler = _make_handler()
    created = _RecordingCreateTask()

    with _handle_query_patches(created):
        await handler.handle_query({
            "prompt": "brand new chat, no token",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
            # no request_id
        })

    try:
        created_frames = _session_created_frames(handler)
        assert len(created_frames) == 1
        assert "request_id" not in created_frames[0]

        rq = _run_query_coros(created)
        assert len(rq) == 1
        assert rq[0].get("request_id") is None
    finally:
        created.close_all()
        task_registry.reset_all()


@pytest.mark.asyncio
async def test_handle_query_existing_session_threads_no_request_id():
    """A query with a session_id already set is an EXISTING-session turn: even if a
    stray request_id is present, it must NOT be threaded into run_query (the token
    only authenticates NEW-session adoption)."""
    task_registry.reset_all()
    handler = _make_handler()
    handler.current_session_id = "sess-existing"
    created = _RecordingCreateTask()

    with _handle_query_patches(created):
        await handler.handle_query({
            "prompt": "follow-up on an open session",
            "session_id": "sess-existing",
            "profile": "claude-code",
            "project": None,
            "request_id": "req-should-be-ignored",
        })

    try:
        # No session_created for an existing session.
        assert _session_created_frames(handler) == []
        rq = _run_query_coros(created)
        assert len(rq) == 1
        assert rq[0].get("request_id") is None, "existing-session run_query must carry no token"
    finally:
        created.close_all()
        task_registry.reset_all()

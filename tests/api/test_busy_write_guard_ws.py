"""
Busy-write guard — WebSocket layer (prod-readiness Phase 2.7).

The kill vector: a plain ``query`` frame arriving MID-TURN used to make the
handlers bare-``cancel()`` the live turn's task (no SDK interrupt, no
``remove_broken``) and immediately re-``query()`` — writing the new prompt into
the mid-turn CLI's stdin. The CLI dies ("Fatal error in message reader"), the
whole in-flight turn is lost (messages persist only at turn end), subagents die,
and the UI is stranded parked. ("Send now" is NOT this path — it goes through
the two-phase ``interrupt_for_query``.)

These tests pin the fix on the unified ``ChatWebSocketHandler`` (since Phase
6.1 step 7 the ``/ws/chat`` route is a thin shim over it — the legacy inline
closure twin is gone):

1. ``handle_query`` busy → the message is ENQUEUED via the existing queue
   machinery (never dropped, user gets queue_updated acks) and the live turn is
   never cancelled; late TOCTOU re-check registers under ONE lock acquisition.
2. ``_process_next_queued`` busy → the popped message is re-queued at the
   front, never fired into the live turn.
3. ``_deliver_unpark_impl`` yields (no clobber) when a user query owns the
   session — both at entry and at the late re-check — and clears the
   unpark-pending latch on the late yield.
4. Route-level: the real /ws/chat entry point enqueues a busy query and spawns
   no run_query; a source-level guard pins the class implementation.

All heavy collaborators are mocked (mirrors test_chat_ws_adoption.py /
test_ws_chat_inline_adoption.py); no Postgres / SDK required.
"""

import asyncio
import inspect
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.websockets import WebSocketState

import app.api.chat_ws_handler as cwh
import app.api.websocket as ws_mod

# Import eagerly so query_engine's module-level ``task_registry`` binding is
# resolved BEFORE _unpark_patches swaps app.core.task_registry.task_registry
# for a MagicMock. Without this, _deliver_unpark_impl's lazy import of
# query_engine (via _build_task_notification_prompt) happens INSIDE the patch
# window and freezes query_engine.task_registry to the mock for the rest of
# the pytest process, silently no-op'ing complete_task() in every later test.
import app.core.query_engine  # noqa: F401


# ─── helpers ────────────────────────────────────────────────────────────────


class _FakeLiveTask:
    """Stands in for a live (not done) asyncio.Task in _active_chat_sessions.
    Records cancel() so tests can assert the live turn is NEVER cancelled."""

    def __init__(self, done: bool = False):
        self._done = done
        self.cancel_calls = 0

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancel_calls += 1


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


class _RecordingCreateTask:
    """Captures coroutines handed to asyncio.create_task WITHOUT running them.
    Returns a done-looking MagicMock so registry bookkeeping is happy."""

    def __init__(self):
        self.coros = []
        self.names = []

    def __call__(self, coro):
        self.coros.append(coro)
        self.names.append(getattr(coro, "__qualname__", ""))
        task = MagicMock()
        task.done.return_value = True
        return task

    def close_all(self):
        for c in self.coros:
            try:
                c.close()
            except Exception:
                pass

    @property
    def run_query_spawns(self):
        return [n for n in self.names if "run_query" in n]


def _handler_patches(stack: ExitStack, created: _RecordingCreateTask, registry: dict):
    """Patch every boundary handle_query / _process_next_queued touch."""
    stack.enter_context(patch.object(cwh.asyncio, "create_task", new=created))

    sync = stack.enter_context(patch.object(cwh, "sync_engine"))
    sync.register_device = AsyncMock()
    sync.unregister_device = AsyncMock()
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_queue_updated = AsyncMock()
    sync.broadcast_session_updated = AsyncMock()
    sync.broadcast_user_session_created = AsyncMock()

    db = stack.enter_context(patch.object(cwh, "database"))
    db.get_session = MagicMock(return_value={"sdk_session_id": "sdk-1", "title": "t", "worktree_id": None})
    db.get_queued_messages = MagicMock(return_value=[{"id": "q1", "content": "queued"}])

    async def _fake_run_sync(fn, *a, **k):
        return fn(*a, **k)

    stack.enter_context(patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))

    stack.enter_context(patch.object(ws_mod, "_active_chat_sessions", new=registry))
    stack.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))
    return sync, db


# ═══ 1. handle_query (class twin) ═══════════════════════════════════════════


async def test_handle_query_busy_enqueues_and_never_cancels_live_turn():
    """A query frame for a session with a live turn is routed through
    handle_queue_message (queue machinery + acks) — the live task is not
    cancelled and no run_query is spawned into the busy CLI."""
    handler = _make_handler()
    handler.handle_queue_message = AsyncMock()
    live = _FakeLiveTask()
    registry = {"sess-busy": live}
    created = _RecordingCreateTask()

    with ExitStack() as stack:
        _handler_patches(stack, created, registry)
        await handler.handle_query({
            "prompt": "second message mid-turn",
            "session_id": "sess-busy",
            "profile": "claude-code",
            "project": None,
        })
        created.close_all()

    handler.handle_queue_message.assert_awaited_once_with({
        "session_id": "sess-busy",
        "content": "second message mid-turn",
        "profile": "claude-code",
        "project": None,
    })
    assert live.cancel_calls == 0, "live turn must NEVER be cancelled by a plain query"
    assert registry["sess-busy"] is live, "live turn must not be clobbered in the registry"
    assert created.run_query_spawns == [], "no query may be written into the busy CLI"


async def test_handle_query_done_task_proceeds_normally():
    """A DONE task in the registry is not 'busy' — the query proceeds and
    registers a fresh turn (guards against over-suppression)."""
    handler = _make_handler()
    handler.handle_queue_message = AsyncMock()
    registry = {"sess-idle": _FakeLiveTask(done=True)}
    created = _RecordingCreateTask()

    with ExitStack() as stack:
        _handler_patches(stack, created, registry)
        await handler.handle_query({
            "prompt": "follow-up on idle session",
            "session_id": "sess-idle",
            "profile": "claude-code",
            "project": None,
        })
        created.close_all()

    handler.handle_queue_message.assert_not_awaited()
    assert len(created.run_query_spawns) == 1, "idle session must run the query"


async def test_handle_query_late_preempt_enqueues_under_lock():
    """TOCTOU: a server-initiated turn (queue drain / unpark) registers while
    handle_query is persisting the user message. The late check-and-register
    happens under ONE lock acquisition — the interloper is never clobbered or
    cancelled; the prompt is enqueued and acked instead."""
    handler = _make_handler()
    handler.handle_queue_message = AsyncMock()
    interloper = _FakeLiveTask()
    registry: dict = {}
    created = _RecordingCreateTask()

    with ExitStack() as stack:
        sync, db = _handler_patches(stack, created, registry)

        async def _inject_interloper(**kwargs):
            registry["sess-race"] = interloper

        sync.broadcast_message_added = AsyncMock(side_effect=_inject_interloper)

        await handler.handle_query({
            "prompt": "raced message",
            "session_id": "sess-race",
            "profile": "claude-code",
            "project": None,
        })
        created.close_all()

    assert interloper.cancel_calls == 0
    assert registry["sess-race"] is interloper, "interloper turn must keep the registry slot"
    assert created.run_query_spawns == [], "preempted query must not spawn run_query"
    db.add_queued_message.assert_called_once()
    assert db.add_queued_message.call_args.kwargs["content"] == "raced message"
    # User feedback: queue_updated broadcast + direct frame.
    sync.broadcast_queue_updated.assert_awaited_once()
    queue_frames = [
        c.args[0] for c in handler.send_json.call_args_list
        if c.args and c.args[0].get("event_type") == "queue_updated"
    ]
    assert queue_frames, "user must see a queue_updated ack for the enqueued message"


# ═══ 2. _process_next_queued (class twin) ═══════════════════════════════════


async def test_process_next_queued_busy_requeues_at_front():
    """A queued message popped while a turn is live goes BACK to the front of
    the queue — it is never fired into the busy CLI and the turn survives."""
    handler = _make_handler()
    live = _FakeLiveTask()
    registry = {"sess-busy": live}
    created = _RecordingCreateTask()

    with ExitStack() as stack:
        sync, db = _handler_patches(stack, created, registry)
        db.pop_next_queued_message = MagicMock(
            return_value={"content": "queued mid-turn", "profile_id": None, "project_id": None}
        )
        await handler._process_next_queued("sess-busy")
        created.close_all()

    assert live.cancel_calls == 0
    db.requeue_message_at_front.assert_called_once()
    assert db.requeue_message_at_front.call_args.kwargs["content"] == "queued mid-turn"
    db.add_session_message.assert_not_called(), "must not persist a message it won't run"
    assert created.run_query_spawns == []
    sync.broadcast_queue_updated.assert_awaited()


async def test_process_next_queued_late_preempt_requeues_at_front():
    """TOCTOU inside the drain: an explicit query registers during the drain's
    persist/broadcast awaits — the drain yields, re-queues at the front, and
    never cancels the interloper."""
    handler = _make_handler()
    interloper = _FakeLiveTask()
    registry: dict = {}
    created = _RecordingCreateTask()

    with ExitStack() as stack:
        sync, db = _handler_patches(stack, created, registry)
        db.pop_next_queued_message = MagicMock(
            return_value={"content": "drained then raced", "profile_id": None, "project_id": None}
        )

        async def _inject_interloper(**kwargs):
            registry["sess-race"] = interloper

        sync.broadcast_message_added = AsyncMock(side_effect=_inject_interloper)
        await handler._process_next_queued("sess-race")
        created.close_all()

    assert interloper.cancel_calls == 0
    assert registry["sess-race"] is interloper
    assert created.run_query_spawns == []
    db.requeue_message_at_front.assert_called_once()
    assert db.requeue_message_at_front.call_args.kwargs["content"] == "drained then raced"


# ═══ 3. _deliver_unpark_impl — unpark yields to a live user query ════════════


def _unpark_patches(stack: ExitStack, registry: dict):
    sync = stack.enter_context(patch.object(cwh, "sync_engine"))
    sync.broadcast_message_added = AsyncMock()
    store = stack.enter_context(patch.object(cwh, "live_session_store"))
    store.end_streaming = AsyncMock()
    db = stack.enter_context(patch.object(cwh, "database"))

    async def _fake_run_sync(fn, *a, **k):
        return fn(*a, **k)

    stack.enter_context(patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
    reg = stack.enter_context(patch("app.core.task_registry.task_registry"))
    stack.enter_context(patch.object(ws_mod, "_active_chat_sessions", new=registry))
    stack.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))
    return sync, store, db, reg


_PENDING = [{"task_id": "t1", "status": "completed", "description": "bg task", "output_tail": ""}]


async def test_deliver_unpark_yields_when_live_turn_owns_session():
    """Early guard: a live user turn already owns the session → unpark returns
    False without persisting a synthetic prompt or touching the registry."""
    live = _FakeLiveTask()
    registry = {"sess-1": live}
    run_query = MagicMock()
    set_query_task = MagicMock()

    with ExitStack() as stack:
        sync, store, db, reg = _unpark_patches(stack, registry)
        handed_off = await cwh._deliver_unpark_impl(
            pending=_PENDING,
            session_id="sess-1",
            profile_id="claude-code",
            project_id=None,
            worktree_id=None,
            ctx=MagicMock(),
            final_metadata=None,
            run_query=run_query,
            set_query_task=set_query_task,
        )

    assert handed_off is False
    assert live.cancel_calls == 0
    assert registry["sess-1"] is live, "unpark must not clobber the live turn"
    run_query.assert_not_called()
    set_query_task.assert_not_called()
    db.add_session_message.assert_not_called(), "no synthetic prompt may be persisted"
    reg.mark_unpark_pending.assert_not_called()


async def test_deliver_unpark_late_race_yields_and_clears_latch():
    """TOCTOU: a user query registers during the unpark's persist/broadcast
    awaits. The late re-check (under the registration lock) yields, clears the
    unpark_pending latch (so pollers don't park forever), and never cancels or
    clobbers the winning turn."""
    interloper = _FakeLiveTask()
    registry: dict = {}
    run_query = MagicMock()
    set_query_task = MagicMock()

    with ExitStack() as stack:
        sync, store, db, reg = _unpark_patches(stack, registry)

        async def _inject_interloper(**kwargs):
            registry["sess-1"] = interloper

        store.end_streaming = AsyncMock(side_effect=_inject_interloper)

        handed_off = await cwh._deliver_unpark_impl(
            pending=_PENDING,
            session_id="sess-1",
            profile_id="claude-code",
            project_id=None,
            worktree_id=None,
            ctx=MagicMock(),
            final_metadata=None,
            run_query=run_query,
            set_query_task=set_query_task,
        )

    assert handed_off is False
    assert interloper.cancel_calls == 0
    assert registry["sess-1"] is interloper, "the user query keeps the session"
    run_query.assert_not_called()
    set_query_task.assert_not_called()
    reg.clear_unpark_pending.assert_called_once_with("sess-1")


async def test_deliver_unpark_no_race_hands_off():
    """Sanity: with no competing turn the unpark still delivers (returns True,
    registers the follow-up) — the new guards must not break normal delivery."""
    registry: dict = {}
    created = _RecordingCreateTask()
    set_query_task = MagicMock()

    async def run_query(*a, **k):  # captured by the recorder, never run
        return None

    with ExitStack() as stack:
        sync, store, db, reg = _unpark_patches(stack, registry)
        stack.enter_context(patch.object(cwh.asyncio, "create_task", new=created))
        handed_off = await cwh._deliver_unpark_impl(
            pending=_PENDING,
            session_id="sess-1",
            profile_id="claude-code",
            project_id=None,
            worktree_id=None,
            ctx=MagicMock(),
            final_metadata=None,
            run_query=run_query,
            set_query_task=set_query_task,
        )
        created.close_all()

    assert handed_off is True
    assert "sess-1" in registry, "follow-up turn must be registered"
    set_query_task.assert_called_once()
    reg.mark_unpark_pending.assert_called_once_with("sess-1")
    reg.clear_unpark_pending.assert_not_called()


# ═══ 4. Closure twin — the LIVE /ws/chat route ═══════════════════════════════


class _FakeWebSocket:
    """Minimal Starlette-WebSocket stand-in (mirrors test_ws_chat_inline_adoption)."""

    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.client_state = WebSocketState.CONNECTED
        self.sent: list = []
        self.cookies = {}

    async def accept(self):
        return None

    async def receive_json(self):
        if self._inbound:
            return self._inbound.pop(0)
        self.client_state = WebSocketState.DISCONNECTED
        raise ws_mod.WebSocketDisconnect(code=1000)

    async def send_text(self, text: str):
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str = ""):
        self.client_state = WebSocketState.DISCONNECTED


class _RouteRecordingCreateTask:
    """Intercepts ONLY inline run_query spawns; schedules everything else for
    real so the route's ping-loop teardown works."""

    def __init__(self):
        self.coros = []
        self.spawn_count = 0

    def __call__(self, coro):
        qn = getattr(coro, "__qualname__", "")
        if not (qn.endswith(".run_query") or qn == "run_query"):
            return asyncio.ensure_future(coro)
        self.coros.append(coro)
        self.spawn_count += 1
        task = MagicMock()
        task.done.return_value = True
        return task

    def close_all(self):
        for c in self.coros:
            try:
                c.close()
            except Exception:
                pass


async def test_closure_query_busy_enqueues_and_never_cancels_live_turn():
    """The REAL inline /ws/chat route: a query for a busy session is enqueued
    (add_queued_message + queue_updated frame), the live task is not cancelled,
    the user message is NOT double-persisted, and no run_query is spawned."""

    async def _forever():
        await asyncio.Event().wait()

    live_task = asyncio.create_task(_forever())  # real Task, created pre-patch
    ws = _FakeWebSocket([{
        "type": "query",
        "prompt": "second message mid-turn",
        "session_id": "sess-busy",
        "profile": "claude-code",
        "project": None,
    }])
    created = _RouteRecordingCreateTask()

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                ws_mod, "authenticate_websocket", new=AsyncMock(return_value=(True, None))
            ))

            async def _noop_ping(*a, **k):
                return None

            stack.enter_context(patch.object(ws_mod, "ping_loop", new=_noop_ping))
            stack.enter_context(patch.object(ws_mod.asyncio, "create_task", new=created))

            # Post-flip: the route is a shim over ChatWebSocketHandler, which
            # resolves collaborators from cwh module globals — mirror the SAME
            # mock objects into both namespaces.
            sync = stack.enter_context(patch.object(ws_mod, "sync_engine"))
            stack.enter_context(patch.object(cwh, "sync_engine", new=sync))
            sync.register_user_device = AsyncMock()
            sync.unregister_user_device = AsyncMock()
            sync.register_device = AsyncMock()
            sync.unregister_device = AsyncMock()
            sync.broadcast_message_added = AsyncMock()
            sync.broadcast_queue_updated = AsyncMock()
            sync.clear_streaming_state = AsyncMock()
            sync.get_user_streaming_sessions = MagicMock(return_value=[])
            sync.get_device_count = MagicMock(return_value=1)

            store = stack.enter_context(patch.object(ws_mod, "live_session_store"))
            stack.enter_context(patch.object(cwh, "live_session_store", new=store))
            store.get_session = MagicMock(return_value=None)
            store.has_session = MagicMock(return_value=False)
            store.is_streaming = MagicMock(return_value=False)
            store.cleanup_session = AsyncMock()

            db = stack.enter_context(patch.object(ws_mod, "database"))
            stack.enter_context(patch.object(cwh, "database", new=db))
            db.get_session = MagicMock(return_value={"sdk_session_id": None, "title": "x"})
            db.get_queued_messages = MagicMock(return_value=[{"id": "q1", "content": "second message mid-turn"}])
            db.pop_next_queued_message = MagicMock(return_value=None)

            async def _fake_run_sync(fn, *a, **k):
                return fn(*a, **k)

            _run_sync = AsyncMock(side_effect=_fake_run_sync)
            stack.enter_context(patch.object(ws_mod, "run_sync", new=_run_sync))
            stack.enter_context(patch.object(cwh, "run_sync", new=_run_sync))
            stack.enter_context(patch.object(ws_mod, "cleanup_session_handlers", new=AsyncMock()))

            registry = {"sess-busy": live_task}
            stopped: set = set()
            interrupt_pending: set = set()
            stack.enter_context(patch.object(ws_mod, "_active_chat_sessions", new=registry))
            stack.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))
            stack.enter_context(patch.object(ws_mod, "_stopped_sessions", new=stopped))
            stack.enter_context(patch.object(cwh, "_stopped_sessions", new=stopped))
            stack.enter_context(patch.object(ws_mod, "_interrupt_pending_sessions", new=interrupt_pending))
            stack.enter_context(patch.object(cwh, "_interrupt_pending_sessions", new=interrupt_pending))
            stack.enter_context(patch.object(ws_mod, "_draining_sessions", new=set()))

            try:
                await ws_mod.chat_websocket(ws, token="tok", device_id="dev-1")
            finally:
                created.close_all()

            # The live turn survived untouched.
            assert not live_task.cancelled()
            assert registry["sess-busy"] is live_task
            # No run_query was spawned into the busy CLI.
            assert created.spawn_count == 0
            # The prompt was enqueued, not persisted as a session message.
            db.add_queued_message.assert_called_once()
            assert db.add_queued_message.call_args.kwargs["content"] == "second message mid-turn"
            db.add_session_message.assert_not_called()
            # User feedback: broadcast + direct queue_updated frame.
            sync.broadcast_queue_updated.assert_awaited()
            queue_frames = [
                f for f in ws.sent
                if isinstance(f, dict) and f.get("event_type") == "queue_updated"
            ]
            assert queue_frames, f"expected a queue_updated ack frame, got {ws.sent}"
            # The busy-guard's opportunistic drain saw the live turn and no-oped.
            db.pop_next_queued_message.assert_not_called()
    finally:
        live_task.cancel()
        try:
            await live_task
        except asyncio.CancelledError:
            pass


# ═══ 5. Source-level parity — keep the twins aligned ═════════════════════════


def test_busy_guard_source_parity():
    """The unified handler must carry the early busy guard AND the late
    lock-atomic check-and-register; handle_query must contain NO bare cancel.

    (Phase 6.1 step 7: the legacy inline ``chat_websocket`` closure twin was
    deleted, so this now guards the single class implementation only.)"""
    hq_src = inspect.getsource(cwh.ChatWebSocketHandler.handle_query)
    pq_src = inspect.getsource(cwh.ChatWebSocketHandler._process_next_queued)

    # Early guards.
    assert "[BusyGuard]" in hq_src
    # Late lock-atomic check-and-register (never clobber, never cancel).
    assert "preempted = False" in hq_src
    assert "preempted = False" in pq_src
    # The message is never dropped: busy paths enqueue / requeue.
    assert "add_queued_message" in hq_src
    assert "requeue_message_at_front" in pq_src
    # The old kill vector — bare-cancelling the live turn — must stay dead.
    assert ".cancel()" not in hq_src, "handle_query must never cancel a live turn"
    assert ".cancel()" not in pq_src, "_process_next_queued must never cancel a live turn"


def test_unpark_impl_yields_no_clobber_source_guard():
    """_deliver_unpark_impl must re-check the registry under the lock at the
    registration point and yield (clearing the latch) when a query won."""
    src = inspect.getsource(cwh._deliver_unpark_impl)
    assert "clear_unpark_pending" in src
    assert "no clobber" in src
    # create_task + registration happen inside the lock block.
    assert "_active_sessions_lock" in src

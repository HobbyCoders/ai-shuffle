"""
Regression tests: "Send now" (two-phase interrupt+query) must not strand or
crash the session when the CLI is busy (e.g. deep in extended thinking).

Two failure modes observed live:

1. ``interrupt_session`` awaited ``client.interrupt()`` with no timeout — a
   wedged CLI never answers the control request, the phase-1 handler hangs
   past the frontend's confirm window, and the user's message is lost.

2. When the streaming task did not drain within the budget the handler
   force-cancelled it, but the pooled client was KEPT (the stream generator
   swallows CancelledError and its ``finally`` keeps connected clients for
   reuse). The follow-up query then wrote a new user message into a CLI that
   was still mid-turn, killing it with "Fatal error in message reader:
   Command failed with exit code 1" and stopping the session dead.

Fix under test: a timeout around the SDK interrupt, plus
``SessionPool.remove_broken`` — evict the entry synchronously and tear the
subprocess down in the background — invoked by the interrupt handlers on
drain timeout so the follow-up query resumes on a fresh client.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.query_session_lifecycle as lifecycle
from app.core.session_pool import SessionPool, SessionState


def _fake_state(**overrides) -> SessionState:
    adapter = MagicMock()
    # state.client proxies adapter.sdk_client; subprocess_pid()'s handle walk
    # raises on the mock and safely returns None.
    adapter.sdk_client = MagicMock()
    adapter.sdk_client.interrupt = AsyncMock()
    adapter.disconnect = AsyncMock()
    state = SessionState(adapter=adapter, is_connected=True)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


# ─── interrupt_session: SDK interrupt must be bounded ─────────────────────


class _StubPool:
    def __init__(self, state):
        self._state = state

    def peek(self, session_id):
        return self._state


@pytest.mark.asyncio
async def test_interrupt_session_returns_false_when_sdk_interrupt_hangs(monkeypatch):
    """A CLI that never answers the interrupt control request must not hang
    the phase-1 handler forever — interrupt_session gives up and returns False."""
    state = _fake_state()

    async def _never_returns():
        await asyncio.Event().wait()

    state.adapter.sdk_client.interrupt = AsyncMock(side_effect=_never_returns)
    monkeypatch.setattr(lifecycle, "_SDK_INTERRUPT_TIMEOUT_SECONDS", 0.05)
    with patch.object(lifecycle, "get_session_pool", return_value=_StubPool(state)):
        result = await asyncio.wait_for(lifecycle.interrupt_session("s1"), timeout=2.0)
    assert result is False
    assert state.interrupt_requested is True  # failsafe flag still set


@pytest.mark.asyncio
async def test_interrupt_session_success_returns_true(monkeypatch):
    state = _fake_state()
    monkeypatch.setattr(lifecycle, "_SDK_INTERRUPT_TIMEOUT_SECONDS", 1.0)
    with patch.object(lifecycle, "get_session_pool", return_value=_StubPool(state)):
        result = await lifecycle.interrupt_session("s1")
    assert result is True


# ─── SessionPool.remove_broken ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_broken_pops_immediately_and_tears_down_in_background():
    pool = SessionPool(max_size=4)
    state = _fake_state()
    pool.put("s1", state)

    removed = await pool.remove_broken("s1")

    # The entry is gone synchronously — a follow-up query must get a fresh client.
    assert removed is state
    assert pool.peek("s1") is None
    assert state.is_connected is False

    # The subprocess teardown runs in the background without blocking the caller.
    for _ in range(10):
        await asyncio.sleep(0)
    state.adapter.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_remove_broken_missing_session_is_noop():
    pool = SessionPool(max_size=4)
    assert await pool.remove_broken("nope") is None


# ─── handle_interrupt_for_query: drain timeout evicts the broken client ────


@pytest.mark.asyncio
async def test_drain_timeout_evicts_pooled_client_and_still_confirms(monkeypatch):
    """When the streaming task must be force-cancelled, the pooled client is
    in an unknown mid-turn state and MUST be evicted before confirming, or the
    follow-up query kills the subprocess and stops the session."""
    from app.api import chat_ws_handler as cwh
    from app.api import websocket as ws_mod
    from app.core import session_pool as sp

    session_id = "send-now-test-session"

    pool = SessionPool(max_size=4)
    state = _fake_state()
    pool.put(session_id, state)
    monkeypatch.setattr(sp, "_session_pool", pool, raising=False)
    monkeypatch.setattr(sp, "get_session_pool", lambda: pool)
    monkeypatch.setattr(cwh, "_INTERRUPT_DRAIN_TIMEOUT_SECONDS", 0.05, raising=True)

    # A streaming task that ignores the interrupt and only ends on cancel —
    # mirrors a CLI wedged in extended thinking.
    async def _stubborn_stream():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass  # generator swallows CancelledError, like the real stream

    task = asyncio.create_task(_stubborn_stream())
    await asyncio.sleep(0)
    ws_mod._active_chat_sessions[session_id] = task

    handler = cwh.ChatWebSocketHandler(
        websocket=None, device_id="d1", user_id="u1", api_user_id=None
    )
    handler.current_session_id = session_id
    sent: list[dict] = []

    async def _capture_send(data):
        sent.append(data)
        return True

    handler.send_json = _capture_send

    try:
        with patch("app.core.query_engine.interrupt_session", new=AsyncMock(return_value=True)):
            await asyncio.wait_for(
                handler.handle_interrupt_for_query({"session_id": session_id}),
                timeout=5.0,
            )
    finally:
        ws_mod._active_chat_sessions.pop(session_id, None)
        if not task.done():
            task.cancel()

    assert any(m.get("type") == "interrupt_confirmed" for m in sent), sent
    # The broken client must be gone from the pool.
    assert pool.peek(session_id) is None
    assert state.is_connected is False


# ─── escalation must NOT evict a healthy client with live background work ───


def _wire_pool(monkeypatch, session_id, state):
    """Put ``state`` in a fresh pool and patch session_pool's singleton +
    getter so both chat_ws_handler and the helper resolve to it."""
    from app.core import session_pool as sp

    pool = SessionPool(max_size=4)
    pool.put(session_id, state)
    monkeypatch.setattr(sp, "_session_pool", pool, raising=False)
    monkeypatch.setattr(sp, "get_session_pool", lambda: pool)
    return pool


async def _stubborn_task():
    """A streaming task that ignores interrupt and only ends on cancel."""
    async def _run():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
    task = asyncio.create_task(_run())
    await asyncio.sleep(0)
    return task


def _make_handler(session_id):
    from app.api import chat_ws_handler as cwh

    handler = cwh.ChatWebSocketHandler(
        websocket=None, device_id="d1", user_id="u1", api_user_id=None
    )
    handler.current_session_id = session_id
    sent: list[dict] = []

    async def _capture_send(data):
        sent.append(data)
        return True

    handler.send_json = _capture_send
    return handler, sent


@pytest.mark.asyncio
async def test_drain_timeout_keeps_client_when_live_background_work_and_alive(monkeypatch):
    """Interrupt escalation must NEVER evict a HEALTHY client that still owns
    live background work — remove_broken → SIGTERM would kill every sub-agent /
    backgrounded shell parented to that subprocess. The turn is cancelled, but
    the client stays pooled and the interrupt is still confirmed."""
    from app.api import chat_ws_handler as cwh
    from app.api import websocket as ws_mod
    from app.core import task_registry as tr_mod

    session_id = "bg-work-alive-session"

    state = _fake_state()
    state.adapter.is_alive = MagicMock(return_value=True)  # subprocess alive
    pool = _wire_pool(monkeypatch, session_id, state)
    monkeypatch.setattr(cwh, "_INTERRUPT_DRAIN_TIMEOUT_SECONDS", 0.05, raising=True)
    monkeypatch.setattr(
        tr_mod.task_registry, "has_active_background_work", lambda sid: True
    )

    task = await _stubborn_task()
    ws_mod._active_chat_sessions[session_id] = task
    handler, sent = _make_handler(session_id)

    try:
        with patch("app.core.query_engine.interrupt_session", new=AsyncMock(return_value=True)):
            await asyncio.wait_for(
                handler.handle_interrupt_for_query({"session_id": session_id}),
                timeout=5.0,
            )
    finally:
        ws_mod._active_chat_sessions.pop(session_id, None)
        if not task.done():
            task.cancel()

    # Confirmed AND the wedged parent turn was force-cancelled ...
    assert any(m.get("type") == "interrupt_confirmed" for m in sent), sent
    assert task.done()
    # ... but the healthy client with live children MUST stay pooled.
    assert pool.peek(session_id) is state
    assert state.is_connected is True


@pytest.mark.asyncio
async def test_drain_timeout_evicts_when_background_work_but_subprocess_dead(monkeypatch):
    """If the subprocess is already DEAD, the background children are gone anyway
    and a broken client must not be reused — evict regardless of the registry."""
    from app.api import chat_ws_handler as cwh
    from app.api import websocket as ws_mod
    from app.core import task_registry as tr_mod

    session_id = "bg-work-dead-session"

    state = _fake_state()
    state.adapter.is_alive = MagicMock(return_value=False)  # subprocess dead
    pool = _wire_pool(monkeypatch, session_id, state)
    monkeypatch.setattr(cwh, "_INTERRUPT_DRAIN_TIMEOUT_SECONDS", 0.05, raising=True)
    monkeypatch.setattr(
        tr_mod.task_registry, "has_active_background_work", lambda sid: True
    )

    task = await _stubborn_task()
    ws_mod._active_chat_sessions[session_id] = task
    handler, sent = _make_handler(session_id)

    try:
        with patch("app.core.query_engine.interrupt_session", new=AsyncMock(return_value=True)):
            await asyncio.wait_for(
                handler.handle_interrupt_for_query({"session_id": session_id}),
                timeout=5.0,
            )
    finally:
        ws_mod._active_chat_sessions.pop(session_id, None)
        if not task.done():
            task.cancel()

    assert any(m.get("type") == "interrupt_confirmed" for m in sent), sent
    assert pool.peek(session_id) is None
    assert state.is_connected is False


@pytest.mark.asyncio
async def test_stop_safety_timer_cancels_but_keeps_client_with_live_background_work(monkeypatch):
    """handle_stop's safety timer force-cancels a query task that outlives the
    budget, but with live background work parented to a still-alive subprocess it
    must NOT evict the pooled client (that would SIGTERM every sub-agent/shell)."""
    from app.api import chat_ws_handler as cwh
    from app.api import websocket as ws_mod
    from app.core import task_registry as tr_mod

    session_id = "stop-bg-work-session"

    state = _fake_state()
    state.adapter.is_alive = MagicMock(return_value=True)  # subprocess alive
    pool = _wire_pool(monkeypatch, session_id, state)
    # Tiny safety budget so the timer fires almost immediately.
    monkeypatch.setattr(cwh, "_STOP_SAFETY_TIMEOUT_SECONDS", 0.05, raising=True)
    monkeypatch.setattr(
        tr_mod.task_registry, "has_active_background_work", lambda sid: True
    )
    # handle_stop reaches into _stopped_sessions — keep that mutation local.
    monkeypatch.setattr(cwh, "_stopped_sessions", set(), raising=True)

    task = await _stubborn_task()
    ws_mod._active_chat_sessions[session_id] = task
    handler, _sent = _make_handler(session_id)

    try:
        with patch("app.core.query_engine.interrupt_session", new=AsyncMock(return_value=True)):
            await handler.handle_stop({"session_id": session_id})
            # handle_stop spawns _safety_cancel as a background task; give the
            # (shrunk) timer time to fire and force-cancel.
            for _ in range(50):
                if task.done():
                    break
                await asyncio.sleep(0.02)
    finally:
        ws_mod._active_chat_sessions.pop(session_id, None)
        if not task.done():
            task.cancel()

    # The wedged turn was cancelled ...
    assert task.done()
    # ... but the healthy client with live children MUST stay pooled.
    assert pool.peek(session_id) is state
    assert state.is_connected is True


@pytest.mark.asyncio
async def test_stop_safety_timer_evicts_when_no_background_work(monkeypatch):
    """No live background work → original behavior: the safety timer cancels the
    task AND evicts the (possibly wedged) pooled client."""
    from app.api import chat_ws_handler as cwh
    from app.api import websocket as ws_mod
    from app.core import task_registry as tr_mod

    session_id = "stop-no-bg-session"

    state = _fake_state()
    state.adapter.is_alive = MagicMock(return_value=True)
    pool = _wire_pool(monkeypatch, session_id, state)
    monkeypatch.setattr(cwh, "_STOP_SAFETY_TIMEOUT_SECONDS", 0.05, raising=True)
    monkeypatch.setattr(
        tr_mod.task_registry, "has_active_background_work", lambda sid: False
    )
    monkeypatch.setattr(cwh, "_stopped_sessions", set(), raising=True)

    task = await _stubborn_task()
    ws_mod._active_chat_sessions[session_id] = task
    handler, _sent = _make_handler(session_id)

    try:
        with patch("app.core.query_engine.interrupt_session", new=AsyncMock(return_value=True)):
            await handler.handle_stop({"session_id": session_id})
            for _ in range(50):
                if pool.peek(session_id) is None:
                    break
                await asyncio.sleep(0.02)
    finally:
        ws_mod._active_chat_sessions.pop(session_id, None)
        if not task.done():
            task.cancel()

    assert task.done()
    assert pool.peek(session_id) is None
    assert state.is_connected is False


# ─── stale_turn_undrained: keep-branch flags + starts the between-turns drain ─


def _bg_alive_patches(monkeypatch, session_id, state):
    """Common wiring for keep-branch escalation tests: alive subprocess, live
    background work, a fresh pool, a fake session row, and a captured
    _start_drain_pump so we can assert the pump is (or isn't) kicked."""
    from app.api import chat_ws_handler as cwh
    from app.core import task_registry as tr_mod

    state.adapter.is_alive = MagicMock(return_value=True)
    pool = _wire_pool(monkeypatch, session_id, state)
    monkeypatch.setattr(
        tr_mod.task_registry, "has_active_background_work", lambda sid: True
    )

    session_row = {
        "id": session_id,
        "profile_id": "claude-code",
        "project_id": "proj-1",
        "worktree_id": "wt-1",
    }
    monkeypatch.setattr(cwh.database, "get_session", lambda sid: session_row)
    return pool


@pytest.mark.asyncio
async def test_drain_timeout_keep_branch_flags_stale_and_starts_drainer(monkeypatch):
    """Send-now escalation KEEPS the client (live bg work + alive subprocess): it
    must FLAG the pooled state ``stale_turn_undrained`` (before cancelling the
    task) and START the between-turns drain pump to consume the stale terminal
    frame. interrupt_confirmed is still sent."""
    from app.api import chat_ws_handler as cwh
    from app.api import websocket as ws_mod

    session_id = "flag-drain-session"
    state = _fake_state(is_streaming=True)  # mid-turn at fire time
    pool = _bg_alive_patches(monkeypatch, session_id, state)
    monkeypatch.setattr(cwh, "_INTERRUPT_DRAIN_TIMEOUT_SECONDS", 0.05, raising=True)

    started: list = []
    monkeypatch.setattr(
        cwh.ChatWebSocketHandler, "_start_drain_pump",
        lambda self, sid, pid, prj, wt: started.append((sid, pid, prj, wt)),
    )

    task = await _stubborn_task()
    ws_mod._active_chat_sessions[session_id] = task
    handler, sent = _make_handler(session_id)

    try:
        with patch("app.core.query_engine.interrupt_session", new=AsyncMock(return_value=True)):
            await asyncio.wait_for(
                handler.handle_interrupt_for_query({"session_id": session_id}),
                timeout=5.0,
            )
    finally:
        ws_mod._active_chat_sessions.pop(session_id, None)
        if not task.done():
            task.cancel()

    assert any(m.get("type") == "interrupt_confirmed" for m in sent), sent
    assert pool.peek(session_id) is state  # kept
    assert state.stale_turn_undrained is True
    assert started == [(session_id, "claude-code", "proj-1", "wt-1")], started


@pytest.mark.asyncio
async def test_stop_safety_keep_branch_flags_stale_and_starts_drainer(monkeypatch):
    """handle_stop's safety timer keep-branch (live bg work + alive) must flag
    stale + start the drain pump, same as the send-now path."""
    from app.api import chat_ws_handler as cwh
    from app.api import websocket as ws_mod

    session_id = "flag-stop-session"
    state = _fake_state(is_streaming=True)
    pool = _bg_alive_patches(monkeypatch, session_id, state)
    monkeypatch.setattr(cwh, "_STOP_SAFETY_TIMEOUT_SECONDS", 0.05, raising=True)
    monkeypatch.setattr(cwh, "_stopped_sessions", set(), raising=True)

    started: list = []
    monkeypatch.setattr(
        cwh.ChatWebSocketHandler, "_start_drain_pump",
        lambda self, sid, pid, prj, wt: started.append((sid, pid, prj, wt)),
    )

    task = await _stubborn_task()
    ws_mod._active_chat_sessions[session_id] = task
    handler, _sent = _make_handler(session_id)

    try:
        with patch("app.core.query_engine.interrupt_session", new=AsyncMock(return_value=True)):
            await handler.handle_stop({"session_id": session_id})
            for _ in range(50):
                if started:
                    break
                await asyncio.sleep(0.02)
    finally:
        ws_mod._active_chat_sessions.pop(session_id, None)
        if not task.done():
            task.cancel()

    assert task.done()
    assert pool.peek(session_id) is state
    assert state.stale_turn_undrained is True
    assert started == [(session_id, "claude-code", "proj-1", "wt-1")], started


@pytest.mark.asyncio
async def test_evict_branch_does_not_flag_or_start_drainer(monkeypatch):
    """The EVICT branch (no live background work) must NOT flag stale nor start a
    drain pump — the client is being torn down, there is nothing to drain."""
    from app.api import chat_ws_handler as cwh
    from app.api import websocket as ws_mod
    from app.core import task_registry as tr_mod

    session_id = "evict-no-flag-session"
    state = _fake_state(is_streaming=True)
    state.adapter.is_alive = MagicMock(return_value=True)
    pool = _wire_pool(monkeypatch, session_id, state)
    monkeypatch.setattr(cwh, "_INTERRUPT_DRAIN_TIMEOUT_SECONDS", 0.05, raising=True)
    monkeypatch.setattr(
        tr_mod.task_registry, "has_active_background_work", lambda sid: False
    )

    started: list = []
    monkeypatch.setattr(
        cwh.ChatWebSocketHandler, "_start_drain_pump",
        lambda self, sid, pid, prj, wt: started.append((sid, pid, prj, wt)),
    )

    task = await _stubborn_task()
    ws_mod._active_chat_sessions[session_id] = task
    handler, sent = _make_handler(session_id)

    try:
        with patch("app.core.query_engine.interrupt_session", new=AsyncMock(return_value=True)):
            await asyncio.wait_for(
                handler.handle_interrupt_for_query({"session_id": session_id}),
                timeout=5.0,
            )
    finally:
        ws_mod._active_chat_sessions.pop(session_id, None)
        if not task.done():
            task.cancel()

    assert any(m.get("type") == "interrupt_confirmed" for m in sent), sent
    assert pool.peek(session_id) is None  # evicted
    assert started == []


# ─── drain pump: clears the flag on the stale ResultMessage + kicks the queue ─


@pytest.mark.asyncio
async def test_drainer_clears_flag_on_stale_result_and_kicks_queue(monkeypatch):
    """The pump, reading a fake client that yields ONE stale ResultMessage while
    stale_turn_undrained is set + a queued message waits, must clear the flag and
    invoke _process_next_queued. When _stopped_sessions is latched it must NOT
    kick the queue."""
    from app.api import chat_ws_handler as cwh
    from app.core.query_engine import ResultMessage

    session_id = "drainer-clears-session"

    def _make_result():
        return ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sdk-1",
            total_cost_usd=0.0,
            usage={},
            result="done",
        )

    class _FakeClient:
        def __init__(self, messages):
            self._messages = list(messages)

        async def receive_messages(self):
            for m in self._messages:
                yield m
            await asyncio.Event().wait()  # then hang

    def _fresh_state(client):
        st = MagicMock()
        st.is_streaming = False
        st.stale_turn_undrained = True
        st.client = client
        adapter = MagicMock()
        adapter.is_alive = MagicMock(return_value=True)
        st.adapter = adapter
        return st

    # Case A: queue non-empty, no stop latch → kick the queue after clearing.
    state_a = _fresh_state(_FakeClient([_make_result()]))
    pool_a = MagicMock()
    pool_a.peek = MagicMock(return_value=state_a)

    kicked: list = []

    async def _fake_process(sid):
        kicked.append(sid)

    monkeypatch.setattr(cwh, "_stopped_sessions", set(), raising=True)
    monkeypatch.setattr(cwh, "_interrupt_pending_sessions", set(), raising=True)

    # Non-empty queue + drain-flag both must be visible to the pump's ResultMessage
    # branch. The pump checks queue-non-empty via database.get_queued_messages.
    from app.core.task_registry import task_registry
    task_registry.reset_all()
    monkeypatch.setattr(cwh.database, "get_queued_messages", lambda sid: [{"id": "q1"}])

    handler_a = cwh.ChatWebSocketHandler(
        websocket=MagicMock(), device_id="d1", user_id="u1", api_user_id=None
    )
    handler_a.send_json = AsyncMock(return_value=True)
    handler_a._deliver_unpark = AsyncMock(return_value=True)
    handler_a._process_next_queued = AsyncMock(side_effect=_fake_process)

    with patch("app.core.session_pool.get_session_pool", return_value=pool_a):
        await asyncio.wait_for(
            handler_a._drain_notifications(session_id, "claude-code", None, None),
            timeout=5.0,
        )

    assert state_a.stale_turn_undrained is False
    assert kicked == [session_id]

    # Case B: same, but _stopped_sessions latched → NO queue kick.
    state_b = _fresh_state(_FakeClient([_make_result()]))
    pool_b = MagicMock()
    pool_b.peek = MagicMock(return_value=state_b)
    kicked_b: list = []

    monkeypatch.setattr(cwh, "_stopped_sessions", {session_id}, raising=True)
    monkeypatch.setattr(cwh.database, "get_queued_messages", lambda sid: [{"id": "q1"}])

    handler_b = cwh.ChatWebSocketHandler(
        websocket=MagicMock(), device_id="d1", user_id="u1", api_user_id=None
    )
    handler_b.send_json = AsyncMock(return_value=True)
    handler_b._deliver_unpark = AsyncMock(return_value=True)
    handler_b._process_next_queued = AsyncMock(side_effect=lambda sid: kicked_b.append(sid))

    with patch("app.core.session_pool.get_session_pool", return_value=pool_b):
        # The pump loops (flag stays, but no kick); cancel it after a beat.
        pump = asyncio.create_task(
            handler_b._drain_notifications(session_id, "claude-code", None, None)
        )
        await asyncio.sleep(0.1)
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass

    # Flag cleared (stale frame consumed) but the queue was NOT kicked.
    assert state_b.stale_turn_undrained is False
    assert kicked_b == []


# ─── query during stale drain enqueues; _process_next_queued re-queues on flag ─


@pytest.mark.asyncio
async def test_query_during_stale_drain_is_enqueued_not_run(monkeypatch):
    """handle_query arriving while the pooled state carries stale_turn_undrained
    (and no live task) must route to handle_queue_message, not start a turn."""
    from app.api import chat_ws_handler as cwh
    from app.api import websocket as ws_mod

    session_id = "enqueue-during-drain"
    state = _fake_state(is_streaming=False, stale_turn_undrained=True)
    _wire_pool(monkeypatch, session_id, state)

    # No live task registered for this session.
    ws_mod._active_chat_sessions.pop(session_id, None)

    handler, _sent = _make_handler(session_id)
    handler.api_user_id = None
    queued: list = []

    async def _fake_queue(data):
        queued.append(data)

    handler.handle_queue_message = _fake_queue

    # Minimal data payload; handle_query short-circuits at the busy/stale check
    # before any DB work when the stale flag routes it to the queue.
    await handler.handle_query({
        "session_id": session_id,
        "prompt": "hello during drain",
        "profile": "claude-code",
    })

    assert len(queued) == 1, queued
    assert queued[0]["content"] == "hello during drain"


@pytest.mark.asyncio
async def test_process_next_queued_requeues_while_stale_flag_set(monkeypatch):
    """_process_next_queued must re-queue (not launch) while the pooled state
    carries stale_turn_undrained — even with no live task registered."""
    from app.api import chat_ws_handler as cwh
    from app.api import websocket as ws_mod

    session_id = "requeue-while-stale"
    state = _fake_state(is_streaming=False, stale_turn_undrained=True)
    _wire_pool(monkeypatch, session_id, state)
    ws_mod._active_chat_sessions.pop(session_id, None)
    ws_mod._draining_sessions.discard(session_id)

    monkeypatch.setattr(cwh, "_stopped_sessions", set(), raising=True)
    monkeypatch.setattr(cwh, "_interrupt_pending_sessions", set(), raising=True)

    popped = {"content": "queued msg", "profile_id": "claude-code", "project_id": None}
    requeued: list = []

    monkeypatch.setattr(cwh.database, "pop_next_queued_message", lambda sid: dict(popped))
    monkeypatch.setattr(
        cwh.database, "requeue_message_at_front",
        lambda **kw: requeued.append(kw),
    )
    monkeypatch.setattr(cwh.database, "get_queued_messages", lambda sid: [])
    monkeypatch.setattr(cwh.database, "get_session", lambda sid: {"worktree_id": None})
    monkeypatch.setattr(cwh.sync_engine, "broadcast_queue_updated", AsyncMock())

    launched: list = []

    async def _fake_run_query(*a, **k):
        launched.append(a)

    handler, _sent = _make_handler(session_id)
    handler.run_query = _fake_run_query

    await handler._process_next_queued(session_id)

    assert launched == [], "must not launch a turn while the drain flag is set"
    assert len(requeued) == 1, requeued
    assert requeued[0]["content"] == "queued msg"


# ─── Bug 3: shielded SDK interrupt must not be cancelled on timeout ───────────


@pytest.mark.asyncio
async def test_interrupt_session_timeout_leaves_sdk_interrupt_uncancelled(monkeypatch):
    """On interrupt timeout, the in-flight client.interrupt() must be left running
    (shielded) rather than cancelled — cancelling it leaks pending_control_responses
    inside the SDK client. interrupt_session still returns False; the late
    completion must not raise an un-retrieved-exception warning."""
    state = _fake_state()

    observed = {"cancelled": False, "completed": False}
    release = asyncio.Event()

    async def _slow_interrupt():
        try:
            await release.wait()
            observed["completed"] = True
        except asyncio.CancelledError:
            observed["cancelled"] = True
            raise

    state.adapter.sdk_client.interrupt = AsyncMock(side_effect=_slow_interrupt)
    monkeypatch.setattr(lifecycle, "_SDK_INTERRUPT_TIMEOUT_SECONDS", 0.05)

    with patch.object(lifecycle, "get_session_pool", return_value=_StubPool(state)):
        result = await asyncio.wait_for(lifecycle.interrupt_session("s1"), timeout=2.0)

    assert result is False
    # Give the event loop a beat: the detached interrupt task must still be alive.
    await asyncio.sleep(0.05)
    assert observed["cancelled"] is False, "SDK interrupt must NOT be cancelled on timeout"

    # Let the detached task finish; its completion must be swallowed cleanly.
    release.set()
    await asyncio.sleep(0.05)
    assert observed["completed"] is True

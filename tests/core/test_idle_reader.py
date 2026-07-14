"""Unit tests for app/core/idle_reader.py — the between-turns single reader
(v2 core, Phase 2 A4).

The pooled SDK client is faked (an async generator of messages); the turns/runs
DB is exercised for real (pg_shim). Registry/recorder helpers are patched at the
query_engine / turn_recorder bindings the reader imports lazily.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import idle_reader, session_runtime
from app.core.query_engine import (
    ResultMessage,
    TaskNotificationMessage,
)
from app.db import turns as turns_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db):
        yield test_db


@pytest.fixture(autouse=True)
def _clean_module_state():
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime.register_scheduler(None)
    idle_reader.register_on_task_terminal(None)
    yield
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime.register_scheduler(None)
    idle_reader.register_on_task_terminal(None)


# ---------------------------------------------------------------------------
# Fake pooled client / pool
# ---------------------------------------------------------------------------

def _make_message(cls, **attrs):
    """A bare instance of a real SDK message class (bypassing __init__) with the
    attributes the reader touches — so ``isinstance`` gates fire correctly."""
    msg = cls.__new__(cls)
    for k in ("parent_tool_use_id", "task_id"):
        attrs.setdefault(k, None)
    for k, v in attrs.items():
        setattr(msg, k, v)
    return msg


class _FakeAgen:
    """Async generator over a list of messages; raises ``raise_exc`` (if given)
    when the list is exhausted, else ``StopAsyncIteration``."""

    def __init__(self, messages, raise_exc=None):
        self._it = iter(messages)
        self._raise_exc = raise_exc

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            if self._raise_exc is not None:
                raise self._raise_exc
            raise StopAsyncIteration


def _make_pool(messages, *, alive=True, raise_exc=None):
    """A get_session_pool() stand-in whose peek() yields a drainable state whose
    client streams ``messages``."""
    client = MagicMock()
    client.receive_messages.return_value = _FakeAgen(messages, raise_exc=raise_exc)

    state = MagicMock()
    state.adapter.is_alive.return_value = alive
    state.is_streaming = False
    state.client = client

    pool = MagicMock()
    pool.peek.return_value = state
    pool.remove_broken = AsyncMock()
    pool.remove_dead = AsyncMock()
    return pool, state


@pytest.fixture(autouse=True)
def _neutralize_helpers():
    """Stub the registry/recorder taps so the reader's message handling reduces
    to its control flow (isinstance gates + settlement/handoff)."""
    with ExitStack() as stack:
        stack.enter_context(patch("app.core.query_engine._record_subagent_lane_launches", new=MagicMock()))
        stack.enter_context(patch("app.core.query_engine._record_task_message", new=MagicMock()))
        stack.enter_context(patch("app.core.query_engine._subagent_events_from_message", new=MagicMock(return_value=[])))
        stack.enter_context(patch("app.core.query_engine._task_status_events_from_message", new=MagicMock(return_value=[])))
        stack.enter_context(patch("app.core.query_engine._task_status_event_from_notification", new=MagicMock(return_value=[])))
        stack.enter_context(patch("app.core.query_engine._cli_turn_events_from_message", new=MagicMock(return_value=[])))
        stack.enter_context(patch("app.core.turn_recorder.record_drained_event", new=AsyncMock()))
        stack.enter_context(patch("app.core.turn_finalize.persist_turn_end_state", new=AsyncMock()))
        yield


# ---------------------------------------------------------------------------
# 1. Drain mode — stale ResultMessage settles aborted
# ---------------------------------------------------------------------------

async def test_drain_mode_settles_aborted_on_result(mock_db):
    turn = turns_db.enqueue_turn("s1", "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")  # live, drainable

    scheduler = MagicMock()
    session_runtime.register_scheduler(scheduler)

    result_msg = _make_message(ResultMessage)
    pool, _ = _make_pool([result_msg])

    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        await idle_reader.run_idle_reader("s1", drain_turn_id=turn["id"])

    assert turns_db.get_turn(turn["id"])["state"] == "aborted"
    scheduler.assert_called_once_with("s1", turn["id"], "aborted")


# ---------------------------------------------------------------------------
# 2. Terminal task notification handoff
# ---------------------------------------------------------------------------

async def test_terminal_notification_handoff(mock_db):
    turns_db.enqueue_turn("s2", "user", prompt="hi")  # no live turn needed

    handoff = AsyncMock()
    idle_reader.register_on_task_terminal(handoff)

    notif = _make_message(TaskNotificationMessage)
    frame = {"status": "completed", "task_id": "task-9", "tool_use_id": "tu-9", "summary": "done"}
    pool, _ = _make_pool([notif])  # stream: notif, then StopAsyncIteration

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_notification_frame", return_value=frame))
        await idle_reader.run_idle_reader("s2")

    handoff.assert_awaited_once_with("s2", frame)


async def test_non_terminal_notification_no_handoff(mock_db):
    handoff = AsyncMock()
    idle_reader.register_on_task_terminal(handoff)

    notif = _make_message(TaskNotificationMessage)
    frame = {"status": "running", "task_id": "task-1", "tool_use_id": "tu-1"}
    pool, _ = _make_pool([notif])

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_notification_frame", return_value=frame))
        await idle_reader.run_idle_reader("s2")

    handoff.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Broken-client eviction on read error
# ---------------------------------------------------------------------------

async def test_broken_client_evicted_on_read_error(mock_db):
    pool, _ = _make_pool([], raise_exc=RuntimeError("read loop dead"))

    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        await idle_reader.run_idle_reader("s3")

    # subprocess alive → remove_broken (not remove_dead).
    pool.remove_broken.assert_awaited_once_with("s3")
    pool.remove_dead.assert_not_awaited()


async def test_stream_end_evicts_client(mock_db):
    """A cleanly-ended stream (StopAsyncIteration) also evicts — a finished
    stream cannot serve another turn."""
    pool, _ = _make_pool([])  # immediate StopAsyncIteration

    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        await idle_reader.run_idle_reader("s4")

    pool.remove_broken.assert_awaited_once_with("s4")


async def test_no_drainable_client_returns_quietly(mock_db):
    """A missing pooled state is a clean no-op (nothing to drain)."""
    pool = MagicMock()
    pool.peek.return_value = None

    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        await idle_reader.run_idle_reader("s5")  # must not raise

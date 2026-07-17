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
from app.core.task_registry import task_registry
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


async def test_terminal_notification_record_includes_task_id(mock_db):
    """The drain-pump task_notification chip record must carry ``task_id`` so
    turn_recorder can populate the contract task.updated event (else the reducer
    bails on empty task_id and the agent's summary never reaches its card)."""
    turns_db.enqueue_turn("s2", "user", prompt="hi")
    idle_reader.register_on_task_terminal(AsyncMock())
    # task-9 is the parent's OWN background work so the chip is recorded (the
    # ownership gate would otherwise suppress a non-owned task's chip).
    task_registry.start_task("s2", "task-9", "agent", "bg", tool_use_id="tu-9")
    task_registry.mark_background_launch("s2", "tu-9")

    notif = _make_message(TaskNotificationMessage)
    frame = {"status": "completed", "task_id": "task-9", "tool_use_id": "tu-9",
             "summary": "done", "usage": None}
    pool, _ = _make_pool([notif])

    rec = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_notification_frame", return_value=frame))
        stack.enter_context(patch("app.core.turn_recorder.record_drained_event", new=rec))
        await idle_reader.run_idle_reader("s2")

    rec.assert_awaited_once()
    _, recorded = rec.call_args.args
    assert recorded["type"] == "task_notification"
    assert recorded["task_id"] == "task-9"
    assert recorded["tool_use_id"] == "tu-9"


async def test_idle_reader_fires_handoff_regardless_of_ownership(mock_db):
    """The reader stays GENERIC: a terminal notification for a NON-owned
    (grandchild/synchronous) task still hands off to the scheduler seam — the
    ownership policy lives in on_task_terminal, not in the reader."""
    turns_db.enqueue_turn("sg", "user", prompt="hi")

    handoff = AsyncMock()
    idle_reader.register_on_task_terminal(handoff)

    notif = _make_message(TaskNotificationMessage)
    frame = {"status": "completed", "task_id": "gc-x", "tool_use_id": "tu-gc-x",
             "summary": "done"}
    pool, _ = _make_pool([notif])

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_notification_frame", return_value=frame))
        await idle_reader.run_idle_reader("sg")

    handoff.assert_awaited_once_with("sg", frame)


async def test_grandchild_terminal_chip_suppressed(mock_db):
    """R2: the terminal task_notification CHIP is suppressed only for a task that
    is POSITIVELY subagent-owned. A grandchild's terminal notification (its
    launch tool_use armed as a subagent background launch) must NOT record a
    task_notification chip in the parent chat — even though the handoff still
    fires."""
    turns_db.enqueue_turn("sg", "user", prompt="hi")
    idle_reader.register_on_task_terminal(AsyncMock())
    # Positively subagent-owned: the grandchild's launch tool_use is armed.
    task_registry.mark_subagent_background_launch("sg", "tu-gc-x")

    notif = _make_message(TaskNotificationMessage)
    frame = {"status": "completed", "task_id": "gc-x", "tool_use_id": "tu-gc-x",
             "summary": "done", "usage": None}
    pool, _ = _make_pool([notif])

    rec = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_notification_frame", return_value=frame))
        stack.enter_context(patch("app.core.turn_recorder.record_drained_event", new=rec))
        await idle_reader.run_idle_reader("sg")

    # No task_notification chip recorded for a subagent-owned (grandchild) task.
    assert not any(
        call.args[1].get("type") == "task_notification"
        for call in rec.call_args_list
    )


async def test_unknown_task_terminal_chip_recorded(mock_db):
    """Pre-diff safety net: an UNKNOWN terminal task (no ownership lane armed
    either way) keeps the always-chip behavior so an ownership-arming MISS can
    never leave a subagent card stuck-active (the #617/#659 settle path)."""
    turns_db.enqueue_turn("sg", "user", prompt="hi")
    idle_reader.register_on_task_terminal(AsyncMock())

    notif = _make_message(TaskNotificationMessage)
    frame = {"status": "completed", "task_id": "gc-x", "tool_use_id": "tu-gc-x",
             "summary": "done", "usage": None}
    pool, _ = _make_pool([notif])

    rec = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_notification_frame", return_value=frame))
        stack.enter_context(patch("app.core.turn_recorder.record_drained_event", new=rec))
        await idle_reader.run_idle_reader("sg")

    # An unknown task still records its terminal task_notification chip.
    assert any(
        call.args[1].get("type") == "task_notification"
        for call in rec.call_args_list
    )


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


# ---------------------------------------------------------------------------
# 5. Killed TaskUpdated-alone terminal — single chip + tool_use_id handoff
# ---------------------------------------------------------------------------

async def test_killed_task_update_single_chip_and_tool_use_id_handoff(mock_db):
    """A killed background task terminating via a ``TaskUpdatedMessage`` ALONE:

    (a) records EXACTLY ONE task_updated chip (only the generic own-background
        branch — the deleted killed-branch chip must not duplicate it), and
    (b) hands off the LAUNCH tool_use_id (from the registry record) — not the
        hardcoded None — so the terminal chip can settle the subagent card.
    """
    from app.core.query_engine import TaskUpdatedMessage
    from app.core.task_registry import task_registry

    task_registry.reset_all()
    # An own background task (registry record carries the launch tool_use_id).
    task_registry.start_task("s6", "task-k", "agent", "bg", tool_use_id="tu-k")
    task_registry.mark_background_launch("s6", "tu-k")

    handoff = AsyncMock()
    idle_reader.register_on_task_terminal(handoff)

    upd = _make_message(
        TaskUpdatedMessage,
        task_id="task-k",
        subtype="task_updated",
        data={"task_id": "task-k", "patch": {"status": "killed"}},
    )
    pool, _ = _make_pool([upd])

    rec = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_update_terminal_status", return_value="killed"))
        # The generic own-background chip branch fires (it is the ONLY chip source
        # now the killed-branch chip is deleted).
        stack.enter_context(patch(
            "app.core.query_engine._task_status_events_from_message",
            new=MagicMock(return_value=[{
                "type": "system", "subtype": "task_updated",
                "data": {"task_id": "task-k", "patch": {"status": "killed"}},
            }]),
        ))
        stack.enter_context(patch("app.core.turn_recorder.record_drained_event", new=rec))
        await idle_reader.run_idle_reader("s6")

    # (a) exactly one chip (generic branch) — no duplicate from the deleted branch.
    assert rec.await_count == 1
    # (b) handoff carries the launch tool_use_id from the registry record.
    handoff.assert_awaited_once()
    _, frame = handoff.call_args.args
    assert frame["task_id"] == "task-k"
    assert frame["status"] == "killed"
    assert frame["tool_use_id"] == "tu-k"

    task_registry.reset_all()

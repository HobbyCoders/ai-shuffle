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


async def test_drain_mode_settles_fluid_adapter_without_sdk_client(mock_db):
    """Fluid/harness adapters expose no SDK client (state.client is None). The
    drain used to return without settling, leaving the turn 'draining' after
    execute() released its lease — reap_expired then requeued and RE-RAN the
    interrupted user turn (replayed prompt, re-spawned Task workers). The
    reader must settle the drained turn aborted once the wedge resolves."""
    turn = turns_db.enqueue_turn("s1", "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")

    scheduler = MagicMock()
    session_runtime.register_scheduler(scheduler)

    pool, state = _make_pool([])
    state.client = None  # fluid adapter: no SDK receive_messages() stream

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


# ---------------------------------------------------------------------------
# 6. Completed task: bare TaskUpdated + rich TaskNotification → ONE effective wake
# ---------------------------------------------------------------------------

async def test_completed_task_updated_plus_notification_single_effective_wake(mock_db):
    """A completed background task streams BOTH frames the SDK emits: the bare
    ``TaskUpdatedMessage`` (task_id/status/tool_use_id only) AND the rich
    ``TaskNotificationMessage`` (summary/usage). Both branches hand off to the
    scheduler (idle_reader.py:339, :385) — the historical DOUBLE handoff.

    With the REAL ``turn_scheduler.on_task_terminal`` seam and a live (NOT parked)
    parent, the bare frame builds the OWN wake and the rich frame UPGRADES it in
    place (Fix A-2): the session ends with exactly ONE queued ``unpark`` row whose
    prompt carries the rich summary — not two rows, not a bare-only wake.
    """
    from app.core.query_engine import TaskUpdatedMessage
    from app.core import turn_scheduler

    task_registry.reset_all()
    turn = turns_db.enqueue_turn("s7", "user", prompt="kick off bg")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.settle_turn(turn["id"], "completed")  # settled, NOT parked
    task_registry.start_task("s7", "task-bt", "agent", "bg", tool_use_id="tu-bt")
    task_registry.mark_background_launch("s7", "tu-bt")

    idle_reader.register_on_task_terminal(turn_scheduler.on_task_terminal)

    upd = _make_message(
        TaskUpdatedMessage, task_id="task-bt", subtype="task_updated",
        data={"task_id": "task-bt", "patch": {"status": "completed"}},
    )
    notif = _make_message(TaskNotificationMessage, task_id="task-bt")
    rich_frame = {
        "type": "task_notification", "task_id": "task-bt", "status": "completed",
        "tool_use_id": "tu-bt", "summary": "crawl done", "output_file": "",
        "usage": {"total_tokens": 99, "tool_uses": 3, "duration_ms": 2000},
    }
    pool, _ = _make_pool([upd, notif])

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_update_terminal_status", return_value="completed"))
        stack.enter_context(patch("app.core.query_engine._task_notification_frame", return_value=rich_frame))
        stack.enter_context(patch("app.core.query_engine._read_task_output_file", return_value=""))
        stack.enter_context(patch("app.core.session_runtime.execute", new=AsyncMock(return_value="completed")))
        await idle_reader.run_idle_reader("s7")
        # Drain any kick tasks the scheduler spawned after the enqueue/upgrade.
        for _ in range(10):
            tasks = list(turn_scheduler._kick_tasks)
            if not tasks:
                break
            await asyncio.gather(*tasks, return_exceptions=True)

    wakes = [t for t in turns_db.get_queued_turns("s7") if t["source"] == "unpark"]
    assert len(wakes) == 1                                   # ONE effective wake
    assert "<summary>crawl done</summary>" in wakes[0]["prompt"]      # rich, upgraded
    assert "<subagent_tokens>99</subagent_tokens>" in wakes[0]["prompt"]
    assert wakes[0]["payload"]["task_ids"] == ["task-bt"]    # task not duplicated

    task_registry.reset_all()


# ---------------------------------------------------------------------------
# B-prime observability gate: the drain surfaces a running owner's inner
# activity (parent_tool_use_id = the owner's own background launch) so the
# deferred-pump discriminator can tell running from parked.
# ---------------------------------------------------------------------------

class _InnerMsg:
    """A plain drained message (not a task/result frame) carrying inner activity
    of a running sub-agent — only its ``parent_tool_use_id`` matters here."""


async def test_owner_inner_activity_stamps_note(mock_db):
    task_registry.reset_all()
    # tu-A is one of the parent's OWN background launches (an owner sub-agent).
    task_registry.mark_background_launch("s8", "tu-A")
    msg = _make_message(_InnerMsg, parent_tool_use_id="tu-A")
    pool, _ = _make_pool([msg])

    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        await idle_reader.run_idle_reader("s8")

    # The inner-message dispatch stamped the owner's activity.
    assert task_registry.owner_activity_ts("s8", "tu-A") is not None
    task_registry.reset_all()


async def test_non_owner_inner_activity_not_stamped(mock_db):
    task_registry.reset_all()
    # parent_tool_use_id is NOT a parent background launch → no owner activity.
    msg = _make_message(_InnerMsg, parent_tool_use_id="tu-stranger")
    pool, _ = _make_pool([msg])

    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        await idle_reader.run_idle_reader("s9")

    assert task_registry.owner_activity_ts("s9", "tu-stranger") is None
    task_registry.reset_all()


# ---------------------------------------------------------------------------
# CLI-owned wake forwarding (#691): the wake turn's render surface reaches the
# session log AND is bracketed by a synthetic turn.started/turn.completed so an
# already-open client renders it live (no user poke) and speculative subagent
# cards settle.
# ---------------------------------------------------------------------------

class _WakeMsg:
    """A plain top-level message of a CLI-driven wake turn (its events are
    supplied by the mocked ``_cli_turn_events_from_message``)."""


def _cli_side_effect(events_for_wake):
    def _fn(session_id, message, tool_names):
        return list(events_for_wake) if isinstance(message, _WakeMsg) else []
    return _fn


def _wake_events(session_id):
    from app.db import session_events as se
    return se.get_events(session_id)


async def test_cli_wake_brackets_turn_and_records_render_surface(mock_db, caplog):
    """The wake turn's chunk/tool_use render surface is recorded (NOT dropped),
    wrapped in turn.started (before the first event) + turn.completed (on the
    ResultMessage), all sharing one synthetic turn_id."""
    import logging

    wake = _make_message(_WakeMsg)
    result_msg = _make_message(ResultMessage)
    pool, _ = _make_pool([wake, result_msg])

    cli = MagicMock(side_effect=_cli_side_effect([
        {"type": "chunk", "content": "all agents done"},
        {"type": "tool_use", "id": "tu-x", "name": "Bash", "input": {"command": "echo hi"}},
    ]))

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._cli_turn_events_from_message", new=cli))
        stack.enter_context(patch("app.core.turn_finalize.persist_turn_end_state", new=AsyncMock()))
        stack.enter_context(patch("app.core.query_persistence.schedule_context_usage_capture", new=MagicMock()))
        with caplog.at_level(logging.INFO, logger="app.core.idle_reader"):
            await idle_reader.run_idle_reader("cw1")

    rows = _wake_events("cw1")
    types = [r["event_type"] for r in rows]
    assert types == [
        "turn.started", "assistant.text_delta", "tool.use", "turn.completed",
    ]
    # One synthetic wake turn id ties the whole bracket together.
    tid = rows[0]["turn_id"]
    assert tid is not None and tid.startswith("cliwake-")
    assert all(r["turn_id"] == tid for r in rows)
    assert rows[1]["payload"]["text"] == "all agents done"

    # Delivery-vs-drop proof line: nothing dropped.
    proof = [r.message for r in caplog.records if "decision=cli-wake-record" in r.message]
    assert proof, "expected the cli-wake-record INFO proof line"
    assert "recorded=2" in proof[0]
    assert "dropped={}" in proof[0]


async def test_cli_wake_thinking_only_opens_no_turn(mock_db):
    """A wake message that yields only thinking_block (never rendered live) must
    NOT open a turn — so no unpaired turn.started/turn.completed lands."""
    wake = _make_message(_WakeMsg)
    result_msg = _make_message(ResultMessage)
    pool, _ = _make_pool([wake, result_msg])

    cli = MagicMock(side_effect=_cli_side_effect([
        {"type": "thinking_block", "content": "hmm"},
    ]))

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._cli_turn_events_from_message", new=cli))
        stack.enter_context(patch("app.core.turn_finalize.persist_turn_end_state", new=AsyncMock()))
        stack.enter_context(patch("app.core.query_persistence.schedule_context_usage_capture", new=MagicMock()))
        await idle_reader.run_idle_reader("cw2")

    assert _wake_events("cw2") == []


async def test_cli_wake_drain_mode_does_not_bracket(mock_db):
    """In drain mode (aborting a wedged turn) the wide bracketing is OFF — the
    forwarded events go through the narrow record_drained_event (which drops the
    render surface), and NO turn.started is appended."""
    turn = turns_db.enqueue_turn("cw3", "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")  # live, drainable
    session_runtime.register_scheduler(MagicMock())

    wake = _make_message(_WakeMsg)
    result_msg = _make_message(ResultMessage)
    pool, _ = _make_pool([wake, result_msg])

    cli = MagicMock(side_effect=_cli_side_effect([{"type": "chunk", "content": "x"}]))
    drained = AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._cli_turn_events_from_message", new=cli))
        stack.enter_context(patch("app.core.turn_recorder.record_drained_event", new=drained))
        await idle_reader.run_idle_reader("cw3", drain_turn_id=turn["id"])

    # Drain mode routes through record_drained_event (chunk → dropped there) and
    # never appends a synthetic turn.started.
    drained.assert_awaited_once()
    assert [r["event_type"] for r in _wake_events("cw3")] == []


# ---------------------------------------------------------------------------
# CLI-owned-wake grace window (LEG 1): when the LAST background task completes,
# the scheduler suppresses the app's own wake because the CLI delivers its own
# wake turn 7-60s later. The reader must NOT exit on its next poll — it arms a
# grace deadline and keeps draining until that wake lands (and then, LEG 2A,
# settles the stale parked turn rows).
# ---------------------------------------------------------------------------

class _StreamAgen:
    """Async generator over ``steps``. A step wrapped as ``('gap', msg)`` first
    awaits ``gap`` seconds (forcing >=1 idle poll-timeout) before yielding
    ``msg``. When ``steps`` is exhausted it raises ``StopAsyncIteration`` unless
    ``block`` — in which case it blocks forever (a wake that never arrives)."""

    def __init__(self, steps, *, gap=0.0, block=False):
        self._steps = list(steps)
        self._i = 0
        self._gap = gap
        self._block = block

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._steps):
            if self._block:
                await asyncio.Event().wait()  # never set — blocks forever
            raise StopAsyncIteration
        step = self._steps[self._i]
        self._i += 1
        if isinstance(step, tuple) and step[0] == "gap":
            await asyncio.sleep(self._gap)
            return step[1]
        return step


def _stream_pool(steps, *, gap=0.0, block=False):
    client = MagicMock()
    client.receive_messages.return_value = _StreamAgen(steps, gap=gap, block=block)
    state = MagicMock()
    state.adapter.is_alive.return_value = True
    state.is_streaming = False
    state.client = client
    pool = MagicMock()
    pool.peek.return_value = state
    pool.remove_broken = AsyncMock()
    pool.remove_dead = AsyncMock()
    return pool


async def test_cli_wake_grace_holds_reader_until_wake_lands_and_settles(
    mock_db, monkeypatch, caplog
):
    """The grace window keeps the reader draining across an idle poll gap so it
    reads the CLI-owned wake's ResultMessage — and then settles the stale parked
    turn (LEG 1 + LEG 2A). Without the grace the reader would exit at the first
    poll after the last task completed, leaving the wake buffered and the row
    parked forever."""
    import logging

    turn = turns_db.enqueue_turn("gw1", "user", prompt="hi")
    turns_db.start_turn(turn["id"])
    turns_db.park_turn(turn["id"], task_ids=["task-1"])

    # Handoff takes the CLI-owned suppression branch → arms the grace window.
    handoff = AsyncMock(return_value=idle_reader.CLI_OWNED_WAKE_SIGNAL)
    idle_reader.register_on_task_terminal(handoff)

    monkeypatch.setattr(idle_reader, "IDLE_READ_POLL_SECONDS", 0.01)
    monkeypatch.setattr(idle_reader.settings, "cli_wake_grace_seconds", 300.0)

    notif = _make_message(TaskNotificationMessage)
    result_msg = _make_message(ResultMessage)
    frame = {"status": "completed", "task_id": "task-1", "tool_use_id": "tu-1",
             "summary": "done"}
    # notif now; then a gap spanning several idle polls before the CLI wake's
    # ResultMessage; then the stream ends.
    pool = _stream_pool([notif, ("gap", result_msg)], gap=0.06)

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_notification_frame", return_value=frame))
        stack.enter_context(patch("app.core.query_persistence.schedule_context_usage_capture", new=MagicMock()))
        with caplog.at_level(logging.INFO, logger="app.core.idle_reader"):
            await asyncio.wait_for(idle_reader.run_idle_reader("gw1"), timeout=5)

    handoff.assert_awaited_once_with("gw1", frame)
    assert any("arming CLI-wake grace window" in r.message for r in caplog.records)
    # The reader survived the poll gap, read the wake's ResultMessage, and
    # settled the stale parked row.
    assert turns_db.get_turn(turn["id"])["state"] == "completed"


async def test_cli_wake_grace_expires_and_reader_exits(mock_db, monkeypatch, caplog):
    """If the CLI wake never arrives the reader must not hang: once the grace
    deadline lapses it exits (with the greppable grace-expiry log)."""
    import logging

    handoff = AsyncMock(return_value=idle_reader.CLI_OWNED_WAKE_SIGNAL)
    idle_reader.register_on_task_terminal(handoff)

    monkeypatch.setattr(idle_reader, "IDLE_READ_POLL_SECONDS", 0.01)
    monkeypatch.setattr(idle_reader.settings, "cli_wake_grace_seconds", 0.05)

    notif = _make_message(TaskNotificationMessage)
    frame = {"status": "completed", "task_id": "task-1", "tool_use_id": "tu-1",
             "summary": "done"}
    # notif now; then the stream blocks forever (no wake ever comes).
    pool = _stream_pool([notif], block=True)

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_notification_frame", return_value=frame))
        with caplog.at_level(logging.INFO, logger="app.core.idle_reader"):
            await asyncio.wait_for(idle_reader.run_idle_reader("gw2"), timeout=5)

    handoff.assert_awaited_once()
    assert any("CLI-wake grace expired" in r.message for r in caplog.records)


async def test_no_grace_without_cli_wake_signal_exits_immediately(mock_db, monkeypatch):
    """A terminal handoff that does NOT return the CLI-owned signal arms no grace
    window: with no background work the reader exits on the next poll as before
    (no regression for the ordinary at-rest case)."""
    handoff = AsyncMock(return_value=None)
    idle_reader.register_on_task_terminal(handoff)

    monkeypatch.setattr(idle_reader, "IDLE_READ_POLL_SECONDS", 0.01)
    # Long grace: if it were (wrongly) armed the reader would block past timeout.
    monkeypatch.setattr(idle_reader.settings, "cli_wake_grace_seconds", 300.0)

    notif = _make_message(TaskNotificationMessage)
    frame = {"status": "completed", "task_id": "task-1", "tool_use_id": "tu-1",
             "summary": "done"}
    # notif now; then block forever — the reader must still exit promptly since
    # no grace was armed.
    pool = _stream_pool([notif], block=True)

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._task_notification_frame", return_value=frame))
        await asyncio.wait_for(idle_reader.run_idle_reader("gw3"), timeout=5)

    handoff.assert_awaited_once()


async def test_cli_wake_grace_expires_mid_wake_and_reader_exits(
    mock_db, monkeypatch, caplog
):
    """A CLI that hangs MID-wake (the wake turn opened, then no frames, no read
    error) must not hold the reader forever: opening the wake arms a grace
    deadline, and once it lapses with the turn still open the reader exits with
    the greppable mid-wake expiry log — AND it CLOSES the open wake turn
    (turn.completed) + settles the stale parked row instead of leaving the FE
    stuck streaming and the row parked forever (FIX 3)."""
    import logging

    # A parked row with NO background work: closing the abandoned wake must
    # settle it (guarded on has_active_background_work, False for this session).
    turn = turns_db.enqueue_turn("gw4", "user", prompt="hi")
    turns_db.start_turn(turn["id"])
    turns_db.park_turn(turn["id"], task_ids=["task-1"])

    monkeypatch.setattr(idle_reader, "IDLE_READ_POLL_SECONDS", 0.01)
    monkeypatch.setattr(idle_reader.settings, "cli_wake_grace_seconds", 0.05)

    # A wake message opens the turn (renderable chunk → turn.started +
    # wake_turn_id set); then the stream blocks forever — no ResultMessage ever
    # closes the wake.
    wake = _make_message(_WakeMsg)
    pool = _stream_pool([wake], block=True)
    cli = MagicMock(side_effect=_cli_side_effect([{"type": "chunk", "content": "x"}]))

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch("app.core.query_engine._cli_turn_events_from_message", new=cli))
        with caplog.at_level(logging.INFO, logger="app.core.idle_reader"):
            await asyncio.wait_for(idle_reader.run_idle_reader("gw4"), timeout=5)

    assert any(
        "CLI-wake grace expired mid-wake" in r.message for r in caplog.records
    )
    # The wake turn was opened AND then CLOSED with a synthetic turn.completed
    # (the FE reducer needs it to flip streaming off — FIX 3).
    assert [r["event_type"] for r in _wake_events("gw4")] == [
        "turn.started", "assistant.text_delta", "turn.completed",
    ]
    # And the stale parked row was settled (no bg work remained).
    assert turns_db.get_turn(turn["id"])["state"] == "completed"

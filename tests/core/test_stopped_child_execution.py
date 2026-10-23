"""A stopped Task child's turn execution must end, and never count as running work.

The owner's report: after a review chat was stopped, one of its Task children
(a scorer) still counted as running work. Its parent's teardown had evicted
the child's client and settled the child's turn row, so the child's own
interrupt saw a settled turn and returned without cancelling its pump. The pump
waited forever on the evicted client, the execution stayed registered, every
later Stop was swallowed by the interrupt's idempotency guard, and every
application update was refused until the container was restarted.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.core import desktop_lifecycle, session_runtime
from app.db import turns as turns_db
from tests.core.test_session_runtime import _interrupt_env, _runtime_env, _wedged_pump


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    monkeypatch.setattr(session_runtime, "ORPHANED_PUMP_GRACE_SECONDS", 0.05)
    for registry in (session_runtime._reader_locks, session_runtime._idle_readers,
                     session_runtime._live_runtimes, session_runtime._executions,
                     session_runtime._execution_runtimes, session_runtime._ended_executions):
        registry.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)
    yield
    for registry in (session_runtime._reader_locks, session_runtime._idle_readers,
                     session_runtime._live_runtimes, session_runtime._executions,
                     session_runtime._execution_runtimes, session_runtime._ended_executions):
        registry.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)


def _stubborn_pump(entered: asyncio.Event, release: asyncio.Event):
    """A pump that ignores cancellation until released (the worst case)."""
    async def _impl(**kwargs):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        yield {"type": "interrupted"}
    return _impl


def _settled_by_parent(turn):
    """What the parent's teardown does to a Task child it stops."""
    assert turns_db.interrupt_turn(turn["session_id"], "stop", source="user_stopped") is not None
    assert turns_db.settle_turn(turn["id"], "aborted", "user_stopped") is not None


async def test_interrupt_ends_a_child_whose_turn_its_parent_already_settled(mock_db):
    turn = turns_db.enqueue_turn("child", "provider_agent", prompt="score")
    entered = asyncio.Event()
    with _runtime_env(pump=_wedged_pump(entered)) as env, _interrupt_env(evict=True):
        env["registry"].active_session_ids.return_value = []
        execution = asyncio.create_task(session_runtime.execute(turn))
        await asyncio.wait_for(entered.wait(), 2)
        assert desktop_lifecycle.active_session_ids() == {"child"}
        runtime = session_runtime.get_live_runtime("child")
        _settled_by_parent(turn)

        assert await asyncio.wait_for(runtime.interrupt("stop"), 2) == "aborted"
        # Before the fix the interrupt returned here and the execution never did.
        await asyncio.wait_for(execution, 2)
        assert desktop_lifecycle.active_session_ids() == set()

    row = turns_db.get_turn(turn["id"])
    assert row["state"] == "aborted" and row["stop_reason"] == "user_stopped"
    env["scheduler"].assert_not_called()  # the parent's settle owns the outcome


async def test_a_later_stop_ends_an_execution_an_earlier_interrupt_left_running(mock_db):
    turn = turns_db.enqueue_turn("child", "provider_agent", prompt="score")
    entered = asyncio.Event()
    with _runtime_env(pump=_wedged_pump(entered)), _interrupt_env(evict=True):
        execution = asyncio.create_task(session_runtime.execute(turn))
        await asyncio.wait_for(entered.wait(), 2)
        runtime = session_runtime.get_live_runtime("child")
        _settled_by_parent(turn)
        # An interrupt already ran to completion for this turn (as on the
        # build that hit the bug); its guard used to swallow every later Stop.
        runtime._interrupt_driving = runtime._interrupt_finished = True

        assert await runtime.interrupt("stop") == "interrupting"
        await asyncio.wait_for(execution, 2)


async def test_an_interrupt_still_in_progress_keeps_the_second_call_a_no_op(mock_db):
    turn = turns_db.enqueue_turn("busy", "user", prompt="hi")
    entered = asyncio.Event()
    with _runtime_env(pump=_wedged_pump(entered)), _interrupt_env(evict=True):
        execution = asyncio.create_task(session_runtime.execute(turn))
        await asyncio.wait_for(entered.wait(), 2)
        runtime = session_runtime.get_live_runtime("busy")
        runtime._interrupt_driving = True  # another driver owns the escalation clock
        assert await runtime.interrupt("stop") == "interrupting"
        assert not execution.done()
        runtime._cancel_pump()
        await asyncio.wait_for(execution, 2)


async def test_an_execution_that_outlives_its_turn_no_longer_counts_as_running(mock_db):
    turn = turns_db.enqueue_turn("child", "provider_agent", prompt="score")
    entered, release = asyncio.Event(), asyncio.Event()
    with _runtime_env(pump=_stubborn_pump(entered, release)) as env:
        env["registry"].active_session_ids.return_value = []
        execution = asyncio.create_task(session_runtime.execute(turn))
        await asyncio.wait_for(entered.wait(), 2)
        # A live turn is never reconciled away.
        assert await session_runtime.reconcile_executions(ended_for=0) == []
        _settled_by_parent(turn)
        # Only once the turn has been over for a while.
        assert await session_runtime.reconcile_executions(ended_for=60) == []
        assert desktop_lifecycle.active_session_ids() == {"child"}
        assert await desktop_lifecycle.reconciled_active_session_ids() == {"child"}

        assert await session_runtime.reconcile_executions(ended_for=0) == ["child"]
        assert desktop_lifecycle.active_session_ids() == set()
        assert not execution.done()  # still unwinding; it just is not running work

        release.set()
        await asyncio.wait_for(execution, 2)
    assert session_runtime._ended_executions == {}
    assert session_runtime._execution_runtimes == {}


async def test_force_end_cancels_a_stuck_execution_and_stops_counting_it():
    release = asyncio.Event()
    cancelled = []

    async def stuck():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append("task")

    task = asyncio.create_task(stuck())
    session_runtime._executions[task] = "child"
    session_runtime._execution_runtimes[task] = SimpleNamespace(
        session_id="child", turn_id="t", _cancel_pump=lambda: cancelled.append("pump"))
    try:
        assert await session_runtime.force_end_executions({"child", "other"}, grace=0.05) == ["child"]
        assert cancelled == ["pump", "task"]
        assert session_runtime.active_executions() == {}
        assert await session_runtime.force_end_executions({"child"}, grace=0.05) == []
    finally:
        release.set()
        await task

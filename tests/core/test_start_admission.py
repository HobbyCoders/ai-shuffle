"""New starts use measured DB pressure, not a session-count ceiling."""

import asyncio
import time
from collections.abc import AsyncIterator
from itertools import pairwise
from unittest.mock import patch

import pytest

from app.core import session_runtime, start_admission
from app.db import base
from app.db import turns as turns_db
from tests.core.helpers import make_pump
from tests.core.test_session_runtime import _runtime_env
from tests.db.test_base_pool_reclaim import retaining_pool  # noqa: F401


@pytest.mark.asyncio
async def test_producing_turn_waits_before_lease_and_recovers(
    mock_db: object, retaining_pool: tuple, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    # Actual psycopg2 pool bookkeeping produces the snapshot; only sockets and
    # the provider pump are doubles. Runtime writes use the isolated SQLite shim.
    pool, _ = retaining_pool
    pool._reserved = 2
    # Shim connections are not borrowers of the separate measured psycopg2 pool.
    monkeypatch.setattr(base, "_close_connection", lambda conn: None)
    holders = [pool.getconn() for _ in range(6)]
    gate = start_admission.StartAdmission()
    monkeypatch.setattr(start_admission, "admission", gate)
    with patch.multiple(
        start_admission.settings, db_start_sustain_seconds=0.0,
        db_start_recovery_seconds=0.0, db_start_interval_seconds=0.02,
        db_start_poll_seconds=0.01,
    ), _runtime_env(pump=make_pump([{"type": "done"}])) as env:
        turn = turns_db.enqueue_turn("pressure-worker", "worker", prompt="hello")
        task = asyncio.create_task(session_runtime.SessionRuntime(turn).execute())
        try:
            for _ in range(100):
                if gate.queued:
                    break
                await asyncio.sleep(0.01)
            assert gate.queued == 1
            assert start_admission.snapshot()["utilization"] == 1
            assert turns_db.get_turn(turn["id"])["state"] == "queued"
            from app.db import runs
            assert runs.get_active_run("pressure-worker") is None
            env["recorder"].start.assert_not_awaited()
            # Reserved lease capacity stays available while new turns wait.
            token = base._lease_checkout.set(True)
            try:
                lease = pool.getconn()
                pool.putconn(lease)
            finally:
                base._lease_checkout.reset(token)
            for conn in holders:
                pool.putconn(conn)
            holders.clear()
            assert await asyncio.wait_for(task, 3) == "completed"
            assert turns_db.get_turn(turn["id"])["state"] == "completed"
        finally:
            for conn in holders:
                pool.putconn(conn)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def quick_gate(monkeypatch: pytest.MonkeyPatch) -> tuple:
    stats = {"utilization": 1.0, "waiting": 1, "oldest": 10.0, "latency": 0.0}
    monkeypatch.setattr(start_admission, "snapshot", lambda: dict(stats))
    for key, value in {
        "sustain_seconds": 0.0, "recovery_seconds": 0.0,
        "interval_seconds": 0.02, "poll_seconds": 0.005,
    }.items():
        monkeypatch.setattr(start_admission.settings, "db_start_" + key, value)
    return start_admission.StartAdmission(), stats


def healthy(stats: dict) -> None:
    stats.update(utilization=0, waiting=0, oldest=0)


@pytest.mark.asyncio
async def test_fifo_recovery_is_paced_and_cancellation_removes_waiter(quick_gate: tuple) -> None:
    gate, stats = quick_gate
    order = []
    starts = []

    async def start(i: int) -> str:
        result = await gate.wait(str(i), str(i), lambda: False)
        if result == "ready":
            order.append(i)
            starts.append(time.monotonic())
        return result

    tasks = [asyncio.create_task(start(i)) for i in range(45)]
    await asyncio.sleep(0.02)
    assert gate.queued == 45
    tasks[1].cancel()
    await asyncio.gather(tasks[1], return_exceptions=True)
    assert gate.queued == 44
    gate.cancel("2")
    healthy(stats)
    await asyncio.wait_for(asyncio.gather(*[t for i, t in enumerate(tasks) if i != 1]), 3)
    assert order == [0] + list(range(3, 45))
    assert min(b - a for a, b in pairwise(starts)) >= 0.019
    assert gate.queued == 0


@pytest.mark.asyncio
async def test_bound_timeout_and_stop_do_not_claim_work(
    quick_gate: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, _ = quick_gate
    monkeypatch.setattr(start_admission.settings, "db_start_max_waiters", 1)
    monkeypatch.setattr(start_admission.settings, "db_start_wait_seconds", 0.03)
    stopped = False
    task = asyncio.create_task(gate.wait("a", "s", lambda: stopped))
    await asyncio.sleep(0.01)
    assert await gate.wait("a", "s", lambda: False) == "cancelled"  # duplicate
    assert await gate.wait("b", "other", lambda: False) == "deferred"  # overflow
    stopped = True
    assert await task == "cancelled"
    assert gate.queued == 0
    assert await gate.wait("b", "other", lambda: False) == "deferred"  # deadline
    assert gate.queued == 0


def test_sustained_pressure_hysteresis_and_recent_latency(
    quick_gate: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, stats = quick_gate
    stats["oldest"] = 0
    monkeypatch.setattr(start_admission.settings, "db_start_sustain_seconds", 2.0)
    monkeypatch.setattr(start_admission.settings, "db_start_recovery_seconds", 3.0)
    assert not gate._pressured(10)
    assert not gate._pressured(11)
    assert gate._pressured(12)
    healthy(stats)
    assert gate._pressured(13)
    stats["utilization"] = 0.7  # below high threshold but not recovery low
    assert gate._pressured(16)
    healthy(stats)
    assert gate._pressured(17)
    assert not gate._pressured(20)
    stats.update(holds=2, latency=4.0)
    assert not gate._pressured(21)
    assert gate._pressured(23)
    assert gate._pressured(24)
    assert not gate._pressured(27)  # no lifetime-max latch


@pytest.mark.asyncio
async def test_running_turn_finishes_while_new_turn_waits(
    mock_db: object, quick_gate: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, stats = quick_gate
    healthy(stats)
    monkeypatch.setattr(start_admission, "admission", gate)
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def pump(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        entered.set()
        await finish.wait()
        yield {"type": "done"}

    with _runtime_env(pump=pump):
        running = turns_db.enqueue_turn("already-running", "worker", prompt="keep working")
        live = asyncio.create_task(session_runtime.SessionRuntime(running).execute())
        waiting = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            stats.update(utilization=1, waiting=1, oldest=10)
            queued = turns_db.enqueue_turn("new-worker", "worker", prompt="wait")
            waiting = asyncio.create_task(session_runtime.SessionRuntime(queued).execute())
            for _ in range(100):
                if gate.queued:
                    break
                await asyncio.sleep(0.005)
            assert gate.queued == 1
            finish.set()
            assert await asyncio.wait_for(live, 2) == "completed"
            assert turns_db.get_turn(queued["id"])["state"] == "queued"
            assert not waiting.done()
            gate.cancel(queued["id"])
            assert await asyncio.wait_for(waiting, 1) is None
        finally:
            for task in (live, waiting):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_queue_chip_removal_cancels_admission(
    mock_db: object, quick_gate: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import turn_manager
    gate, _ = quick_gate
    monkeypatch.setattr(start_admission, "admission", gate)
    turn = turns_db.enqueue_turn("remove-pressure", "user", prompt="hello")
    task = asyncio.create_task(gate.wait(turn["id"], turn["session_id"], lambda: False))
    await asyncio.sleep(0.01)
    assert await asyncio.to_thread(turn_manager.remove_queued, turn["id"])
    assert await task == "cancelled"
    assert gate.queued == 0
    assert turns_db.get_turn(turn["id"])["state"] == "aborted"

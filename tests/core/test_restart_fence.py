"""Rules of the idle-restart admission fence (#1121)."""
import asyncio
import threading

import pytest

from app.core import restart_fence, session_runtime
from app.core.task_registry import task_registry

TOKEN = "a" * 32
OTHER = "b" * 32


@pytest.fixture(autouse=True)
def _idle(monkeypatch):
    monkeypatch.setattr("app.core.desktop_lifecycle.active_session_ids", lambda: set())
    kicked = []
    monkeypatch.setattr("app.core.turn_scheduler.kick", kicked.append)
    yield kicked
    restart_fence._reset_for_tests()


async def test_reserve_refuses_while_an_admission_is_in_flight_and_changes_nothing():
    admission = restart_fence.admit("test start")
    answer = await restart_fence.reserve(TOKEN)
    assert answer["reserved"] is False and answer["admissions"] == 1
    assert not restart_fence.is_closed()
    admission.release()
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True
    assert restart_fence.is_closed()


async def test_reserve_refuses_registered_work(monkeypatch):
    monkeypatch.setattr("app.core.desktop_lifecycle.active_session_ids", lambda: {"s1"})
    answer = await restart_fence.reserve(TOKEN)
    assert answer == {"reserved": False, "active_sessions": 1, "admissions": 0, "queries": 0,
                      "reason": "Finish active sessions before restarting"}
    assert not restart_fence.is_closed()


async def test_reserve_reconciles_before_its_synchronous_check(monkeypatch):
    """A start admitted while reconcile awaits its database read is seen by the
    check that follows; the latch happens in the same loop step as the check."""
    async def reconcile():
        await asyncio.sleep(0)
        restart_fence.admit("arrived during reconcile")
        return []
    monkeypatch.setattr(session_runtime, "reconcile_executions", reconcile)
    assert (await restart_fence.reserve(TOKEN))["reserved"] is False
    assert not restart_fence.is_closed()


async def test_admit_refuses_while_reserved_and_two_reserves_do_not_both_win():
    first, second = await asyncio.gather(restart_fence.reserve(TOKEN), restart_fence.reserve(OTHER))
    assert [first["reserved"], second["reserved"]] == [True, False]
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True  # idempotent retry
    with pytest.raises(restart_fence.AdmissionClosed):
        restart_fence.admit("late start")


async def test_release_requires_the_token_and_kicks_held_turns(_idle):
    await restart_fence.reserve(TOKEN)
    restart_fence.defer_kick("held-session")
    assert restart_fence.release(OTHER) is False and restart_fence.is_closed()
    assert restart_fence.release(TOKEN) is True
    assert not restart_fence.is_closed()
    assert _idle == ["held-session"]
    assert restart_fence.release(TOKEN) is False


async def test_lease_expiry_reopens_and_kicks(monkeypatch, _idle):
    monkeypatch.setattr(restart_fence, "LEASE_SECONDS", 0.05)
    await restart_fence.reserve(TOKEN)
    restart_fence.defer_kick("held-session")
    await asyncio.sleep(0.2)  # the timer, not a reader, reopens it
    assert restart_fence._reservation is None
    assert _idle == ["held-session"]
    assert restart_fence.admit("after expiry").held


async def test_cancelled_thread_admission_stays_busy_until_the_thread_returns():
    started, finish = threading.Event(), threading.Event()

    def commit():
        started.set()
        finish.wait(5)

    admission = restart_fence.admit("thread start")

    async def producer():
        try:
            await admission.to_thread(commit)
        finally:
            admission.release()

    task = asyncio.create_task(producer())
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await restart_fence.reserve(TOKEN))["reserved"] is False, "the thread may still commit"
    finish.set()
    for _ in range(100):
        if not restart_fence.in_flight():
            break
        await asyncio.sleep(0.01)
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True


async def test_hold_until_keeps_an_uncertain_operation_busy():
    gate = asyncio.get_running_loop().create_future()
    admission = restart_fence.admit("remote start")
    admission.hold_until(gate)
    admission.release()
    assert restart_fence.in_flight() == ["remote start"]
    gate.set_result(None)
    await asyncio.sleep(0)
    assert restart_fence.in_flight() == []


async def test_registered_task_rows_count_as_busy(monkeypatch):
    monkeypatch.undo()  # the real registry view
    monkeypatch.setattr("app.core.turn_scheduler.kick", lambda sid: None)
    monkeypatch.setattr(task_registry, "active_session_ids", lambda: ["s-task"])
    assert (await restart_fence.reserve(TOKEN))["reserved"] is False


def test_invalid_token_is_rejected():
    with pytest.raises(ValueError):
        asyncio.run(restart_fence.reserve("short"))


async def test_a_running_headless_query_refuses_and_none_starts_while_reserved():
    from app.core import query_shutdown

    async with query_shutdown.turn():
        answer = await restart_fence.reserve(TOKEN)
        assert answer["reserved"] is False and answer["queries"] == 1
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True
    with pytest.raises(restart_fence.AdmissionClosed):
        async with query_shutdown.turn():
            pass

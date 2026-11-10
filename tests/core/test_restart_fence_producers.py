"""Work producers share the idle-restart admission fence (#1121).

Each test drives the real producer: a start in flight keeps an idle restart's
reservation refused, a start while reserved is refused (retryable) without
creating anything, and a cancelled start stays busy until rolled back.
"""
import asyncio
import os
import sys
import threading
from unittest.mock import AsyncMock

import pytest

from app.core import restart_fence
from app.core.task_registry import task_registry
from app.mcp import builtin_tools as bt

TOKEN = "f" * 32
SESSION = "fence-producer"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell process groups")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    bt._background_tasks.clear()
    task_registry.reset_all()
    monkeypatch.setattr(bt, "_emit_shell_start_frame", AsyncMock())
    monkeypatch.setattr(bt, "_emit_shell_settle_frame", AsyncMock())
    monkeypatch.setattr("app.core.task_persistence.schedule_upsert", lambda *a, **k: None, raising=False)
    yield
    for task in list(bt._background_tasks.values()):
        if task.process is not None and task.process.returncode is None:
            task.process.kill()
    bt._background_tasks.clear()
    task_registry.reset_all()
    restart_fence._reset_for_tests()


def _blocking_ledger(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    pids = []

    def record_process(pid, *args, **kwargs):
        pids.append(pid)
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr("app.core.process_ledger.record_process", record_process)
    return entered, release, pids


async def _until(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a reaped zombie is gone; an unreaped one is not running work
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split()[2] != "Z"
    except OSError:
        return True


async def test_background_shell_is_refused_while_a_restart_is_reserved(monkeypatch):
    spawned = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawned)
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True
    with pytest.raises(restart_fence.AdmissionClosed):
        await bt._spawn_background("sleep 30", None, SESSION)
    spawned.assert_not_called()
    assert task_registry.get_active(SESSION) == []


async def test_background_shell_in_flight_refuses_the_reservation_until_registered(monkeypatch):
    entered, release, _ = _blocking_ledger(monkeypatch)
    spawn = asyncio.create_task(bt._spawn_background("sleep 30", None, SESSION))
    await asyncio.to_thread(entered.wait, 10)
    # The process exists but is not registered yet: only the fence sees it.
    assert task_registry.get_active(SESSION) == []
    assert (await restart_fence.reserve(TOKEN))["reserved"] is False
    release.set()
    task = await spawn
    assert [row["id"] for row in task_registry.get_active(SESSION)] == [task.task_id]
    assert restart_fence.in_flight() == []
    assert (await restart_fence.reserve(TOKEN))["reserved"] is False  # registered work
    task.process.kill()


async def test_cancelled_background_shell_stays_busy_until_its_child_is_ended(monkeypatch):
    entered, release, pids = _blocking_ledger(monkeypatch)
    spawn = asyncio.create_task(bt._spawn_background("sleep 30", None, SESSION))
    await asyncio.to_thread(entered.wait, 10)
    spawn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await spawn
    assert task_registry.get_active(SESSION) == []
    assert (await restart_fence.reserve(TOKEN))["reserved"] is False, "the ledger thread may still commit"
    release.set()
    await _until(lambda: not restart_fence.in_flight())
    assert not _alive(pids[0]), "the unregistered child is rolled back"
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True

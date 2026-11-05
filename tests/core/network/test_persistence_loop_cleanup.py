"""Exercise the workers fixture's real asyncio.run wrapper and persistence writer."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.core import task_persistence
from app.db import background_tasks, database


def _enqueue(task_id):
    task_persistence.enqueue_task_snapshot("caller", task_id, {
        "session_id": "caller", "task_id": task_id, "kind": "agent",
        "status": "running", "title": task_id, "started_at": 1.0,
        "updated_at": 1.0,
    })


def _assert_released():
    assert task_persistence.pending_count() == 0
    assert task_persistence._consumer is None
    assert task_persistence._wakeup is None
    assert task_persistence._drain_lock is None


def test_nested_worker_loop_leaves_foreign_writer_untouched(workers):
    owner, target = workers("owner"), workers("target")

    async def nested():
        assert asyncio.get_running_loop() is not consumer.get_loop()
        return "report delivered"

    def report_on_target():
        with target.active():
            return asyncio.run(nested())

    async def scenario():
        nonlocal consumer
        _enqueue("written")
        await task_persistence.flush()
        await asyncio.sleep(0)  # let the real consumer return to its event wait
        consumer = task_persistence._consumer
        assert consumer.get_loop() is asyncio.get_running_loop()
        wakeup, lock = task_persistence._wakeup, task_persistence._drain_lock
        assert not lock.locked()
        _enqueue("queued")
        pending = dict(task_persistence._pending)

        # Like remote tracking's run_as(), block the owner while the target's
        # separate thread/loop runs; the owner's consumer must not be touched.
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(report_on_target).result(timeout=5) == "report delivered"

        assert task_persistence._consumer is consumer
        assert not consumer.done() and consumer.cancelling() == 0
        assert task_persistence._wakeup is wakeup
        assert task_persistence._drain_lock is lock
        assert task_persistence._pending == pending
        assert background_tasks.get_background_task("queued", "caller") is None

    consumer = None
    with owner.active():
        database.create_session(session_id="caller", profile_id="default", title="Owner")
        asyncio.run(scenario())
        for task_id in ("written", "queued"):
            assert background_tasks.get_background_task(task_id, "caller")["status"] == "running"
    with target.active():
        assert background_tasks.get_background_task("queued", "caller") is None
    assert consumer.done() and consumer.cancelled()
    _assert_released()


@pytest.mark.parametrize("writer_state", ["unstarted", "queued", "inflight"])
def test_owner_loop_drains_and_releases_writer(workers, monkeypatch, writer_state):
    owner = workers("owner")
    entered, release = Event(), Event()
    write_cancellations = []
    real_upsert = background_tasks.upsert_background_tasks

    def controlled_upsert(snapshots):
        # Controlled IO scheduling only: the real consumer/flush/lock/reset and
        # actual SQLite-shim commit remain in use, including the popped batch.
        inflight = any(row["task_id"] == "inflight" for row in snapshots)
        if inflight:
            entered.set()
            assert release.wait(5), "owner cleanup did not release the in-flight write"
        real_upsert(snapshots)
        if inflight:
            write_cancellations.append(consumer.cancelling())

    monkeypatch.setattr(background_tasks, "upsert_background_tasks", controlled_upsert)

    async def scenario():
        nonlocal consumer
        if writer_state == "unstarted":
            assert task_persistence._consumer is None
            assert task_persistence.pending_count() == 1
            return
        _enqueue("inflight" if writer_state == "inflight" else "queued")
        consumer = task_persistence._consumer
        assert consumer.get_loop() is asyncio.get_running_loop()
        if writer_state == "inflight":
            assert await asyncio.to_thread(entered.wait, 5), "consumer did not start writing"
            assert task_persistence.pending_count() == 0  # batch already popped
            assert task_persistence._drain_lock.locked()
            _enqueue("queued")
            # Release only after this coroutine returns to the fixture's finally.
            asyncio.get_running_loop().call_soon(release.set)

    consumer = None
    with owner.active():
        database.create_session(session_id="caller", profile_id="default", title="Owner")
        if writer_state == "unstarted":
            _enqueue("queued")  # no running loop: flush must still commit this
        try:
            asyncio.run(scenario())
        finally:
            release.set()
        ids = ("inflight", "queued") if writer_state == "inflight" else ("queued",)
        for task_id in ids:
            assert background_tasks.get_background_task(task_id, "caller")["status"] == "running"
    if consumer is not None:
        assert consumer.done() and consumer.cancelled()
    if writer_state == "inflight":
        assert write_cancellations == [0]  # reset/cancel happened AFTER the commit
    _assert_released()

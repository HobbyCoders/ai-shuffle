"""Edge cases for spawn_detached: retention, failures, cancellation, no loop.

There is no cancel-on-shutdown hook for detached tasks; nothing to test there.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import warnings

from app.core import detached_tasks
from app.core.detached_tasks import spawn_detached


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0)


async def test_many_tasks_all_retained_until_done():
    gate = asyncio.Event()
    done = []

    async def _work(i):
        await gate.wait()
        done.append(i)

    for i in range(50):
        spawn_detached(_work(i))  # the returned task is dropped on purpose
    gc.collect()
    await _settle()
    assert len(detached_tasks._DETACHED_TASKS) >= 50
    gate.set()
    await _settle()
    assert sorted(done) == list(range(50))
    assert not any(t.get_coro().__name__ == "_work" for t in detached_tasks._DETACHED_TASKS)


async def test_exception_is_logged_while_the_caller_still_holds_the_task(caplog):
    # Before the fix the failure surfaced only when the task object was
    # garbage-collected ("Task exception was never retrieved"), so a caller
    # holding the returned task silenced it.
    async def _boom():
        raise ValueError("detached boom")

    caplog.set_level(logging.ERROR)
    task = spawn_detached(_boom(), name="boom-task")
    await _settle()
    assert task.done()
    assert task not in detached_tasks._DETACHED_TASKS
    failures = [r for r in caplog.records if r.exc_info and "detached boom" in str(r.exc_info[1])]
    assert len(failures) == 1
    assert "boom-task" in failures[0].getMessage()


async def test_exception_is_not_logged_twice_after_gc(caplog):
    async def _boom():
        raise RuntimeError("once only")

    caplog.set_level(logging.ERROR)
    spawn_detached(_boom())
    await _settle()
    gc.collect()
    await _settle()
    hits = [r for r in caplog.records if "once only" in (r.getMessage() + str(r.exc_info and r.exc_info[1]))]
    assert len(hits) == 1


async def test_success_and_cancellation_are_not_logged(caplog):
    async def _ok():
        return 1

    async def _forever():
        await asyncio.Event().wait()

    caplog.set_level(logging.DEBUG, logger=detached_tasks.__name__)
    ok = spawn_detached(_ok())
    slow = spawn_detached(_forever())
    await _settle()
    slow.cancel()
    await _settle()
    assert ok.result() == 1 and slow.cancelled()
    assert slow not in detached_tasks._DETACHED_TASKS
    assert [r for r in caplog.records if r.name == detached_tasks.__name__ and r.levelno >= logging.WARNING] == []


def test_no_running_loop_returns_none_and_retains_nothing():
    async def _work():
        return None

    before = set(detached_tasks._DETACHED_TASKS)
    coro = _work()
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert spawn_detached(coro, name="x") is None
    assert coro.cr_frame is None
    assert set(detached_tasks._DETACHED_TASKS) == before


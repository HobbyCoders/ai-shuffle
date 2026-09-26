"""spawn_detached keeps fire-and-forget tasks referenced until they finish."""

import asyncio
import gc
import warnings

from app.core import detached_tasks
from app.core.detached_tasks import spawn_detached


async def test_task_held_until_done_then_released():
    gate = asyncio.Event()
    ran = []

    async def _work():
        await gate.wait()
        ran.append(True)

    task = spawn_detached(_work(), name="detached-test")
    assert task is not None
    assert task.get_name() == "detached-test"
    assert task in detached_tasks._DETACHED_TASKS
    gc.collect()
    await asyncio.sleep(0)
    assert task in detached_tasks._DETACHED_TASKS
    gate.set()
    await task
    await asyncio.sleep(0)
    assert ran == [True]
    assert task not in detached_tasks._DETACHED_TASKS


def test_no_running_loop_closes_coroutine():
    async def _work():
        return None

    coro = _work()
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert spawn_detached(coro) is None
    assert coro.cr_frame is None  # closed, never scheduled

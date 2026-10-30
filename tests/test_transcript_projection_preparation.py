from __future__ import annotations

import asyncio
import threading

import pytest

from app.core.transcript_views.preparation import PreparationBusy, PreparationWorker


@pytest.mark.asyncio
async def test_cpu_work_is_off_loop_and_overlap_is_rejected_before_submission():
    worker = PreparationWorker()
    entered, release = threading.Event(), threading.Event()
    owner = threading.get_ident()
    def work():
        entered.set()
        assert release.wait(2)
        return threading.get_ident()
    task = asyncio.create_task(worker.run(work))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(PreparationBusy):
            await worker.run(lambda: None)
        release.set()
        assert await task != owner
        assert await worker.run(lambda: 'next') == 'next'
    finally:
        release.set()
        await task
        await worker.close()


@pytest.mark.asyncio
async def test_cancellation_joins_owned_work_before_restart():
    worker = PreparationWorker()
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        assert release.wait(2)
    task = asyncio.create_task(worker.run(work))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # A second shutdown request must not cancel the wrapper.
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(PreparationBusy):
            await worker.run(lambda: None)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await worker.run(lambda: 'resumed') == 'resumed'
    finally:
        release.set()
        await worker.close()


@pytest.mark.asyncio
async def test_close_does_not_repeat_an_observed_preparation_failure():
    worker = PreparationWorker()
    def fail():
        raise ValueError('fixture')
    with pytest.raises(ValueError, match='fixture'):
        await worker.run(fail)
    await worker.close()

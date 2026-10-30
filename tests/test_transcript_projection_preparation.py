from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from app.core.transcript_views.preparation import (
    PreparationBusy,
    PreparationUnavailable,
    PreparationWorker,
)


def blocked(entered, release, worker_failure=False):
    marker = Path(entered).with_suffix('.tmp')
    marker.write_text(str(os.getpid()))
    marker.replace(entered)  # Publish completion, never an observable empty file.
    deadline = time.monotonic() + 10
    while not Path(release).exists():
        if time.monotonic() > deadline:
            raise AssertionError('fixture release not received')
        time.sleep(.005)
    if worker_failure:
        raise ValueError('fixture worker failed after cancellation')
    return os.getpid()


async def entered_worker(path):
    async def wait():
        while not path.exists():
            await asyncio.sleep(.005)
        return int(path.read_text())
    return await asyncio.wait_for(wait(), 10)


@pytest.mark.asyncio
async def test_cpu_work_is_off_process_and_overlap_is_rejected_before_submission(tmp_path):
    worker = PreparationWorker()
    entered, release = tmp_path/'entered', tmp_path/'release'
    task = asyncio.create_task(worker.run(blocked, entered, release))
    try:
        pid = await entered_worker(entered)
        assert pid != os.getpid()
        with pytest.raises(PreparationBusy):
            await worker.run(os.getpid)
        release.touch()
        assert await task == pid
        assert await worker.run(os.getpid) == pid  # bounded worker is reused
    finally:
        release.touch()
        await task
        await worker.close()
    assert pid not in {process.pid for process in multiprocessing.active_children()}


@pytest.mark.asyncio
@pytest.mark.parametrize('worker_failure', [False, True])
async def test_cancellation_joins_owned_work_before_restart(tmp_path, worker_failure):
    worker = PreparationWorker()
    entered, release = tmp_path/'entered', tmp_path/'release'
    task = asyncio.create_task(worker.run(blocked, entered, release, worker_failure))
    try:
        pid = await entered_worker(entered)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(PreparationBusy):
            await worker.run(os.getpid)
        release.touch()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await worker.run(os.getpid) == pid
    finally:
        release.touch()
        await asyncio.gather(task, return_exceptions=True)
        await worker.close()
    assert pid not in {process.pid for process in multiprocessing.active_children()}


def fail():
    raise ValueError('fixture')


@pytest.mark.asyncio
async def test_close_does_not_repeat_an_observed_preparation_failure():
    worker = PreparationWorker()
    with pytest.raises(ValueError, match='fixture'):
        await worker.run(fail)
    await worker.close()


@pytest.mark.asyncio
async def test_concurrent_cancelled_closes_join_child_exit(tmp_path):
    worker = PreparationWorker()
    entered, release = tmp_path/'entered', tmp_path/'release'
    task = asyncio.create_task(worker.run(blocked, entered, release))
    first = second = None
    try:
        pid = await entered_worker(entered)
        first, second = (asyncio.create_task(worker.close()) for _ in range(2))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        release.touch()
        await task
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert pid not in {process.pid for process in multiprocessing.active_children()}
    finally:
        release.touch()
        await asyncio.gather(*(t for t in (task, first, second) if t is not None), return_exceptions=True)
        await worker.close()


@pytest.mark.asyncio
async def test_crashed_process_can_be_replaced_without_inheriting_requests():
    worker = PreparationWorker()
    try:
        pid = await worker.run(os.getpid)
        with pytest.raises(PreparationUnavailable, match='preparation_unavailable'):
            await worker.run(os._exit, 1)
        replacement = await worker.run(os.getpid)
        assert replacement not in (pid, os.getpid())
    finally:
        await worker.close()


def replay_flags():
    from app.core.transcript_views.preprocess import replay_flags as flags
    return flags()


@pytest.mark.asyncio
async def test_reused_process_sees_current_canonical_replay_switches(monkeypatch):
    worker = PreparationWorker()
    try:
        for value, expected in [('0', False), ('1', True), ('0', False)]:
            monkeypatch.setenv('AI_SHUFFLE_PERF_COALESCE_SUBAGENT_PROGRESS', value)
            monkeypatch.setenv('AI_SHUFFLE_PERF_SLIM_USAGE_REPLAY', value)
            assert await worker.run(replay_flags) == {'progress': expected, 'usage': expected}
    finally:
        await worker.close()

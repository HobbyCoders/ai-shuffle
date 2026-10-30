"""One bounded, cancellation-joined CPU worker for the dark producer.

Admission happens before executor submission. No request or shutdown can leave
an unbounded queue of preprocessing or JSON jobs behind the serving loop.
"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import partial
from typing import TypeVar

T = TypeVar("T")


class PreparationBusy(RuntimeError):
    pass


class PreparationUnavailable(RuntimeError):
    pass


def _invoke(call, args, kwargs, environment):
    # The canonical preprocessor reads these live switches. A spawned worker
    # must see the current values, not just those inherited at first startup.
    for name, value in environment.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    return call(*args, **kwargs)


async def join_worker(future: asyncio.Future):
    """Defer every cancellation until owned executor work has actually finished."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # The worker has finished; propagate its result below, unless an
            # earlier cancellation must remain the caller's terminal outcome.
            break
    if cancelled:
        if not future.cancelled():
            future.exception()  # Consume a worker failure without losing cancellation.
        raise asyncio.CancelledError
    return future.result()


class PreparationWorker:
    def __init__(self) -> None:
        self._executor: ProcessPoolExecutor | None = None
        self._future: asyncio.Future | None = None
        self._closing: asyncio.Task | None = None

    async def run(self, call: Callable[..., T], *args, **kwargs) -> T:
        # One process and one admitted job, never an executor submission queue.
        # Spawn on every platform: forking the multithreaded server would inherit
        # DB sockets and locks. Calls/arguments are private, server-owned values.
        if self._closing is not None or (self._future is not None and not self._future.done()):
            raise PreparationBusy("preparation_busy")
        from app.api import events
        environment = {name: os.environ.get(name) for name in
                       (events._COALESCE_PROGRESS_ENV, events._SLIM_USAGE_ENV)}
        try:
            if self._executor is None:
                self._executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
            future = asyncio.get_running_loop().run_in_executor(
                self._executor, partial(_invoke, call, args, kwargs, environment))
            self._future = future
            return await join_worker(future)
        except BrokenProcessPool as error:
            executor, self._executor = self._executor, None
            if executor is not None:
                await join_worker(asyncio.create_task(asyncio.to_thread(executor.shutdown, wait=True)))
            raise PreparationUnavailable("preparation_unavailable") from error

    async def _shutdown(self) -> None:
        if self._future is not None and not self._future.done():
            # Wait for completion without re-raising the run owner's failure.
            # This private shutdown task is itself cancellation-shielded.
            await asyncio.wait((self._future,))
        executor, self._executor = self._executor, None
        self._future = None
        if executor is not None:
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)

    async def close(self) -> None:
        # Concurrent/repeatedly cancelled stop calls join ONE shutdown task.
        # Returning means the owned child has exited, not merely been signalled.
        if self._closing is None:
            self._closing = asyncio.create_task(self._shutdown())
        await join_worker(self._closing)

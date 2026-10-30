"""One bounded, cancellation-joined CPU worker for the dark producer.

Admission happens before executor submission. No request or shutdown can leave
an unbounded queue of preprocessing or JSON jobs behind the serving loop.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, TypeVar

T = TypeVar("T")


class PreparationBusy(RuntimeError):
    pass


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
        self._executor: ThreadPoolExecutor | None = None
        self._future: asyncio.Future | None = None

    async def run(self, call: Callable[..., T], *args, **kwargs) -> T:
        # The single actor normally submits sequentially; reject accidental
        # overlap instead of growing the executor's hidden submission queue.
        if self._future is not None and not self._future.done():
            raise PreparationBusy("preparation_busy")
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcript-preparation")
        future = asyncio.get_running_loop().run_in_executor(self._executor, partial(call, *args, **kwargs))
        self._future = future
        # Shield every join, including repeated cancellation during shutdown:
        # cancelling an asyncio wrapper cannot stop the underlying CPU job.
        return await join_worker(future)

    async def close(self) -> None:
        try:
            if self._future is not None and not self._future.done():
                await join_worker(self._future)
        finally:
            executor, self._executor = self._executor, None
            self._future = None
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

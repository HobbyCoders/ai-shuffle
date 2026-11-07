"""
Run synchronous database operations off the asyncio event loop.

All database functions in ``app.db`` are synchronous ``psycopg2`` calls. Called
directly from ``async def`` code they block the event loop, so async callers
route them through :func:`run_sync`, which runs them in asyncio's default
thread pool. Each call acquires its own pooled connection via ``get_db()``, so
no connection is shared across threads.
"""

import asyncio
import contextvars
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class TurnWork:
    """Actual off-loop futures issued by one admission generation.

    Shielding the *future*, rather than only its cancelled caller, keeps the
    real executor work observable until its thread has returned. The release
    owner drains this set even if phase detection or caller cleanup failed.
    ContextVars propagate the exact tracker into child tasks and threads.
    """

    def __init__(self) -> None:
        self.pending: set[asyncio.Future] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def add(self, future: asyncio.Future) -> None:
        # A sync dispatch can call asyncio.run(), inheriting this ContextVar
        # into a second loop. Its outer dispatch is already tracked here;
        # asyncio.run joins that loop's default executor before returning.
        # Never mix foreign-loop futures into the release owner's gather.
        if future.get_loop() is not self.loop or future in self.pending:
            return
        self.pending.add(future)
        future.add_done_callback(self._settled)

    def _settled(self, future: asyncio.Future) -> None:
        self.pending.discard(future)
        if not future.cancelled():
            future.exception()  # retrieve errors even if the caller has left

    def reap_done(self) -> None:
        # Done callbacks run on the next loop step, not inline. Python 3.12's
        # all-done gather/shield completes eagerly and cannot yield to them.
        for future in tuple(self.pending):
            if future.done():
                self._settled(future)

    async def drain(self) -> None:
        while self.pending:
            self.reap_done()
            if self.pending:
                await asyncio.shield(asyncio.gather(*tuple(self.pending), return_exceptions=True))


_turn_work: contextvars.ContextVar[TurnWork | None] = contextvars.ContextVar("turn_work", default=None)


def _track_executor_dispatch(loop: asyncio.AbstractEventLoop) -> None:
    """Capture issued executor jobs, including raw asyncio.to_thread callers.

    Install once per admission loop. Outside a turn this delegates unchanged;
    inside one, shield the underlying executor future *before* returning it to
    any cancellable coroutine. Registering only selected DB helpers missed
    permission bridges and would miss the next adapter/tool helper as well.
    """
    dispatch = loop.run_in_executor
    if getattr(dispatch, "_tracks_turn_work", False):
        return

    @functools.wraps(dispatch)
    def tracked(executor, func, *args):
        future = dispatch(executor, func, *args)
        work = _turn_work.get()
        if work is None or work.loop is not loop:
            return future
        work.add(future)
        return asyncio.shield(future)

    tracked._tracks_turn_work = True
    loop.run_in_executor = tracked


def bind_turn_work(work: TurnWork | None) -> None:
    if work is not None:
        loop = asyncio.get_running_loop()
        if work.loop is None:
            work.loop = loop
        if work.loop is loop:
            _track_executor_dispatch(loop)
    _turn_work.set(work)


def unbind_turn_work(work: TurnWork) -> None:
    if _turn_work.get() is work:
        _turn_work.set(None)


def keep_in_turn(future: asyncio.Future) -> None:
    """Include a detached app-write producer in the current turn's release."""
    work = _turn_work.get()
    if work is not None:
        work.add(future)


async def _await_thread(future: asyncio.Future) -> Any:
    work = _turn_work.get()
    if work is None:
        return await future
    if future.get_loop() is not work.loop:
        # Foreign-loop children belong to the tracked outer sync dispatch.
        # asyncio.run joins its default pool, but not our custom pools: keep
        # that outer dispatch alive until even a cancelled custom write returns.
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
        result = future.result()
        if cancelled:
            raise asyncio.CancelledError
        return result
    work.add(future)
    return await asyncio.shield(future)

# Ordinary DB / filesystem work can occupy every default-executor thread.
# Lease admission alone is not enough if its call never reaches the pool.
# More workers than reserved slots: two lock waiters must not monopolize all
# heartbeat dispatch. Pool admission still caps the total connection budget;
# transaction-local SQL timeouts bound lock waits and callers bound queue time.
_lease_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db-lease")
# Event backpressure must not occupy every ordinary DB/filesystem executor
# thread. The synchronous gate also bounds event callers outside this executor.
_event_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db-event")


async def run_event_write(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run an event transaction off-loop without starving ordinary DB dispatch."""
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await _await_thread(asyncio.get_running_loop().run_in_executor(_event_executor, call))


async def run_lease(fn: Callable[..., T], *args: Any) -> T:
    """Heartbeats only: never queue bulk/reaper work ahead of lease renewal."""
    call = functools.partial(contextvars.copy_context().run, fn, *args)
    return await _await_thread(asyncio.get_running_loop().run_in_executor(_lease_executor, call))


async def run_sync(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous database function in a thread without blocking the event loop.

    Usage::

        from app.db.async_database import run_sync
        from app.db import database

        session = await run_sync(database.get_session, session_id)
    """
    if _turn_work.get() is None:
        return await asyncio.to_thread(fn, *args, **kwargs)
    # Keep the actual task alive when its awaiting coroutine is cancelled.
    return await _await_thread(asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs)))


async def run_maintenance(function, *args, **kwargs):
    """Cancellation signals the off-loop worker before it can borrow again."""
    import threading

    stop = threading.Event()
    try:
        return await run_sync(function, *args, stop=stop, **kwargs)
    finally:
        stop.set()

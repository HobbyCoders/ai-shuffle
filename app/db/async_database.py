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

# Ordinary DB / filesystem work can occupy every default-executor thread.
# Lease admission alone is not enough if its call never reaches the pool.
# More workers than reserved slots: two lock waiters must not monopolize all
# heartbeat dispatch. Pool admission still caps the total connection budget;
# transaction-local SQL timeouts bound lock waits and callers bound queue time.
_lease_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db-lease")


async def run_lease(fn: Callable[..., T], *args: Any) -> T:
    """Heartbeats only: never queue bulk/reaper work ahead of lease renewal."""
    call = functools.partial(contextvars.copy_context().run, fn, *args)
    return await asyncio.get_running_loop().run_in_executor(_lease_executor, call)


async def run_sync(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous database function in a thread without blocking the event loop.

    Usage::

        from app.db.async_database import run_sync
        from app.db import database

        session = await run_sync(database.get_session, session_id)
    """
    return await asyncio.to_thread(fn, *args, **kwargs)

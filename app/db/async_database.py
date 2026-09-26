"""
Run synchronous database operations off the asyncio event loop.

All database functions in ``app.db`` are synchronous ``psycopg2`` calls. Called
directly from ``async def`` code they block the event loop, so async callers
route them through :func:`run_sync`, which runs them in asyncio's default
thread pool. Each call acquires its own pooled connection via ``get_db()``, so
no connection is shared across threads.
"""

import asyncio
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def run_sync(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous database function in a thread without blocking the event loop.

    Usage::

        from app.db.async_database import run_sync
        from app.db import database

        session = await run_sync(database.get_session, session_id)
    """
    return await asyncio.to_thread(fn, *args, **kwargs)

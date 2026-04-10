"""
Async wrappers for all synchronous database operations.

Problem:
    All database functions in ``app.db.database`` are synchronous, using
    ``psycopg2`` directly.  When called from ``async def`` route handlers
    (FastAPI, WebSocket handlers, etc.) they **block the asyncio event loop**,
    stalling all concurrent connections.

Solution:
    This module dynamically wraps every public callable in ``app.db.database``
    with ``asyncio.to_thread()``, offloading the blocking I/O to a thread-pool
    worker.  The result is a drop-in async version of the entire database API.

Usage::

    # Before (blocks event loop):
    from app.db import database
    session = database.get_session(session_id)

    # After (non-blocking):
    from app.db import async_database as adb
    session = await adb.get_session(session_id)

Incremental migration:
    Callers can migrate one at a time — the synchronous ``database`` module
    still works exactly as before for non-critical paths.  Critical paths
    (WebSocket handlers, streaming, query engine) should migrate to this
    async version.

Thread safety:
    Each ``to_thread`` call runs in a separate thread from Python's default
    ``ThreadPoolExecutor``.  Since ``get_db()`` acquires a connection from the
    PostgreSQL pool per call, there is no connection sharing across threads.
"""

import asyncio
import inspect
import logging
from functools import wraps
from typing import Any, Callable, Coroutine, TypeVar

from app.db import database

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _make_async(fn: Callable[..., T]) -> Callable[..., Coroutine[Any, Any, T]]:
    """Wrap a synchronous function to run in asyncio's default thread pool."""

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(fn, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Convenience helper for one-off calls (when you don't want to import this module)
# ---------------------------------------------------------------------------

async def run_sync(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous database function in a thread without blocking the event loop.

    Usage::

        from app.db.async_database import run_sync
        from app.db import database

        session = await run_sync(database.get_session, session_id)

    This is useful when you need to call just one or two DB functions without
    importing the entire async_database module.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Dynamically generate async wrappers for ALL public database functions
# ---------------------------------------------------------------------------

# These names are not database operations — skip them.
_SKIP_NAMES = frozenset({
    "get_connection",  # Returns a raw connection — callers should use get_db()
    "get_db",          # Context manager — not suitable for to_thread wrapping
    "init_database",   # Startup-only, runs before event loop
    "WorktreeStatus",  # Class constant, not a function
})

_wrapped_count = 0

for _name in dir(database):
    if _name.startswith("_"):
        continue
    if _name in _SKIP_NAMES:
        continue

    _obj = getattr(database, _name)

    # Only wrap actual callables (functions), not classes, constants, or modules
    if not callable(_obj) or inspect.isclass(_obj) or inspect.ismodule(_obj):
        continue

    globals()[_name] = _make_async(_obj)
    _wrapped_count += 1

logger.debug(f"async_database: wrapped {_wrapped_count} functions from database module")

# Re-export non-callable attributes for convenience (WorktreeStatus, etc.)
WorktreeStatus = database.WorktreeStatus

# Clean up module namespace
del _name, _obj, _wrapped_count

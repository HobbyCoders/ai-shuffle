"""Fire-and-forget asyncio tasks that are kept alive until they finish.

The event loop holds only a weak reference to a task, so a bare
``asyncio.create_task(coro)`` whose result is dropped can be garbage-collected
before it completes. :func:`spawn_detached` keeps a strong reference in a module
set and drops it from a done-callback, the same pattern
``app.core.task_dispatch`` uses for background workers.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Optional, Set

#: Detached tasks still running; each removes itself when done.
_DETACHED_TASKS: Set["asyncio.Task[Any]"] = set()


def spawn_detached(
    coro: Coroutine[Any, Any, Any], *, name: Optional[str] = None
) -> Optional["asyncio.Task[Any]"]:
    """Schedule ``coro`` on the running loop and hold it until it completes.

    Returns the task, or ``None`` (after closing ``coro`` so it is not reported
    as never awaited) when no event loop is running.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return None
    task = loop.create_task(coro, name=name)
    _DETACHED_TASKS.add(task)
    task.add_done_callback(_DETACHED_TASKS.discard)
    return task

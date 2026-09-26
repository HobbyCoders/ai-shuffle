"""Fire-and-forget asyncio tasks that are kept alive until they finish.

The event loop holds only a weak reference to a task, so a bare
``asyncio.create_task(coro)`` whose result is dropped can be garbage-collected
before it completes. :func:`spawn_detached` keeps a strong reference in a module
set and drops it from a done-callback, the same pattern
``app.core.task_dispatch`` uses for background workers. The same callback logs
a task's exception: nobody awaits a detached task, and asyncio would report it
only whenever the task object happened to be garbage-collected.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Optional, Set

logger = logging.getLogger(__name__)

#: Detached tasks still running; each removes itself when done.
_DETACHED_TASKS: Set["asyncio.Task[Any]"] = set()


def _on_done(task: "asyncio.Task[Any]") -> None:
    _DETACHED_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()  # also marks it retrieved: logged once, here
    if exc is not None:
        logger.error("Detached task %s failed", task.get_name(), exc_info=exc)


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
    task.add_done_callback(_on_done)
    return task

"""Retire request-owned engine reads before HTTP/lifespan teardown.

The default ten-second stop budget allows 6.5s for finite requests, 1.5s for
query settlement and two seconds for lifespan (idle measurement: 0.282s). A
Python supervisor can supply its larger budget. These are shared deadlines,
not a fresh timeout for every query. Only raw engine reads are cancelled here.
Slow app writes/disconnect can still exceed the reserve: see issue #1305.
This is not worst-case cancellation-safe settlement.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import math
import os

GRACE_SECONDS = 6.5
SETTLE_SECONDS = 1.5
CLEANUP_RESERVE_SECONDS = 2.0
logger = logging.getLogger(__name__)


class QueryShutdown(asyncio.CancelledError):
    """Only the raw engine read ended; the turn must still save its outcome."""


@dataclass(eq=False)
class _Turn:
    owner: asyncio.Task
    interrupt: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)


_turns: set[_Turn] = set()
_current: ContextVar[_Turn | None] = ContextVar("request_query_shutdown", default=None)
_closing_loop: asyncio.AbstractEventLoop | None = None
_retired: set[asyncio.Task] = set()


@asynccontextmanager
async def turn():
    if _closing_loop is asyncio.get_running_loop():
        raise RuntimeError("Worker is shutting down; query was not started")
    work = _Turn(asyncio.current_task())
    _turns.add(work)
    token = _current.set(work)
    try:
        yield
    finally:
        _current.reset(token)
        _turns.discard(work)
        work.finished.set()


async def track_stream(stream):
    async with turn():
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()


def _reaped(task):
    _retired.discard(task)
    if not task.cancelled():
        task.exception()


async def engine_event(events, on_shutdown=None):
    """Race only raw engine reads, never adapter-side app-session mutations."""
    work = _current.get()
    if work is None:
        return await anext(events)
    reading = asyncio.create_task(anext(events))
    stopping = asyncio.create_task(work.interrupt.wait())
    try:
        await asyncio.wait({reading, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if reading.done():
            return reading.result()
        if on_shutdown is not None:
            on_shutdown()  # Fence delayed engine/store writes before cancelling.
        reading.cancel()
        # A cancellation-resistant provider must not hold the app's settlement.
        _retired.add(reading)
        reading.add_done_callback(_reaped)
        raise QueryShutdown("Worker is shutting down")
    finally:
        stopping.cancel()
        if not reading.done() and not work.interrupt.is_set():
            reading.cancel()
            _retired.add(reading)
            reading.add_done_callback(_reaped)


def configured_grace_seconds(environ=None):
    """Infrastructure metadata from the supervisor; never extend an unknown budget."""
    environ = os.environ if environ is None else environ
    try:
        budget = float(environ.get("SHUFFLE_SUPERVISOR_STOP_SECONDS", "10"))
    except (TypeError, ValueError):
        return GRACE_SECONDS
    if not math.isfinite(budget) or not 10 <= budget <= 30:
        return GRACE_SECONDS
    return budget - SETTLE_SECONDS - CLEANUP_RESERVE_SECONDS


async def settle(*, grace=None, http_tasks=()):
    """Close admission, drain finite requests, then retire remaining engine reads."""
    if grace is None:
        grace = configured_grace_seconds()
    global _closing_loop
    _closing_loop = asyncio.get_running_loop()
    watched = set(http_tasks) | {work.owner for work in _turns}
    watched.discard(asyncio.current_task())
    if watched:
        await asyncio.wait(watched, timeout=grace)
    turns = list(_turns)
    for work in turns:
        work.interrupt.set()
    if turns:
        waits = {asyncio.create_task(work.finished.wait()) for work in turns}
        _, pending = await asyncio.wait(waits, timeout=SETTLE_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            logger.error("%d query settlements exceeded the shutdown reserve", len(pending))
            return False
    return True

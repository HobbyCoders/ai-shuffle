"""Retire request-owned engine reads before HTTP/lifespan teardown.

The default ten-second stop budget allows 6.5s for finite requests, 1.5s for
query settlement and two seconds for lifespan (idle measurement: 0.282s). A
Python supervisor can supply its larger budget; the settlement share scales
with it, retaining two seconds for lifespan. These are shared deadlines, not a
fresh timeout for every query. Only raw engine reads are cancelled here.
The absolute drain deadline is set once when stopping. Retired non-streaming
finalization outlives its HTTP reader, while durable receipts cover process death
or writes that cannot finish before that deadline. See issue #1305 for legacy
memory-mirror publication and lifecycle work beyond the supervisor budget.
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
_owned: set[asyncio.Task] = set()
_stop_deadline: float | None = None
_retire_at: float | None = None
_settle_at: float | None = None


async def run(factory):
    """Retired saving belongs to the query, not to its disconnected HTTP reader."""
    work = None

    async def invoke():
        nonlocal work
        async with turn() as work:
            return await factory()

    task = asyncio.create_task(invoke())
    _owned.add(task)

    def done(completed):
        _owned.discard(completed)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(done)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Before retirement a disconnect still stops provider execution. After
        # retirement, another cancel must not cut an issued write off from its
        # terminal/memory continuation. Process death is covered by the receipt.
        if work is None or not work.interrupt.is_set():
            from app.core.provider.session_admission import finish_saving

            task.cancel()
            try:
                # Preserve ordinary cancellation's admission/teardown contract.
                # Only a retired turn may deliberately outlive its HTTP owner.
                await finish_saving(task)
            except asyncio.CancelledError:
                pass
        raise


@asynccontextmanager
async def turn():
    if _closing_loop is asyncio.get_running_loop():
        raise RuntimeError("Worker is shutting down; query was not started")
    from app.core import restart_fence

    if restart_fence.is_closed():  # an idle update restart is being prepared (#1121)
        raise restart_fence.AdmissionClosed()
    work = _Turn(asyncio.current_task())
    _turns.add(work)
    token = _current.set(work)
    try:
        yield work
    finally:
        _current.reset(token)
        _turns.discard(work)
        work.finished.set()


def active_count() -> int:
    """Headless queries running now; they are in no session registry (#1121)."""
    return len(_turns)


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


def configured_stop_budget(environ=None):
    """Infrastructure metadata from the supervisor; never extend an unknown budget."""
    environ = os.environ if environ is None else environ
    try:
        budget = float(environ.get("SHUFFLE_SUPERVISOR_STOP_SECONDS", "10"))
    except (TypeError, ValueError):
        return 10.0
    return budget if math.isfinite(budget) and 10 <= budget <= 30 else 10.0


def configured_settle_seconds(environ=None):
    # Preserve the default 15% settlement share for a larger supplied budget,
    # rather than giving all extra time to engine reads and starving the tail.
    return SETTLE_SECONDS * configured_stop_budget(environ) / 10


def configured_grace_seconds(environ=None):
    return (configured_stop_budget(environ) - configured_settle_seconds(environ)
            - CLEANUP_RESERVE_SECONDS)


def begin_shutdown(*, grace=None):
    """Close admission and set one absolute deadline, never renewed by a waiter."""
    global _closing_loop, _stop_deadline, _retire_at, _settle_at
    loop = asyncio.get_running_loop()
    if _closing_loop is not loop:
        _closing_loop = loop
        now = loop.time()
        # The existing final Uvicorn scheduling tick stays inside the budget.
        _stop_deadline = now + configured_stop_budget() - 0.1
        _retire_at = min(_stop_deadline, now + (
            configured_grace_seconds() if grace is None else grace))
        _settle_at = min(_stop_deadline, _retire_at + configured_settle_seconds())
    return _stop_deadline


async def finish(*, deadline=None):
    """Wait for retired finalization without cancelling it or extending the stop."""
    if _closing_loop is not asyncio.get_running_loop():
        return True
    deadline = _stop_deadline if deadline is None else min(deadline, _stop_deadline)
    turns = list(_turns)
    if not turns:
        return True
    waits = {asyncio.create_task(work.finished.wait()) for work in turns}
    _, pending = await asyncio.wait(
        waits, timeout=max(0, deadline - asyncio.get_running_loop().time()))
    for task in pending:
        task.cancel()
    return not pending


async def settle(*, grace=None, http_tasks=()):
    """Drain finite requests, retire reads, then share the absolute stop deadline."""
    begin_shutdown(grace=grace)
    watched = set(http_tasks) | {work.owner for work in _turns}
    watched.discard(asyncio.current_task())
    if watched:
        await asyncio.wait(watched, timeout=max(
            0, _retire_at - asyncio.get_running_loop().time()))
    for work in list(_turns):
        work.interrupt.set()
    # Enter lifespan on the existing schedule even if a tail is slow. Lifespan
    # waits for it before closing the DB; both waits consume this SAME deadline.
    return await finish(deadline=_settle_at)

"""App shutdown waits for session reflections once, not once per chat.

Each pooled engine's close starts its chat's learner drain (session
reflection) and, outside shutdown, waits for it within the adapter's
five-second engine close. Closing seven chats with slow reflections one
after another would spend 35 s there, past a supervisor's 30 s kill. At
shutdown the pool stops every transport first, then waits for all drains
together under one budget that ends, cancellation included, before the
cleanup reserve that precedes the stop deadline.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import app.core.session_pool as sp
from app.core.query_shutdown import CLEANUP_RESERVE_SECONDS
from app.core.session_pool import SessionPool, SessionState
from app.core.task_registry import task_registry
from fluid.codemode import journal

ENGINE_CLOSE_GRACE = 5.0  # the adapter's per-engine close wait


class SlowJournal:
    """A learner journal whose reflection outlasts any shutdown."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.cancelled = False
        self.unwound = False

    async def aclose(self) -> None:
        self.started.set()
        try:
            try:
                await asyncio.wait_for(self.finish.wait(), 30)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            self.cancelled = True
            await asyncio.sleep(0.05)  # a cancelled call's own cleanup
            self.unwound = True
            raise


class Engine:
    """An engine close as ACP, delegated and native do it: stop the
    transport, start the journal's drain, wait for it (deferred at shutdown)."""

    def __init__(self) -> None:
        self.journal = SlowJournal()
        self.transport_stopped = False

    async def aclose(self) -> None:
        self.transport_stopped = True
        await journal.wait_drain(journal.start_drain(self.journal))


def _state(engine: Engine) -> SessionState:
    adapter = MagicMock()
    adapter.is_alive.return_value = True
    adapter.subprocess_pid.return_value = None

    async def disconnect(**kwargs):
        try:
            await asyncio.wait_for(engine.aclose(), timeout=ENGINE_CLOSE_GRACE)
        except asyncio.TimeoutError:
            pass

    adapter.disconnect = disconnect
    return SessionState(adapter=adapter, is_connected=True, last_activity=datetime.now(timezone.utc))


@pytest.fixture(autouse=True)
def _isolate():
    task_registry.reset_all()
    sp._terminated_trees.clear()
    yield
    task_registry.reset_all()
    sp._terminated_trees.clear()


async def _shutdown(pool: SessionPool, **kwargs) -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    await pool.shutdown(**kwargs)
    return loop.time() - started


async def test_seven_slow_reflections_share_one_shutdown_budget(monkeypatch):
    monkeypatch.setattr(journal, "SHUTDOWN_DRAIN_SECONDS", 0.5)
    pool = SessionPool(max_size=10)
    engines = [Engine() for _ in range(7)]
    for n, engine in enumerate(engines):
        pool.put(f"chat-{n}", _state(engine))

    elapsed = await _shutdown(pool)

    # Serial per-chat waits would be 7 × 5 s; one shared wait is the budget.
    assert elapsed < 0.5 + 0.3
    assert all(e.transport_stopped for e in engines)
    assert all(e.journal.started.is_set() and e.journal.cancelled for e in engines)
    assert len(pool) == 0


async def test_the_shared_wait_leaves_the_cleanup_reserve_intact():
    """With the production reserve, reflections (and their cancellation)
    end before the reserve begins, so connector shutdown, pending event
    writes and database teardown keep all of it."""
    pool = SessionPool(max_size=10)
    engines = [Engine() for _ in range(3)]
    for n, engine in enumerate(engines):
        pool.put(f"chat-{n}", _state(engine))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CLEANUP_RESERVE_SECONDS + 0.4

    await _shutdown(pool, deadline=deadline)

    assert deadline - loop.time() >= CLEANUP_RESERVE_SECONDS  # cleanup starts with all of it
    assert all(e.journal.cancelled and e.journal.unwound for e in engines)


async def test_a_deadline_already_in_the_reserve_cuts_reflections_off_at_once():
    pool = SessionPool(max_size=10)
    engines = [Engine() for _ in range(2)]
    for n, engine in enumerate(engines):
        pool.put(f"chat-{n}", _state(engine))
    loop = asyncio.get_running_loop()

    elapsed = await _shutdown(pool, deadline=loop.time() + 1.0)  # inside the 2 s reserve

    assert elapsed < 0.2
    # Cancelled with no time left to unwind: nothing is waited for past the cutoff.
    assert all(e.journal._drain_task.cancelling() or e.journal._drain_task.cancelled()
               for e in engines)


async def test_after_a_shutdown_the_same_loop_closes_wait_for_reflection_again():
    await _shutdown(SessionPool(max_size=10))  # empty pool, same loop

    pool = SessionPool(max_size=10)
    engine = Engine()
    pool.put("chat-later", _state(engine))
    remover = asyncio.create_task(pool.remove("chat-later"))
    await asyncio.sleep(0.3)
    # The close is still waiting for its reflection (until the 5 s grace).
    assert not remover.done() and engine.transport_stopped and not engine.journal.cancelled
    engine.journal.finish.set()
    await asyncio.wait_for(remover, 2)


async def test_an_eviction_during_shutdown_still_waits_for_its_reflection(monkeypatch):
    monkeypatch.setattr(journal, "SHUTDOWN_DRAIN_SECONDS", 0.3)
    pool = SessionPool(max_size=10)
    slow_close = asyncio.Event()

    class SlowStop(Engine):
        async def aclose(self) -> None:
            await slow_close.wait()  # keeps shutdown in its removal calls
            await super().aclose()

    pool.put("chat-shutdown", _state(SlowStop()))
    other = Engine()
    shutdown = asyncio.create_task(pool.shutdown())
    await asyncio.sleep(0.05)
    # An independent close on the same loop while shutdown is under way.
    evict = asyncio.create_task(asyncio.wait_for(other.aclose(), timeout=0.3))
    with pytest.raises(asyncio.TimeoutError):
        await evict
    assert other.transport_stopped  # it waited out its own deadline for the drain
    assert not other.journal._drain_task.done()
    other.journal.finish.set()
    slow_close.set()
    await asyncio.wait_for(shutdown, 2)


async def test_a_close_outside_shutdown_still_waits_for_its_reflection():
    engine = Engine()
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(engine.aclose(), timeout=0.2)
    # The loop runs a timer up to one clock tick early (15.6 ms on Windows).
    assert loop.time() - started >= 0.2 - loop._clock_resolution
    # The deadline ended the wait, never the reflection: it keeps running.
    task = engine.journal._drain_task
    assert not task.done() and not engine.journal.cancelled
    task.cancel()
    await asyncio.wait({task}, timeout=1)


async def test_cancelling_shutdown_cancels_its_reflections_and_ends_the_deferral():
    pool = SessionPool(max_size=10)
    engines = [Engine() for _ in range(2)]
    for n, engine in enumerate(engines):
        pool.put(f"chat-{n}", _state(engine))
    shutdown = asyncio.create_task(pool.shutdown())
    await asyncio.wait_for(asyncio.gather(*(e.journal.started.wait() for e in engines)), 2)
    await asyncio.sleep(0.05)  # in the shared wait now
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    await asyncio.sleep(0.1)
    # Cut off, so nothing is marked and those chats reflect at their next close.
    assert all(e.journal.cancelled for e in engines)

    later = Engine()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(later.aclose(), timeout=0.2)  # closes wait again
    later.journal.finish.set()

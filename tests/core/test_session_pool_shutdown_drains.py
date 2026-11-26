"""App shutdown waits for session reflections once, not once per chat.

Each pooled engine's close starts its chat's learner drain (session
reflection) and, outside shutdown, waits for it within the adapter's
five-second engine close. Closing seven chats with slow reflections one
after another would spend 35 s there, past a supervisor's 30 s kill. At
shutdown the pool stops every transport first, then waits for all drains
together under one budget that also never passes the stop deadline.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import app.core.session_pool as sp
from app.core.session_pool import SessionPool, SessionState
from app.core.task_registry import task_registry
from fluid.codemode import journal

ENGINE_CLOSE_GRACE = 5.0  # the adapter's per-engine close wait


class SlowJournal:
    """A learner journal whose reflection outlasts any shutdown."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def aclose(self) -> None:
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
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
    try:
        await pool.shutdown(**kwargs)
    finally:
        journal._DEFERRING.discard(loop)
    return loop.time() - started


async def test_seven_slow_reflections_share_one_shutdown_budget(monkeypatch):
    monkeypatch.setattr(journal, "SHUTDOWN_DRAIN_SECONDS", 0.5)
    pool = SessionPool(max_size=10)
    engines = [Engine() for _ in range(7)]
    for n, engine in enumerate(engines):
        pool.put(f"chat-{n}", _state(engine))

    elapsed = await _shutdown(pool)

    # Serial per-chat waits would be 7 × 5 s; one shared wait is the budget.
    assert elapsed < 0.5 + 1.0 + 0.5
    assert all(e.transport_stopped for e in engines)
    assert all(e.journal.started.is_set() and e.journal.cancelled for e in engines)
    assert len(pool) == 0


async def test_the_shared_wait_never_passes_the_stop_deadline():
    pool = SessionPool(max_size=10)
    engines = [Engine() for _ in range(3)]
    for n, engine in enumerate(engines):
        pool.put(f"chat-{n}", _state(engine))
    loop = asyncio.get_running_loop()

    elapsed = await _shutdown(pool, deadline=loop.time() + 0.3)

    assert elapsed < 0.3 + 1.0 + 0.5  # not SHUTDOWN_DRAIN_SECONDS (10 s)
    assert all(e.journal.cancelled for e in engines)


async def test_a_close_outside_shutdown_still_waits_for_its_reflection():
    engine = Engine()
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(engine.aclose(), timeout=0.2)
    assert loop.time() - started >= 0.2
    # The deadline ended the wait, never the reflection: it keeps running.
    task = engine.journal._drain_task
    assert not task.done() and not engine.journal.cancelled
    task.cancel()
    await asyncio.wait({task}, timeout=1)

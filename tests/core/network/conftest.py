"""Shared fixtures for the multi-worker network tests."""
import asyncio

import pytest
from fluid.codemode import transactions

from app.api import network as network_api
from app.api import pairing
from app.network import engine, memory_records, tokens, transport
from tests.core.network.harness import Worker


@pytest.fixture
def workers(tmp_path, monkeypatch):
    """Factory for in-process ``Worker``s; closes them and resets network state."""
    from app.core import task_persistence
    from tests.core.network.harness import _ACTIVE

    original_run = asyncio.run

    def run(coro, **kwargs):
        async def drained():
            try:
                return await coro
            finally:
                consumer = task_persistence._consumer
                # Nested run_as() loops must not flush/reset another loop's
                # consumer or its loop-bound wakeup and drain lock.
                owns_writer = consumer is None or consumer.get_loop() is asyncio.get_running_loop()
                if _ACTIVE.get() is not None and owns_writer:
                    # The app keeps its loop alive. These scenarios don't: wait
                    # for the REAL writer (including a popped in-flight snapshot)
                    # before ending its loop, then release loop-bound primitives.
                    await task_persistence.flush()
                    assert task_persistence.pending_count() == 0
                    consumer = task_persistence._consumer
                    task_persistence.reset()
                    if consumer is not None:
                        await asyncio.gather(consumer, return_exceptions=True)
        return original_run(drained(), **kwargs)

    monkeypatch.setattr(asyncio, "run", run)
    # A prior app lifespan registers materialization callbacks. Do not inherit
    # them: these workers explicitly drive apply side effects themselves.
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()
    made = []

    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()


_PER_PROCESS_LIMITERS = (
    pairing._exchange_limiter,
    network_api._join_limiter,
    network_api._challenge_limiter,
)


@pytest.fixture(autouse=True)
def _reset_request_limiters():
    """Start every test with empty per-process request limiters.

    Every in-process worker calls from the same client address, so pairings,
    joins and peer challenges (one per ``PeerLink.authenticate``) all count
    against one per-minute bucket. Without a reset, a test's outcome depended
    on how many earlier tests had authenticated in the last minute: a run of
    the network tests alone came within a few challenges of the 60/min cap,
    and an unlucky order got HTTP 429 instead of a device token.
    """
    for limiter in _PER_PROCESS_LIMITERS:
        limiter.reset()
    yield
    for limiter in _PER_PROCESS_LIMITERS:
        limiter.reset()


@pytest.fixture(autouse=True)
def _isolate_memory_publisher(monkeypatch):
    """Start every network test with no memory write hook and no open window.

    The app installs Fluid's post-commit hook, and #1224 defers ``publish()``
    until a committed save's quiet gap. Both are process globals: a hook left
    installed, or a window opened by an earlier test's last save, would make
    this test's explicit ``publish()`` return 0. Tests that exercise the real
    hook install it themselves; monkeypatch restores both globals afterwards.
    """
    monkeypatch.setattr(transactions, "_change_callback", None, raising=False)
    monkeypatch.setattr(memory_records, "_write_window", None)

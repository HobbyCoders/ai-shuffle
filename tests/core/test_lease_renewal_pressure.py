"""Lease retries and execution remain independent of ordinary worker pressure."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg2.pool
import pytest

from app.core import session_runtime
from app.db.async_database import run_lease


@pytest.mark.asyncio
async def test_pool_failure_retries_promptly_then_resumes_normal_cadence(monkeypatch):
    clock = [0.0]
    sleeps = []
    results = iter([psycopg2.pool.PoolError("exhausted"), True, False])

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    async def renew(*args):
        result = next(results)
        if isinstance(result, Exception):
            clock[0] += 2.0
            raise result
        return result

    monkeypatch.setattr(session_runtime.asyncio, "sleep", sleep)
    monkeypatch.setattr(session_runtime.asyncio, "get_running_loop", lambda: SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(session_runtime, "run_lease", renew)
    runtime = SimpleNamespace(_on_lease_lost=Mock())
    await session_runtime.SessionRuntime._renew_loop(runtime, "run", "runner", 1)
    assert sleeps == [20, 1, 20]
    runtime._on_lease_lost.assert_called_once()


@pytest.mark.asyncio
async def test_lease_executor_survives_default_executor_saturation():
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(lambda: None)  # Ensure the original executor exists.
    original = loop._default_executor
    release = threading.Event()
    ready = threading.Barrier(3)
    context = ContextVar("lease-test", default=False)
    context.set(True)

    def hold():
        ready.wait(timeout=3)
        assert release.wait(3)

    with ThreadPoolExecutor(max_workers=2) as blocked:
        loop.set_default_executor(blocked)
        holders = [blocked.submit(hold) for _ in range(2)]
        try:
            ready.wait(timeout=3)
            assert await asyncio.wait_for(run_lease(context.get), 1)
        finally:
            release.set()
            loop.set_default_executor(original)
        for future in holders:
            future.result(timeout=3)

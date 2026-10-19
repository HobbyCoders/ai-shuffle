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
    real_wait_for = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        assert timeout > 0
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(session_runtime.asyncio, "wait_for", wait_for)
    monkeypatch.setattr(session_runtime.asyncio, "get_running_loop", lambda: SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(session_runtime, "run_lease", renew)
    runtime = SimpleNamespace(_lease_finalizing=False, _on_lease_lost=Mock())
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

@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["runtime", "recorder"])
@pytest.mark.parametrize("failure", ["stuck", "pool"])
async def test_heartbeat_fails_closed_at_ttl(monkeypatch, owner, failure):
    from app.core import turn_recorder

    module = session_runtime if owner == "runtime" else turn_recorder
    monkeypatch.setattr(module, "RUN_LEASE_TTL_SECONDS", 0.05)
    monkeypatch.setattr(module, "RUN_LEASE_RENEW_SECONDS", 0.005)
    cancelled = asyncio.Event()
    calls = []

    async def renew(*args):
        calls.append(args)
        if failure == "pool":
            raise psycopg2.pool.PoolError("exhausted")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(module, "run_lease", renew)
    lost = Mock()
    if owner == "runtime":
        runtime = SimpleNamespace(_lease_finalizing=False, _on_lease_lost=lost)
        heartbeat = session_runtime.SessionRuntime._renew_loop(runtime, "run", "runner", 1)
    else:
        recorder = turn_recorder.TurnRecorder("session", None, "run", 1, "runner")
        recorder.on_superseded = lost
        heartbeat = recorder._renew_loop()
    await asyncio.wait_for(heartbeat, 1)
    assert calls
    lost.assert_called_once()
    if failure == "stuck":
        assert cancelled.is_set()
    if owner == "recorder":
        assert recorder.superseded


@pytest.mark.asyncio
async def test_recorder_heartbeat_escapes_default_executor_pressure(monkeypatch):
    from app.core import turn_recorder

    loop = asyncio.get_running_loop()
    await asyncio.to_thread(lambda: None)
    original = loop._default_executor
    release = threading.Event()
    ready = threading.Barrier(3)
    calls = []

    def hold():
        ready.wait(timeout=3)
        assert release.wait(3)

    def renew(*args):
        calls.append(threading.current_thread().name)
        return False

    monkeypatch.setattr(turn_recorder.runs_db, "renew_run", renew)
    monkeypatch.setattr(turn_recorder, "RUN_LEASE_RENEW_SECONDS", 0.005)
    recorder = turn_recorder.TurnRecorder("session", None, "run", 1, "runner")
    with ThreadPoolExecutor(max_workers=2) as blocked:
        loop.set_default_executor(blocked)
        holders = [blocked.submit(hold) for _ in range(2)]
        try:
            ready.wait(timeout=3)
            await asyncio.wait_for(recorder._renew_loop(), 1)
            assert recorder.superseded
            assert calls[0].startswith("db-lease")
        finally:
            release.set()
            loop.set_default_executor(original)
        for future in holders:
            future.result(timeout=3)


@pytest.mark.asyncio
async def test_two_blocked_lease_workers_do_not_stall_other_heartbeats():
    release = threading.Event()
    ready = threading.Barrier(3)

    def hold():
        ready.wait(timeout=3)
        assert release.wait(3)

    holders = [asyncio.create_task(run_lease(hold)) for _ in range(2)]
    try:
        # Barrier wait itself must not block the event loop running the holders.
        await asyncio.to_thread(ready.wait, 3)
        assert await asyncio.wait_for(run_lease(lambda: True), 1)
    finally:
        release.set()
        await asyncio.gather(*holders)

"""Regression tests for connection retention and slot reclamation in app.db.base.

Covers the leak fixed in ``_close_connection``: psycopg2's
ThreadedConnectionPool.putconn() rolls back the connection *before* it
removes the slot from ``_used``/``_rused``. When that rollback raises (the
server dropped the connection), the slot was leaked permanently and the old
bare ``except: pass`` hid it. ``_close_connection`` must now force the slot
free so the pool cannot exhaust itself with phantom "in use" connections.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import psycopg2
import psycopg2.pool
import pytest

import app.db.base as base


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["native", "delegated"])
@pytest.mark.parametrize("phase", ["node", "terminal", "cli-id"])
async def test_turn_writes_outlast_ordinary_pool_deadline(
    retaining_pool: tuple[base._RetainingThreadedConnectionPool, list[_PhysicalConn]],
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    phase: str,
) -> None:
    from app.core.provider.fluid_pg_store import (
        PgFluidStore, _append_node_sync, _set_cli_session_id_sync,
    )
    from app.db.async_database import run_sync
    from fluid.engine.delegated import DelegatedLoop
    from fluid.engine.native import NativeLoop
    from fluid.events.model import ErrorEvent, TurnComplete
    from fluid.messages import Message
    from fluid.store.base import SessionNode

    pool, physical = retaining_pool
    pool._reserved = 2
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    monkeypatch.setattr(base, "_POOL_ACQUIRE_MAX_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(base, "_TURN_POOL_ACQUIRE_MAX_WAIT_SECONDS", 2.0)

    def commit(self: _PhysicalConn) -> None:
        pass

    monkeypatch.setattr(_PhysicalConn, "commit", commit, raising=False)
    monkeypatch.setattr(_FakeCursor, "rowcount", 1, raising=False)
    ready = threading.Barrier(pool.maxconn - pool._reserved + 1)
    release = threading.Event()

    def holder() -> None:
        with base.get_db():
            ready.wait(timeout=3)
            assert release.wait(4)

    async def finish(index: int) -> None:
        sid = f"turn-{index}"
        store = PgFluidStore()
        if engine == "native":
            loop = NativeLoop(provider=object(), store=store, model="test", session_id=sid)
        else:
            loop = DelegatedLoop(store=store, model="test", session_id=sid)
        try:
            # Drive the real node and terminal-event writers, not a retry mock.
            node = SessionNode(
                node_id=f"node-{index}", parent_id=None,
                message=Message(role="assistant", content=[]),
            )
            if phase == "cli-id":
                await run_sync(_set_cli_session_id_sync, sid, f"cli-{index}")
            if phase == "node":
                await run_sync(_append_node_sync, sid, node)
            event = await loop._emit(TurnComplete(
                session_id=sid, stop_reason="end_turn", num_model_calls=1,
            ))
            if phase != "node":
                await run_sync(_append_node_sync, sid, node)
            assert isinstance(event, TurnComplete)
            assert not any(isinstance(e, ErrorEvent) for e in loop.bus.log(sid))
        finally:
            await loop.aclose()

    with ThreadPoolExecutor(max_workers=pool.maxconn) as executor:
        holders = [executor.submit(holder) for _ in range(pool.maxconn - pool._reserved)]
        tasks: list[asyncio.Task[None]] = []
        try:
            ready.wait(timeout=3)
            tasks = [asyncio.create_task(finish(index)) for index in range(45)]
            # Ordinary callers still time out, while turn writes stay queued.
            with pytest.raises(psycopg2.pool.PoolError, match="exhausted"):
                await asyncio.to_thread(base._get_pg_connection)
            await asyncio.sleep(0.1)
            assert not any(task.done() for task in tasks)
            assert len(physical) <= pool.maxconn - pool._reserved
        finally:
            release.set()
            await asyncio.gather(*tasks)
        for future in holders:
            future.result(timeout=3)
    statements = [sql for conn in physical for sql in conn.executed]
    assert sum("INSERT INTO fluid_session_nodes" in sql for sql in statements) == 45
    assert sum("INSERT INTO fluid_session_events" in sql for sql in statements) == 45
    assert not any(sql.startswith("SET") for sql in statements)
    assert not pool._used and not pool._waiters and not pool._lease_waiters
    assert base._pool_acquire_wait.get() is None
    assert not base._lease_checkout.get()


def test_turn_admission_is_bounded_and_does_not_take_lease_slots(
    retaining_pool: tuple[base._RetainingThreadedConnectionPool, list[_PhysicalConn]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = retaining_pool
    pool._reserved = 2
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    monkeypatch.setattr(base, "_TURN_POOL_ACQUIRE_MAX_WAIT_SECONDS", 0.05)

    def commit(self: _PhysicalConn) -> None:
        pass

    monkeypatch.setattr(_PhysicalConn, "commit", commit, raising=False)
    held = [pool.getconn() for _ in range(pool.maxconn - pool._reserved)]
    started = time.monotonic()
    try:
        with pytest.raises(psycopg2.pool.PoolError, match="exhausted"):
            with base.get_turn_db():
                pytest.fail("turn admission consumed a lease slot")
        assert time.monotonic() - started < 1
        assert base._pool_acquire_wait.get() is None
        assert not pool._waiters
        # Lease callers, unlike turn writers, can still use the reservation.
        with base.get_lease_db():
            pass
    finally:
        for conn in held:
            pool.putconn(conn)


def test_exhaustion_logs_once_until_admission_recovers_with_stats(
    retaining_pool: tuple[base._RetainingThreadedConnectionPool, list[_PhysicalConn]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool, _ = retaining_pool
    monkeypatch.setattr(base, "_POOL_ACQUIRE_MAX_WAIT_SECONDS", 0.1)
    held = [pool.getconn() for _ in range(pool.maxconn)]
    ready = threading.Barrier(9)

    def timeout() -> None:
        ready.wait(timeout=3)
        with pytest.raises(psycopg2.pool.PoolError, match="exhausted"):
            base._get_pg_connection()

    try:
        for wave in range(3):
            if wave == 2:
                # Draining timeout queues alone is not recovery. A successful
                # ordinary admission rearms the warning for a later shortage.
                pool.putconn(held.pop())
                recovered = base._get_pg_connection()
                base._close_connection(recovered)
                held.append(pool.getconn())
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(timeout) for _ in range(8)]
                ready.wait(timeout=3)
                for future in futures:
                    future.result(timeout=3)
        messages = [r.message for r in caplog.records if "PostgreSQL pool exhausted:" in r.message]
        assert len(messages) == 2
        assert all("in_use=8 max=8 waiters=" in message for message in messages)
    finally:
        for conn in held:
            pool.putconn(conn)


def test_turn_transaction_failure_is_not_retried(
    retaining_pool: tuple[base._RetainingThreadedConnectionPool, list[_PhysicalConn]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = retaining_pool
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    with pytest.raises(ValueError, match="write failed"):
        with base.get_turn_db():
            raise ValueError("write failed")
    assert pool.pressure_stats()["acquisitions"] == 1
    assert base._pool_acquire_wait.get() is None
    assert not pool._used



@pytest.mark.asyncio
async def test_event_backpressure_keeps_pool_capacity_free(
    retaining_pool: tuple[base._RetainingThreadedConnectionPool, list[_PhysicalConn]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.provider.fluid_pg_store import _append_events_sync
    from app.db.async_database import run_event_write
    from fluid.events.model import TurnComplete

    pool, _ = retaining_pool
    pool._reserved = 2
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    monkeypatch.setattr(_FakeCursor, "rowcount", 1, raising=False)
    release = threading.Event()
    saturated = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def commit(self: _PhysicalConn) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                saturated.set()
        try:
            assert release.wait(3)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(_PhysicalConn, "commit", commit, raising=False)
    tasks = [asyncio.create_task(run_event_write(_append_events_sync, f"session-{i}", [
        TurnComplete(session_id=f"session-{i}", seq=1, stop_reason="end_turn", num_model_calls=1),
    ])) for i in range(45)]
    try:
        assert await asyncio.to_thread(saturated.wait, 2)
        assert len(pool._used) == 4
        pressure = base.event_pressure_stats()
        assert pressure["active"] == 4
        assert pressure["oldest_write_seconds"] > 0
        from app.core.start_admission import snapshot
        assert snapshot()["oldest"] >= pressure["oldest_write_seconds"]
        # Backpressured event producers hold no DB slot: an ordinary read can
        # still borrow one without using either lease-only reserved connection.
        # Even with 45 pending event writers, default-executor work dispatches.
        probe = await asyncio.wait_for(asyncio.to_thread(base._get_pg_connection), timeout=1)
        base._close_connection(probe)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert peak == 4
    assert not pool._used
    assert base.event_pressure_stats()["active"] == 0
    assert base.event_pressure_stats()["waiting"] == 0


def test_event_admission_timeout_never_borrows_a_connection(
    retaining_pool: tuple[base._RetainingThreadedConnectionPool, list[_PhysicalConn]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = retaining_pool
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(base, "_event_write_slots", slots)
    monkeypatch.setattr(base, "_TURN_POOL_ACQUIRE_MAX_WAIT_SECONDS", 0.01)
    try:
        with pytest.raises(psycopg2.pool.PoolError, match="event write admission"):
            with base.get_event_db():
                pytest.fail("no event slot available")
        assert pool.pressure_stats()["acquisitions"] == 0
    finally:
        slots.release()


@pytest.mark.parametrize("scope", ["get_db", "transaction"])
def test_slow_hold_logs_location_not_local_values(monkeypatch, caplog, scope):
    clock = [10.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
    private_value = "never-log-connection-parameters"
    with getattr(base, scope)():
        clock[0] += 11
    assert "Database connection held 11.0s" in caplog.text
    assert "test_slow_hold_logs_location_not_local_values" in caplog.text
    assert private_value not in caplog.text


def test_pressure_reports_age_of_still_held_connections(retaining_pool, monkeypatch):
    pool, _ = retaining_pool
    clock = [10.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
    conn = pool.getconn()
    clock[0] += 61
    assert base.pressure_stats()["oldest_hold_seconds"] == 61
    pool.putconn(conn)
    assert base.pressure_stats()["oldest_hold_seconds"] == 0


@pytest.mark.parametrize("reserved, succeeds", [(0, False), (2, True)])
def test_concurrent_holders_cannot_starve_renewal(retaining_pool, monkeypatch, reserved, succeeds):
    from concurrent.futures import ThreadPoolExecutor
    from app.db import runs, turns

    pool, _ = retaining_pool
    pool._reserved = reserved
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    monkeypatch.setattr(base, "_POOL_ACQUIRE_MAX_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(_PhysicalConn, "commit", lambda self: None, raising=False)
    monkeypatch.setattr(_FakeCursor, "rowcount", 1, raising=False)
    monkeypatch.setattr(_FakeCursor, "fetchall", lambda self: [], raising=False)
    n = pool.maxconn - reserved
    ready = threading.Barrier(n + 1)
    release = threading.Event()

    def holder():
        with base.get_db():
            ready.wait(timeout=3)
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=n + 1) as executor:
        holders = [executor.submit(holder) for _ in range(n)]
        try:
            ready.wait(timeout=3)
            # An ordinary waiter ahead of renewal must not block the lease FIFO.
            ordinary = executor.submit(base._get_pg_connection)
            if succeeds:
                assert runs.renew_run("run", "owner", 1, 60)
                assert turns.reap_expired() == []
            else:
                with pytest.raises(psycopg2.pool.PoolError, match="exhausted"):
                    runs.renew_run("run", "owner", 1, 60)
            with pytest.raises(psycopg2.pool.PoolError, match="exhausted"):
                ordinary.result(timeout=2)
        finally:
            release.set()
        for future in holders:
            future.result(timeout=3)
    assert not pool._used and not pool._waiters and not pool._lease_waiters
    assert base._lease_checkout.get() is False


def test_reaper_uses_longer_timeouts_and_restores_heartbeat_limits(retaining_pool, monkeypatch):
    from app.db import turns

    pool, physical = retaining_pool
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    monkeypatch.setattr(_PhysicalConn, "commit", lambda self: None, raising=False)
    monkeypatch.setattr(_FakeCursor, "fetchall", lambda self: [], raising=False)
    assert turns.reap_expired() == []
    statements = [sql for conn in physical for sql in conn.executed]
    assert "SET LOCAL lock_timeout = '5000ms'" in statements
    assert "SET LOCAL statement_timeout = '15000ms'" in statements
    assert base._lease_timeouts.get() == (500, 1500)
    with base.get_lease_db():
        pass
    statements = [sql for conn in physical for sql in conn.executed]
    assert "SET LOCAL lock_timeout = '500ms'" in statements
    assert "SET LOCAL statement_timeout = '1500ms'" in statements
    assert not pool._used


def test_lease_checkout_resets_priority_after_failure(retaining_pool, monkeypatch):
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    with pytest.raises(ValueError):
        with base.get_lease_db(lock_timeout_ms=5000, statement_timeout_ms=15000):
            raise ValueError("body failed")
    assert base._lease_timeouts.get() == (500, 1500)
    assert not base._lease_checkout.get()
    assert not retaining_pool[0]._used



class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _RollbackFailsPool:
    """Mimics psycopg2 putconn: the normal return path raises (rollback on a
    dead connection), leaving the slot in ``_used``; close=True discards it
    cleanly and frees the slot.
    """

    def __init__(self, conn):
        self._lock = threading.Lock()
        self._used = {1: conn}
        self._rused = {id(conn): 1}
        self.calls = []

    def putconn(self, conn=None, key=None, close=False):
        self.calls.append(close)
        if not close:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        k = self._rused.pop(id(conn), None)
        if k is not None:
            self._used.pop(k, None)


class _AlwaysFailsPool:
    """putconn always raises — forces the last-resort bookkeeping cleanup."""

    def __init__(self, conn):
        self._lock = threading.Lock()
        self._used = {1: conn}
        self._rused = {id(conn): 1}
        self.calls = []

    def putconn(self, conn=None, key=None, close=False):
        self.calls.append(close)
        raise psycopg2.OperationalError("boom")


def test_close_connection_reclaims_slot_when_rollback_fails(monkeypatch):
    conn = _FakeConn()
    pool = _RollbackFailsPool(conn)
    monkeypatch.setattr(base, "_pg_pool", pool)

    base._close_connection(conn)

    # Normal putconn was tried, then the close=True retry reclaimed the slot.
    assert pool.calls == [False, True]
    assert pool._used == {}
    assert pool._rused == {}


def test_close_connection_force_frees_slot_when_putconn_always_fails(monkeypatch):
    conn = _FakeConn()
    pool = _AlwaysFailsPool(conn)
    monkeypatch.setattr(base, "_pg_pool", pool)

    base._close_connection(conn)

    # Both putconn attempts failed; last-resort cleanup must still free the slot
    # so it cannot leak and exhaust the pool.
    assert pool.calls == [False, True]
    assert pool._used == {}
    assert pool._rused == {}
    assert conn.closed is True


def test_pool_stats_none_when_uninitialized(monkeypatch):
    monkeypatch.setattr(base, "_pg_pool", None)
    assert base.pool_stats() is None


def test_pool_stats_reports_in_use_idle_and_bounds(monkeypatch):
    class _StatsPool:
        minconn = 2
        maxconn = 20

        def __init__(self):
            self._used = {1: "a", 2: "b", 3: "c"}
            self._pool = ["x", "y"]

    monkeypatch.setattr(base, "_pg_pool", _StatsPool())
    assert base.pool_stats() == {"in_use": 3, "idle": 2, "min": 2, "max": 20}


def test_close_connection_happy_path_returns_without_retry(monkeypatch):
    conn = _FakeConn()

    class _GoodPool:
        def __init__(self):
            self.calls = []

        def putconn(self, conn=None, key=None, close=False):
            self.calls.append(close)

    pool = _GoodPool()
    monkeypatch.setattr(base, "_pg_pool", pool)

    base._close_connection(conn)

    # A successful return must not trigger any retry/force-free path.
    assert pool.calls == [False]
    assert conn.closed is False

class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def execute(self, sql, params=None):
        self._conn.executed.append(sql)
        if self._conn.ping_error is not None:
            raise self._conn.ping_error

    def close(self):
        self._conn.cursors_closed += 1


class _PhysicalConn(_FakeConn):
    """Only the physical connection is fake; pool bookkeeping is psycopg2's."""

    def __init__(self):
        super().__init__()
        self.info = self
        self.transaction_status = psycopg2.extensions.TRANSACTION_STATUS_IDLE
        self.rollbacks = 0
        self.fail_rollback = False
        self.executed = []
        self.cursors_closed = 0
        self.ping_error = None

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1
        if self.fail_rollback:
            raise psycopg2.OperationalError("connection lost during rollback")
        self.transaction_status = psycopg2.extensions.TRANSACTION_STATUS_IDLE


@pytest.fixture
def retaining_pool(monkeypatch):
    created = []

    def connect(*args, **kwargs):
        conn = _PhysicalConn()
        created.append(conn)
        return conn

    monkeypatch.setattr(psycopg2, "connect", connect)
    # conftest deliberately blocks init_pool; construct the real subclass here.
    pool = base._RetainingThreadedConnectionPool(2, 8)
    monkeypatch.setattr(base, "_pg_pool", pool)
    yield pool, created
    if not pool.closed:
        pool.closeall()




def test_return_after_pool_close_discards_without_warning(retaining_pool, caplog):
    pool, _ = retaining_pool
    conn = pool.getconn()
    pool.closeall()
    base._close_connection(conn)
    assert conn.closed
    assert not pool._used and not pool._rused
    assert not caplog.records


def test_closed_pool_does_not_close_unowned_connection(retaining_pool):
    pool, _ = retaining_pool
    foreign = _FakeConn()
    pool.closeall()
    with pytest.raises(psycopg2.pool.PoolError, match="closed"):
        pool.putconn(foreign)
    assert not foreign.closed


def test_shutdown_races_with_connection_return(retaining_pool, monkeypatch, caplog):
    from concurrent.futures import ThreadPoolExecutor

    pool, _ = retaining_pool
    conn = pool.getconn()
    returning, resume = threading.Event(), threading.Event()
    putconn = pool.putconn

    def paused_return(*args, **kwargs):
        returning.set()
        assert resume.wait(3)
        return putconn(*args, **kwargs)

    monkeypatch.setattr(pool, "putconn", paused_return)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(base._close_connection, conn)
        try:
            assert returning.wait(3)
            pool.closeall()
            monkeypatch.setattr(base, "_pg_pool", None)
        finally:
            resume.set()
        future.result(timeout=3)
    assert conn.closed
    assert not pool._used and not pool._rused
    assert not caplog.records


def test_return_reclaims_original_pool_if_global_changes(monkeypatch):
    conn = _FakeConn()
    pool = _AlwaysFailsPool(conn)
    replacement = _AlwaysFailsPool(_FakeConn())
    failing_putconn = pool.putconn

    def replace_pool(*args, **kwargs):
        monkeypatch.setattr(base, "_pg_pool", replacement)
        return failing_putconn(*args, **kwargs)

    monkeypatch.setattr(pool, "putconn", replace_pool)
    monkeypatch.setattr(base, "_pg_pool", pool)
    base._close_connection(conn)
    assert pool.calls == [False, True]
    assert not pool._used and not pool._rused
    assert not replacement.calls
    assert len(replacement._used) == len(replacement._rused) == 1
    assert conn.closed




@pytest.mark.asyncio
async def test_shutdown_drains_cancelled_to_thread_due_reports(retaining_pool, monkeypatch, caplog):
    import asyncio
    from app.db import remote_tasks

    pool, physical = retaining_pool
    entered, draining, resume, finished = (threading.Event() for _ in range(4))
    results, errors = [], []
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    monkeypatch.setattr(remote_tasks, "init_schema", lambda conn: None)
    monkeypatch.setattr(_PhysicalConn, "commit", lambda self: None, raising=False)

    def fetchall(cursor):
        entered.set()
        assert resume.wait(5)
        if cursor._conn.closed:
            raise psycopg2.InterfaceError("cursor already closed")
        return []

    monkeypatch.setattr(_FakeCursor, "fetchall", fetchall, raising=False)
    notify = pool._notify_available

    def notify_shutdown():
        if getattr(pool, "_closing", False) or pool.closed:
            draining.set()
        notify()

    monkeypatch.setattr(pool, "_notify_available", notify_shutdown)

    def query():
        try:
            results.append(remote_tasks.due_reports())
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    task = asyncio.create_task(asyncio.to_thread(query))
    shutdown = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        shutdown = asyncio.create_task(asyncio.to_thread(getattr(pool, "shutdown", pool.closeall)))
        assert await asyncio.to_thread(draining.wait, 3)
        # Cancellation must not let shutdown close a still-running DB query.
        assert not any(conn.closed for conn in physical)
        with pytest.raises(psycopg2.pool.PoolError, match="clos"):
            pool.getconn()
    finally:
        resume.set()
        assert await asyncio.to_thread(finished.wait, 3)
        if shutdown is not None:
            await shutdown
        if not task.done():
            await task
    assert errors == []
    assert results == [[]]
    assert pool.closed and all(conn.closed for conn in physical)
    assert not pool._used and not pool._rused
    assert not caplog.records


def test_shutdown_drain_is_bounded_for_stuck_borrower(retaining_pool, caplog):
    pool, _ = retaining_pool
    conn = pool.getconn()
    pool.shutdown(timeout=0)
    assert pool.closed and conn.closed
    assert "1 borrowed connection" in caplog.text
    pool.putconn(conn)
    assert not pool._used and not pool._rused


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_force_close_warns_and_cancels_all_before_close(retaining_pool, monkeypatch, caplog, cancel_fails):
    pool, _ = retaining_pool
    borrowed = [pool.getconn(), pool.getconn()]
    cancelled = []

    def cancel(conn):
        cancelled.append(conn)
        if cancel_fails and conn is borrowed[0]:
            raise psycopg2.OperationalError("cancel failed")

    for conn in borrowed:
        monkeypatch.setattr(conn, "cancel", lambda conn=conn: cancel(conn), raising=False)
    closeall = pool._closeall

    def close():
        assert cancelled == borrowed
        assert "2 borrowed connection" in caplog.text
        closeall()

    monkeypatch.setattr(pool, "_closeall", close)
    pool.shutdown(timeout=0)
    assert all(conn.closed for conn in borrowed)
    for conn in borrowed:
        pool.putconn(conn)
    assert not pool._used and not pool._rused


def test_retaining_pool_opens_only_startup_minimum(retaining_pool):
    pool, created = retaining_pool
    assert len(created) == 2
    assert base.pool_stats() == {"in_use": 0, "idle": 2, "min": 2, "max": 8}


def test_retaining_pool_reuses_repeated_bursts(retaining_pool):
    pool, created = retaining_pool
    for _ in range(5):
        burst = [pool.getconn() for _ in range(8)]
        for conn in burst:
            pool.putconn(conn)
    # Stock ThreadedConnectionPool creates 32 connections for these five bursts.
    assert len(created) == 8
    assert not any(conn.closed for conn in created)
    assert base.pool_stats() == {"in_use": 0, "idle": 8, "min": 2, "max": 8}


def test_retaining_pool_enforces_maximum(retaining_pool):
    pool, created = retaining_pool
    for _ in range(2):
        burst = [pool.getconn() for _ in range(8)]
        with pytest.raises(psycopg2.pool.PoolError, match="exhausted"):
            pool.getconn()
        assert len(created) == 8
        for conn in burst:
            pool.putconn(conn)
    assert len(pool._pool) == 8


@pytest.mark.parametrize("state", ["closed", "unknown", "explicit"])
def test_retaining_pool_discards_broken_connections(retaining_pool, state):
    pool, created = retaining_pool
    burst = [pool.getconn() for _ in range(8)]
    broken = burst.pop()
    for conn in burst:
        pool.putconn(conn)
    if state == "closed":
        broken.close()
    elif state == "unknown":
        broken.transaction_status = psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN
    pool.putconn(broken, close=state == "explicit")
    assert broken.closed
    assert broken not in pool._pool
    assert not pool._used and not pool._rused
    replacement_burst = [pool.getconn() for _ in range(8)]
    assert broken not in replacement_burst
    assert len(created) == 9
    for conn in replacement_burst:
        pool.putconn(conn)


@pytest.mark.parametrize("status", [
    psycopg2.extensions.TRANSACTION_STATUS_INTRANS,
    psycopg2.extensions.TRANSACTION_STATUS_INERROR,
])
def test_retaining_pool_rolls_back_before_reuse(retaining_pool, status):
    pool, _ = retaining_pool
    burst = [pool.getconn() for _ in range(8)]
    dirty = burst.pop()
    for conn in burst:
        pool.putconn(conn)
    dirty.transaction_status = status
    pool.putconn(dirty)
    assert dirty.rollbacks == 1
    assert pool.getconn() is dirty
    pool.putconn(dirty)


def test_retaining_pool_reclaims_failed_rollback(retaining_pool):
    pool, _ = retaining_pool
    conn = pool.getconn()
    conn.transaction_status = psycopg2.extensions.TRANSACTION_STATUS_INTRANS
    conn.fail_rollback = True
    base._close_connection(conn)
    assert conn.closed
    assert conn.rollbacks == 1
    assert not pool._used and not pool._rused
    assert conn not in pool._pool


def test_retaining_pool_serializes_concurrent_returns(retaining_pool):
    from concurrent.futures import ThreadPoolExecutor

    pool, created = retaining_pool
    for _ in range(3):
        burst = [pool.getconn() for _ in range(8)]
        barrier = threading.Barrier(8)

        def return_connection(conn):
            barrier.wait(timeout=5)
            pool.putconn(conn)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(return_connection, burst))
        assert len(pool._pool) == len(set(pool._pool)) == 8
        assert not pool._used and not pool._rused
    assert len(created) == 8


def test_retaining_pool_shutdown_closes_idle_and_checked_out(retaining_pool):
    pool, created = retaining_pool
    burst = [pool.getconn() for _ in range(8)]
    for conn in burst[:-1]:
        pool.putconn(conn)
    pool.closeall()
    assert pool.closed
    assert all(conn.closed for conn in created)
    with pytest.raises(psycopg2.pool.PoolError, match="closed"):
        pool.getconn()
    # Shutdown still rejects new work, but an existing borrower's late return
    # is a discard, not an error or a chance to resurrect a closed socket.
    idle_before = list(pool._pool)
    pool.putconn(burst[-1])
    assert pool.closed and burst[-1].closed
    assert pool._pool == idle_before
    assert burst[-1] not in pool._pool
    assert not pool._used and not pool._rused

@pytest.mark.parametrize("state", ["closed", "unknown", "explicit", "rollback"])
def test_pressure_disposal_and_shutdown(retaining_pool, state):
    pool, created = retaining_pool
    conn = pool.getconn()
    if state == "closed":
        conn.close()
    elif state == "unknown":
        conn.transaction_status = psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN
    elif state == "rollback":
        conn.transaction_status = psycopg2.extensions.TRANSACTION_STATUS_INTRANS
        conn.fail_rollback = True
    if state == "explicit":
        pool.putconn(conn, close=True)
    else:
        base._close_connection(conn)
    stats = base.pressure_stats()
    assert stats["connections_opened"] == 2
    assert stats["connections_discarded"] == 1
    assert stats["holds"] == 1
    assert stats["hold_seconds_total"] >= stats["hold_seconds_max"] >= 0
    pool.closeall()
    assert base.pressure_stats()["connections_discarded"] == len(created)
    assert not pool._held_since and not pool._physical


def test_pressure_timeout_and_cleanup_failure(retaining_pool, monkeypatch):
    pool, _ = retaining_pool
    clock = [10.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pool._available, "wait", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    burst = [pool.getconn() for _ in range(8)]
    monkeypatch.setattr(base, "_POOL_ACQUIRE_MAX_WAIT_SECONDS", 0.25)
    with pytest.raises(psycopg2.pool.PoolError):
        base._get_pg_connection()
    stats = base.pressure_stats()
    assert stats["waiting"] == 0
    assert stats["acquisition_waits"] == stats["acquisition_timeouts"] == 1
    for prefix in ("acquisition", "wait", "timeout"):
        assert stats[prefix + "_seconds_total"] == pytest.approx(0.25)
        assert stats[prefix + "_seconds_max"] == pytest.approx(0.25)
    conn = burst.pop()
    conn.transaction_status = psycopg2.extensions.TRANSACTION_STATUS_INTRANS
    conn.fail_rollback = True
    def failed_close():
        clock[0] += 0.5
        raise psycopg2.OperationalError("close failed")
    monkeypatch.setattr(conn, "close", failed_close)
    base._close_connection(conn)
    assert id(conn) not in pool._rused
    stats = base.pressure_stats()
    assert stats["holds"] == stats["connections_discarded"] == 1
    assert stats["hold_seconds_total"] == pytest.approx(1.25)
    for conn in burst:
        pool.putconn(conn)


def test_pressure_80_contending_clients_reuse_and_drain(retaining_pool, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    pool, created = retaining_pool
    # Hold all slots until all 80 clients have entered backpressure.
    held = [pool.getconn() for _ in range(8)]
    all_waiting = threading.Event()
    original = pool._wait_started
    def started():
        original()
        if pool.pressure_stats()["waiting"] == 80:
            all_waiting.set()
    monkeypatch.setattr(pool, "_wait_started", started)
    monkeypatch.setattr(base, "_POOL_ACQUIRE_MAX_WAIT_SECONDS", 5.0)
    def client(_):
        conn = base._get_pg_connection()
        try:
            time.sleep(0.002)
        finally:
            base._close_connection(conn)
    with ThreadPoolExecutor(max_workers=80) as executor:
        futures = [executor.submit(client, n) for n in range(80)]
        try:
            assert all_waiting.wait(4)
            assert base.pressure_stats()["waiting"] == 80
            assert len(created) == 8
        finally:
            for conn in held:
                pool.putconn(conn)
        for future in futures:
            future.result(timeout=6)
    stats = base.pressure_stats()
    assert stats["waiting"] == stats["acquisition_timeouts"] == 0
    assert stats["acquisitions"] == stats["acquisition_waits"] == 80
    assert stats["holds"] == 88
    assert stats["connections_opened"] == len(created) == 8
    assert stats["connections_discarded"] == 0
    assert stats["wait_seconds_total"] >= stats["wait_seconds_max"] > 0
    assert not pool._held_since
    assert base.pool_stats() == {"in_use": 0, "idle": 8, "min": 2, "max": 8}


def test_pressure_non_pool_and_lifecycle_reset(retaining_pool, monkeypatch):
    pool, _ = retaining_pool
    snapshot = base.pressure_stats()
    snapshot["connections_opened"] = -1
    assert base.pressure_stats()["connections_opened"] == 2
    pool.closeall()
    monkeypatch.setattr(base, "_pg_pool", None)
    assert base.pressure_stats() is None
    replacement = base._RetainingThreadedConnectionPool(1, 8)
    monkeypatch.setattr(base, "_pg_pool", replacement)
    try:
        assert base.pressure_stats()["connections_opened"] == 1
        assert base.pressure_stats()["connections_discarded"] == 0
        assert base.pressure_stats()["acquisitions"] == 0
    finally:
        replacement.closeall()


def test_pressure_waiter_drains_on_non_pool_error(retaining_pool, monkeypatch):
    pool, _ = retaining_pool
    calls = []
    def getconn():
        calls.append(1)
        if len(calls) == 1:
            pool._notify_available()
            raise psycopg2.pool.PoolError("exhausted")
        raise psycopg2.OperationalError("connect failed")
    monkeypatch.setattr(pool, "getconn", getconn)
    with pytest.raises(psycopg2.OperationalError):
        base._get_pg_connection()
    stats = base.pressure_stats()
    assert stats["waiting"] == stats["acquisition_timeouts"] == 0
    assert stats["acquisitions"] == stats["acquisition_waits"] == 1


@pytest.fixture
def fifo_pool(retaining_pool, monkeypatch):
    pool, created = retaining_pool
    # Leave just one slot cycling, without changing production capacity.
    held = [pool.getconn() for _ in range(pool.maxconn)]
    waiting = {}
    original = pool._wait_started

    def started():
        original()
        waiting[threading.current_thread().name].set()

    monkeypatch.setattr(pool, "_wait_started", started)
    workers = []
    outcomes = []

    def launch(name):
        waiting[name] = threading.Event()
        done = threading.Event()

        def run():
            try:
                conn = base._get_pg_connection()
                outcomes.append((name, "ok"))
                base._close_connection(conn)
            except BaseException as exc:
                outcomes.append((name, exc))
            finally:
                done.set()

        worker = threading.Thread(target=run, name=name, daemon=True)
        workers.append(worker)
        worker.start()
        return waiting[name], done

    yield pool, held, launch, outcomes
    if not pool.closed:
        pool.closeall()
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()
    assert not pool._waiters
    assert pool.pressure_stats()["waiting"] == 0


def test_fifo_older_waiters_precede_fresh_arrivals(fifo_pool):
    pool, held, launch, outcomes = fifo_pool
    done = []
    for name in ("old", "second", "fresh", "freshest"):
        waiting, finished = launch(name)
        assert waiting.wait(1)
        done.append(finished)
    # All arrivals are registered before the only slot is returned.
    pool.putconn(held.pop())
    for finished in done:
        assert finished.wait(1)
    assert outcomes == [(name, "ok") for name in ("old", "second", "fresh", "freshest")]
    assert pool.pressure_stats()["waiting_peak"] == 4
    assert pool.pressure_stats()["connections_opened"] == pool.maxconn


def test_independent_polling_allows_fresh_arrival_to_bypass(retaining_pool):
    """Deterministic old-algorithm counterexample: an older sleeper loses."""
    pool, _ = retaining_pool
    held = [pool.getconn() for _ in range(pool.maxconn)]
    with pytest.raises(psycopg2.pool.PoolError):
        pool.getconn()  # old caller now sleeps independently
    pool.putconn(held.pop())
    fresh = pool.getconn()  # fresh arrival bypasses the sleeping older caller
    with pytest.raises(psycopg2.pool.PoolError):
        pool.getconn()  # older caller's retry still cannot acquire
    pool.putconn(fresh)
    for conn in held:
        pool.putconn(conn)


@pytest.mark.parametrize("release", ["normal", "discard", "forced", "shutdown"])
def test_fifo_return_and_shutdown_wake_immediately(fifo_pool, monkeypatch, release):
    pool, held, launch, outcomes = fifo_pool
    waiting, done = launch("old")
    assert waiting.wait(1)
    conn = held.pop()
    if release == "shutdown":
        pool.closeall()
    elif release == "forced":
        conn.transaction_status = psycopg2.extensions.TRANSACTION_STATUS_INTRANS
        conn.fail_rollback = True
        def fail_close():
            raise psycopg2.OperationalError("close failed")
        monkeypatch.setattr(conn, "close", fail_close)
        base._close_connection(conn)
    else:
        pool.putconn(conn, close=release == "discard")
    assert done.wait(0.5)  # far below the unchanged two-second deadline
    if release == "shutdown":
        assert isinstance(outcomes[0][1], psycopg2.pool.PoolError)
        assert "closed" in str(outcomes[0][1])
    else:
        assert outcomes == [("old", "ok")]


def test_fifo_return_between_exhaustion_and_wait_is_not_lost(fifo_pool, monkeypatch):
    pool, held, launch, outcomes = fifo_pool
    original = pool.getconn
    failed = threading.Event()
    resume = threading.Event()

    def getconn():
        try:
            return original()
        except psycopg2.pool.PoolError:
            failed.set()
            assert resume.wait(1)
            raise

    monkeypatch.setattr(pool, "getconn", getconn)
    _, done = launch("old")
    assert failed.wait(1)
    pool.putconn(held.pop())
    resume.set()
    assert done.wait(0.5)
    assert outcomes == [("old", "ok")]


@pytest.mark.parametrize("timeout_head", [True, False])
def test_fifo_timeout_removes_head_or_nonhead(fifo_pool, monkeypatch, timeout_head):
    pool, held, launch, outcomes = fifo_pool
    monkeypatch.setattr(base, "_POOL_ACQUIRE_MAX_WAIT_SECONDS", 0.1 if timeout_head else 2)
    waiting, first = launch("head")
    assert waiting.wait(1)
    monkeypatch.setattr(base, "_POOL_ACQUIRE_MAX_WAIT_SECONDS", 2 if timeout_head else 0.1)
    waiting, second = launch("tail")
    assert waiting.wait(1)
    expired, survivor = (first, second) if timeout_head else (second, first)
    assert expired.wait(1)
    assert isinstance(outcomes[0][1], psycopg2.pool.PoolError)
    assert pool.pressure_stats()["acquisition_timeouts"] == 1
    pool.putconn(held.pop())
    assert survivor.wait(0.5)
    assert outcomes[1][1] == "ok"


def test_fifo_cancellation_removes_ticket(fifo_pool, monkeypatch):
    pool, held, launch, outcomes = fifo_pool
    class Cancelled(BaseException):
        pass
    original = pool._available.wait
    cancel = threading.Event()
    def wait(timeout):
        result = original(timeout)
        if threading.current_thread().name == "cancel" and cancel.is_set():
            raise Cancelled()
        return result
    monkeypatch.setattr(pool._available, "wait", wait)
    waiting, cancelled = launch("cancel")
    assert waiting.wait(1)
    waiting, survivor = launch("survivor")
    assert waiting.wait(1)
    cancel.set()
    pool._notify_available()
    assert cancelled.wait(1)
    pool.putconn(held.pop())
    assert survivor.wait(0.5)
    assert isinstance(outcomes[0][1], Cancelled)
    assert outcomes[1] == ("survivor", "ok")


@pytest.mark.parametrize("failure", ["connect", "setup"])
def test_fifo_failure_advances_to_next_waiter(fifo_pool, monkeypatch, failure):
    pool, held, launch, outcomes = fifo_pool
    waiting, failed = launch("failed")
    assert waiting.wait(1)
    waiting, next_done = launch("next")
    assert waiting.wait(1)
    if failure == "connect":
        original = psycopg2.connect
        calls = []
        def connect(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise psycopg2.OperationalError("connect failed")
            return original(*args, **kwargs)
        monkeypatch.setattr(psycopg2, "connect", connect)
        pool.putconn(held.pop(), close=True)
    else:
        original = _PhysicalConn.__setattr__
        def set_attribute(self, name, value):
            if name == "autocommit" and threading.current_thread().name == "failed":
                raise psycopg2.OperationalError("setup failed")
            original(self, name, value)
        monkeypatch.setattr(_PhysicalConn, "__setattr__", set_attribute)
        pool.putconn(held.pop())
    assert failed.wait(1)
    assert next_done.wait(1)
    assert isinstance(outcomes[0][1], psycopg2.OperationalError)
    assert outcomes[1] == ("next", "ok")




def test_fifo_fresh_arrival_cannot_bypass_paused_head(fifo_pool, monkeypatch):
    pool, held, launch, outcomes = fifo_pool
    original = pool.getconn
    exhausted = threading.Event()
    resume = threading.Event()

    def getconn():
        try:
            return original()
        except psycopg2.pool.PoolError:
            if threading.current_thread().name == "old":
                exhausted.set()
                assert resume.wait(2)
            raise

    monkeypatch.setattr(pool, "getconn", getconn)
    _, old_done = launch("old")
    try:
        assert exhausted.wait(1)
        pool.putconn(held.pop())
        waiting, fresh_done = launch("fresh")
        # Old polling lets fresh take this available slot before old retries.
        assert waiting.wait(0.5)
        assert not fresh_done.is_set()
    finally:
        resume.set()
    assert old_done.wait(1)
    assert fresh_done.wait(1)
    assert outcomes == [("old", "ok"), ("fresh", "ok")]


def test_fifo_does_not_change_keyed_driver_calls(retaining_pool):
    pool, _ = retaining_pool
    conn = pool.getconn("legacy")
    assert pool.getconn("legacy") is conn
    pool.putconn(conn, "legacy")
    assert not pool._waiters
    assert not pool._used


# =============================================================================
# Dead-connection detection on checkout
# =============================================================================
# After a Postgres restart every retained connection is dead but still looks
# fine locally. Without a checkout check the next N requests each blow up
# inside the caller's own query, and get_db's rollback() on the dead socket
# raises again and replaces the real error with an InterfaceError.


@pytest.fixture
def one_idle_connection(retaining_pool):
    """Leave exactly one idle connection, so we know which one the next
    checkout will hand out (psycopg2 pops from the end of ``_pool``)."""
    pool, created = retaining_pool
    # Grow to the retained maximum first, then hold all but one.
    burst = [pool.getconn() for _ in range(pool.maxconn)]
    for conn in burst:
        pool.putconn(conn)
    held = [pool.getconn() for _ in range(pool.maxconn - 1)]
    assert len(pool._pool) == 1 and len(created) == pool.maxconn
    idle = pool._pool[-1]
    yield pool, created, idle, held
    if not pool.closed:
        checked_out = set(map(id, pool._used.values()))
        for conn in held:
            if id(conn) in checked_out:
                pool.putconn(conn)


@pytest.mark.parametrize("failure", ["closed", "unknown"])
def test_checkout_discards_a_dead_connection_and_retries_once(
    one_idle_connection, failure
):
    pool, created, idle, _held = one_idle_connection
    if failure == "closed":
        idle.closed = True
    else:
        idle.transaction_status = psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN

    conn = base._get_pg_connection()

    # A fresh physical connection was opened for the retry.
    assert conn._conn is not idle
    assert conn._conn is created[-1]
    assert len(created) == 9
    # Neither local check costs a round trip.
    assert idle.executed == []
    stats = base.pressure_stats()
    assert stats["connections_stale"] == 1
    assert stats["connections_discarded"] == 1
    assert idle not in pool._pool
    base._close_connection(conn)


def test_checkout_does_not_ping_a_recently_used_connection(one_idle_connection):
    pool, created, idle, _held = one_idle_connection
    conn = base._get_pg_connection()
    assert conn._conn is idle
    assert idle.executed == []  # no SELECT 1 on the hot path
    assert base.pressure_stats()["connections_stale"] == 0
    assert len(created) == 8
    base._close_connection(conn)


def test_checkout_pings_a_connection_that_sat_idle_too_long(
    one_idle_connection, monkeypatch
):
    pool, created, idle, _held = one_idle_connection
    monkeypatch.setattr(pool, "idle_seconds", lambda conn: 120.0)

    conn = base._get_pg_connection()

    assert conn._conn is idle
    assert idle.executed == ["SELECT 1"]
    assert idle.cursors_closed == 1
    assert base.pressure_stats()["connections_stale"] == 0
    assert len(created) == 8
    base._close_connection(conn)


@pytest.mark.parametrize("error", [
    psycopg2.OperationalError("server closed the connection unexpectedly"),
    psycopg2.InterfaceError("connection already closed"),
])
def test_checkout_discards_a_connection_that_fails_its_idle_ping(
    one_idle_connection, monkeypatch, error
):
    pool, created, idle, _held = one_idle_connection
    monkeypatch.setattr(pool, "idle_seconds", lambda conn: 120.0)
    idle.ping_error = error

    conn = base._get_pg_connection()

    assert conn._conn is not idle
    assert idle.executed == ["SELECT 1"]
    assert idle.closed
    assert idle not in pool._pool
    stats = base.pressure_stats()
    assert stats["connections_stale"] == 1
    assert stats["connections_discarded"] == 1
    assert len(created) == 9
    base._close_connection(conn)


def test_checkout_gives_up_after_one_retry(retaining_pool, monkeypatch):
    # Every connection in the world is dead: report that honestly rather than
    # handing the caller a connection that cannot run its query.
    pool, created = retaining_pool
    monkeypatch.setattr(base, "_connection_is_live", lambda pool, conn: False)

    with pytest.raises(psycopg2.OperationalError, match="dead connection twice"):
        base._get_pg_connection()

    assert base.pressure_stats()["connections_stale"] == 2
    assert not pool._used and not pool._rused


def test_ping_threshold_is_measured_from_the_last_return(
    retaining_pool, monkeypatch
):
    pool, _ = retaining_pool
    conn = pool.getconn()
    pool.putconn(conn)
    returned_at = time.monotonic()
    monkeypatch.setattr(base.time, "monotonic", lambda: returned_at + 300.0)
    assert pool.idle_seconds(conn) == pytest.approx(300.0, abs=1.0)


def test_a_discarded_connection_leaves_no_idle_bookkeeping(retaining_pool):
    pool, _ = retaining_pool
    conn = pool.getconn()
    pool.putconn(conn, close=True)
    assert id(conn) not in pool._last_returned
    assert pool.idle_seconds(conn) == 0.0


# =============================================================================
# Guarded rollback in get_db()/transaction()
# =============================================================================

@pytest.fixture
def unshimmed(monkeypatch):
    """conftest's autouse shim points base.get_connection at SQLite; these
    tests need the real pooled path."""
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)


@pytest.mark.parametrize("scope", ["get_db", "transaction"])
def test_failed_rollback_does_not_replace_the_original_exception(
    retaining_pool, unshimmed, scope
):
    pool, _ = retaining_pool
    manager = base.get_db if scope == "get_db" else base.transaction

    with pytest.raises(ValueError, match="the real failure"):
        with manager() as conn:
            conn._conn.transaction_status = \
                psycopg2.extensions.TRANSACTION_STATUS_INTRANS
            conn._conn.fail_rollback = True
            raise ValueError("the real failure")

    # The slot is still reclaimed even though both the rollback and the
    # pool's own return-path rollback raised.
    assert not pool._used and not pool._rused
    assert base.pressure_stats()["connections_discarded"] == 1


@pytest.mark.parametrize("scope", ["get_db", "transaction"])
def test_successful_rollback_still_returns_the_connection(
    retaining_pool, unshimmed, scope
):
    pool, created = retaining_pool
    manager = base.get_db if scope == "get_db" else base.transaction

    with pytest.raises(ValueError):
        with manager() as conn:
            physical = conn._conn
            physical.transaction_status = \
                psycopg2.extensions.TRANSACTION_STATUS_INTRANS
            raise ValueError("boom")

    assert physical.rollbacks == 1  # rolled back once, not once per layer
    assert physical in pool._pool
    assert not physical.closed
    assert base.pressure_stats()["connections_discarded"] == 0

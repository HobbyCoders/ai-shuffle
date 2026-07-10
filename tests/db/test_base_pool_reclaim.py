"""Regression tests for connection-pool slot reclamation in app.db.base.

Covers the leak fixed in ``_close_connection``: psycopg2's
ThreadedConnectionPool.putconn() rolls back the connection *before* it
removes the slot from ``_used``/``_rused``. When that rollback raises (the
server dropped the connection), the slot was leaked permanently and the old
bare ``except: pass`` hid it. ``_close_connection`` must now force the slot
free so the pool cannot exhaust itself with phantom "in use" connections.
"""

import threading
import time

import psycopg2
import psycopg2.pool
import pytest

import app.db.base as base


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


class _ExhaustedThenFreePool:
    """getconn raises PoolError until ``release()`` is called, then returns a
    connection — mimics another worker returning a slot while a caller is
    backpressuring on an exhausted pool.
    """

    minconn = 2
    maxconn = 32

    def __init__(self):
        self._free = threading.Event()
        self._used = {1: "a"}
        self._pool = []
        self.attempts = 0
        self.conn = _FakeConn()

    def release(self):
        self._free.set()

    def getconn(self):
        self.attempts += 1
        if not self._free.is_set():
            raise psycopg2.pool.PoolError("connection pool exhausted")
        return self.conn


class _AlwaysExhaustedPool:
    """getconn always raises PoolError — the pool never frees a slot."""

    minconn = 2
    maxconn = 32

    def __init__(self):
        self._used = {1: "a"}
        self._pool = []
        self.attempts = 0

    def getconn(self):
        self.attempts += 1
        raise psycopg2.pool.PoolError("connection pool exhausted")


def test_get_connection_waits_then_succeeds_when_slot_freed(monkeypatch):
    # A concurrent caller must WAIT on an exhausted pool and then succeed once a
    # slot is returned, instead of getconn's immediate PoolError becoming a 500.
    pool = _ExhaustedThenFreePool()
    monkeypatch.setattr(base, "_pg_pool", pool)

    def _free_soon():
        time.sleep(0.05)
        pool.release()

    freer = threading.Thread(target=_free_soon)
    freer.start()
    try:
        start = time.monotonic()
        wrapped = base._get_pg_connection()
        elapsed = time.monotonic() - start
    finally:
        freer.join()

    assert wrapped._conn is pool.conn
    assert pool.attempts >= 2  # retried rather than failing on first exhaustion
    assert elapsed >= 0.04     # actually backpressured until the slot freed


def test_get_connection_raises_poolerror_after_bounded_wait(monkeypatch):
    # When no slot ever frees, the wait is bounded: re-raise PoolError rather
    # than blocking forever.
    pool = _AlwaysExhaustedPool()
    monkeypatch.setattr(base, "_pg_pool", pool)
    # Shrink the bounded window so the test stays fast.
    monkeypatch.setattr(base, "_POOL_ACQUIRE_MAX_WAIT_SECONDS", 0.1)

    start = time.monotonic()
    with pytest.raises(psycopg2.pool.PoolError):
        base._get_pg_connection()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.09  # waited out the bounded window before giving up
    assert elapsed < 1.0    # but did not block unboundedly
    assert pool.attempts >= 2  # retried before re-raising


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

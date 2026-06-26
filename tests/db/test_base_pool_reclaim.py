"""Regression tests for connection-pool slot reclamation in app.db.base.

Covers the leak fixed in ``_close_connection``: psycopg2's
ThreadedConnectionPool.putconn() rolls back the connection *before* it
removes the slot from ``_used``/``_rused``. When that rollback raises (the
server dropped the connection), the slot was leaked permanently and the old
bare ``except: pass`` hid it. ``_close_connection`` must now force the slot
free so the pool cannot exhaust itself with phantom "in use" connections.
"""

import threading

import psycopg2

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

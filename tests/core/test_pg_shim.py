"""
Regression tests for the ``ShimConnection`` commit/rollback contract.

The test harness runs the PostgreSQL-only app against one shared in-memory
SQLite connection (``check_same_thread=False``). Starlette's
``BaseHTTPMiddleware`` runs the downstream app inside an anyio task group, so a
read-only ``get_db()`` body — which still calls ``conn.commit()`` on exit — can
reach commit *after* another interleaved task already ended the connection's
(auto-)transaction. SQLite then raises
``OperationalError: cannot commit - no transaction is active`` and the request
500s, flaking CI non-deterministically (observed on tests/api/test_analytics).

psycopg2 — and the real ``_PgDictConnection`` the app uses in production —
treat commit/rollback on an idle connection as a harmless no-op. The shim must
emulate that: swallow ONLY the idle-transaction error, and let every real
failure through.
"""

import sqlite3

import pytest

from tests.pg_shim import ShimConnection, new_sqlite_connection


class _FakeRaw:
    """Minimal stand-in for a sqlite3 connection whose commit/rollback raise."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def commit(self):
        self.calls += 1
        raise self._exc

    def rollback(self):
        self.calls += 1
        raise self._exc


_IDLE_COMMIT = sqlite3.OperationalError("cannot commit - no transaction is active")
_IDLE_ROLLBACK = sqlite3.OperationalError("cannot rollback - no transaction is active")
_REAL_ERROR = sqlite3.OperationalError("database is locked")


def test_commit_swallows_idle_transaction_error():
    """commit() on an idle connection is a no-op (matches psycopg2), not a raise."""
    raw = _FakeRaw(_IDLE_COMMIT)
    conn = ShimConnection(raw)
    assert conn.commit() is None
    assert raw.calls == 1  # the underlying commit WAS attempted


def test_rollback_swallows_idle_transaction_error():
    raw = _FakeRaw(_IDLE_ROLLBACK)
    conn = ShimConnection(raw)
    assert conn.rollback() is None
    assert raw.calls == 1


def test_commit_reraises_real_operational_error():
    """A genuine failure (locked db, disk full, …) must still propagate."""
    conn = ShimConnection(_FakeRaw(_REAL_ERROR))
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        conn.commit()


def test_rollback_reraises_real_operational_error():
    conn = ShimConnection(_FakeRaw(_REAL_ERROR))
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        conn.rollback()


def test_commit_after_readonly_body_does_not_raise_on_real_connection():
    """End-to-end shape of the flaky path: a read-only statement leaves no active
    transaction, and the get_db()-style commit-on-exit must not raise."""
    raw = new_sqlite_connection()
    conn = ShimConnection(raw)
    conn.cursor().execute("SELECT 1")  # read-only → no transaction opened
    # Previously this could surface SQLite's idle-commit error under the shared
    # connection race; now it is always a clean no-op.
    assert conn.commit() is None
    raw.close()


def test_commit_still_persists_a_real_write():
    """Guard against over-broad swallowing: a genuine pending write still commits."""
    raw = new_sqlite_connection()
    conn = ShimConnection(raw)
    cur = conn.cursor()
    cur.execute("CREATE TABLE _t (id INTEGER PRIMARY KEY, v TEXT)")
    cur.execute("INSERT INTO _t (v) VALUES (?)", ("hello",))
    conn.commit()
    check = ShimConnection(raw).cursor()
    check.execute("SELECT v FROM _t")
    assert check.fetchone() == {"v": "hello"}
    raw.close()

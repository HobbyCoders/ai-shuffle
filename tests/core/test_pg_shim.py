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
import threading
import time

import pytest

from tests.pg_shim import (
    ShimConnection,
    ShimConnectionClosedError,
    close_connection,
    new_sqlite_connection,
)


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


# =============================================================================
# issue #622: race-proof close against cross-thread statements
# =============================================================================
#
# The event_gateway LISTEN daemon can call record_error -> ShimCursor.execute
# against the shared test connection WHILE the main thread's _raw_sqlite fixture
# teardown closes it. Cross-thread use of a mid-close sqlite3 connection frees
# the C-level handle under the running statement and SEGFAULTS the interpreter
# (exit 139). close_connection serializes the close against in-flight statements
# under _TXN_LOCK and marks the connection closed so late statements raise
# cleanly instead of touching a freed handle.


def test_execute_after_close_raises_clean_not_segfault():
    """A statement issued after close_connection raises a recognizable error
    (never touches the freed sqlite handle)."""
    raw = new_sqlite_connection()
    cur = ShimConnection(raw).cursor()
    close_connection(raw)
    with pytest.raises(ShimConnectionClosedError):
        cur.execute("SELECT 1")


def test_close_is_idempotent_and_survives_never_opened():
    """close_connection tolerates a double close without raising or deadlocking."""
    raw = new_sqlite_connection()
    close_connection(raw)
    close_connection(raw)  # second close is a clean no-op


def test_commit_rollback_after_close_are_noops():
    """commit/rollback on a closed shim connection no-op instead of touching the
    freed handle (matches psycopg2's tolerance of a gone connection)."""
    raw = new_sqlite_connection()
    conn = ShimConnection(raw)
    close_connection(raw)
    assert conn.commit() is None
    assert conn.rollback() is None


def test_concurrent_execute_vs_close_no_crash_no_deadlock():
    """Reproduce the #622 race shape deterministically: one thread loops shim
    statements while the main thread closes the connection. The worker must see
    a clean exception (no interpreter crash), and neither thread may deadlock.

    Reaching the assertions at all proves there was no segfault — a crash would
    take the whole process down."""
    raw = new_sqlite_connection()
    started = threading.Event()
    observed_clean_close = threading.Event()
    worker_error: list = []

    def hammer():
        started.set()
        while True:
            try:
                cur = ShimConnection(raw).cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
            except ShimConnectionClosedError:
                observed_clean_close.set()
                return
            except sqlite3.ProgrammingError:
                # e.g. "Cannot operate on a closed database" — also clean;
                # covers cursor creation on an already-closed connection.
                observed_clean_close.set()
                return
            except Exception as exc:  # pragma: no cover - unexpected
                worker_error.append(exc)
                return

    t = threading.Thread(target=hammer, name="shim-hammer", daemon=True)
    t.start()
    assert started.wait(timeout=1.0)
    # Let the worker spin a few iterations, then close under it.
    time.sleep(0.02)

    deadline = time.monotonic() + 2.0
    close_connection(raw)  # serialized against the in-flight statement
    t.join(timeout=2.0)

    assert not t.is_alive(), "worker deadlocked (lock not released on close)"
    assert time.monotonic() < deadline, "close/join exceeded 2s budget"
    assert not worker_error, f"unexpected worker error: {worker_error}"
    assert observed_clean_close.is_set(), "worker never observed a clean close"

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

import re
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


# =============================================================================
# issue #1860: the schema is built once per process and copied per connection
# =============================================================================


def _fresh_build() -> sqlite3.Connection:
    from tests import pg_shim

    raw = pg_shim._open_raw_connection()
    pg_shim._build_schema(raw)
    return raw


def _dump_without_timestamps(raw: sqlite3.Connection) -> list[str]:
    # Seed rows take their DEFAULT NOW() timestamp from when the schema was
    # built, and the per-process copy may have been built minutes before the
    # fresh one. Everything else in the dump must match exactly.
    stamp = re.compile(r"'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?'")
    return [stamp.sub("'<timestamp>'", line) for line in raw.iterdump()]


def test_copied_connection_is_the_database_a_fresh_build_produces():
    fresh = _fresh_build()
    copied = new_sqlite_connection()
    try:
        assert _dump_without_timestamps(copied) == _dump_without_timestamps(fresh)
        assert copied.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] > 300
        # Connection-level settings are per connection, never copied: each new
        # connection must carry them itself.
        assert copied.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert copied.row_factory is sqlite3.Row
    finally:
        close_connection(fresh)
        close_connection(copied)


def test_copied_connections_do_not_share_rows():
    first = new_sqlite_connection()
    second = new_sqlite_connection()
    try:
        cur = ShimConnection(first).cursor()
        cur.execute("INSERT INTO system_settings (key, value) VALUES (%s, %s)", ("k", "v"))
        ShimConnection(first).commit()
        assert second.execute("SELECT COUNT(*) FROM system_settings WHERE key = 'k'").fetchone()[0] == 0
        third = new_sqlite_connection()
        assert third.execute("SELECT COUNT(*) FROM system_settings WHERE key = 'k'").fetchone()[0] == 0
        close_connection(third)
    finally:
        close_connection(first)
        close_connection(second)


def test_a_patched_translation_builds_through_the_patch_and_is_not_kept(monkeypatch):
    from tests import pg_shim

    seen = []
    original = pg_shim.translate_sql

    def recording(sql):
        seen.append(sql)
        return original(sql)

    template_before = pg_shim._TEMPLATE
    monkeypatch.setattr(pg_shim, "translate_sql", recording)
    raw = new_sqlite_connection()
    try:
        assert seen, "the schema must be built through the patched translation"
        assert pg_shim._TEMPLATE is template_before
    finally:
        close_connection(raw)

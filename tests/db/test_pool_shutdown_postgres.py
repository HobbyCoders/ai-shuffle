"""Pool shutdown deadlines against a private PostgreSQL 16, never the live DB."""
from concurrent.futures import ThreadPoolExecutor
import time

import psycopg2
import pytest

from app.db import base
from tests.db import schema_lifecycle_helpers as lifecycle
from tests.db.postgres_harness import requires_real_pg

pytestmark = requires_real_pg


@pytest.fixture(scope="module")
def private_pg(tmp_path_factory):
    bindir = lifecycle.pg16_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL 16 server binaries not found")
    with lifecycle.private_postgres(bindir, tmp_path_factory.mktemp("shutdown-pg")) as descriptor:
        yield descriptor


def test_shutdown_cancels_query_before_blocking_socket_close(private_pg, caplog):
    d = private_pg
    kwargs = dict(host=d["host"], port=d["port"], user=d["user"],
                  password=d["password"], dbname=d["database"])
    pool = base._RetainingThreadedConnectionPool(1, 2, **kwargs)
    conn = pool.getconn()
    pid = conn.get_backend_pid()

    def query():
        try:
            conn.cursor().execute("SELECT pg_sleep(30)")
        except psycopg2.errors.QueryCanceled:
            return "cancelled"
        return "completed"

    try:
        with psycopg2.connect(**kwargs) as observer, ThreadPoolExecutor(max_workers=1) as executor:
            observer.autocommit = True
            future = executor.submit(query)
            try:
                deadline = time.monotonic() + 10
                while True:
                    with observer.cursor() as cursor:
                        cursor.execute("SELECT wait_event FROM pg_stat_activity WHERE pid = %s", (pid,))
                        row = cursor.fetchone()
                    if row and row[0] == "PgSleep":
                        break
                    assert time.monotonic() < deadline, "borrower never entered pg_sleep"
                    time.sleep(0.01)
                started = time.monotonic()
                pool.shutdown(timeout=0.1)
                elapsed = time.monotonic() - started
                print(f"pg_sleep(30) shutdown latency: {elapsed:.3f}s (drain budget 0.1s)")
                assert elapsed < 3, f"shutdown blocked {elapsed:.2f}s behind pg_sleep(30)"
                assert future.result(timeout=3) == "cancelled"
                assert "1 borrowed connection" in caplog.text
            finally:
                # Also makes a failing-first run safe if shutdown fails early.
                with observer.cursor() as cursor:
                    cursor.execute("SELECT pg_cancel_backend(%s)", (pid,))
    finally:
        if not pool.closed:
            conn.cancel()
            pool.closeall()
    assert pool.closed and conn.closed
    pool.putconn(conn)
    assert not pool._used and not pool._rused


class _UncancellableConnection(psycopg2.extensions.connection):
    """Keep a real libpq query's connection lock after the pool's cancel attempt."""

    def cancel(self):
        # Cancellation can fail or be ineffective; only the observer can free it.
        pass

    def rollback(self):
        if hasattr(self, "rollback_entered"):
            self.rollback_entered.set()
        return super().rollback()

    def close(self):
        try:
            return super().close()
        finally:
            if hasattr(self, "close_finished"):
                self.close_finished.set()


@pytest.mark.parametrize("operation", ["return", "shutdown"])
def test_stuck_connection_does_not_hold_driver_lock(private_pg, operation):
    import threading

    d = private_pg
    kwargs = dict(host=d["host"], port=d["port"], user=d["user"],
                  password=d["password"], dbname=d["database"])
    pool = base._RetainingThreadedConnectionPool(
        1, 3, connection_factory=_UncancellableConnection, **kwargs)
    conn = pool.getconn()
    pid = conn.get_backend_pid()
    conn.rollback_entered = threading.Event()
    conn.close_finished = threading.Event()
    query_finished = threading.Event()

    def query():
        try:
            conn.cursor().execute("SELECT pg_sleep(30)")
        except (psycopg2.Error, psycopg2.Warning):
            pass  # Cancellation by the independent observer is test teardown.
        finally:
            query_finished.set()

    with psycopg2.connect(**kwargs) as observer, ThreadPoolExecutor(max_workers=3) as executor:
        observer.autocommit = True
        query_future = executor.submit(query)
        action = checkout = None
        try:
            deadline = time.monotonic() + 5
            with observer.cursor() as probe:
                while True:
                    probe.execute("SELECT wait_event FROM pg_stat_activity WHERE pid=%s", (pid,))
                    row = probe.fetchone()
                    if row and row[0] == "PgSleep":
                        break
                    assert time.monotonic() < deadline, "query never entered pg_sleep"
                    time.sleep(0.01)
            if operation == "return":
                action = executor.submit(pool.putconn, conn)
                assert conn.rollback_entered.wait(2), "return never attempted rollback"
                # Other borrowers work even while return's rollback is stuck.
                checkout = executor.submit(pool.getconn)
                healthy = checkout.result(timeout=0.5)
                try:
                    with healthy.cursor() as cur:
                        cur.execute("SELECT 42")
                        assert cur.fetchone() == (42,)
                    healthy.rollback()
                    assert healthy is not conn
                finally:
                    pool.putconn(healthy)
                # Return/discard itself is bounded too (two 0.5s cleanup windows).
                action.result(timeout=2)
                assert not query_finished.is_set(), "test did not keep libpq stuck"
                assert id(conn) not in pool._rused
                assert conn not in pool._pool
            else:
                started = time.monotonic()
                action = executor.submit(pool.shutdown, 0.1)
                action.result(timeout=2)
                assert time.monotonic() - started < 2
                assert pool.closed
                assert not query_finished.is_set(), "shutdown waited for the query to end"
                checkout = executor.submit(pool.getconn)
                with pytest.raises(psycopg2.pool.PoolError, match="closed"):
                    checkout.result(timeout=0.5)
        finally:
            with observer.cursor() as probe:
                probe.execute("SELECT pg_cancel_backend(%s)", (pid,))
            query_future.result(timeout=5)
            if action is not None:
                action.result(timeout=5)
            if checkout is not None and checkout.done() and checkout.exception() is None:
                # Only needed if a failing-first run reached checkout then failed.
                returned = checkout.result()
                if id(returned) in pool._rused:
                    pool.putconn(returned, close=True)
            if not pool.closed:
                pool.closeall()
            else:
                pool.putconn(conn)
            assert conn.close_finished.wait(5), "retired socket cleanup did not finish"

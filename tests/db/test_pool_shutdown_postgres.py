"""Pool shutdown deadlines against a private PostgreSQL 16, never the live DB."""
from concurrent.futures import ThreadPoolExecutor
import time

import psycopg2
import pytest

from app.db import base
from tests.db import schema_lifecycle_helpers as lifecycle


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

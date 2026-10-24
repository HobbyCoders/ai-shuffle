"""Real disposable PostgreSQL contracts for #580; never use the worker database."""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import psycopg2
import pytest

from app.db import base, commit_deadline
from tests.db import schema_lifecycle_helpers as lifecycle


@pytest.fixture(scope="module")
def private_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    bindir = lifecycle.pg16_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL 16 server binaries not available")
    data: Path = tmp_path_factory.mktemp("commit-deadline-pg")
    with lifecycle.private_postgres(bindir, data) as descriptor:
        yield descriptor


@pytest.fixture
def private_pool(
    private_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> Iterator[base._RetainingThreadedConnectionPool]:
    params = {k: private_db[k] for k in ("host", "port", "user", "password")}
    params["dbname"] = private_db["database"]
    pool = base._RetainingThreadedConnectionPool(1, 4, reserved=2, **params)
    monkeypatch.setattr(base, "_pg_pool", pool)
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    try:
        yield pool
    finally:
        pool.closeall()


@pytest.mark.skipif(os.name == "nt", reason="os.get_blocking cannot inspect Windows sockets")
@pytest.mark.parametrize("default_timeout", [None, 0.0, 0.25])
@pytest.mark.parametrize("expire", [False, True])
def test_bounded_commit_preserves_libpq_socket_mode(
    private_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    default_timeout: float | None, expire: bool,
) -> None:
    conn = lifecycle.pg_connect(private_db)
    conn.autocommit = False
    # Keep a descriptor alive after bounded_commit closes an expired connection,
    # so the expiry/disarm path's shared flags can still be inspected.
    observer = socket.dup(conn.fileno())
    original_timeout = socket.getdefaulttimeout()
    modes = [os.get_blocking(observer)]
    assert modes == [False]  # libpq uses nonblocking I/O even for synchronous connections.
    monkeypatch.setattr(commit_deadline, "COMMIT_TIMEOUT_SECONDS", 0.1 if expire else 5.0)
    try:
        socket.setdefaulttimeout(default_timeout)
        if expire:
            with pytest.raises(psycopg2.OperationalError):
                with commit_deadline.bounded_commit(conn):
                    modes.append(os.get_blocking(observer))
                    conn.cursor().execute("SELECT pg_sleep(5)")
            assert conn.closed
        else:
            conn.cursor().execute("SELECT 1")
            with commit_deadline.bounded_commit(conn):
                modes.append(os.get_blocking(observer))
                conn.commit()
        modes.append(os.get_blocking(observer))
        assert modes == [False, False, False]  # Before arm, armed, and after disarm/expiry.
    finally:
        socket.setdefaulttimeout(original_timeout)
        socket.close(observer)
        conn.close()


def test_large_result_after_bounded_commit(private_db: dict[str, Any]) -> None:
    conn = lifecycle.pg_connect(private_db)
    conn.autocommit = False
    # An independent shutdown bounds even a libpq ClientRead stall after the
    # server finished the query (statement_timeout cannot interrupt that stall).
    transport = socket.socket(fileno=socket.dup(conn.fileno()))
    deadline = threading.Timer(5.0, transport.shutdown, args=(socket.SHUT_RDWR,))
    try:
        conn.cursor().execute("SELECT 1")
        with commit_deadline.bounded_commit(conn):
            conn.commit()
        deadline.start()
        with conn.cursor() as cur:
            cur.execute("SELECT repeat('x', 5000000) AS payload")
            assert len(cur.fetchone()["payload"]) == 5_000_000
    finally:
        deadline.cancel()
        if deadline.ident is not None:
            deadline.join()
        transport.close()
        conn.close()


def test_managed_server_defaults_bound_queries_and_idle_transactions(
    private_pool: base._RetainingThreadedConnectionPool,
) -> None:
    with base.get_db() as conn:
        cur = conn.cursor()
        for setting, expected in (
            ("statement_timeout", "30s"), ("lock_timeout", "5s"),
            ("idle_in_transaction_session_timeout", "30s"),
        ):
            cur.execute(f"SHOW {setting}")
            assert cur.fetchone()[setting] == expected
    started = time.monotonic()
    with pytest.raises(psycopg2.errors.QueryCanceled):
        with base.get_db() as conn:
            conn.cursor().execute("SET LOCAL statement_timeout = '100ms'")
            conn.cursor().execute("SELECT pg_sleep(5)")
    assert time.monotonic() - started < 2
    assert not private_pool._used
    with base.get_db() as conn:
        conn.cursor().execute("SELECT 1")




def test_managed_lock_and_idle_timeouts_release_connections(
    private_pool: base._RetainingThreadedConnectionPool,
    private_db: dict[str, Any],
) -> None:
    with base.get_db() as conn:
        conn.cursor().execute("CREATE TABLE locked_probe (value int)")
    blocker = lifecycle.pg_connect(private_db)
    blocker.autocommit = False
    try:
        blocker.cursor().execute("LOCK TABLE locked_probe IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            with base.get_db() as conn:
                conn.cursor().execute("SET LOCAL lock_timeout = '100ms'")
                conn.cursor().execute("SELECT * FROM locked_probe")
        assert time.monotonic() - started < 2
    finally:
        blocker.rollback()
        blocker.close()
    with pytest.raises(psycopg2.OperationalError):
        with base.get_db() as conn:
            conn.cursor().execute("SET LOCAL idle_in_transaction_session_timeout = '100ms'")
            time.sleep(0.2)
            conn.cursor().execute("SELECT 1")
    assert not private_pool._used
    with base.get_db() as conn:
        conn.cursor().execute("SELECT 1")


def test_stalled_commit_disconnects_and_reclaims_pool_slot_without_retry(
    private_pool: base._RetainingThreadedConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A deferred trigger runs inside COMMIT, not INSERT. This exercises the
    # driver's actual blocking commit, without simulating WAL/disk failure.
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE commit_probe (value int)")
        cur.execute("""CREATE FUNCTION slow_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM pg_sleep(2); RETURN NEW; END $$""")
        cur.execute("""CREATE CONSTRAINT TRIGGER slow_commit_trigger
            AFTER INSERT ON commit_probe DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION slow_commit()""")
    monkeypatch.setattr(commit_deadline, "COMMIT_TIMEOUT_SECONDS", 0.1)
    started = time.monotonic()
    with pytest.raises(psycopg2.OperationalError):
        with base.get_db() as conn:
            conn.cursor().execute("INSERT INTO commit_probe VALUES (1)")
    assert time.monotonic() - started < 1.5
    assert conn.closed
    assert not private_pool._used
    assert private_pool.pressure_stats()["connections_discarded"] == 1
    # The failed physical connection is not reused, and no transaction retry
    # can duplicate a commit whose acknowledgement was lost.
    with base.get_db() as replacement:
        replacement.cursor().execute("SELECT 1")
        assert replacement._conn is not conn._conn
    assert not commit_deadline._watchdog._pending
    assert not commit_deadline._watchdog._expired


def test_successful_commit_disarms_before_connection_reuse(
    private_pool: base._RetainingThreadedConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(commit_deadline, "COMMIT_TIMEOUT_SECONDS", 0.5)
    with base.get_db() as conn:
        conn.cursor().execute("SELECT 1")
    time.sleep(0.6)
    with base.get_db() as reused:
        reused.cursor().execute("SELECT 1")
        assert reused._conn is conn._conn
    assert not private_pool._used
    assert not commit_deadline._watchdog._pending

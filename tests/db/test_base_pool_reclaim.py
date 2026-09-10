"""Regression tests for connection retention and slot reclamation in app.db.base.

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
    with pytest.raises(psycopg2.pool.PoolError, match="closed"):
        pool.putconn(burst[-1])

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

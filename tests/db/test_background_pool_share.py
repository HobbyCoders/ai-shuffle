"""Background database work has its own share of the pool and its own counters.

Sync scans, cleanup and maintenance are marked with
``base.background_database_work``. However long they hold a connection, they
must not queue new turns (``start_admission``) or take the pool from them.
Only the physical connection is fake; the pool and its bookkeeping are real.
"""

from __future__ import annotations

import contextvars
import threading
import time

import psycopg2
import psycopg2.extensions
import psycopg2.pool
import pytest

import app.db.base as base
from app.core import start_admission
from app.core.config import settings


class _Cursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        pass

    def fetchone(self):
        return (1,)

    def close(self):
        pass


class _Conn:
    def __init__(self):
        self.closed = False
        self.info = self
        self.transaction_status = psycopg2.extensions.TRANSACTION_STATUS_IDLE
        self.autocommit = False

    def cursor(self, *args, **kwargs):
        return _Cursor(self)

    def rollback(self):
        pass

    def commit(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: _Conn())
    # conftest blocks init_pool; build the real subclass with production's shape.
    made = base._RetainingThreadedConnectionPool(1, 8, reserved=2, background=2)
    monkeypatch.setattr(base, "_pg_pool", made)
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    yield made
    if not made.closed:
        made.closeall()


@base.background_database_work
def _background(action):
    return action()


def test_background_holds_never_count_as_start_pressure(pool, monkeypatch):
    """A scan that holds a connection for a minute queues no new turn."""
    monkeypatch.setattr(settings, "db_start_sustain_seconds", 2.0)
    conn = _background(base._get_pg_connection)
    key = id(getattr(conn, "_conn", conn))
    pool._held_since[key] -= 60

    stats = pool.pressure_stats()
    assert stats["oldest_hold_seconds"] >= 60  # Still reported for diagnosis.
    assert stats["oldest_live_hold_seconds"] == 0
    assert stats["background_in_use"] == 1
    assert start_admission.snapshot()["oldest"] == 0
    assert start_admission.StartAdmission()._pressured(time.monotonic()) is False

    base._close_connection(conn)
    stats = pool.pressure_stats()
    # Its minute is not averaged into the latency that slows new turns either.
    assert (stats["holds"], stats["hold_seconds_total"]) == (0, 0.0)
    assert stats["background_holds"] == 1
    assert stats["background_hold_seconds_max"] >= 60
    assert stats["background_in_use"] == 0
    assert start_admission.StartAdmission()._pressured(time.monotonic()) is False


def test_the_same_hold_by_a_turn_still_counts(pool, monkeypatch):
    """The exemption is for marked work only; an unmarked stall still queues turns."""
    monkeypatch.setattr(settings, "db_start_sustain_seconds", 2.0)
    conn = base._get_pg_connection()
    pool._held_since[id(getattr(conn, "_conn", conn))] -= 60
    assert pool.pressure_stats()["oldest_live_hold_seconds"] >= 60
    assert start_admission.StartAdmission()._pressured(time.monotonic()) is True
    base._close_connection(conn)
    assert pool.pressure_stats()["holds"] == 1


def test_background_work_cannot_take_more_than_its_share(pool, monkeypatch):
    monkeypatch.setattr(base, "_BACKGROUND_ACQUIRE_MAX_WAIT_SECONDS", 0.05)
    held = [_background(base._get_pg_connection) for _ in range(2)]

    with pytest.raises(psycopg2.pool.PoolError, match="exhausted"):
        _background(base._get_pg_connection)

    stats = pool.pressure_stats()
    assert stats["background_acquisition_timeouts"] == 1
    assert stats["background_waiting"] == 0
    # Nothing a turn is measured by moved: it was never waiting behind this.
    assert (stats["waiting"], stats["acquisition_waits"], stats["acquisition_timeouts"]) == (0, 0, 0)
    assert start_admission.snapshot()["waiting"] == 0

    # A turn still gets a connection at once, and so do the four after it.
    turns = [base._get_pg_connection() for _ in range(4)]
    for conn in turns + held:
        base._close_connection(conn)
    assert not pool._background_held and not pool._background_waiters


def test_a_waiting_background_job_does_not_block_the_ordinary_line(pool):
    """Each class queues apart: a full background share is not ahead of a turn."""
    held = [_background(base._get_pg_connection) for _ in range(2)]
    waiting, got = threading.Event(), []
    original = pool._background_wait_started

    def started():
        original()
        waiting.set()

    pool._background_wait_started = started

    def job():
        conn = _background(base._get_pg_connection)
        got.append(conn)

    worker = threading.Thread(target=job, daemon=True)
    worker.start()
    assert waiting.wait(3)
    assert pool.pressure_stats()["background_waiting"] == 1

    turn = base._get_pg_connection()  # Not behind the waiting job.
    assert pool.pressure_stats()["waiting"] == 0
    base._close_connection(turn)

    base._close_connection(held.pop())  # A share slot frees: the job proceeds.
    worker.join(3)
    assert not worker.is_alive() and len(got) == 1
    assert pool.pressure_stats()["background_waiting"] == 0
    for conn in got + held:
        base._close_connection(conn)


def test_lease_checkouts_inside_background_work_keep_their_reserved_slots(pool, monkeypatch):
    monkeypatch.setattr(base, "_BACKGROUND_ACQUIRE_MAX_WAIT_SECONDS", 0.05)
    held = [_background(base._get_pg_connection) for _ in range(2)]

    def lease():
        token = base._lease_checkout.set(True)
        try:
            return base._get_pg_connection()
        finally:
            base._lease_checkout.reset(token)

    conn = _background(lease)
    assert pool.pressure_stats()["background_in_use"] == 2  # Not charged to the share.
    for each in held + [conn]:
        base._close_connection(each)


def test_the_mark_ends_with_the_call_and_does_not_follow_a_copied_context(pool):
    """A turn woken from inside a sync pass must not inherit the mark."""
    seen = {}

    def inside():
        seen["inside"] = base._background_checkout()
        seen["nested"] = _background(base._background_checkout)
        seen["after_nested"] = base._background_checkout()
        context = contextvars.copy_context()
        worker = threading.Thread(
            target=lambda: seen.setdefault("other_thread", context.run(base._background_checkout)))
        worker.start()
        worker.join(3)
        raise RuntimeError("failed pass")

    with pytest.raises(RuntimeError, match="failed pass"):
        _background(inside)
    assert seen == {"inside": True, "nested": True, "after_nested": True, "other_thread": False}
    assert base._background_checkout() is False

    conn = base._get_pg_connection()  # So the next checkout on this thread is ordinary.
    assert pool.pressure_stats()["background_in_use"] == 0
    base._close_connection(conn)


def test_the_share_always_leaves_ordinary_room(monkeypatch):
    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: _Conn())
    for maximum, reserved, asked, expected in ((8, 2, 100, 5), (32, 2, 8, 8), (2, 0, 5, 1), (8, 2, 0, 0)):
        made = base._RetainingThreadedConnectionPool(
            1, maximum, reserved=reserved, background=asked)
        try:
            assert made._background_limit == expected
        finally:
            made.closeall()
    assert 0 < settings.db_pool_background < settings.db_pool_max - 2


def test_async_functions_are_refused():
    async def scan():
        return None

    with pytest.raises(TypeError, match="synchronous"):
        base.background_database_work(scan)


@pytest.mark.parametrize("module, name", [
    ("app.network.engine", "scan_all"),
    ("app.network.catalog", "publish_owned"),
    ("app.db.network", "prune_ops"),
    ("app.db.network", "prune_transcript_ops"),
    ("app.db.network", "compact_memory_ops"),
    ("app.db.network", "compact_session_ops"),
    ("app.db.database", "run_database_maintenance"),
])
def test_the_long_running_jobs_found_on_the_workers_are_marked(module, name):
    """Each of these held a connection for 11 to 37 seconds on a real worker."""
    import importlib

    function = getattr(importlib.import_module(module), name)
    # Every marked function runs the one wrapper body; the original sits behind it.
    assert function.__code__ is _background.__code__
    assert function.__wrapped__.__name__ == name

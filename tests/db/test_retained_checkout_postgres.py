"""A checkout that never comes back must not stop every new turn (#1020).

These wedge a real pooled connection on a real disposable PostgreSQL the way the
incident's log points at: a descriptor number changes hands while COMMIT is on
the wire. Two numbers matter. ``fd`` is libpq's own, the one its poll waits on.
``duplicate`` is the private copy the commit deadline shuts down to wake it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest

from app.core import start_admission
from app.db import base, commit_deadline
from tests.db.test_commit_deadline_postgres import private_db, private_pool  # noqa: F401

pytestmark = pytest.mark.skipif(os.name == "nt", reason="replaces raw socket descriptors")


@pytest.fixture
def quick_gate(monkeypatch: pytest.MonkeyPatch) -> start_admission.StartAdmission:
    """The real gate on real pool counters; only its timers are shortened."""
    for key, value in {
        "sustain_seconds": 0.05, "recovery_seconds": 0.05,
        "interval_seconds": 0.01, "poll_seconds": 0.01, "wait_seconds": 8.0,
    }.items():
        monkeypatch.setattr(start_admission.settings, "db_start_" + key, value)
    monkeypatch.setattr(commit_deadline, "COMMIT_TIMEOUT_SECONDS", 0.2)
    return start_admission.StartAdmission()


class _Wedge:
    """One borrower stuck in COMMIT, on a thread, until ``release()``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The stranger is an idle socket owned by someone else: writes land in it,
        # nothing ever answers, and a wrong shutdown() of it can be observed.
        self.stranger, self.stranger_peer = socket.socketpair()
        self.armed = threading.Event()
        self.finished = threading.Event()
        self.error: BaseException | None = None
        self.conn: Any = None
        self._monkeypatch = monkeypatch

    def steal(self, *, libpq_fd: bool, duplicate: bool) -> None:
        real_arm = commit_deadline._watchdog.arm
        wedge = self

        def arm(fd: int, seconds: float) -> socket.socket:
            sock = real_arm(fd, seconds)
            if threading.current_thread().name == "wedged-borrower":
                if duplicate:
                    os.close(sock.fileno())
                if libpq_fd:
                    os.dup2(wedge.stranger.fileno(), fd)
                wedge.armed.set()
            return sock

        self._monkeypatch.setattr(commit_deadline._watchdog, "arm", arm)

    def start(self) -> None:
        def run() -> None:
            try:
                with base.get_db() as conn:
                    self.conn = conn
                    conn.cursor().execute("SELECT 1")
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                self.error = exc
            finally:
                self.finished.set()

        threading.Thread(target=run, name="wedged-borrower", daemon=True).start()
        assert self.armed.wait(5)

    def release(self) -> None:
        # libpq's poll is on the stranger: only its peer closing can wake that.
        self.stranger_peer.close()
        assert self.finished.wait(10)
        self.stranger.close()


@pytest.fixture
def wedge(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Wedge]:
    stuck = _Wedge(monkeypatch)
    yield stuck
    if not stuck.finished.is_set():
        stuck.release()


async def _admit(gate: start_admission.StartAdmission, turn: str) -> str:
    return await asyncio.wait_for(gate.wait(turn, turn, lambda: False), 10)


def _serve_healthy_reads() -> None:
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 AS one")
        assert cursor.fetchone()["one"] == 1


# ---------------------------------------------------------------------------
# Admission recovers with the checkout still gone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_turn_starts_while_one_checkout_is_retained_forever(
    private_pool, quick_gate, wedge, monkeypatch, caplog,  # noqa: F811
) -> None:
    """The incident's state: one held, others idle, no waiters, turns must still run."""
    caplog.set_level(logging.WARNING, logger="app.db")
    monkeypatch.setattr(base, "_RETAINED_HOLD_SECONDS", 1.0)
    wedge.steal(libpq_fd=True, duplicate=True)
    wedge.start()

    # While the hold can still be a stalled operation the gate keeps its protection.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not quick_gate._pressured(time.monotonic()):
        await asyncio.sleep(0.01)
    assert quick_gate._pressured(time.monotonic())

    # It stays stuck past its deadline: nothing can wake a poll on the wrong socket.
    assert await _admit(quick_gate, "turn-1") == "ready"
    assert not wedge.finished.is_set()
    await asyncio.to_thread(_serve_healthy_reads)
    pressure = base.pressure_stats()
    assert pressure["retained_holds"] == 1 and pressure["oldest_hold_seconds"] >= 1.0
    assert len(private_pool._used) == 1

    # Named once, where the process can still say who it is.
    retained = [r.getMessage() for r in caplog.records if "connection retained" in r.getMessage()]
    assert len(retained) == 1, retained
    assert "wedged-borrower" in retained[0]
    assert "test_retained_checkout_postgres.py" in retained[0]
    base.pressure_stats()
    assert len([r for r in caplog.records if "connection retained" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_a_younger_stuck_checkout_keeps_turns_queued(
    private_pool, quick_gate, monkeypatch,  # noqa: F811
) -> None:
    """Real pressure beside an old retained hold is still real pressure."""
    monkeypatch.setattr(base, "_RETAINED_HOLD_SECONDS", 0.2)
    forgotten = private_pool.getconn()
    await asyncio.sleep(0.3)
    assert start_admission.snapshot()["oldest"] == 0
    young = private_pool.getconn()
    await asyncio.sleep(0.08)
    assert start_admission.snapshot()["oldest"] >= 0.05
    assert quick_gate._pressured(time.monotonic())
    private_pool.putconn(young)
    private_pool.putconn(forgotten)


# ---------------------------------------------------------------------------
# The deadline still reaches a connection whose private duplicate was lost
# ---------------------------------------------------------------------------

def _create_slow_commit() -> None:
    # A deferred trigger runs inside COMMIT, so this is the driver's real wait.
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS retained_probe (value int)")
        cursor.execute("""CREATE OR REPLACE FUNCTION retained_slow() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(20); RETURN NEW; END $$""")
        cursor.execute("DROP TRIGGER IF EXISTS retained_slow_trigger ON retained_probe")
        cursor.execute("""CREATE CONSTRAINT TRIGGER retained_slow_trigger
            AFTER INSERT ON retained_probe DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION retained_slow()""")


def test_deadline_still_disconnects_when_its_duplicate_was_closed(
    private_pool, monkeypatch, caplog,  # noqa: F811
) -> None:
    """Shutting down only the duplicate left this commit waiting with no bound."""
    caplog.set_level(logging.WARNING, logger=commit_deadline.logger.name)
    _create_slow_commit()
    monkeypatch.setattr(commit_deadline, "COMMIT_TIMEOUT_SECONDS", 0.2)
    real_arm = commit_deadline._watchdog.arm

    def arm(fd: int, seconds: float) -> socket.socket:
        sock = real_arm(fd, seconds)
        os.close(sock.fileno())
        return sock

    monkeypatch.setattr(commit_deadline._watchdog, "arm", arm)
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            with base.get_db() as conn:
                conn.cursor().execute("INSERT INTO retained_probe VALUES (1)")
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "the commit outlived its deadline"
    assert isinstance(outcome[0], psycopg2.OperationalError)
    assert "commit deadline exceeded" in caplog.text
    assert "deadline expired" not in caplog.text  # it woke, so it reported for itself
    assert not private_pool._used


def test_deadline_does_not_shut_down_a_stranger_on_the_same_number() -> None:
    """The number was reused: shutting it down would break whoever owns it now."""
    watchdog = commit_deadline._CommitWatchdog()
    transport, transport_peer = socket.socketpair()
    stranger, stranger_peer = socket.socketpair()
    try:
        sock = watchdog.arm(transport.fileno(), 0.05)
        os.dup2(stranger.fileno(), sock.fileno())
        time.sleep(0.3)
        assert watchdog.disarm(sock) is True
        # The transport the deadline was armed for was the one disconnected...
        transport_peer.settimeout(1)
        assert transport_peer.recv(1) == b""
        # ...and the stranger still works both ways.
        stranger_peer.sendall(b"x")
        assert stranger.recv(1) == b"x"
        stranger.sendall(b"y")
        assert stranger_peer.recv(1) == b"y"
    finally:
        for item in (transport, transport_peer, stranger, stranger_peer):
            item.close()


def test_disarm_does_not_close_a_stranger_on_the_same_number() -> None:
    """Otherwise one stray close breeds another: the leak that wedges a connection."""
    watchdog = commit_deadline._CommitWatchdog()
    transport, transport_peer = socket.socketpair()
    stranger, stranger_peer = socket.socketpair()
    number = -1
    try:
        sock = watchdog.arm(transport.fileno(), 5.0)
        number = sock.fileno()
        os.dup2(stranger.fileno(), number)
        assert watchdog.disarm(sock) is False
        os.fstat(number)  # raises EBADF if the disarm closed the new owner's descriptor
    finally:
        if number >= 0:
            try:
                os.close(number)
            except OSError:
                pass
        for item in (transport, transport_peer, stranger, stranger_peer):
            item.close()


def test_a_deadline_with_a_retained_borrower_names_it_without_private_values(
    caplog, monkeypatch,
) -> None:
    caplog.set_level(logging.WARNING, logger=commit_deadline.logger.name)
    watchdog = commit_deadline._CommitWatchdog()
    transport, transport_peer = socket.socketpair()
    stranger, stranger_peer = socket.socketpair()
    private_value = "never-log-connection-parameters"  # must not appear
    try:
        sock = watchdog.arm(transport.fileno(), 0.05)
        os.dup2(stranger.fileno(), transport.fileno())
        time.sleep(0.3)
        watchdog.disarm(sock)
    finally:
        for item in (transport, transport_peer, stranger, stranger_peer):
            item.close()
    lines = [r.getMessage() for r in caplog.records if "deadline expired" in r.getMessage()]
    assert len(lines) == 1
    assert "transport=replaced" in lines[0]
    assert "test_a_deadline_with_a_retained_borrower" in lines[0]
    assert private_value not in lines[0]

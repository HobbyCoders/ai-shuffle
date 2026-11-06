"""A checkout that never comes back must not stop every new turn (#1020).

These wedge a real pooled connection on a real disposable PostgreSQL the way the
incident's log points at: a descriptor number changes hands while COMMIT is on
the wire. Two numbers matter. ``fd`` is libpq's own, the one its poll waits on.
``duplicate`` is the private copy the commit deadline shuts down to wake it.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import psycopg2
import pytest

from app.core import session_runtime, start_admission
from app.db import backend_probe, base, commit_deadline, database, history_indexing
from app.db import turns as turns_db
from app.db.schema_postgres import create_postgres_schema
from tests.core.helpers import make_pump
from tests.core.test_session_runtime import _runtime_env
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
    monkeypatch.setattr(base, "_HOLD_RECHECK_SECONDS", 0.05)
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


async def _until(condition: Callable[[], Any], seconds: float = 5.0) -> None:
    """Read the pool's stats while waiting: reading them is what asks PostgreSQL."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        base.pressure_stats()
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _peer_still_works(sock: socket.socket, peer: socket.socket) -> None:
    peer.settimeout(1)
    sock.settimeout(1)
    peer.sendall(b"x")
    assert sock.recv(1) == b"x"
    sock.sendall(b"y")
    assert peer.recv(1) == b"y"


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
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 1.0)
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
    assert "backend=gone" in retained[0]
    await asyncio.sleep(0.2)
    base.pressure_stats()
    assert len([r for r in caplog.records if "connection retained" in r.getMessage()]) == 1
    # The deadline and the checkout name the same backend, so a log can join them.
    expired = [r.getMessage() for r in caplog.records if "deadline expired" in r.getMessage()]
    pid = re.search(r"backend_pid=(\d+)", retained[0]).group(1)
    assert len(expired) == 1 and f"backend_pid={pid} " in expired[0]


@pytest.mark.asyncio
async def test_a_younger_stuck_checkout_keeps_turns_queued(
    private_pool, quick_gate, monkeypatch,  # noqa: F811
) -> None:
    """Real pressure beside an abandoned hold is still real pressure."""
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.5)
    forgotten = private_pool.getconn()  # Its thread never touches it: backend stays idle.
    await _until(lambda: base.pressure_stats()["retained_holds"] == 1)
    assert start_admission.snapshot()["oldest"] == 0
    young = private_pool.getconn()
    await asyncio.sleep(0.08)
    assert start_admission.snapshot()["oldest"] >= 0.05
    assert quick_gate._pressured(time.monotonic())
    private_pool.putconn(young)
    private_pool.putconn(forgotten)


# ---------------------------------------------------------------------------
# Age alone never releases a checkout that is still working
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_progressing_transaction_keeps_turns_queued_past_the_check_age(
    private_pool, quick_gate, monkeypatch, caplog,  # noqa: F811
) -> None:
    """statement_timeout bounds each statement: the transaction can outlive any total."""
    caplog.set_level(logging.WARNING, logger="app.db")
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.4)
    committed = threading.Event()

    def backfill() -> None:
        with base.get_db() as conn:
            cursor = conn.cursor()
            for _ in range(5):
                cursor.execute("SELECT pg_sleep(0.25)")
                time.sleep(0.05)
        committed.set()

    worker = threading.Thread(target=backfill, name="progressing-borrower", daemon=True)
    worker.start()
    await _until(lambda: "still counted" in caplog.text)
    assert not committed.is_set()
    assert base.pressure_stats()["oldest_hold_seconds"] >= 0.4
    assert await _admit(quick_gate, "turn-1") == "ready"
    assert committed.is_set(), "a turn started while the transaction was still working"
    assert base.pressure_stats()["retained_holds"] == 0
    assert "connection retained" not in caplog.text
    worker.join(5)


@pytest.mark.asyncio
async def test_one_running_statement_counts_however_old_and_reports_its_wait(
    private_pool, quick_gate, monkeypatch, caplog,  # noqa: F811
) -> None:
    caplog.set_level(logging.WARNING, logger="app.db")
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.2)
    secret = "never-log-this-statement"

    def run() -> None:
        with base.get_db() as conn:
            conn.cursor().execute(f"SELECT pg_sleep(1.0), '{secret}'")

    worker = threading.Thread(target=run, name="long-statement", daemon=True)
    worker.start()
    await _until(lambda: "still counted" in caplog.text)
    await asyncio.sleep(0.5)  # Well past the check age, and rechecked since.
    base.pressure_stats()
    assert start_admission.snapshot()["oldest"] >= 0.5
    assert base.pressure_stats()["retained_holds"] == 0
    assert "state=active wait=Timeout:PgSleep" in caplog.text
    assert "long-statement" in caplog.text and secret not in caplog.text
    worker.join(5)


@pytest.mark.asyncio
async def test_no_answer_from_postgres_keeps_the_checkout_counted(
    private_pool, quick_gate, monkeypatch,  # noqa: F811
) -> None:
    """A database too stalled to answer is exactly what admission protects."""
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.1)
    forgotten = private_pool.getconn()
    asked = threading.Event()

    def unreachable(*args: Any, **kwargs: Any) -> None:
        asked.set()
        raise backend_probe.NoAnswer("OperationalError")

    monkeypatch.setattr(private_pool._backend_probe, "read", unreachable)
    await _until(asked.is_set)
    await asyncio.sleep(0.2)
    assert base.pressure_stats()["retained_holds"] == 0
    assert start_admission.snapshot()["oldest"] >= 0.1
    assert quick_gate._pressured(time.monotonic())
    private_pool.putconn(forgotten)


@pytest.mark.asyncio
async def test_a_checkout_that_resumes_work_counts_again(
    private_pool, quick_gate, monkeypatch,  # noqa: F811
) -> None:
    """Idle is a verdict for now: only a backend that has exited is final."""
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.2)
    resumed = private_pool.getconn()
    await _until(lambda: base.pressure_stats()["retained_holds"] == 1)
    cursor = resumed.cursor()
    worker = threading.Thread(
        target=lambda: cursor.execute("SELECT pg_sleep(0.8)"), daemon=True)
    worker.start()
    await _until(lambda: base.pressure_stats()["retained_holds"] == 0)
    assert start_admission.snapshot()["oldest"] >= 0.2
    worker.join(5)
    resumed.rollback()
    private_pool.putconn(resumed)


@pytest.mark.asyncio
async def test_a_backend_that_exited_is_final_and_never_asked_about_again(
    private_pool, quick_gate, monkeypatch,  # noqa: F811
) -> None:
    """Its pid can be reused by another backend, whose work is not this checkout's."""
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.2)
    gone = private_pool.getconn()
    killer = private_pool.getconn()
    killer.autocommit = True
    killer.cursor().execute("SELECT pg_terminate_backend(%s)", (gone.info.backend_pid,))
    private_pool.putconn(killer)
    await _until(lambda: base.pressure_stats()["retained_holds"] == 1)
    asked = threading.Event()
    monkeypatch.setattr(private_pool._backend_probe, "read",
                        lambda *a, **k: asked.set() or {})
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        base.pressure_stats()
        await asyncio.sleep(0.01)
    assert not asked.is_set()
    assert base.pressure_stats()["retained_holds"] == 1
    private_pool.putconn(gone, close=True)
    assert base.pressure_stats()["retained_holds"] == 0


# ---------------------------------------------------------------------------
# A real turn on the real pool, and declared maintenance
# ---------------------------------------------------------------------------

@pytest.fixture
def schema_pool(
    private_db, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> Iterator[base._RetainingThreadedConnectionPool]:
    """Pool size as production has it, with the application's real schema."""
    params = {k: private_db[k] for k in ("host", "port", "user", "password")}
    pool = base._RetainingThreadedConnectionPool(1, 10, reserved=2, dbname=private_db["database"], **params)
    monkeypatch.setattr(base, "_pg_pool", pool)
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    monkeypatch.setattr(database, "get_connection", base._get_pg_connection)
    try:
        with base.get_db() as conn:
            create_postgres_schema(conn.cursor())
        yield pool
    finally:
        pool.closeall()


@pytest.mark.asyncio
async def test_a_producing_turn_runs_on_the_real_pool_beside_a_wedged_checkout(
    schema_pool, quick_gate, wedge, monkeypatch,
) -> None:
    """#1020 at the level it hurt: lease, turn store and events on real PostgreSQL."""
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 1.0)
    monkeypatch.setattr(start_admission, "admission", quick_gate)
    # PostgreSQL's answer waits for the test: the gate's order is then fixed, not timed.
    may_answer = threading.Event()
    probe = base._pg_pool._backend_probe
    real_read = probe.read

    def read_when_allowed(*args: Any) -> dict:
        assert may_answer.wait(15)
        return real_read(*args)

    monkeypatch.setattr(probe, "read", read_when_allowed)
    wedge.steal(libpq_fd=True, duplicate=True)
    wedge.start()
    await asyncio.sleep(0.1)
    assert quick_gate._pressured(time.monotonic())
    with _runtime_env(pump=make_pump([{"type": "done"}])):
        with base.get_db() as conn:
            conn.cursor().execute("INSERT INTO sessions (id) VALUES ('retained-pg')")
        turn = turns_db.enqueue_turn("retained-pg", "user", prompt="hello")
        running = asyncio.create_task(session_runtime.SessionRuntime(turn).execute())
        await asyncio.sleep(1.3)  # Past the check age, so only the answer can release it.
        assert not running.done(), "the gate must protect until PostgreSQL has answered"
        assert base.pressure_stats()["retained_holds"] == 0
        may_answer.set()
        assert await asyncio.wait_for(running, 15) == "completed"
    assert turns_db.get_turn(turn["id"])["state"] == "completed"
    assert base.pressure_stats()["retained_holds"] == 1
    assert not wedge.finished.is_set()


def test_declared_maintenance_does_not_hold_new_turns(
    schema_pool, monkeypatch,
) -> None:
    declared: list[Any] = []
    real = schema_pool.exempt_hold

    def exempt(conn: Any) -> None:
        real(conn)
        declared.append(base.pressure_stats()["maintenance_holds"])
        assert start_admission.snapshot()["oldest"] == 0

    monkeypatch.setattr(schema_pool, "exempt_hold", exempt)
    database.run_database_maintenance()
    assert declared == [1, 1], "ANALYZE and VACUUM each declare their checkout"
    with history_indexing._online_cursor():
        assert declared == [1, 1, 1]
    assert base.pressure_stats()["maintenance_holds"] == 0


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


def test_replacing_the_number_while_arming_cannot_aim_the_deadline_at_a_stranger(
    monkeypatch,
) -> None:
    """Identity comes from the duplicate we own, not the number that just changed hands."""
    watchdog = commit_deadline._CommitWatchdog()
    transport, transport_peer = socket.socketpair()
    stranger, stranger_peer = socket.socketpair()
    real_dup = socket.dup

    def dup_then_replace(fd: int) -> int:
        duplicate = real_dup(fd)
        if fd == transport.fileno():
            os.dup2(stranger.fileno(), fd)
        return duplicate

    monkeypatch.setattr(commit_deadline.socket, "dup", dup_then_replace)
    try:
        sock = watchdog.arm(transport.fileno(), 0.05)
        number = sock.fileno()
        real_identity = commit_deadline._identity(number)
        monkeypatch.setattr(commit_deadline.socket, "dup", real_dup)
        time.sleep(0.3)
        assert watchdog.disarm(sock) is True
        transport_peer.settimeout(1)
        assert transport_peer.recv(1) == b""  # The armed transport was the one disconnected.
        _peer_still_works(stranger, stranger_peer)
        # ...and its private duplicate was closed, not forgotten and leaked.
        assert commit_deadline._identity(number) != real_identity
    finally:
        for item in (transport, transport_peer, stranger, stranger_peer):
            item.close()


def test_a_number_replaced_before_arming_is_never_taken_for_the_transport(caplog) -> None:
    """Idle in the pool is the widest window: the duplicate itself is then of a stranger."""
    caplog.set_level(logging.WARNING, logger=commit_deadline.logger.name)
    watchdog = commit_deadline._CommitWatchdog()
    transport, transport_peer = socket.socketpair()
    stranger, stranger_peer = socket.socketpair()
    try:
        opened_with = commit_deadline._identity(transport.fileno())
        os.dup2(stranger.fileno(), transport.fileno())
        sock = watchdog.arm(transport.fileno(), 0.05)
        number = sock.fileno()
        watchdog.bind(sock, opened_with, 4242)
        time.sleep(0.3)
        assert watchdog.disarm(sock) is True
        _peer_still_works(stranger, stranger_peer)
        # The duplicate was ours to close even though it named the stranger.
        assert commit_deadline._identity(number) is None or number == stranger.fileno()
        assert "shutdown=unreachable transport=replaced" in caplog.text
        assert "backend_pid=4242" in caplog.text
    finally:
        for item in (transport, transport_peer, stranger, stranger_peer):
            item.close()


def test_replacing_the_number_during_the_fallback_cannot_reach_a_stranger(
    monkeypatch, caplog,
) -> None:
    """The fallback validates the copy it took, not the number before it took it."""
    caplog.set_level(logging.WARNING, logger=commit_deadline.logger.name)
    watchdog = commit_deadline._CommitWatchdog()
    transport, transport_peer = socket.socketpair()
    stranger, stranger_peer = socket.socketpair()
    stranger_identity = commit_deadline._identity(stranger.fileno())
    real_dup = socket.dup
    copies: list[int] = []

    def replace_then_dup(fd: int) -> int:
        if threading.current_thread().name == "db-commit-deadline":
            os.dup2(stranger.fileno(), fd)  # The number changed hands after any check of it.
            copies.append(real_dup(fd))
            return copies[-1]
        return real_dup(fd)

    monkeypatch.setattr(commit_deadline.socket, "dup", replace_then_dup)
    try:
        sock = watchdog.arm(transport.fileno(), 0.05)
        os.close(sock.fileno())  # The duplicate is lost, which sends expiry to the fallback.
        time.sleep(0.3)
        assert watchdog.disarm(sock) is True
        assert copies, "expiry never reached the fallback"
        _peer_still_works(stranger, stranger_peer)
        # The transport= label was read before the number changed hands; what counts
        # is that the copy was checked and nothing was shut down.
        assert "shutdown=unreachable" in caplog.text
        # Its copy of the stranger was closed again, not left open.
        assert commit_deadline._identity(copies[0]) != stranger_identity
    finally:
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


# ---------------------------------------------------------------------------
# Any failure of the probe means no answer, and no answer keeps the checkout counted
# ---------------------------------------------------------------------------

@pytest.fixture
def silent_server() -> Iterator[int]:
    """Accepts TCP and never says a word: what a dead network looks like from here."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        yield server.getsockname()[1]


@pytest.mark.asyncio
async def test_a_probe_that_is_killed_keeps_the_checkout_counted_and_the_next_one_recovers(
    private_pool, quick_gate, silent_server, monkeypatch,  # noqa: F811
) -> None:
    """The real child, really killed at its deadline, then a real answer."""
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.1)
    monkeypatch.setattr(base, "_HOLD_PROBE_SECONDS", 0.5)
    monkeypatch.setattr(base, "_HOLD_RECHECK_SECONDS", 0.0)
    forgotten = private_pool.getconn()
    real = private_pool._kwargs
    private_pool._kwargs = {**real, "host": "127.0.0.1", "hostaddr": "127.0.0.1",
                            "port": silent_server}
    await _until(lambda: base.pressure_stats() and private_pool._hold_check_running)
    await _until(lambda: private_pool._backend_probe._child is not None)
    child = private_pool._backend_probe._child
    await _until(lambda: child.returncode is not None, 3)
    assert child.returncode != 0, "the deadline, not the child, must have ended it"
    assert base.pressure_stats()["retained_holds"] == 0
    assert quick_gate._pressured(time.monotonic()), "no answer must not release anything"
    private_pool._kwargs = real
    await _until(lambda: base.pressure_stats()["retained_holds"] == 1)
    private_pool.putconn(forgotten)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["thread", "process"])
async def test_a_probe_that_cannot_start_is_no_answer_and_backs_off(
    private_pool, quick_gate, monkeypatch, failure,  # noqa: F811
) -> None:
    """A working borrower must not keep an earlier idle answer because nothing could ask."""
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.1)
    resumed = private_pool.getconn()
    await _until(lambda: base.pressure_stats()["retained_holds"] == 1)
    monkeypatch.setattr(base, "_HOLD_RECHECK_SECONDS", 60.0)
    attempts: list[int] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        attempts.append(1)
        raise (RuntimeError if failure == "thread" else OSError)("cannot start")

    private_pool._next_hold_check = 0.0
    with monkeypatch.context() as broken:
        if failure == "thread":
            broken.setattr(base.threading.Thread, "start", refuse)
        else:
            broken.setattr(subprocess, "Popen", refuse)
        await _until(lambda: base.pressure_stats() and attempts)
        await _until(lambda: not private_pool._hold_check_running)
        assert not private_pool._holds[id(resumed)].silent(time.monotonic())
        for _ in range(20):
            base.pressure_stats()
            await asyncio.sleep(0.01)
        assert len(attempts) == 1, "a failure to start must wait for the next check"
        assert private_pool._next_hold_check > time.monotonic()
    assert quick_gate._pressured(time.monotonic())
    private_pool.putconn(resumed)


@pytest.mark.asyncio
async def test_a_failed_thread_start_holds_the_turn_in_that_same_admission_call(
    private_pool, quick_gate, monkeypatch,  # noqa: F811
) -> None:
    """A real resumed query with an old idle answer: the failure must not admit from it."""
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.1)
    resumed = private_pool.getconn()
    await _until(lambda: base.pressure_stats()["retained_holds"] == 1)
    monkeypatch.setattr(base, "_HOLD_RECHECK_SECONDS", 60.0)
    await _until(lambda: not private_pool._hold_check_running)
    cursor = resumed.cursor()
    worker = threading.Thread(target=lambda: cursor.execute("SELECT pg_sleep(1.5)"), daemon=True)
    worker.start()
    attempts: list[int] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        attempts.append(1)
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(start_admission.settings, "db_start_wait_seconds", 0.2)
    private_pool._next_hold_check = 0.0
    with monkeypatch.context() as broken:
        broken.setattr(base.threading.Thread, "start", refuse)
        assert private_pool._holds[id(resumed)].silent(time.monotonic()), "old idle answer"
        assert await _admit(quick_gate, "t-launch-failed") == "deferred"
    assert len(attempts) == 1, "the fifteen-second backoff still holds"
    assert private_pool._next_hold_check > time.monotonic()
    assert worker.is_alive(), "the query was still running through that call"
    worker.join(5)
    resumed.rollback()
    private_pool.putconn(resumed)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ['{"rows": {}}', '{"rows": ""}', "null identity"])
async def test_a_malformed_reply_does_not_release_a_working_checkout(
    private_pool, quick_gate, monkeypatch, reply, tmp_path,  # noqa: F811
) -> None:
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.1)
    script = tmp_path / "child.py"
    working = private_pool.getconn()
    if reply == "null identity":
        reply = '{"rows": [[%d, "active", null, null, null, 0]]}' % working.get_backend_pid()
    script.write_text(f"import sys\nwith open(sys.argv[3], 'r+b') as f: f.write({reply!r}.encode())\n")
    monkeypatch.setattr(backend_probe, "_CHILD", script)
    cursor = working.cursor()
    worker = threading.Thread(target=lambda: cursor.execute("SELECT pg_sleep(1.0)"), daemon=True)
    worker.start()
    asked: list[int] = []
    real_read = private_pool._backend_probe.read

    def read(*args: Any) -> dict:
        asked.append(1)
        return real_read(*args)

    monkeypatch.setattr(private_pool._backend_probe, "read", read)
    await _until(lambda: len(asked) >= 2)
    assert worker.is_alive(), "the statement was still running throughout"
    assert not private_pool._holds[id(working)].exited
    assert base.pressure_stats()["retained_holds"] == 0
    assert quick_gate._pressured(time.monotonic())
    worker.join(5)
    working.rollback()
    private_pool.putconn(working)


def test_shutting_down_kills_a_running_probe_and_starts_no_more(
    private_db, silent_server,  # noqa: F811
) -> None:
    params = {k: private_db[k] for k in ("host", "port", "user", "password")}
    pool = base._RetainingThreadedConnectionPool(
        1, 3, reserved=1, dbname=private_db["database"], **params)
    probe = pool._backend_probe
    worker = threading.Thread(target=lambda: _expect_no_answer(probe, silent_server), daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while probe._child is None and time.monotonic() < deadline:
        time.sleep(0.01)
    child = probe._child
    pool.shutdown(timeout=1)
    worker.join(3)
    assert not worker.is_alive() and child.returncode is not None
    with pytest.raises(backend_probe.NoAnswer):
        probe.read([1], (), {}, 1)


def _expect_no_answer(probe: backend_probe.BackendProbe, port: int) -> None:
    with pytest.raises(Exception):
        probe.read([1], (), {"host": "127.0.0.1", "hostaddr": "127.0.0.1", "port": port,
                             "connect_timeout": 30}, 30)


def test_the_probe_reads_real_backends_through_its_own_process(
    private_pool,  # noqa: F811
) -> None:
    held = private_pool.getconn()
    pid = held.info.backend_pid
    kwargs = {k: v for k, v in private_pool._kwargs.items() if k != "application_name"}
    answer = private_pool._backend_probe.read([pid], private_pool._args, kwargs, 15)
    assert answer[pid]["tag"].startswith("ai-shuffle-pool-")
    assert answer[pid]["state"] == "idle"
    private_pool.putconn(held)


# ---------------------------------------------------------------------------
# An idle answer only lasts while it is fresh; only an exit is final
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_failed_recheck_ends_an_idle_answer_for_a_checkout_that_resumed(
    private_pool, quick_gate, monkeypatch,  # noqa: F811
) -> None:
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.2)
    resumed = private_pool.getconn()
    await _until(lambda: base.pressure_stats()["retained_holds"] == 1)

    def unreachable(*args: Any, **kwargs: Any) -> None:
        raise backend_probe.NoAnswer("OperationalError")

    monkeypatch.setattr(private_pool._backend_probe, "read", unreachable)
    cursor = resumed.cursor()
    worker = threading.Thread(target=lambda: cursor.execute("SELECT pg_sleep(0.8)"), daemon=True)
    worker.start()
    await _until(lambda: base.pressure_stats()["retained_holds"] == 0)
    assert worker.is_alive(), "the statement was still running when it counted again"
    assert start_admission.snapshot()["oldest"] >= 0.2
    assert quick_gate._pressured(time.monotonic())
    worker.join(5)
    resumed.rollback()
    private_pool.putconn(resumed)


@pytest.mark.asyncio
async def test_a_stalled_recheck_lets_the_idle_answer_lapse_without_a_replacement_probe(
    private_pool, quick_gate, monkeypatch,  # noqa: F811
) -> None:
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.1)
    monkeypatch.setattr(base, "_HOLD_ANSWER_SECONDS", 0.4)
    forgotten = private_pool.getconn()
    await _until(lambda: base.pressure_stats()["retained_holds"] == 1)
    release = threading.Event()
    calls: list[int] = []

    def stalled(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        assert release.wait(10)
        raise psycopg2.OperationalError("gave up")

    monkeypatch.setattr(private_pool._backend_probe, "read", stalled)
    await _until(lambda: base.pressure_stats()["retained_holds"] == 0)
    assert start_admission.snapshot()["oldest"] >= 0.1
    assert private_pool._hold_check_running and len(calls) == 1
    release.set()
    await _until(lambda: not private_pool._hold_check_running)
    private_pool.putconn(forgotten)


@pytest.mark.asyncio
async def test_a_confirmed_exit_stays_final_when_later_probes_fail(
    private_pool, quick_gate, monkeypatch,  # noqa: F811
) -> None:
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.1)
    gone = private_pool.getconn()
    private_pool._holds[id(gone)].exited = True
    young = private_pool.getconn()
    private_pool._holds[id(young)].idle_until = time.monotonic() + 60
    monkeypatch.setattr(private_pool._backend_probe, "read", lambda *a: 1 / 0)
    private_pool._check_holds({})  # What every failed probe does, whichever checkout it was for.
    private_pool._check_holds({id(young): private_pool._holds[id(young)]})
    assert private_pool._holds[id(gone)].exited
    assert private_pool._holds[id(young)].idle_until == 0.0
    private_pool.putconn(young)
    private_pool.putconn(gone, close=True)


# ---------------------------------------------------------------------------
# A reused pid is not the same backend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_pid_reused_before_any_absence_is_seen_is_not_charged_to_the_dead_checkout(
    private_pool, quick_gate, monkeypatch, caplog,  # noqa: F811
) -> None:
    """A real busy backend's real row, reported under the dead pid: the OS cannot be forced.

    Every probe sees the pid present, so no absence is ever observed. Its work must
    not keep this checkout, and with it every new turn, counted.
    """
    caplog.set_level(logging.WARNING, logger="app.db")
    monkeypatch.setattr(base, "_HOLD_CHECK_SECONDS", 0.2)
    dead = private_pool.getconn()
    dead_pid = dead.info.backend_pid
    busy = private_pool.getconn()
    busy_pid = busy.info.backend_pid
    cursor = busy.cursor()
    worker = threading.Thread(target=lambda: cursor.execute("SELECT pg_sleep(1.0)"), daemon=True)
    real_read = private_pool._backend_probe.read
    answers: list[dict] = []

    def read(pids: list[int], args: Any, kwargs: Any, seconds: float) -> dict:
        rows = real_read([busy_pid], args, kwargs, seconds)
        answers.append(rows[busy_pid])
        return {dead_pid: rows[busy_pid], busy_pid: rows[busy_pid]}

    monkeypatch.setattr(private_pool._backend_probe, "read", read)
    worker.start()
    await _until(lambda: private_pool._holds[id(dead)].exited)
    assert answers[0]["state"] == "active"
    assert "backend=gone" in caplog.text
    # The real owner of that row is still its own checkout's work.
    assert not private_pool._holds[id(busy)].silent(time.monotonic())
    worker.join(5)
    busy.rollback()
    private_pool.putconn(busy)
    private_pool.putconn(dead, close=True)


# ---------------------------------------------------------------------------
# A commit whose number is gone fails like any lost connection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("allocate_on_failure", [False, True])
def test_a_commit_whose_number_was_closed_fails_as_a_lost_connection(
    private_pool, monkeypatch, allocate_on_failure,  # noqa: F811
) -> None:
    """Callers handle psycopg2 errors; a bare EBADF from arming was none of those."""
    real_arm = commit_deadline._watchdog.arm
    stranger: socket.socket | None = None
    stranger_identity: tuple[int, int] | None = None

    def arm(fd: int, seconds: float) -> socket.socket:
        nonlocal stranger, stranger_identity
        try:
            return real_arm(fd, seconds)
        except OSError:
            if allocate_on_failure and stranger is None:
                # Force the allocator's competing open at the failure boundary.
                # A raw close of libpq's fd lets it steal and later close this socket.
                stranger = socket.socket()
                stranger_identity = commit_deadline._identity(stranger.fileno())
                assert stranger_identity is not None
            raise

    monkeypatch.setattr(commit_deadline._watchdog, "arm", arm)
    real_dup = socket.dup
    number = -1

    def lost_number(fd: int) -> int:
        if fd == number:
            # Inject the actual arming failure without freeing libpq's number.
            # A raw os.close leaves libpq owning a stale number: its later close
            # can hit an unrelated allocation, which can then hit the next read.
            raise OSError(errno.EBADF, "connection socket was closed")
        return real_dup(fd)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(commit_deadline.socket, "dup", lost_number)
            with pytest.raises(psycopg2.OperationalError, match="statement not sent") as error:
                with base.get_db() as conn:
                    conn.cursor().execute("SELECT 1")
                    number = conn.fileno()
            assert isinstance(error.value.__cause__, OSError)
            assert error.value.__cause__.errno == errno.EBADF
        assert not private_pool._used
        _serve_healthy_reads()
        if allocate_on_failure:
            assert stranger is not None
            assert commit_deadline._identity(stranger.fileno()) == stranger_identity
    finally:
        if stranger is not None:
            if commit_deadline._identity(stranger.fileno()) == stranger_identity:
                stranger.close()
            else:
                # A failed regression must not close the next pooled connection.
                stranger.detach()

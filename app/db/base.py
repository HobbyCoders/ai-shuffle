"""
Shared database utilities — connection management, row conversion, constants.

Uses PostgreSQL via psycopg2 with a threaded connection pool.

All domain modules import from here. Do NOT import domain modules from this file.
"""

import json
import re
import uuid
from collections import deque
from dataclasses import dataclass
import threading
import time
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import psycopg2
import psycopg2.pool
import psycopg2.extras
import psycopg2.extensions

from app.core.config import settings
from app.db.backend_probe import BackendProbe
from app.db.commit_deadline import bounded_commit, owner_frames, remember_transport

logger = logging.getLogger("app.db")

# ---------------------------------------------------------------------------
# Connection pool (initialized once via init_pool at startup)
# ---------------------------------------------------------------------------
_pg_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None

# Regex for valid SQL identifiers: alphanumeric + underscores, must start with letter/underscore.
# This prevents SQL injection in DDL statements where parameterization is not supported.
_VALID_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(value: str, kind: str = "identifier") -> str:
    """Validate that a string is a safe SQL identifier (table name, column name).

    Args:
        value: The identifier to validate.
        kind: Human-readable label for error messages.

    Returns:
        The validated identifier string.

    Raises:
        ValueError: If the identifier contains invalid characters.
    """
    if not value or not _VALID_IDENTIFIER_RE.match(value):
        raise ValueError(
            f"Invalid SQL {kind}: {value!r}. "
            f"Identifiers must match [a-zA-Z_][a-zA-Z0-9_]*"
        )
    return value


# =============================================================================
# Worktree Status Constants
# =============================================================================
class WorktreeStatus:
    """Worktree status values — use these constants for consistency."""
    ACTIVE = "active"
    COMPLETED = "completed"  # Worktree cleaned up after merge; DB record preserved for session reopen
    DELETED = "deleted"
    ORPHANED = "orphaned"


# =============================================================================
# Pool management
# =============================================================================

def parse_database_url(db_url: str) -> Dict[str, Any]:
    """Decompose a PostgreSQL URL into ``psycopg2.connect`` keyword args.

    psycopg2/libpq cannot reliably parse raw DSNs: percent-encoded passwords
    stay encoded, and a literal ``@`` in the password makes libpq treat part
    of it as the hostname. Delegates to the canonical parser in
    app.core.config (which needs it at settings-load time, before this module
    can be imported). Every direct PG connection (pool or dedicated, e.g. the
    event-gateway LISTEN connection) must connect through this instead of
    passing the raw URL.
    """
    from app.core.config import _parse_legacy_database_url

    return _parse_legacy_database_url(db_url)


@dataclass
class _Hold:
    """One checkout and what is known about it. See _HOLD_CHECK_SECONDS.

    Three things stop it counting as pressure, and nothing else does: PostgreSQL
    confirmed its backend exited (final), PostgreSQL recently said it is idle (only
    until ``idle_until``), or it was declared maintenance.
    """
    owner: tuple
    backend: Optional[tuple]  # (pid, tag); None for doubles, which are never asked about.
    maintenance: bool = False
    exited: bool = False
    idle_until: float = 0.0
    reported: Optional[bool] = None  # The last verdict logged, so each is logged once.

    def silent(self, now: float) -> bool:
        return self.exited or now < self.idle_until

    def exempt(self, now: float) -> bool:
        return self.maintenance or self.silent(now)


class _RetainingThreadedConnectionPool(psycopg2.pool.ThreadedConnectionPool):
    """Open the startup minimum, then retain healthy connections up to maxconn."""

    def __init__(
        self, minconn: int, maxconn: int, *args: Any, reserved: int = 0, **kwargs: Any
    ) -> None:
        self._reserved = min(reserved, maxconn - 1)
        self._lease_waiters = deque()
        # Separate lock: snapshots and sleeping acquirers never block on libpq.
        self._available = threading.Condition()
        self._waiters = deque()
        self._generation = 0
        self._closing = False
        self._exhaustion_logged = False
        self._pressure_lock = threading.Lock()
        self._pressure = dict.fromkeys((
            "connections_opened", "connections_discarded", "connections_stale",
            "acquisitions",
            "acquisition_waits", "acquisition_timeouts", "waiting", "waiting_peak", "holds",
        ), 0)
        self._pressure.update(dict.fromkeys((
            "acquisition_seconds_total", "acquisition_seconds_max",
            "wait_seconds_total", "wait_seconds_max",
            "timeout_seconds_total", "timeout_seconds_max",
            "hold_seconds_total", "hold_seconds_max",
        ), 0.0))
        # Only live physical connections and active checkouts; bounded by maxconn.
        self._physical = set()
        self._held_since = {}
        self._holds = {}
        # Which backend each physical connection opened with.
        self._backends = {}
        self._hold_check_running = False
        self._next_hold_check = 0.0
        self._backend_probe = BackendProbe()
        # When each connection last became idle in the pool. Read on checkout to
        # decide whether the socket is old enough to be worth probing; see
        # _get_pg_connection. Entries are dropped when a connection is discarded.
        self._last_returned = {}
        super().__init__(minconn, maxconn, *args, **kwargs)
        self.startup_minconn = self.minconn
        # psycopg2 also uses minconn as its idle-retention limit in _putconn.
        # Raise it only AFTER construction so we don't eagerly open maxconn.
        # Inherit the driver's locking, rollback/discard and shutdown paths.
        self.minconn = self.maxconn


    def _notify_available(self):
        with self._available:
            self._generation += 1
            self._available.notify_all()

    @contextmanager
    def _acquire(self) -> Iterator[psycopg2.extensions.connection]:
        """FIFO admission; direct driver calls retain their keyed semantics.

        Never enter the driver under _available: returns notify under the
        driver's lock. The generation closes the failed-getconn/wait race.
        """
        start = time.monotonic()
        wait_seconds = _pool_acquire_wait.get()
        if wait_seconds is None:
            wait_seconds = _POOL_ACQUIRE_MAX_WAIT_SECONDS
        deadline = start + wait_seconds
        ticket = object()
        lease = _lease_checkout.get()
        waiters = self._lease_waiters if lease else self._waiters
        waited = timed_out = False
        with self._available:
            waiters.append(ticket)
        try:
            while True:
                with self._available:
                    if self.closed or self._closing:
                        raise psycopg2.pool.PoolError("connection pool is closed")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        raise psycopg2.pool.PoolError("connection pool exhausted")
                    if waiters[0] is not ticket:
                        if not waited:
                            waited = True
                            self._wait_started()
                        self._available.wait(remaining)
                        continue
                    generation = self._generation
                try:
                    conn = self.getconn()
                    if not lease:
                        with self._available:
                            self._exhaustion_logged = False
                    break
                except psycopg2.pool.PoolError:
                    with self._available:
                        if self.closed or self._closing:
                            raise
                        if not waited:
                            waited = True
                            self._wait_started()
                        if generation == self._generation:
                            self._available.wait(max(0, deadline - time.monotonic()))
            # Include setup in timing; advance the queue even if setup fails.
            yield conn
        finally:
            exhaustion = None
            with self._available:
                if timed_out and not self._exhaustion_logged:
                    self._exhaustion_logged = True
                    with self._pressure_lock:
                        in_use = len(self._held_since)
                    exhaustion = (
                        in_use, self.maxconn, len(self._waiters) + len(self._lease_waiters),
                    )
                waiters.remove(ticket)
                self._available.notify_all()
            self._acquisition_finished(time.monotonic() - start, waited, timed_out)
            if exhaustion is not None:
                logger.warning(
                    "PostgreSQL pool exhausted: in_use=%d max=%d waiters=%d "
                    "(acquisition deadline %.1fs; once until ordinary admission recovers)",
                    *exhaustion, wait_seconds, stack_info=True,
                )

    def pressure_stats(self):
        stats = self._read_pressure()
        due = self._claim_hold_check()
        if due:
            try:
                threading.Thread(target=self._check_holds, args=(due,),
                                 name="db-hold-check", daemon=True).start()
            except RuntimeError:  # Out of threads: the same as any other lack of an answer.
                with self._pressure_lock:
                    self._record_no_answer()
                stats = self._read_pressure()  # That just ended any idle answer.
        return stats

    def _read_pressure(self):
        now = time.monotonic()
        with self._pressure_lock:
            stats = dict(self._pressure)
            stats["ordinary_capacity"] = self.maxconn - self._reserved
            stats["oldest_hold_seconds"] = (
                now - min(self._held_since.values()) if self._held_since else 0.0
            )
            stats["oldest_live_hold_seconds"] = max((
                now - started for key, started in self._held_since.items()
                if not self._holds[key].exempt(now)
            ), default=0.0)
            stats["retained_holds"] = sum(h.silent(now) for h in self._holds.values())
            stats["maintenance_holds"] = sum(h.maintenance for h in self._holds.values())
        return stats

    def _claim_hold_check(self):
        """The checkouts to ask about, if a check should start now."""
        now = time.monotonic()
        with self._pressure_lock:
            if self._hold_check_running or self._closing or now < self._next_hold_check:
                return None
            due = {
                key: hold for key, hold in self._holds.items()
                if now - self._held_since[key] >= _HOLD_CHECK_SECONDS
                and hold.backend and not hold.maintenance and not hold.exited
            }
            self._hold_check_running = bool(due)
            return due

    def abandoned_holds(self):
        """Connection ids PostgreSQL shows no work for; see _HOLD_CHECK_SECONDS."""
        now = time.monotonic()
        with self._pressure_lock:
            return {key for key, hold in self._holds.items() if hold.silent(now)}

    def exempt_hold(self, conn):
        """This checkout runs long on purpose and is not a stall; ends with its return."""
        with self._pressure_lock:
            hold = self._holds.get(id(conn))
            if hold is not None:
                hold.maintenance = True

    def _check_holds(self, due):
        """Ask PostgreSQL about long checkouts; see app/db/backend_probe.py for how."""
        asked = time.monotonic()
        backends = None
        reports = []
        try:
            kwargs = {k: v for k, v in self._kwargs.items() if k != "application_name"}
            backends = self._backend_probe.read(
                [hold.backend[0] for hold in due.values()], self._args, kwargs,
                _HOLD_PROBE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - never decides anything; type only
            logger.info("Could not check long database checkouts: %s", type(exc).__name__)
        finally:
            with self._pressure_lock:
                if backends is None:
                    self._record_no_answer()
                else:
                    self._hold_check_running = False
                    self._next_hold_check = time.monotonic() + _HOLD_RECHECK_SECONDS
                    reports = self._record_answer(due, backends, asked)
        for report in reports:
            _report_long_hold(*report)

    def _record_no_answer(self):
        # Caller holds _pressure_lock. No answer means no verdict, and it also ends any
        # earlier idle answer: when the database cannot reply, that is exactly the
        # pressure admission exists for. A confirmed exit stays final.
        self._hold_check_running = False
        self._next_hold_check = time.monotonic() + _HOLD_RECHECK_SECONDS
        for hold in self._holds.values():
            hold.idle_until = 0.0

    def _record_answer(self, due, backends, asked):
        # Caller holds _pressure_lock. Returns what to log, outside it.
        reports = []
        now = time.monotonic()
        for key, hold in due.items():
            if self._holds.get(key) is not hold:
                continue  # Returned while we asked; the answer is about nobody.
            pid, tag = hold.backend
            backend = backends.get(pid)
            if backend is not None and backend["tag"] != tag:
                backend = None  # That pid is now another backend, so this one exited.
            # Final: a backend never returns. An idle answer is only as good as it is fresh.
            hold.exited = backend is None
            idle = backend is not None and _backend_shows_no_work(backend)
            hold.idle_until = asked + _HOLD_ANSWER_SECONDS if idle else 0.0
            silent = hold.exited or idle
            if hold.reported is not silent:
                hold.reported = silent
                reports.append((silent, now - self._held_since[key], hold.owner, pid, backend))
        return reports

    def _duration(self, prefix, elapsed):
        # Caller holds _pressure_lock.
        self._pressure[prefix + "_seconds_total"] += elapsed
        self._pressure[prefix + "_seconds_max"] = max(
            self._pressure[prefix + "_seconds_max"], elapsed)

    def _wait_started(self):
        with self._pressure_lock:
            self._pressure["waiting"] += 1
            self._pressure["waiting_peak"] = max(
                self._pressure["waiting_peak"], self._pressure["waiting"])
            self._pressure["acquisition_waits"] += 1

    def _acquisition_finished(self, elapsed, waited, timed_out):
        with self._pressure_lock:
            self._pressure["acquisitions"] += 1
            self._duration("acquisition", elapsed)
            if waited:
                self._pressure["waiting"] -= 1
                self._duration("wait", elapsed)
            if timed_out:
                self._pressure["acquisition_timeouts"] += 1
                self._duration("timeout", elapsed)

    def _connect(self, key=None):
        # A pid can be reused. libpq sends this name in its startup packet, so telling
        # backends apart takes no extra round trip and adds nothing that can fail here.
        # Always called under the driver's lock or from the constructor.
        tag = f"ai-shuffle-pool-{uuid.uuid4().hex[:16]}"
        self._kwargs = {**self._kwargs, "application_name": tag}
        conn = super()._connect(key)
        remember_transport(conn)
        with self._pressure_lock:
            self._physical.add(id(conn))
            self._last_returned[id(conn)] = time.monotonic()
            self._pressure["connections_opened"] += 1
            backend_pid = getattr(getattr(conn, "info", None), "backend_pid", None)
            self._backends[id(conn)] = (backend_pid, tag) if isinstance(backend_pid, int) else None
        return conn

    def _getconn(self, key=None):
        # Called under the driver lock: admission is atomic with returns.
        if self._closing:
            raise psycopg2.pool.PoolError("connection pool is closing")
        # Existing keyed checkouts do not consume another slot.
        if (self._reserved and not _lease_checkout.get()
                and key not in self._used
                and len(self._used) >= self.maxconn - self._reserved):
            raise psycopg2.pool.PoolError("connection pool exhausted")
        conn = super()._getconn(key)
        with self._pressure_lock:
            if id(conn) not in self._held_since:
                self._held_since[id(conn)] = time.monotonic()
                thread = threading.current_thread()
                self._holds[id(conn)] = _Hold(
                    (thread.ident, thread.name), self._backends.get(id(conn)))
        return conn

    def idle_seconds(self, conn) -> float:
        """How long this connection sat unused before the current checkout.

        Zero for a connection with no recorded return (freshly opened, or one
        the driver handed out through a keyed path we do not track).
        """
        with self._pressure_lock:
            since = self._last_returned.get(id(conn))
        if since is None:
            return 0.0
        return max(0.0, time.monotonic() - since)

    def stale_detected(self) -> None:
        """Count a connection that failed its checkout health check."""
        with self._pressure_lock:
            self._pressure["connections_stale"] += 1

    def _returned(self, conn, discarded):
        # Called under the driver's lock, including the force-reclaim path.
        with self._pressure_lock:
            started = self._held_since.pop(id(conn), None)
            self._holds.pop(id(conn), None)
            if started is not None:
                self._pressure["holds"] += 1
                self._duration("hold", time.monotonic() - started)
            if discarded:
                self._last_returned.pop(id(conn), None)
                self._backends.pop(id(conn), None)
                if id(conn) in self._physical:
                    self._physical.remove(id(conn))
                    self._pressure["connections_discarded"] += 1
            else:
                self._last_returned[id(conn)] = time.monotonic()

    def _putconn(self, conn, key=None, close=False):
        try:
            if self.closed and id(conn) in self._rused:
                # closeall() also closes checked-out sockets, but psycopg2
                # retains their bookkeeping and rejects their later returns.
                # A borrower finishing during shutdown must discard, not try
                # to roll back/reuse the socket or report a broken-pool error.
                # Both paths run under the driver's same lock, so closeall
                # cannot race this check or put the socket back in service.
                if not conn.closed:
                    conn.close()
                held_key = self._rused.pop(id(conn), None)
                if held_key is not None:
                    self._used.pop(held_key, None)
                return
            super()._putconn(conn, key, close)
        finally:
            try:
                if id(conn) not in self._rused:
                    self._returned(conn, conn not in self._pool)
            finally:
                self._notify_available()

    def shutdown(self, timeout: float = 3.0) -> None:
        """Stop admission and let in-flight DB threads finish before socket close.

        Cancelling an asyncio.to_thread await does not stop its running thread.
        Shutdown callers must leave the pool alive for those borrowers, but a
        stuck query must not hang process shutdown indefinitely. The ordinary
        closeall() remains an immediate force-close operation.
        """
        deadline = time.monotonic() + timeout
        with self._lock:
            self._closing = True
            self._backend_probe.close()
        self._notify_available()
        while True:
            # Match _acquire's generation protocol: never take the driver lock
            # while holding _available (returns notify in the opposite order).
            with self._available:
                generation = self._generation
            with self._lock:
                if self.closed:
                    return
                remaining = deadline - time.monotonic()
                if not self._used or remaining <= 0:
                    borrowed = len(self._used)
                    if borrowed:
                        # close() itself takes the connection lock, so closing
                        # an executing query can outlive the drain deadline.
                        # Cancel first to release that lock before socket close.
                        logger.warning("Closing PostgreSQL pool with %d borrowed connection(s) "
                                       "after %.1fs shutdown drain", borrowed, timeout)
                        for conn in self._used.values():
                            try:
                                conn.cancel()
                            except Exception:
                                # Best effort: one failed cancellation must not
                                # prevent cancelling the remaining borrowers.
                                pass
                    self._closeall()
                    break
            with self._available:
                if generation == self._generation:
                    self._available.wait(max(0, remaining))

    def _closeall(self):
        try:
            super()._closeall()
            # Discard means removed from service, even if socket close failed.
            for conn in self._pool + list(self._used.values()):
                self._returned(conn, True)
        finally:
            self._notify_available()


def init_pool(min_conn: int = 2, max_conn: int = 10) -> None:
    """Initialize the PostgreSQL connection pool.

    Must be called once during application startup.
    """
    global _pg_pool

    if _pg_pool is not None:
        logger.warning("PostgreSQL pool already initialized, closing old pool")
        _pg_pool.closeall()

    _pg_pool = _RetainingThreadedConnectionPool(
        min_conn,
        max_conn,
        reserved=2,
        **settings.get_database_connection(),
    )
    logger.info(
        f"PostgreSQL connection pool initialized (min={min_conn}, max={max_conn})"
    )


def close_pool() -> None:
    """Shutdown the PostgreSQL connection pool. Called during app shutdown."""
    global _pg_pool
    pool = _pg_pool
    if pool is not None:
        if isinstance(pool, _RetainingThreadedConnectionPool):
            pool.shutdown()
        else:
            pool.closeall()
        if _pg_pool is pool:
            _pg_pool = None
        logger.info("PostgreSQL connection pool closed")


# =============================================================================
# Connection wrapper
# =============================================================================

class _PgDictConnection:
    """Wraps a psycopg2 connection so that ``.cursor()`` returns
    a ``RealDictCursor`` by default, giving dict-style row access.
    """

    def __init__(self, conn):
        object.__setattr__(self, '_conn', conn)

    def cursor(self, *args, cursor_factory=None, **kwargs):
        if cursor_factory is None:
            cursor_factory = psycopg2.extras.RealDictCursor
        return self._conn.cursor(*args, cursor_factory=cursor_factory, **kwargs)

    def commit(self) -> None:
        with bounded_commit(self._conn):
            self._conn.commit()

    def rollback(self) -> None:
        with bounded_commit(self._conn):
            self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name == '_conn':
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)


# =============================================================================
# Connection management
# =============================================================================

# Past this age a checkout is worth asking PostgreSQL about. Age alone proves
# nothing: statement_timeout bounds each statement, not the transaction, so one
# checkout can do useful work for as long as it keeps issuing statements. Only
# the backend can say. A checkout stops counting as pressure once its backend is
# gone, or for as long as PostgreSQL keeps saying it has run nothing for this long
# while the borrower still holds it, for example a borrower polling a descriptor
# number that now names someone else's socket, which nothing can wake. A running
# statement always counts, however old.
_HOLD_CHECK_SECONDS = 60.0
_HOLD_RECHECK_SECONDS = 15.0
# An idle answer lapses after this unless a newer one renews it: the borrower can
# resume at any moment, and a probe that fails or stalls cannot say so.
_HOLD_ANSWER_SECONDS = 30.0
# The whole probe, starting its process included. It is then killed.
_HOLD_PROBE_SECONDS = 5.0


def _backend_shows_no_work(backend) -> bool:
    """The backend is idle and has been since before the check age."""
    return (backend["state"] in ("idle", "idle in transaction", "idle in transaction (aborted)")
            and backend["state_age"] >= _HOLD_CHECK_SECONDS)


def _report_long_hold(silent, age, owner, backend_pid, backend) -> None:
    """Name a long checkout once per verdict, with where its thread and backend are.

    Thread, pid, frame locations and wait state only: no SQL, parameters or locals.
    A log sink cannot stop admission.
    """
    try:
        thread_id, thread_name = owner or (0, "unknown")
        logger.warning(
            "Database connection %s %.1fs: thread=%s backend_pid=%s backend=%s frames=%s",
            "retained, not counted as pressure," if silent else "held, still counted as pressure,",
            age, thread_name, backend_pid,
            "gone" if backend is None else "state={state} wait={wait_type}:{wait} "
            "state_age={state_age:.1f}".format(**backend),
            owner_frames(thread_id),
        )
    except Exception:  # noqa: BLE001 - a diagnostic never decides whether a turn starts
        pass


def exempt_from_start_pressure(conn) -> None:
    """Declare a maintenance checkout (VACUUM, online index build) not a stall.

    It runs for minutes on purpose under its own raised limits, so holding new
    turns for its age relieves nothing. Slow interval latency still blocks starts
    if it really does load the database, and its slot still counts in utilization.
    """
    pool = _pg_pool
    if isinstance(pool, _RetainingThreadedConnectionPool):
        pool.exempt_hold(getattr(conn, "_conn", conn))


# Application callers wait in FIFO order, bounded by the existing deadline.
_POOL_ACQUIRE_MAX_WAIT_SECONDS = 2.0
# Durable turn writes must outlast short commit bursts, without unbounded waits.
# They share the ordinary FIFO and cannot consume the lease-only reservation.
_TURN_POOL_ACQUIRE_MAX_WAIT_SECONDS = 20.0
_pool_acquire_wait: ContextVar[Optional[float]] = ContextVar("db_acquire_wait", default=None)

# A connection that has sat in the pool longer than this is probed with
# SELECT 1 before it is handed out. Below the threshold we rely on the two
# free local checks only: pinging every checkout would double the round trips
# on the hot path, and a connection returned seconds ago is almost certainly
# still alive. Above it the socket has had time to be dropped by an idle NAT,
# a Postgres restart or a failover, and one round trip is cheaper than
# failing the caller's real query.
_CONNECTION_PING_IDLE_SECONDS = 60.0


def _connection_is_live(pool, conn) -> bool:
    """Is this checked-out connection still usable?

    Two local checks first — ``closed`` and the libpq transaction status are
    both read from client-side state, so they cost nothing. ``UNKNOWN`` is
    precisely what libpq reports once the server connection is gone.

    Only a connection that has been idle past the ping threshold pays for a
    round trip. A Postgres restart leaves every retained connection dead while
    all of them still look fine locally, and without the probe the next N
    requests each blow up inside the caller's own query.
    """
    try:
        if conn.closed != 0:
            return False
        if conn.info.transaction_status == \
                psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN:
            return False
    except Exception:  # noqa: BLE001 — an introspection failure is a dead conn
        return False

    idle_seconds = 0.0
    if isinstance(pool, _RetainingThreadedConnectionPool):
        idle_seconds = pool.idle_seconds(conn)
    if idle_seconds <= _CONNECTION_PING_IDLE_SECONDS:
        return True

    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT 1")
            # The probe starts a transaction on an ordinary pooled connection.
            # End it before checkout sets autocommit or the caller starts work.
            conn.rollback()
        finally:
            try:
                cursor.close()
            except Exception:
                pass
        return True
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        logger.debug(
            "pooled connection failed its idle ping after %.1fs; discarding",
            idle_seconds, exc_info=True,
        )
        return False


def _discard_connection(pool, conn) -> None:
    """Take a connection permanently out of service, freeing its slot."""
    if isinstance(pool, _RetainingThreadedConnectionPool):
        pool.stale_detected()
    try:
        pool.putconn(conn, close=True)
    except Exception:  # noqa: BLE001 — fall back to the force-reclaim ladder
        _close_connection(conn)


def _get_pg_connection() -> "_PgDictConnection":
    """Acquire with bounded FIFO backpressure and aggregate pressure metrics.

    A connection that fails its checkout health check is discarded and the
    acquire is retried once, so a Postgres restart costs one extra round trip
    rather than an error in the caller's query.
    """
    pool = _pg_pool
    if pool is None:
        raise RuntimeError(
            "PostgreSQL connection pool not initialized. Call init_pool() first."
        )
    attempts = 2
    for attempt in range(attempts):
        with pool._acquire() as conn:
            if not _connection_is_live(pool, conn):
                _discard_connection(pool, conn)
                if attempt + 1 < attempts:
                    continue
                raise psycopg2.OperationalError(
                    "could not obtain a live PostgreSQL connection: the pool "
                    "returned a dead connection twice in a row"
                )
            try:
                conn.autocommit = False
                if _lease_checkout.get():
                    # Transaction-local limits also bound workers left finishing
                    # after an async TTL timeout. Never leak these to borrowers.
                    with conn.cursor() as cursor:
                        lock_ms, statement_ms = _lease_timeouts.get()
                        cursor.execute(f"SET LOCAL lock_timeout = '{int(lock_ms)}ms'")
                        cursor.execute(f"SET LOCAL statement_timeout = '{int(statement_ms)}ms'")
            except BaseException:
                _close_connection(conn)
                raise
            return _PgDictConnection(conn)


def get_connection():
    """Get a database connection from the pool."""
    return _get_pg_connection()


def _close_connection(conn) -> None:
    """Return a connection to the pool.

    psycopg2's ThreadedConnectionPool.putconn() rolls back / closes the
    connection *before* it removes the slot from its internal bookkeeping
    (``_used`` / ``_rused``). If that rollback/close raises — which happens
    when the underlying server connection has already been dropped (idle
    timeout, network blip, DB restart, shared-DB failover) — the slot is
    never released and is leaked for the lifetime of the process. Enough
    leaks and the pool raises "connection pool exhausted" even though no
    caller is actually holding a connection.

    The old implementation swallowed the putconn error with a bare
    ``except: pass``, which both leaked the slot and hid the leak entirely.
    Here we log it and force the slot free.
    """
    # Keep one pool throughout return/reclaim while shutdown clears the global.
    pool = _pg_pool
    if pool is None:
        return
    raw_conn = conn._conn if isinstance(conn, _PgDictConnection) else conn
    try:
        pool.putconn(raw_conn)
        return
    except Exception:
        logger.warning(
            "putconn failed; forcing connection discard to avoid leaking a "
            "pool slot",
            exc_info=True,
        )
    # Retry with close=True — this skips the rollback path that just failed
    # and goes straight to conn.close(), so the _used slot is reclaimed.
    try:
        pool.putconn(raw_conn, close=True)
        return
    except Exception:
        pass
    # Last resort: drop the leaked bookkeeping directly so the slot frees,
    # then close the (already broken) connection. These are psycopg2 pool
    # internals (no public API to force-reclaim a slot), so access them
    # defensively in case a future version renames them.
    try:
        lock = getattr(pool, "_lock", None)
        rused = getattr(pool, "_rused", None)
        used = getattr(pool, "_used", None)
        if lock is not None and rused is not None and used is not None:
            with lock:
                key = rused.pop(id(raw_conn), None)
                if key is not None:
                    used.pop(key, None)
    except Exception:
        pass
    try:
        raw_conn.close()
    except Exception:
        pass
    finally:
        if isinstance(pool, _RetainingThreadedConnectionPool):
            with pool._lock:
                try:
                    pool._returned(raw_conn, True)
                finally:
                    pool._notify_available()


def pressure_stats() -> Optional[Dict[str, Any]]:
    """Per-pool aggregate counters and monotonic seconds; no connection labels.

    Discards include shutdown and broken connections removed from service.
    ``connections_stale`` counts the subset that failed their checkout health
    check — a rising count means the server is dropping idle connections
    (restart, failover, an idle NAT timeout shorter than the keepalive).
    Hold durations include return/rollback cleanup, but not acquisition waits.
    ``oldest_hold_seconds`` is the true age; ``oldest_live_hold_seconds`` leaves
    out ``retained_holds``, checkouts PostgreSQL shows no work for (see
    ``_HOLD_CHECK_SECONDS``), and declared ``maintenance_holds``.
    Snapshots are copies; a new pool starts fresh and shutdown returns None.
    """
    pool = _pg_pool
    if isinstance(pool, _RetainingThreadedConnectionPool):
        return pool.pressure_stats()
    return None


def pool_stats() -> Optional[Dict[str, int]]:
    """Return current connection-pool usage, or None if not initialized.

    ``in_use`` is the key exhaustion/leak indicator: when it reaches ``max``
    the pool raises "connection pool exhausted". A steadily climbing
    ``in_use`` that never drops back to baseline points at a slot leak.
    Reads psycopg2 pool internals defensively (no public introspection API).
    """
    if _pg_pool is None:
        return None
    try:
        used = getattr(_pg_pool, "_used", None)
        idle = getattr(_pg_pool, "_pool", None)
        if used is None or idle is None:
            return None
        return {
            "in_use": len(used),
            "idle": len(idle),
            "min": getattr(_pg_pool, "startup_minconn", getattr(_pg_pool, "minconn", 0)),
            "max": getattr(_pg_pool, "maxconn", 0),
        }
    except Exception:
        return None


def _rollback_quietly(conn) -> None:
    """Roll back without letting the rollback replace the real error.

    ``rollback()`` on a connection whose socket is already gone raises
    ``InterfaceError``. Raised from inside an ``except`` block that was about
    to re-raise, it becomes the exception the caller sees — so a Postgres
    restart surfaced as "connection already closed" everywhere instead of the
    query error that actually happened, and the traceback pointed at the
    cleanup rather than at the failing statement. The failure is worth a debug
    line and nothing more: ``_close_connection`` already discards a connection
    whose return path breaks, so the slot is not leaked either way.
    """
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 — must not mask the original exception
        logger.debug(
            "rollback failed while handling an error; discarding the "
            "connection and re-raising the original exception",
            exc_info=True,
        )


_commit_callbacks = ContextVar("db_commit_callbacks", default=())
_lease_checkout = ContextVar("db_lease_checkout", default=False)
_lease_timeouts = ContextVar("db_lease_timeouts", default=(500, 1500))


@contextmanager
def get_turn_db() -> Iterator[_PgDictConnection]:
    """Wait longer for turn-store admission, never retry an executed transaction.

    Only acquisition changes: SQL/commit errors still propagate. In particular,
    this does not use get_lease_db's reserved slots or issue its SET LOCAL SQL.
    """
    token = _pool_acquire_wait.set(_TURN_POOL_ACQUIRE_MAX_WAIT_SECONDS)
    try:
        with get_db() as conn:
            yield conn
    finally:
        _pool_acquire_wait.reset(token)


# Bound concurrent self-owned event transactions before they borrow a DB slot.
# Four fsync writers leave the ordinary pool and lease reservation available.
_event_write_slots = threading.BoundedSemaphore(4)
_event_pressure_lock = threading.Lock()
_event_waiting: Dict[object, float] = {}
_event_active: Dict[object, float] = {}
_event_connections: Dict[object, int] = {}


def event_pressure_stats() -> Dict[str, float]:
    """Non-DB snapshot, including writers waiting BEFORE pool checkout."""
    pool = _pg_pool
    abandoned = pool.abandoned_holds() if isinstance(pool, _RetainingThreadedConnectionPool) else ()
    now = time.monotonic()
    with _event_pressure_lock:
        return {
            "waiting": len(_event_waiting),
            "active": len(_event_active),
            "oldest_wait_seconds": now - min(_event_waiting.values(), default=now),
            "oldest_write_seconds": now - min(_event_active.values(), default=now),
            "oldest_live_write_seconds": max((
                now - started for ticket, started in _event_active.items()
                if _event_connections.get(ticket) not in abandoned
            ), default=0.0),
        }


@contextmanager
def get_event_db() -> Iterator[_PgDictConnection]:
    """Backpressure event commits without holding a scarce pooled connection."""
    ticket = object()
    with _event_pressure_lock:
        _event_waiting[ticket] = time.monotonic()
    acquired = False
    try:
        acquired = _event_write_slots.acquire(timeout=_TURN_POOL_ACQUIRE_MAX_WAIT_SECONDS)
        with _event_pressure_lock:
            _event_waiting.pop(ticket, None)
            if acquired:
                _event_active[ticket] = time.monotonic()
        if not acquired:
            raise psycopg2.pool.PoolError("event write admission deadline exceeded")
        with get_turn_db() as conn:
            with _event_pressure_lock:
                _event_connections[ticket] = id(getattr(conn, "_conn", conn))
            yield conn
    finally:
        with _event_pressure_lock:
            _event_waiting.pop(ticket, None)
            _event_active.pop(ticket, None)
            _event_connections.pop(ticket, None)
        if acquired:
            _event_write_slots.release()


@contextmanager
def get_lease_db(*, lock_timeout_ms=500, statement_timeout_ms=1500):
    """Lease admission with transaction-local SQL limits (longer for recovery)."""
    token = _lease_checkout.set(True)
    timeout_token = _lease_timeouts.set((lock_timeout_ms, statement_timeout_ms))
    try:
        with get_db() as conn:
            yield conn
    finally:
        _lease_timeouts.reset(timeout_token)
        _lease_checkout.reset(token)


def after_commit(conn, callback):
    """Queue a notification for the owning get_db/transaction, never a rollback."""
    # resource_sync's observing connection delegates to the physical connection.
    connection = getattr(conn, "_connection", conn)
    for owner, callbacks in reversed(_commit_callbacks.get()):
        if owner is connection:
            callbacks.append(callback)
            return
    raise RuntimeError("after_commit requires an app-managed transaction")


def _warn_slow_hold(started):
    elapsed = time.monotonic() - started
    if elapsed >= 10.0:
        # Collect only on a slow return, not every hot-path checkout. Frame
        # locations identify the holder without SQL, parameters or locals.
        frames = traceback.extract_stack(limit=8)[:-1]
        locations = " <- ".join(f"{f.filename}:{f.lineno}:{f.name}" for f in frames)
        logger.warning("Database connection held %.1fs: %s", elapsed, locations)


@contextmanager
def get_db():
    """Context manager for database connections.

    Yields a connection, commits on success, rolls back on error,
    and always returns the connection to the pool.
    """
    conn = get_connection()
    started = time.monotonic()
    callbacks = []
    token = _commit_callbacks.set((*_commit_callbacks.get(), (conn, callbacks)))
    try:
        yield conn
        conn.commit()
    except Exception:
        _rollback_quietly(conn)
        raise
    finally:
        _commit_callbacks.reset(token)
        _close_connection(conn)
        _warn_slow_hold(started)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            logger.warning("Post-commit notification failed", exc_info=True)


@contextmanager
def transaction():
    """Context manager for multi-step atomic database transactions.

    Groups multiple database operations into a single transaction.
    Unlike get_db() which auto-commits on exit, this yields a connection
    that callers pass to domain functions (via ``_conn`` parameter) so all
    operations share one commit/rollback boundary.

    Usage::

        from app.db.base import transaction

        with transaction() as conn:
            sessions.create_session(session_id, ..., _conn=conn)
            sessions.add_session_message(session_id, ..., _conn=conn)
            # Both committed atomically, or both rolled back on error

    Domain functions that accept ``_conn`` should use it instead of opening
    their own ``get_db()`` connection when provided.
    """
    conn = get_connection()
    started = time.monotonic()
    callbacks = []
    token = _commit_callbacks.set((*_commit_callbacks.get(), (conn, callbacks)))
    try:
        yield conn
        conn.commit()
    except Exception:
        _rollback_quietly(conn)
        raise
    finally:
        _commit_callbacks.reset(token)
        _close_connection(conn)
        _warn_slow_hold(started)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            logger.warning("Post-commit notification failed", exc_info=True)


# =============================================================================
# Cursor helpers
# =============================================================================

def get_cursor(conn):
    """Get a RealDictCursor from a connection."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# =============================================================================
# Schema introspection
# =============================================================================

def column_exists(cursor, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    _validate_identifier(table, "table name")
    _validate_identifier(column, "column name")
    cursor.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return cursor.fetchone() is not None


def table_exists(cursor, table: str) -> bool:
    """Check if a table exists in the database."""
    cursor.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return cursor.fetchone() is not None


def column_is_not_null(cursor, table: str, column: str) -> bool:
    """Check if a column has a NOT NULL constraint.

    Returns True if the column exists and has NOT NULL constraint.
    Returns False if column doesn't exist or is nullable.
    """
    _validate_identifier(table, "table name")
    _validate_identifier(column, "column name")
    cursor.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    )
    row = cursor.fetchone()
    if row is None:
        return False
    nullable = row["is_nullable"]
    return nullable == "NO"


# =============================================================================
# Row conversion utilities
# =============================================================================

def row_to_dict(row) -> Optional[Dict[str, Any]]:
    """Convert a database row to a dictionary.

    RealDictCursor returns dicts directly; handles None gracefully.
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return dict(row)


def rows_to_list(rows: list) -> List[Dict[str, Any]]:
    """Convert a list of database rows to a list of dictionaries."""
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return list(rows)
    return [dict(row) for row in rows]


def parse_jsonb(raw: Any) -> Any:
    """Decode a JSONB column that the test shim stores as TEXT (dict/list pass
    through untouched; None stays None)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()

"""
Shared database utilities — connection management, row conversion, constants.

Uses PostgreSQL via psycopg2 with a threaded connection pool.

All domain modules import from here. Do NOT import domain modules from this file.
"""

import json
import re
from collections import deque
import threading
import time
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
from contextvars import ContextVar

import psycopg2
import psycopg2.pool
import psycopg2.extras
import psycopg2.extensions

from app.core.config import settings

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


class _RetainingThreadedConnectionPool(psycopg2.pool.ThreadedConnectionPool):
    """Open the startup minimum, then retain healthy connections up to maxconn."""

    def __init__(self, minconn, maxconn, *args, **kwargs):
        # Separate lock: snapshots and sleeping acquirers never block on libpq.
        self._available = threading.Condition()
        self._waiters = deque()
        self._generation = 0
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
    def _acquire(self):
        """FIFO admission; direct driver calls retain their keyed semantics.

        Never enter the driver under _available: returns notify under the
        driver's lock. The generation closes the failed-getconn/wait race.
        """
        start = time.monotonic()
        deadline = start + _POOL_ACQUIRE_MAX_WAIT_SECONDS
        ticket = object()
        waited = timed_out = False
        with self._available:
            self._waiters.append(ticket)
        try:
            while True:
                with self._available:
                    if self.closed:
                        raise psycopg2.pool.PoolError("connection pool is closed")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        raise psycopg2.pool.PoolError("connection pool exhausted")
                    if self._waiters[0] is not ticket:
                        if not waited:
                            waited = True
                            self._wait_started()
                        self._available.wait(remaining)
                        continue
                    generation = self._generation
                try:
                    conn = self.getconn()
                    break
                except psycopg2.pool.PoolError:
                    with self._available:
                        if self.closed:
                            raise
                        if not waited:
                            waited = True
                            self._wait_started()
                        if generation == self._generation:
                            self._available.wait(max(0, deadline - time.monotonic()))
            # Include setup in timing; advance the queue even if setup fails.
            yield conn
        finally:
            with self._available:
                self._waiters.remove(ticket)
                self._available.notify_all()
            self._acquisition_finished(time.monotonic() - start, waited, timed_out)

    def pressure_stats(self):
        with self._pressure_lock:
            return dict(self._pressure)

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
        conn = super()._connect(key)
        with self._pressure_lock:
            self._physical.add(id(conn))
            self._last_returned[id(conn)] = time.monotonic()
            self._pressure["connections_opened"] += 1
        return conn

    def _getconn(self, key=None):
        conn = super()._getconn(key)
        with self._pressure_lock:
            self._held_since.setdefault(id(conn), time.monotonic())
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
            if started is not None:
                self._pressure["holds"] += 1
                self._duration("hold", time.monotonic() - started)
            if discarded:
                self._last_returned.pop(id(conn), None)
                if id(conn) in self._physical:
                    self._physical.remove(id(conn))
                    self._pressure["connections_discarded"] += 1
            else:
                self._last_returned[id(conn)] = time.monotonic()

    def _putconn(self, conn, key=None, close=False):
        try:
            super()._putconn(conn, key, close)
        finally:
            try:
                if id(conn) not in self._rused:
                    self._returned(conn, conn not in self._pool)
            finally:
                self._notify_available()

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
        **settings.get_database_connection(),
    )
    logger.info(
        f"PostgreSQL connection pool initialized (min={min_conn}, max={max_conn})"
    )


def close_pool() -> None:
    """Shutdown the PostgreSQL connection pool. Called during app shutdown."""
    global _pg_pool
    if _pg_pool is not None:
        _pg_pool.closeall()
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

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

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

# Application callers wait in FIFO order, bounded by the existing deadline.
_POOL_ACQUIRE_MAX_WAIT_SECONDS = 2.0

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
    if _pg_pool is None:
        return
    raw_conn = conn._conn if isinstance(conn, _PgDictConnection) else conn
    try:
        _pg_pool.putconn(raw_conn)
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
        _pg_pool.putconn(raw_conn, close=True)
        return
    except Exception:
        pass
    # Last resort: drop the leaked bookkeeping directly so the slot frees,
    # then close the (already broken) connection. These are psycopg2 pool
    # internals (no public API to force-reclaim a slot), so access them
    # defensively in case a future version renames them.
    try:
        lock = getattr(_pg_pool, "_lock", None)
        rused = getattr(_pg_pool, "_rused", None)
        used = getattr(_pg_pool, "_used", None)
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
        if isinstance(_pg_pool, _RetainingThreadedConnectionPool):
            with _pg_pool._lock:
                try:
                    _pg_pool._returned(raw_conn, True)
                finally:
                    _pg_pool._notify_available()


def pressure_stats() -> Optional[Dict[str, Any]]:
    """Per-pool aggregate counters and monotonic seconds; no connection labels.

    Discards include shutdown and broken connections removed from service.
    ``connections_stale`` counts the subset that failed their checkout health
    check — a rising count means the server is dropping idle connections
    (restart, failover, an idle NAT timeout shorter than the keepalive).
    Hold durations include return/rollback cleanup, but not acquisition waits.
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


def after_commit(conn, callback):
    """Queue a notification for the owning get_db/transaction, never a rollback."""
    # resource_sync's observing connection delegates to the physical connection.
    connection = getattr(conn, "_connection", conn)
    for owner, callbacks in reversed(_commit_callbacks.get()):
        if owner is connection:
            callbacks.append(callback)
            return
    raise RuntimeError("after_commit requires an app-managed transaction")


@contextmanager
def get_db():
    """Context manager for database connections.

    Yields a connection, commits on success, rolls back on error,
    and always returns the connection to the pool.
    """
    conn = get_connection()
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

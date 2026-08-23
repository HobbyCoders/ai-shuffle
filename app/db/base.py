"""
Shared database utilities — connection management, row conversion, constants.

Uses PostgreSQL via psycopg2 with a threaded connection pool.

All domain modules import from here. Do NOT import domain modules from this file.
"""

import re
import time
import logging
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
import psycopg2.extras
import psycopg2.extensions

from app.core.config import settings

logger = logging.getLogger("app.db")

# Type aliases for Postgres connections/cursors
DbConnection = psycopg2.extensions.connection
DbCursor = psycopg2.extensions.cursor

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
# Backend helpers (always Postgres — kept for downstream compatibility)
# =============================================================================

def is_postgres() -> bool:
    """Always returns True. Kept for backward compatibility during migration."""
    return True


def is_sqlite() -> bool:
    """Always returns False. Kept for backward compatibility during migration."""
    return False


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


def init_pool(min_conn: int = 2, max_conn: int = 10) -> None:
    """Initialize the PostgreSQL connection pool.

    Must be called once during application startup.
    """
    global _pg_pool

    if _pg_pool is not None:
        logger.warning("PostgreSQL pool already initialized, closing old pool")
        _pg_pool.closeall()

    _pg_pool = psycopg2.pool.ThreadedConnectionPool(
        min_conn,
        max_conn,
        **parse_database_url(settings.get_database_url()),
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

# Bounded backpressure on pool acquisition. Multiple thread pools draw on the
# single PG pool concurrently (asyncio's default executor, anyio's sync-dep
# pool, the git pool), so a transient overlap can momentarily take every slot.
# psycopg2's ThreadedConnectionPool.getconn() raises PoolError *immediately* on
# exhaustion — no waiting — which turns a sub-second burst into a hard 500 on an
# unrelated endpoint. Instead of failing outright, wait (with a short capped
# backoff, bounded in total) for a slot to be returned. These run on worker
# threads, so a plain time.sleep is correct here — never asyncio.
_POOL_ACQUIRE_MAX_WAIT_SECONDS = 2.0
_POOL_ACQUIRE_BACKOFF_START_SECONDS = 0.01
_POOL_ACQUIRE_BACKOFF_CAP_SECONDS = 0.1


def _get_pg_connection() -> "_PgDictConnection":
    """Acquire a PostgreSQL connection from the pool.

    Returns a wrapper that defaults to RealDictCursor for dict-style row access.
    On transient pool exhaustion, waits (short capped backoff, bounded to
    ~``_POOL_ACQUIRE_MAX_WAIT_SECONDS`` total) for a slot to free rather than
    raising PoolError immediately. A single warning is logged when a wait
    actually happens so pool pressure is visible without per-retry log spam.
    """
    if _pg_pool is None:
        raise RuntimeError(
            "PostgreSQL connection pool not initialized. Call init_pool() first."
        )

    start = time.monotonic()
    deadline = start + _POOL_ACQUIRE_MAX_WAIT_SECONDS
    backoff = _POOL_ACQUIRE_BACKOFF_START_SECONDS
    waited = False
    while True:
        try:
            conn = _pg_pool.getconn()
            break
        except psycopg2.pool.PoolError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Bounded wait expired — surface the exhaustion as before, but
                # now with visibility into how long we backpressured.
                stats = pool_stats() or {}
                logger.warning(
                    "PG pool acquisition failed after %dms of backpressure — "
                    "pool exhausted (in_use=%s max=%s)",
                    round((time.monotonic() - start) * 1000),
                    stats.get("in_use"),
                    stats.get("max"),
                )
                raise
            waited = True
            time.sleep(min(backoff, remaining))
            backoff = min(backoff * 2, _POOL_ACQUIRE_BACKOFF_CAP_SECONDS)

    if waited:
        stats = pool_stats() or {}
        logger.warning(
            "PG pool acquisition waited %dms for a free slot (pool pressure; "
            "in_use=%s max=%s)",
            round((time.monotonic() - start) * 1000),
            stats.get("in_use"),
            stats.get("max"),
        )
    conn.autocommit = False
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
            "min": getattr(_pg_pool, "minconn", 0),
            "max": getattr(_pg_pool, "maxconn", 0),
        }
    except Exception:
        return None


@contextmanager
def get_db():
    """Context manager for database connections.

    Yields a connection, commits on success, rolls back on error,
    and always returns the connection to the pool.
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_connection(conn)


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
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_connection(conn)


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

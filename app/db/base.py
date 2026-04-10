"""
Shared database utilities — connection management, row conversion, constants.

All domain modules import from here. Do NOT import domain modules from this file.
"""

import re
import sqlite3
import logging
import threading
import time
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from app.core.config import settings

logger = logging.getLogger("app.db")

# Regex for valid SQLite identifiers: alphanumeric + underscores, must start with letter/underscore.
# This prevents SQL injection in PRAGMA and ALTER TABLE statements where parameterization
# is not supported by SQLite.
_VALID_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(value: str, kind: str = "identifier") -> str:
    """Validate that a string is a safe SQL identifier (table name, column name).

    SQLite PRAGMA and ALTER TABLE statements do not support parameterized queries
    for table/column names, so we must validate these values ourselves.

    Args:
        value: The identifier to validate.
        kind: Human-readable label for error messages (e.g. "table name").

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
    DELETED = "deleted"
    ORPHANED = "orphaned"


# =============================================================================
# Connection Pool
# =============================================================================
class ConnectionPool:
    """Thread-safe SQLite connection pool with lazy initialization and idle eviction.

    Connections are created on demand up to ``max_size``.  PRAGMAs are executed
    once at creation time, not on every checkout.  Idle connections older than
    ``max_idle_time`` seconds are evicted on the next pool access.
    """

    def __init__(self, max_size: int = 8, max_idle_time: float = 300.0):
        self._max_size = max_size
        self._max_idle_time = max_idle_time
        self._lock = threading.Lock()
        # Pool of idle connections: list of (conn, last_returned_timestamp)
        self._idle: list[tuple[sqlite3.Connection, float]] = []
        # Total connections currently alive (idle + checked-out)
        self._total: int = 0

    # ----- internal helpers ------------------------------------------------

    @staticmethod
    def _create_connection() -> sqlite3.Connection:
        """Create a new SQLite connection with PRAGMAs applied once."""
        conn = sqlite3.connect(
            str(settings.db_path), check_same_thread=False, timeout=30.0
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
        timeout_ms = int(settings.db_busy_timeout_ms)
        conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        return conn

    def _evict_idle(self) -> None:
        """Close connections that have been idle longer than max_idle_time.

        Must be called while holding ``self._lock``.
        """
        now = time.monotonic()
        surviving: list[tuple[sqlite3.Connection, float]] = []
        for conn, returned_at in self._idle:
            if now - returned_at > self._max_idle_time:
                self._total -= 1
                try:
                    conn.execute("PRAGMA optimize")
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                surviving.append((conn, returned_at))
        self._idle = surviving

    @staticmethod
    def _health_check(conn: sqlite3.Connection) -> bool:
        """Return True if the connection is still usable."""
        try:
            conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ----- public API ------------------------------------------------------

    def checkout(self) -> sqlite3.Connection:
        """Acquire a connection from the pool.

        Returns an idle connection if one is available and healthy, otherwise
        creates a new one (if the pool has capacity).  Evicts stale idle
        connections on every access.
        """
        with self._lock:
            self._evict_idle()

            # Try to reuse an idle connection
            while self._idle:
                conn, _returned_at = self._idle.pop()
                if self._health_check(conn):
                    return conn
                # Dead connection — discard it
                self._total -= 1
                try:
                    conn.close()
                except Exception:
                    pass

            # No idle connection available — create a new one if under limit
            if self._total < self._max_size:
                self._total += 1
            else:
                # Pool exhausted — log a warning but still create a connection
                # so callers don't deadlock.  The extra connection will be
                # discarded on return if the pool is full.
                logger.warning(
                    "Connection pool exhausted (max_size=%d, total=%d). "
                    "Creating overflow connection.",
                    self._max_size,
                    self._total,
                )
                self._total += 1

        # Create outside the lock to avoid holding it during I/O
        return self._create_connection()

    def checkin(self, conn: sqlite3.Connection) -> None:
        """Return a connection to the pool.

        If the pool is already at capacity the connection is closed instead.
        """
        with self._lock:
            if len(self._idle) < self._max_size:
                self._idle.append((conn, time.monotonic()))
            else:
                # Overflow connection — close it
                self._total -= 1
                try:
                    conn.execute("PRAGMA optimize")
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass

    def shutdown(self) -> None:
        """Close all pooled connections.  Call during application shutdown."""
        with self._lock:
            for conn, _returned_at in self._idle:
                try:
                    conn.execute("PRAGMA optimize")
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            self._idle.clear()
            self._total = 0
            logger.info("Connection pool shut down.")


# Module-level singleton pool
_pool = ConnectionPool()


def get_connection() -> sqlite3.Connection:
    """Get a one-off database connection with row factory.

    This does NOT use the pool — it creates a fresh connection every time.
    Use this for one-off operations like ``init_database`` that need a
    dedicated connection with a deterministic lifecycle.
    """
    conn = sqlite3.connect(str(settings.db_path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 1000")
    timeout_ms = int(settings.db_busy_timeout_ms)
    conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    return conn


def _close_connection(conn: sqlite3.Connection):
    """Close a connection with lightweight maintenance.

    Runs PRAGMA optimize before closing — per SQLite docs, this should be
    called on every connection close.  It's a near-instant no-op most of the
    time; it only runs ANALYZE on tables whose stats have become stale since
    the last call, keeping the query planner accurate continuously rather than
    letting it degrade between periodic maintenance runs.
    """
    try:
        conn.execute("PRAGMA optimize")
    except Exception:
        pass  # Best-effort; don't let maintenance failure break the caller
    conn.close()


@contextmanager
def get_db():
    """Context manager for database connections.

    Checks out a connection from the pool, yields it, and returns it to the
    pool on exit.  Commits on success, rolls back on exception.
    """
    conn = _pool.checkout()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.checkin(conn)


@contextmanager
def transaction():
    """Context manager for multi-step atomic database transactions.

    Groups multiple database operations into a single transaction.
    Unlike get_db() which auto-commits on exit, this yields a connection
    that callers pass to domain functions (via `_conn` parameter) so all
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
    conn = _pool.checkout()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.checkin(conn)


def column_exists(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    """Check if a column exists in a table using PRAGMA table_info."""
    _validate_identifier(table, "table name")
    _validate_identifier(column, "column name")
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row["name"] for row in cursor.fetchall()]
    return column in columns


def table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    """Check if a table exists in the database."""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    )
    return cursor.fetchone() is not None


def column_is_not_null(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    """Check if a column has a NOT NULL constraint.

    Returns True if the column exists and has NOT NULL constraint.
    Returns False if column doesn't exist or is nullable.
    """
    _validate_identifier(table, "table name")
    _validate_identifier(column, "column name")
    cursor.execute(f"PRAGMA table_info({table})")
    for row in cursor.fetchall():
        if row["name"] == column:
            return row["notnull"] == 1
    return False


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    """Convert a sqlite3.Row to a dictionary."""
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """Convert a list of sqlite3.Row to a list of dictionaries."""
    return [dict(row) for row in rows]

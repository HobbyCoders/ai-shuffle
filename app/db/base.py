"""
Shared database utilities — connection management, row conversion, constants.

Uses PostgreSQL via psycopg2 with a threaded connection pool.

All domain modules import from here. Do NOT import domain modules from this file.
"""

import re
import logging
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from urllib.parse import urlparse, unquote

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

def init_pool(min_conn: int = 2, max_conn: int = 10) -> None:
    """Initialize the PostgreSQL connection pool.

    Must be called once during application startup.
    """
    global _pg_pool

    if _pg_pool is not None:
        logger.warning("PostgreSQL pool already initialized, closing old pool")
        _pg_pool.closeall()

    # Parse the DATABASE_URL to extract components — handles percent-encoded
    # passwords (e.g. %40 for @) that psycopg2 cannot decode from a raw DSN.
    # Also handles URLs with or without the postgresql:// scheme prefix.
    db_url = settings.database_url
    if not db_url.startswith(("postgresql://", "postgres://")):
        db_url = f"postgresql://{db_url}"
    parsed = urlparse(db_url)
    _pg_pool = psycopg2.pool.ThreadedConnectionPool(
        min_conn,
        max_conn,
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
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

def _get_pg_connection() -> "_PgDictConnection":
    """Acquire a PostgreSQL connection from the pool.

    Returns a wrapper that defaults to RealDictCursor for dict-style
    row access.
    """
    if _pg_pool is None:
        raise RuntimeError(
            "PostgreSQL connection pool not initialized. Call init_pool() first."
        )
    conn = _pg_pool.getconn()
    conn.autocommit = False
    return _PgDictConnection(conn)


def get_connection():
    """Get a database connection from the pool."""
    return _get_pg_connection()


def _close_connection(conn) -> None:
    """Return a connection to the pool."""
    if _pg_pool is None:
        return
    raw_conn = conn._conn if isinstance(conn, _PgDictConnection) else conn
    try:
        _pg_pool.putconn(raw_conn)
    except Exception:
        pass


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

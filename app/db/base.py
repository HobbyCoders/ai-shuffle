"""
Shared database utilities — connection management, row conversion, constants.

All domain modules import from here. Do NOT import domain modules from this file.
"""

import sqlite3
import logging
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from app.core.config import settings

logger = logging.getLogger("app.db")


# =============================================================================
# Worktree Status Constants
# =============================================================================
class WorktreeStatus:
    """Worktree status values — use these constants for consistency."""
    ACTIVE = "active"
    DELETED = "deleted"
    ORPHANED = "orphaned"


def get_connection() -> sqlite3.Connection:
    """Get a database connection with row factory."""
    conn = sqlite3.connect(str(settings.db_path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Set busy timeout from config (default 30s) — critical on Windows where file locking is more aggressive
    conn.execute(f"PRAGMA busy_timeout = {settings.db_busy_timeout_ms}")
    return conn


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def column_exists(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    """Check if a column exists in a table using PRAGMA table_info."""
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

"""Explicit SQL budgets for offline DDL and bounded maintenance, not user queries."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import psycopg2.extensions


def migration_timeouts(cursor: Any) -> None:
    """Allow startup DDL/lock waits to finish; settings end with its transaction.

    Startup owns its admission/readiness deadline. An ordinary query deadline
    would otherwise cancel every retry of a large index build. The boot lock
    also idles while other startup transactions run. The SQLite shim has no SET.
    """
    if isinstance(cursor, psycopg2.extensions.cursor):
        cursor.execute("SET LOCAL statement_timeout = 0")
        cursor.execute("SET LOCAL lock_timeout = 0")
        cursor.execute("SET LOCAL idle_in_transaction_session_timeout = 0")


def analyze_timeouts(cursor: Any) -> None:
    """Statistics may scan a large database, but must not wait indefinitely."""
    if isinstance(cursor, psycopg2.extensions.cursor):
        cursor.execute("SET LOCAL statement_timeout = '5min'")
        cursor.execute("SET LOCAL lock_timeout = '30s'")


@contextmanager
def vacuum_timeouts(cursor: Any) -> Iterator[None]:
    """VACUUM needs autocommit: save and restore its session-level SQL budgets.

    Restoration errors propagate so the caller discards the connection rather
    than leaking a maintenance budget to an ordinary borrower.
    """
    if not isinstance(cursor, psycopg2.extensions.cursor):
        yield
        return
    cursor.execute("SELECT current_setting('statement_timeout') AS statement, "
                   "current_setting('lock_timeout') AS lock")
    row = cursor.fetchone()
    previous = (row["statement"], row["lock"]) if isinstance(row, Mapping) else tuple(row)
    try:
        cursor.execute("SET statement_timeout = '15min'")
        cursor.execute("SET lock_timeout = '30s'")
        yield
    finally:
        try:
            cursor.execute("SELECT set_config('statement_timeout', %s, false), "
                           "set_config('lock_timeout', %s, false)", previous)
        except Exception:
            cursor.connection.close()
            raise

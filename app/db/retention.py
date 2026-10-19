"""Bound retention work without holding a pool connection across batches."""

import psycopg2.extensions

from app.db.base import get_db

BATCH_SIZE = 5000


def delete_batches(sql, params, *, cursor=None) -> int:
    """Run a DELETE limited to ``BATCH_SIZE`` rows until it is drained.

    The SQL/params must include that limit. A supplied cursor retains its caller's
    transaction ownership; normal background callers commit and release each batch.
    """
    def delete(cur):
        # The SQLite test shim has no SET. Production cursors are psycopg2 cursors.
        if isinstance(cur, psycopg2.extensions.cursor):
            cur.execute("SET LOCAL statement_timeout = '30s'")
        cur.execute(sql, params)
        return cur.rowcount or 0

    total = 0
    while True:
        if cursor is not None:
            deleted = delete(cursor)
        else:
            with get_db() as conn:
                deleted = delete(conn.cursor())
        total += deleted
        if deleted < BATCH_SIZE:
            return total

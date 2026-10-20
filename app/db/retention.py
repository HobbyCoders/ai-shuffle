"""Bound retention work without holding a pool connection across batches."""

import psycopg2.extensions

from app.db.base import get_db

BATCH_SIZE = 5000


def delete_batches(sql, params, *, cursor=None, advance=None, stop=None) -> int:
    """Run a DELETE limited to ``BATCH_SIZE`` rows until it is drained.

    The SQL/params must include that limit. A supplied cursor retains its caller's
    transaction ownership AND the 30s timeout for the rest of that transaction;
    normal background callers commit and release each batch. ``advance`` consumes
    RETURNING rows and supplies the next batch's parameters before releasing the
    connection. ``stop`` is a threading.Event checked between batches.
    """
    def delete(cur):
        nonlocal params
        set_timeout(cur)
        cur.execute(sql, params)
        if advance is not None:
            params = advance(cur)
        return cur.rowcount or 0

    total = 0
    while stop is None or not stop.is_set():
        if cursor is not None:
            deleted = delete(cursor)
        else:
            with get_db() as conn:
                deleted = delete(conn.cursor())
        total += deleted
        if deleted < BATCH_SIZE:
            break
    return total


def set_timeout(cursor):
    """The shim has no SET; real PostgreSQL transactions always get a deadline."""
    if isinstance(cursor, psycopg2.extensions.cursor):
        cursor.execute("SET LOCAL statement_timeout = '30s'")


def mutate_batches(table, where, params=(), *, keys=("id",), update=None,
                   update_params=(), returning=None, consume=None, stop=None,
                   batch_size=None, max_batches=None, columns=()):
    """Keyset-page maintenance DELETEs/UPDATEs in independently committed batches.

    Identifiers and SQL fragments are internal constants, never request input.
    Select keys in database order (not Python collation order), then recheck the
    predicate when mutating: a concurrent refresh must not be deleted/scrubbed.
    ``consume`` runs after commit, receiving RETURNING rows or selected keys and
    optional diagnostic ``columns`` when no RETURNING clause is requested.
    """
    size = batch_size or BATCH_SIZE
    key_sql = ", ".join(keys)
    boundary = None
    total = batches = 0
    while stop is None or not stop.is_set():
        with get_db() as conn:
            cur = conn.cursor()
            set_timeout(cur)
            after = ""
            args = tuple(params)
            if boundary is not None:
                after = f" AND ({key_sql}) > ({', '.join(['%s'] * len(keys))})"
                args += boundary
            select_sql = ", ".join((*keys, *columns))
            cur.execute(f"SELECT {select_sql} FROM {table} WHERE ({where}){after} "
                        f"ORDER BY {key_sql} LIMIT %s", (*args, size))
            selected = cur.fetchall()
            if not selected:
                break
            boundary = tuple(selected[-1][key] for key in keys)
            marks = ", ".join(["(" + ", ".join(["%s"] * len(keys)) + ")"] * len(selected))
            values = tuple(row[key] for row in selected for key in keys)
            action = f"UPDATE {table} SET {update}" if update else f"DELETE FROM {table}"
            sql = f"{action} WHERE ({key_sql}) IN ({marks}) AND ({where})"
            if returning:
                sql += " RETURNING " + returning
            cur.execute(sql, (*update_params, *values, *params))
            total += cur.rowcount or 0
            rows = cur.fetchall() if returning else selected
        if consume is not None:
            consume(rows)
        batches += 1
        if len(selected) < size or (max_batches is not None and batches >= max_batches):
            break
    return total

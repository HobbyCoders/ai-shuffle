"""Online, restart-safe GIN isolation for hand-off history.

0 = ordinary live write, 1 = imported/pending, 2 = imported/indexed. History
GINs have their own bounded pending lists. Live inserts and updates always use
state 0, never those lists. No setting on a live GIN is changed. The state is the
persistent work queue; each promotion transaction commits independently.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import psycopg2
from psycopg2.extras import RealDictCursor

from app.db.base import get_db

logger = logging.getLogger(__name__)
COLUMN = "history_index_state"
SETTING = "aishuffle.history_index_state"
KEY = "migration:history_indexing_v1"
TABLES = {
    "fluid_session_nodes": ("session_id, node_id", "message"),
    "session_store_entries": ("id", "entry"),
    "session_events": ("session_id, seq", "payload"),
    "session_messages": ("id", "content"),
    "session_recall_text": ("source, session_id, row_key, part", "body"),
}
# Optional cached-text indexes and the raw indexes can coexist on old installs.
INDEXES = (
    "idx_fluid_nodes_trgm", "idx_fluid_recall_text_trgm",
    "idx_sse_entry_trgm", "idx_sse_recall_text_trgm",
    "idx_session_events_recall_trgm", "idx_events_recall_text_trgm",
    "idx_session_recall_text_trgm_n", "idx_session_recall_text_trgm_s",
    "idx_session_recall_text_trgm_e",
    "idx_sse_fts", "idx_session_messages_fts",
)
_DEFAULT = f"COALESCE(NULLIF(current_setting('{SETTING}', true), ''), '0')::smallint"


def enabled(cursor: Any) -> bool:
    cursor.execute("SELECT value FROM system_settings WHERE key = %s", (KEY,))
    row = cursor.fetchone()
    return bool(row and row["value"] == "ready")


def search_table(
    cursor: Any, table: str, alias: str | None = None, *, include_pending: bool = False,
) -> str:
    """Two disjoint arms let PostgreSQL push text predicates into each GIN.

    A factored ``(state=0 OR state=2) AND body LIKE ...`` does not reliably use
    the partial indexes. UNION ALL preserves rows without that planner trap.
    """
    if table not in TABLES:
        raise ValueError("unsupported history search table")
    from app.db.base import column_exists
    # Projection repair can clear the ready stamp while the source tables still
    # have partial indexes. Keep using those indexes throughout that repair.
    if not column_exists(cursor, table, COLUMN):
        return table + (f" {alias}" if alias else "")
    states = (0, 2, 1) if include_pending else (0, 2)
    arms = " UNION ALL ".join(f"SELECT * FROM {table} WHERE {COLUMN} = {s}" for s in states)
    return f"({arms}) {alias or table}"


@contextmanager
def importing(cursor: Any, table: str) -> Iterator[bool]:
    """Scope deferral to hand-off INSERTs, never the rest of publication."""
    active = table in TABLES and enabled(cursor)
    if active:
        cursor.execute("SELECT set_config(%s, '1', true)", (SETTING,))
    try:
        yield active
    finally:
        if active:
            # On a failed statement the caller rolls back the entire transaction.
            # Do not mask that exception with a command in an aborted transaction.
            import sys
            if sys.exc_info()[0] is None:
                cursor.execute("SELECT set_config(%s, '0', true)", (SETTING,))


def _valid(cursor: Any, name: str) -> bool:
    cursor.execute("SELECT indisvalid AND indisready AS ok FROM pg_index "
                   "WHERE indexrelid = to_regclass(%s)", (name,))
    row = cursor.fetchone()
    return bool(row and row["ok"])


@contextmanager
def _online_cursor() -> Iterator[Any]:
    """Borrow only the initialized database; never bypass test/shim routing.

    Concurrent DDL needs autocommit. Restore both session settings and the
    transaction mode before returning the slot, and release only our own lock.
    """
    with get_db() as db:
        raw = getattr(db, "_conn", None)
        if not isinstance(raw, psycopg2.extensions.connection):
            raise RuntimeError("online history indexing requires PostgreSQL")
        raw.commit()
        raw.autocommit = True
        try:
            with raw.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT current_setting('lock_timeout') AS lock, "
                            "current_setting('statement_timeout') AS statement")
                previous = cur.fetchone()
                cur.execute("SELECT pg_try_advisory_lock(hashtext(current_database()), "
                            "hashtext(current_schema() || '.history_indexing_v1')) AS ok")
                locked = cur.fetchone()["ok"]
                try:
                    yield cur if locked else None
                finally:
                    if locked:
                        cur.execute("SELECT pg_advisory_unlock(hashtext(current_database()), "
                                    "hashtext(current_schema() || '.history_indexing_v1'))")
                    cur.execute("SELECT set_config('lock_timeout',%s,false), "
                                "set_config('statement_timeout',%s,false)",
                                (previous["lock"], previous["statement"]))
        finally:
            raw.autocommit = False


def migrate() -> bool:
    """One online migration pass. No startup transaction or table rewrite.

    Lock acquisition is capped at 250ms. Concurrent builds survive cancellation
    as invalid indexes, which the next pass drops and retries. The original
    index remains usable until *both* replacement indexes are valid. An advisory
    lock serializes migrators, not writers. Imports defer only after publication
    of the final ready marker. Ordinary old rows stay state 0 without a backfill.
    """
    with _online_cursor() as cur:
        if cur is None:
            return False
        cur.execute("SET lock_timeout = '250ms'")
        cur.execute("SET statement_timeout = '15min'")
        cur.execute("CREATE TABLE IF NOT EXISTS history_index_migrations "
                    "(name text PRIMARY KEY, tail text NOT NULL)")
        for table in TABLES:
            cur.execute("SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass(%s) "
                        "AND attname=%s AND NOT attisdropped", (table, COLUMN))
            if cur.fetchone():
                continue
            # PostgreSQL 11+ constant defaults are metadata-only, even on
            # large tables. Change to the transaction setting afterwards.
            cur.execute("BEGIN")
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                            f"{COLUMN} smallint NOT NULL DEFAULT 0")
                cur.execute(f"ALTER TABLE {table} ALTER COLUMN {COLUMN} SET DEFAULT {_DEFAULT}")
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
        cur.execute(f"""
            CREATE OR REPLACE FUNCTION history_index_on_update() RETURNS trigger
            LANGUAGE plpgsql AS $fn$
            BEGIN
                IF COALESCE(current_setting('{SETTING}', true), '') NOT IN ('1','2') THEN
                    NEW.{COLUMN} := 0;
                END IF;
                RETURN NEW;
            END
            $fn$
        """)
        for table in TABLES:
            trigger = f"trg_{table}_history_live_update"
            cur.execute("SELECT 1 FROM pg_trigger WHERE tgrelid=to_regclass(%s) "
                        "AND tgname=%s AND NOT tgisinternal", (table, trigger))
            if not cur.fetchone():
                cur.execute(f"CREATE TRIGGER {trigger} BEFORE UPDATE ON {table} "
                            "FOR EACH ROW EXECUTE FUNCTION history_index_on_update()")
        cur.execute("INSERT INTO system_settings(key,value) VALUES (%s,'building') "
                    "ON CONFLICT (key) DO NOTHING", (KEY,))
        for name in INDEXES:
            cur.execute("SELECT pg_get_indexdef(indexrelid) AS ddl, "
                        "pg_get_expr(indpred, indrelid) AS pred FROM pg_index "
                        "WHERE indexrelid = to_regclass(%s)", (name,))
            row = cur.fetchone()
            partial = bool(row and COLUMN in (row["pred"] or ""))
            if partial and _valid(cur, name) and _valid(cur, name + "_h2"):
                cur.execute("SELECT reloptions FROM pg_class WHERE oid=to_regclass(%s)",
                            (name + "_h2",))
                options = cur.fetchone()["reloptions"] or []
                wanted = {"fastupdate=on", "gin_pending_list_limit=4096"}
                if not wanted.issubset(options):
                    cur.execute(f"ALTER INDEX {name}_h2 SET "
                                "(fastupdate=on, gin_pending_list_limit=4096)")
                continue
            if row and not partial:
                tail = row["ddl"].split(" USING gin ", 1)[1]
                cur.execute("INSERT INTO history_index_migrations(name, tail) VALUES (%s,%s) "
                            "ON CONFLICT DO NOTHING", (name, tail))
            cur.execute("SELECT tail FROM history_index_migrations WHERE name=%s", (name,))
            saved = cur.fetchone()
            if not saved:
                continue  # Optional index was never installed.
            tail = saved["tail"]
            # PostgreSQL supplies this expression/predicate, not external input.
            expression, sep, original_predicate = tail.partition(" WHERE ")
            expression, has_options, original_options = expression.partition(" WITH ")
            table = next(t for t in TABLES if (
                (t == "fluid_session_nodes" and "fluid" in name)
                or (t == "session_store_entries" and "sse" in name)
                or (t == "session_recall_text" and "session_recall_text" in name)
                or (t == "session_events" and "events" in name)
                or (t == "session_messages" and "session_messages" in name)))
            for state in (0, 2):
                replacement = name + f"_h{state}"
                if not _valid(cur, replacement):
                    cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {replacement}")
                    where = f"({original_predicate}) AND " if sep else ""
                    if state == 2:
                        options = " WITH (fastupdate=on, gin_pending_list_limit=4096)"
                    else:
                        options = " WITH " + original_options if has_options else ""
                    cur.execute(f"CREATE INDEX CONCURRENTLY {replacement} ON {table} "
                                f"USING gin {expression}{options} WHERE "
                                f"{where}{COLUMN} = {state}")
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            cur.execute(f"ALTER INDEX {name}_h0 RENAME TO {name}")
            # Yield between indexes. A restart resumes from the valid ones.
            return False
        for table, (keys, _) in TABLES.items():
            queue = f"idx_{table}_history_pending"
            if not _valid(cur, queue):
                cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {queue}")
                cur.execute(f"CREATE INDEX CONCURRENTLY {queue} ON {table} ({keys}) "
                            f"WHERE {COLUMN} = 1")
        cur.execute("INSERT INTO system_settings(key,value) VALUES (%s,'ready') "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (KEY,))
        return True


def promote_batch(
    *, rows: int = 32, byte_budget: int = 128_000, seconds: float = 0.1,
    can_work: Callable[[], bool] = lambda: True,
) -> int:
    """Promote a bounded locked batch, atomically with its index updates.

    A single oversize source row is allowed to ensure forward progress. SKIP LOCKED
    and a short lock timeout avoid waiting behind live writers. The compact
    projection has its own durable queue. Row, byte and elapsed-time budgets
    apply to the entire call, not separately to every table. A statement already
    executing cannot be preempted; recheck the foreground gate before each row.
    """
    count = 0
    used = 0
    limit = max(1, min(rows, 32))
    deadline = time.monotonic() + seconds
    for table, (keys, payload) in TABLES.items():
        if not can_work() or (count and time.monotonic() >= deadline):
            return count
        if count >= limit or used >= byte_budget:
            return count
        with get_db() as conn:
            cur = conn.cursor()
            if not enabled(cur):
                return 0
            cur.execute("SET LOCAL lock_timeout = '250ms'")
            cur.execute("SELECT set_config(%s, '2', true)", (SETTING,))
            # An indexed queue, independent of table size or promotion progress.
            # Keep the queue-index lookup bounded, then visit just that locked
            # window in heap order. Ordering the whole queue by ctid would scan
            # all remaining history on every call.
            cur.execute(f"SELECT * FROM (SELECT {keys}, pg_column_size({payload}) AS bytes, "
                        f"ctid AS history_tid FROM {table} WHERE {COLUMN} = 1 "
                        f"ORDER BY {keys} LIMIT %s FOR UPDATE SKIP LOCKED) pending "
                        "ORDER BY history_tid", (limit - count,))
            candidates = cur.fetchall()
            for row in candidates:
                if not can_work() or (count and time.monotonic() >= deadline):
                    return count
                size = row["bytes"] or 0
                if used and used + size > byte_budget:
                    break
                where = " AND ".join(f"{k.strip()} = %s" for k in keys.split(","))
                cur.execute(f"UPDATE {table} SET {COLUMN} = 2 WHERE {where} AND {COLUMN} = 1",
                            tuple(row[k.strip()] for k in keys.split(",")))
                used += size
                count += 1
    return count


def run_pass(
    *, seconds: float = 5.0, pause: float = 0.1, can_work: Callable[[], bool] = lambda: True,
) -> bool:
    """Yield between batches when a foreground turn needs the worker's I/O."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM system_settings WHERE key=%s "
                    "AND value IN ('building','ready')", (KEY,))
        if not cur.fetchone():
            return True  # Experimental: no online DDL or deferral without opt-in.
    if not can_work() or not migrate():
        return False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not can_work():
            return False
        if not promote_batch(can_work=can_work):
            return can_work()
        time.sleep(pause)
    return False

"""Online, restart-safe GIN isolation for hand-off history.

0 = ordinary live write, 2 = imported, in the history GINs. 1 = imported by an
older build and still waiting for promotion; nothing writes it any more, but
those rows drain through the durable queue. Live inserts and updates always use
state 0 and never touch a history GIN, so no setting on a live GIN is changed.

Imports only append to a large, history-only pending list, which keeps the
expensive GIN merge out of the move. Imported text is searchable at once and the
list is merged in the background, between foreground turns.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import psycopg2
from psycopg2.extras import RealDictCursor

from app.db.base import exempt_from_start_pressure, get_db

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
# Only reader-backed indexes are split. Legacy raw/cached JSON GINs (including
# any _h0/_h2 left by an interrupted older migration) are retired separately.
INDEXES = (
    "idx_session_recall_text_trgm_n", "idx_session_recall_text_trgm_s",
    "idx_session_recall_text_trgm_e",
    "idx_sse_fts", "idx_session_messages_fts",
)
IMPORT_STATE = "2"
# Just below 2 GiB: Windows PostgreSQL caps this option at 2097151 KB. A list of
# this size holds about three copies of the 78 MB all-text load-test chat, and
# reaching the limit would make the importer merge it inline, using work_mem.
HISTORY_PENDING_KB = 2_096_128
_HISTORY_OPTIONS = f"fastupdate=on, gin_pending_list_limit={HISTORY_PENDING_KB}"
# Let a move finish before merging what it wrote.
FLUSH_QUIET_SECONDS = 5.0
# When something last wrote to a history list; None once merged. Starts dirty so
# the first pass after a restart merges whatever the previous process left behind.
_unmerged_at: float | None = 0.0
# Imports running right now: a single table's publish can outlast the quiet time.
_active_imports = 0
_imports_lock = threading.Lock()
# An import that takes a list past its limit merges the whole list itself. It
# blocks only that import: live writes use other indexes. So give it longer than
# the ordinary 30s and the memory the background cleaner gets (PostgreSQL's inline
# merge uses work_mem, which took over 10 minutes where the cleaner took under one).
IMPORT_STATEMENT_TIMEOUT = "10min"
# A rebuild retains the original index until it is finished, and WAL-logs the copy.
BUILD_RESERVE_BYTES = 1024 ** 3
BUILD_RETRY_SECONDS = 600.0
_retry_build_at = 0.0
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


def note_history_write() -> None:
    """Record that a history GIN's pending list has something to merge.

    Call it after the write, so a merge either saw the entries or leaves this set.
    """
    global _unmerged_at
    _unmerged_at = time.monotonic()


@contextmanager
def importing(cursor: Any, table: str) -> Iterator[bool]:
    """Scope deferral to hand-off INSERTs, never the rest of publication."""
    global _active_imports
    active = table in TABLES and enabled(cursor)
    if active:
        with _imports_lock:
            _active_imports += 1
        note_history_write()
    previous = None
    try:
        if active:
            # OFFSET keeps the old values' read ahead of the set_config calls.
            cursor.execute(
                "SELECT old.timeout, old.memory, set_config(%s, %s, true), "
                "set_config('statement_timeout', %s, true), "
                "set_config('work_mem', current_setting('maintenance_work_mem'), true) FROM "
                "(SELECT current_setting('statement_timeout') AS timeout, "
                "current_setting('work_mem') AS memory OFFSET 0) old",
                (SETTING, IMPORT_STATE, IMPORT_STATEMENT_TIMEOUT),
            )
            previous = cursor.fetchone()
        yield active
    finally:
        if active:
            # The quiet time starts now, so stamp this before the scope ends.
            note_history_write()
            with _imports_lock:
                _active_imports -= 1
            # On a failed statement the caller rolls back the entire transaction.
            # Do not mask that exception with a command in an aborted transaction.
            import sys
            if sys.exc_info()[0] is None:
                cursor.execute("SELECT set_config(%s, '0', true), "
                               "set_config('statement_timeout', %s, true), "
                               "set_config('work_mem', %s, true)",
                               (SETTING, previous["timeout"], previous["memory"]))


def _valid(cursor: Any, name: str) -> bool:
    cursor.execute("SELECT indisvalid AND indisready AS ok FROM pg_index "
                   "WHERE indexrelid = to_regclass(%s)", (name,))
    row = cursor.fetchone()
    return bool(row and row["ok"])


class _OnlineCursorComplete(Exception):
    """Let get_db reclaim a closed autocommit connection without committing it."""


@contextmanager
def _online_cursor() -> Iterator[Any]:
    """Borrow only the initialized database; never bypass test/shim routing.

    Concurrent DDL needs autocommit. Take startup's schema lock too: otherwise
    startup's plain index DDL can queue behind us and indirectly block DML.
    Always close before returning the pool slot: startup may already have
    selected this backend for cancellation, even after its tag/locks are gone.
    """
    try:
        with get_db() as db:
            raw = getattr(db, "_conn", None)
            if not isinstance(raw, psycopg2.extensions.connection):
                raise RuntimeError("online history indexing requires PostgreSQL")
            try:
                exempt_from_start_pressure(raw)
                raw.commit()
                raw.autocommit = True
                with raw.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT pg_try_advisory_lock(hashtext(current_database()), "
                                "hashtext(current_schema() || '.history_indexing_v1')) AS ok")
                    locked = cur.fetchone()["ok"]
                    schema_locked = False
                    if locked:
                        from app.db.database import SCHEMA_INIT_LOCK_KEY, ONLINE_DDL_APPLICATION_NAME
                        cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (SCHEMA_INIT_LOCK_KEY,))
                        schema_locked = cur.fetchone()["ok"]
                        if schema_locked:
                            cur.execute("SELECT set_config('application_name',%s,false)",
                                        (ONLINE_DDL_APPLICATION_NAME,))
                    yield cur if locked and schema_locked else None
            finally:
                # Closing also releases our advisory locks and settings. Do it
                # before get_db returns the slot, not after another borrower
                # could have acquired the same backend.
                raw.close()
            # get_db normally commits on success. Its exception path instead
            # rolls back quietly and reclaims this already-closed connection.
            raise _OnlineCursorComplete
    except _OnlineCursorComplete:
        pass


def _concurrent_ddl(cur: Any, table: str, statement: str) -> None:
    """Check the metadata lock quickly, then let the statement wait for anyone.

    CONCURRENTLY also uses lock_timeout for its waits on writers and old
    snapshots, so 250ms would fail it behind ordinary traffic and leave an
    INVALID index. Those waits block nobody: its ShareUpdateExclusive lock does
    not conflict with DML, and production's own timeouts end long transactions.
    Keep the build free of any statement limit too; restarting the heap scans
    could starve a slow worker for ever.
    """
    cur.execute("BEGIN")
    try:
        cur.execute(f"LOCK TABLE {table} IN SHARE UPDATE EXCLUSIVE MODE")
    except BaseException:
        cur.execute("ROLLBACK")
        raise
    cur.execute("COMMIT")
    cur.execute("SET lock_timeout = '0'")
    cur.execute("SET statement_timeout = '0'")
    try:
        cur.execute(statement)
    finally:
        cur.execute("SET lock_timeout = '250ms'")
        cur.execute("SET statement_timeout = '1h'")


def _tablespace_clause(cur: Any, name: str) -> str:
    """Keep a replacement where its original lives, the place _volumes measures."""
    cur.execute("SELECT quote_ident(t.spcname) AS name FROM pg_class c "
                "JOIN pg_tablespace t ON t.oid = c.reltablespace "
                "WHERE c.oid = to_regclass(%s)", (name,))
    row = cur.fetchone()
    return f" TABLESPACE {row['name']}" if row else ""


def _disk(path: Any) -> tuple[int, int]:
    """(device, free bytes) of the filesystem holding this path."""
    return os.stat(path).st_dev, shutil.disk_usage(path).free


def _volumes(cur: Any, name: str) -> list[tuple[str, int, int, int]]:
    """(label, device, free bytes, bytes needed) of each place a rebuild writes."""
    from app.core.config import settings
    from app.core.private_storage import service_files
    from app.maintenance.session_recall_cache import managed_data_directory

    cur.execute("SELECT pg_relation_size(c.oid) AS bytes, "
                "NULLIF(pg_tablespace_location(c.reltablespace), '') AS place "
                "FROM pg_class c WHERE c.oid = to_regclass(%s)", (name,))
    row = cur.fetchone()
    data = managed_data_directory(cur, settings.effective_data_dir)
    needed = int(row["bytes"]) if row else 0
    places = [("data", row["place"] if row and row["place"] else data, needed),
              ("WAL", data / "pg_wal", needed)]
    with service_files():
        return [(label, *_disk(path), need) for label, path, need in places]


def _space_problem(cur: Any, name: str) -> str | None:
    """Why rebuilding this index now is unsafe for the PostgreSQL volumes, if so."""
    from app.maintenance.session_recall_cache import ManagedDataDirectoryError

    try:
        volumes = _volumes(cur, name)
    except (ManagedDataDirectoryError, OSError, ValueError):
        return ("its free disk space cannot be measured, because PostgreSQL is not "
                "this worker's own managed database")
    need_by_device: dict[int, int] = {}
    for _, device, _, need in volumes:
        need_by_device[device] = need_by_device.get(device, 0) + need
    for label, device, free, _ in volumes:
        need = need_by_device[device] + BUILD_RESERVE_BYTES
        if free < need:
            return (f"the {label} volume has {free / 1024 ** 3:.1f} GiB free and this step "
                    f"needs {need / 1024 ** 3:.1f} GiB; free some disk space")
    return None


def migrate() -> bool:
    """One online migration pass. No startup transaction or table rewrite.

    Metadata locks are capped at 250ms; see _concurrent_ddl for the builds.
    Concurrent builds survive cancellation
    as invalid indexes, which the next pass drops and retries. The original
    index remains usable until *both* replacement indexes are valid. An advisory
    lock serializes migrators, not writers. Imports defer only after publication
    of the final ready marker. Ordinary old rows stay state 0 without a backfill.
    """
    global _retry_build_at
    if time.monotonic() < _retry_build_at:
        return False
    with _online_cursor() as cur:
        if cur is None:
            return False
        cur.execute("SET lock_timeout = '250ms'")
        cur.execute("SET statement_timeout = '1h'")
        # An index with no tablespace of its own stays with the database, as
        # _volumes assumes, whatever a role or database setting says.
        cur.execute("SET default_tablespace = ''")
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
                wanted = set(_HISTORY_OPTIONS.split(", "))
                if not wanted.issubset(options):
                    cur.execute(f"ALTER INDEX {name}_h2 SET ({_HISTORY_OPTIONS})")
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
            tablespace = _tablespace_clause(cur, name)
            for state in (0, 2):
                replacement = name + f"_h{state}"
                if not _valid(cur, replacement):
                    _concurrent_ddl(cur, table, f"DROP INDEX CONCURRENTLY IF EXISTS {replacement}")
                    # Only the live half holds existing rows; history starts empty.
                    problem = _space_problem(cur, name) if state == 0 else None
                    if problem:
                        _retry_build_at = time.monotonic() + BUILD_RETRY_SECONDS
                        logger.warning("History index upgrade for %s is waiting: %s. Moving "
                                       "long chats stays slower until then; it retries by itself.",
                                       name, problem)
                        return False
                    where = f"({original_predicate}) AND " if sep else ""
                    if state == 2:
                        options = f" WITH ({_HISTORY_OPTIONS})"
                    else:
                        options = " WITH " + original_options if has_options else ""
                    _concurrent_ddl(cur, table, f"CREATE INDEX CONCURRENTLY {replacement} ON {table} "
                                    f"USING gin {expression}{options}{tablespace} WHERE "
                                    f"{where}{COLUMN} = {state}")
            _concurrent_ddl(cur, table, f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            cur.execute(f"ALTER INDEX {name}_h0 RENAME TO {name}")
            # Yield between indexes. A restart resumes from the valid ones.
            return False
        for table, (keys, _) in TABLES.items():
            queue = f"idx_{table}_history_pending"
            if not _valid(cur, queue):
                _concurrent_ddl(cur, table, f"DROP INDEX CONCURRENTLY IF EXISTS {queue}")
                _concurrent_ddl(cur, table, f"CREATE INDEX CONCURRENTLY {queue} ON {table} ({keys}) "
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


def flush_pending(
    *, can_work: Callable[[], bool] = lambda: True,
    quiet: float | None = None, poll: float = 1.0,
) -> bool:
    """Merge the history GINs' pending lists; True once nothing is left to merge.

    Live writers never use these lists. PostgreSQL applies a merge as physical
    index changes, so a cancelled or crashed one keeps what it finished and the
    rest stays searchable in the list. That is what makes it safe to cancel the
    statement as soon as a foreground turn or another import starts, and to
    retry later.
    """
    global _unmerged_at
    started = _unmerged_at
    if started is None:
        return True

    def clear_to_run() -> bool:
        return not _active_imports and can_work()

    if time.monotonic() - started < (FLUSH_QUIET_SECONDS if quiet is None else quiet) or not clear_to_run():
        return False
    try:
        with _online_cursor() as cur:
            if cur is None:
                return False
            cur.execute("SET lock_timeout = '0'")
            cur.execute("SET statement_timeout = '10min'")
            for name in INDEXES:
                cur.execute("SELECT to_regclass(%s) AS name", (name + "_h2",))
                if not cur.fetchone()["name"]:
                    continue
                if not clear_to_run():
                    return False
                done = threading.Event()

                def cancel_on_foreground() -> None:
                    while not done.wait(poll):
                        if not clear_to_run():
                            cur.connection.cancel()
                            return

                watcher = threading.Thread(target=cancel_on_foreground, daemon=True)
                watcher.start()
                try:
                    cur.execute("SELECT gin_clean_pending_list(%s::regclass)", (name + "_h2",))
                finally:
                    done.set()
                    watcher.join()
    except psycopg2.Error:
        # A foreground turn, the time limit or a lost connection. Retry later.
        logger.info("History index merge deferred; will retry", exc_info=True)
        return False
    if _unmerged_at == started:
        _unmerged_at = None
    return True


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
    try:
        if not can_work() or not migrate():
            return False
    except psycopg2.Error:
        # A busy table, cancellation or a lost connection: invalid leftovers are
        # repaired by the next pass, which must not wait out startup's back-off.
        logger.info("History index migration deferred; will retry", exc_info=True)
        return False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not can_work():
            return False
        if not promote_batch(can_work=can_work):
            return flush_pending(can_work=can_work)
        note_history_write()
        time.sleep(pause)
    return False

"""
PostgreSQL-on-SQLite compatibility shim for the test suite.

The application is PostgreSQL-only: every domain module funnels DB access
through ``app.db.base.get_connection`` (called by name inside ``get_db`` /
``get_cursor`` / ``transaction``) and writes PostgreSQL-flavoured SQL
(``%s`` placeholders, ``JSONB``, ``NOW()``, ``BIGSERIAL``, ``tsvector`` …).

There is no PostgreSQL server available in local/dev/CI sandboxes, so the
test harness runs against an in-memory SQLite database instead. This module
provides a thin translation layer so that the *real* app SQL executes on
SQLite:

* ``translate_sql`` rewrites the handful of PostgreSQL-isms the domain code
  uses into their SQLite equivalents (placeholders, type names, ``NOW()``,
  ``ILIKE``, aggregate names, ``::`` casts).
* Statements SQLite cannot model at all (GIN indexes, ``plpgsql`` ``DO $$``
  blocks, trigger/function bodies) are skipped — they only back Postgres-only
  features (FTS) that the corresponding tests already special-case.
* ``ShimConnection`` / ``ShimCursor`` mimic the ``_PgDictConnection`` +
  ``RealDictCursor`` contract the app relies on: ``.cursor()`` returns dict
  rows keyed by column name, regardless of a requested ``cursor_factory``.

The canonical schema (``app.db.schema_postgres.create_postgres_schema``) is
executed *through* the shim to build the test schema, so the test database
always matches the app's real schema — no more hand-maintained drift.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import date, datetime


# ---------------------------------------------------------------------------
# Shared-connection transaction serialization
# ---------------------------------------------------------------------------
#
# Production hands every ``get_db()`` / ``asyncio.to_thread`` caller its OWN
# pooled psycopg2 connection, so two concurrent writers never share a
# transaction. The test harness instead funnels every caller through ONE shared
# ``check_same_thread=False`` SQLite connection (see ``conftest`` /
# ``route_db_to``). Two ``asyncio.to_thread`` writers can then interleave their
# implicit ``BEGIN``s on that one connection and either
#
#   * raise ``sqlite3.OperationalError: cannot start a transaction within a
#     transaction`` (both threads auto-BEGIN in the check-then-BEGIN window), or
#   * clobber each other's uncommitted writes (one thread's ``commit()`` flushes
#     the other's half-written statement, or the raised error above aborts a
#     write) — a lost ``UPDATE`` surfaces downstream as, e.g.,
#     ``parked_task_kind`` reading ``None`` when the test just wrote ``'agent'``.
#
# Hold this reentrant lock for the life of each WRITE transaction — from the
# statement that opens it through its ``commit()`` / ``rollback()`` — so only
# one writer touches the shared connection at a time, restoring the
# per-connection isolation the real pool provides. Reentrant (``RLock``) so a
# nested ``get_db()`` on the same thread never deadlocks.
#
# CRITICAL: a statement that leaves NO transaction open (any SELECT, or an
# auto-committed statement) releases the calling thread's claim IMMEDIATELY,
# inside ``execute``/``executemany`` itself. Read-only code paths never call
# ``commit()`` (several exist — e.g. pool users that ``putconn`` after a bare
# SELECT), so holding until commit/rollback/close would leak the lock forever
# from an idle worker thread and deadlock the whole suite at the next writer
# (proven: resource_monitor's periodic cleanup blocked the event loop while a
# finished read still owned the lock). Only a genuinely OPEN write transaction
# may hold across statements. As a backstop, acquisition times out loudly
# instead of hanging silently.
#
# The acquire depth is tracked PER THREAD (``threading.local``), never on the
# connection object: ``RLock`` ownership is per-thread, and the whole point of
# this lock is that several threads share one ShimConnection — per-connection
# bookkeeping would let thread B's ``commit()`` "release" acquires actually
# owned by thread A (RuntimeError from the non-owner release, plus A's acquire
# leaked forever = executor-shutdown deadlock). ``commit``/``rollback``/
# ``close`` release exactly the acquires the CALLING thread holds; that always
# matches the real SQLite state because the shared connection has a single
# transaction, which any ``commit()`` on any wrapper ends.
_TXN_LOCK = threading.RLock()
_txn_local = threading.local()  # .depth = acquires held by this thread


def _txn_acquire() -> None:
    """Take the shared transaction lock for the calling thread (one per
    statement; the depth is repaid in full by ``_txn_release_all``).

    Times out rather than hanging: if the lock is held for 60s the owner has
    leaked it (a write transaction with no commit/rollback/close), and a loud
    error beats a silently frozen CI job."""
    if not _TXN_LOCK.acquire(timeout=60):
        raise RuntimeError(
            "pg_shim: shared transaction lock held >60s — some code path "
            "opened a write transaction and never reached commit/rollback/"
            "close. Find the owner thread with py-spy."
        )
    _txn_local.depth = getattr(_txn_local, "depth", 0) + 1


def _txn_release_all() -> None:
    """Release every acquire the CALLING thread holds (commit/rollback/close
    ends the shared connection's single transaction, so the thread's whole
    claim is repaid at once)."""
    depth = getattr(_txn_local, "depth", 0)
    _txn_local.depth = 0
    for _ in range(depth):
        _TXN_LOCK.release()


# ---------------------------------------------------------------------------
# SQL translation
# ---------------------------------------------------------------------------

_NAMED_PARAM_RE = re.compile(r"%\((\w+)\)s")
_CAST_RE = re.compile(r"::\w+")


def translate_sql(sql: str) -> str:
    """Rewrite PostgreSQL SQL into an SQLite-compatible equivalent.

    Only the constructs actually used by the domain layer are handled; the
    translation is intentionally lexical (regex-based) rather than a full
    parser, which is sufficient for the app's static query strings.
    """
    s = sql

    # --- parameter placeholders -------------------------------------------
    # Protect psycopg2 '%%' escapes, translate named + positional params,
    # then restore literal percents.
    s = s.replace("%%", "\x00PCT\x00")
    s = _NAMED_PARAM_RE.sub(r":\1", s)        # %(name)s -> :name
    s = s.replace("%s", "?")                   # positional -> qmark
    s = s.replace("\x00PCT\x00", "%")

    # --- type names (DDL) -------------------------------------------------
    # BIGSERIAL/SERIAL PRIMARY KEY -> INTEGER PRIMARY KEY (SQLite rowid alias
    # auto-increments and exposes lastrowid, matching BIGSERIAL semantics).
    s = re.sub(r"\bBIGSERIAL\b", "INTEGER", s, flags=re.I)
    s = re.sub(r"\bSERIAL\b", "INTEGER", s, flags=re.I)
    s = re.sub(r"\bJSONB\b", "TEXT", s, flags=re.I)
    s = re.sub(r"\bJSON\b", "TEXT", s, flags=re.I)          # after JSONB
    s = re.sub(r"\btsvector\b", "TEXT", s, flags=re.I)
    s = re.sub(r"\bTIMESTAMPTZ\b", "TIMESTAMP", s, flags=re.I)
    s = re.sub(r"TIMESTAMP\s+WITH(?:OUT)?\s+TIME\s+ZONE", "TIMESTAMP", s, flags=re.I)
    s = re.sub(r"\bDOUBLE\s+PRECISION\b", "REAL", s, flags=re.I)

    # --- expressions ------------------------------------------------------
    s = re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", s, flags=re.I)
    # make_interval(secs => X) is used only in the installs "online" window
    # check: `<ts> - make_interval(secs => X)`. SQLite has no make_interval and
    # chokes on the `=>` named-arg syntax, so rewrite the whole subexpression to
    # SQLite's datetime() modifier form. The bound placeholder stays in place, so
    # parameter order is preserved.
    s = re.sub(
        r"(?:CURRENT_TIMESTAMP|NOW\(\))\s*-\s*make_interval\(\s*secs\s*=>\s*([^)]+)\)",
        r"datetime('now', '-' || \1 || ' seconds')",
        s,
        flags=re.I,
    )
    # `NOW() + (<n> * INTERVAL '1 second')` — used by app.db.runs to compute a
    # lease expiry from a ttl-in-seconds param. SQLite has no INTERVAL literal;
    # rewrite to datetime() second-modifier arithmetic. The bound placeholder
    # (<n>, already qmark) stays in place, preserving parameter order; the
    # concat form handles positive, zero, and negative ttls.
    s = re.sub(
        r"(?:CURRENT_TIMESTAMP|NOW\(\))\s*\+\s*\(\s*([^)*]+?)\s*\*\s*INTERVAL\s*'1 second'\s*\)",
        r"datetime('now', (\1) || ' seconds')",
        s,
        flags=re.I,
    )
    # Row-lock clauses (`FOR SHARE` / `FOR UPDATE`) — no-ops on SQLite's single
    # writer; strip them so the SELECT parses.
    s = re.sub(r"\bFOR\s+SHARE\b", "", s, flags=re.I)
    s = re.sub(r"\bFOR\s+UPDATE\b", "", s, flags=re.I)
    s = _CAST_RE.sub("", s)                    # x::date -> x
    s = re.sub(r"\bILIKE\b", "LIKE", s, flags=re.I)
    s = re.sub(r"\bjsonb_agg\b", "json_group_array", s, flags=re.I)
    s = re.sub(r"\bjson_agg\b", "json_group_array", s, flags=re.I)
    s = re.sub(r"\bstring_agg\b", "group_concat", s, flags=re.I)

    # --- schema introspection ---------------------------------------------
    # The app checks table/column existence and nullability via
    # information_schema (no equivalent in SQLite). Rewrite the three fixed
    # query shapes emitted by app.db.base / app.db.compat to SQLite's
    # sqlite_master / pragma_table_info. Placeholders are already qmark and
    # bind order (table[, column]) is preserved.
    if "information_schema.tables" in s:
        return (
            "SELECT name AS table_name FROM sqlite_master "
            "WHERE type='table' AND name = ?"
        )
    if "information_schema.columns" in s:
        if "is_nullable" in s:
            return (
                "SELECT CASE \"notnull\" WHEN 1 THEN 'NO' ELSE 'YES' END "
                "AS is_nullable FROM pragma_table_info(?) WHERE name = ?"
            )
        return (
            "SELECT name AS column_name FROM pragma_table_info(?) "
            "WHERE name = ?"
        )

    return s


# Statements SQLite cannot execute at all. These back Postgres-only FTS
# infrastructure; the tests that exercise search skip themselves on SQLite.
def _should_skip(sql: str) -> bool:
    up = sql.upper()
    if "$$" in sql:                                    # plpgsql DO / function body
        return True
    if "USING GIN" in up or "USING GIST" in up:        # GIN/GiST indexes
        return True
    if "ALTER COLUMN" in up and "SET DEFAULT" in up:   # SQLite can't alter defaults
        return True
    stripped = up.lstrip()
    return stripped.startswith((
        "CREATE EXTENSION",
        "CREATE OR REPLACE FUNCTION",
        "CREATE FUNCTION",
        "CREATE TRIGGER",
    ))


# ---------------------------------------------------------------------------
# Cursor / connection wrappers
# ---------------------------------------------------------------------------

class ShimCursor:
    """Wraps a sqlite3 cursor: translates SQL and returns dict rows.

    Mirrors psycopg2's ``RealDictCursor`` — every fetched row is a plain
    ``dict`` keyed by column name, which is how the domain code accesses rows.
    """

    def __init__(self, sqlite_cursor: sqlite3.Cursor, serialize: bool = False):
        self._cur = sqlite_cursor
        # True for cursors handed out by a ShimConnection (which multiple
        # threads share — see _TXN_LOCK). False for the standalone schema-build
        # cursor (single-threaded), which needs no serialization.
        self._serialize = serialize

    def execute(self, sql, params=None):
        tsql = translate_sql(sql)
        if _should_skip(tsql):
            return self
        if not self._serialize:
            self._cur.execute(tsql, params if params is not None else ())
            return self
        _txn_acquire()
        try:
            self._cur.execute(tsql, params if params is not None else ())
        finally:
            self._release_if_idle()
        return self

    def executemany(self, sql, seq_of_params):
        tsql = translate_sql(sql)
        if _should_skip(tsql):
            return self
        if not self._serialize:
            self._cur.executemany(tsql, seq_of_params)
            return self
        _txn_acquire()
        try:
            self._cur.executemany(tsql, seq_of_params)
        finally:
            self._release_if_idle()
        return self

    def _release_if_idle(self):
        """Release the calling thread's whole lock claim unless the statement
        left a transaction OPEN on the shared connection.

        Reads (and auto-committed statements) must not hold the lock past their
        own execution: read-only code paths never call ``commit()``, so a claim
        held here would leak forever and deadlock the next writer. The check is
        race-free because only lock holders can execute statements; a
        concurrent ``commit()`` from another thread ending our transaction only
        means releasing now is MORE correct, not less."""
        if not self._cur.connection.in_transaction:
            _txn_release_all()

    @staticmethod
    def _as_dict(row):
        return dict(row) if row is not None else None

    def fetchone(self):
        return self._as_dict(self._cur.fetchone())

    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany() if size is None else self._cur.fetchmany(size)
        return [dict(r) for r in rows]

    def __iter__(self):
        for r in self._cur:
            yield dict(r)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def description(self):
        return self._cur.description

    def close(self):
        return self._cur.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ShimConnection:
    """Wraps a sqlite3 connection to mimic ``_PgDictConnection``.

    ``.cursor()`` always yields a dict-row :class:`ShimCursor`, ignoring any
    requested ``cursor_factory`` (the app only ever wants RealDictCursor
    behaviour). ``.autocommit`` is accepted as a no-op attribute because the
    real connection wrapper exposes it.
    """

    def __init__(self, sqlite_conn: sqlite3.Connection):
        object.__setattr__(self, "_conn", sqlite_conn)
        object.__setattr__(self, "autocommit", False)

    def cursor(self, *args, cursor_factory=None, **kwargs):
        return ShimCursor(self._conn.cursor(), serialize=True)

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        try:
            return self._commit_or_rollback(self._conn.commit)
        finally:
            _txn_release_all()

    def rollback(self):
        try:
            return self._commit_or_rollback(self._conn.rollback)
        finally:
            _txn_release_all()

    @staticmethod
    def _commit_or_rollback(fn):
        """Run commit/rollback with psycopg2's idle-is-a-no-op semantics.

        psycopg2 (and Postgres) treat ``commit()``/``rollback()`` on a
        connection with no open transaction as a harmless no-op — the real
        ``_PgDictConnection`` the app uses does exactly that. SQLite instead
        raises ``OperationalError: cannot commit - no transaction is active``.

        The harness shares ONE ``check_same_thread=False`` connection across
        Starlette's ``BaseHTTPMiddleware`` task group, so a read-only
        ``get_db()`` body (which still calls ``conn.commit()`` on exit) can
        reach commit after another task already ended the (auto-)transaction —
        a race that surfaces only under interleaving and flakes CI. Swallow ONLY
        that idle-transaction error, matching psycopg2; any real failure
        (locked db, disk full, …) still propagates.
        """
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "no transaction is active" in str(e):
                return None
            raise

    def close(self):
        try:
            return self._conn.close()
        finally:
            # Defensive: never strand this thread's claim if a transaction
            # was left open when the connection went away.
            _txn_release_all()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)


# ---------------------------------------------------------------------------
# Connection + schema construction
# ---------------------------------------------------------------------------

# Bind Python objects the domain code passes verbatim (psycopg2 adapts these;
# SQLite needs explicit adapters). JSON columns are usually pre-serialised with
# json.dumps, but register dict/list defensively so a raw payload still binds.
sqlite3.register_adapter(dict, lambda d: json.dumps(d))
sqlite3.register_adapter(list, lambda v: json.dumps(v))
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat(sep=" "))
sqlite3.register_adapter(date, lambda d: d.isoformat())


from contextlib import contextmanager


@contextmanager
def route_db_to(raw_conn: sqlite3.Connection):
    """Route the app DB layer at a shim-wrapped SQLite connection.

    Redirects both chokepoints the domain code uses:

    * ``app.db.base.get_connection`` — resolved *by name* inside base's
      ``get_db`` / ``get_cursor`` / ``transaction``, so patching it here
      redirects the entire DB layer.
    * ``app.db.database.get_connection`` — the one module that imports
      ``get_connection`` directly, needing its own patch point.

    Both are pointed at a :class:`ShimConnection` wrapping ``raw_conn`` so that
    PostgreSQL SQL is translated to SQLite and rows come back as dicts, exactly
    as the global ``conftest`` autouse fixture does for the API tests. The raw
    connection is left untouched for tests that execute their own
    ``?``-placeholder SQL directly.
    """
    import app.db.base as base
    import app.db.database as database
    from unittest.mock import patch

    shim = ShimConnection(raw_conn)

    def _get_connection():
        return shim

    with patch.object(base, "get_connection", _get_connection), \
            patch.object(database, "get_connection", _get_connection):
        yield shim


def new_sqlite_connection() -> sqlite3.Connection:
    """Create a fresh in-memory SQLite connection with the app schema built.

    The schema is created by executing the canonical
    ``create_postgres_schema`` through :class:`ShimCursor`, so it stays in
    lockstep with the production schema definition.
    """
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")

    from app.db.schema_postgres import create_postgres_schema

    create_postgres_schema(ShimCursor(raw.cursor()))
    raw.commit()
    return raw

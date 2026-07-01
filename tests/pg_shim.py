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
from datetime import date, datetime


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

    def __init__(self, sqlite_cursor: sqlite3.Cursor):
        self._cur = sqlite_cursor

    def execute(self, sql, params=None):
        tsql = translate_sql(sql)
        if _should_skip(tsql):
            return self
        self._cur.execute(tsql, params if params is not None else ())
        return self

    def executemany(self, sql, seq_of_params):
        tsql = translate_sql(sql)
        if _should_skip(tsql):
            return self
        self._cur.executemany(tsql, seq_of_params)
        return self

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
        return ShimCursor(self._conn.cursor())

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

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

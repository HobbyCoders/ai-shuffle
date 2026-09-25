"""Helpers for ``test_schema_lifecycle.py`` and its child process (not collected).

The app schema is PostgreSQL-only and has no per-step migration list: boot runs
``app.db.database.init_database()``, which (below ``SCHEMA_VERSION``) replays
the idempotent ``create_postgres_schema`` bootstrap and then the guarded
``_run_column_migrations`` pass. These helpers run that real entry point
against a FILE-backed SQLite database through the test shim, so a second
process can open the same file.

``_LifecycleCursor`` models two PostgreSQL behaviours the shared shim does not:

* ``ALTER TABLE .. ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS``
  (SQLite has neither form; the upgrade path uses both).
* ``pg_advisory_xact_lock`` — a lock held until the transaction ends. Mapped to
  ``BEGIN IMMEDIATE``, which holds SQLite's single write lock until
  commit/rollback, the same scope.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]

_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.I)
_DROP_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+COLUMN\s+IF\s+EXISTS\s+(\w+)", re.I)


def connect(path, timeout: float = 5.0) -> sqlite3.Connection:
    """A file-backed connection shaped like ``tests.pg_shim.new_sqlite_connection``."""
    from tests.pg_shim import _ShimSQLiteConnection

    raw = sqlite3.connect(str(path), timeout=timeout, check_same_thread=False,
                          factory=_ShimSQLiteConnection, cached_statements=0)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return raw


def _lifecycle_classes():
    from tests.pg_shim import ShimConnection, ShimCursor

    class _LifecycleCursor(ShimCursor):
        def execute(self, sql, params=None):
            raw = self._cur.connection
            if "pg_advisory_xact_lock" in sql:
                if not raw.in_transaction:
                    raw.execute("BEGIN IMMEDIATE")
                return self
            for pattern, run_when_present in ((_ADD_COLUMN, False), (_DROP_COLUMN, True)):
                match = pattern.search(sql)
                if match:
                    table, column = match.groups()
                    present = column in {r[1] for r in raw.execute(f"PRAGMA table_info('{table}')")}
                    if present != run_when_present:
                        return self
                    sql = re.sub(r"\s+IF\s+(?:NOT\s+)?EXISTS", "", sql, count=1, flags=re.I)
            return super().execute(sql, params)

    class _LifecycleConnection(ShimConnection):
        def cursor(self, *args, **kwargs):
            return _LifecycleCursor(self._conn.cursor(), serialize=True)

    return _LifecycleCursor, _LifecycleConnection


@contextmanager
def route(raw: sqlite3.Connection):
    """Point the app DB layer at ``raw`` (like ``tests.pg_shim.route_db_to``)."""
    import app.db.base as base
    import app.db.database as database

    _, connection_class = _lifecycle_classes()
    shim = connection_class(raw)
    with patch.object(base, "get_connection", lambda: shim), \
            patch.object(database, "get_connection", lambda: shim):
        yield shim


def boot(raw: sqlite3.Connection) -> None:
    """Run the real migration entry point against ``raw``."""
    from app.db import database

    with route(raw):
        database.init_database()


# ---------------------------------------------------------------------------
# Old-schema fixtures, built from git history at test time
# ---------------------------------------------------------------------------

def git_show(commit: str, path: str) -> str | None:
    """``git show commit:path``, or None when the object is not available
    (a shallow checkout, or no git at all)."""
    try:
        return subprocess.run(
            ["git", "show", f"{commit}:{path}"], cwd=REPO_ROOT, capture_output=True,
            text=True, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _module_from_source(name: str, source: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = f"<git:{name}>"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


@contextmanager
def _swap_modules(replacements: dict):
    """Serve ``replacements`` from ``sys.modules`` for the block, touching no
    other entry (``patch.dict`` would also evict modules first imported
    inside the block)."""
    saved = {name: sys.modules.get(name) for name in replacements}
    sys.modules.update(replacements)
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def build_old_database(raw: sqlite3.Connection, commit: str, *, with_column_pass: bool) -> bool:
    """Create the schema commit ``commit`` would have created on first boot.

    Runs that commit's own ``create_postgres_schema`` (with that commit's
    ``app.db.network`` / ``app.db.mobile_hub`` schema modules when it imports
    them) and, when asked, that commit's ``_run_column_migrations`` — the
    whole of its ``init_database`` for a fresh install. Returns False when git
    cannot supply the sources.
    """
    import json
    import logging

    import psycopg2

    from app.db import base, compat
    from tests.pg_shim import ShimCursor

    schema_source = git_show(commit, "app/db/schema_postgres.py")
    if schema_source is None:
        return False
    old_modules = {}
    for dotted, path in (("app.db.network", "app/db/network.py"),
                         ("app.db.mobile_hub", "app/db/mobile_hub.py")):
        if dotted in schema_source:
            source = git_show(commit, path)
            if source is None:
                return False
            old_modules[dotted] = _module_from_source(dotted, source)

    with _swap_modules(old_modules):
        schema = _module_from_source("old_schema_postgres", schema_source)
        cursor = ShimCursor(raw.cursor())
        schema.create_postgres_schema(cursor)
        if with_column_pass:
            database_source = git_show(commit, "app/db/database.py")
            if database_source is None:
                return False
            start = database_source.index("def _run_column_migrations(cursor):")
            end = database_source.index("def init_database():")
            namespace = {
                "json": json, "logger": logging.getLogger("old_database"), "psycopg2": psycopg2,
                "_DB_ERRORS": (psycopg2.Error, sqlite3.Error),
                "column_exists": base.column_exists, "table_exists": base.table_exists,
                "column_is_not_null": base.column_is_not_null,
                "_validate_identifier": base._validate_identifier, "ph": compat.ph,
            }
            exec(compile(database_source[start:end], "<git:old_database>", "exec"), namespace)
            namespace["_run_column_migrations"](cursor)
    raw.commit()
    return True


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

def _normalise_sql(sql: str | None) -> str:
    text = re.sub(r"\s+", " ", sql or "").strip()
    text = re.sub(r"\bIF NOT EXISTS\s+", "", text, flags=re.I)
    return text.replace("( ", "(").replace(" )", ")")


def schema(raw: sqlite3.Connection) -> dict:
    """Structural snapshot of every table and index.

    Tables are compared by column set (name, type, NOT NULL, default, pk) and
    foreign keys rather than by CREATE text: ``ALTER TABLE .. ADD COLUMN``
    appends to the stored text, so an upgraded table's text differs from a
    fresh one's even when the columns match. Index text is compared verbatim
    after whitespace / ``IF NOT EXISTS`` normalisation. SQLite's implicit
    ``sqlite_autoindex_*`` (inline UNIQUE) are internal and left out.
    """
    out = {}
    for row in raw.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        kind, name = row["type"], row["name"]
        if kind == "table":
            columns = {
                c[1]: ((c[2] or "").upper(), c[3], None if c[4] is None else str(c[4]), c[5])
                for c in raw.execute(f"PRAGMA table_info('{name}')")
            }
            fks = sorted(tuple(str(v) for v in tuple(fk)[2:])
                         for fk in raw.execute(f"PRAGMA foreign_key_list('{name}')"))
            out[("table", name)] = {"columns": columns, "fks": fks}
        else:
            out[(kind, name)] = {"table": row["tbl_name"], "sql": _normalise_sql(row["sql"])}
    return out


def dump(raw: sqlite3.Connection) -> dict:
    """Every row of every table, order-independent."""
    tables = [r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {
        t: sorted(repr(tuple(r)) for r in raw.execute(f'SELECT * FROM "{t}"'))
        for t in tables
    }

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
* ``ALTER TABLE .. DROP CONSTRAINT IF EXISTS <table>_<column>_fkey`` (the name
  PostgreSQL gives an unnamed foreign key). SQLite cannot alter a table's
  constraints, so the clause is removed from the stored table definition; a
  foreign key does not change the on-disk format (SQLite's documented
  ``writable_schema`` procedure for removing FOREIGN KEY constraints).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.I)
_DROP_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+COLUMN\s+IF\s+EXISTS\s+(\w+)", re.I)
_DROP_FKEY = re.compile(
    r"^\s*ALTER\s+TABLE\s+(\w+)\s+DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+(\w+)_fkey\s*$", re.I)


def _drop_foreign_key(raw: sqlite3.Connection, table: str, column: str) -> None:
    """Remove the table-level ``FOREIGN KEY (column) ..`` clause, if present."""
    row = raw.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                      (table,)).fetchone()
    if row is None:
        return
    clause = re.compile(
        r",\s*FOREIGN\s+KEY\s*\(\s*" + re.escape(column) + r"\s*\)\s*REFERENCES\s+(?:\w+|\"[^\"]+\")\s*"
        r"\([^)]*\)(?:\s+ON\s+(?:DELETE|UPDATE)\s+(?:CASCADE|RESTRICT|NO\s+ACTION|SET\s+NULL|SET\s+DEFAULT))*",
        re.I)
    new_sql, count = clause.subn("", row[0])
    if not count:
        return
    version = raw.execute("PRAGMA schema_version").fetchone()[0]
    raw.execute("PRAGMA writable_schema = ON")
    try:
        raw.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
                    (new_sql, table))
        raw.execute(f"PRAGMA schema_version = {version + 1}")
    finally:
        raw.execute("PRAGMA writable_schema = OFF")


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
            match = _DROP_FKEY.match(sql)
            if match:
                table, name = match.groups()
                prefix = f"{table.lower()}_"
                if name.lower().startswith(prefix):
                    _drop_foreign_key(raw, table, name[len(prefix):])
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
# Portable old-schema fixtures
# ---------------------------------------------------------------------------

SCHEMA_FIXTURES = Path(__file__).with_name("fixtures")


def build_old_database(raw: sqlite3.Connection, version: int) -> None:
    """Load a frozen schema-only fixture, never today's schema or git history.

    v49 includes the old column pass; v88 is the bootstrap-only boundary.
    The SQL contains only table/index definitions and the schema version row;
    application data is seeded explicitly by each lifecycle test.
    """
    if version not in (49, 88):
        raise ValueError(f"Unsupported schema fixture: {version}")
    raw.executescript((SCHEMA_FIXTURES / f"schema_v{version}.sql").read_text(encoding="utf-8"))
    raw.commit()


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


# ---------------------------------------------------------------------------
# A private real PostgreSQL 16 cluster (for the concurrent-start test)
# ---------------------------------------------------------------------------

def pg16_bindir() -> str | None:
    """PostgreSQL 16 server binaries, found as the boot smoke test finds them
    (``SMOKE_PG_BIN``, ``initdb`` on PATH, ``/usr/lib/postgresql/16/bin``)."""
    from tests.smoke.test_app_boot import _pg16_bindir

    return _pg16_bindir()


@contextmanager
def private_postgres(bindir: str, data_dir: Path):
    """Start a throwaway cluster the way the container supervisor does and
    publish ``managed-postgres.json`` in ``data_dir`` (the app's ``DATA_DIR``).

    Yields the descriptor (host, port, user, password, database)."""
    import json
    import os
    import socket
    from types import SimpleNamespace

    from tests.smoke.test_app_boot import _load_container_runtime

    runtime = _load_container_runtime()
    runtime.STOP.clear()

    class ThrowawayDatabase(runtime.Database):
        """The supervisor's cluster without durability work nobody needs here.

        ``close()`` gives PostgreSQL 30 s for a fast shutdown, whose shutdown
        checkpoint fsyncs every relation file the schema created (thousands of
        files; about 25 s on a loaded loop-device disk), so teardown could
        miss the deadline. A throwaway cluster skips initdb's sync and runs
        with ``fsync = off``; start-up and shutdown are otherwise the
        supervisor's own."""

        def command(self, args, **kwargs):
            if Path(args[0]).name == "initdb" and "-D" in args:
                args = [*args, "--no-sync"]
            return super().command(args, **kwargs)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    db = ThrowawayDatabase(SimpleNamespace(effective_data_dir=data_dir), port=port)
    try:
        with patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}):
            db.open()
            # fsync is a reload-time setting: no restart, the same process.
            db.admin_script("ALTER SYSTEM SET fsync = off;\nSELECT pg_reload_conf();")
        db.publish_descriptor()
        yield json.loads((data_dir / "managed-postgres.json").read_text())
    finally:
        db.close()


def pg_connect(descriptor: dict):
    """A plain autocommit psycopg2 connection to the private cluster."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(
        host=descriptor.get("host", "127.0.0.1"), port=descriptor["port"],
        user=descriptor["user"], password=descriptor["password"],
        dbname=descriptor["database"], cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn

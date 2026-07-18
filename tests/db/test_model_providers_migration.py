"""Migration-ordering test for the v64 model_providers table.

v64 adds the ``model_providers`` table (admin-configured non-Anthropic model
gateways). The table is a self-contained block WITH its index inline in
``create_postgres_schema`` (immune to the schema-pass-before-column-migrations
ordering trap that crashed worker startup for v63's idx_sessions_active), and a
defensive idempotent CREATE-TABLE bootstrap lives in ``_run_column_migrations``.

This mirrors the v63 idx_sessions_active regression test
(tests/db/test_active_migration.py): it stamps a DB at v63 without the new
table, runs the REAL ``init_database()`` path, and asserts the table + its
index exist and the version is bumped to 64 — with no exception. A fresh-DB
init test is included too.
"""

from app.db import database


def _table_exists(conn, name):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _index_exists(conn, name):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone() is not None


def _schema_version(conn):
    return conn.execute("SELECT version FROM schema_version").fetchone()[0]


def test_fresh_db_has_model_providers_table_index_and_version(mock_db):
    """A freshly built DB (create_postgres_schema) carries the table, its index,
    and schema version 64."""
    conn = mock_db
    assert _table_exists(conn, "model_providers")
    assert _index_exists(conn, "idx_model_providers_enabled")
    assert _schema_version(conn) == 64


def test_init_upgrades_pre_v64_db(mock_db):
    """EXISTING-DB path: stamp v63 and drop the table, then run the real
    init_database() and assert the upgrade lands the table + index + version 64
    without raising."""
    conn = mock_db
    # Simulate a pre-v64 database: the base schema builds the v64 shape, so drop
    # the model_providers table + index and stamp the version back to 63.
    conn.execute("DROP INDEX IF EXISTS idx_model_providers_enabled")
    conn.execute("DROP TABLE IF EXISTS model_providers")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (63)")
    conn.commit()

    assert not _table_exists(conn, "model_providers")

    # Real init path (version gate fires because 63 < 64) — must not raise.
    database.init_database()

    assert _table_exists(conn, "model_providers")
    assert _index_exists(conn, "idx_model_providers_enabled")
    assert _schema_version(conn) == 64


def test_column_migrations_bootstrap_is_idempotent(mock_db):
    """The defensive bootstrap in _run_column_migrations re-runs cleanly when the
    table is missing AND when it already exists (no exception, no duplication)."""
    conn = mock_db
    conn.execute("DROP INDEX IF EXISTS idx_model_providers_enabled")
    conn.execute("DROP TABLE IF EXISTS model_providers")
    conn.commit()

    # First pass: bootstraps the table + index.
    database.init_database()
    assert _table_exists(conn, "model_providers")
    assert _index_exists(conn, "idx_model_providers_enabled")

    # Second boot: table already exists → guarded block is a no-op.
    database.init_database()
    assert _table_exists(conn, "model_providers")
    assert _index_exists(conn, "idx_model_providers_enabled")

"""Migration-ordering tests for the model_providers table (v64) and its
Codex/ChatGPT-subscription OAuth columns (v65).

v64 adds the ``model_providers`` table (admin-configured non-Anthropic model
gateways). The table is a self-contained block WITH its index inline in
``create_postgres_schema`` (immune to the schema-pass-before-column-migrations
ordering trap that crashed worker startup for v63's idx_sessions_active), and a
defensive idempotent CREATE-TABLE bootstrap lives in ``_run_column_migrations``.

v65 adds the Codex OAuth columns 1:1 on that same row (one account per provider
— plan amendment A1, no pool table): ``auth_mode``, the encrypted write-only
``codex_access_token`` / ``codex_refresh_token`` blobs, ``codex_account_id``,
``codex_id_token_expires_at``, ``codex_last_refresh``, ``needs_reauth``. These
are additive/nullable, added idempotently in ``_run_column_migrations`` (no
index, so no ordering trap), and included in the fresh ``create_postgres_schema``
CREATE TABLE. Because ``create_postgres_schema`` uses ``CREATE TABLE IF NOT
EXISTS``, an EXISTING v64 table is NOT altered by the schema pass — the guarded
ADD COLUMNs in ``_run_column_migrations`` are the sole upgrade path, so the
existing-DB test below is what actually exercises them.

This mirrors the v63 idx_sessions_active regression test
(tests/db/test_active_migration.py): it stamps a DB at the previous version,
runs the REAL ``init_database()`` path, and asserts the schema + version bump
without raising. Fresh-DB init tests are included too.
"""

from app.db import database


# The seven columns v65 adds to model_providers.
_CODEX_COLUMNS = (
    "auth_mode",
    "codex_access_token",
    "codex_refresh_token",
    "codex_account_id",
    "codex_id_token_expires_at",
    "codex_last_refresh",
    "needs_reauth",
)


def _table_exists(conn, name):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _index_exists(conn, name):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone() is not None


def _column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _schema_version(conn):
    return conn.execute("SELECT version FROM schema_version").fetchone()[0]


def test_fresh_db_has_model_providers_table_index_and_version(mock_db):
    """A freshly built DB (create_postgres_schema) carries the table, its index,
    the v65 Codex columns, and the current schema version (66)."""
    conn = mock_db
    assert _table_exists(conn, "model_providers")
    assert _index_exists(conn, "idx_model_providers_enabled")
    cols = _column_names(conn, "model_providers")
    for col in _CODEX_COLUMNS:
        assert col in cols, col
    assert _schema_version(conn) == 66


def test_init_upgrades_pre_v64_db(mock_db):
    """EXISTING-DB path: stamp v63 and drop the table, then run the real
    init_database() and assert the upgrade lands the table + index + Codex
    columns + the current version (66) without raising."""
    conn = mock_db
    # Simulate a pre-v64 database: the base schema builds the current shape, so
    # drop the model_providers table + index and stamp the version back to 63.
    conn.execute("DROP INDEX IF EXISTS idx_model_providers_enabled")
    conn.execute("DROP TABLE IF EXISTS model_providers")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (63)")
    conn.commit()

    assert not _table_exists(conn, "model_providers")

    # Real init path (version gate fires because 63 < 66) — must not raise.
    database.init_database()

    assert _table_exists(conn, "model_providers")
    assert _index_exists(conn, "idx_model_providers_enabled")
    cols = _column_names(conn, "model_providers")
    for col in _CODEX_COLUMNS:
        assert col in cols, col
    assert _schema_version(conn) == 66


def test_init_adds_codex_columns_to_existing_v64_table(mock_db):
    """EXISTING-DB path for v65: a v64-shaped model_providers table (phase-1
    columns only, no Codex columns) is upgraded in place — the guarded ADD
    COLUMNs in _run_column_migrations land all seven Codex columns and the
    version bumps to the current schema version (66), with the existing row
    preserved and no exception."""
    conn = mock_db
    # Rebuild the table with ONLY the phase-1 (v64) columns, then stamp v64.
    conn.execute("DROP INDEX IF EXISTS idx_model_providers_enabled")
    conn.execute("DROP TABLE IF EXISTS model_providers")
    # SQLite-friendly DDL (this runs against the raw connection, not the pg
    # shim): the exact column TYPES are irrelevant — all that matters is that
    # the table exists WITHOUT the v65 Codex columns so the ADD COLUMNs fire.
    conn.execute(
        """
        CREATE TABLE model_providers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            provider_type TEXT NOT NULL,
            base_url TEXT NOT NULL,
            auth_token TEXT,
            models_json TEXT DEFAULT '[]',
            default_model TEXT,
            small_fast_model TEXT,
            extra_env_json TEXT DEFAULT '{}',
            context_window INTEGER,
            max_output_tokens INTEGER,
            enabled INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO model_providers (id, name, provider_type, base_url) "
        "VALUES ('p-legacy', 'Legacy', 'openrouter', 'https://gw/api')"
    )
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (64)")
    conn.commit()

    cols = _column_names(conn, "model_providers")
    assert "codex_access_token" not in cols  # precondition: pre-v65 shape

    # Real init path (64 < 65) — must add the columns without raising.
    database.init_database()

    cols = _column_names(conn, "model_providers")
    for col in _CODEX_COLUMNS:
        assert col in cols, col
    assert _schema_version(conn) == 66
    # Existing row survived the ALTERs (new columns default NULL/FALSE).
    row = conn.execute(
        "SELECT name, codex_access_token, needs_reauth "
        "FROM model_providers WHERE id = 'p-legacy'"
    ).fetchone()
    assert row[0] == "Legacy"
    assert row[1] is None


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

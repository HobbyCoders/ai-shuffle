"""Migration-ordering test for the per-session provider binding column
(v66, multi-provider phase-2 Chunk E / ruling G2-r).

v66 adds ONE nullable ``sessions.provider_id TEXT`` column: the model_providers
id a session is bound to (stamped at creation from the profile's provider). NULL
= native Claude (gateway bypass, F1). One column only — one account per provider
(amendment A1), so there is deliberately NO ``provider_account_id``. The column
is additive/nullable with NO index, so it is immune to the schema-pass-before-
column-migrations ordering trap (the v63 idx_sessions_active regression); the
guarded ADD COLUMN in ``_run_column_migrations`` is the upgrade path and the
fresh ``create_postgres_schema`` CREATE TABLE carries it.

Follows the trap recipe from the plan's test plan: stamp a DB at the prior
version WITHOUT the column, run the REAL ``init_database()``, assert the column +
the current schema version land without raising — on the EXISTING-DB path AND a
fresh-DB test.
Mirrors tests/db/test_model_providers_migration.py.
"""

from app.db import database


def _column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _schema_version(conn):
    return conn.execute("SELECT version FROM schema_version").fetchone()[0]


def test_fresh_db_has_provider_id_column_and_version(mock_db):
    """A freshly built DB (create_postgres_schema) carries sessions.provider_id
    and the current schema version."""
    conn = mock_db
    assert "provider_id" in _column_names(conn, "sessions")
    assert _schema_version(conn) == database.SCHEMA_VERSION


def test_init_upgrades_pre_v66_db(mock_db):
    """EXISTING-DB path: a v65-shaped sessions table (no provider_id) is upgraded
    in place — the guarded ADD COLUMN lands provider_id and the version bumps to
    the current schema version, with the existing row preserved and no exception."""
    conn = mock_db
    # Simulate a pre-v66 database: drop the column and stamp the version back.
    conn.execute("ALTER TABLE sessions DROP COLUMN provider_id")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (65)")
    # A pre-migration session row, provider unaware.
    conn.execute(
        "INSERT INTO sessions (id, profile_id, instance_id) VALUES (?, ?, ?)",
        ("s-legacy", "default", ""),
    )
    conn.commit()

    assert "provider_id" not in _column_names(conn, "sessions")

    # Real init path (65 < SCHEMA_VERSION) — must add the column without raising.
    database.init_database()

    assert "provider_id" in _column_names(conn, "sessions")
    assert _schema_version(conn) == database.SCHEMA_VERSION
    # Existing pre-migration row survived and its binding is NULL → native.
    row = conn.execute(
        "SELECT provider_id FROM sessions WHERE id = 's-legacy'"
    ).fetchone()
    assert row[0] is None


def test_column_migration_is_idempotent(mock_db):
    """The guarded ADD COLUMN re-runs cleanly when the column already exists
    (no exception, no duplication)."""
    conn = mock_db
    # First pass (column already present from the fresh schema) — no-op.
    database.init_database()
    assert "provider_id" in _column_names(conn, "sessions")
    # Second boot — still a no-op.
    database.init_database()
    assert "provider_id" in _column_names(conn, "sessions")
    assert _schema_version(conn) == database.SCHEMA_VERSION

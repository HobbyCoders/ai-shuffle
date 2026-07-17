"""Unit test for the v63 favorites->active rename in
app/db/database.py._run_column_migrations.

v63 renames sessions.is_favorite -> is_active (favorites reworked into an
"Active group") and renames the backing index. The column rename is done with
``ALTER TABLE ... RENAME COLUMN`` (supported by both Postgres and the SQLite
test shim); the index is renamed via guarded DROP+CREATE because SQLite has no
``ALTER INDEX ... RENAME``. The block is guarded so it runs exactly once (only
when the legacy column exists and the new one does not) and preserves row data.

Modeled on tests/db/test_placement_migration.py — it exercises the FULL
`_run_column_migrations` pipeline against a shim-wrapped SQLite connection.
"""

import pytest

from app.db import database


@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    yield conn
    conn.close()


def _revert_to_pre_v63(shim):
    """Simulate a pre-v63 database: the base schema builds the current
    (is_active) shape, so rename the column back to is_favorite and restore the
    legacy index before running migrations."""
    cur = shim.cursor()
    cur.execute("DROP INDEX IF EXISTS idx_sessions_active")
    cur.execute("ALTER TABLE sessions RENAME COLUMN is_active TO is_favorite")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_favorite ON sessions(is_favorite)")


def test_is_favorite_renamed_to_is_active_preserving_data(test_db):
    from tests.pg_shim import route_db_to
    from app.db.base import column_exists

    with route_db_to(test_db) as shim:
        _revert_to_pre_v63(shim)
        # Seed a favorited row (empty-string instance_id stays untouched by the
        # placement backfill, so only the v63 rename affects it).
        shim.cursor().execute(
            "INSERT INTO sessions (id, instance_id, is_favorite) VALUES (%s, %s, %s)",
            ("sess-fav", "", True),
        )

        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        assert column_exists(cur, "sessions", "is_active")
        assert not column_exists(cur, "sessions", "is_favorite")

        cur.execute("SELECT is_active FROM sessions WHERE id = %s", ("sess-fav",))
        assert cur.fetchone()["is_active"] == 1


def test_v63_rename_is_idempotent(test_db):
    from tests.pg_shim import route_db_to
    from app.db.base import column_exists

    with route_db_to(test_db) as shim:
        _revert_to_pre_v63(shim)
        shim.cursor().execute(
            "INSERT INTO sessions (id, instance_id, is_favorite) VALUES (%s, %s, %s)",
            ("sess-fav", "", True),
        )

        database._run_column_migrations(shim.cursor())
        # Second boot: column is already is_active, so the guarded block is a
        # no-op (no error, value unchanged).
        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        assert column_exists(cur, "sessions", "is_active")
        cur.execute("SELECT is_active FROM sessions WHERE id = %s", ("sess-fav",))
        assert cur.fetchone()["is_active"] == 1

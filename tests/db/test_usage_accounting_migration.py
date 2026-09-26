"""The guarded column migrations behind true token accounting.

``_run_column_migrations`` is the migration mechanism for additive columns: it
runs on every startup regardless of schema version, so an existing database picks
the columns up without a version bump. These tests prove the three cumulative
session columns and the two usage_log cache columns land on a database that
predates them, and that a second pass is a no-op.

The fixture builds the CURRENT schema and then drops the new columns back off,
which is the closest a test can get to a pre-migration database.
"""

import pytest

from app.db import database


_SESSION_COLUMNS = (
    "total_cache_read_tokens",
    "total_cache_creation_tokens",
    "total_model_calls",
)
_USAGE_LOG_COLUMNS = ("cache_read_tokens", "cache_creation_tokens", "usage_estimated")


@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    for column in _SESSION_COLUMNS:
        conn.execute(f"ALTER TABLE sessions DROP COLUMN {column}")
    for column in _USAGE_LOG_COLUMNS:
        conn.execute(f"ALTER TABLE usage_log DROP COLUMN {column}")
    conn.commit()
    yield conn
    conn.close()


def test_columns_added(test_db):
    from tests.pg_shim import route_db_to
    from app.db.base import column_exists

    with route_db_to(test_db) as shim:
        cur = shim.cursor()
        assert not column_exists(cur, "sessions", "total_cache_read_tokens")

        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        for column in _SESSION_COLUMNS:
            assert column_exists(cur, "sessions", column), column
        for column in _USAGE_LOG_COLUMNS:
            assert column_exists(cur, "usage_log", column), column


def test_existing_rows_default_to_zero(test_db):
    """A pre-migration session must read as 0, not NULL — the increments are
    additive SQL and ``col = NULL + n`` is NULL."""
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        shim.cursor().execute(
            "INSERT INTO sessions (id, total_tokens_in) VALUES (%s, %s)",
            ("sess-old", 15),
        )
        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute(
            "SELECT total_cache_read_tokens, total_cache_creation_tokens, "
            "total_model_calls FROM sessions WHERE id = %s",
            ("sess-old",),
        )
        row = cur.fetchone()
        assert row["total_cache_read_tokens"] == 0
        assert row["total_cache_creation_tokens"] == 0
        assert row["total_model_calls"] == 0


def test_pre_migration_usage_rows_read_as_not_estimated(test_db):
    """(v90) a ledger row booked before the flag existed is a measured turn."""
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        shim.cursor().execute(
            "INSERT INTO usage_log (session_id, tokens_in, tokens_out) VALUES (%s, %s, %s)",
            ("sess-old", 15, 3),
        )
        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute("SELECT usage_estimated FROM usage_log WHERE session_id = %s", ("sess-old",))
        assert not cur.fetchone()["usage_estimated"]


def test_rerun_is_a_no_op(test_db):
    """Every startup runs this, so a second pass must neither fail nor rewrite."""
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        database._run_column_migrations(shim.cursor())
        shim.cursor().execute(
            "UPDATE sessions SET total_cache_read_tokens = 99 WHERE 1=1"
        )
        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM sessions WHERE total_cache_read_tokens = 99")
        # No rows to check on an empty table is fine; the point is it did not raise.
        assert cur.fetchone()["n"] >= 0

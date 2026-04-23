"""
Conformance tests for :class:`app.core.pg_session_store.PgSessionStore`.

Uses the SDK's shared ``run_session_store_conformance`` harness to assert all
14 behavioral contracts every :class:`claude_agent_sdk.SessionStore` adapter
must satisfy.

A real PostgreSQL instance is required — the store writes JSONB and uses
partial indexes that SQLite can't emulate. The test initializes the
application connection pool once, then isolates each contract by truncating
the two ``session_store_*`` tables between invocations of ``make_store``.

The tests are skipped when ``DATABASE_URL`` is not set so the unit-test
suite on machines without Postgres still passes.
"""

from __future__ import annotations

import os
import pytest

# Skip the whole module if no Postgres is available — the adapter is
# Postgres-specific by design.
DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set; PgSessionStore conformance requires Postgres",
)


@pytest.fixture(scope="module")
def pg_pool():
    """Initialize the app's connection pool once for the module."""
    from app.db import base

    # Allow reinitialization if another test module already opened the pool.
    if base._pg_pool is None:
        base.init_pool(min_conn=1, max_conn=4)
    yield
    # Intentionally do not close_pool() here — other test modules in the
    # same session may still need it.


@pytest.fixture(scope="module", autouse=True)
def _schema(pg_pool):
    """Ensure the SessionStore tables exist.

    Running the full app init_database() in tests would touch every
    unrelated table and risks colliding with other fixtures, so we execute
    only the DDL this adapter needs. The statements are copied verbatim
    from ``_run_column_migrations()`` and are idempotent.
    """
    from app.db.base import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS session_store_entries (
                id BIGSERIAL PRIMARY KEY,
                project_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                subpath TEXT,
                entry JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sse_main "
            "ON session_store_entries(project_key, session_id, id) "
            "WHERE subpath IS NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sse_sub "
            "ON session_store_entries(project_key, session_id, subpath, id) "
            "WHERE subpath IS NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sse_project "
            "ON session_store_entries(project_key) "
            "WHERE subpath IS NULL"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS session_store_summaries (
                project_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                data JSONB NOT NULL,
                mtime_ms BIGINT NOT NULL,
                PRIMARY KEY (project_key, session_id)
            )
            """
        )


def _truncate() -> None:
    """Reset both tables to give each ``make_store()`` call a clean slate."""
    from app.db.base import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "TRUNCATE session_store_entries, session_store_summaries "
            "RESTART IDENTITY"
        )


@pytest.mark.asyncio
async def test_pg_session_store_conformance():
    """Run all 14 SDK SessionStore contracts against PgSessionStore."""
    from claude_agent_sdk.testing import run_session_store_conformance

    from app.core.pg_session_store import PgSessionStore

    def make_store() -> PgSessionStore:
        _truncate()
        return PgSessionStore()

    await run_session_store_conformance(make_store)

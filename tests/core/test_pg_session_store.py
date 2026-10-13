"""
Conformance tests for :class:`app.core.pg_session_store.PgSessionStore`.

Exercises the owned append/load/list/delete transcript-store contract, including
subagent isolation, summary folding, ordering and idempotent cleanup.

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

# Skip the whole module unless the caller has explicitly opted in to running
# against a REAL Postgres via ALLOW_REAL_PG=1. This conformance harness
# TRUNCATES the session_store_* tables between contracts, so it must never run
# against the app's configured DATABASE_URL (which is commonly a shared or
# production database). Point DATABASE_URL at a disposable test database and
# export ALLOW_REAL_PG=1 to run it. See tests/conftest.py for the guard.
pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOW_REAL_PG") != "1",
    reason="ALLOW_REAL_PG!=1; PgSessionStore conformance mutates a real Postgres "
    "(truncates session_store_* tables) and is skipped by default for safety",
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
                folded_through_id BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (project_key, session_id)
            )
            """
        )
        # v84 fold watermark (the table may predate it from an earlier run).
        cur.execute(
            "ALTER TABLE session_store_summaries "
            "ADD COLUMN IF NOT EXISTS folded_through_id BIGINT NOT NULL DEFAULT 0"
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
    """Exercise the app's durable transcript contract on a disposable Postgres."""
    from app.core.pg_session_store import PgSessionStore

    _truncate()
    store = PgSessionStore()
    key = {"project_key": "project", "session_id": "test-session"}
    sub = {**key, "subpath": "subagents/agent-1"}
    other = {"project_key": "other", "session_id": "other-session"}
    assert await store.load(key) is None
    await store.append(key, [])
    a = {"type": "user", "uuid": "u", "message": {"content": "hello"}}
    b = {"type": "assistant", "uuid": "a", "message": {"content": "world"}}
    await store.append(key, [a])
    await store.append(key, [b])
    await store.append(sub, [b])
    await store.append(other, [a])
    # A fresh adapter sees committed order and isolates subagent transcripts.
    assert await PgSessionStore().load(key) == [a, b]
    assert await store.load(sub) == [b]
    assert await store.list_subkeys(key) == ["subagents/agent-1"]
    assert [e["session_id"] for e in await store.list_sessions("project")] == [key["session_id"]]
    summaries = await store.list_session_summaries("project")
    assert summaries[0]["data"]["first_prompt"] == "hello"
    await store.delete(sub)
    assert await store.load(sub) is None
    assert await store.load(key) == [a, b]
    await store.append(sub, [b])
    await store.delete(key)
    assert await store.load(key) is None
    assert await store.load(sub) is None
    assert await store.list_subkeys(key) == []
    assert await store.list_sessions("project") == []
    assert await store.list_session_summaries("project") == []
    assert await store.load(other) == [a]
    await store.delete(key)  # idempotent cleanup

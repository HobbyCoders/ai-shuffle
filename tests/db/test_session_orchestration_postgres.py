"""Opt-in producer concurrency against a disposable PostgreSQL database.

Run with ALLOW_REAL_PG=1 and the same isolated managed-DB setup used by the
existing PostgreSQL tests. This never launches inference or truncates tables.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("ALLOW_REAL_PG") != "1",
                                reason="requires disposable TEST_POSTGRES_DSN")


@pytest.fixture(scope="module", autouse=True)
def pg_schema():
    from app.db import base
    from app.db.schema_postgres import create_postgres_schema
    assert os.environ.get("TEST_POSTGRES_DSN")
    # Initialise before the function-level auto-shim fixture makes its choice.
    base.init_pool(min_conn=1, max_conn=2)
    try:
        with base.get_db() as conn:
            create_postgres_schema(conn.cursor())
        yield
    finally:
        base.close_pool()


def test_concurrent_top_level_starts_and_replays(monkeypatch, tmp_path):
    from app.db import base, sessions, turns, database
    from app.core.session_orchestration import _queue
    from app.network import identity
    from types import SimpleNamespace
    # Direct SQL tests bypass startup. Provision an isolated identity rather
    # than reading or creating one in the real worker's credential directory.
    monkeypatch.setattr(identity, "settings", SimpleNamespace(effective_data_dir=tmp_path / "network"))
    monkeypatch.setattr(identity, "_cached", None)
    identity.load()
    # More callers than pooled connections catches nested checkout deadlocks.
    sid = "pg-orchestration-" + uuid4().hex
    pid = sid + "-agent"
    try:
        database.create_profile(pid, "Peer race", None, {"model": "sonnet"})
        sessions.create_session(sid, pid, title="Origin", is_active=True, model="sonnet")
        origin = turns.enqueue_turn(sid, "user", prompt="Coordinate", profile_id=pid)
        with base.get_db() as conn:
            conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (origin["id"],))
        def start(i):
            try:
                return _queue(sid, {"title": "Peer", "prompt": "No inference launched",
                                    "request_id": str(i)}, "start")
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(start, range(8)))
        successes = [r for r in outcomes if isinstance(r, dict)]
        assert len(successes) == 4, outcomes
        assert len({r["session_id"] for r in successes}) == 4
        assert all("budget" in r for r in outcomes if isinstance(r, str))
        first = next(i for i, r in enumerate(outcomes) if isinstance(r, dict))
        assert start(first)["turn_id"] == outcomes[first]["turn_id"]
    finally:
        # Leave no queued work for a scheduler if this fixture shares a test app.
        with base.get_db() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM turns WHERE session_id IN (SELECT id FROM sessions WHERE profile_id=%s)", (pid,))
            c.execute("DELETE FROM sessions WHERE profile_id=%s", (pid,))
            c.execute("DELETE FROM agents WHERE id=%s", (pid,))

"""Real PostgreSQL admission and row-lock races on a private disposable cluster."""
from concurrent.futures import ThreadPoolExecutor
import threading
from uuid import uuid4

import pytest
import psycopg2.pool

from app.db import base, runs, turns
from tests.db import schema_lifecycle_helpers as lifecycle


@pytest.fixture(scope="module")
def private_pg(tmp_path_factory):
    bindir = lifecycle.pg16_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL 16 server binaries not found")
    with lifecycle.private_postgres(bindir, tmp_path_factory.mktemp("lease-pg")) as descriptor:
        yield descriptor


@pytest.fixture
def lease_pool(private_pg, monkeypatch):
    d = private_pg
    pool = base._RetainingThreadedConnectionPool(
        1, 6, reserved=2, host=d["host"], port=d["port"],
        user=d["user"], password=d["password"], dbname=d["database"],
    )
    monkeypatch.setattr(base, "_pg_pool", pool)
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    # Use the application's real schema and producing paths, not fabricated runs.
    from app.db.schema_postgres import create_postgres_schema
    with base.get_db() as conn:
        create_postgres_schema(conn.cursor())
    try:
        yield pool
    finally:
        pool.closeall()


def test_real_holders_leave_renewal_and_reaper_available(lease_pool, monkeypatch):
    sid = uuid4().hex
    run = runs.claim_run(sid, "owner", 60)
    ready = threading.Barrier(5)
    release = threading.Event()
    monkeypatch.setattr(base, "_POOL_ACQUIRE_MAX_WAIT_SECONDS", 0.1)

    def hold():
        with base.get_db() as conn:
            conn.cursor().execute("SELECT 1")
            ready.wait(timeout=5)
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=4) as executor:
        holders = [executor.submit(hold) for _ in range(4)]
        try:
            ready.wait(timeout=5)
            with pytest.raises(psycopg2.pool.PoolError, match="exhausted"):
                with base.get_db():
                    pass
            assert runs.renew_run(run["id"], "owner", run["fence_token"], 60)
            assert turns.reap_expired() == []
        finally:
            release.set()
        for future in holders:
            future.result(timeout=5)
    assert not lease_pool._used


@pytest.mark.parametrize("contender", ["claim", "reap"])
def test_late_renewal_serializes_with_takeover(lease_pool, contender):
    # Repeat with fresh rows: both operations really compete for the PG row lock.
    for _ in range(10):
        sid = uuid4().hex
        run = runs.claim_run(sid, "old", -1)
        turn = turns.enqueue_turn(sid, "user", prompt="race")
        assert turns.start_turn(turn["id"], run_id=run["id"])
        ready = threading.Barrier(2)

        def renew():
            ready.wait(timeout=5)
            return runs.renew_run(run["id"], "old", run["fence_token"], 60)

        def take():
            ready.wait(timeout=5)
            if contender == "claim":
                return runs.claim_run(sid, "new", 60)
            return any(t["id"] == turn["id"] for t in turns.reap_expired())

        with ThreadPoolExecutor(max_workers=2) as executor:
            renewal, takeover = executor.submit(renew), executor.submit(take)
            renewed, taken = renewal.result(timeout=5), takeover.result(timeout=5)
        assert renewed != bool(taken)
        if taken:
            assert not runs.renew_run(run["id"], "old", run["fence_token"], 60)
        else:
            assert runs.get_active_run(sid)["id"] == run["id"]
        # Avoid another iteration's reaper touching the prior test turn.
        with base.get_db() as conn:
            conn.cursor().execute("DELETE FROM turns WHERE id=%s", (turn["id"],))
            conn.cursor().execute("DELETE FROM runs WHERE session_id=%s", (sid,))

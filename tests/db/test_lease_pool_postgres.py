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


def _settle(sid):
    from app.core import session_settlement
    from app.core.chat_access import broker

    snapshot = session_settlement._capture_and_dismiss(sid, broker, None)
    assert snapshot is not None
    return snapshot["aborted"]


def _waiting_session(sid, run):
    from app.db import interactions, profiles, sessions

    profiles.create_profile("lease-test", "Lease test", None, {}, if_absent=True)
    sessions.create_session(sid, "lease-test", derive_provider_from_profile=False)
    interactions.create_interaction(sid, "question", {"questions": []}, None, run_id=run["id"])


@pytest.mark.parametrize("contender", ["claim", "reap", "stop", "settle"])
def test_late_renewal_serializes_with_takeover(lease_pool, contender):
    # Repeat with fresh rows: both operations really compete for the PG row lock.
    for _ in range(10):
        sid = uuid4().hex
        run = runs.claim_run(sid, "old", -1)
        turn = turns.enqueue_turn(sid, "user", prompt="race")
        assert turns.start_turn(turn["id"], run_id=run["id"])
        if contender == "settle":
            _waiting_session(sid, run)
        ready = threading.Barrier(2)

        def renew():
            ready.wait(timeout=5)
            return runs.renew_run(run["id"], "old", run["fence_token"], 60)

        def take():
            ready.wait(timeout=5)
            if contender == "claim":
                return runs.claim_run(sid, "new", 60)
            if contender == "stop":
                return turns.stop_orphaned_turns(sid, turn["id"])
            if contender == "settle":
                return _settle(sid)
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

@pytest.mark.parametrize("operation", ["stop", "settle"])
def test_aborted_late_owner_cannot_renew_or_append(lease_pool, operation):
    from app.db import session_events

    sid = uuid4().hex
    run = runs.claim_run(sid, "old", -1)
    turn = turns.enqueue_turn(sid, "user", prompt="late heartbeat")
    assert turns.start_turn(turn["id"], run_id=run["id"])
    if operation == "settle":
        _waiting_session(sid, run)
        assert _settle(sid)
    else:
        assert turns.stop_orphaned_turns(sid, turn["id"])
    assert turns.get_turn(turn["id"])["state"] == "aborted"
    assert not runs.renew_run(run["id"], "old", run["fence_token"], 60)
    with pytest.raises(session_events.FenceViolation):
        session_events.append_events(
            sid, [{"type": "system.note", "payload": {"content": "stale"}}],
            run_id=run["id"], fence_token=run["fence_token"],
        )
    successor = runs.claim_run(sid, "new", 60)
    assert successor["fence_token"] > run["fence_token"]
    assert runs.claim_run(sid, "third", 60) is None
    assert turns.stop_orphaned_turns(sid, turn["id"]) == []
    assert runs.renew_run(successor["id"], "new", successor["fence_token"], 60)


@pytest.mark.parametrize("outcome", ["aborted", "completed", "parked", "reassigned"])
def test_late_renewal_rejects_terminal_or_reassigned_turn(lease_pool, outcome):
    sid = uuid4().hex
    run = runs.claim_run(sid, "old", -1)
    turn = turns.enqueue_turn(sid, "user", prompt="race")
    assert turns.start_turn(turn["id"], run_id=run["id"])
    if outcome == "parked":
        assert turns.park_turn(turn["id"], ["background"])
    elif outcome == "reassigned":
        assert any(t["id"] == turn["id"] for t in turns.reap_expired())
        successor = runs.claim_run(sid, "new", 60)
        assert successor
        assert turns.start_turn(turn["id"], run_id=successor["id"])
        assert turns.get_turn(turn["id"])["run_id"] == successor["id"]
    else:
        assert turns.settle_turn(turn["id"], outcome)
    assert not runs.renew_run(run["id"], "old", run["fence_token"], 60)


def test_lease_sql_timeouts_release_workers_and_do_not_leak(lease_pool):
    sid = uuid4().hex
    run = runs.claim_run(sid, "owner", 60)
    with base.get_db() as blocker:
        blocker.cursor().execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (run["id"],))
        with ThreadPoolExecutor(max_workers=1) as executor:
            renewal = executor.submit(runs.renew_run, run["id"], "owner", run["fence_token"], 60)
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                renewal.result(timeout=3)
    with base.get_lease_db() as conn:
        with pytest.raises(psycopg2.errors.QueryCanceled):
            conn.cursor().execute("SELECT pg_sleep(5)")
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SHOW lock_timeout")
        assert cursor.fetchone()["lock_timeout"] == "0"
        cursor.execute("SHOW statement_timeout")
        assert cursor.fetchone()["statement_timeout"] == "0"
    assert runs.renew_run(run["id"], "owner", run["fence_token"], 60)


def test_reaper_fences_even_skipped_local_turns(lease_pool):
    sid = uuid4().hex
    run = runs.claim_run(sid, "old", -1)
    turn = turns.enqueue_turn(sid, "user", prompt="local")
    assert turns.start_turn(turn["id"], run_id=run["id"])
    assert not any(t["id"] == turn["id"] for t in turns.reap_expired([turn["id"]]))
    assert turns.get_turn(turn["id"])["state"] == "running"
    assert not runs.renew_run(run["id"], "old", run["fence_token"], 60)

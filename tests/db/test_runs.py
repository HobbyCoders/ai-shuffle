"""Unit tests for app/db/runs.py — fenced, leased session ownership."""

import pytest

from app.db import runs
from app.db import session_events as se


@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db):
        yield test_db


def test_claim_then_second_claim_returns_none(mock_db):
    r1 = runs.claim_run("s1", "runnerA", 60)
    assert r1 is not None
    assert r1["state"] == "active"
    assert r1["fence_token"] == 1
    r2 = runs.claim_run("s1", "runnerB", 60)
    assert r2 is None
    # The original owner is untouched.
    active = runs.get_active_run("s1")
    assert active["id"] == r1["id"]


def test_claim_after_lease_expiry_succeeds_and_old_renew_fails(mock_db):
    # Negative ttl means the lease is already expired on insert — deterministic
    # without freezegun.
    r1 = runs.claim_run("s1", "runnerA", -1)
    assert r1["fence_token"] == 1
    r2 = runs.claim_run("s1", "runnerB", 60)
    assert r2 is not None
    assert r2["fence_token"] == 2
    # The reaped run can no longer renew (not active + lease lapsed).
    assert runs.renew_run(r1["id"], "runnerA", r1["fence_token"], 60) is False


def test_renew_wrong_token_or_runner_fails(mock_db):
    r = runs.claim_run("s1", "runnerA", 60)
    assert runs.renew_run(r["id"], "runnerA", r["fence_token"] + 1, 60) is False
    assert runs.renew_run(r["id"], "someone-else", r["fence_token"], 60) is False
    # Correct token + runner renews.
    assert runs.renew_run(r["id"], "runnerA", r["fence_token"], 60) is True


def test_release_then_reclaim_increments_fence_token(mock_db):
    r1 = runs.claim_run("s1", "runnerA", 60)
    assert runs.release_run(r1["id"], r1["fence_token"]) is True
    # Releasing a non-active run is a no-op.
    assert runs.release_run(r1["id"], r1["fence_token"]) is False
    r2 = runs.claim_run("s1", "runnerB", 60)
    assert r2["fence_token"] == r1["fence_token"] + 1


def test_transfer_supersedes_and_old_token_cannot_append(mock_db):
    r1 = runs.claim_run("s1", "runnerA", 60)
    r2 = runs.transfer_run("s1", "runnerB", 60)
    assert r2["fence_token"] == r1["fence_token"] + 1
    assert r2["state"] == "active"
    # Old row is now 'transferred', not active.
    active = runs.get_active_run("s1")
    assert active["id"] == r2["id"]
    # Cross-module: appending with the superseded token is fenced out.
    with pytest.raises(se.FenceViolation):
        se.append_events(
            "s1", [{"type": "a"}], run_id=r1["id"], fence_token=r1["fence_token"]
        )
    # The new owner can append.
    fs, ls = se.append_events(
        "s1", [{"type": "a"}], run_id=r2["id"], fence_token=r2["fence_token"]
    )
    assert (fs, ls) == (1, 1)


def test_transfer_with_no_active_run_still_claims(mock_db):
    r = runs.transfer_run("fresh", "runnerA", 60)
    assert r["state"] == "active"
    assert r["fence_token"] == 1


def test_expire_due_runs_flips_only_overdue_actives(mock_db):
    live = runs.claim_run("s-live", "runnerA", 60)
    overdue = runs.claim_run("s-overdue", "runnerB", -1)
    count = runs.expire_due_runs()
    assert count == 1
    assert runs.get_active_run("s-live")["id"] == live["id"]
    assert runs.get_active_run("s-overdue") is None
    assert overdue is not None  # existed, now expired

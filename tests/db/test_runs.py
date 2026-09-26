"""Unit tests for app/db/runs.py — fenced, leased session ownership."""


from app.db import runs


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

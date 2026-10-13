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


def test_activity_summary_attributes_leased_work_without_identities(mock_db):
    from app.db import turns

    assert runs.activity_summary("local") == {
        "scope": "shared_database",
        "local_active_runs": 0, "other_active_runs": 0, "other_active_runners": 0,
        "local_live_turns": 0, "other_live_turns": 0, "stale_live_turns": 0,
        "local_running_background_tasks": 0, "other_running_background_tasks": 0,
        "unattributed_running_background_tasks": 0,
    }
    mine = runs.claim_run("s-local", "local", 60)
    theirs = runs.claim_run("s-a", "sibling", 60)
    runs.claim_run("s-b", "sibling", 60)
    runs.claim_run("s-c", "cluster-peer", 60)
    lapsed = runs.claim_run("s-dead", "crashed-peer", -1)  # lease already expired
    for session, run in (("s-local", mine), ("s-a", theirs), ("s-dead", lapsed)):
        turn = turns.enqueue_turn(session, "user", prompt="never-collect-this-prompt")
        assert turns.start_turn(turn["id"], run_id=run["id"])
    mock_db.executemany(
        "INSERT INTO background_tasks (task_id, session_id, instance_id, status) VALUES (?, ?, ?, ?)",
        [("t1", "s-local", "local", "running"), ("t2", "s-a", "sibling", "running"),
         ("t3", "s-b", "sibling", "completed"), ("t4", "s-c", None, "running")],
    )
    mock_db.commit()

    summary = runs.activity_summary("local")

    assert summary == {
        "scope": "shared_database",
        "local_active_runs": 1, "other_active_runs": 3, "other_active_runners": 2,
        "local_live_turns": 1, "other_live_turns": 1,
        # The crashed peer's turn is still "running" but its lease lapsed.
        "stale_live_turns": 1,
        "local_running_background_tasks": 1, "other_running_background_tasks": 1,
        "unattributed_running_background_tasks": 1,
    }
    rendered = repr(summary)
    for secret in ("never-collect-this-prompt", "sibling", "cluster-peer", "s-local", mine["id"]):
        assert secret not in rendered

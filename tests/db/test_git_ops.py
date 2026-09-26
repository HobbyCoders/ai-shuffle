"""Unit tests for app/db/git_ops.py — the durable cross-install git-operation
queue (D5).

The whole point of the table is that TWO racers (the target install's git-ops
claim loop and the spawn path's inline claim) compete for the same row and the
DATABASE picks the winner. These tests pin that: only the targeted runner can
claim, and a second claim of a won row returns None rather than double-running
``git worktree add``.
"""

from datetime import datetime, timedelta, timezone

from app.db import git_ops

RUNNER = "runner-a"
OTHER = "runner-b"
PV2 = "pv2-abc"


def _got(op_id: str):
    """The op row, asserted present — the caller just wrote it, so a None
    here is a test bug rather than a branch worth handling."""
    row = git_ops.get_op(op_id)
    assert row is not None, f"op {op_id} vanished"
    return row


def _claimed(op_id: str, runner: str):
    """Claim an op and assert we won it."""
    row = git_ops.claim_op(op_id, runner)
    assert row is not None, f"expected to win the claim on {op_id}"
    return row


def _ensure_op(runner=RUNNER, branch="feat/x", create_new_branch=True, project=PV2):
    return git_ops.insert_op(
        runner,
        "ensure_worktree",
        project_v2_id=project,
        params={
            "branch": branch,
            "base_branch": "main",
            "create_new_branch": create_new_branch,
        },
    )


# ═══ insert / read ═══════════════════════════════════════════════════════════


def test_insert_op_is_queued_and_carries_params(mock_db):
    op = _ensure_op()
    assert op["status"] == "queued"
    assert op["runner_id"] == RUNNER
    assert op["op"] == "ensure_worktree"
    # params round-trip through JSONB (TEXT on the shim) — the create_new_branch
    # intent is the whole reason this row exists.
    assert op["params"]["branch"] == "feat/x"
    assert op["params"]["create_new_branch"] is True
    assert _got(op["id"])["params"] == op["params"]


def test_list_queued_is_scoped_to_the_runner(mock_db):
    mine = _ensure_op(runner=RUNNER)
    _ensure_op(runner=OTHER, branch="feat/y")

    queued = git_ops.list_queued_for_runner(RUNNER)
    assert [o["id"] for o in queued] == [mine["id"]]


# ═══ claim semantics ═════════════════════════════════════════════════════════


def test_only_the_target_runner_can_claim(mock_db):
    op = _ensure_op(runner=RUNNER)

    assert git_ops.claim_op(op["id"], OTHER) is None
    assert _got(op["id"])["status"] == "queued"

    claimed = _claimed(op["id"], RUNNER)
    assert claimed["status"] == "running"


def test_double_claim_loses(mock_db):
    """The loop and the spawn-path inline claim race for the same row; exactly
    one may win, or the checkout would be materialized twice."""
    op = _ensure_op()

    first = git_ops.claim_op(op["id"], RUNNER)
    second = git_ops.claim_op(op["id"], RUNNER)

    assert first is not None
    assert second is None


def test_terminal_ops_are_not_claimable(mock_db):
    op = _ensure_op()
    git_ops.claim_op(op["id"], RUNNER)
    git_ops.complete_op(op["id"], {"path": "/w/x"})

    assert git_ops.claim_op(op["id"], RUNNER) is None


# ═══ settle ══════════════════════════════════════════════════════════════════


def test_complete_records_result(mock_db):
    op = _ensure_op()
    git_ops.claim_op(op["id"], RUNNER)

    done = git_ops.complete_op(op["id"], {"path": "/work/feat-x", "branch": "feat/x"})
    assert done is not None
    assert done["status"] == "done"
    assert done["result"]["path"] == "/work/feat-x"


def test_fail_records_the_error_message(mock_db):
    op = _ensure_op()
    git_ops.claim_op(op["id"], RUNNER)

    failed = git_ops.fail_op(op["id"], "no base checkout here")
    assert failed is not None
    assert failed["status"] == "error"
    assert failed["result"]["error"] == "no base checkout here"


# ═══ find_queued (the spawn-path lookup) ═════════════════════════════════════


def test_find_queued_matches_runner_project_and_branch(mock_db):
    target = _ensure_op(branch="feat/x")
    _ensure_op(branch="feat/other")

    found = git_ops.find_queued(RUNNER, PV2, "ensure_worktree", branch="feat/x")
    assert found is not None and found["id"] == target["id"]


def test_find_queued_misses_on_wrong_runner_project_or_branch(mock_db):
    _ensure_op(branch="feat/x")

    assert git_ops.find_queued(OTHER, PV2, "ensure_worktree", branch="feat/x") is None
    assert git_ops.find_queued(RUNNER, "pv2-other", "ensure_worktree", branch="feat/x") is None
    assert git_ops.find_queued(RUNNER, PV2, "ensure_worktree", branch="nope") is None


def test_find_queued_ignores_already_claimed_rows(mock_db):
    op = _ensure_op(branch="feat/x")
    git_ops.claim_op(op["id"], RUNNER)

    assert git_ops.find_queued(RUNNER, PV2, "ensure_worktree", branch="feat/x") is None


# ═══ find_matching (pure read; skips stale running) ══════════════════════════


def test_find_matching_returns_queued(mock_db):
    target = _ensure_op(branch="feat/x")
    found = git_ops.find_matching(RUNNER, PV2, "ensure_worktree", branch="feat/x")
    assert found is not None and found["id"] == target["id"]
    assert found["status"] == "queued"


def test_find_matching_returns_fresh_running(mock_db):
    op = _ensure_op(branch="feat/x")
    git_ops.claim_op(op["id"], RUNNER)
    found = git_ops.find_matching(RUNNER, PV2, "ensure_worktree", branch="feat/x")
    assert found is not None and found["id"] == op["id"]
    assert found["status"] == "running"


def test_find_matching_skips_stale_running_and_returns_newer_queued(mock_db):
    """A 3-hour zombie must not hide a claimable retry for the same branch."""
    zombie = _ensure_op(branch="feat/x")
    git_ops.claim_op(zombie["id"], RUNNER)
    _backdate(zombie["id"], minutes=180)

    queued = _ensure_op(branch="feat/x")
    found = git_ops.find_matching(RUNNER, PV2, "ensure_worktree", branch="feat/x")
    assert found is not None and found["id"] == queued["id"]
    assert found["status"] == "queued"


def test_find_matching_skips_stale_running_when_nothing_else_matches(mock_db):
    zombie = _ensure_op(branch="feat/x")
    git_ops.claim_op(zombie["id"], RUNNER)
    _backdate(zombie["id"], minutes=20)
    assert git_ops.find_matching(RUNNER, PV2, "ensure_worktree", branch="feat/x") is None


def test_find_matching_never_calls_fail_op(mock_db):
    from unittest.mock import patch

    zombie = _ensure_op(branch="feat/x")
    git_ops.claim_op(zombie["id"], RUNNER)
    _backdate(zombie["id"], minutes=20)
    with patch.object(git_ops, "fail_op") as fail:
        git_ops.find_matching(RUNNER, PV2, "ensure_worktree", branch="feat/x")
        fail.assert_not_called()


def test_find_matching_keeps_running_younger_than_stale_bound(mock_db):
    op = _ensure_op(branch="feat/x")
    git_ops.claim_op(op["id"], RUNNER)
    _backdate(op["id"], minutes=5)
    found = git_ops.find_matching(RUNNER, PV2, "ensure_worktree", branch="feat/x")
    assert found is not None and found["id"] == op["id"]
    assert git_ops.list_stale_running(
        RUNNER, PV2, "ensure_worktree", branch="feat/x"
    ) == []


def test_touch_op_refreshes_updated_at(mock_db):
    op = _ensure_op()
    git_ops.claim_op(op["id"], RUNNER)
    _backdate(op["id"], minutes=20)
    assert git_ops._is_stale_running(_got(op["id"]))
    touched = git_ops.touch_op(op["id"])
    assert touched is not None
    assert not git_ops._is_stale_running(touched)


# ═══ sweeper ═════════════════════════════════════════════════════════════════


def _backdate(op_id: str, days: int = 0, minutes: int = 0):
    from app.db.base import get_db

    old = (
        datetime.now(timezone.utc) - timedelta(days=days, minutes=minutes)
    ).isoformat()
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE git_ops SET updated_at = %s WHERE id = %s", (old, op_id)
        )


def test_purge_drops_old_terminal_rows_only(mock_db):
    stale_done = _ensure_op(branch="a")
    git_ops.claim_op(stale_done["id"], RUNNER)
    git_ops.complete_op(stale_done["id"], {})
    _backdate(stale_done["id"], 30)

    stale_queued = _ensure_op(branch="b")
    _backdate(stale_queued["id"], 30)

    fresh_done = _ensure_op(branch="c")
    git_ops.claim_op(fresh_done["id"], RUNNER)
    git_ops.complete_op(fresh_done["id"], {})

    removed = git_ops.purge_terminal(older_than_days=7)

    assert removed == 1
    assert git_ops.get_op(stale_done["id"]) is None
    # In-flight work is never swept, however old it is.
    assert git_ops.get_op(stale_queued["id"]) is not None
    assert git_ops.get_op(fresh_done["id"]) is not None

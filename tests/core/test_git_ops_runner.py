"""The git-ops claim loop and the spawn-path inline claim (D5).

Two routes execute the same ``ensure_worktree`` work and they RACE by design:
the target install's poll loop, and — when the first turn of a remote-created
branch session is claimed before the loop's next tick — the spawn path itself.
The op row is the durable carrier of ``create_new_branch``, so the inline claim
is what stops a brand-new branch from being stranded. These tests pin both, plus
the rule that neither may run an op targeted at a different install.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core import git_ops_runner
from app.db import git_ops as git_ops_db
from tests.shared_fixtures import LOCAL_ME as ME, pin_runner  # noqa: F401  (autouse fixture)

PEER = "peer-1"
PV2 = "pv2-abc"


@pytest.fixture
def project(mock_db):
    from app.db import projects_v2
    return projects_v2.upsert_project("git:example/repo", name="Repo")


def _got(op_id: str):
    """The op row, asserted present — a None here is a test bug."""
    row = git_ops_db.get_op(op_id)
    assert row is not None, f"op {op_id} vanished"
    return row


def _claimed(op_id: str, runner: str = ME):
    """Claim an op and assert we won it."""
    row = git_ops_db.claim_op(op_id, runner)
    assert row is not None, f"expected to win the claim on {op_id}"
    return row


def _queue(project, runner=ME, branch="feat/x", create_new_branch=True):
    return git_ops_db.insert_op(
        runner,
        "ensure_worktree",
        project_v2_id=project["id"],
        params={
            "branch": branch,
            "base_branch": "main",
            "create_new_branch": create_new_branch,
        },
    )


# ═══ execute_ensure_worktree ═════════════════════════════════════════════════


def test_executor_threads_the_create_new_branch_intent(project):
    """The intent lives ONLY on the op row — losing it would leave a brand-new
    branch unmaterializable anywhere."""
    op = _queue(project, create_new_branch=True)
    claimed = _claimed(op["id"])

    from app.core import checkout_manager

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(return_value="/w/feat-x")
    ) as ensure:
        result = git_ops_runner.execute_ensure_worktree(claimed)

    assert result == {"path": "/w/feat-x", "branch": "feat/x"}
    assert ensure.call_args.args[1] == "feat/x"
    assert ensure.call_args.kwargs == {
        "base_branch": "main", "create_new_branch": True,
    }


def test_executor_raises_when_the_branch_is_not_materializable(project):
    op = _queue(project)
    claimed = _claimed(op["id"])

    from app.core import checkout_manager

    with patch.object(checkout_manager, "ensure_checkout", MagicMock(return_value=None)):
        with pytest.raises(RuntimeError, match="could not be materialized"):
            git_ops_runner.execute_ensure_worktree(claimed)


# ═══ head_sha / dirty capture (W3-5) ═════════════════════════════════════════
#
# The ensure_worktree executor is the ONLY place allowed to spend git
# subprocesses on git-state display fields — the hot spawn/refresh paths stay
# subprocess-free. And because it is display metadata, it must never be able to
# fail an otherwise-successful op.


def _completed(stdout: str = "", returncode: int = 0):
    import subprocess

    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=""
    )


def _git_replies(**by_subcommand):
    """A ``_run_git`` stub keyed on the first git argument."""
    def _run(working_dir, args, **kwargs):
        reply = by_subcommand.get(args[0])
        if isinstance(reply, Exception):
            raise reply
        return reply
    return MagicMock(side_effect=_run)


def test_executor_records_head_sha_and_dirty_on_the_checkout(project):
    from app.core import checkout_manager
    from app.core.git_service import git_service
    from app.db import checkouts as checkouts_db

    op = _queue(project, branch="feat/x")
    claimed = _claimed(op["id"])

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(return_value="/w/feat-x")
    ), patch.object(
        git_service,
        "_run_git",
        _git_replies(**{
            "rev-parse": _completed("abc123def456\n"),
            "status": _completed(" M app/main.py\n"),
        }),
    ):
        result = git_ops_runner.execute_ensure_worktree(claimed)

    assert result == {"path": "/w/feat-x", "branch": "feat/x"}
    row = checkouts_db.get_checkout(project["id"], ME, branch="feat/x")
    assert row is not None
    assert row["head_sha"] == "abc123def456"
    assert row["dirty"] is True
    assert row["path"] == "/w/feat-x"


def test_a_clean_tree_records_dirty_false(project):
    from app.core import checkout_manager
    from app.core.git_service import git_service
    from app.db import checkouts as checkouts_db

    op = _queue(project, branch="feat/x")
    claimed = _claimed(op["id"])

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(return_value="/w/feat-x")
    ), patch.object(
        git_service,
        "_run_git",
        _git_replies(**{
            "rev-parse": _completed("abc123\n"),
            "status": _completed("\n"),
        }),
    ):
        git_ops_runner.execute_ensure_worktree(claimed)

    row = checkouts_db.get_checkout(project["id"], ME, branch="feat/x")
    assert row is not None and row["dirty"] is False


async def test_a_head_sha_failure_does_not_fail_the_op(project):
    """git blowing up on the state read must leave the op ``done`` — the
    checkout was materialized, and head_sha is only ever a display field."""
    from app.core import checkout_manager
    from app.core.git_service import git_service

    op = _queue(project, branch="feat/x")

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(return_value="/w/feat-x")
    ), patch.object(
        git_service,
        "_run_git",
        MagicMock(side_effect=OSError("git not found")),
    ):
        assert await git_ops_runner.claim_once() == 1

    settled = _got(op["id"])
    assert settled["status"] == "done"
    assert settled["result"]["path"] == "/w/feat-x"


def test_executor_raises_for_an_unknown_project(mock_db):
    op = git_ops_db.insert_op(
        ME, "ensure_worktree", project_v2_id="pv2-gone", params={"branch": "b"}
    )
    claimed = _claimed(op["id"])

    with pytest.raises(RuntimeError, match="unknown project_v2"):
        git_ops_runner.execute_ensure_worktree(claimed)


# ═══ claim_once (the loop pass) ══════════════════════════════════════════════


async def test_loop_claims_and_completes_its_own_ops(project):
    op = _queue(project)

    from app.core import checkout_manager

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(return_value="/w/feat-x")
    ):
        assert await git_ops_runner.claim_once() == 1

    settled = _got(op["id"])
    assert settled["status"] == "done"
    assert settled["result"]["path"] == "/w/feat-x"


async def test_loop_ignores_ops_targeted_at_another_install(project):
    op = _queue(project, runner=PEER)

    from app.core import checkout_manager

    with patch.object(
        checkout_manager, "ensure_checkout",
        MagicMock(side_effect=AssertionError("must not run another runner's op")),
    ):
        assert await git_ops_runner.claim_once() == 0

    assert _got(op["id"])["status"] == "queued"


async def test_loop_records_a_failure_on_the_row(project):
    op = _queue(project)

    from app.core import checkout_manager

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(side_effect=RuntimeError("boom"))
    ):
        await git_ops_runner.claim_once()

    settled = _got(op["id"])
    assert settled["status"] == "error"
    assert "boom" in settled["result"]["error"]


async def test_unknown_op_kind_is_failed_not_silently_dropped(mock_db):
    op = git_ops_db.insert_op(ME, "teleport", params={})

    await git_ops_runner.claim_once()

    settled = _got(op["id"])
    assert settled["status"] == "error"
    assert "unknown git op" in settled["result"]["error"]


# ═══ execute_refresh_status ══════════════════════════════════════════════════


def _seed_checkouts(project, tmp_path, n, *, runner=ME, start=0):
    from app.db import checkouts as checkouts_db

    rows = []
    for i in range(start, start + n):
        path = tmp_path / f"co-{runner}-{i}"
        path.mkdir()
        branch = None if i == 0 and runner == ME else f"b{i}"
        rows.append(
            checkouts_db.upsert_checkout(
                project["id"], runner, str(path), branch=branch
            )
        )
    return rows


def test_refresh_status_caps_at_three_checkouts(project, tmp_path):
    """Unbounded refresh would sit in front of ensure_worktree on claim_once."""
    _seed_checkouts(project, tmp_path, 5)
    _seed_checkouts(project, tmp_path, 2, runner=PEER, start=10)

    with patch.object(git_ops_runner, "_record_checkout_state") as rec:
        result = git_ops_runner.execute_refresh_status(
            {"project_v2_id": project["id"], "params": {}}
        )

    assert result == {"refreshed": 3, "skipped": 2}
    assert rec.call_count == 3
    for call in rec.call_args_list:
        assert call.args[0] == project["id"]
        assert f"co-{ME}-" in call.args[1]


def test_refresh_status_prefers_stalest_updated_at(project, tmp_path):
    """A second click must cover worktrees the first cap skipped."""
    from app.db.base import get_db

    rows = _seed_checkouts(project, tmp_path, 5)
    with get_db() as conn:
        cursor = conn.cursor()
        for i, row in enumerate(rows):
            cursor.execute(
                "UPDATE checkouts SET updated_at = %s WHERE id = %s",
                (f"2026-01-0{i + 1}T00:00:00", row["id"]),
            )

    with patch.object(git_ops_runner, "_record_checkout_state") as rec:
        git_ops_runner.execute_refresh_status(
            {"project_v2_id": project["id"], "params": {}}
        )

    assert [call.args[1] for call in rec.call_args_list] == [
        rows[0]["path"],
        rows[1]["path"],
        rows[2]["path"],
    ]

    with get_db() as conn:
        cursor = conn.cursor()
        for i, row in enumerate(rows[:3]):
            cursor.execute(
                "UPDATE checkouts SET updated_at = %s WHERE id = %s",
                (f"2026-02-0{i + 1}T00:00:00", row["id"]),
            )

    with patch.object(git_ops_runner, "_record_checkout_state") as rec:
        git_ops_runner.execute_refresh_status(
            {"project_v2_id": project["id"], "params": {}}
        )

    assert [call.args[1] for call in rec.call_args_list] == [
        rows[3]["path"],
        rows[4]["path"],
        rows[0]["path"],
    ]


def test_refresh_status_caps_at_20s_wall_clock(project, tmp_path):
    _seed_checkouts(project, tmp_path, 4)

    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        if calls["n"] == 1:
            return 1000.0  # deadline = 1020
        if calls["n"] == 2:
            return 1000.1  # first checkout still inside the window
        return 1021.0  # remaining rows are past the wall clock

    with patch.object(git_ops_runner.time, "monotonic", fake_monotonic), patch.object(
        git_ops_runner, "_record_checkout_state"
    ) as rec:
        result = git_ops_runner.execute_refresh_status(
            {"project_v2_id": project["id"], "params": {}}
        )

    assert result == {"refreshed": 1, "skipped": 3}
    assert rec.call_count == 1


async def test_loop_runs_refresh_status(project, tmp_path):
    _seed_checkouts(project, tmp_path, 1)
    op = git_ops_db.insert_op(
        ME, "refresh_status", project_v2_id=project["id"], params={}
    )

    with patch.object(git_ops_runner, "_record_checkout_state"):
        assert await git_ops_runner.claim_once() == 1

    settled = _got(op["id"])
    assert settled["status"] == "done"
    assert settled["result"] == {"refreshed": 1, "skipped": 0}


# ═══ spawn-path inline claim ═════════════════════════════════════════════════


def test_inline_claim_runs_a_matching_queued_op(project):
    from app.core import checkout_manager, query_options

    op = _queue(project, branch="feat/x")

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(return_value="/w/feat-x")
    ):
        ran = query_options._claim_and_run_pending_checkout(project, "feat/x")

    assert ran is True
    assert _got(op["id"])["status"] == "done"


def test_inline_claim_is_a_no_op_without_a_matching_op(project):
    from app.core import checkout_manager, query_options

    _queue(project, branch="some/other")

    with patch.object(
        checkout_manager, "ensure_checkout",
        MagicMock(side_effect=AssertionError("nothing to run")),
    ):
        assert query_options._claim_and_run_pending_checkout(project, "feat/x") is False


def test_inline_claim_loses_to_the_loop_and_reports_false(project):
    """When the loop already claimed the op, the spawn path must NOT re-run
    ``git worktree add`` against the same path — it refuses honestly instead.

    The sync helper must return False immediately (no wait). Waiting lives in
    ``ensure_pending_checkout``.
    """
    from app.core import checkout_manager, query_options

    op = _queue(project, branch="feat/x")
    git_ops_db.claim_op(op["id"], ME)  # the loop got there first

    slept: list = []
    with patch.object(
        checkout_manager, "ensure_checkout",
        MagicMock(side_effect=AssertionError("must not double-run")),
    ), patch("time.sleep", side_effect=lambda s: slept.append(s)):
        assert query_options._claim_and_run_pending_checkout(project, "feat/x") is False
    assert slept == []
    assert _got(op["id"])["status"] == "running"


def test_inline_claim_records_the_failure_and_reports_false(project):
    from app.core import checkout_manager, query_options

    op = _queue(project, branch="feat/x")

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(side_effect=RuntimeError("nope"))
    ):
        assert query_options._claim_and_run_pending_checkout(project, "feat/x") is False

    assert _got(op["id"])["status"] == "error"


def test_inline_claim_ignores_another_installs_op(project):
    from app.core import checkout_manager, query_options

    op = _queue(project, runner=PEER, branch="feat/x")

    with patch.object(
        checkout_manager, "ensure_checkout",
        MagicMock(side_effect=AssertionError("not ours")),
    ):
        assert query_options._claim_and_run_pending_checkout(project, "feat/x") is False

    assert _got(op["id"])["status"] == "queued"


def test_inline_claim_without_a_branch_is_a_no_op(project):
    from app.core import query_options

    assert query_options._claim_and_run_pending_checkout(project, None) is False


# ═══ async spawn preflight (ensure_pending_checkout) ═════════════════════════


def _session(project, branch="feat/x"):
    return {"id": "ses-1", "project_v2_id": project["id"], "branch": branch}


def _backdate(op_id: str, minutes: int):
    from datetime import datetime, timedelta, timezone
    from app.db.base import get_db

    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE git_ops SET updated_at = %s WHERE id = %s", (old, op_id)
        )


async def test_ensure_pending_checkout_done_op_returns_true_without_ensure_checkout(
    project,
):
    """Wait path: a running op that settles ``done`` is True and must not
    re-run ``git worktree add``."""
    from app.core import checkout_manager, query_options

    op = _queue(project, branch="feat/x")
    git_ops_db.claim_op(op["id"], ME)
    real_get = git_ops_db.get_op

    def complete_then_get(op_id):
        git_ops_db.complete_op(op_id, {"path": "/w/feat-x"})
        return real_get(op_id)

    with patch.object(
        checkout_manager, "ensure_checkout",
        MagicMock(side_effect=AssertionError("must not double-run")),
    ), patch.object(git_ops_db, "get_op", side_effect=complete_then_get):
        assert await query_options.ensure_pending_checkout(_session(project)) is True


async def test_ensure_pending_checkout_timeout_returns_false_without_ensure_checkout(
    project,
):
    from app.core import checkout_manager, query_options

    op = _queue(project, branch="feat/x")
    git_ops_db.claim_op(op["id"], ME)

    with patch.object(
        checkout_manager, "ensure_checkout",
        MagicMock(side_effect=AssertionError("must not double-run")),
    ):
        assert await query_options.ensure_pending_checkout(
            _session(project), timeout_s=0.0
        ) is False
    assert _got(op["id"])["status"] == "running"


async def test_ensure_pending_checkout_error_returns_false_without_ensure_checkout(
    project,
):
    from app.core import checkout_manager, query_options

    op = _queue(project, branch="feat/x")
    git_ops_db.claim_op(op["id"], ME)
    real_get = git_ops_db.get_op

    def fail_then_get(op_id):
        git_ops_db.fail_op(op_id, "nope")
        return real_get(op_id)

    with patch.object(
        checkout_manager, "ensure_checkout",
        MagicMock(side_effect=AssertionError("must not double-run")),
    ), patch.object(git_ops_db, "get_op", side_effect=fail_then_get):
        assert await query_options.ensure_pending_checkout(_session(project)) is False


async def test_ensure_pending_checkout_zombie_plus_queued_claims_queued_once(
    project,
):
    """Stale running must not hide a newer queued retry: claim the queued
    row and run ``ensure_checkout`` once."""
    from app.core import checkout_manager, query_options

    zombie = _queue(project, branch="feat/x")
    git_ops_db.claim_op(zombie["id"], ME)
    _backdate(zombie["id"], minutes=180)

    queued = _queue(project, branch="feat/x")

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(return_value="/w/feat-x")
    ) as ensure:
        assert await query_options.ensure_pending_checkout(_session(project)) is True

    assert ensure.call_count == 1
    assert _got(queued["id"])["status"] == "done"
    assert _got(zombie["id"])["status"] == "error"
    assert "stale running" in _got(zombie["id"])["result"]["error"]


async def test_ensure_pending_checkout_does_not_fail_fresh_running(project):
    """A running row younger than 15 minutes is the wait target, never failed."""
    from app.core import checkout_manager, query_options

    op = _queue(project, branch="feat/x")
    git_ops_db.claim_op(op["id"], ME)
    _backdate(op["id"], minutes=5)

    with patch.object(
        checkout_manager, "ensure_checkout",
        MagicMock(side_effect=AssertionError("must not double-run")),
    ):
        assert await query_options.ensure_pending_checkout(
            _session(project), timeout_s=0.0
        ) is False
    assert _got(op["id"])["status"] == "running"


def test_find_matching_never_calls_fail_op(project):
    zombie = _queue(project, branch="feat/x")
    git_ops_db.claim_op(zombie["id"], ME)
    _backdate(zombie["id"], minutes=20)
    with patch.object(git_ops_db, "fail_op") as fail:
        found = git_ops_db.find_matching(
            ME, project["id"], "ensure_worktree", branch="feat/x"
        )
        fail.assert_not_called()
    assert found is None


async def test_ensure_pending_checkout_claims_queued_and_runs(project):
    from app.core import checkout_manager, query_options

    op = _queue(project, branch="feat/x")

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(return_value="/w/feat-x")
    ):
        assert await query_options.ensure_pending_checkout(_session(project)) is True
    assert _got(op["id"])["status"] == "done"


def test_executor_heartbeats_updated_at_at_start(project):
    """A live add past a few minutes must not look stale: touch before git."""
    from app.core import checkout_manager

    op = _queue(project)
    claimed = _claimed(op["id"])
    _backdate(op["id"], minutes=20)
    assert git_ops_db._is_stale_running(_got(op["id"]))

    with patch.object(
        checkout_manager, "ensure_checkout", MagicMock(return_value="/w/feat-x")
    ):
        git_ops_runner.execute_ensure_worktree(claimed)

    assert not git_ops_db._is_stale_running(_got(op["id"]))


# ═══ sweeper ═════════════════════════════════════════════════════════════════


def test_sweep_once_reports_both_tables(mock_db):
    assert set(git_ops_runner.sweep_once()) == {"git_ops", "upload_blobs"}

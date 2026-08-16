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
from app.core.config import settings
from app.db import git_ops as git_ops_db

ME = "local-me"
PEER = "peer-1"
PV2 = "pv2-abc"


@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: ME)
    )


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
    ``git worktree add`` against the same path — it refuses honestly instead."""
    from app.core import checkout_manager, query_options

    op = _queue(project, branch="feat/x")
    git_ops_db.claim_op(op["id"], ME)  # the loop got there first

    with patch.object(
        checkout_manager, "ensure_checkout",
        MagicMock(side_effect=AssertionError("must not double-run")),
    ):
        assert query_options._claim_and_run_pending_checkout(project, "feat/x") is False


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


# ═══ sweeper ═════════════════════════════════════════════════════════════════


def test_sweep_once_reports_both_tables(mock_db):
    assert set(git_ops_runner.sweep_once()) == {"git_ops", "upload_blobs"}

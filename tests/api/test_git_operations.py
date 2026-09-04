"""
Unit tests for the composite git-operation orchestrator (app/api/git_operations.py).

Two layers:

* **Executor tests** await ``_execute_finish`` / ``_execute_ship`` directly with
  mocked services so the per-step outcomes (happy path, merge failure,
  already-merged, cleanup failure/guard, ship step-set) are deterministic.
* **Endpoint tests** drive the HTTP layer through a TestClient for the
  synchronous guard 400s, 409 concurrency, op lifecycle, 404, and TTL sweep.
  ``asyncio.create_task`` is stubbed so the background flow doesn't race the
  assertions.
"""

import time
import pytest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.git_operations as gitops
import app.api.git_helpers as git_helpers
from app.api.git_operations import (
    Operation,
    _execute_finish,
    _execute_ship,
    _sweep_expired,
    _step,
    router,
    require_auth,
)


# =============================================================================
# Helpers / fixtures
# =============================================================================

def _finish_op(project_id="p1", session_id=None):
    op = Operation(
        project_id=project_id,
        kind="finish",
        session_id=session_id,
        steps=[
            _step("guard", "Validate branch and pull request"),
            _step("push", "Push commits"),
            _step("merge", "Merge pull request"),
            _step("cleanup", "Clean up branch"),
        ],
    )
    op.set_step("guard", "done")
    return op


def _ship_op(project_id="p1", session_id=None):
    return Operation(
        project_id=project_id,
        kind="ship",
        session_id=session_id,
        steps=[
            _step("commit", "Commit changes"),
            _step("push", "Push commits"),
            _step("create_pr", "Create pull request"),
            _step("merge", "Merge pull request"),
            _step("cleanup", "Clean up branch"),
        ],
    )


def _pr(number=7, url="https://github.com/o/r/pull/7", head="feat/x"):
    return SimpleNamespace(number=number, url=url, head_branch=head, state="open")


def _step_status(op, step_id):
    return next(s["status"] for s in op.steps if s["id"] == step_id)


@pytest.fixture(autouse=True)
def _clear_registry():
    gitops._operations.clear()
    yield
    gitops._operations.clear()


@pytest.fixture
def mock_git_service():
    m = MagicMock()
    m.is_git_repo.return_value = True
    m.get_status.return_value = {"is_clean": True, "ahead": 0, "current_branch": "feat/x"}
    m.get_current_branch.return_value = "feat/x"
    m.get_default_branch.return_value = "main"
    m.push.return_value = (True, None)
    m.pull.return_value = (True, None, [])
    m.commit.return_value = (True, "abc123")
    m.stage_files.return_value = True
    m.checkout.return_value = True
    m.delete_branch.return_value = True
    return m


@pytest.fixture
def mock_github_service():
    m = MagicMock()
    m.get_repo_from_remote.return_value = "o/r"
    m.list_pulls.return_value = [_pr()]
    m.merge_pull.return_value = (True, None)
    m.create_pull.return_value = (_pr(), None)
    return m


@pytest.fixture
def mock_worktree_manager():
    m = MagicMock()
    m.cleanup_worktree = AsyncMock(return_value=True)
    return m


# =============================================================================
# Executor: finish
# =============================================================================

class TestExecuteFinish:
    async def test_happy_path_worktree_mode(self, mock_git_service, mock_github_service, mock_worktree_manager):
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 3}
        op = _finish_op()
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ):
            await _execute_finish(
                op, working_dir="/wd", worktree_id="wt-1", repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="s1",
                status={"is_clean": True, "ahead": 3},
            )

        assert op.status == "done"
        assert _step_status(op, "push") == "done"
        assert _step_status(op, "merge") == "done"
        assert _step_status(op, "cleanup") == "done"
        assert op.result["pr_number"] == 7
        assert op.result["worktree_deleted"] is True
        mock_worktree_manager.cleanup_worktree.assert_awaited_once_with("wt-1", keep_branch=False)
        mock_github_service.merge_pull.assert_called_once_with("o/r", 7, "squash")

    async def test_finish_persists_merged_and_keeps_session_active(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        from app.db import database

        database.create_session(session_id="s1", profile_id="default", is_active=True)
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        op = _finish_op(session_id="s1")
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ):
            await _execute_finish(
                op, working_dir="/wd", worktree_id=None, repo="o/r",
                pr_number=7, pr_url="https://github.com/o/r/pull/7",
                feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=False, session_id="s1",
                status={"is_clean": True, "ahead": 0},
            )

        assert op.status == "done"
        row = database.get_session("s1")
        assert row["pr_state"] == "merged"
        assert row["pr_number"] == 7
        assert row["pr_url"] == "https://github.com/o/r/pull/7"
        assert row["is_active"]

    async def test_happy_path_project_mode(self, mock_git_service, mock_github_service, mock_worktree_manager):
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        op = _finish_op()
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ):
            await _execute_finish(
                op, working_dir="/wd", worktree_id=None, repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="merge", cleanup=True, session_id=None,
            )

        assert op.status == "done"
        assert _step_status(op, "push") == "skipped"  # not ahead
        assert _step_status(op, "cleanup") == "done"
        mock_git_service.checkout.assert_called_once()
        mock_git_service.delete_branch.assert_called_once()
        mock_worktree_manager.cleanup_worktree.assert_not_called()

    async def test_merge_failure_stops_and_leaves_later_steps_pending(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        mock_github_service.merge_pull.return_value = (False, "merge conflict")
        op = _finish_op()
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ):
            await _execute_finish(
                op, working_dir="/wd", worktree_id="wt-1", repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id=None,
            )

        assert op.status == "error"
        assert _step_status(op, "merge") == "error"
        assert _step_status(op, "cleanup") == "pending"  # never reached
        mock_worktree_manager.cleanup_worktree.assert_not_called()

    async def test_already_merged_treated_as_success(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        mock_github_service.merge_pull.return_value = (False, "Pull request #7 is already merged")
        op = _finish_op()
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ):
            await _execute_finish(
                op, working_dir="/wd", worktree_id="wt-1", repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=False, session_id=None,
            )

        assert op.status == "done"
        assert _step_status(op, "merge") == "done"
        assert _step_status(op, "cleanup") == "skipped"

    async def test_cleanup_failure_after_merge_is_warning_not_error(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        mock_worktree_manager.cleanup_worktree = AsyncMock(return_value=False)
        op = _finish_op()
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ):
            await _execute_finish(
                op, working_dir="/wd", worktree_id="wt-1", repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id=None,
            )

        assert op.status == "done"
        assert _step_status(op, "merge") == "done"
        assert _step_status(op, "cleanup") == "warning"

    async def test_cleanup_guard_other_session_streaming(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        op = _finish_op(session_id="me")

        mock_db = MagicMock()
        mock_db.get_sessions_for_worktree.return_value = [{"id": "me"}, {"id": "other"}]

        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ), patch(
            "app.core.streaming_status.has_active_run",
            side_effect=lambda sid: sid == "other",
        ), patch("app.db.database.get_sessions_for_worktree", mock_db.get_sessions_for_worktree):
            await _execute_finish(
                op, working_dir="/wd", worktree_id="wt-1", repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="me",
            )

        assert op.status == "done"
        assert _step_status(op, "cleanup") == "warning"
        mock_worktree_manager.cleanup_worktree.assert_not_called()

    async def test_cleanup_guard_worktree_other_session_background_work_only(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        """A PARKED sibling session (not streaming) with live background work
        must still block worktree cleanup — the liveness guard counts
        task_registry.has_active_background_work, not just the streaming read.
        """
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        op = _finish_op(session_id="me")

        mock_reg = MagicMock()
        mock_reg.has_active_background_work.side_effect = lambda sid: sid == "other"
        mock_db = MagicMock()
        mock_db.get_sessions_for_worktree.return_value = [{"id": "me"}, {"id": "other"}]

        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ), patch(
            "app.core.streaming_status.has_active_run",
            return_value=False,  # nobody is streaming
        ), patch("app.core.task_registry.task_registry", mock_reg), \
             patch("app.db.database.get_sessions_for_worktree", mock_db.get_sessions_for_worktree):
            await _execute_finish(
                op, working_dir="/wd", worktree_id="wt-1", repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="me",
                status={"ahead": 0},
            )

        assert op.status == "done"
        assert _step_status(op, "cleanup") == "warning"
        mock_worktree_manager.cleanup_worktree.assert_not_called()

    async def test_cleanup_guard_other_session_active_run_only(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        """A sibling session with a lease-valid active run must block worktree
        cleanup even with NO live background work: the durable ``runs`` table
        is the single streaming truth (durable across restarts, shared across
        installs), so the liveness guard must consult it — otherwise cleanup
        would delete the worktree out from under a live turn.
        """
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        op = _finish_op(session_id="me")

        mock_reg = MagicMock()
        mock_reg.has_active_background_work.return_value = False  # no bg work
        mock_db = MagicMock()
        mock_db.get_sessions_for_worktree.return_value = [{"id": "me"}, {"id": "other"}]

        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ), patch(
            "app.core.streaming_status.has_active_run",
            # Only "other" has a lease-valid active run.
            side_effect=lambda sid: sid == "other",
        ), patch("app.core.task_registry.task_registry", mock_reg), \
             patch("app.db.database.get_sessions_for_worktree", mock_db.get_sessions_for_worktree):
            await _execute_finish(
                op, working_dir="/wd", worktree_id="wt-1", repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="me",
                status={"ahead": 0},
            )

        assert op.status == "done"
        assert _step_status(op, "cleanup") == "warning"
        mock_worktree_manager.cleanup_worktree.assert_not_called()

    async def test_cleanup_guard_project_mode_other_streaming_session(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        """Project-mode cleanup (no worktree) rewrites the shared main checkout;
        it must refuse when another of the project's sessions is streaming.
        """
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        op = _finish_op(project_id="p1", session_id="me")

        mock_reg = MagicMock()
        mock_reg.has_active_background_work.return_value = False
        mock_db = MagicMock()
        mock_db.get_sessions.return_value = [{"id": "me"}, {"id": "other"}]

        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ), patch(
            "app.core.streaming_status.has_active_run",
            side_effect=lambda sid: sid == "other",
        ), patch("app.core.task_registry.task_registry", mock_reg), \
             patch("app.db.database.get_sessions", mock_db.get_sessions):
            await _execute_finish(
                op, working_dir="/wd", worktree_id=None, repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="me",
                status={"ahead": 0},
            )

        assert op.status == "done"
        assert _step_status(op, "cleanup") == "warning"
        # The dangerous ops must NOT have run.
        mock_git_service.checkout.assert_not_called()
        mock_git_service.delete_branch.assert_not_called()

    async def test_linked_worktree_cleanup_removes_worktree_not_checkout_main(
        self, mock_git_service, mock_github_service, mock_worktree_manager, tmp_path
    ):
        """V2 branch sessions have no worktree_id. Their cwd is a linked git
        worktree; checking out main there fails because main is already held by
        the base checkout. Finish must ``git worktree remove`` instead.
        """
        main = tmp_path / "main"
        wt = main / ".fluid" / "worktrees" / "feat-x"
        wt.mkdir(parents=True)
        gitdir = main / ".git" / "worktrees" / "feat-x"
        (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        mock_git_service.remove_worktree.return_value = True
        op = _finish_op()
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ), patch("app.db.checkouts.archive_checkout_by_path", return_value=None):
            await _execute_finish(
                op, working_dir=str(wt), worktree_id=None, repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id=None,
                status={"ahead": 0},
            )

        assert op.status == "done"
        assert _step_status(op, "cleanup") == "done"
        assert op.result["worktree_deleted"] is True
        mock_git_service.remove_worktree.assert_called_once_with(str(main), str(wt), True)
        mock_git_service.delete_branch.assert_called_once_with(str(main), "feat/x", True)
        mock_git_service.checkout.assert_not_called()
        mock_worktree_manager.cleanup_worktree.assert_not_called()

    async def test_linked_worktree_cleanup_rebinds_session_off_deleted_branch(
        self, mock_git_service, mock_github_service, mock_worktree_manager, tmp_path
    ):
        """After deleting the feature branch, sessions still bound to it cannot
        rematerialize — rebind them onto the base checkout."""
        main = tmp_path / "main"
        wt = main / ".fluid" / "worktrees" / "feat-x"
        wt.mkdir(parents=True)
        gitdir = main / ".git" / "worktrees" / "feat-x"
        (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        mock_git_service.remove_worktree.return_value = True
        op = _finish_op(session_id="s1")
        set_branch = MagicMock()
        list_sessions = MagicMock(return_value=[{"id": "other"}])
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ), patch("app.db.checkouts.archive_checkout_by_path", return_value=None), patch(
            "app.db.database.set_session_branch", set_branch
        ), patch(
            "app.db.database.list_sessions_for_branch", list_sessions
        ):
            await _execute_finish(
                op, working_dir=str(wt), worktree_id=None, repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="s1",
                status={"ahead": 0},
            )

        assert op.status == "done"
        assert _step_status(op, "cleanup") == "done"
        assert ("p1", "feat/x") in [c.args for c in list_sessions.call_args_list]
        set_branch.assert_any_call("s1", None)
        set_branch.assert_any_call("other", None)

    async def test_linked_worktree_cleanup_failure_is_warning(
        self, mock_git_service, mock_github_service, mock_worktree_manager, tmp_path
    ):
        main = tmp_path / "main"
        wt = main / ".fluid" / "worktrees" / "feat-x"
        wt.mkdir(parents=True)
        gitdir = main / ".git" / "worktrees" / "feat-x"
        (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        mock_git_service.remove_worktree.return_value = False
        op = _finish_op()
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ):
            await _execute_finish(
                op, working_dir=str(wt), worktree_id=None, repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id=None,
                status={"ahead": 0},
            )

        assert op.status == "done"
        assert _step_status(op, "cleanup") == "warning"
        mock_git_service.checkout.assert_not_called()
        mock_git_service.delete_branch.assert_not_called()


# =============================================================================
# Executor: ship (step-set construction)
# =============================================================================

class TestExecuteShip:
    async def test_clean_tree_skips_commit_existing_pr_skips_create(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        op = _ship_op()
        existing = _pr(number=9, url="https://github.com/o/r/pull/9")
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ):
            await _execute_ship(
                op, working_dir="/wd", worktree_id=None, repo="o/r",
                feature_branch="feat/x", default_branch="main", base_branch="main",
                existing_pr=existing, dirty=False, include_unstaged=True,
                merge_method="squash", draft=False, cleanup=False, session_id=None,
            )

        assert op.status == "done"
        assert _step_status(op, "commit") == "skipped"
        assert _step_status(op, "create_pr") == "skipped"
        assert _step_status(op, "push") == "done"
        assert _step_status(op, "merge") == "done"
        assert op.result["pr_number"] == 9
        mock_github_service.create_pull.assert_not_called()

    async def test_dirty_commits_and_creates_pr(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        op = _ship_op()
        gen_msg = AsyncMock(return_value="feat: ship it")
        gen_pr = AsyncMock(return_value=("feat: ship it", "## Summary\n- x"))
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
            generate_commit_message_for_dir=gen_msg,
            generate_pr_description_for_dir=gen_pr,
        ), patch.object(git_helpers, "git_service", mock_git_service):
            await _execute_ship(
                op, working_dir="/wd", worktree_id=None, repo="o/r",
                feature_branch="feat/x", default_branch="main", base_branch="main",
                existing_pr=None, dirty=True, include_unstaged=True,
                merge_method="squash", draft=True, cleanup=False, session_id=None,
            )

        assert op.status == "done"
        assert _step_status(op, "commit") == "done"
        assert _step_status(op, "create_pr") == "done"
        gen_msg.assert_awaited_once()
        mock_git_service.commit.assert_called_once()
        # draft flag forwarded to create_pull
        assert mock_github_service.create_pull.call_args.args[-1] is True

    async def test_push_failure_stops(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        mock_git_service.push.return_value = (False, "rejected")
        op = _ship_op()
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ):
            await _execute_ship(
                op, working_dir="/wd", worktree_id=None, repo="o/r",
                feature_branch="feat/x", default_branch="main", base_branch="main",
                existing_pr=_pr(), dirty=False, include_unstaged=True,
                merge_method="squash", draft=False, cleanup=False, session_id=None,
            )

        assert op.status == "error"
        assert _step_status(op, "push") == "error"
        assert _step_status(op, "merge") == "pending"


# =============================================================================
# TTL sweep + Operation snapshot
# =============================================================================

class TestRegistry:
    def test_sweep_drops_expired_done_ops_keeps_running(self):
        done = Operation(project_id="p1", kind="finish", steps=[])
        done.status = "done"
        done.created_at = time.monotonic() - 999999
        running = Operation(project_id="p2", kind="ship", steps=[])
        running.created_at = time.monotonic() - 999999  # old but still running
        gitops._operations[done.id] = done
        gitops._operations[running.id] = running

        _sweep_expired()

        assert done.id not in gitops._operations
        assert running.id in gitops._operations

    def test_snapshot_shape(self):
        op = _finish_op()
        op.set_step("push", "done", detail="Pushed 2 commit(s)")
        snap = op.snapshot()
        assert snap["operation_id"] == op.id
        assert snap["status"] == "running"
        push = next(s for s in snap["steps"] if s["id"] == "push")
        assert push["status"] == "done"
        assert push["detail"] == "Pushed 2 commit(s)"


# =============================================================================
# Endpoint layer
# =============================================================================

def _no_run(coro):
    """Stub for asyncio.create_task: close the coroutine, don't schedule it."""
    coro.close()
    return MagicMock()


@pytest.fixture
def op_app():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-token"
    return app


@pytest.fixture
def op_client(op_app, mock_git_service, mock_github_service, mock_worktree_manager):
    with ExitStack() as stack:
        stack.enter_context(patch.object(gitops, "git_service", mock_git_service))
        stack.enter_context(patch.object(gitops, "github_service", mock_github_service))
        stack.enter_context(patch.object(gitops, "worktree_manager", mock_worktree_manager))
        stack.enter_context(patch.object(gitops, "check_project_access", MagicMock()))
        stack.enter_context(patch.object(gitops, "get_working_dir_async", AsyncMock(return_value="/wd")))
        stack.enter_context(patch.object(gitops.asyncio, "create_task", _no_run))
        with TestClient(op_app) as client:
            yield client


class TestFinishEndpoint:
    def test_lifecycle_returns_running_and_registers(self, op_client):
        resp = op_client.post("/api/v1/projects/p1/git/finish", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["operation_id"] in gitops._operations
        # guard already validated synchronously
        guard = next(s for s in data["steps"] if s["id"] == "guard")
        assert guard["status"] == "done"

    def test_not_a_repo_400(self, op_client, mock_git_service):
        mock_git_service.is_git_repo.return_value = False
        resp = op_client.post("/api/v1/projects/p1/git/finish", json={})
        assert resp.status_code == 400
        assert "not a git repository" in resp.json()["detail"]

    def test_dirty_tree_400(self, op_client, mock_git_service):
        mock_git_service.get_status.return_value = {"is_clean": False, "ahead": 0}
        resp = op_client.post("/api/v1/projects/p1/git/finish", json={})
        assert resp.status_code == 400
        assert "commit or discard" in resp.json()["detail"]

    def test_branch_pr_mismatch_400(self, op_client, mock_github_service):
        mock_github_service.list_pulls.return_value = [_pr(head="some-other-branch")]
        resp = op_client.post("/api/v1/projects/p1/git/finish", json={})
        assert resp.status_code == 400
        assert "No open pull request" in resp.json()["detail"]

    def test_concurrent_op_409(self, op_client):
        first = op_client.post("/api/v1/projects/p1/git/finish", json={})
        assert first.status_code == 200
        second = op_client.post("/api/v1/projects/p1/git/finish", json={})
        assert second.status_code == 409

    def test_concurrent_allowed_across_projects(self, op_client):
        assert op_client.post("/api/v1/projects/p1/git/finish", json={}).status_code == 200
        assert op_client.post("/api/v1/projects/p2/git/finish", json={}).status_code == 200

    def test_worktree_id_from_body_reaches_working_dir(self, op_client):
        """Regression: worktree_id is a BODY field (the frontend sends it in the
        JSON body). It must reach get_working_dir so worktree ops run against the
        worktree, not the main repo. (Previously declared as a query param → the
        body value was silently dropped and every op hit the main checkout.)
        """
        resp = op_client.post(
            "/api/v1/projects/p1/git/finish",
            json={"worktree_id": "wt-1", "session_id": "ses-1", "cleanup": True},
        )
        assert resp.status_code == 200
        gitops.get_working_dir_async.assert_any_call("p1", "wt-1", "ses-1")


class TestShipEndpoint:
    def test_lifecycle_returns_running(self, op_client):
        resp = op_client.post("/api/v1/projects/p1/git/ship", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert {s["id"] for s in data["steps"]} == {"commit", "push", "create_pr", "merge", "cleanup"}

    def test_not_a_repo_400(self, op_client, mock_git_service):
        mock_git_service.is_git_repo.return_value = False
        resp = op_client.post("/api/v1/projects/p1/git/ship", json={})
        assert resp.status_code == 400

    def test_worktree_id_from_body_reaches_working_dir(self, op_client):
        resp = op_client.post(
            "/api/v1/projects/p1/git/ship",
            json={"worktree_id": "wt-9", "session_id": "ses-2"},
        )
        assert resp.status_code == 200
        gitops.get_working_dir_async.assert_any_call("p1", "wt-9", "ses-2")

    def test_head_equals_base_400(self, op_client, mock_git_service):
        """Backstop: can't ship the base branch onto itself (head == base)."""
        mock_git_service.get_current_branch.return_value = "main"
        mock_git_service.get_default_branch.return_value = "main"
        resp = op_client.post("/api/v1/projects/p1/git/ship", json={})
        assert resp.status_code == 400
        assert "base branch" in resp.json()["detail"]
        # Guard failure must not leave a placeholder op registered.
        assert not gitops._operations

    def test_head_equals_base_explicit_base_400(self, op_client, mock_git_service):
        mock_git_service.get_current_branch.return_value = "release"
        resp = op_client.post(
            "/api/v1/projects/p1/git/ship", json={"base_branch": "release"}
        )
        assert resp.status_code == 400


class TestGetOperationEndpoint:
    def test_unknown_op_404(self, op_client):
        resp = op_client.get("/api/v1/projects/p1/git/operations/does-not-exist")
        assert resp.status_code == 404

    def test_returns_snapshot(self, op_client):
        created = op_client.post("/api/v1/projects/p1/git/finish", json={}).json()
        op_id = created["operation_id"]
        resp = op_client.get(f"/api/v1/projects/p1/git/operations/{op_id}")
        assert resp.status_code == 200
        assert resp.json()["operation_id"] == op_id

    def test_wrong_project_404(self, op_client):
        created = op_client.post("/api/v1/projects/p1/git/finish", json={}).json()
        op_id = created["operation_id"]
        resp = op_client.get(f"/api/v1/projects/p2/git/operations/{op_id}")
        assert resp.status_code == 404


# =============================================================================
# Guard race: check + register must be atomic
# =============================================================================

class TestConcurrentGuardRace:
    async def test_two_concurrent_finish_posts_exactly_one_200_one_409(
        self, op_app, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        """Two overlapping requests for the same project must not both start an
        op: the active-op check and _register are now adjacent (no await between
        them), so exactly one gets 200 and the other 409.
        """
        import httpx

        with ExitStack() as stack:
            stack.enter_context(patch.object(gitops, "git_service", mock_git_service))
            stack.enter_context(patch.object(gitops, "github_service", mock_github_service))
            stack.enter_context(patch.object(gitops, "worktree_manager", mock_worktree_manager))
            stack.enter_context(patch.object(gitops, "check_project_access", MagicMock()))
            stack.enter_context(patch.object(gitops, "get_working_dir_async", AsyncMock(return_value="/wd")))
            stack.enter_context(patch.object(gitops.asyncio, "create_task", _no_run))

            transport = httpx.ASGITransport(app=op_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                import asyncio as _asyncio
                r1, r2 = await _asyncio.gather(
                    client.post("/api/v1/projects/pracey/git/finish", json={}),
                    client.post("/api/v1/projects/pracey/git/finish", json={}),
                )

        codes = sorted([r1.status_code, r2.status_code])
        assert codes == [200, 409]
        # Exactly one op registered for the project.
        assert sum(1 for o in gitops._operations.values() if o.project_id == "pracey") == 1

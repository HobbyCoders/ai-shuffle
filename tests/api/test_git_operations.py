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
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.git_operations as gitops
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
            )

        assert op.status == "done"
        assert _step_status(op, "push") == "done"
        assert _step_status(op, "merge") == "done"
        assert _step_status(op, "cleanup") == "done"
        assert op.result["pr_number"] == 7
        assert op.result["worktree_deleted"] is True
        mock_worktree_manager.cleanup_worktree.assert_awaited_once_with("wt-1", keep_branch=False)
        mock_github_service.merge_pull.assert_called_once_with("o/r", 7, "squash")

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

        mock_sync = MagicMock()
        mock_sync.is_session_streaming.side_effect = lambda sid: sid == "other"
        mock_db = MagicMock()
        mock_db.get_sessions_for_worktree.return_value = [{"id": "me"}, {"id": "other"}]

        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ), patch("app.core.sync_engine.sync_engine", mock_sync), \
             patch("app.db.database.get_sessions_for_worktree", mock_db.get_sessions_for_worktree):
            await _execute_finish(
                op, working_dir="/wd", worktree_id="wt-1", repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="me",
            )

        assert op.status == "done"
        assert _step_status(op, "cleanup") == "warning"
        mock_worktree_manager.cleanup_worktree.assert_not_called()


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
        ):
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
        stack.enter_context(patch.object(gitops, "get_working_dir", MagicMock(return_value="/wd")))
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

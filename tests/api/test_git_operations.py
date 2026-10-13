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
    return SimpleNamespace(number=number, url=url, head_branch=head, base_branch="main", state="open")


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
    m.get_pull.return_value = _pr()
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

    @pytest.mark.parametrize("activity", ["turn", "background"])
    @pytest.mark.parametrize("busy_session", ["other", "me"])
    async def test_cleanup_stops_busy_worktree_sessions_then_removes(
        self, mock_git_service, mock_github_service, mock_worktree_manager,
        busy_session, activity,
    ):
        """The worktree is about to disappear, so a busy session in it (a
        sibling, or the shipping session itself) is stopped, not left running
        with its cwd deleted and not a reason to keep the worktree. A turn
        shows up in the durable ``runs`` table; a parked session's background
        work only in the task registry.
        """
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        op = _finish_op(session_id="me")
        busy = {busy_session}
        stopped = []

        async def stop(sid):
            stopped.append(sid)
            busy.discard(sid)

        def background_work(sid):
            return activity == "background" and sid in busy

        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ), patch(
            "app.core.streaming_status.has_active_run",
            side_effect=lambda sid: activity == "turn" and sid in busy,
        ), patch(
            "app.core.task_registry.task_registry.has_active_background_work",
            side_effect=background_work,
        ), patch(
            "app.db.database.get_sessions_for_worktree",
            return_value=[{"id": "me"}, {"id": "other"}, {"id": "idle"}],
        ), patch.object(gitops, "_stop_session_for_cleanup", side_effect=stop):
            await _execute_finish(
                op, working_dir="/wd", worktree_id="wt-1", repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="me",
                status={"ahead": 0},
            )

        assert op.status == "done"
        assert stopped == [busy_session]  # the idle sessions are left alone
        cleanup = next(s for s in op.steps if s["id"] == "cleanup")
        assert cleanup["status"] == "done"
        assert cleanup["detail"] == "Stopped 1 session, removed worktree"
        assert op.result["worktree_deleted"] is True
        mock_worktree_manager.cleanup_worktree.assert_awaited_once_with("wt-1", keep_branch=False)

    async def test_cleanup_with_idle_worktree_sessions_stops_nothing(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        op = _finish_op(session_id="me")
        stop = AsyncMock()
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ), patch.object(gitops, "_session_is_busy", return_value=False), patch(
            "app.db.database.get_sessions_for_worktree",
            return_value=[{"id": "me"}, {"id": "other"}],
        ), patch.object(gitops, "_stop_session_for_cleanup", stop):
            await gitops._do_cleanup(op, "/wd", "wt-1", "feat/x", "main", "me")

        stop.assert_not_awaited()
        cleanup = next(s for s in op.steps if s["id"] == "cleanup")
        assert (cleanup["status"], cleanup["detail"]) == ("done", "Removed worktree")
        mock_worktree_manager.cleanup_worktree.assert_awaited_once()

    @pytest.mark.parametrize("stop_raises", [False, True])
    async def test_cleanup_keeps_worktree_when_a_session_does_not_stop(
        self, mock_git_service, mock_worktree_manager, monkeypatch, stop_raises,
    ):
        """A session still busy at the deadline keeps its worktree, whether its
        stop timed out or raised. The merge already landed, so this is a
        warning, never an error.
        """
        monkeypatch.setattr(gitops, "_CLEANUP_STOP_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(gitops, "_CLEANUP_STOP_POLL_SECONDS", 0.01)
        op = _finish_op(session_id="me")
        stop = AsyncMock(side_effect=RuntimeError("wedged") if stop_raises else None)
        with patch.multiple(
            gitops, git_service=mock_git_service, worktree_manager=mock_worktree_manager,
        ), patch.object(gitops, "_session_is_busy", side_effect=lambda sid: sid == "other"), patch(
            "app.db.database.get_sessions_for_worktree",
            return_value=[{"id": "me"}, {"id": "other"}],
        ), patch.object(gitops, "_stop_session_for_cleanup", stop):
            await gitops._do_cleanup(op, "/wd", "wt-1", "feat/x", "main", "me")

        stop.assert_awaited_once_with("other")
        cleanup = next(s for s in op.steps if s["id"] == "cleanup")
        assert cleanup["status"] == "warning"
        assert cleanup["detail"] == "Skipped: 1 session in this worktree did not stop in time"
        assert "worktree_deleted" not in op.result
        mock_worktree_manager.cleanup_worktree.assert_not_called()

    async def test_cleanup_keeps_worktree_when_session_lookup_fails(
        self, mock_git_service, mock_worktree_manager,
    ):
        op = _finish_op(session_id="me")
        stop = AsyncMock()
        with patch.multiple(
            gitops, git_service=mock_git_service, worktree_manager=mock_worktree_manager,
        ), patch(
            "app.db.database.get_sessions_for_worktree", side_effect=RuntimeError("db down"),
        ), patch.object(gitops, "_stop_session_for_cleanup", stop):
            await gitops._do_cleanup(op, "/wd", "wt-1", "feat/x", "main", "me")

        stop.assert_not_awaited()
        assert _step_status(op, "cleanup") == "warning"
        mock_worktree_manager.cleanup_worktree.assert_not_called()

    async def test_stop_matches_the_stop_button(self, monkeypatch):
        """Cleanup's stop is the UI Stop: the durable interrupt with background
        teardown, then the user-stop sweep. Monitor shells go too, whatever
        STOP_KEEPS_MONITOR_SHELLS says, because their cwd is being removed.
        """
        from app.core import query_session_lifecycle, turn_manager

        calls = []

        async def interrupt(sid, mode, stop_background=False, turn_id=None, **kw):
            calls.append(("interrupt", sid, mode, stop_background))

        async def stop_all(sid, **kw):
            calls.append(("stop_all", sid, kw))
            return []

        monkeypatch.setattr(turn_manager, "interrupt_async", interrupt)
        monkeypatch.setattr(query_session_lifecycle, "stop_all_background_tasks", stop_all)
        await gitops._stop_session_for_cleanup("s1")
        assert calls == [
            ("interrupt", "s1", "stop", True),
            ("stop_all", "s1", {"user_stop": True, "respect_shell_preference": False}),
        ]

    async def test_stop_waits_for_the_local_execution_to_settle(self, monkeypatch):
        """The run lease is released in the execution's finally block, so the
        busy re-check must come after the execution ends, not race it."""
        import asyncio
        from app.core import session_runtime

        interrupted = asyncio.Event()
        busy = {"s1"}

        async def execution():
            await interrupted.wait()
            await asyncio.sleep(0.05)
            busy.discard("s1")

        task = asyncio.create_task(execution())

        async def stop(sid):
            interrupted.set()

        monkeypatch.setattr(session_runtime, "active_executions", lambda: {task: "s1"})
        monkeypatch.setattr(gitops, "_stop_session_for_cleanup", stop)
        monkeypatch.setattr(gitops, "_session_is_busy", lambda sid: sid in busy)
        # A poll would sleep far past the test; only the execution wait can pass.
        monkeypatch.setattr(gitops, "_CLEANUP_STOP_POLL_SECONDS", 10)

        assert await gitops._stop_sessions_for_cleanup(["s1"]) == []
        assert task.done()

    async def test_cleanup_guard_project_mode_other_streaming_session(
        self, mock_git_service, mock_github_service, mock_worktree_manager
    ):
        """Project-mode cleanup (no worktree) rewrites the shared main checkout;
        it must refuse when another of the project's sessions is streaming.
        """
        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        op = _finish_op(project_id="p1", session_id="me")

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
        ), patch("app.core.task_registry.task_registry.has_active_background_work", return_value=False), \
             patch("app.db.database.get_sessions", mock_db.get_sessions), \
             patch.object(gitops, "_stop_session_for_cleanup", new_callable=AsyncMock) as stop:
            await _execute_finish(
                op, working_dir="/wd", worktree_id=None, repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="me",
                status={"ahead": 0},
            )

        assert op.status == "done"
        assert _step_status(op, "cleanup") == "warning"
        # The shared checkout's other sessions are unrelated work: never stopped.
        stop.assert_not_awaited()
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

    @pytest.mark.parametrize("activity", ["turn", "background", "idle"])
    @pytest.mark.parametrize("sibling_branch", ["feat/x", "feat/other", None])
    @pytest.mark.parametrize("legacy_project_id", ["p1", None, "legacy-alias"])
    async def test_linked_cleanup_scopes_busy_sessions_to_checkout(
        self, mock_git_service, mock_github_service, mock_worktree_manager,
        tmp_path, test_db, sibling_branch, activity, legacy_project_id,
    ):
        from app.core.config import settings
        from app.db import checkouts, projects_v2, sessions

        test_db.execute("PRAGMA foreign_keys = OFF")
        project = projects_v2.upsert_project("local:cleanup-scope", "Cleanup scope")
        main = tmp_path / "main"
        wt = main / ".fluid" / "worktrees" / "feat-x"
        other = main / ".fluid" / "worktrees" / "feat-other"
        # The resolver now verifies root + HEAD; exercise real worktree bindings
        # rather than a .git marker pointing at a nonexistent repository.
        from app.db.async_database import run_sync
        from tests.git_repo_helpers import git, init_repo

        await run_sync(init_repo, main)
        for branch, path in [("feat/x", wt), ("feat/other", other)]:
            await run_sync(git, main, "worktree", "add", str(path), "-b", branch)
        for branch, path in [(None, main), ("feat/x", wt), ("feat/other", other)]:
            checkouts.upsert_checkout(
                project["id"], settings.effective_instance_id, str(path), branch=branch
            )
        # Use the production creation and branch-binding writes, not a mocked
        # resolver or a pre-seeded cwd. Hidden workers must remain in the sweep.
        for sid, branch in [("me", "feat/x"), ("other", sibling_branch)]:
            sessions.create_session(
                sid, "profile", project_id=legacy_project_id,
                project_v2_id=project["id"], hidden=True, is_worker=True,
                derive_provider_from_profile=False,
            )
            sessions.set_session_branch(sid, branch, project["id"])

        mock_git_service.remove_worktree.return_value = True
        op = _finish_op(project_id="p1", session_id="me")
        # The caller is idle. Shared head/base activity must block BEFORE merge,
        # not interrupt the sibling later during destructive cleanup.
        busy = {"other"} if activity != "idle" else set()
        stopped = []

        async def stop(sid):
            stopped.append(sid)
            busy.discard(sid)

        mock_git_service.get_current_branch.side_effect = lambda path: "main" if path == str(main) else "feat/other" if path == str(other) else "feat/x"
        with patch.multiple(
            gitops, git_service=mock_git_service,
            github_service=mock_github_service, worktree_manager=mock_worktree_manager,
        ), patch("app.core.git_activity.git_service", mock_git_service), patch("app.core.git_activity._live_session_ids", side_effect=lambda: set(busy) if activity == "turn" else set()), patch(
            "app.core.streaming_status.has_active_run",
            side_effect=lambda sid: sid in busy and (sid == "me" or activity == "turn"),
        ), patch(
            "app.core.task_registry.task_registry.has_active_background_work",
            side_effect=lambda sid: sid in busy and activity == "background",
        ), patch.object(gitops, "_stop_session_for_cleanup", side_effect=stop), \
             patch.object(gitops, "_release_session_cwd", new_callable=AsyncMock):
            await _execute_finish(
                op, working_dir=str(wt), worktree_id=None, repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="me", status={"ahead": 0},
            )

        # Both the head and the merge base must be idle. Unrelated branches
        # remain usable and their sessions are never stopped by this operation.
        related_busy = sibling_branch in ("feat/x", None) and activity != "idle"
        if related_busy:
            assert op.status == "error"
            assert stopped == []
            mock_github_service.merge_pull.assert_not_called()
            mock_git_service.remove_worktree.assert_not_called()
            return
        assert stopped == []  # idle caller needs cwd release, never an interrupt
        assert op.status == "done"
        cleanup = next(s for s in op.steps if s["id"] == "cleanup")
        assert cleanup["status"] == "done"
        assert cleanup["detail"] == "Removed worktree"
        mock_git_service.remove_worktree.assert_called_once()
        mock_git_service.checkout.assert_not_called()

    @pytest.mark.parametrize("failure", ["query", "resolve", "missing"])
    async def test_linked_cleanup_does_not_delete_when_safety_lookup_fails(
        self, mock_git_service, failure, tmp_path,
    ):
        (tmp_path / ".git").write_text("gitdir: /repo/.git/worktrees/feature\n")
        op = _finish_op()
        with patch.object(gitops, "git_service", mock_git_service), patch(
            "app.db.checkouts.get_checkout_by_path", return_value=None,
        ), patch(
            "app.db.sessions.list_sessions_for_cleanup",
            side_effect=RuntimeError("lookup failed") if failure == "query" else None,
            return_value=[{"id": "other"}],
        ), patch.object(gitops, "_session_is_busy", return_value=True), patch(
            "app.core.project_resolver.resolve_existing_cwd",
            side_effect=RuntimeError("resolution failed") if failure == "resolve" else None,
            return_value=None,
        ):
            await gitops._do_cleanup(op, str(tmp_path), None, "feat/x", "main", "me")
        assert _step_status(op, "cleanup") == "warning"
        mock_git_service.remove_worktree.assert_not_called()
        mock_git_service.delete_branch.assert_not_called()

    async def test_linked_worktree_cleanup_stamps_head_sha(
        self, mock_git_service, mock_github_service, mock_worktree_manager, tmp_path
    ):
        """Cleanup records the worktree tip before deleting the branch so spawn
        can restore it and rematerialize the checkout."""
        main = tmp_path / "main"
        wt = main / ".fluid" / "worktrees" / "feat-x"
        wt.mkdir(parents=True)
        gitdir = main / ".git" / "worktrees" / "feat-x"
        (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

        mock_git_service.get_status.return_value = {"is_clean": True, "ahead": 0}
        mock_git_service.remove_worktree.return_value = True
        mock_git_service._run_git.return_value = SimpleNamespace(
            returncode=0, stdout="abc123def456\n"
        )
        op = _finish_op()
        stamp = MagicMock()
        with patch.multiple(
            gitops,
            git_service=mock_git_service,
            github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
        ), patch("app.db.checkouts.archive_checkout_by_path", return_value=None), patch(
            "app.db.checkouts.set_head_sha_for_path", stamp
        ):
            await _execute_finish(
                op, working_dir=str(wt), worktree_id=None, repo="o/r",
                pr_number=7, pr_url="u", feature_branch="feat/x", default_branch="main",
                merge_method="squash", cleanup=True, session_id="s1",
                status={"ahead": 0},
            )

        assert op.status == "done"
        assert _step_status(op, "cleanup") == "done"
        stamp.assert_called_once_with(str(wt), "abc123def456", project_id=op.project_id)

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
    @pytest.mark.parametrize("new_work", [False, True])
    async def test_real_remote_status_drives_push(self, tmp_path, mock_github_service, new_work):
        import subprocess
        from app.core.git_service import GitService

        remote = tmp_path / "remote.git"
        work = tmp_path / "work"
        work.mkdir()
        def git(*args):
            subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)

        git("init", "--initial-branch=main")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        git("commit", "--allow-empty", "-m", "Base")
        git("init", "--bare", str(remote))
        git("remote", "add", "origin", str(remote))
        git("push", "-u", "origin", "main")
        git("checkout", "-b", "feat/x")
        (work / "feature.txt").write_text("initial work")
        git("add", ".")
        git("commit", "-m", "Feature")
        git("push", "-u", "origin", "feat/x")
        if new_work:
            (work / "feature.txt").write_text("new work")
            git("commit", "-am", "Update feature")

        service = GitService()
        state = service.get_status(str(work))
        assert state["is_clean"] is True
        assert state["unpushed"] == int(new_work)
        # The branch differs from main in both cases; only remote-ahead matters.
        assert state["ahead"] > 0
        op = _ship_op()
        with patch.object(gitops, "git_service", service), \
             patch.object(gitops, "github_service", mock_github_service), \
             patch.object(service, "push", wraps=service.push) as push:
            await _execute_ship(
                op, working_dir=str(work), worktree_id=None, repo="o/r",
                feature_branch="feat/x", default_branch="main", base_branch="main",
                existing_pr=_pr(), dirty=not state["is_clean"],
                needs_push=state["unpushed"] != 0, include_unstaged=True,
                merge_method="squash", draft=False, cleanup=False, session_id=None,
            )
        assert op.status == "done"
        assert push.call_count == int(new_work)
        assert service.get_status(str(work))["unpushed"] == 0

    @pytest.mark.parametrize("dirty,needs_push", [(False, False), (False, True), (True, False)])
    async def test_push_only_when_needed(
        self, mock_git_service, mock_github_service, mock_worktree_manager, dirty, needs_push
    ):
        op = _ship_op()
        with patch.multiple(
            gitops, git_service=mock_git_service, github_service=mock_github_service,
            worktree_manager=mock_worktree_manager,
            generate_commit_message_for_dir=AsyncMock(return_value="Update feature"),
            stage_and_commit=AsyncMock(return_value=(True, "abc123", None)),
        ):
            await _execute_ship(
                op, working_dir="/wd", worktree_id=None, repo="o/r",
                feature_branch="feat/x", default_branch="main", base_branch="main",
                existing_pr=_pr(), dirty=dirty, needs_push=needs_push, include_unstaged=True,
                merge_method="squash", draft=False, cleanup=False, session_id=None,
            )
        assert op.status == "done"
        assert mock_git_service.push.call_count == int(dirty or needs_push)
        assert _step_status(op, "push") == ("done" if dirty or needs_push else "skipped")
        mock_github_service.merge_pull.assert_called_once_with("o/r", 7, "squash")
        mock_github_service.create_pull.assert_not_called()

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

    @pytest.mark.parametrize("generation_model", [None, "prov:strong:astra"])
    async def test_dirty_commits_and_creates_pr(
        self, mock_git_service, mock_github_service, mock_worktree_manager, generation_model
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
                generation_model=generation_model,
                merge_method="squash", draft=True, cleanup=False, session_id=None,
            )

        assert op.status == "done"
        assert _step_status(op, "commit") == "done"
        assert _step_status(op, "create_pr") == "done"
        override = {"model": generation_model} if generation_model else {}
        gen_msg.assert_awaited_once_with("/wd", True, **override)
        gen_pr.assert_awaited_once_with("/wd", "main", **override)
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
        stack.enter_context(patch("app.api.git_owner.database.get_session", side_effect=lambda sid: {"id": sid, "project_id": "p1"}))
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
        JSON body). It must reach get_working_dir_async so worktree ops run against the
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
    def test_request_binding_reaches_executor(self, op_client):
        with patch.object(gitops, "_execute_ship", new_callable=AsyncMock) as execute:
            resp = op_client.post("/api/v1/projects/p1/git/ship", json={"generation_model": "prov:strong:astra"})
        assert resp.status_code == 200
        assert execute.call_args.kwargs["generation_model"] == "prov:strong:astra"

    @pytest.mark.parametrize("unpushed,needs_push", [(0, False), (2, True), (None, True)])
    def test_preflight_passes_remote_ahead_state(self, op_client, mock_git_service, unpushed, needs_push):
        mock_git_service.get_status.return_value = {"is_clean": True, "unpushed": unpushed, "ahead": 5}
        with patch.object(gitops, "_execute_ship", new_callable=AsyncMock) as execute:
            resp = op_client.post("/api/v1/projects/p1/git/ship", json={"cleanup": False})
        assert resp.status_code == 200
        assert execute.call_args.kwargs["needs_push"] is needs_push

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

async def test_release_requesting_session_waits_for_execution_finally(monkeypatch):
    import asyncio
    from app.core import session_runtime, session_pool
    from app.mcp import builtin_tools
    from app.db import turns

    stopped = asyncio.Event()
    events = []
    async def execution():
        await stopped.wait()
        events.append("terminal persisted and lease released")
    task = asyncio.create_task(execution())
    async def interrupt(*args, **kwargs):
        events.append("interrupt")
        stopped.set()
    async def remove(sid):
        assert task.done()
        events.append("remove")
    runtime = SimpleNamespace(turn_id="requesting-turn", interrupt=interrupt)
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: runtime)
    monkeypatch.setattr(session_runtime, "active_executions", lambda: {task: "requesting"})
    monkeypatch.setattr(turns, "interrupt_turn", lambda *a, **kw: events.append("durable interrupt"))
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: SimpleNamespace(remove=remove))
    async def stop_shells(sid, *, require_stopped=False):
        assert require_stopped
        await asyncio.sleep(0)
        events.append("reap")
    monkeypatch.setattr(builtin_tools, "stop_session_background_shells", stop_shells)
    assert await gitops._release_session_cwd("requesting") is True
    assert events == ["durable interrupt", "interrupt", "terminal persisted and lease released", "remove", "reap"]


async def test_cleanup_retains_checkout_when_requesting_interrupt_fails(monkeypatch):
    from app.core import session_runtime, session_pool
    from app.db import turns
    runtime = SimpleNamespace(turn_id="t", interrupt=AsyncMock(side_effect=TimeoutError()))
    remove = AsyncMock()
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: runtime)
    monkeypatch.setattr(session_runtime, "active_executions", lambda: {})
    monkeypatch.setattr(turns, "interrupt_turn", lambda *a, **kw: None)
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: SimpleNamespace(remove=remove))
    assert await gitops._release_session_cwd("requesting") is False
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_checkout_closes_idle_siblings_and_runtime_cwds(monkeypatch, tmp_path):
    from app.core import project_resolver, session_pool
    from app.db import checkouts, sessions
    target = tmp_path / "checkout"
    target.mkdir()
    monkeypatch.setattr(checkouts, "get_checkout_by_path", lambda p: {"project_id": "v2"})
    monkeypatch.setattr(sessions, "list_sessions_for_cleanup", lambda *a: [
        {"id": "idle"}, {"id": "unrelated"},
    ])
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda s:
        str(target) if s["id"] == "idle" else str(tmp_path / "elsewhere"))
    def state(cwd):
        return SimpleNamespace(connect_options=SimpleNamespace(cwd=str(cwd)))
    pool = SimpleNamespace(items=lambda: [
        ("runtime-only", state(target / "nested")),
        ("other-runtime", state(tmp_path / "other")),
    ])
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: pool)
    monkeypatch.setattr(gitops, "_session_is_busy", lambda sid: False)
    release = AsyncMock(return_value=True)
    monkeypatch.setattr(gitops, "_release_session_cwd", release)
    assert await gitops._release_checkout_sessions("legacy", str(target), "requester")
    assert {c.args[0] for c in release.await_args_list} == {"idle", "runtime-only", "requester"}
    monkeypatch.setattr(gitops, "_session_is_busy", lambda sid: sid == "runtime-only")
    release.reset_mock()
    assert not await gitops._release_checkout_sessions("legacy", str(target), "requester")
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_session_closes_app_owned_browser_before_shells(monkeypatch):
    from app.core import browser_runtime, session_runtime, session_pool
    from app.mcp import builtin_tools
    events = []
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: None)
    monkeypatch.setattr(session_runtime, "active_executions", lambda: {})
    async def remove(sid):
        events.append("engine")
    async def close(sid):
        events.append("browser")
    async def shells(sid, **kwargs):
        events.append("shells")
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: SimpleNamespace(remove=remove))
    monkeypatch.setattr(browser_runtime, "close_session", close)
    monkeypatch.setattr(builtin_tools, "stop_session_background_shells", shells)
    assert await gitops._release_session_cwd("s")
    assert events == ["engine", "browser", "shells"]
    monkeypatch.setattr(browser_runtime, "close_session", AsyncMock(side_effect=TimeoutError()))
    assert not await gitops._release_session_cwd("s")

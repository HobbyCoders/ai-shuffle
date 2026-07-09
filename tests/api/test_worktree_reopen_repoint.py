"""
Tests for the manual worktree ``reopen`` endpoint's session repointing
(PR #493 follow-up #2).

A cross-device reopen rebinds the worktree onto THIS install's clone of the repo
(recreate_worktree flips ``repository_id`` to the local repo). Because a manual
reopen carries no ``rebind_session_id``, recreate_worktree re-points NO session —
and afterward the send-path self-heal can never fire (the cwd now exists locally,
so every rebind gate is skipped). So the endpoint must broadly repoint every
session on the worktree to the local project, or their ``project_id`` stays
foreign forever and MCP servers resolve the wrong project.

A same-device reopen (no rebind) resolves to the sessions' existing project, so
NO repoint call should fire.

The endpoint coroutine is driven directly with the collaborators patched at the
git_worktrees module boundary; no real DB, git, or pool is needed.
"""

import asyncio
from unittest.mock import MagicMock, patch

import app.api.git_worktrees as gw


def _run(coro):
    return asyncio.run(coro)


def _call_reopen():
    return _run(
        gw.reopen_worktree(
            request=MagicMock(name="request"),
            project_id="proj-local",
            worktree_id="wt-1",
            token="tok",
        )
    )


def _pool_no_sessions_live():
    pool = MagicMock(name="pool")
    pool.get.return_value = None  # nothing pooled → no eviction path taken
    return pool


def test_reopen_cross_device_repoints_all_sessions():
    """Rebound reopen → both sessions moved onto the local project."""
    updated = {"id": "wt-1", "repository_id": "repo-local", "branch_name": "feat/x"}
    sessions = [
        {"id": "ses-1", "project_id": "proj-foreign"},
        {"id": "ses-2", "project_id": "proj-foreign"},
    ]
    db = MagicMock(name="database")
    db.get_git_repository.return_value = {"id": "repo-local", "project_id": "proj-local"}
    db.get_sessions_for_worktree.return_value = sessions

    wm = MagicMock(name="worktree_manager")
    wm.recreate_worktree.return_value = updated

    with patch.object(gw, "check_project_access", lambda *a, **k: None), patch.object(
        gw, "_require_worktree_for_project", return_value={"id": "wt-1", "branch_name": "feat/x"}
    ), patch.object(gw, "worktree_manager", wm), patch.object(
        gw, "database", db
    ), patch.object(
        gw, "_convert_worktree_to_info", side_effect=lambda wt: wt
    ), patch(
        "app.core.session_pool.get_session_pool", return_value=_pool_no_sessions_live()
    ):
        result = _call_reopen()

    assert result is updated
    assert db.set_session_project.call_count == 2
    db.set_session_project.assert_any_call("ses-1", "proj-local")
    db.set_session_project.assert_any_call("ses-2", "proj-local")


def test_reopen_same_device_does_not_repoint():
    """Same-device reopen (no rebind): sessions already on the local project → no
    repoint call fires."""
    updated = {"id": "wt-1", "repository_id": "repo-local", "branch_name": "feat/x"}
    sessions = [
        {"id": "ses-1", "project_id": "proj-local"},
        {"id": "ses-2", "project_id": "proj-local"},
    ]
    db = MagicMock(name="database")
    db.get_git_repository.return_value = {"id": "repo-local", "project_id": "proj-local"}
    db.get_sessions_for_worktree.return_value = sessions

    wm = MagicMock(name="worktree_manager")
    wm.recreate_worktree.return_value = updated

    with patch.object(gw, "check_project_access", lambda *a, **k: None), patch.object(
        gw, "_require_worktree_for_project", return_value={"id": "wt-1", "branch_name": "feat/x"}
    ), patch.object(gw, "worktree_manager", wm), patch.object(
        gw, "database", db
    ), patch.object(
        gw, "_convert_worktree_to_info", side_effect=lambda wt: wt
    ), patch(
        "app.core.session_pool.get_session_pool", return_value=_pool_no_sessions_live()
    ):
        _call_reopen()

    db.set_session_project.assert_not_called()

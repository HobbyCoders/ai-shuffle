"""
Tests for worktree working-directory resolution in build_options.

Covers ``_resolve_session_worktree_cwd`` — the helper that guarantees a
worktree-flagged session NEVER silently executes in the project's main
checkout. When the linked worktree is missing/inactive it must auto-recreate
it, and only raise ``WorktreeUnavailableError`` if that fails.

All DB / worktree-manager access is patched at the boundary used by
query_options.py so no real Postgres or git is needed.
"""

from dataclasses import dataclass

import pytest
from unittest.mock import MagicMock

import app.core.query_options as qo
from app.core.query_options import (
    _resolve_session_worktree_cwd,
    WorktreeUnavailableError,
)

SESSION_ID = "ses-abc123"


def _wt(status="active", wt_id="wt-1", branch="feat/foo", base="main"):
    return {
        "id": wt_id,
        "status": status,
        "branch_name": branch,
        "base_branch": base,
        "worktree_path": f".worktrees/{branch}",
    }


@dataclass
class Mocks:
    wm: MagicMock
    get_worktree_by_session: MagicMock
    get_session: MagicMock


@pytest.fixture
def mocks(monkeypatch) -> Mocks:
    """Patch the DB + worktree_manager boundary and the path helper."""
    wm = MagicMock(name="worktree_manager")
    get_wt = MagicMock(name="get_worktree_by_session", return_value=None)
    get_sess = MagicMock(name="get_session", return_value=None)

    # The lazy import inside the helper resolves to this singleton.
    monkeypatch.setattr("app.core.worktree_manager.worktree_manager", wm)
    # Path helper lives in the query_options namespace.
    monkeypatch.setattr(qo, "get_worktree_working_dir", lambda wt: f"/abs/{wt['worktree_path']}")
    monkeypatch.setattr(qo.database, "get_worktree_by_session", get_wt)
    monkeypatch.setattr(qo.database, "get_session", get_sess)
    return Mocks(wm=wm, get_worktree_by_session=get_wt, get_session=get_sess)


# ---------------------------------------------------------------------------
# Happy path: active + valid worktree
# ---------------------------------------------------------------------------

def test_active_valid_worktree_returns_worktree_cwd(mocks):
    mocks.get_worktree_by_session.return_value = _wt(status="active")
    mocks.wm.is_worktree_valid.return_value = True

    working_dir, mode, info = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert working_dir == "/abs/.worktrees/feat/foo"
    assert mode == "worktree"
    assert info == {"branch": "feat/foo", "base_branch": "main"}
    # An active, valid worktree must NOT be recreated.
    mocks.wm.recreate_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# Inactive / invalid worktree → auto-recreate
# ---------------------------------------------------------------------------

def test_inactive_worktree_is_auto_recreated(mocks):
    mocks.get_worktree_by_session.return_value = _wt(status="completed", wt_id="wt-1")
    # Original is not valid (completed) → triggers recreate; recreated is active.
    mocks.wm.is_worktree_valid.side_effect = lambda wt: wt.get("status") == "active"
    mocks.wm.recreate_worktree.return_value = _wt(status="active", wt_id="wt-1")

    working_dir, mode, _ = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    mocks.wm.recreate_worktree.assert_called_once_with("wt-1")
    assert mode == "worktree"
    assert working_dir == "/abs/.worktrees/feat/foo"


def test_active_but_invalid_on_disk_is_recreated(mocks):
    """status active but the folder is gone → recreate, never fall back."""
    mocks.get_worktree_by_session.return_value = _wt(status="active", wt_id="wt-9")
    # First validity check (original) fails; recreated one passes.
    mocks.wm.is_worktree_valid.side_effect = [False, True]
    mocks.wm.recreate_worktree.return_value = _wt(status="active", wt_id="wt-9")

    working_dir, mode, _ = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    mocks.wm.recreate_worktree.assert_called_once_with("wt-9")
    assert mode == "worktree"
    assert working_dir and working_dir.startswith("/abs/")


# ---------------------------------------------------------------------------
# Recreate fails → raise, NEVER main
# ---------------------------------------------------------------------------

def test_recreate_raises_surfaces_worktree_unavailable(mocks):
    mocks.get_worktree_by_session.return_value = _wt(status="deleted", wt_id="wt-2", branch="fix/bar")
    mocks.wm.is_worktree_valid.return_value = False
    mocks.wm.recreate_worktree.side_effect = RuntimeError("git boom")

    with pytest.raises(WorktreeUnavailableError) as ei:
        _resolve_session_worktree_cwd(SESSION_ID, project={"path": "/repo"})

    assert ei.value.branch_name == "fix/bar"
    assert "git boom" in str(ei.value)


def test_recreate_returns_non_active_raises(mocks):
    mocks.get_worktree_by_session.return_value = _wt(status="completed", wt_id="wt-3", branch="fix/baz")
    mocks.wm.is_worktree_valid.return_value = False
    # Recreate "succeeds" but yields a still-broken worktree.
    mocks.wm.recreate_worktree.return_value = _wt(status="deleted", wt_id="wt-3")

    with pytest.raises(WorktreeUnavailableError):
        _resolve_session_worktree_cwd(SESSION_ID, project={"path": "/repo"})


# ---------------------------------------------------------------------------
# No worktree row
# ---------------------------------------------------------------------------

def test_worktree_flagged_session_without_row_raises(mocks):
    """worktree_id was NULLed but the session still records a worktree branch."""
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": "feat/orphaned",
        "worktree_path": ".worktrees/feat/orphaned",
    }

    with pytest.raises(WorktreeUnavailableError) as ei:
        _resolve_session_worktree_cwd(SESSION_ID, project={"path": "/repo"})

    assert ei.value.branch_name == "feat/orphaned"


def test_local_session_returns_none(mocks):
    """A genuinely non-worktree session resolves to (None, None, None)."""
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": None,
        "worktree_path": None,
    }

    result = _resolve_session_worktree_cwd(SESSION_ID, project={"path": "/repo"})

    assert result == (None, None, None)


# ---------------------------------------------------------------------------
# No linked row but branch + project present → CREATE + link a fresh worktree
# (the never-a-dead-end path). These exercise the create-from-branch fallback.
# ---------------------------------------------------------------------------

def _patch_create_path(mocks, monkeypatch, *, branches, create_returns):
    """Wire up the DB/manager boundary used by the create+link fallback."""
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": "feat/recreate",
        "worktree_path": ".worktrees/feat/recreate",
        "project_id": "proj-1",
    }
    monkeypatch.setattr(
        qo.database, "get_git_repository_by_project",
        MagicMock(return_value={"default_branch": "main"}),
    )
    monkeypatch.setattr(
        qo.database, "get_project",
        MagicMock(return_value={"id": "proj-1", "path": "/repo"}),
    )
    update_session = MagicMock(name="update_session")
    monkeypatch.setattr(qo.database, "update_session", update_session)
    monkeypatch.setattr(qo, "get_project_working_dir", lambda p: "/repo")
    mocks.wm.git_service.list_branches.return_value = branches
    mocks.wm.create_worktree.return_value = create_returns
    mocks.wm.is_worktree_valid.return_value = True
    return update_session


def test_no_row_with_existing_branch_creates_and_links(mocks, monkeypatch):
    """worktree_id NULLed but branch still exists locally → reuse it, link the new row."""
    new_wt = _wt(status="active", wt_id="wt-new", branch="feat/recreate")
    update_session = _patch_create_path(
        mocks,
        monkeypatch,
        branches=[{"name": "feat/recreate", "is_remote": False}],
        create_returns=new_wt,
    )

    working_dir, mode, info = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert mode == "worktree"
    assert working_dir == "/abs/.worktrees/feat/recreate"
    assert info == {"branch": "feat/recreate", "base_branch": "main"}
    # Reuse existing branch: create_new_branch False, no base_branch.
    mocks.wm.create_worktree.assert_called_once_with(
        "proj-1", "feat/recreate", create_new_branch=False, base_branch=None
    )
    update_session.assert_called_once_with(SESSION_ID, worktree_id="wt-new")


def test_no_row_missing_branch_creates_new_off_default(mocks, monkeypatch):
    """Branch is gone (merged + pruned) → create a fresh branch off the default."""
    new_wt = _wt(status="active", wt_id="wt-new2", branch="feat/recreate")
    _patch_create_path(
        mocks,
        monkeypatch,
        branches=[{"name": "main", "is_remote": False}],
        create_returns=new_wt,
    )

    working_dir, mode, _ = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert mode == "worktree"
    assert working_dir and working_dir.startswith("/abs/")
    # Branch missing → create_new_branch True, based on the default branch.
    mocks.wm.create_worktree.assert_called_once_with(
        "proj-1", "feat/recreate", create_new_branch=True, base_branch="main"
    )


def test_no_row_create_failure_raises_unavailable(mocks, monkeypatch):
    """If git can't create the worktree, surface WorktreeUnavailableError — never main."""
    _patch_create_path(
        mocks,
        monkeypatch,
        branches=[{"name": "feat/recreate", "is_remote": False}],
        create_returns=None,
    )
    mocks.wm.create_worktree.side_effect = RuntimeError("git add-worktree boom")

    with pytest.raises(WorktreeUnavailableError) as ei:
        _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert ei.value.branch_name == "feat/recreate"
    assert "git add-worktree boom" in str(ei.value)


def test_no_row_non_git_project_raises(mocks, monkeypatch):
    """A worktree-flagged session in a non-git project can't recreate → raises."""
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": "feat/recreate",
        "worktree_path": ".worktrees/feat/recreate",
        "project_id": "proj-1",
    }
    monkeypatch.setattr(
        qo.database, "get_git_repository_by_project", MagicMock(return_value=None)
    )
    monkeypatch.setattr(
        qo.database, "get_project",
        MagicMock(return_value={"id": "proj-1", "path": "/repo"}),
    )

    with pytest.raises(WorktreeUnavailableError):
        _resolve_session_worktree_cwd(SESSION_ID, project=None)

    mocks.wm.create_worktree.assert_not_called()

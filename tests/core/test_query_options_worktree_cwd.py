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

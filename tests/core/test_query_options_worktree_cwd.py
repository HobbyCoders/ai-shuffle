"""
Tests for worktree working-directory resolution in build_options.

Covers ``_resolve_session_worktree_cwd`` — the helper that resolves the cwd for a
session that may be a worktree session.

New contract (resume like a local session): a worktree session only needs its
initial working directory to still EXIST on disk. Status, ``.git`` presence, and
any recreate/create machinery are NOT consulted on the resume path:

  - dir exists  → use it as cwd regardless of the row's status or ``.git``;
                  self-heal a stale (non-active) status back to 'active'.
  - dir missing → raise ``WorktreeUnavailableError`` (never recreate, never fall
                  back to the main checkout).

All DB / worktree-manager / filesystem access is patched at the boundary used by
query_options.py so no real Postgres, git, or disk is needed.
"""

from dataclasses import dataclass

import pytest
from unittest.mock import MagicMock

import app.core.project_utils as pu
import app.core.query_options as qo
from app.core.query_options import (
    _resolve_session_worktree_cwd,
    WorktreeUnavailableError,
)
from tests.fake_path import FakePath

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
    update_worktree: MagicMock


@pytest.fixture
def mocks(monkeypatch) -> Mocks:
    """Patch the DB + worktree_manager boundary, the path helper, and Path."""
    wm = MagicMock(name="worktree_manager")
    get_wt = MagicMock(name="get_worktree_by_session", return_value=None)
    get_sess = MagicMock(name="get_session", return_value=None)
    update_wt = MagicMock(name="update_worktree")

    # worktree_manager is no longer imported by the resume path — patch it anyway
    # so the "recreate/create NEVER called" assertions are meaningful guards.
    monkeypatch.setattr("app.core.worktree_manager.worktree_manager", wm, raising=False)
    # The cwd rule lives in project_utils (resolve_existing_worktree_cwd uses the
    # project_utils bindings of Path/get_worktree_working_dir); query_options
    # keeps its OWN from-import of get_worktree_working_dir for error messages.
    # Patch both namespaces so the fake filesystem governs everything.
    _resolver = lambda wt: f"/abs/{wt['worktree_path']}"  # noqa: E731
    monkeypatch.setattr(pu, "get_worktree_working_dir", _resolver)
    monkeypatch.setattr(pu, "Path", FakePath)
    monkeypatch.setattr(qo, "get_worktree_working_dir", _resolver)
    monkeypatch.setattr(qo, "Path", FakePath)
    monkeypatch.setattr(qo.database, "get_worktree_by_session", get_wt)
    monkeypatch.setattr(qo.database, "get_session", get_sess)
    monkeypatch.setattr(qo.database, "update_worktree", update_wt)

    FakePath.existing = set()
    yield Mocks(
        wm=wm,
        get_worktree_by_session=get_wt,
        get_session=get_sess,
        update_worktree=update_wt,
    )
    FakePath.existing = set()


# ---------------------------------------------------------------------------
# (a) row active + dir exists → cwd returned, no recreate
# ---------------------------------------------------------------------------

def test_active_row_dir_exists_returns_cwd_no_recreate(mocks):
    mocks.get_worktree_by_session.return_value = _wt(status="active")
    FakePath.existing = {"/abs/.worktrees/feat/foo"}

    working_dir, mode, info = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert working_dir == "/abs/.worktrees/feat/foo"
    assert mode == "worktree"
    assert info == {"branch": "feat/foo", "base_branch": "main"}
    mocks.wm.recreate_worktree.assert_not_called()
    # An already-active row needs no self-heal.
    mocks.update_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# (b) row status deleted/orphaned/completed + dir exists → cwd + self-heal,
#     NEVER recreate. This is the regression test for the false-recreate bug.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stale_status", ["deleted", "orphaned", "completed"])
def test_stale_status_with_dir_self_heals_and_never_recreates(mocks, stale_status):
    mocks.get_worktree_by_session.return_value = _wt(status=stale_status, wt_id="wt-7")
    FakePath.existing = {"/abs/.worktrees/feat/foo"}

    working_dir, mode, info = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert working_dir == "/abs/.worktrees/feat/foo"
    assert mode == "worktree"
    # Regression: the old code called recreate_worktree here. It must NOT now.
    mocks.wm.recreate_worktree.assert_not_called()
    # Self-heal: the stale status is corrected back to 'active' on disk-present.
    mocks.update_worktree.assert_called_once_with("wt-7", status="active")


# ---------------------------------------------------------------------------
# (c) row status active but dir missing → raise, no recreate, honest message
# ---------------------------------------------------------------------------

def test_active_row_dir_missing_raises_no_recreate(mocks):
    mocks.get_worktree_by_session.return_value = _wt(status="active", branch="fix/bar")
    FakePath.existing = set()  # directory is gone

    with pytest.raises(WorktreeUnavailableError) as ei:
        _resolve_session_worktree_cwd(SESSION_ID, project={"path": "/repo"})

    msg = str(ei.value)
    assert ei.value.branch_name == "fix/bar"
    # Honest message names the missing path and does NOT mention recreation.
    assert "/abs/.worktrees/fix/bar" in msg
    assert "no longer exists" in msg
    assert "could not be recreated" not in msg
    mocks.wm.recreate_worktree.assert_not_called()
    mocks.update_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# (d) dir exists but .git missing → still returns cwd (no is_worktree_valid gate)
# ---------------------------------------------------------------------------

def test_dir_exists_but_no_git_still_returns_cwd(mocks):
    """No validity gate: a present directory is enough even without a .git file."""
    mocks.get_worktree_by_session.return_value = _wt(status="active")
    FakePath.existing = {"/abs/.worktrees/feat/foo"}
    # Force any accidental validity check to report invalid — must be irrelevant.
    mocks.wm.is_worktree_valid.return_value = False

    working_dir, mode, _ = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert working_dir == "/abs/.worktrees/feat/foo"
    assert mode == "worktree"
    # The resume path must not consult is_worktree_valid at all.
    mocks.wm.is_worktree_valid.assert_not_called()
    mocks.wm.recreate_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# (e) no row + backup columns + dir exists → cwd from backup, no create
# ---------------------------------------------------------------------------

def test_no_row_backup_dir_exists_returns_backup_cwd(mocks):
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": "feat/orphaned",
        "worktree_path": ".worktrees/feat/orphaned",
    }
    FakePath.existing = {"/abs/.worktrees/feat/orphaned"}

    working_dir, mode, info = _resolve_session_worktree_cwd(SESSION_ID, project={"path": "/repo"})

    assert working_dir == "/abs/.worktrees/feat/orphaned"
    assert mode == "worktree"
    assert info == {"branch": "feat/orphaned", "base_branch": "main"}
    mocks.wm.create_worktree.assert_not_called()
    mocks.wm.recreate_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# (f) no row + backup columns + dir missing → raise, no create
# ---------------------------------------------------------------------------

def test_no_row_backup_dir_missing_raises_no_create(mocks):
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": "feat/gone",
        "worktree_path": ".worktrees/feat/gone",
    }
    FakePath.existing = set()

    with pytest.raises(WorktreeUnavailableError) as ei:
        _resolve_session_worktree_cwd(SESSION_ID, project={"path": "/repo"})

    msg = str(ei.value)
    assert ei.value.branch_name == "feat/gone"
    assert "/abs/.worktrees/feat/gone" in msg
    assert "could not be recreated" not in msg
    mocks.wm.create_worktree.assert_not_called()
    mocks.wm.recreate_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# (g) no row + no backup flags → (None, None, None)
# ---------------------------------------------------------------------------

def test_local_session_returns_none(mocks):
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": None,
        "worktree_path": None,
    }

    result = _resolve_session_worktree_cwd(SESSION_ID, project={"path": "/repo"})

    assert result == (None, None, None)
    mocks.wm.create_worktree.assert_not_called()
    mocks.wm.recreate_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# (h) self-heal update_worktree raising → resume still succeeds
# ---------------------------------------------------------------------------

def test_self_heal_failure_does_not_block_resume(mocks):
    mocks.get_worktree_by_session.return_value = _wt(status="deleted", wt_id="wt-9")
    FakePath.existing = {"/abs/.worktrees/feat/foo"}
    mocks.update_worktree.side_effect = RuntimeError("db write boom")

    working_dir, mode, _ = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    # The heal was attempted and failed, but the resume still returns the cwd.
    mocks.update_worktree.assert_called_once_with("wt-9", status="active")
    assert working_dir == "/abs/.worktrees/feat/foo"
    assert mode == "worktree"
    mocks.wm.recreate_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# (i) row path missing on disk but the session's BACKUP worktree_path exists →
#     resume on the backup. adopt/recreate rewrite the ROW path while the
#     session column is a write-once snapshot, so they can genuinely diverge.
# ---------------------------------------------------------------------------

def test_row_path_stale_but_session_backup_valid_resumes_on_backup(mocks):
    row = _wt(status="active", wt_id="wt-3", branch="feat/foo")
    row["worktree_path"] = ".worktrees/feat/foo-recreated"  # rewritten, gone on disk
    mocks.get_worktree_by_session.return_value = row
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": "wt-3",
        "worktree_branch": "feat/foo",
        "worktree_path": ".worktrees/feat/foo",  # original snapshot, still on disk
    }
    FakePath.existing = {"/abs/.worktrees/feat/foo"}

    working_dir, mode, info = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert working_dir == "/abs/.worktrees/feat/foo"
    assert mode == "worktree"
    assert info == {"branch": "feat/foo", "base_branch": "main"}
    mocks.wm.recreate_worktree.assert_not_called()

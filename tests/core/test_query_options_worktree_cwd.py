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
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# (j) cross-device rebind — a WORKTREE ROW exists but its cwd is gone because the
#     row points at another (offline) machine's checkout. When THIS install has a
#     clone of the same repo (link group), recreate the worktree there and resume.
#     This is the ONE place a worktree is (re)created on the resume path, gated on
#     the cwd being GENUINELY missing AND a local sibling clone existing.
# ---------------------------------------------------------------------------

def test_row_dir_missing_rebinds_via_recreate(mocks):
    """Row cwd gone + local clone of the repo present → recreate_worktree runs in
    that clone and its cwd is used (the user's actual cross-device bug)."""
    mocks.get_worktree_by_session.return_value = _wt(
        status="active", wt_id="wt-x", branch="feat/foo"
    )
    mocks.get_session.return_value = {"id": SESSION_ID, "project_id": "proj-foreign"}
    FakePath.existing = set()  # row + backup dirs both gone here

    new_wt = _wt(status="active", wt_id="wt-x", branch="feat/foo")
    new_wt["worktree_path"] = ".worktrees/feat/foo-local"
    mocks.wm.recreate_worktree.return_value = new_wt
    mocks.wm.is_worktree_valid.return_value = True

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, {"id": "proj-local", "path": "/local/clone"}),
    ):
        working_dir, mode, info = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert mode == "worktree"
    assert working_dir == "/abs/.worktrees/feat/foo-local"
    # recreate_worktree owns the rebind (sibling, repo id) and is told which single
    # session to re-point — never all sessions on the worktree (follow-up #2).
    mocks.wm.recreate_worktree.assert_called_once_with("wt-x", rebind_session_id=SESSION_ID)
    assert info["branch"] == "feat/foo"
    # follow-up #3: the local project the session was rebound onto is surfaced so
    # build_options can refresh its in-memory project for THIS send (MCP servers).
    assert info["rebound_project"] == {"id": "proj-local", "path": "/local/clone"}


def test_row_dir_missing_no_sibling_raises(mocks):
    """Row cwd gone + NO local clone → honest WorktreeUnavailableError naming the
    path; recreate is never attempted."""
    mocks.get_worktree_by_session.return_value = _wt(
        status="active", wt_id="wt-x", branch="fix/bar"
    )
    mocks.get_session.return_value = {"id": SESSION_ID, "project_id": "proj-foreign"}
    FakePath.existing = set()

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, None),
    ):
        with pytest.raises(WorktreeUnavailableError) as ei:
            _resolve_session_worktree_cwd(SESSION_ID, project={"path": "/repo"})

    assert ei.value.branch_name == "fix/bar"
    assert "/abs/.worktrees/fix/bar" in str(ei.value)
    mocks.wm.recreate_worktree.assert_not_called()


def test_row_dir_missing_same_device_raises_no_recreate(mocks):
    """Row cwd gone but the runnable local project is the session's OWN project
    (same id → same device): this is NOT a cross-device relocation, so we must
    raise the honest error and NEVER recreate — else a worktree the user simply
    removed gets silently resurrected (and a branch surviving only on origin
    could be fabricated empty off the default). Regression guard for the gate
    that requires a DIFFERENT clone."""
    mocks.get_worktree_by_session.return_value = _wt(
        status="active", wt_id="wt-x", branch="feat/foo"
    )
    mocks.get_session.return_value = {"id": SESSION_ID, "project_id": "proj-owned"}
    FakePath.existing = set()  # worktree folder was removed on this device

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        # local id == the session's project_id → own project, same device.
        return_value=({"id": "proj-owned"}, {"id": "proj-owned", "path": "/repo"}),
    ):
        with pytest.raises(WorktreeUnavailableError) as ei:
            _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert ei.value.branch_name == "feat/foo"
    assert "/abs/.worktrees/feat/foo" in str(ei.value)
    mocks.wm.recreate_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# (k) cross-device rebind — NO worktree row (row hard-deleted; only the session's
#     worktree_* columns remain). Create a fresh worktree in the local sibling
#     clone from the branch, reusing/recovering it rather than fabricating one.
# ---------------------------------------------------------------------------

def test_no_row_foreign_rebinds_creates_in_sibling(mocks, monkeypatch):
    """No row + project checkout missing here → rebind onto this install's clone,
    create the worktree from the local branch, and re-point the session."""
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": "feat/recreate",
        "worktree_path": ".worktrees/feat/recreate",
        "project_id": "proj-foreign",
    }
    FakePath.existing = set()  # backup dir gone → falls through to rebind

    set_project = MagicMock(name="set_session_project")
    update_session = MagicMock(name="update_session")
    monkeypatch.setattr(qo.database, "set_session_project", set_project)
    monkeypatch.setattr(qo.database, "update_session", update_session)
    monkeypatch.setattr(qo, "get_project_working_dir", lambda p: p["path"])

    new_wt = _wt(status="active", wt_id="wt-new", branch="feat/recreate")
    mocks.wm.git_service.list_branches.return_value = [
        {"name": "feat/recreate", "is_remote": False}
    ]
    mocks.wm.create_worktree.return_value = new_wt
    mocks.wm.is_worktree_valid.return_value = True

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, {"id": "proj-local", "path": "/local/clone"}),
    ):
        working_dir, mode, _ = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert mode == "worktree"
    assert working_dir == "/abs/.worktrees/feat/recreate"
    # Reuse the local branch: no new branch, no base — created under the clone.
    mocks.wm.create_worktree.assert_called_once_with(
        "proj-local", "feat/recreate", create_new_branch=False, base_branch=None
    )
    set_project.assert_called_once_with(SESSION_ID, "proj-local")
    update_session.assert_called_once_with(SESSION_ID, worktree_id="wt-new")


def test_no_row_foreign_no_sibling_raises(mocks, monkeypatch):
    """No row + project missing here + no local clone → WorktreeUnavailableError,
    no create."""
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": "feat/recreate",
        "worktree_path": ".worktrees/feat/recreate",
        "project_id": "proj-foreign",
    }
    FakePath.existing = set()
    monkeypatch.setattr(qo, "get_project_working_dir", lambda p: p["path"])

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, None),
    ):
        with pytest.raises(WorktreeUnavailableError) as ei:
            _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert ei.value.branch_name == "feat/recreate"
    assert "/abs/.worktrees/feat/recreate" in str(ei.value)
    mocks.wm.create_worktree.assert_not_called()


def test_no_row_rebound_branch_recovered_from_origin(mocks, monkeypatch):
    """No row + rebind + branch missing locally but pushed to origin → create from
    origin/<branch>, NOT a fresh branch off the default (which would silently
    present an empty branch as the user's work)."""
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": "feat/recreate",
        "worktree_path": ".worktrees/feat/recreate",
        "project_id": "proj-foreign",
    }
    FakePath.existing = set()
    monkeypatch.setattr(qo.database, "set_session_project", MagicMock())
    monkeypatch.setattr(qo.database, "update_session", MagicMock())
    monkeypatch.setattr(qo, "get_project_working_dir", lambda p: p["path"])

    new_wt = _wt(status="active", wt_id="wt-new", branch="feat/recreate")

    def _branches(_dir, include_remote=False):
        if include_remote:
            return [
                {"name": "main", "is_remote": False},
                {"name": "origin/main", "is_remote": True},
                {"name": "origin/feat/recreate", "is_remote": True},
            ]
        return [{"name": "main", "is_remote": False}]

    mocks.wm.git_service.list_branches.side_effect = _branches
    mocks.wm.create_worktree.return_value = new_wt
    mocks.wm.is_worktree_valid.return_value = True

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, {"id": "proj-local", "path": "/local/clone"}),
    ):
        working_dir, mode, _ = _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert mode == "worktree"
    # We must fetch before concluding origin has the branch.
    mocks.wm.git_service.fetch.assert_called_once()
    mocks.wm.create_worktree.assert_called_once_with(
        "proj-local", "feat/recreate", create_new_branch=True, base_branch="feat/recreate"
    )


def test_no_row_rebound_branch_missing_everywhere_raises(mocks, monkeypatch):
    """No row + rebind + branch missing locally AND on origin →
    WorktreeUnavailableError carrying the loud "push it from origin" guidance,
    and NO create off the default branch."""
    mocks.get_worktree_by_session.return_value = None
    mocks.get_session.return_value = {
        "id": SESSION_ID,
        "worktree_id": None,
        "worktree_branch": "feat/recreate",
        "worktree_path": ".worktrees/feat/recreate",
        "project_id": "proj-foreign",
    }
    FakePath.existing = set()
    monkeypatch.setattr(qo.database, "set_session_project", MagicMock())
    monkeypatch.setattr(qo, "get_project_working_dir", lambda p: p["path"])

    def _branches(_dir, include_remote=False):
        # Neither local nor origin carries the branch.
        if include_remote:
            return [
                {"name": "main", "is_remote": False},
                {"name": "origin/main", "is_remote": True},
            ]
        return [{"name": "main", "is_remote": False}]

    mocks.wm.git_service.list_branches.side_effect = _branches

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, {"id": "proj-local", "path": "/local/clone"}),
    ):
        with pytest.raises(WorktreeUnavailableError) as ei:
            _resolve_session_worktree_cwd(SESSION_ID, project=None)

    assert ei.value.branch_name == "feat/recreate"
    assert "origin" in str(ei.value)
    mocks.wm.create_worktree.assert_not_called()
    mocks.wm.recreate_worktree.assert_not_called()

"""
Regression tests for issue #568: importing an external detached-HEAD worktree.

Root cause: git's porcelain worktree listing carries no ``branch`` line for a
detached worktree, and list_worktrees() fills the key with None (setdefault) —
so ``git_wt.get("branch", "unknown")`` at the sync-import site still yielded
None and the insert violated the ``branch_name TEXT NOT NULL`` schema
constraint. The per-item try/except then silently skipped the worktree on every
sync. The fix derives a ``detached-<short-sha>`` sentinel from the HEAD SHA.

Git/DB access is patched at the same boundaries as test_worktree_manager_adopt.
"""

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock

import app.core.worktree_manager as wm_module
from app.core.git_worktree_ops import GitWorktreeMixin
from app.core.worktree_manager import WorktreeManager, _external_worktree_branch_name
from app.db.base import WorktreeStatus


DETACHED_SHA = "abc123def4567890abc123def4567890abc123de"


# ---------------------------------------------------------------------------
# The sentinel helper
# ---------------------------------------------------------------------------

class TestExternalWorktreeBranchName:
    def test_real_branch_passes_through(self):
        assert _external_worktree_branch_name({"branch": "feat/x", "head": DETACHED_SHA}) == "feat/x"

    def test_detached_none_branch_derives_sentinel_from_head(self):
        wt = {"branch": None, "head": DETACHED_SHA, "is_detached": True}
        assert _external_worktree_branch_name(wt) == f"detached-{DETACHED_SHA[:12]}"

    def test_missing_branch_key_derives_sentinel(self):
        assert _external_worktree_branch_name({"head": DETACHED_SHA}) == f"detached-{DETACHED_SHA[:12]}"

    def test_no_branch_and_no_head_falls_back_to_unknown(self):
        assert _external_worktree_branch_name({"branch": None}) == "unknown"
        assert _external_worktree_branch_name({"branch": None, "head": "  "}) == "unknown"


# ---------------------------------------------------------------------------
# Porcelain parser contract the helper relies on: a detached worktree comes
# back with branch=None (filled key, NOT missing) and its HEAD SHA.
# ---------------------------------------------------------------------------

class _FakeGitService(GitWorktreeMixin):
    def __init__(self, stdout: str):
        self._stdout = stdout

    def _run_git(self, working_dir, args, timeout=10):
        return SimpleNamespace(returncode=0, stdout=self._stdout, stderr="")


def test_list_worktrees_detached_has_none_branch_and_head(tmp_path):
    porcelain = (
        f"worktree {tmp_path}\n"
        f"HEAD 1111111111111111111111111111111111111111\n"
        f"branch refs/heads/main\n"
        f"\n"
        f"worktree {tmp_path / 'wt-detached'}\n"
        f"HEAD {DETACHED_SHA}\n"
        f"detached\n"
    )
    worktrees = _FakeGitService(porcelain).list_worktrees(str(tmp_path))

    assert len(worktrees) == 2
    detached = worktrees[1]
    assert detached["is_detached"] is True
    assert detached["branch"] is None  # setdefault fills the key with None
    assert detached["head"] == DETACHED_SHA


# ---------------------------------------------------------------------------
# Issue #581: cross-install repo path (belongs to another install, absent
# locally) must NOT reach git — enumeration returns [] and logs at DEBUG,
# never an ERROR-level "Error listing worktrees" stack.
# ---------------------------------------------------------------------------

class _ExplodingGitService(GitWorktreeMixin):
    """Fails the test if git is ever invoked for a non-local path."""

    def __init__(self):
        self.calls = 0

    def _run_git(self, working_dir, args, timeout=10):
        self.calls += 1
        raise FileNotFoundError(2, "No such file or directory", working_dir)


def test_list_worktrees_foreign_path_returns_empty_without_running_git(caplog):
    # A path that cannot exist on this machine (another install's checkout).
    foreign_path = "D:\\Development\\ai-shuffle" if os.name != "nt" else "/workspace/does-not-exist"
    svc = _ExplodingGitService()

    with caplog.at_level(logging.DEBUG, logger="app.core.git_worktree_ops"):
        result = svc.list_worktrees(foreign_path)

    assert result == []
    # git was never invoked → no FileNotFoundError could have been raised.
    assert svc.calls == 0
    # No ERROR-level record for this skip; a DEBUG skip line is emitted.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        r.levelno == logging.DEBUG and "not present on this install" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Both sync-import sites store the sentinel instead of None
# ---------------------------------------------------------------------------

@pytest.fixture
def manager(monkeypatch, tmp_path):
    """Mock import/DB boundaries, but keep the physical project-root guard real."""
    from tests.git_repo_helpers import hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    init_repo(tmp_path)
    mgr = WorktreeManager()
    mgr.git_service = MagicMock(name="git_service")

    db = MagicMock(name="database")
    monkeypatch.setattr(wm_module, "database", db)
    monkeypatch.setattr(
        wm_module, "get_project_working_dir", lambda p: p["path"] if isinstance(p, dict) else p
    )
    return mgr, db


def _detached_git_listing(tmp_path: Path):
    """git listing: main worktree + one external detached-HEAD worktree on disk."""
    wt_dir = tmp_path / "wt-detached"
    wt_dir.mkdir()
    return [
        {"path": str(tmp_path), "branch": "main", "head": "1" * 40,
         "is_main": True, "is_bare": False, "is_detached": False},
        {"path": str(wt_dir), "branch": None, "head": DETACHED_SHA,
         "is_main": False, "is_bare": False, "is_detached": True},
    ]


def test_sync_worktrees_for_project_imports_detached(manager, tmp_path):
    mgr, db = manager
    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}
    db.get_git_repository_by_project.return_value = {"id": "repo-1", "default_branch": "main"}
    db.get_active_worktrees_for_repository.return_value = []
    mgr.git_service.list_worktrees.return_value = _detached_git_listing(tmp_path)

    result = mgr.sync_worktrees_for_project("proj-1")

    assert result["imported"] == 1
    db.create_worktree.assert_called_once()
    kwargs = db.create_worktree.call_args.kwargs
    assert kwargs["branch_name"] == f"detached-{DETACHED_SHA[:12]}"
    assert kwargs["status"] == WorktreeStatus.ACTIVE


# ---------------------------------------------------------------------------
# V2 placement checkouts (tracked in the ``checkouts`` table) must NEVER be
# imported as legacy ``worktrees`` rows by the sync path.
# ---------------------------------------------------------------------------

from app.db import checkouts as checkouts_db
from app.db import projects_v2


@pytest.fixture
def v2_project(_auto_db_shim):
    """A real project_v2 row so ``checkouts`` FK inserts satisfy the constraint."""
    return projects_v2.upsert_project("git:repo", name="My Cool Repo")


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """Redirect the runner's workspace root (hence V2 checkout root) to tmp_path."""
    from app.core import config
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", root)
    return root


def _listing_with_external(tmp_path: Path, ext_path: Path):
    """git listing: main worktree + one external (non-detached) worktree that
    exists on disk at ``ext_path``."""
    ext_path.mkdir(parents=True, exist_ok=True)
    return [
        {"path": str(tmp_path), "branch": "main", "head": "1" * 40,
         "is_main": True, "is_bare": False, "is_detached": False},
        {"path": str(ext_path), "branch": "feature-x", "head": "2" * 40,
         "is_main": False, "is_bare": False, "is_detached": False},
    ]


def _stub_db(db, tmp_path):
    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}
    db.get_git_repository_by_project.return_value = {"id": "repo-1", "default_branch": "main"}
    db.get_active_worktrees_for_repository.return_value = []
    db.get_worktrees_for_repository.return_value = []


@pytest.mark.parametrize("sync_fn", ["sync_worktrees_for_project"])
def test_v2_checkout_with_active_row_is_not_imported(manager, tmp_path, workspace_root, v2_project, sync_fn):
    mgr, db = manager
    _stub_db(db, tmp_path)
    # A V2 checkout materialized at {workspace}/checkouts/{slug}/{branch} with an
    # ACTIVE checkouts row at that exact path.
    checkout_path = workspace_root / "checkouts" / "my-slug" / "feature-x"
    checkouts_db.upsert_checkout(v2_project["id"], "runnerA", str(checkout_path))
    mgr.git_service.list_worktrees.return_value = _listing_with_external(tmp_path, checkout_path)

    getattr(mgr, sync_fn)("proj-1")

    # The V2 checkout must NOT become a legacy worktrees row.
    db.create_worktree.assert_not_called()


@pytest.mark.parametrize("sync_fn", ["sync_worktrees_for_project"])
def test_dir_under_checkout_root_without_row_is_not_imported(manager, tmp_path, workspace_root, sync_fn):
    mgr, db = manager
    _stub_db(db, tmp_path)
    # Mid-materialization race: the dir exists under the checkout root but its
    # checkouts row has not committed yet (no active row for this path).
    checkout_path = workspace_root / "checkouts" / "my-slug" / "racing-branch"
    mgr.git_service.list_worktrees.return_value = _listing_with_external(tmp_path, checkout_path)

    getattr(mgr, sync_fn)("proj-1")

    db.create_worktree.assert_not_called()


@pytest.mark.parametrize("sync_fn", ["sync_worktrees_for_project"])
def test_genuine_external_worktree_outside_checkout_root_still_imports(
    manager, tmp_path, workspace_root, v2_project, sync_fn
):
    mgr, db = manager
    _stub_db(db, tmp_path)
    # An unrelated ACTIVE checkout row exists, but the git worktree lives OUTSIDE
    # the checkout root → it is still a genuine external worktree and imports.
    checkouts_db.upsert_checkout(
        v2_project["id"], "runnerA", str(workspace_root / "checkouts" / "other" / "b")
    )
    ext_path = tmp_path / "external_wt"
    mgr.git_service.list_worktrees.return_value = _listing_with_external(tmp_path, ext_path)

    getattr(mgr, sync_fn)("proj-1")

    db.create_worktree.assert_called_once()
    assert db.create_worktree.call_args.kwargs["branch_name"] == "feature-x"

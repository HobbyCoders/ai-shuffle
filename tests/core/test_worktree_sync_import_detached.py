"""
Regression tests for issue #568: importing an external detached-HEAD worktree.

Root cause: git's porcelain worktree listing carries no ``branch`` line for a
detached worktree, and list_worktrees() fills the key with None (setdefault) —
so ``git_wt.get("branch", "unknown")`` at both sync-import sites still yielded
None and the insert violated the ``branch_name TEXT NOT NULL`` schema
constraint. The per-item try/except then silently skipped the worktree on every
sync. The fix derives a ``detached-<short-sha>`` sentinel from the HEAD SHA.

Git/DB access is patched at the same boundaries as test_worktree_manager_adopt.
"""

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
# Both sync-import sites store the sentinel instead of None
# ---------------------------------------------------------------------------

@pytest.fixture
def manager(monkeypatch):
    """A WorktreeManager with a fully mocked git_service + patched DB module."""
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


def test_sync_worktrees_imports_detached(manager, tmp_path):
    mgr, db = manager
    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}
    db.get_git_repository_by_project.return_value = {"id": "repo-1", "default_branch": "main"}
    db.get_worktrees_for_repository.return_value = []
    mgr.git_service.list_worktrees.return_value = _detached_git_listing(tmp_path)

    result = mgr.sync_worktrees("proj-1")

    assert result["imported"] == 1
    assert result["errors"] == []
    db.create_worktree.assert_called_once()
    kwargs = db.create_worktree.call_args.kwargs
    assert kwargs["branch_name"] == f"detached-{DETACHED_SHA[:12]}"

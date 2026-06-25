"""
Tests for WorktreeManager.is_worktree_valid — the redefined availability check.

Availability is exactly two rules:
  1. the folder exists, AND
  2. it contains a git repo (``.git`` present).

``.git`` may be a FILE (standard linked worktree pointing at the main repo) OR a
DIRECTORY (a regular checkout) — both now count as valid (the old check rejected
the directory form). No real git or DB is needed: the check is a pure filesystem
probe. We monkeypatch the path resolver so the test is decoupled from how a
worktree dict maps to a directory.
"""

from pathlib import Path

import pytest

import app.core.worktree_manager as wm_module
from app.core.worktree_manager import worktree_manager


@pytest.fixture(autouse=True)
def patch_resolver(monkeypatch):
    """Resolve a worktree straight to its (absolute) ``worktree_path``."""
    monkeypatch.setattr(
        wm_module,
        "get_worktree_working_dir_as_path",
        lambda wt: Path(wt["worktree_path"]),
    )


def _wt(path) -> dict:
    return {"id": "wt-1", "worktree_path": str(path)}


def test_valid_when_git_is_a_file(tmp_path):
    """Standard linked worktree: .git is a FILE pointing at the main repo."""
    (tmp_path / ".git").write_text("gitdir: /repo/.git/worktrees/wt-1\n")
    assert worktree_manager.is_worktree_valid(_wt(tmp_path)) is True


def test_valid_when_git_is_a_directory(tmp_path):
    """A regular checkout: .git is a DIRECTORY — now accepted (loosened rule)."""
    (tmp_path / ".git").mkdir()
    assert worktree_manager.is_worktree_valid(_wt(tmp_path)) is True


def test_invalid_when_folder_missing(tmp_path):
    """Folder cleaned up entirely → not valid (rule 1 fails)."""
    assert worktree_manager.is_worktree_valid(_wt(tmp_path / "gone")) is False


def test_invalid_when_folder_has_no_git(tmp_path):
    """Folder exists but has no .git → not a git repo → not valid (rule 2 fails)."""
    assert worktree_manager.is_worktree_valid(_wt(tmp_path)) is False


def test_invalid_for_empty_or_missing_worktree():
    """Defensive: a falsy/empty worktree is never valid."""
    assert worktree_manager.is_worktree_valid({}) is False
    assert worktree_manager.is_worktree_valid(None) is False

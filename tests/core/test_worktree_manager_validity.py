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


# ---------------------------------------------------------------------------
# active-session election vs. the 'failed' session status
# ---------------------------------------------------------------------------

def test_failed_session_never_wins_the_active_session_election(
    tmp_path, monkeypatch
):
    """``sessions.status = 'failed'`` (ProviderAgent's verdict on a worker whose
    dispatch errored) must not be elected the worktree's active session. The
    election tests for the ``WorktreeStatus.ACTIVE`` string, which is also
    exactly why the value is "failed" and not "completed" — ``COMPLETED`` is a
    live member of that same enum and would have aliased a worktree state."""
    (tmp_path / ".git").write_text("gitdir: /repo/.git/worktrees/wt-1\n")

    monkeypatch.setattr(
        wm_module.database, "get_git_repository_by_project",
        lambda pid: {"id": "repo-1"},
    )
    monkeypatch.setattr(
        wm_module.database, "get_active_worktrees_for_repository",
        lambda rid: [{"id": "wt-1", "worktree_path": str(tmp_path)}],
    )
    monkeypatch.setattr(
        wm_module.database, "get_sessions_for_worktree",
        lambda wid: [
            {"id": "s-failed", "title": "worker", "status": "failed"},
            {"id": "s-live", "title": "chat", "status": "active"},
        ],
    )

    (wt,) = worktree_manager.get_available_worktrees("proj-1")

    assert wt["active_session"]["id"] == "s-live"
    # The failed worker is still LISTED — only the election skips it.
    assert [s["id"] for s in wt["sessions"]] == ["s-failed", "s-live"]


def test_only_failed_sessions_leave_the_worktree_with_no_active_session(
    tmp_path, monkeypatch
):
    (tmp_path / ".git").write_text("gitdir: /repo/.git/worktrees/wt-1\n")

    monkeypatch.setattr(
        wm_module.database, "get_git_repository_by_project",
        lambda pid: {"id": "repo-1"},
    )
    monkeypatch.setattr(
        wm_module.database, "get_active_worktrees_for_repository",
        lambda rid: [{"id": "wt-1", "worktree_path": str(tmp_path)}],
    )
    monkeypatch.setattr(
        wm_module.database, "get_sessions_for_worktree",
        lambda wid: [{"id": "s-failed", "title": "worker", "status": "failed"}],
    )

    (wt,) = worktree_manager.get_available_worktrees("proj-1")

    assert wt["active_session"] is None
    assert wt["session_count"] == 1

"""Read a deliberately switched real checkout without losing binding safety."""
import pytest

from app.core import worktree_report as wr
from app.core import streaming_status
from app.core.worktree_manager import worktree_manager
from app.db import database
from tests.git_repo_helpers import commit_file, git, hermetic_git_env, init_repo, write


def test_switched_checkout_reports_its_own_dirty_and_unpushed_counts(mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "repo", {"base.txt": "base\n"})
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    database.create_project("p-switch", "Switch", None, str(repo))
    registry = worktree_manager._ensure_repository_record("p-switch", str(repo))
    path = worktree_manager._get_worktree_path(str(repo), "fix/bound")
    git(repo, "worktree", "add", "-q", "-b", "fix/bound", str(path))
    database.create_worktree("wt-switch", registry["id"], "fix/bound", str(path))
    database.create_session("s-switch", None, project_id="p-switch", worktree_path=str(path),
                            worktree_branch="fix/bound", derive_provider_from_profile=False)
    git(path, "switch", "-q", "-c", "fix/deliberate")
    commit_file(path, "valuable.txt", "valuable\n", "unpushed")
    write(path / "dirty.txt", "do not lose\n")
    write(repo / "parent-only.txt", "not checkout state\n")
    monkeypatch.setattr(streaming_status, "has_active_run", lambda sid: sid == "s-switch")
    row = wr.report("p-switch")["projects"][0]["worktrees"][0]
    assert row["state"] == "branch_mismatch"
    assert row["branch"] == "fix/deliberate"
    assert row["dirty_files"] == 1 and row["unpushed_commits"] == 1
    assert row["reclaimable"] is False
    assert "a session is working" in row["reason"]
    assert "bound branch" in row["binding_warning"]
    assert git(path, "branch", "--show-current") == "fix/deliberate"
    assert database.get_worktree("wt-switch")["branch_name"] == "fix/bound"

"""Worktree janitor report: decide reclaimable without ever deleting."""
import subprocess

import pytest

from app.core import worktree_report as wr
from tests.shared_fixtures import foreign_keys_off  # noqa: F401 (autouse fixture)

PORCELAIN = """worktree /repo
HEAD aaa
branch refs/heads/main

worktree /repo/.fluid/worktrees/fix-a
HEAD bbb
branch refs/heads/fix/a

worktree /repo/.fluid/worktrees/locked
HEAD ccc
branch refs/heads/feat/b
locked

worktree /repo/.fluid/worktrees/gone
HEAD ddd
detached
prunable gitdir file points to non-existent location
"""


def test_parse_worktree_list():
    items = wr.parse_worktree_list(PORCELAIN)
    assert [i["path"] for i in items] == ["/repo", "/repo/.fluid/worktrees/fix-a",
                                          "/repo/.fluid/worktrees/locked", "/repo/.fluid/worktrees/gone"]
    assert items[1]["branch"] == "fix/a" and items[1]["head"] == "bbb"
    assert items[2]["locked"] is True
    assert items[3]["prunable"] is True and items[3]["branch"] is None


def _item(**extra):
    return {"path": "/wt", "branch": "fix/a", "head": "bbb", "locked": False, "prunable": False, **extra}


def test_merged_clean_and_idle_is_reclaimable():
    row = wr.assess(_item(), [{"id": "s1", "pr_state": "merged", "running": False}], 0, 0, False)
    assert row["reclaimable"] is True and row["reason"] == "safe to remove"


def test_every_blocker_is_named():
    row = wr.assess(_item(locked=True), [{"id": "s1", "pr_state": "open", "running": True}], 3, 2, False)
    assert row["reclaimable"] is False
    for reason in ("a session is working in it", "locked", "uncommitted changes", "PR still open",
                   "2 unpushed commit(s)"):
        assert reason in row["reason"]


def test_branch_merged_without_pr_is_reclaimable_and_unknown_status_is_not():
    assert wr.assess(_item(), [], 0, 0, True)["reclaimable"] is True
    assert wr.assess(_item(), [], None, 0, True)["reason"] == "status unknown"
    assert "no closed PR" in wr.assess(_item(), [], 0, 0, False)["reason"]


def test_parses_a_real_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args, cwd=repo: subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    run("worktree", "add", "-q", "-b", "fix/x", str(tmp_path / "wt"))
    items = wr.parse_worktree_list(wr._git(str(repo), "worktree", "list", "--porcelain"))
    assert [i["branch"] for i in items] == ["main", "fix/x"]

@pytest.mark.parametrize("state", ["valid", "invalid", "missing"])
def test_report_unions_real_git_and_legacy_registry(mock_db, tmp_path, monkeypatch, state):
    import shutil

    from app.core.worktree_manager import worktree_manager
    from app.db import database
    from tests.git_repo_helpers import git, hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "repo")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    database.create_project("p-report", "Report", None, str(repo))
    repository = worktree_manager._ensure_repository_record("p-report", str(repo))
    path = repo / ".fluid" / "worktrees" / "fix-report"
    git(repo, "worktree", "add", "-q", "-b", "fix/report", str(path))
    database.create_worktree("wt-report", repository["id"], "fix/report", str(path))
    if state == "invalid":
        (path / ".git").unlink()
        git(repo, "worktree", "prune")  # only the disposable fixture
    elif state == "missing":
        shutil.rmtree(path)  # only the disposable fixture
        git(repo, "worktree", "prune")
    probes = []
    real_git = wr._git

    def tracked_git(root, *args, **kwargs):
        probes.append((root, args))
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(wr, "_git", tracked_git)
    result = wr.report("p-report")
    assert result["total"] == 1
    assert ((str(path), ("status", "--porcelain")) in probes) is (state == "valid")
    row = result["projects"][0]["worktrees"][0]
    assert row["path"] == str(path) and row["registry_ids"] == ["wt-report"]
    assert row["state"] == state
    assert row["reclaimable"] is (state != "invalid")
    if state != "valid":
        assert "needs cleanup" in row["reason"]
    if state == "invalid":
        assert row["dirty_files"] is None and "status unknown" in row["reason"]
    assert database.get_worktree("wt-report") is not None  # report never cleans rows
    assert git(repo, "branch", "--show-current") == "main"


def test_report_canonical_registry_missing_running_and_unmerged_guards(mock_db, tmp_path, monkeypatch):
    import shutil

    from app.core import streaming_status
    from app.core.config import settings
    from app.db import checkouts, database, projects_v2
    from tests.git_repo_helpers import commit_file, git, hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "repo")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    project = projects_v2.upsert_project("local:report", name="Canonical")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(repo))
    path = repo / ".fluid" / "worktrees" / "unmerged"
    git(repo, "worktree", "add", "-q", "-b", "fix/unmerged", str(path))
    commit_file(path, "valuable.txt", "unmerged\n", "valuable")
    checkout = checkouts.upsert_checkout(project["id"], settings.effective_instance_id,
                                         str(path), branch="fix/unmerged")
    # Foreign replicated paths must never be probed on this worker.
    checkouts.upsert_checkout(project["id"], "other-runner", "/foreign/worktree", branch="fix/foreign")
    database.create_session("report-session", None, project_v2_id=project["id"],
                            worktree_branch="fix/unmerged", derive_provider_from_profile=False)
    monkeypatch.setattr(streaming_status, "has_active_run", lambda sid: sid == "report-session")
    shutil.rmtree(path)  # only the disposable fixture
    git(repo, "worktree", "prune")
    assert database.get_session("report-session")["branch"] is None
    result = wr.report(project["id"])
    assert database.get_session("report-session")["branch"] is None  # no self-healing writes
    assert result["total"] == 1
    row = result["projects"][0]["worktrees"][0]
    assert row["state"] == "missing" and row["registry_ids"] == [checkout["id"]]
    assert row["reclaimable"] is False
    assert "a session is working" in row["reason"]
    assert "branch not merged" in row["reason"]
    assert "1 unpushed commit" in row["reason"]
    assert row["sessions"][0]["id"] == "report-session"


def test_missing_locked_entry_retains_blockers(mock_db, tmp_path, monkeypatch):
    from app.db import database
    from tests.git_repo_helpers import git, hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "repo")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    database.create_project("p-lock", "Lock", None, str(repo))
    path = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "fix/lock", str(path))
    git(repo, "worktree", "lock", str(path))
    path.rename(tmp_path / "moved-wt")
    row = wr.report("p-lock")["projects"][0]["worktrees"][0]
    assert row["state"] == "missing" and row["reclaimable"] is False
    assert "directory missing" in row["reason"] and "locked" in row["reason"]


@pytest.mark.parametrize("marker", [None, "gitdir: /missing-fixture/gitdir\n"])
def test_invalid_project_root_never_discovers_parent_worktrees(mock_db, tmp_path, monkeypatch, marker):
    from app.core.worktree_manager import worktree_manager
    from app.db import database
    from tests.git_repo_helpers import git, hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    parent = init_repo(tmp_path / "parent")
    git(parent, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(parent, "worktree", "add", "-q", "-b", "fix/parent", str(tmp_path / "parent-wt"))
    root = parent / "registry-only"
    git(parent, "worktree", "add", "-q", "-b", "fix/root", str(root))
    database.create_project("p-invalid-root", "Invalid root", None, str(root))
    repository = worktree_manager._ensure_repository_record("p-invalid-root", str(root))
    missing = root / "missing-wt"
    database.create_worktree("wt-invalid-root", repository["id"], "fix/parent", str(missing))
    (root / ".git").unlink()  # stale the producing path after registration
    if marker is not None:
        (root / ".git").write_text(marker)

    result = wr.report("p-invalid-root")
    assert result["total"] == 1  # no worktrees inherited from the enclosing repo
    project = result["projects"][0]
    assert project["error"] == "git worktree list failed"
    row = project["worktrees"][0]
    assert row["state"] == "missing" and row["registry_ids"] == ["wt-invalid-root"]
    assert row["merged_into_default"] is None and row["unpushed_commits"] is None
    assert row["reclaimable"] is False


def test_report_unions_canonical_and_legacy_metadata_and_keeps_branch_mismatch_unsafe(mock_db, tmp_path, monkeypatch):
    from app.core.config import settings
    from app.core.worktree_manager import worktree_manager
    from app.db import checkouts, database, projects_v2
    from tests.git_repo_helpers import git, hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "repo")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    database.create_project("p-union", "Union", None, str(repo))
    repository = worktree_manager._ensure_repository_record("p-union", str(repo))
    project = projects_v2.upsert_project("local:union", name="Canonical Union")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(repo))
    path = repo / ".fluid" / "worktrees" / "union"
    git(repo, "worktree", "add", "-q", "-b", "fix/union", str(path))
    database.create_worktree("wt-union", repository["id"], "fix/union", str(path))
    checkout = checkouts.upsert_checkout(project["id"], settings.effective_instance_id,
                                         str(path), branch="fix/union")
    row = wr.report(project["id"])["projects"][0]["worktrees"][0]
    assert row["state"] == "valid" and row["reclaimable"] is True
    assert set(row["registry_ids"]) == {"wt-union", checkout["id"]}

    git(path, "switch", "-q", "-c", "fix/reassigned")
    result = wr.report("p-union")
    assert result["total"] == 1 and len(result["projects"]) == 1
    row = result["projects"][0]["worktrees"][0]
    assert row["state"] == "invalid" and row["reclaimable"] is False
    assert row["branch"] == "fix/reassigned" and row["dirty_files"] is None
    assert set(row["registry_ids"]) == {"wt-union", checkout["id"]}
    assert "bound branch" in row["reason"] and "status unknown" in row["reason"]
    assert git(path, "branch", "--show-current") == "fix/reassigned"  # never switch it back


def test_report_excludes_remote_owned_sessions(mock_db, tmp_path, monkeypatch):
    from app.core.config import settings
    from app.db import checkouts, database, projects_v2
    from app.network import control
    from tests.git_repo_helpers import hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "repo")
    project = projects_v2.upsert_project("local:remote-session", name="Remote session")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id,
                             str(repo), branch="fix/remote")
    database.create_session("remote-session", None, project_v2_id=project["id"],
                            worktree_branch="fix/remote", derive_provider_from_profile=False)
    monkeypatch.setattr(control, "owner_of", lambda row: "foreign-device")
    assert wr._sessions_by_path() == {}

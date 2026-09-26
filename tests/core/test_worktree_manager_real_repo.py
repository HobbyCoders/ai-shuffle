"""WorktreeManager lifecycle on real repositories and the test database.

Covers path placement and confinement (``.fluid/worktrees/<sanitized>`` inside
the project checkout, never followed out through a symlink), the
``.git/info/exclude`` entry, recreate/adopt, cleanup with and without the
branch, disk/DB sync (stale rows, external and detached worktrees) and
repository-record discovery — all with the real git CLI in ``tmp_path``.
"""

import os
import shutil
from pathlib import Path

import pytest

from app.core import config
from app.core.worktree_manager import WorktreeError, WorktreeManager, worktree_manager
from app.db import database
from app.db.base import WorktreeStatus
from tests.git_repo_helpers import (
    commit_file,
    git,
    hermetic_git_env,
    init_repo,
    write,
)


@pytest.fixture
def project(mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    repo = init_repo(tmp_path / "proj", {"a.txt": "a\n"})
    database.create_project("p1", "Proj", None, str(repo))
    record = worktree_manager._ensure_repository_record("p1", str(repo))
    assert record is not None
    return repo, record


def _worktree_row(record, branch, path, status=WorktreeStatus.COMPLETED, wt_id="wt-1"):
    return database.create_worktree(
        worktree_id=wt_id, repository_id=record["id"], branch_name=branch,
        worktree_path=str(path), status=status,
    )


def _branch_of(path):
    return git(path, "rev-parse", "--abbrev-ref", "HEAD")


# =============================================================================
# Placement, sanitizing and confinement
# =============================================================================


@pytest.mark.parametrize("branch, folder", [
    ("feature/auth", "feature-auth"),
    ("user\\fix:1*x", "user-fix-1-x"),
    ("plain", "plain"),
])
def test_worktree_path_is_sanitized_under_fluid_worktrees(project, branch, folder):
    repo, _ = project
    path = worktree_manager._get_worktree_path(str(repo), branch)
    assert path == repo / ".fluid" / "worktrees" / folder
    assert path.parent.is_dir()


def test_worktrees_dir_is_git_ignored_once(project):
    repo, _ = project
    worktree_manager._get_worktree_path(str(repo), "one")
    worktree_manager._get_worktree_path(str(repo), "two")
    git(repo, "worktree", "add", "-q", "-b", "one", str(repo / ".fluid/worktrees/one"))

    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(".fluid/worktrees/") == 1
    assert git(repo, "status", "--porcelain") == ""


def test_exclude_goes_to_common_dir_for_linked_worktree_project(project, tmp_path):
    repo, _ = project
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "linked", str(linked))
    assert (linked / ".git").is_file()

    worktree_manager._ensure_worktrees_ignored(str(linked))

    common_exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert ".fluid/worktrees/" in common_exclude.splitlines()


def _assert_path_confined(repo, outside, link_at):
    """A symlinked ``.fluid`` (or ``.fluid/worktrees``) cannot move worktrees out."""
    link_at.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link_at, target_is_directory=True)
    with pytest.raises(WorktreeError, match="outside the project checkout"):
        worktree_manager._get_worktree_path(str(repo), "feature")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("link", [".fluid", ".fluid/worktrees"])
def test_symlinked_fluid_dir_outside_checkout_is_refused(project, tmp_path, link):
    repo, _ = project
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _assert_path_confined(repo, outside, repo / link)


def test_confinement_guard_bites_when_disabled(project, tmp_path, monkeypatch):
    repo, _ = project
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setattr(
        WorktreeManager, "_require_inside_checkout", staticmethod(lambda *_a: None)
    )
    with pytest.raises(pytest.fail.Exception):
        _assert_path_confined(repo, outside, repo / ".fluid")


@pytest.mark.parametrize("branch", ["", ".", ".."])
def test_branch_names_that_resolve_to_the_worktrees_dir_are_refused(project, branch):
    repo, _ = project
    with pytest.raises(WorktreeError, match="Invalid branch name"):
        worktree_manager._get_worktree_path(str(repo), branch)


def test_symlinked_worktree_leaf_outside_checkout_is_refused(project, tmp_path):
    repo, _ = project
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    worktrees = repo / ".fluid" / "worktrees"
    worktrees.mkdir(parents=True)
    os.symlink(outside, worktrees / "feature", target_is_directory=True)
    with pytest.raises(WorktreeError):
        worktree_manager._get_worktree_path(str(repo), "feature")


# =============================================================================
# Recreate / adopt
# =============================================================================


def test_recreate_reuses_existing_branch(project, tmp_path):
    repo, record = project
    git(repo, "branch", "feature/x")
    _worktree_row(record, "feature/x", tmp_path / "gone")

    updated = worktree_manager.recreate_worktree("wt-1")

    expected = repo / ".fluid" / "worktrees" / "feature-x"
    assert updated["status"] == WorktreeStatus.ACTIVE
    assert Path(updated["worktree_path"]) == expected
    assert _branch_of(expected) == "feature/x"
    assert worktree_manager.is_worktree_valid(updated) is True

    details = worktree_manager.get_worktree_details("wt-1")
    assert details["exists"] is True
    assert details["git_status"]["current_branch"] == "feature/x"
    assert details["session_count"] == 0 and details["active_session"] is None

    # ACTIVE and valid on disk: returned untouched, nothing re-added.
    assert worktree_manager.recreate_worktree("wt-1")["worktree_path"] == str(expected)


def test_recreate_creates_missing_branch_from_default(project, tmp_path):
    repo, record = project
    tip = commit_file(repo, "b.txt", "b\n", "main tip")
    _worktree_row(record, "deleted-branch", tmp_path / "gone")

    updated = worktree_manager.recreate_worktree("wt-1")

    path = Path(updated["worktree_path"])
    assert _branch_of(path) == "deleted-branch"
    assert git(path, "rev-parse", "HEAD") == tip


def test_recreate_adopts_branch_already_checked_out(project, tmp_path):
    repo, record = project
    elsewhere = tmp_path / "already"
    git(repo, "worktree", "add", "-q", "-b", "busy", str(elsewhere))
    _worktree_row(record, "busy", tmp_path / "gone", status=WorktreeStatus.ORPHANED)

    updated = worktree_manager.recreate_worktree("wt-1")

    assert updated["status"] == WorktreeStatus.ACTIVE
    assert os.path.realpath(updated["worktree_path"]) == os.path.realpath(elsewhere)
    assert not (repo / ".fluid" / "worktrees" / "busy").exists()


def test_recreate_errors(project, tmp_path):
    repo, record = project
    with pytest.raises(WorktreeError, match="Worktree not found"):
        worktree_manager.recreate_worktree("missing")

    _worktree_row(record, "bad..name", tmp_path / "gone", wt_id="wt-bad")
    with pytest.raises(WorktreeError, match="Git failed to recreate worktree"):
        worktree_manager.recreate_worktree("wt-bad")

    shutil.rmtree(repo / ".fluid")  # left behind by the attempt above
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, repo / ".fluid", target_is_directory=True)
    _worktree_row(record, "escape", tmp_path / "gone-too", wt_id="wt-escape")
    with pytest.raises(WorktreeError, match="outside the project checkout"):
        worktree_manager.recreate_worktree("wt-escape")
    assert list(outside.iterdir()) == []


# =============================================================================
# Cleanup
# =============================================================================


@pytest.fixture
def live_worktree(project):
    repo, record = project
    path = worktree_manager._get_worktree_path(str(repo), "feature/live")
    git(repo, "worktree", "add", "-q", "-b", "feature/live", str(path))
    write(path / "dirty.txt", "uncommitted\n")
    _worktree_row(record, "feature/live", path, status=WorktreeStatus.ACTIVE)
    return repo, path


async def test_cleanup_removes_worktree_and_branch(live_worktree):
    repo, path = live_worktree

    assert await worktree_manager.cleanup_worktree("wt-1", keep_branch=False) is True

    assert not path.exists()
    assert git(repo, "branch", "--list", "feature/live") == ""
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.COMPLETED
    listed = {os.path.realpath(w["path"]) for w in worktree_manager.git_service.list_worktrees(str(repo))}
    assert listed == {os.path.realpath(repo)}


async def test_cleanup_keeps_branch_by_default(live_worktree):
    repo, path = live_worktree
    assert await worktree_manager.cleanup_worktree("wt-1") is True
    assert not path.exists()
    assert git(repo, "branch", "--list", "feature/live").strip() == "feature/live"


async def test_cleanup_never_deletes_the_default_branch(project, tmp_path):
    repo, record = project
    _worktree_row(record, "main", tmp_path / "gone", status=WorktreeStatus.ACTIVE)
    assert await worktree_manager.cleanup_worktree("wt-1", keep_branch=False) is True
    assert git(repo, "branch", "--list", "main").strip() == "* main"


async def test_cleanup_unknown_worktree(project):
    assert await worktree_manager.cleanup_worktree("nope") is False


# =============================================================================
# Sync DB <-> disk
# =============================================================================


def test_sync_cleans_stale_rows_and_imports_external_worktrees(project, tmp_path):
    repo, record = project
    # Stale: row ACTIVE, directory deleted outside the app.
    stale = tmp_path / "stale"
    git(repo, "worktree", "add", "-q", "-b", "stale-branch", str(stale))
    _worktree_row(record, "stale-branch", stale, status=WorktreeStatus.ACTIVE, wt_id="wt-stale")
    shutil.rmtree(stale)
    # External worktrees created with the git CLI.
    external = tmp_path / "external"
    git(repo, "worktree", "add", "-q", "-b", "ext", str(external))
    detached = tmp_path / "detached"
    git(repo, "worktree", "add", "-q", "--detach", str(detached))
    # App-managed placement inside .fluid/worktrees is never imported.
    managed = worktree_manager._get_worktree_path(str(repo), "managed")
    git(repo, "worktree", "add", "-q", "-b", "managed", str(managed))

    result = worktree_manager.sync_worktrees_for_project("p1")

    assert result["is_git_repo"] is True
    assert result["cleaned_up"] == [{
        "id": "wt-stale", "branch_name": "stale-branch",
        "reason": "path_not_found", "branch_deleted": True,
    }]
    assert database.get_worktree("wt-stale")["status"] == WorktreeStatus.DELETED
    assert git(repo, "branch", "--list", "stale-branch") == ""
    assert result["imported"] == 2 and result["active"] == 2
    rows = database.get_active_worktrees_for_repository(record["id"])
    head = git(detached, "rev-parse", "HEAD")
    assert {r["branch_name"] for r in rows} == {"ext", f"detached-{head[:12]}"}

    # Idempotent: nothing new on a second pass.
    again = worktree_manager.sync_worktrees_for_project("p1")
    assert again["imported"] == 0 and again["cleaned_up"] == []


def test_sync_without_project_or_repo(mock_db, tmp_path):
    assert worktree_manager.sync_worktrees_for_project("ghost")["error"] == "Project not found"
    database.create_project("p2", "Plain", None, str(tmp_path))
    assert worktree_manager.sync_worktrees_for_project("p2") == {
        "cleaned_up": [], "active": 0, "is_git_repo": False,
    }


def test_worktree_listing_marks_missing_rows(project, tmp_path):
    repo, record = project
    live = tmp_path / "live"
    git(repo, "worktree", "add", "-q", "-b", "live", str(live))
    _worktree_row(record, "live", live, status=WorktreeStatus.ACTIVE, wt_id="wt-live")
    _worktree_row(record, "gone", tmp_path / "gone", status=WorktreeStatus.ACTIVE, wt_id="wt-gone")

    listed = {w["id"]: w for w in worktree_manager.get_worktrees_for_project("p1")}
    available = [w["id"] for w in worktree_manager.get_available_worktrees("p1")]

    assert listed["wt-live"]["exists"] is True and listed["wt-gone"]["exists"] is False
    assert available == ["wt-live"]


# =============================================================================
# Repository record discovery
# =============================================================================


@pytest.mark.parametrize("url, name", [
    ("https://github.com/acme/widgets.git", "acme/widgets"),
    ("git@github.com:acme/widgets.git", "acme/widgets"),
    ("https://gitlab.example/acme/widgets.git", None),
])
def test_repository_record_parses_github_name(mock_db, tmp_path, monkeypatch, url, name):
    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "r")
    git(repo, "remote", "add", "origin", url)
    database.create_project("p3", "R", None, str(repo))

    record = WorktreeManager()._ensure_repository_record("p3", str(repo))

    assert record["remote_url"] == url
    assert record["github_repo_name"] == name
    assert record["default_branch"] == "main"


def test_repository_record_uses_origin_head_and_skips_non_repos(mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "r", branch="trunk")
    origin_trunk = tmp_path / "o.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin_trunk))
    git(repo, "remote", "add", "origin", str(origin_trunk))
    git(repo, "push", "-q", "-u", "origin", "trunk")
    git(repo, "remote", "set-head", "origin", "trunk")
    database.create_project("p4", "R", None, str(repo))
    plain = tmp_path / "plain"
    plain.mkdir()

    assert WorktreeManager()._ensure_repository_record("p4", str(repo))["default_branch"] == "trunk"
    assert WorktreeManager()._ensure_repository_record("p5", str(plain)) is None

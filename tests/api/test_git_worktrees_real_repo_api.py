"""Legacy worktree routes end to end on a real repository.

List (with per-worktree git status), get, file browsing (confined to the
worktree), delete (git removal + optional branch delete) and reopen
(recreate under ``.fluid/worktrees``) — the WorktreeManager and GitService are
real, only auth is overridden.
"""

import os
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import git as git_api
from app.api import git_helpers, git_worktrees
from app.core import config
from app.core.worktree_manager import worktree_manager
from app.db import database
from app.db.base import WorktreeStatus
from tests.git_repo_helpers import git, hermetic_git_env, init_repo, write

BASE = "/api/v1/projects/p1/git/worktrees"


@pytest.fixture
def wt_api(mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    repo = init_repo(tmp_path / "proj", {"a.txt": "a\n", "docs/guide.md": "g\n"})
    database.create_project("p1", "Proj", None, str(repo))
    record = worktree_manager._ensure_repository_record("p1", str(repo))
    path = worktree_manager._get_worktree_path(str(repo), "feature/api")
    git(repo, "worktree", "add", "-q", "-b", "feature/api", str(path))
    write(path / "wip.txt", "wip\n")
    database.create_worktree(
        worktree_id="wt-1", repository_id=record["id"], branch_name="feature/api",
        worktree_path=str(path), status=WorktreeStatus.ACTIVE,
    )
    git_helpers.invalidate_working_dir_cache()
    git_worktrees._worktree_status_cache.clear()

    app = FastAPI()
    app.include_router(git_api.router)
    app.dependency_overrides[git_api.require_auth] = lambda: "test-token"
    with TestClient(app) as client:
        yield client, repo, path, tmp_path
    git_worktrees._worktree_status_cache.clear()
    git_helpers.invalidate_working_dir_cache()


def test_list_worktrees_with_git_status(wt_api):
    client, _, path, _ = wt_api

    response = client.get(BASE)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    wt = body["worktrees"][0]
    assert wt["id"] == "wt-1" and wt["branch_name"] == "feature/api"
    assert wt["worktree_path"] == str(path)
    assert wt["exists"] is True and wt["resource_kind"] == "worktree"
    assert wt["git_status"]["current_branch"] == "feature/api"
    assert wt["git_status"]["untracked"] == ["wip.txt"]
    assert wt["git_status"]["is_clean"] is False

    no_status = client.get(BASE, params={"include_status": "false"}).json()
    assert no_status["worktrees"][0]["git_status"] is None


def test_get_worktree_and_foreign_ids_are_404(wt_api, mock_db):
    client, _, _, tmp_path = wt_api
    assert client.get(f"{BASE}/wt-1").json()["git_status"]["current_branch"] == "feature/api"
    assert client.get(f"{BASE}/nope").status_code == 404

    other = init_repo(tmp_path / "other")
    database.create_project("p2", "Other", None, str(other))
    worktree_manager._ensure_repository_record("p2", str(other))
    response = client.get("/api/v1/projects/p2/git/worktrees/wt-1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Worktree not found in this project: wt-1"


def test_worktree_file_browsing_is_confined(wt_api):
    client, _, path, tmp_path = wt_api
    (tmp_path / "outside").mkdir()

    root = client.get(f"{BASE}/wt-1/files").json()
    names = [f["name"] for f in root["files"]]
    assert names == ["docs", "a.txt", "wip.txt"]            # dirs first, hidden skipped
    assert root["worktreeId"] == "wt-1" and root["worktreePath"] == str(path)
    sub = client.get(f"{BASE}/wt-1/files", params={"path": "docs"}).json()
    assert [f["path"] for f in sub["files"]] == ["docs/guide.md"]

    for escape in ("..", "../..", "../../outside", str(tmp_path)):
        response = client.get(f"{BASE}/wt-1/files", params={"path": escape})
        assert response.status_code == 400, escape
        assert response.json()["detail"] == "Path escapes worktree boundary"
    assert client.get(f"{BASE}/wt-1/files", params={"path": "a.txt"}).status_code == 400
    assert client.get(f"{BASE}/wt-1/files", params={"path": "missing"}).status_code == 404


def test_delete_then_reopen_worktree(wt_api):
    client, repo, path, _ = wt_api

    deleted = client.delete(f"{BASE}/wt-1", params={"keep_branch": "false"})

    assert deleted.status_code == 200
    assert deleted.json()["message"] == "Removed worktree for branch 'feature/api' (branch deleted)"
    assert not path.exists()
    assert git(repo, "branch", "--list", "feature/api") == ""
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.COMPLETED

    reopened = client.post(f"{BASE}/wt-1/reopen")

    assert reopened.status_code == 200, reopened.text
    body = reopened.json()
    assert body["status"] == WorktreeStatus.ACTIVE
    assert body["worktree_path"] == str(repo / ".fluid" / "worktrees" / "feature-api")
    assert git(body["worktree_path"], "rev-parse", "--abbrev-ref", "HEAD") == "feature/api"


def test_delete_keeps_branch_by_default_and_reopen_errors_are_400(wt_api):
    client, repo, _, tmp_path = wt_api
    assert client.delete(f"{BASE}/wt-1").json()["message"].endswith("(branch kept)")
    assert git(repo, "branch", "--list", "feature/api").strip() == "feature/api"
    # A .fluid that points out of the checkout: reopen is a clean 400 and
    # nothing is written at the symlink target.
    shutil.rmtree(repo / ".fluid")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / ".fluid", target_is_directory=True)
    response = client.post(f"{BASE}/wt-1/reopen")
    assert response.status_code == 400
    assert "outside the project checkout" in response.json()["detail"]
    assert list(outside.iterdir()) == []

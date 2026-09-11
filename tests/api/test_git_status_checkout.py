"""Git status must read a v2 session's actual checkout, not its dirty base."""

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import git as git_api, git_helpers
from app.core import checkout_manager, config
from app.db import checkouts, database, projects_v2


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def checkout_client(mock_db, tmp_path, monkeypatch):
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    base = tmp_path / "repo"
    base.mkdir()
    _git(base, "init", "-b", "main")
    _git(base, "config", "user.name", "Test")
    _git(base, "config", "user.email", "test@example.invalid")
    (base / "tracked.txt").write_text("initial\n")
    _git(base, "add", "tracked.txt")
    _git(base, "commit", "-m", "initial")

    database.create_project("p1", "Repo", None, str(base))
    project = projects_v2.upsert_project("local:p1", name="Repo")
    checkouts.upsert_checkout(project["id"], config.settings.effective_instance_id, str(base))
    database.create_session(session_id="s1", profile_id="default", project_id="p1")
    database.set_session_branch("s1", "feature/new", project["id"])
    # Use the production materializer: a v2 branch session has no worktrees row.
    branch = Path(checkout_manager.ensure_checkout(
        project, branch="feature/new", create_new_branch=True,
    ))
    assert database.get_session("s1")["worktree_id"] is None
    assert _git(branch, "status", "--porcelain") == ""
    (base / "tracked.txt").write_text("unrelated base edit\n")

    # Repository metadata isn't under test. Do not run remote discovery.
    monkeypatch.setattr(git_api, "peek_repo_record_cache", lambda _: None)
    git_helpers.invalidate_working_dir_cache()
    app = FastAPI()
    app.include_router(git_api.router)
    app.dependency_overrides[git_api.require_auth] = lambda: "test-token"
    with TestClient(app) as client:
        yield client, base, branch, project
    git_helpers.invalidate_working_dir_cache()


def test_clean_new_session_does_not_inherit_base_changes(checkout_client):
    client, base, branch, _ = checkout_client
    url = "/api/v1/projects/p1/git/status"
    # Alternate scopes: neither path resolution nor coalescing may mix them.
    for params, expected_branch, clean in [
        ({}, "main", False),
        ({"session_id": "s1"}, "feature/new", True),
        ({}, "main", False),
        ({"session_id": "s1"}, "feature/new", True),
    ]:
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["current_branch"] == expected_branch
        assert data["is_clean"] is clean
        assert data["modified"] == ([] if clean else ["tracked.txt"])
    assert _git(branch, "status", "--porcelain") == ""
    assert _git(base, "status", "--porcelain") != ""


def test_session_status_still_reports_its_own_edits(checkout_client):
    client, _, branch, _ = checkout_client
    (branch / "own.txt").write_text("own edit\n")
    response = client.get("/api/v1/projects/p1/git/status", params={"session_id": "s1"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["current_branch"] == "feature/new"
    assert data["is_clean"] is False
    assert data["untracked"] == ["own.txt"]
    assert data["modified"] == []


def test_unmaterialized_session_does_not_fall_back_to_dirty_base(checkout_client):
    client, _, _, project = checkout_client
    database.create_session(session_id="pending", profile_id="default", project_id="p1")
    database.set_session_branch("pending", "feature/pending", project["id"])
    response = client.get("/api/v1/projects/p1/git/status", params={"session_id": "pending"})
    assert response.status_code == 409
    assert "checkout is not available" in response.json()["detail"]
    assert checkouts.get_checkout(
        project["id"], config.settings.effective_instance_id, branch="feature/pending",
    ) is None

"""Admin bulk reclaim route, exercising the registry + real Git producing path."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import git_worktrees
from app.core import owner_auth, preview_access
from app.core.config import settings
from app.db import checkouts, projects_v2
from tests.git_repo_helpers import commit_file, git, hermetic_git_env, init_repo
from tests.shared_fixtures import foreign_keys_off  # noqa: F401


@pytest.fixture
def api(mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "project")
    project = projects_v2.upsert_project("local:reclaim-api", name="Reclaim API")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(repo))
    path = repo / ".fluid" / "worktrees" / "task"
    git(repo, "worktree", "add", "-q", "-b", "task", str(path))
    commit_file(path, "change", "integrated", "change")
    git(repo, "merge", "--ff-only", "task")
    row = checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(path), branch="task")
    app = FastAPI()
    app.include_router(git_worktrees.router, prefix="/api/v1/projects/{project_id}/git")
    url = f"/api/v1/projects/{project['id']}/git/worktrees/reclaim"
    return app, url, repo, path, row, project


def test_admin_default_dry_run_then_apply(api):
    app, url, repo, path, row, project = api
    app.dependency_overrides[git_worktrees.require_admin] = lambda: "owner"
    with TestClient(app) as client:
        response = client.post(url)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dry_run"] is True and body["reclaimed"] == 0
        assert body["reclaimable"] == 1
        assert body["items"][0]["registry_ids"] == [row["id"]]
        assert body["items"][0]["decision"] == "remove"
        assert path.exists()
        result = client.post(url, json={"dry_run": False}).json()
        assert result["reclaimed"] == 1 and result["items"][0]["applied"] is True
        assert not path.exists() and (repo / ".git").exists()
        archived = checkouts.get_checkout_by_path(str(path), project_id=project["id"])
        assert archived["status"] == "archived"
        # Archived records do not pile up in future cleanup previews.
        again = client.post(url).json()
        assert again["total"] == 0 and again["items"] == []


def test_non_admin_denied_before_git(api, monkeypatch):
    app, url, _, path, _, _ = api
    monkeypatch.setattr(owner_auth, "local_access", lambda request: False)
    with TestClient(app) as client:
        response = client.post(url, json={"dry_run": False})
    assert response.status_code == 403 and path.exists()


@pytest.mark.parametrize("dry_run", [True, False])
def test_preview_cookie_denied_even_with_independent_owner_identity(api, monkeypatch, tmp_path, dry_run):
    app, url, _, path, _, _ = api
    monkeypatch.setattr(settings, "data_dir", tmp_path / "preview-key")
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    monkeypatch.setattr(owner_auth, "local_access", lambda request: True)
    monkeypatch.setattr(owner_auth, "same_origin", lambda request: None)
    token = preview_access.mint("preview-session")
    with TestClient(app, base_url="http://10.0.0.30:8000", client=("10.0.0.20", 1234)) as client:
        client.cookies.set(preview_access.cookie_name(), token)
        response = client.post(url, json={"dry_run": dry_run})
    assert response.status_code == 403, response.text
    assert "Agent previews" in response.json()["detail"]
    assert path.exists()


def test_apply_pagination_does_not_skip_rows_as_registry_shrinks(api):
    app, url, repo, first, _, project = api
    for name in ("task-two", "task-three"):
        path = repo / ".fluid" / "worktrees" / name
        git(repo, "worktree", "add", "-q", "-b", name, str(path))
        checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(path), branch=name)
    app.dependency_overrides[git_worktrees.require_admin] = lambda: "owner"
    with TestClient(app) as client:
        first_page = client.post(url, json={"dry_run": False, "limit": 1}).json()
        assert first_page["reclaimed"] == 1 and first_page["next_offset"] == 0
        second_page = client.post(url, json={"dry_run": False, "limit": 2, "offset": first_page["next_offset"]}).json()
        assert second_page["reclaimed"] == 2 and second_page["next_offset"] is None
        assert client.post(url).json()["total"] == 0
    assert not first.exists()


def test_unknown_project_returns_404(api):
    app, _, _, path, _, _ = api
    app.dependency_overrides[git_worktrees.require_admin] = lambda: "owner"
    with TestClient(app) as client:
        response = client.post("/api/v1/projects/no-such-project/git/worktrees/reclaim")
    assert response.status_code == 404 and path.exists()


@pytest.mark.parametrize("payload", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_bounded_request_validation(api, payload):
    app, url, _, path, _, _ = api
    app.dependency_overrides[git_worktrees.require_admin] = lambda: "owner"
    with TestClient(app) as client:
        assert client.post(url, json=payload).status_code == 422
    assert path.exists()

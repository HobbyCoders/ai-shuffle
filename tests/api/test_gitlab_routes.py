"""Project PR routes and Git-panel PR state for a project whose origin is on GitLab.

The project's checkout is a real repository with a GitLab ``origin``; GitLab's
REST API is the in-process fake from ``tests.gitlab_stub`` and ``gh`` is the
stub from ``tests.gh_stub`` (signed out unless a test signs it in), so a
GitLab project must work without GitHub and a GitHub project must still go to
``gh`` when GitLab is also connected.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth as auth_api
from app.api import github_repos
from app.core import config
from app.core.gitlab_service import gitlab_service
from app.db import database
from app.core.github_service import github_service
from tests.git_flow_helpers import build_git_app, github_repo, reset_git_state
from tests.git_repo_helpers import git, hermetic_git_env
from tests.gitlab_stub import HOST, TOKEN, FakeGitLab, mr, save_account

BASE = "/api/v1/projects/p1/github"
MRS = "/projects/acme%2Fapp/merge_requests"


@pytest.fixture
def env(mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    reset_git_state()
    gh = GhSpy(monkeypatch)
    api = FakeGitLab(monkeypatch)
    save_account(gitlab_service)
    repo = github_repo(tmp_path)  # a real checkout on feat/x; origin is repointed below
    git(repo, "remote", "set-url", "origin", f"{HOST}/acme/app.git")
    database.create_project("p1", "Repo", None, str(repo))
    with TestClient(build_git_app()) as client:
        yield SimpleNamespace(client=client, repo=repo, gh=gh, api=api)
    reset_git_state()


class GhSpy:
    """Stands in for ``gh``: signed out by default, records every repo call."""

    METHODS = ("get_repo_info", "list_pulls", "get_pull", "create_pull", "merge_pull", "close_pull")

    def __init__(self, monkeypatch):
        self.signed = False
        self.calls: list[str] = []
        monkeypatch.setattr(github_service, "is_authenticated", lambda *a, **k: self.signed)
        for name in self.METHODS:
            monkeypatch.setattr(github_service, name, self._recorder(name))

    def _recorder(self, name):
        def record(*_args, **_kwargs):
            self.calls.append(name)
            return [] if name == "list_pulls" else None
        return record


def _gh_repo_calls(env):
    return env.gh.calls


def test_status_reports_gitlab_without_a_github_sign_in(env):
    env.api.route("GET", "/projects/acme%2Fapp", 200, {
        "name": "app", "namespace": {"full_path": "acme"}, "web_url": f"{HOST}/acme/app",
        "default_branch": "main",
    })
    data = env.client.get(f"{BASE}/status").json()
    assert data["provider"] == "gitlab"
    assert data["authenticated"] is True
    assert data["repo"] == "acme/app"
    assert data["repo_info"]["default_branch"] == "main"
    assert _gh_repo_calls(env) == []


def test_list_get_create_merge_close_go_to_gitlab(env):
    env.api.route("GET", MRS, 200, [mr(3)])
    env.api.route("GET", f"{MRS}/3", 200, mr(3))
    env.api.route("POST", MRS, 201, mr(4))
    env.api.route("PUT", f"{MRS}/4/merge", 200, mr(4, "merged"))
    env.api.route("PUT", f"{MRS}/5", 200, mr(5, "closed"))

    listed = env.client.get(f"{BASE}/pulls")
    assert listed.status_code == 200, listed.text
    assert [p["number"] for p in listed.json()] == [3]
    assert listed.json()[0]["url"].endswith("/acme/app/-/merge_requests/3")

    assert env.client.get(f"{BASE}/pulls/3").json()["state"] == "OPEN"

    created = env.client.post(f"{BASE}/pulls", json={"title": "Add feature", "body": "", "head": "feat/x", "base": "main"})
    assert created.status_code == 201, created.text
    assert created.json()["number"] == 4

    merged = env.client.post(f"{BASE}/pulls/4/merge", json={"method": "squash"})
    assert merged.status_code == 200, merged.text

    closed = env.client.delete(f"{BASE}/pulls/5")
    assert closed.status_code == 200, closed.text

    assert ("PUT", f"{MRS}/4/merge") in env.api.paths()
    assert ("PUT", f"{MRS}/5") in env.api.paths()
    assert _gh_repo_calls(env) == []


def test_gitlab_failure_is_502_not_an_empty_list(env):
    env.api.route("GET", MRS, 500, {"message": "boom"})
    response = env.client.get(f"{BASE}/pulls")
    assert response.status_code == 502, response.text
    assert "GitLab" in response.json()["detail"]


def test_a_github_project_still_uses_gh_when_gitlab_is_connected(env):
    git(env.repo, "remote", "set-url", "origin", "https://github.com/acme/app.git")
    reset_git_state()
    env.gh.signed = True
    data = env.client.get(f"{BASE}/status").json()
    assert data["provider"] == "github"
    assert data["repo"] == "acme/app"
    assert env.client.get(f"{BASE}/pulls").status_code == 200
    assert "list_pulls" in env.gh.calls
    assert not any(path.startswith("/projects/") for _m, path in env.api.paths())


def test_a_github_project_without_gh_is_still_401(env):
    git(env.repo, "remote", "set-url", "origin", "https://github.com/acme/app.git")
    reset_git_state()
    assert env.client.get(f"{BASE}/pulls").status_code == 401


def test_pr_state_reads_the_open_merge_request(env):
    env.api.route("GET", MRS, 200, [mr(4)])
    data = env.client.get("/api/v1/projects/p1/git/pr-state").json()
    assert data["has_github"] is True
    assert data["provider"] == "gitlab"
    assert data["open_pr"]["number"] == 4
    assert data["open_pr"]["mergeable"] is True
    assert data["pr_lookup_failed"] is False


def test_pr_state_marks_a_gitlab_outage_as_failed(env):
    env.api.unreachable = True
    data = env.client.get("/api/v1/projects/p1/git/pr-state").json()
    assert data["has_github"] is True
    assert data["pr_lookup_failed"] is True
    assert data["open_pr"] is None


def test_pr_state_after_disconnect_is_no_forge(env):
    gitlab_service.logout()
    data = env.client.get("/api/v1/projects/p1/git/pr-state").json()
    assert data["has_github"] is False


# =============================================================================
# Settings endpoints: /auth/gitlab/* and /gitlab/repos
# =============================================================================


@pytest.fixture
def settings_client(tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    api = FakeGitLab(monkeypatch)
    app = FastAPI()
    app.include_router(auth_api.router)
    app.include_router(github_repos.gitlab_router)
    for dep in (auth_api.require_auth, auth_api.require_admin, github_repos.require_admin):
        app.dependency_overrides[dep] = lambda: "test-token"
    with TestClient(app) as client:
        yield client, api


def test_connect_status_and_disconnect(settings_client):
    client, _api = settings_client
    assert client.get("/api/v1/auth/gitlab/status").json()["authenticated"] is False

    bad = client.post("/api/v1/auth/gitlab/login", json={"token": "glpat-wrong", "host": HOST}).json()
    assert bad["success"] is False

    ok = client.post("/api/v1/auth/gitlab/login", json={"token": TOKEN, "host": HOST}).json()
    assert ok == {"success": True, "user": "octo", "host": HOST}

    status = client.get("/api/v1/auth/gitlab/status").json()
    assert status == {"authenticated": True, "user": "octo", "host": HOST, "error": None}
    assert TOKEN not in str(status)

    assert client.post("/api/v1/auth/gitlab/logout").json() == {"success": True}
    assert client.get("/api/v1/auth/gitlab/status").json()["authenticated"] is False


@pytest.mark.parametrize("body", [{}, {"token": ""}, {"token": 5}, {"token": TOKEN, "host": 5}])
def test_login_rejects_malformed_bodies(settings_client, body):
    client, api = settings_client
    assert client.post("/api/v1/auth/gitlab/login", json=body).status_code == 400
    assert api.calls == []


def test_repo_list_for_the_clone_picker(settings_client):
    client, api = settings_client
    assert client.get("/api/v1/gitlab/repos").json()["connected"] is False
    save_account(gitlab_service)
    api.route("GET", "/projects", 200, [
        {"path_with_namespace": "acme/app", "path": "app", "namespace": {"full_path": "acme"},
         "visibility": "private", "http_url_to_repo": f"{HOST}/acme/app.git", "web_url": f"{HOST}/acme/app"},
        {"path_with_namespace": "acme/tools", "path": "tools", "namespace": {"full_path": "acme"},
         "visibility": "public", "http_url_to_repo": f"{HOST}/acme/tools.git", "web_url": f"{HOST}/acme/tools"},
    ])
    page = client.get("/api/v1/gitlab/repos", params={"q": "tool"}).json()
    assert page["connected"] is True
    assert [r["full_name"] for r in page["items"]] == ["acme/tools"]
    assert page["items"][0]["clone_url"] == f"{HOST}/acme/tools.git"


def test_repo_list_outage_is_502(settings_client):
    client, api = settings_client
    save_account(gitlab_service)
    api.route("GET", "/projects", 503, {"message": "maintenance"})
    response = client.get("/api/v1/gitlab/repos")
    assert response.status_code == 502
    assert "GitLab" in response.json()["detail"]

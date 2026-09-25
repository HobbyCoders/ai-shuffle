"""GitHub PR and workflow-run routes through a stub ``gh`` on PATH.

The project points at a real repository whose ``origin`` is a github.com URL;
``github_service`` is not mocked, so the argv each route hands to ``gh`` and
the mapping of gh's replies to status codes are both pinned against
docs/content/api-reference/miscellaneous.md ("GitHub Integration").
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import github as github_api
from app.core import config
from app.db import database
from tests.gh_stub import FakeGh, pr_json, run_json
from tests.git_flow_helpers import REPO, build_git_app, github_repo, reset_git_state
from tests.git_repo_helpers import git, hermetic_git_env

BASE = "/api/v1/projects/p1/github"
NOT_SIGNED_IN = "GitHub CLI is not authenticated. Please configure GitHub authentication."
PR_FIELDS = {"number", "title", "body", "state", "head_branch", "base_branch", "author",
             "url", "created_at", "updated_at", "mergeable"}
RUN_FIELDS = {"id", "name", "status", "conclusion", "branch", "event", "url", "created_at"}

AUTHED_ROUTES = [
    ("get", "/pulls"),
    ("get", "/pulls/42"),
    ("post", "/pulls/42/merge"),
    ("delete", "/pulls/42"),
    ("get", "/actions"),
    ("get", "/actions/123"),
    ("post", "/actions/123/rerun"),
]


@pytest.fixture
def env(mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    reset_git_state()
    gh = FakeGh(tmp_path, monkeypatch).signed_in()
    repo = github_repo(tmp_path)
    database.create_project("p1", "Repo", None, str(repo))
    with TestClient(build_git_app()) as client:
        yield SimpleNamespace(client=client, repo=repo, gh=gh, tmp=tmp_path)
    reset_git_state()


def _call(client, method, path, **kwargs):
    return getattr(client, method)(f"{BASE}{path}", **kwargs)


# =============================================================================
# Auth
# =============================================================================


def _assert_signed_out_routes_are_401(env):
    env.gh.signed_in(False)
    for method, path in AUTHED_ROUTES:
        response = _call(env.client, method, path)
        assert response.status_code == 401, (method, path, response.text)
        assert response.json()["detail"] == NOT_SIGNED_IN
    # Refused before any repository call reached gh.
    assert [c for c in env.gh.argvs() if c[:2] != ["auth", "status"]] == []


def test_every_route_but_status_needs_a_signed_in_gh(env):
    _assert_signed_out_routes_are_401(env)


def test_auth_guard_bites_when_disabled(env, monkeypatch):
    async def no_check():
        return None

    monkeypatch.setattr(github_api, "check_github_auth", no_check)
    with pytest.raises(AssertionError):
        _assert_signed_out_routes_are_401(env)


def test_auth_answer_is_cached(env):
    for _ in range(3):
        _call(env.client, "get", "/actions/123")
    assert len(env.gh.calls("auth", "status")) == 1


def test_status_reports_auth_repo_and_gh_failures(env):
    env.gh.signed_in(False)
    assert _call(env.client, "get", "/status").json() == {
        "authenticated": False, "repo": None, "repo_info": None,
        "error": "GitHub CLI is not authenticated",
    }

    reset_git_state()
    env.gh.signed_in(True)
    env.gh.on("repo", "view", REPO, stdout={
        "name": "app", "owner": {"login": "acme"}, "description": "d",
        "url": "https://github.com/acme/app", "defaultBranchRef": {"name": "trunk"},
    })
    data = _call(env.client, "get", "/status").json()
    assert data["authenticated"] is True and data["repo"] == REPO and data["error"] is None
    assert data["repo_info"] == {"name": "app", "owner": "acme", "description": "d",
                                 "url": "https://github.com/acme/app", "default_branch": "trunk"}

    env.gh.on("repo", "view", REPO, code=1, stderr="HTTP 502: Bad Gateway")
    data = _call(env.client, "get", "/status").json()
    assert data["repo"] == REPO and data["repo_info"] is None
    assert data["error"] == "GitHub CLI is unavailable: HTTP 502: Bad Gateway"


def test_routes_without_a_github_remote_are_400(env):
    git(env.repo, "remote", "set-url", "origin", "https://gitlab.com/acme/app.git")
    for method, path in AUTHED_ROUTES:
        response = _call(env.client, method, path)
        assert response.status_code == 400, (method, path)
        assert response.json()["detail"].startswith("Could not determine GitHub repository")
    assert env.gh.calls("pr") == [] and env.gh.calls("run") == []


# =============================================================================
# Pull requests
# =============================================================================


def test_get_pull_request_fields(env):
    env.gh.on("pr", "view", "42", stdout=pr_json(42, "feat/x", mergeable="CONFLICTING"))

    response = _call(env.client, "get", "/pulls/42")

    assert response.status_code == 200
    data = response.json()
    assert set(data) == PR_FIELDS
    assert data["number"] == 42 and data["head_branch"] == "feat/x" and data["author"] == "octo"
    assert data["mergeable"] is False
    [argv] = env.gh.argvs("pr", "view")
    assert argv[:5] == ["pr", "view", "42", "--repo", REPO]


def test_get_pull_request_not_found_404_and_unreachable_502(env):
    env.gh.on("pr", "view", "42", code=1,
              stderr="GraphQL: Could not resolve to a PullRequest with the number of 42.")
    response = _call(env.client, "get", "/pulls/42")
    assert response.status_code == 404
    assert response.json()["detail"] == "Pull request #42 not found"

    env.gh.on("pr", "view", "42", code=1, stderr="error connecting to api.github.com")
    response = _call(env.client, "get", "/pulls/42")
    assert response.status_code == 502
    assert response.json()["detail"] == (
        "GitHub CLI could not read pull request #42: error connecting to api.github.com"
    )


def test_list_pull_requests_clamps_limit_to_100(env):
    env.gh.on("pr", "list", stdout=[pr_json(1, "a"), pr_json(2, "b")])

    data = _call(env.client, "get", "/pulls", params={"state": "all", "limit": 999}).json()

    assert [p["number"] for p in data] == [1, 2]
    [argv] = env.gh.argvs("pr", "list")
    assert argv[argv.index("--limit") + 1] == "100"
    assert argv[argv.index("--state") + 1] == "all"


@pytest.mark.parametrize("body, flag", [
    (None, "--merge"),                 # no body: documented default
    ({}, "--merge"),                   # empty body: same default
    ({"method": "squash"}, "--squash"),
    ({"method": "rebase"}, "--rebase"),
])
def test_merge_method_default_and_choices(env, body, flag):
    env.gh.on("pr", "merge", "42")
    env.gh.on("pr", "view", "42", stdout=pr_json(42, "feat/x", state="MERGED"))

    kwargs = {} if body is None else {"json": body}
    response = _call(env.client, "post", "/pulls/42/merge", **kwargs)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "Pull request #42 merged successfully"}
    assert env.gh.argvs("pr", "merge") == [["pr", "merge", "42", "--repo", REPO, flag, "--delete-branch"]]


@pytest.mark.parametrize("method", ["admin", "--admin", "squash --admin", "MERGE", ""])
def test_merge_rejects_unknown_methods_before_gh(env, method):
    response = _call(env.client, "post", "/pulls/42/merge", json={"method": method})

    assert response.status_code == 422
    assert env.gh.calls("pr") == []


def test_merge_conflict_is_400_with_githubs_reason(env):
    reason = "Pull request acme/app#42 is not mergeable: the merge commit cannot be cleanly created."
    env.gh.on("pr", "merge", "42", code=1, stderr=reason)

    response = _call(env.client, "post", "/pulls/42/merge", json={"method": "squash"})

    # Documented: 400 with GitHub's reason (not 409).
    assert response.status_code == 400
    assert response.json()["detail"] == f"Failed to merge pull request #42: {reason}"


def test_close_pull_request(env):
    env.gh.on("pr", "close", "42")
    env.gh.on("pr", "view", "42", stdout=pr_json(42, "feat/x", state="CLOSED"))

    response = _call(env.client, "delete", "/pulls/42")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "Pull request #42 closed"}
    assert env.gh.argvs("pr", "close") == [["pr", "close", "42", "--repo", REPO]]

    env.gh.on("pr", "close", "42", code=1, stderr="Pull request #42 is already closed")
    response = _call(env.client, "delete", "/pulls/42")
    assert response.status_code == 400
    assert response.json()["detail"] == "Failed to close pull request #42: Pull request #42 is already closed"


def test_create_pull_request_title_is_a_value_not_a_flag(env):
    env.gh.on("pr", "create", stdout=f"https://github.com/{REPO}/pull/9\n")
    env.gh.on("pr", "view", "9", stdout=pr_json(9, "feat/x", title="--draft --base evil"))

    response = env.client.post(f"{BASE}/pulls", json={
        "title": "--draft --base evil", "body": "--web", "head": "feat/x", "base": "main",
    })

    assert response.status_code == 201, response.text
    assert response.json()["number"] == 9
    [call] = env.gh.calls("pr", "create")
    argv = call["args"]
    assert argv[argv.index("--title") + 1] == "--draft --base evil"
    assert argv[argv.index("--base") + 1] == "main"
    assert "--draft" not in argv and "--web" not in argv
    assert call["stdin"] == "--web"


def test_create_pull_request_reuses_an_existing_one(env):
    env.gh.on("pr", "create", code=1, stderr=(
        f'a pull request for branch "feat/x" into branch "main" already exists:\n'
        f"https://github.com/{REPO}/pull/5"
    ))
    env.gh.on("pr", "view", "5", stdout=pr_json(5, "feat/x"))

    response = env.client.post(f"{BASE}/pulls", json={"title": "t", "head": "feat/x", "base": "main"})

    assert response.status_code == 201
    assert response.json()["number"] == 5


# =============================================================================
# Workflow runs
# =============================================================================


@pytest.mark.parametrize("params, expected", [({}, "10"), ({"limit": 50}, "50"), ({"limit": 51}, "50"),
                                              ({"limit": 1000}, "50"), ({"limit": 3}, "3")])
def test_list_runs_limit_default_10_max_50(env, params, expected):
    env.gh.on("run", "list", stdout=[run_json(1), run_json(2, conclusion=None)])

    response = _call(env.client, "get", "/actions", params=params)

    assert response.status_code == 200
    data = response.json()
    assert [r["id"] for r in data] == [1, 2] and data[1]["conclusion"] is None
    assert all(set(r) == RUN_FIELDS for r in data)
    [argv] = env.gh.argvs("run", "list")
    assert argv[argv.index("--limit") + 1] == expected


def test_list_runs_unreachable_is_502(env):
    env.gh.on("run", "list", code=1, stderr="HTTP 503")
    response = _call(env.client, "get", "/actions")
    assert response.status_code == 502
    assert response.json()["detail"] == "GitHub CLI could not list workflow runs: HTTP 503"


def test_get_run(env):
    env.gh.on("run", "view", "123", stdout=run_json(123, name="Deploy"))

    response = _call(env.client, "get", "/actions/123")

    assert response.status_code == 200
    assert response.json() == {
        "id": 123, "name": "Deploy", "status": "completed", "conclusion": "success",
        "branch": "main", "event": "push",
        "url": "https://github.com/acme/app/actions/runs/123", "created_at": "2026-01-01T00:00:00Z",
    }

    env.gh.on("run", "view", "123", code=1, stderr="HTTP 404: Not Found")
    response = _call(env.client, "get", "/actions/123")
    assert response.status_code == 404
    assert response.json()["detail"] == "Workflow run #123 not found"

    env.gh.on("run", "view", "123", code=1, stderr="timeout awaiting response headers")
    assert _call(env.client, "get", "/actions/123").status_code == 502

    # Path validation: a run id is an integer.
    assert _call(env.client, "get", "/actions/--web").status_code == 422


def test_rerun(env):
    env.gh.on("run", "rerun", "123")

    response = _call(env.client, "post", "/actions/123/rerun")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "Workflow #123 re-run triggered"}
    assert env.gh.argvs("run", "rerun") == [["run", "rerun", "123", "--repo", REPO]]

    env.gh.on("run", "rerun", "123", code=1, stderr="run 123 cannot be rerun")
    response = _call(env.client, "post", "/actions/123/rerun")
    assert response.status_code == 400
    assert response.json()["detail"] == "Failed to re-run workflow #123"

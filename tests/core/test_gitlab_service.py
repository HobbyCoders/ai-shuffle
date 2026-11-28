"""GitLab service: sign-in, git credential wiring and merge requests.

GitLab's REST API is replaced by a small in-process fake (``FakeGitLab``);
git itself is real, pointed at a throwaway global config, so the credential
helper sign-in writes is checked by asking git for a credential.
"""

import json
import subprocess
from pathlib import Path

import pytest

from app.core.gitlab_service import GitLabApiError, GitLabService, normalize_host, repo_path_for_remote
from tests.git_repo_helpers import git, hermetic_git_env
from tests.gitlab_stub import HOST, TOKEN, FakeGitLab, mr as _mr

@pytest.fixture
def env(tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    service = GitLabService(config_dir=tmp_path / "gitlab")
    return service, FakeGitLab(monkeypatch), tmp_path


# =============================================================================
# Host and remote parsing
# =============================================================================


@pytest.mark.parametrize("typed, expected", [
    ("", "https://gitlab.com"),
    (None, "https://gitlab.com"),
    ("gitlab.example.com", "https://gitlab.example.com"),
    ("https://GitLab.Example.com/", "https://gitlab.example.com"),
    ("https://git.example.com:8443/gitlab/", "https://git.example.com:8443/gitlab"),
])
def test_normalize_host(typed, expected):
    assert normalize_host(typed) == expected


@pytest.mark.parametrize("typed", [
    "ftp://gitlab.example.com", "https://user:pw@gitlab.example.com",
    "https://gitlab.example.com/?x=1", "https://gitlab.example.com/#f", "https://git'lab.example.com",
])
def test_normalize_host_rejects_unsafe_addresses(typed):
    with pytest.raises(ValueError):
        normalize_host(typed)


@pytest.mark.parametrize("remote, host, expected", [
    ("https://gitlab.example.com/acme/app.git", HOST, "acme/app"),
    ("https://gitlab.example.com/acme/team/app", HOST, "acme/team/app"),
    ("git@gitlab.example.com:acme/app.git", HOST, "acme/app"),
    ("ssh://git@gitlab.example.com:2222/acme/app.git", HOST, "acme/app"),
    ("https://git.example.com/gitlab/acme/app.git", "https://git.example.com/gitlab", "acme/app"),
    # Not this server, not a project path, or a GitLab route rather than a repo.
    ("https://github.com/acme/app.git", HOST, None),
    ("https://gitlab.example.com/app.git", HOST, None),
    ("https://gitlab.example.com/acme/app/-/tree/main", HOST, None),
    ("https://git.example.com/other/acme/app.git", "https://git.example.com/gitlab", None),
    ("", HOST, None),
])
def test_repo_path_for_remote(remote, host, expected):
    assert repo_path_for_remote(remote, host) == expected


# =============================================================================
# Sign-in
# =============================================================================


def test_login_saves_the_account_and_git_answers_with_the_token(env):
    service, _api, tmp = env
    result = service.login(TOKEN, "gitlab.example.com")
    assert result == {"success": True, "user": "octo", "host": HOST}
    assert service.is_authenticated()
    assert json.loads(service.account_file.read_text())["token"] == TOKEN

    # Real git, real helper: a credential lookup for this host gets the token.
    answer = subprocess.run(
        ["git", "credential", "fill"], input="protocol=https\nhost=gitlab.example.com\n\n",
        capture_output=True, text=True, timeout=30,
    )
    assert answer.returncode == 0, answer.stderr
    assert "username=octo" in answer.stdout
    assert f"password={TOKEN}" in answer.stdout


def test_helper_is_scoped_to_the_gitlab_host_and_survives_a_rejected_push(env):
    service, _api, _tmp = env
    service.login(TOKEN, HOST)
    helpers = git(".", "config", "--global", "--get-regexp", r"credential\..*\.helper")
    assert helpers.count("credential.https://gitlab.example.com.helper") == 2  # reset + ours
    assert "github" not in helpers
    # git calls "erase" after a rejected password; the saved token must stay.
    subprocess.run(
        ["git", "credential", "reject"], input=f"protocol=https\nhost=gitlab.example.com\nusername=octo\npassword={TOKEN}\n\n",
        capture_output=True, text=True, timeout=30,
    )
    assert TOKEN in service.credential_file.read_text()


def test_login_rejects_a_bad_token_and_saves_nothing(env):
    service, _api, _tmp = env
    result = service.login("glpat-wrong", HOST)
    assert result["success"] is False
    assert "rejected" in result["error"]
    assert not service.account_file.exists()
    assert not service.is_authenticated()


@pytest.mark.parametrize("token", ["", "   ", "two words"])
def test_login_validates_the_token_before_any_request(env, token):
    service, api, _tmp = env
    assert service.login(token, HOST)["success"] is False
    assert api.calls == []


def test_login_reports_an_unreachable_server(env):
    service, api, _tmp = env
    api.unreachable = True
    result = service.login(TOKEN, HOST)
    assert result["success"] is False and "Could not validate" in result["error"]


def test_logout_removes_the_token_and_only_our_git_helper(env):
    service, _api, _tmp = env
    git(".", "config", "--global", "credential.https://github.com.helper", "!gh auth git-credential")
    service.login(TOKEN, HOST)
    assert service.logout() == {"success": True}
    assert not service.account_file.exists() and not service.credential_file.exists()
    helpers = git(".", "config", "--global", "--get-regexp", r"credential\..*\.helper")
    assert "gitlab.example.com" not in helpers
    assert "gh auth git-credential" in helpers


def test_logout_keeps_a_helper_the_owner_set_for_the_same_host(env):
    service, _api, _tmp = env
    service.login(TOKEN, HOST)
    git(".", "config", "--global", "--add", "credential.https://gitlab.example.com.helper", "manager")
    service.logout()
    assert "manager" in git(".", "config", "--global", "--get-all", "credential.https://gitlab.example.com.helper")


def test_auth_info_flags_a_revoked_token(env):
    service, api, _tmp = env
    service.login(TOKEN, HOST)
    assert service.auth_info()["error"] is None
    api.valid_tokens.clear()
    info = service.auth_info()
    assert info["authenticated"] is True
    assert "no longer accepts" in info["error"]


# =============================================================================
# Merge requests
# =============================================================================


@pytest.fixture
def signed_in(env):
    service, api, tmp = env
    service.login(TOKEN, HOST)
    api.calls.clear()
    return service, api, tmp


def test_get_repo_from_remote_only_for_the_connected_server(signed_in):
    service, _api, tmp = signed_in
    repo = tmp / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", "git@gitlab.example.com:acme/app.git")
    assert service.get_repo_from_remote(str(repo)) == "acme/app"
    git(repo, "remote", "set-url", "origin", "https://github.com/acme/app.git")
    assert service.get_repo_from_remote(str(repo)) is None


def test_list_pulls_maps_states_and_filters_by_branch(signed_in):
    service, api, _tmp = signed_in
    api.route("GET", "/projects/acme%2Fapp/merge_requests", 200,
              [_mr(1, "opened"), _mr(2, "merged"), _mr(3, "closed", detailed_merge_status="conflict")])
    pulls = service.list_pulls("acme/app", state="all", head="feat/x")
    assert [(p.number, p.state, p.mergeable) for p in pulls] == [(1, "OPEN", True), (2, "MERGED", True), (3, "CLOSED", False)]
    _m, _p, params, _b = api.calls[-1]
    assert params["source_branch"] == "feat/x" and "state" not in params
    assert pulls[0].head_repo == "acme/app"


def test_fork_merge_requests_are_not_this_repos_branch(signed_in):
    service, api, _tmp = signed_in
    api.route("GET", "/projects/acme%2Fapp/merge_requests/9", 200, _mr(9, source_project_id=55))
    assert service.get_pull("acme/app", 9).head_repo == "fork:55"


def test_read_failures_raise_instead_of_answering_empty(signed_in):
    service, api, _tmp = signed_in
    api.route("GET", "/projects/acme%2Fapp/merge_requests", 500, {"message": "boom"})
    with pytest.raises(GitLabApiError):
        service.list_pulls("acme/app")
    assert service.get_pull("acme/app", 404) is None  # not found is an answer


def test_create_pull_marks_drafts_and_reuses_an_existing_mr(signed_in):
    service, api, _tmp = signed_in
    api.route("POST", "/projects/acme%2Fapp/merge_requests", 201, _mr(4, title="Draft: Add feature"))
    pr, error = service.create_pull("acme/app", "Add feature", "body", "feat/x", "main", draft=True)
    assert error is None and pr.number == 4
    assert api.calls[-1][3]["title"] == "Draft: Add feature"

    api.route("POST", "/projects/acme%2Fapp/merge_requests", 409,
              {"message": ["Another open merge request already exists for this source branch: !4"]})
    api.route("GET", "/projects/acme%2Fapp/merge_requests/4", 200, _mr(4))
    pr, error = service.create_pull("acme/app", "Add feature", "", "feat/x", "main")
    assert error is None and pr.number == 4


def test_merge_and_close(signed_in):
    service, api, _tmp = signed_in
    api.route("PUT", "/projects/acme%2Fapp/merge_requests/4/merge", 200, _mr(4, "merged"))
    assert service.merge_pull("acme/app", 4, "squash") == (True, None)
    assert api.calls[-1][3] == {"squash": True, "should_remove_source_branch": True}

    api.route("PUT", "/projects/acme%2Fapp/merge_requests/5/merge", 405, {"message": "405 Method Not Allowed"})
    api.route("GET", "/projects/acme%2Fapp/merge_requests/5", 200, _mr(5, "opened"))
    ok, error = service.merge_pull("acme/app", 5)
    assert ok is False and "!5" in error

    api.route("PUT", "/projects/acme%2Fapp/merge_requests/6", 200, _mr(6, "closed"))
    assert service.close_pull("acme/app", 6) == (True, None)
    assert api.calls[-1][3] == {"state_event": "close"}


def test_user_repos_use_https_clone_urls_and_cache(signed_in):
    service, api, _tmp = signed_in
    api.route("GET", "/projects", 200, [
        {"path_with_namespace": "acme/app", "path": "app", "namespace": {"full_path": "acme"},
         "visibility": "private", "http_url_to_repo": f"{HOST}/acme/app.git", "web_url": f"{HOST}/acme/app"},
        {"path_with_namespace": "acme/empty", "http_url_to_repo": ""},
    ])
    repos, truncated = service.list_user_repos()
    assert [r["full_name"] for r in repos] == ["acme/app"]
    assert repos[0]["clone_url"].startswith("https://") and TOKEN not in repos[0]["clone_url"]
    assert truncated is False
    service.list_user_repos()
    assert len([c for c in api.calls if c[1] == "/projects"]) == 1


def test_signed_out_requests_fail_as_unknown(env):
    service, _api, _tmp = env
    with pytest.raises(GitLabApiError):
        service.list_pulls("acme/app")


def test_account_file_with_garbage_is_not_connected(env):
    service, _api, _tmp = env
    service.account_file.parent.mkdir(parents=True)
    service.account_file.write_text("{not json")
    assert service.is_authenticated() is False
    Path(service.account_file).write_text(json.dumps({"host": HOST}))
    assert service.is_authenticated() is False

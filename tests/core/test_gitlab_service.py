"""GitLab service: sign-in, git credential wiring and merge requests.

GitLab's REST API is replaced by a small in-process fake (``FakeGitLab``);
git itself is real, pointed at a throwaway global config, so the credential
helper sign-in writes is checked by asking git for a credential.
"""

import json
import os
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
    # Same host name, another port: another server.
    ("https://git.example.com:8443/acme/app.git", "https://git.example.com:8443", "acme/app"),
    ("https://git.example.com:9443/acme/app.git", "https://git.example.com:8443", None),
    ("https://gitlab.example.com:443/acme/app.git", HOST, "acme/app"),
    ("https://gitlab.example.com:8443/acme/app.git", HOST, None),
    ("http://gitlab.example.com/acme/app.git", HOST, None),
    ("git@git.example.com:acme/app.git", "https://git.example.com:8443", "acme/app"),
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


def _fill(host: str, protocol: str = "https") -> subprocess.CompletedProcess:
    # An unanswered lookup fails instead of prompting.
    return subprocess.run(
        ["git", "-c", "credential.interactive=never", "credential", "fill"],
        input=f"protocol={protocol}\nhost={host}\n\n",
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
    )


def _global_entries() -> str:
    return git(".", "config", "--global", "--list", check=False)


def test_helper_is_scoped_to_the_gitlab_host_and_survives_a_rejected_push(env):
    service, _api, _tmp = env
    service.login(TOKEN, HOST)
    # The owner's global config gains one include line and nothing else.
    entries = _global_entries()
    assert entries.splitlines() == [f"include.path={service.git_config_file.as_posix()}"]
    helpers = git(".", "config", "--global", "--includes", "--get-regexp", r"credential\..*\.helper")
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


def test_logout_removes_the_token_and_only_our_git_config(env):
    service, _api, _tmp = env
    git(".", "config", "--global", "credential.https://github.com.helper", "!gh auth git-credential")
    service.login(TOKEN, HOST)
    credential = service.credential_file
    assert service.logout() == {"success": True}
    assert not service.account_file.exists() and not credential.exists()
    assert not service.git_config_file.exists()
    entries = _global_entries()
    assert "include.path" not in entries and "gitlab.example.com" not in entries
    assert "credential.https://github.com.helper=!gh auth git-credential" in entries


def test_connect_and_disconnect_leave_the_owners_helper_for_the_same_host(env):
    """Connecting never rewrites an entry the owner wrote; disconnecting
    leaves it exactly as it was, wherever it sits relative to ours."""
    service, _api, _tmp = env
    key = "credential.https://gitlab.example.com.helper"
    git(".", "config", "--global", "--add", key, "manager")
    service.login(TOKEN, HOST)
    git(".", "config", "--global", "--add", key, "store")  # added while connected
    service.logout()
    assert git(".", "config", "--global", "--get-all", key).splitlines() == ["manager", "store"]
    assert "include.path" not in _global_entries()


def test_a_helper_left_behind_cannot_answer_for_another_server(env):
    """Each server's token lives in its own file. Even with the include line
    still registered (the owner copied it, say), switching servers leaves no
    helper on the old server that would hand it the new server's token."""
    service, api, _tmp = env
    other = "https://code.example.org"
    api.extra_hosts.add(other)
    api.valid_tokens.add("glpat-other-0002")
    service.login(TOKEN, HOST)
    service.login("glpat-other-0002", other)
    assert service.load_account()["host"] == other
    # The old server gets nothing: no helper names it and its token file is gone.
    old = _fill("gitlab.example.com")
    assert "glpat-other-0002" not in old.stdout and TOKEN not in old.stdout
    new = _fill("code.example.org")
    assert "password=glpat-other-0002" in new.stdout
    assert len(list(service.config_dir.glob("git-credential-*"))) == 1


def _fill_url(url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "credential.interactive=never", "credential", "fill"],
        input=f"url={url}\n\n", capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
    )


def test_two_servers_under_paths_of_one_domain_keep_their_tokens_apart(env):
    """git matches the helper's path prefix, so switching from /old to /new
    on the same domain never hands /old the new server's token."""
    service, api, _tmp = env
    old, new = "https://git.example.com/old", "https://git.example.com/new"
    api.extra_hosts.update({old, new})
    api.valid_tokens.add("glpat-other-0002")
    service.login(TOKEN, old)
    assert f"password={TOKEN}" in _fill_url(f"{old}/acme/app.git").stdout
    service.login("glpat-other-0002", new)
    for url in (f"{old}/acme/app.git", "https://git.example.com/newer/acme/app.git"):
        answer = _fill_url(url).stdout
        assert "glpat-other-0002" not in answer and TOKEN not in answer
    assert "password=glpat-other-0002" in _fill_url(f"{new}/acme/app.git").stdout


def test_connect_without_git_reports_a_setup_error_and_disconnect_still_works(env, monkeypatch):
    service, _api, _tmp = env
    service.login(TOKEN, HOST)

    def no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(service, "_git_config", no_git)
    before = service.account_file.read_text()
    result = service.login(TOKEN, HOST)
    assert result["success"] is False and "could not be saved" in result["error"]
    assert service.account_file.read_text() == before
    assert service.logout() == {"success": True}
    assert not service.is_authenticated()


def test_failed_git_setup_keeps_the_previous_connection(env, monkeypatch):
    service, api, _tmp = env
    service.login(TOKEN, HOST)
    before = service.account_file.read_text()
    api.extra_hosts.add("https://code.example.org")
    api.valid_tokens.add("glpat-other-0002")

    def broken(*_args, **_kwargs):
        raise RuntimeError("git config failed")

    monkeypatch.setattr(service, "_register_include", broken)
    result = service.login("glpat-other-0002", "https://code.example.org")
    assert result["success"] is False
    # Settings still shows the old server and git still answers for it.
    assert service.account_file.read_text() == before
    assert f"password={TOKEN}" in _fill("gitlab.example.com").stdout
    assert len(list(service.config_dir.glob("git-credential-*"))) == 1


def test_failed_first_git_setup_saves_nothing(env, monkeypatch):
    service, _api, _tmp = env

    def broken(*_args, **_kwargs):
        raise RuntimeError("git config failed")

    monkeypatch.setattr(service, "_register_include", broken)
    assert service.login(TOKEN, HOST)["success"] is False
    assert not service.is_authenticated()
    assert not service.git_config_file.exists()
    assert list(service.config_dir.glob("git-credential-*")) == []


def test_a_pinned_view_refuses_a_server_changed_mid_operation(env):
    service, api, _tmp = env
    api.route("GET", "/projects/acme%2Fapp/merge_requests/4", 200, _mr(4))
    service.login(TOKEN, HOST)
    pinned = service.pinned()
    assert pinned.get_pull("acme/app", 4).number == 4
    api.extra_hosts.add("https://code.example.org")
    api.valid_tokens.add("glpat-other-0002")
    service.login("glpat-other-0002", "https://code.example.org")
    calls = len(api.calls)
    with pytest.raises(GitLabApiError, match="changed during this operation"):
        pinned.get_pull("acme/app", 4)
    assert len(api.calls) == calls  # nothing reached the new server
    # Its remote matching stays on the server it was bound to.
    assert pinned.host() == HOST
    assert service.get_pull("acme/app", 4).number == 4  # the shared service follows the switch


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


def test_a_branch_lookup_skips_a_forks_same_named_branch(signed_in):
    """A fork's MR from feat/x must not stand in for this repo's feat/x, even
    when GitLab lists it first and the caller asks for one result."""
    service, api, _tmp = signed_in
    api.route("GET", "/projects/acme%2Fapp/merge_requests", 200,
              [_mr(9, source_project_id=55), _mr(3)])
    pulls = service.list_pulls("acme/app", state="open", head="feat/x", limit=1)
    assert [(p.number, p.head_repo) for p in pulls] == [(3, "acme/app")]
    assert api.calls[-1][2]["per_page"] == 100  # the limit applies after filtering
    api.route("GET", "/projects/acme%2Fapp/merge_requests", 200, [_mr(9, source_project_id=55)])
    assert service.list_pulls("acme/app", state="open", head="feat/x", limit=1) == []


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

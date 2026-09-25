"""GitHubService argv and reply handling against a stub ``gh`` on PATH.

Complements tests/core/test_github_service.py (which mocks ``subprocess``):
here the real ``subprocess.run`` spawns ``tests/gh_stub.py``'s executable, so
the exact argv, stdin and exit-code handling are observed end to end.
"""

import pytest

from app.core.github_service import GitHubCliError, GitHubService
from tests.gh_stub import FakeGh, pr_json, run_json


@pytest.fixture
def gh(tmp_path, monkeypatch):
    return FakeGh(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return GitHubService()


@pytest.mark.parametrize("name", ["--source=.", "--template=acme/evil", "-h", ""])
def test_create_repo_refuses_option_like_names_without_running_gh(gh, svc, name):
    assert svc.create_repo(name) is None
    assert gh.calls() == []


def test_create_repo_argv(gh, svc):
    gh.on("repo", "create", stdout={"name": "app", "nameWithOwner": "acme/app",
                                    "url": "https://github.com/acme/app", "sshUrl": ""})

    info = svc.create_repo("app", private=True, description="--public")

    assert info == {"name": "app", "full_name": "acme/app",
                    "url": "https://github.com/acme/app", "clone_url": "https://github.com/acme/app"}
    assert gh.argvs("repo", "create") == [[
        "repo", "create", "app", "--private", "--description", "--public",
        "--json", "name,nameWithOwner,url,sshUrl",
    ]]


def test_create_repo_failure_and_bad_json_are_none(gh, svc):
    gh.on("repo", "create", code=1, stderr="exists")
    assert svc.create_repo("app") is None
    gh.on("repo", "create", stdout="not json")
    assert svc.create_repo("app") is None


def test_auth_status_is_cached_positive_and_refreshable(gh, svc):
    gh.signed_in(True)
    assert svc.is_authenticated() and svc.is_authenticated()
    assert len(gh.calls("auth")) == 1
    gh.signed_in(False)
    assert svc.is_authenticated() is True  # still cached
    assert svc.is_authenticated(refresh=True) is False
    assert len(gh.calls("auth")) == 2


@pytest.mark.parametrize("method", ["merge", "squash", "rebase"])
def test_merge_pull_argv(gh, svc, method):
    gh.on("pr", "merge")
    assert svc.merge_pull("acme/app", 4, method=method) == (True, None)
    assert gh.argvs("pr", "merge") == [["pr", "merge", "4", "--repo", "acme/app", f"--{method}", "--delete-branch"]]


def test_merge_and_close_failures_carry_stderr_or_a_default(gh, svc):
    gh.on("pr", "merge", code=1, stderr="  not mergeable  ")
    assert svc.merge_pull("acme/app", 4) == (False, "not mergeable")
    gh.on("pr", "merge", code=1)
    assert svc.merge_pull("acme/app", 4) == (False, "gh pr merge failed with no error output")
    gh.on("pr", "close", code=1)
    assert svc.close_pull("acme/app", 4) == (False, "gh pr close failed with no error output")


def test_create_pull_flattens_title_and_sends_body_on_stdin(gh, svc):
    gh.on("pr", "create", stdout="https://github.com/acme/app/pull/3\n")
    gh.on("pr", "view", "3", stdout=pr_json(3, "-feat"))

    pr, err = svc.create_pull("acme/app", "Line one\n  line two\n:wq", "Body\n:wq\n", "-feat", "main", draft=True)

    assert err is None and pr.number == 3 and pr.head_branch == "-feat"
    [call] = gh.calls("pr", "create")
    assert call["args"] == ["pr", "create", "--repo", "acme/app", "--title", "Line one line two",
                            "--body-file", "-", "--head", "-feat", "--base", "main", "--draft"]
    assert call["stdin"] == "Body"


def test_create_pull_without_url_or_readback(gh, svc):
    gh.on("pr", "create", stdout="done\n")
    assert svc.create_pull("acme/app", "t", "", "h", "main") == (
        None, "gh pr create succeeded but returned no PR URL")
    gh.on("pr", "create", stdout="https://github.com/acme/app/pull/3\n")
    gh.on("pr", "view", "3", code=1, stderr="HTTP 500")
    assert svc.create_pull("acme/app", "t", "", "h", "main") == (
        None, "PR #3 created but could not be fetched")


def test_run_reads_distinguish_missing_from_unreachable(gh, svc):
    gh.on("run", "list", stdout=[run_json(1)])
    assert [r.id for r in svc.list_runs("acme/app", limit=7)] == [1]
    assert gh.argvs("run", "list")[0][4:6] == ["--limit", "7"]

    gh.on("run", "view", "9", code=1, stderr="could not find any workflow run with ID 9")
    assert svc.get_run("acme/app", 9) is None
    gh.on("run", "view", "9", code=1, stderr="HTTP 502")
    with pytest.raises(GitHubCliError) as info:
        svc.get_run("acme/app", 9)
    assert info.value.stderr == "HTTP 502"
    gh.on("run", "view", "9", stdout="{")
    with pytest.raises(GitHubCliError):
        svc.get_run("acme/app", 9)

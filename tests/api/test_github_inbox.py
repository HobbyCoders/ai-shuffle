"""Cross-project pull request inbox: GET /api/v1/github/inbox."""

import json
import subprocess
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.api import github_inbox
from app.core.github_service import GitHubCliError, GitHubService, summarize_checks


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


class _Req:
    class state:  # noqa: N801 — mimics request.state
        pass


# ---------------------------------------------------------------------------
# summarize_checks / list_inbox_pulls
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rollup, expected", [
    (None, "none"),
    ([], "none"),
    ([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"state": "SUCCESS"}], "passing"),
    ([{"status": "COMPLETED", "conclusion": "SKIPPED"}, {"status": "COMPLETED", "conclusion": "NEUTRAL"}], "passing"),
    ([{"status": "IN_PROGRESS", "conclusion": ""}, {"status": "COMPLETED", "conclusion": "SUCCESS"}], "pending"),
    ([{"state": "PENDING"}], "pending"),
    ([{"status": "IN_PROGRESS"}, {"status": "COMPLETED", "conclusion": "FAILURE"}], "failing"),
    ([{"state": "ERROR"}], "failing"),
    ([{"status": "COMPLETED", "conclusion": "TIMED_OUT"}], "failing"),
])
def test_summarize_checks(rollup, expected):
    assert summarize_checks(rollup) == expected


def test_list_inbox_pulls_maps_state_review_and_checks():
    service = GitHubService()
    payload = [
        {"number": 7, "title": "Draft", "url": "u7", "author": {"login": "a"}, "headRefName": "f", "baseRefName": "main",
         "state": "OPEN", "isDraft": True, "reviewDecision": "", "statusCheckRollup": [], "updatedAt": "2026-10-01T00:00:00Z"},
        {"number": 8, "title": "Ready", "url": "u8", "author": None, "headRefName": "g", "baseRefName": "main",
         "state": "OPEN", "isDraft": False, "reviewDecision": "CHANGES_REQUESTED",
         "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}], "updatedAt": "2026-10-02T00:00:00Z"},
        {"number": 9, "title": "Done", "url": "u9", "author": {"login": "b"}, "headRefName": "h", "baseRefName": "main",
         "state": "MERGED", "isDraft": False, "reviewDecision": "APPROVED",
         "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}], "updatedAt": "2026-09-30T00:00:00Z"},
    ]
    with patch.object(service, "_run_gh", return_value=_completed(json.dumps(payload))) as run:
        pulls = service.list_inbox_pulls("o/r", "all", 5)

    args = run.call_args[0][0]
    assert args[:4] == ["pr", "list", "--repo", "o/r"]
    assert "--state" in args and args[args.index("--state") + 1] == "all"
    assert "reviewDecision" in args[args.index("--json") + 1]
    assert [(p["number"], p["state"], p["review"], p["checks"]) for p in pulls] == [
        (7, "draft", "none", "none"),
        (8, "open", "changes_requested", "failing"),
        (9, "merged", "approved", "passing"),
    ]
    assert pulls[1]["author"] == "unknown"


def test_list_inbox_pulls_raises_when_gh_fails():
    service = GitHubService()
    with patch.object(service, "_run_gh", return_value=_completed("", 1, "HTTP 404")):
        with pytest.raises(GitHubCliError):
            service.list_inbox_pulls("o/r")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

def _pr(number: int, updated: str) -> dict:
    return {"number": number, "title": f"PR {number}", "url": f"https://github.com/x/pull/{number}", "author": "me",
            "head_branch": "f", "base_branch": "main", "state": "open", "review": "none", "checks": "passing",
            "updated_at": updated}


@pytest.fixture
def projects(tmp_path):
    folders = {}
    for name in ("alpha", "alpha-worktree", "beta", "notgit"):
        folders[name] = tmp_path / name
        folders[name].mkdir()
    rows = [
        {"id": "p-alpha", "name": "Alpha", "path": str(folders["alpha"])},
        {"id": "p-alpha2", "name": "Alpha copy", "path": str(folders["alpha-worktree"])},
        {"id": "p-beta", "name": "Beta", "path": str(folders["beta"])},
        {"id": "p-notgit", "name": "Scratch", "path": str(folders["notgit"])},
        {"id": "p-gone", "name": "Elsewhere", "path": str(tmp_path / "missing")},
    ]
    remotes = {
        str(folders["alpha"]): "o/alpha",
        str(folders["alpha-worktree"]): "o/alpha",
        str(folders["beta"]): "o/beta",
    }
    return rows, remotes


async def _call(rows, remotes, pulls_by_repo, *, api_user=None, state="open"):
    seen_project_ids = []

    def load(api_user_project_id):
        seen_project_ids.append(api_user_project_id)
        return [r for r in rows if not api_user_project_id or r["id"] == api_user_project_id], {}

    def list_pulls(repo, s, limit):
        result = pulls_by_repo[repo]
        if isinstance(result, Exception):
            raise result
        return result

    request = _Req()
    if api_user:
        request.state.api_user = api_user
    with patch("app.api.projects._load_projects_for_listing", side_effect=load), \
         patch.object(github_inbox, "get_project_working_dir", side_effect=lambda p: p["path"]), \
         patch.object(github_inbox.github_service, "get_repo_from_remote", side_effect=lambda path: remotes.get(path)), \
         patch.object(github_inbox.github_service, "list_inbox_pulls", side_effect=list_pulls) as gh, \
         patch.object(github_inbox, "check_github_auth", return_value=None):
        result = await github_inbox.pull_request_inbox(request, state=state, limit=30, token="t")
    return result, gh, seen_project_ids


@pytest.mark.asyncio
async def test_inbox_merges_repos_across_projects_and_sorts_by_activity(projects):
    rows, remotes = projects
    result, gh, _ = await _call(rows, remotes, {
        "o/alpha": [_pr(1, "2026-10-01T00:00:00Z")],
        "o/beta": [_pr(2, "2026-10-02T00:00:00Z")],
    })

    # One gh call per repository, even though two projects share o/alpha.
    assert sorted(c.args[0] for c in gh.call_args_list) == ["o/alpha", "o/beta"]
    assert [(i.repo, i.number) for i in result.items] == [("o/beta", 2), ("o/alpha", 1)]
    assert [p.name for p in result.items[1].projects] == ["Alpha", "Alpha copy"]
    assert {(s.project_id, s.reason) for s in result.skipped} == {
        ("p-notgit", "No GitHub remote"),
        ("p-gone", "Project folder is not on this machine"),
    }
    assert result.repos == ["o/alpha", "o/beta"]


@pytest.mark.asyncio
async def test_one_unreadable_repo_does_not_sink_the_inbox(projects):
    rows, remotes = projects
    result, _, _ = await _call(rows, remotes, {
        "o/alpha": GitHubCliError("boom", stderr="HTTP 403"),
        "o/beta": [_pr(2, "2026-10-02T00:00:00Z")],
    })
    assert [i.number for i in result.items] == [2]
    assert any(s.repo == "o/alpha" and "HTTP 403" in s.reason for s in result.skipped)


@pytest.mark.asyncio
async def test_api_user_only_sees_its_own_project(projects):
    rows, remotes = projects
    result, gh, seen = await _call(rows, remotes, {"o/beta": [_pr(2, "2026-10-02T00:00:00Z")]},
                                   api_user={"id": "u", "project_id": "p-beta"})
    assert seen == ["p-beta"]
    assert [c.args[0] for c in gh.call_args_list] == ["o/beta"]
    assert [p.id for p in result.items[0].projects] == ["p-beta"]


@pytest.mark.asyncio
async def test_inbox_requires_github_auth():
    request = _Req()
    with patch.object(github_inbox, "check_github_auth", side_effect=HTTPException(status_code=401, detail="no")):
        with pytest.raises(HTTPException) as exc:
            await github_inbox.pull_request_inbox(request, state="open", limit=30, token="t")
    assert exc.value.status_code == 401

"""GET /api/v1/github/repos: the connected account's repositories (clone picker).

``github_service`` is not mocked: a stub ``gh`` on PATH (tests/gh_stub.py)
answers, so the argv sent to gh, the paging walk, the cache and the mapping
of gh's replies to the response are all exercised for real. No GitHub API is
reached.
"""

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth as auth_api
from app.api import github_repos
from app.core import github_service as gs
from app.core.github_service import github_service, user_repos_endpoint
from tests.gh_stub import FakeGh

URL = "/api/v1/github/repos"
# Shaped like a real token so the redaction patterns must catch it.
SECRET = "ghp_" + "A1b2C3d4E5" * 4


def _repo(full_name, *, private=False, updated="2026-01-01T00:00:00Z", **extra):
    owner, name = full_name.split("/")
    return {
        "full_name": full_name, "name": name, "owner": owner, "private": private,
        "fork": False, "archived": False, "description": f"about {name}",
        "clone_url": f"https://github.com/{full_name}.git",
        "html_url": f"https://github.com/{full_name}",
        "default_branch": "main", "updated_at": updated, **extra,
    }


def _ndjson(repos):
    # gh api --jq '.[] | {...}' prints one compact object per line.
    return "".join(json.dumps(r) + "\n" for r in repos)


@pytest.fixture
def env(tmp_path, monkeypatch):
    gh = FakeGh(tmp_path, monkeypatch).signed_in()
    github_service.invalidate_auth_cache()
    github_service.invalidate_repos_cache()
    app = FastAPI()
    app.include_router(github_repos.router)
    app.dependency_overrides[auth_api.require_admin] = lambda: "owner"
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, gh=gh)
    github_service.invalidate_auth_cache()
    github_service.invalidate_repos_cache()


def _serve_page(gh, page, repos, **kw):
    gh.on("api", "--hostname", "github.com", user_repos_endpoint(page), stdout=_ndjson(repos), **kw)


def _api_calls(gh):
    return gh.argvs("api")


# =============================================================================
# Auth and connection
# =============================================================================


def test_api_keys_and_anonymous_callers_are_refused(client, mock_db, tmp_path, monkeypatch):
    """Owner-only: an API user gets 403 and gh is never asked."""
    from app.api.auth import hash_api_key
    from app.db import database

    gh = FakeGh(tmp_path, monkeypatch).signed_in()
    database.create_api_user("repo-user", "Repo User", hash_api_key("aih_repo_user_key"))
    response = client.get(URL, headers={"Authorization": "Bearer aih_repo_user_key"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Owner access required"
    assert gh.argvs() == []


def test_not_connected_is_reported_not_raised(env):
    env.gh.signed_in(False)
    response = env.client.get(URL)
    assert response.status_code == 200
    assert response.json() == {
        "connected": False, "items": [], "total": 0, "page": 1, "per_page": 30,
        "has_more": False, "truncated": False,
    }
    assert _api_calls(env.gh) == []


# =============================================================================
# Listing, search and paging
# =============================================================================


def test_lists_repos_newest_first_with_https_clone_urls(env):
    _serve_page(env.gh, 1, [
        _repo("me/old", updated="2025-01-01T00:00:00Z"),
        _repo("acme/private-app", private=True, updated="2026-05-01T00:00:00Z"),
        _repo("friend/shared", updated="2026-03-01T00:00:00Z"),
    ])
    data = env.client.get(URL).json()
    assert data["connected"] is True and data["total"] == 3 and data["has_more"] is False
    assert [r["full_name"] for r in data["items"]] == ["acme/private-app", "friend/shared", "me/old"]
    first = data["items"][0]
    assert first["private"] is True and first["owner"] == "acme" and first["name"] == "private-app"
    assert first["clone_url"] == "https://github.com/acme/private-app.git"
    assert first["updated_at"] == "2026-05-01T00:00:00Z"

    (argv,) = _api_calls(env.gh)
    assert argv[:4] == ["api", "--hostname", "github.com", user_repos_endpoint(1)]
    assert "affiliation=owner,collaborator,organization_member" in argv[3]
    assert argv[4] == "--jq"


def test_server_side_search_and_pages_use_the_cached_list(env):
    _serve_page(env.gh, 1, [
        _repo(f"acme/svc-{i}", updated=f"2026-01-{i + 1:02d}T00:00:00Z") for i in range(5)
    ] + [_repo("me/website")])

    page1 = env.client.get(URL, params={"per_page": 2}).json()
    assert page1["total"] == 6 and page1["has_more"] is True
    assert [r["full_name"] for r in page1["items"]] == ["acme/svc-4", "acme/svc-3"]
    page3 = env.client.get(URL, params={"per_page": 2, "page": 3}).json()
    assert page3["has_more"] is False and len(page3["items"]) == 2

    found = env.client.get(URL, params={"q": "ACME svc-1"}).json()
    assert [r["full_name"] for r in found["items"]] == ["acme/svc-1"] and found["total"] == 1
    # Description text matches too.
    assert env.client.get(URL, params={"q": "about website"}).json()["total"] == 1
    assert env.client.get(URL, params={"q": "nothing-like-this"}).json()["items"] == []

    # One gh call served every page and search above.
    assert len(_api_calls(env.gh)) == 1
    assert len(env.gh.calls("auth", "status")) == 1


def test_refresh_refetches(env):
    _serve_page(env.gh, 1, [_repo("me/a")])
    env.client.get(URL)
    _serve_page(env.gh, 1, [_repo("me/a"), _repo("me/new")])
    assert env.client.get(URL).json()["total"] == 1
    assert env.client.get(URL, params={"refresh": "true"}).json()["total"] == 2
    assert len(_api_calls(env.gh)) == 2


def test_walks_github_pages_until_a_short_one(env, monkeypatch):
    monkeypatch.setattr(gs, "USER_REPOS_PAGE_SIZE", 2)
    _serve_page(env.gh, 1, [_repo("me/a"), _repo("me/b")])
    _serve_page(env.gh, 2, [_repo("me/c"), _repo("me/a")])  # a duplicate across pages
    _serve_page(env.gh, 3, [_repo("me/d")])
    data = env.client.get(URL).json()
    assert sorted(r["full_name"] for r in data["items"]) == ["me/a", "me/b", "me/c", "me/d"]
    assert data["truncated"] is False
    assert [a[3] for a in _api_calls(env.gh)] == [user_repos_endpoint(p) for p in (1, 2, 3)]


def test_stops_at_the_page_cap_and_says_so(env, monkeypatch):
    monkeypatch.setattr(gs, "USER_REPOS_PAGE_SIZE", 1)
    monkeypatch.setattr(gs, "USER_REPOS_MAX_PAGES", 2)
    for page in (1, 2, 3):
        _serve_page(env.gh, page, [_repo(f"me/r{page}")])
    data = env.client.get(URL).json()
    assert data["total"] == 2 and data["truncated"] is True
    assert len(_api_calls(env.gh)) == 2


# =============================================================================
# Failures and secrets
# =============================================================================


def test_gh_failure_is_502_redacted_and_not_cached(env, caplog):
    _serve_page(env.gh, 1, [], code=1, stderr=f"HTTP 401: Bad credentials (token {SECRET})")
    with caplog.at_level(logging.DEBUG):
        response = env.client.get(URL)
    assert response.status_code == 502
    assert response.json()["detail"].startswith("GitHub could not list your repositories: HTTP 401")
    assert SECRET not in response.text
    assert SECRET not in caplog.text

    _serve_page(env.gh, 1, [_repo("me/a")])
    assert env.client.get(URL).json()["total"] == 1


def test_invalid_output_is_502(env):
    env.gh.on("api", "--hostname", "github.com", user_repos_endpoint(1), stdout="not json\n")
    assert env.client.get(URL).status_code == 502


def test_only_listed_fields_leave_the_server(env, caplog):
    # The REST repo object carries temp_clone_token for private repos; even if
    # one reached us, the response model must not echo it.
    _serve_page(env.gh, 1, [_repo("me/secret", private=True, temp_clone_token=SECRET,
                                  permissions={"admin": True})])
    with caplog.at_level(logging.DEBUG):
        response = env.client.get(URL)
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert set(item) == {"full_name", "name", "owner", "private", "fork", "archived", "description",
                         "clone_url", "html_url", "default_branch", "updated_at"}
    assert SECRET not in response.text and SECRET not in caplog.text
    # Nothing secret-shaped on the gh command line either.
    assert all(SECRET not in arg for argv in env.gh.argvs() for arg in argv)


def test_sign_in_changes_drop_the_cached_list_but_status_reads_keep_it(env):
    _serve_page(env.gh, 1, [_repo("me/a")])
    env.client.get(URL)
    auth_api._invalidate_github_auth_caches(True, account_changed=False)
    env.client.get(URL)
    assert len(_api_calls(env.gh)) == 1
    auth_api._invalidate_github_auth_caches(True)  # login / logout path
    env.client.get(URL)
    assert len(_api_calls(env.gh)) == 2

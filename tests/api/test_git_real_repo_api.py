"""Git API routes end to end against real temporary repositories.

The router runs in a bare FastAPI app with auth overridden; the project row
points at a real repository under ``tmp_path`` and GitService is NOT mocked,
so these tests pin the response shapes and status codes the docs promise
(docs/content/api-reference/miscellaneous.md, "Git Integration") on real git
behaviour. Each test runs once with the pygit2 reader and once with the CLI
fallback.
"""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import git as git_api
from app.api import git_helpers, git_worktrees
from app.core import config
from app.core.git_native import HAS_PYGIT2
from app.db import database
from tests.git_repo_helpers import (
    auth_required_http_remote,
    clone,
    commit_file,
    git,
    hermetic_git_env,
    init_repo,
    make_origin,
    non_utf8_name_supported,
    use_backend,
    write,
)

BASE = "/api/v1/projects/p1/git"
STATUS_FIELDS = {
    "is_git_repo", "current_branch", "is_detached", "head_commit", "remote_url",
    "is_clean", "staged", "modified", "untracked", "ahead", "behind",
    "differs_from_base", "matches_base_tree", "unpushed", "conflicts",
    "repository_id", "default_branch", "github_repo_name", "last_synced_at",
}


@pytest.fixture(params=[
    pytest.param("native", marks=pytest.mark.skipif(not HAS_PYGIT2, reason="pygit2 unavailable")),
    "cli",
])
def api(request, mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    use_backend(monkeypatch, request.param)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    repo = init_repo(
        tmp_path / "repo", {"a.txt": "a\n", "b.txt": "b\n", "src/app.py": "print(1)\n"}
    )
    database.create_project("p1", "Repo", None, str(repo))
    # Repository-record discovery is not under test here.
    monkeypatch.setattr(git_api, "peek_repo_record_cache", lambda _pid: None)
    git_helpers.invalidate_working_dir_cache()
    git_worktrees._worktree_status_cache.clear()

    app = FastAPI()
    app.include_router(git_api.router)
    app.dependency_overrides[git_api.require_auth] = lambda: "test-token"
    with TestClient(app) as client:
        yield client, repo, tmp_path
    git_helpers.invalidate_working_dir_cache()
    git_worktrees._worktree_status_cache.clear()


def _status(client):
    response = client.get(f"{BASE}/status")
    assert response.status_code == 200, response.text
    return response.json()


def _refresh(repo):
    git_api.git_service.invalidate_status_cache(str(repo))


# =============================================================================
# Status, changed files, diffs
# =============================================================================


def test_status_shape_and_index_codes(api):
    client, repo, _ = api
    write(repo / "a.txt", "a\nb\n")
    git(repo, "add", "a.txt")
    write(repo / "new.txt", "n\n")
    git(repo, "add", "new.txt")
    git(repo, "rm", "-q", "--cached", "b.txt")
    write(repo / "loose.txt", "l\n")

    data = _status(client)

    assert set(data) == STATUS_FIELDS
    assert data["is_git_repo"] is True and data["current_branch"] == "main"
    assert {(s["file"], s["status"]) for s in data["staged"]} == {
        ("a.txt", "M"), ("new.txt", "A"), ("b.txt", "D"),
    }
    # The index code set the docs promise: A, M, D, R or C.
    assert all(s["status"] in {"A", "M", "D", "R", "C"} for s in data["staged"])
    assert set(data["untracked"]) == {"loose.txt", "b.txt"}
    assert data["is_clean"] is False
    assert data["repository_id"] is None


def test_status_detached_empty_and_non_repo(api, mock_db):
    client, repo, tmp_path = api
    git(repo, "checkout", "-q", "--detach")
    data = _status(client)
    assert data["is_detached"] is True and data["current_branch"] is None
    assert len(data["head_commit"]) == 12

    empty = init_repo(tmp_path / "empty", commit=False)
    write(empty / "x.txt", "x\n")
    database.create_project("p-empty", "Empty", None, str(empty))
    data = client.get("/api/v1/projects/p-empty/git/status").json()
    assert data["is_git_repo"] is True
    assert data["current_branch"] is None and data["head_commit"] is None
    assert data["untracked"] == ["x.txt"]

    plain = tmp_path / "plain"
    plain.mkdir()
    database.create_project("p-plain", "Plain", None, str(plain))
    data = client.get("/api/v1/projects/p-plain/git/status").json()
    assert data["is_git_repo"] is False
    assert client.get("/api/v1/projects/p-plain/git/branches").status_code == 400


def test_status_survives_non_utf8_file_name(api):
    """Regression: one undecodable name made /git/status a 500 (pygit2)."""
    client, repo, _ = api
    if not non_utf8_name_supported(repo):
        pytest.skip("filesystem rejects non-UTF-8 names")
    with open(os.path.join(os.fsencode(repo), b"bad\xff.txt"), "wb") as fh:
        fh.write(b"x\n")

    data = _status(client)
    assert data["untracked"] == ["bad�.txt"]
    files = client.get(f"{BASE}/changed-files").json()["files"]
    assert [f["file"] for f in files] == ["bad�.txt"]


def test_changed_files_shape_and_totals(api):
    client, repo, _ = api
    commit_file(repo, "c.txt", "".join(f"{i}\n" for i in range(10)), "ten lines")
    write(repo / "a.txt", "a\nstaged\n")
    git(repo, "add", "a.txt")
    write(repo / "a.txt", "a\nstaged\nunstaged\n")
    write(repo / "c.txt", "only line\n")
    write(repo / "img.bin", b"\x00\x01")
    git(repo, "add", "img.bin")
    write(repo / "loose.txt", "l\n")

    response = client.get(f"{BASE}/changed-files")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"files", "total_additions", "total_deletions"}
    rows = {(f["file"], f["category"]): f for f in body["files"]}
    assert set(rows) == {
        ("a.txt", "staged"), ("img.bin", "staged"),
        ("a.txt", "modified"), ("c.txt", "modified"), ("loose.txt", "untracked"),
    }
    for row in body["files"]:
        assert set(row) == {"file", "status", "category", "additions", "deletions", "is_binary"}
    assert rows[("img.bin", "staged")]["is_binary"] is True
    assert rows[("img.bin", "staged")]["status"] == "A"
    assert rows[("c.txt", "modified")]["deletions"] == 10
    assert rows[("loose.txt", "untracked")]["status"] == "?"
    # a.txt's staged and unstaged stats are merged per file, then reported for
    # both of its rows.
    assert rows[("a.txt", "staged")]["additions"] == 2
    assert body["total_additions"] == 2 + 2 + 1
    assert body["total_deletions"] == 10


def test_changed_files_rename_and_conflict(api):
    client, repo, _ = api
    git(repo, "mv", "a.txt", "moved.txt")
    rows = client.get(f"{BASE}/changed-files").json()["files"]
    staged = {(r["file"], r["status"]) for r in rows if r["category"] == "staged"}
    assert ("moved.txt", "R") in staged or ("moved.txt", "A") in staged
    assert not any(" -> " in r["file"] or "=>" in r["file"] for r in rows)
    git(repo, "commit", "-q", "-m", "rename")

    git(repo, "checkout", "-q", "-b", "other")
    commit_file(repo, "moved.txt", "other\n", "other")
    git(repo, "checkout", "-q", "main")
    commit_file(repo, "moved.txt", "main\n", "main")
    git(repo, "merge", "other", check=False)
    _refresh(repo)
    rows = client.get(f"{BASE}/changed-files").json()["files"]
    assert [(r["file"], r["status"], r["category"]) for r in rows] == [("moved.txt", "U", "conflict")]


def test_changed_files_for_non_repo_is_empty(api, mock_db):
    client, _, tmp_path = api
    plain = tmp_path / "plain"
    plain.mkdir()
    database.create_project("p-plain", "Plain", None, str(plain))
    body = client.get("/api/v1/projects/p-plain/git/changed-files").json()
    assert body == {"files": [], "total_additions": 0, "total_deletions": 0}


def test_file_diff_shapes(api):
    client, repo, tmp_path = api
    write(repo / "a.txt", "a\nmore\n")
    write(repo / "blob.bin", b"\x00" * 64)
    (tmp_path / "secret.txt").write_text("TOP SECRET\n")

    text = client.post(f"{BASE}/file-diff", json={"file_path": "a.txt"}).json()
    binary = client.post(f"{BASE}/file-diff", json={"file_path": "blob.bin"}).json()
    escape = client.post(f"{BASE}/file-diff", json={"file_path": "../secret.txt"}).json()
    staged = client.post(f"{BASE}/file-diff", json={"file_path": "a.txt", "staged": True}).json()

    assert set(text) == {"file", "diff", "is_binary", "truncated"}
    assert text["file"] == "a.txt" and "+more" in text["diff"]
    assert text["is_binary"] is False and text["truncated"] is False
    assert binary["is_binary"] is True
    assert "TOP SECRET" not in escape["diff"]
    assert staged["diff"] == ""
    assert client.post(f"{BASE}/file-diff", json={"file_path": ""}).status_code == 422


def test_diff_stats(api):
    client, repo, _ = api
    write(repo / "a.txt", "a\nb\nc\n")
    git(repo, "add", "a.txt")
    write(repo / "src/app.py", "")

    both = client.get(f"{BASE}/diff-stats").json()
    staged = client.get(f"{BASE}/diff-stats", params={"include_unstaged": "false"}).json()

    assert both == {"additions": 2, "deletions": 1, "files_changed": 2}
    assert staged == {"additions": 2, "deletions": 0, "files_changed": 1}


# =============================================================================
# Branches
# =============================================================================


def test_list_and_create_branch_with_start_point(api):
    client, repo, _ = api
    first = git(repo, "rev-parse", "HEAD")
    commit_file(repo, "second.txt", "2\n", "second")

    created = client.post(f"{BASE}/branches", json={"name": "feature/x", "start_point": first})
    assert created.status_code == 200
    assert created.json() == {
        "success": True, "message": "Created branch 'feature/x'", "status": None, "diff_stats": None,
    }
    assert git(repo, "rev-parse", "feature/x") == first

    body = client.get(f"{BASE}/branches").json()
    assert body["current_branch"] == "main"
    by_name = {b["name"]: b for b in body["branches"]}
    assert set(by_name) == {"main", "feature/x"}
    assert set(by_name["main"]) == {
        "name", "is_current", "is_remote", "commit", "commit_message", "upstream",
        "ahead", "behind", "has_worktree", "worktree_id",
    }
    assert by_name["main"]["is_current"] is True and by_name["main"]["has_worktree"] is False
    assert by_name["feature/x"]["commit"] == first[:7]


@pytest.mark.parametrize("name", ["has space", "a..b", "x~1", "y^", "c:d", "q?", "s*", "[x", "e@{1}"])
def test_create_branch_rejects_invalid_names(api, name):
    client, repo, _ = api
    response = client.post(f"{BASE}/branches", json={"name": name})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("Invalid branch name")
    assert git(repo, "branch", "--list") == "* main"


def test_create_branch_duplicate_or_bad_start_point_is_400(api):
    client, repo, _ = api
    assert client.post(f"{BASE}/branches", json={"name": "dup"}).status_code == 200
    for body in ({"name": "dup"}, {"name": "other", "start_point": "nope"}):
        response = client.post(f"{BASE}/branches", json=body)
        assert response.status_code == 400
        assert "may already exist or start point is invalid" in response.json()["detail"]


def _assert_current_branch_delete_refused(client, repo):
    response = client.delete(f"{BASE}/branches/main", params={"force": "true"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Cannot delete the currently checked out branch"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_delete_current_branch_is_refused(api):
    client, repo, _ = api
    _assert_current_branch_delete_refused(client, repo)


def test_current_branch_guard_bites_when_disabled(api, monkeypatch):
    """Without the route's guard, git's own refusal surfaces a different 400."""
    client, repo, _ = api
    monkeypatch.setattr(git_api.git_service, "get_current_branch", lambda _wd: None)
    with pytest.raises(AssertionError):
        _assert_current_branch_delete_refused(client, repo)


def test_delete_branch_force_and_nested_names(api):
    client, repo, _ = api
    git(repo, "checkout", "-q", "-b", "feature/deep/work")
    commit_file(repo, "w.txt", "w\n", "unmerged")
    git(repo, "checkout", "-q", "main")

    refused = client.delete(f"{BASE}/branches/feature/deep/work")
    assert refused.status_code == 400
    assert "use force=true" in refused.json()["detail"]

    forced = client.delete(f"{BASE}/branches/feature/deep/work", params={"force": "true"})
    assert forced.status_code == 200
    assert forced.json()["message"] == "Deleted branch 'feature/deep/work'"
    assert git(repo, "branch", "--list", "feature/deep/work") == ""
    assert client.delete(f"{BASE}/branches/ghost").status_code == 400


# =============================================================================
# Checkout
# =============================================================================


def _assert_dirty_checkout_is_409(client, repo):
    response = client.post(f"{BASE}/checkout", json={"ref": "other"})
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Cannot checkout: you have uncommitted changes. Commit or stash them first."
    )
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


@pytest.fixture
def dirty_with_other_branch(api):
    client, repo, _ = api
    git(repo, "branch", "other")
    write(repo / "a.txt", "local edit\n")
    return client, repo


def test_checkout_with_uncommitted_changes_is_409(dirty_with_other_branch):
    _assert_dirty_checkout_is_409(*dirty_with_other_branch)


def test_dirty_checkout_guard_bites_when_disabled(dirty_with_other_branch, monkeypatch):
    """Report the tree as clean and the non-conflicting edit is carried across."""
    client, repo = dirty_with_other_branch
    monkeypatch.setattr(
        git_api.git_service, "get_status",
        lambda _wd: {"staged": [], "modified": [], "conflicts": []},
    )
    with pytest.raises(AssertionError):
        _assert_dirty_checkout_is_409(client, repo)


def test_checkout_auto_stash(dirty_with_other_branch):
    client, repo = dirty_with_other_branch

    response = client.post(f"{BASE}/checkout", json={"ref": "other", "auto_stash": True})

    assert response.status_code == 200
    assert response.json()["message"] == "Stashed changes and checked out 'other'"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "other"
    assert (repo / "a.txt").read_text() == "a\n"
    assert "auto-stash before checkout other" in git(repo, "stash", "list")


def test_checkout_auto_stash_failure_is_409(dirty_with_other_branch, monkeypatch):
    client, repo = dirty_with_other_branch
    monkeypatch.setattr(git_api.git_service, "stash_push", lambda *a, **k: False)
    response = client.post(f"{BASE}/checkout", json={"ref": "other", "auto_stash": True})
    assert response.status_code == 409
    assert response.json()["detail"] == "Cannot checkout: failed to stash your uncommitted changes."


def test_checkout_clean_untracked_missing_and_worktree(api):
    client, repo, _ = api
    git(repo, "branch", "other")
    write(repo / "loose.txt", "untracked files do not block\n")

    ok = client.post(f"{BASE}/checkout", json={"ref": "other"})
    assert ok.status_code == 200 and ok.json()["message"] == "Checked out 'other'"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "other"

    missing = client.post(f"{BASE}/checkout", json={"ref": "nope"})
    assert missing.status_code == 400
    assert missing.json()["detail"] == "Failed to checkout 'nope'. It may not exist."

    in_worktree = client.post(
        f"{BASE}/checkout", params={"worktree_id": "wt-1"}, json={"ref": "main"},
    )
    assert in_worktree.status_code == 400
    assert "Worktrees are bound to their branch" in in_worktree.json()["detail"]


# =============================================================================
# Fetch / pull / push error mapping
# =============================================================================


def test_fetch_without_remote_is_502(api):
    client, _, _ = api
    response = client.post(f"{BASE}/fetch")
    assert response.status_code == 502
    assert response.json()["detail"] == (
        "Failed to fetch from remote 'origin'. Check network connection and authentication."
    )


def test_fetch_invalid_remote_name_is_400(api):
    client, _, _ = api
    response = client.post(f"{BASE}/fetch", params={"remote": "--upload-pack=touch x"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid remote name"


def test_fetch_success_returns_inline_status(api):
    client, repo, tmp_path = api
    bare = make_origin(tmp_path, repo)
    other = clone(tmp_path, bare, "other")
    commit_file(other, "o.txt", "o\n", "remote")
    git(other, "push", "-q", "origin", "main")

    response = client.post(f"{BASE}/fetch")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True and body["message"] == "Fetched from 'origin'"
    assert body["status"]["behind"] == 1


def test_auth_failure_maps_to_502_fetch_400_push_and_failed_pull(api):
    client, repo, _ = api
    with auth_required_http_remote() as url:
        git(repo, "remote", "add", "origin", url)

        fetch = client.post(f"{BASE}/fetch")
        push = client.post(f"{BASE}/push", json={})
        pull = client.post(f"{BASE}/pull", json={})

    assert fetch.status_code == 502
    assert push.status_code == 400
    assert "could not read Username" in push.json()["detail"] or (
        "Authentication failed" in push.json()["detail"]
    )
    assert pull.status_code == 200
    assert pull.json() == {
        "success": False, "message": "Failed to fetch from 'origin'",
        "conflicts": [], "has_conflicts": False, "status": None, "diff_stats": None,
    }


def test_push_without_remote_is_400_with_git_reason(api):
    client, _, _ = api
    response = client.post(f"{BASE}/push", json={})
    assert response.status_code == 400
    assert "origin" in response.json()["detail"]


def test_push_detached_head_needs_a_branch(api):
    client, repo, _ = api
    git(repo, "checkout", "-q", "--detach")
    response = client.post(f"{BASE}/push", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "Could not determine current branch"


def test_push_success_sets_upstream(api):
    client, repo, tmp_path = api
    bare = make_origin(tmp_path, repo)
    git(repo, "checkout", "-q", "-b", "feat")
    commit_file(repo, "f.txt", "f\n", "feat")

    response = client.post(f"{BASE}/push", json={"set_upstream": True})

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Pushed to origin/feat"
    assert body["status"]["unpushed"] == 0
    assert git(bare, "rev-parse", "feat") == git(repo, "rev-parse", "HEAD")


def test_pull_with_uncommitted_changes_is_409(api):
    client, repo, _ = api
    write(repo / "loose.txt", "l\n")
    response = client.post(f"{BASE}/pull", json={})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("Cannot pull: you have uncommitted changes")


def test_pull_success_and_conflict(api):
    client, repo, tmp_path = api
    bare = make_origin(tmp_path, repo)
    other = clone(tmp_path, bare, "other")
    commit_file(other, "o.txt", "o\n", "remote")
    git(other, "push", "-q", "origin", "main")

    ok = client.post(f"{BASE}/pull", json={"strategy": "rebase"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["success"] is True and body["message"] == "Pulled from origin using rebase"
    assert body["status"]["behind"] == 0 and body["diff_stats"]["files_changed"] == 0

    commit_file(other, "a.txt", "theirs\n", "remote a")
    git(other, "push", "-q", "origin", "main")
    commit_file(repo, "a.txt", "ours\n", "local a")
    conflict = client.post(f"{BASE}/pull", json={})
    assert conflict.status_code == 200
    assert conflict.json()["success"] is False
    assert conflict.json()["has_conflicts"] is True
    assert conflict.json()["conflicts"] == ["a.txt"]
    assert git(repo, "status", "--porcelain") == ""   # merge was aborted


def test_pull_rejects_unknown_strategy(api):
    client, _, _ = api
    assert client.post(f"{BASE}/pull", json={"strategy": "octopus"}).status_code == 422


# =============================================================================
# Stage / unstage / discard / commit / graph / init
# =============================================================================


def test_stage_unstage_discard_return_fresh_state(api):
    client, repo, _ = api
    write(repo / "a.txt", "a\nb\n")
    write(repo / "loose.txt", "l\n")

    staged = client.post(f"{BASE}/stage", json={"files": ["a.txt"]}).json()
    assert staged["success"] is True
    assert staged["status"]["staged"] == [{"file": "a.txt", "status": "M"}]
    assert staged["diff_stats"] == {"additions": 1, "deletions": 0, "files_changed": 1}

    unstaged = client.post(f"{BASE}/unstage", json={"files": ["a.txt"]}).json()
    assert unstaged["status"]["staged"] == [] and unstaged["status"]["modified"] == ["a.txt"]

    discarded = client.post(f"{BASE}/discard", json={"files": ["a.txt", "loose.txt"]}).json()
    assert discarded["success"] is True
    assert discarded["message"] == "Discarded changes in 2 file(s)"
    assert discarded["status"]["is_clean"] is True
    assert not (repo / "loose.txt").exists()

    bad = client.post(f"{BASE}/stage", json={"files": ["missing.txt"]})
    assert bad.status_code == 500 and bad.json()["detail"] == "Failed to stage files"


def test_discard_cannot_delete_checkout_or_git_dir(api):
    """Regression: a client-declared 'untracked' "." or ".git" was rmtree'd."""
    client, repo, _ = api
    response = client.post(f"{BASE}/discard", json={
        "files": [".", ".git"],
        "file_statuses": {".": "untracked", ".git": "untracked"},
    })

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["message"] == "Failed to discard some files: ., .git"
    assert (repo / "a.txt").read_text() == "a\n"
    assert git(repo, "rev-parse", "HEAD")
    assert body["status"]["is_git_repo"] is True


def test_commit_endpoint(api):
    client, repo, _ = api
    nothing = client.post(f"{BASE}/commit", json={"message": "x", "include_unstaged": False})
    assert nothing.status_code == 400
    assert nothing.json()["detail"] == "No staged changes to commit"

    empty = client.post(f"{BASE}/commit", json={"message": "x"})
    assert empty.status_code == 400
    assert empty.json()["detail"].startswith("Failed to create commit:")

    write(repo / "a.txt", "changed\n")
    done = client.post(f"{BASE}/commit", json={"message": "Change a"})
    assert done.status_code == 200
    body = done.json()
    head = git(repo, "rev-parse", "HEAD")
    assert body["commit_sha"] == head[:12]
    assert body["message"] == f"Created commit {head[:12]}"
    assert body["status"]["is_clean"] is True
    assert body["diff_stats"]["files_changed"] == 0


def test_commit_graph_endpoint(api):
    client, repo, _ = api
    git(repo, "checkout", "-q", "-b", "side")
    side = commit_file(repo, "s.txt", "s\n", "side")
    git(repo, "checkout", "-q", "main")

    body = client.get(f"{BASE}/graph").json()
    assert body["total"] == 2 == len(body["commits"])
    assert {c["sha"] for c in body["commits"]} >= {side}
    assert set(body["commits"][0]) == {
        "sha", "short_sha", "message", "author", "author_email", "timestamp", "parents", "refs",
    }
    assert client.get(f"{BASE}/graph", params={"limit": 1}).json()["total"] == 1
    assert client.get(f"{BASE}/graph", params={"branch": "side"}).json()["commits"][0]["sha"] == side


def test_init_repository(api, mock_db):
    client, _, tmp_path = api
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    write(fresh / "readme.md", "hi\n")
    database.create_project("p-new", "New", None, str(fresh))

    response = client.post("/api/v1/projects/p-new/git/init", json={"default_branch": "trunk"})

    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Git repository initialized with initial commit"
    assert git(fresh, "rev-parse", "--abbrev-ref", "HEAD") == "trunk"
    assert git(fresh, "log", "--format=%s") == "Initial commit"
    again = client.post("/api/v1/projects/p-new/git/init", json={})
    assert again.status_code == 400
    assert again.json()["detail"] == "Project is already a git repository"


def test_clone_repository(api, mock_db):
    client, repo, tmp_path = api
    bare = make_origin(tmp_path, repo)
    target = tmp_path / "cloned"
    database.create_project("p-clone", "Clone", None, str(target))

    response = client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": str(bare)})

    assert response.status_code == 200, response.text
    assert response.json()["message"] == f"Repository cloned from {bare}"
    assert (target / "a.txt").read_text() == "a\n"
    assert git(target, "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")

    again = client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": str(bare)})
    assert again.status_code == 400
    assert again.json()["detail"] == "Project is already a git repository"


@pytest.mark.parametrize("url", ["--upload-pack=touch /tmp/x", "ext::sh -c touch% /tmp/x"])
def test_clone_rejects_option_and_helper_urls(api, mock_db, url):
    client, _, tmp_path = api
    target = tmp_path / "cloned"
    database.create_project("p-clone", "Clone", None, str(target))

    response = client.post("/api/v1/projects/p-clone/git/clone", json={"clone_url": url})

    # Refused before git runs (the route classifies the source first).
    assert response.status_code == 400
    assert response.json()["detail"].startswith(("Invalid clone URL", "Refusing unsafe clone URL scheme"))
    assert not (target / ".git").exists()

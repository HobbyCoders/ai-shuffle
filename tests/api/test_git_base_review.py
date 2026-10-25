"""Base review stays scoped to a session and survives commits and base moves."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import git as git_api, git_helpers, git_owner
from app.core import checkout_manager, config, git_cache
from app.core.git_base_diff import BaseReviewUnavailable
from app.core.git_service import git_service
from app.db import checkouts, database, projects_v2
from app.network import control
from tests.git_repo_helpers import commit_file, git, hermetic_git_env, init_repo, make_origin, write

BASE = "/api/v1/projects/p1/git"
QUERY = "?session_id=s1&scope=base"


@pytest.fixture
def review(mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    git_cache._git_cache.invalidate_prefix("")
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    repo = init_repo(
        tmp_path / "repo",
        {"a.txt": "first\n", "delete.txt": "delete\n", "rename.txt": "rename\n"},
    )
    make_origin(tmp_path, repo)
    database.create_project("p1", "Repo", None, str(repo))
    project = projects_v2.upsert_project("local:p1", name="Repo")
    checkouts.upsert_checkout(project["id"], config.settings.effective_instance_id, str(repo))
    database.create_session(session_id="s1", profile_id="default", project_id="p1")
    database.set_session_branch("s1", "feature/review", project["id"])
    branch = Path(checkout_manager.ensure_checkout(
        project, branch="feature/review", create_new_branch=True,
    ))
    git_helpers.invalidate_working_dir_cache()
    app = FastAPI()
    app.include_router(git_api.router)
    app.dependency_overrides[git_api.require_auth] = lambda: "test-token"
    with TestClient(app) as client:
        yield client, repo, branch
    git_helpers.invalidate_working_dir_cache()


def changed(client):
    response = client.get(BASE + "/changed-files" + QUERY)
    assert response.status_code == 200, response.text
    data = response.json()
    return data, {row["file"]: row for row in data["files"]}


def diff(client, path):
    response = client.post(
        BASE + "/file-diff?session_id=s1", json={"file_path": path, "scope": "base"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_clean_commit_remains_reviewable_and_does_not_read_project_tree(review):
    client, repo, branch = review
    base = git(branch, "rev-parse", "HEAD")
    commit_file(branch, "a.txt", "first\ncommitted\n", "task")
    write(repo / "project-only.txt", "must not appear\n")
    assert git(branch, "status", "--porcelain") == ""
    data, files = changed(client)
    assert set(files) == {"a.txt"}
    assert files["a.txt"]["change_origin"] == "committed"
    assert files["a.txt"]["additions"] == 1
    assert data["base_ref"] == "origin/main" and data["merge_base"] == base
    result = diff(client, "a.txt")
    assert "+committed" in result["diff"]
    assert result["merge_base"] == base and result["base_ref"] == "origin/main"


def test_uncommitted_staged_unstaged_untracked_and_both(review):
    client, _, branch = review
    commit_file(branch, "a.txt", "first\ncommitted\n", "task")
    write(branch / "a.txt", "first\ncommitted\nstaged\n")
    git(branch, "add", "a.txt")
    write(branch / "a.txt", "first\ncommitted\nstaged\nunstaged\n")
    write(branch / "new dir/new.txt", "untracked\n")
    data, files = changed(client)
    assert files["a.txt"]["change_origin"] == "both"
    assert files["a.txt"]["additions"] == 3
    assert files["new dir/new.txt"]["change_origin"] == "uncommitted"
    assert files["new dir/new.txt"]["additions"] == 1
    assert data["total_additions"] == 4
    assert "+staged" in diff(client, "a.txt")["diff"]
    assert "+unstaged" in diff(client, "a.txt")["diff"]
    assert "+untracked" in diff(client, "new dir/new.txt")["diff"]


def test_renames_and_deletes_then_uncommitted_rename(review):
    client, _, branch = review
    git(branch, "mv", "rename.txt", "renamed.txt")
    git(branch, "rm", "delete.txt")
    git(branch, "commit", "-qm", "rename and delete")
    _, files = changed(client)
    assert files["renamed.txt"]["status"] == "R"
    assert files["renamed.txt"]["original_file"] == "rename.txt"
    assert files["delete.txt"]["status"] == "D"
    assert "rename from rename.txt" in diff(client, "renamed.txt")["diff"]
    assert "-delete" in diff(client, "delete.txt")["diff"]
    git(branch, "mv", "renamed.txt", "final.txt")
    _, files = changed(client)
    assert "renamed.txt" not in files
    assert files["final.txt"]["change_origin"] == "both"
    assert files["final.txt"]["original_file"] == "rename.txt"
    assert "rename to final.txt" in diff(client, "final.txt")["diff"]


def test_index_only_and_committed_changes_canceled_in_worktree_still_listed(review):
    client, _, branch = review
    commit_file(branch, "a.txt", "committed\n", "task")
    write(branch / "a.txt", "first\n")
    write(branch / "rename.txt", "staged edit\n")
    git(branch, "add", "rename.txt")
    write(branch / "rename.txt", "rename\n")
    _, files = changed(client)
    assert files["a.txt"]["change_origin"] == "both"
    assert diff(client, "a.txt")["diff"] == ""
    assert files["rename.txt"]["change_origin"] == "uncommitted"
    assert diff(client, "rename.txt")["diff"] == ""
    git(branch, "rm", "delete.txt")
    _, files = changed(client)
    assert files["delete.txt"]["status"] == "D"
    assert files["delete.txt"]["change_origin"] == "uncommitted"
    assert "-delete" in diff(client, "delete.txt")["diff"]


def test_moving_base_is_resolved_without_stale_status_cache(review):
    client, _, branch = review
    first_base, _ = changed(client)
    tip = commit_file(branch, "a.txt", "first\ntask\n", "task")
    assert "a.txt" in changed(client)[1]
    git(branch, "update-ref", "refs/remotes/origin/main", tip)
    data, files = changed(client)
    assert files == {}
    assert data["merge_base"] == tip != first_base["merge_base"]


@pytest.mark.parametrize("case,detail", [
    ("detached", "detached"),
    ("no_remote", "requires a remote"),
    ("unrelated", "unrelated history"),
    ("no_base", "no default branch"),
])
def test_unavailable_base_returns_clear_error(review, case, detail):
    client, _, branch = review
    if case == "detached":
        git(branch, "checkout", "--detach")
        with pytest.raises(BaseReviewUnavailable, match=detail):
            git_service.get_base_changed_files(str(branch))
        response = client.get(BASE + "/changed-files" + QUERY)
        assert response.status_code == 409, response.text
        assert "detached HEAD" in response.json()["detail"]
        return
    if case == "no_remote":
        git(branch, "remote", "remove", "origin")
    elif case == "unrelated":
        tree = git(branch, "rev-parse", "HEAD^{tree}")
        unrelated = git(branch, "commit-tree", tree, "-m", "unrelated")
        git(branch, "update-ref", "refs/remotes/origin/main", unrelated)
    else:
        git(branch, "update-ref", "-d", "refs/remotes/origin/main")
        git(branch, "branch", "-m", "main", "trunk")
    response = client.get(BASE + "/changed-files" + QUERY)
    assert response.status_code == 409, response.text
    assert detail in response.json()["detail"]
    response = client.post(
        BASE + "/file-diff?session_id=s1", json={"file_path": "a.txt", "scope": "base"},
    )
    assert response.status_code == 409


def test_missing_session_checkout_and_legacy_worktree_never_fall_back(review, monkeypatch):
    client, repo, _ = review
    write(repo / "project-only.txt", "must not appear\n")
    monkeypatch.setattr("app.core.project_resolver.resolve_existing_cwd", lambda _: None)
    for suffix in ("", "&worktree_id=wrong-checkout"):
        response = client.get(BASE + "/changed-files" + QUERY + suffix)
        assert response.status_code == 409, response.text
        assert "checkout is not available" in response.json()["detail"]
    assert client.get(BASE + "/changed-files?scope=base").status_code == 400
    assert client.post(
        BASE + "/file-diff", json={"file_path": "a.txt", "scope": "base"},
    ).status_code == 400


def test_untracked_literal_path_binary_and_recreated_deletion(review):
    client, _, branch = review
    write(branch / "[new].txt", "literal untracked\n")
    write(branch / "untracked.bin", b"\0\1")
    git(branch, "rm", "delete.txt")
    git(branch, "commit", "-qm", "delete")
    write(branch / "delete.txt", "recreated\n")
    _, files = changed(client)
    assert files["untracked.bin"]["is_binary"] is True
    assert diff(client, "untracked.bin")["is_binary"] is True
    assert "+literal untracked" in diff(client, "[new].txt")["diff"]
    assert files["delete.txt"]["change_origin"] == "both"
    text = diff(client, "delete.txt")["diff"]
    assert "-delete" in text and "+recreated" in text


def test_local_default_ref_fallback(review):
    client, _, branch = review
    git(branch, "update-ref", "-d", "refs/remotes/origin/main")
    data, _ = changed(client)
    assert data["base_ref"] == "main"
    assert data["merge_base"] == git(branch, "rev-parse", "main")


def test_binary_and_truncation_and_literal_paths(review):
    client, _, branch = review
    commit_file(branch, "bin.dat", b"\0\1\2", "binary")
    commit_file(branch, "[literal].txt", "hello\n" * 10000, "large")
    _, files = changed(client)
    assert files["bin.dat"]["is_binary"] is True
    assert diff(client, "bin.dat")["is_binary"] is True
    result = diff(client, "[literal].txt")
    assert result["truncated"] is True
    assert len(result["diff"]) < 50100
    assert "diff truncated" in result["diff"]
    response = client.post(
        BASE + "/file-diff?session_id=s1", json={"file_path": "../outside.txt", "scope": "base"},
    )
    assert response.status_code == 409


@pytest.mark.parametrize("method,path,body,action", [
    ("GET", "/changed-files?session_id=s1&scope=base", None, "get_changed_files"),
    ("POST", "/file-diff?session_id=s1", {"file_path": "a.txt", "scope": "base"}, "get_file_diff"),
])
def test_remote_owner_receives_scope_and_offline_never_reads_local(
    review, monkeypatch, method, path, body, action,
):
    client, _, _ = review
    monkeypatch.setattr(control, "owner_of", lambda _: "remote-worker")
    call = AsyncMock(return_value={"status": 409, "detail": "Owner base unavailable"})
    monkeypatch.setattr(control, "call", call)
    monkeypatch.setattr(
        git_api, "get_session_review_working_dir_async",
        AsyncMock(side_effect=AssertionError("local checkout touched")),
    )
    response = client.request(method, BASE + path, json=body)
    assert response.status_code == 409 and response.json()["detail"] == "Owner base unavailable"
    target, kind, envelope = call.call_args.args
    assert (target, kind, envelope["action"]) == ("remote-worker", "session-git", action)
    params = envelope["params"]
    assert (params["scope"] if method == "GET" else params["body"]["scope"]) == "base"
    call.side_effect = control.RunnerUnreachable("offline")
    assert client.request(method, BASE + path, json=body).status_code == 503


async def test_remote_dispatch_executes_real_endpoint_on_own_checkout(review, monkeypatch):
    _, _, branch = review
    commit_file(branch, "a.txt", "first\nremote task\n", "task")
    monkeypatch.setattr(control, "owner_of", lambda _: None)
    for action, extra in (("get_changed_files", {"scope": "base"}),
                          ("get_file_diff", {"body": {"file_path": "a.txt", "scope": "base"}})):
        result = await git_owner.handle_session_git({
            "action": action, "session_id": "s1", "params": {
                "project_id": "p1", "session_id": "s1", "worktree_id": "viewer-checkout", **extra,
            },
        }, "viewer")
        assert result["status"] == 200, result
        assert result["result"]["base_ref"] == "origin/main"
        if action == "get_changed_files":
            assert result["result"]["files"][0]["change_origin"] == "committed"
        else:
            assert "+remote task" in result["result"]["diff"]

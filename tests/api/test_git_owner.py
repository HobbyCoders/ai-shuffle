"""Session Git actions execute on their owner, never a viewer's checkout."""
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import git, git_operations, git_owner, github
from app.api.auth import require_auth
from app.network import control


@pytest.fixture
def remote(monkeypatch):
    row = {"id": "session-b", "project_id": "project-b", "project_v2_id": "pv2-shared",
           "owner_device_id": "worker-b", "worktree_id": "owner-worktree"}
    monkeypatch.setattr(git_owner.database, "get_session", lambda sid: row if sid == row["id"] else None)
    monkeypatch.setattr(control, "owner_of", lambda session: "worker-b")
    app = FastAPI()
    for router in (git.router, git_operations.router, github.router):
        app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "authorized-viewer"
    return row, TestClient(app)


@pytest.mark.parametrize("method,path,body,action", [
    ("GET", "status", None, "get_git_status"),
    ("GET", "pr-state", None, "get_pr_state"),
    ("GET", "branches", None, "list_branches"),
    ("GET", "changed-files", None, "get_changed_files"),
    ("POST", "generate-commit-message", {}, "generate_commit_message"),
    ("POST", "commit", {"message": "Saved commit", "include_unstaged": False}, "create_commit"),
    ("POST", "push", {"set_upstream": True}, "push_commits"),
    ("POST", "pull", {"strategy": "rebase", "auto_stash": True}, "pull_from_remote"),
    ("POST", "finish", {"session_id": "session-b", "merge_method": "rebase", "cleanup": True}, "finish"),
    ("POST", "ship", {"session_id": "session-b", "merge_method": "squash", "cleanup": False}, "ship"),
    ("GET", "operations/op-b", None, "get_operation"),
])
def test_routes_before_touching_viewer_checkout(remote, monkeypatch, method, path, body, action):
    _, client = remote
    # An owner's structured error is returned unchanged, including 409 guards.
    call = AsyncMock(return_value={"status": 409, "detail": "Owner guard"})
    monkeypatch.setattr(control, "call", call)
    local_git = AsyncMock(side_effect=AssertionError("Viewer must not resolve a checkout"))
    monkeypatch.setattr(git, "get_working_dir_async", local_git)
    response = client.request(method, f"/api/v1/projects/pv2-shared/git/{path}?session_id=session-b", json=body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Owner guard"
    target, kind, envelope = call.call_args.args
    assert (target, kind, envelope["action"]) == ("worker-b", "session-git", action)
    assert envelope["session_id"] == "session-b"
    assert "token" not in envelope["params"] and "request" not in envelope["params"]
    if body:
        for key, value in body.items():
            assert envelope["params"]["body"][key] == value
    local_git.assert_not_called()


def test_create_pr_routes_with_saved_session(remote, monkeypatch):
    _, client = remote
    call = AsyncMock(return_value={"status": 409, "detail": "Owner guard"})
    monkeypatch.setattr(control, "call", call)
    response = client.post("/api/v1/projects/pv2-shared/github/pulls", json={
        "session_id": "session-b", "title": "PR", "head": "feature", "base": "main", "draft": True,
    })
    assert response.status_code == 409
    assert call.call_args.args[2]["action"] == "create_pull_request"


def test_offline_owner_never_falls_back_to_local(remote, monkeypatch):
    _, client = remote
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=control.RunnerUnreachable("offline")))
    response = client.post("/api/v1/projects/pv2-shared/git/push?session_id=session-b", json={})
    assert response.status_code == 503
    assert "Owning worker" in response.json()["detail"]


def test_access_checked_before_forwarding(remote, monkeypatch):
    _, client = remote
    call = AsyncMock()
    monkeypatch.setattr(control, "call", call)
    response = client.get("/api/v1/projects/unrelated/git/status?session_id=session-b")
    assert response.status_code == 403
    call.assert_not_called()
    client.app.dependency_overrides[require_auth] = lambda: (_ for _ in ()).throw(HTTPException(401))
    assert client.get("/api/v1/projects/pv2-shared/git/status?session_id=session-b").status_code == 401
    call.assert_not_called()


@pytest.mark.parametrize("action,params", [
    ("finish", {"body": {"session_id": "session-b", "worktree_id": "viewer-worktree", "merge_method": "rebase", "cleanup": True}}),
    ("create_commit", {"session_id": "session-b", "worktree_id": "viewer-worktree", "body": {"message": "Saved", "include_unstaged": False}}),
    ("get_operation", {"session_id": "session-b", "operation_id": "owner-op"}),
])
async def test_owner_uses_existing_flow_and_own_checkout(remote, monkeypatch, action, params):
    row, _ = remote
    monkeypatch.setattr(control, "owner_of", lambda session: None)
    _original, signature, hints = git_owner._actions[action]
    execute = AsyncMock(return_value={"operation_id": "owner-op", "status": "running"})
    monkeypatch.setitem(git_owner._actions, action, (execute, signature, hints))
    result = await git_owner.handle_session_git({
        "action": action, "session_id": row["id"], "params": {"project_id": "pv2-shared", **params},
    }, "viewer")
    assert result["status"] == 200
    args = execute.call_args.kwargs
    assert args["project_id"] == "project-b"
    if action == "finish":
        assert args["body"].worktree_id == "owner-worktree"
        assert args["body"].merge_method == "rebase" and args["body"].cleanup
    elif action == "create_commit":
        assert args["worktree_id"] == "owner-worktree"
        assert args["body"].message == "Saved" and not args["body"].include_unstaged
    else:
        assert args["operation_id"] == "owner-op"


async def test_owner_rejects_foreign_sessions_and_invalid_options(remote, monkeypatch):
    body = {"action": "finish", "session_id": "session-b", "params": {
        "project_id": "pv2-shared", "body": {"session_id": "session-b", "merge_method": "invalid"},
    }}
    with pytest.raises(PermissionError):
        await git_owner.handle_session_git(body, "viewer")
    monkeypatch.setattr(control, "owner_of", lambda session: None)
    assert (await git_owner.handle_session_git(body, "viewer"))["status"] == 422
    body["params"]["body"] = {"session_id": "different-session"}
    with pytest.raises(ValueError, match="session does not match"):
        await git_owner.handle_session_git(body, "viewer")
    body["action"] = "arbitrary-shell-command"
    with pytest.raises(ValueError, match="Unsupported"):
        await git_owner.handle_session_git(body, "viewer")


async def test_owner_executes_real_commit_endpoint(remote, monkeypatch):
    monkeypatch.setattr(control, "owner_of", lambda session: None)
    resolve = AsyncMock(return_value="/owner/feature")
    commit = AsyncMock(return_value=(True, "abc123", None))
    monkeypatch.setattr(git, "get_working_dir_async", resolve)
    monkeypatch.setattr(git, "stage_and_commit", commit)
    monkeypatch.setattr(git, "run_git", AsyncMock(side_effect=[True, {"is_clean": True}, {
        "additions": 0, "deletions": 0, "files_changed": 0,
    }]))
    result = await control.handle("session-git", {
        "action": "create_commit", "session_id": "session-b", "params": {
            "project_id": "pv2-shared", "session_id": "session-b",
            "worktree_id": "wrong-viewer-checkout", "body": {"message": "Saved", "include_unstaged": False},
        },
    }, from_device="viewer")
    assert result["status"] == 200
    assert result["result"]["commit_sha"] == "abc123"
    resolve.assert_awaited_once_with("project-b", "owner-worktree", "session-b")
    commit.assert_awaited_once_with("/owner/feature", "Saved", False)


@pytest.fixture
def worker_repositories(tmp_path, monkeypatch):
    import subprocess
    from app.core.config import settings
    from app.db import checkouts, projects_v2
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda self: "worker-b"))
    def git_at(path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    a, b = tmp_path / "a", tmp_path / "b"
    subprocess.run(["git", "clone", str(origin), str(a)], check=True, capture_output=True)
    for key, value in [("user.name", "Test"), ("user.email", "test@example.invalid")]:
        git_at(a, "config", key, value)
    (a / "tracked.txt").write_text("initial\n")
    git_at(a, "add", ".")
    git_at(a, "commit", "-m", "initial")
    git_at(a, "push", "origin", "main")
    subprocess.run(["git", "clone", str(origin), str(b)], check=True, capture_output=True)
    project = projects_v2.upsert_project("local:worker-target", "Worker target")
    checkouts.upsert_checkout(project["id"], "worker-a", str(a))
    checkouts.upsert_checkout(project["id"], "worker-b", str(b))
    (a / "tracked.txt").write_text("incoming\n")
    git_at(a, "commit", "-am", "incoming")
    git_at(a, "push", "origin", "main")
    git_at(b, "fetch")
    # Viewer-local dirty data must survive the selected worker's pull untouched.
    (a / "viewer-only.txt").write_text("do not touch")
    return project, a, b, git_at


async def test_sessionless_worker_status_and_real_pull(worker_repositories):
    project, a, b, git_at = worker_repositories
    def payload(action, **params):
        return {"action": action, "project_id": project["id"], "branch": "main",
                "params": {"project_id": project["id"], **params}}
    status = await control.handle("project-git", payload("get_git_status"), from_device="worker-a")
    assert status["status"] == 200, status
    assert status["result"]["behind"] == 1
    assert status["result"]["untracked"] == []
    before = git_at(a, "rev-parse", "HEAD")
    pull = await control.handle("project-git", payload("pull_from_remote", body={"strategy": "merge", "auto_stash": False}), from_device="worker-a")
    assert pull["status"] == 200, pull
    assert (b / "tracked.txt").read_text() == "incoming\n"
    assert git_at(b, "rev-parse", "HEAD") == before == git_at(a, "rev-parse", "HEAD")
    assert (a / "viewer-only.txt").read_text() == "do not touch"
    status = await control.handle("project-git", payload("get_git_status"), from_device="worker-a")
    assert status["result"]["behind"] == 0
    assert (await control.handle("project-git", {**payload("get_git_status"), "branch": "absent"}, from_device="worker-a"))["status"] == 409
    with pytest.raises(ValueError, match="Mixed"):
        await control.handle("project-git", payload("get_git_status", session_id="foreign"), from_device="worker-a")
    with pytest.raises(ValueError, match="Unsupported"):
        await control.handle("project-git", payload("ship"), from_device="worker-a")


def test_sessionless_worker_forwarding_access_and_offline(worker_repositories, monkeypatch):
    from app.api import common
    from app.network import git as network_git
    project, a, b, git_at = worker_repositories
    application = FastAPI()
    application.include_router(git.router)
    application.dependency_overrides[require_auth] = lambda: "authorized-viewer"
    client = TestClient(application)
    monkeypatch.setattr(network_git, "device_id", lambda runner: "worker-other")
    call = AsyncMock(side_effect=control.RunnerUnreachable("offline"))
    monkeypatch.setattr(control, "call", call)
    response = client.post(f"/api/v1/projects/{project['id']}/git/pull?worker_id=worker-other&checkout_branch=main", json={})
    assert response.status_code == 503, response.text
    assert "No local Git action" in response.json()["detail"]
    assert call.call_args.args[0:2] == ("worker-other", "project-git")
    assert call.call_args.args[2]["branch"] == "main"
    assert (b / "tracked.txt").read_text() == "initial\n"
    call.reset_mock()
    response = client.get(f"/api/v1/projects/{project['id']}/git/status?worker_id=worker-other&session_id=bogus")
    assert response.status_code == 400
    call.assert_not_called()
    monkeypatch.setattr(common, "get_api_user_from_request", lambda request: {"project_id": "foreign-project"})
    response = client.get(f"/api/v1/projects/{project['id']}/git/status?worker_id=worker-other")
    assert response.status_code == 403
    call.assert_not_called()


"""Plain project folders are positive non-repos, not unavailable checkouts."""
import os
import subprocess
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import git
from app.api.auth import require_auth
from app.core.config import settings
from app.db import checkouts, projects_v2
from app.network import control


@pytest.fixture
def plain_project(tmp_path, monkeypatch):
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda self: "worker-b"))
    folder = tmp_path / "plain"
    folder.mkdir()
    project = projects_v2.upsert_project("local:plain-project", "Plain project")
    checkouts.upsert_checkout(project["id"], "worker-b", str(folder))
    app = FastAPI()
    app.include_router(git.router)
    app.dependency_overrides[require_auth] = lambda: "authorized-viewer"
    return project, folder, TestClient(app, raise_server_exceptions=False)


def git_at(folder, *args):
    return subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True, text=True)


def deny_read_access(monkeypatch, target):
    access = os.access
    target = os.path.normcase(os.path.realpath(target))

    def readable(path, mode, **kwargs):
        if os.path.normcase(os.path.realpath(path)) == target and mode & os.R_OK:
            return False
        return access(path, mode, **kwargs)

    monkeypatch.setattr(os, "access", readable)


@pytest.mark.parametrize("worker", ["local", "remote-worker"])
def test_plain_status_then_external_git_init(plain_project, monkeypatch, worker):
    from app.network import git as network_git
    project, folder, client = plain_project
    # Exercise the peer receiver as well as the local worker HTTP path.
    async def receive(target, kind, payload, **kwargs):
        assert target == "remote-device" and kind == "project-git"
        return await control.handle(kind, payload, from_device="viewer")
    call = AsyncMock(side_effect=receive)
    monkeypatch.setattr(network_git, "device_id", lambda value: "remote-device")
    monkeypatch.setattr(control, "call", call)
    url = f"/api/v1/projects/{project['id']}/git/status?worker_id={worker}"
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["is_git_repo"] is False
    assert response.json()["current_branch"] is None
    assert list(folder.iterdir()) == []  # Status did not initialize anything.
    git_at(folder, "init", "-b", "main")
    response = client.get(url)  # No restart, cache invalidation or TTL wait.
    assert response.status_code == 200, response.text
    assert response.json()["is_git_repo"] is True
    if worker == "local":
        call.assert_not_called()
    else:
        assert call.await_count == 2


@pytest.mark.parametrize("state", ["missing", "two-plain", "plain-and-repo"])
def test_invalid_or_ambiguous_checkout_stays_409(plain_project, state):
    project, folder, client = plain_project
    if state == "missing":
        folder.rmdir()
    else:
        other = folder.parent / "other"
        other.mkdir()
        if state == "plain-and-repo":
            git_at(other, "init", "-b", "main")
        checkouts.upsert_checkout(project["id"], "worker-b", str(other))
    response = client.get(f"/api/v1/projects/{project['id']}/git/status?worker_id=local")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Selected checkout is missing or ambiguous on this worker"


@pytest.mark.parametrize("diagnostic,is_plain", [
    ("fatal: not a git repository (or any parent up to mount point /home/appuser)\n"
     "Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).\n", True),
    ("fatal: cannot change to directory: Permission denied\n", False),
    ("fatal: detected dubious ownership in repository at '/project'\n", False),
])
def test_git_diagnostics_distinguish_mount_boundary_from_failure(plain_project, monkeypatch, diagnostic, is_plain):
    project, folder, client = plain_project
    monkeypatch.setattr(git.git_service, "_run_git", lambda *args, **kwargs:
                        subprocess.CompletedProcess(["git", "rev-parse"], 128, stdout="", stderr=diagnostic))
    response = client.get(f"/api/v1/projects/{project['id']}/git/status?worker_id=local")
    assert response.status_code == (200 if is_plain else 500)
    if is_plain:
        assert response.json()["is_git_repo"] is False


def test_non_english_environment_uses_c_locale_for_confirmation(plain_project, monkeypatch):
    project, folder, client = plain_project
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    original = git.git_service._run_git
    def run(*args, **kwargs):
        assert kwargs["env"]["LC_ALL"] == "C"
        return original(*args, **kwargs)
    monkeypatch.setattr(git.git_service, "_run_git", run)
    response = client.get(f"/api/v1/projects/{project['id']}/git/status?worker_id=local")
    assert response.status_code == 200, response.text
    assert response.json()["is_git_repo"] is False


def test_linked_worktree_git_file_is_a_repository(plain_project, monkeypatch):
    project, folder, client = plain_project
    base = folder.parent / "base-repo"
    base.mkdir()
    git_at(base, "init", "-b", "main")
    git_at(base, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "initial")
    git_at(base, "worktree", "add", "-b", "linked", str(folder))
    assert (folder / ".git").is_file()
    response = client.get(f"/api/v1/projects/{project['id']}/git/status?worker_id=local")
    assert response.status_code == 200, response.text
    assert response.json()["is_git_repo"] is True
    assert response.json()["current_branch"] == "linked"
    target = folder / (folder / ".git").read_text().strip().removeprefix("gitdir: ")
    # Model an access denial (e.g. a Windows ACL), not POSIX chmod bits:
    # Windows chmod(0) only changes the read-only attribute, not readability.
    deny_read_access(monkeypatch, target)
    response = client.get(f"/api/v1/projects/{project['id']}/git/status?worker_id=local")
    assert response.status_code == 503, response.text


def test_structural_git_marker_never_classifies_as_plain(plain_project, monkeypatch):
    from app.api.git_owner import _is_plain_project_folder
    project, folder, client = plain_project
    (folder / ".git").write_text("gitdir: missing\n")
    def unexpected(*args, **kwargs):
        raise AssertionError("A malformed or inaccessible Git entry must never be called a plain folder")
    monkeypatch.setattr(git.git_service, "_run_git", unexpected)
    with pytest.raises(PermissionError, match="Cannot read Git metadata"):
        _is_plain_project_folder(str(folder))


@pytest.mark.parametrize("failure", [PermissionError("denied"), FileNotFoundError("git missing"),
                                      subprocess.TimeoutExpired("git", 5),
                                      subprocess.CalledProcessError(128, "git", stderr="permission denied")])
def test_probe_failure_is_never_a_positive_non_repo(plain_project, monkeypatch, failure):
    project, folder, client = plain_project
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(git.git_service, "_run_git", fail)
    response = client.get(f"/api/v1/projects/{project['id']}/git/status?worker_id=local")
    assert response.status_code == 500


@pytest.mark.parametrize("action,body", [("commit", {"message": "no"}),
                                        ("branches", {"name": "feature/no"})])
def test_worker_mutations_keep_refusing_plain_folder(plain_project, action, body):
    project, folder, client = plain_project
    response = client.post(f"/api/v1/projects/{project['id']}/git/{action}?worker_id=local", json=body)
    assert response.status_code in (400, 409), response.text
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize("scope", ["session", "worker"])
@pytest.mark.parametrize("own_repository", [False, True])
def test_nested_project_reports_scope_without_parent_files(plain_project, scope, own_repository):
    from app.db import database
    from app.core.project_resolver import resolve_existing_cwd
    project, folder, client = plain_project
    git_at(folder.parent, "init", "-b", "main")
    (folder.parent / "outside.txt").write_text("parent-only content")
    if own_repository:
        git_at(folder, "init", "-b", "nested")
        git_at(folder, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
               "commit", "--allow-empty", "-m", "Initial nested repository")
    session_id = f"nested-chat-{scope}-{own_repository}"
    database.create_session(session_id, "default", project_v2_id=project["id"])
    assert resolve_existing_cwd(database.get_session(session_id)) == str(folder)
    query = f"?session_id={session_id}" if scope == "session" else "?worker_id=local"
    response = client.get(f"/api/v1/projects/{project['id']}/git/status{query}")
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["is_git_repo"] is True
    assert status["enclosing_repository_root"] == (None if own_repository else str(folder.parent.resolve()))
    assert status["untracked"] == []
    if own_repository:
        assert status["current_branch"] == "nested"
    else:
        assert status["mutations_blocked"] is True
        assert not (folder / ".git").exists()
        for action in ("diff-stats", "changed-files"):
            refused = client.get(f"/api/v1/projects/{project['id']}/git/{action}{query}")
            assert refused.status_code == 409, refused.text
            assert "outside.txt" not in refused.text
        for action, body in [("stage", {"files": ["../outside.txt"]}), ("init", {})]:
            refused = client.post(f"/api/v1/projects/{project['id']}/git/{action}{query}", json=body)
            assert refused.status_code == 409, refused.text
        assert git_at(folder.parent, "diff", "--cached", "--name-only").stdout == ""


def test_real_discovery_ceiling_ignores_repository_above_it(plain_project, monkeypatch):
    project, folder, client = plain_project
    git_at(folder.parent, "init", "-b", "main")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(folder.parent))
    response = client.get(f"/api/v1/projects/{project['id']}/git/status?worker_id=local")
    assert response.status_code == 200, response.text
    assert response.json()["is_git_repo"] is False


def test_exec_only_ancestor_does_not_make_readable_project_unavailable(plain_project):
    project, folder, client = plain_project
    parent = folder.parent
    previous = parent.stat().st_mode
    parent.chmod(0o311)
    try:
        response = client.get(f"/api/v1/projects/{project['id']}/git/status?worker_id=local")
        assert response.status_code == 200, response.text
        assert response.json()["is_git_repo"] is False
    finally:
        parent.chmod(previous)


@pytest.mark.parametrize("state", ["plain", "missing", "unreadable-git"])
def test_existing_session_status_only_confirms_readable_plain_folder(plain_project, monkeypatch, state):
    from app.db import database
    project, folder, client = plain_project
    session_id = f"plain-chat-{state}"
    database.create_session(session_id, "default", project_v2_id=project["id"])
    if state == "missing":
        folder.rmdir()
    elif state == "unreadable-git":
        git_at(folder, "init", "-b", "main")
        # Warm native handles and the session resolver before changing access.
        warm = client.get(f"/api/v1/projects/{project['id']}/git/status?session_id={session_id}")
        assert warm.status_code == 200, warm.text
        assert warm.json()["is_git_repo"] is True
        deny_read_access(monkeypatch, folder / ".git")
    response = client.get(f"/api/v1/projects/{project['id']}/git/status?session_id={session_id}")
    assert response.status_code == (200 if state == "plain" else 503), response.text
    if state == "plain":
        assert response.json()["is_git_repo"] is False
    else:
        assert "unavailable" in response.json()["detail"].lower()


@pytest.mark.parametrize("scope", ["project", "session", "worker"])
def test_real_repo_with_failed_status_never_becomes_plain(plain_project, monkeypatch, scope):
    from app.db import database
    project, folder, client = plain_project
    git_at(folder, "init", "-b", "main")
    failed_status = Mock(return_value={"is_git_repo": False})
    monkeypatch.setattr(git.git_service, "get_status", failed_status)
    query = ""
    target_id = project["id"]
    if scope == "project":
        # An unscoped project read resolves the registered legacy project path,
        # rather than using a session or selected-worker checkout scope.
        legacy = database.create_project("failed-status-project", "Failed status", None, str(folder))
        target_id = legacy["id"]
    elif scope == "session":
        database.create_session("failed-git-chat", "default", project_v2_id=project["id"])
        query = "?session_id=failed-git-chat"
    elif scope == "worker":
        query = "?worker_id=local"
    response = client.get(f"/api/v1/projects/{target_id}/git/status{query}")
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "Git checkout is unavailable. Check again."
    failed_status.assert_called_once_with(str(folder))


@pytest.mark.parametrize("requested_branch", [None, "feature/missing"])
async def test_branch_bound_plain_checkout_never_uses_plain_fallback(plain_project, monkeypatch, requested_branch):
    from app.api import git_owner
    project, folder, client = plain_project
    checkouts.upsert_checkout(project["id"], "worker-b", str(folder), branch="feature/missing")
    classify = Mock(return_value=True)
    monkeypatch.setattr(git_owner, "_is_plain_project_folder", classify)
    result = await control.handle("project-git", {
        "action": "get_git_status", "project_id": project["id"], "branch": requested_branch,
        "params": {"project_id": project["id"]},
    }, from_device="viewer")
    assert result["status"] == 409
    assert result["detail"] == "Selected checkout is missing or ambiguous on this worker"
    # A named checkout must be refused before any non-repository fallback,
    # even if later branch-identity guards would also reject the folder.
    classify.assert_not_called()


@pytest.mark.parametrize("new_branch", [False, True])
async def test_worktree_creation_refused_before_side_effects(plain_project, monkeypatch, new_branch):
    from app.api import sessions_worktree
    from app.api.sessions_helpers import CreateWorktreeSessionRequest
    from app.core import checkout_manager
    project, folder, client = plain_project
    # Keep real checkout resolution/planning and real intake. Use the canonical
    # project identity just as the HTTP intake does.
    monkeypatch.setattr(checkout_manager, "local_branch_exists", lambda *args: False)
    with pytest.raises(HTTPException) as error:
        await sessions_worktree._create_v2_branch_session(
            body=CreateWorktreeSessionRequest(project_v2_id=project["id"], branch_name="feature/no",
                                              create_new_branch=new_branch),
            project=None, api_user_id=None,
        )
    assert error.value.status_code == 409
    assert list(folder.iterdir()) == []
    assert len(checkouts.list_checkouts(project["id"])) == 1

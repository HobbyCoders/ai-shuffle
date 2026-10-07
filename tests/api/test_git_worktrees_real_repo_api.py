"""Legacy worktree routes end to end on a real repository.

List (with per-worktree git status), get, file browsing (confined to the
worktree), delete (git removal + optional branch delete) and reopen
(recreate under ``.fluid/worktrees``) — the WorktreeManager and GitService are
real, only auth is overridden.
"""

import os
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import git as git_api
from app.api import git_helpers, git_worktrees
from app.core import config
from app.core.worktree_manager import worktree_manager
from app.db import database
from app.db.base import WorktreeStatus
from tests.git_repo_helpers import git, hermetic_git_env, init_repo, write

BASE = "/api/v1/projects/p1/git/worktrees"


@pytest.fixture
def wt_api(mock_db, tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    repo = init_repo(tmp_path / "proj", {"a.txt": "a\n", "docs/guide.md": "g\n"})
    database.create_project("p1", "Proj", None, str(repo))
    record = worktree_manager._ensure_repository_record("p1", str(repo))
    path = worktree_manager._get_worktree_path(str(repo), "feature/api")
    git(repo, "worktree", "add", "-q", "-b", "feature/api", str(path))
    write(path / "wip.txt", "wip\n")
    database.create_worktree(
        worktree_id="wt-1", repository_id=record["id"], branch_name="feature/api",
        worktree_path=str(path), status=WorktreeStatus.ACTIVE,
    )
    git_helpers.invalidate_working_dir_cache()
    git_worktrees._worktree_status_cache.clear()

    app = FastAPI()
    app.include_router(git_api.router)
    app.dependency_overrides[git_api.require_auth] = lambda: "test-token"
    with TestClient(app) as client:
        yield client, repo, path, tmp_path
    git_worktrees._worktree_status_cache.clear()
    git_helpers.invalidate_working_dir_cache()


def test_list_worktrees_with_git_status(wt_api):
    client, _, path, _ = wt_api

    response = client.get(BASE)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    wt = body["worktrees"][0]
    assert wt["id"] == "wt-1" and wt["branch_name"] == "feature/api"
    assert wt["worktree_path"] == str(path)
    assert wt["exists"] is True and wt["resource_kind"] == "worktree"
    assert wt["git_status"]["current_branch"] == "feature/api"
    assert wt["git_status"]["untracked"] == ["wip.txt"]
    assert wt["git_status"]["is_clean"] is False

    no_status = client.get(BASE, params={"include_status": "false"}).json()
    assert no_status["worktrees"][0]["git_status"] is None




def test_stale_marker_is_reported_not_parent_status_and_recovery_is_detected(wt_api, monkeypatch):
    monkeypatch.setattr(git_worktrees, "_WORKTREE_STATUS_BACKOFF_BASE_SECONDS", 0)
    monkeypatch.setattr(git_worktrees, "_WORKTREE_STATUS_FRESH_SECONDS", 0)
    client, repo, path, _ = wt_api
    marker = (path / ".git").read_text()
    # Keep .git present but point it at a main checkout that no longer exists.
    (path / ".git").write_text("gitdir: /missing-fixture/gitdir\n")
    response = client.get(BASE)
    assert response.status_code == 200
    row = response.json()["worktrees"][0]
    assert row["git_status"] is None
    assert "not its own Git working tree" in row["status_error"]
    (path / ".git").write_text(marker)
    client.get(BASE)  # retry on the normal refresh path after back-off
    pending = git_worktrees._worktree_status_cache["p1"].inflight
    if pending is not None:
        pending.result(timeout=5)
    row = client.get(BASE).json()["worktrees"][0]
    assert row["status_error"] is None
    assert row["git_status"]["current_branch"] == "feature/api"
    assert git(repo, "branch", "--show-current") == "main"


def test_missing_marker_is_reported_as_needing_cleanup(wt_api):
    client, _, path, _ = wt_api
    (path / ".git").unlink()
    row = client.get(BASE).json()["worktrees"][0]
    assert row["exists"] is False
    assert row["git_status"] is None
    assert "needs cleanup" in row["status_error"]
    compact = client.get(BASE, params={"compact": "true"}).json()["worktrees"]
    assert compact[0]["id"] == "wt-1" and compact[0]["exists"] is False


def test_wrong_branch_reports_actual_counts_with_binding_warning(wt_api):
    client, repo, path, _ = wt_api
    git(path, "switch", "-q", "-c", "feature/reassigned")
    row = client.get(BASE).json()["worktrees"][0]
    assert row["git_status"]["current_branch"] == "feature/reassigned"
    assert row["git_status"]["untracked_count"] == 1
    assert row["status_error"] is None
    assert "not the bound branch" in row["binding_warning"]
    compact = client.get(BASE, params={"compact": "true"}).json()["worktrees"][0]
    assert compact["git_status"]["current_branch"] == "feature/reassigned"
    assert compact["git_status"]["untracked_count"] == 1
    assert compact["binding_warning"] == row["binding_warning"]
    details = client.get(f"{BASE}/wt-1").json()
    assert details["git_status"]["current_branch"] == "feature/reassigned"
    assert details["binding_warning"] == row["binding_warning"]
    assert git(path, "branch", "--show-current") == "feature/reassigned"
    assert git(repo, "branch", "--show-current") == "main"


@pytest.mark.parametrize("state", ["missing", "invalid", "branch_mismatch"])
def test_canonical_checkout_list_preserves_stale_bindings_with_actual_root_status(wt_api, state):
    from app.core.config import settings
    from app.core.checkout_manager import ensure_checkout
    from app.db import projects_v2, checkouts

    client, repo, path, _ = wt_api
    canonical = projects_v2.upsert_project("local:canonical-api", name="Canonical API")
    checkouts.upsert_checkout(canonical["id"], settings.effective_instance_id, str(repo))
    checkout = checkouts.upsert_checkout(canonical["id"], settings.effective_instance_id,
                                         str(path), branch="feature/api")
    assert ensure_checkout(canonical, branch="feature/api", provision_runtime=False) == str(path)
    if state == "missing":
        shutil.rmtree(path)
    elif state == "invalid":
        (path / ".git").unlink()
    else:
        git(path, "switch", "-q", "-c", "feature/canonical-actual")
    url = f"/api/v1/projects/{canonical['id']}/git/worktrees"
    response = client.get(url, params={"compact": "true"})
    assert response.status_code == 200
    row = response.json()["worktrees"][0]
    assert row["id"] == checkout["id"] and row["resource_kind"] == "checkout"
    if state == "branch_mismatch":
        assert row["git_status"]["current_branch"] == "feature/canonical-actual"
        assert row["git_status"]["untracked_count"] == 1 and row["binding_warning"]
    else:
        assert row["exists"] is False and row["git_status"] is None
        assert "needs cleanup" in row["status_error"]
    # Canonical checkouts never gain the legacy destructive lifecycle endpoint.
    assert client.delete(f"{url}/{checkout['id']}").status_code == 404
    if state != "missing":
        assert (path / "wip.txt").read_text() == "wip\n"


def test_cleanup_refuses_registry_only_directory_without_deleting_files_or_branch(wt_api):
    client, repo, path, _ = wt_api
    (path / ".git").unlink()
    git(repo, "worktree", "prune")  # registry-only after fixture's marker is lost
    response = client.delete(f"{BASE}/wt-1", params={"keep_branch": "false"})
    assert response.status_code == 409
    assert "preserved" in response.json()["detail"]
    assert (path / "wip.txt").read_text() == "wip\n"
    assert git(repo, "branch", "--list", "feature/api").strip() == "feature/api"
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.ACTIVE


def test_cleanup_switched_branch_refuses_before_detaching_links_or_terminating(wt_api, monkeypatch):
    from unittest.mock import AsyncMock
    from app.core import checkout_manager

    client, repo, path, tmp_path = wt_api
    dependency = tmp_path / "owned-dependency"
    dependency.mkdir()
    (dependency / "keep.txt").write_text("keep me")
    (path / "node_modules").symlink_to(dependency, target_is_directory=True)
    git(path, "switch", "-q", "-c", "feature/deliberate")
    terminate = AsyncMock()
    monkeypatch.setattr(worktree_manager, "terminate_worktree_sessions", terminate)
    sever = []
    monkeypatch.setattr(checkout_manager, "sever_runtime_links", lambda p: sever.append(p))
    response = client.delete(f"{BASE}/wt-1", params={"keep_branch": "false"})
    assert response.status_code == 409 and "bound branch" in response.json()["detail"]
    assert (path / "node_modules").is_symlink() and (dependency / "keep.txt").read_text() == "keep me"
    assert sever == [] and not terminate.called
    assert git(path, "branch", "--show-current") == "feature/deliberate"
    assert git(repo, "branch", "--list", "feature/api").strip() == "feature/api"
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.ACTIVE


@pytest.mark.parametrize("missing", [False, True])
def test_cleanup_running_checkout_refuses_without_stopping_session(wt_api, monkeypatch, missing):
    from unittest.mock import AsyncMock
    from app.core import streaming_status

    client, _, path, _ = wt_api
    database.create_session("s-cleanup", None, project_id="p1", worktree_id="wt-1",
                            worktree_path=str(path), worktree_branch="feature/api",
                            derive_provider_from_profile=False)
    if missing:
        shutil.rmtree(path)
    from app.db import runs
    claim = runs.claim_run("s-cleanup", "fixture-runner", 300)
    assert claim is not None and streaming_status._has_active_run_db("s-cleanup")
    terminate = AsyncMock()
    monkeypatch.setattr(worktree_manager, "terminate_worktree_sessions", terminate)
    response = client.delete(f"{BASE}/wt-1")
    assert response.status_code == 409 and "session is working" in response.json()["detail"]
    assert not terminate.called
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.ACTIVE


def test_missing_registry_cleanup_keeps_unpushed_history(wt_api):
    from tests.git_repo_helpers import commit_file

    client, repo, path, _ = wt_api
    commit_file(path, "valuable.txt", "keep history\n", "unmerged")
    shutil.rmtree(path)
    response = client.delete(f"{BASE}/wt-1", params={"keep_branch": "false"})
    assert response.status_code == 409 and "keep_branch=true" in response.json()["detail"]
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.ACTIVE
    assert client.delete(f"{BASE}/wt-1").status_code == 200
    assert git(repo, "show", "feature/api:valuable.txt") == "keep history"
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.COMPLETED


def test_cleanup_symlink_checkout_preserves_target(wt_api):
    client, _, path, tmp_path = wt_api
    preserved = tmp_path / "preserved-target"
    shutil.move(path, preserved)
    path.symlink_to(preserved, target_is_directory=True)
    response = client.delete(f"{BASE}/wt-1")
    assert response.status_code == 409 and "symlink" in response.json()["detail"]
    assert path.is_symlink() and (preserved / "wip.txt").read_text() == "wip\n"
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.ACTIVE


@pytest.mark.parametrize("foreign_repository", [True, False])
def test_cleanup_replaced_git_binding_preserves_directory(wt_api, monkeypatch, foreign_repository):
    from unittest.mock import AsyncMock

    client, repo, path, tmp_path = wt_api
    foreign_path = tmp_path / "other-worktree"
    if foreign_repository:
        foreign = init_repo(tmp_path / "foreign")
        git(foreign, "worktree", "add", "-q", "-b", "feature/api", str(foreign_path))
    else:
        git(repo, "worktree", "add", "--force", "-q", str(foreign_path), "feature/api")
    # The app registry and original Git administrative entry still name path,
    # but its marker belongs to another checkout (possibly another repository).
    (path / ".git").write_text((foreign_path / ".git").read_text())
    terminate = AsyncMock()
    monkeypatch.setattr(worktree_manager, "terminate_worktree_sessions", terminate)
    response = client.delete(f"{BASE}/wt-1", params={"keep_branch": "false"})
    assert response.status_code == 409 and "preserved" in response.json()["detail"]
    assert not terminate.called
    assert (path / "wip.txt").read_text() == "wip\n"
    assert git(repo, "branch", "--list", "feature/api", "--format=%(refname:short)") == "feature/api"
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.ACTIVE


def test_cleanup_corrupt_path_refuses_before_touching_files_or_sessions(wt_api, monkeypatch):
    from unittest.mock import AsyncMock

    client, repo, path, _ = wt_api
    database.update_worktree("wt-1", worktree_path="")
    terminate = AsyncMock()
    monkeypatch.setattr(worktree_manager, "terminate_worktree_sessions", terminate)
    response = client.delete(f"{BASE}/wt-1", params={"keep_branch": "false"})
    assert response.status_code == 409 and "preserved" in response.json()["detail"]
    assert not terminate.called
    assert (path / "wip.txt").read_text() == "wip\n"
    assert git(repo, "branch", "--list", "feature/api", "--format=%(refname:short)") == "feature/api"
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.ACTIVE


def test_cleanup_busy_status_error_fails_closed(wt_api, monkeypatch):
    from app.core import streaming_status

    client, _, path, _ = wt_api
    database.create_session("s-unknown", None, project_id="p1", worktree_id="wt-1",
                            derive_provider_from_profile=False)
    def fail_status(sid):
        raise RuntimeError("fixture unavailable")
    monkeypatch.setattr(streaming_status, "_has_active_run_db", fail_status)
    response = client.delete(f"{BASE}/wt-1")
    assert response.status_code == 409 and "could not be verified" in response.json()["detail"]
    assert (path / "wip.txt").read_text() == "wip\n"
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.ACTIVE


def test_cleanup_locked_worktree_preserves_dirty_files(wt_api):
    client, repo, path, _ = wt_api
    git(repo, "worktree", "lock", str(path))
    response = client.delete(f"{BASE}/wt-1")
    assert response.status_code == 409 and "locked" in response.json()["detail"]
    assert (path / "wip.txt").read_text() == "wip\n"


def test_get_worktree_and_foreign_ids_are_404(wt_api, mock_db):
    client, _, _, tmp_path = wt_api
    assert client.get(f"{BASE}/wt-1").json()["git_status"]["current_branch"] == "feature/api"
    assert client.get(f"{BASE}/nope").status_code == 404

    other = init_repo(tmp_path / "other")
    database.create_project("p2", "Other", None, str(other))
    worktree_manager._ensure_repository_record("p2", str(other))
    response = client.get("/api/v1/projects/p2/git/worktrees/wt-1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Worktree not found in this project: wt-1"


def test_git_routes_refuse_another_projects_worktree_id(wt_api, monkeypatch):
    """A key limited to p2 cannot reach p1's worktree by passing its id on p2's routes."""
    from app.api import common

    client, _, path, tmp_path = wt_api
    other = init_repo(tmp_path / "other")
    database.create_project("p2", "Other", None, str(other))
    worktree_manager._ensure_repository_record("p2", str(other))
    monkeypatch.setattr(common, "get_api_user_from_request", lambda request: {"project_id": "p2"})

    status_resp = client.get("/api/v1/projects/p2/git/status", params={"worktree_id": "wt-1"})
    assert status_resp.status_code == 404
    assert status_resp.json()["detail"] == "Worktree not found in this project: wt-1"

    stage = client.post(
        "/api/v1/projects/p2/git/stage", params={"worktree_id": "wt-1"}, json={"files": ["wip.txt"]},
    )
    assert stage.status_code == 404
    assert git(path, "status", "--porcelain").strip() == "?? wip.txt"

    # The project the key is limited to still works.
    assert client.get("/api/v1/projects/p2/git/status").status_code == 200


def test_worktree_file_browsing_is_confined(wt_api):
    client, _, path, tmp_path = wt_api
    (tmp_path / "outside").mkdir()

    root = client.get(f"{BASE}/wt-1/files").json()
    names = [f["name"] for f in root["files"]]
    assert names == ["docs", "a.txt", "wip.txt"]            # dirs first, hidden skipped
    assert root["worktreeId"] == "wt-1" and root["worktreePath"] == str(path)
    sub = client.get(f"{BASE}/wt-1/files", params={"path": "docs"}).json()
    assert [f["path"] for f in sub["files"]] == ["docs/guide.md"]

    for escape in ("..", "../..", "../../outside", str(tmp_path)):
        response = client.get(f"{BASE}/wt-1/files", params={"path": escape})
        assert response.status_code == 400, escape
        assert response.json()["detail"] == "Path escapes worktree boundary"
    assert client.get(f"{BASE}/wt-1/files", params={"path": "a.txt"}).status_code == 400
    assert client.get(f"{BASE}/wt-1/files", params={"path": "missing"}).status_code == 404


def test_delete_then_reopen_worktree(wt_api):
    client, repo, path, _ = wt_api

    deleted = client.delete(f"{BASE}/wt-1", params={"keep_branch": "false"})

    assert deleted.status_code == 200
    assert deleted.json()["message"] == "Removed worktree for branch 'feature/api' (branch deleted)"
    assert not path.exists()
    assert git(repo, "branch", "--list", "feature/api") == ""
    assert database.get_worktree("wt-1")["status"] == WorktreeStatus.COMPLETED

    reopened = client.post(f"{BASE}/wt-1/reopen")

    assert reopened.status_code == 200, reopened.text
    body = reopened.json()
    assert body["status"] == WorktreeStatus.ACTIVE
    assert body["worktree_path"] == str(repo / ".fluid" / "worktrees" / "feature-api")
    assert git(body["worktree_path"], "rev-parse", "--abbrev-ref", "HEAD") == "feature/api"


def test_delete_keeps_branch_by_default_and_reopen_errors_are_400(wt_api):
    client, repo, _, tmp_path = wt_api
    assert client.delete(f"{BASE}/wt-1").json()["message"].endswith("(branch kept)")
    assert git(repo, "branch", "--list", "feature/api").strip() == "feature/api"
    # A .fluid that points out of the checkout: reopen is a clean 400 and
    # nothing is written at the symlink target.
    shutil.rmtree(repo / ".fluid")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / ".fluid", target_is_directory=True)
    response = client.post(f"{BASE}/wt-1/reopen")
    assert response.status_code == 400
    assert "outside the project checkout" in response.json()["detail"]
    assert list(outside.iterdir()) == []

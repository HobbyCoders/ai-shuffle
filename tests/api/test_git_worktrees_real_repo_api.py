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
    app.dependency_overrides[git_worktrees.require_admin] = lambda: "test-owner"
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


@pytest.mark.parametrize("state", ["missing", "invalid", "broken_marker", "registry_only"])
def test_forget_stale_registration_keeps_files_branches_history_and_other_worktrees(wt_api, state):
    from pathlib import Path

    client, repo, path, tmp_path = wt_api
    admin = Path(git(path, "rev-parse", "--absolute-git-dir"))
    sibling = repo / ".fluid" / "worktrees" / "other"
    git(repo, "worktree", "add", "-q", "-b", "feature/other", str(sibling))
    sibling_marker = (sibling / ".git").read_bytes()
    database.create_session("forget-history", "default", project_id="p1", worktree_id="wt-1",
                            worktree_branch="feature/api", worktree_path=str(path))
    if state == "missing":
        path.rename(tmp_path / "kept-checkout")
    elif state == "registry_only":
        path = repo / ".claude" / "worktrees" / "registry-only"
        write(path / "precious.txt", "keep me")
        database.update_worktree("wt-1", worktree_path=str(path))
    elif state == "broken_marker":
        (path / ".git").unlink()  # Git marks this file hidden on Windows.
        (path / ".git").write_text("gitdir: /missing-fixture/admin\n")
    else:
        (path / ".git").unlink()
    before = {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()} if path.exists() else {}
    branch_head = git(repo, "rev-parse", "feature/api")

    response = client.post(f"{BASE}/wt-1/forget")

    assert response.status_code == 200, response.text
    assert database.get_worktree("wt-1") is None
    session = database.get_session("forget-history")
    assert session["worktree_branch"] == "feature/api" and session["worktree_path"]
    assert git(repo, "rev-parse", "feature/api") == branch_head
    assert (sibling / ".git").read_bytes() == sibling_marker
    assert git(sibling, "branch", "--show-current") == "feature/other"
    assert str(path).replace("\\", "/") not in git(repo, "worktree", "list", "--porcelain")
    if state != "registry_only":
        assert not admin.exists()
    else:
        assert admin.exists()  # unrelated Git registration was not pruned
    if before:
        assert {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()} == before
    assert client.get(BASE, params={"compact": "true"}).json()["worktrees"] == []


@pytest.mark.parametrize("state", ["valid", "switched", "locked", "admin_lock", "busy", "foreign", "outside"])
def test_forget_refuses_live_locked_busy_foreign_or_unmanaged_rows(wt_api, monkeypatch, state):
    from pathlib import Path
    from app.db import runs

    client, repo, path, tmp_path = wt_api
    admin = Path(git(path, "rev-parse", "--absolute-git-dir"))
    if state == "switched":
        git(path, "switch", "-q", "-c", "feature/switched")
    elif state in {"locked", "admin_lock", "busy"}:
        if state == "locked":
            git(repo, "worktree", "lock", str(path))
        elif state == "admin_lock":
            write(admin / "index.lock", "")
        else:
            database.create_session("forget-busy", "default", project_id="p1", worktree_id="wt-1")
            runs.claim_run("forget-busy", "fixture-runner", 60)
        (path / ".git").unlink()
    elif state == "foreign":
        foreign = init_repo(tmp_path / "foreign")
        (path / ".git").unlink()
        (path / ".git").write_text(f"gitdir: {foreign / '.git'}\n")
    elif state == "outside":
        path = tmp_path / "not-managed"
        write(path / "keep", "precious")
        database.update_worktree("wt-1", worktree_path=str(path))
    marker = (path / ".git").read_bytes() if (path / ".git").exists() else None

    response = client.post(f"{BASE}/wt-1/forget")

    assert response.status_code == 409, response.text
    assert database.get_worktree("wt-1") is not None
    assert path.exists() and admin.exists()
    assert git(repo, "rev-parse", "feature/api")
    if marker:
        assert (path / ".git").read_bytes() == marker


def test_forget_canonical_removes_legacy_alias_and_stops_resolver_warnings(wt_api, monkeypatch, caplog):
    import logging
    from app.core import project_resolver
    from app.core.config import settings
    from app.db import checkouts, projects_v2

    client, repo, path, _ = wt_api
    project = projects_v2.upsert_project("local:forget-canonical", name="Forget")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(repo))
    row = checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(path), branch="feature/api")
    (path / ".git").unlink()
    monkeypatch.setattr(project_resolver, "_refusals", {})
    session = {"project_v2_id": project["id"], "branch": "feature/api"}
    with caplog.at_level(logging.WARNING, logger=project_resolver.logger.name):
        for _ in range(10):
            assert project_resolver.resolve_existing_cwd(session) is None
        assert len([r for r in caplog.records if r.name == project_resolver.logger.name]) == 1
        response = client.post(f"/api/v1/projects/{project['id']}/git/worktrees/{row['id']}/forget")
        assert response.status_code == 200, response.text
        assert database.get_worktree("wt-1") is None
        assert len(checkouts.list_checkouts(project["id"])) == 1
        for _ in range(10):
            assert project_resolver.resolve_existing_cwd(session) is None
        assert len([r for r in caplog.records if r.name == project_resolver.logger.name]) == 1
    assert (path / "wip.txt").read_text() == "wip\n"
    assert client.get(f"/api/v1/projects/{project['id']}/git/worktrees", params={"compact": "true"}).json()["worktrees"] == []


def test_forget_wrong_project_and_worker_are_preserving_refusals(wt_api):
    from app.core.config import settings
    from app.db import checkouts, projects_v2

    client, repo, path, _ = wt_api
    assert client.post(f"{BASE}/unknown/forget").status_code == 404
    database.create_project("other-project", "Other", None, str(repo))
    assert client.post("/api/v1/projects/other-project/git/worktrees/wt-1/forget").status_code == 404
    project = projects_v2.upsert_project("local:foreign-forget", name="Foreign")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(repo))
    row = checkouts.upsert_checkout(project["id"], "other-runner", str(path), branch="feature/api")
    (path / ".git").unlink()
    assert client.post(f"/api/v1/projects/{project['id']}/git/worktrees/{row['id']}/forget").status_code == 409
    assert database.get_worktree("wt-1") is not None
    assert len(checkouts.list_checkouts(project["id"])) == 2


def test_forget_does_not_follow_checkout_dependency_or_admin_junctions(wt_api):
    import subprocess
    from pathlib import Path

    client, repo, path, tmp_path = wt_api
    dependency = tmp_path / "owned-dependency"
    write(dependency / "keep.txt", "keep")
    link = path / "node_modules"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(dependency)], check=True, capture_output=True)
    else:
        link.symlink_to(dependency, target_is_directory=True)
    admin = Path(git(path, "rev-parse", "--absolute-git-dir"))
    (path / ".git").unlink()
    assert client.post(f"{BASE}/wt-1/forget").status_code == 200
    assert (link / "keep.txt").read_text() == "keep"
    assert not admin.exists()


def test_forget_refuses_admin_link_before_removing_any_metadata(wt_api):
    import subprocess
    from pathlib import Path

    client, repo, path, tmp_path = wt_api
    admin = Path(git(path, "rev-parse", "--absolute-git-dir"))
    outside = tmp_path / "must-not-touch"
    write(outside / "keep.txt", "keep")
    link = admin / "unexpected-link"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    (path / ".git").unlink()
    before = (admin / "HEAD").read_bytes()
    response = client.post(f"{BASE}/wt-1/forget")
    assert response.status_code == 409, response.text
    assert database.get_worktree("wt-1")
    assert (admin / "HEAD").read_bytes() == before
    assert (outside / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("kind", ["known_socket", "already_stopped", "unknown_socket", "fifo", "stop_failure", "replaced_endpoint"])
def test_forget_validates_fsmonitor_ipc_before_removing_metadata(wt_api, monkeypatch, kind):
    import stat
    from pathlib import Path
    from types import SimpleNamespace
    import subprocess

    client, repo, path, _ = wt_api
    admin = Path(git(path, "rev-parse", "--absolute-git-dir"))
    # These cases model endpoint races/failures, not Git's live daemon. The
    # preservation tests above exercise the real fsmonitor-producing path.
    live_endpoint = admin / "fsmonitor--daemon.ipc"
    if live_endpoint.exists():
        git(path, "fsmonitor--daemon", "stop")
    git(repo, "config", "core.fsmonitor", "false")
    if live_endpoint.exists():
        live_endpoint.unlink()
    endpoint = admin / ("unexpected.ipc" if kind == "unknown_socket" else "fsmonitor--daemon.ipc")
    write(endpoint, "fixture endpoint")
    (path / ".git").unlink()
    lstat = Path.lstat
    mode = stat.S_IFIFO if kind == "fifo" else stat.S_IFSOCK
    stopped = []
    changed = False

    def endpoint_type(candidate, *args, **kwargs):
        if candidate == endpoint and not changed:
            return SimpleNamespace(st_mode=mode | 0o600, st_file_attributes=0, st_dev=lstat(candidate).st_dev)
        return lstat(candidate, *args, **kwargs)

    original_run = subprocess.run
    def stop_git(args, *positional, **kwargs):
        nonlocal changed
        if args[-2:] == ["fsmonitor--daemon", "stop"]:
            stopped.append(args)
            assert kwargs["env"]["LC_ALL"] == "C"
            if kind == "stop_failure":
                return subprocess.CompletedProcess(args, 128, "", "fatal: could not connect to daemon")
            if kind == "already_stopped":
                return subprocess.CompletedProcess(args, 128, "", "fatal: fsmonitor--daemon is not running\n")
            changed = kind == "replaced_endpoint"
            return subprocess.CompletedProcess(args, 0, "", "")
        return original_run(args, *positional, **kwargs)

    monkeypatch.setattr(Path, "lstat", endpoint_type)
    monkeypatch.setattr(subprocess, "run", stop_git)
    response = client.post(f"{BASE}/wt-1/forget")
    if kind in {"known_socket", "already_stopped"}:
        assert response.status_code == 200, response.text
        assert not admin.exists() and not database.get_worktree("wt-1")
    else:
        assert response.status_code == 409, response.text
        assert admin.exists() and (admin / "HEAD").exists()
        assert database.get_worktree("wt-1")
    if kind in {"unknown_socket", "fifo"}:
        assert not stopped  # No Git mutation before the manifest is validated.
    else:
        assert stopped == [["git", "-C", str(repo), f"--git-dir={admin}", f"--work-tree={repo}", "fsmonitor--daemon", "stop"]]
    assert (path / "wip.txt").read_text() == "wip\n"
    assert git(repo, "rev-parse", "feature/api")


@pytest.mark.parametrize("state", ["running", "draining", "turn_read_failure"])
@pytest.mark.parametrize("canonical", [False, True])
def test_forget_blocks_busy_branch_at_another_path(wt_api, monkeypatch, state, canonical):
    from app.db import checkouts, projects_v2, runs, turns
    from app.core.config import settings
    from app.core import git_activity
    from unittest.mock import MagicMock

    client, repo, path, tmp = wt_api
    project_id, checkout_id = "p1", "wt-1"
    if canonical:
        project = projects_v2.upsert_project("local:busy-other-path", name="Busy")
        project_id = project["id"]
        checkouts.upsert_checkout(project_id, settings.effective_instance_id, str(repo))
        checkout_id = checkouts.upsert_checkout(project_id, settings.effective_instance_id, str(path), branch="feature/api")["id"]
    other_path = str(tmp / "another-checkout")
    session_project = None
    if canonical:
        alias = projects_v2.upsert_project("local:busy-alias", name="Alias")
        session_project = alias["id"]
        checkouts.upsert_checkout(session_project, settings.effective_instance_id, str(repo))
        checkouts.upsert_checkout(session_project, settings.effective_instance_id, other_path, branch="feature/api")
    database.create_session("branch-busy", "default", project_id=None if canonical else "p1",
                            project_v2_id=session_project, worktree_path=other_path)
    database.set_session_branch("branch-busy", "feature/api", session_project)
    from app.core.worktree_report import _sessions_by_path, _path_key
    assert not _sessions_by_path().get(_path_key(str(path)))
    if state == "running":
        runs.claim_run("branch-busy", "fixture-runner", 60)
    else:
        turn = turns.enqueue_turn("branch-busy", "user", prompt="work")
        turns.start_turn(turn["id"], run_id="expired-run")
        turns.interrupt_turn("branch-busy", "stop")
        turns.escalate_to_draining(turn["id"])
        if state == "turn_read_failure":
            connection = MagicMock()
            cursor = connection.__enter__.return_value.cursor.return_value
            cursor.fetchone.return_value = None
            def execute(sql, *args):
                if "FROM turns" in sql:
                    raise RuntimeError("fixture turn read unavailable")
            cursor.execute.side_effect = execute
            monkeypatch.setattr(git_activity, "get_db", lambda: connection)
    (path / ".git").unlink()
    response = client.post(f"/api/v1/projects/{project_id}/git/worktrees/{checkout_id}/forget")
    assert response.status_code == 409, response.text
    assert database.get_worktree("wt-1")
    assert ("could not be verified" if state == "turn_read_failure" else "branch") in response.json()["detail"]
    assert "feature/api" in git(repo, "worktree", "list", "--porcelain")
    assert (path / "wip.txt").read_text() == "wip\n"


@pytest.mark.parametrize("identity", ["api_user", "paired_controller", "preview_owner"])
def test_forget_requires_owner_and_refuses_preview_cookie(wt_api, monkeypatch, identity):
    from app.api import auth
    from app.core import preview_access

    client, repo, path, _ = wt_api
    client.app.dependency_overrides.pop(git_worktrees.require_admin)
    monkeypatch.setattr(auth, "get_session_token", lambda request: None)
    monkeypatch.setattr(auth, "get_api_key", lambda request: "fixture-key")
    monkeypatch.setattr(auth, "get_paired_client_from_token", lambda *a, **k:
                        {"account_owner_admin": False} if identity == "paired_controller" else None)
    monkeypatch.setattr(auth.owner_auth, "owner_session", lambda *a: False)
    monkeypatch.setattr(auth.owner_auth, "local_access", lambda request: identity == "preview_owner")
    monkeypatch.setattr(preview_access, "verify", lambda token: object() if token == "fixture-preview" else None)
    if identity == "preview_owner":
        client.cookies.set(preview_access.COOKIE_NAME, "fixture-preview")
    (path / ".git").unlink()
    response = client.post(f"{BASE}/wt-1/forget")
    assert response.status_code == 403, response.text
    assert database.get_worktree("wt-1")
    assert "feature/api" in git(repo, "worktree", "list", "--porcelain")
    assert (path / "wip.txt").read_text() == "wip\n"


@pytest.mark.parametrize("state", ["foreign_device", "mount", "mount_during_delete", "inspection_error"])
def test_forget_refuses_foreign_admin_filesystems_and_inspection_errors(wt_api, monkeypatch, state):
    from pathlib import Path
    from types import SimpleNamespace
    from app.core import git_worktree_ops

    client, repo, path, _ = wt_api
    admin = Path(git(path, "rev-parse", "--absolute-git-dir"))
    foreign = admin / "foreign-files"
    write(foreign / "precious", "preserve")
    (path / ".git").unlink()
    lstat = Path.lstat
    read_text = Path.read_text
    unlink = Path.unlink
    deleting = False
    def changed_stat(candidate, *args, **kwargs):
        info = lstat(candidate, *args, **kwargs)
        if candidate == foreign:
            if state == "inspection_error":
                raise PermissionError("fixture inspection denied")
            if state == "foreign_device":
                return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev + 1, st_file_attributes=0)
        return info
    def mounted(candidate, *args, **kwargs):
        if candidate.as_posix() == "/proc/self/mountinfo":
            active = state == "mount" or (state == "mount_during_delete" and deleting)
            escaped = str(foreign).replace(" ", r"\040")
            return f"123 1 0:1 / {escaped} rw - tmpfs tmpfs rw\n" if active else ""
        return read_text(candidate, *args, **kwargs)
    def begin_delete(candidate, *args, **kwargs):
        nonlocal deleting
        deleting = True
        return unlink(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", changed_stat)
    monkeypatch.setattr(Path, "read_text", mounted)
    monkeypatch.setattr(Path, "unlink", begin_delete)
    if state.startswith("mount"):
        monkeypatch.setattr(git_worktree_ops.os.sys, "platform", "linux")
    response = client.post(f"{BASE}/wt-1/forget")
    assert response.status_code == 409, response.text
    assert database.get_worktree("wt-1")
    assert (foreign / "precious").read_text() == "preserve"
    assert admin.exists()
    assert (path / "wip.txt").read_text() == "wip\n"


@pytest.mark.parametrize("activity", ["run", "monitor", "agent", "sdk_shell"])
def test_forget_canonical_preserves_all_legacy_aliases_and_blocks_background_work(wt_api, monkeypatch, activity):
    from app.core import git_activity, worktree_report
    from app.core.config import settings
    from app.core.task_registry import TaskRegistry
    from app.db import checkouts, projects_v2, runs

    client, repo, path, tmp = wt_api
    registry = TaskRegistry()
    monkeypatch.setattr(git_activity, "task_registry", registry)
    for project_id, name in [("legacy-alpha", "Alpha"), ("legacy-zulu", "Zulu")]:
        database.create_project(project_id, name, None, str(repo))
        record = worktree_manager._ensure_repository_record(project_id, str(repo))
        database.create_worktree(project_id + "-wt", record["id"], "feature/api", str(path))
    project = projects_v2.upsert_project("local:shared-aliases", name="Canonical")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(repo))
    row = checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(path), branch="feature/api")
    source = next(s for s in worktree_report._project_repos() if s["path"] == str(repo.resolve()))
    assert {"legacy-alpha", "legacy-zulu", "p1", project["id"]} <= set(source["aliases"])
    assert {"legacy-alpha-wt", "legacy-zulu-wt", "wt-1", row["id"]} <= {
        registry_id for registered in source["registered"] for registry_id in registered["registry_ids"]
    }
    database.create_session("alpha-busy", "default", project_id="legacy-alpha",
                            worktree_path=str(tmp / "another-checkout"))
    database.set_session_branch("alpha-busy", "feature/api")
    assert not worktree_report._sessions_by_path().get(worktree_report._path_key(str(path)))
    run = None
    if activity == "run":
        run = runs.claim_run("alpha-busy", "fixture-runner", 60)
    else:
        registry.start_task("alpha-busy", "background-only", "agent" if activity == "agent" else "shell", "Working")
        if activity == "monitor":
            registry.mark_monitor_shell_task("alpha-busy", "background-only")
        elif activity == "sdk_shell":
            registry.mark_sdk_background_task("alpha-busy", "background-only")
        assert "alpha-busy" not in git_activity._live_session_ids()
    (path / ".git").unlink()
    response = client.post(f"/api/v1/projects/{project['id']}/git/worktrees/{row['id']}/forget")
    assert response.status_code == 409, response.text
    assert database.get_worktree("legacy-alpha-wt")
    assert database.get_worktree("legacy-zulu-wt")
    assert "feature/api" in git(repo, "worktree", "list", "--porcelain")
    assert (path / "wip.txt").read_text() == "wip\n"
    if run:
        runs.release_run(run["id"], run["fence_token"])
    else:
        registry.complete_task("alpha-busy", "background-only", "completed", notify=False)
    response = client.post(f"/api/v1/projects/{project['id']}/git/worktrees/{row['id']}/forget")
    assert response.status_code == 200, response.text
    assert database.get_worktree("legacy-alpha-wt") is None
    assert database.get_worktree("legacy-zulu-wt") is None
    assert database.get_worktree("wt-1") is None
    assert len(checkouts.list_checkouts(project["id"])) == 1
    assert (path / "wip.txt").read_text() == "wip\n"


def test_forget_busy_read_failure_preserves_metadata(wt_api, monkeypatch):
    from app.core import git_activity

    client, _, path, _ = wt_api
    database.create_session("forget-unavailable", "default", project_id="p1", worktree_id="wt-1")
    (path / ".git").unlink()
    def unavailable():
        raise RuntimeError("busy database unavailable")
    monkeypatch.setattr(git_activity, "_live_session_ids", unavailable)
    response = client.post(f"{BASE}/wt-1/forget")
    assert response.status_code == 409, response.text
    assert database.get_worktree("wt-1")


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

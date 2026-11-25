"""Removing a project deletes this worker's chats bound to its record, through the ordinary chat delete.

A chat bound only by a shared project identity is never deleted: the identity
does not prove which project record the chat came from (review round 2 S5).
"""
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks

from app.api import sessions as sessions_api
from app.core import project_removal
from app.db import database, projects_v2
from app.db.base import get_db
from tests.shared_fixtures import LOCAL_ME as ME, pin_runner  # noqa: F401

ORIGIN = "git:example/removal"


@pytest.fixture
def project(mock_db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.browser_runtime.close_session", AsyncMock())
    folder = tmp_path / "project"
    folder.mkdir()
    database.create_project("proj", "Proj", None, str(folder))
    alias = projects_v2.get_project_v2_by_origin("local:proj")["id"]
    database.set_project_link_group("proj", ORIGIN)
    current = projects_v2.get_project_v2_by_origin(ORIGIN)["id"]
    return folder, current, alias


def _set(session_id, **columns):
    with get_db() as conn:
        for column, value in columns.items():
            conn.cursor().execute(f"UPDATE sessions SET {column} = %s WHERE id = %s", (value, session_id))


async def test_removal_deletes_this_workers_chats_bound_to_the_project(project, tmp_path):
    folder, current, alias = project
    # This worker's chat of the project record, and one bound by shared id only (kept).
    database.create_session("legacy", "default", project_id="proj")
    database.create_session("shared", "default", project_v2_id=current)
    # A retained legacy worktree and its chat: the chat goes through the
    # ordinary delete, the worktree folder stays on disk.
    worktree_folder = tmp_path / "proj-topic"
    worktree_folder.mkdir()
    (worktree_folder / "work.txt").write_text("uncommitted")
    repo = database.create_git_repository("repo-proj", "proj")
    worktree = database.create_worktree("wt-proj", repo["id"], "topic", str(worktree_folder))
    # A cleaned-up worktree (folder already gone) is not listed as staying.
    database.create_worktree("wt-done", repo["id"], "done", str(tmp_path / "proj-done"), status="completed")
    database.create_session("in-worktree", "default", project_id="proj", worktree_id=worktree["id"])
    # The same logical project on another worker, a mirror another device
    # executes, a detached chat on the first-link alias, and another project.
    database.create_session("peer", "default", project_v2_id=current)
    _set("peer", instance_id="other-worker")
    database.create_session("peer-legacy", "default", project_id="proj")
    _set("peer-legacy", instance_id="other-worker")
    database.create_session("mirror", "default", project_v2_id=current)
    _set("mirror", owner_device_id="other-device")
    database.create_session("detached", "default", project_v2_id=alias)
    database.create_project("other", "Other", None, str(tmp_path))
    database.create_session("elsewhere", "default", project_id="other", project_v2_id=current)

    assert project_removal.preview(["proj"]) == {"chats": 2, "worktrees": [str(worktree_folder)]}
    tasks = BackgroundTasks()
    result = await project_removal.remove_projects(["proj"], tasks)

    assert result == {"removed": ["proj"], "chats_removed": 2}
    assert database.get_project("proj") is None
    for gone in ("legacy", "in-worktree"):
        assert database.get_session(gone) is None
    for kept in ("shared", "peer", "mirror", "detached", "elsewhere"):
        assert database.get_session(kept) is not None, kept
    # Another worker's legacy chat is not deleted; its row reference is cleared.
    assert database.get_session("peer-legacy")["project_id"] is None
    # Each removed chat got the ordinary teardown (workers, pool, transcript).
    cleaned = sorted(t.args[0] for t in tasks.tasks if t.func is sessions_api._cleanup_session_artifacts)
    assert cleaned == ["in-worktree", "legacy"]
    # The worktree records went with the project; the folder stays on disk.
    assert database.get_worktree("wt-proj") is None
    assert (worktree_folder / "work.txt").read_text() == "uncommitted"
    assert folder.is_dir()


def _relinked_clone(project, tmp_path):
    """Review round 2 S5: a chat bound only by origin R's shared id on clone A;
    A is relinked to S (the chat keeps R), then clone B registers at R here.
    B now holds R's identity, but A's chat was never B's."""
    _folder, current, _alias = project
    database.create_session("a-shared", "default", project_v2_id=current)
    database.set_project_link_group("proj", "git:example/relinked")
    folder_b = tmp_path / "clone-b"
    folder_b.mkdir()
    database.create_project("clone-b", "Clone B", None, str(folder_b))
    database.set_project_link_group("clone-b", ORIGIN)
    assert projects_v2.get_project_v2_by_origin(ORIGIN)["id"] == current
    database.create_session("b-legacy", "default", project_id="clone-b")
    return current


async def test_relinked_clones_shared_id_chat_survives_removing_the_new_clone(project, tmp_path):
    _relinked_clone(project, tmp_path)

    assert project_removal.preview(["clone-b"]) == {"chats": 1, "worktrees": []}
    assert (await project_removal.remove_projects(["clone-b"], BackgroundTasks()))["chats_removed"] == 1
    assert database.get_session("b-legacy") is None
    assert database.get_session("a-shared") is not None


async def test_shared_id_only_chat_survives_removing_every_local_clone(project, tmp_path):
    _folder, current, _alias = project
    database.create_project("clone", "Clone", None, str(tmp_path))
    database.set_project_link_group("clone", ORIGIN)
    database.create_session("shared", "default", project_v2_id=current)

    assert project_removal.preview(["proj", "clone"])["chats"] == 0
    assert (await project_removal.remove_projects(["proj", "clone"], BackgroundTasks()))["chats_removed"] == 0
    assert database.get_session("shared")["project_v2_id"] == current


def _owner_client(monkeypatch):
    """The projects router as the project page calls it, with owner auth."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import projects as projects_api
    from app.api.auth import require_admin

    monkeypatch.setattr(projects_api, "_announce_catalog_change", lambda: None)
    app = FastAPI()
    app.include_router(projects_api.router)
    app.dependency_overrides[require_admin] = lambda: "owner-token"
    return TestClient(app)


def test_project_page_delete_route_removes_exactly_this_workers_chats(project, monkeypatch):
    """``DELETE /api/v1/projects/{id}`` as the project page calls it, owner auth."""
    _, current, _ = project
    database.create_session("legacy", "default", project_id="proj")
    database.create_session("shared", "default", project_v2_id=current)
    database.create_session("peer-legacy", "default", project_id="proj")
    _set("peer-legacy", instance_id="other-worker")
    database.create_session("peer", "default", project_v2_id=current)
    _set("peer", instance_id="other-worker")

    with _owner_client(monkeypatch) as client:
        assert client.get("/api/v1/projects/proj/removal").json() == {
            "project_id": "proj", "chats": 1, "worktrees": []}
        assert client.delete("/api/v1/projects/proj").status_code == 204
        assert client.delete("/api/v1/projects/proj").status_code == 404

    assert database.get_session("legacy") is None
    assert database.get_session("shared") is not None
    assert database.get_session("peer") is not None
    assert database.get_session("peer-legacy")["project_id"] is None


def test_project_page_delete_route_keeps_a_relinked_clones_chat(project, tmp_path, monkeypatch):
    _relinked_clone(project, tmp_path)
    with _owner_client(monkeypatch) as client:
        assert client.get("/api/v1/projects/clone-b/removal").json()["chats"] == 1
        assert client.delete("/api/v1/projects/clone-b").status_code == 204
    assert database.get_session("b-legacy") is None
    assert database.get_session("a-shared") is not None


def _as_enrolled_worker(monkeypatch):
    """This worker's runner is device dev-me, as an enrolled worker sees itself."""
    from types import SimpleNamespace

    from app.network import engine, git as network_git

    me = SimpleNamespace(device_id="dev-me")
    monkeypatch.setattr(engine, "context", lambda: me)
    monkeypatch.setattr(engine, "_context", me)  # the chat delete's owner check reads it
    monkeypatch.setattr(network_git, "device_id", lambda runner_id: "dev-me" if runner_id == ME else runner_id)


async def test_peer_remove_action_removes_exactly_this_workers_chats(project, monkeypatch):
    """A peer's 'Remove project and chats from <worker>' reaches this worker as the
    ``project-setup`` control action ``remove``; it uses the same removal core."""
    from app.core import project_setup

    _as_enrolled_worker(monkeypatch)
    _, current, _ = project
    database.create_session("legacy", "default", project_id="proj")
    database.create_session("shared", "default", project_v2_id=current)
    database.create_session("peer", "default", project_v2_id=current)
    _set("peer", instance_id="other-worker")

    status = await project_setup.handle_control({"action": "status", "project_v2_id": current}, "dev-requester")
    assert status["detail"]["removal_chats"] == 1
    assert (await project_setup.handle_control(
        {"action": "remove", "project_v2_id": current}, "dev-requester"))["removed"]

    assert database.get_session("legacy") is None
    assert database.get_session("shared") is not None
    assert database.get_session("peer") is not None
    assert database.get_project("proj") is None


async def test_peer_remove_action_keeps_a_relinked_clones_chat(project, tmp_path, monkeypatch):
    from app.core import project_setup

    _as_enrolled_worker(monkeypatch)
    current = _relinked_clone(project, tmp_path)
    status = await project_setup.handle_control({"action": "status", "project_v2_id": current}, "dev-requester")
    assert status["detail"]["removal_chats"] == 1
    assert (await project_setup.handle_control(
        {"action": "remove", "project_v2_id": current}, "dev-requester"))["removed"]
    assert database.get_project("clone-b") is None
    assert database.get_project("proj") is not None
    assert database.get_session("b-legacy") is None
    assert database.get_session("a-shared") is not None


async def test_unlinked_folder_project_keeps_shared_id_only_chats(mock_db, tmp_path, monkeypatch):
    # A folder project's local:<id> identity can be an earlier project's
    # (same id reused), so a chat bound only by it is kept and not counted.
    monkeypatch.setattr("app.core.browser_runtime.close_session", AsyncMock())
    database.create_project("folder", "Folder", None, str(tmp_path))
    local = projects_v2.get_project_v2_by_origin("local:folder")["id"]
    database.create_session("legacy", "default", project_id="folder")
    database.create_session("shared-only", "default", project_v2_id=local)

    assert project_removal.preview(["folder"])["chats"] == 1
    assert (await project_removal.remove_projects(["folder"], BackgroundTasks()))["chats_removed"] == 1
    assert database.get_session("legacy") is None
    assert database.get_session("shared-only")["project_v2_id"] == local


async def test_chat_moved_after_the_plan_is_left_alone(project, monkeypatch):
    from app.db import projects

    database.create_session("legacy", "default", project_id="proj")
    original = projects.project_removal_plan

    def plan_then_move(ids):
        result = original(ids)
        _set("legacy", instance_id="other-worker")  # handed to another worker meanwhile
        return result

    monkeypatch.setattr(projects, "project_removal_plan", plan_then_move)
    result = await project_removal.remove_projects(["proj"], BackgroundTasks())
    assert result["chats_removed"] == 0
    assert database.get_session("legacy") is not None

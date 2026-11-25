"""Removing a project deletes exactly this worker's chats of it, through the ordinary chat delete."""
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


async def test_removal_deletes_this_workers_chats_however_bound(project, tmp_path):
    folder, current, alias = project
    # This worker's chats of the project: bound by legacy row, or by shared id only.
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

    assert project_removal.preview(["proj"]) == {"chats": 3, "worktrees": [str(worktree_folder)]}
    tasks = BackgroundTasks()
    result = await project_removal.remove_projects(["proj"], tasks)

    assert result == {"removed": ["proj"], "chats_removed": 3}
    assert database.get_project("proj") is None
    for gone in ("legacy", "shared", "in-worktree"):
        assert database.get_session(gone) is None
    for kept in ("peer", "mirror", "detached", "elsewhere"):
        assert database.get_session(kept) is not None, kept
    # Another worker's legacy chat is not deleted; its row reference is cleared.
    assert database.get_session("peer-legacy")["project_id"] is None
    # Each removed chat got the ordinary teardown (workers, pool, transcript).
    cleaned = sorted(t.args[0] for t in tasks.tasks if t.func is sessions_api._cleanup_session_artifacts)
    assert cleaned == ["in-worktree", "legacy", "shared"]
    # The worktree records went with the project; the folder stays on disk.
    assert database.get_worktree("wt-proj") is None
    assert (worktree_folder / "work.txt").read_text() == "uncommitted"
    assert folder.is_dir()


async def test_shared_id_chat_stays_while_another_local_clone_claims_it(project, tmp_path):
    _folder, current, _alias = project
    database.create_project("clone", "Clone", None, str(tmp_path))
    database.set_project_link_group("clone", ORIGIN)
    database.create_session("legacy", "default", project_id="proj")
    database.create_session("shared", "default", project_v2_id=current)

    assert project_removal.preview(["proj"])["chats"] == 1
    assert (await project_removal.remove_projects(["proj"], BackgroundTasks()))["chats_removed"] == 1
    assert database.get_session("shared") is not None
    # Removing every local record of the project together takes it too.
    assert project_removal.preview(["clone"])["chats"] == 1
    assert (await project_removal.remove_projects(["clone"], BackgroundTasks()))["chats_removed"] == 1
    assert database.get_session("shared") is None


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

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.sessions import update_workspace_folders
from app.core.models import WorkspaceFoldersUpdate
from app.core.workspace_folders import normalize_workspace_project_ids
from app.db import database as db


@pytest.mark.asyncio
async def test_updates_only_requested_chat_and_removal_keeps_project():
    db.create_profile(profile_id="folder-api", name="Folders", description=None, config={})
    db.create_project(project_id="folder-main", name="Main", description=None, path="/main")
    db.create_project(project_id="folder-docs", name="Docs", description=None, path="/docs")
    for sid in ("folders-a", "folders-b"):
        db.create_session(session_id=sid, profile_id="folder-api", project_id="folder-main")
    request = Request({"type": "http", "headers": []})
    with patch("app.api.sessions.require_session", AsyncMock(return_value=db.get_session("folders-a"))):
        result = await update_workspace_folders(request, "folders-a", WorkspaceFoldersUpdate(workspace_project_ids=["folder-main", "folder-docs", "folder-docs"]), "token")
        assert result == {"workspace_project_ids": ["folder-docs"]}
        assert normalize_workspace_project_ids(db.get_session("folders-a")["workspace_project_ids"]) == ["folder-docs"]
        assert normalize_workspace_project_ids(db.get_session("folders-b")["workspace_project_ids"]) == []
        await update_workspace_folders(request, "folders-a", WorkspaceFoldersUpdate(workspace_project_ids=[]), "token")
        assert db.get_project("folder-docs") is not None
        assert db.get_session("folders-a")["project_id"] == "folder-main"


@pytest.mark.asyncio
async def test_missing_project_rejected_without_saving():
    request = Request({"type": "http", "headers": []})
    with patch("app.api.sessions.require_session", AsyncMock(return_value={"project_id": "main"})), patch.object(db, "set_session_run_binding") as save:
        with pytest.raises(HTTPException) as exc:
            await update_workspace_folders(request, "chat-a", WorkspaceFoldersUpdate(workspace_project_ids=["missing"]), "token")
        assert exc.value.status_code == 404
        save.assert_not_called()


@pytest.mark.asyncio
async def test_multiple_deleted_projects_can_be_removed_one_at_a_time():
    request = Request({"type": "http", "headers": []})
    existing = {"project_id": "main", "workspace_project_ids": ["missing-a", "missing-b"]}
    with patch("app.api.sessions.require_session", AsyncMock(return_value=existing)), patch.object(db, "set_session_run_binding") as save:
        result = await update_workspace_folders(request, "chat-a", WorkspaceFoldersUpdate(workspace_project_ids=["missing-b"]), "token")
        assert result == {"workspace_project_ids": ["missing-b"]}
        save.assert_called_once_with("chat-a", workspace_project_ids=["missing-b"])

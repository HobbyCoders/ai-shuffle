"""Project tools use the REST creation path and the fixture database, never live state."""

import asyncio
import subprocess
import threading
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fluid.permissions.engine import PermissionEngine
from fluid.permissions.modes import MODE_POLICY, mode_rules
from fluid.tools.registry import ToolRegistry

from app.api import projects as projects_api
from app.api.auth import require_admin
from app.core import background_permissions, config, session_modes
from app.core.provider.fluid_tool_bridge import APP_CONFIG_RISK_MAP, register_sdk_tools
from app.core.tool_access import ToolAccess, tool_permitted
from app.db import database
from app.mcp.app_config_sdk import collect_app_config_tools, get_app_config_tool_names
from app.mcp.config_projects import _create_project_tools
from tests.mcp.tool_helpers import get_tool, tool_result_json


@pytest.fixture(autouse=True)
def workspace(mock_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "workspace_dir", tmp_path)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    monkeypatch.setattr(projects_api, "is_local_mode", lambda: False)
    monkeypatch.setattr(projects_api, "_announce_catalog_change", Mock())
    return tmp_path


@pytest.fixture
def api_client():
    app = FastAPI()
    app.include_router(projects_api.router)
    app.dependency_overrides[require_admin] = lambda: "fixture-owner"
    with TestClient(app) as client:
        yield client


async def call(name, args=None, *, admin=True):
    tool = get_tool(_create_project_tools(admin), name)
    return tool_result_json(await tool.handler(args or {}))


def test_registration_and_permission_categories():
    registry = ToolRegistry()
    tools = collect_app_config_tools(is_admin=True)
    names = {tool.name for tool in tools}
    assert {"listProjects", "createProject"} <= names
    register_sdk_tools(registry, "app-config", tools, risk_map=APP_CONFIG_RISK_MAP)
    access = session_modes.narrow_tool_access_for_mode(
        ToolAccess(allowed=None, disallowed=None), "chat", owner_session=True,
    )
    for name, risk, level in (
        ("listProjects", "safe", background_permissions.LOW),
        ("createProject", "write", background_permissions.HIGH),
    ):
        full_name = "mcp__app-config__" + name
        assert full_name in get_app_config_tool_names()
        assert registry.get(full_name).risk == risk
        assert background_permissions.declared(full_name)
        assert background_permissions.level(full_name, {}, {}) == level
        assert tool_permitted(full_name, access.allowed, access.disallowed)


@pytest.mark.parametrize("mode,expected", [
    ("default", "ask"), ("acceptEdits", "ask"),
    ("auto", "ask"), ("bypassPermissions", "allow"),
])
def test_native_approval_matches_other_config_writes(mode, expected):
    registry = ToolRegistry()
    register_sdk_tools(registry, "app-config", _create_project_tools(True), risk_map=APP_CONFIG_RISK_MAP)
    effective = session_modes.effective_permission_mode(mode, delegated=False)
    permissions = PermissionEngine(MODE_POLICY[effective], rules=mode_rules(effective))
    assert permissions.evaluate(registry.get("mcp__app-config__createProject"), {"name": "Example"}) == expected
    assert permissions.evaluate(registry.get("mcp__app-config__listProjects"), {}) == "allow"
    # Coordination chats keep the same high-risk owner confirmation in every mode.
    assert background_permissions.level("mcp__app-config__createProject", {}, {}) == background_permissions.HIGH
    delegated_mode = session_modes.effective_permission_mode(mode, delegated=True)
    assert session_modes.mode_approves_without_prompt(delegated_mode, "mcp__app-config__createProject") == (
        mode in ("auto", "bypassPermissions")
    )


@pytest.mark.asyncio
async def test_capabilities_advertise_project_tools():
    result = tool_result_json(await get_tool(collect_app_config_tools(True), "getConfigCapabilities").handler({}))
    assert result["projects"]["list_tool"] == "listProjects"
    assert result["projects"]["create_tool"] == "createProject"


@pytest.mark.asyncio
async def test_create_with_name_creates_workspace_folder_and_can_be_listed(workspace):
    result = await call("createProject", {"name": "New Project"})
    assert result == {
        "success": True, "project_id": "new-project", "name": "New Project",
        "path": str(workspace / "new-project"),
    }
    assert (workspace / "new-project").is_dir()
    stored = database.get_project(result["project_id"])
    assert stored["path"] == "new-project"
    assert stored["name"] == result["name"]
    projects_api._announce_catalog_change.assert_called_once()
    listed = await call("listProjects")
    assert listed == {"projects": [{"id": result["project_id"], "name": result["name"], "path": result["path"]}], "count": 1}
    # Listing is a read, not the REST listing's optional link-group backfill.
    projects_api._announce_catalog_change.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [False, True])
async def test_existing_folder_is_attached_without_moving_files(workspace, monkeypatch, local):
    folder = workspace / "existing"
    folder.mkdir()
    (folder / "keep.txt").write_text("unchanged")
    monkeypatch.setattr(projects_api, "is_local_mode", lambda: local)
    result = await call("createProject", {
        "id": "chosen-id", "name": "Existing Project", "path": str(folder), "description": "Attached folder",
    })
    assert result["success"] is True
    assert result["project_id"] == "chosen-id"
    stored = database.get_project("chosen-id")
    assert stored["path"] == (str(folder) if local else "existing")
    assert stored["description"] == "Attached folder"
    assert (folder / "keep.txt").read_text() == "unchanged"


@pytest.mark.asyncio
async def test_local_absolute_folder_outside_workspace_is_supported(workspace, monkeypatch, tmp_path_factory):
    folder = tmp_path_factory.mktemp("project-outside-workspace")
    monkeypatch.setattr(projects_api, "is_local_mode", lambda: True)
    result = await call("createProject", {"name": "Outside", "path": str(folder)})
    assert result["success"] is True
    assert database.get_project("outside")["path"] == str(folder)


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", ["id", "name", "path"])
async def test_duplicate_created_by_rest_is_refused_by_tool(api_client, workspace, duplicate):
    response = api_client.post("/api/v1/projects", json={"id": "existing", "name": "Existing", "path": "folder"})
    assert response.status_code == 201
    args = {"id": "different", "name": "Different", "path": "different-folder"}
    if duplicate == "id":
        args["id"] = "existing"
    elif duplicate == "name":
        args["name"] = " existing "
    else:
        args["path"] = str(workspace / "folder")
    result = await call("createProject", args)
    assert result["success"] is False
    assert "already exists" in result["error"]
    assert "existing" in result["error"]
    assert len(database.get_all_projects()) == 1
    assert not (workspace / "different-folder").exists()


@pytest.mark.asyncio
async def test_rest_refuses_tool_created_project_at_same_path(api_client):
    assert (await call("createProject", {"name": "Existing", "path": "folder"}))["success"]
    response = api_client.post("/api/v1/projects", json={"id": "other", "name": "Other", "path": "folder"})
    assert response.status_code == 409
    assert "path" in response.json()["detail"]
    assert len(database.get_all_projects()) == 1


@pytest.mark.asyncio
async def test_symlink_alias_is_a_duplicate(workspace, requires_symlinks):
    assert (await call("createProject", {"name": "Existing", "path": "folder"}))["success"]
    (workspace / "alias").symlink_to(workspace / "folder", target_is_directory=True)
    result = await call("createProject", {"name": "Other", "path": "alias"})
    assert result["success"] is False
    assert "already exists at path" in result["error"]


@pytest.mark.asyncio
async def test_cross_install_id_collision_keeps_server_assigned_id(mock_db):
    database.create_project("shared", "Shared", None, "shared")
    mock_db.execute("UPDATE projects SET instance_id = ? WHERE id = ?", ("other-worker", "shared"))
    result = await call("createProject", {"name": "Shared"})
    assert result["success"] is True
    assert result["project_id"].startswith("shared-")
    assert result["project_id"] != "shared"
    assert database.get_project(result["project_id"])["name"] == "Shared"
    assert (await call("listProjects"))["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [
    ("escape", "boundary"), ("absolute", "Absolute paths"), ("file", "not a directory"),
])
async def test_invalid_path_never_creates_a_project(workspace, kind, expected):
    paths = {"escape": "../outside", "absolute": str(workspace.parent / "outside"), "file": "file.txt"}
    (workspace / "file.txt").write_text("keep")
    result = await call("createProject", {"name": "Invalid", "path": paths[kind]})
    assert result["success"] is False
    assert expected in result["error"]
    assert database.get_all_projects() == []
    projects_api._announce_catalog_change.assert_not_called()


@pytest.mark.asyncio
async def test_nested_repository_path_is_refused(workspace):
    repo = workspace / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "child").mkdir()
    result = await call("createProject", {"name": "Nested", "path": "repo/child"})
    assert result["success"] is False
    assert "repository root" in result["error"]
    assert database.get_all_projects() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"name": " "}, {"name": "!"}, {"name": "a" * 101}, {"name": "OK", "id": "Invalid ID"},
])
async def test_project_create_model_validation_is_used(args):
    result = await call("createProject", args)
    assert result["success"] is False
    assert "validation error" in result["error"]
    assert database.get_all_projects() == []


@pytest.mark.asyncio
async def test_non_admin_cannot_create_but_can_list(workspace):
    result = await call("createProject", {"name": "Denied"}, admin=False)
    assert result["success"] is False
    assert "admin access" in result["error"]
    assert not (workspace / "denied").exists()
    assert (await call("listProjects", admin=False))["count"] == 0


@pytest.mark.asyncio
async def test_creation_keeps_database_work_off_the_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    threads = []
    for name in ("get_project", "get_all_projects", "create_project"):
        original = getattr(database, name)

        def record(*args, _original=original, **kwargs):
            threads.append(threading.get_ident())
            return _original(*args, **kwargs)

        monkeypatch.setattr(database, name, record)
    assert (await call("createProject", {"name": "Offloaded"}))["success"]
    assert threads and loop_thread not in threads
    assert len(set(threads)) == 1


@pytest.mark.asyncio
async def test_concurrent_creates_cannot_register_the_same_folder_twice():
    results = await asyncio.gather(*[
        call("createProject", {"name": name, "path": "shared-folder"})
        for name in ("First", "Second")
    ])
    assert sum(result["success"] for result in results) == 1
    assert "already exists at path" in next(result["error"] for result in results if not result["success"])
    assert len(database.get_all_projects()) == 1

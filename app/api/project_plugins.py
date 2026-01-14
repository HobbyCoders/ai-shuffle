"""
Project plugins API routes - manages project-scoped plugins.

Plugins can be installed to:
- LOCAL scope: <project>/.claude/plugins/ (gitignored)
- PROJECT scope: <project>/plugins/ (committed to git)
"""

from pathlib import Path
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field

from app.db import database
from app.api.auth import require_auth, get_api_user_from_request
from app.core.plugin_service import (
    get_plugin_service,
    PluginService,
    PluginScope,
    PluginInfo,
    AvailablePlugin,
)

router = APIRouter(prefix="/api/v1/projects", tags=["Project Plugins"])


# ============================================================================
# Request/Response Models
# ============================================================================

class ProjectPluginResponse(BaseModel):
    """Plugin response for project-scoped plugins"""
    id: str
    name: str
    marketplace: str
    description: str
    version: str
    scope: str  # "local" or "project"
    install_path: str
    installed_at: Optional[datetime] = None
    enabled: bool
    has_commands: bool = False
    has_agents: bool = False
    has_skills: bool = False
    has_hooks: bool = False


class AvailablePluginResponse(BaseModel):
    """Available plugin from marketplace"""
    id: str
    name: str
    marketplace: str
    marketplace_path: str
    description: str
    has_commands: bool = False
    has_agents: bool = False
    has_skills: bool = False
    has_hooks: bool = False
    installed: bool = False  # Already installed in project?
    enabled: bool = False


class InstallPluginRequest(BaseModel):
    """Request body for installing a plugin"""
    plugin_id: str = Field(..., description="Plugin ID in format 'name@marketplace'")
    scope: str = Field(
        "local",
        description="Installation scope: 'local' (.claude/plugins/) or 'project' (plugins/)"
    )


# ============================================================================
# Helper Functions
# ============================================================================

def _validate_project_access(request: Request, project_id: str) -> dict:
    """
    Validate that the requester has access to the project.

    Returns the project dict if valid.
    """
    api_user = get_api_user_from_request(request)
    if api_user and api_user.get("project_id"):
        # API user restricted to specific project
        if api_user["project_id"] != project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: You can only access plugins for your assigned project"
            )

    # Verify project exists
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    return project


def _get_project_path(project: dict) -> Path:
    """Get the full path to the project directory"""
    from app.core.config import settings
    return settings.effective_workspace_dir / project["path"]


def _get_project_plugin_service(project: dict) -> PluginService:
    """Get a PluginService instance configured for the project"""
    project_path = _get_project_path(project)
    return get_plugin_service(project_dir=project_path)


def _plugin_to_response(plugin: PluginInfo) -> ProjectPluginResponse:
    """Convert PluginInfo to response model"""
    return ProjectPluginResponse(
        id=plugin.id,
        name=plugin.name,
        marketplace=plugin.marketplace,
        description=plugin.description,
        version=plugin.version,
        scope=plugin.scope,
        install_path=plugin.install_path,
        installed_at=plugin.installed_at,
        enabled=plugin.enabled,
        has_commands=plugin.has_commands,
        has_agents=plugin.has_agents,
        has_skills=plugin.has_skills,
        has_hooks=plugin.has_hooks,
    )


def _available_to_response(
    plugin: AvailablePlugin,
    installed_ids: set
) -> AvailablePluginResponse:
    """Convert AvailablePlugin to response model"""
    return AvailablePluginResponse(
        id=plugin.id,
        name=plugin.name,
        marketplace=plugin.marketplace,
        marketplace_path=plugin.marketplace_path,
        description=plugin.description,
        has_commands=plugin.has_commands,
        has_agents=plugin.has_agents,
        has_skills=plugin.has_skills,
        has_hooks=plugin.has_hooks,
        installed=plugin.id in installed_ids or plugin.name in installed_ids,
        enabled=plugin.enabled,
    )


# ============================================================================
# API Endpoints
# ============================================================================

@router.get("/{project_id}/plugins/installed", response_model=List[ProjectPluginResponse])
async def list_project_plugins(
    project_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    List all plugins installed in the project.

    Returns plugins from both LOCAL (.claude/plugins/) and PROJECT (plugins/) scopes.
    """
    project = _validate_project_access(request, project_id)
    service = _get_project_plugin_service(project)

    plugins = service.get_project_installed_plugins()
    return [_plugin_to_response(p) for p in plugins]


@router.get("/{project_id}/plugins/available", response_model=List[AvailablePluginResponse])
async def list_available_plugins(
    project_id: str,
    request: Request,
    marketplace: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    List plugins available for installation from marketplaces.

    Optionally filter by marketplace ID.
    """
    project = _validate_project_access(request, project_id)
    service = _get_project_plugin_service(project)

    # Get available plugins from global service (marketplaces are global)
    global_service = get_plugin_service()
    available = global_service.get_available_plugins(marketplace)

    # Get installed project plugins to mark which are already installed
    installed = service.get_project_installed_plugins()
    installed_names = {p.name for p in installed}
    installed_ids = {p.id for p in installed}

    return [_available_to_response(p, installed_ids | installed_names) for p in available]


@router.post("/{project_id}/plugins/install", response_model=ProjectPluginResponse, status_code=status.HTTP_201_CREATED)
async def install_project_plugin(
    project_id: str,
    install_data: InstallPluginRequest,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    Install a plugin to the project.

    The plugin is copied from the marketplace to the project directory.
    Use scope "local" for .claude/plugins/ (gitignored) or "project" for plugins/ (committed).
    """
    project = _validate_project_access(request, project_id)
    service = _get_project_plugin_service(project)

    # Map scope string to enum
    if install_data.scope == "local":
        scope = PluginScope.LOCAL
    elif install_data.scope == "project":
        scope = PluginScope.PROJECT
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid scope: {install_data.scope}. Must be 'local' or 'project'."
        )

    try:
        plugin = service.install_plugin_to_project(install_data.plugin_id, scope)
        return _plugin_to_response(plugin)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.delete("/{project_id}/plugins/{plugin_id}", status_code=status.HTTP_204_NO_CONTENT)
async def uninstall_project_plugin(
    project_id: str,
    plugin_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    Uninstall a plugin from the project.

    Removes the plugin directory from the project.
    """
    project = _validate_project_access(request, project_id)
    service = _get_project_plugin_service(project)

    try:
        service.uninstall_project_plugin(plugin_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.post("/{project_id}/plugins/{plugin_id}/enable", status_code=status.HTTP_204_NO_CONTENT)
async def enable_project_plugin(
    project_id: str,
    plugin_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    Enable a plugin in the project.
    """
    project = _validate_project_access(request, project_id)
    service = _get_project_plugin_service(project)

    # Verify plugin exists
    plugins = service.get_project_installed_plugins()
    plugin_ids = {p.id for p in plugins}

    if plugin_id not in plugin_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin not found: {plugin_id}"
        )

    service.enable_project_plugin(plugin_id)


@router.post("/{project_id}/plugins/{plugin_id}/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_project_plugin(
    project_id: str,
    plugin_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    Disable a plugin in the project.
    """
    project = _validate_project_access(request, project_id)
    service = _get_project_plugin_service(project)

    # Verify plugin exists
    plugins = service.get_project_installed_plugins()
    plugin_ids = {p.id for p in plugins}

    if plugin_id not in plugin_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin not found: {plugin_id}"
        )

    service.disable_project_plugin(plugin_id)

"""
Plugin Management API Routes

Provides REST endpoints for managing Claude Code plugins including:
- Marketplace management (list, add, remove, sync)
- Plugin discovery (available plugins from marketplaces)
- Plugin installation (install, uninstall, batch operations)
- Plugin enablement (enable/disable globally)
- File-based agents from plugins
"""

from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

from app.api.auth import require_auth, require_admin
from app.core.plugin_service import (
    get_plugin_service,
    MarketplaceInfo,
    PluginInfo,
    PluginDetails,
    AvailablePlugin
)

router = APIRouter(prefix="/api/v1/plugins", tags=["Plugins"])


# ============================================================================
# Request/Response Models
# ============================================================================

class MarketplaceResponse(BaseModel):
    """Response model for marketplace info"""
    id: str
    source: str
    url: str
    install_location: str
    last_updated: Optional[datetime] = None


class MarketplaceAddRequest(BaseModel):
    """Request to add a new marketplace"""
    url: str = Field(..., description="Git repository URL for the marketplace")
    name: Optional[str] = Field(None, description="Optional name (derived from URL if not provided)")


class PluginResponse(BaseModel):
    """Response model for installed plugin info"""
    id: str
    name: str
    marketplace: str
    description: str
    version: str
    scope: str
    install_path: str
    installed_at: Optional[datetime] = None
    enabled: bool
    has_commands: bool
    has_agents: bool
    has_skills: bool
    has_hooks: bool


class PluginDetailsResponse(PluginResponse):
    """Response model for detailed plugin info"""
    commands: List[str]
    agents: List[str]
    skills: List[str]
    readme: Optional[str] = None


class AvailablePluginResponse(BaseModel):
    """Response model for available (not necessarily installed) plugin"""
    id: str
    name: str
    marketplace: str
    marketplace_path: str
    description: str
    has_commands: bool
    has_agents: bool
    has_skills: bool
    has_hooks: bool
    installed: bool
    enabled: bool


class PluginInstallRequest(BaseModel):
    """Request to install a plugin"""
    plugin_id: str = Field(..., description="Plugin ID in format 'name@marketplace'")
    scope: str = Field("user", description="Installation scope: 'user' or 'project'")


class PluginBatchInstallRequest(BaseModel):
    """Request to install multiple plugins"""
    plugin_ids: List[str] = Field(..., description="List of plugin IDs to install")
    scope: str = Field("user", description="Installation scope")


class FileAgentResponse(BaseModel):
    """Response model for file-based agent from plugin"""
    id: str
    name: str
    description: str
    model: Optional[str] = None
    tools: Optional[List[str]] = None
    prompt: str
    source_plugin: str
    file_path: str


class PluginEnableStateResponse(BaseModel):
    """Response for plugin enable states"""
    enabled_plugins: dict


# ============================================================================
# Marketplace Endpoints
# ============================================================================

@router.get("/marketplaces", response_model=List[MarketplaceResponse])
async def list_marketplaces(token: str = Depends(require_auth)):
    """List all registered plugin marketplaces"""
    service = get_plugin_service()
    marketplaces = service.get_marketplaces()
    return [
        MarketplaceResponse(
            id=mp.id,
            source=mp.source,
            url=mp.url,
            install_location=mp.install_location,
            last_updated=mp.last_updated
        )
        for mp in marketplaces
    ]


@router.post("/marketplaces", response_model=MarketplaceResponse, status_code=status.HTTP_201_CREATED)
async def add_marketplace(
    request: MarketplaceAddRequest,
    token: str = Depends(require_admin)
):
    """Add a new plugin marketplace from a git repository - Admin only"""
    service = get_plugin_service()
    try:
        mp = service.add_marketplace(request.url, request.name)
        return MarketplaceResponse(
            id=mp.id,
            source=mp.source,
            url=mp.url,
            install_location=mp.install_location,
            last_updated=mp.last_updated
        )
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.delete("/marketplaces/{marketplace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_marketplace(
    marketplace_id: str,
    token: str = Depends(require_admin)
):
    """Remove a plugin marketplace and all its installed plugins - Admin only"""
    service = get_plugin_service()
    try:
        service.remove_marketplace(marketplace_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.post("/marketplaces/{marketplace_id}/sync", status_code=status.HTTP_200_OK)
async def sync_marketplace(
    marketplace_id: str,
    token: str = Depends(require_admin)
):
    """Sync a marketplace with its remote repository - Admin only"""
    service = get_plugin_service()
    try:
        service.sync_marketplace(marketplace_id)
        return {"status": "synced", "marketplace_id": marketplace_id}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


# ============================================================================
# Available Plugins Endpoints
# ============================================================================

@router.get("/available", response_model=List[AvailablePluginResponse])
async def list_available_plugins(
    marketplace: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """List all available plugins from marketplaces"""
    service = get_plugin_service()
    plugins = service.get_available_plugins(marketplace)
    return [
        AvailablePluginResponse(
            id=p.id,
            name=p.name,
            marketplace=p.marketplace,
            marketplace_path=p.marketplace_path,
            description=p.description,
            has_commands=p.has_commands,
            has_agents=p.has_agents,
            has_skills=p.has_skills,
            has_hooks=p.has_hooks,
            installed=p.installed,
            enabled=p.enabled
        )
        for p in plugins
    ]


# ============================================================================
# Installed Plugins Endpoints
# ============================================================================

@router.get("/installed", response_model=List[PluginResponse])
async def list_installed_plugins(token: str = Depends(require_auth)):
    """List all installed plugins"""
    service = get_plugin_service()
    plugins = service.get_installed_plugins()
    return [
        PluginResponse(
            id=p.id,
            name=p.name,
            marketplace=p.marketplace,
            description=p.description,
            version=p.version,
            scope=p.scope,
            install_path=p.install_path,
            installed_at=p.installed_at,
            enabled=p.enabled,
            has_commands=p.has_commands,
            has_agents=p.has_agents,
            has_skills=p.has_skills,
            has_hooks=p.has_hooks
        )
        for p in plugins
    ]


# ============================================================================
# File-based Agents Endpoints
# NOTE: This MUST be defined BEFORE the catch-all /{plugin_id:path} route
# ============================================================================

@router.get("/agents/file-based", response_model=List[FileAgentResponse])
async def list_file_based_agents(token: str = Depends(require_auth)):
    """List all file-based agents from enabled plugins"""
    service = get_plugin_service()
    agents = service.get_file_based_agents()
    return [
        FileAgentResponse(
            id=a["id"],
            name=a["name"],
            description=a.get("description", ""),
            model=a.get("model"),
            tools=a.get("tools"),
            prompt=a.get("prompt", ""),
            source_plugin=a["source_plugin"],
            file_path=a["file_path"]
        )
        for a in agents
    ]


@router.get("/{plugin_id:path}", response_model=PluginDetailsResponse)
async def get_plugin_details(
    plugin_id: str,
    token: str = Depends(require_auth)
):
    """Get detailed information about a specific plugin"""
    service = get_plugin_service()
    plugin = service.get_plugin_details(plugin_id)

    if not plugin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin not found: {plugin_id}"
        )

    return PluginDetailsResponse(
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
        commands=plugin.commands,
        agents=plugin.agents,
        skills=plugin.skills,
        readme=plugin.readme
    )


# ============================================================================
# Plugin Installation Endpoints
# ============================================================================

@router.post("/install", response_model=PluginResponse, status_code=status.HTTP_201_CREATED)
async def install_plugin(
    request: PluginInstallRequest,
    token: str = Depends(require_admin)
):
    """Install a plugin from a marketplace - Admin only"""
    service = get_plugin_service()
    try:
        plugin = service.install_plugin(request.plugin_id, request.scope)
        return PluginResponse(
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
            has_hooks=plugin.has_hooks
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/install/batch", response_model=List[PluginResponse], status_code=status.HTTP_201_CREATED)
async def install_plugins_batch(
    request: PluginBatchInstallRequest,
    token: str = Depends(require_admin)
):
    """Install multiple plugins in batch - Admin only"""
    service = get_plugin_service()
    plugins = service.install_plugins_batch(request.plugin_ids, request.scope)
    return [
        PluginResponse(
            id=p.id,
            name=p.name,
            marketplace=p.marketplace,
            description=p.description,
            version=p.version,
            scope=p.scope,
            install_path=p.install_path,
            installed_at=p.installed_at,
            enabled=p.enabled,
            has_commands=p.has_commands,
            has_agents=p.has_agents,
            has_skills=p.has_skills,
            has_hooks=p.has_hooks
        )
        for p in plugins
    ]


@router.delete("/{plugin_id:path}", status_code=status.HTTP_204_NO_CONTENT)
async def uninstall_plugin(
    plugin_id: str,
    token: str = Depends(require_admin)
):
    """Uninstall a plugin - Admin only"""
    service = get_plugin_service()
    try:
        service.uninstall_plugin(plugin_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


# ============================================================================
# Plugin Enable/Disable Endpoints
# ============================================================================

@router.post("/{plugin_id:path}/enable", status_code=status.HTTP_200_OK)
async def enable_plugin(
    plugin_id: str,
    token: str = Depends(require_admin)
):
    """Enable a plugin globally - Admin only"""
    service = get_plugin_service()
    success = service.enable_plugin(plugin_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enable plugin"
        )
    return {"status": "enabled", "plugin_id": plugin_id}


@router.post("/{plugin_id:path}/disable", status_code=status.HTTP_200_OK)
async def disable_plugin(
    plugin_id: str,
    token: str = Depends(require_admin)
):
    """Disable a plugin globally - Admin only"""
    service = get_plugin_service()
    success = service.disable_plugin(plugin_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to disable plugin"
        )
    return {"status": "disabled", "plugin_id": plugin_id}


@router.get("/state/enabled", response_model=PluginEnableStateResponse)
async def get_enabled_plugins(token: str = Depends(require_auth)):
    """Get all plugin enable/disable states"""
    service = get_plugin_service()
    enabled = service.get_enabled_plugins()
    return PluginEnableStateResponse(enabled_plugins=enabled)

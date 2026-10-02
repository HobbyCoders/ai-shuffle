"""
Plugin Management API Routes

Provides REST endpoints for managing Claude Code plugins including:
- Marketplace management (list, add, remove, sync)
- Plugin discovery (available plugins from marketplaces)
- Plugin installation (install, uninstall, batch operations)
- Plugin enablement (enable/disable globally)
- File-based agents from plugins
"""

import logging
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from app.api.auth import require_auth, require_admin
from app.db.async_database import run_sync
from app.api.common import api_error_handler
from app.core.plugin_service import (
    get_plugin_service
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


class PluginComponentResponse(BaseModel):
    """One component of a plugin and whether this app runs it."""
    kind: str
    name: str
    support: str  # loaded | unsupported | needs_config
    reason: str = ""


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
    has_mcp: bool = False
    connector_type: Optional[str] = None
    # Optional additions (#1610): package format, trust level and every
    # component with its support status. Older clients ignore them.
    format: Optional[str] = None
    trust: Optional[str] = None
    components: List[PluginComponentResponse] = Field(default_factory=list)


def _record_fields(plugin) -> dict:
    """The optional format/trust/components fields of an installed plugin."""
    def text(name: str) -> Optional[str]:
        value = getattr(plugin, name, None)
        return value if isinstance(value, str) else None

    components = getattr(plugin, "components", None)
    return {
        "format": text("format"),
        "trust": text("trust"),
        "components": components if isinstance(components, list) else [],
    }


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
    has_mcp: bool = False
    connector_type: Optional[str] = None
    installed: bool
    enabled: bool
    format: Optional[str] = None
    components: List[PluginComponentResponse] = Field(default_factory=list)


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


class PluginUpdateInfoResponse(BaseModel):
    """Response model for a plugin update"""
    plugin_id: str
    name: str
    marketplace: str
    installed_version: str
    available_version: str
    is_local: bool


class PluginUpdateRequest(BaseModel):
    """Request to update a specific plugin"""
    plugin_id: str = Field(..., description="Plugin ID in format 'name@marketplace'")


class AutoUpdateSettingsResponse(BaseModel):
    """Response model for auto-update settings"""
    enabled: bool
    interval_hours: int
    auto_sync_marketplaces: bool
    auto_update_plugins: bool
    last_check: Optional[str] = None


class AutoUpdateSettingsRequest(BaseModel):
    """Request to update auto-update settings"""
    enabled: Optional[bool] = None
    interval_hours: Optional[int] = Field(None, ge=1, le=168)  # 1h to 1 week
    auto_sync_marketplaces: Optional[bool] = None
    auto_update_plugins: Optional[bool] = None


class AutoUpdateRunResponse(BaseModel):
    """Response from running auto-update"""
    synced_marketplaces: dict
    updates_available: list
    updated_plugins: list
    errors: list


# ============================================================================
# Marketplace Endpoints
# ============================================================================

@router.get("/marketplaces", response_model=List[MarketplaceResponse])
async def list_marketplaces(token: str = Depends(require_auth)):
    """List all registered plugin marketplaces"""
    async with api_error_handler("load marketplaces"):
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
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except RuntimeError:
        logger.exception("Failed to add marketplace")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add marketplace"
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
    except RuntimeError:
        logger.exception("Failed to sync marketplace")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to sync marketplace"
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
    async with api_error_handler("load available plugins"):
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
                connector_type=p.connector_type,
                has_mcp=p.has_mcp,
                installed=p.installed,
                enabled=p.enabled,
                format=_record_fields(p)["format"],
                components=_record_fields(p)["components"],
            )
            for p in plugins
        ]


# ============================================================================
# Installed Plugins Endpoints
# ============================================================================

@router.get("/installed", response_model=List[PluginResponse])
async def list_installed_plugins(token: str = Depends(require_auth)):
    """List all installed plugins"""
    async with api_error_handler("load installed plugins"):
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
                has_hooks=p.has_hooks,
                connector_type=p.connector_type,
                has_mcp=p.has_mcp,
                **_record_fields(p)
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
    async with api_error_handler("load file-based agents"):
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


# ============================================================================
# Plugin Update Endpoints
# NOTE: These MUST be defined BEFORE the catch-all /{plugin_id:path} route
# ============================================================================

@router.get("/updates", response_model=List[PluginUpdateInfoResponse])
async def check_for_updates(token: str = Depends(require_auth)):
    """Check all installed plugins for available updates"""
    async with api_error_handler("check for updates"):
        service = get_plugin_service()
        updates = service.check_for_updates()
        return [
            PluginUpdateInfoResponse(
                plugin_id=u.plugin_id,
                name=u.name,
                marketplace=u.marketplace,
                installed_version=u.installed_version,
                available_version=u.available_version,
                is_local=u.is_local
            )
            for u in updates
        ]


@router.post("/update", response_model=PluginResponse, status_code=status.HTTP_200_OK)
async def update_plugin(
    request: PluginUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update a specific plugin to the latest version - Admin only"""
    async with api_error_handler("update plugin"):
        service = get_plugin_service()
        try:
            plugin = await run_sync(service.update_plugin, request.plugin_id)
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
                has_hooks=plugin.has_hooks,
                connector_type=plugin.connector_type, has_mcp=plugin.has_mcp,
                **_record_fields(plugin)
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e)
            )


@router.post("/update/all", response_model=List[PluginResponse], status_code=status.HTTP_200_OK)
async def update_all_plugins(token: str = Depends(require_admin)):
    """Update all plugins that have available updates - Admin only"""
    async with api_error_handler("update all plugins"):
        service = get_plugin_service()
        plugins = await run_sync(service.update_all_plugins)
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
                has_hooks=p.has_hooks,
                connector_type=p.connector_type,
                has_mcp=p.has_mcp,
                **_record_fields(p)
            )
            for p in plugins
        ]


# ============================================================================
# Auto-Update Settings Endpoints
# NOTE: These MUST be defined BEFORE the catch-all /{plugin_id:path} route
# ============================================================================

@router.get("/auto-update/settings", response_model=AutoUpdateSettingsResponse)
async def get_auto_update_settings(token: str = Depends(require_auth)):
    """Get auto-update settings"""
    service = get_plugin_service()
    au_settings = service.get_auto_update_settings()
    return AutoUpdateSettingsResponse(
        enabled=au_settings.enabled,
        interval_hours=au_settings.interval_hours,
        auto_sync_marketplaces=au_settings.auto_sync_marketplaces,
        auto_update_plugins=au_settings.auto_update_plugins,
        last_check=au_settings.last_check
    )


@router.put("/auto-update/settings", response_model=AutoUpdateSettingsResponse)
async def update_auto_update_settings(
    request: AutoUpdateSettingsRequest,
    token: str = Depends(require_admin)
):
    """Update auto-update settings - Admin only"""
    service = get_plugin_service()
    current = service.get_auto_update_settings()

    # Merge provided fields
    if request.enabled is not None:
        current.enabled = request.enabled
    if request.interval_hours is not None:
        current.interval_hours = request.interval_hours
    if request.auto_sync_marketplaces is not None:
        current.auto_sync_marketplaces = request.auto_sync_marketplaces
    if request.auto_update_plugins is not None:
        current.auto_update_plugins = request.auto_update_plugins

    service.save_auto_update_settings(current)

    # Signal the background task to reschedule if needed
    from app.core.plugin_auto_update import reschedule_auto_update
    reschedule_auto_update()

    return AutoUpdateSettingsResponse(
        enabled=current.enabled,
        interval_hours=current.interval_hours,
        auto_sync_marketplaces=current.auto_sync_marketplaces,
        auto_update_plugins=current.auto_update_plugins,
        last_check=current.last_check
    )


@router.post("/auto-update/run", response_model=AutoUpdateRunResponse)
async def run_auto_update(token: str = Depends(require_admin)):
    """Manually trigger an auto-update cycle - Admin only"""
    import asyncio
    async with api_error_handler("run auto-update"):
        service = get_plugin_service()
        summary = await asyncio.to_thread(service.run_auto_update)
        return AutoUpdateRunResponse(**summary)


@router.get("/state/enabled", response_model=PluginEnableStateResponse)
async def get_enabled_plugins(token: str = Depends(require_auth)):
    """Get all plugin enable/disable states"""
    service = get_plugin_service()
    enabled = service.get_enabled_plugins()
    return PluginEnableStateResponse(enabled_plugins=enabled)


# ============================================================================
# Plugin Details (catch-all - MUST be last among GET routes)
# ============================================================================

@router.get("/runtime/{plugin_id}/connections")
async def runtime_connections(plugin_id: str, token: str = Depends(require_admin)):
    from app.core import connector_packages as packages
    kind = packages.kind_for(plugin_id)
    if not kind:
        raise HTTPException(404, "Connector plugin not found")
    return await run_sync(packages.dependencies, kind)


@router.post("/runtime/{plugin_id}/rollback", response_model=PluginResponse)
async def rollback_runtime(plugin_id: str, token: str = Depends(require_admin)):
    from app.core import connector_packages as packages
    kind = packages.kind_for(plugin_id)
    if not kind:
        raise HTTPException(404, "Connector plugin not found")
    try:
        return (await run_sync(packages.rollback, kind)).to_dict()
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/{plugin_id:path}", response_model=PluginDetailsResponse)
async def get_plugin_details(
    plugin_id: str,
    token: str = Depends(require_auth)
):
    """Get detailed information about a plugin (installed or available)"""
    service = get_plugin_service()

    # Try installed first, then fall back to available
    plugin = service.get_plugin_details(plugin_id)
    if not plugin:
        plugin = service.get_available_plugin_details(plugin_id)

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
        connector_type=plugin.connector_type, has_mcp=plugin.has_mcp,
        **_record_fields(plugin),
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
        plugin = await run_sync(service.install_plugin, request.plugin_id, request.scope)
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
            has_hooks=plugin.has_hooks,
            connector_type=plugin.connector_type, has_mcp=plugin.has_mcp,
                **_record_fields(plugin)
        )
    except ValueError as e:
        logger.warning(f"Plugin install failed for {request.plugin_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except RuntimeError as e:
        logger.exception(f"Plugin install error for {request.plugin_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.post("/install/batch", response_model=List[PluginResponse], status_code=status.HTTP_201_CREATED)
async def install_plugins_batch(
    request: PluginBatchInstallRequest,
    token: str = Depends(require_admin)
):
    """Install multiple plugins in batch - Admin only

    Installs in order and stops at the first failure, answering like the
    single install route: 400 for a bad ID/source, 500 for a clone or other
    install failure, and the 409 of an unreadable settings/registry file
    (refused before anything is copied). Plugins installed before the failing
    one stay installed.
    """
    service = get_plugin_service()
    try:
        plugins = await run_sync(service.install_plugins_batch, request.plugin_ids, request.scope)
    except ValueError as e:
        logger.warning(f"Batch plugin install failed: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.exception("Batch plugin install error")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
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
            has_hooks=p.has_hooks,
            connector_type=p.connector_type, has_mcp=p.has_mcp,
                **_record_fields(p)
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
        await run_sync(service.uninstall_plugin, plugin_id)
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
    try:
        success = await run_sync(service.enable_plugin, plugin_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
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
    try:
        success = await run_sync(service.disable_plugin, plugin_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to disable plugin"
        )
    return {"status": "disabled", "plugin_id": plugin_id}

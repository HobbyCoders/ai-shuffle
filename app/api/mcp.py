"""
MCP Server Management API Routes

Provides REST endpoints for managing MCP (Model Context Protocol) servers:
- Global (USER scope) MCP servers
- Project-scoped MCP servers (LOCAL and PROJECT scopes)
- MCP tool discovery
"""

import logging
from pathlib import Path
from typing import List, Optional, Dict, Literal

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field

from app.db import database
from app.api.auth import require_auth, require_admin, get_api_user_from_request
from app.core.project_utils import get_project_working_dir_as_path
from app.api.common import validate_project_access, get_project_path_from_dict
from app.core.mcp_service import (
    get_mcp_service,
    McpScope,
    McpServerConfig,
    McpServerType,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["MCP"])


# ============================================================================
# Request/Response Models
# ============================================================================

class McpServerResponse(BaseModel):
    """Response model for MCP server info"""
    name: str
    type: str  # "stdio" | "sse" | "http"
    command: Optional[str] = None
    args: Optional[List[str]] = None
    url: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    headers: Optional[Dict[str, str]] = None
    scope: str
    enabled: bool = True
    description: Optional[str] = None
    builtin: bool = False


class McpServerCreateRequest(BaseModel):
    """Request to create a new MCP server"""
    name: str = Field(..., description="Unique server name", min_length=1, max_length=64)
    type: Literal["stdio", "sse", "http"] = Field("stdio", description="Server type: stdio, sse, or http")
    command: Optional[str] = Field(None, description="Command to execute (stdio only)")
    args: Optional[List[str]] = Field(None, description="Command arguments (stdio only)")
    url: Optional[str] = Field(None, description="Server URL (sse/http only)")
    env: Optional[Dict[str, str]] = Field(None, description="Environment variables")
    headers: Optional[Dict[str, str]] = Field(None, description="HTTP headers (sse/http only)")
    description: Optional[str] = Field(None, description="Optional description")


class McpServerUpdateRequest(BaseModel):
    """Request to update an MCP server"""
    type: Optional[Literal["stdio", "sse", "http"]] = Field(None, description="Server type: stdio, sse, or http")
    command: Optional[str] = Field(None, description="Command to execute (stdio only)")
    args: Optional[List[str]] = Field(None, description="Command arguments (stdio only)")
    url: Optional[str] = Field(None, description="Server URL (sse/http only)")
    env: Optional[Dict[str, str]] = Field(None, description="Environment variables")
    headers: Optional[Dict[str, str]] = Field(None, description="HTTP headers (sse/http only)")
    description: Optional[str] = Field(None, description="Optional description")


class ProjectMcpServerCreateRequest(McpServerCreateRequest):
    """Request to create a project-scoped MCP server"""
    scope: str = Field("local", description="Scope: 'local' or 'project'")


class McpServersResponse(BaseModel):
    """Response containing all MCP servers grouped by scope"""
    user: List[McpServerResponse] = []
    installed: List[McpServerResponse] = []
    local: List[McpServerResponse] = []
    project: List[McpServerResponse] = []
    total_count: int = 0


class McpToolResponse(BaseModel):
    """Response for an MCP tool"""
    name: str
    server: str
    description: Optional[str] = None


# ============================================================================
# Helper Functions
# ============================================================================

class McpServerToggleRequest(BaseModel):
    """Request to toggle MCP server enabled/disabled"""
    enabled: bool


def _server_to_response(server: McpServerConfig) -> McpServerResponse:
    """Convert McpServerConfig to response model"""
    return McpServerResponse(
        name=server.name,
        type=server.type.value if isinstance(server.type, McpServerType) else server.type,
        command=server.command,
        args=server.args,
        url=server.url,
        env=server.env,
        headers=server.headers,
        scope=server.scope,
        enabled=server.enabled,
        description=server.description,
        builtin=server.builtin,
    )


def _request_to_config(
    name: str,
    request: McpServerCreateRequest,
    scope: str = "user"
) -> McpServerConfig:
    """Convert request model to McpServerConfig"""
    server_type = McpServerType.STDIO
    if request.type == "sse":
        server_type = McpServerType.SSE
    elif request.type == "http":
        server_type = McpServerType.HTTP

    return McpServerConfig(
        name=name,
        type=server_type,
        command=request.command,
        args=request.args,
        url=request.url,
        env=request.env,
        headers=request.headers,
        scope=scope,
        enabled=True,
        description=request.description,
    )


def _parse_scope(scope_str: str) -> McpScope:
    """Parse scope string to McpScope enum"""
    scope_str = scope_str.lower()
    if scope_str == "local":
        return McpScope.LOCAL
    elif scope_str == "project":
        return McpScope.PROJECT
    elif scope_str == "user":
        return McpScope.USER
    else:
        raise ValueError(f"Invalid scope: {scope_str}")


# ============================================================================
# Global (USER scope) MCP Endpoints
# ============================================================================

@router.get("/mcp/servers", response_model=List[McpServerResponse])
async def list_global_mcp_servers(token: str = Depends(require_auth)):
    """List all globally available MCP servers including builtins"""
    try:
        service = get_mcp_service()
        servers = service.get_all_servers_with_builtins()
        return [_server_to_response(s) for s in servers]
    except Exception as e:
        logger.exception("Failed to list MCP servers")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list MCP servers"
        )


@router.post("/mcp/servers", response_model=McpServerResponse, status_code=status.HTTP_201_CREATED)
async def add_global_mcp_server(
    request: McpServerCreateRequest,
    token: str = Depends(require_admin)
):
    """Add a new global (USER scope) MCP server - Admin only"""
    try:
        service = get_mcp_service()

        # Create config
        config = _request_to_config(request.name, request, "user")

        # Validate
        errors = service.validate_server_config(config)
        if errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="; ".join(errors)
            )

        # Add server
        server = service.add_server(request.name, config, McpScope.USER)
        return _server_to_response(server)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.exception("Failed to add MCP server")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add MCP server"
        )


@router.put("/mcp/servers/{server_name}", response_model=McpServerResponse)
async def update_global_mcp_server(
    server_name: str,
    request: McpServerUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update a global (USER scope) MCP server - Admin only"""
    try:
        service = get_mcp_service()

        # Get existing server
        existing = service.get_server(server_name, McpScope.USER)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"MCP server not found: {server_name}"
            )

        # Merge updates
        server_type = existing.type
        if request.type:
            if request.type == "stdio":
                server_type = McpServerType.STDIO
            elif request.type == "sse":
                server_type = McpServerType.SSE
            elif request.type == "http":
                server_type = McpServerType.HTTP

        updated = McpServerConfig(
            name=server_name,
            type=server_type,
            command=request.command if request.command is not None else existing.command,
            args=request.args if request.args is not None else existing.args,
            url=request.url if request.url is not None else existing.url,
            env=request.env if request.env is not None else existing.env,
            headers=request.headers if request.headers is not None else existing.headers,
            scope="user",
            enabled=existing.enabled,
            description=request.description if request.description is not None else existing.description,
        )

        # Validate
        errors = service.validate_server_config(updated)
        if errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="; ".join(errors)
            )

        # Update
        server = service.update_server(server_name, updated, McpScope.USER)
        return _server_to_response(server)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update MCP server")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update MCP server"
        )


@router.delete("/mcp/servers/{server_name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_global_mcp_server(
    server_name: str,
    token: str = Depends(require_admin)
):
    """Remove a global (USER scope) MCP server - Admin only"""
    try:
        service = get_mcp_service()
        service.remove_server(server_name, McpScope.USER)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.exception("Failed to remove MCP server")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove MCP server"
        )


@router.patch("/mcp/servers/{server_name}/toggle", response_model=McpServerResponse)
async def toggle_global_mcp_server(
    server_name: str,
    request: McpServerToggleRequest,
    token: str = Depends(require_admin)
):
    """Toggle a global MCP server enabled/disabled via settings.json - Admin only"""
    try:
        service = get_mcp_service()

        # Set enabled state in settings.json
        success = service.set_server_enabled(server_name, request.enabled)
        if not success:
            raise RuntimeError("Failed to update settings.json")

        # Return updated server info
        servers = service.get_all_servers_with_builtins()
        for s in servers:
            if s.name == server_name:
                return _server_to_response(s)

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP server not found: {server_name}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to toggle MCP server")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle MCP server"
        )


# ============================================================================
# Project-scoped MCP Endpoints
# ============================================================================

@router.get("/projects/{project_id}/mcp/servers", response_model=McpServersResponse)
async def list_project_mcp_servers(
    project_id: str,
    http_request: Request,
    token: str = Depends(require_auth)
):
    """List all MCP servers for a project (all scopes)"""
    try:
        project = validate_project_access(http_request, project_id)
        project_path = get_project_path_from_dict(project)

        service = get_mcp_service(project_path)
        all_servers = service.get_all_servers()

        response = McpServersResponse(
            user=[_server_to_response(s) for s in all_servers.get("user", [])],
            installed=[_server_to_response(s) for s in all_servers.get("installed", [])],
            local=[_server_to_response(s) for s in all_servers.get("local", [])],
            project=[_server_to_response(s) for s in all_servers.get("project", [])],
        )
        response.total_count = len(response.user) + len(response.installed) + len(response.local) + len(response.project)
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to list project MCP servers")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list MCP servers"
        )


@router.post(
    "/projects/{project_id}/mcp/servers",
    response_model=McpServerResponse,
    status_code=status.HTTP_201_CREATED
)
async def add_project_mcp_server(
    project_id: str,
    request: ProjectMcpServerCreateRequest,
    http_request: Request,
    token: str = Depends(require_admin)
):
    """Add an MCP server to a project (LOCAL, PROJECT, or INSTALLED scope) - Admin only"""
    try:
        project = validate_project_access(http_request, project_id)
        project_path = get_project_path_from_dict(project)

        if request.scope == "user":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Use /mcp/servers endpoint for USER scope"
            )

        service = get_mcp_service(project_path)

        # Create config
        config = _request_to_config(request.name, request, request.scope)

        # Validate
        errors = service.validate_server_config(config)
        if errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="; ".join(errors)
            )

        # Add server — installed scope uses a different method
        if request.scope == "installed":
            server = service.add_project_installed_server(request.name, config)
        else:
            scope = _parse_scope(request.scope)
            server = service.add_server(request.name, config, scope)
        return _server_to_response(server)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to add project MCP server")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add MCP server"
        )


@router.put("/projects/{project_id}/mcp/servers/{server_name}", response_model=McpServerResponse)
async def update_project_mcp_server(
    project_id: str,
    server_name: str,
    request: McpServerUpdateRequest,
    http_request: Request,
    scope: str = "local",
    token: str = Depends(require_admin)
):
    """Update a project-scoped MCP server - Admin only"""
    try:
        project = validate_project_access(http_request, project_id)
        project_path = get_project_path_from_dict(project)

        if scope == "user":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Use /mcp/servers endpoint for USER scope"
            )

        service = get_mcp_service(project_path)

        # Get existing server
        if scope == "installed":
            installed = service.get_project_installed_servers()
            existing = next((s for s in installed if s.name == server_name), None)
        else:
            mcp_scope = _parse_scope(scope)
            existing = service.get_server(server_name, mcp_scope)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"MCP server not found: {server_name}"
            )

        # Merge updates
        server_type = existing.type
        if request.type:
            if request.type == "stdio":
                server_type = McpServerType.STDIO
            elif request.type == "sse":
                server_type = McpServerType.SSE
            elif request.type == "http":
                server_type = McpServerType.HTTP

        updated = McpServerConfig(
            name=server_name,
            type=server_type,
            command=request.command if request.command is not None else existing.command,
            args=request.args if request.args is not None else existing.args,
            url=request.url if request.url is not None else existing.url,
            env=request.env if request.env is not None else existing.env,
            headers=request.headers if request.headers is not None else existing.headers,
            scope=scope,
            enabled=existing.enabled,
            description=request.description if request.description is not None else existing.description,
        )

        # Validate
        errors = service.validate_server_config(updated)
        if errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="; ".join(errors)
            )

        # Update — installed scope needs special handling
        if scope == "installed":
            service.remove_project_installed_server(server_name)
            server = service.add_project_installed_server(server_name, updated)
        else:
            server = service.update_server(server_name, updated, mcp_scope)
        return _server_to_response(server)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update project MCP server")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update MCP server"
        )


@router.delete(
    "/projects/{project_id}/mcp/servers/{server_name}",
    status_code=status.HTTP_204_NO_CONTENT
)
async def remove_project_mcp_server(
    project_id: str,
    server_name: str,
    http_request: Request,
    scope: str = "local",
    token: str = Depends(require_admin)
):
    """Remove an MCP server from a project scope - Admin only

    Supports scope: local, project, installed, user.
    - installed: removes from ~/.claude.json projects[path].mcpServers
    - user: removes from ~/.claude.json root mcpServers (global)
    - local/project: removes from .claude/.mcp.json or .mcp.json
    """
    try:
        project = validate_project_access(http_request, project_id)
        project_path = get_project_path_from_dict(project)

        service = get_mcp_service(project_path)

        if scope == "installed":
            service.remove_project_installed_server(server_name)
        elif scope == "user":
            service.remove_server(server_name, McpScope.USER)
        else:
            mcp_scope = _parse_scope(scope)
            service.remove_server(server_name, mcp_scope)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to remove project MCP server")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove MCP server"
        )


@router.patch(
    "/projects/{project_id}/mcp/servers/{server_name}/toggle",
    response_model=McpServerResponse
)
async def toggle_project_mcp_server(
    project_id: str,
    server_name: str,
    request: McpServerToggleRequest,
    http_request: Request,
    token: str = Depends(require_admin)
):
    """Toggle an MCP server enabled/disabled for a project - Admin only

    Writes to ~/.claude.json projects[path].disabledMcpServers.
    This is the per-project disable mechanism that Claude Code uses.
    """
    try:
        project = validate_project_access(http_request, project_id)
        project_path = get_project_path_from_dict(project)

        service = get_mcp_service(project_path)
        service.set_project_server_enabled(server_name, request.enabled)

        # Return updated server info — check all scopes
        server = service.get_server(server_name, McpScope.USER)
        if not server:
            # Check installed servers
            installed = service.get_project_installed_servers()
            server = next((s for s in installed if s.name == server_name), None)
        if not server:
            server = service.get_server(server_name, McpScope.LOCAL)
        if not server:
            server = service.get_server(server_name, McpScope.PROJECT)
        if not server:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"MCP server not found: {server_name}"
            )

        return _server_to_response(server)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to toggle project MCP server")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle MCP server"
        )


# ============================================================================
# MCP Tools Discovery
# ============================================================================

@router.get("/mcp/tools", response_model=List[McpToolResponse])
async def list_mcp_tools(
    token: str = Depends(require_auth),
    refresh: bool = False
):
    """
    List all discovered MCP tools from the CLI cache.

    Query params:
        refresh: If true, force refresh from Claude CLI
    """
    try:
        # Import here to avoid circular imports
        from app.api.system import discover_tools_from_cli

        result = await discover_tools_from_cli(force_refresh=refresh)
        tools = result.get("tools") or []

        # Filter to just MCP tools
        mcp_tools = []
        for tool_name in tools:
            if tool_name.startswith("mcp__"):
                # Parse: mcp__server__tool
                parts = tool_name.split("__")
                if len(parts) >= 3:
                    server = parts[1]
                    tool = "__".join(parts[2:])  # Handle tools with __ in name
                    mcp_tools.append(McpToolResponse(
                        name=tool,
                        server=server,
                        description=None
                    ))

        return mcp_tools

    except Exception as e:
        logger.exception("Failed to list MCP tools")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list MCP tools"
        )


@router.get("/mcp/stats")
async def get_mcp_stats(token: str = Depends(require_auth)):
    """Get MCP server statistics"""
    try:
        service = get_mcp_service()
        user_servers = service.get_servers_for_scope(McpScope.USER)

        return {
            "global_server_count": len(user_servers),
        }
    except Exception as e:
        logger.exception("Failed to get MCP stats")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get MCP stats"
        )

"""
System API routes - health, version, stats, workspace configuration, tools
"""

import subprocess
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.models import HealthResponse, VersionResponse, StatsResponse
from app.core.auth import auth_service
from app.core.config import settings
from app.db import database
from app.api.auth import require_auth, require_admin


# ============================================================================
# Built-in Tool Definitions
# ============================================================================

# Tool categories for UI grouping
TOOL_CATEGORIES = {
    "read_only": {
        "name": "Read-only tools",
        "description": "Tools that only read data without making changes"
    },
    "edit": {
        "name": "Edit tools",
        "description": "Tools that modify files"
    },
    "execution": {
        "name": "Execution tools",
        "description": "Tools that execute commands or code"
    },
    "mcp": {
        "name": "MCP tools",
        "description": "Tools from MCP servers"
    },
    "other": {
        "name": "Other tools",
        "description": "Utility and miscellaneous tools"
    }
}

# Built-in Claude Code tools with their metadata
BUILTIN_TOOLS = [
    # Read-only tools
    {"name": "Read", "category": "read_only", "description": "Read file contents"},
    {"name": "Glob", "category": "read_only", "description": "Find files by pattern"},
    {"name": "Grep", "category": "read_only", "description": "Search file contents"},
    {"name": "WebFetch", "category": "read_only", "description": "Fetch and analyze web content"},
    {"name": "WebSearch", "category": "read_only", "description": "Search the web"},
    {"name": "BashOutput", "category": "read_only", "description": "Get output from background shell"},

    # Edit tools
    {"name": "Edit", "category": "edit", "description": "Edit file contents"},
    {"name": "Write", "category": "edit", "description": "Write new files"},
    {"name": "NotebookEdit", "category": "edit", "description": "Edit Jupyter notebooks"},

    # Execution tools
    {"name": "Bash", "category": "execution", "description": "Execute shell commands"},
    {"name": "Task", "category": "execution", "description": "Launch subagent tasks"},
    {"name": "KillShell", "category": "execution", "description": "Kill background shell"},

    # Other tools
    {"name": "TodoWrite", "category": "other", "description": "Manage task lists"},
    {"name": "Skill", "category": "other", "description": "Execute skills"},
    {"name": "SlashCommand", "category": "other", "description": "Run slash commands"},
    {"name": "EnterPlanMode", "category": "other", "description": "Enter planning mode"},
    {"name": "ExitPlanMode", "category": "other", "description": "Exit planning mode"},
    {"name": "ListMcpResources", "category": "other", "description": "List MCP resources"},
    {"name": "ReadMcpResource", "category": "other", "description": "Read MCP resource"},
]

router = APIRouter(tags=["System"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint (no auth required)"""
    return {
        "status": "healthy",
        "service": settings.service_name,
        "version": settings.version,
        "authenticated": False,  # Not checking session for health
        "setup_required": auth_service.is_setup_required(),
        "claude_authenticated": auth_service.is_claude_authenticated()
    }


@router.get("/api/v1/health", response_model=HealthResponse)
async def api_health_check():
    """API health check endpoint"""
    return await health_check()


@router.get("/api/v1/version", response_model=VersionResponse)
async def get_version():
    """Get API and Claude Code versions"""
    claude_version = None

    try:
        result = subprocess.run(
            ['claude', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            claude_version = result.stdout.strip()
    except Exception:
        pass

    return {
        "api_version": settings.version,
        "claude_version": claude_version
    }


@router.get("/api/v1/stats", response_model=StatsResponse)
async def get_stats(token: str = Depends(require_auth)):
    """Get usage statistics"""
    stats = database.get_usage_stats()
    return stats


@router.get("/api/v1/deployment")
async def get_deployment_info(token: str = Depends(require_auth)):
    """
    Get deployment configuration and platform information.

    Useful for debugging and understanding the current deployment mode.
    """
    return settings.get_deployment_info()


# ============================================================================
# Workspace Configuration (for local mode)
# ============================================================================

class WorkspaceConfigResponse(BaseModel):
    """Workspace configuration response"""
    workspace_path: str
    is_configured: bool
    is_local_mode: bool
    default_path: str
    exists: bool


class WorkspaceConfigRequest(BaseModel):
    """Request to set workspace path"""
    workspace_path: str


@router.get("/api/v1/workspace/config")
async def get_workspace_config() -> WorkspaceConfigResponse:
    """
    Get current workspace configuration.

    This endpoint is used during setup to determine if workspace needs to be configured.
    No authentication required so it can be called before admin setup.
    """
    from app.core.platform import get_default_workspace_dir, is_local_mode

    # Get configured path from database (if any)
    configured_path = database.get_system_setting("workspace_path")

    # Determine current effective path
    if configured_path:
        current_path = configured_path
        is_configured = True
    else:
        current_path = str(settings.effective_workspace_dir)
        is_configured = False

    # Get default path for the platform
    default_path = str(get_default_workspace_dir())

    return WorkspaceConfigResponse(
        workspace_path=current_path,
        is_configured=is_configured,
        is_local_mode=is_local_mode(),
        default_path=default_path,
        exists=Path(current_path).exists()
    )


@router.post("/api/v1/workspace/config")
async def set_workspace_config(
    request: WorkspaceConfigRequest,
    token: str = Depends(require_admin)
) -> WorkspaceConfigResponse:
    """
    Set the workspace directory path (admin only).

    This is primarily used in local mode to let users choose where to store projects.
    The directory will be created if it doesn't exist.
    """
    from app.core.platform import is_local_mode

    workspace_path = request.workspace_path.strip()

    if not workspace_path:
        raise HTTPException(status_code=400, detail="Workspace path cannot be empty")

    # Convert to absolute path
    path = Path(workspace_path).expanduser().resolve()

    # Validate the path
    try:
        # Create directory if it doesn't exist
        path.mkdir(parents=True, exist_ok=True)

        # Test write permissions by creating a temp file
        test_file = path / ".ai-hub-test"
        test_file.touch()
        test_file.unlink()

    except PermissionError:
        raise HTTPException(
            status_code=400,
            detail=f"Permission denied: Cannot write to '{path}'"
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid path: {str(e)}"
        )

    # Save to database
    database.set_system_setting("workspace_path", str(path))

    # Update the runtime settings
    object.__setattr__(settings, 'workspace_dir', path)

    return WorkspaceConfigResponse(
        workspace_path=str(path),
        is_configured=True,
        is_local_mode=is_local_mode(),
        default_path=str(path),  # After setting, this becomes the effective path
        exists=True
    )


@router.post("/api/v1/workspace/validate")
async def validate_workspace_path(request: WorkspaceConfigRequest) -> dict:
    """
    Validate a workspace path without saving it.

    No authentication required - used during initial setup.
    """
    workspace_path = request.workspace_path.strip()

    if not workspace_path:
        return {
            "valid": False,
            "error": "Workspace path cannot be empty",
            "path": "",
            "exists": False,
            "writable": False
        }

    try:
        path = Path(workspace_path).expanduser().resolve()
        exists = path.exists()
        writable = False
        error = None

        if exists:
            # Check if writable
            try:
                test_file = path / ".ai-hub-test"
                test_file.touch()
                test_file.unlink()
                writable = True
            except PermissionError:
                error = "Directory exists but is not writable"
            except Exception as e:
                error = f"Cannot write to directory: {str(e)}"
        else:
            # Check if parent exists and is writable
            parent = path.parent
            if parent.exists():
                try:
                    path.mkdir(parents=True, exist_ok=True)
                    test_file = path / ".ai-hub-test"
                    test_file.touch()
                    test_file.unlink()
                    path.rmdir()  # Remove the test directory
                    writable = True
                except PermissionError:
                    error = "Cannot create directory: permission denied"
                except Exception as e:
                    error = f"Cannot create directory: {str(e)}"
            else:
                error = f"Parent directory does not exist: {parent}"

        return {
            "valid": writable and error is None,
            "error": error,
            "path": str(path),
            "exists": exists,
            "writable": writable
        }

    except Exception as e:
        return {
            "valid": False,
            "error": str(e),
            "path": workspace_path,
            "exists": False,
            "writable": False
        }


# ============================================================================
# Tools API
# ============================================================================

class ToolInfo(BaseModel):
    """Information about a single tool"""
    name: str
    category: str
    description: str
    mcp_server: Optional[str] = None  # For MCP tools, the server name


class ToolCategory(BaseModel):
    """Tool category with its tools"""
    id: str
    name: str
    description: str
    tools: List[ToolInfo]


class ToolsResponse(BaseModel):
    """Response containing all available tools organized by category"""
    categories: List[ToolCategory]
    all_tools: List[ToolInfo]


@router.get("/api/v1/tools", response_model=ToolsResponse)
async def list_available_tools(token: str = Depends(require_auth)):
    """
    List all available tools organized by category.

    Returns built-in Claude Code tools and any MCP tools from configured servers.
    Use this to populate tool selection UI in profile configuration.
    """
    # Build the list of all tools
    all_tools = []
    tools_by_category = {}

    # Add built-in tools
    for tool in BUILTIN_TOOLS:
        tool_info = ToolInfo(
            name=tool["name"],
            category=tool["category"],
            description=tool["description"]
        )
        all_tools.append(tool_info)

        if tool["category"] not in tools_by_category:
            tools_by_category[tool["category"]] = []
        tools_by_category[tool["category"]].append(tool_info)

    # TODO: Add MCP tools from configured MCP servers
    # This would require reading MCP server configuration and querying for available tools
    # For now, MCP tools need to be entered manually or added via setting_sources

    # Build category list
    categories = []
    for cat_id, cat_info in TOOL_CATEGORIES.items():
        category = ToolCategory(
            id=cat_id,
            name=cat_info["name"],
            description=cat_info["description"],
            tools=tools_by_category.get(cat_id, [])
        )
        categories.append(category)

    return ToolsResponse(
        categories=categories,
        all_tools=all_tools
    )


@router.get("/api/v1/tools/builtin", response_model=List[ToolInfo])
async def list_builtin_tools(token: str = Depends(require_auth)):
    """
    List only built-in Claude Code tools.

    Simpler endpoint that returns a flat list of built-in tools.
    """
    return [
        ToolInfo(
            name=tool["name"],
            category=tool["category"],
            description=tool["description"]
        )
        for tool in BUILTIN_TOOLS
    ]

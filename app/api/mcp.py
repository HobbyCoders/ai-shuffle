"""
MCP Server Management API Routes

Provides REST endpoints for managing MCP (Model Context Protocol) servers:
- Global (USER scope) MCP servers
- Project-scoped MCP servers (LOCAL and PROJECT scopes)
- MCP tool discovery
"""

import logging
import math
import re
from typing import List, Optional, Dict, Literal

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field

from app.api.auth import get_api_user_from_request, require_auth, require_admin
from app.api.common import validate_project_access, get_project_path_from_dict, api_error_handler
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
    # Legacy: set for CLI-flag built-ins (not a spawnable server, enabled via a
    # CLI flag). No current built-in uses this; None for normal servers.
    cli_flag: Optional[str] = None


# Checked by the create handlers (not the schema) so an over-long name gets
# the same 400 as the other name rules instead of a schema 422.
MAX_SERVER_NAME_LENGTH = 64


class McpServerCreateRequest(BaseModel):
    """Request to create a new MCP server"""
    name: str = Field(
        ...,
        description=(
            f"Unique server name: 1 to {MAX_SERVER_NAME_LENGTH} letters, digits, hyphens or underscores"
        ),
    )
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
    builtin: List[McpServerResponse] = []
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
        cli_flag=getattr(server, "cli_flag", None),
    )


_SECRET_MASK = "********"


def _is_owner(request: Request) -> bool:
    """False for an API key or a non-owner paired device.

    Every ``require_auth`` branch stamps ``request.state.is_admin``; only a
    caller it marked as a non-owner (or an API user) loses the secrets.
    """
    if get_api_user_from_request(request) is not None:
        return False
    return getattr(request.state, "is_admin", True) is not False


# Name parts that make a flag, a NAME=value pair or a header carry a secret
# (split on - _ . so ``--author`` or ``--keyboard`` do not match).
_SECRET_NAME_PARTS = frozenset({
    "token", "tokens", "secret", "secrets", "password", "passwd", "pwd", "passphrase",
    "apikey", "auth", "authorization", "credential", "credentials", "bearer", "cookie",
    "pat", "jwt", "session", "sessionid", "signature",
})
# Two-part names whose halves are harmless alone (``--key-file`` stays).
_SECRET_NAME_PAIRS = frozenset({
    ("api", "key"), ("access", "key"), ("secret", "key"), ("private", "key"),
    ("client", "secret"), ("auth", "key"), ("account", "key"), ("master", "key"),
})
# Well-known credential prefixes (GitHub, OpenAI/Anthropic, Slack, GitLab, AWS,
# Stripe, Hugging Face, npm, Google).
_TOKEN_PREFIX_RE = re.compile(
    r"^(gh[pousr]_|github_pat_|sk-|sk_live_|sk_test_|rk_live_|xox[abprs]-|glpat-|AKIA|ASIA|hf_|npm_|AIza)",
)
_FLAG_RE = re.compile(r"^(--?)([A-Za-z0-9][\w.-]*)(=(.*))?$", re.DOTALL)
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)=(.+)$", re.DOTALL)
_HEADER_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.+)$", re.DOTALL)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic|token)\s+[^\s\"']+")
_QUERY_SECRET_RE = re.compile(r"(?i)([?&][\w.-]*(?:token|key|secret|password|auth|sig)[\w.-]*=)[^&#\s]+")


def _secret_name(name: str) -> bool:
    parts = [p for p in re.split(r"[-_.]+", name.lower()) if p]
    if any(p in _SECRET_NAME_PARTS for p in parts) or parts == ["key"]:
        return True
    return any(pair in _SECRET_NAME_PAIRS for pair in zip(parts, parts[1:]))


def _looks_like_token(value: str) -> bool:
    """A standalone credential: a known token prefix, or a long high-entropy word.

    Paths, URLs, package names (``@scope/pkg``) and plain words stay readable.
    """
    value = value.strip().strip("'\"")
    if len(value) < 16 or any(c.isspace() for c in value):
        return False
    if _TOKEN_PREFIX_RE.match(value) and len(value) >= 20:
        return True
    if len(value) < 24 or value.startswith(("/", ".", "~", "@", "-")) or "://" in value:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_\-+/=.]+", value):
        return False
    if re.search(r"\.[A-Za-z][A-Za-z0-9]{0,9}$", value):
        return False  # a file name
    if "/" in value and all(len(part) < 20 for part in value.split("/")):
        return False  # a relative path
    words = [part for part in re.split(r"[-_.]+", value) if part]
    if len(words) >= 2 and all(part.isalpha() or part.isdigit() for part in words):
        return False  # an identifier such as a model id or a dated name
    classes = sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    if classes < 2:
        return False
    counts: Dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    entropy = -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())
    return entropy >= 3.5


def _mask_arg_text(arg: str) -> str:
    """One argument with any secret-looking value replaced by the mask."""
    from app.core.scrub import redact_url_credentials

    flag = _FLAG_RE.match(arg)
    if flag and flag.group(3) is not None:
        # ``--name=value``: a secret name masks it all; otherwise the value is
        # checked on its own (``--header=Authorization: x``, ``--env=K=v``).
        if _secret_name(flag.group(2)):
            return f"{flag.group(1)}{flag.group(2)}={_SECRET_MASK}"
        return f"{flag.group(1)}{flag.group(2)}={_mask_arg_text(flag.group(4))}"
    assign = _ASSIGN_RE.match(arg)
    if assign and not flag and _secret_name(assign.group(1)):
        return f"{assign.group(1)}={_SECRET_MASK}"
    header = _HEADER_RE.match(arg)
    if header and "://" not in arg and _secret_name(header.group(1)):
        return f"{header.group(1)}: {_SECRET_MASK}"
    if _looks_like_token(arg):
        return _SECRET_MASK
    masked = redact_url_credentials(arg)
    masked = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + _SECRET_MASK, masked)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} {_SECRET_MASK}", masked)


def _mask_args(args: Optional[List[str]]) -> Optional[List[str]]:
    """Command args as a non-owner may see them (see ``_redacted``).

    Masks ``--token=v`` / ``--api-key v`` values, ``NAME=v`` pairs and
    ``Header: v`` values whose name reads as a secret, the argument after a
    bare ``Authorization``, bearer tokens, URL userinfo and secret query
    parameters, and any standalone value that looks like a credential.
    """
    if not args:
        return args
    out: List[str] = []
    mask_next = False
    for arg in args:
        text = arg if isinstance(arg, str) else str(arg)
        if mask_next and not text.startswith("--"):
            out.append(_SECRET_MASK)
            mask_next = False
            continue
        mask_next = False
        flag = _FLAG_RE.match(text)
        if flag and flag.group(3) is None and _secret_name(flag.group(2)):
            mask_next = True  # ``--token VALUE``
        elif text.strip().rstrip(":").lower() in ("authorization", "proxy-authorization", "cookie"):
            mask_next = True  # ``-H Authorization "Bearer x"`` split in two
        out.append(_mask_arg_text(text))
    return out


def _redacted(response: McpServerResponse) -> McpServerResponse:
    """A server as a non-owner may see it: env and header VALUES masked (the
    names stay, so the UI can still say what a server needs), secret-looking
    args masked the same way, and URL-embedded credentials scrubbed."""
    from app.core.scrub import scrub_url_credentials

    return response.model_copy(update={
        "args": _mask_args(response.args),
        "env": {k: _SECRET_MASK for k in response.env} if response.env else response.env,
        "headers": (
            {k: _SECRET_MASK for k in response.headers}
            if response.headers else response.headers
        ),
        "url": scrub_url_credentials(response.url) if response.url else response.url,
    })


def _server_view(server: McpServerConfig, request: Request) -> McpServerResponse:
    """``_server_to_response`` for a read route: secrets only for the owner."""
    response = _server_to_response(server)
    return response if _is_owner(request) else _redacted(response)


def _create_errors(service, config: McpServerConfig) -> List[str]:
    """Validation errors for a new server: the service rules plus the name length."""
    errors = list(service.validate_server_config(config))
    if len(config.name) > MAX_SERVER_NAME_LENGTH:
        errors.append(f"Server name must be at most {MAX_SERVER_NAME_LENGTH} characters")
    return errors


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
async def list_global_mcp_servers(request: Request, token: str = Depends(require_auth)):
    """List all globally available MCP servers including builtins.

    Readable by API keys (the web UI's search lists them); secret values are
    the owner's only.
    """
    async with api_error_handler("list MCP servers"):
        service = get_mcp_service()
        servers = service.get_all_servers_with_builtins()
        return [_server_view(s, request) for s in servers]


@router.post("/mcp/servers", response_model=McpServerResponse, status_code=status.HTTP_201_CREATED)
async def add_global_mcp_server(
    request: McpServerCreateRequest,
    token: str = Depends(require_admin)
):
    """Add a new global (USER scope) MCP server - Admin only"""
    async with api_error_handler("add MCP server"):
        try:
            service = get_mcp_service()

            # Create config
            config = _request_to_config(request.name, request, "user")

            # Validate
            errors = _create_errors(service, config)
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


@router.put("/mcp/servers/{server_name}", response_model=McpServerResponse)
async def update_global_mcp_server(
    server_name: str,
    request: McpServerUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update a global (USER scope) MCP server - Admin only"""
    async with api_error_handler("update MCP server"):
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


@router.delete("/mcp/servers/{server_name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_global_mcp_server(
    server_name: str,
    token: str = Depends(require_admin)
):
    """Remove a global (USER scope) MCP server - Admin only"""
    async with api_error_handler("remove MCP server"):
        try:
            service = get_mcp_service()
            service.remove_server(server_name, McpScope.USER)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e)
            )


@router.patch("/mcp/servers/{server_name}/toggle", response_model=McpServerResponse)
async def toggle_global_mcp_server(
    server_name: str,
    request: McpServerToggleRequest,
    token: str = Depends(require_admin)
):
    """Toggle a global MCP server enabled/disabled via settings.json - Admin only"""
    async with api_error_handler("toggle MCP server"):
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
    async with api_error_handler("list project MCP servers"):
        project = validate_project_access(http_request, project_id)
        project_path = get_project_path_from_dict(project)

        service = get_mcp_service(project_path)
        all_servers = service.get_all_servers()

        response = McpServersResponse(**{
            group: [_server_view(s, http_request) for s in all_servers.get(group, [])]
            for group in ("builtin", "user", "installed", "local", "project")
        })
        response.total_count = (
            len(response.builtin) + len(response.user) + len(response.installed)
            + len(response.local) + len(response.project)
        )
        return response


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
    async with api_error_handler("add project MCP server"):
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
            errors = _create_errors(service, config)
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
    async with api_error_handler("update project MCP server"):
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
    async with api_error_handler("remove project MCP server"):
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
    async with api_error_handler("toggle project MCP server"):
        project = validate_project_access(http_request, project_id)
        project_path = get_project_path_from_dict(project)

        service = get_mcp_service(project_path)

        # Built-in/managed servers (e.g. the `browser` automation
        # integration) have no per-project disable mechanism —
        # get_builtin_servers() only honours
        # the GLOBAL disable list (settings.json -> disabledMcpServers). Route a
        # project-level toggle of a built-in through the global path so it
        # actually takes effect on agent sessions instead of silently no-opping.
        from app.core.builtin_mcp_servers import get_builtin_servers as _builtins
        builtin_names = {s.name for s in _builtins()}

        if server_name in builtin_names:
            success = service.set_server_enabled(server_name, request.enabled)
            if not success:
                raise RuntimeError("Failed to update settings.json")
        else:
            service.set_project_server_enabled(server_name, request.enabled)

        # Return updated server info — check built-ins + all scopes
        server = next(
            (s for s in service.get_builtin_servers() if s.name == server_name),
            None,
        )
        if not server:
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


# ============================================================================
# MCP Tools Discovery
# ============================================================================

@router.get("/mcp/tools", response_model=List[McpToolResponse])
async def list_mcp_tools(
    token: str = Depends(require_auth),
    refresh: bool = False
):
    """
    List every MCP tool a session can be given.

    Discovered live from the configured servers rather than from
    whatever the Claude CLI last reported: the same ``tools/list`` round-trip
    the tool-surface endpoint runs, cached per server for 10 minutes.

    Query params:
        refresh: If true, bypass the per-server cache
    """
    async with api_error_handler("list MCP tools"):
        from app.core.tool_catalog import build_tool_surface

        surface = await build_tool_surface(refresh=refresh)
        mcp_tools = []
        for group in surface["groups"]:
            # Only real MCP servers — the in-process bridge groups
            # (computer-use, app-config, ai-tools) name a server too but are
            # not MCP servers anyone configured.
            if group["kind"] not in ("browser", "mcp"):
                continue
            for entry in group["tools"]:
                # mcp__<server>__<tool>; tool names may themselves contain "__"
                parts = entry["name"].split("__")
                if len(parts) < 3:
                    continue
                mcp_tools.append(McpToolResponse(
                    name="__".join(parts[2:]),
                    server=parts[1],
                    description=entry.get("description"),
                ))

        return mcp_tools


@router.get("/mcp/stats")
async def get_mcp_stats(token: str = Depends(require_auth)):
    """Get MCP server statistics"""
    async with api_error_handler("get MCP stats"):
        service = get_mcp_service()
        user_servers = service.get_servers_for_scope(McpScope.USER)

        return {
            "global_server_count": len(user_servers),
        }

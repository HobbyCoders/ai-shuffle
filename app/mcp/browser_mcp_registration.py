"""
Auto-registration for the Browser MCP Server.

Ensures the browser-tools MCP server is registered in the user-scope
.mcp.json so the Claude CLI can discover and use browser tools.
"""

import logging
import os
import sys
from pathlib import Path

from app.core.mcp_service import McpServerConfig, McpScope, McpServerType

logger = logging.getLogger(__name__)

# Name used in .mcp.json
BROWSER_MCP_SERVER_NAME = "browser-tools"

# Project root (parent of app/)
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)


def get_browser_server_command() -> list[str]:
    """Get the command to run the browser MCP server.

    Uses the absolute script path instead of `-m` so the server can be
    spawned from any working directory without PYTHONPATH issues.
    """
    script_path = str(
        Path(__file__).resolve().parent / "browser_mcp_server.py"
    )
    return [sys.executable, script_path]


def _build_browser_config() -> McpServerConfig:
    """Build the current expected MCP config for the browser server."""
    command_parts = get_browser_server_command()
    return McpServerConfig(
        name=BROWSER_MCP_SERVER_NAME,
        type=McpServerType.STDIO,
        command=command_parts[0],
        args=command_parts[1:],
        env={},
        scope=McpScope.USER.value,
        enabled=True,
        description="Headless browser automation for AI agents — navigate, click, type, and interact with web pages",
    )


def ensure_browser_mcp_registered(mcp_service) -> bool:
    """
    Ensure the browser-tools MCP server is registered in user scope.

    If already registered, verifies the config is up to date (command path
    may change when switching venvs or project directories). Updates the
    entry if it has gone stale.

    Args:
        mcp_service: The MCP service instance

    Returns:
        True if the server was registered or already exists
    """
    try:
        desired = _build_browser_config()

        # Check if already registered
        existing = mcp_service.get_all_servers()
        for scope_servers in existing.values():
            for server in scope_servers:
                if server.name == BROWSER_MCP_SERVER_NAME:
                    # Verify the command and args are still correct
                    if (
                        server.command == desired.command
                        and server.args == desired.args
                    ):
                        logger.debug("Browser MCP server already registered and up to date")
                        return True

                    # Stale entry — update it
                    logger.info(
                        "Browser MCP server config is stale "
                        f"(command was {server.command!r}, now {desired.command!r}), updating"
                    )
                    mcp_service.update_server(
                        name=BROWSER_MCP_SERVER_NAME,
                        config=desired,
                        scope=McpScope.USER,
                    )
                    return True

        # Not registered yet — add it
        mcp_service.add_server(
            name=BROWSER_MCP_SERVER_NAME,
            config=desired,
            scope=McpScope.USER,
        )

        logger.info(f"Registered browser MCP server: {BROWSER_MCP_SERVER_NAME}")
        return True

    except ValueError as e:
        if "already exists" in str(e):
            logger.debug("Browser MCP server already registered")
            return True
        logger.error(f"Failed to register browser MCP server: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to register browser MCP server: {e}")
        return False


def unregister_browser_mcp(mcp_service) -> bool:
    """Remove the browser-tools MCP server registration."""
    try:
        mcp_service.remove_server(BROWSER_MCP_SERVER_NAME, McpScope.USER)
        logger.info(f"Unregistered browser MCP server: {BROWSER_MCP_SERVER_NAME}")
        return True
    except Exception as e:
        logger.debug(f"Browser MCP server not found for removal: {e}")
        return False

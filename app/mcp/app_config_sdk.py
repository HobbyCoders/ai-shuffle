"""
App Configuration SDK MCP Server — AI-powered configuration via chat.

Exposes AI Shuffle's configuration surfaces (hooks, automations, MCP servers,
profiles, connectors) as SDK MCP tools.  The Claude agent can call these
tools directly in conversation to configure the system on the user's behalf.

Architecture:
  - Tools call the database/service layer in-process (no HTTP round-trip).
  - Only registered for admin sessions with ``app_config_tools`` enabled.
  - Every write tool enforces admin access as a runtime check.
  - Destructive operations (delete) require ``confirm=true``.
  - Secrets are sanitized from all read results.

Tool naming convention: ``mcp__app-config__<toolName>``

Module layout (split for maintainability):
  - config_helpers.py      — constants, result formatting, sanitization
  - config_hooks.py        — hook CRUD tools
  - config_automations.py  — automation CRUD tools
  - config_mcp_servers.py  — MCP server management tools
  - config_profiles.py     — profile CRUD tools
  - config_connectors.py   — connector status/toggle tools
  - config_subagents.py    — subagent CRUD tools
"""

import logging
from typing import Any, List, Optional

from app.mcp.config_helpers import _tool_result
from app.mcp.config_hooks import _create_hook_tools
from app.mcp.config_automations import _create_automation_tools
from app.mcp.config_mcp_servers import _create_mcp_tools
from app.mcp.config_profiles import _create_profile_tools
from app.mcp.config_connectors import _create_connector_tools
from app.mcp.config_subagents import _create_subagent_tools

logger = logging.getLogger(__name__)


# ============================================================================
# System Prompt
# ============================================================================

APP_CONFIG_SYSTEM_PROMPT = """
## AI Shuffle Configuration

You can configure AI Shuffle directly through conversation. Use the `app-config` tools
to manage hooks, automations, MCP servers, profiles, and connectors.

### How to Use

1. **Discover first** — Call `getConfigCapabilities` to learn valid event types,
   trigger types, connector types, and current system state before creating anything.
2. **Check for duplicates** — Always `list*` before creating to avoid duplicates
   or conflicts with existing configuration.
3. **Describe your plan** — Before any write operation, tell the user exactly what
   you're about to create/change. Include the key parameters. Wait for acknowledgment.
4. **Execute and confirm** — After creating/updating, confirm success and tell the
   user how to find it in the UI or modify it later.
5. **Never delete without asking** — Always get explicit user confirmation before
   deleting hooks, automations, or MCP servers. Describe what will be lost.

### Intent Mapping

- "Block dangerous commands" / "prevent rm -rf" → Hook (PreToolUse on Bash)
- "Run X every morning" / "schedule a daily check" → Automation (cron trigger)
- "Check for Y every 5 minutes" → Automation (interval trigger)
- "Set up a webhook for Z" → Automation (webhook trigger) or Hook (webhook type)
- "Add an MCP server" / "connect to X service" → MCP Server
- "Create a profile for code review" → Profile
- "Connect Telegram/Discord/Slack" → Connector (direct user to Settings UI for secrets)

### Safety Rules

- Hook bash commands run with the app's permissions. Explain what each command does.
- Automation prompts run as Claude sessions with real tool access and cost.
  Always set budget limits (max_cost_per_run and max_runs_per_day).
- MCP servers execute external processes. Only add servers the user explicitly requests.
- Never expose webhook secrets, API keys, or HMAC tokens in your responses.
"""


# ============================================================================
# Discovery Tools
# ============================================================================

def _create_discovery_tools() -> list:
    """Create the getConfigCapabilities discovery tool."""
    from app.mcp.inproc import tool
    from app.db import database
    from app.db.async_database import run_sync

    tools = []

    @tool(
        "getConfigCapabilities",
        "Discover what can be configured in AI Shuffle — valid event types, "
        "trigger types, available connectors, and current system stats. "
        "Call this first before creating hooks, automations, or other config.",
        {"type": "object", "properties": {}},
    )
    async def get_capabilities(args: dict) -> dict:
        def _load_stats():
            """The three discovery counts in ONE hop.

            Contiguous, independent reads with nothing between them — folding
            them into a single closure costs one thread hop instead of three
            separate stalls of the event loop, and this tool is the one an
            agent is told to call FIRST in every config turn.
            """
            return (
                database.get_app_hooks(),
                database.get_all_automations(),
                database.get_all_profiles(),
            )

        all_hooks, all_automations, all_profiles = await run_sync(_load_stats)

        return _tool_result({
            "hook_events": [
                {"event": "PreToolUse", "description": "Before a tool executes. Can block (return {\"decision\":\"block\",\"reason\":\"...\"}) or allow. Requires tool_matcher regex."},
                {"event": "PostToolUse", "description": "After a tool completes. For logging/notifications. Requires tool_matcher regex."},
                {"event": "Stop", "description": "When Claude finishes responding. Gets full transcript (120s timeout)."},
                {"event": "SessionStart", "description": "When a new session begins."},
                {"event": "SessionEnd", "description": "When a session ends."},
                {"event": "UserPromptSubmit", "description": "When the user submits a message, before Claude processes it."},
                {"event": "PreCompact", "description": "Before context compaction occurs."},
                {"event": "SubagentStop", "description": "When a subagent finishes."},
                {"event": "Notification", "description": "For notification routing."},
            ],
            "hook_types": [
                {"type": "bash", "description": "Shell command. Receives TOOL_NAME, TOOL_INPUT, SESSION_ID env vars. Must output JSON for Pre/PostToolUse decisions."},
                {"type": "webhook", "description": "HTTP POST with optional HMAC-SHA256 signing."},
                {"type": "message", "description": "Template-based message with {{tool_name}}, {{tool_input}} substitution."},
            ],
            "automation_triggers": [
                {"type": "cron", "description": "Cron expression + timezone (e.g., '0 9 * * 1-5' = weekdays at 9am).", "examples": [
                    "0 9 * * 1-5  (weekdays 9am)",
                    "0 * * * *    (every hour)",
                    "*/15 * * * * (every 15 min)",
                    "0 8 * * 1    (Monday 8am)",
                ]},
                {"type": "interval", "description": "Fixed seconds between runs (minimum 60)."},
                {"type": "webhook", "description": "Triggered by external HTTP POST with HMAC verification."},
            ],
            "mcp_server_types": [
                {"type": "stdio", "description": "Local process via stdin/stdout. Needs command + args."},
                {"type": "sse", "description": "Remote Server-Sent Events. Needs url."},
                {"type": "http", "description": "Remote HTTP API. Needs url."},
            ],
            "mcp_scopes": [
                {"scope": "user", "description": "Global — available to all projects."},
                {"scope": "local", "description": "Project-specific, gitignored."},
                {"scope": "project", "description": "Project-specific, committed to git."},
            ],
            "available_connectors": [
                "telegram", "discord", "slack", "whatsapp", "line",
                "signal", "imessage", "teams", "bluebubbles",
                "spotify", "hue", "home_assistant", "gmail", "plaid",
            ],
            "common_tool_matchers": {
                "Bash": "Matches the Bash/shell tool",
                "Edit": "Matches the file Edit tool",
                "Write": "Matches the file Write tool",
                "Read": "Matches the file Read tool",
                ".*": "Matches any tool (use with caution)",
            },
            "current_stats": {
                "hooks": len(all_hooks),
                "automations": len(all_automations),
                "profiles": len(all_profiles),
            },
        })

    tools.append(get_capabilities)
    return tools


# ============================================================================
# Server Factory
# ============================================================================

def get_app_config_tool_names() -> List[str]:
    """
    Return the list of MCP tool names for allowed_tools registration.

    Names follow the ``mcp__app-config__<toolName>`` convention.
    """
    # All tools are always registered — admin check is at runtime inside each tool
    return [
        # Discovery
        "mcp__app-config__getConfigCapabilities",
        # Hooks
        "mcp__app-config__listHooks",
        "mcp__app-config__getHook",
        "mcp__app-config__createHook",
        "mcp__app-config__updateHook",
        "mcp__app-config__deleteHook",
        "mcp__app-config__toggleHook",
        "mcp__app-config__testHook",
        # Automations
        "mcp__app-config__listAutomations",
        "mcp__app-config__getAutomation",
        "mcp__app-config__createAutomation",
        "mcp__app-config__updateAutomation",
        "mcp__app-config__deleteAutomation",
        "mcp__app-config__toggleAutomation",
        "mcp__app-config__triggerAutomation",
        "mcp__app-config__getAutomationRuns",
        # MCP Servers
        "mcp__app-config__listMcpServers",
        "mcp__app-config__addMcpServer",
        "mcp__app-config__removeMcpServer",
        "mcp__app-config__toggleMcpServer",
        # Profiles
        "mcp__app-config__listProfiles",
        "mcp__app-config__getProfile",
        "mcp__app-config__createProfile",
        "mcp__app-config__updateProfile",
        "mcp__app-config__deleteProfile",
        # Connectors
        "mcp__app-config__listConnectors",
        "mcp__app-config__getConnectorStatus",
        "mcp__app-config__toggleConnector",
        # Subagents
        "mcp__app-config__listSubagents",
        "mcp__app-config__getSubagent",
        "mcp__app-config__createSubagent",
        "mcp__app-config__updateSubagent",
        "mcp__app-config__deleteSubagent",
    ]


def collect_app_config_tools(
    is_admin: bool = False,
    project_id: Optional[str] = None,
    project_dir: Optional[str] = None,
) -> list:
    """The raw ``@tool`` objects of the app-config surface, no server around them.

    Shared by :func:`create_app_config_server` (chat sessions, wrapped in an
    SDK MCP server) and the voice bridge, which declares the same tools on the
    OpenAI Realtime sideband and calls their handlers in-process. Returns an
    empty list when the SDK is unavailable — the caller degrades, never fails.
    """
    try:
        import app.mcp.inproc  # noqa: F401 — the tool factories need @tool
    except ImportError:
        logger.debug("Fluid tool helpers not available — app-config tools disabled")
        return []

    all_tools: list = []
    all_tools.extend(_create_discovery_tools())
    all_tools.extend(_create_hook_tools(is_admin, project_id))
    all_tools.extend(_create_automation_tools(is_admin, project_id))
    all_tools.extend(_create_mcp_tools(is_admin, project_dir))
    all_tools.extend(_create_profile_tools(is_admin))
    all_tools.extend(_create_connector_tools(is_admin))
    all_tools.extend(_create_subagent_tools(is_admin))
    return all_tools


def create_app_config_server(
    is_admin: bool = False,
    project_id: Optional[str] = None,
    project_dir: Optional[str] = None,
) -> Optional[Any]:
    """
    Create the app-config SDK MCP server with all configuration tools.

    Args:
        is_admin: Whether the current session is an admin session.
                  Write tools check this at runtime.
        project_id: Current project *identifier* for project-scoped hook and
                  automation queries.
        project_dir: Current project *directory* (filesystem path) for the MCP
                  server tools' LOCAL/PROJECT scope .mcp.json files. This is a
                  path, not an identifier — do not pass project_id here.

    Returns:
        McpSdkServerConfig from create_tool_server(), or None on failure.
    """
    try:
        from app.mcp.inproc import create_tool_server
    except ImportError:
        logger.debug("Fluid tool helpers not available — app-config tools disabled")
        return None

    all_tools = collect_app_config_tools(is_admin, project_id, project_dir)
    if not all_tools:
        return None

    logger.info(
        "Registering %d app-config tool(s) as MCP tools (admin=%s, project=%s)",
        len(all_tools), is_admin, project_id,
    )

    from app.mcp.sdk_compat import patch_sdk_server
    return patch_sdk_server(create_tool_server(
        name="app-config",
        version="1.0.0",
        tools=all_tools,
    ), all_tools)

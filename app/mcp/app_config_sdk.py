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
"""

import json
import logging
import secrets as _secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

VALID_HOOK_EVENTS = [
    "PreToolUse", "PostToolUse", "Stop", "SessionStart",
    "SessionEnd", "UserPromptSubmit", "PreCompact",
    "SubagentStop", "Notification",
]

VALID_HOOK_TYPES = ["bash", "webhook", "message"]

VALID_TRIGGER_TYPES = ["cron", "interval", "webhook"]


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
# Helpers
# ============================================================================

def _tool_result(result: dict) -> dict:
    """Wrap a result dict in the MCP tool response format."""
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


def _error(message: str) -> dict:
    """Return an error result."""
    return _tool_result({"success": False, "error": message})


def _sanitize_hook(hook: dict) -> dict:
    """Strip secrets from hook data before returning to the agent."""
    safe = dict(hook)
    if safe.get("secret"):
        safe["secret"] = "••••••••"
    return safe


def _sanitize_automation(auto: dict) -> dict:
    """Strip secrets from automation data before returning to the agent."""
    safe = dict(auto)
    # Mask secrets in trigger_config
    tc = safe.get("trigger_config")
    if tc and isinstance(tc, dict) and tc.get("secret"):
        tc = dict(tc)
        tc["secret"] = "••••••••"
        safe["trigger_config"] = tc
    # Mask secrets in triggers array
    triggers = safe.get("triggers")
    if triggers and isinstance(triggers, list):
        safe_triggers = []
        for t in triggers:
            t = dict(t)
            if t.get("secret"):
                t["secret"] = "••••••••"
            safe_triggers.append(t)
        safe["triggers"] = safe_triggers
    return safe


def _sanitize_connector(conn: dict) -> dict:
    """Strip secrets from connector config."""
    safe = dict(conn)
    config = safe.get("config")
    if config and isinstance(config, dict):
        masked = {}
        for k, v in config.items():
            if any(secret_word in k.lower() for secret_word in
                   ("token", "secret", "key", "password", "credential")):
                masked[k] = "••••••••" if v else None
            else:
                masked[k] = v
        safe["config"] = masked
    return safe


def _hook_summary(hook: dict) -> dict:
    """Return a compact summary of a hook for list views."""
    return {
        "id": hook.get("id"),
        "name": hook.get("name"),
        "event": hook.get("event"),
        "hook_type": hook.get("hook_type"),
        "enabled": hook.get("enabled"),
        "tool_matcher": hook.get("tool_matcher"),
        "scope": "project" if hook.get("project_id") else "app",
        "last_triggered_at": hook.get("last_triggered_at"),
        "failure_count": hook.get("failure_count", 0),
    }


def _automation_summary(auto: dict) -> dict:
    """Return a compact summary of an automation for list views."""
    return {
        "id": auto.get("id"),
        "name": auto.get("name"),
        "description": auto.get("description"),
        "trigger_type": auto.get("trigger_type"),
        "is_enabled": auto.get("is_enabled"),
        "status": auto.get("status"),
        "last_run_at": auto.get("last_run_at"),
        "run_count": auto.get("run_count", 0),
        "failure_count": auto.get("failure_count", 0),
        "total_cost_used": auto.get("total_cost_used", 0),
        "requires_approval": auto.get("requires_approval", False),
    }


# ============================================================================
# Discovery Tools
# ============================================================================

def _create_discovery_tools() -> list:
    """Create the getConfigCapabilities discovery tool."""
    from claude_agent_sdk import tool
    from app.db import database

    tools = []

    @tool(
        "getConfigCapabilities",
        "Discover what can be configured in AI Shuffle — valid event types, "
        "trigger types, available connectors, and current system stats. "
        "Call this first before creating hooks, automations, or other config.",
        {"type": "object", "properties": {}},
    )
    async def get_capabilities(args: dict) -> dict:
        all_hooks = database.get_app_hooks()
        all_automations = database.get_all_automations()
        all_profiles = database.get_all_profiles()

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
                "spotify", "hue", "home_assistant", "gmail",
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
# Hook Tools
# ============================================================================

def _create_hook_tools(is_admin: bool, project_id: Optional[str]) -> list:
    """Create hook management tools."""
    from claude_agent_sdk import tool
    from app.db import database

    _is_admin = is_admin
    _project_id = project_id
    tools = []

    # -- listHooks --------------------------------------------------------

    @tool(
        "listHooks",
        "List hooks configured in AI Shuffle, optionally filtered by scope or event type.",
        {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["app", "project", "all"],
                    "description": "Filter by scope. 'all' returns both app and project hooks.",
                },
                "event": {
                    "type": "string",
                    "enum": VALID_HOOK_EVENTS,
                    "description": "Filter by event type.",
                },
            },
        },
    )
    async def list_hooks(args: dict) -> dict:
        scope = args.get("scope", "all")

        if scope == "project" and _project_id:
            hooks = database.get_hooks_for_project(_project_id)
        elif scope == "app":
            hooks = database.get_app_hooks()
        elif scope == "all" and _project_id:
            hooks = database.get_all_hooks_for_project(_project_id)
        else:
            hooks = database.get_app_hooks()

        event_filter = args.get("event")
        if event_filter:
            hooks = [h for h in hooks if h.get("event") == event_filter]

        return _tool_result({
            "hooks": [_hook_summary(h) for h in hooks],
            "count": len(hooks),
        })

    tools.append(list_hooks)

    # -- getHook ----------------------------------------------------------

    @tool(
        "getHook",
        "Get full details of a specific hook by ID.",
        {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "The hook ID."},
            },
            "required": ["hook_id"],
        },
    )
    async def get_hook(args: dict) -> dict:
        hook = database.get_hook(args["hook_id"])
        if not hook:
            return _error(f"Hook not found: {args['hook_id']}")
        return _tool_result(_sanitize_hook(hook))

    tools.append(get_hook)

    # -- createHook -------------------------------------------------------

    @tool(
        "createHook",
        "Create a new hook that triggers on specific AI Shuffle events. "
        "Hooks can run bash commands, send webhooks, or inject messages.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable hook name."},
                "event": {
                    "type": "string",
                    "enum": VALID_HOOK_EVENTS,
                    "description": "Event that triggers this hook.",
                },
                "hook_type": {
                    "type": "string",
                    "enum": VALID_HOOK_TYPES,
                    "description": "How the hook executes: bash command, webhook POST, or message injection.",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to execute (bash type only). "
                    "For PreToolUse: must output JSON with 'decision' (allow/block) and optional 'reason'.",
                },
                "url": {"type": "string", "description": "Webhook URL (webhook type only)."},
                "message_template": {
                    "type": "string",
                    "description": "Message template with {{variable}} substitution (message type only).",
                },
                "tool_matcher": {
                    "type": "string",
                    "description": "Regex matching tool names. Required for PreToolUse/PostToolUse events (e.g., 'Bash', 'Write|Edit').",
                },
                "scope": {
                    "type": "string",
                    "enum": ["app", "project"],
                    "description": "Scope: 'app' (global) or 'project' (current project only). Defaults to 'app'.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30, max 300).",
                },
                "enabled": {"type": "boolean", "description": "Whether the hook is enabled immediately. Defaults to true."},
            },
            "required": ["name", "event", "hook_type"],
        },
    )
    async def create_hook(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        event = args["event"]
        hook_type = args["hook_type"]

        # Validate event
        if event not in VALID_HOOK_EVENTS:
            return _error(f"Invalid event '{event}'. Valid: {VALID_HOOK_EVENTS}")

        # Validate hook_type has required fields
        if hook_type == "bash" and not args.get("command"):
            return _error("Bash hooks require a 'command' field.")
        if hook_type == "webhook" and not args.get("url"):
            return _error("Webhook hooks require a 'url' field.")
        if hook_type == "message" and not args.get("message_template"):
            return _error("Message hooks require a 'message_template' field.")

        # PreToolUse/PostToolUse should have a tool_matcher
        if event in ("PreToolUse", "PostToolUse") and not args.get("tool_matcher"):
            return _error(
                f"{event} hooks require a 'tool_matcher' regex to filter by tool name "
                "(e.g., 'Bash', 'Write', 'Edit', 'Read'). Without one the hook "
                "would fire on EVERY tool call."
            )

        # Determine scope and project_id
        scope = args.get("scope", "app")
        pid = _project_id if scope == "project" else None
        if scope == "project" and not pid:
            return _error("No project context. Use scope='app' or switch to a project first.")

        # Timeout: user provides seconds, DB stores milliseconds
        timeout_s = min(args.get("timeout", 30), 300)

        hook_id = f"hook-{_secrets.token_hex(4)}"
        hook = database.create_hook(
            hook_id=hook_id,
            name=args["name"],
            event=event,
            hook_type=hook_type,
            project_id=pid,
            command=args.get("command"),
            timeout=timeout_s * 1000,
            url=args.get("url"),
            enabled=args.get("enabled", True),
            tool_matcher=args.get("tool_matcher"),
            message_template=args.get("message_template"),
        )

        if not hook:
            return _error("Failed to create hook — database error.")

        return _tool_result({
            "success": True,
            "hook_id": hook_id,
            "name": args["name"],
            "event": event,
            "hook_type": hook_type,
            "scope": scope,
            "enabled": args.get("enabled", True),
            "message": (
                f"Hook '{args['name']}' created. It will trigger on {event} events"
                + (f" matching tool pattern '{args.get('tool_matcher')}'" if args.get("tool_matcher") else "")
                + ". Visible in Settings → Hooks."
            ),
        })

    tools.append(create_hook)

    # -- updateHook -------------------------------------------------------

    @tool(
        "updateHook",
        "Update properties of an existing hook.",
        {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "The hook ID to update."},
                "name": {"type": "string"},
                "event": {"type": "string", "enum": VALID_HOOK_EVENTS},
                "hook_type": {"type": "string", "enum": VALID_HOOK_TYPES},
                "command": {"type": "string"},
                "url": {"type": "string"},
                "message_template": {"type": "string"},
                "tool_matcher": {"type": "string"},
                "timeout": {"type": "integer", "description": "Timeout in seconds."},
                "enabled": {"type": "boolean"},
            },
            "required": ["hook_id"],
        },
    )
    async def update_hook(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        hook_id = args["hook_id"]
        existing = database.get_hook(hook_id)
        if not existing:
            return _error(f"Hook not found: {hook_id}")

        # Build update kwargs — only include provided fields
        kwargs: Dict[str, Any] = {}
        for field in ("name", "event", "hook_type", "command", "url",
                      "message_template", "tool_matcher", "enabled"):
            if field in args and args[field] is not None:
                kwargs[field] = args[field]

        if "timeout" in args and args["timeout"] is not None:
            kwargs["timeout"] = min(args["timeout"], 300) * 1000  # seconds → ms

        if not kwargs:
            return _error("No fields provided to update.")

        updated = database.update_hook(hook_id, **kwargs)
        if not updated:
            return _error("Failed to update hook.")

        return _tool_result({
            "success": True,
            "hook_id": hook_id,
            "updated_fields": list(kwargs.keys()),
            "message": f"Hook '{updated.get('name')}' updated.",
        })

    tools.append(update_hook)

    # -- deleteHook -------------------------------------------------------

    @tool(
        "deleteHook",
        "Delete a hook. This cannot be undone.",
        {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "The hook ID to delete."},
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to confirm deletion. Always ask the user before setting this.",
                },
            },
            "required": ["hook_id", "confirm"],
        },
    )
    async def delete_hook(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")
        if not args.get("confirm"):
            return _error("Set confirm=true to delete. Ask the user for confirmation first.")

        hook = database.get_hook(args["hook_id"])
        if not hook:
            return _error(f"Hook not found: {args['hook_id']}")

        name = hook.get("name", args["hook_id"])
        deleted = database.delete_hook(args["hook_id"])
        if not deleted:
            return _error("Failed to delete hook.")

        return _tool_result({
            "success": True,
            "message": f"Hook '{name}' deleted.",
        })

    tools.append(delete_hook)

    # -- toggleHook -------------------------------------------------------

    @tool(
        "toggleHook",
        "Enable or disable a hook without deleting it.",
        {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string"},
                "enabled": {"type": "boolean", "description": "True to enable, false to disable."},
            },
            "required": ["hook_id", "enabled"],
        },
    )
    async def toggle_hook(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        hook = database.get_hook(args["hook_id"])
        if not hook:
            return _error(f"Hook not found: {args['hook_id']}")

        updated = database.update_hook(args["hook_id"], enabled=args["enabled"])
        if not updated:
            return _error("Failed to toggle hook.")

        state = "enabled" if args["enabled"] else "disabled"
        return _tool_result({
            "success": True,
            "hook_id": args["hook_id"],
            "enabled": args["enabled"],
            "message": f"Hook '{updated.get('name')}' {state}.",
        })

    tools.append(toggle_hook)

    # -- testHook ---------------------------------------------------------

    @tool(
        "testHook",
        "Test-fire a hook with sample event data and return the result.",
        {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "The hook ID to test."},
            },
            "required": ["hook_id"],
        },
    )
    async def test_hook(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        hook = database.get_hook(args["hook_id"])
        if not hook:
            return _error(f"Hook not found: {args['hook_id']}")

        try:
            from app.core.hook_executor import test_hook as execute_test_hook
            result = await execute_test_hook(hook)
            return _tool_result({
                "success": True,
                "hook_id": args["hook_id"],
                "test_result": result,
            })
        except Exception as e:
            return _tool_result({
                "success": False,
                "hook_id": args["hook_id"],
                "error": f"Test failed: {str(e)}",
            })

    tools.append(test_hook)

    return tools


# ============================================================================
# Automation Tools
# ============================================================================

def _create_automation_tools(is_admin: bool, project_id: Optional[str]) -> list:
    """Create automation management tools."""
    from claude_agent_sdk import tool
    from app.db import database

    _is_admin = is_admin
    _project_id = project_id
    tools = []

    # -- listAutomations --------------------------------------------------

    @tool(
        "listAutomations",
        "List all automations with their status, schedule, and run stats.",
        {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["idle", "running", "paused", "error"],
                    "description": "Filter by status.",
                },
                "enabled_only": {
                    "type": "boolean",
                    "description": "If true, only return enabled automations.",
                },
            },
        },
    )
    async def list_automations(args: dict) -> dict:
        automations = database.get_all_automations()

        if args.get("status"):
            automations = [a for a in automations if a.get("status") == args["status"]]
        if args.get("enabled_only"):
            automations = [a for a in automations if a.get("is_enabled")]

        return _tool_result({
            "automations": [_automation_summary(a) for a in automations],
            "count": len(automations),
        })

    tools.append(list_automations)

    # -- getAutomation ----------------------------------------------------

    @tool(
        "getAutomation",
        "Get full details of a specific automation including trigger config, budget, and recent runs.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
            },
            "required": ["automation_id"],
        },
    )
    async def get_automation(args: dict) -> dict:
        auto = database.get_automation(args["automation_id"])
        if not auto:
            return _error(f"Automation not found: {args['automation_id']}")

        sanitized = _sanitize_automation(auto)

        # Include recent runs summary
        runs = database.get_automation_runs(args["automation_id"], limit=5)
        sanitized["recent_runs"] = [
            {
                "id": r.get("id"),
                "status": r.get("status"),
                "cost_usd": r.get("cost_usd", 0),
                "duration_ms": r.get("duration_ms", 0),
                "started_at": r.get("started_at"),
                "completed_at": r.get("completed_at"),
                "error": r.get("error"),
            }
            for r in runs
        ]

        return _tool_result(sanitized)

    tools.append(get_automation)

    # -- createAutomation -------------------------------------------------

    @tool(
        "createAutomation",
        "Create a scheduled automation that runs a Claude prompt on a cron schedule, "
        "at fixed intervals, or via webhook trigger. Always set budget limits.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Automation name."},
                "description": {"type": "string", "description": "What this automation does."},
                "trigger_type": {
                    "type": "string",
                    "enum": VALID_TRIGGER_TYPES,
                    "description": "How the automation is triggered.",
                },
                "cron_expression": {
                    "type": "string",
                    "description": "Cron expression for 'cron' trigger (e.g., '0 9 * * 1-5').",
                },
                "cron_timezone": {
                    "type": "string",
                    "description": "Timezone for cron (default 'UTC'). E.g., 'America/New_York'.",
                },
                "interval_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "description": "Seconds between runs for 'interval' trigger (minimum 60).",
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt to send to Claude when triggered. Supports {{variable}} template syntax.",
                },
                "profile_id": {
                    "type": "string",
                    "description": "Profile to use for execution. Use listProfiles to find available profiles.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Project scope. Uses current project if not specified.",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Maximum conversation turns per run (default 1).",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds per run (default 300).",
                },
                "max_cost_per_run": {
                    "type": "number",
                    "description": "Maximum cost in USD per run (strongly recommended).",
                },
                "max_cost_per_day": {
                    "type": "number",
                    "description": "Maximum daily cost in USD.",
                },
                "max_runs_per_day": {
                    "type": "integer",
                    "description": "Maximum number of runs per day.",
                },
                "requires_approval": {
                    "type": "boolean",
                    "description": "If true, each run requires manual approval before executing.",
                },
            },
            "required": ["name", "trigger_type", "prompt", "profile_id"],
        },
    )
    async def create_automation(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        trigger_type = args["trigger_type"]

        # Validate profile exists
        profile = database.get_profile(args["profile_id"])
        if not profile:
            return _error(
                f"Profile '{args['profile_id']}' not found. "
                "Use listProfiles to see available profiles."
            )

        # Build trigger config
        trigger_config: Dict[str, Any] = {}
        if trigger_type == "cron":
            if not args.get("cron_expression"):
                return _error("Cron automations require 'cron_expression' (e.g., '0 9 * * 1-5').")
            trigger_config = {
                "expression": args["cron_expression"],
                "timezone": args.get("cron_timezone", "UTC"),
            }
        elif trigger_type == "interval":
            if not args.get("interval_seconds"):
                return _error("Interval automations require 'interval_seconds' (minimum 60).")
            if args["interval_seconds"] < 60:
                return _error("Interval must be at least 60 seconds.")
            trigger_config = {"seconds": args["interval_seconds"]}
        elif trigger_type == "webhook":
            trigger_config = {"secret": _secrets.token_urlsafe(32)}

        # Enforce budget defaults if not provided
        max_cost_per_run = args.get("max_cost_per_run")
        if max_cost_per_run is None:
            max_cost_per_run = 0.50  # Safe default

        automation_id = f"auto-{_secrets.token_hex(4)}"
        proj_id = args.get("project_id") or _project_id

        auto = database.create_automation(
            automation_id=automation_id,
            name=args["name"],
            description=args.get("description"),
            trigger_type=trigger_type,
            trigger_config=trigger_config,
            prompt=args["prompt"],
            profile_id=args["profile_id"],
            project_id=proj_id,
            max_turns=args.get("max_turns", 1),
            timeout_seconds=args.get("timeout_seconds", 300),
            max_cost_per_run=max_cost_per_run,
            max_cost_per_day=args.get("max_cost_per_day"),
            max_runs_per_day=args.get("max_runs_per_day"),
            requires_approval=args.get("requires_approval", False),
        )

        if not auto:
            return _error("Failed to create automation — database error.")

        # Schedule it with APScheduler
        scheduled = False
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.schedule_automation(automation_id)
            scheduled = True
        except Exception as e:
            logger.warning(f"Automation created but scheduling failed: {e}")

        result: Dict[str, Any] = {
            "success": True,
            "automation_id": automation_id,
            "name": args["name"],
            "trigger_type": trigger_type,
            "scheduled": scheduled,
            "max_cost_per_run": max_cost_per_run,
        }

        if trigger_type == "cron":
            result["schedule"] = args["cron_expression"]
            result["timezone"] = args.get("cron_timezone", "UTC")
        elif trigger_type == "interval":
            result["interval_seconds"] = args["interval_seconds"]
        elif trigger_type == "webhook":
            result["webhook_note"] = "Webhook secret has been auto-generated. Use the Automations UI to view the endpoint URL."

        result["message"] = (
            f"Automation '{args['name']}' created"
            + (" and scheduled." if scheduled else " (scheduling deferred — will activate on next restart).")
            + " Visible in the Automations panel."
        )

        return _tool_result(result)

    tools.append(create_automation)

    # -- updateAutomation -------------------------------------------------

    @tool(
        "updateAutomation",
        "Update properties of an existing automation.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "prompt": {"type": "string"},
                "profile_id": {"type": "string"},
                "max_turns": {"type": "integer"},
                "timeout_seconds": {"type": "integer"},
                "max_cost_per_run": {"type": "number"},
                "max_cost_per_day": {"type": "number"},
                "max_runs_per_day": {"type": "integer"},
                "requires_approval": {"type": "boolean"},
            },
            "required": ["automation_id"],
        },
    )
    async def update_automation(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        auto_id = args["automation_id"]
        existing = database.get_automation(auto_id)
        if not existing:
            return _error(f"Automation not found: {auto_id}")

        # Build update kwargs
        kwargs: Dict[str, Any] = {}
        for field in ("name", "description", "prompt", "profile_id",
                      "max_turns", "timeout_seconds", "max_cost_per_run",
                      "max_cost_per_day", "max_runs_per_day", "requires_approval"):
            if field in args and args[field] is not None:
                kwargs[field] = args[field]

        if not kwargs:
            return _error("No fields provided to update.")

        # Validate profile if changing
        if "profile_id" in kwargs:
            profile = database.get_profile(kwargs["profile_id"])
            if not profile:
                return _error(f"Profile '{kwargs['profile_id']}' not found.")

        updated = database.update_automation(auto_id, **kwargs)
        if not updated:
            return _error("Failed to update automation.")

        return _tool_result({
            "success": True,
            "automation_id": auto_id,
            "updated_fields": list(kwargs.keys()),
            "message": f"Automation '{updated.get('name')}' updated.",
        })

    tools.append(update_automation)

    # -- deleteAutomation -------------------------------------------------

    @tool(
        "deleteAutomation",
        "Delete an automation and all its run history. This cannot be undone.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to confirm deletion.",
                },
            },
            "required": ["automation_id", "confirm"],
        },
    )
    async def delete_automation(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")
        if not args.get("confirm"):
            return _error("Set confirm=true to delete. Ask the user for confirmation first.")

        auto = database.get_automation(args["automation_id"])
        if not auto:
            return _error(f"Automation not found: {args['automation_id']}")

        name = auto.get("name", args["automation_id"])

        # Unschedule first
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.unschedule_automation(args["automation_id"])
        except Exception as e:
            logger.warning(f"Failed to unschedule automation: {e}")

        deleted = database.delete_automation(args["automation_id"])
        if not deleted:
            return _error("Failed to delete automation.")

        return _tool_result({
            "success": True,
            "message": f"Automation '{name}' deleted (including all run history).",
        })

    tools.append(delete_automation)

    # -- toggleAutomation -------------------------------------------------

    @tool(
        "toggleAutomation",
        "Enable or disable an automation without deleting it.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "enabled": {"type": "boolean", "description": "True to enable, false to disable."},
            },
            "required": ["automation_id", "enabled"],
        },
    )
    async def toggle_automation(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        auto = database.get_automation(args["automation_id"])
        if not auto:
            return _error(f"Automation not found: {args['automation_id']}")

        updated = database.update_automation(args["automation_id"], is_enabled=args["enabled"])
        if not updated:
            return _error("Failed to toggle automation.")

        # Update scheduling
        try:
            from app.core.automation_manager import automation_manager
            if args["enabled"]:
                await automation_manager.schedule_automation(args["automation_id"])
            else:
                await automation_manager.unschedule_automation(args["automation_id"])
        except Exception as e:
            logger.warning(f"Schedule update failed: {e}")

        state = "enabled" if args["enabled"] else "disabled"
        return _tool_result({
            "success": True,
            "automation_id": args["automation_id"],
            "enabled": args["enabled"],
            "message": f"Automation '{updated.get('name')}' {state}.",
        })

    tools.append(toggle_automation)

    # -- triggerAutomation ------------------------------------------------

    @tool(
        "triggerAutomation",
        "Manually trigger an automation run right now.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
            },
            "required": ["automation_id"],
        },
    )
    async def trigger_automation(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        auto = database.get_automation(args["automation_id"])
        if not auto:
            return _error(f"Automation not found: {args['automation_id']}")

        try:
            from app.core.automation_manager import automation_manager
            result = await automation_manager.trigger_manual(args["automation_id"])
            return _tool_result({
                "success": True,
                "automation_id": args["automation_id"],
                "message": f"Automation '{auto.get('name')}' triggered manually.",
                "run_info": result,
            })
        except Exception as e:
            return _error(f"Failed to trigger automation: {str(e)}")

    tools.append(trigger_automation)

    # -- getAutomationRuns ------------------------------------------------

    @tool(
        "getAutomationRuns",
        "Get run history for an automation.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum runs to return (default 10).",
                },
            },
            "required": ["automation_id"],
        },
    )
    async def get_automation_runs(args: dict) -> dict:
        auto = database.get_automation(args["automation_id"])
        if not auto:
            return _error(f"Automation not found: {args['automation_id']}")

        limit = min(args.get("limit", 10), 50)
        runs = database.get_automation_runs(args["automation_id"], limit=limit)

        return _tool_result({
            "automation_id": args["automation_id"],
            "automation_name": auto.get("name"),
            "runs": [
                {
                    "id": r.get("id"),
                    "status": r.get("status"),
                    "cost_usd": r.get("cost_usd", 0),
                    "tokens_in": r.get("tokens_in", 0),
                    "tokens_out": r.get("tokens_out", 0),
                    "duration_ms": r.get("duration_ms", 0),
                    "started_at": r.get("started_at"),
                    "completed_at": r.get("completed_at"),
                    "error": r.get("error"),
                }
                for r in runs
            ],
            "count": len(runs),
        })

    tools.append(get_automation_runs)

    return tools


# ============================================================================
# MCP Server Tools
# ============================================================================

def _create_mcp_tools(is_admin: bool, project_id: Optional[str]) -> list:
    """Create MCP server management tools."""
    from claude_agent_sdk import tool
    from app.core.mcp_service import get_mcp_service, McpScope, McpServerType

    _is_admin = is_admin
    _project_id = project_id
    tools = []

    def _server_to_dict(server) -> dict:
        """Convert McpServerConfig dataclass to serializable dict."""
        return {
            "name": server.name,
            "type": server.type.value if hasattr(server.type, "value") else str(server.type),
            "command": server.command,
            "args": server.args,
            "url": server.url,
            "scope": server.scope if isinstance(server.scope, str) else str(server.scope),
            "enabled": server.enabled,
            "description": server.description,
        }

    # -- listMcpServers ---------------------------------------------------

    @tool(
        "listMcpServers",
        "List all configured MCP servers across all scopes (user, local, project).",
        {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["user", "local", "project", "all"],
                    "description": "Filter by scope. Defaults to 'all'.",
                },
            },
        },
    )
    async def list_mcp_servers(args: dict) -> dict:
        service = get_mcp_service(project_dir=_project_id)
        scope_filter = args.get("scope", "all")

        if scope_filter == "all":
            all_servers = service.get_all_servers()
            result = {}
            for scope_name, servers in all_servers.items():
                result[scope_name] = [_server_to_dict(s) for s in servers]
            total = sum(len(v) for v in result.values())
            return _tool_result({"servers": result, "total": total})
        else:
            scope_map = {"user": McpScope.USER, "local": McpScope.LOCAL, "project": McpScope.PROJECT}
            scope = scope_map.get(scope_filter)
            if not scope:
                return _error(f"Invalid scope: {scope_filter}")
            servers = service.get_servers_for_scope(scope)
            return _tool_result({
                "scope": scope_filter,
                "servers": [_server_to_dict(s) for s in servers],
                "count": len(servers),
            })

    tools.append(list_mcp_servers)

    # -- addMcpServer -----------------------------------------------------

    @tool(
        "addMcpServer",
        "Add a new MCP server configuration. Requires a session restart to take effect.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique server name."},
                "server_type": {
                    "type": "string",
                    "enum": ["stdio", "sse", "http"],
                    "description": "Server type.",
                },
                "command": {
                    "type": "string",
                    "description": "Command to run (stdio type). E.g., 'npx', 'python', 'node'.",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command arguments (stdio type).",
                },
                "url": {
                    "type": "string",
                    "description": "Server URL (sse/http type).",
                },
                "env": {
                    "type": "object",
                    "description": "Environment variables for the server process.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["user", "local", "project"],
                    "description": "Where to save: 'user' (global), 'local' (project, gitignored), 'project' (committed). Default 'user'.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional description.",
                },
            },
            "required": ["name", "server_type"],
        },
    )
    async def add_mcp_server(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        server_type = args["server_type"]
        if server_type == "stdio" and not args.get("command"):
            return _error("stdio servers require a 'command'.")
        if server_type in ("sse", "http") and not args.get("url"):
            return _error(f"{server_type} servers require a 'url'.")

        scope_str = args.get("scope", "user")
        scope_map = {"user": McpScope.USER, "local": McpScope.LOCAL, "project": McpScope.PROJECT}
        scope = scope_map.get(scope_str)
        if not scope:
            return _error(f"Invalid scope: {scope_str}")

        from app.core.mcp_service import McpServerConfig
        type_map = {"stdio": McpServerType.STDIO, "sse": McpServerType.SSE, "http": McpServerType.HTTP}

        config = McpServerConfig(
            name=args["name"],
            type=type_map[server_type],
            command=args.get("command"),
            args=args.get("args"),
            env=args.get("env"),
            url=args.get("url"),
            scope=scope_str,
            enabled=True,
            description=args.get("description"),
        )

        try:
            service = get_mcp_service(project_dir=_project_id)
            service.add_server(args["name"], config, scope)
            return _tool_result({
                "success": True,
                "name": args["name"],
                "server_type": server_type,
                "scope": scope_str,
                "message": (
                    f"MCP server '{args['name']}' added to {scope_str} scope. "
                    "A session restart is needed for the agent to use it."
                ),
            })
        except Exception as e:
            return _error(f"Failed to add MCP server: {str(e)}")

    tools.append(add_mcp_server)

    # -- removeMcpServer --------------------------------------------------

    @tool(
        "removeMcpServer",
        "Remove an MCP server configuration. This cannot be undone.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Server name."},
                "scope": {
                    "type": "string",
                    "enum": ["user", "local", "project"],
                    "description": "The scope to remove from.",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to confirm removal.",
                },
            },
            "required": ["name", "scope", "confirm"],
        },
    )
    async def remove_mcp_server(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")
        if not args.get("confirm"):
            return _error("Set confirm=true to remove. Ask the user for confirmation first.")

        scope_map = {"user": McpScope.USER, "local": McpScope.LOCAL, "project": McpScope.PROJECT}
        scope = scope_map.get(args["scope"])
        if not scope:
            return _error(f"Invalid scope: {args['scope']}")

        try:
            service = get_mcp_service(project_dir=_project_id)
            service.remove_server(args["name"], scope)
            return _tool_result({
                "success": True,
                "message": f"MCP server '{args['name']}' removed from {args['scope']} scope.",
            })
        except Exception as e:
            return _error(f"Failed to remove MCP server: {str(e)}")

    tools.append(remove_mcp_server)

    # -- toggleMcpServer --------------------------------------------------

    @tool(
        "toggleMcpServer",
        "Enable or disable an MCP server in the user scope.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Server name."},
                "enabled": {"type": "boolean"},
            },
            "required": ["name", "enabled"],
        },
    )
    async def toggle_mcp_server(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        try:
            service = get_mcp_service(project_dir=_project_id)
            service.set_server_enabled(args["name"], args["enabled"])
            state = "enabled" if args["enabled"] else "disabled"
            return _tool_result({
                "success": True,
                "name": args["name"],
                "enabled": args["enabled"],
                "message": f"MCP server '{args['name']}' {state}.",
            })
        except Exception as e:
            return _error(f"Failed to toggle MCP server: {str(e)}")

    tools.append(toggle_mcp_server)

    return tools


# ============================================================================
# Profile Tools
# ============================================================================

def _create_profile_tools(is_admin: bool) -> list:
    """Create profile management tools."""
    from claude_agent_sdk import tool
    from app.db import database

    _is_admin = is_admin
    tools = []

    # -- listProfiles -----------------------------------------------------

    @tool(
        "listProfiles",
        "List all available profiles with their model, tools preset, and agent configuration.",
        {"type": "object", "properties": {}},
    )
    async def list_profiles(args: dict) -> dict:
        profiles = database.get_all_profiles()
        return _tool_result({
            "profiles": [
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "description": p.get("description"),
                    "is_builtin": p.get("is_builtin", False),
                    "model": p.get("config", {}).get("model"),
                    "tools_preset": p.get("config", {}).get("tools_preset"),
                    "permission_mode": p.get("config", {}).get("permission_mode"),
                    "app_config_tools": p.get("config", {}).get("app_config_tools", False),
                }
                for p in profiles
            ],
            "count": len(profiles),
        })

    tools.append(list_profiles)

    # -- getProfile -------------------------------------------------------

    @tool(
        "getProfile",
        "Get full configuration of a specific profile.",
        {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string"},
            },
            "required": ["profile_id"],
        },
    )
    async def get_profile(args: dict) -> dict:
        profile = database.get_profile(args["profile_id"])
        if not profile:
            return _error(f"Profile not found: {args['profile_id']}")
        return _tool_result(profile)

    tools.append(get_profile)

    # -- createProfile ----------------------------------------------------

    @tool(
        "createProfile",
        "Create a new AI profile with specific model, tools, and behavior settings.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Profile name (1-100 chars)."},
                "description": {"type": "string"},
                "model": {
                    "type": "string",
                    "enum": ["sonnet", "opus", "haiku"],
                    "description": "AI model to use.",
                },
                "tools_preset": {
                    "type": "string",
                    "enum": ["claude_code"],
                    "description": "Tools preset. 'claude_code' gives access to all standard tools.",
                },
                "permission_mode": {
                    "type": "string",
                    "enum": ["default", "acceptEdits", "plan", "bypassPermissions"],
                    "description": "Tool permission mode.",
                },
                "thinking_mode": {
                    "type": "string",
                    "enum": ["default", "disabled", "adaptive", "think", "think_harder", "ultra_think"],
                },
                "system_prompt_content": {
                    "type": "string",
                    "description": "Custom system prompt content. If omitted, uses claude_code preset.",
                },
                "system_prompt_append": {
                    "type": "string",
                    "description": "Text to append to the preset system prompt.",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit tool whitelist (e.g., ['Read', 'Grep', 'Glob']).",
                },
                "disallowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool blacklist (e.g., ['Bash', 'Write']).",
                },
                "max_turns": {"type": "integer"},
                "app_config_tools": {
                    "type": "boolean",
                    "description": "Enable app configuration tools on this profile.",
                },
            },
            "required": ["name"],
        },
    )
    async def create_profile(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        import re
        # Generate ID from name
        profile_id = re.sub(r'[^a-z0-9-]', '-', args["name"].lower().strip())
        profile_id = re.sub(r'-+', '-', profile_id).strip('-')[:50]

        # Check for existing profile with this ID
        existing = database.get_profile(profile_id)
        if existing:
            return _error(
                f"A profile with ID '{profile_id}' already exists. "
                "Choose a different name."
            )

        # Build config
        config: Dict[str, Any] = {
            "model": args.get("model", "sonnet"),
            "permission_mode": args.get("permission_mode", "default"),
            "tools_preset": args.get("tools_preset", "claude_code"),
        }

        if args.get("thinking_mode"):
            config["thinking_mode"] = args["thinking_mode"]
        if args.get("max_turns"):
            config["max_turns"] = args["max_turns"]
        if args.get("allowed_tools"):
            config["allowed_tools"] = args["allowed_tools"]
        if args.get("disallowed_tools"):
            config["disallowed_tools"] = args["disallowed_tools"]
        if args.get("app_config_tools"):
            config["app_config_tools"] = True

        # System prompt
        if args.get("system_prompt_content"):
            config["system_prompt"] = {
                "type": "custom",
                "content": args["system_prompt_content"],
                "inject_env_details": True,
            }
        elif args.get("system_prompt_append"):
            config["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
                "append": args["system_prompt_append"],
            }
        else:
            config["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
            }

        profile = database.create_profile(
            profile_id=profile_id,
            name=args["name"],
            description=args.get("description"),
            config=config,
        )

        return _tool_result({
            "success": True,
            "profile_id": profile_id,
            "name": args["name"],
            "model": config["model"],
            "message": (
                f"Profile '{args['name']}' created (ID: {profile_id}). "
                "Select it from the profile dropdown to use it."
            ),
        })

    tools.append(create_profile)

    # -- updateProfile ----------------------------------------------------

    @tool(
        "updateProfile",
        "Update configuration of an existing profile.",
        {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "model": {"type": "string", "enum": ["sonnet", "opus", "haiku"]},
                "permission_mode": {"type": "string", "enum": ["default", "acceptEdits", "plan", "bypassPermissions"]},
                "thinking_mode": {"type": "string", "enum": ["default", "disabled", "adaptive", "think", "think_harder", "ultra_think"]},
                "tools_preset": {"type": "string", "enum": ["claude_code"]},
                "max_turns": {"type": "integer"},
                "app_config_tools": {"type": "boolean"},
            },
            "required": ["profile_id"],
        },
    )
    async def update_profile(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        profile = database.get_profile(args["profile_id"])
        if not profile:
            return _error(f"Profile not found: {args['profile_id']}")

        # Merge updates into existing config
        config = dict(profile.get("config", {}))
        updated_fields = []

        for field in ("model", "permission_mode", "thinking_mode",
                      "tools_preset", "max_turns", "app_config_tools"):
            if field in args and args[field] is not None:
                config[field] = args[field]
                updated_fields.append(field)

        # Update profile-level fields
        name = args.get("name")
        description = args.get("description")
        if name:
            updated_fields.append("name")
        if description is not None:
            updated_fields.append("description")

        if not updated_fields:
            return _error("No fields provided to update.")

        updated = database.update_profile(
            args["profile_id"],
            name=name,
            description=description,
            config=config,
            allow_builtin=False,
        )

        if not updated:
            return _error("Failed to update profile (may be a built-in profile).")

        return _tool_result({
            "success": True,
            "profile_id": args["profile_id"],
            "updated_fields": updated_fields,
            "message": f"Profile '{updated.get('name')}' updated.",
        })

    tools.append(update_profile)

    return tools


# ============================================================================
# Connector Tools (read-heavy — setup requires secrets via UI)
# ============================================================================

def _create_connector_tools(is_admin: bool) -> list:
    """Create connector status tools."""
    from claude_agent_sdk import tool
    from app.db import database

    _is_admin = is_admin
    tools = []

    # -- listConnectors ---------------------------------------------------

    @tool(
        "listConnectors",
        "List all configured connectors with their type, status, and events.",
        {"type": "object", "properties": {}},
    )
    async def list_connectors(args: dict) -> dict:
        connectors = database.get_all_connectors()
        return _tool_result({
            "connectors": [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "type": c.get("type"),
                    "is_active": c.get("is_active", False),
                    "events": c.get("events", []),
                    "last_activity_at": c.get("last_activity_at"),
                }
                for c in connectors
            ],
            "count": len(connectors),
            "note": "To create or configure connectors (which require API tokens/secrets), use Settings → Connectors in the UI.",
        })

    tools.append(list_connectors)

    # -- getConnectorStatus -----------------------------------------------

    @tool(
        "getConnectorStatus",
        "Get runtime status of a specific connector.",
        {
            "type": "object",
            "properties": {
                "connector_id": {"type": "string"},
            },
            "required": ["connector_id"],
        },
    )
    async def get_connector_status(args: dict) -> dict:
        connector = database.get_connector(args["connector_id"])
        if not connector:
            return _error(f"Connector not found: {args['connector_id']}")

        sanitized = _sanitize_connector(connector)

        # Get runtime status if running
        try:
            from app.core.connectors.manager import connector_manager
            runtime = connector_manager.get_connector_status(args["connector_id"])
            if runtime:
                sanitized["runtime_status"] = runtime
        except Exception:
            pass

        return _tool_result(sanitized)

    tools.append(get_connector_status)

    # -- toggleConnector --------------------------------------------------

    @tool(
        "toggleConnector",
        "Start or stop a connector.",
        {
            "type": "object",
            "properties": {
                "connector_id": {"type": "string"},
                "active": {"type": "boolean", "description": "True to start, false to stop."},
            },
            "required": ["connector_id", "active"],
        },
    )
    async def toggle_connector(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        connector = database.get_connector(args["connector_id"])
        if not connector:
            return _error(f"Connector not found: {args['connector_id']}")

        try:
            from app.core.connectors.manager import connector_manager

            if args["active"]:
                await connector_manager.start_connector(args["connector_id"])
                msg = f"Connector '{connector.get('name')}' started."
            else:
                await connector_manager.stop_connector(args["connector_id"])
                msg = f"Connector '{connector.get('name')}' stopped."

            return _tool_result({"success": True, "message": msg})
        except Exception as e:
            return _error(f"Failed to toggle connector: {str(e)}")

    tools.append(toggle_connector)

    return tools


# ============================================================================
# Server Factory
# ============================================================================

def get_app_config_tool_names(is_admin: bool = True) -> List[str]:
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
        # Connectors
        "mcp__app-config__listConnectors",
        "mcp__app-config__getConnectorStatus",
        "mcp__app-config__toggleConnector",
    ]


def create_app_config_server(
    is_admin: bool = False,
    project_id: Optional[str] = None,
) -> Optional[Any]:
    """
    Create the app-config SDK MCP server with all configuration tools.

    Args:
        is_admin: Whether the current session is an admin session.
                  Write tools check this at runtime.
        project_id: Current project ID for project-scoped operations.

    Returns:
        McpSdkServerConfig from create_sdk_mcp_server(), or None on failure.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server
    except ImportError:
        logger.debug("claude_agent_sdk not available — app-config tools disabled")
        return None

    all_tools: list = []
    all_tools.extend(_create_discovery_tools())
    all_tools.extend(_create_hook_tools(is_admin, project_id))
    all_tools.extend(_create_automation_tools(is_admin, project_id))
    all_tools.extend(_create_mcp_tools(is_admin, project_id))
    all_tools.extend(_create_profile_tools(is_admin))
    all_tools.extend(_create_connector_tools(is_admin))

    if not all_tools:
        return None

    logger.info(
        "Registering %d app-config tool(s) as MCP tools (admin=%s, project=%s)",
        len(all_tools), is_admin, project_id,
    )

    from app.mcp.sdk_compat import patch_sdk_server
    return patch_sdk_server(create_sdk_mcp_server(
        name="app-config",
        version="1.0.0",
        tools=all_tools,
    ), all_tools)

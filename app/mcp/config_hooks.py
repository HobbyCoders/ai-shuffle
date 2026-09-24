"""
Hook management tools for the app-config SDK MCP server.

Provides CRUD operations for AI Shuffle hooks (PreToolUse, PostToolUse,
Stop, SessionStart, etc.).
"""

import logging
import secrets as _secrets
from typing import Any, Dict, Optional

from app.mcp.config_helpers import (
    VALID_HOOK_EVENTS,
    VALID_HOOK_TYPES,
    _tool_result,
    _error,
    _sanitize_hook,
    _hook_summary,
)

logger = logging.getLogger(__name__)


def _create_hook_tools(is_admin: bool, project_id: Optional[str]) -> list:
    """Create hook management tools."""
    from app.mcp.inproc import tool
    from app.db import database
    from app.db.async_database import run_sync

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

        def _load():
            """The scope branch, off-loop. Exactly one of these reads runs per
            call — as before — it just runs on a worker thread."""
            if scope == "project" and _project_id:
                return database.get_hooks_for_project(_project_id)
            elif scope == "app":
                return database.get_app_hooks()
            elif scope == "all" and _project_id:
                return database.get_all_hooks_for_project(_project_id)
            else:
                return database.get_app_hooks()

        hooks = await run_sync(_load)

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
        hook = await run_sync(database.get_hook, args["hook_id"])
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
        hook = await run_sync(
            database.create_hook,
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

        def _read_validate_write():
            """READ-MODIFY-WRITE — must stay in ONE hop.

            The existence read and the UPDATE that depends on it are one
            logical operation. Split across thread hops, two concurrent
            updateHook calls could interleave and the later write would clobber
            the earlier one (lost update), or write to a hook deleted between
            the check and the write.

            Returns ``(error_message, updated_row, kwargs)``.
            """
            existing = database.get_hook(hook_id)
            if not existing:
                return f"Hook not found: {hook_id}", None, None

            # Build update kwargs — only include provided fields
            kwargs: Dict[str, Any] = {}
            for field in ("name", "event", "hook_type", "command", "url",
                          "message_template", "tool_matcher", "enabled"):
                if field in args and args[field] is not None:
                    kwargs[field] = args[field]

            if "timeout" in args and args["timeout"] is not None:
                kwargs["timeout"] = min(args["timeout"], 300) * 1000  # seconds → ms

            if not kwargs:
                return "No fields provided to update.", None, None

            return None, database.update_hook(hook_id, **kwargs), kwargs

        error, updated, kwargs = await run_sync(_read_validate_write)
        if error:
            return _error(error)
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

        def _read_and_delete():
            """Existence read + DELETE in ONE hop — nothing runs between them,
            so the name reported as deleted is the row that was deleted."""
            hook = database.get_hook(args["hook_id"])
            if not hook:
                return None, None
            return hook, database.delete_hook(args["hook_id"])

        hook, deleted = await run_sync(_read_and_delete)
        if not hook:
            return _error(f"Hook not found: {args['hook_id']}")

        name = hook.get("name", args["hook_id"])
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

        def _read_and_toggle():
            """READ-MODIFY-WRITE — existence check plus the enabled flip in ONE
            hop, so a concurrent toggle cannot land between them and leave the
            hook in a state this call did not report."""
            hook = database.get_hook(args["hook_id"])
            if not hook:
                return None, None
            return hook, database.update_hook(
                args["hook_id"], enabled=args["enabled"]
            )

        hook, updated = await run_sync(_read_and_toggle)
        if not hook:
            return _error(f"Hook not found: {args['hook_id']}")

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

        hook = await run_sync(database.get_hook, args["hook_id"])
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

"""
Profile management tools for the app-config SDK MCP server.

Provides CRUD operations for AI Shuffle profiles (model, tools preset,
permission mode, system prompt, etc.).
"""

import logging
from typing import Any, Dict

from app.core.model_registry import DEFAULT_MODEL, model_values
from app.mcp.config_helpers import _tool_result, _error

logger = logging.getLogger(__name__)


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
                    "enum": model_values(),
                    "description": "AI model to use.",
                },
                "tools_preset": {
                    "type": "string",
                    "enum": ["claude_code"],
                    "description": "Tools preset. 'claude_code' gives access to all standard tools.",
                },
                "permission_mode": {
                    "type": "string",
                    "enum": ["default", "auto", "acceptEdits", "plan", "bypassPermissions"],
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
            "model": args.get("model", DEFAULT_MODEL),
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
                "model": {"type": "string", "enum": model_values()},
                "permission_mode": {"type": "string", "enum": ["default", "auto", "acceptEdits", "plan", "bypassPermissions"]},
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

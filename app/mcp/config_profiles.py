"""
Profile management tools for the app-config SDK MCP server.

Provides CRUD operations for AI Shuffle profiles (model, tools preset,
permission mode, system prompt, etc.).
"""

import logging
from typing import Any, Dict, Optional, cast

from app.mcp.config_helpers import _tool_result, _error

logger = logging.getLogger(__name__)


_TOOL_MODE_SCHEMA = {
    "type": "string",
    "enum": ["all", "none", "allow", "disallow"],
    "description": (
        "What the tool lists mean. 'all': every tool (both lists ignored). "
        "'none': no tools at all. 'allow': only the tools in allowed_tools. "
        "'disallow': every tool except those in disallowed_tools."
    ),
}

_ALLOW_EMPTY_ERROR = (
    "allowed_tools is empty while tool_selection_mode is 'allow', which leaves "
    "the agent with no tools at all. Pass tool_selection_mode 'all' to give it "
    "every tool, 'none' to remove every tool on purpose, or list the tools it "
    "should keep."
)


def _tool_mode_error(args: Dict[str, Any]) -> Optional[str]:
    """Why ``args["tool_selection_mode"]`` is invalid, or None when absent/valid."""
    from app.core.tool_access import TOOL_SELECTION_MODES

    mode = args.get("tool_selection_mode")
    if mode is None or mode in TOOL_SELECTION_MODES:
        return None
    return (
        f"Invalid tool_selection_mode {mode!r}; use one of: "
        + ", ".join(TOOL_SELECTION_MODES) + "."
    )


def _allow_list_empty(config: Dict[str, Any]) -> bool:
    """True when ``config`` resolves to "allow only selected" with nothing selected."""
    from app.core.tool_access import apply_legacy_tool_aliases, tool_selection_mode

    return tool_selection_mode(config) == "allow" and not apply_legacy_tool_aliases(
        config.get("allowed_tools") or []
    )


def _create_profile_tools(is_admin: bool) -> list:
    """Create profile management tools."""
    from app.mcp.inproc import tool
    from app.db import database
    from app.db.async_database import run_sync

    _is_admin = is_admin
    tools = []

    # -- listProfiles -----------------------------------------------------

    @tool(
        "listProfiles",
        "List all available profiles with their model, tools preset, and agent configuration.",
        {"type": "object", "properties": {}},
    )
    async def list_profiles(args: dict) -> dict:
        profiles = await run_sync(database.get_all_profiles)
        return _tool_result({
            "profiles": [
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "description": p.get("description"),
                    "is_builtin": p.get("is_builtin", False),
                    "tools_preset": p.get("config", {}).get("tools_preset"),
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
        profile = await run_sync(database.get_profile, args["profile_id"])
        if not profile:
            return _error(f"Profile not found: {args['profile_id']}")
        from app.core.builtin_subagents import DEFAULTS_RECORD_KEY  # internal seeding record
        config = {k: v for k, v in (profile.get("config") or {}).items() if k != DEFAULTS_RECORD_KEY}
        return _tool_result(cast(dict, {**profile, "config": config}))

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
                "tools_preset": {
                    "type": "string",
                    "enum": ["claude_code"],
                    "description": "Tools preset. 'claude_code' gives access to all standard tools.",
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
                "tool_selection_mode": {
                    **_TOOL_MODE_SCHEMA,
                    "description": _TOOL_MODE_SCHEMA["description"] + (
                        " Omit to infer it: a non-empty allowed_tools means "
                        "'allow', else a non-empty disallowed_tools means "
                        "'disallow', else 'all'."
                    ),
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tools the agent may use in 'allow' mode (e.g., ['Read', "
                        "'Grep', 'Glob']; mcp__<server>__* selects a whole MCP "
                        "server). Required and non-empty when "
                        "tool_selection_mode is 'allow'."
                    ),
                },
                "disallowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tools the agent may not use in 'disallow' mode (e.g., ['Bash', 'Write']).",
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
        mode_error = _tool_mode_error(args)
        if mode_error:
            return _error(mode_error)
        if args.get("tool_selection_mode") == "allow" and not args.get("allowed_tools"):
            return _error(_ALLOW_EMPTY_ERROR)

        import re
        # Generate ID from name
        profile_id = re.sub(r'[^a-z0-9-]', '-', args["name"].lower().strip())
        profile_id = re.sub(r'-+', '-', profile_id).strip('-')[:50]

        def _check_and_create():
            """Duplicate-ID check + INSERT in ONE hop.

            The check and the insert are one logical operation: split across
            thread hops, two concurrent createProfile calls for the same name
            could both see "no such profile" and the second would overwrite the
            first. Keeping them in one closure preserves the guarantee the
            duplicate-ID error is meant to give.
            """
            # Check for existing profile with this ID
            existing = database.get_profile(profile_id)
            if existing:
                return (
                    f"A profile with ID '{profile_id}' already exists. "
                    "Choose a different name."
                ), None

            # Build config. No model or permission mode: an agent is
            # configuration, and the person starting a chat picks the model,
            # effort and permission mode for that session.
            config: Dict[str, Any] = {
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
            if args.get("tool_selection_mode"):
                config["tool_selection_mode"] = args["tool_selection_mode"]
            if args.get("app_config_tools"):
                config["app_config_tools"] = True

            # System prompt
            if args.get("system_prompt_content"):
                config["system_prompt"] = {
                    "type": "custom",
                    "content": args["system_prompt_content"],
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

            return None, database.create_profile(
                profile_id=profile_id,
                name=args["name"],
                description=args.get("description"),
                config=config,
            )

        error, profile = await run_sync(_check_and_create)
        if error:
            return _error(error)
        if not profile:
            return _error(f"Failed to create profile: {profile_id}")

        return _tool_result({
            "success": True,
            "profile_id": profile_id,
            "name": args["name"],
            "message": (
                f"Profile '{args['name']}' created (ID: {profile_id}). "
                "Select it from the profile dropdown to use it. Its model, "
                "effort and permission mode are chosen per chat, not here."
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
                "thinking_mode": {"type": "string", "enum": ["default", "disabled", "adaptive", "think", "think_harder", "ultra_think"]},
                "tools_preset": {"type": "string", "enum": ["claude_code"]},
                "max_turns": {"type": "integer"},
                "app_config_tools": {"type": "boolean"},
                "system_prompt_content": {"type": "string", "description": "Custom system prompt content. Pass non-empty string to switch profile to custom system prompt; omit to leave unchanged."},
                "system_prompt_append": {"type": "string", "description": "Text to append to the preset system prompt. Pass non-empty string to switch profile to preset+append; omit to leave unchanged."},
                "tool_selection_mode": {
                    **_TOOL_MODE_SCHEMA,
                    "description": _TOOL_MODE_SCHEMA["description"] + " Omit to leave unchanged.",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tools the agent may use in 'allow' mode; replaces the "
                        "stored list. Omit to leave unchanged. Ignored in other "
                        "modes. An empty list is refused while the mode is "
                        "'allow' (it would remove every tool): to lift the "
                        "restriction pass tool_selection_mode 'all' instead."
                    ),
                },
                "disallowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tools the agent may not use in 'disallow' mode; replaces "
                        "the stored list. Pass [] to block nothing; omit to leave "
                        "unchanged. Ignored in other modes."
                    ),
                },
            },
            "required": ["profile_id"],
        },
    )
    async def update_profile(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")
        from app.db.owner_state import AGENT_ID as ASSISTANT_AGENT_ID
        if args.get("profile_id") == ASSISTANT_AGENT_ID:
            return _error("The assistant's built-in agent cannot be changed or deleted.")
        mode_error = _tool_mode_error(args)
        if mode_error:
            return _error(mode_error)

        def _read_merge_write():
            """READ-MODIFY-WRITE — must stay in ONE hop.

            This tool reads the stored ``config`` blob, merges the requested
            keys into a copy and writes the WHOLE blob back. Split across
            thread hops, two concurrent updateProfile calls would each merge
            onto the config they read before the other wrote, and whichever
            wrote last would silently erase the other's change — the classic
            lost update, and the most damaging one in this file because config
            is a single JSON column rather than per-field columns.

            Returns ``(error_message, updated_row, updated_fields)``.
            """
            profile = database.get_profile(args["profile_id"])
            if not profile:
                return f"Profile not found: {args['profile_id']}", None, None

            # Merge updates into existing config
            config = dict(profile.get("config", {}))
            updated_fields = []

            for field in ("thinking_mode", "tools_preset", "max_turns",
                          "app_config_tools"):
                if field in args and args[field] is not None:
                    config[field] = args[field]
                    updated_fields.append(field)

            from app.core.tool_access import tool_selection_mode

            mode_before = tool_selection_mode(config)
            for field in ("allowed_tools", "disallowed_tools"):
                if field in args:
                    config[field] = args[field]
                    updated_fields.append(field)
            if args.get("tool_selection_mode") is not None:
                config["tool_selection_mode"] = args["tool_selection_mode"]
                updated_fields.append("tool_selection_mode")

            # The mode, not list emptiness, decides access
            # (app.core.tool_access): an empty allow list in "allow" mode
            # resolves to no tools at all. Refuse a call that would land there
            # instead of silently stripping the agent. Only calls that touch
            # the tool settings are checked, so renaming an agent that the
            # owner deliberately left with nothing ticked still works.
            touches_tools = "allowed_tools" in args or "tool_selection_mode" in updated_fields
            if touches_tools and _allow_list_empty(config):
                return _ALLOW_EMPTY_ERROR, None, None
            # A profile saved without a mode infers one from its lists, and
            # there an empty allow list reads as "none". Don't let a "clear the
            # list" call flip such a profile to no tools by inference.
            if (
                "allowed_tools" in args
                and not args.get("allowed_tools")
                and args.get("tool_selection_mode") is None
                and mode_before != "none"
                and tool_selection_mode(config) == "none"
            ):
                return (
                    "allowed_tools: [] would leave this agent with no tools at "
                    "all: it has no saved tool_selection_mode, so an empty list "
                    "reads as 'none'. Pass tool_selection_mode 'all' to lift the "
                    "restriction, or 'none' to remove every tool on purpose."
                ), None, None

            # System prompt merge — mirrors createProfile semantics
            if args.get("system_prompt_content"):
                config["system_prompt"] = {
                    "type": "custom",
                    "content": args["system_prompt_content"],
                }
                updated_fields.append("system_prompt")
            elif args.get("system_prompt_append"):
                existing_sp = config.get("system_prompt") or {}
                config["system_prompt"] = {
                    "type": "preset",
                    "preset": existing_sp.get("preset", "claude_code"),
                    "append": args["system_prompt_append"],
                }
                updated_fields.append("system_prompt")
            # If neither key provided (or both empty string), leave system_prompt untouched.

            # Update profile-level fields
            name = args.get("name")
            description = args.get("description")
            if name:
                updated_fields.append("name")
            if description is not None:
                updated_fields.append("description")

            if not updated_fields:
                return "No fields provided to update.", None, None

            # Built-in profiles remain editable via MCP (mirrors REST behavior).
            # Use the restore endpoint / UI to revert built-in profiles to defaults.
            return None, database.update_profile(
                args["profile_id"],
                name=name,
                description=description,
                config=config,
                allow_builtin=True,
            ), updated_fields

        error, updated, updated_fields = await run_sync(_read_merge_write)
        if error:
            return _error(error)

        if not updated:
            return _error("Failed to update profile.")

        return _tool_result({
            "success": True,
            "profile_id": args["profile_id"],
            "updated_fields": updated_fields,
            "message": f"Profile '{updated.get('name')}' updated.",
        })

    tools.append(update_profile)

    # -- deleteProfile ----------------------------------------------------

    @tool(
        "deleteProfile",
        "Delete a profile permanently. Sessions using it are preserved but unlinked. Built-in profiles cannot be deleted.",
        {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string"},
                "confirm": {"type": "boolean", "description": "Must be true to confirm deletion."},
            },
            "required": ["profile_id", "confirm"],
        },
    )
    async def delete_profile(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")
        from app.db.owner_state import AGENT_ID as ASSISTANT_AGENT_ID
        if args.get("profile_id") == ASSISTANT_AGENT_ID:
            return _error("The assistant's built-in agent cannot be changed or deleted.")

        if not args.get("confirm"):
            return _error("Deletion requires confirm=true.")

        def _guard_and_delete():
            """Existence check → built-in guard → in-use guard → DELETE, in ONE
            hop. Keeping the guards and the delete in one closure is what makes
            "no automation still references this profile" true AT THE MOMENT OF
            DELETION rather than merely true a few thread hops earlier.

            Returns ``(error_message, profile_row, deleted)``.
            """
            profile = database.get_profile(args["profile_id"])
            if not profile:
                return f"Profile not found: {args['profile_id']}", None, None

            if profile.get("is_builtin"):
                return f"Cannot delete built-in profile: {args['profile_id']}", None, None

            blocking = database.get_automations_for_profile(args["profile_id"])
            if blocking:
                names = ", ".join(a["name"] for a in blocking)
                return (
                    f"Agent is used by {len(blocking)} automation(s): {names} — delete or reassign them first"
                ), None, None

            return None, profile, database.delete_profile(args["profile_id"])

        error, profile, deleted = await run_sync(_guard_and_delete)
        if error:
            return _error(error)

        if not deleted:
            return _error("Failed to delete profile.")

        return _tool_result({
            "success": True,
            "profile_id": args["profile_id"],
            "message": f"Profile '{profile.get('name')}' deleted.",
        })

    tools.append(delete_profile)

    return tools

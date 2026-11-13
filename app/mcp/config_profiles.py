"""
Profile management tools for the app-config SDK MCP server.

Provides CRUD operations for AI Shuffle profiles (model, tools preset,
permission mode, system prompt, etc.).
"""

import logging
from typing import Any, Dict, Optional, cast

from app.core.models import AGENT_AVATAR_COLORS
from app.mcp.config_agent_edits import (
    PROMPT_EDITS_SCHEMA,
    apply_list_ops,
    apply_map_ops,
    apply_prompt_edits,
    list_ops_for,
    list_ops_schema,
    pruned_provider_models,
    unknown_op_fields,
    validate_profile_fields,
)
from app.mcp.config_helpers import _tool_result, _error

logger = logging.getLogger(__name__)


_STRINGS = {"type": "array", "items": {"type": "string"}}

# Agent settings an agent may set on create/update. Deliberately absent: model,
# provider, permission_mode and effort (chosen per chat, v79); env, extra_args,
# cwd and setting_sources (machine-specific or secret-bearing); and the retired
# ai_tools / computer_use switches, which the runtime no longer reads for access
# (app.core.tool_access.fold_retired_tool_switches; the computer-use toggle is
# app-wide), so setting them would report success and change nothing.
_AGENT_SETTINGS_SCHEMA: Dict[str, Any] = {
    "enabled_agents": {**_STRINGS, "description": "Agent ids for 'custom' mode."},
    "agent_invoke_mode": {
        "type": "string",
        "enum": ["all", "custom", "none"],
        "description": "Subagents it may call: every agent, enabled_agents only, or none.",
    },
    "provider_models": {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": (
            "Its model when run as a subagent, by caller provider "
            "(__native__ Claude, __voice__ voice, else a provider id)."
        ),
    },
    "enabled_plugins": {**_STRINGS, "description": "Plugins to load."},
    "add_dirs": {**_STRINGS, "description": "Extra directories it may access."},
    "avatar_color": {"type": "string", "enum": list(AGENT_AVATAR_COLORS)},
}
_AGENT_SETTINGS = ("enabled_agents", "agent_invoke_mode", "provider_models",
                   "enabled_plugins", "add_dirs")
_PROFILE_LIST_FIELDS = ("allowed_tools", "disallowed_tools", "enabled_agents",
                        "enabled_plugins", "add_dirs")
_PROMPT_ONLY_ONE = (
    "Pass only one way to change the system prompt: system_prompt_content, "
    "system_prompt_append, reset_system_prompt or prompt_edits."
)


def _avatar_color_error(value: Any) -> Optional[str]:
    from app.core.models import _check_avatar_color

    try:
        _check_avatar_color(value)
    except ValueError as exc:
        return str(exc)
    return None


def _masked_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Hide env values (they may hold secrets); keys stay visible."""
    env = config.get("env")
    if isinstance(env, dict) and env:
        config = {**config, "env": {k: "••••••••" if v else v for k, v in env.items()}}
    return config


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
        return _tool_result(cast(dict, {**profile, "config": _masked_config(config)}))

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
                **_AGENT_SETTINGS_SCHEMA,
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
        # Judge the tool keys exactly as they will be stored (only truthy values
        # are kept), so blank-only names or an inferred mode can't save "no tools".
        stored_tools = {key: args[key] for key in ("tool_selection_mode", "allowed_tools", "disallowed_tools")
                        if args.get(key)}
        if _allow_list_empty(stored_tools):
            return _error(_ALLOW_EMPTY_ERROR)
        # The new agent settings pass the same ProfileConfig check as the
        # Agents page before anything is written.
        settings = {key: args[key] for key in _AGENT_SETTINGS
                    if args.get(key) is not None}
        if "provider_models" in settings:
            settings["provider_models"] = pruned_provider_models(settings["provider_models"])
        settings_error, normalized = validate_profile_fields(settings, settings)
        if settings_error:
            return _error(settings_error)
        settings.update(normalized)
        color_error = _avatar_color_error(args.get("avatar_color"))
        if color_error:
            return _error(color_error)

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
            config.update(settings)

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

            extra = {"avatar_color": args["avatar_color"].strip()} if args.get("avatar_color") else {}
            return None, database.create_profile(
                profile_id=profile_id,
                name=args["name"],
                description=args.get("description"),
                config=config,
                **extra,
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
        "Update a profile. Omitted fields stay unchanged; null clears an agent setting.",
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
                "system_prompt_content": {"type": "string", "description": "Replace the whole prompt (custom)."},
                "system_prompt_append": {"type": "string", "description": "Switch to the preset prompt plus this text."},
                "reset_system_prompt": {"type": "boolean", "description": "Restore the bare preset prompt."},
                "prompt_edits": {
                    **PROMPT_EDITS_SCHEMA,
                    "description": PROMPT_EDITS_SCHEMA["description"].replace(
                        "the prompt", "the prompt (custom text, or text appended to the preset)", 1),
                },
                "tool_selection_mode": _TOOL_MODE_SCHEMA,
                "allowed_tools": {
                    **_STRINGS,
                    "description": (
                        "Tools allowed in 'allow' mode; replaces the list. An "
                        "empty list is refused in 'allow' mode (no tools left): "
                        "pass tool_selection_mode 'all' instead."
                    ),
                },
                "disallowed_tools": {
                    **_STRINGS,
                    "description": "Tools blocked in 'disallow' mode; replaces the list ([] blocks nothing).",
                },
                **_AGENT_SETTINGS_SCHEMA,
                **list_ops_schema(_PROFILE_LIST_FIELDS),
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
        mode_error = _tool_mode_error(args) or unknown_op_fields(
            args, (*_PROFILE_LIST_FIELDS, "provider_models"))
        if mode_error:
            return _error(mode_error)
        prompt_ways = [key for key in ("system_prompt_content", "system_prompt_append",
                                       "reset_system_prompt") if args.get(key)]
        if args.get("prompt_edits") is not None:
            prompt_ways.append("prompt_edits")
        if len(prompt_ways) > 1 and {"reset_system_prompt", "prompt_edits"} & set(prompt_ways):
            return _error(_PROMPT_ONLY_ONE)
        if "avatar_color" in args:
            color_error = _avatar_color_error(args["avatar_color"])
            if color_error:
                return _error(color_error)

        def _read_merge_write():
            """READ-MODIFY-WRITE — must stay in ONE hop.

            This tool reads the stored ``config`` blob, merges the requested
            keys into a copy and writes the WHOLE blob back. Split across
            thread hops, two concurrent updateProfile calls would each merge
            onto the config they read before the other wrote, and whichever
            wrote last would silently erase the other's change — the classic
            lost update, and the most damaging one in this file because config
            is a single JSON column rather than per-field columns. Prompt
            patches and list add/remove read the stored value, so they must
            run here too.

            Returns ``(error_message, updated_row, updated_fields, details)``.
            """
            profile = database.get_profile(args["profile_id"])
            if not profile:
                return f"Profile not found: {args['profile_id']}", None, None, None

            # Merge updates into existing config
            config = dict(profile.get("config", {}))
            updated_fields = []
            details: Dict[str, Any] = {}

            def mark(field: str) -> None:
                if field not in updated_fields:
                    updated_fields.append(field)

            for field in ("thinking_mode", "tools_preset", "max_turns",
                          "app_config_tools"):
                if field in args and args[field] is not None:
                    config[field] = args[field]
                    mark(field)

            from app.core.tool_access import tool_selection_mode

            mode_before = tool_selection_mode(config)
            for field in ("allowed_tools", "disallowed_tools"):
                if field in args:
                    config[field] = args[field]
                    mark(field)
            if args.get("tool_selection_mode") is not None:
                config["tool_selection_mode"] = args["tool_selection_mode"]
                mark("tool_selection_mode")

            # Agent settings: null clears.
            for field in _AGENT_SETTINGS:
                if field not in args:
                    continue
                value = args[field]
                if value is None:
                    config.pop(field, None)
                else:
                    config[field] = (
                        pruned_provider_models(value) if field == "provider_models" else value
                    )
                mark(field)

            # add / remove: single entries without resending the list.
            unmatched: Dict[str, Any] = {}
            for field in (*_PROFILE_LIST_FIELDS, "provider_models"):
                add, remove = list_ops_for(args, field)
                if add is None and remove is None:
                    continue
                apply = apply_map_ops if field == "provider_models" else apply_list_ops
                value, op_error, missing = apply(config.get(field), add, remove, field)
                if op_error:
                    return op_error, None, None, None
                config[field] = value
                details.setdefault("lists", {})[field] = value
                if missing:
                    unmatched[field] = missing
                mark(field)
            if unmatched:
                details["not_found_for_remove"] = unmatched

            # The mode, not list emptiness, decides access
            # (app.core.tool_access): an empty allow list in "allow" mode
            # resolves to no tools at all. Refuse a call that would land there
            # instead of silently stripping the agent. Only calls that touch
            # the tool settings are checked, so renaming an agent that the
            # owner deliberately left with nothing ticked still works.
            touches_allowed = "allowed_tools" in updated_fields
            touches_tools = touches_allowed or "tool_selection_mode" in updated_fields
            if touches_tools and _allow_list_empty(config):
                return _ALLOW_EMPTY_ERROR, None, None, None
            # A profile saved without a mode infers one from its lists, and
            # there an empty allow list reads as "none". Don't let a "clear the
            # list" call (or removing its last entry) flip such a profile to no
            # tools by inference.
            if (
                touches_allowed
                and not config.get("allowed_tools")
                and args.get("tool_selection_mode") is None
                and mode_before != "none"
                and tool_selection_mode(config) == "none"
            ):
                return (
                    "An empty allowed_tools would leave this agent with no tools "
                    "at all: it has no saved tool_selection_mode, so an empty list "
                    "reads as 'none'. Pass tool_selection_mode 'all' to lift the "
                    "restriction, or 'none' to remove every tool on purpose."
                ), None, None, None

            # System prompt merge — mirrors createProfile semantics
            existing_sp = config.get("system_prompt")
            if isinstance(existing_sp, str):  # legacy plain-text prompt
                existing_sp = {"type": "custom", "content": existing_sp}
            elif not isinstance(existing_sp, dict):
                # No prompt runs on the security text alone (query_options),
                # i.e. an empty custom prompt; a dict without a type is a preset.
                existing_sp = {"type": "custom", "content": ""}
            if args.get("system_prompt_content"):
                config["system_prompt"] = {
                    "type": "custom",
                    "content": args["system_prompt_content"],
                }
                mark("system_prompt")
            elif args.get("system_prompt_append"):
                config["system_prompt"] = {
                    "type": "preset",
                    "preset": existing_sp.get("preset", "claude_code"),
                    "append": args["system_prompt_append"],
                }
                mark("system_prompt")
            elif args.get("reset_system_prompt"):
                config["system_prompt"] = {
                    "type": "preset",
                    "preset": existing_sp.get("preset") or "claude_code",
                }
                mark("system_prompt")
            elif args.get("prompt_edits") is not None:
                # A custom prompt patches its content; a preset prompt patches
                # the agent's own text appended to the preset.
                custom = existing_sp.get("type") == "custom"
                key = "content" if custom else "append"
                text, edit_error, excerpts = apply_prompt_edits(
                    existing_sp.get(key) or "", args["prompt_edits"])
                if edit_error:
                    return edit_error, None, None, None
                patched = dict(existing_sp) if custom else {
                    "preset": "claude_code", **existing_sp, "type": "preset"}
                patched[key] = text
                config["system_prompt"] = patched
                details["prompt"] = {
                    "edited": "custom prompt" if custom else "text appended to the preset",
                    "length": len(text),
                    "changes": excerpts,
                }
                mark("system_prompt")
            # If neither key provided (or both empty string), leave system_prompt untouched.

            # Same ProfileConfig check as the Agents page, on the touched keys.
            settings_error, _ = validate_profile_fields(config, updated_fields)
            if settings_error:
                return settings_error, None, None, None

            # Update profile-level fields
            name = args.get("name")
            description = args.get("description")
            if name:
                mark("name")
            if description is not None:
                mark("description")
            extra = {}
            if "avatar_color" in args:
                color = args["avatar_color"]
                extra["avatar_color"] = color.strip() if isinstance(color, str) else None
                mark("avatar_color")

            if not updated_fields:
                return "No fields provided to update.", None, None, None

            # Built-in profiles remain editable via MCP (mirrors REST behavior).
            # Use the restore endpoint / UI to revert built-in profiles to defaults.
            return None, database.update_profile(
                args["profile_id"],
                name=name,
                description=description,
                config=config,
                allow_builtin=True,
                **extra,
            ), updated_fields, details

        error, updated, updated_fields, details = await run_sync(_read_merge_write)
        if error:
            return _error(error)

        if not updated:
            return _error("Failed to update profile.")

        return _tool_result({
            "success": True,
            "profile_id": args["profile_id"],
            "updated_fields": updated_fields,
            **details,
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

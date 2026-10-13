"""
Subagent management tools for the app-config SDK MCP server.

Provides CRUD operations for AI Shuffle subagents (model, tools, skills,
memory, mcp_servers, system prompt, etc.).
"""

import logging
from typing import Any, cast

from app.core.model_registry import model_values
from app.mcp.config_helpers import _tool_result, _error

logger = logging.getLogger(__name__)


def _create_subagent_tools(is_admin: bool) -> list:
    """Create subagent management tools."""
    from app.mcp.inproc import tool
    from app.db import database
    from app.db.async_database import run_sync

    _is_admin = is_admin
    tools = []

    # -- listSubagents --------------------------------------------------------

    @tool(
        "listSubagents",
        "List all available subagents with their provider-scoped Task assignments, "
        "legacy model, memory scope, and status. provider_models is keyed by the "
        "caller's provider (__native__ for Claude, __voice__ for voice); model is "
        "the legacy fallback.",
        {"type": "object", "properties": {}},
    )
    async def list_subagents(args: dict) -> dict:
        subagents = await run_sync(database.get_all_subagents)
        return _tool_result({
            "subagents": [
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "description": s.get("description"),
                    "is_builtin": s.get("is_builtin", False),
                    "model": s.get("model"),
                    "provider_models": s.get("provider_models") or {},
                    "memory": s.get("memory"),
                }
                for s in subagents
            ],
            "count": len(subagents),
        })

    tools.append(list_subagents)

    # -- getSubagent ----------------------------------------------------------

    @tool(
        "getSubagent",
        "Get full configuration of a specific subagent. provider_models holds "
        "Task assignments keyed by the caller's provider (__native__ for Claude, "
        "__voice__ for voice); model is the legacy fallback.",
        {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string"},
            },
            "required": ["subagent_id"],
        },
    )
    async def get_subagent(args: dict) -> dict:
        subagent = await run_sync(database.get_subagent, args["subagent_id"])
        if not subagent:
            return _error(f"Subagent not found: {args['subagent_id']}")
        return _tool_result(cast(dict, subagent))

    tools.append(get_subagent)

    # -- createSubagent -------------------------------------------------------

    @tool(
        "createSubagent",
        "Create a new subagent with specific model, tools, skills, and behavior settings.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Subagent name (1-100 chars)."},
                "description": {"type": "string", "description": "When to use this subagent."},
                "prompt": {"type": "string", "description": "System prompt for the subagent."},
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of allowed tools. Omit to inherit all tools from profile.",
                },
                "model": {
                    "type": "string",
                    "enum": model_values(),
                    "description": "AI model override. Omit to inherit from profile.",
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of skill names the subagent can use.",
                },
                "memory": {
                    "type": "string",
                    "enum": ["user", "project", "local"],
                    "description": "Memory scope for the subagent.",
                },
                "mcp_servers": {
                    "type": "array",
                    "description": "MCP servers the subagent can access.",
                },
            },
            "required": ["name", "description", "prompt"],
        },
    )
    async def create_subagent(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        import re
        # Generate ID from name using same logic as createProfile
        name = args["name"]
        subagent_id = re.sub(r'[^a-z0-9-]', '-', name.lower().strip())
        subagent_id = re.sub(r'-+', '-', subagent_id).strip('-')[:50]

        def _check_and_create():
            """Duplicate-ID check + INSERT in ONE hop.

            Split across thread hops, two concurrent createSubagent calls for
            the same name could both see "no such subagent" and the second
            would overwrite the first — the duplicate-ID error exists precisely
            to prevent that, so the check and the insert stay together.
            """
            # Check for existing subagent with this ID
            existing = database.get_subagent(subagent_id)
            if existing:
                return (
                    f"A subagent with ID '{subagent_id}' already exists. "
                    "Choose a different name."
                ), None

            return None, database.create_subagent(
                subagent_id=subagent_id,
                name=name,
                description=args["description"],
                prompt=args["prompt"],
                tools=args.get("tools"),
                model=args.get("model"),
                skills=args.get("skills"),
                memory=args.get("memory"),
                mcp_servers=args.get("mcp_servers"),
            )

        error, subagent = await run_sync(_check_and_create)
        if error:
            return _error(error)
        if not subagent:
            return _error(f"Failed to create subagent: {subagent_id}")

        return _tool_result({
            "success": True,
            "subagent_id": subagent_id,
            "name": name,
            "message": f"Subagent '{name}' created (ID: {subagent_id}).",
        })

    tools.append(create_subagent)

    # -- updateSubagent -------------------------------------------------------

    @tool(
        "updateSubagent",
        "Update configuration of an existing subagent.",
        {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "prompt": {"type": "string"},
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of allowed tools. Pass null to clear (inherit all).",
                },
                "model": {
                    "type": "string",
                    "enum": model_values(),
                    "description": "AI model override. Pass null to inherit from profile.",
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "memory": {
                    "type": "string",
                    "enum": ["user", "project", "local"],
                },
                "mcp_servers": {
                    "type": "array",
                    "description": "MCP servers the subagent can access.",
                },
            },
            "required": ["subagent_id"],
        },
    )
    async def update_subagent(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")
        from app.db.assistant import AGENT_ID as ASSISTANT_AGENT_ID
        if args.get("subagent_id") == ASSISTANT_AGENT_ID:
            return _error("The assistant's built-in agent cannot be changed or deleted.")

        def _read_validate_write():
            """READ-MODIFY-WRITE — must stay in ONE hop.

            The existence read, the field selection and the UPDATE are one
            logical operation, and the response name falls back to the row read
            here. Split across thread hops a concurrent updateSubagent could
            land between them, so the tool would report a name that never
            coexisted with the fields it just wrote — or write to a subagent
            deleted after the check.

            Returns ``(error_message, updated_row, existing_row, updated_fields)``.
            """
            existing = database.get_subagent(args["subagent_id"])
            if not existing:
                return f"Subagent not found: {args['subagent_id']}", None, None, None

            # Built-in subagents remain editable via MCP (mirrors REST behavior).
            # Use revertSubagent / built-in revert flow to undo unwanted changes.

            updatable_fields = (
                "name", "description", "prompt", "tools",
                "model", "skills", "memory", "mcp_servers",
            )
            fields = [k for k in updatable_fields if k in args]

            if not fields:
                return "No fields provided to update.", None, None, None

            # Build kwargs only for keys explicitly supplied. Omitted keys fall
            # through to the DB layer's Ellipsis sentinel default, which means
            # "leave unchanged". Explicit None clears the field.
            update_kwargs: dict[str, Any] = {
                k: args[k] for k in updatable_fields if k in args
            }

            return None, database.update_subagent(
                args["subagent_id"],
                **update_kwargs,
            ), existing, fields

        error, updated, existing, updated_fields = await run_sync(_read_validate_write)
        if error:
            return _error(error)

        updated_name = (updated or existing).get("name")
        return _tool_result({
            "success": True,
            "subagent_id": args["subagent_id"],
            "updated_fields": updated_fields,
            "message": f"Subagent '{updated_name}' updated.",
        })

    tools.append(update_subagent)

    # -- deleteSubagent -------------------------------------------------------

    @tool(
        "deleteSubagent",
        "Delete a subagent permanently. Requires explicit confirmation.",
        {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to confirm deletion.",
                },
            },
            "required": ["subagent_id", "confirm"],
        },
    )
    async def delete_subagent(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")
        from app.db.assistant import AGENT_ID as ASSISTANT_AGENT_ID
        if args.get("subagent_id") == ASSISTANT_AGENT_ID:
            return _error("The assistant's built-in agent cannot be changed or deleted.")

        if not args.get("confirm"):
            return _error("Deletion requires confirm=true.")

        def _guard_and_delete():
            """Existence check → built-in guard → in-use guard → DELETE, in ONE
            hop, so "no automation still references this subagent" is true at
            the moment of deletion rather than a few thread hops earlier.

            Returns ``(error_message, existing_row, deleted)``.
            """
            existing = database.get_subagent(args["subagent_id"])
            if not existing:
                return f"Subagent not found: {args['subagent_id']}", None, None

            if existing.get("is_builtin"):
                return (
                    f"Cannot delete built-in subagent: {args['subagent_id']}."
                ), None, None

            blocking = database.get_automations_for_profile(args["subagent_id"])
            if blocking:
                names = ", ".join(a["name"] for a in blocking)
                return (
                    f"Agent is used by {len(blocking)} automation(s): {names} — delete or reassign them first"
                ), None, None

            return None, existing, database.delete_subagent(args["subagent_id"])

        error, existing, deleted = await run_sync(_guard_and_delete)
        if error:
            return _error(error)

        if not deleted:
            return _error("Failed to delete subagent.")

        return _tool_result({
            "success": True,
            "subagent_id": args["subagent_id"],
            "message": f"Subagent '{existing.get('name')}' deleted.",
        })

    tools.append(delete_subagent)

    return tools

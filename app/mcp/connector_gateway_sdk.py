"""Connector action gateway — the generic ``{type}_actions`` / ``{type}_invoke``
MCP tool pair.

Instead of one MCP tool per platform capability, each gateway-enabled connector
(``SUPPORTS_ACTION_GATEWAY = True``) exposes exactly two tools to a linked AI
session:

    {type}_actions  — discover the action catalog (ids, schemas, availability,
                      filtered by the install's enabled categories and the
                      bot's live platform permissions)
    {type}_invoke   — execute one action: {action, params}, validated against
                      the catalog schema server-side before dispatch

Tools are built per AI session and registered under the shared ``builtins``
SDK server ONLY when that session is linked to a connector of the matching
type (same seam the legacy Discord-native tools used), so nothing leaks to
unrelated sessions. Connector/channel resolution is re-done on every call —
no live handles are captured in the closures.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from app.mcp.config_helpers import _json_default

logger = logging.getLogger(__name__)


def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps({"success": True, **payload}, default=_json_default)}]}


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": message, **extra}, default=_json_default)}]}


# ---------------------------------------------------------------------------
# Call-time resolution: session_id + connector_type -> running instance + ctx
# ---------------------------------------------------------------------------

async def _resolve_gateway(
    session_id: Optional[str], connector_type: str, allow_global: bool = False
) -> Tuple[Optional[Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Resolve ``(instance, context, error_envelope)`` for a gateway call.

    Re-done on every call so connector restarts and channel rebinds are always
    respected. ``context`` carries the session scoping handed to the
    connector's availability/execute hooks.

    ``allow_global`` is set for non-messaging connectors (Home Assistant,
    Gmail, ...): when the session has no link to a connector of this type, the
    first running instance of the type is used with no bound channel — the
    same reach their legacy globally-registered tools had.
    """
    if not session_id:
        return None, None, _err("No session context — connector tools require an active session.")

    from app.db import database as _db

    try:
        sessions = await asyncio.to_thread(
            _db.get_connector_sessions_for_ai_session, session_id
        )
    except Exception as e:
        return None, None, _err(f"Failed to resolve connector session: {e}")

    linked_cs: Optional[Dict[str, Any]] = None
    connector_id: Optional[str] = None
    for cs in sessions or []:
        cid = cs.get("connector_id")
        if not cid:
            continue
        try:
            row = await asyncio.to_thread(_db.get_connector, cid)
        except Exception:
            row = None
        if row and row.get("type") == connector_type:
            linked_cs = cs
            connector_id = cid
            break

    from app.core.connectors.manager import connector_manager

    if not linked_cs or not connector_id:
        if not allow_global:
            return None, None, _err(
                f"This session is not linked to a {connector_type} connector."
            )
        running = list(connector_manager.get_running_connectors(connector_type))
        if not running:
            return None, None, _err(
                f"No {connector_type} connector is running on this device."
            )
        connector_id, instance = running[0]
        context = {
            "session_id": session_id,
            "connector_id": connector_id,
            "bound_chat_id": "",
        }
        return instance, context, None

    instance = connector_manager.get_running_instance(connector_id)
    if instance is None:
        return None, None, _err(
            f"The {connector_type} connector is not running on this device."
        )

    context = {
        "session_id": session_id,
        "connector_id": connector_id,
        "bound_chat_id": str(linked_cs.get("platform_chat_id") or ""),
    }
    return instance, context, None


def _config_flag(config: Optional[Dict[str, Any]], key: str) -> bool:
    """Truthy config flag that treats 'false'/'0'/'no' strings as False."""
    value = (config or {}).get(key)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _action_payload(action: Any) -> Dict[str, Any]:
    data = asdict(action)
    return {
        "id": data["id"],
        "name": data["name"],
        "description": data["description"],
        "category": data.get("category", "general"),
        "risk": data.get("risk", "safe"),
        "required_permission": data.get("required_permission"),
        "input_schema": data.get("input_schema") or {"type": "object", "properties": {}},
    }


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------

def build_gateway_tools(session_id: Optional[str]) -> List[Any]:
    """Build the gateway ``@tool`` pairs for every gateway-enabled connector
    type linked to ``session_id``. Returns [] when none apply."""
    try:
        from claude_agent_sdk import tool
    except ImportError:  # pragma: no cover - SDK always present in app runtime
        logger.debug("claude_agent_sdk not available — gateway tools disabled")
        return []

    if not session_id:
        return []

    try:
        from mcp.types import ToolAnnotations
        annotations = ToolAnnotations(maxResultSizeChars=200_000)
    except Exception:  # pragma: no cover
        annotations = None

    # Which gateway-enabled connector types is this session linked to?
    linked_types: List[str] = []
    try:
        from app.db import database as _db
        from app.core.connectors.registry import get_connector_class

        for cs in _db.get_connector_sessions_for_ai_session(session_id) or []:
            cid = cs.get("connector_id")
            row = _db.get_connector(cid) if cid else None
            ctype = (row or {}).get("type")
            if not ctype or ctype in linked_types:
                continue
            try:
                cls = get_connector_class(ctype)
            except ValueError:
                continue
            if getattr(cls, "SUPPORTS_ACTION_GATEWAY", False) and cls.ACTIONS:
                linked_types.append(ctype)
    except Exception:
        logger.debug("Gateway tool linkage lookup failed", exc_info=True)
        return []

    # Non-messaging gateway connectors (Home Assistant, Gmail, ...) have no
    # session/channel binding, so their pairs register for every session while
    # an instance is running — the same reach their legacy tools had.
    global_types: List[str] = []
    try:
        from app.core.connectors.manager import connector_manager

        for _cid, instance in connector_manager.get_running_connectors():
            ctype = instance.connector_type
            if ctype in linked_types or ctype in global_types:
                continue
            if (
                getattr(instance, "SUPPORTS_ACTION_GATEWAY", False)
                and instance.ACTIONS
                and not instance.CAPABILITIES.is_messaging
            ):
                global_types.append(ctype)
    except Exception:
        logger.debug("Gateway global connector lookup failed", exc_info=True)

    tools: List[Any] = []
    for connector_type in linked_types:
        tools.extend(_build_pair_for_type(tool, annotations, session_id, connector_type))
    for connector_type in global_types:
        tools.extend(_build_pair_for_type(
            tool, annotations, session_id, connector_type, allow_global=True,
        ))

    if tools:
        logger.info(
            "Registered %d gateway tool(s) for session %s (%s)",
            len(tools), session_id, ", ".join(linked_types + global_types),
        )
    return tools


def _build_pair_for_type(
    tool: Any, annotations: Any, session_id: str, connector_type: str,
    allow_global: bool = False,
) -> List[Any]:
    """Build the {type}_actions / {type}_invoke pair for one connector type."""

    actions_tool_name = f"{connector_type}_actions"
    invoke_tool_name = f"{connector_type}_invoke"

    tool_kwargs = {"annotations": annotations} if annotations is not None else {}

    @tool(
        actions_tool_name,
        f"Discover every {connector_type} action available to this session: ids, "
        "input schemas, categories, and risk tiers, filtered by this install's "
        "enabled categories and the bot's live platform permissions. Call this "
        f"before {invoke_tool_name}.",
        {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional: only return actions in this category.",
                },
                "include_unavailable": {
                    "type": "boolean",
                    "description": "Include full definitions of unavailable actions too.",
                },
            },
            "required": [],
        },
        **tool_kwargs,
    )
    async def gateway_actions(args: dict) -> dict:
        instance, context, err = await _resolve_gateway(session_id, connector_type, allow_global)
        if err:
            return err
        try:
            availability = await instance.get_action_availability(context)
        except Exception as e:
            logger.exception("get_action_availability failed for %s", connector_type)
            return _err(f"Failed to compute action availability: {e}")

        category = str(args.get("category") or "").strip().lower()
        include_unavailable = bool(args.get("include_unavailable"))

        available_out: List[Dict[str, Any]] = []
        unavailable_out: List[Dict[str, Any]] = []
        for action in instance.ACTIONS:
            if category and action.category.lower() != category:
                continue
            ok, reason = availability.get(action.id, (True, None))
            if ok:
                available_out.append(_action_payload(action))
            elif include_unavailable:
                unavailable_out.append({**_action_payload(action), "unavailable_reason": reason})
            else:
                unavailable_out.append({"id": action.id, "unavailable_reason": reason})

        categories = sorted({a.category for a in instance.ACTIONS})
        return _ok({
            "connector_type": connector_type,
            "actions": available_out,
            "unavailable": unavailable_out,
            "categories": categories,
            "note": (
                f"Invoke with {invoke_tool_name}({{action, params}}). Actions with "
                "risk 'destructive' require in-channel user approval before running."
            ),
        })

    @tool(
        invoke_tool_name,
        f"Execute one {connector_type} action by id with parameters matching its "
        f"input schema (discover both via {actions_tool_name}). Destructive "
        "actions prompt the user for Approve/Deny in-channel first.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "Action id from the catalog."},
                "params": {"type": "object", "description": "Parameters per the action's input_schema."},
            },
            "required": ["action"],
        },
        **tool_kwargs,
    )
    async def gateway_invoke(args: dict) -> dict:
        from app.core.connectors.base import validate_action_params

        action_id = str(args.get("action") or "").strip()
        params = args.get("params") or {}
        if not action_id:
            return _err("action is required.")
        if not isinstance(params, dict):
            return _err("params must be an object.")

        instance, context, err = await _resolve_gateway(session_id, connector_type, allow_global)
        if err:
            return err

        action_def = next((a for a in instance.ACTIONS if a.id == action_id), None)
        if action_def is None:
            return _err(
                f"Unknown action {action_id!r}. Call {actions_tool_name} for the catalog."
            )

        try:
            availability = await instance.get_action_availability(context)
        except Exception as e:
            logger.exception("get_action_availability failed for %s", connector_type)
            return _err(f"Failed to compute action availability: {e}")
        ok, reason = availability.get(action_id, (True, None))
        if not ok:
            return _err(f"Action {action_id!r} is not available: {reason}")

        validation_error = validate_action_params(action_def, params)
        if validation_error:
            return _err(f"Invalid params for {action_id!r}: {validation_error}")

        if action_def.risk == "destructive" and not _config_flag(
            instance.config, "auto_approve_destructive"
        ):
            approved, approval_reason = await instance.request_action_approval(
                context.get("bound_chat_id") or "", action_def, params
            )
            if not approved:
                return _err(f"Destructive action not approved: {approval_reason}")

        try:
            result = await instance.execute_action(action_id, params, context=context)
        except NotImplementedError:
            return _err(f"Action {action_id!r} is not implemented by {connector_type}.")
        except Exception as e:
            logger.exception("Gateway invoke %s/%s failed", connector_type, action_id)
            return _err(f"Action {action_id!r} failed: {e}")

        if isinstance(result, dict) and result.get("success") is False:
            return _err(str(result.get("error") or "Action failed."), action=action_id)
        payload = result if isinstance(result, dict) else {"result": result}
        payload.pop("success", None)
        return _ok({"action": action_id, **payload})

    return [gateway_actions, gateway_invoke]


# ---------------------------------------------------------------------------
# Allowed-tools names
# ---------------------------------------------------------------------------

def get_gateway_tool_names() -> List[str]:
    """MCP tool names for every gateway-enabled connector type.

    Allow-listed unconditionally (like the legacy Discord-native tools):
    per-session REGISTRATION in ``create_builtin_tools_server`` is what
    actually gates availability.
    """
    names: List[str] = []
    try:
        from app.core.connectors.registry import get_available_types, get_connector_class

        for ctype in get_available_types():
            try:
                cls = get_connector_class(ctype)
            except ValueError:
                continue
            if getattr(cls, "SUPPORTS_ACTION_GATEWAY", False) and cls.ACTIONS:
                names.append(f"mcp__builtins__{ctype}_actions")
                names.append(f"mcp__builtins__{ctype}_invoke")
    except Exception:
        logger.debug("Could not enumerate gateway tool names", exc_info=True)
    return names

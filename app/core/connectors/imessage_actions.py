"""iMessage action catalog + handlers for the connector action gateway.

Mirrors ``slack_actions.py``: every capability the ``imsg`` CLI's JSON-RPC
interface exposes is an ``ActionDef`` with a matching handler, reached through
the ``imessage_actions`` / ``imessage_invoke`` MCP tool pair.

The imsg RPC has no delete/ban-equivalent operation, so the catalog carries no
``risk == "destructive"`` actions and the connector does not implement an
action-approval flow (the base class default — deny — is fine since nothing
here ever needs it).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.connectors.base import ActionDef, OutgoingMessage

logger = logging.getLogger(__name__)

_VALID_REACTIONS = {"love", "like", "dislike", "laugh", "emphasize", "question"}


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_CHAT_PROP = {
    "platform_chat_id": {
        "type": "string",
        "description": "Optional chat id (handle or chat identifier) to target instead of the bound chat.",
    },
}

IMESSAGE_ACTIONS: List[ActionDef] = [
    ActionDef(
        id="send_message",
        name="Send Message",
        description="Send a text message to a chat.",
        input_schema=_schema({"text": {"type": "string"}, **_CHAT_PROP}, ["text"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="send_tapback",
        name="Send Tapback",
        description="React to a message with a tapback (love, like, dislike, laugh, emphasize, question).",
        input_schema=_schema({
            "message_guid": {"type": "string"},
            "reaction": {"type": "string", "enum": sorted(_VALID_REACTIONS)},
        }, ["message_guid", "reaction"]),
        category="reactions", risk="write",
    ),
    ActionDef(
        id="list_chats",
        name="List Chats",
        description="List recent chats known to the imsg client.",
        input_schema=_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
        category="chats", risk="safe",
    ),
    ActionDef(
        id="get_chat",
        name="Get Chat Info",
        description="Get details for one chat.",
        input_schema=_schema({**_CHAT_PROP}),
        category="chats", risk="safe",
    ),
    ActionDef(
        id="send_typing",
        name="Send Typing Indicator",
        description="Show a typing indicator in the chat, if the imsg CLI supports it.",
        input_schema=_schema({**_CHAT_PROP}),
        category="presence", risk="safe",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in IMESSAGE_ACTIONS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


def _target_chat(ctx: Dict[str, Any], params: Dict[str, Any]) -> str:
    return str(params.get("platform_chat_id") or ctx.get("bound_chat_id") or "").strip()


Handler = Callable[[Any, Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
_HANDLERS: Dict[str, Handler] = {}


def _handler(action_id: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[action_id] = fn
        return fn
    return deco


@_handler("send_message")
async def _send_message(conn, params, ctx):
    text = str(params.get("text") or "")
    if not text.strip():
        return _err("text is required.")
    target = _target_chat(ctx, params)
    if not target:
        return _err("No target chat — pass 'platform_chat_id' or use a bound session.")
    success = await conn.send_message(OutgoingMessage(platform_chat_id=target, text=text))
    if not success:
        return _err("Failed to send message.")
    return _ok(platform_chat_id=target)


@_handler("send_tapback")
async def _send_tapback(conn, params, ctx):
    message_guid = str(params.get("message_guid") or "")
    reaction = str(params.get("reaction") or "").strip().lower()
    if not message_guid:
        return _err("message_guid is required.")
    if reaction not in _VALID_REACTIONS:
        return _err(f"reaction must be one of: {', '.join(sorted(_VALID_REACTIONS))}")
    if conn._rpc is None:
        return _err("iMessage RPC is not running.")
    await conn._rpc.request("react", {"message_guid": message_guid, "reaction": reaction})
    return _ok(message_guid=message_guid, reaction=reaction)


@_handler("list_chats")
async def _list_chats(conn, params, ctx):
    if conn._rpc is None:
        return _err("iMessage RPC is not running.")
    try:
        limit = max(1, min(100, int(params.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    result = await conn._rpc.request("chats.list", {"limit": limit})
    if isinstance(result, list):
        chats = result
    elif isinstance(result, dict):
        chats = result.get("chats", [])
    else:
        chats = []
    return _ok(chats=chats, count=len(chats))


@_handler("get_chat")
async def _get_chat(conn, params, ctx):
    target = _target_chat(ctx, params)
    if not target:
        return _err("No target chat — pass 'platform_chat_id' or use a bound session.")
    if conn._rpc is None:
        return _err("iMessage RPC is not running.")
    result = await conn._rpc.request("chats.get", {"chat_id": target})
    return _ok(chat=result)


@_handler("send_typing")
async def _send_typing(conn, params, ctx):
    target = _target_chat(ctx, params)
    if not target:
        return _err("No target chat — pass 'platform_chat_id' or use a bound session.")
    if conn._rpc is None:
        return _err("iMessage RPC is not running.")
    try:
        await conn._rpc.request("typing", {"to": target})
    except Exception as exc:
        return _err(f"Typing indicator failed: {exc}")
    return _ok(platform_chat_id=target)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live IMessageConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        logger.exception("iMessage action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in IMESSAGE_ACTIONS if a.id not in _HANDLERS]

"""BlueBubbles action catalog + handlers for the connector action gateway.

Mirrors ``slack_actions.py``: every BlueBubbles REST API capability is an
``ActionDef`` with a matching handler, reached through the
``bluebubbles_actions`` / ``bluebubbles_invoke`` MCP tool pair.

BlueBubbles has no interactive card/button surface, so destructive-action
approval (``leave_chat``) is a plain text prompt: the connector asks the user
to reply 'approve'/'deny' and resolves the pending future from
``_check_permission_response`` (see ``BlueBubblesConnector``).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from app.core.connectors.base import ActionDef, OutgoingMessage

logger = logging.getLogger(__name__)

_VALID_REACTIONS = {"love", "like", "dislike", "laugh", "emphasize", "question"}


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_CHAT_PROP = {
    "platform_chat_id": {
        "type": "string",
        "description": "Optional chat guid/handle to target instead of the bound chat.",
    },
}

BLUEBUBBLES_ACTIONS: List[ActionDef] = [
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
            **_CHAT_PROP,
        }, ["message_guid", "reaction"]),
        category="reactions", risk="write",
    ),
    ActionDef(
        id="list_chats",
        name="List Chats",
        description="List chats known to the BlueBubbles server.",
        input_schema=_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
        category="chats", risk="safe",
    ),
    ActionDef(
        id="get_chat",
        name="Get Chat Info",
        description="Get details for one chat (display name, participant count).",
        input_schema=_schema({**_CHAT_PROP}),
        category="chats", risk="safe",
    ),
    ActionDef(
        id="rename_chat",
        name="Rename Chat",
        description="Rename a group chat's display name.",
        input_schema=_schema({
            "display_name": {"type": "string"},
            **_CHAT_PROP,
        }, ["display_name"]),
        category="chats", risk="write",
    ),
    ActionDef(
        id="leave_chat",
        name="Leave Chat",
        description="Make the bot leave a group chat.",
        input_schema=_schema({**_CHAT_PROP}),
        category="chats", risk="destructive",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in BLUEBUBBLES_ACTIONS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


async def _resolve_chat(
    conn: Any, ctx: Dict[str, Any], params: Dict[str, Any]
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve a target into a BlueBubbles chat GUID."""
    target = str(
        params.get("platform_chat_id") or ctx.get("bound_chat_id") or ""
    ).strip()
    if not target:
        return None, _err("No target chat — pass 'platform_chat_id' or use a bound session.")
    guid = await conn._resolve_chat_guid(target)
    if not guid:
        return None, _err(f"Could not resolve a BlueBubbles chat for {target!r}.")
    return guid, None


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
    target = str(params.get("platform_chat_id") or ctx.get("bound_chat_id") or "").strip()
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
    chat_guid, err = await _resolve_chat(conn, ctx, params)
    if err:
        return err
    payload = {
        "chatGuid": chat_guid,
        "selectedMessageGuid": message_guid,
        "reactionType": reaction.upper(),
    }
    resp = await conn._api_post("/api/v1/message/react", json_data=payload)
    if resp.status_code not in (200, 201):
        return _err(f"BlueBubbles API returned {resp.status_code}: {resp.text[:200]}")
    return _ok(message_guid=message_guid, reaction=reaction)


@_handler("list_chats")
async def _list_chats(conn, params, ctx):
    try:
        limit = max(1, min(200, int(params.get("limit") or 50)))
    except (TypeError, ValueError):
        limit = 50
    resp = await conn._api_post(
        "/api/v1/chat/query",
        json_data={"limit": limit, "offset": 0, "with": ["participants"]},
    )
    if resp.status_code != 200:
        return _err(f"BlueBubbles API returned {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    chats = data.get("data", []) if isinstance(data, dict) else []
    out = [{
        "guid": c.get("guid"),
        "display_name": c.get("displayName"),
        "participants": len(c.get("participants", []) or []),
    } for c in chats]
    return _ok(chats=out, count=len(out))


@_handler("get_chat")
async def _get_chat(conn, params, ctx):
    chat_guid, err = await _resolve_chat(conn, ctx, params)
    if err:
        return err
    resp = await conn._api_get(f"/api/v1/chat/{chat_guid}")
    if resp.status_code != 200:
        return _err(f"BlueBubbles API returned {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    chat = data.get("data", {}) if isinstance(data, dict) else {}
    return _ok(chat={
        "guid": chat.get("guid"),
        "display_name": chat.get("displayName"),
        "participants": len(chat.get("participants", []) or []),
    })


@_handler("rename_chat")
async def _rename_chat(conn, params, ctx):
    display_name = str(params.get("display_name") or "")
    if not display_name.strip():
        return _err("display_name is required.")
    chat_guid, err = await _resolve_chat(conn, ctx, params)
    if err:
        return err
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.put(
            f"{conn._base_url}/api/v1/chat/{chat_guid}",
            params=conn._params(),
            json={"displayName": display_name},
        )
    if resp.status_code not in (200, 201):
        return _err(f"BlueBubbles API returned {resp.status_code}: {resp.text[:200]}")
    return _ok(chat_guid=chat_guid, display_name=display_name)


@_handler("leave_chat")
async def _leave_chat(conn, params, ctx):
    chat_guid, err = await _resolve_chat(conn, ctx, params)
    if err:
        return err
    bound = str(ctx.get("bound_chat_id") or "")
    if bound and chat_guid == bound:
        return _err("Refusing to leave the chat this session is bound to.")
    resp = await conn._api_post(f"/api/v1/chat/{chat_guid}/leave")
    if resp.status_code not in (200, 201):
        return _err(f"BlueBubbles API returned {resp.status_code}: {resp.text[:200]}")
    return _ok(left_chat_guid=chat_guid)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live BlueBubblesConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        logger.exception("BlueBubbles action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in BLUEBUBBLES_ACTIONS if a.id not in _HANDLERS]

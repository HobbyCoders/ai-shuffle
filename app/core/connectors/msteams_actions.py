"""Microsoft Teams action catalog + handlers for the connector action gateway.

Mirrors ``slack_actions.py``: every Teams capability the Bot Framework REST API
exposes is an ``ActionDef`` with a matching handler, reached through the
``msteams_actions`` / ``msteams_invoke`` MCP tool pair.

Bot Framework is proactive-only: the bot can only act in a conversation it has
already received an inbound activity for (the connector caches the
``ConversationReference`` from the last inbound activity). Handlers resolve
the target conversation from ``platform_chat_id`` / the session's bound
conversation and fail cleanly when no reference is cached yet.

``risk == "destructive"`` actions require in-channel Approve/Deny confirmation
via an Adaptive Card (see ``MSTeamsConnector.request_action_approval``).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from app.core.connectors.base import ActionDef, OutgoingMessage

logger = logging.getLogger(__name__)

# Fallback Bot Framework service URL — only used if a cached conversation
# reference is missing a serviceUrl (should not normally happen).
DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/teams"


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_CONV_PROP = {
    "platform_chat_id": {
        "type": "string",
        "description": (
            "Optional conversation id to target instead of the bound "
            "conversation. The bot must have already received a message in "
            "that conversation."
        ),
    },
}


MSTEAMS_ACTIONS: List[ActionDef] = [
    ActionDef(
        id="send_message",
        name="Send Message",
        description="Send a plain-text message to a conversation. Long text is chunked automatically.",
        input_schema=_schema({"text": {"type": "string"}, **_CONV_PROP}, ["text"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="send_card",
        name="Send Adaptive Card",
        description="Send a rich Adaptive Card (arbitrary body, optional actions).",
        input_schema=_schema({
            "card": {"type": "object", "description": "Adaptive Card body (type/version added automatically if omitted)."},
            "fallback_text": {"type": "string", "description": "Plain-text summary shown in notifications."},
            **_CONV_PROP,
        }, ["card"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="update_message",
        name="Update Message",
        description="Edit the text of a message previously sent by this bot.",
        input_schema=_schema({
            "activity_id": {"type": "string", "description": "Activity id returned when the message was sent."},
            "text": {"type": "string"},
            **_CONV_PROP,
        }, ["activity_id", "text"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="delete_message",
        name="Delete Message",
        description="Delete a message previously sent by this bot.",
        input_schema=_schema({
            "activity_id": {"type": "string"},
            **_CONV_PROP,
        }, ["activity_id"]),
        category="messages", risk="destructive",
    ),
    ActionDef(
        id="send_typing",
        name="Send Typing Indicator",
        description="Show a typing indicator in the conversation.",
        input_schema=_schema({**_CONV_PROP}),
        category="presence", risk="safe",
    ),
    ActionDef(
        id="list_members",
        name="List Conversation Members",
        description="List the members of the conversation.",
        input_schema=_schema({**_CONV_PROP}),
        category="members", risk="safe",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in MSTEAMS_ACTIONS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


def _resolve_conversation(
    conn: Any, ctx: Dict[str, Any], params: Dict[str, Any]
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Resolve the target conversation id + its cached ConversationReference."""
    target = str(
        params.get("platform_chat_id") or ctx.get("bound_chat_id") or ""
    ).strip()
    if not target:
        return None, None, _err(
            "No target conversation — pass 'platform_chat_id' or use a bound session."
        )
    ref = conn._get_ref(target)
    if not ref:
        return None, None, _err(
            f"No conversation reference for {target} — the bot must receive a "
            "message here first before it can act proactively."
        )
    return target, ref, None


async def _bf_call(
    conn: Any, method: str, service_url: str, path: str,
    json_body: Optional[Dict[str, Any]] = None,
) -> httpx.Response:
    """Make an authenticated Bot Framework REST call."""
    token = await conn._ensure_token()
    url = f"{service_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.request(
            method, url, headers=conn._auth_headers(token), json=json_body,
        )


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
        return _err("No target conversation — pass 'platform_chat_id' or use a bound session.")
    success = await conn.send_message(OutgoingMessage(platform_chat_id=target, text=text))
    if not success:
        return _err("Failed to send message (no conversation reference or Bot Framework error).")
    return _ok(platform_chat_id=target)


@_handler("send_card")
async def _send_card(conn, params, ctx):
    card = params.get("card")
    if not isinstance(card, dict):
        return _err("card must be an object (Adaptive Card body).")
    conv_id, ref, err = _resolve_conversation(conn, ctx, params)
    if err:
        return err
    card_body = {"type": "AdaptiveCard", "version": "1.4", **card}
    activity = {
        "type": "message",
        "text": str(params.get("fallback_text") or ""),
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card_body,
        }],
    }
    ok = await conn._send_activity(
        ref.get("service_url", DEFAULT_SERVICE_URL), conv_id, activity,
    )
    if not ok:
        return _err("Failed to send Adaptive Card.")
    return _ok(platform_chat_id=conv_id)


@_handler("update_message")
async def _update_message(conn, params, ctx):
    activity_id = str(params.get("activity_id") or "")
    text = str(params.get("text") or "")
    if not activity_id or not text.strip():
        return _err("activity_id and text are required.")
    conv_id, ref, err = _resolve_conversation(conn, ctx, params)
    if err:
        return err
    resp = await _bf_call(
        conn, "PUT", ref.get("service_url", DEFAULT_SERVICE_URL),
        f"/v3/conversations/{conv_id}/activities/{activity_id}",
        {"type": "message", "text": text},
    )
    if not (200 <= resp.status_code < 300):
        return _err(f"Bot Framework returned {resp.status_code}: {resp.text[:200]}")
    return _ok(activity_id=activity_id, platform_chat_id=conv_id)


@_handler("delete_message")
async def _delete_message(conn, params, ctx):
    activity_id = str(params.get("activity_id") or "")
    if not activity_id:
        return _err("activity_id is required.")
    conv_id, ref, err = _resolve_conversation(conn, ctx, params)
    if err:
        return err
    resp = await _bf_call(
        conn, "DELETE", ref.get("service_url", DEFAULT_SERVICE_URL),
        f"/v3/conversations/{conv_id}/activities/{activity_id}",
    )
    if not (200 <= resp.status_code < 300):
        return _err(f"Bot Framework returned {resp.status_code}: {resp.text[:200]}")
    return _ok(deleted_activity_id=activity_id, platform_chat_id=conv_id)


@_handler("send_typing")
async def _send_typing(conn, params, ctx):
    conv_id, ref, err = _resolve_conversation(conn, ctx, params)
    if err:
        return err
    ok = await conn._send_activity(
        ref.get("service_url", DEFAULT_SERVICE_URL), conv_id, {"type": "typing"},
    )
    if not ok:
        return _err("Failed to send typing indicator.")
    return _ok(platform_chat_id=conv_id)


@_handler("list_members")
async def _list_members(conn, params, ctx):
    conv_id, ref, err = _resolve_conversation(conn, ctx, params)
    if err:
        return err
    resp = await _bf_call(
        conn, "GET", ref.get("service_url", DEFAULT_SERVICE_URL),
        f"/v3/conversations/{conv_id}/members",
    )
    if resp.status_code != 200:
        return _err(f"Bot Framework returned {resp.status_code}: {resp.text[:200]}")
    members = resp.json()
    if not isinstance(members, list):
        members = []
    return _ok(
        members=[{"id": m.get("id"), "name": m.get("name")} for m in members],
        count=len(members),
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live MSTeamsConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        logger.exception("Teams action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in MSTEAMS_ACTIONS if a.id not in _HANDLERS]

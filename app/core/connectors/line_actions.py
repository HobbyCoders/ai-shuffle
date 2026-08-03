"""LINE action catalog + handlers for the connector action gateway.

Mirrors ``slack_actions.py``: every LINE Messaging API capability is an
``ActionDef`` with a matching handler, reached through the ``line_actions`` /
``line_invoke`` MCP tool pair.

Scoping rules:
    * Message-level actions may only target the session's bound chat or the
      connector's ``allowed_user_ids`` / ``allowed_group_ids``.
    * ``risk == "destructive"`` actions (``leave_group``) require in-channel
      Approve/Deny confirmation via a Flex Message
      (see ``LINEConnector.request_action_approval``).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

import httpx

from app.core.connectors.base import ActionDef, OutgoingMessage

logger = logging.getLogger(__name__)

LINE_API_BASE = "https://api.line.me/v2"


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_TARGET_PROP = {
    "platform_chat_id": {
        "type": "string",
        "description": "Optional user/group/room id to target instead of the bound chat.",
    },
}

LINE_ACTIONS: List[ActionDef] = [
    ActionDef(
        id="send_message",
        name="Send Message",
        description="Push a text message to a user or group. Long text is chunked automatically.",
        input_schema=_schema({"text": {"type": "string"}, **_TARGET_PROP}, ["text"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="send_sticker",
        name="Send Sticker",
        description="Send a LINE sticker message (packageId + stickerId).",
        input_schema=_schema({
            "package_id": {"type": "string"},
            "sticker_id": {"type": "string"},
            **_TARGET_PROP,
        }, ["package_id", "sticker_id"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="send_image",
        name="Send Image",
        description="Send an image message from public HTTPS URLs.",
        input_schema=_schema({
            "image_url": {"type": "string", "description": "Original full-size image URL (HTTPS)."},
            "preview_url": {"type": "string", "description": "Preview thumbnail URL; defaults to image_url."},
            **_TARGET_PROP,
        }, ["image_url"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="get_profile",
        name="Get User Profile",
        description="Get a LINE user's display name, picture, and status message.",
        input_schema=_schema({"user_id": {"type": "string"}}, ["user_id"]),
        category="users", risk="safe",
    ),
    ActionDef(
        id="get_group_summary",
        name="Get Group Summary",
        description="Get a group chat's name and picture.",
        input_schema=_schema({"group_id": {"type": "string"}}, ["group_id"]),
        category="groups", risk="safe",
    ),
    ActionDef(
        id="leave_group",
        name="Leave Group",
        description="Make the bot leave a group or room chat.",
        input_schema=_schema({"group_id": {"type": "string"}}, ["group_id"]),
        category="groups", risk="destructive",
    ),
    ActionDef(
        id="get_quota",
        name="Get Message Quota",
        description="Get this month's push/broadcast message quota and consumption.",
        input_schema=_schema({}),
        category="account", risk="safe",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in LINE_ACTIONS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


def _allowed_targets(conn: Any, ctx: Dict[str, Any]) -> Set[str]:
    allowed: Set[str] = set()
    bound = str(ctx.get("bound_chat_id") or "")
    if bound:
        allowed.add(bound)
    for key in ("allowed_user_ids", "allowed_group_ids"):
        raw = (conn.config or {}).get(key)
        if raw:
            if isinstance(raw, str):
                raw = [raw]
            allowed.update(str(c) for c in raw)
    if not bound:
        return set()  # non-gateway caller (automation) — unrestricted
    return allowed


def _resolve_target(
    conn: Any, ctx: Dict[str, Any], params: Dict[str, Any]
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    target = str(
        params.get("platform_chat_id") or ctx.get("bound_chat_id") or ""
    ).strip()
    if not target:
        return None, _err("No target chat — pass 'platform_chat_id' or use a bound session.")
    allowed = _allowed_targets(conn, ctx)
    if allowed and target not in allowed:
        return None, _err(
            f"Chat {target} is not permitted for this connector session. "
            "Only the bound chat or the connector's allowed users/groups may be used."
        )
    return target, None


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
    to, err = _resolve_target(conn, ctx, params)
    if err:
        return err
    success = await conn.send_message(OutgoingMessage(platform_chat_id=to, text=text))
    if not success:
        return _err("Failed to send message.")
    return _ok(platform_chat_id=to)


@_handler("send_sticker")
async def _send_sticker(conn, params, ctx):
    package_id = str(params.get("package_id") or "")
    sticker_id = str(params.get("sticker_id") or "")
    if not package_id or not sticker_id:
        return _err("package_id and sticker_id are required.")
    to, err = _resolve_target(conn, ctx, params)
    if err:
        return err
    ok = await conn._push_message(to, [
        {"type": "sticker", "packageId": package_id, "stickerId": sticker_id},
    ])
    if not ok:
        return _err("Failed to send sticker.")
    return _ok(platform_chat_id=to)


@_handler("send_image")
async def _send_image(conn, params, ctx):
    original = str(params.get("image_url") or "")
    if not original:
        return _err("image_url is required.")
    preview = str(params.get("preview_url") or original)
    to, err = _resolve_target(conn, ctx, params)
    if err:
        return err
    ok = await conn._push_message(to, [
        {"type": "image", "originalContentUrl": original, "previewImageUrl": preview},
    ])
    if not ok:
        return _err("Failed to send image.")
    return _ok(platform_chat_id=to)


@_handler("get_profile")
async def _get_profile(conn, params, ctx):
    user_id = str(params.get("user_id") or "")
    if not user_id:
        return _err("user_id is required.")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{LINE_API_BASE}/bot/profile/{user_id}", headers=conn._headers(),
        )
    if resp.status_code != 200:
        return _err(f"LINE API returned {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return _ok(profile={
        "user_id": data.get("userId"),
        "display_name": data.get("displayName"),
        "picture_url": data.get("pictureUrl"),
        "status_message": data.get("statusMessage"),
    })


@_handler("get_group_summary")
async def _get_group_summary(conn, params, ctx):
    group_id = str(params.get("group_id") or "")
    if not group_id:
        return _err("group_id is required.")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{LINE_API_BASE}/bot/group/{group_id}/summary", headers=conn._headers(),
        )
    if resp.status_code != 200:
        return _err(f"LINE API returned {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return _ok(group={
        "group_id": data.get("groupId"),
        "name": data.get("groupName"),
        "picture_url": data.get("pictureUrl"),
    })


@_handler("leave_group")
async def _leave_group(conn, params, ctx):
    group_id = str(params.get("group_id") or "")
    if not group_id:
        return _err("group_id is required.")
    if group_id == str(ctx.get("bound_chat_id") or ""):
        return _err("Refusing to leave the group this session is bound to.")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{LINE_API_BASE}/bot/group/{group_id}/leave", headers=conn._headers(),
        )
    if not (200 <= resp.status_code < 300):
        return _err(f"LINE API returned {resp.status_code}: {resp.text[:200]}")
    return _ok(left_group_id=group_id)


@_handler("get_quota")
async def _get_quota(conn, params, ctx):
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{LINE_API_BASE}/bot/message/quota/consumption", headers=conn._headers(),
        )
    if resp.status_code != 200:
        return _err(f"LINE API returned {resp.status_code}: {resp.text[:200]}")
    return _ok(quota=resp.json())


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live LINEConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        logger.exception("LINE action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in LINE_ACTIONS if a.id not in _HANDLERS]

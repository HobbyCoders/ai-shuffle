"""Signal action catalog + handlers for the connector action gateway.

Mirrors ``slack_actions.py``. Signal's REST API surface (signal-cli-rest-api)
is far smaller than Slack/Discord's — no channel management, no roles, no
files-as-a-first-class-object — so the catalog stays small: messaging,
presence (typing indicator), reactions, and read-only group lookups plus
leaving a group. There is no discrete permission-scope concept on this
platform, so ``required_permission`` is left unset.

Scoping rules:
    * Chat-level actions may only target the session's bound chat (a phone
      number or group id) or the connector's ``allowed_numbers``.
    * ``risk == "destructive"`` actions require in-channel Approve/Deny
      confirmation. Signal has no interactive buttons, so
      ``SignalConnector.request_action_approval`` asks the user to reply
      with a plain-text "approve"/"deny" (mirroring the existing
      ``send_permission_prompt`` behaviour).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from app.core.connectors.base import ActionDef, chunk_text

logger = logging.getLogger(__name__)


_CHAT_PROP = {
    "chat_id": {
        "type": "string",
        "description": (
            "Optional recipient (phone number or group id) to target instead "
            "of the bound chat. Must be an allowed number or the call is "
            "rejected."
        ),
    },
}


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


SIGNAL_ACTIONS: List[ActionDef] = [
    # -- messages --------------------------------------------------------
    ActionDef(
        id="send_message",
        name="Send Message",
        description="Send a message. Long text is chunked automatically.",
        input_schema=_schema({
            "text": {"type": "string"},
            "platform_chat_id": {"type": "string", "description": "Legacy alias for chat_id."},
            **_CHAT_PROP,
        }, ["text"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="mark_read",
        name="Mark Message Read",
        description="Send a read receipt for a message.",
        input_schema=_schema({
            "timestamp": {"type": "integer", "description": "Original message timestamp."},
            **_CHAT_PROP,
        }, ["timestamp"]),
        category="messages", risk="write",
    ),
    # -- presence ----------------------------------------------------------
    ActionDef(
        id="send_typing_indicator",
        name="Start Typing Indicator",
        description="Show a typing indicator to the recipient.",
        input_schema=_schema({**_CHAT_PROP}),
        category="presence", risk="write",
    ),
    ActionDef(
        id="stop_typing_indicator",
        name="Stop Typing Indicator",
        description="Clear the typing indicator shown to the recipient.",
        input_schema=_schema({**_CHAT_PROP}),
        category="presence", risk="write",
    ),
    # -- reactions ---------------------------------------------------------
    ActionDef(
        id="add_reaction",
        name="Add Reaction",
        description="React to a message with an emoji.",
        input_schema=_schema({
            "emoji": {"type": "string", "description": "A single emoji character."},
            "target_author": {"type": "string", "description": "Phone number that sent the original message."},
            "timestamp": {"type": "integer", "description": "Original message timestamp."},
            **_CHAT_PROP,
        }, ["emoji", "target_author", "timestamp"]),
        category="reactions", risk="write",
    ),
    ActionDef(
        id="remove_reaction",
        name="Remove Reaction",
        description="Remove this bot's own reaction from a message.",
        input_schema=_schema({
            "emoji": {"type": "string"},
            "target_author": {"type": "string"},
            "timestamp": {"type": "integer"},
            **_CHAT_PROP,
        }, ["emoji", "target_author", "timestamp"]),
        category="reactions", risk="write",
    ),
    # -- groups --------------------------------------------------------------
    ActionDef(
        id="list_groups",
        name="List Groups",
        description="List Signal groups this account is a member of.",
        input_schema=_schema({}),
        category="groups", risk="safe",
    ),
    ActionDef(
        id="get_group_info",
        name="Get Group Info",
        description="Details for one group (name, members, description).",
        input_schema=_schema({"group_id": {"type": "string"}}, ["group_id"]),
        category="groups", risk="safe",
    ),
    ActionDef(
        id="leave_group",
        name="Leave Group",
        description="Leave (quit) a Signal group.",
        input_schema=_schema({"group_id": {"type": "string"}}, ["group_id"]),
        category="groups", risk="destructive",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in SIGNAL_ACTIONS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


def _allowed_chat_ids(conn: Any, ctx: Dict[str, Any]) -> Set[str]:
    allowed: Set[str] = set()
    bound = str(ctx.get("bound_chat_id") or "")
    if bound:
        allowed.add(bound)
    raw = (conn.config or {}).get("allowed_numbers")
    if raw:
        if isinstance(raw, str):
            raw = [raw]
        allowed.update(str(c) for c in raw)
    if not bound:
        return set()  # non-gateway caller (automation) — unrestricted
    return allowed


def _resolve_chat(
    conn: Any, ctx: Dict[str, Any], params: Dict[str, Any]
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    target = str(
        params.get("chat_id") or params.get("platform_chat_id")
        or ctx.get("bound_chat_id") or ""
    ).strip()
    if not target:
        return None, _err("No target chat — pass 'chat_id' or use a bound session.")
    allowed = _allowed_chat_ids(conn, ctx)
    if allowed and target not in allowed:
        return None, _err(
            f"Chat {target} is not permitted for this connector session. "
            "Only the bound chat or the connector's allowed numbers may be used."
        )
    return target, None


def _check_status(resp: Any, ok_codes=(200, 201, 204)) -> Optional[Dict[str, Any]]:
    if resp.status_code not in ok_codes:
        return _err(f"signal-cli API returned {resp.status_code}: {resp.text[:200]}")
    return None


Handler = Callable[[Any, Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
_HANDLERS: Dict[str, Handler] = {}


def _handler(action_id: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[action_id] = fn
        return fn
    return deco


# -- messages -----------------------------------------------------------------

@_handler("send_message")
async def _send_message(conn, params, ctx):
    text = str(params.get("text") or "")
    if not text.strip():
        return _err("text is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    for chunk in chunk_text(text, conn.MAX_MESSAGE_LENGTH or 4000):
        resp = await conn._api_post("/api/v2/send", {
            "message": chunk, "number": conn._account, "recipients": [chat_id],
        })
        bad = _check_status(resp)
        if bad:
            return bad
    return _ok(chat_id=chat_id)


@_handler("mark_read")
async def _mark_read(conn, params, ctx):
    timestamp = params.get("timestamp")
    if timestamp is None:
        return _err("timestamp is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    resp = await conn._api_post(f"/api/v1/receipt/{conn._account}", {
        "recipient": chat_id, "timestamps": [int(timestamp)],
    })
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id, timestamp=int(timestamp))


# -- presence ------------------------------------------------------------------

@_handler("send_typing_indicator")
async def _send_typing_indicator(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    resp = await conn._api_put(f"/api/v1/typing-indicator/{conn._account}", {"recipient": chat_id})
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id, typing=True)


@_handler("stop_typing_indicator")
async def _stop_typing_indicator(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    resp = await conn._api_delete(f"/api/v1/typing-indicator/{conn._account}", {"recipient": chat_id})
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id, typing=False)


# -- reactions -------------------------------------------------------------

@_handler("add_reaction")
async def _add_reaction(conn, params, ctx):
    emoji = str(params.get("emoji") or "").strip()
    target_author = str(params.get("target_author") or "").strip()
    timestamp = params.get("timestamp")
    if not emoji or not target_author or timestamp is None:
        return _err("emoji, target_author, and timestamp are required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    resp = await conn._api_post(f"/api/v1/reactions/{conn._account}", {
        "recipient": chat_id, "reaction": emoji,
        "target_author": target_author, "timestamp": int(timestamp),
    })
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id, emoji=emoji)


@_handler("remove_reaction")
async def _remove_reaction(conn, params, ctx):
    emoji = str(params.get("emoji") or "").strip()
    target_author = str(params.get("target_author") or "").strip()
    timestamp = params.get("timestamp")
    if not emoji or not target_author or timestamp is None:
        return _err("emoji, target_author, and timestamp are required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    resp = await conn._api_delete(f"/api/v1/reactions/{conn._account}", {
        "recipient": chat_id, "reaction": emoji,
        "target_author": target_author, "timestamp": int(timestamp),
    })
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id, emoji=emoji, removed=True)


# -- groups ------------------------------------------------------------------

@_handler("list_groups")
async def _list_groups(conn, params, ctx):
    resp = await conn._api_get(f"/api/v1/groups/{conn._account}")
    bad = _check_status(resp)
    if bad:
        return bad
    groups = resp.json() or []
    out = [{
        "id": g.get("id"),
        "name": g.get("name"),
        "member_count": len(g.get("members", [])),
    } for g in groups]
    return _ok(groups=out, count=len(out))


@_handler("get_group_info")
async def _get_group_info(conn, params, ctx):
    group_id = str(params.get("group_id") or "").strip()
    if not group_id:
        return _err("group_id is required.")
    resp = await conn._api_get(f"/api/v1/groups/{conn._account}/{group_id}")
    bad = _check_status(resp)
    if bad:
        return bad
    g = resp.json() or {}
    return _ok(group={
        "id": g.get("id"),
        "name": g.get("name"),
        "description": g.get("description"),
        "members": g.get("members", []),
    })


@_handler("leave_group")
async def _leave_group(conn, params, ctx):
    group_id = str(params.get("group_id") or "").strip()
    if not group_id:
        return _err("group_id is required.")
    bound = str(ctx.get("bound_chat_id") or "")
    if bound and group_id == bound:
        return _err("Refusing to leave the group this session is bound to.")
    resp = await conn._api_post(f"/api/v1/groups/{conn._account}/{group_id}/quit", {})
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(left_group_id=group_id)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live SignalConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        logger.exception("Signal action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in SIGNAL_ACTIONS if a.id not in _HANDLERS]

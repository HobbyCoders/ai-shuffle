"""Slack action catalog + handlers for the connector action gateway.

Mirrors ``discord_actions.py``: every Slack capability is an ``ActionDef``
(id, JSON schema, category, required scope, risk tier) with a matching
handler, reached through the ``slack_actions`` / ``slack_invoke`` MCP tool
pair. ``required_permission`` holds the Slack OAuth scope the bot token needs
— Slack offers no cheap way to introspect granted scopes, so scopes are
documented in discovery output and enforced by Slack itself at call time.

Scoping rules:
    * Message-level actions may only target the session's bound channel or
      the connector's ``allowed_channel_ids``.
    * Workspace-level actions (channel management, users) are gated by
      category config and risk tier.
    * ``risk == "destructive"`` actions require in-channel Approve/Deny
      confirmation (see SlackConnector.request_action_approval).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from app.core.connectors.base import ActionDef, chunk_text

logger = logging.getLogger(__name__)


_CHANNEL_PROP = {
    "channel": {
        "type": "string",
        "description": (
            "Optional channel ID to target instead of the bound channel. Must "
            "be an allowed channel or the call is rejected."
        ),
    },
}

_TS = {"type": "string", "description": "Slack message timestamp (ts), e.g. '1712345678.000200'."}


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


SLACK_ACTIONS: List[ActionDef] = [
    # -- messages ----------------------------------------------------------
    ActionDef(
        id="send_message",
        name="Send Message",
        description="Send a message (optionally into a thread). Long text is chunked automatically.",
        input_schema=_schema({
            "text": {"type": "string"},
            "thread_ts": {"type": "string", "description": "Optional thread timestamp to reply in."},
            "platform_chat_id": {"type": "string", "description": "Legacy alias for channel."},
            **_CHANNEL_PROP,
        }, ["text"]),
        category="messages", required_permission="chat:write", risk="write",
    ),
    ActionDef(
        id="read_messages",
        name="Read Messages",
        description="Read recent channel messages (newest first) for context.",
        input_schema=_schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            **_CHANNEL_PROP,
        }),
        category="messages", required_permission="channels:history", risk="safe",
    ),
    ActionDef(
        id="read_thread",
        name="Read Thread",
        description="Read the replies of a thread.",
        input_schema=_schema({
            "thread_ts": _TS,
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            **_CHANNEL_PROP,
        }, ["thread_ts"]),
        category="messages", required_permission="channels:history", risk="safe",
    ),
    ActionDef(
        id="edit_message",
        name="Edit Message",
        description="Edit a message previously sent by this bot.",
        input_schema=_schema({
            "ts": _TS,
            "text": {"type": "string"},
            **_CHANNEL_PROP,
        }, ["ts", "text"]),
        category="messages", required_permission="chat:write", risk="write",
    ),
    ActionDef(
        id="delete_message",
        name="Delete Message",
        description="Delete a message.",
        input_schema=_schema({"ts": _TS, **_CHANNEL_PROP}, ["ts"]),
        category="messages", required_permission="chat:write", risk="destructive",
    ),
    ActionDef(
        id="schedule_message",
        name="Schedule Message",
        description="Schedule a message for later delivery (ISO 8601 time, assumed UTC without offset).",
        input_schema=_schema({
            "text": {"type": "string"},
            "post_at": {"type": "string", "description": "ISO 8601 delivery time."},
            **_CHANNEL_PROP,
        }, ["text", "post_at"]),
        category="messages", required_permission="chat:write", risk="write",
    ),
    # -- reactions ---------------------------------------------------------
    ActionDef(
        id="add_reaction",
        name="Add Reaction",
        description="React to a message with an emoji (name without colons, e.g. 'thumbsup').",
        input_schema=_schema({
            "ts": _TS,
            "emoji": {"type": "string", "description": "Emoji name without colons."},
            **_CHANNEL_PROP,
        }, ["ts", "emoji"]),
        category="reactions", required_permission="reactions:write", risk="write",
    ),
    ActionDef(
        id="remove_reaction",
        name="Remove Reaction",
        description="Remove this bot's own reaction from a message.",
        input_schema=_schema({
            "ts": _TS,
            "emoji": {"type": "string"},
            **_CHANNEL_PROP,
        }, ["ts", "emoji"]),
        category="reactions", required_permission="reactions:write", risk="write",
    ),
    # -- pins --------------------------------------------------------------
    ActionDef(
        id="pin_message",
        name="Pin Message",
        description="Pin a message in the channel.",
        input_schema=_schema({"ts": _TS, **_CHANNEL_PROP}, ["ts"]),
        category="pins", required_permission="pins:write", risk="write",
    ),
    ActionDef(
        id="unpin_message",
        name="Unpin Message",
        description="Unpin a message in the channel.",
        input_schema=_schema({"ts": _TS, **_CHANNEL_PROP}, ["ts"]),
        category="pins", required_permission="pins:write", risk="write",
    ),
    ActionDef(
        id="list_pins",
        name="List Pinned Messages",
        description="List the channel's pinned items.",
        input_schema=_schema({**_CHANNEL_PROP}),
        category="pins", required_permission="pins:read", risk="safe",
    ),
    # -- channels ----------------------------------------------------------
    ActionDef(
        id="list_channels",
        name="List Channels",
        description="List workspace channels the bot can see.",
        input_schema=_schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }),
        category="channels", required_permission="channels:read", risk="safe",
    ),
    ActionDef(
        id="get_channel_info",
        name="Get Channel Info",
        description="Details for one channel (topic, purpose, member count).",
        input_schema=_schema({"channel_id": {"type": "string"}}, ["channel_id"]),
        category="channels", required_permission="channels:read", risk="safe",
    ),
    ActionDef(
        id="create_channel",
        name="Create Channel",
        description="Create a public or private channel.",
        input_schema=_schema({
            "name": {"type": "string", "description": "Lowercase, no spaces (use hyphens)."},
            "private": {"type": "boolean"},
        }, ["name"]),
        category="channels", required_permission="channels:manage", risk="write",
    ),
    ActionDef(
        id="set_topic",
        name="Set Channel Topic",
        description="Set the channel's topic.",
        input_schema=_schema({"topic": {"type": "string"}, **_CHANNEL_PROP}, ["topic"]),
        category="channels", required_permission="channels:manage", risk="write",
    ),
    ActionDef(
        id="invite_to_channel",
        name="Invite Users To Channel",
        description="Invite one or more users to the channel.",
        input_schema=_schema({
            "user_ids": {"type": "array", "description": "Slack user IDs."},
            **_CHANNEL_PROP,
        }, ["user_ids"]),
        category="channels", required_permission="channels:manage", risk="write",
    ),
    ActionDef(
        id="archive_channel",
        name="Archive Channel",
        description="Archive a channel.",
        input_schema=_schema({"channel_id": {"type": "string"}}, ["channel_id"]),
        category="channels", required_permission="channels:manage", risk="destructive",
    ),
    # -- users -------------------------------------------------------------
    ActionDef(
        id="get_user",
        name="Get User Info",
        description="Profile info for one user.",
        input_schema=_schema({"user_id": {"type": "string"}}, ["user_id"]),
        category="users", required_permission="users:read", risk="safe",
    ),
    ActionDef(
        id="list_users",
        name="List Users",
        description="List workspace members.",
        input_schema=_schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }),
        category="users", required_permission="users:read", risk="safe",
    ),
    # -- files -------------------------------------------------------------
    ActionDef(
        id="upload_file",
        name="Upload File",
        description="Upload a file from the local filesystem to the channel.",
        input_schema=_schema({
            "path": {"type": "string", "description": "Local filesystem path."},
            "title": {"type": "string"},
            "caption": {"type": "string", "description": "Optional message sent with the file."},
            **_CHANNEL_PROP,
        }, ["path"]),
        category="files", required_permission="files:write", risk="write",
    ),
    # -- workspace ---------------------------------------------------------
    ActionDef(
        id="get_workspace_info",
        name="Get Workspace Info",
        description="Workspace name, domain, and the bot's identity.",
        input_schema=_schema({}),
        category="workspace", risk="safe",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in SLACK_ACTIONS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


def _client(conn: Any) -> Optional[Any]:
    return getattr(conn, "_web_client", None)


def _allowed_channel_ids(conn: Any, ctx: Dict[str, Any]) -> Set[str]:
    allowed: Set[str] = set()
    bound = str(ctx.get("bound_chat_id") or "")
    if bound:
        allowed.add(bound)
    raw = (conn.config or {}).get("allowed_channel_ids")
    if raw:
        if isinstance(raw, str):
            raw = [raw]
        allowed.update(str(c) for c in raw)
    if not bound:
        return set()  # non-gateway caller (automation) — unrestricted
    return allowed


def _resolve_channel(
    conn: Any, ctx: Dict[str, Any], params: Dict[str, Any]
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve and allow-list-check the target channel id (string)."""
    target = str(
        params.get("channel") or params.get("platform_chat_id")
        or ctx.get("bound_chat_id") or ""
    ).strip()
    if not target:
        return None, _err("No target channel — pass 'channel' or use a bound session.")
    allowed = _allowed_channel_ids(conn, ctx)
    if allowed and target not in allowed:
        return None, _err(
            f"Channel {target} is not permitted for this connector session. "
            "Only the bound channel or the connector's allowed channels may be used."
        )
    return target, None


def _parse_iso_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _msg_payload(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ts": m.get("ts"),
        "user": m.get("user") or m.get("bot_id"),
        "bot": bool(m.get("bot_id")),
        "thread_ts": m.get("thread_ts"),
        "reply_count": m.get("reply_count"),
        "text": (m.get("text") or "")[:500],
    }


Handler = Callable[[Any, Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
_HANDLERS: Dict[str, Handler] = {}


def _handler(action_id: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[action_id] = fn
        return fn
    return deco


# -- messages ---------------------------------------------------------------

@_handler("send_message")
async def _send_message(conn, params, ctx):
    text = str(params.get("text") or "")
    if not text.strip():
        return _err("text is required.")
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    thread_ts = params.get("thread_ts") or None
    ts_list = []
    for chunk in chunk_text(text, conn.MAX_MESSAGE_LENGTH or 4000):
        resp = await _client(conn).chat_postMessage(
            channel=channel, text=chunk, thread_ts=thread_ts,
        )
        ts_list.append(resp.get("ts"))
    return _ok(message_ts=ts_list, channel_id=channel)


@_handler("read_messages")
async def _read_messages(conn, params, ctx):
    try:
        limit = max(1, min(50, int(params.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    resp = await _client(conn).conversations_history(channel=channel, limit=limit)
    messages = [_msg_payload(m) for m in resp.get("messages", [])]
    return _ok(messages=messages, count=len(messages), channel_id=channel)


@_handler("read_thread")
async def _read_thread(conn, params, ctx):
    thread_ts = str(params.get("thread_ts") or "").strip()
    if not thread_ts:
        return _err("thread_ts is required.")
    try:
        limit = max(1, min(50, int(params.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    resp = await _client(conn).conversations_replies(
        channel=channel, ts=thread_ts, limit=limit,
    )
    messages = [_msg_payload(m) for m in resp.get("messages", [])]
    return _ok(messages=messages, count=len(messages), thread_ts=thread_ts)


@_handler("edit_message")
async def _edit_message(conn, params, ctx):
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    resp = await _client(conn).chat_update(
        channel=channel, ts=str(params.get("ts")), text=str(params.get("text") or ""),
    )
    return _ok(ts=resp.get("ts"), channel_id=channel)


@_handler("delete_message")
async def _delete_message(conn, params, ctx):
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    await _client(conn).chat_delete(channel=channel, ts=str(params.get("ts")))
    return _ok(deleted_ts=str(params.get("ts")), channel_id=channel)


@_handler("schedule_message")
async def _schedule_message(conn, params, ctx):
    text = str(params.get("text") or "")
    if not text.strip():
        return _err("text is required.")
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    try:
        post_at = int(_parse_iso_utc(params.get("post_at")).timestamp())
    except (TypeError, ValueError):
        return _err(f"Invalid post_at: {params.get('post_at')!r} (use ISO 8601).")
    resp = await _client(conn).chat_scheduleMessage(
        channel=channel, text=text, post_at=post_at,
    )
    return _ok(scheduled_message_id=resp.get("scheduled_message_id"), post_at=post_at)


# -- reactions --------------------------------------------------------------

@_handler("add_reaction")
async def _add_reaction(conn, params, ctx):
    emoji = str(params.get("emoji") or "").strip().strip(":")
    if not emoji:
        return _err("emoji is required.")
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    await _client(conn).reactions_add(
        channel=channel, timestamp=str(params.get("ts")), name=emoji,
    )
    return _ok(ts=str(params.get("ts")), emoji=emoji)


@_handler("remove_reaction")
async def _remove_reaction(conn, params, ctx):
    emoji = str(params.get("emoji") or "").strip().strip(":")
    if not emoji:
        return _err("emoji is required.")
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    await _client(conn).reactions_remove(
        channel=channel, timestamp=str(params.get("ts")), name=emoji,
    )
    return _ok(ts=str(params.get("ts")), emoji=emoji, removed=True)


# -- pins -------------------------------------------------------------------

@_handler("pin_message")
async def _pin_message(conn, params, ctx):
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    await _client(conn).pins_add(channel=channel, timestamp=str(params.get("ts")))
    return _ok(ts=str(params.get("ts")), pinned=True)


@_handler("unpin_message")
async def _unpin_message(conn, params, ctx):
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    await _client(conn).pins_remove(channel=channel, timestamp=str(params.get("ts")))
    return _ok(ts=str(params.get("ts")), pinned=False)


@_handler("list_pins")
async def _list_pins(conn, params, ctx):
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    resp = await _client(conn).pins_list(channel=channel)
    pins = []
    for item in resp.get("items", []):
        msg = item.get("message", {})
        pins.append({"ts": msg.get("ts"), "text": (msg.get("text") or "")[:300]})
    return _ok(pins=pins, count=len(pins))


# -- channels ---------------------------------------------------------------

@_handler("list_channels")
async def _list_channels(conn, params, ctx):
    try:
        limit = max(1, min(200, int(params.get("limit") or 100)))
    except (TypeError, ValueError):
        limit = 100
    resp = await _client(conn).conversations_list(
        types="public_channel,private_channel", limit=limit,
    )
    channels = [{
        "id": c.get("id"),
        "name": c.get("name"),
        "private": bool(c.get("is_private")),
        "archived": bool(c.get("is_archived")),
        "num_members": c.get("num_members"),
    } for c in resp.get("channels", [])]
    return _ok(channels=channels, count=len(channels))


@_handler("get_channel_info")
async def _get_channel_info(conn, params, ctx):
    resp = await _client(conn).conversations_info(channel=str(params.get("channel_id")))
    c = resp.get("channel", {})
    return _ok(channel={
        "id": c.get("id"),
        "name": c.get("name"),
        "topic": (c.get("topic") or {}).get("value"),
        "purpose": (c.get("purpose") or {}).get("value"),
        "private": bool(c.get("is_private")),
        "archived": bool(c.get("is_archived")),
        "num_members": c.get("num_members"),
    })


@_handler("create_channel")
async def _create_channel(conn, params, ctx):
    name = str(params.get("name") or "").strip()
    if not name:
        return _err("name is required.")
    resp = await _client(conn).conversations_create(
        name=name, is_private=bool(params.get("private")),
    )
    c = resp.get("channel", {})
    return _ok(channel_id=c.get("id"), name=c.get("name"))


@_handler("set_topic")
async def _set_topic(conn, params, ctx):
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    await _client(conn).conversations_setTopic(
        channel=channel, topic=str(params.get("topic") or ""),
    )
    return _ok(channel_id=channel, topic=str(params.get("topic") or ""))


@_handler("invite_to_channel")
async def _invite_to_channel(conn, params, ctx):
    user_ids = params.get("user_ids")
    if not isinstance(user_ids, list) or not user_ids:
        return _err("user_ids must be a non-empty list.")
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    await _client(conn).conversations_invite(
        channel=channel, users=",".join(str(u) for u in user_ids),
    )
    return _ok(channel_id=channel, invited=[str(u) for u in user_ids])


@_handler("archive_channel")
async def _archive_channel(conn, params, ctx):
    channel_id = str(params.get("channel_id") or "")
    if channel_id == str(ctx.get("bound_chat_id") or ""):
        return _err("Refusing to archive the channel this session is bound to.")
    await _client(conn).conversations_archive(channel=channel_id)
    return _ok(archived_channel_id=channel_id)


# -- users ------------------------------------------------------------------

@_handler("get_user")
async def _get_user(conn, params, ctx):
    resp = await _client(conn).users_info(user=str(params.get("user_id")))
    u = resp.get("user", {})
    profile = u.get("profile", {})
    return _ok(user={
        "id": u.get("id"),
        "name": u.get("name"),
        "real_name": profile.get("real_name"),
        "display_name": profile.get("display_name"),
        "title": profile.get("title"),
        "bot": bool(u.get("is_bot")),
        "admin": bool(u.get("is_admin")),
        "tz": u.get("tz"),
    })


@_handler("list_users")
async def _list_users(conn, params, ctx):
    try:
        limit = max(1, min(200, int(params.get("limit") or 100)))
    except (TypeError, ValueError):
        limit = 100
    resp = await _client(conn).users_list(limit=limit)
    users = [{
        "id": u.get("id"),
        "name": u.get("name"),
        "real_name": (u.get("profile") or {}).get("real_name"),
        "bot": bool(u.get("is_bot")),
        "deleted": bool(u.get("deleted")),
    } for u in resp.get("members", [])]
    return _ok(users=users, count=len(users))


# -- files ------------------------------------------------------------------

@_handler("upload_file")
async def _upload_file(conn, params, ctx):
    path = str(params.get("path") or "")
    if not path or not os.path.isfile(path):
        return _err(f"File not found: {path!r}")
    channel, err = _resolve_channel(conn, ctx, params)
    if err:
        return err
    resp = await _client(conn).files_upload_v2(
        channel=channel,
        file=path,
        title=params.get("title") or None,
        initial_comment=params.get("caption") or None,
    )
    f = resp.get("file", {}) or {}
    return _ok(file_id=f.get("id"), title=f.get("title"), channel_id=channel)


# -- workspace --------------------------------------------------------------

@_handler("get_workspace_info")
async def _get_workspace_info(conn, params, ctx):
    resp = await _client(conn).team_info()
    team = resp.get("team", {})
    return _ok(workspace={
        "id": team.get("id"),
        "name": team.get("name"),
        "domain": team.get("domain"),
        "bot_user_id": getattr(conn, "_bot_user_id", None),
    })


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live SlackConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    if _client(conn) is None:
        return _err("Slack client is not running.")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        # slack_sdk raises SlackApiError with the API error code in the message
        logger.exception("Slack action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in SLACK_ACTIONS if a.id not in _HANDLERS]

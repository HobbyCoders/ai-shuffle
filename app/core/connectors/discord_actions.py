"""Discord action catalog + handlers for the connector action gateway.

Every capability the bot can exercise is declared here as an ``ActionDef``
(id, JSON schema, category, required Discord permission, risk tier) with a
matching handler. The AI reaches these through exactly two MCP tools —
``discord_actions`` (discover) and ``discord_invoke`` (execute) — so adding a
capability means adding one catalog entry and one handler, never a new tool.

Scoping rules (enforced here, server-side):
    * Message-level actions (send/read/pin/react/purge/...) may only target the
      session's bound channel, the connector's ``allowed_channel_ids``, or a
      thread whose parent is one of those.
    * Guild-level actions (roles, moderation, channel management, ...) operate
      on the guild containing the bound channel only.
    * ``required_permission`` is checked against the bot's live guild
      permissions before dispatch (and reflected in discovery output).
    * ``risk == "destructive"`` actions additionally require in-channel
      Approve/Deny confirmation (see DiscordConnector.request_action_approval).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from app.core.connectors.base import ActionDef, chunk_text

logger = logging.getLogger(__name__)

try:
    import discord

    DISCORD_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without discord.py
    DISCORD_AVAILABLE = False


# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

_CHANNEL_PROP = {
    "channel": {
        "type": "string",
        "description": (
            "Optional channel ID to target instead of the bound channel. Must "
            "be an allowed channel (or a thread inside one) or the call is "
            "rejected."
        ),
    },
}

_USER_ID = {"type": "string", "description": "Discord user ID (snowflake)."}
_ROLE_ID = {"type": "string", "description": "Discord role ID (snowflake)."}
_REASON = {"type": "string", "description": "Optional audit-log reason."}


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

DISCORD_ACTIONS: List[ActionDef] = [
    # -- messages ----------------------------------------------------------
    ActionDef(
        id="send_message",
        name="Send Message",
        description="Send a plain text message. Long text is chunked automatically.",
        input_schema=_schema({
            "text": {"type": "string"},
            "reply_to_message_id": {"type": "string", "description": "Optional message to reply to."},
            "platform_chat_id": {"type": "string", "description": "Legacy alias for channel."},
            **_CHANNEL_PROP,
        }, ["text"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="send_embed",
        name="Send Embed",
        description="Send a rich embed (title/description/color/fields/footer). Prefer over plain text for structured info.",
        input_schema=_schema({
            "title": {"type": "string"},
            "description": {"type": "string"},
            "color": {"type": "string", "description": "Hex color like '#5865F2'."},
            "fields": {"type": "array", "description": "List of {name, value, inline} objects (max 25)."},
            "footer": {"type": "string"},
            **_CHANNEL_PROP,
        }),
        category="messages", required_permission="embed_links", risk="write",
    ),
    ActionDef(
        id="send_file",
        name="Send File",
        description="Upload a file from the local filesystem to the channel.",
        input_schema=_schema({
            "path": {"type": "string", "description": "Local filesystem path of the file."},
            "filename": {"type": "string", "description": "Optional name shown in Discord."},
            "caption": {"type": "string", "description": "Optional message text sent with the file."},
            **_CHANNEL_PROP,
        }, ["path"]),
        category="messages", required_permission="attach_files", risk="write",
    ),
    ActionDef(
        id="create_poll",
        name="Create Poll",
        description="Create a NATIVE Discord poll (2-10 answers). Prefer over text-listing options for votes.",
        input_schema=_schema({
            "question": {"type": "string", "description": "Poll question (max 300 chars)."},
            "answers": {"type": "array", "description": "2 to 10 answer strings."},
            "duration_hours": {"type": "number", "minimum": 1, "maximum": 768},
            "allow_multiselect": {"type": "boolean"},
            **_CHANNEL_PROP,
        }, ["question", "answers"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="read_messages",
        name="Read Messages",
        description="Read recent messages (newest first) for conversation context.",
        input_schema=_schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "before_message_id": {"type": "string", "description": "Only messages before this one (for paging back)."},
            **_CHANNEL_PROP,
        }),
        category="messages", required_permission="read_message_history", risk="safe",
    ),
    ActionDef(
        id="edit_message",
        name="Edit Message",
        description="Edit a message previously sent by this bot.",
        input_schema=_schema({
            "message_id": {"type": "string"},
            "text": {"type": "string"},
            **_CHANNEL_PROP,
        }, ["message_id", "text"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="delete_message",
        name="Delete Message",
        description="Delete a single message.",
        input_schema=_schema({
            "message_id": {"type": "string"},
            **_CHANNEL_PROP,
        }, ["message_id"]),
        category="messages", required_permission="manage_messages", risk="destructive",
    ),
    ActionDef(
        id="pin_message",
        name="Pin Message",
        description="Pin a message in the channel.",
        input_schema=_schema({"message_id": {"type": "string"}, **_CHANNEL_PROP}, ["message_id"]),
        category="messages", required_permission="manage_messages", risk="write",
    ),
    ActionDef(
        id="unpin_message",
        name="Unpin Message",
        description="Unpin a message in the channel.",
        input_schema=_schema({"message_id": {"type": "string"}, **_CHANNEL_PROP}, ["message_id"]),
        category="messages", required_permission="manage_messages", risk="write",
    ),
    ActionDef(
        id="list_pins",
        name="List Pinned Messages",
        description="List the channel's pinned messages.",
        input_schema=_schema({**_CHANNEL_PROP}),
        category="messages", risk="safe",
    ),
    # -- reactions ---------------------------------------------------------
    ActionDef(
        id="add_reaction",
        name="Add Reaction",
        description="React to a message with a unicode emoji.",
        input_schema=_schema({
            "message_id": {"type": "string"},
            "emoji": {"type": "string", "description": "Unicode emoji, e.g. '\U0001F44D'."},
            **_CHANNEL_PROP,
        }, ["message_id", "emoji"]),
        category="reactions", required_permission="add_reactions", risk="write",
    ),
    ActionDef(
        id="remove_reaction",
        name="Remove Own Reaction",
        description="Remove this bot's own reaction from a message.",
        input_schema=_schema({
            "message_id": {"type": "string"},
            "emoji": {"type": "string"},
            **_CHANNEL_PROP,
        }, ["message_id", "emoji"]),
        category="reactions", risk="write",
    ),
    ActionDef(
        id="clear_reactions",
        name="Clear All Reactions",
        description="Remove ALL reactions from a message.",
        input_schema=_schema({"message_id": {"type": "string"}, **_CHANNEL_PROP}, ["message_id"]),
        category="reactions", required_permission="manage_messages", risk="destructive",
    ),
    # -- threads -----------------------------------------------------------
    ActionDef(
        id="create_thread",
        name="Create Thread",
        description="Create a thread, optionally branched off a message. Prefer threads for long tangents.",
        input_schema=_schema({
            "name": {"type": "string", "description": "Thread name (max 100 chars)."},
            "message_id": {"type": "string", "description": "Optional message to branch from."},
            **_CHANNEL_PROP,
        }, ["name"]),
        category="threads", required_permission="create_public_threads", risk="write",
    ),
    ActionDef(
        id="list_threads",
        name="List Threads",
        description="List active threads of the channel.",
        input_schema=_schema({**_CHANNEL_PROP}),
        category="threads", risk="safe",
    ),
    ActionDef(
        id="edit_thread",
        name="Edit Thread",
        description="Rename, archive/unarchive, or lock/unlock a thread.",
        input_schema=_schema({
            "thread_id": {"type": "string"},
            "name": {"type": "string"},
            "archived": {"type": "boolean"},
            "locked": {"type": "boolean"},
        }, ["thread_id"]),
        category="threads", required_permission="manage_threads", risk="write",
    ),
    # -- channels ----------------------------------------------------------
    ActionDef(
        id="list_channels",
        name="List Channels",
        description="List the server's channels and categories.",
        input_schema=_schema({}),
        category="channels", risk="safe",
    ),
    ActionDef(
        id="get_channel_info",
        name="Get Channel Info",
        description="Details for one channel (topic, slowmode, category, NSFW).",
        input_schema=_schema({"channel_id": {"type": "string"}}, ["channel_id"]),
        category="channels", risk="safe",
    ),
    ActionDef(
        id="create_channel",
        name="Create Channel",
        description="Create a text/voice/category channel in the server.",
        input_schema=_schema({
            "name": {"type": "string"},
            "type": {"type": "string", "enum": ["text", "voice", "category"]},
            "category_id": {"type": "string", "description": "Optional parent category."},
            "topic": {"type": "string"},
        }, ["name"]),
        category="channels", required_permission="manage_channels", risk="write",
    ),
    ActionDef(
        id="edit_channel",
        name="Edit Channel",
        description="Change a channel's name, topic, slowmode, or NSFW flag.",
        input_schema=_schema({
            "channel_id": {"type": "string"},
            "name": {"type": "string"},
            "topic": {"type": "string"},
            "slowmode_seconds": {"type": "integer", "minimum": 0, "maximum": 21600},
            "nsfw": {"type": "boolean"},
        }, ["channel_id"]),
        category="channels", required_permission="manage_channels", risk="write",
    ),
    ActionDef(
        id="delete_channel",
        name="Delete Channel",
        description="Permanently delete a channel in the server.",
        input_schema=_schema({
            "channel_id": {"type": "string"},
            "reason": _REASON,
        }, ["channel_id"]),
        category="channels", required_permission="manage_channels", risk="destructive",
    ),
    # -- roles -------------------------------------------------------------
    ActionDef(
        id="list_roles",
        name="List Roles",
        description="List the server's roles.",
        input_schema=_schema({}),
        category="roles", risk="safe",
    ),
    ActionDef(
        id="create_role",
        name="Create Role",
        description="Create a role. permissions is a list of Discord permission flag names.",
        input_schema=_schema({
            "name": {"type": "string"},
            "color": {"type": "string", "description": "Hex color like '#99AAB5'."},
            "permissions": {"type": "array", "description": "Permission flag names, e.g. ['send_messages']."},
            "hoist": {"type": "boolean", "description": "Show role members separately."},
            "mentionable": {"type": "boolean"},
        }, ["name"]),
        category="roles", required_permission="manage_roles", risk="write",
    ),
    ActionDef(
        id="edit_role",
        name="Edit Role",
        description="Change a role's name, color, hoist, or mentionable flag.",
        input_schema=_schema({
            "role_id": _ROLE_ID,
            "name": {"type": "string"},
            "color": {"type": "string"},
            "hoist": {"type": "boolean"},
            "mentionable": {"type": "boolean"},
        }, ["role_id"]),
        category="roles", required_permission="manage_roles", risk="write",
    ),
    ActionDef(
        id="delete_role",
        name="Delete Role",
        description="Permanently delete a role.",
        input_schema=_schema({"role_id": _ROLE_ID, "reason": _REASON}, ["role_id"]),
        category="roles", required_permission="manage_roles", risk="destructive",
    ),
    ActionDef(
        id="add_role",
        name="Add Role To Member",
        description="Give a member a role.",
        input_schema=_schema({
            "user_id": _USER_ID, "role_id": _ROLE_ID, "reason": _REASON,
        }, ["user_id", "role_id"]),
        category="roles", required_permission="manage_roles", risk="write",
    ),
    ActionDef(
        id="remove_role",
        name="Remove Role From Member",
        description="Take a role away from a member.",
        input_schema=_schema({
            "user_id": _USER_ID, "role_id": _ROLE_ID, "reason": _REASON,
        }, ["user_id", "role_id"]),
        category="roles", required_permission="manage_roles", risk="write",
    ),
    # -- members -----------------------------------------------------------
    ActionDef(
        id="get_member",
        name="Get Member Info",
        description="Fetch one member's profile (roles, join date, timeout state).",
        input_schema=_schema({"user_id": _USER_ID}, ["user_id"]),
        category="members", risk="safe",
    ),
    ActionDef(
        id="search_members",
        name="Search Members",
        description="Search server members by name prefix.",
        input_schema=_schema({
            "query": {"type": "string", "description": "Username/nickname prefix."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
        }, ["query"]),
        category="members", risk="safe",
    ),
    ActionDef(
        id="set_nickname",
        name="Set Nickname",
        description="Change a member's server nickname (empty string clears it).",
        input_schema=_schema({
            "user_id": _USER_ID,
            "nickname": {"type": "string"},
            "reason": _REASON,
        }, ["user_id", "nickname"]),
        category="members", required_permission="manage_nicknames", risk="write",
    ),
    # -- moderation --------------------------------------------------------
    ActionDef(
        id="kick_member",
        name="Kick Member",
        description="Kick a member from the server.",
        input_schema=_schema({"user_id": _USER_ID, "reason": _REASON}, ["user_id"]),
        category="moderation", required_permission="kick_members", risk="destructive",
    ),
    ActionDef(
        id="ban_member",
        name="Ban Member",
        description="Ban a user from the server, optionally deleting their recent messages.",
        input_schema=_schema({
            "user_id": _USER_ID,
            "reason": _REASON,
            "delete_message_days": {"type": "integer", "minimum": 0, "maximum": 7},
        }, ["user_id"]),
        category="moderation", required_permission="ban_members", risk="destructive",
    ),
    ActionDef(
        id="unban_member",
        name="Unban Member",
        description="Lift a user's ban.",
        input_schema=_schema({"user_id": _USER_ID, "reason": _REASON}, ["user_id"]),
        category="moderation", required_permission="ban_members", risk="write",
    ),
    ActionDef(
        id="timeout_member",
        name="Timeout Member",
        description="Temporarily mute a member (Discord timeout).",
        input_schema=_schema({
            "user_id": _USER_ID,
            "minutes": {"type": "number", "minimum": 1, "maximum": 40320},
            "reason": _REASON,
        }, ["user_id", "minutes"]),
        category="moderation", required_permission="moderate_members", risk="destructive",
    ),
    ActionDef(
        id="remove_timeout",
        name="Remove Timeout",
        description="Lift a member's timeout early.",
        input_schema=_schema({"user_id": _USER_ID, "reason": _REASON}, ["user_id"]),
        category="moderation", required_permission="moderate_members", risk="write",
    ),
    ActionDef(
        id="purge_messages",
        name="Purge Messages",
        description="Bulk-delete the channel's most recent messages.",
        input_schema=_schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            **_CHANNEL_PROP,
        }, ["limit"]),
        category="moderation", required_permission="manage_messages", risk="destructive",
    ),
    # -- webhooks ----------------------------------------------------------
    ActionDef(
        id="list_webhooks",
        name="List Webhooks",
        description="List the channel's webhooks (names and IDs only).",
        input_schema=_schema({**_CHANNEL_PROP}),
        category="webhooks", required_permission="manage_webhooks", risk="safe",
    ),
    ActionDef(
        id="create_webhook",
        name="Create Webhook",
        description="Create an incoming webhook on the channel. Returns the webhook URL — treat it as a secret.",
        input_schema=_schema({"name": {"type": "string"}, **_CHANNEL_PROP}, ["name"]),
        category="webhooks", required_permission="manage_webhooks", risk="destructive",
    ),
    ActionDef(
        id="delete_webhook",
        name="Delete Webhook",
        description="Delete a webhook by ID.",
        input_schema=_schema({"webhook_id": {"type": "string"}, **_CHANNEL_PROP}, ["webhook_id"]),
        category="webhooks", required_permission="manage_webhooks", risk="destructive",
    ),
    # -- invites -----------------------------------------------------------
    ActionDef(
        id="create_invite",
        name="Create Invite",
        description="Create an invite link for the channel.",
        input_schema=_schema({
            "max_age_hours": {"type": "number", "minimum": 0, "maximum": 168,
                              "description": "0 = never expires (default 24h)."},
            "max_uses": {"type": "integer", "minimum": 0, "maximum": 100,
                         "description": "0 = unlimited."},
            **_CHANNEL_PROP,
        }),
        category="invites", required_permission="create_instant_invite", risk="write",
    ),
    ActionDef(
        id="list_invites",
        name="List Invites",
        description="List the server's active invites.",
        input_schema=_schema({}),
        category="invites", required_permission="manage_guild", risk="safe",
    ),
    # -- emojis ------------------------------------------------------------
    ActionDef(
        id="list_emojis",
        name="List Custom Emojis",
        description="List the server's custom emojis.",
        input_schema=_schema({}),
        category="emojis", risk="safe",
    ),
    # -- events ------------------------------------------------------------
    ActionDef(
        id="list_scheduled_events",
        name="List Scheduled Events",
        description="List the server's scheduled events.",
        input_schema=_schema({}),
        category="events", risk="safe",
    ),
    ActionDef(
        id="create_scheduled_event",
        name="Create Scheduled Event",
        description=(
            "Create a scheduled event. Provide voice_channel_id for a voice "
            "event, or location + end_time for an external one. Times are ISO "
            "8601 (assumed UTC if no offset)."
        ),
        input_schema=_schema({
            "name": {"type": "string"},
            "start_time": {"type": "string", "description": "ISO 8601 start time."},
            "end_time": {"type": "string", "description": "ISO 8601 end time (required for external events)."},
            "description": {"type": "string"},
            "location": {"type": "string", "description": "External location (makes it an external event)."},
            "voice_channel_id": {"type": "string", "description": "Voice channel to host the event in."},
        }, ["name", "start_time"]),
        category="events", required_permission="manage_events", risk="write",
    ),
    ActionDef(
        id="delete_scheduled_event",
        name="Delete Scheduled Event",
        description="Delete a scheduled event by ID.",
        input_schema=_schema({"event_id": {"type": "string"}}, ["event_id"]),
        category="events", required_permission="manage_events", risk="destructive",
    ),
    # -- server ------------------------------------------------------------
    ActionDef(
        id="get_server_info",
        name="Get Server Info",
        description="Server overview: counts, owner, boost tier, and the bot's own permissions.",
        input_schema=_schema({}),
        category="server", risk="safe",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in DISCORD_ACTIONS}


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


def _allowed_channel_ids(conn: Any, ctx: Dict[str, Any]) -> Set[str]:
    """Channels this session may touch. Empty set = unrestricted (trusted
    non-gateway caller such as an automation)."""
    allowed: Set[str] = set()
    bound = str(ctx.get("bound_chat_id") or "")
    if bound:
        allowed.add(bound)
    raw = (conn.config or {}).get("allowed_channel_ids")
    if raw:
        if isinstance(raw, str):
            raw = [raw]
        allowed.update(str(c) for c in raw)
    # Only restrict when the call carries session context; automations and
    # API callers pass explicit targets and are already admin-gated.
    if not bound:
        return set()
    return allowed


async def _get_channel_obj(client: Any, channel_id: int) -> Optional[Any]:
    ch = client.get_channel(channel_id)
    if ch is None and hasattr(client, "fetch_channel"):
        try:
            ch = await client.fetch_channel(channel_id)
        except Exception:
            ch = None
    return ch


async def _resolve_channel(
    conn: Any, ctx: Dict[str, Any], params: Dict[str, Any]
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Resolve the message-level target channel, enforcing the allow-list.

    A thread is permitted when its parent channel is allowed.
    """
    client = getattr(conn, "_client", None)
    if client is None:
        return None, _err("Discord client is not running.")

    target = str(
        params.get("channel") or params.get("platform_chat_id")
        or ctx.get("bound_chat_id") or ""
    ).strip()
    if not target:
        return None, _err("No target channel — pass 'channel' or use a bound session.")

    try:
        channel = await _get_channel_obj(client, int(target))
    except (TypeError, ValueError):
        return None, _err(f"Invalid channel id: {target!r}")
    if channel is None:
        return None, _err(f"Channel {target} not found — the bot may lack access.")

    allowed = _allowed_channel_ids(conn, ctx)
    if allowed:
        parent_id = str(getattr(channel, "parent_id", "") or "")
        if str(channel.id) not in allowed and parent_id not in allowed:
            return None, _err(
                f"Channel {target} is not permitted for this connector session. "
                "Only the bound channel, the connector's allowed channels, or "
                "their threads may be used."
            )
    return channel, None


async def _resolve_guild(
    conn: Any, ctx: Dict[str, Any]
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Resolve the guild containing the session's bound channel."""
    channel, err = await _resolve_channel(conn, ctx, {})
    if err:
        return None, err
    guild = getattr(channel, "guild", None)
    if guild is None:
        return None, _err("This action requires a server (guild) channel, not a DM.")
    return guild, None


async def _fetch_message(channel: Any, message_id: Any) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    try:
        mid = int(str(message_id).strip())
    except (TypeError, ValueError):
        return None, _err(f"Invalid message id: {message_id!r}")
    try:
        return await channel.fetch_message(mid), None
    except Exception as e:
        return None, _err(f"Message {message_id} not found: {e}")


def _parse_iso_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def bot_guild_permissions(conn: Any, guild: Any) -> Optional[Any]:
    """The bot's own permissions in a guild, or None when unknown."""
    me = getattr(guild, "me", None)
    return getattr(me, "guild_permissions", None) if me is not None else None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

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
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    reference = None
    reply_to = params.get("reply_to_message_id")
    if reply_to:
        msg, m_err = await _fetch_message(channel, reply_to)
        if m_err:
            return m_err
        reference = msg
    ids = []
    for chunk in chunk_text(text, conn.MAX_MESSAGE_LENGTH or 2000):
        sent = await channel.send(chunk, reference=reference)
        reference = None  # only the first chunk replies
        ids.append(str(sent.id))
    return _ok(message_ids=ids, channel_id=str(channel.id))


@_handler("send_embed")
async def _send_embed(conn, params, ctx):
    title = str(params.get("title") or "").strip()
    description = str(params.get("description") or "")
    if not title and not description:
        return _err("At least one of title or description is required.")
    color = None
    if params.get("color"):
        try:
            color = discord.Color.from_str(str(params["color"]))
        except Exception:
            return _err(f"Invalid color {params['color']!r}. Use hex like '#5865F2'.")
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    embed = discord.Embed(
        title=(title[:256] or None),
        description=(description[:4096] or None),
        color=color,
    )
    fields = params.get("fields") or []
    if isinstance(fields, list):
        for f in fields[:25]:
            if isinstance(f, dict):
                embed.add_field(
                    name=(str(f.get("name") or "")[:256] or "​"),
                    value=(str(f.get("value") or "")[:1024] or "​"),
                    inline=bool(f.get("inline")),
                )
    if params.get("footer"):
        embed.set_footer(text=str(params["footer"])[:2048])
    msg = await channel.send(embed=embed)
    return _ok(message_id=str(msg.id), channel_id=str(channel.id))


@_handler("send_file")
async def _send_file(conn, params, ctx):
    path = str(params.get("path") or "")
    if not path or not os.path.isfile(path):
        return _err(f"File not found: {path!r}")
    if os.path.getsize(path) > 25 * 1024 * 1024:
        return _err("File exceeds Discord's 25 MB upload limit.")
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    file = discord.File(path, filename=params.get("filename") or None)
    msg = await channel.send(content=(params.get("caption") or None), file=file)
    return _ok(message_id=str(msg.id), channel_id=str(channel.id))


@_handler("create_poll")
async def _create_poll(conn, params, ctx):
    question = str(params.get("question") or "").strip()
    answers = params.get("answers")
    if not question:
        return _err("question is required.")
    if not isinstance(answers, list) or not (2 <= len(answers) <= 10):
        return _err("answers must be a list of 2 to 10 options.")
    try:
        duration_hours = float(params.get("duration_hours") or 24)
    except (TypeError, ValueError):
        return _err("duration_hours must be a number.")
    duration_hours = max(1.0, min(768.0, duration_hours))
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    poll = discord.Poll(
        question=question[:300],
        duration=timedelta(hours=duration_hours),
        multiple=bool(params.get("allow_multiselect")),
    )
    for a in answers:
        poll.add_answer(text=str(a)[:55])
    msg = await channel.send(poll=poll)
    return _ok(message_id=str(msg.id), channel_id=str(channel.id), answer_count=len(answers))


@_handler("read_messages")
async def _read_messages(conn, params, ctx):
    try:
        limit = int(params.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(50, limit))
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    kwargs: Dict[str, Any] = {"limit": limit}
    before = params.get("before_message_id")
    if before:
        try:
            kwargs["before"] = discord.Object(id=int(str(before)))
        except (TypeError, ValueError):
            return _err(f"Invalid before_message_id: {before!r}")
    out = []
    async for m in channel.history(**kwargs):
        author = getattr(m, "author", None)
        out.append({
            "id": str(m.id),
            "author": getattr(author, "display_name", None) or str(author),
            "author_id": str(getattr(author, "id", "")),
            "bot": bool(getattr(author, "bot", False)),
            "timestamp": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
            "content": (m.content or "")[:500],
        })
    return _ok(messages=out, count=len(out), channel_id=str(channel.id))


@_handler("edit_message")
async def _edit_message(conn, params, ctx):
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    msg, m_err = await _fetch_message(channel, params.get("message_id"))
    if m_err:
        return m_err
    bot_user = getattr(getattr(conn, "_client", None), "user", None)
    if bot_user is None or getattr(msg.author, "id", None) != bot_user.id:
        return _err("Can only edit messages sent by this bot.")
    await msg.edit(content=str(params.get("text") or "")[: conn.MAX_MESSAGE_LENGTH or 2000])
    return _ok(message_id=str(msg.id))


@_handler("delete_message")
async def _delete_message(conn, params, ctx):
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    msg, m_err = await _fetch_message(channel, params.get("message_id"))
    if m_err:
        return m_err
    await msg.delete()
    return _ok(deleted_message_id=str(msg.id))


@_handler("pin_message")
async def _pin_message(conn, params, ctx):
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    msg, m_err = await _fetch_message(channel, params.get("message_id"))
    if m_err:
        return m_err
    await msg.pin()
    return _ok(message_id=str(msg.id), pinned=True)


@_handler("unpin_message")
async def _unpin_message(conn, params, ctx):
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    msg, m_err = await _fetch_message(channel, params.get("message_id"))
    if m_err:
        return m_err
    await msg.unpin()
    return _ok(message_id=str(msg.id), pinned=False)


@_handler("list_pins")
async def _list_pins(conn, params, ctx):
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    # channel.pins() is a coroutine -> List[Message] on older discord.py and an
    # async iterator on newer releases; support both.
    result = channel.pins()
    if hasattr(result, "__aiter__"):
        pins = [m async for m in result]
    else:
        pins = await result
    return _ok(pins=[{
        "id": str(m.id),
        "author": str(getattr(m, "author", "")),
        "content": (m.content or "")[:300],
    } for m in pins], count=len(pins))


# -- reactions --------------------------------------------------------------

@_handler("add_reaction")
async def _add_reaction(conn, params, ctx):
    emoji = str(params.get("emoji") or "").strip()
    if not emoji:
        return _err("emoji is required.")
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    msg, m_err = await _fetch_message(channel, params.get("message_id"))
    if m_err:
        return m_err
    await msg.add_reaction(emoji)
    return _ok(message_id=str(msg.id), emoji=emoji)


@_handler("remove_reaction")
async def _remove_reaction(conn, params, ctx):
    emoji = str(params.get("emoji") or "").strip()
    if not emoji:
        return _err("emoji is required.")
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    msg, m_err = await _fetch_message(channel, params.get("message_id"))
    if m_err:
        return m_err
    bot_user = getattr(getattr(conn, "_client", None), "user", None)
    await msg.remove_reaction(emoji, bot_user)
    return _ok(message_id=str(msg.id), emoji=emoji, removed=True)


@_handler("clear_reactions")
async def _clear_reactions(conn, params, ctx):
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    msg, m_err = await _fetch_message(channel, params.get("message_id"))
    if m_err:
        return m_err
    await msg.clear_reactions()
    return _ok(message_id=str(msg.id), cleared=True)


# -- threads ----------------------------------------------------------------

@_handler("create_thread")
async def _create_thread(conn, params, ctx):
    name = str(params.get("name") or "").strip()
    if not name:
        return _err("name is required.")
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    message_id = params.get("message_id")
    if message_id:
        msg, m_err = await _fetch_message(channel, message_id)
        if m_err:
            return m_err
        thread = await msg.create_thread(name=name[:100])
    else:
        thread = await channel.create_thread(
            name=name[:100], type=discord.ChannelType.public_thread
        )
    return _ok(thread_id=str(thread.id), name=name[:100], channel_id=str(channel.id))


@_handler("list_threads")
async def _list_threads(conn, params, ctx):
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    threads = list(getattr(channel, "threads", []) or [])
    return _ok(threads=[{
        "id": str(t.id),
        "name": t.name,
        "archived": bool(getattr(t, "archived", False)),
        "locked": bool(getattr(t, "locked", False)),
        "message_count": getattr(t, "message_count", None),
    } for t in threads], count=len(threads))


@_handler("edit_thread")
async def _edit_thread(conn, params, ctx):
    client = getattr(conn, "_client", None)
    try:
        thread = await _get_channel_obj(client, int(str(params.get("thread_id"))))
    except (TypeError, ValueError):
        return _err(f"Invalid thread id: {params.get('thread_id')!r}")
    if thread is None or not hasattr(thread, "parent_id"):
        return _err("Thread not found.")
    allowed = _allowed_channel_ids(conn, ctx)
    if allowed and str(thread.parent_id) not in allowed:
        return _err("Thread's parent channel is not permitted for this session.")
    kwargs: Dict[str, Any] = {}
    if params.get("name") is not None:
        kwargs["name"] = str(params["name"])[:100]
    if params.get("archived") is not None:
        kwargs["archived"] = bool(params["archived"])
    if params.get("locked") is not None:
        kwargs["locked"] = bool(params["locked"])
    if not kwargs:
        return _err("Nothing to change — pass name, archived, or locked.")
    await thread.edit(**kwargs)
    return _ok(thread_id=str(thread.id), **{k: v for k, v in kwargs.items()})


# -- channels ---------------------------------------------------------------

@_handler("list_channels")
async def _list_channels(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    return _ok(channels=[{
        "id": str(c.id),
        "name": c.name,
        "type": str(getattr(c, "type", "")),
        "category": getattr(getattr(c, "category", None), "name", None),
        "position": getattr(c, "position", None),
    } for c in guild.channels], count=len(guild.channels))


@_handler("get_channel_info")
async def _get_channel_info(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    try:
        channel = guild.get_channel(int(str(params.get("channel_id"))))
    except (TypeError, ValueError):
        return _err(f"Invalid channel id: {params.get('channel_id')!r}")
    if channel is None:
        return _err("Channel not found in this server.")
    return _ok(channel={
        "id": str(channel.id),
        "name": channel.name,
        "type": str(getattr(channel, "type", "")),
        "topic": getattr(channel, "topic", None),
        "nsfw": bool(getattr(channel, "nsfw", False)),
        "slowmode_seconds": getattr(channel, "slowmode_delay", None),
        "category": getattr(getattr(channel, "category", None), "name", None),
        "created_at": channel.created_at.isoformat() if getattr(channel, "created_at", None) else None,
    })


@_handler("create_channel")
async def _create_channel(conn, params, ctx):
    name = str(params.get("name") or "").strip()
    if not name:
        return _err("name is required.")
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    category = None
    if params.get("category_id"):
        try:
            category = guild.get_channel(int(str(params["category_id"])))
        except (TypeError, ValueError):
            return _err(f"Invalid category id: {params['category_id']!r}")
        if category is None:
            return _err("Category not found in this server.")
    ctype = str(params.get("type") or "text")
    if ctype == "voice":
        channel = await guild.create_voice_channel(name, category=category)
    elif ctype == "category":
        channel = await guild.create_category(name)
    else:
        channel = await guild.create_text_channel(
            name, category=category, topic=params.get("topic") or None
        )
    return _ok(channel_id=str(channel.id), name=channel.name, type=ctype)


@_handler("edit_channel")
async def _edit_channel(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    try:
        channel = guild.get_channel(int(str(params.get("channel_id"))))
    except (TypeError, ValueError):
        return _err(f"Invalid channel id: {params.get('channel_id')!r}")
    if channel is None:
        return _err("Channel not found in this server.")
    kwargs: Dict[str, Any] = {}
    if params.get("name") is not None:
        kwargs["name"] = str(params["name"])
    if params.get("topic") is not None:
        kwargs["topic"] = str(params["topic"])
    if params.get("slowmode_seconds") is not None:
        kwargs["slowmode_delay"] = int(params["slowmode_seconds"])
    if params.get("nsfw") is not None:
        kwargs["nsfw"] = bool(params["nsfw"])
    if not kwargs:
        return _err("Nothing to change.")
    await channel.edit(**kwargs)
    return _ok(channel_id=str(channel.id), changed=sorted(kwargs))


@_handler("delete_channel")
async def _delete_channel(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    raw_id = str(params.get("channel_id") or "")
    try:
        channel_id = int(raw_id)
    except (TypeError, ValueError):
        return _err(f"Invalid channel id: {raw_id!r}")
    # Compare the normalised id: " <id>" and "0<id>" name the same channel.
    if str(channel_id) == str(ctx.get("bound_chat_id") or ""):
        return _err("Refusing to delete the channel this session is bound to.")
    channel = guild.get_channel(channel_id)
    if channel is None:
        return _err("Channel not found in this server.")
    await channel.delete(reason=params.get("reason") or None)
    return _ok(deleted_channel_id=str(channel_id), name=channel.name)


# -- roles ------------------------------------------------------------------

def _role_payload(r: Any) -> Dict[str, Any]:
    color = getattr(r, "color", None)
    return {
        "id": str(r.id),
        "name": r.name,
        "color": str(color) if color else None,
        "position": getattr(r, "position", None),
        "hoist": bool(getattr(r, "hoist", False)),
        "mentionable": bool(getattr(r, "mentionable", False)),
        "managed": bool(getattr(r, "managed", False)),
    }


@_handler("list_roles")
async def _list_roles(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    roles = sorted(guild.roles, key=lambda r: -getattr(r, "position", 0))
    return _ok(roles=[_role_payload(r) for r in roles], count=len(roles))


def _parse_role_kwargs(params: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    kwargs: Dict[str, Any] = {}
    if params.get("name") is not None:
        kwargs["name"] = str(params["name"])
    if params.get("color"):
        try:
            kwargs["colour"] = discord.Colour.from_str(str(params["color"]))
        except Exception:
            return {}, f"Invalid color {params['color']!r}."
    if params.get("hoist") is not None:
        kwargs["hoist"] = bool(params["hoist"])
    if params.get("mentionable") is not None:
        kwargs["mentionable"] = bool(params["mentionable"])
    return kwargs, None


@_handler("create_role")
async def _create_role(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    kwargs, k_err = _parse_role_kwargs(params)
    if k_err:
        return _err(k_err)
    if not kwargs.get("name"):
        return _err("name is required.")
    perm_names = params.get("permissions") or []
    if perm_names:
        valid = set(discord.Permissions.VALID_FLAGS)
        bad = [p for p in perm_names if str(p) not in valid]
        if bad:
            return _err(f"Unknown permission flag(s): {', '.join(map(str, bad))}")
        kwargs["permissions"] = discord.Permissions(**{str(p): True for p in perm_names})
    role = await guild.create_role(**kwargs)
    return _ok(role=_role_payload(role))


def _get_role(guild: Any, raw_id: Any) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    try:
        role = guild.get_role(int(str(raw_id)))
    except (TypeError, ValueError):
        return None, _err(f"Invalid role id: {raw_id!r}")
    if role is None:
        return None, _err("Role not found in this server.")
    return role, None


@_handler("edit_role")
async def _edit_role(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    role, r_err = _get_role(guild, params.get("role_id"))
    if r_err:
        return r_err
    kwargs, k_err = _parse_role_kwargs(params)
    if k_err:
        return _err(k_err)
    if not kwargs:
        return _err("Nothing to change.")
    await role.edit(**kwargs)
    return _ok(role=_role_payload(role))


@_handler("delete_role")
async def _delete_role(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    role, r_err = _get_role(guild, params.get("role_id"))
    if r_err:
        return r_err
    await role.delete(reason=params.get("reason") or None)
    return _ok(deleted_role_id=str(params.get("role_id")), name=role.name)


async def _fetch_member(guild: Any, raw_id: Any) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    try:
        uid = int(str(raw_id).strip())
    except (TypeError, ValueError):
        return None, _err(f"Invalid user id: {raw_id!r}")
    try:
        return await guild.fetch_member(uid), None
    except Exception as e:
        return None, _err(f"Member {raw_id} not found: {e}")


@_handler("add_role")
async def _add_role(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    role, r_err = _get_role(guild, params.get("role_id"))
    if r_err:
        return r_err
    member, m_err = await _fetch_member(guild, params.get("user_id"))
    if m_err:
        return m_err
    await member.add_roles(role, reason=params.get("reason") or None)
    return _ok(user_id=str(member.id), role=role.name, added=True)


@_handler("remove_role")
async def _remove_role(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    role, r_err = _get_role(guild, params.get("role_id"))
    if r_err:
        return r_err
    member, m_err = await _fetch_member(guild, params.get("user_id"))
    if m_err:
        return m_err
    await member.remove_roles(role, reason=params.get("reason") or None)
    return _ok(user_id=str(member.id), role=role.name, removed=True)


# -- members ----------------------------------------------------------------

def _member_payload(m: Any) -> Dict[str, Any]:
    return {
        "id": str(m.id),
        "name": str(m),
        "display_name": getattr(m, "display_name", None),
        "bot": bool(getattr(m, "bot", False)),
        "joined_at": m.joined_at.isoformat() if getattr(m, "joined_at", None) else None,
        "roles": [str(r.id) for r in getattr(m, "roles", [])],
        "timed_out_until": (
            m.timed_out_until.isoformat() if getattr(m, "timed_out_until", None) else None
        ),
    }


@_handler("get_member")
async def _get_member(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    member, m_err = await _fetch_member(guild, params.get("user_id"))
    if m_err:
        return m_err
    return _ok(member=_member_payload(member))


@_handler("search_members")
async def _search_members(conn, params, ctx):
    query = str(params.get("query") or "").strip()
    if not query:
        return _err("query is required.")
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    try:
        limit = max(1, min(25, int(params.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    members = await guild.query_members(query=query, limit=limit)
    return _ok(members=[_member_payload(m) for m in members], count=len(members))


@_handler("set_nickname")
async def _set_nickname(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    member, m_err = await _fetch_member(guild, params.get("user_id"))
    if m_err:
        return m_err
    nick = str(params.get("nickname") or "").strip() or None
    await member.edit(nick=nick, reason=params.get("reason") or None)
    return _ok(user_id=str(member.id), nickname=nick)


# -- moderation -------------------------------------------------------------

@_handler("kick_member")
async def _kick_member(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    member, m_err = await _fetch_member(guild, params.get("user_id"))
    if m_err:
        return m_err
    await guild.kick(member, reason=params.get("reason") or None)
    return _ok(kicked_user_id=str(member.id))


@_handler("ban_member")
async def _ban_member(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    try:
        uid = int(str(params.get("user_id")).strip())
    except (TypeError, ValueError):
        return _err(f"Invalid user id: {params.get('user_id')!r}")
    try:
        days = max(0, min(7, int(params.get("delete_message_days") or 0)))
    except (TypeError, ValueError):
        days = 0
    await guild.ban(
        discord.Object(id=uid),
        reason=params.get("reason") or None,
        delete_message_seconds=days * 86400,
    )
    return _ok(banned_user_id=str(uid))


@_handler("unban_member")
async def _unban_member(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    try:
        uid = int(str(params.get("user_id")).strip())
    except (TypeError, ValueError):
        return _err(f"Invalid user id: {params.get('user_id')!r}")
    await guild.unban(discord.Object(id=uid), reason=params.get("reason") or None)
    return _ok(unbanned_user_id=str(uid))


@_handler("timeout_member")
async def _timeout_member(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    member, m_err = await _fetch_member(guild, params.get("user_id"))
    if m_err:
        return m_err
    try:
        minutes = float(params.get("minutes") or 0)
    except (TypeError, ValueError):
        return _err("minutes must be a number.")
    if not (1 <= minutes <= 40320):  # Discord max: 28 days
        return _err("minutes must be between 1 and 40320 (28 days).")
    await member.timeout(timedelta(minutes=minutes), reason=params.get("reason") or None)
    return _ok(user_id=str(member.id), timeout_minutes=minutes)


@_handler("remove_timeout")
async def _remove_timeout(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    member, m_err = await _fetch_member(guild, params.get("user_id"))
    if m_err:
        return m_err
    await member.timeout(None, reason=params.get("reason") or None)
    return _ok(user_id=str(member.id), timeout_removed=True)


@_handler("purge_messages")
async def _purge_messages(conn, params, ctx):
    try:
        limit = int(params.get("limit"))
    except (TypeError, ValueError):
        return _err("limit is required and must be an integer.")
    limit = max(1, min(100, limit))
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    deleted = await channel.purge(limit=limit)
    return _ok(deleted_count=len(deleted), channel_id=str(channel.id))


# -- webhooks ---------------------------------------------------------------

@_handler("list_webhooks")
async def _list_webhooks(conn, params, ctx):
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    hooks = await channel.webhooks()
    return _ok(webhooks=[{"id": str(h.id), "name": h.name} for h in hooks], count=len(hooks))


@_handler("create_webhook")
async def _create_webhook(conn, params, ctx):
    name = str(params.get("name") or "").strip()
    if not name:
        return _err("name is required.")
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    hook = await channel.create_webhook(name=name[:80])
    return _ok(webhook_id=str(hook.id), name=hook.name, url=hook.url)


@_handler("delete_webhook")
async def _delete_webhook(conn, params, ctx):
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    raw_id = str(params.get("webhook_id") or "")
    hooks = await channel.webhooks()
    for h in hooks:
        if str(h.id) == raw_id:
            await h.delete()
            return _ok(deleted_webhook_id=raw_id)
    return _err(f"Webhook {raw_id} not found on this channel.")


# -- invites ----------------------------------------------------------------

@_handler("create_invite")
async def _create_invite(conn, params, ctx):
    channel, err = await _resolve_channel(conn, ctx, params)
    if err:
        return err
    try:
        max_age_hours = float(params.get("max_age_hours", 24))
        max_uses = int(params.get("max_uses") or 0)
    except (TypeError, ValueError):
        return _err("max_age_hours and max_uses must be numbers.")
    invite = await channel.create_invite(
        max_age=int(max_age_hours * 3600), max_uses=max_uses,
    )
    return _ok(url=invite.url, code=getattr(invite, "code", None))


@_handler("list_invites")
async def _list_invites(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    invites = await guild.invites()
    return _ok(invites=[{
        "code": getattr(i, "code", None),
        "url": i.url,
        "channel": getattr(getattr(i, "channel", None), "name", None),
        "uses": getattr(i, "uses", None),
        "max_uses": getattr(i, "max_uses", None),
    } for i in invites], count=len(invites))


# -- emojis -----------------------------------------------------------------

@_handler("list_emojis")
async def _list_emojis(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    return _ok(emojis=[{
        "id": str(e.id),
        "name": e.name,
        "animated": bool(getattr(e, "animated", False)),
        "usage": f"<{'a' if getattr(e, 'animated', False) else ''}:{e.name}:{e.id}>",
    } for e in guild.emojis], count=len(guild.emojis))


# -- events -----------------------------------------------------------------

@_handler("list_scheduled_events")
async def _list_scheduled_events(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    events = await guild.fetch_scheduled_events()
    return _ok(events=[{
        "id": str(ev.id),
        "name": ev.name,
        "description": getattr(ev, "description", None),
        "start_time": ev.start_time.isoformat() if getattr(ev, "start_time", None) else None,
        "end_time": ev.end_time.isoformat() if getattr(ev, "end_time", None) else None,
        "location": getattr(getattr(ev, "entity_metadata", None), "location", None),
        "status": str(getattr(ev, "status", "")),
    } for ev in events], count=len(events))


@_handler("create_scheduled_event")
async def _create_scheduled_event(conn, params, ctx):
    name = str(params.get("name") or "").strip()
    if not name:
        return _err("name is required.")
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    try:
        start_time = _parse_iso_utc(params.get("start_time"))
    except (TypeError, ValueError):
        return _err(f"Invalid start_time: {params.get('start_time')!r} (use ISO 8601).")
    kwargs: Dict[str, Any] = {
        "name": name[:100],
        "start_time": start_time,
        "privacy_level": discord.PrivacyLevel.guild_only,
    }
    if params.get("description"):
        kwargs["description"] = str(params["description"])[:1000]
    if params.get("end_time"):
        try:
            kwargs["end_time"] = _parse_iso_utc(params["end_time"])
        except (TypeError, ValueError):
            return _err(f"Invalid end_time: {params.get('end_time')!r}.")
    if params.get("voice_channel_id"):
        try:
            vc = guild.get_channel(int(str(params["voice_channel_id"])))
        except (TypeError, ValueError):
            return _err(f"Invalid voice channel id: {params['voice_channel_id']!r}")
        if vc is None:
            return _err("Voice channel not found in this server.")
        kwargs["channel"] = vc
        kwargs["entity_type"] = discord.EntityType.voice
    elif params.get("location"):
        if "end_time" not in kwargs:
            return _err("External events require end_time.")
        kwargs["location"] = str(params["location"])[:100]
        kwargs["entity_type"] = discord.EntityType.external
    else:
        return _err("Provide voice_channel_id or location.")
    ev = await guild.create_scheduled_event(**kwargs)
    return _ok(event_id=str(ev.id), name=ev.name)


@_handler("delete_scheduled_event")
async def _delete_scheduled_event(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    try:
        eid = int(str(params.get("event_id")).strip())
    except (TypeError, ValueError):
        return _err(f"Invalid event id: {params.get('event_id')!r}")
    try:
        ev = await guild.fetch_scheduled_event(eid)
    except Exception as e:
        return _err(f"Scheduled event {eid} not found: {e}")
    await ev.delete()
    return _ok(deleted_event_id=str(eid))


# -- server -----------------------------------------------------------------

@_handler("get_server_info")
async def _get_server_info(conn, params, ctx):
    guild, err = await _resolve_guild(conn, ctx)
    if err:
        return err
    perms = bot_guild_permissions(conn, guild)
    granted = sorted(
        name for name, value in (perms or []) if value
    ) if perms is not None else None
    me = getattr(guild, "me", None)
    return _ok(server={
        "id": str(guild.id),
        "name": guild.name,
        "description": getattr(guild, "description", None),
        "owner_id": str(getattr(guild, "owner_id", "")),
        "member_count": getattr(guild, "member_count", None),
        "created_at": guild.created_at.isoformat() if getattr(guild, "created_at", None) else None,
        "premium_tier": getattr(guild, "premium_tier", None),
        "channel_count": len(guild.channels),
        "role_count": len(guild.roles),
        "emoji_count": len(guild.emojis),
        "bot_top_role": getattr(getattr(me, "top_role", None), "name", None),
        "bot_permissions": granted,
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
    """Dispatch one catalog action against a live DiscordConnector."""
    if not DISCORD_AVAILABLE:
        return _err("discord.py is not installed.")
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    try:
        return await handler(conn, params or {}, context or {})
    except discord.Forbidden as e:
        return _err(
            f"Discord refused '{action_id}': the bot lacks permission "
            f"(check role hierarchy and channel overrides). {e}"
        )
    except discord.HTTPException as e:
        return _err(f"Discord API error running '{action_id}': {e}")
    except Exception as e:
        logger.exception("Discord action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    """Catalog entries without a handler — used by tests to keep them in sync."""
    return [a.id for a in DISCORD_ACTIONS if a.id not in _HANDLERS]

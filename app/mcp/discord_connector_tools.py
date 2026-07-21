"""Discord-native MCP tools for connector sessions.

These tools let a connector-session model behave like a native Discord
participant — create real polls, send rich embeds, react, open threads, and
read recent channel history — instead of being limited to text-in/text-out.

They are registered per AI session (only when that session is linked to a
Discord-type connector) and surfaced under the shared ``mcp__builtins__*``
namespace, following the same ``@tool`` / ``_ok`` / ``_err`` pattern as
``app.mcp.builtin_tools``.

Security rails (hard requirements):
    * The bot token and decrypted config are NEVER logged or returned.
    * Every tool resolves its target channel server-side and enforces a
      channel allow-list: the session's bound channel plus the connector's
      configured ``allowed_channel_ids``. An arbitrary channel id outside that
      set is rejected.
    * Connector/channel resolution is done fresh on EACH call (no stale
      binding) so a connector restart or channel change is always respected.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.mcp.config_helpers import _json_default

logger = logging.getLogger(__name__)


# ============================================================================
# Result helpers (mirrors app.mcp.builtin_tools so the envelope is identical)
# ============================================================================

def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    import json as _json
    return {"content": [{"type": "text", "text": _json.dumps({"success": True, **payload}, default=_json_default)}]}


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    import json as _json
    return {"content": [{"type": "text", "text": _json.dumps({"success": False, "error": message, **extra}, default=_json_default)}]}


# ============================================================================
# Call-time resolution: session_id -> bound Discord channel (allow-listed)
# ============================================================================

async def _resolve_channel(
    session_id: Optional[str], requested_channel: Optional[Any] = None
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Resolve the Discord channel a tool should operate on.

    Returns ``(channel, None)`` on success or ``(None, error_envelope)`` on
    failure. The resolution is intentionally re-done on every call:

        session_id
          -> connector_sessions linked to this AI session (discord one)
          -> connector_id + bound platform_chat_id
          -> running DiscordConnector instance on THIS install
          -> its live discord.py client
          -> the target channel object

    ``requested_channel`` (optional) lets a tool target a channel other than
    the bound one, but ONLY if it is in the connector's allow-list.
    """
    if not session_id:
        return None, _err("No session context — Discord tools require an active session.")

    from app.db import database as _db

    try:
        sessions = await asyncio.to_thread(
            _db.get_connector_sessions_for_ai_session, session_id
        )
    except Exception as e:
        return None, _err(f"Failed to resolve connector session: {e}")

    discord_cs: Optional[Dict[str, Any]] = None
    connector_id: Optional[str] = None
    for cs in sessions or []:
        cid = cs.get("connector_id")
        if not cid:
            continue
        try:
            row = await asyncio.to_thread(_db.get_connector, cid)
        except Exception:
            row = None
        if row and row.get("type") == "discord":
            discord_cs = cs
            connector_id = cid
            break

    if not discord_cs or not connector_id:
        return None, _err("This session is not linked to a Discord connector.")

    bound_chat = str(discord_cs.get("platform_chat_id") or "")
    if not bound_chat:
        return None, _err("Discord connector session has no bound channel.")

    from app.core.connectors.manager import connector_manager

    instance = connector_manager.get_running_instance(connector_id)
    client = getattr(instance, "_client", None) if instance is not None else None
    if instance is None or client is None:
        return None, _err("Discord client is not running on this device.")

    # Build the server-side allow-list: the bound channel is always permitted;
    # add any channels the connector was explicitly configured to serve. Only
    # allowed_channel_ids is read from the (already-decrypted, in-memory)
    # config — the bot token is never touched here.
    allowed = {bound_chat}
    try:
        raw_allowed = (instance.config or {}).get("allowed_channel_ids")
        if raw_allowed:
            if isinstance(raw_allowed, str):
                raw_allowed = [raw_allowed]
            allowed.update(str(c) for c in raw_allowed)
    except Exception:
        pass

    target = str(requested_channel).strip() if requested_channel else bound_chat
    if target not in allowed:
        return None, _err(
            f"Channel {target} is not permitted for this connector session. "
            "Only the bound channel or the connector's allowed channels may be used."
        )

    try:
        channel = client.get_channel(int(target))
    except (TypeError, ValueError):
        return None, _err(f"Invalid channel id: {target!r}")
    if channel is None:
        return None, _err(f"Channel {target} not found in cache — the bot may lack access.")

    return channel, None


# ============================================================================
# Tool factory
# ============================================================================

# Optional per-tool "channel" property, shared across schemas.
_CHANNEL_PROP = {
    "channel": {
        "type": "string",
        "description": (
            "Optional channel ID to target instead of the bound channel. Must be "
            "one of the connector's allowed channels, else the call is rejected."
        ),
    },
}


def build_discord_tools(session_id: Optional[str]) -> List[Any]:
    """Build the Discord-native ``@tool`` functions bound to ``session_id``.

    Returns a list of decorated tool callables (empty if the SDK is
    unavailable). Channel/connector resolution happens inside each tool at call
    time, so the returned closures hold no live Discord handles.
    """
    try:
        from claude_agent_sdk import tool
    except ImportError:  # pragma: no cover - SDK always present in app runtime
        logger.debug("claude_agent_sdk not available — Discord tools disabled")
        return []

    tools: List[Any] = []

    # ------------------------------------------------------------------
    # DiscordCreatePoll
    # ------------------------------------------------------------------
    @tool(
        "DiscordCreatePoll",
        "Create a NATIVE Discord poll in the bound channel. Prefer this over "
        "listing options as plain text when you want the user(s) to vote. "
        "answers must have 2-10 options.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Poll question (max 300 chars)."},
                "answers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2 to 10 answer options.",
                },
                "duration_hours": {"type": "number", "description": "How long the poll stays open (1-768h, default 24)."},
                "allow_multiselect": {"type": "boolean", "description": "Allow selecting multiple answers."},
                **_CHANNEL_PROP,
            },
            "required": ["question", "answers"],
        },
    )
    async def discord_create_poll(args: dict) -> dict:
        import discord

        question = (args.get("question") or "").strip()
        answers = args.get("answers")
        if not question:
            return _err("question is required.")
        if not isinstance(answers, list) or not (2 <= len(answers) <= 10):
            return _err("answers must be a list of 2 to 10 options.")
        try:
            duration_hours = float(args.get("duration_hours") or 24)
        except (TypeError, ValueError):
            return _err("duration_hours must be a number.")
        duration_hours = max(1.0, min(768.0, duration_hours))

        channel, err = await _resolve_channel(session_id, args.get("channel"))
        if err:
            return err
        try:
            poll = discord.Poll(
                question=question[:300],
                duration=timedelta(hours=duration_hours),
                multiple=bool(args.get("allow_multiselect")),
            )
            for a in answers:
                poll.add_answer(text=str(a)[:55])
            msg = await channel.send(poll=poll)
            return _ok({
                "message_id": str(msg.id),
                "channel_id": str(channel.id),
                "question": question,
                "answer_count": len(answers),
            })
        except Exception as e:
            logger.exception("DiscordCreatePoll failed")
            return _err(f"create poll failed: {e}")

    tools.append(discord_create_poll)

    # ------------------------------------------------------------------
    # DiscordSendEmbed
    # ------------------------------------------------------------------
    @tool(
        "DiscordSendEmbed",
        "Send a rich Discord embed to the bound channel. Prefer this over plain "
        "text when presenting structured information (fields, sections, a titled "
        "block). color is an optional hex string like '#5865F2'.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "color": {"type": "string", "description": "Optional hex color, e.g. '#5865F2'."},
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "value": {"type": "string"},
                            "inline": {"type": "boolean"},
                        },
                    },
                    "description": "Optional list of {name, value, inline} fields.",
                },
                "footer": {"type": "string", "description": "Optional footer text."},
                **_CHANNEL_PROP,
            },
            "required": [],
        },
    )
    async def discord_send_embed(args: dict) -> dict:
        import discord

        title = (args.get("title") or "").strip()
        description = args.get("description") or ""
        if not title and not description:
            return _err("At least one of title or description is required.")

        color = None
        raw_color = args.get("color")
        if raw_color:
            try:
                color = discord.Color.from_str(str(raw_color))
            except Exception:
                return _err(f"Invalid color {raw_color!r}. Use a hex string like '#5865F2'.")

        channel, err = await _resolve_channel(session_id, args.get("channel"))
        if err:
            return err
        try:
            embed = discord.Embed(
                title=(title[:256] or None),
                description=(str(description)[:4096] or None),
                color=color,
            )
            fields = args.get("fields") or []
            if isinstance(fields, list):
                for f in fields[:25]:
                    if not isinstance(f, dict):
                        continue
                    embed.add_field(
                        name=(str(f.get("name") or "")[:256] or "​"),
                        value=(str(f.get("value") or "")[:1024] or "​"),
                        inline=bool(f.get("inline")),
                    )
            footer = args.get("footer")
            if footer:
                embed.set_footer(text=str(footer)[:2048])
            msg = await channel.send(embed=embed)
            return _ok({"message_id": str(msg.id), "channel_id": str(channel.id)})
        except Exception as e:
            logger.exception("DiscordSendEmbed failed")
            return _err(f"send embed failed: {e}")

    tools.append(discord_send_embed)

    # ------------------------------------------------------------------
    # DiscordAddReaction
    # ------------------------------------------------------------------
    @tool(
        "DiscordAddReaction",
        "Add an emoji reaction to a message in the bound channel. emoji is a "
        "unicode emoji (e.g. '\U0001F44D').",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "emoji": {"type": "string", "description": "Unicode emoji to react with."},
                **_CHANNEL_PROP,
            },
            "required": ["message_id", "emoji"],
        },
    )
    async def discord_add_reaction(args: dict) -> dict:
        message_id = str(args.get("message_id") or "").strip()
        emoji = (args.get("emoji") or "").strip()
        if not message_id or not emoji:
            return _err("message_id and emoji are required.")

        channel, err = await _resolve_channel(session_id, args.get("channel"))
        if err:
            return err
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.add_reaction(emoji)
            return _ok({"message_id": message_id, "emoji": emoji, "channel_id": str(channel.id)})
        except (TypeError, ValueError):
            return _err(f"Invalid message id: {message_id!r}")
        except Exception as e:
            logger.exception("DiscordAddReaction failed")
            return _err(f"add reaction failed: {e}")

    tools.append(discord_add_reaction)

    # ------------------------------------------------------------------
    # DiscordCreateThread
    # ------------------------------------------------------------------
    @tool(
        "DiscordCreateThread",
        "Create a Discord thread in the bound channel. Pass message_id to branch "
        "the thread off an existing message, or omit it for a standalone channel "
        "thread. Prefer a thread for long tangents so the main channel stays clean.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Thread name (max 100 chars)."},
                "message_id": {"type": "string", "description": "Optional message to start the thread from."},
                **_CHANNEL_PROP,
            },
            "required": ["name"],
        },
    )
    async def discord_create_thread(args: dict) -> dict:
        import discord

        name = (args.get("name") or "").strip()
        if not name:
            return _err("name is required.")

        channel, err = await _resolve_channel(session_id, args.get("channel"))
        if err:
            return err
        message_id = str(args.get("message_id") or "").strip()
        try:
            if message_id:
                try:
                    msg = await channel.fetch_message(int(message_id))
                except (TypeError, ValueError):
                    return _err(f"Invalid message id: {message_id!r}")
                thread = await msg.create_thread(name=name[:100])
            else:
                thread = await channel.create_thread(
                    name=name[:100], type=discord.ChannelType.public_thread
                )
            return _ok({
                "thread_id": str(thread.id),
                "name": name[:100],
                "channel_id": str(channel.id),
            })
        except Exception as e:
            logger.exception("DiscordCreateThread failed")
            return _err(f"create thread failed: {e}")

    tools.append(discord_create_thread)

    # ------------------------------------------------------------------
    # DiscordReadMessages
    # ------------------------------------------------------------------
    @tool(
        "DiscordReadMessages",
        "Read recent messages from the bound channel to get conversation context "
        "beyond the current prompt. Returns newest-first, up to 50 messages.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many recent messages (1-50, default 20)."},
                **_CHANNEL_PROP,
            },
            "required": [],
        },
    )
    async def discord_read_messages(args: dict) -> dict:
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(50, limit))

        channel, err = await _resolve_channel(session_id, args.get("channel"))
        if err:
            return err
        try:
            out: List[Dict[str, Any]] = []
            async for m in channel.history(limit=limit):
                author = getattr(m, "author", None)
                content = m.content or ""
                out.append({
                    "id": str(m.id),
                    "author": getattr(author, "display_name", None) or str(author),
                    "bot": bool(getattr(author, "bot", False)),
                    "timestamp": m.created_at.isoformat() if getattr(m, "created_at", None) else None,
                    "content": content[:500],
                })
            return _ok({"messages": out, "count": len(out), "channel_id": str(channel.id)})
        except Exception as e:
            logger.exception("DiscordReadMessages failed")
            return _err(f"read messages failed: {e}")

    tools.append(discord_read_messages)

    return tools


def get_discord_connector_tool_names() -> List[str]:
    """MCP tool names for the Discord-native tools (for ``allowed_tools``).

    They live under the shared ``builtins`` server namespace. Adding them to
    allowed_tools unconditionally is safe: per-session REGISTRATION (only for
    Discord-linked sessions) is what actually gates availability.
    """
    return [
        "mcp__builtins__DiscordCreatePoll",
        "mcp__builtins__DiscordSendEmbed",
        "mcp__builtins__DiscordAddReaction",
        "mcp__builtins__DiscordCreateThread",
        "mcp__builtins__DiscordReadMessages",
    ]

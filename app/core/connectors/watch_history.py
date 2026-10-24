"""SDK-side history polling, shipped inside the isolated messaging connector packages."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


async def poll(
    kind: str, instance: Any, source: dict[str, Any], cursor: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    channel_id = str(source.get("channel_id", ""))
    allowed = instance.config.get(
        "allowed_chat_ids" if kind == "telegram" else "allowed_channel_ids"
    )
    if allowed and channel_id not in {str(item) for item in allowed}:
        raise PermissionError("Watch channel is no longer allowed by the connector")
    if kind == "slack":
        client = instance._web_client
        if client is None:
            raise ValueError("Slack is offline")
        if not cursor:
            return [], {"after": str(datetime.now(timezone.utc).timestamp())}
        args = dict(channel=channel_id, oldest=cursor["after"], inclusive=False, limit=100)
        if cursor.get("page"):
            args["cursor"] = cursor["page"]
        page = await client.conversations_history(**args)
        messages = page.get("messages", [])
        high = max([cursor.get("high", cursor["after"])] + [m["ts"] for m in messages])
        next_page = (page.get("response_metadata") or {}).get("next_cursor")
        if page.get("has_more") and not next_page:
            raise ValueError("Slack history is truncated without a replay cursor")
        checkpoint = (
            {"after": cursor["after"], "page": next_page, "high": high}
            if next_page else {"after": high}
        )
        return [
            ("message_received:" + channel_id + ":" + m["ts"], dict(
                event_type="message_received", message_id=m["ts"],
                platform_chat_id=channel_id, platform_user_id=m.get("user", ""),
                text=str(m.get("text", ""))[:12000],
            )) for m in messages
        ], checkpoint
    from discord import Object

    client = instance._client
    if client is None:
        raise ValueError("Discord is offline")
    channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
    guilds = instance.config.get("guild_ids")
    guild_id = str(getattr(getattr(channel, "guild", None), "id", ""))
    if guilds and guild_id not in {str(g) for g in guilds}:
        raise PermissionError("Watch guild is no longer allowed by the connector")
    if not cursor:
        # A snowflake boundary avoids a race between approval and the first message.
        boundary = (int(datetime.now(timezone.utc).timestamp() * 1000) - 1420070400000) << 22
        return [], {"after": str(boundary)}
    messages = [m async for m in channel.history(
        limit=100, after=Object(id=int(cursor["after"])), oldest_first=True
    )]
    return [
        ("message_received:" + channel_id + ":" + str(m.id), dict(
            event_type="message_received", message_id=str(m.id), platform_chat_id=channel_id,
            platform_user_id=str(m.author.id), text=str(m.content)[:12000],
        )) for m in messages
    ], {"after": str(messages[-1].id) if messages else cursor["after"]}

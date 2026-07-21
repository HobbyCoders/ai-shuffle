"""Unit tests for the Discord-native connector MCP tools.

These invoke each ``@tool`` handler directly (no MCP transport) with a mocked
discord.py client, covering: happy paths, channel-allow-list rejection, and the
client-not-running error. The discord.py classes (Poll, Embed, Color) construct
offline, so only the channel/message/client objects are faked — the only awaited
network calls are on those fakes.
"""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.discord_connector_tools import (
    build_discord_tools,
    get_discord_connector_tool_names,
)

BOUND_CHAT = 1472821837229723766
EXTRA_CHAT = 2222222222222222222
FORBIDDEN_CHAT = 9999999999999999999


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, mid=555, content="hi", author_name="Alice", bot=False):
        self.id = mid
        self.content = content
        self.created_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
        self.author = MagicMock()
        self.author.display_name = author_name
        self.author.bot = bot
        self.add_reaction = AsyncMock()
        self.create_thread = AsyncMock(return_value=MagicMock(id=7001))


class FakeChannel:
    def __init__(self, cid=BOUND_CHAT, history_msgs=None):
        self.id = cid
        self._history = history_msgs or []
        self.send = AsyncMock(return_value=MagicMock(id=999))
        self.fetch_message = AsyncMock(return_value=FakeMessage())
        self.create_thread = AsyncMock(return_value=MagicMock(id=8002))

    def history(self, limit=100):
        msgs = list(self._history)[:limit]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()


class FakeClient:
    def __init__(self, channels):
        if isinstance(channels, FakeChannel):
            channels = {channels.id: channels}
        self._channels = {int(k): v for k, v in channels.items()}

    def get_channel(self, cid):
        return self._channels.get(int(cid))


class FakeInstance:
    def __init__(self, client, allowed=None):
        self._client = client
        self.config = {"allowed_channel_ids": allowed or []}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _handlers(session_id="ai-1"):
    return {t.name: t.handler for t in build_discord_tools(session_id)}


def _payload(result):
    return json.loads(result["content"][0]["text"])


@contextmanager
def _patched(instance, bound_chat=BOUND_CHAT, *, connector_type="discord", running=True):
    sessions = [{"connector_id": "c1", "platform_chat_id": str(bound_chat)}]
    connector_row = {"type": connector_type} if connector_type else None
    with patch(
        "app.db.database.get_connector_sessions_for_ai_session",
        return_value=sessions,
    ), patch(
        "app.db.database.get_connector",
        return_value=connector_row,
    ), patch(
        "app.core.connectors.manager.connector_manager.get_running_instance",
        return_value=(instance if running else None),
    ):
        yield


# ---------------------------------------------------------------------------
# Registration / names
# ---------------------------------------------------------------------------

def test_build_returns_five_tools():
    tools = build_discord_tools("ai-1")
    assert {t.name for t in tools} == {
        "DiscordCreatePoll",
        "DiscordSendEmbed",
        "DiscordAddReaction",
        "DiscordCreateThread",
        "DiscordReadMessages",
    }


def test_tool_names_namespaced():
    assert get_discord_connector_tool_names() == [
        "mcp__builtins__DiscordCreatePoll",
        "mcp__builtins__DiscordSendEmbed",
        "mcp__builtins__DiscordAddReaction",
        "mcp__builtins__DiscordCreateThread",
        "mcp__builtins__DiscordReadMessages",
    ]


# ---------------------------------------------------------------------------
# DiscordCreatePoll
# ---------------------------------------------------------------------------

class TestCreatePoll:
    @pytest.mark.asyncio
    async def test_happy(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordCreatePoll"](
                {"question": "Best language?", "answers": ["Python", "Rust"], "duration_hours": 12}
            ))
        assert res["success"] is True
        assert res["message_id"] == "999"
        assert res["answer_count"] == 2
        ch.send.assert_awaited_once()
        assert "poll" in ch.send.await_args.kwargs

    @pytest.mark.asyncio
    async def test_rejects_too_few_answers(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordCreatePoll"](
                {"question": "Q", "answers": ["only one"]}
            ))
        assert res["success"] is False
        assert "2 to 10" in res["error"]
        ch.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_client_not_running(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst, running=False):
            res = _payload(await _handlers()["DiscordCreatePoll"](
                {"question": "Q", "answers": ["A", "B"]}
            ))
        assert res["success"] is False
        assert "not running" in res["error"]


# ---------------------------------------------------------------------------
# DiscordSendEmbed
# ---------------------------------------------------------------------------

class TestSendEmbed:
    @pytest.mark.asyncio
    async def test_happy_with_fields_and_color(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordSendEmbed"]({
                "title": "Status",
                "description": "All good",
                "color": "#5865F2",
                "fields": [{"name": "Uptime", "value": "99%", "inline": True}],
                "footer": "generated",
            }))
        assert res["success"] is True
        assert res["message_id"] == "999"
        assert "embed" in ch.send.await_args.kwargs

    @pytest.mark.asyncio
    async def test_requires_title_or_description(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordSendEmbed"]({}))
        assert res["success"] is False
        assert "title or description" in res["error"]

    @pytest.mark.asyncio
    async def test_bad_color(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordSendEmbed"](
                {"title": "T", "color": "not-a-color"}
            ))
        assert res["success"] is False
        assert "color" in res["error"].lower()

    @pytest.mark.asyncio
    async def test_channel_allowlist_rejection(self):
        # Requested channel is neither the bound one nor in allowed_channel_ids.
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch), allowed=[str(EXTRA_CHAT)])
        with _patched(inst):
            res = _payload(await _handlers()["DiscordSendEmbed"](
                {"title": "T", "channel": str(FORBIDDEN_CHAT)}
            ))
        assert res["success"] is False
        assert "not permitted" in res["error"]
        ch.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_allowlist_accepts_configured_channel(self):
        bound = FakeChannel(cid=BOUND_CHAT)
        extra = FakeChannel(cid=EXTRA_CHAT)
        inst = FakeInstance(FakeClient({BOUND_CHAT: bound, EXTRA_CHAT: extra}),
                            allowed=[str(EXTRA_CHAT)])
        with _patched(inst):
            res = _payload(await _handlers()["DiscordSendEmbed"](
                {"title": "T", "channel": str(EXTRA_CHAT)}
            ))
        assert res["success"] is True
        extra.send.assert_awaited_once()
        bound.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# DiscordAddReaction
# ---------------------------------------------------------------------------

class TestAddReaction:
    @pytest.mark.asyncio
    async def test_happy(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordAddReaction"](
                {"message_id": "555", "emoji": "\U0001F44D"}
            ))
        assert res["success"] is True
        ch.fetch_message.assert_awaited_once_with(555)

    @pytest.mark.asyncio
    async def test_missing_args(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordAddReaction"]({"emoji": "x"}))
        assert res["success"] is False


# ---------------------------------------------------------------------------
# DiscordCreateThread
# ---------------------------------------------------------------------------

class TestCreateThread:
    @pytest.mark.asyncio
    async def test_from_channel(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordCreateThread"]({"name": "tangent"}))
        assert res["success"] is True
        assert res["thread_id"] == "8002"
        ch.create_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_from_message(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordCreateThread"](
                {"name": "deep dive", "message_id": "555"}
            ))
        assert res["success"] is True
        assert res["thread_id"] == "7001"
        ch.fetch_message.assert_awaited_once_with(555)

    @pytest.mark.asyncio
    async def test_requires_name(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordCreateThread"]({"name": "  "}))
        assert res["success"] is False


# ---------------------------------------------------------------------------
# DiscordReadMessages
# ---------------------------------------------------------------------------

class TestReadMessages:
    @pytest.mark.asyncio
    async def test_happy_truncates_and_flags_bot(self):
        msgs = [
            FakeMessage(mid=1, content="a" * 600, author_name="Bob", bot=False),
            FakeMessage(mid=2, content="beep", author_name="TheBot", bot=True),
        ]
        ch = FakeChannel(history_msgs=msgs)
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst):
            res = _payload(await _handlers()["DiscordReadMessages"]({"limit": 10}))
        assert res["success"] is True
        assert res["count"] == 2
        first = res["messages"][0]
        assert len(first["content"]) == 500
        assert first["author"] == "Bob"
        assert res["messages"][1]["bot"] is True

    @pytest.mark.asyncio
    async def test_client_not_running(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst, running=False):
            res = _payload(await _handlers()["DiscordReadMessages"]({}))
        assert res["success"] is False
        assert "not running" in res["error"]


# ---------------------------------------------------------------------------
# Resolution guards shared across tools
# ---------------------------------------------------------------------------

class TestResolutionGuards:
    @pytest.mark.asyncio
    async def test_no_session_context(self):
        res = _payload(await _handlers(session_id=None)["DiscordReadMessages"]({}))
        assert res["success"] is False
        assert "session" in res["error"].lower()

    @pytest.mark.asyncio
    async def test_session_not_linked_to_discord(self):
        ch = FakeChannel()
        inst = FakeInstance(FakeClient(ch))
        with _patched(inst, connector_type="telegram"):
            res = _payload(await _handlers()["DiscordReadMessages"]({}))
        assert res["success"] is False
        assert "not linked to a Discord connector" in res["error"]

"""Tests for the connector action gateway.

Covers: catalog integrity (every action has a handler and a sane definition),
schema validation, gateway tool registration/gating, availability filtering
(category config + live bot permissions), the destructive-action approval
gate, and a sample of Discord action handlers against a faked discord.py
client — no network, no MCP transport.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from app.core.connectors import discord_actions
from app.core.connectors.base import ActionDef, validate_action_params
from app.core.connectors.discord_connector import DiscordConnector
from app.mcp.connector_gateway_sdk import (
    build_gateway_tools,
    get_gateway_tool_names,
)

BOUND_CHAT = 1472821837229723766
EXTRA_CHAT = 2222222222222222222
FORBIDDEN_CHAT = 9999999999999999999


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, mid=555, content="hi", author_id=42, bot=False):
        self.id = mid
        self.content = content
        self.created_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
        self.author = MagicMock()
        self.author.id = author_id
        self.author.display_name = "Alice"
        self.author.bot = bot
        self.add_reaction = AsyncMock()
        self.pin = AsyncMock()
        self.unpin = AsyncMock()
        self.delete = AsyncMock()
        self.edit = AsyncMock()
        self.create_thread = AsyncMock(return_value=MagicMock(id=7001))


class FakeChannel:
    def __init__(self, cid=BOUND_CHAT, guild=None, history_msgs=None):
        self.id = cid
        self.name = f"chan-{cid}"
        self.guild = guild
        self.parent_id = None
        self._history = history_msgs or []
        self.send = AsyncMock(return_value=MagicMock(id=999))
        self.fetch_message = AsyncMock(return_value=FakeMessage())
        self.create_thread = AsyncMock(return_value=MagicMock(id=8002))
        self.purge = AsyncMock(return_value=[1, 2, 3])
        self.threads = []

    def history(self, limit=100, **kwargs):
        msgs = list(self._history)[:limit]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()


class FakeGuild:
    def __init__(self, permissions=None):
        self.id = 31337
        self.name = "Test Guild"
        self.channels = []
        self.roles = []
        self.emojis = []
        self.owner_id = 1
        self.member_count = 5
        self.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.premium_tier = 0
        self.description = None
        self.me = MagicMock()
        self.me.guild_permissions = permissions or discord.Permissions.all()
        self.me.top_role = MagicMock()
        self.me.top_role.name = "BotRole"
        self.kick = AsyncMock()
        self.ban = AsyncMock()

    def get_channel(self, cid):
        for c in self.channels:
            if c.id == int(cid):
                return c
        return None


class FakeClient:
    def __init__(self, channels):
        if isinstance(channels, FakeChannel):
            channels = {channels.id: channels}
        self._channels = {int(k): v for k, v in channels.items()}
        self.user = MagicMock()
        self.user.id = 42

    def get_channel(self, cid):
        return self._channels.get(int(cid))


def make_connector(client=None, config=None):
    conn = DiscordConnector(
        connector_id="c1",
        connector_type="discord",
        config=config or {},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._client = client
    return conn


CTX = {"session_id": "ai-1", "connector_id": "c1", "bound_chat_id": str(BOUND_CHAT)}


def _payload(result):
    return json.loads(result["content"][0]["text"])


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------

class TestCatalogIntegrity:
    def test_every_action_has_a_handler(self):
        assert discord_actions.missing_handlers() == []

    def test_unique_ids(self):
        ids = [a.id for a in discord_actions.DISCORD_ACTIONS]
        assert len(ids) == len(set(ids))

    def test_risk_and_schema_shape(self):
        for a in discord_actions.DISCORD_ACTIONS:
            assert a.risk in ("safe", "write", "destructive"), a.id
            assert a.input_schema.get("type") == "object", a.id
            assert a.category, a.id

    def test_required_permissions_are_real_discord_flags(self):
        valid = set(discord.Permissions.VALID_FLAGS)
        for a in discord_actions.DISCORD_ACTIONS:
            if a.required_permission:
                assert a.required_permission in valid, a.id

    def test_catalog_covers_major_categories(self):
        cats = {a.category for a in discord_actions.DISCORD_ACTIONS}
        assert {
            "messages", "reactions", "threads", "channels", "roles",
            "members", "moderation", "webhooks", "invites", "events", "server",
        } <= cats

    def test_connector_actions_is_the_catalog(self):
        assert DiscordConnector.ACTIONS is discord_actions.DISCORD_ACTIONS
        assert DiscordConnector.SUPPORTS_ACTION_GATEWAY is True
        assert DiscordConnector.TOOLS == []


# ---------------------------------------------------------------------------
# Param validation (base layer)
# ---------------------------------------------------------------------------

class TestValidateParams:
    ACTION = ActionDef(
        id="x", name="X", description="d",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "kind": {"type": "string", "enum": ["a", "b"]},
                "flag": {"type": "boolean"},
            },
            "required": ["text"],
        },
    )

    def test_ok(self):
        assert validate_action_params(self.ACTION, {"text": "hi", "limit": 5}) is None

    def test_missing_required(self):
        assert "text" in validate_action_params(self.ACTION, {})

    def test_wrong_type(self):
        assert "limit" in validate_action_params(self.ACTION, {"text": "x", "limit": "many"})

    def test_bool_is_not_integer(self):
        assert "limit" in validate_action_params(self.ACTION, {"text": "x", "limit": True})

    def test_enum(self):
        assert "kind" in validate_action_params(self.ACTION, {"text": "x", "kind": "z"})

    def test_range(self):
        assert "limit" in validate_action_params(self.ACTION, {"text": "x", "limit": 99})


# ---------------------------------------------------------------------------
# Availability (category config + live permissions)
# ---------------------------------------------------------------------------

class TestAvailability:
    @pytest.mark.asyncio
    async def test_category_gating(self):
        conn = make_connector(config={"enabled_action_categories": ["messages"]})
        avail = await conn.get_action_availability(CTX)
        ok, _ = avail["send_message"]
        assert ok is True
        ok, reason = avail["ban_member"]
        assert ok is False
        assert "disabled" in reason

    @pytest.mark.asyncio
    async def test_permission_gating(self):
        perms = discord.Permissions.none()
        perms.update(send_messages=True, add_reactions=True)
        guild = FakeGuild(permissions=perms)
        channel = FakeChannel(guild=guild)
        conn = make_connector(FakeClient(channel))
        avail = await conn.get_action_availability(CTX)
        ok, _ = avail["add_reaction"]
        assert ok is True
        ok, reason = avail["ban_member"]
        assert ok is False
        assert "ban_members" in reason

    @pytest.mark.asyncio
    async def test_no_guild_context_leaves_available(self):
        conn = make_connector()  # no client
        avail = await conn.get_action_availability(CTX)
        ok, _ = avail["ban_member"]
        assert ok is True  # permission unknown -> checked at invoke time


# ---------------------------------------------------------------------------
# Gateway registration / naming
# ---------------------------------------------------------------------------

def _patched_db(connector_type="discord"):
    sessions = [{"connector_id": "c1", "platform_chat_id": str(BOUND_CHAT)}]
    return patch.multiple(
        "app.db.database",
        get_connector_sessions_for_ai_session=MagicMock(return_value=sessions),
        get_connector=MagicMock(return_value={"type": connector_type} if connector_type else None),
    )


class TestRegistration:
    def test_discord_linked_session_gets_pair(self):
        with _patched_db():
            tools = build_gateway_tools("ai-1")
        assert {t.name for t in tools} == {"discord_actions", "discord_invoke"}

    def test_non_gateway_connector_gets_nothing(self):
        # webhook connectors have no action catalog and are not part of the
        # action-gateway conversion, so they never register a tool pair.
        with _patched_db(connector_type="webhook"):
            assert build_gateway_tools("ai-1") == []

    def test_no_session_gets_nothing(self):
        assert build_gateway_tools(None) == []

    def test_allowed_tool_names_include_discord_pair(self):
        names = get_gateway_tool_names()
        assert "mcp__builtins__discord_actions" in names
        assert "mcp__builtins__discord_invoke" in names


# ---------------------------------------------------------------------------
# Gateway tool behavior
# ---------------------------------------------------------------------------

def _handlers():
    with _patched_db():
        return {t.name: t.handler for t in build_gateway_tools("ai-1")}


def _run_ctx(conn, running=True):
    return patch(
        "app.core.connectors.manager.connector_manager.get_running_instance",
        return_value=(conn if running else None),
    )


class TestActionsTool:
    @pytest.mark.asyncio
    async def test_lists_catalog_with_availability(self):
        perms = discord.Permissions.none()
        perms.update(send_messages=True, read_message_history=True)
        guild = FakeGuild(permissions=perms)
        channel = FakeChannel(guild=guild)
        conn = make_connector(FakeClient(channel))
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_actions"]({}))
        assert res["success"] is True
        ids = {a["id"] for a in res["actions"]}
        assert "send_message" in ids
        assert "read_messages" in ids
        assert "ban_member" not in ids
        unavailable_ids = {u["id"] for u in res["unavailable"]}
        assert "ban_member" in unavailable_ids
        assert "messages" in res["categories"]
        # schemas ride along for available actions
        sm = next(a for a in res["actions"] if a["id"] == "send_message")
        assert sm["input_schema"]["required"] == ["text"]

    @pytest.mark.asyncio
    async def test_category_filter(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        conn = make_connector(FakeClient(channel))
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_actions"]({"category": "roles"}))
        assert res["success"] is True
        assert {a["category"] for a in res["actions"]} == {"roles"}

    @pytest.mark.asyncio
    async def test_connector_not_running(self):
        handlers = _handlers()
        with _patched_db(), _run_ctx(None, running=False):
            res = _payload(await handlers["discord_actions"]({}))
        assert res["success"] is False
        assert "not running" in res["error"]


class TestInvokeTool:
    @pytest.mark.asyncio
    async def test_happy_send_message(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        conn = make_connector(FakeClient(channel))
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_invoke"]({
                "action": "send_message", "params": {"text": "hello"},
            }))
        assert res["success"] is True
        assert res["action"] == "send_message"
        channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        conn = make_connector(FakeClient(FakeChannel(guild=FakeGuild())))
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_invoke"]({"action": "explode"}))
        assert res["success"] is False
        assert "Unknown action" in res["error"]

    @pytest.mark.asyncio
    async def test_invalid_params_rejected_before_dispatch(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        conn = make_connector(FakeClient(channel))
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_invoke"]({
                "action": "send_message", "params": {},
            }))
        assert res["success"] is False
        assert "text" in res["error"]
        channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_allowlist_rejection(self):
        guild = FakeGuild()
        bound = FakeChannel(cid=BOUND_CHAT, guild=guild)
        other = FakeChannel(cid=FORBIDDEN_CHAT, guild=guild)
        conn = make_connector(FakeClient({BOUND_CHAT: bound, FORBIDDEN_CHAT: other}))
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_invoke"]({
                "action": "send_message",
                "params": {"text": "hi", "channel": str(FORBIDDEN_CHAT)},
            }))
        assert res["success"] is False
        assert "not permitted" in res["error"]
        other.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_destructive_requires_approval_and_denial_blocks(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        conn = make_connector(FakeClient(channel))
        conn.request_action_approval = AsyncMock(return_value=(False, "Denied by user."))
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_invoke"]({
                "action": "purge_messages", "params": {"limit": 5},
            }))
        assert res["success"] is False
        assert "not approved" in res["error"]
        conn.request_action_approval.assert_awaited_once()
        channel.purge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_destructive_approved_runs(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        conn = make_connector(FakeClient(channel))
        conn.request_action_approval = AsyncMock(return_value=(True, "Approved by user."))
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_invoke"]({
                "action": "purge_messages", "params": {"limit": 5},
            }))
        assert res["success"] is True
        assert res["deleted_count"] == 3

    @pytest.mark.asyncio
    async def test_auto_approve_skips_prompt(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        conn = make_connector(
            FakeClient(channel), config={"auto_approve_destructive": "true"},
        )
        conn.request_action_approval = AsyncMock()
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_invoke"]({
                "action": "purge_messages", "params": {"limit": 2},
            }))
        assert res["success"] is True
        conn.request_action_approval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_approve_false_string_still_prompts(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        conn = make_connector(
            FakeClient(channel), config={"auto_approve_destructive": "false"},
        )
        conn.request_action_approval = AsyncMock(return_value=(False, "Denied by user."))
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_invoke"]({
                "action": "purge_messages", "params": {"limit": 2},
            }))
        assert res["success"] is False
        conn.request_action_approval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_permission_blocked_at_invoke(self):
        perms = discord.Permissions.none()
        perms.update(send_messages=True)
        guild = FakeGuild(permissions=perms)
        channel = FakeChannel(guild=guild)
        conn = make_connector(FakeClient(channel))
        handlers = _handlers()
        with _patched_db(), _run_ctx(conn):
            res = _payload(await handlers["discord_invoke"]({
                "action": "kick_member", "params": {"user_id": "77"},
            }))
        assert res["success"] is False
        assert "kick_members" in res["error"]


# ---------------------------------------------------------------------------
# Handler sampling (direct dispatch through the connector)
# ---------------------------------------------------------------------------

class TestHandlers:
    @pytest.mark.asyncio
    async def test_create_poll(self):
        channel = FakeChannel(guild=FakeGuild())
        conn = make_connector(FakeClient(channel))
        res = await conn.execute_action(
            "create_poll",
            {"question": "Best?", "answers": ["A", "B"], "duration_hours": 2},
            context=CTX,
        )
        assert res["success"] is True
        assert "poll" in channel.send.await_args.kwargs

    @pytest.mark.asyncio
    async def test_send_embed(self):
        channel = FakeChannel(guild=FakeGuild())
        conn = make_connector(FakeClient(channel))
        res = await conn.execute_action(
            "send_embed", {"title": "T", "color": "#5865F2"}, context=CTX,
        )
        assert res["success"] is True
        assert "embed" in channel.send.await_args.kwargs

    @pytest.mark.asyncio
    async def test_read_messages(self):
        msgs = [FakeMessage(mid=1, content="a" * 600), FakeMessage(mid=2, bot=True)]
        channel = FakeChannel(guild=FakeGuild(), history_msgs=msgs)
        conn = make_connector(FakeClient(channel))
        res = await conn.execute_action("read_messages", {"limit": 10}, context=CTX)
        assert res["success"] is True
        assert res["count"] == 2
        assert len(res["messages"][0]["content"]) == 500

    @pytest.mark.asyncio
    async def test_pin_message(self):
        channel = FakeChannel(guild=FakeGuild())
        conn = make_connector(FakeClient(channel))
        res = await conn.execute_action("pin_message", {"message_id": "555"}, context=CTX)
        assert res["success"] is True

    @pytest.mark.asyncio
    async def test_edit_message_rejects_foreign_author(self):
        channel = FakeChannel(guild=FakeGuild())
        channel.fetch_message = AsyncMock(return_value=FakeMessage(author_id=1000))
        conn = make_connector(FakeClient(channel))
        res = await conn.execute_action(
            "edit_message", {"message_id": "555", "text": "new"}, context=CTX,
        )
        assert res["success"] is False
        assert "own" in res["error"] or "this bot" in res["error"]

    @pytest.mark.asyncio
    async def test_edit_message_own(self):
        channel = FakeChannel(guild=FakeGuild())
        msg = FakeMessage(author_id=42)  # FakeClient.user.id == 42
        channel.fetch_message = AsyncMock(return_value=msg)
        conn = make_connector(FakeClient(channel))
        res = await conn.execute_action(
            "edit_message", {"message_id": "555", "text": "new"}, context=CTX,
        )
        assert res["success"] is True
        msg.edit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_channels(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        guild.channels = [channel, FakeChannel(cid=123, guild=guild)]
        conn = make_connector(FakeClient(channel))
        res = await conn.execute_action("list_channels", {}, context=CTX)
        assert res["success"] is True
        assert res["count"] == 2

    @pytest.mark.asyncio
    async def test_delete_channel_refuses_bound(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        guild.channels = [channel]
        conn = make_connector(FakeClient(channel))
        res = await conn.execute_action(
            "delete_channel", {"channel_id": str(BOUND_CHAT)}, context=CTX,
        )
        assert res["success"] is False
        assert "bound" in res["error"]

    @pytest.mark.asyncio
    async def test_ban_member(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        conn = make_connector(FakeClient(channel))
        res = await conn.execute_action(
            "ban_member",
            {"user_id": "777", "reason": "spam", "delete_message_days": 1},
            context=CTX,
        )
        assert res["success"] is True
        guild.ban.assert_awaited_once()
        assert guild.ban.await_args.kwargs["delete_message_seconds"] == 86400

    @pytest.mark.asyncio
    async def test_thread_send_allowed_via_parent(self):
        guild = FakeGuild()
        bound = FakeChannel(cid=BOUND_CHAT, guild=guild)
        thread = FakeChannel(cid=333, guild=guild)
        thread.parent_id = BOUND_CHAT
        conn = make_connector(FakeClient({BOUND_CHAT: bound, 333: thread}))
        res = await conn.execute_action(
            "send_message", {"text": "in thread", "channel": "333"}, context=CTX,
        )
        assert res["success"] is True
        thread.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_maps_to_send(self):
        guild = FakeGuild()
        channel = FakeChannel(guild=guild)
        conn = make_connector(FakeClient(channel))
        res = await conn.execute_tool(
            "discord_send", {"platform_chat_id": str(BOUND_CHAT), "text": "hi"},
        )
        assert res["success"] is True
        channel.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# Approval flow (button interaction resolves the pending future)
# ---------------------------------------------------------------------------

class TestApprovalFlow:
    @pytest.mark.asyncio
    async def test_approve_via_interaction(self):
        channel = FakeChannel(guild=FakeGuild())
        conn = make_connector(FakeClient(channel))
        action = discord_actions.ACTIONS_BY_ID["purge_messages"]

        task = asyncio.ensure_future(
            conn.request_action_approval(str(BOUND_CHAT), action, {"limit": 5}, timeout=5)
        )
        await asyncio.sleep(0.05)  # let the prompt send and the future register
        channel.send.assert_awaited_once()
        assert len(conn._pending_action_approvals) == 1
        request_id = next(iter(conn._pending_action_approvals))

        interaction = MagicMock()
        interaction.data = {"custom_id": f"actreq:{request_id}:approve"}
        interaction.response.send_message = AsyncMock()
        await conn._on_interaction_handler(interaction)

        approved, reason = await task
        assert approved is True
        assert "Approved" in reason
        assert conn._pending_action_approvals == {}

    @pytest.mark.asyncio
    async def test_deny_via_interaction(self):
        channel = FakeChannel(guild=FakeGuild())
        conn = make_connector(FakeClient(channel))
        action = discord_actions.ACTIONS_BY_ID["kick_member"]

        task = asyncio.ensure_future(
            conn.request_action_approval(str(BOUND_CHAT), action, {"user_id": "1"}, timeout=5)
        )
        await asyncio.sleep(0.05)
        request_id = next(iter(conn._pending_action_approvals))

        interaction = MagicMock()
        interaction.data = {"custom_id": f"actreq:{request_id}:deny"}
        interaction.response.send_message = AsyncMock()
        await conn._on_interaction_handler(interaction)

        approved, reason = await task
        assert approved is False
        assert "Denied" in reason

    @pytest.mark.asyncio
    async def test_timeout_denies(self):
        channel = FakeChannel(guild=FakeGuild())
        conn = make_connector(FakeClient(channel))
        action = discord_actions.ACTIONS_BY_ID["ban_member"]
        approved, reason = await conn.request_action_approval(
            str(BOUND_CHAT), action, {"user_id": "1"}, timeout=0.05,
        )
        assert approved is False
        assert "cancelled" in reason

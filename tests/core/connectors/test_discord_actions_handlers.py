"""Discord action-gateway handlers against a faked discord.py client.

Complements tests/mcp/test_connector_gateway.py (catalog, gateway wiring and a
handful of handlers) with the rest of the handler surface: the channel
allowlist and guild resolution, parameter validation, discord.py error
mapping (Forbidden / HTTPException / anything else), limit clamping, and the
guild-management handlers (channels, roles, members, moderation, webhooks,
invites, events). No network: every discord.py object is a local fake.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from app.core.connectors import discord_actions
from app.core.connectors.discord_connector import DiscordConnector

BOUND = 1472821837229723766
EXTRA = 2222222222222222222
OTHER = 3333333333333333333
THREAD = 4444444444444444444
CTX = {"session_id": "ai-1", "connector_id": "c1", "bound_chat_id": str(BOUND)}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, mid=555, content="hi", author_id=42):
        self.id = mid
        self.content = content
        self.created_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
        self.author = SimpleNamespace(id=author_id, display_name="Alice", bot=False)
        for name in ("add_reaction", "remove_reaction", "clear_reactions", "pin",
                     "unpin", "delete", "edit"):
            setattr(self, name, AsyncMock())
        self.create_thread = AsyncMock(return_value=SimpleNamespace(id=7001))


class FakeChannel:
    def __init__(self, cid=BOUND, guild=None, parent_id=None, history=None):
        self.id = cid
        self.name = f"chan-{cid}"
        self.guild = guild
        self.parent_id = parent_id
        self.type = discord.ChannelType.text
        self.topic = "the topic"
        self.nsfw = False
        self.slowmode_delay = 0
        self.category = SimpleNamespace(name="General")
        self.position = 1
        self.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self._history = history or []
        self.history_kwargs = None
        self._sent = 0

        async def _send(*args, **kwargs):
            self._sent += 1
            return SimpleNamespace(id=900 + self._sent)

        self.send = AsyncMock(side_effect=_send)
        self.fetch_message = AsyncMock(return_value=FakeMessage())
        self.create_thread = AsyncMock(return_value=SimpleNamespace(id=8002))
        self.purge = AsyncMock(return_value=[1, 2, 3])
        self.pins = AsyncMock(return_value=[FakeMessage(mid=1, content="pinned")])
        self.threads = []
        self.edit = AsyncMock()
        self.delete = AsyncMock()
        self.webhooks = AsyncMock(return_value=[])
        self.create_webhook = AsyncMock(
            return_value=SimpleNamespace(id=61, name="hook", url="https://discord.com/api/webhooks/61/x")
        )
        self.create_invite = AsyncMock(
            return_value=SimpleNamespace(url="https://discord.gg/abc", code="abc")
        )

    def history(self, limit=100, **kwargs):
        self.history_kwargs = {"limit": limit, **kwargs}
        msgs = list(self._history)[:limit]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()


class FakeRole:
    def __init__(self, rid, name, position):
        self.id = rid
        self.name = name
        self.position = position
        self.color = discord.Color.default()
        self.hoist = False
        self.mentionable = False
        self.managed = False
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class FakeMember:
    def __init__(self, uid=77, name="bob"):
        self.id = uid
        self.display_name = name.title()
        self.bot = False
        self.joined_at = datetime(2025, 5, 1, tzinfo=timezone.utc)
        self.roles = []
        self.timed_out_until = None
        self._name = name
        self.add_roles = AsyncMock()
        self.remove_roles = AsyncMock()
        self.edit = AsyncMock()
        self.timeout = AsyncMock()

    def __str__(self):
        return self._name


class FakeGuild:
    def __init__(self):
        self.id = 31337
        self.name = "Test Guild"
        self.description = "desc"
        self.owner_id = 1
        self.member_count = 5
        self.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.premium_tier = 0
        self.channels = []
        self.roles = [FakeRole(1, "@everyone", 0), FakeRole(2, "Mod", 5), FakeRole(3, "Member", 2)]
        self.emojis = [
            SimpleNamespace(id=11, name="party", animated=True),
            SimpleNamespace(id=12, name="wave", animated=False),
        ]
        perms = discord.Permissions.none()
        perms.manage_roles = True
        perms.kick_members = True
        self.me = SimpleNamespace(guild_permissions=perms, top_role=SimpleNamespace(name="BotRole"))
        self.members = {77: FakeMember()}
        self.kick = AsyncMock()
        self.ban = AsyncMock()
        self.unban = AsyncMock()
        self.create_text_channel = AsyncMock(side_effect=lambda name, **kw: SimpleNamespace(id=501, name=name))
        self.create_voice_channel = AsyncMock(side_effect=lambda name, **kw: SimpleNamespace(id=502, name=name))
        self.create_category = AsyncMock(side_effect=lambda name, **kw: SimpleNamespace(id=503, name=name))
        self.create_role = AsyncMock(side_effect=lambda **kw: FakeRole(600, kw.get("name"), 1))
        self.query_members = AsyncMock(return_value=[FakeMember()])
        self.invites = AsyncMock(return_value=[
            SimpleNamespace(code="abc", url="https://discord.gg/abc",
                            channel=SimpleNamespace(name="general"), uses=3, max_uses=10),
        ])
        self.fetch_scheduled_events = AsyncMock(return_value=[])
        self.fetch_scheduled_event = AsyncMock()
        self.create_scheduled_event = AsyncMock(
            side_effect=lambda **kw: SimpleNamespace(id=801, name=kw["name"])
        )

    def get_channel(self, cid):
        for c in self.channels:
            if c.id == int(cid):
                return c
        return None

    def get_role(self, rid):
        for r in self.roles:
            if r.id == rid:
                return r
        return None

    async def fetch_member(self, uid):
        if uid not in self.members:
            raise discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Member")
        return self.members[uid]


class FakeClient:
    def __init__(self, *channels, fetchable=None):
        self._channels = {c.id: c for c in channels}
        self._fetchable = {c.id: c for c in (fetchable or [])}
        self.user = SimpleNamespace(id=42)

    def get_channel(self, cid):
        return self._channels.get(int(cid))

    async def fetch_channel(self, cid):
        if cid in self._fetchable:
            return self._fetchable[cid]
        raise discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Channel")


def make_conn(client, config=None):
    conn = DiscordConnector(
        connector_id="c1", connector_type="discord", config=config or {},
        on_incoming_message=AsyncMock(), on_permission_response=AsyncMock(),
    )
    conn._client = client
    return conn


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def bound(guild):
    ch = FakeChannel(BOUND, guild=guild)
    guild.channels.append(ch)
    return ch


@pytest.fixture
def conn(bound):
    return make_conn(FakeClient(bound))


async def run(conn, action, params=None, ctx=CTX):
    return await discord_actions.execute(conn, action, params or {}, ctx)


# ---------------------------------------------------------------------------
# Channel allowlist / resolution
# ---------------------------------------------------------------------------

class TestChannelResolution:
    async def test_client_not_running(self):
        res = await run(make_conn(None), "send_message", {"text": "x"})
        assert res == {"success": False, "error": "Discord client is not running."}

    async def test_no_target_without_bound_session(self, conn):
        res = await run(conn, "send_message", {"text": "x"}, ctx={})
        assert res["success"] is False
        assert "No target channel" in res["error"]

    async def test_invalid_channel_id(self, conn):
        res = await run(conn, "send_message", {"text": "x", "channel": "general"})
        assert res["success"] is False
        assert "Invalid channel id" in res["error"]

    async def test_unknown_channel_falls_back_to_fetch_then_errors(self, conn):
        res = await run(conn, "send_message", {"text": "x", "channel": str(OTHER)})
        assert res["success"] is False
        assert f"Channel {OTHER} not found" in res["error"]

    async def test_fetch_channel_fallback_resolves_uncached_channel(self, guild, bound):
        extra = FakeChannel(EXTRA, guild=guild)
        conn = make_conn(FakeClient(bound, fetchable=[extra]),
                         config={"allowed_channel_ids": [str(EXTRA)]})
        res = await run(conn, "send_message", {"text": "hello", "channel": str(EXTRA)})
        assert res["success"] is True
        assert res["channel_id"] == str(EXTRA)

    async def test_channel_outside_allowlist_is_rejected(self, guild, bound):
        other = FakeChannel(OTHER, guild=guild)
        conn = make_conn(FakeClient(bound, other))
        res = await run(conn, "send_message", {"text": "x", "channel": str(OTHER)})
        assert res["success"] is False
        assert "not permitted" in res["error"]
        other.send.assert_not_awaited()

    async def test_allowlist_as_single_string(self, guild, bound):
        extra = FakeChannel(EXTRA, guild=guild)
        conn = make_conn(FakeClient(bound, extra), config={"allowed_channel_ids": str(EXTRA)})
        res = await run(conn, "send_message", {"text": "x", "channel": str(EXTRA)})
        assert res["success"] is True

    async def test_legacy_platform_chat_id_alias_is_allowlisted_too(self, guild, bound):
        other = FakeChannel(OTHER, guild=guild)
        conn = make_conn(FakeClient(bound, other))
        res = await run(conn, "send_message", {"text": "x", "platform_chat_id": str(OTHER)})
        assert res["success"] is False
        assert "not permitted" in res["error"]

    async def test_no_session_context_is_unrestricted(self, guild, bound):
        # Automations/API callers pass explicit targets and carry no bound chat.
        other = FakeChannel(OTHER, guild=guild)
        conn = make_conn(FakeClient(bound, other))
        res = await run(conn, "send_message", {"text": "x", "channel": str(OTHER)}, ctx={})
        assert res["success"] is True

    async def test_thread_of_allowed_channel_is_permitted(self, guild, bound):
        thread = FakeChannel(THREAD, guild=guild, parent_id=BOUND)
        conn = make_conn(FakeClient(bound, thread))
        res = await run(conn, "send_message", {"text": "x", "channel": str(THREAD)})
        assert res["success"] is True

    async def test_guild_action_in_dm_is_rejected(self):
        dm = FakeChannel(BOUND, guild=None)
        res = await run(make_conn(FakeClient(dm)), "list_roles")
        assert res["success"] is False
        assert "requires a server" in res["error"]


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

class TestMessages:
    async def test_send_message_chunks_and_replies_only_on_first(self, conn, bound):
        res = await run(conn, "send_message", {"text": "a" * 4500, "reply_to_message_id": "555"})
        assert res["success"] is True
        assert len(res["message_ids"]) == 3
        refs = [c.kwargs["reference"] for c in bound.send.await_args_list]
        assert refs[0] is not None and refs[1:] == [None, None]
        assert all(len(c.args[0]) <= 2000 for c in bound.send.await_args_list)

    async def test_send_message_blank_text(self, conn):
        res = await run(conn, "send_message", {"text": "   "})
        assert res == {"success": False, "error": "text is required."}

    async def test_send_message_reply_target_missing(self, conn, bound):
        bound.fetch_message.side_effect = discord.NotFound(MagicMock(status=404, reason="x"), "Unknown Message")
        res = await run(conn, "send_message", {"text": "hi", "reply_to_message_id": "999"})
        assert res["success"] is False
        assert "Message 999 not found" in res["error"]
        bound.send.assert_not_awaited()

    async def test_send_message_reply_invalid_id(self, conn):
        res = await run(conn, "send_message", {"text": "hi", "reply_to_message_id": "abc"})
        assert res["success"] is False
        assert "Invalid message id" in res["error"]

    async def test_send_embed_validation(self, conn):
        assert "At least one of" in (await run(conn, "send_embed", {}))["error"]
        res = await run(conn, "send_embed", {"title": "t", "color": "not-a-color"})
        assert res["success"] is False and "Invalid color" in res["error"]

    async def test_send_embed_caps_fields_and_sets_footer(self, conn, bound):
        fields = [{"name": f"f{i}", "value": "v", "inline": True} for i in range(30)] + ["junk"]
        res = await run(conn, "send_embed", {
            "title": "T" * 300, "description": "d", "color": "#5865F2",
            "fields": fields, "footer": "foot",
        })
        assert res["success"] is True
        embed = bound.send.await_args.kwargs["embed"]
        assert len(embed.fields) == 25
        assert len(embed.title) == 256
        assert embed.footer.text == "foot"

    async def test_send_file_missing(self, conn, tmp_path):
        res = await run(conn, "send_file", {"path": str(tmp_path / "nope.txt")})
        assert res["success"] is False and "File not found" in res["error"]

    async def test_send_file_too_large(self, conn, tmp_path, monkeypatch):
        f = tmp_path / "big.bin"
        f.write_bytes(b"x")
        monkeypatch.setattr(discord_actions.os.path, "getsize", lambda p: 26 * 1024 * 1024)
        res = await run(conn, "send_file", {"path": str(f)})
        assert res["success"] is False and "25 MB" in res["error"]

    async def test_send_file_happy(self, conn, bound, tmp_path):
        f = tmp_path / "report.txt"
        f.write_text("data")
        res = await run(conn, "send_file", {"path": str(f), "filename": "r.txt", "caption": "here"})
        assert res["success"] is True
        kw = bound.send.await_args.kwargs
        assert kw["content"] == "here"
        assert kw["file"].filename == "r.txt"

    @pytest.mark.parametrize("params,needle", [
        ({"question": "", "answers": ["a", "b"]}, "question is required"),
        ({"question": "q", "answers": ["only"]}, "2 to 10"),
        ({"question": "q", "answers": [str(i) for i in range(11)]}, "2 to 10"),
        ({"question": "q", "answers": "a,b"}, "2 to 10"),
        ({"question": "q", "answers": ["a", "b"], "duration_hours": "soon"}, "must be a number"),
    ])
    async def test_create_poll_validation(self, conn, params, needle):
        res = await run(conn, "create_poll", params)
        assert res["success"] is False and needle in res["error"]

    async def test_create_poll_clamps_duration(self, conn, bound):
        res = await run(conn, "create_poll", {"question": "Lunch?", "answers": ["x", "y"], "duration_hours": 10000})
        assert res["success"] is True
        assert bound.send.await_args.kwargs["poll"].duration == timedelta(hours=768)

    async def test_read_messages_clamps_limit_and_pages(self, conn, bound):
        bound._history = [FakeMessage(mid=i, content="c" * 800) for i in range(60)]
        res = await run(conn, "read_messages", {"limit": 500, "before_message_id": "123"})
        assert res["success"] is True
        assert res["count"] == 50
        assert bound.history_kwargs["limit"] == 50
        assert bound.history_kwargs["before"].id == 123
        assert len(res["messages"][0]["content"]) == 500
        assert res["messages"][0]["author"] == "Alice"

    async def test_read_messages_bad_limit_defaults_and_bad_before(self, conn, bound):
        res = await run(conn, "read_messages", {"limit": "lots"})
        assert res["success"] is True and bound.history_kwargs["limit"] == 20
        res = await run(conn, "read_messages", {"before_message_id": "yesterday"})
        assert res["success"] is False and "Invalid before_message_id" in res["error"]

    async def test_delete_unpin_and_list_pins(self, conn, bound):
        msg = FakeMessage()
        bound.fetch_message.return_value = msg
        assert (await run(conn, "delete_message", {"message_id": "555"}))["deleted_message_id"] == "555"
        msg.delete.assert_awaited_once()
        res = await run(conn, "unpin_message", {"message_id": "555"})
        assert res == {"success": True, "message_id": "555", "pinned": False}
        res = await run(conn, "list_pins")
        assert res["count"] == 1 and res["pins"][0]["content"] == "pinned"

    async def test_list_pins_async_iterator_shape(self, conn, bound):
        async def _aiter():
            for m in (FakeMessage(mid=1), FakeMessage(mid=2)):
                yield m

        bound.pins = MagicMock(return_value=_aiter())
        res = await run(conn, "list_pins")
        assert res["count"] == 2


class TestReactions:
    async def test_add_remove_clear(self, conn, bound):
        msg = FakeMessage()
        bound.fetch_message.return_value = msg
        assert (await run(conn, "add_reaction", {"message_id": "555", "emoji": "\U0001F44D"}))["success"]
        msg.add_reaction.assert_awaited_once_with("\U0001F44D")
        res = await run(conn, "remove_reaction", {"message_id": "555", "emoji": "\U0001F44D"})
        assert res["removed"] is True
        assert msg.remove_reaction.await_args.args[1].id == 42  # the bot's own reaction
        assert (await run(conn, "clear_reactions", {"message_id": "555"}))["cleared"] is True

    @pytest.mark.parametrize("action", ["add_reaction", "remove_reaction"])
    async def test_emoji_required(self, conn, action):
        res = await run(conn, action, {"message_id": "555", "emoji": " "})
        assert res == {"success": False, "error": "emoji is required."}


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

class TestThreads:
    async def test_create_thread_from_message_and_standalone(self, conn, bound):
        msg = FakeMessage()
        bound.fetch_message.return_value = msg
        res = await run(conn, "create_thread", {"name": "n" * 150, "message_id": "555"})
        assert res["thread_id"] == "7001" and len(res["name"]) == 100
        res = await run(conn, "create_thread", {"name": "standalone"})
        assert res["thread_id"] == "8002"
        assert bound.create_thread.await_args.kwargs["type"] == discord.ChannelType.public_thread
        assert (await run(conn, "create_thread", {}))["error"] == "name is required."

    async def test_list_threads(self, conn, bound):
        bound.threads = [SimpleNamespace(id=1, name="t1", archived=True, locked=False, message_count=4)]
        res = await run(conn, "list_threads")
        assert res["threads"] == [{"id": "1", "name": "t1", "archived": True, "locked": False, "message_count": 4}]

    async def test_edit_thread_paths(self, guild, bound):
        good = FakeChannel(THREAD, guild=guild, parent_id=BOUND)
        foreign = FakeChannel(5555, guild=guild, parent_id=OTHER)
        conn = make_conn(FakeClient(bound, good, foreign))
        assert "Invalid thread id" in (await run(conn, "edit_thread", {"thread_id": "x"}))["error"]
        assert (await run(conn, "edit_thread", {"thread_id": "12345"}))["error"] == "Thread not found."
        res = await run(conn, "edit_thread", {"thread_id": "5555", "archived": True})
        assert "not permitted" in res["error"]
        foreign.edit.assert_not_awaited()
        res = await run(conn, "edit_thread", {"thread_id": str(THREAD)})
        assert "Nothing to change" in res["error"]
        res = await run(conn, "edit_thread", {"thread_id": str(THREAD), "name": "renamed", "locked": True})
        assert res == {"success": True, "thread_id": str(THREAD), "name": "renamed", "locked": True}
        good.edit.assert_awaited_once_with(name="renamed", locked=True)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

class TestChannels:
    async def test_get_channel_info(self, conn, bound):
        res = await run(conn, "get_channel_info", {"channel_id": str(BOUND)})
        assert res["channel"]["topic"] == "the topic"
        assert res["channel"]["category"] == "General"
        assert "Invalid channel id" in (await run(conn, "get_channel_info", {"channel_id": "x"}))["error"]
        res = await run(conn, "get_channel_info", {"channel_id": str(OTHER)})
        assert res["error"] == "Channel not found in this server."

    @pytest.mark.parametrize("ctype,method,new_id", [
        ("text", "create_text_channel", "501"),
        ("voice", "create_voice_channel", "502"),
        ("category", "create_category", "503"),
    ])
    async def test_create_channel_types(self, conn, guild, ctype, method, new_id):
        res = await run(conn, "create_channel", {"name": "new", "type": ctype})
        assert res == {"success": True, "channel_id": new_id, "name": "new", "type": ctype}
        getattr(guild, method).assert_awaited_once()

    async def test_create_channel_validation(self, conn):
        assert (await run(conn, "create_channel", {"name": " "}))["error"] == "name is required."
        res = await run(conn, "create_channel", {"name": "a", "category_id": "cat"})
        assert "Invalid category id" in res["error"]
        res = await run(conn, "create_channel", {"name": "a", "category_id": str(OTHER)})
        assert res["error"] == "Category not found in this server."

    async def test_edit_channel(self, conn, bound):
        assert (await run(conn, "edit_channel", {"channel_id": str(BOUND)}))["error"] == "Nothing to change."
        res = await run(conn, "edit_channel", {"channel_id": str(BOUND), "topic": "t", "slowmode_seconds": "30", "nsfw": 0})
        assert res["changed"] == ["nsfw", "slowmode_delay", "topic"]
        bound.edit.assert_awaited_once_with(topic="t", slowmode_delay=30, nsfw=False)

    async def test_edit_channel_bad_slowmode_maps_to_error_shape(self, conn):
        res = await run(conn, "edit_channel", {"channel_id": str(BOUND), "slowmode_seconds": "fast"})
        assert res["success"] is False
        assert "edit_channel" in res["error"]

    async def test_delete_channel(self, conn, guild):
        victim = FakeChannel(OTHER, guild=guild)
        guild.channels.append(victim)
        res = await run(conn, "delete_channel", {"channel_id": str(OTHER), "reason": "cleanup"})
        assert res == {"success": True, "deleted_channel_id": str(OTHER), "name": victim.name}
        victim.delete.assert_awaited_once_with(reason="cleanup")
        assert "Invalid channel id" in (await run(conn, "delete_channel", {"channel_id": "abc"}))["error"]
        assert "not found" in (await run(conn, "delete_channel", {"channel_id": "1"}))["error"]

    @pytest.mark.xfail(strict=True, reason=(
        "bug: delete_channel compares the raw channel_id string to bound_chat_id before "
        "int() normalisation, so ' <id>' or '0<id>' deletes the bound channel"
    ))
    @pytest.mark.parametrize("raw", [f" {BOUND}", f"0{BOUND}"])
    async def test_delete_channel_refuses_bound_channel_in_any_spelling(self, conn, bound, raw):
        res = await run(conn, "delete_channel", {"channel_id": raw})
        assert res["success"] is False
        bound.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# Roles and members
# ---------------------------------------------------------------------------

class TestRoles:
    async def test_list_roles_sorted_by_position(self, conn):
        res = await run(conn, "list_roles")
        assert [r["name"] for r in res["roles"]] == ["Mod", "Member", "@everyone"]

    async def test_create_role_validation(self, conn, guild):
        assert (await run(conn, "create_role", {}))["error"] == "name is required."
        assert "Invalid color" in (await run(conn, "create_role", {"name": "x", "color": "red!"}))["error"]
        res = await run(conn, "create_role", {"name": "x", "permissions": ["kick_members", "fly"]})
        assert res["error"] == "Unknown permission flag(s): fly"
        guild.create_role.assert_not_awaited()

    async def test_create_role_happy(self, conn, guild):
        res = await run(conn, "create_role", {
            "name": "Helper", "color": "#00ff00", "hoist": True, "permissions": ["kick_members"],
        })
        assert res["success"] is True and res["role"]["name"] == "Helper"
        kw = guild.create_role.await_args.kwargs
        assert kw["hoist"] is True and kw["permissions"].kick_members is True
        assert kw["colour"] == discord.Colour.from_str("#00ff00")

    async def test_edit_and_delete_role(self, conn, guild):
        mod = guild.get_role(2)
        assert (await run(conn, "edit_role", {"role_id": "2"}))["error"] == "Nothing to change."
        assert (await run(conn, "edit_role", {"role_id": "2", "mentionable": True}))["success"]
        mod.edit.assert_awaited_once_with(mentionable=True)
        res = await run(conn, "delete_role", {"role_id": "2"})
        assert res == {"success": True, "deleted_role_id": "2", "name": "Mod"}
        assert "Invalid role id" in (await run(conn, "delete_role", {"role_id": "mod"}))["error"]
        assert (await run(conn, "delete_role", {"role_id": "999"}))["error"] == "Role not found in this server."

    async def test_add_and_remove_role(self, conn, guild):
        member = guild.members[77]
        res = await run(conn, "add_role", {"role_id": "3", "user_id": "77", "reason": "promo"})
        assert res == {"success": True, "user_id": "77", "role": "Member", "added": True}
        member.add_roles.assert_awaited_once_with(guild.get_role(3), reason="promo")
        res = await run(conn, "remove_role", {"role_id": "3", "user_id": "77"})
        assert res["removed"] is True
        res = await run(conn, "add_role", {"role_id": "3", "user_id": "88"})
        assert res["success"] is False and "Member 88 not found" in res["error"]
        res = await run(conn, "add_role", {"role_id": "3", "user_id": "bob"})
        assert "Invalid user id" in res["error"]


class TestMembers:
    async def test_get_member_payload(self, conn):
        res = await run(conn, "get_member", {"user_id": "77"})
        assert res["member"]["name"] == "bob"
        assert res["member"]["joined_at"].startswith("2025-05-01")

    async def test_search_members_clamps_limit(self, conn, guild):
        assert (await run(conn, "search_members", {}))["error"] == "query is required."
        res = await run(conn, "search_members", {"query": "bo", "limit": 999})
        assert res["count"] == 1
        guild.query_members.assert_awaited_with(query="bo", limit=25)
        await run(conn, "search_members", {"query": "bo", "limit": "many"})
        guild.query_members.assert_awaited_with(query="bo", limit=10)

    async def test_set_nickname_blank_clears(self, conn, guild):
        res = await run(conn, "set_nickname", {"user_id": "77", "nickname": "  "})
        assert res["nickname"] is None
        guild.members[77].edit.assert_awaited_once_with(nick=None, reason=None)


class TestModeration:
    async def test_kick(self, conn, guild):
        res = await run(conn, "kick_member", {"user_id": "77", "reason": "spam"})
        assert res == {"success": True, "kicked_user_id": "77"}
        guild.kick.assert_awaited_once_with(guild.members[77], reason="spam")

    async def test_ban_clamps_delete_days(self, conn, guild):
        await run(conn, "ban_member", {"user_id": "99", "delete_message_days": 30})
        assert guild.ban.await_args.kwargs["delete_message_seconds"] == 7 * 86400
        await run(conn, "ban_member", {"user_id": "99", "delete_message_days": "some"})
        assert guild.ban.await_args.kwargs["delete_message_seconds"] == 0
        assert "Invalid user id" in (await run(conn, "ban_member", {}))["error"]

    async def test_unban(self, conn, guild):
        res = await run(conn, "unban_member", {"user_id": "99"})
        assert res == {"success": True, "unbanned_user_id": "99"}
        assert guild.unban.await_args.args[0].id == 99
        assert "Invalid user id" in (await run(conn, "unban_member", {"user_id": "x"}))["error"]

    @pytest.mark.parametrize("minutes,needle", [
        ("abc", "must be a number"), (0, "between 1 and 40320"), (40321, "between 1 and 40320"),
    ])
    async def test_timeout_validation(self, conn, guild, minutes, needle):
        res = await run(conn, "timeout_member", {"user_id": "77", "minutes": minutes})
        assert res["success"] is False and needle in res["error"]
        guild.members[77].timeout.assert_not_awaited()

    async def test_timeout_and_remove(self, conn, guild):
        member = guild.members[77]
        res = await run(conn, "timeout_member", {"user_id": "77", "minutes": 10})
        assert res["timeout_minutes"] == 10
        assert member.timeout.await_args.args[0] == timedelta(minutes=10)
        res = await run(conn, "remove_timeout", {"user_id": "77"})
        assert res["timeout_removed"] is True
        assert member.timeout.await_args.args[0] is None

    async def test_purge_limit(self, conn, bound):
        assert "must be an integer" in (await run(conn, "purge_messages", {}))["error"]
        res = await run(conn, "purge_messages", {"limit": 5000})
        assert res["deleted_count"] == 3
        bound.purge.assert_awaited_once_with(limit=100)


# ---------------------------------------------------------------------------
# Webhooks / invites / emojis / events / server
# ---------------------------------------------------------------------------

class TestWebhooksInvites:
    async def test_webhooks(self, conn, bound):
        hook = SimpleNamespace(id=61, name="ci", delete=AsyncMock())
        bound.webhooks.return_value = [hook]
        assert (await run(conn, "list_webhooks"))["webhooks"] == [{"id": "61", "name": "ci"}]
        assert (await run(conn, "create_webhook", {}))["error"] == "name is required."
        res = await run(conn, "create_webhook", {"name": "n" * 100})
        assert res["url"].startswith("https://discord.com/api/webhooks/")
        assert len(bound.create_webhook.await_args.kwargs["name"]) == 80
        assert "not found" in (await run(conn, "delete_webhook", {"webhook_id": "1"}))["error"]
        assert (await run(conn, "delete_webhook", {"webhook_id": "61"}))["success"] is True
        hook.delete.assert_awaited_once()

    async def test_invites(self, conn, bound):
        res = await run(conn, "create_invite", {"max_age_hours": 2, "max_uses": 5})
        assert res == {"success": True, "url": "https://discord.gg/abc", "code": "abc"}
        bound.create_invite.assert_awaited_once_with(max_age=7200, max_uses=5)
        res = await run(conn, "create_invite", {"max_age_hours": "forever"})
        assert "must be numbers" in res["error"]
        res = await run(conn, "list_invites")
        assert res["invites"][0]["channel"] == "general" and res["count"] == 1

    async def test_emojis_usage_strings(self, conn):
        res = await run(conn, "list_emojis")
        assert [e["usage"] for e in res["emojis"]] == ["<a:party:11>", "<:wave:12>"]


class TestScheduledEvents:
    async def test_list(self, conn, guild):
        guild.fetch_scheduled_events.return_value = [SimpleNamespace(
            id=1, name="Launch", description="d",
            start_time=datetime(2026, 10, 1, 18, tzinfo=timezone.utc), end_time=None,
            entity_metadata=SimpleNamespace(location="Online"), status="scheduled",
        )]
        res = await run(conn, "list_scheduled_events")
        assert res["events"][0]["location"] == "Online"
        assert res["events"][0]["start_time"] == "2026-10-01T18:00:00+00:00"

    @pytest.mark.parametrize("params,needle", [
        ({}, "name is required"),
        ({"name": "x", "start_time": "tomorrow"}, "Invalid start_time"),
        ({"name": "x", "start_time": "2026-10-01T18:00:00Z", "end_time": "later", "location": "x"}, "Invalid end_time"),
        ({"name": "x", "start_time": "2026-10-01T18:00:00Z"}, "Provide voice_channel_id or location"),
        ({"name": "x", "start_time": "2026-10-01T18:00:00Z", "location": "Park"}, "require end_time"),
        ({"name": "x", "start_time": "2026-10-01T18:00:00Z", "voice_channel_id": "v"}, "Invalid voice channel id"),
        ({"name": "x", "start_time": "2026-10-01T18:00:00Z", "voice_channel_id": "12"}, "Voice channel not found"),
    ])
    async def test_create_validation(self, conn, guild, params, needle):
        res = await run(conn, "create_scheduled_event", params)
        assert res["success"] is False and needle in res["error"]
        guild.create_scheduled_event.assert_not_awaited()

    async def test_create_external_event(self, conn, guild):
        res = await run(conn, "create_scheduled_event", {
            "name": "Meetup", "description": "come", "location": "Park",
            "start_time": "2026-10-01T18:00:00", "end_time": "2026-10-01T20:00:00+02:00",
        })
        assert res == {"success": True, "event_id": "801", "name": "Meetup"}
        kw = guild.create_scheduled_event.await_args.kwargs
        assert kw["entity_type"] == discord.EntityType.external
        assert kw["start_time"].tzinfo == timezone.utc  # naive input is taken as UTC
        assert kw["privacy_level"] == discord.PrivacyLevel.guild_only

    async def test_create_voice_event(self, conn, guild, bound):
        voice = FakeChannel(6000, guild=guild)
        guild.channels.append(voice)
        res = await run(conn, "create_scheduled_event", {
            "name": "Standup", "start_time": "2026-10-01T09:00:00Z", "voice_channel_id": "6000",
        })
        assert res["success"] is True
        kw = guild.create_scheduled_event.await_args.kwargs
        assert kw["channel"] is voice and kw["entity_type"] == discord.EntityType.voice

    async def test_delete(self, conn, guild):
        ev = SimpleNamespace(delete=AsyncMock())
        guild.fetch_scheduled_event.return_value = ev
        assert (await run(conn, "delete_scheduled_event", {"event_id": "5"}))["deleted_event_id"] == "5"
        ev.delete.assert_awaited_once()
        assert "Invalid event id" in (await run(conn, "delete_scheduled_event", {"event_id": "x"}))["error"]
        guild.fetch_scheduled_event.side_effect = discord.NotFound(MagicMock(status=404, reason="x"), "gone")
        assert "not found" in (await run(conn, "delete_scheduled_event", {"event_id": "6"}))["error"]


async def test_get_server_info_reports_bot_permissions(conn):
    res = await run(conn, "get_server_info")
    server = res["server"]
    assert server["bot_permissions"] == ["kick_members", "manage_roles"]
    assert server["role_count"] == 3 and server["emoji_count"] == 2
    assert server["bot_top_role"] == "BotRole"


# ---------------------------------------------------------------------------
# Dispatch-level error mapping
# ---------------------------------------------------------------------------

class TestExecuteErrorMapping:
    async def test_unknown_action(self, conn):
        assert await run(conn, "summon_dragon") == {"success": False, "error": "Unknown action: summon_dragon"}

    async def test_discord_unavailable(self, conn, monkeypatch):
        monkeypatch.setattr(discord_actions, "DISCORD_AVAILABLE", False)
        assert (await run(conn, "list_roles"))["error"] == "discord.py is not installed."

    async def test_forbidden_is_mapped(self, conn, guild):
        guild.kick.side_effect = discord.Forbidden(MagicMock(status=403, reason="Forbidden"),
                                                   {"code": 50013, "message": "Missing Permissions"})
        res = await run(conn, "kick_member", {"user_id": "77"})
        assert res["success"] is False
        assert "lacks permission" in res["error"] and "Missing Permissions" in res["error"]

    async def test_http_exception_is_mapped(self, conn, bound):
        bound.send.side_effect = discord.HTTPException(MagicMock(status=503, reason="Service Unavailable"), "upstream down")
        res = await run(conn, "send_message", {"text": "hi"})
        assert res["success"] is False
        assert res["error"].startswith("Discord API error running 'send_message'")
        assert "503" in res["error"]

    async def test_rate_limited_429_is_mapped(self, conn, bound):
        bound.purge.side_effect = discord.HTTPException(MagicMock(status=429, reason="Too Many Requests"), "slow down")
        res = await run(conn, "purge_messages", {"limit": 5})
        assert res["success"] is False and "429" in res["error"]

    async def test_unexpected_exception_is_mapped(self, conn, bound):
        bound.send.side_effect = TimeoutError("gateway timed out")
        res = await run(conn, "send_message", {"text": "hi"})
        assert res == {"success": False, "error": "Action 'send_message' failed: gateway timed out"}

    async def test_none_params_and_context_are_tolerated(self, conn):
        res = await discord_actions.execute(conn, "send_message", None, None)
        assert res == {"success": False, "error": "text is required."}

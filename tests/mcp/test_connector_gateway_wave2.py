"""Tests for the wave-2 action-gateway conversions: Slack, Home Assistant, Gmail.

Covers catalog integrity and legacy-id preservation, the gateway's global
registration mode for non-messaging connectors, the Slack Block Kit approval
flow, and handler sampling for each platform against faked clients — no
network, no MCP transport.
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.connectors import gmail_actions, homeassistant_actions, slack_actions
from app.core.connectors.gmail import GmailConnector
from app.core.connectors.homeassistant import HomeAssistantConnector
from app.core.connectors.slack import SlackConnector
from app.mcp.connector_gateway_sdk import build_gateway_tools, get_gateway_tool_names

BOUND_CHAT = "C0000000001"
FORBIDDEN_CHAT = "C9999999999"
CTX = {"session_id": "ai-1", "connector_id": "c1", "bound_chat_id": BOUND_CHAT}


def _payload(result):
    return json.loads(result["content"][0]["text"])


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------

class TestCatalogs:
    @pytest.mark.parametrize("mod,cls,legacy_ids", [
        (slack_actions, SlackConnector, {"send_message"}),
        (homeassistant_actions, HomeAssistantConnector,
         {"call_service", "toggle_entity", "get_states"}),
        (gmail_actions, GmailConnector, {"send_email"}),
    ])
    def test_catalog_integrity(self, mod, cls, legacy_ids):
        catalog = cls.ACTIONS
        ids = [a.id for a in catalog]
        assert mod.missing_handlers() == []
        assert len(ids) == len(set(ids))
        assert legacy_ids <= set(ids)  # automations keep working
        for a in catalog:
            assert a.risk in ("safe", "write", "destructive"), a.id
            assert a.input_schema.get("type") == "object", a.id
        assert cls.SUPPORTS_ACTION_GATEWAY is True
        assert cls.TOOLS == []

    def test_gateway_names_include_all_four_pairs(self):
        names = set(get_gateway_tool_names())
        for t in ("discord", "slack", "homeassistant", "gmail"):
            assert f"mcp__builtins__{t}_actions" in names
            assert f"mcp__builtins__{t}_invoke" in names

    def test_non_messaging_platforms_have_no_destructive_actions(self):
        # HA and Gmail have no interactive approval surface, so nothing in
        # their catalogs may require one.
        for catalog in (homeassistant_actions.HOMEASSISTANT_ACTIONS,
                        gmail_actions.GMAIL_ACTIONS):
            assert [a.id for a in catalog if a.risk == "destructive"] == []


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def make_slack(config=None):
    conn = SlackConnector(
        connector_id="c1",
        connector_type="slack",
        config=config or {},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    client = MagicMock()
    for method in (
        "chat_postMessage", "chat_update", "chat_delete", "chat_scheduleMessage",
        "conversations_history", "conversations_replies", "conversations_list",
        "conversations_info", "conversations_create", "conversations_archive",
        "conversations_setTopic", "conversations_invite",
        "reactions_add", "reactions_remove",
        "pins_add", "pins_remove", "pins_list",
        "users_info", "users_list", "files_upload_v2", "team_info",
    ):
        setattr(client, method, AsyncMock(return_value={"ok": True, "ts": "111.222"}))
    conn._web_client = client
    conn._bot_user_id = "UBOT"
    return conn


class TestSlackHandlers:
    @pytest.mark.asyncio
    async def test_send_message_chunks_and_threads(self):
        conn = make_slack()
        res = await conn.execute_action(
            "send_message",
            {"text": "x" * 4500, "thread_ts": "99.1"},
            context=CTX,
        )
        assert res["success"] is True
        assert conn._web_client.chat_postMessage.await_count == 2
        kwargs = conn._web_client.chat_postMessage.await_args.kwargs
        assert kwargs["channel"] == BOUND_CHAT
        assert kwargs["thread_ts"] == "99.1"

    @pytest.mark.asyncio
    async def test_read_messages(self):
        conn = make_slack()
        conn._web_client.conversations_history = AsyncMock(return_value={
            "messages": [
                {"ts": "1.0", "user": "U1", "text": "hello"},
                {"ts": "2.0", "bot_id": "B1", "text": "beep"},
            ],
        })
        res = await conn.execute_action("read_messages", {"limit": 5}, context=CTX)
        assert res["success"] is True
        assert res["count"] == 2
        assert res["messages"][1]["bot"] is True

    @pytest.mark.asyncio
    async def test_channel_allowlist_rejection(self):
        conn = make_slack()
        res = await conn.execute_action(
            "send_message", {"text": "hi", "channel": FORBIDDEN_CHAT}, context=CTX,
        )
        assert res["success"] is False
        assert "not permitted" in res["error"]
        conn._web_client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_add_reaction_strips_colons(self):
        conn = make_slack()
        res = await conn.execute_action(
            "add_reaction", {"ts": "1.0", "emoji": ":thumbsup:"}, context=CTX,
        )
        assert res["success"] is True
        assert conn._web_client.reactions_add.await_args.kwargs["name"] == "thumbsup"

    @pytest.mark.asyncio
    async def test_archive_refuses_bound_channel(self):
        conn = make_slack()
        res = await conn.execute_action(
            "archive_channel", {"channel_id": BOUND_CHAT}, context=CTX,
        )
        assert res["success"] is False
        assert "bound" in res["error"]

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_maps_to_send(self):
        conn = make_slack()
        res = await conn.execute_tool(
            "slack_send", {"platform_chat_id": "C777", "text": "hi"},
        )
        assert res["success"] is True
        assert conn._web_client.chat_postMessage.await_args.kwargs["channel"] == "C777"

    @pytest.mark.asyncio
    async def test_category_gating(self):
        conn = make_slack(config={"enabled_action_categories": ["messages"]})
        avail = await conn.get_action_availability(CTX)
        assert avail["send_message"][0] is True
        ok, reason = avail["archive_channel"]
        assert ok is False and "disabled" in reason


class TestSlackApprovalFlow:
    @pytest.mark.asyncio
    async def test_approve_via_interactive(self):
        conn = make_slack()
        action = slack_actions.ACTIONS_BY_ID["delete_message"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"ts": "1.0"}, timeout=5)
        )
        await asyncio.sleep(0.05)
        conn._web_client.chat_postMessage.assert_awaited_once()
        request_id = next(iter(conn._pending_action_approvals))

        payload = {
            "actions": [{"action_id": f"actreq:{request_id}:approve"}],
            "channel": {"id": BOUND_CHAT},
            "message": {"ts": "5.0", "text": "Approval needed"},
        }
        await conn._handle_interactive(payload)

        approved, reason = await task
        assert approved is True
        assert conn._pending_action_approvals == {}
        conn._web_client.chat_update.assert_awaited_once()  # buttons removed

    @pytest.mark.asyncio
    async def test_timeout_denies(self):
        conn = make_slack()
        action = slack_actions.ACTIONS_BY_ID["archive_channel"]
        approved, reason = await conn.request_action_approval(
            BOUND_CHAT, action, {"channel_id": "C2"}, timeout=0.05,
        )
        assert approved is False
        assert "cancelled" in reason


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------

def _resp(status=200, body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body if body is not None else {})
    r.text = text or json.dumps(body or {})
    return r


def make_ha(config=None):
    conn = HomeAssistantConnector(
        connector_id="c1",
        connector_type="homeassistant",
        config=config or {"url": "http://ha:8123", "access_token": "t"},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._http_client = MagicMock()  # truthy — real calls go through mocked helpers
    conn._api_get = AsyncMock(return_value=_resp())
    conn._api_post = AsyncMock(return_value=_resp())
    return conn


class TestHomeAssistantHandlers:
    @pytest.mark.asyncio
    async def test_get_states_domain_and_entity_filter(self):
        conn = make_ha(config={
            "url": "u", "access_token": "t", "entity_filter": "light.,switch.",
        })
        conn._api_get = AsyncMock(return_value=_resp(body=[
            {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
            {"entity_id": "switch.fan", "state": "off", "attributes": {}},
            {"entity_id": "climate.house", "state": "heat", "attributes": {}},
        ]))
        res = await conn.execute_action("get_states", {"domain": "light"}, context={})
        assert res["success"] is True
        assert [e["entity_id"] for e in res["entities"]] == ["light.kitchen"]

    @pytest.mark.asyncio
    async def test_set_value_maps_light_brightness(self):
        conn = make_ha()
        res = await conn.execute_action(
            "set_value", {"entity_id": "light.kitchen", "value": "128"}, context={},
        )
        assert res["success"] is True
        path, kwargs = conn._api_post.await_args.args[0], conn._api_post.await_args.kwargs
        assert path == "/api/services/light/turn_on"
        assert kwargs["data"] == {"entity_id": "light.kitchen", "brightness": 128}

    @pytest.mark.asyncio
    async def test_set_value_unknown_domain_points_to_call_service(self):
        conn = make_ha()
        res = await conn.execute_action(
            "set_value", {"entity_id": "lock.door", "value": "1"}, context={},
        )
        assert res["success"] is False
        assert "call_service" in res["error"]

    @pytest.mark.asyncio
    async def test_call_service_merges_data(self):
        conn = make_ha()
        res = await conn.execute_action("call_service", {
            "domain": "climate", "service": "set_temperature",
            "entity_id": "climate.house", "data": {"temperature": 21},
        }, context={})
        assert res["success"] is True
        kwargs = conn._api_post.await_args.kwargs
        assert kwargs["data"] == {"entity_id": "climate.house", "temperature": 21}

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_control(self):
        conn = make_ha()
        res = await conn.execute_tool(
            "homeassistant_control", {"command": "toggle", "entity_id": "light.x"},
        )
        assert res["success"] is True
        assert conn._api_post.await_args.args[0] == "/api/services/homeassistant/toggle"

    @pytest.mark.asyncio
    async def test_http_error_surfaces(self):
        conn = make_ha()
        conn._api_post = AsyncMock(return_value=_resp(status=401, text="unauthorized"))
        res = await conn.execute_action("turn_on", {"entity_id": "light.x"}, context={})
        assert res["success"] is False
        assert "401" in res["error"]


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def make_gmail():
    conn = GmailConnector(
        connector_id="c1",
        connector_type="gmail",
        config={"client_id": "i", "client_secret": "s", "refresh_token": "r"},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._service = MagicMock()
    conn._email_address = "bot@example.com"
    return conn


def _gmail_api(conn):
    return conn._service.users.return_value


class TestGmailHandlers:
    @pytest.mark.asyncio
    async def test_send_email(self):
        conn = make_gmail()
        _gmail_api(conn).messages.return_value.send.return_value.execute.return_value = {
            "id": "m1", "threadId": "t1",
        }
        res = await conn.execute_action("send_email", {
            "to": "a@b.c", "subject": "Hi", "body": "Hello",
        }, context={})
        assert res["success"] is True
        assert res["message_id"] == "m1"
        body = _gmail_api(conn).messages.return_value.send.call_args.kwargs["body"]
        assert "raw" in body

    @pytest.mark.asyncio
    async def test_search_messages_fetches_summaries(self):
        conn = make_gmail()
        msgs = _gmail_api(conn).messages.return_value
        msgs.list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
        msgs.get.return_value.execute.return_value = {
            "id": "m1", "threadId": "t1", "snippet": "hey",
            "payload": {"headers": [
                {"name": "From", "value": "Alice <a@b.c>"},
                {"name": "Subject", "value": "Yo"},
            ]},
        }
        res = await conn.execute_action(
            "search_messages", {"query": "is:unread"}, context={},
        )
        assert res["success"] is True
        assert res["messages"][0]["subject"] == "Yo"
        assert msgs.list.call_args.kwargs["q"] == "is:unread"

    @pytest.mark.asyncio
    async def test_read_message_extracts_body(self):
        conn = make_gmail()
        data = base64.urlsafe_b64encode(b"plain body here").decode("ascii")
        _gmail_api(conn).messages.return_value.get.return_value.execute.return_value = {
            "id": "m1", "threadId": "t1", "snippet": "s",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "S"}],
                "body": {"data": data},
            },
        }
        res = await conn.execute_action("read_message", {"message_id": "m1"}, context={})
        assert res["success"] is True
        assert res["message"]["body"] == "plain body here"

    @pytest.mark.asyncio
    async def test_mark_read_removes_unread_label(self):
        conn = make_gmail()
        _gmail_api(conn).messages.return_value.modify.return_value.execute.return_value = {
            "id": "m1", "labelIds": ["INBOX"],
        }
        res = await conn.execute_action("mark_read", {"message_id": "m1"}, context={})
        assert res["success"] is True
        body = _gmail_api(conn).messages.return_value.modify.call_args.kwargs["body"]
        assert body == {"removeLabelIds": ["UNREAD"]}

    @pytest.mark.asyncio
    async def test_trash_and_untrash(self):
        conn = make_gmail()
        msgs = _gmail_api(conn).messages.return_value
        msgs.trash.return_value.execute.return_value = {"id": "m1"}
        msgs.untrash.return_value.execute.return_value = {"id": "m1"}
        res = await conn.execute_action("trash_message", {"message_id": "m1"}, context={})
        assert res["success"] is True and res["trashed"] is True
        res = await conn.execute_action("untrash_message", {"message_id": "m1"}, context={})
        assert res["success"] is True and res["trashed"] is False

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_gmail_send(self):
        conn = make_gmail()
        _gmail_api(conn).messages.return_value.send.return_value.execute.return_value = {
            "id": "m9", "threadId": "t9",
        }
        res = await conn.execute_tool("gmail_send", {
            "to": "a@b.c", "subject": "S", "body": "B",
        })
        assert res["success"] is True
        assert res["message_id"] == "m9"


# ---------------------------------------------------------------------------
# Gateway registration — session-linked (Slack) and global (HA / Gmail)
# ---------------------------------------------------------------------------

def _patched_db(connector_type=None):
    sessions = (
        [{"connector_id": "c1", "platform_chat_id": BOUND_CHAT}]
        if connector_type else []
    )
    return patch.multiple(
        "app.db.database",
        get_connector_sessions_for_ai_session=MagicMock(return_value=sessions),
        get_connector=MagicMock(return_value={"type": connector_type} if connector_type else None),
    )


class TestRegistration:
    def test_slack_linked_session_gets_pair(self):
        with _patched_db("slack"):
            tools = build_gateway_tools("ai-1")
        assert {t.name for t in tools} == {"slack_actions", "slack_invoke"}

    def test_non_messaging_running_connectors_register_globally(self):
        ha = make_ha()
        gm = make_gmail()

        def fake_running(ctype=None):
            pairs = [("ha1", ha), ("gm1", gm)]
            if ctype is None:
                return pairs
            return [(cid, c) for cid, c in pairs if c.connector_type == ctype]

        with _patched_db(None), patch(
            "app.core.connectors.manager.connector_manager.get_running_connectors",
            side_effect=fake_running,
        ):
            tools = build_gateway_tools("ai-1")
        assert {t.name for t in tools} == {
            "homeassistant_actions", "homeassistant_invoke",
            "gmail_actions", "gmail_invoke",
        }

    @pytest.mark.asyncio
    async def test_global_invoke_resolves_first_running_instance(self):
        ha = make_ha()
        ha._api_post = AsyncMock(return_value=_resp())

        def fake_running(ctype=None):
            return [("ha1", ha)] if ctype in (None, "homeassistant") else []

        with _patched_db(None), patch(
            "app.core.connectors.manager.connector_manager.get_running_connectors",
            side_effect=fake_running,
        ):
            tools = {t.name: t.handler for t in build_gateway_tools("ai-1")}
            res = _payload(await tools["homeassistant_invoke"]({
                "action": "turn_on", "params": {"entity_id": "light.x"},
            }))
        assert res["success"] is True
        ha._api_post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_global_actions_lists_catalog(self):
        gm = make_gmail()

        def fake_running(ctype=None):
            return [("gm1", gm)] if ctype in (None, "gmail") else []

        with _patched_db(None), patch(
            "app.core.connectors.manager.connector_manager.get_running_connectors",
            side_effect=fake_running,
        ):
            tools = {t.name: t.handler for t in build_gateway_tools("ai-1")}
            res = _payload(await tools["gmail_actions"]({}))
        assert res["success"] is True
        ids = {a["id"] for a in res["actions"]}
        assert {"send_email", "search_messages", "trash_message"} <= ids

    def test_messaging_connector_never_registers_globally(self):
        slack = make_slack()

        def fake_running(ctype=None):
            return [("s1", slack)] if ctype in (None, "slack") else []

        with _patched_db(None), patch(
            "app.core.connectors.manager.connector_manager.get_running_connectors",
            side_effect=fake_running,
        ):
            assert build_gateway_tools("ai-1") == []

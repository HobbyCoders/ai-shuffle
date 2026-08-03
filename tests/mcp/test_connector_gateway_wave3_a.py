"""Tests for the wave-3a action-gateway conversions: Telegram, Signal, WhatsApp.

Covers catalog integrity and legacy-id preservation, handler sampling for
each platform against faked bots/clients, the Approve/Deny destructive-action
flow (in-channel buttons for Telegram/WhatsApp, text reply for Signal), and
gateway registration (these are messaging connectors — session-linked only,
never global) — no network, no MCP transport.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.connectors import signal_actions, telegram_actions, whatsapp_actions
from app.core.connectors.signal_connector import SignalConnector
from app.core.connectors.telegram import TelegramConnector
from app.core.connectors.whatsapp import WhatsAppConnector
from app.mcp.connector_gateway_sdk import build_gateway_tools, get_gateway_tool_names

BOUND_CHAT = "1000001"
FORBIDDEN_CHAT = "9999999"
CTX = {"session_id": "ai-1", "connector_id": "c1", "bound_chat_id": BOUND_CHAT}


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _resp(status=200, body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body if body is not None else {})
    r.text = text or json.dumps(body or {})
    return r


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------

class TestCatalogs:
    @pytest.mark.parametrize("mod,cls,legacy_ids", [
        (telegram_actions, TelegramConnector, {"send_message"}),
        (signal_actions, SignalConnector, {"send_message"}),
        (whatsapp_actions, WhatsAppConnector, {"send_message"}),
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

    def test_gateway_names_include_all_three_pairs(self):
        names = set(get_gateway_tool_names())
        for t in ("telegram", "signal", "whatsapp"):
            assert f"mcp__builtins__{t}_actions" in names
            assert f"mcp__builtins__{t}_invoke" in names

    def test_whatsapp_has_no_destructive_actions(self):
        # WhatsApp Business exposes no bulk-delete/membership surface to bots.
        assert [a.id for a in whatsapp_actions.WHATSAPP_ACTIONS if a.risk == "destructive"] == []

    def test_telegram_and_signal_have_destructive_actions(self):
        assert any(a.risk == "destructive" for a in telegram_actions.TELEGRAM_ACTIONS)
        assert any(a.risk == "destructive" for a in signal_actions.SIGNAL_ACTIONS)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def make_telegram(config=None):
    conn = TelegramConnector(
        connector_id="c1",
        connector_type="telegram",
        config=config or {},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    bot = MagicMock()
    msg = MagicMock(message_id=555)
    for method in (
        "send_message", "edit_message_text", "delete_message", "forward_message",
        "set_message_reaction", "pin_chat_message", "unpin_chat_message",
        "unpin_all_chat_messages", "send_poll", "stop_poll", "send_photo",
        "send_document", "get_chat", "get_chat_member_count", "set_chat_title",
        "set_chat_description", "ban_chat_member", "unban_chat_member", "leave_chat",
    ):
        setattr(bot, method, AsyncMock(return_value=msg))
    bot.get_chat_member_count = AsyncMock(return_value=42)
    chat = MagicMock(id=int(BOUND_CHAT), type="private", title="T", username="u", description="d")
    bot.get_chat = AsyncMock(return_value=chat)
    conn._bot = bot
    return conn


class TestTelegramHandlers:
    @pytest.mark.asyncio
    async def test_send_message_chunks(self):
        conn = make_telegram()
        res = await conn.execute_action(
            "send_message", {"text": "x" * 9000}, context=CTX,
        )
        assert res["success"] is True
        assert conn._bot.send_message.await_count >= 2
        kwargs = conn._bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == int(BOUND_CHAT)

    @pytest.mark.asyncio
    async def test_chat_allowlist_rejection(self):
        conn = make_telegram()
        res = await conn.execute_action(
            "send_message", {"text": "hi", "chat_id": FORBIDDEN_CHAT}, context=CTX,
        )
        assert res["success"] is False
        assert "not permitted" in res["error"]
        conn._bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_message(self):
        conn = make_telegram()
        res = await conn.execute_action(
            "delete_message", {"message_id": 12}, context=CTX,
        )
        assert res["success"] is True
        conn._bot.delete_message.assert_awaited_once_with(
            chat_id=int(BOUND_CHAT), message_id=12,
        )

    @pytest.mark.asyncio
    async def test_send_poll_builds_options(self):
        conn = make_telegram()
        res = await conn.execute_action(
            "send_poll",
            {"question": "Q?", "options": ["a", "b"]},
            context=CTX,
        )
        assert res["success"] is True
        conn._bot.send_poll.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_leave_chat_refuses_bound_chat(self):
        conn = make_telegram()
        res = await conn.execute_action("leave_chat", {}, context=CTX)
        assert res["success"] is False
        assert "bound" in res["error"]
        conn._bot.leave_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_maps_to_send(self):
        conn = make_telegram()
        res = await conn.execute_tool(
            "telegram_send", {"platform_chat_id": "777", "text": "hi"},
        )
        assert res["success"] is True
        assert conn._bot.send_message.await_args.kwargs["chat_id"] == 777

    @pytest.mark.asyncio
    async def test_category_gating(self):
        conn = make_telegram(config={"enabled_action_categories": ["messages"]})
        avail = await conn.get_action_availability(CTX)
        assert avail["send_message"][0] is True
        ok, reason = avail["ban_chat_member"]
        assert ok is False and "disabled" in reason

    @pytest.mark.asyncio
    async def test_bot_not_running_errors(self):
        conn = TelegramConnector(
            connector_id="c1", connector_type="telegram", config={},
            on_incoming_message=AsyncMock(), on_permission_response=AsyncMock(),
        )
        res = await conn.execute_action("send_message", {"text": "hi"}, context=CTX)
        assert res["success"] is False
        assert "not running" in res["error"]


class TestTelegramApprovalFlow:
    @pytest.mark.asyncio
    async def test_approve_via_callback_query(self):
        conn = make_telegram()
        action = telegram_actions.ACTIONS_BY_ID["delete_message"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"message_id": 1}, timeout=5)
        )
        await asyncio.sleep(0.05)
        conn._bot.send_message.assert_awaited_once()
        request_id = next(iter(conn._pending_action_approvals))

        callback_query = MagicMock()
        callback_query.data = f"actreq:{request_id}:approve"
        callback_query.answer = AsyncMock()
        await conn._handle_callback_query(callback_query)

        approved, reason = await task
        assert approved is True
        assert conn._pending_action_approvals == {}

    @pytest.mark.asyncio
    async def test_deny_via_callback_query(self):
        conn = make_telegram()
        action = telegram_actions.ACTIONS_BY_ID["ban_chat_member"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"user_id": 1}, timeout=5)
        )
        await asyncio.sleep(0.05)
        request_id = next(iter(conn._pending_action_approvals))

        callback_query = MagicMock()
        callback_query.data = f"actreq:{request_id}:deny"
        callback_query.answer = AsyncMock()
        await conn._handle_callback_query(callback_query)

        approved, reason = await task
        assert approved is False

    @pytest.mark.asyncio
    async def test_timeout_denies(self):
        conn = make_telegram()
        action = telegram_actions.ACTIONS_BY_ID["leave_chat"]
        approved, reason = await conn.request_action_approval(
            BOUND_CHAT, action, {}, timeout=0.05,
        )
        assert approved is False
        assert "cancelled" in reason


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

def make_signal(config=None):
    conn = SignalConnector(
        connector_id="c1",
        connector_type="signal",
        config=config or {"account": "+15550000000", "api_url": "http://sig:8080"},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._api_get = AsyncMock(return_value=_resp())
    conn._api_post = AsyncMock(return_value=_resp())
    conn._api_put = AsyncMock(return_value=_resp())
    conn._api_delete = AsyncMock(return_value=_resp())
    return conn


class TestSignalHandlers:
    @pytest.mark.asyncio
    async def test_send_message_chunks(self):
        conn = make_signal()
        res = await conn.execute_action(
            "send_message", {"text": "x" * 9000}, context=CTX,
        )
        assert res["success"] is True
        assert conn._api_post.await_count >= 2
        args, kwargs = conn._api_post.await_args.args, conn._api_post.await_args
        assert args[0] == "/api/v2/send"

    @pytest.mark.asyncio
    async def test_chat_allowlist_rejection(self):
        conn = make_signal()
        res = await conn.execute_action(
            "send_message", {"text": "hi", "chat_id": FORBIDDEN_CHAT}, context=CTX,
        )
        assert res["success"] is False
        assert "not permitted" in res["error"]
        conn._api_post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_typing_indicator(self):
        conn = make_signal()
        res = await conn.execute_action("send_typing_indicator", {}, context=CTX)
        assert res["success"] is True
        conn._api_put.assert_awaited_once()
        assert conn._api_put.await_args.args[0] == f"/api/v1/typing-indicator/{conn._account}"

    @pytest.mark.asyncio
    async def test_list_groups(self):
        conn = make_signal()
        conn._api_get = AsyncMock(return_value=_resp(body=[
            {"id": "g1", "name": "Group One", "members": ["+1", "+2"]},
        ]))
        res = await conn.execute_action("list_groups", {}, context=CTX)
        assert res["success"] is True
        assert res["count"] == 1
        assert res["groups"][0]["member_count"] == 2

    @pytest.mark.asyncio
    async def test_leave_group_refuses_bound_group(self):
        conn = make_signal()
        ctx = {"session_id": "ai-1", "connector_id": "c1", "bound_chat_id": "grp-1"}
        res = await conn.execute_action("leave_group", {"group_id": "grp-1"}, context=ctx)
        assert res["success"] is False
        assert "bound" in res["error"]
        conn._api_post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_http_error_surfaces(self):
        conn = make_signal()
        conn._api_post = AsyncMock(return_value=_resp(status=400, text="bad request"))
        res = await conn.execute_action("send_message", {"text": "hi"}, context=CTX)
        assert res["success"] is False
        assert "400" in res["error"]

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_maps_to_send(self):
        conn = make_signal()
        res = await conn.execute_tool(
            "signal_send", {"platform_chat_id": "+15551234567", "text": "hi"},
        )
        assert res["success"] is True

    @pytest.mark.asyncio
    async def test_category_gating(self):
        conn = make_signal(config={
            "account": "+15550000000", "enabled_action_categories": ["messages"],
        })
        avail = await conn.get_action_availability(CTX)
        assert avail["send_message"][0] is True
        ok, reason = avail["leave_group"]
        assert ok is False and "disabled" in reason


class TestSignalApprovalFlow:
    @pytest.mark.asyncio
    async def test_approve_via_text_reply(self):
        conn = make_signal()
        action = signal_actions.ACTIONS_BY_ID["leave_group"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"group_id": "g1"}, timeout=5)
        )
        await asyncio.sleep(0.05)
        conn._api_post.assert_awaited_once()
        assert conn._pending_action_approvals

        handled = conn._check_permission_response("approve")
        assert handled is True

        approved, reason = await task
        assert approved is True
        assert conn._pending_action_approvals == {}

    @pytest.mark.asyncio
    async def test_deny_via_text_reply(self):
        conn = make_signal()
        action = signal_actions.ACTIONS_BY_ID["leave_group"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"group_id": "g1"}, timeout=5)
        )
        await asyncio.sleep(0.05)
        conn._check_permission_response("deny")
        approved, reason = await task
        assert approved is False

    @pytest.mark.asyncio
    async def test_timeout_denies(self):
        conn = make_signal()
        action = signal_actions.ACTIONS_BY_ID["leave_group"]
        approved, reason = await conn.request_action_approval(
            BOUND_CHAT, action, {"group_id": "g1"}, timeout=0.05,
        )
        assert approved is False
        assert "cancelled" in reason


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------

def make_whatsapp(config=None):
    conn = WhatsAppConnector(
        connector_id="c1",
        connector_type="whatsapp",
        config=config or {
            "phone_number_id": "pn1", "access_token": "tok", "verify_token": "vt",
        },
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._api_post = AsyncMock(return_value=_resp())
    conn._api_get_phone_number = AsyncMock(return_value=_resp())
    return conn


class TestWhatsAppHandlers:
    @pytest.mark.asyncio
    async def test_send_message_chunks(self):
        conn = make_whatsapp()
        res = await conn.execute_action(
            "send_message", {"text": "x" * 9000}, context=CTX,
        )
        assert res["success"] is True
        assert conn._api_post.await_count >= 2
        payload = conn._api_post.await_args.args[0]
        assert payload["to"] == BOUND_CHAT

    @pytest.mark.asyncio
    async def test_chat_allowlist_rejection(self):
        conn = make_whatsapp()
        res = await conn.execute_action(
            "send_message", {"text": "hi", "chat_id": FORBIDDEN_CHAT}, context=CTX,
        )
        assert res["success"] is False
        assert "not permitted" in res["error"]
        conn._api_post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_reaction(self):
        conn = make_whatsapp()
        res = await conn.execute_action(
            "send_reaction", {"message_id": "wamid.1", "emoji": "\U0001F44D"}, context=CTX,
        )
        assert res["success"] is True
        payload = conn._api_post.await_args.args[0]
        assert payload["type"] == "reaction"
        assert payload["reaction"]["emoji"] == "\U0001F44D"

    @pytest.mark.asyncio
    async def test_remove_reaction_sends_empty_emoji(self):
        conn = make_whatsapp()
        res = await conn.execute_action(
            "remove_reaction", {"message_id": "wamid.1"}, context=CTX,
        )
        assert res["success"] is True
        payload = conn._api_post.await_args.args[0]
        assert payload["reaction"]["emoji"] == ""

    @pytest.mark.asyncio
    async def test_get_phone_number_info(self):
        conn = make_whatsapp()
        conn._api_get_phone_number = AsyncMock(return_value=_resp(body={
            "id": "pn1", "verified_name": "Acme", "quality_rating": "GREEN",
        }))
        res = await conn.execute_action("get_phone_number_info", {}, context=CTX)
        assert res["success"] is True
        assert res["phone_number"]["verified_name"] == "Acme"

    @pytest.mark.asyncio
    async def test_http_error_surfaces(self):
        conn = make_whatsapp()
        conn._api_post = AsyncMock(return_value=_resp(status=401, text="unauthorized"))
        res = await conn.execute_action("send_message", {"text": "hi"}, context=CTX)
        assert res["success"] is False
        assert "401" in res["error"]

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_maps_to_send(self):
        conn = make_whatsapp()
        res = await conn.execute_tool(
            "whatsapp_send", {"platform_chat_id": "1555000", "text": "hi"},
        )
        assert res["success"] is True

    @pytest.mark.asyncio
    async def test_category_gating(self):
        conn = make_whatsapp(config={
            "phone_number_id": "pn1", "access_token": "tok", "verify_token": "vt",
            "enabled_action_categories": ["messages"],
        })
        avail = await conn.get_action_availability(CTX)
        assert avail["send_message"][0] is True
        ok, reason = avail["send_template"]
        assert ok is False and "disabled" in reason


class TestWhatsAppApprovalFlow:
    @pytest.mark.asyncio
    async def test_approve_via_button_reply(self):
        conn = make_whatsapp()
        # No destructive actions exist yet, but the approval mechanism itself
        # must still work generically for any future destructive action.
        action = whatsapp_actions.ACTIONS_BY_ID["send_template"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"template_name": "t"}, timeout=5)
        )
        await asyncio.sleep(0.05)
        conn._api_post.assert_awaited_once()
        request_id = next(iter(conn._pending_action_approvals))

        await conn._process_webhook_message({
            "type": "interactive",
            "interactive": {"button_reply": {"id": f"actreq:{request_id}:approve"}},
        }, {})

        approved, reason = await task
        assert approved is True
        assert conn._pending_action_approvals == {}

    @pytest.mark.asyncio
    async def test_deny_via_button_reply(self):
        conn = make_whatsapp()
        action = whatsapp_actions.ACTIONS_BY_ID["send_template"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"template_name": "t"}, timeout=5)
        )
        await asyncio.sleep(0.05)
        request_id = next(iter(conn._pending_action_approvals))

        await conn._process_webhook_message({
            "type": "interactive",
            "interactive": {"button_reply": {"id": f"actreq:{request_id}:deny"}},
        }, {})

        approved, reason = await task
        assert approved is False

    @pytest.mark.asyncio
    async def test_timeout_denies(self):
        conn = make_whatsapp()
        action = whatsapp_actions.ACTIONS_BY_ID["send_template"]
        approved, reason = await conn.request_action_approval(
            BOUND_CHAT, action, {"template_name": "t"}, timeout=0.05,
        )
        assert approved is False
        assert "cancelled" in reason


# ---------------------------------------------------------------------------
# Gateway registration — messaging connectors are session-linked only
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
    @pytest.mark.parametrize("connector_type", ["telegram", "signal", "whatsapp"])
    def test_linked_session_gets_pair(self, connector_type):
        with _patched_db(connector_type):
            tools = build_gateway_tools("ai-1")
        assert {t.name for t in tools} == {
            f"{connector_type}_actions", f"{connector_type}_invoke",
        }

    @pytest.mark.parametrize("make_conn,connector_type", [
        (make_telegram, "telegram"),
        (make_signal, "signal"),
        (make_whatsapp, "whatsapp"),
    ])
    def test_messaging_connector_never_registers_globally(self, make_conn, connector_type):
        conn = make_conn()

        def fake_running(ctype=None):
            return [("c1", conn)] if ctype in (None, connector_type) else []

        with _patched_db(None), patch(
            "app.core.connectors.manager.connector_manager.get_running_connectors",
            side_effect=fake_running,
        ):
            assert build_gateway_tools("ai-1") == []

    @pytest.mark.asyncio
    async def test_linked_invoke_resolves_bound_session(self):
        conn = make_telegram()

        def fake_get_instance(cid):
            return conn if cid == "c1" else None

        with _patched_db("telegram"), patch(
            "app.core.connectors.manager.connector_manager.get_running_instance",
            side_effect=fake_get_instance,
        ):
            tools = {t.name: t.handler for t in build_gateway_tools("ai-1")}
            res = _payload(await tools["telegram_invoke"]({
                "action": "send_message", "params": {"text": "hi"},
            }))
        assert res["success"] is True
        conn._bot.send_message.assert_awaited_once()
        assert conn._bot.send_message.await_args.kwargs["chat_id"] == int(BOUND_CHAT)

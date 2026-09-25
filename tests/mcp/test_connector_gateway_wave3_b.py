"""Tests for the wave-3b action-gateway conversions: Microsoft Teams, LINE,
iMessage, BlueBubbles.

Covers catalog integrity and legacy-id preservation, handler sampling for each
platform against faked clients/RPC (no network, no MCP transport), the
per-platform destructive-action approval flows (Adaptive Card buttons for
Teams, Flex Message postback for LINE, plain-text reply for BlueBubbles), and
gateway session-linked registration.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.connectors import (
    bluebubbles_actions,
    imessage_actions,
    line_actions,
    msteams_actions,
)
from app.core.connectors.bluebubbles import BlueBubblesConnector
from app.core.connectors.imessage import IMessageConnector
from app.core.connectors.line import LINEConnector
from app.core.connectors.msteams import MSTeamsConnector
from app.mcp.connector_gateway_sdk import build_gateway_tools, get_gateway_tool_names

BOUND_CHAT = "conv-0000001"
FORBIDDEN_CHAT = "conv-9999999"
CTX = {"session_id": "ai-1", "connector_id": "c1", "bound_chat_id": BOUND_CHAT}


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _async_cm(client: MagicMock) -> MagicMock:
    """Wrap a mock client as an async-context-manager (for `async with X() as c`)."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


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
        (msteams_actions, MSTeamsConnector, {"send_message"}),
        (line_actions, LINEConnector, {"send_message"}),
        (imessage_actions, IMessageConnector, {"send_message"}),
        (bluebubbles_actions, BlueBubblesConnector, {"send_message"}),
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
        for t in ("msteams", "line", "imessage", "bluebubbles"):
            assert f"mcp__builtins__{t}_actions" in names
            assert f"mcp__builtins__{t}_invoke" in names

    def test_imessage_has_no_destructive_actions(self):
        # imsg has no delete/ban-equivalent RPC — nothing here should need
        # the (unimplemented) approval flow.
        assert [a.id for a in imessage_actions.IMESSAGE_ACTIONS if a.risk == "destructive"] == []

    def test_other_three_have_exactly_one_destructive_action(self):
        for catalog in (
            msteams_actions.MSTEAMS_ACTIONS,
            line_actions.LINE_ACTIONS,
            bluebubbles_actions.BLUEBUBBLES_ACTIONS,
        ):
            destructive = [a.id for a in catalog if a.risk == "destructive"]
            assert len(destructive) == 1


# ---------------------------------------------------------------------------
# Microsoft Teams
# ---------------------------------------------------------------------------

def make_msteams(config=None):
    conn = MSTeamsConnector(
        connector_id="c1",
        connector_type="msteams",
        config=config or {"app_id": "app", "app_password": "pw"},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._conversation_refs[BOUND_CHAT] = {
        "service_url": "https://smba.example.com/teams",
        "conversation": {"id": BOUND_CHAT},
    }
    conn._ensure_token = AsyncMock(return_value="tok")
    conn._send_activity = AsyncMock(return_value=True)
    return conn


class TestTeamsHandlers:
    @pytest.mark.asyncio
    async def test_send_message(self):
        conn = make_msteams()
        res = await conn.execute_action("send_message", {"text": "hi"}, context=CTX)
        assert res["success"] is True
        conn._send_activity.assert_awaited()

    @pytest.mark.asyncio
    async def test_send_message_no_ref_fails(self):
        conn = make_msteams()
        res = await conn.execute_action(
            "send_message", {"text": "hi", "platform_chat_id": FORBIDDEN_CHAT}, context=CTX,
        )
        assert res["success"] is False

    @pytest.mark.asyncio
    async def test_send_card(self):
        conn = make_msteams()
        res = await conn.execute_action(
            "send_card", {"card": {"body": [{"type": "TextBlock", "text": "hi"}]}}, context=CTX,
        )
        assert res["success"] is True
        activity = conn._send_activity.await_args.args[2]
        assert activity["attachments"][0]["content"]["type"] == "AdaptiveCard"

    @pytest.mark.asyncio
    async def test_update_message(self):
        conn = make_msteams()
        client = MagicMock()
        client.request = AsyncMock(return_value=_resp(200))
        with patch("app.core.connectors.msteams_actions.httpx.AsyncClient", return_value=_async_cm(client)):
            res = await conn.execute_action(
                "update_message", {"activity_id": "a1", "text": "edited"}, context=CTX,
            )
        assert res["success"] is True
        method, url = client.request.await_args.args[:2]
        assert method == "PUT"
        assert "a1" in url

    @pytest.mark.asyncio
    async def test_delete_message(self):
        conn = make_msteams()
        client = MagicMock()
        client.request = AsyncMock(return_value=_resp(200))
        with patch("app.core.connectors.msteams_actions.httpx.AsyncClient", return_value=_async_cm(client)):
            res = await conn.execute_action("delete_message", {"activity_id": "a1"}, context=CTX)
        assert res["success"] is True
        assert client.request.await_args.args[0] == "DELETE"

    @pytest.mark.asyncio
    async def test_list_members(self):
        conn = make_msteams()
        client = MagicMock()
        client.request = AsyncMock(return_value=_resp(200, body=[{"id": "u1", "name": "Alice"}]))
        with patch("app.core.connectors.msteams_actions.httpx.AsyncClient", return_value=_async_cm(client)):
            res = await conn.execute_action("list_members", {}, context=CTX)
        assert res["success"] is True
        assert res["members"] == [{"id": "u1", "name": "Alice"}]

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_maps_to_send(self):
        conn = make_msteams()
        res = await conn.execute_tool(
            "msteams_send", {"platform_chat_id": BOUND_CHAT, "text": "hi"},
        )
        assert res["success"] is True

    @pytest.mark.asyncio
    async def test_category_gating(self):
        conn = make_msteams(config={
            "app_id": "a", "app_password": "p", "enabled_action_categories": ["messages"],
        })
        avail = await conn.get_action_availability(CTX)
        assert avail["send_message"][0] is True
        ok, reason = avail["list_members"]
        assert ok is False and "disabled" in reason


class TestTeamsApprovalFlow:
    @pytest.mark.asyncio
    async def test_approve_via_invoke_activity(self):
        conn = make_msteams()
        action = msteams_actions.ACTIONS_BY_ID["delete_message"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"activity_id": "a1"}, timeout=5)
        )
        await asyncio.sleep(0.05)
        conn._send_activity.assert_awaited_once()
        request_id = next(iter(conn._pending_action_approvals))

        await conn._handle_invoke_activity({
            "value": {"action": f"actreq:{request_id}:approve"},
        })

        approved, reason = await task
        assert approved is True
        assert conn._pending_action_approvals == {}

    @pytest.mark.asyncio
    async def test_deny_via_invoke_activity(self):
        conn = make_msteams()
        action = msteams_actions.ACTIONS_BY_ID["delete_message"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"activity_id": "a1"}, timeout=5)
        )
        await asyncio.sleep(0.05)
        request_id = next(iter(conn._pending_action_approvals))
        await conn._handle_invoke_activity({
            "value": {"action": f"actreq:{request_id}:deny"},
        })
        approved, reason = await task
        assert approved is False

    @pytest.mark.asyncio
    async def test_timeout_denies(self):
        conn = make_msteams()
        action = msteams_actions.ACTIONS_BY_ID["delete_message"]
        approved, reason = await conn.request_action_approval(
            BOUND_CHAT, action, {"activity_id": "a1"}, timeout=0.05,
        )
        assert approved is False
        assert "cancelled" in reason

    @pytest.mark.asyncio
    async def test_no_conversation_ref_fails_cleanly(self):
        conn = make_msteams()
        action = msteams_actions.ACTIONS_BY_ID["delete_message"]
        approved, reason = await conn.request_action_approval(
            FORBIDDEN_CHAT, action, {"activity_id": "a1"}, timeout=1,
        )
        assert approved is False
        assert "conversation reference" in reason


# ---------------------------------------------------------------------------
# LINE
# ---------------------------------------------------------------------------

def make_line(config=None):
    conn = LINEConnector(
        connector_id="c1",
        connector_type="line",
        config=config or {"channel_access_token": "tok", "channel_secret": "sec"},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._push_message = AsyncMock(return_value=True)
    return conn


class TestLineHandlers:
    @pytest.mark.asyncio
    async def test_send_message(self):
        conn = make_line()
        res = await conn.execute_action("send_message", {"text": "hi"}, context=CTX)
        assert res["success"] is True
        conn._push_message.assert_awaited_once()  # send_message() delegates to _push_message

    @pytest.mark.asyncio
    async def test_send_sticker(self):
        conn = make_line()
        res = await conn.execute_action(
            "send_sticker", {"package_id": "1", "sticker_id": "2"}, context=CTX,
        )
        assert res["success"] is True
        conn._push_message.assert_awaited_once()
        messages = conn._push_message.await_args.args[1]
        assert messages[0]["type"] == "sticker"

    @pytest.mark.asyncio
    async def test_send_image(self):
        conn = make_line()
        res = await conn.execute_action(
            "send_image", {"image_url": "https://x/y.png"}, context=CTX,
        )
        assert res["success"] is True
        messages = conn._push_message.await_args.args[1]
        assert messages[0]["previewImageUrl"] == "https://x/y.png"

    @pytest.mark.asyncio
    async def test_target_scoping_rejects_disallowed_chat(self):
        conn = make_line(config={
            "channel_access_token": "t", "channel_secret": "s",
        })
        res = await conn.execute_action(
            "send_message", {"text": "hi", "platform_chat_id": FORBIDDEN_CHAT}, context=CTX,
        )
        assert res["success"] is False
        assert "not permitted" in res["error"]

    @pytest.mark.asyncio
    async def test_get_profile(self):
        conn = make_line()
        client = MagicMock()
        client.get = AsyncMock(return_value=_resp(200, body={
            "userId": "u1", "displayName": "Alice",
        }))
        with patch("app.core.connectors.line_actions.httpx.AsyncClient", return_value=_async_cm(client)):
            res = await conn.execute_action("get_profile", {"user_id": "u1"}, context=CTX)
        assert res["success"] is True
        assert res["profile"]["display_name"] == "Alice"

    @pytest.mark.asyncio
    async def test_get_quota(self):
        conn = make_line()
        client = MagicMock()
        client.get = AsyncMock(return_value=_resp(200, body={"type": "limited", "value": 1000}))
        with patch("app.core.connectors.line_actions.httpx.AsyncClient", return_value=_async_cm(client)):
            res = await conn.execute_action("get_quota", {}, context=CTX)
        assert res["success"] is True
        assert res["quota"]["value"] == 1000

    @pytest.mark.asyncio
    async def test_leave_group_refuses_bound_chat(self):
        conn = make_line()
        res = await conn.execute_action("leave_group", {"group_id": BOUND_CHAT}, context=CTX)
        assert res["success"] is False
        assert "bound" in res["error"]

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_maps_to_send(self):
        conn = make_line()
        res = await conn.execute_tool(
            "line_send", {"platform_chat_id": BOUND_CHAT, "text": "hi"},
        )
        assert res["success"] is True


class TestLineApprovalFlow:
    @pytest.mark.asyncio
    async def test_approve_via_postback(self):
        conn = make_line()
        action = line_actions.ACTIONS_BY_ID["leave_group"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"group_id": "g2"}, timeout=5)
        )
        await asyncio.sleep(0.05)
        conn._push_message.assert_awaited_once()
        request_id = next(iter(conn._pending_action_approvals))

        await conn._process_postback_event({
            "postback": {"data": f"actreq:{request_id}:approve"},
        })

        approved, reason = await task
        assert approved is True
        assert conn._pending_action_approvals == {}

    @pytest.mark.asyncio
    async def test_deny_via_postback(self):
        conn = make_line()
        action = line_actions.ACTIONS_BY_ID["leave_group"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {"group_id": "g2"}, timeout=5)
        )
        await asyncio.sleep(0.05)
        request_id = next(iter(conn._pending_action_approvals))
        await conn._process_postback_event({
            "postback": {"data": f"actreq:{request_id}:deny"},
        })
        approved, reason = await task
        assert approved is False

    @pytest.mark.asyncio
    async def test_timeout_denies(self):
        conn = make_line()
        action = line_actions.ACTIONS_BY_ID["leave_group"]
        approved, reason = await conn.request_action_approval(
            BOUND_CHAT, action, {"group_id": "g2"}, timeout=0.05,
        )
        assert approved is False
        assert "cancelled" in reason


# ---------------------------------------------------------------------------
# iMessage
# ---------------------------------------------------------------------------

def make_imessage(config=None):
    conn = IMessageConnector(
        connector_id="c1",
        connector_type="imessage",
        config=config or {},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._rpc = MagicMock()
    conn._rpc.request = AsyncMock(return_value=None)
    conn.send_message = AsyncMock(return_value=True)
    return conn


class TestIMessageHandlers:
    @pytest.mark.asyncio
    async def test_send_message(self):
        conn = make_imessage()
        res = await conn.execute_action("send_message", {"text": "hi"}, context=CTX)
        assert res["success"] is True
        conn.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_message_no_target_fails(self):
        conn = make_imessage()
        res = await conn.execute_action("send_message", {"text": "hi"}, context={})
        assert res["success"] is False

    @pytest.mark.asyncio
    async def test_send_tapback_validates_reaction(self):
        conn = make_imessage()
        res = await conn.execute_action(
            "send_tapback", {"message_guid": "m1", "reaction": "bogus"}, context=CTX,
        )
        assert res["success"] is False
        assert "reaction must be" in res["error"]

    @pytest.mark.asyncio
    async def test_send_tapback_ok(self):
        conn = make_imessage()
        res = await conn.execute_action(
            "send_tapback", {"message_guid": "m1", "reaction": "love"}, context=CTX,
        )
        assert res["success"] is True
        conn._rpc.request.assert_awaited_with("react", {"message_guid": "m1", "reaction": "love"})

    @pytest.mark.asyncio
    async def test_list_chats(self):
        conn = make_imessage()
        conn._rpc.request = AsyncMock(return_value=[{"id": "c1"}, {"id": "c2"}])
        res = await conn.execute_action("list_chats", {"limit": 5}, context=CTX)
        assert res["success"] is True
        assert res["count"] == 2

    @pytest.mark.asyncio
    async def test_get_chat(self):
        conn = make_imessage()
        conn._rpc.request = AsyncMock(return_value={"id": BOUND_CHAT, "name": "Group"})
        res = await conn.execute_action("get_chat", {}, context=CTX)
        assert res["success"] is True
        assert res["chat"]["name"] == "Group"

    @pytest.mark.asyncio
    async def test_send_typing(self):
        conn = make_imessage()
        res = await conn.execute_action("send_typing", {}, context=CTX)
        assert res["success"] is True
        conn._rpc.request.assert_awaited_with("typing", {"to": BOUND_CHAT})

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_maps_to_send(self):
        conn = make_imessage()
        res = await conn.execute_tool(
            "imessage_send", {"platform_chat_id": BOUND_CHAT, "text": "hi"},
        )
        assert res["success"] is True

    def test_no_approval_flow_implemented(self):
        # Base class default (deny) is fine since no destructive action exists.
        assert not hasattr(IMessageConnector, "request_action_approval") or (
            IMessageConnector.request_action_approval is not None
        )


# ---------------------------------------------------------------------------
# BlueBubbles
# ---------------------------------------------------------------------------

def make_bluebubbles(config=None):
    conn = BlueBubblesConnector(
        connector_id="c1",
        connector_type="bluebubbles",
        config=config or {"server_url": "http://bb:1234", "password": "pw"},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._resolve_chat_guid = AsyncMock(return_value="iMessage;-;+15551234567")
    conn._api_post = AsyncMock(return_value=_resp(200))
    conn._api_get = AsyncMock(return_value=_resp(200))
    conn.send_message = AsyncMock(return_value=True)
    return conn


class TestBlueBubblesHandlers:
    @pytest.mark.asyncio
    async def test_send_message(self):
        conn = make_bluebubbles()
        res = await conn.execute_action("send_message", {"text": "hi"}, context=CTX)
        assert res["success"] is True
        conn.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_tapback_validates_reaction(self):
        conn = make_bluebubbles()
        res = await conn.execute_action(
            "send_tapback", {"message_guid": "m1", "reaction": "nope"}, context=CTX,
        )
        assert res["success"] is False

    @pytest.mark.asyncio
    async def test_send_tapback_ok(self):
        conn = make_bluebubbles()
        res = await conn.execute_action(
            "send_tapback", {"message_guid": "m1", "reaction": "love"}, context=CTX,
        )
        assert res["success"] is True
        conn._api_post.assert_awaited_once()
        payload = conn._api_post.await_args.kwargs["json_data"]
        assert payload["reactionType"] == "LOVE"

    @pytest.mark.asyncio
    async def test_list_chats(self):
        conn = make_bluebubbles()
        conn._api_post = AsyncMock(return_value=_resp(200, body={
            "data": [{"guid": "g1", "displayName": "Group", "participants": [1, 2]}],
        }))
        res = await conn.execute_action("list_chats", {}, context=CTX)
        assert res["success"] is True
        assert res["chats"][0]["participants"] == 2

    @pytest.mark.asyncio
    async def test_get_chat(self):
        conn = make_bluebubbles()
        conn._api_get = AsyncMock(return_value=_resp(200, body={
            "data": {"guid": "g1", "displayName": "Group", "participants": []},
        }))
        res = await conn.execute_action("get_chat", {}, context=CTX)
        assert res["success"] is True
        assert res["chat"]["display_name"] == "Group"

    @pytest.mark.asyncio
    async def test_rename_chat(self):
        conn = make_bluebubbles()
        client = MagicMock()
        client.put = AsyncMock(return_value=_resp(200))
        with patch("app.core.connectors.bluebubbles_actions.httpx.AsyncClient", return_value=_async_cm(client)):
            res = await conn.execute_action(
                "rename_chat", {"display_name": "New Name"}, context=CTX,
            )
        assert res["success"] is True
        assert client.put.await_args.kwargs["json"] == {"displayName": "New Name"}

    @pytest.mark.asyncio
    async def test_leave_chat_refuses_bound_chat(self):
        conn = make_bluebubbles()
        conn._resolve_chat_guid = AsyncMock(return_value=BOUND_CHAT)
        res = await conn.execute_action("leave_chat", {}, context=CTX)
        assert res["success"] is False
        assert "bound" in res["error"]

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_maps_to_send(self):
        conn = make_bluebubbles()
        res = await conn.execute_tool(
            "bluebubbles_send", {"platform_chat_id": BOUND_CHAT, "text": "hi"},
        )
        assert res["success"] is True


class TestBlueBubblesApprovalFlow:
    @pytest.mark.asyncio
    async def test_approve_via_text_reply(self):
        conn = make_bluebubbles()
        action = bluebubbles_actions.ACTIONS_BY_ID["leave_chat"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {}, timeout=5)
        )
        await asyncio.sleep(0.05)
        conn.send_message.assert_awaited_once()
        assert conn._pending_action_approvals

        handled = conn._check_permission_response("approve")
        assert handled is True

        approved, reason = await task
        assert approved is True
        assert conn._pending_action_approvals == {}

    @pytest.mark.asyncio
    async def test_deny_via_text_reply(self):
        conn = make_bluebubbles()
        action = bluebubbles_actions.ACTIONS_BY_ID["leave_chat"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {}, timeout=5)
        )
        await asyncio.sleep(0.05)
        handled = conn._check_permission_response("deny")
        assert handled is True
        approved, reason = await task
        assert approved is False

    @pytest.mark.asyncio
    async def test_action_approval_takes_priority_over_legacy_permission_prompt(self):
        conn = make_bluebubbles()
        conn._permission_sessions["legacy-req"] = "sess-1"
        action = bluebubbles_actions.ACTIONS_BY_ID["leave_chat"]
        task = asyncio.ensure_future(
            conn.request_action_approval(BOUND_CHAT, action, {}, timeout=5)
        )
        await asyncio.sleep(0.05)
        conn._check_permission_response("approve")
        approved, _ = await task
        assert approved is True
        # Legacy permission session must be untouched — action approvals are
        # resolved first and don't consume the legacy queue.
        assert "legacy-req" in conn._permission_sessions

    @pytest.mark.asyncio
    async def test_timeout_denies(self):
        conn = make_bluebubbles()
        action = bluebubbles_actions.ACTIONS_BY_ID["leave_chat"]
        approved, reason = await conn.request_action_approval(
            BOUND_CHAT, action, {}, timeout=0.05,
        )
        assert approved is False
        assert "cancelled" in reason


# ---------------------------------------------------------------------------
# Gateway registration — session-linked (all four are messaging connectors)
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
    @pytest.mark.parametrize("ctype", ["msteams", "line", "imessage", "bluebubbles"])
    def test_linked_session_gets_pair(self, ctype):
        with _patched_db(ctype):
            tools = build_gateway_tools("ai-1")
        assert {t.name for t in tools} == {f"{ctype}_actions", f"{ctype}_invoke"}

    def test_unlinked_session_gets_nothing(self):
        with _patched_db(None):
            tools = build_gateway_tools("ai-1")
        assert tools == []

    @pytest.mark.asyncio
    async def test_actions_tool_lists_teams_catalog(self):
        teams = make_msteams()

        def fake_running(cid):
            return teams if cid == "c1" else None

        with _patched_db("msteams"), patch(
            "app.core.connectors.manager.connector_manager.get_running_instance",
            side_effect=fake_running,
        ):
            tools = {t.name: t.handler for t in build_gateway_tools("ai-1")}
            res = _payload(await tools["msteams_actions"]({}))
        assert res["success"] is True
        ids = {a["id"] for a in res["actions"]}
        assert {"send_message", "send_card", "list_members"} <= ids

    @pytest.mark.asyncio
    async def test_invoke_tool_dispatches_to_bluebubbles(self):
        bb = make_bluebubbles()

        def fake_running(cid):
            return bb if cid == "c1" else None

        with _patched_db("bluebubbles"), patch(
            "app.core.connectors.manager.connector_manager.get_running_instance",
            side_effect=fake_running,
        ):
            tools = {t.name: t.handler for t in build_gateway_tools("ai-1")}
            res = _payload(await tools["bluebubbles_invoke"]({
                "action": "send_message", "params": {"text": "hi"},
            }))
        assert res["success"] is True
        bb.send_message.assert_awaited_once()

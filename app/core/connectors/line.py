"""LINEConnector — LINE Messaging API integration via linebot-sdk."""

import hashlib
import hmac
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.connectors.base import (
    ActionDef,
    BaseConnector,
    ConnectorCapabilities,
    ConnectorStatus,
    IncomingMessage,
    OutgoingMessage,
    PermissionPrompt,
    IncomingMessageCallback,
    PermissionResponseCallback,
    ToolDef,
    TriggerDef,
    chunk_text,
)

logger = logging.getLogger(__name__)

LINE_API_BASE = "https://api.line.me/v2"
LINE_API_DATA = "https://api-data.line.me/v2"
LINE_API_MESSAGING = f"{LINE_API_BASE}/bot/message"


class LINEConnector(BaseConnector):
    """
    Connector for LINE using the Messaging API (HTTP/webhook-based).

    LINE does not support persistent polling; incoming messages are
    received via webhooks. This connector handles outbound messages
    and provides a ``handle_webhook_payload`` method for the webhook
    route to call.

    Config keys:
        channel_access_token (str): LINE channel access token.
        channel_secret (str): LINE channel secret for webhook signature validation.
        allowed_user_ids (list[str], optional): If set, only messages
            from these LINE user IDs will be processed.

    MAX_MESSAGE_LENGTH is set to 5000 per LINE's text message limit.
        allowed_group_ids (list[str], optional): If set, only messages
            from these LINE group IDs will be processed.
    """

    MAX_MESSAGE_LENGTH = 5000
    CAPABILITIES = ConnectorCapabilities(
        buttons=True, rich_text=True,
    )

    TRIGGERS = [
        TriggerDef(
            id="message_received",
            name="Message Received",
            description="Fires when a new message is received",
            payload_schema={
                "type": "object",
                "properties": {
                    "platform_chat_id": {"type": "string"},
                    "platform_user_id": {"type": "string"},
                    "platform_user_name": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        ),
    ]

    ACTIONS = [
        ActionDef(
            id="send_message",
            name="Send Message",
            description="Send a message to a chat",
            input_schema={
                "type": "object",
                "properties": {
                    "platform_chat_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["platform_chat_id", "text"],
            },
        ),
    ]

    TOOLS = [
        ToolDef(
            id="line_send",
            name="Send a message via LINE",
            description="Send a message via LINE",
            input_schema={
                "type": "object",
                "properties": {
                    "platform_chat_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["platform_chat_id", "text"],
            },
        ),
    ]

    def __init__(
        self,
        connector_id: str,
        connector_type: str,
        config: Dict[str, Any],
        on_incoming_message: IncomingMessageCallback,
        on_permission_response: PermissionResponseCallback,
        **kwargs,
    ):
        super().__init__(
            connector_id, connector_type, config,
            on_incoming_message, on_permission_response,
            **kwargs,
        )
        self._permission_sessions: Dict[str, str] = {}  # request_id -> session_id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config['channel_access_token']}",
            "Content-Type": "application/json",
        }

    def validate_signature(self, body: str, signature: str) -> bool:
        """Validate a LINE webhook signature (HMAC-SHA256, base64)."""
        import base64

        secret = self.config["channel_secret"]
        computed = hmac.new(
            secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        computed_b64 = base64.b64encode(computed).decode("utf-8")

        # Timing-safe comparison
        expected = computed_b64.encode("utf-8")
        actual = signature.encode("utf-8")
        if len(expected) != len(actual):
            return False
        return hmac.compare_digest(expected, actual)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _do_connect(self) -> None:
        """Validate the channel access token by fetching bot info."""
        url = f"{LINE_API_BASE}/bot/info"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=self._headers())

        if response.status_code != 200:
            raise RuntimeError(
                f"API returned {response.status_code}: {response.text[:200]}"
            )

    async def disconnect(self) -> None:
        """Mark the connector as disconnected (webhook-based, no persistent connection)."""
        self._status = ConnectorStatus.DISCONNECTED
        self._status_message = None
        logger.info("LINEConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def _push_message(
        self, to: str, messages: List[Dict[str, Any]]
    ) -> bool:
        """Push messages to a LINE user or group."""
        try:
            payload = {
                "to": to,
                "messages": messages,
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{LINE_API_MESSAGING}/push",
                    headers=self._headers(),
                    json=payload,
                )
            if 200 <= response.status_code < 300:
                return True

            logger.error(
                "LINE push_message failed (%s): %s",
                response.status_code, response.text[:200],
            )
            return False

        except Exception as exc:
            logger.exception("Failed to push LINE message to %s: %s", to, exc)
            return False

    async def _reply_message(
        self, reply_token: str, messages: List[Dict[str, Any]]
    ) -> bool:
        """Reply to a webhook event using a reply token."""
        try:
            payload = {
                "replyToken": reply_token,
                "messages": messages,
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{LINE_API_MESSAGING}/reply",
                    headers=self._headers(),
                    json=payload,
                )
            if 200 <= response.status_code < 300:
                return True

            logger.error(
                "LINE reply_message failed (%s): %s",
                response.status_code, response.text[:200],
            )
            return False

        except Exception as exc:
            logger.exception("Failed to reply LINE message: %s", exc)
            return False

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a text message to a LINE user or group via push."""
        chunks = chunk_text(message.text, self.MAX_MESSAGE_LENGTH)
        messages = [{"type": "text", "text": t} for t in chunks]

        # Add quick-reply buttons to the last message if provided
        if message.buttons:
            items = []
            for btn in message.buttons:
                items.append({
                    "type": "action",
                    "action": {
                        "type": "message",
                        "label": btn["label"][:20],  # LINE limits to 20 chars
                        "text": btn.get("callback_data", btn["label"]),
                    },
                })
            if items:
                messages[-1]["quickReply"] = {"items": items[:13]}  # LINE max 13

        return await self._push_message(message.platform_chat_id, messages)

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send a Flex Message with Approve / Deny buttons."""
        self._permission_sessions[prompt.request_id] = prompt.session_id

        # Use a Flex Message bubble for a rich permission prompt
        flex_contents = {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "Permission Request",
                        "weight": "bold",
                        "size": "lg",
                        "color": "#111111",
                    },
                    {
                        "type": "text",
                        "text": f"Tool: {prompt.tool_name}",
                        "size": "sm",
                        "color": "#444444",
                        "wrap": True,
                    },
                    {
                        "type": "text",
                        "text": prompt.description,
                        "size": "sm",
                        "color": "#666666",
                        "wrap": True,
                    },
                ],
            },
            "footer": {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "color": "#06C755",
                        "action": {
                            "type": "postback",
                            "label": "Approve",
                            "data": f"perm:{prompt.request_id}:approve",
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "postback",
                            "label": "Deny",
                            "data": f"perm:{prompt.request_id}:deny",
                        },
                    },
                ],
            },
        }

        messages = [
            {
                "type": "flex",
                "altText": f"Permission Request: {prompt.tool_name}",
                "contents": flex_contents,
            }
        ]

        return await self._push_message(platform_chat_id, messages)

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a plain text notification message."""
        indicators = {
            "session.started": "\u25b6\ufe0f",
            "session.complete": "\u2705",
            "session.error": "\u274c",
            "permission.requested": "\ud83d\udd10",
        }
        icon = indicators.get(event_type, "\ud83d\udce3")

        if event_type == "session.complete":
            duration = data.get("duration_seconds", 0)
            cost = data.get("total_cost", 0)
            text = (
                f"{icon} Session Complete\n\n"
                f"Duration: {duration:.1f}s\n"
                f"Cost: ${cost:.4f}"
            )
        elif event_type == "session.error":
            error_msg = data.get("error_message", "Unknown error")
            text = f"{icon} Session Error\n\n{error_msg}"
        else:
            text = f"{icon} {event_type}\n\n{data}"

        messages = [{"type": "text", "text": text}]
        return await self._push_message(platform_chat_id, messages)

    # ------------------------------------------------------------------
    # Webhook handling (inbound)
    # ------------------------------------------------------------------

    async def handle_webhook_payload(
        self, payload: Dict[str, Any], reply_token_map: Optional[Dict[str, str]] = None
    ) -> None:
        """
        Parse an incoming LINE webhook payload and dispatch messages
        or postback events (permission responses).

        Args:
            payload: The parsed JSON body from LINE's webhook POST.
            reply_token_map: Optional mapping of event types to reply tokens.
        """
        try:
            events = payload.get("events", [])
            for event in events:
                event_type = event.get("type", "")

                if event_type == "message":
                    await self._process_message_event(event)
                elif event_type == "postback":
                    await self._process_postback_event(event)

        except Exception as exc:
            logger.exception("Error processing LINE webhook payload: %s", exc)

    async def _process_message_event(self, event: Dict[str, Any]) -> None:
        """Process a message event from the LINE webhook."""
        source = event.get("source", {})
        source_type = source.get("type", "")
        user_id = source.get("userId", "")

        # Determine chat ID based on source type
        if source_type == "group":
            chat_id = source.get("groupId", "")
        elif source_type == "room":
            chat_id = source.get("roomId", "")
        else:
            chat_id = user_id

        # Filter by allowed user/group IDs
        allowed_users = self.config.get("allowed_user_ids")
        if allowed_users and user_id not in [str(u) for u in allowed_users]:
            logger.debug("Ignoring LINE message from non-allowed user %s", user_id)
            return

        allowed_groups = self.config.get("allowed_group_ids")
        if source_type == "group" and allowed_groups:
            group_id = source.get("groupId", "")
            if group_id not in [str(g) for g in allowed_groups]:
                logger.debug("Ignoring LINE message from non-allowed group %s", group_id)
                return

        message = event.get("message", {})
        msg_type = message.get("type", "")

        # Extract text from different message types
        if msg_type == "text":
            text = message.get("text", "")
        elif msg_type == "location":
            title = message.get("title", "")
            address = message.get("address", "")
            lat = message.get("latitude", "")
            lon = message.get("longitude", "")
            text = f"[Location] {title} {address} ({lat}, {lon})"
        elif msg_type == "sticker":
            text = "[Sticker]"
        elif msg_type in ("image", "video", "audio", "file"):
            text = f"[{msg_type.capitalize()}]"
        else:
            text = f"[{msg_type}]"

        # Try to get user display name
        user_name = user_id
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                if source_type == "group":
                    url = f"{LINE_API_BASE}/bot/group/{source.get('groupId')}/member/{user_id}"
                else:
                    url = f"{LINE_API_BASE}/bot/profile/{user_id}"
                resp = await client.get(url, headers=self._headers())
                if resp.status_code == 200:
                    user_name = resp.json().get("displayName", user_id)
        except Exception:
            logger.debug("Failed to resolve LINE user name for %s", user_id, exc_info=True)

        msg = IncomingMessage(
            connector_id=self.connector_id,
            platform_chat_id=chat_id,
            platform_user_id=user_id,
            platform_user_name=user_name,
            text=text,
            platform_message_id=message.get("id"),
            metadata={
                "source_type": source_type,
                "message_type": msg_type,
                "reply_token": event.get("replyToken"),
            },
        )
        await self._on_incoming_message(msg)

    async def _process_postback_event(self, event: Dict[str, Any]) -> None:
        """Process a postback event (button clicks) from LINE."""
        postback = event.get("postback", {})
        data = postback.get("data", "")

        if not data.startswith("perm:"):
            return

        parts = data.split(":")
        if len(parts) != 3:
            logger.warning("Invalid LINE postback data format: %s", data)
            return

        _, request_id, decision = parts
        approved = decision == "approve"

        session_id = self._permission_sessions.get(request_id, "")
        await self._on_permission_response(request_id, session_id, approved)

    # ------------------------------------------------------------------
    # Actions & Tools
    # ------------------------------------------------------------------

    async def execute_action(self, action_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a named action."""
        if action_id == "send_message":
            message = OutgoingMessage(
                platform_chat_id=params["platform_chat_id"],
                text=params["text"],
            )
            success = await self.send_message(message)
            if success:
                return {"success": True, "message": "Message sent"}
            return {"success": False, "error": "Failed to send message"}

        return {"success": False, "error": f"Unknown action: {action_id}"}

    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a named tool call."""
        if tool_id == "line_send":
            message = OutgoingMessage(
                platform_chat_id=params["platform_chat_id"],
                text=params["text"],
            )
            success = await self.send_message(message)
            if success:
                return {"success": True, "message": "Message sent"}
            return {"success": False, "error": "Failed to send message"}

        return {"success": False, "error": f"Unknown tool: {tool_id}"}

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        """Validate the channel access token by fetching bot info."""
        try:
            url = f"{LINE_API_BASE}/bot/info"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=self._headers())

            if response.status_code == 200:
                data = response.json()
                display_name = data.get("displayName", "Unknown")
                return {
                    "success": True,
                    "message": f"Authenticated as {display_name}",
                }

            return {
                "success": False,
                "message": f"API returned {response.status_code}: {response.text[:200]}",
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

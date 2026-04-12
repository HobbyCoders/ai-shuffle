"""BlueBubblesConnector — iMessage via BlueBubbles REST API (macOS server)."""

import asyncio
import json
import logging
import re
import uuid
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

# Tapback type code → emoji
REACTION_TYPE_MAP = {
    2000: "\u2764\ufe0f",   # love
    2001: "\ud83d\udc4d",   # like
    2002: "\ud83d\udc4e",   # dislike
    2003: "\ud83d\ude02",   # laugh
    2004: "\u203c\ufe0f",   # emphasize
    2005: "\u2753",         # question
}


class BlueBubblesConnector(BaseConnector):
    """
    Connector for iMessage via the BlueBubbles macOS REST API.

    Requires a running BlueBubbles server on macOS. Incoming messages
    are received via webhooks that BlueBubbles sends to a configured
    endpoint. Outgoing messages use the BlueBubbles REST API.

    Config keys:
        server_url (str): BlueBubbles server URL (e.g. "http://192.168.1.10:1234").
        password (str): BlueBubbles API password.
        webhook_path (str, optional): Path for incoming webhooks.
            Defaults to "/bluebubbles-webhook".
        allowed_handles (list[str], optional): If set, only messages
            from these phone numbers or emails will be processed.
    """

    MAX_MESSAGE_LENGTH = 4000
    CAPABILITIES = ConnectorCapabilities()

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
            id="bluebubbles_send",
            name="Send a message via BlueBubbles",
            description="Send a message via BlueBubbles",
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
        self._server_info: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _base_url(self) -> str:
        url = self.config["server_url"].rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return url

    @property
    def _password(self) -> str:
        return self.config["password"]

    def _params(self) -> Dict[str, str]:
        """Query params with password auth."""
        return {"password": self._password}

    async def _api_get(self, path: str, **kwargs) -> httpx.Response:
        """Make an authenticated GET request to the BlueBubbles API."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.get(
                f"{self._base_url}{path}",
                params=self._params(),
                **kwargs,
            )

    async def _api_post(
        self, path: str, json_data: Optional[Dict] = None, **kwargs
    ) -> httpx.Response:
        """Make an authenticated POST request to the BlueBubbles API."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(
                f"{self._base_url}{path}",
                params=self._params(),
                json=json_data,
                **kwargs,
            )

    async def _resolve_chat_guid(self, target: str) -> Optional[str]:
        """
        Resolve a target (handle, chat_guid, or chat_identifier)
        to a BlueBubbles chat GUID.

        Chat GUID format: ``service;(+|-);identifier``
        """
        # If it already looks like a GUID (contains ;), return directly
        if ";" in target:
            return target

        # Normalize phone numbers
        normalized = re.sub(r"[\s\-\(\)]", "", target)

        # Search existing chats
        offset = 0
        limit = 500
        max_chats = 5000

        while offset < max_chats:
            try:
                response = await self._api_post(
                    "/api/v1/chat/query",
                    json_data={
                        "limit": limit,
                        "offset": offset,
                        "with": ["participants"],
                    },
                )
                if response.status_code != 200:
                    break

                data = response.json()
                chats = data.get("data", [])
                if not chats:
                    break

                for chat in chats:
                    guid = chat.get("guid", "")
                    # Check if this is a DM chat (;-;)
                    if ";-;" in guid:
                        # Extract handle from GUID
                        parts = guid.split(";")
                        if len(parts) >= 3:
                            handle = parts[2]
                            handle_normalized = re.sub(r"[\s\-\(\)]", "", handle)
                            if handle_normalized == normalized or handle == target:
                                return guid

                        # Also check participants
                        participants = chat.get("participants", [])
                        for p in participants:
                            p_id = p.get("address", "") or p.get("id", "")
                            p_normalized = re.sub(r"[\s\-\(\)]", "", p_id)
                            if p_normalized == normalized or p_id == target:
                                return guid

                offset += limit

            except Exception as exc:
                logger.warning("Error querying BlueBubbles chats: %s", exc)
                break

        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _do_connect(self) -> None:
        """Validate the BlueBubbles server connection."""
        response = await self._api_get("/api/v1/server/info")

        if response.status_code != 200:
            raise RuntimeError(
                f"API returned {response.status_code}: {response.text[:200]}"
            )

        self._server_info = response.json().get("data", {})

    async def disconnect(self) -> None:
        """Mark as disconnected (webhook-based, no persistent connection)."""
        self._server_info = None
        self._status = ConnectorStatus.DISCONNECTED
        self._status_message = None
        logger.info("BlueBubblesConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a text message via BlueBubbles."""
        try:
            chat_guid = await self._resolve_chat_guid(message.platform_chat_id)

            if chat_guid:
                # Send to existing chat
                for chunk in chunk_text(message.text, self.MAX_MESSAGE_LENGTH):
                    payload = {
                        "chatGuid": chat_guid,
                        "tempGuid": str(uuid.uuid4()),
                        "message": chunk,
                    }
                    response = await self._api_post(
                        "/api/v1/message/text", json_data=payload
                    )
                    if response.status_code not in (200, 201):
                        logger.error(
                            "BlueBubbles send_message failed (%s): %s",
                            response.status_code, response.text[:200],
                        )
                        return False
                return True

            else:
                # Try to create a new DM chat
                payload = {
                    "addresses": [message.platform_chat_id],
                    "message": message.text,
                    "tempGuid": str(uuid.uuid4()),
                }
                response = await self._api_post(
                    "/api/v1/chat/new", json_data=payload
                )
                if response.status_code in (200, 201):
                    return True

                logger.error(
                    "BlueBubbles create chat + send failed (%s): %s",
                    response.status_code, response.text[:200],
                )
                return False

        except Exception as exc:
            logger.exception(
                "Failed to send BlueBubbles message to %s: %s",
                message.platform_chat_id, exc,
            )
            return False

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send a text-based permission prompt (iMessage has no interactive cards)."""
        self._permission_sessions[prompt.request_id] = prompt.session_id

        text = (
            f"Permission Request\n\n"
            f"Tool: {prompt.tool_name}\n"
            f"{prompt.description}\n\n"
            f"Reply 'approve' or 'deny'"
        )

        outgoing = OutgoingMessage(
            platform_chat_id=platform_chat_id,
            text=text,
        )
        return await self.send_message(outgoing)

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a text notification message."""
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

        outgoing = OutgoingMessage(
            platform_chat_id=platform_chat_id,
            text=text,
        )
        return await self.send_message(outgoing)

    # ------------------------------------------------------------------
    # Webhook handling (inbound)
    # ------------------------------------------------------------------

    async def handle_webhook_payload(self, payload: Dict[str, Any]) -> None:
        """
        Process an incoming BlueBubbles webhook payload.

        BlueBubbles sends webhooks for new-message, updated-message,
        and reaction events.
        """
        try:
            event_type = payload.get("type", "")

            # Extract message data from various payload structures
            msg_data = (
                payload.get("data")
                or payload.get("payload")
                or payload.get("message")
                or payload
            )
            if isinstance(msg_data, dict) and "message" in msg_data:
                msg_data = msg_data["message"]

            if event_type == "new-message":
                await self._handle_new_message(msg_data)
            elif event_type in ("updated-message", "message-reaction", "reaction"):
                # Reactions — log but don't dispatch as incoming messages
                logger.debug(
                    "BlueBubbles reaction event: %s", event_type,
                )

        except Exception as exc:
            logger.exception("Error processing BlueBubbles webhook: %s", exc)

    async def _handle_new_message(self, msg_data: Dict[str, Any]) -> None:
        """Process a new-message webhook event."""
        # Skip messages from self
        if msg_data.get("isFromMe") or msg_data.get("is_from_me"):
            return

        # Extract sender
        handle = msg_data.get("handle", {})
        if isinstance(handle, dict):
            sender = handle.get("address", "") or handle.get("id", "")
            sender_name = handle.get("displayName") or handle.get("name") or sender
        elif isinstance(handle, str):
            sender = handle
            sender_name = handle
        else:
            sender = msg_data.get("sender", "")
            sender_name = msg_data.get("senderName", sender)

        if not sender:
            return

        # Filter by allowed handles
        allowed = self.config.get("allowed_handles")
        if allowed:
            sender_normalized = re.sub(r"[\s\-\(\)]", "", sender)
            allowed_normalized = [re.sub(r"[\s\-\(\)]", "", str(h)) for h in allowed]
            if sender_normalized not in allowed_normalized:
                logger.debug(
                    "Ignoring BlueBubbles message from non-allowed handle %s",
                    sender,
                )
                return

        text = msg_data.get("text", "") or ""

        # Check for permission responses
        if text and self._check_permission_response(text):
            return

        # Determine chat context
        chats = msg_data.get("chats", [])
        chat = chats[0] if chats else {}
        chat_guid = chat.get("guid", "") or msg_data.get("chatGuid", "")
        chat_name = chat.get("displayName") or chat.get("chatIdentifier", "")

        # Determine if group
        is_group = False
        if ";+;" in chat_guid:
            is_group = True
        elif chat.get("participants") and len(chat.get("participants", [])) > 2:
            is_group = True

        message_guid = msg_data.get("guid", "") or msg_data.get("id", "")

        msg = IncomingMessage(
            connector_id=self.connector_id,
            platform_chat_id=chat_guid or sender,
            platform_user_id=sender,
            platform_user_name=sender_name,
            text=text,
            platform_message_id=message_guid,
            metadata={
                "is_group": is_group,
                "chat_guid": chat_guid,
                "chat_name": chat_name,
                "service": msg_data.get("service"),
            },
        )
        await self._on_incoming_message(msg)

    def _check_permission_response(self, text: str) -> bool:
        """Check if text is a simple permission response."""
        text_lower = text.strip().lower()
        for request_id, session_id in list(self._permission_sessions.items()):
            if text_lower in ("approve", "yes", "allow", "y"):
                asyncio.create_task(
                    self._on_permission_response(request_id, session_id, True)
                )
                del self._permission_sessions[request_id]
                return True
            elif text_lower in ("deny", "no", "reject", "n"):
                asyncio.create_task(
                    self._on_permission_response(request_id, session_id, False)
                )
                del self._permission_sessions[request_id]
                return True
        return False

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
        if tool_id == "bluebubbles_send":
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
        """Validate the BlueBubbles server connection."""
        try:
            response = await self._api_get("/api/v1/server/info")

            if response.status_code == 200:
                data = response.json().get("data", {})
                version = data.get("server_version", "unknown")
                os_version = data.get("os_version", "unknown")
                private_api = data.get("private_api", False)
                return {
                    "success": True,
                    "message": (
                        f"Connected to BlueBubbles v{version} "
                        f"(macOS {os_version}, "
                        f"Private API: {'enabled' if private_api else 'disabled'})"
                    ),
                }

            if response.status_code == 401:
                return {
                    "success": False,
                    "message": "Authentication failed — check password",
                }

            return {
                "success": False,
                "message": f"API returned {response.status_code}: {response.text[:200]}",
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

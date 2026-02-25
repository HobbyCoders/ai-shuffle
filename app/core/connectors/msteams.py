"""MSTeamsConnector — Microsoft Teams integration via Bot Framework REST API."""

import asyncio
import json
import logging
from typing import Any, Dict, Optional

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

# Bot Framework OAuth endpoint
BF_LOGIN_URL = "https://login.microsoftonline.com"
BF_API_URL = "https://smba.trafficmanager.net/teams"


class MSTeamsConnector(BaseConnector):
    """
    Connector for Microsoft Teams using the Bot Framework REST API.

    Uses OAuth2 client credentials flow (app_id + app_password) to
    authenticate with the Bot Framework and send proactive messages.
    Incoming messages are received via a webhook endpoint that the
    connector exposes.

    Config keys:
        app_id (str): Azure Bot / App Registration Application (Client) ID.
        app_password (str): Azure App Registration Client Secret.
        tenant_id (str, optional): Azure Tenant ID. Defaults to "botframework.com".
        webhook_port (int, optional): Port for the incoming webhook server.
            Defaults to 3978.
        allowed_user_ids (list[str], optional): If set, only messages from
            these AAD user IDs will be processed.
    """

    MAX_MESSAGE_LENGTH = 4000
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
            id="msteams_send",
            name="Send a message via Microsoft Teams",
            description="Send a message via Microsoft Teams",
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
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._permission_sessions: Dict[str, str] = {}  # request_id -> session_id
        # conversation_id -> ConversationReference (service_url, conversation, bot, etc.)
        self._conversation_refs: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _app_id(self) -> str:
        return self.config["app_id"]

    @property
    def _app_password(self) -> str:
        return self.config["app_password"]

    @property
    def _tenant_id(self) -> str:
        return self.config.get("tenant_id", "botframework.com")

    async def _ensure_token(self) -> str:
        """Obtain or refresh the Bot Framework OAuth2 access token."""
        import time

        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        url = f"{BF_LOGIN_URL}/{self._tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self._app_id,
            "client_secret": self._app_password,
            "scope": "https://api.botframework.com/.default",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, data=data)

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to get Bot Framework token: {response.status_code} {response.text[:200]}"
            )

        token_data = response.json()
        self._access_token = token_data["access_token"]
        self._token_expires_at = time.time() + token_data.get("expires_in", 3600)
        return self._access_token

    def _auth_headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Validate credentials by obtaining a Bot Framework token."""
        try:
            self._status = ConnectorStatus.CONNECTING
            await self._ensure_token()
            self._status = ConnectorStatus.CONNECTED
            self._status_message = None
            logger.info("MSTeamsConnector %s connected", self.connector_id)
            return True

        except Exception as exc:
            self._status = ConnectorStatus.ERROR
            self._status_message = str(exc)
            logger.exception("MSTeamsConnector %s failed to connect", self.connector_id)
            return False

    async def disconnect(self) -> None:
        """Mark as disconnected (webhook-based, no persistent connection)."""
        self._access_token = None
        self._token_expires_at = 0
        self._status = ConnectorStatus.DISCONNECTED
        self._status_message = None
        logger.info("MSTeamsConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def _send_activity(
        self, service_url: str, conversation_id: str, activity: Dict[str, Any]
    ) -> bool:
        """Send an activity to a Teams conversation via Bot Framework."""
        try:
            token = await self._ensure_token()
            url = (
                f"{service_url.rstrip('/')}/v3/conversations"
                f"/{conversation_id}/activities"
            )

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url,
                    headers=self._auth_headers(token),
                    json=activity,
                )

            if 200 <= response.status_code < 300:
                return True

            logger.error(
                "Teams send_activity failed (%s): %s",
                response.status_code, response.text[:200],
            )
            return False

        except Exception as exc:
            logger.exception("Failed to send Teams activity: %s", exc)
            return False

    def _get_ref(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Look up stored conversation reference."""
        return self._conversation_refs.get(conversation_id)

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a text message to a Teams conversation."""
        ref = self._get_ref(message.platform_chat_id)
        if not ref:
            logger.error(
                "No conversation reference for %s — "
                "the bot must receive a message first before it can send proactively",
                message.platform_chat_id,
            )
            return False

        service_url = ref.get("service_url", BF_API_URL)

        for text in chunk_text(message.text, self.MAX_MESSAGE_LENGTH):
            activity = {
                "type": "message",
                "text": text,
            }
            if not await self._send_activity(
                service_url, message.platform_chat_id, activity
            ):
                return False

        return True

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send an Adaptive Card with Approve / Deny buttons."""
        ref = self._get_ref(platform_chat_id)
        if not ref:
            logger.error(
                "No conversation reference for %s", platform_chat_id,
            )
            return False

        self._permission_sessions[prompt.request_id] = prompt.session_id
        service_url = ref.get("service_url", BF_API_URL)

        adaptive_card = {
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": "Permission Request",
                    "weight": "Bolder",
                    "size": "Medium",
                },
                {
                    "type": "TextBlock",
                    "text": f"**Tool:** {prompt.tool_name}",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "text": prompt.description,
                    "wrap": True,
                },
            ],
            "actions": [
                {
                    "type": "Action.Submit",
                    "title": "Approve",
                    "data": {
                        "action": f"perm:{prompt.request_id}:approve",
                    },
                    "style": "positive",
                },
                {
                    "type": "Action.Submit",
                    "title": "Deny",
                    "data": {
                        "action": f"perm:{prompt.request_id}:deny",
                    },
                    "style": "destructive",
                },
            ],
        }

        activity = {
            "type": "message",
            "text": f"Permission Request: {prompt.tool_name}",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": adaptive_card,
                }
            ],
        }

        return await self._send_activity(
            service_url, platform_chat_id, activity
        )

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a notification message to a Teams conversation."""
        ref = self._get_ref(platform_chat_id)
        if not ref:
            logger.error(
                "No conversation reference for %s", platform_chat_id,
            )
            return False

        service_url = ref.get("service_url", BF_API_URL)

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
                f"{icon} **Session Complete**\n\n"
                f"Duration: {duration:.1f}s\n\n"
                f"Cost: ${cost:.4f}"
            )
        elif event_type == "session.error":
            error_msg = data.get("error_message", "Unknown error")
            text = f"{icon} **Session Error**\n\n{error_msg}"
        else:
            text = f"{icon} **{event_type}**\n\n{data}"

        activity = {
            "type": "message",
            "text": text,
            "textFormat": "markdown",
        }

        return await self._send_activity(
            service_url, platform_chat_id, activity
        )

    # ------------------------------------------------------------------
    # Webhook handling (inbound)
    # ------------------------------------------------------------------

    async def handle_webhook_activity(self, activity: Dict[str, Any]) -> None:
        """
        Process an incoming Bot Framework activity from the webhook endpoint.

        Call this from the route that receives POST requests from the
        Bot Framework on ``/api/messages``.
        """
        try:
            activity_type = activity.get("type", "")

            # Store conversation reference for proactive messaging
            conversation = activity.get("conversation", {})
            conversation_id = conversation.get("id", "")
            if conversation_id:
                self._conversation_refs[conversation_id] = {
                    "service_url": activity.get("serviceUrl", BF_API_URL),
                    "conversation": conversation,
                    "bot": activity.get("recipient"),
                    "user": activity.get("from"),
                }

            if activity_type == "message":
                await self._handle_message_activity(activity)
            elif activity_type == "invoke":
                await self._handle_invoke_activity(activity)

        except Exception as exc:
            logger.exception("Error processing Teams webhook activity: %s", exc)

    async def _handle_message_activity(self, activity: Dict[str, Any]) -> None:
        """Process an incoming Teams message activity."""
        sender = activity.get("from", {})
        user_id = sender.get("aadObjectId") or sender.get("id", "")
        user_name = sender.get("name", user_id)

        # Filter by allowed user IDs
        allowed = self.config.get("allowed_user_ids")
        if allowed and user_id not in [str(u) for u in allowed]:
            logger.debug("Ignoring Teams message from non-allowed user %s", user_id)
            return

        conversation = activity.get("conversation", {})
        conversation_id = conversation.get("id", "")
        conversation_type = conversation.get("conversationType", "personal")

        # Strip mention tags: <at>Bot Name</at>
        text = activity.get("text", "")
        import re
        text = re.sub(r"<at>.*?</at>\s*", "", text).strip()

        msg = IncomingMessage(
            connector_id=self.connector_id,
            platform_chat_id=conversation_id,
            platform_user_id=user_id,
            platform_user_name=user_name,
            text=text,
            platform_message_id=activity.get("id"),
            metadata={
                "conversation_type": conversation_type,
                "tenant_id": conversation.get("tenantId"),
                "service_url": activity.get("serviceUrl"),
                "channel_id": activity.get("channelId"),
            },
        )
        await self._on_incoming_message(msg)

    async def _handle_invoke_activity(self, activity: Dict[str, Any]) -> None:
        """Handle invoke activities (Adaptive Card submit actions)."""
        value = activity.get("value", {})
        action_str = value.get("action", "")

        if not action_str.startswith("perm:"):
            return

        parts = action_str.split(":")
        if len(parts) != 3:
            logger.warning("Invalid Teams invoke action format: %s", action_str)
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
        if tool_id == "msteams_send":
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
        """Validate the app credentials by obtaining a Bot Framework token."""
        try:
            url = f"{BF_LOGIN_URL}/{self._tenant_id}/oauth2/v2.0/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": self._app_id,
                "client_secret": self._app_password,
                "scope": "https://api.botframework.com/.default",
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, data=data)

            if response.status_code == 200:
                return {
                    "success": True,
                    "message": f"Authenticated (app_id: {self._app_id[:8]}...)",
                }

            return {
                "success": False,
                "message": f"Auth failed ({response.status_code}): {response.text[:200]}",
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

"""SlackConnector — Slack bot integration via slack_sdk."""

import asyncio
import logging
from typing import Any, Dict, Optional

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

try:
    from slack_sdk.web.async_client import AsyncWebClient
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    SLACK_SDK_AVAILABLE = True
except ImportError:
    SLACK_SDK_AVAILABLE = False

class SlackConnector(BaseConnector):
    """
    Connector for Slack using the slack_sdk library with Socket Mode.

    Socket Mode maintains a persistent WebSocket connection and does not
    require a publicly accessible HTTP endpoint.

    Config keys:
        bot_token (str): Slack bot token (xoxb-...).
        app_token (str): Slack app-level token (xapp-...) for Socket Mode.
        allowed_channel_ids (list[str], optional): If set, only messages
            from these channel IDs will be processed.
        command_prefix (str, optional): If set, only messages starting
            with this prefix will be treated as commands.
    """

    MAX_MESSAGE_LENGTH = 4000
    CAPABILITIES = ConnectorCapabilities(
        reactions=True, threads=True, buttons=True,
        rich_text=True, file_upload=True,
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
            id="slack_send",
            name="Send a message via Slack",
            description="Send a message via Slack",
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
        self._web_client: Optional[Any] = None
        self._socket_client: Optional[Any] = None
        self._bot_user_id: Optional[str] = None
        self._permission_sessions: Dict[str, str] = {}  # request_id -> session_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    _SDK_AVAILABLE = SLACK_SDK_AVAILABLE
    _SDK_NAME = "slack_sdk"

    async def _do_connect(self) -> None:
        """Start the Slack Socket Mode client."""
        bot_token = self.config["bot_token"]
        app_token = self.config["app_token"]

        self._web_client = AsyncWebClient(token=bot_token)

        # Get bot identity
        auth_response = await self._web_client.auth_test()
        self._bot_user_id = auth_response.get("user_id")

        self._socket_client = SocketModeClient(
            app_token=app_token,
            web_client=self._web_client,
        )
        self._socket_client.socket_mode_request_listeners.append(
            self._handle_socket_event
        )

        self._task = asyncio.create_task(self._run_socket())

    async def _run_socket(self) -> None:
        """Background task that runs the Socket Mode client."""
        try:
            await self._socket_client.connect()
            # Keep the connection alive until cancelled
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("SlackConnector %s socket cancelled", self.connector_id)
        except Exception as exc:
            self._status = ConnectorStatus.ERROR
            self._status_message = str(exc)
            logger.exception("SlackConnector %s socket error", self.connector_id)
            try:
                from app.db import database
                database.update_connector_status(self.connector_id, "error", error=str(exc))
            except Exception:
                logger.debug("Failed to update DB status after socket error", exc_info=True)

    async def disconnect(self) -> None:
        """Close the Socket Mode client."""
        try:
            if self._socket_client is not None:
                await self._socket_client.close()
            if self._task is not None and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        except Exception as exc:
            logger.warning("Error during SlackConnector disconnect: %s", exc)
        finally:
            self._status = ConnectorStatus.DISCONNECTED
            self._status_message = None
            logger.info("SlackConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Incoming handlers
    # ------------------------------------------------------------------

    async def _handle_socket_event(
        self, client: Any, req: Any
    ) -> None:
        """Process a Socket Mode event."""
        try:
            # Acknowledge the event
            response = SocketModeResponse(envelope_id=req.envelope_id)
            await client.send_socket_mode_response(response)

            if req.type == "events_api":
                event = req.payload.get("event", {})
                event_type = event.get("type", "")

                if event_type == "message":
                    await self._handle_message_event(event)
                elif event_type == "app_mention":
                    await self._handle_message_event(event)

            elif req.type == "interactive":
                await self._handle_interactive(req.payload)

        except Exception as exc:
            logger.exception("Error handling Slack socket event: %s", exc)

    async def _handle_message_event(self, event: Dict[str, Any]) -> None:
        """Process an incoming Slack message event."""
        try:
            # Skip bot messages (including self)
            if event.get("bot_id") or event.get("subtype") == "bot_message":
                return

            # Skip message edits, deletes, etc.
            subtype = event.get("subtype")
            if subtype and subtype not in ("file_share",):
                return

            channel_id = event.get("channel", "")
            user_id = event.get("user", "")
            text = event.get("text", "")

            # Filter by allowed channels
            allowed = self.config.get("allowed_channel_ids")
            if allowed and channel_id not in [str(c) for c in allowed]:
                return

            # Filter by command prefix
            prefix = self.config.get("command_prefix")
            if prefix and not text.startswith(prefix):
                return

            # Resolve user display name
            user_name = user_id
            try:
                if self._web_client:
                    user_info = await self._web_client.users_info(user=user_id)
                    profile = user_info.get("user", {}).get("profile", {})
                    user_name = (
                        profile.get("display_name")
                        or profile.get("real_name")
                        or user_id
                    )
            except Exception:
                logger.debug("Failed to resolve Slack user name for %s", user_id, exc_info=True)

            thread_ts = event.get("thread_ts")
            msg = IncomingMessage(
                connector_id=self.connector_id,
                platform_chat_id=channel_id,
                platform_user_id=user_id,
                platform_user_name=user_name,
                text=text,
                platform_message_id=event.get("ts", ""),
                reply_to_message_id=thread_ts,
                metadata={
                    "thread_ts": thread_ts,
                    "channel_type": event.get("channel_type"),
                },
            )
            await self._on_incoming_message(msg)

        except Exception as exc:
            logger.exception("Error handling Slack message: %s", exc)

    async def _handle_interactive(self, payload: Dict[str, Any]) -> None:
        """Handle interactive button clicks (permission responses)."""
        try:
            actions = payload.get("actions", [])
            if not actions:
                return

            action = actions[0]
            action_id = action.get("action_id", "")

            if not action_id.startswith("perm:"):
                return

            parts = action_id.split(":")
            if len(parts) != 3:
                logger.warning("Invalid Slack action_id format: %s", action_id)
                return

            _, request_id, decision = parts
            approved = decision == "approve"

            session_id = self._permission_sessions.get(request_id, "")
            await self._on_permission_response(request_id, session_id, approved)

            # Update the original message to show the decision
            status_text = "Approved" if approved else "Denied"
            channel = payload.get("channel", {}).get("id", "")
            message_ts = payload.get("message", {}).get("ts", "")

            if self._web_client and channel and message_ts:
                original_text = payload.get("message", {}).get("text", "")
                await self._web_client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=f"{original_text}\n\n_Permission {status_text}_",
                    blocks=[],  # Remove buttons
                )

        except Exception as exc:
            logger.exception("Error handling Slack interactive: %s", exc)

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a text message to a Slack channel."""
        if self._web_client is None:
            logger.error("Cannot send message: web client is not initialised")
            return False

        try:
            for text in chunk_text(message.text, self.MAX_MESSAGE_LENGTH):
                await self._web_client.chat_postMessage(
                    channel=message.platform_chat_id,
                    text=text,
                    thread_ts=message.reply_to_message_id,
                )
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Slack message to %s: %s",
                message.platform_chat_id, exc,
            )
            return False

    async def send_ack_reaction(
        self, platform_chat_id: str, platform_message_id: str
    ) -> None:
        """Add an eyes reaction to acknowledge receipt."""
        if self._web_client and SLACK_SDK_AVAILABLE:
            try:
                await self._web_client.reactions_add(
                    channel=platform_chat_id,
                    name="eyes",
                    timestamp=platform_message_id,
                )
            except Exception:
                logger.debug("Slack ack reaction failed", exc_info=True)

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send a Block Kit message with Approve / Deny buttons."""
        if self._web_client is None:
            logger.error("Cannot send permission prompt: web client is not initialised")
            return False

        try:
            self._permission_sessions[prompt.request_id] = prompt.session_id

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Permission Request*\n\n"
                            f"*Tool:* `{prompt.tool_name}`\n"
                            f"{prompt.description}"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "style": "primary",
                            "action_id": f"perm:{prompt.request_id}:approve",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Deny"},
                            "style": "danger",
                            "action_id": f"perm:{prompt.request_id}:deny",
                        },
                    ],
                },
            ]

            await self._web_client.chat_postMessage(
                channel=platform_chat_id,
                text=f"Permission Request: {prompt.tool_name}",
                blocks=blocks,
            )
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Slack permission prompt to %s: %s",
                platform_chat_id, exc,
            )
            return False

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a notification message to a Slack channel."""
        if self._web_client is None:
            logger.error("Cannot send notification: web client is not initialised")
            return False

        try:
            emoji_map = {
                "session.started": ":arrow_forward:",
                "session.complete": ":white_check_mark:",
                "session.error": ":x:",
                "permission.requested": ":lock:",
            }
            emoji = emoji_map.get(event_type, ":mega:")

            if event_type == "session.complete":
                duration = data.get("duration_seconds", 0)
                cost = data.get("total_cost", 0)
                text = (
                    f"{emoji} *Session Complete*\n\n"
                    f"Duration: {duration:.1f}s\n"
                    f"Cost: ${cost:.4f}"
                )
            elif event_type == "session.error":
                error_msg = data.get("error_message", "Unknown error")
                text = f"{emoji} *Session Error*\n\n{error_msg}"
            else:
                text = f"{emoji} *{event_type}*\n\n{data}"

            await self._web_client.chat_postMessage(
                channel=platform_chat_id,
                text=text,
            )
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Slack notification to %s: %s",
                platform_chat_id, exc,
            )
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
        if tool_id == "slack_send":
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
        """Validate the bot token by calling auth.test."""
        if not SLACK_SDK_AVAILABLE:
            return {"success": False, "message": "slack_sdk is not installed"}

        try:
            client = AsyncWebClient(token=self.config["bot_token"])
            result = await client.auth_test()
            bot_name = result.get("user", "Unknown")
            team = result.get("team", "Unknown")
            return {
                "success": True,
                "message": f"Authenticated as {bot_name} in {team}",
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

"""WhatsAppConnector — WhatsApp integration via Meta Cloud API."""

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

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
    TriggerDef,
    chunk_text,
)
from app.core.connectors import whatsapp_actions

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"


class WhatsAppConnector(BaseConnector):
    """
    Connector for WhatsApp using the Meta Cloud API.

    Config keys:
        access_token (str): Meta access token with whatsapp_business_messaging
            permission.
        phone_number_id (str): WhatsApp Business phone number ID.
        allowed_phone_numbers (list[str], optional): If set, only messages
            from these phone numbers will be processed.
    """

    MAX_MESSAGE_LENGTH = 4096
    CAPABILITIES = ConnectorCapabilities(
        read_receipts=True, buttons=True,
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

    # Full declarative action catalog (see whatsapp_actions.py). Exposed to
    # the AI through the generic whatsapp_actions/whatsapp_invoke gateway
    # tool pair.
    ACTIONS = whatsapp_actions.WHATSAPP_ACTIONS

    # No per-tool MCP registration: the action gateway replaces the legacy
    # globally-exposed `whatsapp_send` tool.
    TOOLS: List[Any] = []

    SUPPORTS_ACTION_GATEWAY = True

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
        self._pending_action_approvals: Dict[str, asyncio.Future] = {}  # request_id -> Future[bool]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        """Return authorization and content-type headers."""
        return {
            "Authorization": f"Bearer {self.config['access_token']}",
            "Content-Type": "application/json",
        }

    def _messages_url(self) -> str:
        """Return the messages endpoint URL."""
        phone_number_id = self.config["phone_number_id"]
        return f"{GRAPH_API_BASE}/{phone_number_id}/messages"

    async def _api_post(self, payload: Dict[str, Any]) -> httpx.Response:
        """POST to the messages endpoint — used by whatsapp_actions.py
        handlers (mockable in tests)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(self._messages_url(), headers=self._headers(), json=payload)

    async def _api_get_phone_number(self) -> httpx.Response:
        """GET the phone number resource — used by whatsapp_actions.py
        handlers (mockable in tests)."""
        phone_number_id = self.config["phone_number_id"]
        url = f"{GRAPH_API_BASE}/{phone_number_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.get(url, headers=self._headers())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _do_connect(self) -> None:
        """Validate the access token by fetching the phone number details."""
        phone_number_id = self.config["phone_number_id"]
        url = f"{GRAPH_API_BASE}/{phone_number_id}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=self._headers())

        if response.status_code != 200:
            raise RuntimeError(
                f"API returned {response.status_code}: {response.text[:200]}"
            )

    async def disconnect(self) -> None:
        """Mark the connector as disconnected (no persistent connection)."""
        self._status = ConnectorStatus.DISCONNECTED
        self._status_message = None
        logger.info("WhatsAppConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a text message via the WhatsApp Cloud API."""
        try:
            for text in chunk_text(message.text, self.MAX_MESSAGE_LENGTH):
                payload = {
                    "messaging_product": "whatsapp",
                    "to": message.platform_chat_id,
                    "type": "text",
                    "text": {"body": text},
                }

                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        self._messages_url(),
                        headers=self._headers(),
                        json=payload,
                    )

                if not (200 <= response.status_code < 300):
                    logger.error(
                        "WhatsApp send_message failed (%s): %s",
                        response.status_code, response.text[:200],
                    )
                    return False

            return True

        except Exception as exc:
            logger.exception(
                "Failed to send WhatsApp message to %s: %s",
                message.platform_chat_id, exc,
            )
            return False

    async def mark_as_read(
        self, platform_chat_id: str, platform_message_id: str
    ) -> None:
        """Mark a WhatsApp message as read."""
        try:
            payload = {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": platform_message_id,
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    self._messages_url(),
                    headers=self._headers(),
                    json=payload,
                )
        except Exception:
            logger.debug("WhatsApp mark_as_read failed", exc_info=True)

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send an interactive button message for permission approval."""
        try:
            # Store session mapping for later callback lookup
            self._permission_sessions[prompt.request_id] = prompt.session_id

            description = (
                f"Permission Request\n\n"
                f"Tool: {prompt.tool_name}\n"
                f"{prompt.description}"
            )

            payload = {
                "messaging_product": "whatsapp",
                "to": platform_chat_id,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": description},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {
                                    "id": f"perm:{prompt.request_id}:approve",
                                    "title": "Approve",
                                },
                            },
                            {
                                "type": "reply",
                                "reply": {
                                    "id": f"perm:{prompt.request_id}:deny",
                                    "title": "Deny",
                                },
                            },
                        ]
                    },
                },
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self._messages_url(),
                    headers=self._headers(),
                    json=payload,
                )

            if 200 <= response.status_code < 300:
                return True

            logger.error(
                "WhatsApp send_permission_prompt failed (%s): %s",
                response.status_code, response.text[:200],
            )
            return False

        except Exception as exc:
            logger.exception(
                "Failed to send WhatsApp permission prompt to %s: %s",
                platform_chat_id, exc,
            )
            return False

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a plain text notification message."""
        try:
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

            payload = {
                "messaging_product": "whatsapp",
                "to": platform_chat_id,
                "type": "text",
                "text": {"body": text},
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self._messages_url(),
                    headers=self._headers(),
                    json=payload,
                )

            if 200 <= response.status_code < 300:
                return True

            logger.error(
                "WhatsApp send_notification failed (%s): %s",
                response.status_code, response.text[:200],
            )
            return False

        except Exception as exc:
            logger.exception(
                "Failed to send WhatsApp notification to %s: %s",
                platform_chat_id, exc,
            )
            return False

    # ------------------------------------------------------------------
    # Webhook handling (inbound)
    # ------------------------------------------------------------------

    async def handle_webhook_payload(self, payload: Dict[str, Any]) -> None:
        """
        Parse an incoming Meta webhook payload and dispatch messages
        or permission responses.

        This method should be called from the route that receives
        Meta's webhook POST requests.
        """
        try:
            entries = payload.get("entry", [])
            for entry in entries:
                changes = entry.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    for msg_data in messages:
                        await self._process_webhook_message(msg_data, value)
        except Exception as exc:
            logger.exception("Error processing WhatsApp webhook payload: %s", exc)

    async def _process_webhook_message(
        self, msg_data: Dict[str, Any], value: Dict[str, Any]
    ) -> None:
        """Process a single message from the webhook payload."""
        sender = msg_data.get("from", "")

        # Filter by allowed phone numbers
        allowed = self.config.get("allowed_phone_numbers")
        if allowed and sender not in [str(p) for p in allowed]:
            logger.debug("Ignoring message from non-allowed phone %s", sender)
            return

        msg_type = msg_data.get("type", "")

        # Handle button replies (permission responses)
        if msg_type == "interactive":
            interactive = msg_data.get("interactive", {})
            button_reply = interactive.get("button_reply", {})
            reply_id = button_reply.get("id", "")

            if reply_id.startswith("actreq:"):
                parts = reply_id.split(":")
                if len(parts) == 3:
                    _, request_id, decision = parts
                    fut = self._pending_action_approvals.get(request_id)
                    if fut is not None and not fut.done():
                        fut.set_result(decision == "approve")
                    return

            if reply_id.startswith("perm:"):
                parts = reply_id.split(":")
                if len(parts) == 3:
                    _, request_id, decision = parts
                    approved = decision == "approve"
                    session_id = self._permission_sessions.get(request_id, "")
                    await self._on_permission_response(request_id, session_id, approved)
                    return

        # Handle regular text messages
        if msg_type == "text":
            text_body = msg_data.get("text", {}).get("body", "")
            contact = self._find_contact(value, sender)

            msg = IncomingMessage(
                connector_id=self.connector_id,
                platform_chat_id=sender,
                platform_user_id=sender,
                platform_user_name=contact,
                text=text_body,
                platform_message_id=msg_data.get("id"),
            )
            await self._on_incoming_message(msg)

    def _find_contact(self, value: Dict[str, Any], phone: str) -> str:
        """Look up a display name from the contacts list in the webhook payload."""
        contacts = value.get("contacts", [])
        for contact in contacts:
            if contact.get("wa_id") == phone:
                profile = contact.get("profile", {})
                return profile.get("name", phone)
        return phone

    # ------------------------------------------------------------------
    # Actions & Tools
    # ------------------------------------------------------------------

    async def execute_action(
        self,
        action_id: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a catalog action (see whatsapp_actions.py)."""
        return await whatsapp_actions.execute(self, action_id, params, context)

    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Legacy tool entry point — kept for back-compat with old callers."""
        if tool_id == "whatsapp_send":
            return await self.execute_action("send_message", params)
        return {"success": False, "error": f"Unknown tool: {tool_id}"}

    # ------------------------------------------------------------------
    # Action gateway hooks
    # ------------------------------------------------------------------

    def get_context_instructions(self, platform_chat_id: str) -> Optional[str]:
        return (
            "WHATSAPP TOOLS: You are a WhatsApp Business participant. Call "
            "whatsapp_actions to discover everything you can do here "
            "(messages, reactions, templates, media, account info) along "
            "with each action's input schema, then call whatsapp_invoke "
            "with {action, params} to act. Message actions work on this "
            "chat and the connector's allowed phone numbers only. The "
            "WhatsApp Business API does not expose bulk-delete or "
            "membership-management actions to bots, so this catalog has "
            "no destructive actions."
        )

    async def request_action_approval(
        self,
        platform_chat_id: str,
        action: ActionDef,
        params: Dict[str, Any],
        timeout: float = 120.0,
    ) -> Tuple[bool, str]:
        """Post an interactive-button Approve/Deny prompt for a destructive action."""
        if not platform_chat_id:
            return False, "No chat available for the approval prompt."

        request_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_action_approvals[request_id] = fut

        try:
            params_preview = json.dumps(params, default=str)[:900]
            description = (
                f"Destructive Action Approval\n\n"
                f"The AI wants to run {action.name} ({action.id}).\n"
                f"{params_preview}"
            )

            payload = {
                "messaging_product": "whatsapp",
                "to": platform_chat_id,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": description},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {
                                    "id": f"actreq:{request_id}:approve",
                                    "title": "Approve",
                                },
                            },
                            {
                                "type": "reply",
                                "reply": {
                                    "id": f"actreq:{request_id}:deny",
                                    "title": "Deny",
                                },
                            },
                        ]
                    },
                },
            }

            response = await self._api_post(payload)
            if not (200 <= response.status_code < 300):
                return False, f"Failed to send approval prompt ({response.status_code})."

            try:
                approved = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                return False, f"No approval response within {timeout:.0f}s — action cancelled."
            return (True, "Approved by user.") if approved else (False, "Denied by user.")
        except Exception as exc:
            logger.exception("Failed to run WhatsApp action approval flow")
            return False, f"Approval flow failed: {exc}"
        finally:
            self._pending_action_approvals.pop(request_id, None)

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        """Validate the access token by calling the phone number endpoint."""
        try:
            phone_number_id = self.config["phone_number_id"]
            url = f"{GRAPH_API_BASE}/{phone_number_id}"

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=self._headers())

            if response.status_code == 200:
                data = response.json()
                display_name = data.get("verified_name", "Unknown")
                return {
                    "success": True,
                    "message": f"Connected as {display_name}",
                }

            return {
                "success": False,
                "message": f"API returned {response.status_code}: {response.text[:200]}",
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

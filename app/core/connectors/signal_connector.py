"""SignalConnector — Signal messenger integration via signal-cli REST API."""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.connectors.base import (
    BaseConnector,
    ConnectorCapabilities,
    ConnectorStatus,
    IncomingMessage,
    OutgoingMessage,
    PermissionPrompt,
    IncomingMessageCallback,
    PermissionResponseCallback,
    chunk_text,
)

logger = logging.getLogger(__name__)

# Default signal-cli REST API endpoint
DEFAULT_API_URL = "http://127.0.0.1:8080"
SSE_RECONNECT_DELAY = 2.0  # seconds
SSE_MAX_RECONNECT_DELAY = 10.0


class SignalConnector(BaseConnector):
    """
    Connector for Signal using the signal-cli REST API.

    Requires a running signal-cli daemon (or signal-cli-rest-api container)
    that exposes a JSON-RPC / REST endpoint and Server-Sent Events for
    incoming messages.

    Config keys:
        account (str): Signal phone number in E.164 format (e.g. "+1234567890").
        api_url (str, optional): Base URL for the signal-cli REST API.
            Defaults to "http://127.0.0.1:8080".
        allowed_numbers (list[str], optional): If set, only messages
            from these phone numbers will be processed.
    """

    MAX_MESSAGE_LENGTH = 4000
    CAPABILITIES = ConnectorCapabilities(
        read_receipts=True, file_upload=True,
    )

    def __init__(
        self,
        connector_id: str,
        connector_type: str,
        config: Dict[str, Any],
        on_incoming_message: IncomingMessageCallback,
        on_permission_response: PermissionResponseCallback,
    ):
        super().__init__(
            connector_id, connector_type, config,
            on_incoming_message, on_permission_response,
        )
        self._permission_sessions: Dict[str, str] = {}  # request_id -> session_id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _api_url(self) -> str:
        return self.config.get("api_url", DEFAULT_API_URL).rstrip("/")

    @property
    def _account(self) -> str:
        return self.config["account"]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Validate the signal-cli API connection and start the SSE listener."""
        try:
            self._status = ConnectorStatus.CONNECTING

            # Health check
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self._api_url}/api/v1/about")

            if response.status_code != 200:
                self._status = ConnectorStatus.ERROR
                self._status_message = f"API returned {response.status_code}"
                return False

            # Start SSE listener for incoming messages
            self._task = asyncio.create_task(self._sse_listener())
            self._status = ConnectorStatus.CONNECTED
            self._status_message = None
            logger.info("SignalConnector %s connected", self.connector_id)
            return True

        except Exception as exc:
            self._status = ConnectorStatus.ERROR
            self._status_message = str(exc)
            logger.exception("SignalConnector %s failed to connect", self.connector_id)
            return False

    async def _sse_listener(self) -> None:
        """
        Background task that listens for incoming Signal messages via
        Server-Sent Events from the signal-cli REST API.

        Reconnects with exponential backoff on connection failures.
        """
        delay = SSE_RECONNECT_DELAY
        url = f"{self._api_url}/api/v1/receive/{self._account}"

        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", url) as response:
                        delay = SSE_RECONNECT_DELAY  # reset on success
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                if data_str:
                                    try:
                                        event_data = json.loads(data_str)
                                        await self._handle_sse_event(event_data)
                                    except json.JSONDecodeError:
                                        logger.warning(
                                            "Invalid JSON in Signal SSE: %s",
                                            data_str[:100],
                                        )

            except asyncio.CancelledError:
                logger.info("SignalConnector %s SSE listener cancelled", self.connector_id)
                return

            except Exception as exc:
                logger.warning(
                    "SignalConnector %s SSE connection lost: %s (reconnecting in %.1fs)",
                    self.connector_id, exc, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, SSE_MAX_RECONNECT_DELAY)

    async def _handle_sse_event(self, event_data: Dict[str, Any]) -> None:
        """Process a single event from the SSE stream."""
        try:
            envelope = event_data.get("envelope", event_data)

            # Extract sender info
            sender_number = envelope.get("sourceNumber") or envelope.get("source", "")
            sender_name = envelope.get("sourceName") or sender_number

            # Process data messages (regular messages)
            data_message = envelope.get("dataMessage")
            if data_message:
                await self._handle_data_message(
                    sender_number, sender_name, data_message, envelope
                )

        except Exception as exc:
            logger.exception("Error handling Signal SSE event: %s", exc)

    async def _handle_data_message(
        self,
        sender_number: str,
        sender_name: str,
        data_message: Dict[str, Any],
        envelope: Dict[str, Any],
    ) -> None:
        """Process an incoming Signal data message."""
        # Filter by allowed numbers
        allowed = self.config.get("allowed_numbers")
        if allowed and sender_number not in [str(n) for n in allowed]:
            logger.debug("Ignoring Signal message from non-allowed number %s", sender_number)
            return

        text = data_message.get("message", "")

        # Check for permission response patterns in text
        if text and self._check_permission_response(text):
            return

        # Determine chat context
        group_info = data_message.get("groupInfo")
        if group_info:
            chat_id = group_info.get("groupId", sender_number)
            group_name = group_info.get("groupName", "")
        else:
            chat_id = sender_number
            group_name = None

        timestamp = envelope.get("timestamp") or data_message.get("timestamp")

        msg = IncomingMessage(
            connector_id=self.connector_id,
            platform_chat_id=chat_id,
            platform_user_id=sender_number,
            platform_user_name=sender_name,
            text=text or "",
            platform_message_id=str(timestamp) if timestamp else None,
            metadata={
                "group_id": group_info.get("groupId") if group_info else None,
                "group_name": group_name,
                "timestamp": timestamp,
            },
        )
        await self._on_incoming_message(msg)

    def _check_permission_response(self, text: str) -> bool:
        """Check if a text message is a reply to a permission prompt."""
        text_lower = text.strip().lower()
        # Support simple approve/deny text responses
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
    # Outgoing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a text message via the signal-cli REST API."""
        try:
            for text in chunk_text(message.text, self.MAX_MESSAGE_LENGTH):
                payload: Dict[str, Any] = {
                    "message": text,
                    "number": self._account,
                    "recipients": [message.platform_chat_id],
                }

                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        f"{self._api_url}/api/v2/send",
                        json=payload,
                    )

                if response.status_code not in (200, 201):
                    logger.error(
                        "Signal send_message failed (%s): %s",
                        response.status_code, response.text[:200],
                    )
                    return False

            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Signal message to %s: %s",
                message.platform_chat_id, exc,
            )
            return False

    async def mark_as_read(
        self, platform_chat_id: str, platform_message_id: str
    ) -> None:
        """Send a read receipt via signal-cli REST API."""
        try:
            payload = {
                "recipient": platform_chat_id,
                "timestamps": [int(platform_message_id)],
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self._api_url}/api/v1/receipt/{self._account}",
                    json=payload,
                )
        except Exception:
            logger.debug("Signal mark_as_read failed", exc_info=True)

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send a text-based permission prompt (Signal has no interactive buttons)."""
        self._permission_sessions[prompt.request_id] = prompt.session_id

        text = (
            f"Permission Request\n\n"
            f"Tool: {prompt.tool_name}\n"
            f"{prompt.description}\n\n"
            f"Reply 'approve' or 'deny'"
        )

        try:
            payload = {
                "message": text,
                "number": self._account,
                "recipients": [platform_chat_id],
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._api_url}/api/v2/send",
                    json=payload,
                )

            if response.status_code in (200, 201):
                return True

            logger.error(
                "Signal send_permission_prompt failed (%s): %s",
                response.status_code, response.text[:200],
            )
            return False

        except Exception as exc:
            logger.exception(
                "Failed to send Signal permission prompt to %s: %s",
                platform_chat_id, exc,
            )
            return False

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

        try:
            payload = {
                "message": text,
                "number": self._account,
                "recipients": [platform_chat_id],
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._api_url}/api/v2/send",
                    json=payload,
                )

            if response.status_code in (200, 201):
                return True

            logger.error(
                "Signal send_notification failed (%s): %s",
                response.status_code, response.text[:200],
            )
            return False

        except Exception as exc:
            logger.exception(
                "Failed to send Signal notification to %s: %s",
                platform_chat_id, exc,
            )
            return False

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        """Validate the signal-cli REST API connection."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self._api_url}/api/v1/about")

            if response.status_code == 200:
                data = response.json()
                versions = data.get("versions", [])
                version_str = ", ".join(versions) if versions else "unknown"
                return {
                    "success": True,
                    "message": f"Connected to signal-cli (versions: {version_str})",
                }

            return {
                "success": False,
                "message": f"API returned {response.status_code}",
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

"""
Base connector class defining the interface all connectors must implement.

All platform-specific connectors (Telegram, Discord, WhatsApp, etc.)
inherit from BaseConnector and implement the abstract methods.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConnectorStatus(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    ERROR = "error"


@dataclass
class IncomingMessage:
    """Normalized incoming message from any platform."""
    connector_id: str
    platform_chat_id: str
    platform_user_id: str
    platform_user_name: str
    text: str
    platform_message_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OutgoingMessage:
    """Normalized outgoing message to any platform."""
    platform_chat_id: str
    text: str
    reply_to_message_id: Optional[str] = None
    buttons: Optional[List[Dict[str, str]]] = None  # [{label, callback_data}]
    parse_mode: Optional[str] = None  # "markdown", "html"


@dataclass
class PermissionPrompt:
    """A permission request forwarded to a messaging platform."""
    request_id: str
    session_id: str
    tool_name: str
    tool_input: Dict[str, Any]
    description: str


# Type alias for callbacks
IncomingMessageCallback = Callable[[IncomingMessage], Coroutine[Any, Any, None]]
PermissionResponseCallback = Callable[[str, str, bool], Coroutine[Any, Any, None]]


def chunk_text(text: str, max_length: int) -> List[str]:
    """Split text into chunks, preferring newline boundaries.

    Returns a list with at least one element.  When *text* is shorter
    than *max_length* the list contains the original string unchanged.
    """
    if len(text) <= max_length:
        return [text]

    chunks: List[str] = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        # Prefer splitting at the last newline before the limit
        split_at = text.rfind("\n", 0, max_length)
        if split_at <= 0:
            # No suitable newline — hard-split at max_length
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


@dataclass
class ConnectorCapabilities:
    """Declares what features a messaging platform supports."""

    typing_indicator: bool = False
    reactions: bool = False
    read_receipts: bool = False
    threads: bool = False
    buttons: bool = False
    rich_text: bool = False
    file_upload: bool = False
    is_messaging: bool = True


class BaseConnector(ABC):
    """
    Abstract base class for all connectors.

    Subclasses implement platform-specific logic for connecting,
    sending messages, and handling incoming messages.
    """

    MAX_MESSAGE_LENGTH: Optional[int] = None
    CAPABILITIES = ConnectorCapabilities()

    def __init__(
        self,
        connector_id: str,
        connector_type: str,
        config: Dict[str, Any],
        on_incoming_message: IncomingMessageCallback,
        on_permission_response: PermissionResponseCallback,
    ):
        self.connector_id = connector_id
        self.connector_type = connector_type
        self.config = config
        self._on_incoming_message = on_incoming_message
        self._on_permission_response = on_permission_response
        self._status = ConnectorStatus.DISCONNECTED
        self._status_message: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def status(self) -> ConnectorStatus:
        return self._status

    @property
    def status_message(self) -> Optional[str]:
        return self._status_message

    @abstractmethod
    async def connect(self) -> bool:
        """Start the connector. Returns True on success."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully stop the connector."""
        ...

    @abstractmethod
    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a message to a platform chat. Returns True on success."""
        ...

    @abstractmethod
    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send an interactive permission request with approve/deny buttons."""
        ...

    @abstractmethod
    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a notification about a system event."""
        ...

    async def send_typing_indicator(self, platform_chat_id: str) -> None:
        """Send a typing indicator. Override in subclasses that support it."""

    async def send_ack_reaction(
        self, platform_chat_id: str, platform_message_id: str
    ) -> None:
        """React to a message to acknowledge receipt. Override in subclasses."""

    async def mark_as_read(
        self, platform_chat_id: str, platform_message_id: str
    ) -> None:
        """Mark a message as read. Override in subclasses that support it."""

    async def test_connection(self) -> Dict[str, Any]:
        """Test the connector credentials/connection."""
        try:
            result = await self.connect()
            if result:
                await self.disconnect()
                return {"success": True, "message": "Connection successful"}
            return {"success": False, "message": "Connection failed"}
        except Exception as e:
            return {"success": False, "message": str(e)}

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
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

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


# ---------------------------------------------------------------------------
# Connector variant definitions — Triggers, Actions, Tools
# ---------------------------------------------------------------------------

@dataclass
class TriggerDef:
    """A trigger event this connector can emit.

    Triggers fire when something happens on the platform (e.g. a track
    changes on Spotify, a message arrives on Telegram).  They are used
    as automation triggers.
    """
    id: str                                                        # e.g. "track_changed"
    name: str                                                      # e.g. "Track Changed"
    description: str                                               # Human-readable description
    payload_schema: Dict[str, Any] = field(default_factory=dict)   # JSON Schema of trigger payload


@dataclass
class ActionDef:
    """An action this connector can perform.

    Actions are concrete operations invoked by automations, the API, or —
    for connectors that opt into the action gateway — the AI model via the
    generic ``{type}_actions`` / ``{type}_invoke`` MCP tool pair.
    """
    id: str                                                        # e.g. "play_track"
    name: str                                                      # e.g. "Play Track"
    description: str                                               # Human-readable description
    input_schema: Dict[str, Any] = field(default_factory=dict)     # JSON Schema of action params
    category: str = "general"                                      # Grouping, e.g. "messages", "moderation"
    required_permission: Optional[str] = None                      # Platform permission flag, e.g. "ban_members"
    risk: str = "safe"                                             # "safe" (read) | "write" | "destructive"


def validate_action_params(action: ActionDef, params: Dict[str, Any]) -> Optional[str]:
    """Validate ``params`` against an action's JSON Schema subset.

    Supports the schema features the catalogs actually use: top-level object
    ``properties`` with ``type``/``enum``/``minimum``/``maximum`` and a
    ``required`` list. Returns an error message, or ``None`` when valid.
    Unknown params are tolerated (handlers ignore them).
    """
    schema = action.input_schema or {}
    if not isinstance(params, dict):
        return "params must be an object"

    for req in schema.get("required", []):
        if params.get(req) is None:
            return f"Missing required parameter: {req!r}"

    _TYPE_CHECKS = {
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }

    for key, prop in (schema.get("properties") or {}).items():
        value = params.get(key)
        if value is None:
            continue
        expected = prop.get("type")
        check = _TYPE_CHECKS.get(expected)
        if check and not check(value):
            return f"Parameter {key!r} must be of type {expected}"
        enum = prop.get("enum")
        if enum and value not in enum:
            return f"Parameter {key!r} must be one of: {', '.join(map(str, enum))}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if prop.get("minimum") is not None and value < prop["minimum"]:
                return f"Parameter {key!r} must be >= {prop['minimum']}"
            if prop.get("maximum") is not None and value > prop["maximum"]:
                return f"Parameter {key!r} must be <= {prop['maximum']}"
    return None


@dataclass
class ToolDef:
    """An AI-callable tool this connector provides.

    Tools are registered as MCP tools for AI models, letting the AI
    directly invoke connector capabilities during a conversation.
    """
    id: str                                                        # e.g. "spotify_playback"
    name: str                                                      # e.g. "Spotify Playback"
    description: str                                               # Description shown to AI
    input_schema: Dict[str, Any] = field(default_factory=dict)     # JSON Schema for AI tool input


# Type alias for trigger emission callback
TriggerCallback = Callable[[str, str, Dict[str, Any]], Coroutine[Any, Any, None]]
# Signature: (connector_id, trigger_id, payload) -> None


class BaseConnector(ABC):
    """
    Abstract base class for all connectors.

    Subclasses implement platform-specific logic for connecting,
    sending messages, and handling incoming messages.
    """

    MAX_MESSAGE_LENGTH: Optional[int] = None
    CAPABILITIES = ConnectorCapabilities()

    # Override in subclasses to declare available triggers, actions, and tools
    TRIGGERS: List[TriggerDef] = []
    ACTIONS: List[ActionDef] = []
    TOOLS: List[ToolDef] = []

    # Opt-in to the action gateway: sessions linked to this connector get a
    # generic `{type}_actions` (discover) + `{type}_invoke` (execute) MCP tool
    # pair driven by the ACTIONS catalog, instead of one MCP tool per action.
    SUPPORTS_ACTION_GATEWAY: bool = False

    def __init__(
        self,
        connector_id: str,
        connector_type: str,
        config: Dict[str, Any],
        on_incoming_message: IncomingMessageCallback,
        on_permission_response: PermissionResponseCallback,
        on_trigger: Optional[TriggerCallback] = None,
    ):
        self.connector_id = connector_id
        self.connector_type = connector_type
        self.config = config
        self._on_incoming_message = on_incoming_message
        self._on_permission_response = on_permission_response
        self._on_trigger = on_trigger
        self._status = ConnectorStatus.DISCONNECTED
        self._status_message: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def status(self) -> ConnectorStatus:
        return self._status

    @property
    def status_message(self) -> Optional[str]:
        return self._status_message

    # Subclasses that depend on optional SDKs should set these class
    # attributes so the template ``connect()`` can fail fast with a
    # clear error when the SDK is missing.
    _SDK_AVAILABLE: Optional[bool] = None   # Set to True/False when an import guard exists
    _SDK_NAME: Optional[str] = None         # Human-readable SDK package name

    async def connect(self) -> bool:
        """Start the connector. Returns True on success.

        This is a *template method* that handles the common boilerplate
        shared by every connector:

        1. Check that the required SDK is importable (when ``_SDK_AVAILABLE``
           is set).
        2. Transition status to ``CONNECTING``.
        3. Delegate to ``_do_connect()`` for platform-specific logic.
        4. On success transition to ``CONNECTED``; on failure transition
           to ``ERROR``.

        Subclasses should override ``_do_connect()`` instead of
        ``connect()`` in most cases.  If a subclass *does* override
        ``connect()`` directly (e.g. because its connection is
        asynchronous and the ``CONNECTED`` status is set later), the
        template is bypassed and that is fine.
        """
        # --- SDK availability guard ---
        if self._SDK_AVAILABLE is False:
            sdk = self._SDK_NAME or "required SDK"
            self._status = ConnectorStatus.ERROR
            self._status_message = f"{sdk} is not installed"
            logger.error(
                "Cannot start %s %s: %s is not installed",
                self.__class__.__name__, self.connector_id, sdk,
            )
            return False

        try:
            self._status = ConnectorStatus.CONNECTING
            await self._do_connect()
            self._status = ConnectorStatus.CONNECTED
            self._status_message = None
            logger.info("%s %s connected", self.__class__.__name__, self.connector_id)
            return True

        except Exception as exc:
            self._status = ConnectorStatus.ERROR
            self._status_message = str(exc)
            logger.exception(
                "%s %s failed to connect", self.__class__.__name__, self.connector_id,
            )
            return False

    async def _do_connect(self) -> None:
        """Platform-specific connection logic.

        Override this method in subclasses.  Raise any exception to
        signal failure — the base ``connect()`` will catch it, log it,
        and set the status to ``ERROR``.

        The method is called while status is ``CONNECTING``.  On a
        normal return the base class sets status to ``CONNECTED``.
        """
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

    # ------------------------------------------------------------------
    # Variant methods — override in subclasses that have actions/tools
    # ------------------------------------------------------------------

    async def execute_action(self, action_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a named action.  Override in subclasses that declare ACTIONS.

        Gateway connectors may accept an additional keyword-only ``context``
        dict in their override (session/bound-channel info supplied by the
        action gateway); plain callers keep using the two-argument form.
        """
        raise NotImplementedError(f"Action '{action_id}' not implemented by {self.connector_type}")

    # ------------------------------------------------------------------
    # Action gateway hooks — used when SUPPORTS_ACTION_GATEWAY is True
    # ------------------------------------------------------------------

    def get_enabled_action_categories(self) -> Optional[List[str]]:
        """Categories enabled for this install, or None meaning "all"."""
        raw = (self.config or {}).get("enabled_action_categories")
        if not raw:
            return None
        if isinstance(raw, str):
            raw = [raw]
        return [str(c).strip().lower() for c in raw if str(c).strip()]

    async def get_action_availability(
        self, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Tuple[bool, Optional[str]]]:
        """Map action_id -> (available, reason-if-not).

        The base implementation applies only per-install category gating.
        Subclasses layer platform checks on top (e.g. the Discord connector
        checks the bot's live guild permissions).
        """
        enabled = self.get_enabled_action_categories()
        out: Dict[str, Tuple[bool, Optional[str]]] = {}
        for action in self.ACTIONS:
            if enabled is not None and action.category.lower() not in enabled:
                out[action.id] = (
                    False,
                    f"Category '{action.category}' is disabled for this connector install.",
                )
            else:
                out[action.id] = (True, None)
        return out

    def get_context_instructions(self, platform_chat_id: str) -> Optional[str]:
        """Short system-prompt blurb describing how to use this connector's
        tools.  Appended to connector-session prompts by ConnectorManager."""
        return None

    async def request_action_approval(
        self,
        platform_chat_id: str,
        action: ActionDef,
        params: Dict[str, Any],
        timeout: float = 120.0,
    ) -> Tuple[bool, str]:
        """Ask a human in-channel to approve a destructive action.

        Returns (approved, reason). The default denies — connectors without
        an interactive approval surface cannot run destructive actions unless
        the install sets ``auto_approve_destructive``.
        """
        return False, "This connector does not support interactive approval."

    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a named tool call.  Override in subclasses that declare TOOLS."""
        raise NotImplementedError(f"Tool '{tool_id}' not implemented by {self.connector_type}")

    async def emit_trigger(self, trigger_id: str, payload: Dict[str, Any]) -> None:
        """Emit a trigger event.  Called by connector logic when an event occurs."""
        if self._on_trigger:
            try:
                await self._on_trigger(self.connector_id, trigger_id, payload)
            except Exception as e:
                logger.error(
                    "Failed to emit trigger '%s' from connector %s: %s",
                    trigger_id, self.connector_id, e,
                )

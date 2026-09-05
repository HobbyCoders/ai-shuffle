"""
Provider-agnostic streaming message vocabulary.

These dataclasses mirror the ``claude_agent_sdk.types`` message/block classes
1:1 — same class names, same attribute names, same subclass relationships
(Task*/Hook messages extend SystemMessage so subclass-before-base isinstance
chains keep working). The streaming loops in query_engine.py /
query_background.py dispatch on THESE types; adapters translate their native
stream into them (SDK messages via ``sdk_event_translation.to_provider_event``,
HarnessAdapter via its ``_translate``).

Why an exact mirror instead of a redesigned vocabulary: the seam refactor
must be behavior-identical with the flag off. Identical names/fields make the
loop diff a pure import + receive-call swap, so every dispatch branch and
attribute read is provably unchanged. No claude_agent_sdk import appears in
this module — it must stay importable on any backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from app.core.provider.types import ProviderEvent


# ─── Content blocks ──────────────────────────────────────────────────────────

@dataclass
class TextBlock:
    """Text content block."""
    text: str


@dataclass
class ThinkingBlock:
    """Thinking content block."""
    thinking: str
    signature: str = ""


@dataclass
class ToolUseBlock:
    """Tool use content block."""
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class ToolResultBlock:
    """Tool result content block."""
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]], None] = None
    is_error: Optional[bool] = None


@dataclass
class ServerToolUseBlock:
    """Server-side tool use block (e.g. advisor, web_search, web_fetch)."""
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class ServerToolResultBlock:
    """Result block returned for a server-side tool call."""
    tool_use_id: str
    content: Dict[str, Any]


ContentBlock = Union[
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
    ServerToolUseBlock,
    ServerToolResultBlock,
]


# ─── Messages ────────────────────────────────────────────────────────────────

@dataclass
class UserMessage(ProviderEvent):
    """User-lane message (tool results, injected user content)."""
    content: Union[str, List[Any]]
    uuid: Optional[str] = None
    parent_tool_use_id: Optional[str] = None
    tool_use_result: Optional[Dict[str, Any]] = None


@dataclass
class AssistantMessage(ProviderEvent):
    """Assistant message with content blocks."""
    content: List[Any]
    model: str
    parent_tool_use_id: Optional[str] = None
    error: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    message_id: Optional[str] = None
    stop_reason: Optional[str] = None
    session_id: Optional[str] = None
    uuid: Optional[str] = None


@dataclass
class SystemMessage(ProviderEvent):
    """System message with metadata."""
    subtype: str
    data: Dict[str, Any]


@dataclass
class HookEventMessage(SystemMessage):
    """Hook lifecycle event (include_hook_events). Subclass of SystemMessage."""
    hook_event_name: str = ""
    session_id: Optional[str] = None
    uuid: Optional[str] = None


@dataclass
class TaskStartedMessage(SystemMessage):
    """Subagent task started. Subclass of SystemMessage."""
    task_id: str = ""
    description: str = ""
    uuid: str = ""
    session_id: str = ""
    tool_use_id: Optional[str] = None
    task_type: Optional[str] = None


@dataclass
class TaskProgressMessage(SystemMessage):
    """Subagent task progress. Subclass of SystemMessage."""
    task_id: str = ""
    description: str = ""
    usage: Any = None
    uuid: str = ""
    session_id: str = ""
    tool_use_id: Optional[str] = None
    last_tool_name: Optional[str] = None


@dataclass
class TaskNotificationMessage(SystemMessage):
    """Subagent task terminal notification. Subclass of SystemMessage."""
    task_id: str = ""
    status: str = ""
    output_file: str = ""
    summary: str = ""
    uuid: str = ""
    session_id: str = ""
    tool_use_id: Optional[str] = None
    usage: Any = None


@dataclass
class TaskUpdatedMessage(SystemMessage):
    """Background task state patch. Subclass of SystemMessage."""
    task_id: str = ""
    patch: Dict[str, Any] = field(default_factory=dict)
    status: Optional[str] = None
    session_id: Optional[str] = None
    uuid: Optional[str] = None


@dataclass
class ResultMessage(ProviderEvent):
    """Turn-terminal result with cost and usage information."""
    subtype: str
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = False
    num_turns: int = 0
    session_id: str = ""
    stop_reason: Optional[str] = None
    total_cost_usd: Optional[float] = None
    usage: Optional[Dict[str, Any]] = None
    result: Optional[str] = None
    structured_output: Any = None
    model_usage: Optional[Dict[str, Any]] = None
    permission_denials: Optional[List[Any]] = None
    deferred_tool_use: Any = None
    errors: Optional[List[str]] = None
    api_error_status: Optional[int] = None
    uuid: Optional[str] = None
    terminal_reason: Optional[str] = None


@dataclass
class SteerDeliveredEvent(ProviderEvent):
    """Queued user text was handed to the running turn and the model will read it.

    Mirrors fluid's ``SteerDelivered``: ``steers`` holds one
    ``{"id": <correlation handle or None>, "text": <the user's words>}`` record
    per delivered message, in submission order, and arrives immediately after
    the frame that carried them. The id is what lets the host retire the
    durable queue row it steered from — a steer that never shows up here never
    reached the model and must still run as an ordinary turn.
    """
    steers: List[Dict[str, Any]] = field(default_factory=list)
    session_id: str = ""
    uuid: Optional[str] = None


@dataclass
class StreamEvent(ProviderEvent):
    """Partial-message stream event; ``event`` is the raw API stream event."""
    uuid: str
    session_id: str
    event: Dict[str, Any]
    parent_tool_use_id: Optional[str] = None


@dataclass
class RateLimitInfo:
    """Rate limit reading (mirrors the CLI's rate_limit payload)."""
    status: str
    resets_at: Optional[int] = None
    rate_limit_type: Optional[str] = None
    utilization: Optional[float] = None
    overage_status: Optional[str] = None
    overage_resets_at: Optional[int] = None
    overage_disabled_reason: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RateLimitEvent(ProviderEvent):
    """Emitted when the provider's rate limit status changes."""
    rate_limit_info: RateLimitInfo
    uuid: str = ""
    session_id: str = ""

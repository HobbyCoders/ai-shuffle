"""
Provider-agnostic event and configuration types.

These types define the boundary between our application logic and any
AI provider backend. They mirror what our streaming loops already yield
as dicts, but formalized as dataclasses for type safety and documentation.

The streaming loops in query_engine.py currently yield dicts like:
    {"type": "text", "content": "..."}
    {"type": "tool_use", "name": "...", "input": {...}}

These dataclasses are the typed equivalent. The migration path is:
1. Adapter yields ProviderEvent instances
2. Streaming loop converts them to the same dicts (trivial)
3. Eventually, the frontend contract can be typed end-to-end
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


# ─── Configuration ───────────────────────────────────────────────────────────

@dataclass
class SessionOptions:
    """Provider-agnostic session configuration.

    Built from profile + project + overrides, then passed to the adapter.
    The adapter is responsible for translating these to SDK-specific options.
    """
    model: str
    system_prompt: Union[str, dict, None] = None
    cwd: Optional[str] = None
    resume_session_id: Optional[str] = None

    # Tool configuration
    tools: Optional[list] = None
    # PERMISSION allowlist (SDK vocabulary): entries that may run without
    # prompting. NOT a tool-surface restriction — see ``tool_allowlist``.
    allowed_tools: Optional[list] = None
    disallowed_tools: Optional[list] = None
    # TOOL-SURFACE restriction (ruling CE12). A non-empty list is the session's
    # ENTIRE tool surface: on a native leg it becomes
    # ``NativeLoop(allowed_tools=...)``, so a filtered tool's schema is never
    # sent, ToolSearch will not load it, and calling it errors in-context. None
    # (the default) means unrestricted. Set today by Task dispatch for a
    # file/plugin agent whose definition carries a ``tools:`` list.
    tool_allowlist: Optional[list] = None

    # MCP servers: {name: {command, args, env, ...}}
    mcp_servers: Optional[Dict[str, Any]] = None

    # Plugins
    plugins: Optional[list] = None

    # Thinking / effort
    thinking: Optional[Any] = None
    effort: Optional[str] = None

    # Permission mode
    permission_mode: Optional[str] = None

    # Maximum model calls the engine may make inside one turn (None = engine
    # default). Native legs map this onto fluid's ``max_model_calls_per_turn``.
    max_turns: Optional[int] = None

    # Streaming options
    include_partial_messages: bool = False

    # Hooks
    hooks: Optional[Dict[str, list]] = None

    # Agents (subagents)
    agents: Optional[Dict[str, Any]] = None

    # Environment variables
    env: Optional[Dict[str, str]] = None

    # Extra CLI args
    extra_args: Optional[list] = None

    # Settings sources (for plugin auto-loading)
    setting_sources: Optional[list] = None

    # File checkpointing
    enable_file_checkpointing: Optional[bool] = None

    # Stderr callback
    stderr: Optional[Any] = None

    # Maximum buffer size
    max_buffer_size: Optional[int] = None

    # User identifier
    user: Optional[str] = None

    # Additional directories the session may access (delegated leg: --add-dir)
    add_dirs: Optional[list] = None

    # Custom session ID (SDK 0.1.52+) — allows specifying session ID at creation time
    session_id: Optional[str] = None

    # Token budget management (SDK 0.1.51+)
    task_budget: Optional[int] = None

    # Session store (SDK 0.1.65+) — PgSessionStore instance or None
    session_store: Optional[object] = None

    # Session store flush mode (SDK 0.1.65+) — "eager" | None
    session_store_flush: Optional[str] = None

    # Hook event forwarding (SDK 0.1.81+) — emit HookEventMessage to stream
    include_hook_events: bool = True

    # Strict MCP config validation (SDK 0.1.81+) — raise on unknown MCP fields
    strict_mcp_config: bool = False

    # Arbitrary extra options (for forward-compatibility with new SDK fields)
    extra: Dict[str, Any] = field(default_factory=dict)


# ─── Provider Events ─────────────────────────────────────────────────────────
#
# Each event type corresponds to a message the streaming loop yields to the
# WebSocket handler. The `event_type` field matches the dict `type` field
# that the frontend expects.

@dataclass
class ProviderEvent:
    """Base class for all provider events. Not instantiated directly."""
    pass


@dataclass
class SystemInitEvent(ProviderEvent):
    """SDK session initialized — carries the SDK-assigned session ID."""
    sdk_session_id: str


@dataclass
class TextEvent(ProviderEvent):
    """A chunk of assistant text (non-streaming mode)."""
    content: str
    model: Optional[str] = None


@dataclass
class ToolUseEvent(ProviderEvent):
    """The assistant is invoking a tool."""
    name: str
    input: Any
    tool_use_id: Optional[str] = None


@dataclass
class ToolResultEvent(ProviderEvent):
    """Result from a tool execution."""
    name: str
    output: Any
    tool_use_id: Optional[str] = None


@dataclass
class TurnUsageEvent(ProviderEvent):
    """Per-turn token usage from an AssistantMessage."""
    usage: Dict[str, Any]
    model: Optional[str] = None


@dataclass
class TaskNotificationEvent(ProviderEvent):
    """Subagent task notification."""
    task_id: str
    status: str
    summary: Optional[str] = None


@dataclass
class PermissionRequestEvent(ProviderEvent):
    """A tool invocation is blocked awaiting a permission decision.

    Emitted by adapters that surface permission gating as a stream event (the
    fluid HarnessAdapter). The caller responds via
    ``adapter.respond_to_permission(tool_use_id, allow, remember=...)``.

    The Claude Agent SDK path does NOT emit this — it handles permissions
    out-of-band through the ``can_use_tool`` callback — so this type is additive
    and only appears on the harness path.
    """
    tool_use_id: str
    name: str
    input: Any
    risk: str = "safe"
    request_id: Optional[str] = None


@dataclass
class TaskProgressEvent(ProviderEvent):
    """Subagent task progress update."""
    task_id: str
    description: Optional[str] = None


@dataclass
class TaskStartedEvent(ProviderEvent):
    """Subagent task started."""
    task_id: str
    description: Optional[str] = None


# ─── Streaming Events (partial message mode) ─────────────────────────────────

@dataclass
class StreamDeltaEvent(ProviderEvent):
    """Character-level streaming delta (text, thinking, or tool_input)."""
    delta_type: str  # "text", "thinking", "tool_input"
    content: str


@dataclass
class StreamBlockStartEvent(ProviderEvent):
    """A new content block started during streaming."""
    block_type: str  # "text", "thinking", "tool_use"
    block_index: int
    # For tool_use blocks:
    tool_name: Optional[str] = None
    tool_use_id: Optional[str] = None


@dataclass
class StreamBlockStopEvent(ProviderEvent):
    """A content block finished during streaming."""
    block_type: str
    block_index: int


# ─── Terminal Events ─────────────────────────────────────────────────────────

@dataclass
class RateLimitEvent(ProviderEvent):
    """Rate limit information from the provider."""
    status: str
    rate_limit_type: str
    resets_at: Optional[str] = None
    utilization: Optional[float] = None


@dataclass
class ResultEvent(ProviderEvent):
    """Query completed — carries final metadata."""
    duration_ms: Optional[int] = None
    num_turns: Optional[int] = None
    total_cost_usd: Optional[float] = None
    is_error: bool = False
    stop_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None


@dataclass
class InterruptedEvent(ProviderEvent):
    """Query was interrupted by the user."""
    message: str = "Query was interrupted"


@dataclass
class ErrorEvent(ProviderEvent):
    """An error occurred during the query."""
    message: str


# ─── MCP Server Comparison ──────────────────────────────────────────────────

def mcp_servers_equivalent(old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> bool:
    """Compare MCP server configs by logical identity, not object identity.

    SDK MCP servers (type="sdk") contain an ``instance`` field pointing to a
    fresh ``mcp.Server`` object every time they're created.  ``mcp.Server``
    has no ``__eq__``, so dict comparison always fails even when the server
    is logically identical (same name and tool set).

    This helper compares:
    - External servers (stdio/sse/http): full dict equality (all values are
      serializable primitives).
    - SDK servers: by (type, name) since the instance is functionally
      equivalent when those match.  Tool definitions come from the same code
      path each time, so name equality is sufficient.
    """
    old = old or {}
    new = new or {}

    if old.keys() != new.keys():
        return False

    for key in old:
        old_cfg = old[key]
        new_cfg = new[key]

        # SDK MCP servers: compare by type + name (skip instance)
        if isinstance(old_cfg, dict) and old_cfg.get("type") == "sdk":
            if not isinstance(new_cfg, dict) or new_cfg.get("type") != "sdk":
                return False
            if old_cfg.get("name") != new_cfg.get("name"):
                return False
            continue

        # External servers (stdio/sse/http): plain dict equality
        if old_cfg != new_cfg:
            return False

    return True

"""Translate SDK message objects into the app's provider-neutral events.

Used by the idle reader's SDK compatibility path. HarnessAdapter translates
fluid events separately; this module contains no provider lifecycle or spawning.
"""
from __future__ import annotations

from typing import Any

# ─── SDK message → ProviderEvent translation ─────────────────────────────────
#
# The single choke point where claude_agent_sdk message objects become the
# provider-agnostic mirrors in app.core.provider.events. Every consumer of an
# SDK message stream (adapter loops, the headless query() path, idle_reader)
# converts here, so no SDK message class crosses the provider boundary.
from claude_agent_sdk import (
    AssistantMessage as _SdkAssistantMessage,
)
from claude_agent_sdk import (
    ResultMessage as _SdkResultMessage,
)
from claude_agent_sdk import (
    SystemMessage as _SdkSystemMessage,
)
from claude_agent_sdk import (
    TextBlock as _SdkTextBlock,
)
from claude_agent_sdk import (
    ThinkingBlock as _SdkThinkingBlock,
)
from claude_agent_sdk import (
    ToolResultBlock as _SdkToolResultBlock,
)
from claude_agent_sdk import (
    ToolUseBlock as _SdkToolUseBlock,
)
from claude_agent_sdk import (
    UserMessage as _SdkUserMessage,
)
from claude_agent_sdk.types import StreamEvent as _SdkStreamEvent

# Optional per-SDK-version types, guarded like the loops' own imports so an
# SDK downgrade degrades to pass-through instead of ImportError.
try:  # pragma: no cover - present on the pinned SDK
    from claude_agent_sdk import (
        ServerToolResultBlock as _SdkServerToolResultBlock,
    )
    from claude_agent_sdk import (
        ServerToolUseBlock as _SdkServerToolUseBlock,
    )
except ImportError:  # pragma: no cover
    _SdkServerToolUseBlock = _SdkServerToolResultBlock = None  # type: ignore[assignment]
try:  # pragma: no cover
    from claude_agent_sdk import (
        TaskNotificationMessage as _SdkTaskNotificationMessage,
    )
    from claude_agent_sdk import (
        TaskProgressMessage as _SdkTaskProgressMessage,
    )
    from claude_agent_sdk import (
        TaskStartedMessage as _SdkTaskStartedMessage,
    )
except ImportError:  # pragma: no cover
    _SdkTaskStartedMessage = _SdkTaskProgressMessage = _SdkTaskNotificationMessage = None  # type: ignore[assignment]
try:  # pragma: no cover
    from claude_agent_sdk import TaskUpdatedMessage as _SdkTaskUpdatedMessage
except ImportError:  # pragma: no cover
    _SdkTaskUpdatedMessage = None  # type: ignore[assignment]
try:  # pragma: no cover
    from claude_agent_sdk import RateLimitEvent as _SdkRateLimitEvent
except ImportError:  # pragma: no cover
    _SdkRateLimitEvent = None  # type: ignore[assignment]
try:  # pragma: no cover
    from claude_agent_sdk import (
        HookEventMessage as _SdkHookEventMessage,  # type: ignore[attr-defined]
    )
except ImportError:  # pragma: no cover
    _SdkHookEventMessage = None  # type: ignore[assignment]

from app.core.provider import events as pe


def _convert_block(block: Any) -> Any:
    """SDK content block → mirror block. Unknown block types pass through
    unchanged — the loops' isinstance chains skip them either way."""
    if isinstance(block, _SdkTextBlock):
        return pe.TextBlock(text=block.text)
    if isinstance(block, _SdkThinkingBlock):
        return pe.ThinkingBlock(
            thinking=block.thinking, signature=getattr(block, "signature", "") or ""
        )
    if isinstance(block, _SdkToolUseBlock):
        return pe.ToolUseBlock(id=block.id, name=block.name, input=block.input)
    if isinstance(block, _SdkToolResultBlock):
        return pe.ToolResultBlock(
            tool_use_id=block.tool_use_id,
            content=block.content,
            is_error=block.is_error,
        )
    if _SdkServerToolUseBlock is not None and isinstance(block, _SdkServerToolUseBlock):
        return pe.ServerToolUseBlock(id=block.id, name=block.name, input=block.input)
    if _SdkServerToolResultBlock is not None and isinstance(block, _SdkServerToolResultBlock):
        return pe.ServerToolResultBlock(
            tool_use_id=block.tool_use_id, content=block.content
        )
    return block


def to_provider_event(message: Any) -> Any:
    """Translate one SDK stream message into its ProviderEvent mirror.

    Faithful field-for-field copy (dict/list values are passed by reference,
    not deep-copied — same objects the loops would have read off the SDK
    message). Subclass checks run before SystemMessage, mirroring the loops'
    dispatch order. Unknown message types pass through unchanged so the
    loops' else-branch logging still reports the real type name.
    """
    # SystemMessage subclasses first
    if _SdkTaskStartedMessage is not None and isinstance(message, _SdkTaskStartedMessage):
        return pe.TaskStartedMessage(
            subtype=message.subtype,
            data=message.data,
            task_id=message.task_id,
            description=message.description,
            uuid=message.uuid,
            session_id=message.session_id,
            tool_use_id=message.tool_use_id,
            task_type=getattr(message, "task_type", None),
        )
    if _SdkTaskProgressMessage is not None and isinstance(message, _SdkTaskProgressMessage):
        return pe.TaskProgressMessage(
            subtype=message.subtype,
            data=message.data,
            task_id=message.task_id,
            description=message.description,
            usage=getattr(message, "usage", None),
            uuid=message.uuid,
            session_id=message.session_id,
            tool_use_id=message.tool_use_id,
            last_tool_name=getattr(message, "last_tool_name", None),
        )
    if _SdkTaskNotificationMessage is not None and isinstance(message, _SdkTaskNotificationMessage):
        return pe.TaskNotificationMessage(
            subtype=message.subtype,
            data=message.data,
            task_id=message.task_id,
            status=message.status,
            output_file=message.output_file,
            summary=message.summary,
            uuid=message.uuid,
            session_id=message.session_id,
            tool_use_id=message.tool_use_id,
            usage=getattr(message, "usage", None),
        )
    if _SdkTaskUpdatedMessage is not None and isinstance(message, _SdkTaskUpdatedMessage):
        return pe.TaskUpdatedMessage(
            subtype=message.subtype,
            data=message.data,
            task_id=message.task_id,
            patch=message.patch,
            status=getattr(message, "status", None),
            session_id=getattr(message, "session_id", None),
            uuid=getattr(message, "uuid", None),
        )
    if _SdkHookEventMessage is not None and isinstance(message, _SdkHookEventMessage):
        return pe.HookEventMessage(
            subtype=message.subtype,
            data=message.data,
            hook_event_name=getattr(message, "hook_event_name", "") or "",
            session_id=getattr(message, "session_id", None),
            uuid=getattr(message, "uuid", None),
        )
    if isinstance(message, _SdkSystemMessage):
        return pe.SystemMessage(subtype=message.subtype, data=message.data)

    if isinstance(message, _SdkAssistantMessage):
        return pe.AssistantMessage(
            content=[_convert_block(b) for b in message.content],
            model=message.model,
            parent_tool_use_id=message.parent_tool_use_id,
            error=getattr(message, "error", None),
            usage=getattr(message, "usage", None),
            message_id=getattr(message, "message_id", None),
            stop_reason=getattr(message, "stop_reason", None),
            session_id=getattr(message, "session_id", None),
            uuid=getattr(message, "uuid", None),
        )
    if isinstance(message, _SdkUserMessage):
        content = message.content
        if isinstance(content, list):
            content = [_convert_block(b) for b in content]
        return pe.UserMessage(
            content=content,
            uuid=getattr(message, "uuid", None),
            parent_tool_use_id=message.parent_tool_use_id,
            tool_use_result=getattr(message, "tool_use_result", None),
        )
    if isinstance(message, _SdkResultMessage):
        return pe.ResultMessage(
            subtype=message.subtype,
            duration_ms=message.duration_ms,
            duration_api_ms=message.duration_api_ms,
            is_error=message.is_error,
            num_turns=message.num_turns,
            session_id=message.session_id,
            stop_reason=getattr(message, "stop_reason", None),
            total_cost_usd=message.total_cost_usd,
            usage=message.usage,
            result=message.result,
            structured_output=getattr(message, "structured_output", None),
            model_usage=getattr(message, "model_usage", None),
            permission_denials=getattr(message, "permission_denials", None),
            deferred_tool_use=getattr(message, "deferred_tool_use", None),
            errors=getattr(message, "errors", None),
            api_error_status=getattr(message, "api_error_status", None),
            uuid=getattr(message, "uuid", None),
            terminal_reason=getattr(message, "terminal_reason", None),
        )
    if isinstance(message, _SdkStreamEvent):
        return pe.StreamEvent(
            uuid=message.uuid,
            session_id=message.session_id,
            event=message.event,
            parent_tool_use_id=message.parent_tool_use_id,
        )
    if _SdkRateLimitEvent is not None and isinstance(message, _SdkRateLimitEvent):
        info = message.rate_limit_info
        return pe.RateLimitEvent(
            rate_limit_info=pe.RateLimitInfo(
                status=info.status,
                resets_at=info.resets_at,
                rate_limit_type=info.rate_limit_type,
                utilization=info.utilization,
                overage_status=getattr(info, "overage_status", None),
                overage_resets_at=getattr(info, "overage_resets_at", None),
                overage_disabled_reason=getattr(info, "overage_disabled_reason", None),
                raw=getattr(info, "raw", None) or {},
            ),
            uuid=getattr(message, "uuid", "") or "",
            session_id=getattr(message, "session_id", "") or "",
        )

    return message

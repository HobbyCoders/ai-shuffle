"""
Provider adapter abstraction for AI agent backends.

This package isolates SDK-specific code behind a clean interface so that:
1. SDK version changes only affect the concrete adapter, not business logic
2. The codebase can be tested without spawning SDK subprocesses
3. Future providers can be added by implementing the ProviderAdapter protocol
"""

from app.core.provider.protocol import ProviderAdapter
from app.core.provider.types import (
    SessionOptions,
    ProviderEvent,
    TextEvent,
    ToolUseEvent,
    ToolResultEvent,
    SystemInitEvent,
    TaskNotificationEvent,
    TaskProgressEvent,
    TaskStartedEvent,
    StreamDeltaEvent,
    StreamBlockStartEvent,
    StreamBlockStopEvent,
    TurnUsageEvent,
    RateLimitEvent,
    ResultEvent,
    InterruptedEvent,
    ErrorEvent,
)

__all__ = [
    "ProviderAdapter",
    "SessionOptions",
    "ProviderEvent",
    "TextEvent",
    "ToolUseEvent",
    "ToolResultEvent",
    "SystemInitEvent",
    "TaskNotificationEvent",
    "TaskProgressEvent",
    "TaskStartedEvent",
    "StreamDeltaEvent",
    "StreamBlockStartEvent",
    "StreamBlockStopEvent",
    "TurnUsageEvent",
    "RateLimitEvent",
    "ResultEvent",
    "InterruptedEvent",
    "ErrorEvent",
]

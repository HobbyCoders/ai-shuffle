"""
ProviderAdapter — abstract interface for AI agent backends.

Any provider (Claude, OpenAI, Gemini, etc.) implements this protocol.
The rest of the application talks exclusively to this interface.

Design principles:
- Methods map to user-visible actions (connect, query, interrupt, respond)
- Events are provider-agnostic dataclasses (ProviderEvent subclasses)
- Lifecycle management (health check, graceful shutdown) is part of the interface
- No SDK types leak through this boundary
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Callable, Awaitable, Dict, Optional

from app.core.provider.types import ProviderEvent, SessionOptions


class ProviderAdapter(ABC):
    """Abstract interface for an AI provider backend.

    Lifecycle:
        adapter = ClaudeAgentAdapter()
        await adapter.connect(options)
        async for event in adapter.stream_query(prompt):
            handle(event)
        await adapter.disconnect()
    """

    # ─── Connection Lifecycle ─────────────────────────────────────────

    @abstractmethod
    async def connect(self, options: SessionOptions) -> None:
        """Connect to the provider backend.

        For Claude, this spawns a subprocess and establishes communication.
        Must be called before any query methods.
        """

    @abstractmethod
    async def disconnect(
        self, grace_period: float = 5.0, *, session_id: Optional[str] = None
    ) -> None:
        """Disconnect from the provider backend.

        Should allow the backend time to flush any pending writes (e.g., JSONL)
        before forcefully terminating. The grace_period (seconds) controls how
        long to wait for a natural exit before sending SIGTERM.

        ``session_id`` (optional, app-level) lets the adapter flag an intentional
        teardown so expected shutdown noise from the backend can be classified
        as such rather than persisted as a durable error.
        """

    @abstractmethod
    def is_alive(self) -> bool:
        """Check if the backend process is still running.

        Returns False if the process has exited or is not accessible.
        Used by the session pool for health checks and eviction decisions.
        """

    @abstractmethod
    def is_connected(self) -> bool:
        """Whether connect() has been called and the backend is ready for queries."""

    # ─── Query Execution ──────────────────────────────────────────────

    @abstractmethod
    async def send_query(self, prompt: str) -> None:
        """Send a query/prompt to the provider.

        Does not return results — use receive_events() to consume the response.
        This split allows the caller to set up event handling before the
        response starts flowing.
        """

    @abstractmethod
    async def receive_events(self) -> AsyncIterator[ProviderEvent]:
        """Yield provider events for the current query.

        Events are provider-agnostic dataclasses. The caller is responsible
        for translating them to WebSocket messages, storing them, etc.

        This iterator completes when the query finishes (ResultEvent),
        is interrupted (InterruptedEvent), or errors (ErrorEvent).
        """

    @abstractmethod
    async def interrupt(self) -> None:
        """Interrupt the currently running query.

        Should be safe to call even if no query is running (no-op).
        """

    # ─── Interactive Responses ────────────────────────────────────────

    @abstractmethod
    async def respond_to_permission(
        self,
        tool_use_id: str,
        allow: bool,
        *,
        remember: bool = False,
        message: Optional[str] = None,
        updated_input: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Respond to a tool permission request from the provider.

        The provider blocks tool execution until this is called.

        ``message`` is the deny reason the model reads back as the blocked
        call's tool_result (adapters fall back to their own generic text when
        it is None). ``updated_input`` is the SDK's ``updatedInput``: the tool
        input to run on an allow, which is how a decider feeds collected data
        (e.g. AskUserQuestion answers) into the call.
        """

    @abstractmethod
    async def respond_to_user_question(self, answer: str) -> None:
        """Respond to a user question asked by the provider (AskUserQuestion tool)."""

    # ─── Runtime Configuration ────────────────────────────────────────

    @abstractmethod
    async def set_model(self, model: str) -> None:
        """Change the model for subsequent queries (hot-swap, no reconnect)."""

    @abstractmethod
    async def set_permission_mode(self, mode: str) -> None:
        """Change the permission mode for subsequent queries."""

    # ─── Session Identity ─────────────────────────────────────────────

    @property
    @abstractmethod
    def sdk_session_id(self) -> Optional[str]:
        """The provider-assigned session ID (for resume capability), or None."""

    # ─── Options Comparison ───────────────────────────────────────────

    @abstractmethod
    def requires_reconnect(self, new_options: SessionOptions) -> bool:
        """Check if changing to new_options requires a fresh connection.

        Some options (model, permission_mode) can be hot-swapped.
        Others (system_prompt, cwd, mcp_servers) require a new subprocess.
        """

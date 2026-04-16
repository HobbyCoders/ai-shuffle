"""
ClaudeAgentLLMService — bridges Pipecat's LLMService protocol to the
existing ClaudeAgentAdapter + SessionPool.

Why this exists:
    Phase 1 uses Pipecat's built-in AnthropicLLMService which talks directly
    to api.anthropic.com. That skips tools, MCP servers, subagents, hooks,
    cost tracking, and worktree isolation — all the features that make
    AI Shuffle more than a chat wrapper.

    This service plugs the Claude Agent SDK (already running for every text
    chat session) into the voice pipeline. The voice pipeline therefore
    inherits tool execution, MCP, worktree CWD, profile-scoped subagents,
    and unified persistence.

Design:
    - One adapter per voice session (not pooled — voice sessions are
      short-lived and we avoid contention with text chat sessions).
    - When a user utterance is finalized, Pipecat flushes the context
      aggregator. `_process_context` is called with the full context.
    - We extract the latest user message, call `adapter.send_query(...)`,
      then consume `adapter.receive_events()`. Each `StreamDeltaEvent`
      with `delta_type="text"` is pushed as an `LLMTextFrame` so Pipecat's
      TTS can synthesize in real-time.
    - Tool-use events are forwarded via an optional observer callback for
      UI display; tools execute inside the SDK — we just mirror them.

Limitations (documented in docs/KNOWN_ISSUES.md):
    - Permission prompts from tools would require an interactive path;
      voice sessions default to allow-safe tools only (Read/Grep/Glob).
      See `voice_permission_policy()`.
    - Interruptions: when the user barges in, we call `adapter.interrupt()`.
      The SDK's cancellation semantics are documented in the SDK docs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from app.core.provider.claude_adapter import ClaudeAgentAdapter
from app.core.provider.types import (
    ErrorEvent,
    InterruptedEvent,
    ProviderEvent,
    ResultEvent,
    StreamDeltaEvent,
    TextEvent,
    ToolUseEvent,
    ToolResultEvent,
    SessionOptions,
)

try:
    from pipecat.frames.frames import (
        Frame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMMessagesFrame,
        LLMTextFrame,
        StartInterruptionFrame,
        TextFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.services.llm_service import LLMService
except ImportError:  # pragma: no cover — Pipecat optional dep
    Frame = object  # type: ignore
    LLMFullResponseEndFrame = object  # type: ignore
    LLMFullResponseStartFrame = object  # type: ignore
    LLMMessagesFrame = object  # type: ignore
    LLMTextFrame = object  # type: ignore
    StartInterruptionFrame = object  # type: ignore
    TextFrame = object  # type: ignore
    FrameDirection = object  # type: ignore

    class LLMService:  # type: ignore
        """Stub for import-time safety when Pipecat is not installed."""
        pass


logger = logging.getLogger(__name__)


ToolObserver = Callable[[str, str, Any], None]  # (phase, name, payload)


class ClaudeAgentLLMService(LLMService):
    """
    Pipecat LLMService backed by a ClaudeAgentAdapter.

    Args:
        session_options: SDK SessionOptions (model, cwd, tools, MCP servers).
        tool_observer: optional callback for tool-use events (for UI display).
    """

    def __init__(
        self,
        session_options: SessionOptions,
        tool_observer: Optional[ToolObserver] = None,
    ):
        super().__init__()
        self._session_options = session_options
        self._tool_observer = tool_observer
        self._adapter: Optional[ClaudeAgentAdapter] = None
        self._active_turn_task: Optional[asyncio.Task] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        if self._connected and self._adapter is not None:
            return
        self._adapter = ClaudeAgentAdapter()
        await self._adapter.connect(self._session_options)
        self._connected = True

    async def stop(self, frame=None) -> None:  # type: ignore[override]
        await self._teardown()

    async def _teardown(self) -> None:
        if self._active_turn_task and not self._active_turn_task.done():
            self._active_turn_task.cancel()
            try:
                await self._active_turn_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._adapter is not None:
            try:
                await self._adapter.disconnect()
            except Exception:
                logger.debug("ClaudeAgentLLMService: adapter disconnect error", exc_info=True)
        self._adapter = None
        self._connected = False

    # ------------------------------------------------------------------
    # Frame processing
    # ------------------------------------------------------------------

    async def process_frame(self, frame, direction):  # type: ignore[override]
        await super().process_frame(frame, direction)

        # Pipecat sends a StartInterruptionFrame upstream when VAD detects
        # user speech during bot output. Cancel the active turn.
        if isinstance(frame, StartInterruptionFrame):
            await self._interrupt_active_turn()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMMessagesFrame):
            await self._handle_messages(frame)
            return

        # Non-matching frames pass through unchanged.
        await self.push_frame(frame, direction)

    async def _handle_messages(self, frame) -> None:
        """Extract the latest user message and dispatch a turn."""
        messages = getattr(frame, "messages", []) or []
        prompt = self._extract_latest_user_message(messages)
        if not prompt:
            logger.debug("ClaudeAgentLLMService: no user message to dispatch")
            return

        await self._ensure_connected()

        # Cancel any previous turn before starting a new one.
        await self._interrupt_active_turn()

        self._active_turn_task = asyncio.create_task(self._run_turn(prompt))

    @staticmethod
    def _extract_latest_user_message(messages: list[dict]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Content blocks — extract text pieces
                    return "".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                return str(content)
        return ""

    async def _interrupt_active_turn(self) -> None:
        if self._active_turn_task and not self._active_turn_task.done():
            if self._adapter is not None:
                try:
                    await self._adapter.interrupt()
                except Exception:
                    logger.debug("adapter.interrupt failed", exc_info=True)
            self._active_turn_task.cancel()
            try:
                await self._active_turn_task
            except (asyncio.CancelledError, Exception):
                pass
            self._active_turn_task = None

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def _run_turn(self, prompt: str) -> None:
        if self._adapter is None:
            logger.error("ClaudeAgentLLMService: adapter not connected")
            return

        try:
            await self.push_frame(LLMFullResponseStartFrame())
            await self._adapter.send_query(prompt)

            async for event in self._adapter.receive_events():
                await self._dispatch_event(event)
                if isinstance(event, (ResultEvent, InterruptedEvent, ErrorEvent)):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("ClaudeAgentLLMService turn error: %s", e)
        finally:
            try:
                await self.push_frame(LLMFullResponseEndFrame())
            except Exception:
                pass

    async def _dispatch_event(self, event: ProviderEvent) -> None:
        """Convert a ProviderEvent into Pipecat frame(s)."""
        if isinstance(event, StreamDeltaEvent):
            if event.delta_type == "text" and event.content:
                await self.push_frame(LLMTextFrame(event.content))
            # thinking / tool_input deltas are not spoken
            return

        if isinstance(event, TextEvent):
            if event.content:
                await self.push_frame(LLMTextFrame(event.content))
            return

        if isinstance(event, ToolUseEvent):
            if self._tool_observer:
                try:
                    self._tool_observer("tool_use", event.name, event.input)
                except Exception:
                    logger.debug("tool_observer raised", exc_info=True)
            return

        if isinstance(event, ToolResultEvent):
            if self._tool_observer:
                try:
                    self._tool_observer("tool_result", event.name, event.output)
                except Exception:
                    logger.debug("tool_observer raised", exc_info=True)
            return

        if isinstance(event, ErrorEvent):
            logger.warning("Voice LLM error event: %s", event)

        # Other events (SystemInit, Usage, RateLimit, Result, Interrupted)
        # are accepted silently — caller can tap via tool_observer for metrics.

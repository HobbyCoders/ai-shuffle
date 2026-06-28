"""
Live Session Store - In-memory store for active streaming sessions.

This provides server-authoritative state during streaming. The frontend
polls this store to get the current message state, eliminating the need
for complex buffer merging on reconnect.

Key principles:
1. Single source of truth during streaming
2. Messages are updated in-place as chunks arrive
3. Frontend can poll at any time and get consistent state
4. When streaming ends, state is cleared (JSONL becomes source of truth)
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class ToolStatus(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"


class AgentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class SubagentChild:
    """A child message within a subagent (text chunk or tool use)."""
    id: str
    type: str  # 'text', 'tool_use'
    content: str = ""
    streaming: bool = False
    # For tool_use children
    tool_name: Optional[str] = None
    tool_id: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    tool_result: Optional[str] = None
    tool_status: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "streaming": self.streaming,
        }
        if self.tool_name:
            result["toolName"] = self.tool_name
        if self.tool_id:
            result["toolId"] = self.tool_id
        if self.tool_input is not None:
            result["toolInput"] = self.tool_input
        if self.tool_result is not None:
            result["toolResult"] = self.tool_result
        if self.tool_status:
            result["toolStatus"] = self.tool_status
        return result


@dataclass
class LiveMessage:
    """A message in a live streaming session."""
    id: str
    role: str  # 'user', 'assistant', 'system'
    type: str  # 'text', 'thinking', 'tool_use', 'tool_result', 'subagent', 'system'
    content: str = ""
    streaming: bool = False

    # Raw JSONL entry uuid (when seeded from history). Distinct from `id`, which
    # is normalized for keying. Lets the UI rewind by uuid directly instead of
    # falling back to fragile content matching — kept on seeded history messages
    # so rewind works the same during a streaming turn as it does on reload.
    uuid: Optional[str] = None

    # For tool_use messages
    tool_name: Optional[str] = None
    tool_id: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    tool_result: Optional[str] = None
    tool_status: Optional[str] = None  # 'running', 'complete', 'error'
    # Screenshot (data URI) extracted from MCP image content blocks — e.g. the
    # Claude-in-Chrome browser tool — rendered inline below the tool call.
    tool_screenshot: Optional[str] = None

    # For subagent messages
    agent_id: Optional[str] = None
    agent_type: Optional[str] = None
    agent_description: Optional[str] = None
    agent_prompt: Optional[str] = None
    agent_status: Optional[str] = None  # 'pending', 'running', 'completed', 'error'
    agent_children: Optional[List[SubagentChild]] = None
    agent_usage: Optional[Dict[str, Any]] = None  # {total_tokens, tool_uses, duration_ms}

    # For team_member messages (Claude Code agent-teams feature)
    team_name: Optional[str] = None
    teammate_name: Optional[str] = None
    teammate_session_id: Optional[str] = None
    teammate_pid: Optional[int] = None
    teammate_status: Optional[str] = None  # 'running' | 'completed' | 'error' | 'killed'

    # For system messages
    system_subtype: Optional[str] = None
    system_data: Optional[Dict[str, Any]] = None

    # For thinking messages — live estimated token count, fed by the CLI's
    # streaming `thinking_tokens` system messages (desktop-app-style counter
    # shown on the thinking block instead of as standalone system cards).
    thinking_tokens: Optional[int] = None

    # Metadata (added at completion)
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "id": self.id,
            "role": self.role,
            "type": self.type,
            "content": self.content,
            "streaming": self.streaming,
        }

        # Raw JSONL uuid for uuid-based rewind (only on seeded history messages)
        if self.uuid:
            result["uuid"] = self.uuid

        # Tool fields
        if self.tool_name:
            result["toolName"] = self.tool_name
        if self.tool_id:
            result["toolId"] = self.tool_id
        if self.tool_input is not None:
            result["toolInput"] = self.tool_input
        if self.tool_result is not None:
            result["toolResult"] = self.tool_result
        if self.tool_status:
            result["toolStatus"] = self.tool_status
        if self.tool_screenshot:
            result["toolScreenshot"] = self.tool_screenshot

        # Subagent fields
        if self.agent_id:
            result["agentId"] = self.agent_id
        if self.agent_type:
            result["agentType"] = self.agent_type
        if self.agent_description:
            result["agentDescription"] = self.agent_description
        if self.agent_prompt:
            result["agentPrompt"] = self.agent_prompt
        if self.agent_status:
            result["agentStatus"] = self.agent_status
        if self.agent_children is not None:
            result["agentChildren"] = [c.to_dict() for c in self.agent_children]
        if self.agent_usage:
            result["agentUsage"] = self.agent_usage

        # Team member fields
        if self.team_name:
            result["teamName"] = self.team_name
        if self.teammate_name:
            result["teammateName"] = self.teammate_name
        if self.teammate_session_id:
            result["teammateSessionId"] = self.teammate_session_id
        if self.teammate_pid:
            result["teammatePid"] = self.teammate_pid
        if self.teammate_status:
            result["teammateStatus"] = self.teammate_status

        # System message fields
        if self.system_subtype:
            result["systemSubtype"] = self.system_subtype
        if self.system_data is not None:
            result["systemData"] = self.system_data

        # Thinking token estimate (live counter on thinking blocks)
        if self.thinking_tokens is not None:
            result["thinkingTokens"] = self.thinking_tokens

        # Metadata
        if self.metadata:
            result["metadata"] = self.metadata

        return result


@dataclass
class LiveSession:
    """State of an active streaming session."""
    session_id: str
    messages: List[LiveMessage] = field(default_factory=list)
    is_streaming: bool = True
    last_update_id: int = 0  # Monotonically increasing for efficient polling
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Number of initial (history) messages loaded at stream start.
    # These messages never change during streaming, so delta polls can skip them.
    initial_message_count: int = 0

    # Set after auto-compaction to force the next delta poll to return all
    # messages (initial_message_count=0), ensuring the frontend gets a clean
    # full snapshot instead of a stale delta merge.
    needs_full_refresh: bool = False

    # Currently active block type: 'thinking', 'text', 'tool_use', or None
    active_block_type: Optional[str] = None

    # Token usage (updated during streaming)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def to_dict(self, delta_only: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization.

        Args:
            delta_only: If True, only include streaming messages (skip initial history).
                        Used for delta polls where the frontend already has history.
        """
        if delta_only and self.initial_message_count > 0:
            messages = self.messages[self.initial_message_count:]
        else:
            messages = self.messages

        return {
            "session_id": self.session_id,
            "is_streaming": self.is_streaming,
            "last_update_id": self.last_update_id,
            "messages": [m.to_dict() for m in messages],
            "initial_message_count": self.initial_message_count if delta_only else 0,
            "active_block_type": self.active_block_type,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_creation_tokens": self.cache_creation_tokens,
                "cache_read_tokens": self.cache_read_tokens,
            }
        }


class LiveSessionStore:
    """
    In-memory store for active streaming sessions.

    Provides server-authoritative state during streaming. Frontend polls
    this store to get current message state, eliminating buffer merging.
    """

    def __init__(self):
        self._sessions: Dict[str, LiveSession] = {}
        self._lock = asyncio.Lock()  # Global lock for session creation/deletion only
        self._session_locks: Dict[str, asyncio.Lock] = {}  # Per-session locks

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create a per-session lock."""
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _increment_update_id(self, session: LiveSession) -> None:
        """Increment the update ID for change detection."""
        session.last_update_id += 1

    @staticmethod
    def _finalize_streaming(session: LiveSession, block_type: str) -> None:
        """Mark any currently-streaming assistant message of ``block_type`` as
        complete.

        Enforces the "at most one streaming message of a given block type"
        invariant. Without it, two streaming text messages can coexist (e.g.
        text → tool_use → text in one turn, or a rapid same-session re-query),
        and ``append_text``'s reverse-scan would route deltas to the wrong one —
        the "content bleeding between messages" symptom. Type-specific on
        purpose: finalizing text must not disturb a legitimately-streaming
        thinking block (the SDK's text-before-thinking reordering relies on it).
        """
        for msg in session.messages:
            if (
                msg.role == "assistant"
                and msg.streaming
                and msg.type == block_type
            ):
                msg.streaming = False

    @staticmethod
    def _boundary_token(subtype: str, data: Optional[Dict[str, Any]]) -> Optional[int]:
        """Extract the identifying token count for a compaction boundary.

        Handles both snake_case (as normalized by jsonl_parser for compact_boundary)
        and camelCase (as emitted raw by the SDK stream and used for microcompact).
        Returns None if the token cannot be determined.
        """
        if not isinstance(data, dict):
            return None
        if subtype == "compact_boundary":
            meta = data.get("compact_metadata") or data.get("compactMetadata") or {}
            if not isinstance(meta, dict):
                return None
            value = meta.get("pre_tokens")
            if value is None:
                value = meta.get("preTokens")
            return value
        if subtype == "microcompact_boundary":
            meta = (
                data.get("microcompactMetadata")
                or data.get("microcompact_metadata")
                or {}
            )
            if not isinstance(meta, dict):
                return None
            value = meta.get("tokensSaved")
            if value is None:
                value = meta.get("tokens_saved")
            return value
        return None

    def _has_matching_boundary(
        self,
        messages: List[LiveMessage],
        subtype: str,
        data: Optional[Dict[str, Any]]
    ) -> bool:
        """Check if a compaction boundary message already exists.

        Matches by subtype and identifying token count to detect duplicates
        when stream replay includes events already in JSONL history.

        Returns False when the new event's token cannot be extracted — we
        cannot positively identify a duplicate without it, and we must not
        collapse distinct events just because both have missing metadata.
        """
        new_token = self._boundary_token(subtype, data)
        if new_token is None:
            return False

        for msg in messages:
            if msg.system_subtype != subtype:
                continue
            existing_token = self._boundary_token(subtype, msg.system_data)
            if existing_token is not None and existing_token == new_token:
                return True

        return False

    async def start_streaming(
        self,
        session_id: str,
        initial_messages: Optional[List[Dict[str, Any]]] = None
    ) -> LiveSession:
        """
        Initialize a streaming session.

        Args:
            session_id: The session ID
            initial_messages: Existing messages to include (from JSONL history)

        Returns:
            The created LiveSession
        """
        async with self._lock:
            # Convert initial messages to LiveMessage objects
            messages: List[LiveMessage] = []
            if initial_messages:
                for i, m in enumerate(initial_messages):
                    # Rehydrate SubagentChild dicts (from DB persistence) back into
                    # dataclass instances so the live store serializes them
                    # consistently with messages produced during active streaming.
                    raw_children = m.get("agentChildren") or m.get("agent_children")
                    hydrated_children: Optional[List[SubagentChild]] = None
                    if raw_children:
                        hydrated_children = []
                        for c in raw_children:
                            if isinstance(c, SubagentChild):
                                hydrated_children.append(c)
                                continue
                            if not isinstance(c, dict):
                                continue
                            hydrated_children.append(SubagentChild(
                                id=str(c.get("id") or c.get("tool_id") or f"child-{len(hydrated_children)}"),
                                type=c.get("type", "tool_use"),
                                content=c.get("content", "") or "",
                                streaming=bool(c.get("streaming", False)),
                                tool_name=c.get("toolName") or c.get("tool_name"),
                                tool_id=c.get("toolId") or c.get("tool_id"),
                                tool_input=c.get("toolInput") or c.get("tool_input"),
                                tool_result=c.get("toolResult") or c.get("tool_result"),
                                tool_status=c.get("toolStatus") or c.get("tool_status"),
                            ))

                    msg = LiveMessage(
                        id=str(m.get("id", f"history-{i}")),
                        role=m.get("role", "user"),
                        type=m.get("type", "text"),
                        content=m.get("content", ""),
                        streaming=False,
                        uuid=m.get("uuid"),
                        tool_name=m.get("toolName") or m.get("tool_name"),
                        tool_id=m.get("toolId") or m.get("tool_id"),
                        tool_input=m.get("toolInput") or m.get("tool_input"),
                        tool_result=m.get("toolResult") or m.get("tool_result"),
                        tool_status=m.get("toolStatus") or m.get("tool_status"),
                        agent_id=m.get("agentId") or m.get("agent_id"),
                        agent_type=m.get("agentType") or m.get("agent_type"),
                        agent_description=m.get("agentDescription") or m.get("agent_description"),
                        agent_prompt=m.get("agentPrompt") or m.get("agent_prompt"),
                        agent_status=m.get("agentStatus") or m.get("agent_status"),
                        agent_children=hydrated_children,
                        agent_usage=m.get("agentUsage") or m.get("agent_usage"),
                        # Team member fields
                        team_name=m.get("teamName") or m.get("team_name"),
                        teammate_name=m.get("teammateName") or m.get("teammate_name"),
                        teammate_session_id=m.get("teammateSessionId") or m.get("teammate_session_id"),
                        teammate_pid=m.get("teammatePid") or m.get("teammate_pid"),
                        teammate_status=m.get("teammateStatus") or m.get("teammate_status"),
                        # System message fields
                        system_subtype=m.get("systemSubtype") or m.get("subtype"),
                        system_data=m.get("systemData") or m.get("data"),
                        metadata=m.get("metadata"),
                    )
                    messages.append(msg)

            # Carry forward token usage from the previous session if it exists.
            # Without this, a new query in the same session resets usage to 0,
            # causing the frontend context bar to flash to 0% until the first
            # stream_start event arrives with fresh usage data (~300ms gap).
            prev_session = self._sessions.get(session_id)

            session = LiveSession(
                session_id=session_id,
                messages=messages,
                is_streaming=True,
                last_update_id=1,
                initial_message_count=len(messages),
            )

            if prev_session:
                session.input_tokens = prev_session.input_tokens
                session.output_tokens = prev_session.output_tokens
                session.cache_creation_tokens = prev_session.cache_creation_tokens
                session.cache_read_tokens = prev_session.cache_read_tokens

            self._sessions[session_id] = session
            # Reuse the existing per-session lock if one is already present.
            # Replacing it on a rapid same-session re-query would break mutual
            # exclusion: an in-flight writer from the previous query may still
            # hold the OLD lock object, so a new writer acquiring the NEW lock
            # runs concurrently — interleaving appends and corrupting message
            # state. Keep one stable lock per session_id for its whole lifetime.
            if session_id not in self._session_locks:
                self._session_locks[session_id] = asyncio.Lock()
            logger.debug(f"Started live streaming for session {session_id} with {len(messages)} initial messages")

        # Clear any "unpark pending" latch: this turn is now streaming, so a
        # polling client will see is_streaming=true and upshift on its own. This is
        # the primary clear for the unpark follow-up (it flips together with
        # is_streaming). Outside the lock — task_registry runs on the same loop and
        # imports nothing from here (no circular dependency). See task_registry.
        try:
            from app.core.task_registry import task_registry
            task_registry.clear_unpark_pending(session_id)
        except Exception:
            logger.debug("task_registry.clear_unpark_pending failed", exc_info=True)
        return session

    async def add_user_message(self, session_id: str, content: str, message_id: str) -> None:
        """Add a user message to the session."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"Cannot add user message: session {session_id} not found")
                return

            msg = LiveMessage(
                id=message_id,
                role="user",
                type="text",
                content=content,
                streaming=False,
            )
            session.messages.append(msg)
            self._increment_update_id(session)
            logger.debug(f"Added user message to session {session_id}")

    async def start_assistant_message(self, session_id: str, message_id: str) -> None:
        """Start a new assistant text message for streaming."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"Cannot start assistant message: session {session_id} not found")
                return

            # Invariant: at most one streaming text message at a time.
            # Finalize any prior streaming text first, otherwise a later text
            # delta (routed to "the last streaming text message") could bleed
            # into the wrong message when the SDK emits text → tool → text.
            self._finalize_streaming(session, "text")

            msg = LiveMessage(
                id=message_id,
                role="assistant",
                type="text",
                content="",
                streaming=True,
            )
            session.messages.append(msg)
            self._increment_update_id(session)
            logger.debug(f"Started assistant message {message_id} in session {session_id}")

    async def append_text(self, session_id: str, content: str) -> None:
        """Append text to the current streaming text message."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"append_text: session {session_id} not found (data dropped)")
                return

            session.active_block_type = "text"

            # Find the last streaming text message
            for msg in reversed(session.messages):
                if msg.type == "text" and msg.role == "assistant" and msg.streaming:
                    msg.content += content
                    self._increment_update_id(session)
                    return

            # No streaming text message found - create one
            msg = LiveMessage(
                id=f"text-{uuid.uuid4().hex[:12]}",
                role="assistant",
                type="text",
                content=content,
                streaming=True,
            )
            session.messages.append(msg)
            self._increment_update_id(session)

    async def append_thinking(self, session_id: str, content: str) -> None:
        """Append thinking content to the current streaming thinking message."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"append_thinking: session {session_id} not found (data dropped)")
                return

            session.active_block_type = "thinking"

            # Find the last streaming thinking message
            for msg in reversed(session.messages):
                if msg.type == "thinking" and msg.role == "assistant" and msg.streaming:
                    msg.content += content
                    self._increment_update_id(session)
                    return

            # No streaming thinking message found - create one.
            # Insert BEFORE any trailing streaming text messages. The SDK sometimes
            # emits text blocks (e.g., "\n\n") before thinking blocks, creating a
            # text message first. Thinking must appear before text in the array so
            # that when real text content arrives later, it renders below thinking.
            insert_idx = len(session.messages)
            while insert_idx > 0:
                prev = session.messages[insert_idx - 1]
                if prev.type == "text" and prev.role == "assistant" and prev.streaming:
                    insert_idx -= 1
                else:
                    break

            msg = LiveMessage(
                id=f"thinking-{uuid.uuid4().hex[:12]}",
                role="assistant",
                type="thinking",
                content=content,
                streaming=True,
            )
            session.messages.insert(insert_idx, msg)
            self._increment_update_id(session)

    async def finalize_thinking(self, session_id: str) -> None:
        """Mark the current thinking message as complete (not streaming)."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"finalize_thinking: session {session_id} not found")
                return

            # Find and finalize the last streaming thinking message
            for msg in reversed(session.messages):
                if msg.type == "thinking" and msg.role == "assistant" and msg.streaming:
                    msg.streaming = False
                    self._increment_update_id(session)
                    return

    async def update_thinking_tokens(
        self, session_id: str, estimated_tokens: Optional[int]
    ) -> bool:
        """Attach the CLI's live thinking-token estimate to the current thinking
        block — the streaming counterpart to how `update_subagent_progress`
        routes `task_progress` usage to its subagent. Keeps these high-frequency
        estimates off the chat as standalone system cards.

        Returns True if a streaming thinking block was found and updated. If no
        thinking block is currently streaming, the estimate is dropped (the
        handler's documented behavior) so a stale, just-finalized block from a
        previous turn or tool-use loop isn't mutated with the wrong live count.
        """
        if estimated_tokens is None:
            return False
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                return False
            # Only the currently STREAMING thinking block — mirrors
            # append_thinking/finalize_thinking. A finalized block stays in
            # session.messages (finalize only flips streaming=False), so without
            # the streaming filter an early estimate for the next turn's block
            # would overwrite the previous block's counter.
            for msg in reversed(session.messages):
                if msg.type == "thinking" and msg.role == "assistant" and msg.streaming:
                    msg.thinking_tokens = estimated_tokens
                    self._increment_update_id(session)
                    return True
            return False

    async def add_tool_use(
        self,
        session_id: str,
        tool_id: str,
        name: str,
        tool_input: Dict[str, Any]
    ) -> None:
        """Add a tool use message, or update an existing one created early by stream_block_start."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"add_tool_use: session {session_id} not found (tool {name} dropped)")
                return

            session.active_block_type = "tool_use"

            # Check if this tool was already created early (by stream_block_start)
            for msg in session.messages:
                if msg.type == "tool_use" and msg.tool_id == tool_id:
                    # Update existing tool with complete input
                    msg.tool_input = tool_input
                    if name and name != "unknown":
                        msg.tool_name = name
                    self._increment_update_id(session)
                    logger.debug(f"Updated early tool_use {name} ({tool_id}) in session {session_id}")
                    return

            # Mark any current streaming text or thinking as complete
            for msg in reversed(session.messages):
                if msg.role == "assistant" and msg.streaming:
                    if msg.type in ("text", "thinking"):
                        msg.streaming = False
                        # Don't break - finalize both if they exist

            # Add tool use message
            msg = LiveMessage(
                id=f"tool-{tool_id}",
                role="assistant",
                type="tool_use",
                content="",
                streaming=True,
                tool_name=name,
                tool_id=tool_id,
                tool_input=tool_input,
                tool_status=ToolStatus.RUNNING.value,
            )
            session.messages.append(msg)
            self._increment_update_id(session)
            logger.debug(f"Added tool_use {name} ({tool_id}) to session {session_id}")

    async def add_early_tool_use(
        self,
        session_id: str,
        tool_id: str,
        name: str
    ) -> None:
        """Create a tool_use message early from stream_block_start.

        This is called when the API starts streaming a tool_use block, before
        the full AssistantMessage arrives. This makes the tool visible to
        polling clients immediately instead of waiting for the complete input.
        """
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"add_early_tool_use: session {session_id} not found")
                return

            # Don't create duplicate if tool already exists
            for msg in session.messages:
                if msg.type == "tool_use" and msg.tool_id == tool_id:
                    return

            # Mark any current streaming text or thinking as complete
            for msg in reversed(session.messages):
                if msg.role == "assistant" and msg.streaming:
                    if msg.type in ("text", "thinking"):
                        msg.streaming = False

            msg = LiveMessage(
                id=f"tool-{tool_id}",
                role="assistant",
                type="tool_use",
                content="",
                streaming=True,
                tool_name=name,
                tool_id=tool_id,
                tool_input={},  # Will be filled by input_json_delta and final tool_use
                tool_status=ToolStatus.RUNNING.value,
            )
            session.messages.append(msg)
            self._increment_update_id(session)
            logger.debug(f"Added early tool_use {name} ({tool_id}) to session {session_id}")

    async def update_tool_input_streaming(
        self,
        session_id: str,
        tool_id: str,
        input_delta: str
    ) -> None:
        """Update tool input during streaming (for partial JSON input)."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"update_tool_input_streaming: session {session_id} not found")
                return

            # Find the tool_use message
            for msg in reversed(session.messages):
                if msg.type == "tool_use" and msg.tool_id == tool_id:
                    # Append to content (used for streaming display)
                    msg.content += input_delta
                    self._increment_update_id(session)
                    return

    @staticmethod
    def _is_background_ack(msg: "LiveMessage", result: str) -> bool:
        """True if ``result`` is merely the acknowledgement for a *backgrounded*
        command that is still executing — not a real completion.

        Backgrounded Bash (``run_in_background: true``) returns control to the
        model immediately: the SDK emits a ``tool_result`` right away whose body
        is just "Command running in the background ... ID: bash_xxx", while the
        shell keeps working. Treating that early result as COMPLETE flips the UI
        to a green check while the command is still running (the reported bug).
        The real output later arrives via a separate BashOutput/TaskOutput tool,
        not a second result for this id, so we must keep this tool RUNNING.

        Detection uses two signals:
        1. The recorded tool input (authoritative — captured at add_tool_use)
           has a truthy ``run_in_background`` flag.
        2. A content sniff of the acknowledgement text, covering cases where the
           input flag isn't surfaced (e.g. the CLI auto-backgrounds a command).
        """
        # Only Bash-family tools background; never hold back other tools.
        if msg.tool_name not in ("Bash", "PowerShell"):
            return False

        tool_input = msg.tool_input or {}
        backgrounded = bool(
            tool_input.get("run_in_background")
            or tool_input.get("runInBackground")
        )

        if not backgrounded:
            # Fallback: sniff the standard CLI acknowledgement so we still hold
            # the running state even when the input flag wasn't recorded.
            text = (result or "").strip().lower()
            if not text:
                return False
            backgrounded = (
                "running in the background" in text
                or "running in background" in text
                or ("background with id" in text)
            )

        return backgrounded

    async def add_tool_result(
        self,
        session_id: str,
        tool_id: str,
        result: str,
        is_error: bool = False,
        screenshot: Optional[str] = None,
    ) -> None:
        """Add tool result to the matching tool_use message.

        ``screenshot`` is an optional data URI extracted from MCP image content
        blocks (e.g. the Claude-in-Chrome browser tool) for inline display
        directly below the tool call.
        """
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"add_tool_result: session {session_id} not found (tool {tool_id} result dropped)")
                return

            session.active_block_type = None  # Tool result completes the tool

            # Find the tool_use message and add result to it
            for msg in session.messages:
                if msg.type == "tool_use" and msg.tool_id == tool_id:
                    msg.tool_result = result
                    if screenshot:
                        msg.tool_screenshot = screenshot
                    if is_error:
                        msg.tool_status = ToolStatus.ERROR.value
                        msg.streaming = False
                    elif self._is_background_ack(msg, result):
                        # Backgrounded command still executing — surface the
                        # acknowledgement output but keep the running indicator
                        # so the UI doesn't claim completion prematurely.
                        msg.tool_status = ToolStatus.RUNNING.value
                        msg.streaming = False
                        logger.debug(
                            f"Tool {tool_id} ({msg.tool_name}) backgrounded in "
                            f"session {session_id}; keeping status=running"
                        )
                    else:
                        msg.tool_status = ToolStatus.COMPLETE.value
                        msg.streaming = False
                    self._increment_update_id(session)
                    logger.debug(f"Added tool result to {tool_id} in session {session_id} (result_len={len(result) if result else 0})")
                    # Note: We don't create a continuation text message here.
                    # Empty text messages cause UI flickering (showing "Claude [time]" header
                    # with no content). The next text chunk will create a message if needed.
                    return

            # No matching tool_use found — the result will be lost
            tool_types = [(m.type, m.tool_id) for m in session.messages if m.type == "tool_use"]
            logger.warning(
                f"add_tool_result: no matching tool_use for {tool_id} in session {session_id}. "
                f"Existing tool_use messages: {tool_types}"
            )

    async def add_system_message(
        self,
        session_id: str,
        content: str,
        subtype: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add a system message (e.g., /context output)."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"add_system_message: session {session_id} not found (system message dropped)")
                return

            # On compact_boundary, flag the session so the next delta poll
            # returns ALL messages (initial_message_count=0). This forces
            # the frontend to do a full replace instead of a delta merge,
            # preventing stale history from being shown after compaction.
            #
            # Order matters: dedup must run BEFORE mutating session state so
            # that a duplicate event (stream replay of a JSONL-loaded boundary)
            # is a complete no-op. Setting needs_full_refresh for a duplicate
            # would latch the flag onto the next unrelated update and force a
            # spurious full refresh on the frontend.
            if subtype in ('compact_boundary', 'microcompact_boundary'):
                if self._has_matching_boundary(session.messages, subtype, data):
                    logger.debug(
                        f"Skipping duplicate {subtype} message for session {session_id} "
                        f"(already present in history)"
                    )
                    return

                session.needs_full_refresh = True
                logger.debug(
                    f"Compaction boundary: flagged needs_full_refresh "
                    f"for session {session_id}"
                )
            # hook_event is intentionally NOT deduplicated: each fires as a
            # discrete event (PreToolUse / PostToolUse per tool call) and must
            # appear individually. LiveMessage IDs are uuid-suffixed so no two
            # rows share the same ID even when hook_event_name is identical.

            msg = LiveMessage(
                id=f"system-{uuid.uuid4().hex[:12]}",
                role="system",
                type="system",
                content=content,
                streaming=False,
                system_subtype=subtype,
                system_data=data,
            )
            session.messages.append(msg)
            self._increment_update_id(session)

    async def convert_tool_to_subagent(
        self,
        session_id: str,
        tool_id: str,
        agent_id: str,
        agent_type: str,
        description: str,
        prompt: str
    ) -> bool:
        """Convert an early tool_use message into a subagent message.

        Called when we discover that a tool_use created by stream_block_start
        is actually a Task tool (subagent). Returns True if conversion happened.
        """
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                return False

            for msg in session.messages:
                if msg.type == "tool_use" and msg.tool_id == tool_id:
                    # Convert in-place to subagent
                    msg.id = f"subagent-{agent_id}"
                    msg.type = "subagent"
                    msg.tool_name = None
                    msg.tool_input = None
                    msg.tool_status = None
                    msg.agent_id = agent_id
                    msg.agent_type = agent_type
                    msg.agent_description = description
                    msg.agent_prompt = prompt
                    msg.agent_status = AgentStatus.RUNNING.value
                    msg.agent_children = []
                    self._increment_update_id(session)
                    logger.debug(f"Converted early tool_use {tool_id} to subagent in session {session_id}")
                    return True

            return False

    async def add_subagent_start(
        self,
        session_id: str,
        agent_id: str,
        agent_type: str,
        description: str,
        prompt: str,
        tool_id: Optional[str] = None
    ) -> None:
        """Start a subagent message group."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"add_subagent_start: session {session_id} not found (agent {agent_id} dropped)")
                return

            # Mark any current streaming text or thinking as complete
            for msg in reversed(session.messages):
                if msg.role == "assistant" and msg.streaming:
                    if msg.type in ("text", "thinking"):
                        msg.streaming = False
                        # Don't break - finalize both if they exist

            msg = LiveMessage(
                id=f"subagent-{agent_id}",
                role="assistant",
                type="subagent",
                content="",
                streaming=True,
                tool_id=tool_id,
                agent_id=agent_id,
                agent_type=agent_type,
                agent_description=description,
                agent_prompt=prompt,
                agent_status=AgentStatus.RUNNING.value,
                agent_children=[],
            )
            session.messages.append(msg)
            self._increment_update_id(session)
            logger.debug(f"Started subagent {agent_id} in session {session_id}")

    async def append_subagent_text(
        self,
        session_id: str,
        agent_id: str,
        content: str
    ) -> None:
        """Append text to a subagent's output."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"append_subagent_text: session {session_id} not found")
                return

            # Find the subagent message
            for msg in session.messages:
                if msg.type == "subagent" and msg.agent_id == agent_id:
                    if msg.agent_children is None:
                        msg.agent_children = []

                    # Find or create text child
                    text_child = None
                    for child in msg.agent_children:
                        if child.type == "text" and child.streaming:
                            text_child = child
                            break

                    if text_child:
                        text_child.content += content
                    else:
                        text_child = SubagentChild(
                            id=f"{agent_id}-text-{len(msg.agent_children)}",
                            type="text",
                            content=content,
                            streaming=True,
                        )
                        msg.agent_children.append(text_child)

                    self._increment_update_id(session)
                    return

    async def add_subagent_tool_use(
        self,
        session_id: str,
        agent_id: str,
        tool_id: str,
        tool_name: str,
        tool_input: Dict[str, Any]
    ) -> None:
        """Add a tool use within a subagent."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"add_subagent_tool_use: session {session_id} not found")
                return

            for msg in session.messages:
                if msg.type == "subagent" and msg.agent_id == agent_id:
                    if msg.agent_children is None:
                        msg.agent_children = []

                    # Mark any streaming text child as complete
                    for child in msg.agent_children:
                        if child.type == "text" and child.streaming:
                            child.streaming = False

                    # Add tool use child
                    tool_child = SubagentChild(
                        id=f"{agent_id}-tool-{tool_id}",
                        type="tool_use",
                        content="",
                        streaming=True,
                        tool_name=tool_name,
                        tool_id=tool_id,
                        tool_input=tool_input,
                        tool_status=ToolStatus.RUNNING.value,
                    )
                    msg.agent_children.append(tool_child)
                    self._increment_update_id(session)
                    return

    async def add_subagent_tool_result(
        self,
        session_id: str,
        agent_id: str,
        tool_id: str,
        result: str,
        is_error: bool = False
    ) -> None:
        """Add tool result within a subagent."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"add_subagent_tool_result: session {session_id} not found")
                return

            for msg in session.messages:
                if msg.type == "subagent" and msg.agent_id == agent_id:
                    if msg.agent_children:
                        for child in msg.agent_children:
                            if child.type == "tool_use" and child.tool_id == tool_id:
                                child.tool_result = result
                                child.tool_status = ToolStatus.ERROR.value if is_error else ToolStatus.COMPLETE.value
                                child.streaming = False
                                self._increment_update_id(session)
                                return

    async def complete_subagent(
        self,
        session_id: str,
        agent_id: str,
        result: str,
        is_error: bool = False
    ) -> None:
        """Mark a subagent as complete."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"complete_subagent: session {session_id} not found (agent {agent_id})")
                return

            for msg in session.messages:
                if msg.type == "subagent" and msg.agent_id == agent_id:
                    msg.content = result
                    msg.agent_status = AgentStatus.ERROR.value if is_error else AgentStatus.COMPLETED.value
                    msg.streaming = False

                    # Mark all children as not streaming
                    if msg.agent_children:
                        for child in msg.agent_children:
                            child.streaming = False

                    self._increment_update_id(session)
                    # Note: We don't create a continuation text message here.
                    # Empty text messages cause UI flickering (showing "Claude [time]" header
                    # with no content). The next text chunk will create a message if needed.
                    return

    async def update_subagent_progress(
        self,
        session_id: str,
        tool_use_id: str,
        usage: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Update a subagent's progress info (usage stats).

        Matches the subagent by its parent tool_use_id. Returns True if found.
        """
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                return False

            for msg in session.messages:
                if msg.type == "subagent" and msg.tool_id == tool_use_id:
                    if usage:
                        msg.agent_usage = usage
                    self._increment_update_id(session)
                    return True

            return False

    async def complete_subagent_by_tool_id(
        self,
        session_id: str,
        tool_use_id: str,
        is_error: bool = False,
        usage: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Mark a subagent complete, matched by its parent ``tool_use_id``.

        The SDK's ``TaskNotificationMessage`` identifies the finished sub-agent by
        ``tool_use_id`` (not ``agent_id``), so this mirrors ``complete_subagent``
        but matches on ``tool_id`` and optionally applies final usage stats.
        Does NOT overwrite the subagent's accumulated content (the result text is
        delivered to the parent via the unpark turn, not stuffed into the card).
        Returns True if a matching subagent was found.
        """
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                return False

            for msg in session.messages:
                if msg.type == "subagent" and msg.tool_id == tool_use_id:
                    msg.agent_status = (
                        AgentStatus.ERROR.value if is_error else AgentStatus.COMPLETED.value
                    )
                    msg.streaming = False
                    if usage:
                        msg.agent_usage = usage
                    if msg.agent_children:
                        for child in msg.agent_children:
                            child.streaming = False
                    self._increment_update_id(session)
                    return True

            return False

    # ------------------------------------------------------------------
    # Team member methods (Claude Code agent-teams feature)
    # ------------------------------------------------------------------

    def _find_team_member(
        self,
        session: LiveSession,
        team_name: str,
        teammate_name: str,
    ) -> Optional[LiveMessage]:
        for msg in session.messages:
            if (
                msg.type == "team_member"
                and msg.team_name == team_name
                and msg.teammate_name == teammate_name
            ):
                return msg
        return None

    async def start_team_member(
        self,
        session_id: str,
        team_name: str,
        teammate_name: str,
        teammate_session_id: Optional[str] = None,
        pid: Optional[int] = None,
        prompt: Optional[str] = None,
    ) -> None:
        """Add a team_member message (idempotent — updates existing entry)."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(
                    f"start_team_member: session {session_id} not found (team {team_name}/{teammate_name} dropped)"
                )
                return

            existing = self._find_team_member(session, team_name, teammate_name)
            if existing:
                # Refresh metadata in case it was sparse at first.
                if teammate_session_id:
                    existing.teammate_session_id = teammate_session_id
                if pid:
                    existing.teammate_pid = pid
                if prompt and not existing.agent_prompt:
                    existing.agent_prompt = prompt
                if existing.teammate_status in (None, ""):
                    existing.teammate_status = "running"
                self._increment_update_id(session)
                return

            # Finalize any currently streaming text/thinking so the team_member
            # block renders in visual sequence.
            for msg in reversed(session.messages):
                if msg.role == "assistant" and msg.streaming and msg.type in ("text", "thinking"):
                    msg.streaming = False

            msg = LiveMessage(
                id=f"team-{team_name}-{teammate_name}",
                role="assistant",
                type="team_member",
                content="",
                streaming=True,
                agent_prompt=prompt,
                agent_children=[],
                team_name=team_name,
                teammate_name=teammate_name,
                teammate_session_id=teammate_session_id,
                teammate_pid=pid,
                teammate_status="running",
            )
            session.messages.append(msg)
            self._increment_update_id(session)
            logger.debug(
                f"Started team_member {team_name}/{teammate_name} in session {session_id}"
            )

    async def append_team_member_text(
        self,
        session_id: str,
        team_name: str,
        teammate_name: str,
        content: str,
    ) -> None:
        """Append a text chunk to the teammate's output."""
        if not content:
            return
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                return
            msg = self._find_team_member(session, team_name, teammate_name)
            if not msg:
                return
            if msg.agent_children is None:
                msg.agent_children = []

            # Coalesce into the current streaming text child when present.
            text_child = None
            for child in msg.agent_children:
                if child.type == "text" and child.streaming:
                    text_child = child
                    break
            if text_child:
                text_child.content += content
            else:
                text_child = SubagentChild(
                    id=f"{team_name}-{teammate_name}-text-{len(msg.agent_children)}",
                    type="text",
                    content=content,
                    streaming=True,
                )
                msg.agent_children.append(text_child)
            self._increment_update_id(session)

    async def add_team_member_tool_use(
        self,
        session_id: str,
        team_name: str,
        teammate_name: str,
        tool_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
    ) -> None:
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                return
            msg = self._find_team_member(session, team_name, teammate_name)
            if not msg:
                return
            if msg.agent_children is None:
                msg.agent_children = []

            # Close any streaming text child; tool_use starts a new group.
            for child in msg.agent_children:
                if child.type == "text" and child.streaming:
                    child.streaming = False

            # Skip duplicates if the same tool_id arrives twice.
            for child in msg.agent_children:
                if child.type == "tool_use" and child.tool_id == tool_id:
                    return

            tool_child = SubagentChild(
                id=f"{team_name}-{teammate_name}-tool-{tool_id}",
                type="tool_use",
                content="",
                streaming=True,
                tool_name=tool_name,
                tool_id=tool_id,
                tool_input=tool_input,
                tool_status=ToolStatus.RUNNING.value,
            )
            msg.agent_children.append(tool_child)
            self._increment_update_id(session)

    async def add_team_member_tool_result(
        self,
        session_id: str,
        team_name: str,
        teammate_name: str,
        tool_id: str,
        result: str,
        is_error: bool = False,
    ) -> None:
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                return
            msg = self._find_team_member(session, team_name, teammate_name)
            if not msg or not msg.agent_children:
                return

            for child in msg.agent_children:
                if child.type == "tool_use" and child.tool_id == tool_id:
                    child.tool_result = result
                    child.tool_status = (
                        ToolStatus.ERROR.value if is_error else ToolStatus.COMPLETE.value
                    )
                    child.streaming = False
                    self._increment_update_id(session)
                    return

    async def update_team_member_status(
        self,
        session_id: str,
        team_name: str,
        teammate_name: str,
        status: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
        pid: Optional[int] = None,
    ) -> None:
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                return
            msg = self._find_team_member(session, team_name, teammate_name)
            if not msg:
                return
            if status:
                msg.teammate_status = status
                # Bring the header indicator in sync with the lifecycle.
                if status in ("completed", "error", "killed"):
                    msg.streaming = False
                    if msg.agent_children:
                        for c in msg.agent_children:
                            c.streaming = False
            if usage:
                msg.agent_usage = usage
            if pid:
                msg.teammate_pid = pid
            self._increment_update_id(session)

    def has_active_team_members(self, session_id: str) -> bool:
        """True if any team_member in the session is still running."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        for msg in session.messages:
            if msg.type == "team_member" and msg.teammate_status == "running":
                return True
        return False

    async def update_usage(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0
    ) -> None:
        """Update token usage counts."""
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"update_usage: session {session_id} not found")
                return

            session.input_tokens = input_tokens
            session.output_tokens = output_tokens
            session.cache_creation_tokens = cache_creation_tokens
            session.cache_read_tokens = cache_read_tokens
            self._increment_update_id(session)

    async def end_streaming(
        self,
        session_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        interrupted: bool = False
    ) -> Optional[List[Dict[str, Any]]]:
        """
        End streaming for a session.

        IMPORTANT: This method keeps the session in memory (with is_streaming=False)
        instead of removing it immediately. This ensures that if a client reconnects
        and calls load_session before the JSONL file is fully written, they can still
        retrieve the messages from the in-memory store.

        The session will be cleaned up when:
        1. A new streaming session starts (overwrites the old session)
        2. cleanup_ended_session() is called explicitly
        3. The server restarts

        Args:
            session_id: The session ID
            metadata: Final metadata to attach to last assistant message
            interrupted: Whether streaming was interrupted (adds [Stopped] marker)

        Returns:
            The final messages as a list of dicts, or None if session not found
        """
        async with self._get_session_lock(session_id):
            # Backstop clear of the "unpark pending" latch, ABOVE the not-found
            # guard so it fires even when the session was already evicted: a turn
            # ending means no follow-up is mid-handoff for this session. If a
            # follow-up unpark turn dies or is cancelled before its start_streaming
            # (the primary clear) AND its session buffer was concurrently reaped,
            # the only end_streaming reaching here finds no session — so clearing
            # after the guard would leak the latch until cleanup_ended_session.
            # Safe for the unpark path: _deliver_unpark_impl sets the latch AFTER
            # its own end_streaming call, so this never wipes a just-set latch.
            try:
                from app.core.task_registry import task_registry
                task_registry.clear_unpark_pending(session_id)
            except Exception:
                logger.debug("task_registry.clear_unpark_pending failed", exc_info=True)

            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"Cannot end streaming: session {session_id} not found")
                return None

            # Mark all messages as not streaming
            for msg in session.messages:
                msg.streaming = False

            # Remove noise messages that shouldn't render in the chat UI
            session.messages = [
                msg for msg in session.messages
                if not (
                    # Whitespace-only assistant text blocks (SDK artifact before thinking blocks)
                    (msg.type == "text" and msg.role == "assistant" and not msg.content.strip())
                    # SDK task-notification callbacks (background agent completion, already in SubagentMessage)
                    or (msg.role == "user" and "<task-notification>" in msg.content)
                )
            ]

            # Add metadata to last assistant message
            if session.messages:
                for msg in reversed(session.messages):
                    if msg.role == "assistant":
                        msg.metadata = metadata
                        if interrupted:
                            msg.content += "\n\n[Stopped]"
                        break

            session.is_streaming = False
            session.active_block_type = None
            # Force the next poll to return full state instead of a delta.
            # end_streaming may have filtered messages out of self.messages,
            # which shifts the delta slice (self.messages[initial_message_count:])
            # and can drop real streaming messages from the client view.
            # The full-refresh path serializes all messages + initial_message_count=0,
            # matching what a page reload would fetch via since_update_id=0.
            session.needs_full_refresh = True
            self._increment_update_id(session)
            context_used = session.input_tokens + session.cache_creation_tokens + session.cache_read_tokens
            logger.warning(
                f"[ContextUsage:DEBUG] end_streaming session={session_id} "
                f"| final context_used={context_used} "
                f"| input={session.input_tokens} cache_create={session.cache_creation_tokens} cache_read={session.cache_read_tokens} "
                f"| output={session.output_tokens} "
                f"| msgs={len(session.messages)} interrupted={interrupted}"
            )
            logger.info(f"Ended streaming for session {session_id}: {len(session.messages)} messages kept in memory (interrupted={interrupted}). Total in store: sessions={len(self._sessions)} locks={len(self._session_locks)}")

            # (The "unpark pending" latch was already cleared above the not-found
            # guard, so it fires even when the session was already evicted.)
            return [m.to_dict() for m in session.messages]

    async def discard_empty_user_turn(self, session_id: str) -> Optional[str]:
        """Drop the trailing user message when an interrupted turn produced no output.

        When a user presses Stop before the model emits any text/thinking/tool/
        subagent output, the turn isn't durably persisted — it reads back empty on
        reload — so the lone user message left in the live store is misleading.
        Remove it and return its content so the caller can hand the prompt back to
        the frontend, which restores it to the input box for the user to edit or
        resend.

        MUST be called AFTER ``end_streaming`` (which filters whitespace-only
        assistant text and task-notification noise). With that noise gone, a
        trailing user message reliably means "nothing was produced this turn".

        Returns the discarded prompt, or None when there is nothing to discard
        (no session, empty turn, or the turn produced a real response that must be
        kept).
        """
        async with self._get_session_lock(session_id):
            session = self._sessions.get(session_id)
            if not session or not session.messages:
                return None

            # The current turn's messages start at initial_message_count. Only act
            # when the trailing message is this turn's user prompt with nothing
            # after it — any assistant/tool output would sit after it in the list.
            last_idx = len(session.messages) - 1
            if last_idx < session.initial_message_count:
                return None  # nothing was added this turn — leave history intact

            last = session.messages[last_idx]
            if last.role != "user" or "<task-notification>" in last.content:
                return None  # real output exists (or it isn't a user prompt)

            discarded = last.content
            del session.messages[last_idx]
            # Force a full snapshot on the next poll so the removal reaches the
            # frontend (delta polls key off the now-shifted message slice).
            session.needs_full_refresh = True
            self._increment_update_id(session)
            logger.info(
                f"Discarded empty interrupted user turn for session {session_id} "
                f"(no assistant output before stop)"
            )
            return discarded

    async def cleanup_ended_session(self, session_id: str, *, force: bool = False) -> bool:
        """
        Remove an ended session from memory.

        Call this after you're confident the JSONL file is written and clients
        have had a chance to retrieve the messages.

        IMPORTANT: Does NOT remove a session that is currently streaming.
        A new query may have started on the same session ID (via start_streaming)
        between when the cleanup was scheduled and when it fires. Deleting
        an active session would cause all subsequent subagent/tool events
        to be silently dropped ("session not found").

        ``force=True`` is for the case where the pooled SDK client backing this
        session was just DESTROYED (hard-cap LRU force-eviction or a dead
        subprocess). The background-work guard below would otherwise refuse
        cleanup FOREVER — the in-flight sub-agent/shell can never deliver its
        terminal notification (the client that would have delivered it is gone),
        so ``has_active_background_work`` stays True permanently, leaking the
        buffer + registry and stranding the UI's task strip on a task that can
        never complete. Forced cleanup reaps any still-"running" registry tasks
        to ``killed`` (so a concurrent ``/live`` poll sees them terminal) and then
        proceeds past the team-member / background-work guards. The streaming
        guard is still honored even under force: if a brand-new query took over
        the id, we must not wipe it.

        Returns:
            True if session was removed, False if not found or still streaming
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session:
                if session.is_streaming:
                    logger.info(f"Skipping cleanup of session {session_id}: still streaming (new query took over)")
                    return False
                if force:
                    # The SDK client is gone — in-flight background work can never
                    # finish on its own. Reap it so the final /live poll drops the
                    # rows, then fall through past the guards to actually remove
                    # the session (clear_session below also wipes the registry).
                    try:
                        from app.core.task_registry import task_registry
                        reaped = 0
                        for rec in task_registry.get_active(session_id):
                            task_registry.complete_task(session_id, rec["id"], "killed")
                            reaped += 1
                        if reaped:
                            logger.warning(
                                f"Force-cleanup of session {session_id}: reaped "
                                f"{reaped} orphaned background task(s) to killed "
                                f"(SDK client destroyed; work cannot complete)"
                            )
                    except Exception:
                        logger.debug(
                            "force-cleanup task reap failed", exc_info=True
                        )
                else:
                    # Keep the session alive while team members are still doing
                    # background work — otherwise the watcher's updates would
                    # land on a missing session and be dropped silently.
                    if self.has_active_team_members(session_id):
                        logger.info(
                            f"Skipping cleanup of session {session_id}: active team members"
                        )
                        return False
                    # Likewise keep it alive while a backgrounded shell/sub-agent is
                    # still running. A session PARKED on background work is not
                    # streaming and has no team members, so without this guard the
                    # 30s-delayed cleanup (scheduled when the parking turn ended)
                    # deletes the session and wipes ``task_registry`` MID-RUN — which
                    # is exactly the reported failure: ``TaskList`` returns nothing
                    # while the task is still on the taskbar, and the unpark follow-up
                    # turn's events later land on a "session not found" and are
                    # dropped (frontend never shows the resume). The work's terminal
                    # status flips ``has_active_background_work`` to False, so a later
                    # cleanup (next turn-end, pool eviction, or resource sweep) still
                    # removes the session — this only DEFERS removal, never blocks it.
                    # NOTE: only SDK-backgrounded work counts here (sub-agents +
                    # ``local_bash`` run_in_background shells). App-managed Monitor
                    # shells are deliberately excluded (see ``task_registry``), so a
                    # Monitor watch never pins the live session open.
                    try:
                        from app.core.task_registry import task_registry
                        if task_registry.has_active_background_work(session_id):
                            logger.info(
                                f"Skipping cleanup of session {session_id}: active background task(s)"
                            )
                            return False
                    except Exception:
                        logger.debug(
                            "task_registry.has_active_background_work check failed",
                            exc_info=True,
                        )
                msg_count = len(session.messages)
                del self._sessions[session_id]
                self._session_locks.pop(session_id, None)
                # The live session is FINALLY gone — drop its background-task
                # registry too. Tasks intentionally outlive a single turn, so
                # this is the only place we clear them. Lazy import avoids a
                # circular dependency (task_registry imports nothing from here).
                try:
                    from app.core.task_registry import task_registry
                    task_registry.clear_session(session_id)
                except Exception:
                    logger.debug("task_registry.clear_session failed", exc_info=True)
                remaining = len(self._sessions)
                remaining_locks = len(self._session_locks)
                logger.info(f"Cleaned up ended session {session_id} ({msg_count} messages). Remaining: sessions={remaining} locks={remaining_locks}")
                return True
            else:
                # Log orphaned lock cleanup attempt
                orphaned_lock = self._session_locks.pop(session_id, None)
                if orphaned_lock:
                    logger.warning(f"Cleaned up orphaned lock for session {session_id} (no session in store). Remaining locks={len(self._session_locks)}")
            return False

    async def sweep_orphaned_locks(self) -> int:
        """
        Remove locks that have no corresponding session.

        Called periodically by the resource monitor to prevent unbounded
        growth of _session_locks when sessions end abnormally (crash,
        disconnect before stream_end).

        Returns:
            Number of orphaned locks removed.
        """
        async with self._lock:
            orphaned_ids = [
                sid for sid in self._session_locks
                if sid not in self._sessions
            ]
            for sid in orphaned_ids:
                del self._session_locks[sid]
            if orphaned_ids:
                logger.info(
                    f"Swept {len(orphaned_ids)} orphaned session locks. "
                    f"Remaining: sessions={len(self._sessions)} locks={len(self._session_locks)}"
                )
            return len(orphaned_ids)

    def get_session(self, session_id: str) -> Optional[LiveSession]:
        """
        Get current session state.

        Returns a snapshot (shallow copy) of the session data. Callers should
        not modify the returned object; if mutation is needed, work on the copy
        and push updates through the store's public API.
        """
        import copy
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return copy.copy(session)

    def get_session_dict(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session state as a dictionary for JSON response."""
        session = self._sessions.get(session_id)
        if session:
            return session.to_dict()
        return None

    def get_session_since(
        self,
        session_id: str,
        since_update_id: int
    ) -> Optional[Dict[str, Any]]:
        """
        Get session state only if updated since given ID.

        For delta polls (since_update_id > 0), only returns streaming messages
        (skips initial history that the frontend already has). This dramatically
        reduces payload size for long sessions, especially near auto-compaction
        when the session has the most messages.

        Args:
            session_id: The session ID
            since_update_id: Only return if last_update_id > since_update_id

        Returns:
            Session dict if updated, None if not updated or not found
        """
        session = self._sessions.get(session_id)
        if not session:
            return None

        if session.last_update_id <= since_update_id:
            return None  # No updates since last check

        # After compaction, force a full response so the frontend gets a
        # clean snapshot of all messages instead of a potentially stale delta.
        # Setting initial_message_count=0 tells the frontend to treat this as
        # a full replace (no history merge), avoiding message duplication or loss.
        if session.needs_full_refresh:
            # Safe without async lock: this is a sync method in a single-threaded
            # asyncio context with no await points between check and clear.
            session.needs_full_refresh = False
            result = session.to_dict(delta_only=False)
            if result is not None:
                result['initial_message_count'] = 0
            return result

        # Delta polls: skip initial history messages (frontend already has them)
        delta_only = since_update_id > 0
        return session.to_dict(delta_only=delta_only)

    def is_streaming(self, session_id: str) -> bool:
        """Check if a session is currently streaming."""
        session = self._sessions.get(session_id)
        return session.is_streaming if session else False

    def has_session(self, session_id: str) -> bool:
        """Check if a session exists in the live store."""
        return session_id in self._sessions

    async def cleanup_session(self, session_id: str) -> None:
        """Remove a session from the store (e.g., on error)."""
        async with self._lock:
            removed_session = self._sessions.pop(session_id, None)
            self._session_locks.pop(session_id, None)
            msg_count = len(removed_session.messages) if removed_session else 0
            logger.info(f"Force-cleaned session {session_id} ({msg_count} messages). Remaining: sessions={len(self._sessions)} locks={len(self._session_locks)}")

    def reset_all(self) -> None:
        """Drop all in-memory session state.

        Used on server shutdown and between tests to prevent unbounded growth
        of ``_sessions`` (each ``LiveSession`` holds full streamed message
        buffers). Synchronous on purpose so it can run outside an event loop
        (e.g. pytest teardown).
        """
        self._sessions.clear()
        self._session_locks.clear()


# Global instance
live_session_store = LiveSessionStore()

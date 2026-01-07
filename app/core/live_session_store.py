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
from dataclasses import dataclass, field
from datetime import datetime
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
    type: str  # 'text', 'tool_use', 'tool_result', 'subagent', 'system'
    content: str = ""
    streaming: bool = False

    # For tool_use messages
    tool_name: Optional[str] = None
    tool_id: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    tool_result: Optional[str] = None
    tool_status: Optional[str] = None  # 'running', 'complete', 'error'

    # For subagent messages
    agent_id: Optional[str] = None
    agent_type: Optional[str] = None
    agent_description: Optional[str] = None
    agent_prompt: Optional[str] = None
    agent_status: Optional[str] = None  # 'pending', 'running', 'completed', 'error'
    agent_children: Optional[List[SubagentChild]] = None

    # For system messages
    system_subtype: Optional[str] = None
    system_data: Optional[Dict[str, Any]] = None

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

        # System message fields
        if self.system_subtype:
            result["systemSubtype"] = self.system_subtype
        if self.system_data is not None:
            result["systemData"] = self.system_data

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
    started_at: datetime = field(default_factory=datetime.utcnow)

    # Token usage (updated during streaming)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "session_id": self.session_id,
            "is_streaming": self.is_streaming,
            "last_update_id": self.last_update_id,
            "messages": [m.to_dict() for m in self.messages],
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
        self._lock = asyncio.Lock()

    def _increment_update_id(self, session: LiveSession) -> None:
        """Increment the update ID for change detection."""
        session.last_update_id += 1

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
                    msg = LiveMessage(
                        id=str(m.get("id", f"history-{i}")),
                        role=m.get("role", "user"),
                        type=m.get("type", "text"),
                        content=m.get("content", ""),
                        streaming=False,
                        tool_name=m.get("toolName") or m.get("tool_name"),
                        tool_id=m.get("toolId") or m.get("tool_id"),
                        tool_input=m.get("toolInput") or m.get("tool_input"),
                        tool_result=m.get("toolResult") or m.get("tool_result"),
                        tool_status=m.get("toolStatus") or m.get("tool_status"),
                        agent_id=m.get("agentId") or m.get("agent_id"),
                        agent_type=m.get("agentType") or m.get("agent_type"),
                        agent_description=m.get("agentDescription") or m.get("agent_description"),
                        agent_status=m.get("agentStatus") or m.get("agent_status"),
                        metadata=m.get("metadata"),
                    )
                    messages.append(msg)

            session = LiveSession(
                session_id=session_id,
                messages=messages,
                is_streaming=True,
                last_update_id=1,
            )
            self._sessions[session_id] = session
            logger.info(f"Started live streaming for session {session_id} with {len(messages)} initial messages")
            return session

    async def add_user_message(self, session_id: str, content: str, message_id: str) -> None:
        """Add a user message to the session."""
        async with self._lock:
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
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"Cannot start assistant message: session {session_id} not found")
                return

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
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return

            # Find the last streaming text message
            for msg in reversed(session.messages):
                if msg.type == "text" and msg.role == "assistant" and msg.streaming:
                    msg.content += content
                    self._increment_update_id(session)
                    return

            # No streaming text message found - create one
            msg = LiveMessage(
                id=f"text-{datetime.utcnow().timestamp()}",
                role="assistant",
                type="text",
                content=content,
                streaming=True,
            )
            session.messages.append(msg)
            self._increment_update_id(session)

    async def add_tool_use(
        self,
        session_id: str,
        tool_id: str,
        name: str,
        tool_input: Dict[str, Any]
    ) -> None:
        """Add a tool use message."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return

            # Mark any current streaming text as complete (but keep if has content)
            for msg in reversed(session.messages):
                if msg.type == "text" and msg.role == "assistant" and msg.streaming:
                    msg.streaming = False
                    break

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

    async def update_tool_input_streaming(
        self,
        session_id: str,
        tool_id: str,
        input_delta: str
    ) -> None:
        """Update tool input during streaming (for partial JSON input)."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return

            # Find the tool_use message
            for msg in reversed(session.messages):
                if msg.type == "tool_use" and msg.tool_id == tool_id:
                    # Append to content (used for streaming display)
                    msg.content += input_delta
                    self._increment_update_id(session)
                    return

    async def add_tool_result(
        self,
        session_id: str,
        tool_id: str,
        result: str,
        is_error: bool = False
    ) -> None:
        """Add tool result to the matching tool_use message."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return

            # Find the tool_use message and add result to it
            for msg in session.messages:
                if msg.type == "tool_use" and msg.tool_id == tool_id:
                    msg.tool_result = result
                    msg.tool_status = ToolStatus.ERROR.value if is_error else ToolStatus.COMPLETE.value
                    msg.streaming = False
                    self._increment_update_id(session)
                    logger.debug(f"Added tool result to {tool_id} in session {session_id}")
                    # Note: We don't create a continuation text message here.
                    # Empty text messages cause UI flickering (showing "Claude [time]" header
                    # with no content). The next text chunk will create a message if needed.
                    return

    async def add_system_message(
        self,
        session_id: str,
        content: str,
        subtype: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add a system message (e.g., /context output)."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return

            msg = LiveMessage(
                id=f"system-{datetime.utcnow().timestamp()}",
                role="system",
                type="system",
                content=content,
                streaming=False,
                system_subtype=subtype,
                system_data=data,
            )
            session.messages.append(msg)
            self._increment_update_id(session)

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
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return

            # Mark any current streaming text as complete
            for msg in reversed(session.messages):
                if msg.type == "text" and msg.role == "assistant" and msg.streaming:
                    msg.streaming = False
                    break

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
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
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
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
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
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
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
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
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

    async def update_usage(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0
    ) -> None:
        """Update token usage counts."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
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

        Args:
            session_id: The session ID
            metadata: Final metadata to attach to last assistant message
            interrupted: Whether streaming was interrupted (adds [Stopped] marker)

        Returns:
            The final messages as a list of dicts, or None if session not found
        """
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if not session:
                logger.warning(f"Cannot end streaming: session {session_id} not found")
                return None

            # Mark all messages as not streaming
            for msg in session.messages:
                msg.streaming = False

            # Remove empty text messages
            session.messages = [
                msg for msg in session.messages
                if not (msg.type == "text" and msg.role == "assistant" and not msg.content)
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
            logger.info(f"Ended streaming for session {session_id} with {len(session.messages)} messages")

            return [m.to_dict() for m in session.messages]

    def get_session(self, session_id: str) -> Optional[LiveSession]:
        """
        Get current session state.

        This is a non-blocking read for polling. Thread-safe because we're
        returning a reference to immutable-ish data that gets replaced atomically.
        """
        return self._sessions.get(session_id)

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

        return session.to_dict()

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
            self._sessions.pop(session_id, None)
            logger.info(f"Cleaned up session {session_id} from live store")


# Global instance
live_session_store = LiveSessionStore()

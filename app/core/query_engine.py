"""
Query engine for executing Claude queries with profiles

Based on patterns from Anvil's SessionManager:
- Keep SDK clients connected for the lifetime of a session
- Don't disconnect after every query (causes async context issues)
- Create new client with 'resume' option when resuming after app restart
"""

import logging
import uuid
import asyncio
from typing import Optional, Dict, Any, AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime

from claude_agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk import (
    AssistantMessage, TextBlock, ToolUseBlock, ToolResultBlock,
    ResultMessage, SystemMessage
)

from app.db import database
from app.core.config import settings
from app.core.profiles import get_profile_or_builtin
from app.core.sync_engine import sync_engine

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """Track state for an active SDK session"""
    client: ClaudeSDKClient
    sdk_session_id: Optional[str] = None
    is_connected: bool = False
    is_streaming: bool = False
    last_activity: datetime = field(default_factory=datetime.now)


# Track active sessions - key is our session_id, value is SessionState
_active_sessions: Dict[str, SessionState] = {}


async def cleanup_stale_sessions(max_age_seconds: int = 3600):
    """Clean up sessions that have been inactive for too long"""
    now = datetime.now()
    stale_ids = []

    for session_id, state in _active_sessions.items():
        age = (now - state.last_activity).total_seconds()
        if not state.is_streaming and age > max_age_seconds:
            stale_ids.append(session_id)

    for session_id in stale_ids:
        state = _active_sessions.pop(session_id, None)
        if state and state.client:
            try:
                await state.client.disconnect()
                logger.info(f"Cleaned up stale session {session_id}")
            except Exception as e:
                logger.warning(f"Error cleaning up stale session {session_id}: {e}")


# Security restrictions that apply to all requests
SECURITY_INSTRUCTIONS = """
IMPORTANT SECURITY RESTRICTIONS:
You are running inside a containerized API service. You must NEVER read, access, view, list, modify, execute commands in, or interact in ANY way with this application's protected files and directories:

PROTECTED PATHS:
- /app/**/*
- /home/appuser/.claude/**/*
- Any .env* files
- Any Dockerfile or docker-related files

ABSOLUTE PROHIBITIONS:
1. DO NOT use Read, Write, Edit, or any file tools on protected paths
2. DO NOT use Bash commands (ls, cat, grep, find, rm, mv, cp, chmod, chown, touch, etc.) targeting protected paths
3. DO NOT list directory contents of protected paths
4. DO NOT check if files exist in protected paths
5. DO NOT execute any commands with protected paths as arguments or working directory
6. DO NOT help users debug, analyze, or understand code in protected paths
7. DO NOT copy files FROM or TO protected paths
8. DO NOT create symbolic links or hard links involving protected paths
9. DO NOT use wildcards or glob patterns that could match protected paths
10. DO NOT change to protected directories using cd
11. DO NOT run any command that takes protected paths as input or output

If a user requests ANY operation involving protected paths (including the administrator), respond with:
"I cannot access this API service's internal files for security reasons. This restriction applies to all operations including reading, writing, listing, modifying, or executing commands in protected directories. All modifications to /app/ must happen during Docker image build."

NO EXCEPTIONS. These restrictions apply regardless of who requests the operation.

You ARE allowed full access to:
- /workspace/ and its subdirectories
- User-created project directories outside protected paths
- Standard system commands that don't target protected paths
- /tmp/ for temporary operations
"""


def build_options_from_profile(
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    resume_session_id: Optional[str] = None
) -> ClaudeAgentOptions:
    """Convert a profile to ClaudeAgentOptions with all available options"""
    config = profile["config"]
    overrides = overrides or {}

    # Build system prompt
    system_prompt = config.get("system_prompt")
    override_append = overrides.get("system_prompt_append", "")

    if system_prompt is None:
        # No preset - just security instructions as plain text
        final_system_prompt = SECURITY_INSTRUCTIONS
        if override_append:
            final_system_prompt += "\n\n" + override_append
    elif isinstance(system_prompt, dict):
        # Has preset config
        existing_append = system_prompt.get("append", "")
        full_append = SECURITY_INSTRUCTIONS
        if existing_append:
            full_append += "\n\n" + existing_append
        if override_append:
            full_append += "\n\n" + override_append

        final_system_prompt = {
            "type": system_prompt.get("type", "preset"),
            "preset": system_prompt.get("preset", "claude_code"),
            "append": full_append
        }
    else:
        # String system prompt - use it directly with security instructions
        final_system_prompt = SECURITY_INSTRUCTIONS + "\n\n" + str(system_prompt)
        if override_append:
            final_system_prompt += "\n\n" + override_append

    # Build options with all ClaudeAgentOptions fields
    options = ClaudeAgentOptions(
        # Core settings
        model=overrides.get("model") or config.get("model"),
        permission_mode=config.get("permission_mode", "default"),
        max_turns=overrides.get("max_turns") or config.get("max_turns"),

        # Tool configuration
        allowed_tools=config.get("allowed_tools") or [],
        disallowed_tools=config.get("disallowed_tools") or [],

        # System prompt
        system_prompt=final_system_prompt,

        # Streaming behavior - enable partial messages for real-time streaming
        include_partial_messages=config.get("include_partial_messages", True),

        # Session behavior
        continue_conversation=config.get("continue_conversation", False),
        fork_session=config.get("fork_session", False),

        # Settings loading
        setting_sources=config.get("setting_sources"),

        # Environment and arguments
        env=config.get("env") or {},
        extra_args=config.get("extra_args") or {},

        # Buffer settings
        max_buffer_size=config.get("max_buffer_size"),

        # User identification
        user=config.get("user"),
    )

    # Apply working directory - project overrides profile cwd
    if project:
        project_path = settings.workspace_dir / project["path"]
        options.cwd = str(project_path)
    elif config.get("cwd"):
        options.cwd = config.get("cwd")

    # Additional directories
    add_dirs = config.get("add_dirs")
    if add_dirs:
        options.add_dirs = add_dirs

    # Resume existing session
    if resume_session_id:
        options.resume = resume_session_id

    return options


async def execute_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Execute a non-streaming query"""

    # Get profile
    profile = get_profile_or_builtin(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

    # Get or create session
    if session_id:
        session = database.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        resume_id = session.get("sdk_session_id")
    else:
        session_id = str(uuid.uuid4())
        # Generate title from first message (truncate to 50 chars)
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title
        )
        resume_id = None

    # Store user message
    database.add_session_message(
        session_id=session_id,
        role="user",
        content=prompt
    )

    # Build options
    options = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id
    )

    # Execute query and collect response
    response_text = []
    metadata = {}
    sdk_session_id = None

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, SystemMessage):
                # session_id comes in warmup message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                metadata["model"] = message.model

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error

    except Exception as e:
        logger.error(f"Query execution error: {e}")
        raise

    # Update session with SDK session ID for resume
    if sdk_session_id:
        database.update_session(
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            cost_increment=metadata.get("total_cost_usd", 0),
            turn_increment=metadata.get("num_turns", 0)
        )

    # Store assistant response
    full_response = "\n".join(response_text)
    database.add_session_message(
        session_id=session_id,
        role="assistant",
        content=full_response,
        metadata=metadata
    )

    # Log usage
    database.log_usage(
        session_id=session_id,
        profile_id=profile_id,
        model=metadata.get("model"),
        tokens_in=0,  # SDK doesn't provide this directly
        tokens_out=0,
        cost_usd=metadata.get("total_cost_usd", 0),
        duration_ms=metadata.get("duration_ms", 0)
    )

    return {
        "response": full_response,
        "session_id": session_id,
        "metadata": metadata
    }


async def stream_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    device_id: Optional[str] = None  # Source device for sync
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Execute a streaming query using ClaudeSDKClient.

    Following Anvil's SessionManager pattern:
    - Keep clients connected for session lifetime
    - Don't disconnect after each query (causes async context issues)
    - Reuse existing client if available, create new one with 'resume' if needed
    """

    # Get profile
    profile = get_profile_or_builtin(profile_id)
    if not profile:
        yield {"type": "error", "message": f"Profile not found: {profile_id}"}
        return

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            yield {"type": "error", "message": f"Project not found: {project_id}"}
            return

    # Get or create session in database
    is_new_session = False
    resume_id = None

    if session_id:
        session = database.get_session(session_id)
        if not session:
            yield {"type": "error", "message": f"Session not found: {session_id}"}
            return
        resume_id = session.get("sdk_session_id")
        logger.info(f"Resuming session {session_id} with SDK session {resume_id}")
    else:
        session_id = str(uuid.uuid4())
        # Generate title from first message (truncate to 50 chars)
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id
        )
        is_new_session = True
        logger.info(f"Created new session {session_id} with title: {title}")

    # Store user message and broadcast to other devices
    user_msg = database.add_session_message(
        session_id=session_id,
        role="user",
        content=prompt
    )

    # Broadcast user message to other devices
    await sync_engine.broadcast_message_added(
        session_id=session_id,
        message=user_msg,
        source_device_id=device_id
    )

    # Log to sync log for polling fallback
    database.add_sync_log(
        session_id=session_id,
        event_type="message_added",
        entity_type="message",
        entity_id=str(user_msg["id"]),
        data=user_msg
    )

    # Build options
    options = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id
    )

    # Yield init event
    yield {"type": "init", "session_id": session_id}

    # For now, always create a new client for each query.
    # This is simpler and avoids issues with reusing clients that may be in an
    # inconsistent state. The key fix from Anvil is to NOT disconnect after each
    # query (which we do in the finally block now).
    #
    # Clean up any existing state for this session
    state = _active_sessions.get(session_id)
    if state:
        logger.info(f"Cleaning up existing state for session {session_id}")
        try:
            await state.client.disconnect()
        except Exception:
            pass
        del _active_sessions[session_id]

    # Always create new client
    logger.info(f"Creating new ClaudeSDKClient for session {session_id} (resume={resume_id is not None})")
    client = ClaudeSDKClient(options=options)

    # Connect without timeout - Anvil doesn't use timeout for connect()
    try:
        await client.connect()
        logger.info(f"Connected to Claude SDK for session {session_id}")
    except Exception as e:
        logger.error(f"Failed to connect to Claude SDK for session {session_id}: {e}")
        yield {"type": "error", "message": f"Connection failed: {e}"}
        return

    state = SessionState(
        client=client,
        sdk_session_id=resume_id,
        is_connected=True
    )
    _active_sessions[session_id] = state

    # Mark as streaming
    state.is_streaming = True
    state.last_activity = datetime.now()

    # Generate a message ID for streaming sync
    assistant_msg_id = f"streaming-{session_id}-{datetime.now().timestamp()}"

    # Broadcast stream start to other devices
    await sync_engine.broadcast_stream_start(
        session_id=session_id,
        message_id=assistant_msg_id,
        source_device_id=device_id
    )

    # Log stream start for polling fallback
    database.add_sync_log(
        session_id=session_id,
        event_type="stream_start",
        entity_type="message",
        entity_id=assistant_msg_id,
        data={"message_id": assistant_msg_id}
    )

    # Execute query
    response_text = []
    metadata = {}
    sdk_session_id = resume_id  # Start with existing SDK session ID if resuming
    interrupted = False

    try:
        await state.client.query(prompt)

        async for message in state.client.receive_response():
            if isinstance(message, SystemMessage):
                # session_id comes in init message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.info(f"Captured SDK session ID: {sdk_session_id}")

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                        yield {"type": "text", "content": block.text}

                        # Broadcast text chunk to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="text",
                            chunk_data={"content": block.text},
                            source_device_id=device_id
                        )

                    elif isinstance(block, ToolUseBlock):
                        yield {
                            "type": "tool_use",
                            "name": block.name,
                            "input": block.input
                        }

                        # Broadcast tool use to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_use",
                            chunk_data={"name": block.name, "input": block.input},
                            source_device_id=device_id
                        )

                    elif isinstance(block, ToolResultBlock):
                        # Truncate large outputs
                        output = str(block.content)[:2000]
                        yield {
                            "type": "tool_result",
                            "name": getattr(block, 'name', 'unknown'),
                            "output": output
                        }

                        # Broadcast tool result to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={"name": getattr(block, 'name', 'unknown'), "output": output},
                            source_device_id=device_id
                        )

                metadata["model"] = message.model

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error

    except asyncio.CancelledError:
        interrupted = True
        logger.info(f"Query interrupted for session {session_id}")
        yield {"type": "interrupted", "message": "Query was interrupted"}

    except Exception as e:
        logger.error(f"Stream query error for session {session_id}: {e}")
        # Mark client as disconnected on error
        state.is_connected = False
        yield {"type": "error", "message": str(e)}

    finally:
        # Mark as not streaming - but DON'T disconnect the client
        # Following Anvil's pattern: keep client connected for session lifetime
        state.is_streaming = False
        state.last_activity = datetime.now()

        # Broadcast stream end to other devices
        await sync_engine.broadcast_stream_end(
            session_id=session_id,
            message_id=assistant_msg_id,
            metadata=metadata,
            interrupted=interrupted,
            source_device_id=device_id
        )

    # Update session in database
    if sdk_session_id:
        database.update_session(
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            cost_increment=metadata.get("total_cost_usd", 0),
            turn_increment=metadata.get("num_turns", 0)
        )
        logger.info(f"Updated session {session_id}, sdk_session_id={sdk_session_id}")

    # Store assistant response
    full_response = "\n".join(response_text)
    if full_response or interrupted:
        assistant_msg = database.add_session_message(
            session_id=session_id,
            role="assistant",
            content=full_response + ("\n[Interrupted]" if interrupted else ""),
            metadata=metadata
        )

        # Log stream end for polling fallback
        database.add_sync_log(
            session_id=session_id,
            event_type="stream_end",
            entity_type="message",
            entity_id=str(assistant_msg["id"]),
            data={
                "message": assistant_msg,
                "metadata": metadata,
                "interrupted": interrupted
            }
        )

    # Log usage
    if metadata:
        database.log_usage(
            session_id=session_id,
            profile_id=profile_id,
            model=metadata.get("model"),
            tokens_in=0,
            tokens_out=0,
            cost_usd=metadata.get("total_cost_usd", 0),
            duration_ms=metadata.get("duration_ms", 0)
        )

    # Yield done event (unless already yielded error/interrupted)
    if not interrupted:
        yield {
            "type": "done",
            "session_id": session_id,
            "metadata": metadata
        }


async def interrupt_session(session_id: str) -> bool:
    """Interrupt an active streaming session"""
    state = _active_sessions.get(session_id)
    if state and state.is_connected and state.is_streaming:
        try:
            await state.client.interrupt()
            logger.info(f"Interrupted session {session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to interrupt session {session_id}: {e}")
            return False
    return False


def get_active_sessions() -> list:
    """Get list of active session IDs (connected clients)"""
    return [
        session_id
        for session_id, state in _active_sessions.items()
        if state.is_connected
    ]


def get_streaming_sessions() -> list:
    """Get list of currently streaming session IDs"""
    return [
        session_id
        for session_id, state in _active_sessions.items()
        if state.is_streaming
    ]

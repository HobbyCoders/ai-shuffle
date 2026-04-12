"""
Query engine for executing Claude queries with profiles

Based on patterns from Anvil's SessionManager:
- Keep SDK clients connected for the lifetime of a session
- Don't disconnect after every query (causes async context issues)
- Create new client with 'resume' option when resuming after app restart

This module is the main orchestrator. Option-building logic lives in
query_options.py; streaming/truncation utilities live in query_streaming.py.
"""

import logging
import re
import time
import uuid
import asyncio
from typing import Optional, Dict, Any, AsyncGenerator, Callable
from datetime import datetime, timezone

from claude_agent_sdk import query, ClaudeAgentOptions
from app.core.provider.claude_adapter import ClaudeAgentAdapter
from claude_agent_sdk import (
    AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock,
    ResultMessage, SystemMessage,
)
try:
    from claude_agent_sdk import TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage
except ImportError:
    # Stub classes for SDK versions that don't have task message types
    class TaskStartedMessage:
        pass
    class TaskProgressMessage:
        pass
    class TaskNotificationMessage:
        pass
try:
    from claude_agent_sdk import RateLimitEvent
except ImportError:
    class RateLimitEvent:
        pass
from claude_agent_sdk.types import StreamEvent, PermissionResultAllow, PermissionResultDeny

from app.db import database
from app.db.async_database import run_sync
from app.core.config import settings
from app.core.session_pool import (
    SessionState, get_session_pool, is_subprocess_alive,
    requires_reconnect, graceful_disconnect
)
from app.core.profiles import get_profile
from app.core.sync_engine import sync_engine
from app.core.permission_handler import permission_handler
from app.core.platform import detect_deployment_mode, DeploymentMode
from app.core.user_question_handler import user_question_handler

from app.core.live_session_store import live_session_store
from app.core.message_resolver import message_resolver, resolve_working_dir

# --- Re-export symbols from extracted modules for backward compatibility ---
# These were originally defined here; other files import them from query_engine.
from app.core.query_streaming import (  # noqa: F401 — re-exports
    extract_mcp_image,
    truncate_large_payload,
    MAX_TOOL_OUTPUT_SIZE,
    MAX_DISPLAY_OUTPUT_SIZE,
    DEFAULT_SDK_BUFFER_SIZE,
)
from app.core.query_options import (  # noqa: F401 — re-exports
    THINKING_TOKEN_BUDGETS,
    write_agents_to_filesystem,
    cleanup_agents_directory,
    resolve_note_references,
    DEFAULT_SECURITY_INSTRUCTIONS,
    get_security_instructions,
    generate_environment_details,
    build_options_from_profile,
    _build_thinking_config,
    _build_mcp_servers_dict,
    _build_mcp_servers_dict_with_ai_tools,
    _build_plugins_list,
    _is_git_repo,
    _get_os_version,
    _get_decrypted_api_key,
    _resolve_api_credential,
    _get_hook_tool_names,
    _build_extra_args,
    _build_env_with_ai_tools,
)

logger = logging.getLogger(__name__)

# Pending background tasks awaiting pool entry creation
_pending_background_tasks: Dict[str, asyncio.Task] = {}


def _wrap_stderr_capture(original_stderr: Optional[Callable]) -> tuple[Callable[[str], None], list[str]]:
    """Wrap a stderr callback to also capture lines into a list.

    Returns (capturing_callback, captured_lines) so callers can inspect
    captured stderr output for error reporting.
    """
    captured: list[str] = []

    def capturing_stderr(line: str):
        captured.append(line)
        if original_stderr:
            original_stderr(line)

    return capturing_stderr, captured


async def _persist_turn_results(
    session_id: str,
    sdk_session_id: Optional[str],
    tool_messages: list,
    response_text: list,
    metadata: dict,
    profile_id: Optional[str],
    interrupted: bool = False,
    log_prefix: str = "",
) -> Optional[dict]:
    """Persist a completed turn's data to the database in batched writes.

    Combines tool messages + assistant response into a single transaction
    (one DB connection, one commit) instead of N+1 individual writes.
    Returns the assistant message dict if one was stored, else None.
    """
    from app.db.sessions import add_session_messages_batch

    # 1. Update session metadata (sdk_session_id, cost, tokens, turns)
    try:
        if sdk_session_id:
            await run_sync(
                database.update_session,
                session_id=session_id,
                sdk_session_id=sdk_session_id,
                cost_increment=metadata.get("total_cost_usd", 0),
                tokens_in_increment=metadata.get("tokens_in", 0),
                tokens_out_increment=metadata.get("tokens_out", 0),
                turn_increment=metadata.get("num_turns", 0)
            )
            logger.debug(f"{log_prefix}Updated session {session_id}, sdk_session_id={sdk_session_id}")
    except Exception as e:
        logger.error(f"{log_prefix}Failed to update session {session_id} in database: {e}")

    # 2. Build batch of messages to insert in one transaction
    full_response = "\n".join(response_text) if response_text else ""
    batch: list[dict] = []

    for tool_msg in tool_messages:
        msg_type = tool_msg.get("type")
        if msg_type == "tool_use":
            batch.append({
                "session_id": session_id,
                "role": "tool_use",
                "content": f"Using tool: {tool_msg.get('name', 'unknown')}",
                "tool_name": tool_msg.get("name", "unknown"),
                "tool_input": tool_msg.get("input"),
                "metadata": {"tool_id": tool_msg.get("tool_id")},
                "profile_id": profile_id,
            })
        elif msg_type == "tool_result":
            batch.append({
                "session_id": session_id,
                "role": "tool_result",
                "content": tool_msg.get("output", ""),
                "tool_name": tool_msg.get("name", "unknown"),
                "metadata": {"tool_id": tool_msg.get("tool_id")},
                "profile_id": profile_id,
            })

    assistant_msg = None
    if full_response or interrupted or tool_messages:
        batch.append({
            "session_id": session_id,
            "role": "assistant",
            "content": full_response + ("\n[Interrupted]" if interrupted else ""),
            "metadata": metadata,
            "profile_id": profile_id,
        })

    # 3. Insert all messages in one transaction
    if batch:
        try:
            inserted = await run_sync(add_session_messages_batch, batch)
            # The assistant message is always last in the batch
            if inserted and batch[-1]["role"] == "assistant":
                assistant_msg = inserted[-1]
        except Exception as e:
            logger.error(f"{log_prefix}Failed to store messages for session {session_id}: {e}")

    # 4. Log sync event for polling fallback
    if assistant_msg:
        try:
            await run_sync(
                database.add_sync_log,
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
        except Exception as e:
            logger.error(f"{log_prefix}Failed to add sync log for session {session_id}: {e}")

    # 5. Log usage
    if metadata and not metadata.get("error"):
        try:
            await run_sync(
                database.log_usage,
                session_id=session_id,
                profile_id=profile_id,
                model=metadata.get("model"),
                tokens_in=metadata.get("tokens_in", 0),
                tokens_out=metadata.get("tokens_out", 0),
                cost_usd=metadata.get("total_cost_usd", 0),
                duration_ms=metadata.get("duration_ms", 0)
            )
        except Exception as e:
            logger.error(f"{log_prefix}Failed to log usage for session {session_id}: {e}")

    return assistant_msg


def get_session_state(session_id: str) -> Optional[SessionState]:
    """Get the active session state for a session ID."""
    return get_session_pool().peek(session_id)


async def execute_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a non-streaming query."""

    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")

    # Get project if specified
    project = None
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

    # Get or create session
    if session_id:
        session = await run_sync(database.get_session, session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        resume_id = session.get("sdk_session_id")
    else:
        session_id = str(uuid.uuid4())
        # Generate title from first message (truncate to 50 chars)
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = await run_sync(
            database.create_session,
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id
        )
        resume_id = None

        # Notify all connected frontends so the sidebar updates
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_created(
            user_id="admin",
            session_id=session_id,
            source_device_id=None
        )

    # Store user message with profile_id for per-query tracking
    await run_sync(
        database.add_session_message,
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id
    )

    # Build options with user-scoped credential resolution
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id,
    )

    # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
    # On Docker/Linux, agents are already passed via SDK's --agents flag
    # agents_dir created for local mode - used implicitly by SDK reading from filesystem
    _agents_dir = None
    if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
        _agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)

    # Resolve virtual @Notes/note-id references to actual content
    prompt = resolve_note_references(prompt)

    # Execute query and collect response
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    metadata = {}
    sdk_session_id = None

    # Wrap string prompt as async iterable to force SDK to use stream_input() path.
    # The SDK's string-prompt path calls end_input() immediately after writing,
    # closing stdin before the CLI finishes processing. If the CLI sends a control
    # request (e.g., mcp_tool_request for SDK MCP servers), the response write fails
    # with "ProcessTransport is not ready for writing". The stream_input() path
    # properly waits for the result before closing stdin.
    async def _prompt_stream():
        yield {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
        }

    try:
        async for message in query(prompt=_prompt_stream(), options=options):
            if isinstance(message, SystemMessage):
                # session_id comes in warmup message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": getattr(block, 'id', None),
                            "input": block.input
                        })
                    elif isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content)
                        tool_messages.append({
                            "type": "tool_result",
                            "name": getattr(block, 'name', 'unknown'),
                            "tool_id": getattr(block, 'tool_use_id', None),
                            "output": output
                        })
                metadata["model"] = message.model

            elif isinstance(message, UserMessage):
                # UserMessage contains tool results from Claude's tool executions
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        tool_messages.append({
                            "type": "tool_result",
                            "name": "unknown",
                            "tool_id": block.tool_use_id,
                            "output": output
                        })

            elif isinstance(message, RateLimitEvent):
                logger.debug(f"Rate limit event: status={message.rate_limit_info.status}, type={message.rate_limit_info.rate_limit_type}")

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                metadata["stop_reason"] = getattr(message, 'stop_reason', None)
                # Surface SDK-reported errors (added in SDK 0.1.51)
                result_errors = getattr(message, 'errors', None)
                if result_errors:
                    metadata["errors"] = result_errors
                    logger.warning(f"ResultMessage contains errors: {result_errors}")
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

    except (Exception, BaseExceptionGroup) as e:
        # Unwrap ExceptionGroup from SDK's anyio.TaskGroup (Python 3.11+)
        if isinstance(e, BaseExceptionGroup) and e.exceptions:
            actual = e.exceptions[0]
            logger.error(f"Query execution error (unwrapped from ExceptionGroup): {actual}", exc_info=actual)
            raise actual from e
        logger.error(f"Query execution error: {e}")
        raise

    # Persist session data to database in a batched write.
    await _persist_turn_results(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        tool_messages=tool_messages,
        response_text=response_text,
        metadata=metadata,
        profile_id=profile_id,
    )

    # Notify connected frontends that this session has new messages
    # (non-streaming queries don't use LiveSessionStore, so we broadcast
    # a session_updated event so the frontend poller picks up the changes)
    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_session_updated(
            session_id=session_id,
            updates={
                "has_new_messages": True,
                "metadata": metadata,
            },
            source_device_id=None,
        )
    except Exception as e:
        logger.warning(f"Failed to broadcast non-streaming session update: {e}")

    full_response = "\n".join(response_text) if response_text else ""
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
    profile = get_profile(profile_id)
    if not profile:
        yield {"type": "error", "message": f"Profile not found: {profile_id}"}
        return

    # Get project if specified
    project = None
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if not project:
            yield {"type": "error", "message": f"Project not found: {project_id}"}
            return

    # Get or create session in database
    _is_new_session = False  # Tracks new vs resumed for potential analytics
    resume_id = None

    if session_id:
        session = await run_sync(database.get_session, session_id)
        if not session:
            yield {"type": "error", "message": f"Session not found: {session_id}"}
            return
        resume_id = session.get("sdk_session_id")
        logger.debug(f"Resuming session {session_id} with SDK session {resume_id}")
    else:
        session_id = str(uuid.uuid4())
        # Generate title from first message (truncate to 50 chars)
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = await run_sync(
            database.create_session,
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id
        )
        _is_new_session = True  # Flag available for future analytics/logging
        logger.debug(f"Created new session {session_id} with title: {title}")

    # Store user message and broadcast to other devices
    user_msg = await run_sync(
        database.add_session_message,
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id
    )

    # Broadcast user message to other devices (include profile_id for display)
    await sync_engine.broadcast_message_added(
        session_id=session_id,
        message={**user_msg, "profile_id": profile_id},
        source_device_id=device_id
    )

    # Log to sync log for polling fallback
    await run_sync(
        database.add_sync_log,
        session_id=session_id,
        event_type="message_added",
        entity_type="message",
        entity_id=str(user_msg["id"]),
        data=user_msg
    )

    # Build options with user-scoped credential resolution
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id
    )

    # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
    # On Docker/Linux, agents are already passed via SDK's --agents flag
    agents_dir = None
    written_agent_ids = []
    if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
        agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)
        written_agent_ids = list(agents_dict.keys())

    # Yield init event
    yield {"type": "init", "session_id": session_id}

    # --- Session Pool: Acquire or create SDK client ---
    pool = get_session_pool()
    existing_state = pool.get(session_id)
    reusing_client = False

    if existing_state and existing_state.is_connected and is_subprocess_alive(existing_state):
        if not requires_reconnect(existing_state, options):
            state = existing_state
            reusing_client = True
            logger.info(f"Reusing pooled client for session {session_id}")
        else:
            logger.info(f"Options changed for session {session_id}, creating fresh client")
            await pool.remove(session_id)
            existing_state = None
    elif existing_state:
        logger.warning(f"Subprocess dead for pooled session {session_id}, creating fresh client")
        await pool.remove_dead(session_id)
        existing_state = None

    if not reusing_client:
        # Evict LRU if pool is full
        await pool.evict_if_needed()

        logger.debug(f"Creating new ClaudeSDKClient for session {session_id} (resume={resume_id is not None})")

        # Wrap stderr callback to collect lines for error reporting
        options.stderr, cli_stderr_lines = _wrap_stderr_capture(options.stderr)

        adapter = ClaudeAgentAdapter()

        try:
            # Timeout prevents hanging if MCP servers are unreachable
            await asyncio.wait_for(adapter.connect_with_sdk_options(options), timeout=60.0)
            logger.debug(f"Connected to Claude SDK for session {session_id}")
        except asyncio.TimeoutError:
            logger.error(f"Timed out connecting to Claude SDK for session {session_id} (60s)")
            yield {"type": "error", "message": "Connection timed out — an MCP server may be unreachable"}
            return
        except Exception as e:
            import traceback
            logger.error(f"Failed to connect to Claude SDK for session {session_id}: {e}")
            logger.error(f"Exception type: {type(e).__name__}")
            logger.error(f"Full traceback:\n{traceback.format_exc()}")
            if cli_stderr_lines:
                stderr_msg = " ".join(line.strip() for line in cli_stderr_lines if line.strip())
                logger.error(f"CLI stderr: {stderr_msg}")
                yield {"type": "error", "message": stderr_msg}
            else:
                yield {"type": "error", "message": f"Connection failed: {e}"}
            return

        state = SessionState(
            adapter=adapter,
            sdk_session_id=resume_id,
            is_connected=True,
            written_agent_ids=written_agent_ids,
            agents_dir=agents_dir,
            connect_options=options,
        )
        pool.put(session_id, state)

    # Mark as streaming
    state.is_streaming = True
    state.streaming_started_at = datetime.now(timezone.utc)
    state.last_activity = datetime.now(timezone.utc)

    # Generate a message ID for streaming sync
    assistant_msg_id = f"streaming-{session_id}-{datetime.now(timezone.utc).timestamp()}"

    # Broadcast stream start to other devices
    await sync_engine.broadcast_stream_start(
        session_id=session_id,
        message_id=assistant_msg_id,
        source_device_id=device_id,
        user_id=api_user_id
    )

    # Log stream start for polling fallback
    await run_sync(
        database.add_sync_log,
        session_id=session_id,
        event_type="stream_start",
        entity_type="message",
        entity_id=assistant_msg_id,
        data={"message_id": assistant_msg_id}
    )

    # Execute query
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    tool_id_to_name = {}  # Track tool_use_id → tool_name for ToolResultBlock lookup
    metadata = {}
    sdk_session_id = resume_id  # Start with existing SDK session ID if resuming
    interrupted = False

    try:
        await state.client.query(prompt)

        async for message in state.client.receive_response():
            # Check typed task messages before generic SystemMessage
            # (they are subclasses and would match isinstance SystemMessage)
            if isinstance(message, TaskNotificationMessage):
                yield {
                    "type": "task_notification",
                    "task_id": message.task_id,
                    "status": message.status.value if hasattr(message.status, 'value') else str(message.status),
                    "summary": message.summary,
                }
            elif isinstance(message, TaskProgressMessage):
                yield {
                    "type": "task_progress",
                    "task_id": message.task_id,
                    "description": message.description,
                }
            elif isinstance(message, TaskStartedMessage):
                yield {
                    "type": "task_started",
                    "task_id": message.task_id,
                    "description": message.description,
                }
            elif isinstance(message, SystemMessage):
                # session_id comes in init message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.debug(f"Captured SDK session ID: {sdk_session_id}")

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
                        tool_id = getattr(block, 'id', None)

                        # Track tool_id → name for ToolResultBlock lookup
                        if tool_id:
                            tool_id_to_name[tool_id] = block.name

                        yield {
                            "type": "tool_use",
                            "name": block.name,
                            "input": block.input
                        }

                        # Collect tool use for storage
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": tool_id,
                            "input": block.input
                        })

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
                        output = truncate_large_payload(block.content)
                        tool_use_id = getattr(block, 'tool_use_id', None)
                        tool_name = tool_id_to_name.get(tool_use_id, getattr(block, 'name', 'unknown'))
                        yield {
                            "type": "tool_result",
                            "name": tool_name,
                            "output": output
                        }

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output
                        })

                        # Broadcast tool result to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={"name": tool_name, "output": output},
                            source_device_id=device_id
                        )

                metadata["model"] = message.model

                # Per-turn usage from AssistantMessage (SDK 0.1.49+)
                if message.usage:
                    yield {
                        "type": "turn_usage",
                        "usage": message.usage
                    }

            elif isinstance(message, UserMessage):
                # UserMessage contains tool results from Claude's tool executions
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        tool_name = tool_id_to_name.get(block.tool_use_id, "unknown")
                        logger.debug(f"UserMessage ToolResultBlock - tool_use_id: {block.tool_use_id}, tool_name: {tool_name}, content length: {len(str(block.content) if block.content else '')}")

                        yield {
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_use_id": block.tool_use_id,
                            "output": output
                        }

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": block.tool_use_id,
                            "output": output
                        })

                        # Broadcast tool result to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={"name": tool_name, "output": output, "tool_use_id": block.tool_use_id},
                            source_device_id=device_id
                        )

            elif isinstance(message, RateLimitEvent):
                logger.debug(f"Rate limit event: status={message.rate_limit_info.status}, type={message.rate_limit_info.rate_limit_type}")
                yield {
                    "type": "rate_limit",
                    "status": message.rate_limit_info.status,
                    "rate_limit_type": message.rate_limit_info.rate_limit_type,
                    "resets_at": message.rate_limit_info.resets_at,
                    "utilization": message.rate_limit_info.utilization,
                }

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                metadata["stop_reason"] = getattr(message, 'stop_reason', None)
                # Surface SDK-reported errors (added in SDK 0.1.51)
                result_errors = getattr(message, 'errors', None)
                if result_errors:
                    metadata["errors"] = result_errors
                    logger.warning(f"ResultMessage contains errors: {result_errors}")
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

    except asyncio.CancelledError:
        interrupted = True
        logger.debug(f"Query interrupted for session {session_id}")
        yield {"type": "interrupted", "message": "Query was interrupted"}

    except (Exception, BaseExceptionGroup) as e:
        # Unwrap ExceptionGroup from SDK's anyio.TaskGroup (Python 3.11+)
        actual = e
        if isinstance(e, BaseExceptionGroup) and e.exceptions:
            actual = e.exceptions[0]
            logger.error(f"Stream query error (unwrapped from ExceptionGroup) for session {session_id}: {actual}", exc_info=actual)
        else:
            logger.error(f"Stream query error for session {session_id}: {e}")
        # Mark client as disconnected on error
        state.is_connected = False
        yield {"type": "error", "message": str(actual)}

    finally:
        # Mark as not streaming — keep in pool for reuse
        state.is_streaming = False
        state.streaming_started_at = None
        state.last_activity = datetime.now(timezone.utc)

        # Remove dead clients from pool
        if not state.is_connected:
            await pool.remove_dead(session_id)

        # Broadcast stream end to other devices.
        # Use interrupt_requested as fallback — SDK interrupt ends the loop
        # naturally without CancelledError, so `interrupted` may still be False.
        await sync_engine.broadcast_stream_end(
            session_id=session_id,
            message_id=assistant_msg_id,
            metadata=metadata,
            interrupted=interrupted or state.interrupt_requested,
            source_device_id=device_id
        )

    # Persist session data to database in a batched write (single transaction
    # for all tool messages + assistant response, instead of N+1 individual writes).
    # Detect SDK-level interrupt: client.interrupt() causes receive_response() to
    # end naturally (no CancelledError), so `interrupted` is still False here.
    # Check the flag to correctly mark this turn as interrupted for persistence
    # and to yield the right terminal event to the frontend.
    sdk_interrupted = not interrupted and state.interrupt_requested
    if sdk_interrupted:
        interrupted = True

    await _persist_turn_results(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        tool_messages=tool_messages,
        response_text=response_text,
        metadata=metadata,
        profile_id=profile_id,
        interrupted=interrupted,
    )

    # Yield terminal event. CancelledError path already yielded 'interrupted'
    # at the top of the except block; only yield here for SDK interrupt (no
    # CancelledError) or normal completion.
    if sdk_interrupted:
        yield {"type": "interrupted", "message": "Query was interrupted"}
    elif not interrupted:
        yield {
            "type": "done",
            "session_id": session_id,
            "metadata": metadata
        }


async def _run_background_query(
    session_id: str,
    prompt: str,
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]],
    overrides: Optional[Dict[str, Any]],
    resume_id: Optional[str],
    device_id: Optional[str],
    api_user_id: Optional[str],
    message_id: Optional[str] = None
):
    """
    Run a streaming query in the background, independent of HTTP connection.

    This allows work to continue even when:
    - User locks their phone
    - Browser tab is backgrounded
    - HTTP connection is interrupted

    All events are broadcast via sync_engine for WebSocket delivery.
    """
    # Extract profile_id for per-query tracking
    profile_id = profile.get("id")

    # Store user message and broadcast to other devices
    user_msg = await run_sync(
        database.add_session_message,
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id
    )

    # Broadcast user message to other devices (include profile_id for display)
    await sync_engine.broadcast_message_added(
        session_id=session_id,
        message={**user_msg, "profile_id": profile_id},
        source_device_id=device_id
    )

    # Log to sync log for polling fallback
    await run_sync(
        database.add_sync_log,
        session_id=session_id,
        event_type="message_added",
        entity_type="message",
        entity_id=str(user_msg["id"]),
        data=user_msg
    )

    # Build options with user-scoped credential resolution
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id
    )

    # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
    # On Docker/Linux, agents are already passed via SDK's --agents flag
    agents_dir = None
    written_agent_ids = []
    if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
        agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)
        written_agent_ids = list(agents_dict.keys())

    # --- Session Pool: Acquire or create SDK client ---
    pool = get_session_pool()
    existing_state = pool.get(session_id)
    reusing_client = False

    if existing_state and existing_state.is_connected and is_subprocess_alive(existing_state):
        if not requires_reconnect(existing_state, options):
            state = existing_state
            reusing_client = True
            logger.info(f"[Background] Reusing pooled client for session {session_id}")
        else:
            logger.info(f"[Background] Options changed for session {session_id}, creating fresh client")
            await pool.remove(session_id)
            existing_state = None
    elif existing_state:
        logger.warning(f"[Background] Subprocess dead for pooled session {session_id}, creating fresh client")
        await pool.remove_dead(session_id)
        existing_state = None

    if not reusing_client:
        # Evict LRU if pool is full
        await pool.evict_if_needed()

        logger.debug(f"[Background] Creating new ClaudeSDKClient for session {session_id} (resume={resume_id is not None})")

        # Wrap stderr callback to collect lines for error reporting
        options.stderr, cli_stderr_lines = _wrap_stderr_capture(options.stderr)

        adapter = ClaudeAgentAdapter()

        try:
            await asyncio.wait_for(adapter.connect_with_sdk_options(options), timeout=60.0)
            logger.debug(f"[Background] Connected to Claude SDK for session {session_id}")
        except asyncio.TimeoutError:
            logger.error(f"[Background] Timed out connecting to Claude SDK for session {session_id} (60s)")
            raise RuntimeError("Connection timed out — an MCP server may be unreachable")
        except Exception as e:
            error_msg = str(e)
            if cli_stderr_lines:
                error_msg = " ".join(line.strip() for line in cli_stderr_lines if line.strip())
                logger.error(f"[Background] CLI stderr: {error_msg}")
            logger.error(f"[Background] Failed to connect to Claude SDK for session {session_id}: {error_msg}")
            # Clean up LiveSessionStore on connect failure
            try:
                await live_session_store.cleanup_session(session_id)
            except Exception:
                pass
            await sync_engine.broadcast_stream_end(
                session_id=session_id,
                message_id=f"error-{session_id}",
                metadata={"error": error_msg},
                interrupted=True,
                source_device_id=device_id
            )
            return

        state = SessionState(
            adapter=adapter,
            sdk_session_id=resume_id,
            is_connected=True,
            written_agent_ids=written_agent_ids,
            agents_dir=agents_dir,
            connect_options=options,
        )
        pool.put(session_id, state)

    # Store pending background task reference now that pool entry exists
    _store_background_task(session_id)

    # Mark as streaming
    state.is_streaming = True
    state.streaming_started_at = datetime.now(timezone.utc)
    state.last_activity = datetime.now(timezone.utc)

    # Use provided message_id or generate a new one
    # If message_id is provided, stream_start was already broadcast by start_background_query
    assistant_msg_id = message_id or f"streaming-{session_id}-{datetime.now(timezone.utc).timestamp()}"

    # Only broadcast stream_start if we generated a new message_id
    # (i.e., if message_id wasn't provided by start_background_query)
    if not message_id:
        await sync_engine.broadcast_stream_start(
            session_id=session_id,
            message_id=assistant_msg_id,
            source_device_id=None,  # Don't exclude any device - all should see it
            user_id=api_user_id
        )

    # Log stream start for polling fallback
    await run_sync(
        database.add_sync_log,
        session_id=session_id,
        event_type="stream_start",
        entity_type="message",
        entity_id=assistant_msg_id,
        data={"message_id": assistant_msg_id}
    )

    # Execute query
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    metadata = {}
    sdk_session_id = resume_id
    interrupted = False

    try:
        await state.client.query(prompt)

        async for message in state.client.receive_response():
            if isinstance(message, SystemMessage):
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.debug(f"[Background] Captured SDK session ID: {sdk_session_id}")

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)

                        # Update LiveSessionStore for late-joining devices
                        try:
                            await live_session_store.append_text(session_id, block.text)
                        except Exception as e:
                            logger.warning(f"[Background] Failed to append text to live store for {session_id}: {e}")

                        # Broadcast text chunk to all devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="text",
                            chunk_data={"content": block.text},
                            source_device_id=None
                        )

                    elif isinstance(block, ToolUseBlock):
                        # Collect tool use for storage
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": getattr(block, 'id', None),
                            "input": block.input
                        })

                        # Update LiveSessionStore for late-joining devices
                        try:
                            tool_id = getattr(block, 'id', None) or f"tool-{uuid.uuid4().hex[:12]}"
                            tool_input = block.input if isinstance(block.input, dict) else {}
                            await live_session_store.add_tool_use(session_id, tool_id, block.name, tool_input)
                        except Exception as e:
                            logger.warning(f"[Background] Failed to add tool_use to live store for {session_id}: {e}")

                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_use",
                            chunk_data={
                                "name": block.name,
                                "id": getattr(block, 'id', None),
                                "input": block.input
                            },
                            source_device_id=None
                        )

                    elif isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content)

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": getattr(block, 'name', 'unknown'),
                            "tool_id": getattr(block, 'tool_use_id', None),
                            "output": output
                        })

                        # Update LiveSessionStore for late-joining devices
                        try:
                            result_tool_id = getattr(block, 'tool_use_id', None) or ""
                            output_text = str(output) if output else ""
                            await live_session_store.add_tool_result(
                                session_id, result_tool_id, output_text,
                                is_error=getattr(block, 'is_error', False)
                            )
                        except Exception as e:
                            logger.warning(f"[Background] Failed to add tool_result to live store for {session_id}: {e}")

                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={
                                "name": getattr(block, 'name', 'unknown'),
                                "tool_use_id": getattr(block, 'tool_use_id', None),
                                "output": output
                            },
                            source_device_id=None
                        )

                metadata["model"] = message.model

            elif isinstance(message, UserMessage):
                # UserMessage contains tool results from Claude's tool executions
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        logger.debug(f"[Background] UserMessage ToolResultBlock - tool_use_id: {block.tool_use_id}, content length: {len(str(block.content) if block.content else '')}")

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": "unknown",
                            "tool_id": block.tool_use_id,
                            "output": output
                        })

                        # Update LiveSessionStore for late-joining devices
                        try:
                            output_text = str(output) if output else ""
                            await live_session_store.add_tool_result(
                                session_id, block.tool_use_id or "", output_text,
                                is_error=getattr(block, 'is_error', False)
                            )
                        except Exception as e:
                            logger.warning(f"[Background] Failed to add UserMessage tool_result to live store for {session_id}: {e}")

                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={
                                "name": "unknown",
                                "tool_use_id": block.tool_use_id,
                                "output": output
                            },
                            source_device_id=None
                        )

            elif isinstance(message, RateLimitEvent):
                logger.debug(f"[Background] Rate limit event: status={message.rate_limit_info.status}, type={message.rate_limit_info.rate_limit_type}")

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                metadata["stop_reason"] = getattr(message, 'stop_reason', None)
                # Surface SDK-reported errors (added in SDK 0.1.51)
                result_errors = getattr(message, 'errors', None)
                if result_errors:
                    metadata["errors"] = result_errors
                    logger.warning(f"[Background] ResultMessage contains errors: {result_errors}")
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

                    # Update LiveSessionStore usage for late-joining devices
                    try:
                        await live_session_store.update_usage(
                            session_id,
                            input_tokens=message.usage.get("input_tokens", 0),
                            output_tokens=message.usage.get("output_tokens", 0),
                            cache_creation_tokens=message.usage.get("cache_creation_input_tokens", 0),
                            cache_read_tokens=message.usage.get("cache_read_input_tokens", 0),
                        )
                    except Exception as e:
                        logger.warning(f"[Background] Failed to update usage in live store for {session_id}: {e}")

    except asyncio.CancelledError:
        interrupted = True
        logger.debug(f"[Background] Query interrupted for session {session_id}")

    except Exception as e:
        logger.error(f"[Background] Stream query error for session {session_id}: {e}")
        state.is_connected = False
        metadata["error"] = str(e)

    finally:
        # Mark as not streaming — keep in pool for reuse
        state.is_streaming = False
        state.streaming_started_at = None
        state.last_activity = datetime.now(timezone.utc)
        state.background_task = None

        # Remove dead clients from pool
        if not state.is_connected:
            await pool.remove_dead(session_id)

        # End LiveSessionStore streaming BEFORE broadcast_stream_end
        try:
            await live_session_store.end_streaming(session_id, metadata=metadata, interrupted=interrupted)
        except Exception as e:
            logger.warning(f"[Background] Failed to end live session store streaming for {session_id}: {e}")

        # Broadcast stream end to all devices
        await sync_engine.broadcast_stream_end(
            session_id=session_id,
            message_id=assistant_msg_id,
            metadata=metadata,
            interrupted=interrupted,
            source_device_id=None
        )

        # Clean up per-session state that is no longer needed.
        # Permission rules and pending requests accumulate per session and are
        # never cleared otherwise, causing unbounded memory growth.
        permission_handler.clear_session_rules(session_id)
        await permission_handler.cancel_all_requests(session_id)

        # Release session lock in sync engine (in case not released by WS disconnect).
        # Use the public API which checks ownership — but in the stream-end path
        # there's no device_id, so directly pop under the lock.
        async with sync_engine._lock:
            sync_engine._session_locks.pop(session_id, None)  # noqa: SLF001

        # Schedule delayed cleanup of live store data (keep for 30s for late joiners)
        async def _delayed_cleanup():
            await asyncio.sleep(30)
            await live_session_store.cleanup_ended_session(session_id)

        asyncio.create_task(_delayed_cleanup())

    # Persist session data to database in a batched write.
    await _persist_turn_results(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        tool_messages=tool_messages,
        response_text=response_text,
        metadata=metadata,
        profile_id=profile_id,
        interrupted=interrupted,
        log_prefix="[Background] ",
    )

    logger.debug(f"[Background] Query completed for session {session_id}")


async def start_background_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    device_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Start a streaming query in a background task.

    Returns immediately with session info. Work continues in background.
    Use interrupt_session() to stop.
    """
    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")

    # Get project if specified
    project = None
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

    # Get or create session in database
    resume_id = None

    if session_id:
        session = await run_sync(database.get_session, session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        resume_id = session.get("sdk_session_id")
        logger.debug(f"Resuming session {session_id} with SDK session {resume_id}")
    else:
        session_id = str(uuid.uuid4())
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = await run_sync(
            database.create_session,
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id
        )
        logger.debug(f"Created new session {session_id} with title: {title}")

    # Generate a message ID for streaming
    assistant_msg_id = f"streaming-{session_id}-{datetime.now(timezone.utc).timestamp()}"

    # Broadcast stream_start BEFORE returning so the WebSocket can pick it up
    # This ensures is_streaming=true and buffer exists when client connects
    await sync_engine.broadcast_stream_start(
        session_id=session_id,
        message_id=assistant_msg_id,
        source_device_id=None,  # Don't exclude any device - all should see it
        user_id=api_user_id
    )
    logger.debug(f"[Background] Broadcast stream_start for session {session_id}")

    # Initialize LiveSessionStore for late-joining devices
    try:
        working_dir = resolve_working_dir(session)
        sdk_sid = session.get("sdk_session_id") if session else None
        initial_messages = message_resolver.resolve_for_streaming_init(session_id, sdk_sid, working_dir)
    except Exception:
        initial_messages = []

    await live_session_store.start_streaming(session_id, initial_messages)

    # Add user message to live store
    user_msg_id = f"user-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    await live_session_store.add_user_message(session_id, prompt, user_msg_id)

    # Start background task
    task = asyncio.create_task(
        _run_background_query(
            session_id=session_id,
            prompt=prompt,
            profile=profile,
            project=project,
            overrides=overrides,
            resume_id=resume_id,
            device_id=device_id,
            api_user_id=api_user_id,
            message_id=assistant_msg_id
        )
    )

    # Store task reference for interrupt support
    # The background task picks this up after pool.put() creates the state entry
    _pending_background_tasks[session_id] = task

    # Safety net: if the task completes without _store_background_task being
    # called (e.g., connection failure or early return), remove the orphaned entry.
    def _cleanup_pending(t: asyncio.Task):
        _pending_background_tasks.pop(session_id, None)
    task.add_done_callback(_cleanup_pending)

    return {
        "session_id": session_id,
        "status": "started",
        "message_id": assistant_msg_id
    }


def _store_background_task(session_id: str):
    """Store pending background task reference on the pool state entry.

    Called from _run_background_query after pool.put() ensures the state exists.
    """
    task = _pending_background_tasks.pop(session_id, None)
    if task:
        state = get_session_pool().peek(session_id)
        if state:
            state.background_task = task


async def interrupt_session(session_id: str) -> bool:
    """Interrupt an active streaming session.

    This calls the SDK's interrupt() method which signals the Claude API
    to stop processing. This is more reliable than just cancelling the
    asyncio task, which only takes effect at the next await point.
    """
    # Use peek() to avoid promoting to MRU — interrupt is a read-only check
    state = get_session_pool().peek(session_id)
    if not state:
        logger.warning(f"No active session found for {session_id}")
        return False

    if not state.is_connected:
        logger.warning(f"Session {session_id} is not connected")
        return False

    # Set interrupt flag first - this is checked in the streaming loop as a failsafe
    state.interrupt_requested = True

    # Don't check is_streaming too strictly - there might be race conditions
    # and it's better to try to interrupt anyway
    logger.debug(f"Attempting to interrupt session {session_id} (streaming={state.is_streaming})")

    try:
        await state.client.interrupt()
        # Don't set is_streaming = False here. The streaming loop will set it
        # when it actually finishes processing final messages. Setting it
        # prematurely causes a race where the loop still processes messages
        # but the session appears idle.
        logger.debug(f"Successfully interrupted session {session_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to interrupt session {session_id}: {e}", exc_info=True)
        return False


def get_active_sessions() -> list:
    """Get list of active session IDs (connected clients)"""
    return get_session_pool().active_session_ids()


def get_streaming_sessions() -> list:
    """Get list of currently streaming session IDs"""
    return get_session_pool().streaming_session_ids()


async def stream_to_websocket(
    prompt: str,
    session_id: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    broadcast_func: Optional[callable] = None,
    api_user_id: Optional[str] = None
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Stream Claude response directly to WebSocket.

    This is the simplified streaming function for the WebSocket-first architecture.
    No sync engine, no background tasks - just direct streaming.

    Yields events:
    - {"type": "chunk", "content": "..."}
    - {"type": "tool_use", "name": "...", "input": {...}}
    - {"type": "tool_result", "name": "...", "output": "..."}
    - {"type": "permission_request", ...} - When tool permission is needed
    - {"type": "done", "session_id": "...", "metadata": {...}}
    - {"type": "error", "message": "..."}

    Args:
        prompt: The user's message
        session_id: The chat session ID
        profile_id: The profile to use
        project_id: Optional project ID
        overrides: Optional overrides for model, permission_mode, etc.
        broadcast_func: Async function to broadcast messages to the WebSocket
        api_user_id: Optional API user ID for user-specific credential resolution
    """
    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        yield {"type": "error", "message": f"Profile not found: {profile_id}"}
        return

    # Get project if specified
    project = None
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if not project:
            yield {"type": "error", "message": f"Project not found: {project_id}"}
            return

    # Get session for resume ID
    session = await run_sync(database.get_session, session_id)
    resume_id = session.get("sdk_session_id") if session else None

    # Determine permission mode
    config = profile.get("config", {})
    permission_mode = overrides.get("permission_mode") if overrides else None
    if not permission_mode:
        permission_mode = config.get("permission_mode", "default")

    logger.debug(f"[WS] Permission mode for session {session_id}: {permission_mode} (profile config: {config.get('permission_mode')}, override: {overrides.get('permission_mode') if overrides else None})")
    logger.debug(f"[WS] broadcast_func provided: {broadcast_func is not None}")

    # Get tool restrictions for use in callback
    allowed_tools = config.get("allowed_tools") or []
    disallowed_tools = config.get("disallowed_tools") or []

    # --- Session Pool: Acquire or create SDK client ---
    pool = get_session_pool()
    existing_state = pool.get(session_id)
    reusing_client = False
    state = None

    # Create a queue for plugin stderr messages to be broadcast to frontend
    # Plugin hooks run in the SDK's async loop but need to send messages to our streaming loop
    stderr_queue: asyncio.Queue = asyncio.Queue()

    # Build options once (without callback — callback is set later if needed)
    _build_opts_start = time.monotonic()
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        can_use_tool=None,
        hooks=None,
        api_user_id=api_user_id,
        session_id=session_id
    )
    _build_opts_elapsed = time.monotonic() - _build_opts_start
    if _build_opts_elapsed > 0.5:
        logger.warning(f"[WS] build_options_from_profile took {_build_opts_elapsed:.2f}s for session {session_id}")

    if existing_state and existing_state.is_connected and is_subprocess_alive(existing_state):
        # Candidate for reuse — check if critical options changed
        if not requires_reconnect(existing_state, options):
            # REUSE: Subprocess is alive and options are compatible
            state = existing_state
            reusing_client = True

            # Update callback refs so the existing can_use_tool closure sees current values
            state.callback_refs.update({
                "broadcast_func": broadcast_func,
                "permission_mode": permission_mode,
                "allowed_tools": allowed_tools,
                "disallowed_tools": disallowed_tools,
                "profile_id": profile_id,
                "config": config,
                "session_id": session_id,
            })

            # Handle runtime-changeable options via SDK methods
            old_opts = state.connect_options
            if old_opts:
                new_model = getattr(options, 'model', None)
                old_model = getattr(old_opts, 'model', None)
                if new_model and new_model != old_model:
                    try:
                        await state.client.set_model(new_model)
                        logger.debug(f"[WS] Updated model to {new_model} for reused session {session_id}")
                    except Exception as e:
                        logger.warning(f"[WS] Failed to set model on reused session {session_id}: {e}")

            logger.info(f"[WS] Reusing pooled client for session {session_id}")
        else:
            # Options changed — need fresh subprocess
            logger.info(f"[WS] Options changed for session {session_id}, creating fresh client")
            await pool.remove(session_id)
            existing_state = None
    elif existing_state:
        # Subprocess died — clean up dead entry
        logger.warning(f"[WS] Subprocess dead for pooled session {session_id}, creating fresh client")
        await pool.remove_dead(session_id)
        existing_state = None

    if not reusing_client:
        # CREATE: Build full options with callback, connect new subprocess

        # Create callback_refs dict — the callback closure captures this dict reference
        # so we can update values between queries without recreating the callback
        callback_refs = {
            "broadcast_func": broadcast_func,
            "permission_mode": permission_mode,
            "allowed_tools": allowed_tools,
            "disallowed_tools": disallowed_tools,
            "profile_id": profile_id,
            "config": config,
            "session_id": session_id,
        }

        # Create canUseTool callback that reads from callback_refs (mutable dict)
        # This allows the callback to use current values even when the client is reused
        can_use_tool_callback = None
        if broadcast_func:
            logger.debug(f"[WS] Creating canUseTool callback for session {session_id}")

            async def can_use_tool_callback(tool_name: str, tool_input: dict, context):
                """Permission callback that reads from mutable callback_refs dict."""
                refs = callback_refs
                _disallowed = refs["disallowed_tools"]
                _allowed = refs["allowed_tools"]
                _perm_mode = refs["permission_mode"]
                _broadcast = refs["broadcast_func"]
                _profile_id = refs["profile_id"]
                _config = refs["config"]
                _sid = refs["session_id"]

                # Extract tool_use_id and agent_id from context (SDK 0.1.52+)
                # These enable disambiguating parallel permission requests
                _ctx_tool_use_id = getattr(context, 'tool_use_id', None)
                _ctx_agent_id = getattr(context, 'agent_id', None)

                # Check profile-level tool restrictions FIRST
                if _disallowed and tool_name in _disallowed:
                    logger.debug(f"[WS] Tool {tool_name} blocked by profile disallowed_tools")
                    return PermissionResultDeny(message=f"Tool '{tool_name}' is disabled in this profile")
                if _allowed and tool_name not in _allowed:
                    logger.debug(f"[WS] Tool {tool_name} blocked by profile allowed_tools (not in whitelist)")
                    return PermissionResultDeny(message=f"Tool '{tool_name}' is not enabled in this profile")

                # Handle AskUserQuestion specially
                if tool_name == "AskUserQuestion":
                    questions = tool_input.get("questions", [])
                    if not questions:
                        return PermissionResultAllow(updated_input=tool_input)

                    request_id = f"question-{_sid}-{uuid.uuid4().hex[:8]}"
                    logger.debug(f"[WS] AskUserQuestion canUseTool triggered: {request_id} with {len(questions)} questions (tool_use_id={_ctx_tool_use_id}, agent_id={_ctx_agent_id})")

                    answers = await user_question_handler.request_answers(
                        request_id=request_id,
                        tool_use_id=_ctx_tool_use_id or request_id,
                        session_id=_sid,
                        questions=questions,
                        broadcast_func=_broadcast
                    )

                    logger.debug(f"[WS] Got answers for {request_id}: {list(answers.keys())}")
                    return PermissionResultAllow(updated_input={**tool_input, "answers": answers})

                # ExitPlanMode handling — show approval modal in default mode
                # only. In bypass mode, auto-approve so fire-and-forget
                # workflows aren't blocked waiting for user input.
                if tool_name == "ExitPlanMode":
                    if _perm_mode != "bypassPermissions":
                        request_id = f"perm-{_sid}-{uuid.uuid4().hex[:8]}"
                        logger.debug(f"[WS] ExitPlanMode requires approval in {_perm_mode} mode: {request_id}")

                        result = await permission_handler.request_permission(
                            request_id=request_id,
                            session_id=_sid,
                            profile_id=_profile_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            broadcast_func=_broadcast,
                            tool_use_id=_ctx_tool_use_id,
                            agent_id=_ctx_agent_id,
                        )
                        if result.behavior == "deny":
                            return result

                    return PermissionResultAllow()

                # For other tools in default mode, route through permission handler
                if _perm_mode == "default":
                    request_id = f"perm-{_sid}-{uuid.uuid4().hex[:8]}"
                    logger.debug(f"[WS] Permission request for {tool_name}: {request_id}")

                    result = await permission_handler.request_permission(
                        request_id=request_id,
                        session_id=_sid,
                        profile_id=_profile_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        broadcast_func=_broadcast,
                        tool_use_id=_ctx_tool_use_id,
                        agent_id=_ctx_agent_id,
                    )
                    return result
                else:
                    return PermissionResultAllow()

        # Set the callback on the already-built options (avoid redundant rebuild)
        options.can_use_tool = can_use_tool_callback

        # Build hooks configuration from all sources (plugins, database)
        from app.core.hook_service import build_hooks_config

        async def stderr_callback(plugin_name: str, stderr_output: str, severity: str):
            """Callback for plugin hooks to broadcast stderr to frontend."""
            await stderr_queue.put({
                "type": "plugin_message",
                "plugin": plugin_name,
                "message": stderr_output,
                "severity": severity
            })

        if options.cwd:
            plugin_paths = []
            if options.plugins:
                for plugin_entry in options.plugins:
                    if isinstance(plugin_entry, dict) and plugin_entry.get("path"):
                        plugin_paths.append(plugin_entry["path"])

            hooks_config = build_hooks_config(
                working_dir=options.cwd,
                project_id=project_id,
                plugin_paths=plugin_paths,
                stderr_callback=stderr_callback
            )
            if hooks_config:
                options.hooks = hooks_config
                logger.debug(f"[WS] Hooks configured: {list(hooks_config.keys())}")

        # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
        agents_dir = None
        written_agent_ids = []
        if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
            agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)
            written_agent_ids = list(agents_dict.keys())

        # Evict LRU session if pool is full
        evicted = await pool.evict_if_needed()
        if evicted:
            logger.debug(f"[WS] Evicted LRU session {evicted} to make room for {session_id}")

        # Create and connect new client
        logger.debug(f"[WS] Creating ClaudeSDKClient for session {session_id} (resume={resume_id is not None}, include_partial={options.include_partial_messages})")
        logger.debug(f"[WS] Options cwd: {options.cwd}")

        # Wrap stderr callback to collect lines for error reporting
        options.stderr, cli_stderr_lines = _wrap_stderr_capture(options.stderr)

        adapter = ClaudeAgentAdapter()
        logger.debug("[WS] ClaudeAgentAdapter created, attempting connect...")

        try:
            _connect_start = time.monotonic()
            await asyncio.wait_for(adapter.connect_with_sdk_options(options), timeout=60.0)
            _connect_elapsed = time.monotonic() - _connect_start
            logger.info(f"[WS] SDK connect for session {session_id}: {_connect_elapsed:.1f}s (reuse=False)")
        except Exception as e:
            import traceback
            logger.error(f"[WS] Failed to connect for session {session_id}: {e}")
            logger.error(f"[WS] Exception type: {type(e).__name__}")
            logger.error(f"[WS] Full traceback:\n{traceback.format_exc()}")
            # Check if the CLI subprocess is still alive
            if adapter.is_alive():
                logger.error("[WS] CLI process is still running (hung during initialization)")
            elif adapter.sdk_client:
                from app.core.sdk_process_handle import SdkProcessHandle
                handle = SdkProcessHandle(adapter.sdk_client)
                if handle.available and handle.returncode is not None:
                    logger.error(f"[WS] CLI process exited with code {handle.returncode} during initialization")
            if cli_stderr_lines:
                stderr_msg = " ".join(line.strip() for line in cli_stderr_lines if line.strip())
                logger.error(f"[WS] CLI stderr: {stderr_msg}")
                yield {"type": "error", "message": stderr_msg}
            else:
                yield {"type": "error", "message": f"Connection failed: {e}"}
            return

        state = SessionState(
            adapter=adapter,
            sdk_session_id=resume_id,
            is_connected=True,
            written_agent_ids=written_agent_ids,
            agents_dir=agents_dir,
            callback_refs=callback_refs,
            connect_options=options,
        )
        pool.put(session_id, state)

    # Mark as streaming (both reuse and create paths)
    state.is_streaming = True
    state.interrupt_requested = False  # Reset from previous query's interrupt
    state.streaming_started_at = datetime.now(timezone.utc)
    state.last_activity = datetime.now(timezone.utc)

    # Initialize sdk_session_id - use resume_id if resuming, otherwise None until we get it from SDK
    sdk_session_id = resume_id

    # Execute query
    # Note: SDK built-in commands like /context and /compact are passed directly to the SDK
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    task_tool_uses = {}  # Track Task (subagent) tool uses by tool_id
    tool_id_to_name = {}  # Track tool_use_id → tool_name for ToolResultBlock lookup
    metadata = {}
    interrupted = False
    include_partial = options.include_partial_messages  # Track if streaming events are enabled
    has_visible_content = False  # Track if any content was yielded to the frontend
    is_slash_command = prompt.strip().startswith("/")

    # Helper to drain stderr queue and yield plugin messages
    async def drain_stderr_queue():
        """Drain all pending stderr messages from plugin hooks."""
        messages = []
        while True:
            try:
                msg = stderr_queue.get_nowait()
                messages.append(msg)
                logger.debug(f"[WS] Drained plugin message from queue: {msg.get('type')}")
            except asyncio.QueueEmpty:
                break
        if messages:
            logger.debug(f"[WS] Drained {len(messages)} plugin message(s) from stderr queue")
        return messages

    try:
        # Resolve virtual @Notes/note-id references to actual content
        prompt = resolve_note_references(prompt)

        _query_send_start = time.monotonic()
        await state.client.query(prompt)
        _query_send_elapsed = time.monotonic() - _query_send_start
        if _query_send_elapsed > 1.0:
            logger.warning(f"[WS] client.query() took {_query_send_elapsed:.1f}s for session {session_id}")

        _stream_start = time.monotonic()
        _event_count = 0
        async for message in state.client.receive_response():
            # Don't break immediately on interrupt_requested — let receive_response()
            # end naturally so remaining in-flight tool calls reach the UI.
            # The SDK's client.interrupt() causes receive_response() to end after
            # completing any in-progress operations.

            _event_count += 1

            # Drain stderr queue and yield any plugin messages
            for stderr_msg in await drain_stderr_queue():
                logger.debug(f"[WS] Yielding plugin message to websocket: {stderr_msg.get('type')}")
                yield stderr_msg

            # Check typed task messages before generic SystemMessage
            # (they are subclasses and would match isinstance SystemMessage)
            if isinstance(message, TaskNotificationMessage):
                has_visible_content = True
                yield {
                    "type": "task_notification",
                    "task_id": message.task_id,
                    "status": message.status.value if hasattr(message.status, 'value') else str(message.status),
                    "summary": message.summary,
                    "tool_use_id": message.tool_use_id,
                }
            elif isinstance(message, TaskProgressMessage):
                # Include usage stats so the event handler can update subagent progress
                usage_data = None
                if message.usage:
                    usage_data = {
                        "total_tokens": getattr(message.usage, 'total_tokens', 0),
                        "tool_uses": getattr(message.usage, 'tool_uses', 0),
                        "duration_ms": getattr(message.usage, 'duration_ms', 0),
                    }
                yield {
                    "type": "task_progress",
                    "task_id": message.task_id,
                    "description": message.description,
                    "tool_use_id": message.tool_use_id,
                    "usage": usage_data,
                }
            elif isinstance(message, TaskStartedMessage):
                yield {
                    "type": "task_started",
                    "task_id": message.task_id,
                    "description": message.description,
                    "tool_use_id": message.tool_use_id,
                }
            elif isinstance(message, SystemMessage):
                logger.debug(f"[WS] SystemMessage received: subtype={message.subtype}, data_keys={list(message.data.keys()) if message.data else []}, data_preview={str(message.data)[:500] if message.data else 'None'}")
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.debug(f"[WS] Captured SDK session ID: {sdk_session_id}")

                else:
                    # Forward all system messages to frontend except noisy internal ones.
                    # Previous whitelist approach silently dropped SDK errors/warnings
                    # with new subtypes (e.g., skill errors, command output).
                    SUPPRESSED_SUBTYPES = {
                        'init',  # Already handled above
                        'rate_limit_event',  # Internal API rate limit info, not useful in chat
                    }

                    # Suppress system messages from subagent execution.
                    # SystemMessage doesn't have parent_tool_use_id as a direct attribute,
                    # but the raw data dict may contain it. Also suppress if any subagents
                    # are actively running (SDK runs subagents synchronously, so system
                    # messages during active subagent execution are from the subagent context).
                    parent_tool_id = message.data.get("parent_tool_use_id") if message.data else None
                    is_from_subagent = (
                        (parent_tool_id and parent_tool_id in task_tool_uses) or
                        (not parent_tool_id and len(task_tool_uses) > 0)
                    )

                    if is_from_subagent:
                        logger.debug(f"[WS] Suppressed subagent SystemMessage: subtype={message.subtype}")
                    elif message.subtype not in SUPPRESSED_SUBTYPES:
                        has_visible_content = True
                        logger.debug(f"[WS] Forwarding SystemMessage to frontend: subtype={message.subtype}")
                        yield {
                            "type": "system",
                            "subtype": message.subtype,
                            "data": message.data
                        }
                    else:
                        logger.debug(f"[WS] Suppressed SystemMessage subtype: {message.subtype}")

            elif isinstance(message, StreamEvent):
                # StreamEvent is sent when include_partial_messages=True
                # Contains real-time streaming events for character-by-character display
                event = message.event
                event_type = event.get("type") if event else None
                parent_tool_id = getattr(message, 'parent_tool_use_id', None)

                # Route subagent StreamEvents — don't mix into main agent stream
                # The SDK (0.1.30+) sets parent_tool_use_id on subagent events
                if parent_tool_id and parent_tool_id in task_tool_uses:
                    # Only forward text deltas as subagent_chunk for real-time display
                    if event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield {
                                "type": "subagent_chunk",
                                "agent_id": parent_tool_id,
                                "content": delta["text"]
                            }
                    # Skip all other subagent stream events (block_start, block_stop,
                    # message_start, message_delta, thinking_delta, input_json_delta)
                    # The AssistantMessage handler already handles final subagent content
                    continue

                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    delta_type = delta.get("type")

                    if delta_type == "text_delta" and delta.get("text"):
                        # Real-time text streaming chunk
                        text = delta["text"]
                        has_visible_content = True
                        logger.debug(f"[WS] StreamEvent text_delta: {len(text)} chars")
                        yield {
                            "type": "stream_delta",
                            "delta_type": "text",
                            "content": text,
                            "index": event.get("index", 0)
                        }
                    elif delta_type == "thinking_delta" and delta.get("thinking"):
                        # Thinking block streaming
                        yield {
                            "type": "stream_delta",
                            "delta_type": "thinking",
                            "content": delta["thinking"],
                            "index": event.get("index", 0)
                        }
                    elif delta_type == "input_json_delta" and delta.get("partial_json"):
                        # Tool input streaming
                        yield {
                            "type": "stream_delta",
                            "delta_type": "tool_input",
                            "content": delta["partial_json"],
                            "index": event.get("index", 0)
                        }

                elif event_type == "message_start":
                    # Start of a new streaming message
                    logger.debug("[WS] StreamEvent message_start")
                    yield {
                        "type": "stream_start",
                        "message": event.get("message", {})
                    }

                elif event_type == "content_block_start":
                    # Start of a content block (text, thinking, tool_use)
                    content_block = event.get("content_block", {})
                    block_type = content_block.get("type")
                    logger.debug(f"[WS] StreamEvent content_block_start: {block_type}")
                    yield {
                        "type": "stream_block_start",
                        "block_type": block_type,
                        "index": event.get("index", 0),
                        "content_block": content_block
                    }

                elif event_type == "content_block_stop":
                    # End of a content block
                    yield {
                        "type": "stream_block_stop",
                        "index": event.get("index", 0)
                    }

                elif event_type == "message_delta":
                    # Final message metadata (stop_reason, usage)
                    yield {
                        "type": "stream_message_delta",
                        "delta": event.get("delta", {}),
                        "usage": event.get("usage", {})
                    }

            elif isinstance(message, AssistantMessage):
                # Check for API error messages from the SDK.
                # The SDK sets message.error for actual API errors (rate_limit,
                # billing_error, etc.). Don't rely on model == "<synthetic>" alone
                # — the SDK also uses synthetic messages for local command output
                # (e.g., /context, /compact) which are NOT errors.
                if message.error:
                    # Extract error text from content blocks
                    error_text = ""
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            error_text = block.text
                            break
                    if error_text:
                        has_visible_content = True
                        logger.warning(f"[WS] API error message (error={message.error}): {error_text[:100]}...")
                        yield {
                            "type": "system",
                            "subtype": "api_error",
                            "data": {"content": error_text}
                        }
                    continue

                # Non-error synthetic messages — local command output from SDK
                # built-in commands like /context, /compact, /cost, etc.
                if message.model == "<synthetic>":
                    if is_slash_command:
                        # Extract text from synthetic response for display
                        text = ""
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text += block.text
                        text = text.strip()
                        if text and text != "No response requested.":
                            has_visible_content = True
                            logger.debug(f"[WS] Synthetic local command output: {text[:200]}")
                            yield {
                                "type": "system",
                                "subtype": "local_command",
                                "data": {"content": text}
                            }
                    continue

                # Check if this message is from a subagent using parent_tool_use_id
                parent_tool_id = message.parent_tool_use_id

                for block in message.content:
                    if isinstance(block, TextBlock):
                        # Check if this text is from a subagent
                        if parent_tool_id and parent_tool_id in task_tool_uses:
                            # Text from subagent - send as subagent_chunk
                            logger.debug(f"[WS] Subagent chunk len={len(block.text)} for agent={parent_tool_id}")
                            yield {
                                "type": "subagent_chunk",
                                "agent_id": parent_tool_id,
                                "content": block.text
                            }
                        else:
                            # When include_partial_messages=True, text was already streamed via stream_delta
                            # Only send chunk when partial messages is disabled
                            response_text.append(block.text)
                            has_visible_content = True
                            if not include_partial:
                                logger.debug(f"[WS] Text chunk len={len(block.text)} for session={session_id}")
                                yield {"type": "chunk", "content": block.text}

                    elif isinstance(block, ToolUseBlock):
                        tool_id = getattr(block, 'id', None)
                        tool_input = block.input or {}

                        # Track tool_id → name for ToolResultBlock lookup
                        if tool_id:
                            tool_id_to_name[tool_id] = block.name

                        # Collect tool use for storage
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": tool_id,
                            "input": tool_input
                        })

                        # Check if this is a subagent invocation (SDK ≤0.1.30 "Task", SDK ≥0.1.48 "Agent")
                        if block.name in ("Task", "Agent"):
                            # Derive agent_type: prefer subagent_type, fall back to description
                            agent_type = (
                                tool_input.get("subagent_type")
                                or tool_input.get("description")
                                or "Agent"
                            )

                            # Track this Task tool use
                            task_tool_uses[tool_id] = {
                                "agent_type": agent_type,
                                "description": tool_input.get("description", ""),
                                "prompt": tool_input.get("prompt", "")
                            }

                            # Yield subagent_start event
                            yield {
                                "type": "subagent_start",
                                "tool_id": tool_id,
                                "agent_type": agent_type,
                                "description": tool_input.get("description", ""),
                                "prompt": tool_input.get("prompt", ""),
                                "agent_id": tool_id
                            }
                        # Check if this tool use is from a subagent
                        elif parent_tool_id and parent_tool_id in task_tool_uses:
                            # Tool use from subagent - send as subagent_tool_use
                            yield {
                                "type": "subagent_tool_use",
                                "agent_id": parent_tool_id,
                                "name": block.name,
                                "id": tool_id,
                                "input": tool_input
                            }
                        else:
                            yield {
                                "type": "tool_use",
                                "name": block.name,
                                "id": tool_id,
                                "input": tool_input
                            }

                    elif isinstance(block, ToolResultBlock):
                        # Extract screenshot from MCP content blocks before truncation
                        screenshot_b64 = extract_mcp_image(block.content)
                        output = truncate_large_payload(block.content)
                        tool_use_id = getattr(block, 'tool_use_id', None)
                        tool_name = tool_id_to_name.get(tool_use_id, getattr(block, 'name', 'unknown'))

                        # Diagnostic: log when tool result content is empty/missing
                        if not output:
                            content_type = type(block.content).__name__
                            content_repr = repr(block.content)[:200] if block.content else "None"
                            logger.warning(
                                f"[WS] AssistantMsg ToolResultBlock EMPTY output - "
                                f"tool={tool_name}, tool_use_id={tool_use_id}, "
                                f"content_type={content_type}, content_repr={content_repr}"
                            )

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output
                        })

                        # Check if this is a result for a Task tool (subagent)
                        if tool_use_id in task_tool_uses:
                            task_info = task_tool_uses[tool_use_id]
                            yield {
                                "type": "subagent_done",
                                "tool_id": tool_use_id,
                                "agent_id": tool_use_id,  # Use tool_id as agent_id
                                "agent_type": task_info.get("agent_type", "unknown"),
                                "result": output,
                                "is_error": getattr(block, 'is_error', False)
                            }
                            # Clean up tracking
                            del task_tool_uses[tool_use_id]
                        # Check if this tool result is from a subagent
                        elif parent_tool_id and parent_tool_id in task_tool_uses:
                            # Tool result from subagent - send as subagent_tool_result
                            yield {
                                "type": "subagent_tool_result",
                                "agent_id": parent_tool_id,
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": getattr(block, 'is_error', False)
                            }
                        else:
                            event = {
                                "type": "tool_result",
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": getattr(block, 'is_error', False)
                            }
                            # Include screenshot for browser tools (MCP content has image blocks)
                            if screenshot_b64:
                                event["screenshot"] = screenshot_b64
                            yield event

                metadata["model"] = message.model

                # Per-turn usage from AssistantMessage (SDK 0.1.49+)
                if message.usage:
                    yield {
                        "type": "turn_usage",
                        "usage": message.usage
                    }

            elif isinstance(message, UserMessage):
                # UserMessage can contain:
                # 1. Tool results from Claude's tool executions (ToolResultBlock)
                # 2. Local command output like /context, /compact (TextBlock with <local-command-stdout>)
                # Check if this message is from a subagent using parent_tool_use_id
                parent_tool_id = message.parent_tool_use_id
                logger.debug(f"[WS] UserMessage received: content_type={type(message.content).__name__}, parent_tool_id={parent_tool_id}")

                # Suppress local command output from subagent UserMessages
                is_subagent_user_msg = parent_tool_id and parent_tool_id in task_tool_uses

                # Handle string content (rare but possible)
                if isinstance(message.content, str):
                    content = message.content
                    logger.debug(f"[WS] UserMessage string content preview: {content[:200] if content else 'empty'}")
                    # Check for local command output
                    if "<local-command-stdout>" in content:
                        match = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', content, re.DOTALL)
                        if match and not is_subagent_user_msg:
                            has_visible_content = True
                            yield {
                                "type": "system",
                                "subtype": "local_command",
                                "data": {"content": match.group(1).strip()}
                            }
                    elif is_slash_command and content.strip() and content.strip() != "No response requested." and not is_subagent_user_msg:
                        has_visible_content = True
                        logger.debug(f"[WS] Forwarding slash command UserMessage string: {content[:200]}")
                        yield {
                            "type": "system",
                            "subtype": "local_command",
                            "data": {"content": content.strip()}
                        }
                    continue

                for block in message.content:
                    # Handle TextBlock - may contain local command output
                    if isinstance(block, TextBlock):
                        text = block.text
                        # Check for local command output (e.g., /context, /compact)
                        if "<local-command-stdout>" in text:
                            match = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
                            if match and not is_subagent_user_msg:
                                has_visible_content = True
                                yield {
                                    "type": "system",
                                    "subtype": "local_command",
                                    "data": {"content": match.group(1).strip()}
                                }
                        elif is_slash_command and text.strip() and text.strip() != "No response requested." and not is_subagent_user_msg:
                            # For slash commands, forward non-trivial UserMessage text
                            # SDK built-in commands may return output without <local-command-stdout> tags
                            has_visible_content = True
                            logger.debug(f"[WS] Forwarding slash command UserMessage text: {text[:200]}")
                            yield {
                                "type": "system",
                                "subtype": "local_command",
                                "data": {"content": text.strip()}
                            }
                        # Don't yield regular text from UserMessage for non-slash queries

                    elif isinstance(block, ToolResultBlock):
                        # Extract screenshot from MCP content blocks before truncation
                        screenshot_b64 = extract_mcp_image(block.content)
                        output = truncate_large_payload(block.content) if block.content else ""
                        tool_use_id = block.tool_use_id
                        # Look up tool name from preceding ToolUseBlock
                        tool_name = tool_id_to_name.get(tool_use_id, "unknown")
                        content_len = len(str(block.content) if block.content else '')
                        logger.debug(f"[WS] UserMessage ToolResultBlock - tool_use_id: {tool_use_id}, tool_name: {tool_name}, parent_tool_id: {parent_tool_id}, content length: {content_len}, has_screenshot: {screenshot_b64 is not None}, is_error: {block.is_error}")

                        # Diagnostic: log when tool result content is empty/missing
                        if not output:
                            content_type = type(block.content).__name__
                            content_repr = repr(block.content)[:200] if block.content else "None"
                            logger.warning(
                                f"[WS] UserMessage ToolResultBlock EMPTY output - "
                                f"tool={tool_name}, tool_use_id={tool_use_id}, "
                                f"content_type={content_type}, content_repr={content_repr}, "
                                f"block.content falsy={not block.content}"
                            )

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output
                        })

                        # Check if this is a result for a Task tool (subagent)
                        if tool_use_id in task_tool_uses:
                            task_info = task_tool_uses[tool_use_id]
                            yield {
                                "type": "subagent_done",
                                "tool_id": tool_use_id,
                                "agent_id": tool_use_id,  # Use tool_id as agent_id
                                "agent_type": task_info.get("agent_type", "unknown"),
                                "result": output,
                                "is_error": block.is_error
                            }
                            # Clean up tracking
                            del task_tool_uses[tool_use_id]
                        # Check if this tool result is from a subagent
                        elif parent_tool_id and parent_tool_id in task_tool_uses:
                            # Tool result from subagent - send as subagent_tool_result
                            yield {
                                "type": "subagent_tool_result",
                                "agent_id": parent_tool_id,
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": block.is_error
                            }
                        else:
                            event = {
                                "type": "tool_result",
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": block.is_error
                            }
                            # Include screenshot for browser tools (MCP content has image blocks)
                            if screenshot_b64:
                                event["screenshot"] = screenshot_b64
                            yield event

            elif isinstance(message, RateLimitEvent):
                logger.debug(f"[WS] Rate limit event: status={message.rate_limit_info.status}, type={message.rate_limit_info.rate_limit_type}")
                yield {
                    "type": "rate_limit",
                    "status": message.rate_limit_info.status,
                    "rate_limit_type": message.rate_limit_info.rate_limit_type,
                    "resets_at": message.rate_limit_info.resets_at,
                    "utilization": message.rate_limit_info.utilization,
                }

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                metadata["stop_reason"] = getattr(message, 'stop_reason', None)
                # Surface SDK-reported errors (added in SDK 0.1.51)
                result_errors = getattr(message, 'errors', None)
                if result_errors:
                    metadata["errors"] = result_errors
                    logger.warning(f"[WS] ResultMessage contains errors: {result_errors}")
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

            else:
                logger.warning(f"[WS] Unhandled message type: {type(message).__name__}")

        # Log streaming performance summary
        _stream_elapsed = time.monotonic() - _stream_start
        logger.info(f"[WS] Streaming complete for session {session_id}: {_event_count} events in {_stream_elapsed:.1f}s")

        # Check if the loop ended due to an interrupt request
        # (SDK's receive_response() ended naturally after client.interrupt())
        if state.interrupt_requested:
            interrupted = True
            logger.debug(f"[WS] Streaming loop ended after interrupt for session {session_id}")

        # Final drain of stderr queue after streaming loop completes
        for stderr_msg in await drain_stderr_queue():
            yield stderr_msg

    except asyncio.CancelledError:
        interrupted = True
        logger.debug(f"[WS] Query cancelled for session {session_id}")
        # Don't re-raise - we handle it gracefully by yielding interrupted message

    except Exception as e:
        logger.error(f"[WS] Query error for session {session_id}: {e}")
        state.is_connected = False
        yield {"type": "error", "message": str(e)}
        return

    finally:
        # Mark as not streaming and reset interrupt flag
        state.is_streaming = False
        state.streaming_started_at = None
        state.interrupt_requested = False
        state.last_activity = datetime.now(timezone.utc)

        # Clear stale broadcast ref — will be set fresh on next query
        if state.callback_refs:
            state.callback_refs["broadcast_func"] = None

        # Session pool lifecycle: KEEP the client alive for reuse.
        # Only remove from pool if the client died.
        if not state.is_connected:
            # Client died during query — remove dead entry
            await pool.remove_dead(session_id)
            logger.debug(f"[WS] Removed dead session {session_id} from pool")
        else:
            # Normal case: keep in pool for reuse!
            logger.debug(f"[WS] Kept session {session_id} in pool for reuse")

    # Persist session data to database in a batched write.
    await _persist_turn_results(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        tool_messages=tool_messages,
        response_text=response_text,
        metadata=metadata,
        profile_id=profile_id,
        interrupted=interrupted,
        log_prefix="[WS] ",
    )

    # For slash commands that produced no visible content, yield a notification
    # SDK built-in commands like /memory, /cost, /model execute inside the subprocess
    # and may not produce any streaming events visible to the frontend
    if is_slash_command and not has_visible_content and not interrupted:
        cmd_name = prompt.strip().split()[0]  # e.g., "/memory"
        logger.info(f"[WS] Slash command {cmd_name} produced no visible output for session {session_id}")
        yield {
            "type": "system",
            "subtype": "local_command",
            "data": {"content": f"Command {cmd_name} completed (no output)."}
        }

    # Yield done or interrupted event
    if interrupted:
        yield {
            "type": "interrupted",
            "session_id": session_id,
            "message": "Query was interrupted"
        }
    else:
        yield {
            "type": "done",
            "session_id": session_id,
            "metadata": metadata
        }

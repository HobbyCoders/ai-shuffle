"""
Background query execution for fire-and-forget streaming.

Extracted from query_engine.py to isolate background task management.
"""

import logging
import uuid
import asyncio
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from claude_agent_sdk import (
    AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock,
    ResultMessage, SystemMessage,
)
try:
    from claude_agent_sdk import RateLimitEvent
except ImportError:
    class RateLimitEvent:
        pass
try:
    from claude_agent_sdk import HookEventMessage  # type: ignore[attr-defined]
except ImportError:
    class HookEventMessage:  # safe stub for older SDKs
        pass

from app.db import database
from app.db.async_database import run_sync
from app.core.session_pool import (
    SessionState, get_session_pool, is_subprocess_alive,
    requires_reconnect, claude_credentials_mtime,
    credentials_rotated, refresh_subprocess_credentials,
)
from app.core.oauth import direct_oauth
from app.core.profiles import get_profile
from app.core.sync_engine import sync_engine
from app.core.permission_handler import permission_handler
from app.core.live_session_store import live_session_store
from app.core.message_resolver import message_resolver, resolve_working_dir
from app.core.provider.claude_adapter import ClaudeAgentAdapter
from app.core.session_context import set_current_session_id

from app.core.query_persistence import (
    _wrap_stderr_capture,
    _extract_connection_error,
    _persist_turn_results,
    log_captured_cli_stderr,
)
from app.core.query_streaming import truncate_large_payload
from app.core.query_options import build_options_from_profile
from app.core.memory_sync import reconcile_memory, push_memory

logger = logging.getLogger(__name__)

# Pending background tasks awaiting pool entry creation
_pending_background_tasks: Dict[str, asyncio.Task] = {}


def _store_background_task(session_id: str):
    """Store pending background task reference on the pool state entry.

    Called from _run_background_query after pool.put() ensures the state exists.
    """
    task = _pending_background_tasks.pop(session_id, None)
    if task:
        state = get_session_pool().peek(session_id)
        if state:
            state.background_task = task


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

    # Keep the Claude OAuth token alive: refresh it now if it's at/near expiry so
    # the subprocess we're about to spawn/resume authenticates with a valid token
    # instead of 401-ing. No-op when the token is still valid or on API-key auth.
    await direct_oauth.ensure_fresh_credentials()

    # Build options with user-scoped credential resolution
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id
    )

    # --- Single-reader invariant (Bug D) ---
    # Before this background turn becomes a reader of the pooled SDK client
    # (reuse + client.query() + receive_response() below), cancel any between-turns
    # drain pump that may be reading the SAME client. The pump is the R2 reader
    # (receive_messages()) the WS turn loop always stops via stop_drain_pump() at
    # turn start; the REST fire-and-forget path must enforce the same invariant or
    # two coroutines race on one anyio message stream → lost/corrupted messages.
    # The pump only samples is_streaming once at startup, so flipping it below is
    # NOT enough to make it self-terminate — it must be explicitly cancelled here.
    # Function-local import: app.core must not import app.api at module load (cycle).
    from app.api.ws_cleanup import stop_drain_pump
    await stop_drain_pump(session_id)

    # --- Session Pool: Acquire or create SDK client ---
    pool = get_session_pool()
    existing_state = pool.get(session_id)
    reusing_client = False

    if existing_state and existing_state.is_connected and is_subprocess_alive(existing_state):
        # Busy-write guard backstop: never write a second prompt into a client
        # that is mid-turn (kills the CLI, loses the whole in-flight turn).
        # Fail safe like the connect-failure path — but do NOT cleanup the live
        # session store here: the active turn owns it and is still streaming.
        if existing_state.is_streaming:
            error_msg = (
                "Session is busy with another response. "
                "Wait for it to finish, then send your message again."
            )
            logger.warning(
                f"[Background] Pooled client for session {session_id} is mid-turn — "
                f"refusing concurrent background query (busy-write guard)"
            )
            await sync_engine.broadcast_stream_end(
                session_id=session_id,
                message_id=message_id or f"error-{session_id}",
                metadata={"error": error_msg},
                interrupted=True,
                source_device_id=device_id
            )
            return
        if existing_state.stale_turn_undrained:
            # A force-cancelled turn's terminal ResultMessage is still buffered on
            # the pooled client's read loop; the drain pump clears this flag once it
            # consumes the stale frame. Refuse until then — same fail-safe as the
            # busy-write gate, and likewise leave the live store untouched.
            error_msg = (
                "Session is still winding down a cancelled response; "
                "try again shortly."
            )
            logger.warning(
                f"[Background] Pooled client for session {session_id} is winding "
                f"down a cancelled response — refusing (stale-turn guard)"
            )
            await sync_engine.broadcast_stream_end(
                session_id=session_id,
                message_id=message_id or f"error-{session_id}",
                metadata={"error": error_msg},
                interrupted=True,
                source_device_id=device_id
            )
            return
        if not requires_reconnect(existing_state, options):
            state = existing_state
            reusing_client = True
            # CLAIM at the reuse decision point (no await since the is_streaming
            # check above) so two near-simultaneous entrants can't both pass the
            # gate. The finally below clears it on every exit path.
            state.is_streaming = True
            # Claim-to-try guard: any exit between this claim and the main
            # try/finally (the refresh await is a suspension point) must clear
            # the flag — a stranded is_streaming=True bricks the pooled client
            # (busy error forever, never idle-evicted).
            try:
                if credentials_rotated(state):
                    await refresh_subprocess_credentials(state, session_id)
                logger.info(f"[Background] Reusing pooled client for session {session_id}")
            except BaseException:
                state.is_streaming = False
                state.streaming_started_at = None
                raise
        else:
            logger.info(f"[Background] Options changed for session {session_id}, creating fresh client")
            await pool.remove(session_id)
            existing_state = None
    elif existing_state:
        logger.warning(f"[Background] Subprocess dead for pooled session {session_id}, creating fresh client")
        await pool.remove_dead(session_id)
        existing_state = None

    if not reusing_client:
        # Admission backpressure: refuse a NEW session if the box is at its
        # process/RAM/pool ceiling, rather than killing in-flight work later.
        refusal = pool.admission_check()
        if refusal is not None:
            logger.warning(f"[Admission] Refusing new background session {session_id}: {refusal}")
            raise RuntimeError(f"Server is at capacity — {refusal}. Please retry shortly.")

        # Evict LRU if pool is full
        await pool.evict_if_needed()

        logger.debug(f"[Background] Creating new ClaudeSDKClient for session {session_id} (resume={resume_id is not None})")

        # Attribute this session to the SDK's detached message-reader task, which
        # is created inside adapter.connect() below and inherits this context, so
        # a "Fatal error in message reader" carries the session id into error_log.
        set_current_session_id(session_id)

        # Wrap stderr callback to collect lines for error reporting
        options.stderr, cli_stderr_lines = _wrap_stderr_capture(options.stderr)

        # Pull shared agent-memory from the DB before the CLI reads it.
        await reconcile_memory(
            project.get("id") if project else None, getattr(options, "cwd", None)
        )

        adapter = ClaudeAgentAdapter()

        try:
            await asyncio.wait_for(adapter.connect_with_sdk_options(options), timeout=60.0)
            logger.debug(f"[Background] Connected to Claude SDK for session {session_id}")
        except asyncio.TimeoutError:
            logger.error(f"[Background] Timed out connecting to Claude SDK for session {session_id} (60s)")
            raise RuntimeError("Connection timed out — an MCP server may be unreachable")
        except Exception as e:
            error_msg = _extract_connection_error(e, cli_stderr_lines)
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
            connect_options=options,
            credentials_mtime=claude_credentials_mtime(),
            cli_stderr_lines=cli_stderr_lines,
        )
        pool.put(session_id, state)

    # Mark as streaming
    state.is_streaming = True
    state.streaming_started_at = datetime.now(timezone.utc)
    state.last_activity = datetime.now(timezone.utc)

    # Claim-to-try guard: is_streaming is claimed but the protective
    # try/finally hasn't started yet — any exit here (the broadcast/sync-log
    # awaits can raise or be cancelled) must clear the flag or the pooled
    # client is bricked (busy error forever, never idle-evicted).
    try:
        # Store pending background task reference now that pool entry exists
        _store_background_task(session_id)

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
    except BaseException:
        state.is_streaming = False
        state.streaming_started_at = None
        raise

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

                # Track per-API-call usage so we can persist the LAST call's
                # values (= current context window size). ResultMessage.usage
                # is cumulative across the agent loop and would inflate.
                if getattr(message, "usage", None):
                    metadata["last_call_tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["last_call_cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["last_call_cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)
                    # Keep live store in sync on each API call so late-joining
                    # devices and pre_seed_parse_cache see correct values.
                    try:
                        await live_session_store.update_usage(
                            session_id,
                            input_tokens=message.usage.get("input_tokens", 0),
                            output_tokens=message.usage.get("output_tokens", 0),
                            cache_creation_tokens=message.usage.get("cache_creation_input_tokens", 0),
                            cache_read_tokens=message.usage.get("cache_read_input_tokens", 0),
                        )
                    except Exception as e:
                        logger.warning(f"[Background] Failed to update per-call usage in live store for {session_id}: {e}")

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

            elif isinstance(message, HookEventMessage):
                logger.debug(f"[Background] Hook event: {getattr(message, 'hook_event_name', None)}")
                await live_session_store.add_system_message(
                    session_id,
                    content="",
                    subtype="hook_event",
                    data={
                        "hook_event_name": getattr(message, "hook_event_name", None),
                        "matcher": getattr(message, "matcher", None),
                        "decision": getattr(message, "decision", None),
                        "reason": getattr(message, "reason", None),
                        "system_message": getattr(message, "system_message", None),
                        "tool_name": getattr(message, "tool_name", None),
                        "session_id": session_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )

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
                # Surface HTTP error status code (SDK 0.1.81+); no WS yield — background loop has no direct consumer
                api_err = getattr(message, "api_error_status", None)
                if api_err is not None:
                    metadata["api_error_status"] = api_err
                # Extract token counts from usage dict.
                # ResultMessage.usage is CUMULATIVE across every API call in
                # the agent loop — safe for increment totals, unsafe as a
                # context-window snapshot. The live store is already updated
                # per-AssistantMessage above with the correct per-call values.
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

    except asyncio.CancelledError:
        interrupted = True
        logger.debug(f"[Background] Query interrupted for session {session_id}")

    except Exception as e:
        logger.error(f"[Background] Stream query error for session {session_id}: {e}")
        # Surface the subprocess's own stderr (exit code / stack trace it printed
        # before dying) so a mid-stream exit-1 on the background path records WHY
        # instead of leaking as "Task exception was never retrieved".
        log_captured_cli_stderr(session_id, state.cli_stderr_lines, context="background mid-stream")
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

    # Push agent-memory the CLI wrote this turn up to the shared DB.
    await push_memory(
        project.get("id") if project else None, getattr(options, "cwd", None)
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
        # Offload the (potentially uncached) seed transcript parse to a thread:
        # resolve_for_streaming_init can do a full synchronous Postgres store read in
        # postgres_only mode, which would otherwise block the event loop on turn start.
        initial_messages = await run_sync(
            message_resolver.resolve_for_streaming_init, session_id, sdk_sid, working_dir
        )
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

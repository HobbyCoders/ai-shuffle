"""
Event handlers for WebSocket streaming events.

Extracted from the run_query() event dispatch loop in websocket.py.
Each handler updates the live_session_store and broadcasts via sync_engine.
The WebSocket send (send_json) happens BEFORE dispatch in the caller.

Handler signature:
    async def handle_<event_type>(
        event: dict,
        ctx: EventContext,
        state: MutableStreamState,
    ) -> None
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.config import settings
from app.core.live_session_store import live_session_store
from app.core.streaming_context import StreamingContext
from app.core.sync_engine import sync_engine
from app.core.project_utils import get_project_working_dir
from app.db import database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context objects shared across handlers
# ---------------------------------------------------------------------------

@dataclass
class EventContext:
    """Immutable context for the duration of a single streaming query."""

    ctx: StreamingContext
    session_id: str
    message_id: str
    user_id: Optional[str]
    project_id: Optional[str]
    worktree_id: Optional[str]


@dataclass
class MutableStreamState:
    """Mutable state that handlers read and update during streaming."""

    stream_started: bool = False
    current_tool_id: Optional[str] = None
    current_tool_name: Optional[str] = None
    connection_lost_logged: bool = False
    final_metadata: Dict[str, Any] = field(default_factory=dict)
    query_start_time: float = field(default_factory=time.time)
    # Buffer of in-flight team-tool invocations keyed by tool_id so that
    # `handle_tool_result` can recover the original tool_input / tool_name
    # without re-parsing the live store.
    team_tool_buffer: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Terminal background-task notifications stashed in-loop (deduped via
    # task_registry.mark_notified) so run_query can fire ONE consolidated
    # "unpark" follow-up turn after the streaming loop closes.
    pending_task_notifications: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_working_dir(ectx: EventContext) -> Optional[str]:
    """Best-effort lookup of the parent session's working directory."""
    try:
        if ectx.project_id:
            proj = database.get_project(ectx.project_id)
            if proj:
                return get_project_working_dir(proj)
    except Exception:
        logger.debug("resolve_working_dir: project lookup failed", exc_info=True)
    try:
        return str(settings.workspace_dir)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Individual event handlers
# ---------------------------------------------------------------------------

async def handle_stream_start(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle stream_start event (first or subsequent).

    First stream_start: broadcast stream_start to sync_engine.
    Subsequent stream_starts: broadcast usage_update.
    """
    if not state.stream_started:
        # First stream_start — extract usage and broadcast start
        usage_data = None
        message_data = event.get("message", {})
        if isinstance(message_data, dict):
            usage_data = message_data.get("usage")
            if usage_data:
                await live_session_store.update_usage(
                    ectx.session_id,
                    input_tokens=usage_data.get("input_tokens", 0),
                    output_tokens=usage_data.get("output_tokens", 0),
                    cache_creation_tokens=usage_data.get("cache_creation_input_tokens", 0),
                    cache_read_tokens=usage_data.get("cache_read_input_tokens", 0),
                )

        await sync_engine.broadcast_stream_start(
            session_id=ectx.session_id,
            message_id=ectx.message_id,
            source_device_id=None,
            usage=usage_data,
            user_id=ectx.user_id,
            project_id=ectx.project_id,
            worktree_id=ectx.worktree_id,
        )
        state.stream_started = True
    else:
        # Subsequent stream_start (agentic loops with tool calls)
        message_data = event.get("message", {})
        if isinstance(message_data, dict):
            usage_data = message_data.get("usage")
            if usage_data:
                await live_session_store.update_usage(
                    ectx.session_id,
                    input_tokens=usage_data.get("input_tokens", 0),
                    output_tokens=usage_data.get("output_tokens", 0),
                    cache_creation_tokens=usage_data.get("cache_creation_input_tokens", 0),
                    cache_read_tokens=usage_data.get("cache_read_input_tokens", 0),
                )
                await sync_engine.broadcast_usage_update(
                    session_id=ectx.session_id,
                    usage=usage_data,
                )
                ectx.ctx.debug(logger, f"Broadcast usage update: {usage_data}")


async def _maybe_broadcast_first_start(
    event_type: str,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """If stream hasn't started yet and this is a content event, broadcast stream_start.

    Called for stream_delta, chunk, and tool_use events that arrive before any stream_start.
    """
    if not state.stream_started and event_type in ("stream_delta", "chunk", "tool_use", "system"):
        await sync_engine.broadcast_stream_start(
            session_id=ectx.session_id,
            message_id=ectx.message_id,
            source_device_id=None,
            usage=None,
            user_id=ectx.user_id,
            project_id=ectx.project_id,
            worktree_id=ectx.worktree_id,
        )
        state.stream_started = True


async def handle_stream_delta(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle stream_delta event — text, thinking, or tool_input deltas."""
    await _maybe_broadcast_first_start("stream_delta", ectx, state)

    delta_type = event.get("delta_type", "text")
    content = event.get("content", "")

    if delta_type == "tool_input":
        if state.current_tool_id:
            await live_session_store.update_tool_input_streaming(
                ectx.session_id, state.current_tool_id, content
            )
        chunk_data = {
            "content": content,
            "delta_type": delta_type,
            "index": event.get("index", 0),
        }
        await sync_engine.broadcast_stream_chunk(
            session_id=ectx.session_id,
            message_id=ectx.message_id,
            chunk_type="tool_input",
            chunk_data=chunk_data,
            source_device_id=None,
        )
    elif delta_type == "thinking":
        await live_session_store.append_thinking(ectx.session_id, content)
        chunk_data = {
            "content": content,
            "delta_type": delta_type,
            "index": event.get("index", 0),
        }
        await sync_engine.broadcast_stream_chunk(
            session_id=ectx.session_id,
            message_id=ectx.message_id,
            chunk_type="thinking",
            chunk_data=chunk_data,
            source_device_id=None,
        )
    else:
        # Text delta
        await live_session_store.finalize_thinking(ectx.session_id)
        await live_session_store.append_text(ectx.session_id, content)
        chunk_data = {
            "content": content,
            "delta_type": delta_type,
            "index": event.get("index", 0),
        }
        await sync_engine.broadcast_stream_chunk(
            session_id=ectx.session_id,
            message_id=ectx.message_id,
            chunk_type="text",
            chunk_data=chunk_data,
            source_device_id=None,
        )


async def handle_chunk(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle legacy chunk event (when include_partial_messages=False)."""
    await _maybe_broadcast_first_start("chunk", ectx, state)

    content = event.get("content", "")
    await live_session_store.append_text(ectx.session_id, content)
    chunk_data = {"content": content}
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="text",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_tool_use(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle tool_use event."""
    await _maybe_broadcast_first_start("tool_use", ectx, state)

    tool_id = event.get("id")
    tool_name = event.get("name")
    tool_input = event.get("input") or {}
    state.current_tool_id = tool_id
    state.current_tool_name = tool_name
    await live_session_store.add_tool_use(
        session_id=ectx.session_id,
        tool_id=tool_id,
        name=tool_name,
        tool_input=tool_input,
    )

    # Team tools are handled opportunistically — buffer input for the result
    # handler and notify the registry immediately so the UI sees the team
    # node appear as soon as the call is issued. `Task` is ambiguous (also
    # used for regular subagents) so it only counts when `team_name` is set.
    from app.core.team_registry import TEAM_TOOLS, team_registry
    is_team_tool = (
        tool_name in TEAM_TOOLS
        and (tool_name != "Task" or "team_name" in (tool_input or {}))
    )
    if is_team_tool:
        working_dir = _resolve_working_dir(ectx)
        state.team_tool_buffer[tool_id or ""] = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "working_dir": working_dir,
        }
        try:
            await team_registry.handle_team_tool_use(
                parent_session_id=ectx.session_id,
                tool_name=tool_name,
                tool_input=tool_input,
                working_dir=working_dir,
            )
        except Exception:
            logger.exception("team_registry.handle_team_tool_use failed")
    chunk_data = {
        "content": "",
        "tool_name": event.get("name"),
        "tool_id": tool_id,
        "tool_input": event.get("input"),
    }
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="tool_use",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_tool_result(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle tool_result event."""
    tool_id = event.get("tool_use_id")
    tool_name = state.current_tool_name  # Capture before clearing
    state.current_tool_id = None
    state.current_tool_name = None
    output = event.get("output", "")
    # Screenshot (data URI) extracted from MCP image content blocks — e.g. the
    # Claude-in-Chrome browser tool — for inline display below the tool call.
    screenshot = event.get("screenshot")

    # Team-tool follow-up: discover teammates and spin up transcript tailers.
    buffered = state.team_tool_buffer.pop(tool_id or "", None)
    if buffered:
        from app.core.team_registry import team_registry
        from app.core.team_transcript_watcher import team_transcript_watcher
        try:
            changed = await team_registry.handle_team_tool_result(
                parent_session_id=ectx.session_id,
                tool_name=buffered["tool_name"],
                tool_input=buffered["tool_input"],
                tool_output=output,
                working_dir=buffered.get("working_dir"),
            )
            for rec in changed:
                await team_transcript_watcher.ensure_watching(
                    ectx.session_id, rec
                )
        except Exception:
            logger.exception("team_registry.handle_team_tool_result failed")
    if not output:
        logger.warning(
            f"handle_tool_result: EMPTY output for tool {tool_name} ({tool_id}) "
            f"in session {ectx.session_id}. Event keys: {list(event.keys())}"
        )
    await live_session_store.add_tool_result(
        session_id=ectx.session_id,
        tool_id=tool_id,
        result=output,
        is_error=event.get("is_error", False),
        screenshot=screenshot,
    )
    chunk_data = {
        "content": event.get("output", ""),
        "tool_id": tool_id,
        "tool_name": tool_name,
        "is_error": event.get("is_error", False),
    }
    # Note: the screenshot itself is NOT broadcast in the chunk (it can be ~1MB).
    # It's stored on the live message above and delivered once via the /live
    # delta poll — the same path that carries tool_result content to the UI.
    # Flag tool results that likely modify the working tree so the frontend
    # can trigger an immediate git status poll (adaptive polling).
    if tool_name in {"Edit", "Write", "Bash", "NotebookEdit", "MultiEdit", "computer"}:
        chunk_data["git_hint"] = True
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="tool_result",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_system(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle system message event."""
    # Slash commands (e.g., /context, /compact) may only produce system events
    # with no text/chunk/tool_use. Without broadcasting start, the frontend
    # never starts polling and the streaming bridge skips the response.
    await _maybe_broadcast_first_start("system", ectx, state)

    # Route task_progress to its parent subagent instead of creating a
    # standalone system message.  The SDK emits these periodically with
    # usage stats (tokens, tool count, duration) for running Task agents.
    subtype = event.get("subtype")
    data = event.get("data") or {}
    if subtype == "task_progress":
        tool_use_id = data.get("tool_use_id")
        if tool_use_id:
            updated = await live_session_store.update_subagent_progress(
                session_id=ectx.session_id,
                tool_use_id=tool_use_id,
                usage=data.get("usage"),
            )
            if updated:
                # The live session store already bumped the update_id,
                # so the next frontend poll will pick up the change.
                return

    # Route thinking-token estimates to the active thinking block (a live
    # counter, desktop-app style) instead of rendering them as standalone
    # system-message cards. The newer CLI emits these frequently while the
    # model reasons; surfacing each as a card floods the chat. Always return
    # here — if there's no thinking block to attach to, the estimate is dropped
    # (never falls through to add_system_message).
    if subtype == "thinking_tokens":
        await live_session_store.update_thinking_tokens(
            session_id=ectx.session_id,
            estimated_tokens=data.get("estimated_tokens"),
        )
        return

    # Extract content from data if available (e.g., api_error, local_command
    # events store their display text in data["content"])
    system_content = ""
    if isinstance(data, dict) and "content" in data:
        system_content = str(data["content"])

    await live_session_store.add_system_message(
        session_id=ectx.session_id,
        content=system_content,
        subtype=subtype,
        data=data,
    )
    chunk_data = {
        "content": system_content,
        "subtype": subtype,
        "data": data,
    }
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="system",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_subagent_start(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle subagent_start event."""
    agent_id = event.get("agent_id")
    converted = await live_session_store.convert_tool_to_subagent(
        session_id=ectx.session_id,
        tool_id=event.get("tool_id") or agent_id,
        agent_id=agent_id,
        agent_type=event.get("agent_type", "unknown"),
        description=event.get("description", ""),
        prompt=event.get("prompt", ""),
    )
    if not converted:
        await live_session_store.add_subagent_start(
            session_id=ectx.session_id,
            agent_id=agent_id,
            agent_type=event.get("agent_type"),
            description=event.get("description"),
            prompt=event.get("prompt"),
            tool_id=event.get("tool_id"),
        )
    chunk_data = {
        "agent_id": event.get("agent_id"),
        "agent_type": event.get("agent_type"),
        "description": event.get("description"),
        "prompt": event.get("prompt"),
        "tool_id": event.get("tool_id"),
    }
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="subagent_start",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_subagent_chunk(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle subagent_chunk event — text from a subagent."""
    await live_session_store.append_subagent_text(
        session_id=ectx.session_id,
        agent_id=event.get("agent_id"),
        content=event.get("content", ""),
    )
    chunk_data = {
        "agent_id": event.get("agent_id"),
        "content": event.get("content", ""),
    }
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="subagent_chunk",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_subagent_tool_use(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle subagent_tool_use event."""
    await live_session_store.add_subagent_tool_use(
        session_id=ectx.session_id,
        agent_id=event.get("agent_id"),
        tool_id=event.get("id"),
        tool_name=event.get("name"),
        tool_input=event.get("input", {}),
    )
    chunk_data = {
        "agent_id": event.get("agent_id"),
        "name": event.get("name"),
        "id": event.get("id"),
        "input": event.get("input"),
    }
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="subagent_tool_use",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_subagent_tool_result(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle subagent_tool_result event."""
    await live_session_store.add_subagent_tool_result(
        session_id=ectx.session_id,
        agent_id=event.get("agent_id"),
        tool_id=event.get("tool_use_id"),
        result=event.get("output", ""),
        is_error=event.get("is_error", False),
    )
    chunk_data = {
        "agent_id": event.get("agent_id"),
        "tool_use_id": event.get("tool_use_id"),
        "output": event.get("output", ""),
        "is_error": event.get("is_error", False),
    }
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="subagent_tool_result",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_subagent_done(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle subagent_done event."""
    await live_session_store.complete_subagent(
        session_id=ectx.session_id,
        agent_id=event.get("agent_id"),
        result=event.get("result", ""),
        is_error=event.get("is_error", False),
    )
    chunk_data = {
        "agent_id": event.get("agent_id"),
        "result": event.get("result"),
        "is_error": event.get("is_error", False),
    }
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="subagent_done",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_task_progress(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle task_progress event — route usage stats to parent subagent.

    The SDK emits TaskProgressMessage periodically with usage stats
    (tokens, tool count, duration) for running Agent/Task subagents.
    """
    tool_use_id = event.get("tool_use_id")
    if tool_use_id:
        await live_session_store.update_subagent_progress(
            session_id=ectx.session_id,
            tool_use_id=tool_use_id,
            usage=event.get("usage"),
        )


async def handle_task_notification(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle a terminal task_notification event (background sub-agent finished).

    Marks the matching sub-agent node complete (mirrors handle_task_progress,
    applying final usage if present). It deliberately does NOT call
    add_system_message with empty content — that empty card was the Bug-2
    artifact — and it does NOT start a new query. The parent's "unpark" follow-up
    turn is triggered separately in run_query's post-loop block.
    """
    tool_use_id = event.get("tool_use_id")
    status = event.get("status")
    is_error = status in ("failed", "stopped")
    if tool_use_id:
        await live_session_store.complete_subagent_by_tool_id(
            session_id=ectx.session_id,
            tool_use_id=tool_use_id,
            is_error=is_error,
            usage=event.get("usage"),
        )


async def handle_stream_block_start(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle stream_block_start event — start of a content block."""
    block_type = event.get("block_type")
    content_block = event.get("content_block", {})

    # For tool_use blocks, create the tool message in the live store
    # immediately so polling clients see it right away instead of
    # waiting for the full AssistantMessage (which can take seconds
    # for large tool inputs like Write/Edit/Bash).
    if block_type == "tool_use":
        early_tool_id = content_block.get("id")
        early_tool_name = content_block.get("name", "unknown")
        if early_tool_id:
            state.current_tool_id = early_tool_id
            await live_session_store.add_early_tool_use(
                session_id=ectx.session_id,
                tool_id=early_tool_id,
                name=early_tool_name,
            )

    chunk_data = {
        "block_type": block_type,
        "index": event.get("index"),
        "content_block": content_block,
    }
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="stream_block_start",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_stream_block_stop(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle stream_block_stop event — end of a content block."""
    chunk_data = {"index": event.get("index")}
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="stream_block_stop",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_stream_message_delta(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle stream_message_delta event — final message metadata with output token usage."""
    chunk_data = {
        "delta": event.get("delta", {}),
        "usage": event.get("usage", {}),
    }
    await sync_engine.broadcast_stream_chunk(
        session_id=ectx.session_id,
        message_id=ectx.message_id,
        chunk_type="stream_message_delta",
        chunk_data=chunk_data,
        source_device_id=None,
    )


async def handle_done(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Handle done/interrupted event — end streaming and broadcast."""
    is_interrupted = event.get("type") == "interrupted"
    state.final_metadata = event.get("metadata", {})

    # End live session store streaming
    await live_session_store.end_streaming(
        session_id=ectx.session_id,
        metadata=state.final_metadata,
        interrupted=is_interrupted,
    )

    # Pre-seed JSONL parse cache with live store messages
    from app.core.jsonl_parser import pre_seed_parse_cache, invalidate_parse_cache

    session_row = database.get_session(ectx.session_id)
    sdk_sid = session_row.get("sdk_session_id") if session_row else None
    if sdk_sid:
        live_data = live_session_store.get_session_dict(ectx.session_id)
        if live_data and live_data.get("messages"):
            seed_working_dir = str(settings.workspace_dir)
            seed_project_id = session_row.get("project_id") if session_row else None
            if seed_project_id:
                seed_project = database.get_project(seed_project_id)
                if seed_project:
                    seed_working_dir = get_project_working_dir(seed_project)

            # Build usage data matching the format parse_session_history returns.
            # IMPORTANT: Use live store per-turn values, NOT final_metadata cumulative totals.
            # The live store tracks per-API-call values (input_tokens, cache_creation_tokens,
            # cache_read_tokens) which represent the current context window size.
            # final_metadata contains SESSION-WIDE cumulative totals (tokens_in, cache_creation_tokens,
            # cache_read_tokens) which would massively overcount context usage.
            # Bug: Previously used final_metadata cumulative values → 9x context inflation on session switch.
            live_usage = live_data.get("usage", {})
            seed_usage = {
                "total_tokens_in": live_usage.get("input_tokens", 0),
                "total_tokens_out": live_usage.get("output_tokens", 0),
                "cache_creation_tokens": live_usage.get("cache_creation_tokens", 0),
                "cache_read_tokens": live_usage.get("cache_read_tokens", 0),
                "last_input_tokens": live_usage.get("input_tokens", 0),
                "model": state.final_metadata.get("model"),
            }

            context_used = (
                seed_usage["last_input_tokens"]
                + seed_usage["cache_creation_tokens"]
                + seed_usage["cache_read_tokens"]
            )
            logger.warning(
                f"[ContextUsage:DEBUG] pre_seed_parse_cache session={ectx.session_id} "
                f"| context_used={context_used} "
                f"| last_input={seed_usage['last_input_tokens']} "
                f"| cache_create={seed_usage['cache_creation_tokens']} "
                f"| cache_read={seed_usage['cache_read_tokens']}"
            )

            # The live-store messages carry NO JSONL uuid on freshly streamed
            # entries (LiveMessage.uuid is only set on seeded history). Seeding
            # the parse cache with them poisons every later read — the frontend
            # then sees uuid-less messages and rewind fails with "missing its
            # checkpoint id". Under postgres_only the store IS the source of
            # truth and is fully flushed by the time `done` fires, so re-parse
            # it fresh to recover the uuid-bearing transcript and seed THAT.
            # Keep `seed_usage` (deliberate per-turn, not cumulative — see above).
            seed_messages = live_data["messages"]
            if settings.session_store_mode == "postgres_only":
                try:
                    from app.api.sessions_helpers import (
                        parse_session_history_via_store,
                    )

                    fresh_messages, _fresh_usage = await asyncio.to_thread(
                        parse_session_history_via_store,
                        sdk_sid,
                        seed_working_dir,
                        use_cache=False,
                    )
                    # Guard against a not-yet-flushed turn: only prefer the store
                    # re-parse when it is at least as complete as the live store.
                    # The parser groups entries into >= as many messages, so a
                    # shorter result means the latest turn is missing — fall back
                    # to the live messages rather than make a turn "disappear".
                    if fresh_messages and len(fresh_messages) >= len(seed_messages):
                        seed_messages = fresh_messages
                    else:
                        logger.warning(
                            "[Rewind] store re-parse incomplete for %s "
                            "(fresh=%d live=%d); seeding live messages",
                            sdk_sid,
                            len(fresh_messages) if fresh_messages else 0,
                            len(seed_messages),
                        )
                except Exception as e:
                    logger.warning(
                        "[Rewind] store re-parse failed for %s: %s; "
                        "seeding live messages",
                        sdk_sid,
                        e,
                    )

            pre_seed_parse_cache(sdk_sid, seed_messages, seed_usage, seed_working_dir)
        else:
            invalidate_parse_cache(sdk_sid)

    # The in-memory buffer is no longer torn down on a standalone timer. It stays
    # warm for reconnecting clients and is cleaned when the session ages out of the
    # pool (LRU/idle → SessionPool._do_evict → live-store/registry teardown). The
    # pool refuses to evict a session with an in-flight background sub-agent, so a
    # parked session is never cleaned out from under a running agent.

    # Broadcast stream end
    if state.stream_started:
        await sync_engine.broadcast_stream_end(
            session_id=ectx.session_id,
            message_id=ectx.message_id,
            metadata=state.final_metadata,
            interrupted=is_interrupted,
            source_device_id=None,
        )

        # Notify watching mobile devices that the session finished (best-effort,
        # non-blocking). Skip user-initiated interrupts — the user is present.
        if not is_interrupted:
            session_name = (session_row or {}).get("title") or "Your session"

            # OS/mobile push adapter (unchanged) — reaches devices not on this page.
            try:
                from app.services.push_notification import fire_and_forget_notify

                fire_and_forget_notify(
                    title="Session finished",
                    body=f"{session_name} has finished responding.",
                    data={"type": "session_done", "session_id": ectx.session_id},
                )
            except Exception:
                pass  # Notifications are best-effort; never break streaming.

            # Unified funnel: in-app success toast + bell history, fanned out to
            # every device of this user over the user-scoped WS channel. push=False
            # because the mobile/OS push above already covers off-page devices.
            try:
                from app.services.notification_service import emit_notification

                await emit_notification(
                    ectx.user_id or "admin",
                    severity="success",
                    message=f"{session_name} has finished responding.",
                    title="Session finished",
                    category="session_complete",
                    surface="toast",
                    session_id=ectx.session_id,
                    persist=True,
                    push=False,
                )
            except Exception:
                pass  # Notifications are best-effort; never break streaming.


# ---------------------------------------------------------------------------
# Dispatch table and entry point
# ---------------------------------------------------------------------------

EVENT_HANDLERS: Dict[str, Callable[..., Awaitable[None]]] = {
    "stream_start": handle_stream_start,
    "stream_delta": handle_stream_delta,
    "chunk": handle_chunk,
    "tool_use": handle_tool_use,
    "tool_result": handle_tool_result,
    "system": handle_system,
    "subagent_start": handle_subagent_start,
    "subagent_chunk": handle_subagent_chunk,
    "subagent_tool_use": handle_subagent_tool_use,
    "subagent_tool_result": handle_subagent_tool_result,
    "subagent_done": handle_subagent_done,
    "task_progress": handle_task_progress,
    "task_notification": handle_task_notification,
    "stream_block_start": handle_stream_block_start,
    "stream_block_stop": handle_stream_block_stop,
    "stream_message_delta": handle_stream_message_delta,
    "done": handle_done,
    "interrupted": handle_done,  # Same handler, differentiates via event["type"]
}


async def dispatch_event(
    event: dict,
    ectx: EventContext,
    state: MutableStreamState,
) -> None:
    """Look up and call the handler for an event type.

    Events without a registered handler (e.g. plugin_message, error)
    are silently ignored — they were already sent to the WebSocket by
    the caller.
    """
    event_type = event.get("type")
    handler = EVENT_HANDLERS.get(event_type)
    if handler is not None:
        await handler(event, ectx, state)

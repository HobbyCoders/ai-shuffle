"""
Persistence helpers for query turn results.

Extracted from query_engine.py to isolate DB write logic.
"""

import logging
from typing import Optional, Callable

from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


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


def _extract_connection_error(exc: Exception, cli_stderr_lines: list[str]) -> str:
    """Extract the most useful error message from a connection failure.

    Priority order:
    1. Captured CLI stderr lines (real subprocess output)
    2. ProcessError.stderr attribute (if not a placeholder)
    3. Exception message as-is
    """
    # 1. Prefer captured stderr lines — these are the real subprocess output
    if cli_stderr_lines:
        stderr_msg = " ".join(line.strip() for line in cli_stderr_lines if line.strip())
        if stderr_msg:
            return stderr_msg

    # 2. Check ProcessError.stderr attribute from the SDK
    from claude_agent_sdk._errors import ProcessError
    if isinstance(exc, ProcessError) and exc.stderr:
        # Skip the SDK's useless placeholder
        if exc.stderr != "Check stderr output for details":
            return f"Connection failed (exit code {exc.exit_code}): {exc.stderr}"
        # Placeholder stderr — build message from exit code alone
        if exc.exit_code is not None:
            return f"Connection failed: CLI process exited with code {exc.exit_code}"

    # 3. Fall back to raw exception message
    return f"Connection failed: {exc}"


_SUBAGENT_TOOLS = {"Task", "Agent"}


def _row_for_tool_use(session_id: str, tool_msg: dict, profile_id: Optional[str],
                      subagent_children: Optional[dict] = None) -> dict:
    """Build a DB row for a tool_use entry. Task/Agent stored with role=subagent."""
    tool_name = tool_msg.get("name", "unknown")
    tool_id = tool_msg.get("tool_id")
    tool_input = tool_msg.get("input") or {}

    if tool_name in _SUBAGENT_TOOLS:
        # Embed collected subagent children into the parent's metadata so
        # reload rebuilds the full nested display.
        children = (subagent_children or {}).get(tool_id, [])
        return {
            "session_id": session_id,
            "role": "subagent",
            "content": tool_input.get("description", ""),
            "tool_name": tool_name,
            "tool_input": tool_input,
            "metadata": {
                "tool_id": tool_id,
                "agent_type": (
                    tool_input.get("subagent_type")
                    or tool_input.get("description")
                    or "Agent"
                ),
                "agent_description": tool_input.get("description", ""),
                "agent_prompt": tool_input.get("prompt", ""),
                "agent_status": "completed",
                "agent_children": children,
            },
            "profile_id": profile_id,
        }

    return {
        "session_id": session_id,
        "role": "tool_use",
        "content": f"Using tool: {tool_name}",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "metadata": {"tool_id": tool_id},
        "profile_id": profile_id,
    }


def _row_for_tool_result(session_id: str, tool_msg: dict, profile_id: Optional[str]) -> dict:
    return {
        "session_id": session_id,
        "role": "tool_result",
        "content": tool_msg.get("output", ""),
        "tool_name": tool_msg.get("name", "unknown"),
        "metadata": {"tool_id": tool_msg.get("tool_id")},
        "profile_id": profile_id,
    }


def _row_for_thinking(session_id: str, tb: dict, profile_id: Optional[str], model: Optional[str]) -> dict:
    return {
        "session_id": session_id,
        "role": "thinking",
        "content": tb.get("content", ""),
        "metadata": {"model": model},
        "profile_id": profile_id,
    }


def _row_for_system(session_id: str, sm: dict, profile_id: Optional[str]) -> dict:
    return {
        "session_id": session_id,
        "role": "system",
        "content": sm.get("content", ""),
        "metadata": {
            "subtype": sm.get("subtype", ""),
            "system_data": sm.get("data", {}),
        },
        "profile_id": profile_id,
    }


def _row_for_plugin(session_id: str, pm: dict, profile_id: Optional[str]) -> dict:
    return {
        "session_id": session_id,
        "role": "system",
        "content": pm.get("message", ""),
        "metadata": {
            "subtype": "plugin_warning",
            "plugin_name": pm.get("plugin", ""),
            "severity": pm.get("severity", "info"),
        },
        "profile_id": profile_id,
    }


async def _persist_turn_results(
    session_id: str,
    sdk_session_id: Optional[str],
    tool_messages: list,
    response_text: list,
    metadata: dict,
    profile_id: Optional[str],
    interrupted: bool = False,
    log_prefix: str = "",
    thinking_blocks: Optional[list] = None,
    system_messages: Optional[list] = None,
    plugin_messages: Optional[list] = None,
    turn_events: Optional[list] = None,
    subagent_children: Optional[dict] = None,
) -> Optional[dict]:
    """Persist a completed turn's data to the database in batched writes.

    Combines tool messages + assistant response + thinking blocks + system
    messages into a single transaction (one DB connection, one commit)
    instead of N+1 individual writes.

    Ordering:
    - When turn_events is provided, rows are emitted in the exact order the
      events occurred during streaming (thinking, tool_use, tool_result,
      subagent, system, plugin, asst_text-last). This preserves the visual
      flow of the turn on session reload.
    - Legacy path (no turn_events): rows are bucketed thinking → tools →
      system → plugin → asst_text.

    Subagent handling:
    - Task/Agent tool_use entries are stored with role="subagent".
    - Their child tool_use/tool_result entries are embedded into the
      parent's metadata.agent_children (NOT emitted as top-level rows) so
      the nested display reconstructs correctly on reload.

    Returns the assistant message dict if one was stored, else None.
    """
    from app.db.sessions import add_session_messages_batch

    # Normalise optional lists
    thinking_blocks = thinking_blocks or []
    system_messages = system_messages or []
    plugin_messages = plugin_messages or []
    subagent_children = subagent_children or {}

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

    if turn_events:
        # Ordered path — preserves stream order, skips subagent children
        # (they're embedded in their parent subagent's metadata).
        model = metadata.get("model")
        for ev in turn_events:
            kind = ev.get("kind")
            payload = ev.get("payload") or {}
            if kind == "thinking":
                batch.append(_row_for_thinking(session_id, payload, profile_id, model))
            elif kind == "tool_use":
                # Skip subagent children — they go inside the parent's metadata
                if payload.get("parent_tool_id"):
                    continue
                batch.append(_row_for_tool_use(session_id, payload, profile_id, subagent_children))
            elif kind == "subagent":
                batch.append(_row_for_tool_use(session_id, payload, profile_id, subagent_children))
            elif kind == "tool_result":
                if payload.get("parent_tool_id"):
                    continue
                batch.append(_row_for_tool_result(session_id, payload, profile_id))
            elif kind == "system":
                batch.append(_row_for_system(session_id, payload, profile_id))
            elif kind == "plugin":
                batch.append(_row_for_plugin(session_id, payload, profile_id))
    else:
        # Legacy bucketed path (for call sites that don't pass turn_events)
        for tb in thinking_blocks:
            batch.append(_row_for_thinking(session_id, tb, profile_id, metadata.get("model")))

        for tool_msg in tool_messages:
            # Skip children of subagents — parent row embeds them
            if tool_msg.get("parent_tool_id"):
                continue
            msg_type = tool_msg.get("type")
            if msg_type == "tool_use":
                batch.append(_row_for_tool_use(session_id, tool_msg, profile_id, subagent_children))
            elif msg_type == "tool_result":
                batch.append(_row_for_tool_result(session_id, tool_msg, profile_id))

        for sm in system_messages:
            batch.append(_row_for_system(session_id, sm, profile_id))

        for pm in plugin_messages:
            batch.append(_row_for_plugin(session_id, pm, profile_id))

    # Assistant text response (always last — closes the turn visually)
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

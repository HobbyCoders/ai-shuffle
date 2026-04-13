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

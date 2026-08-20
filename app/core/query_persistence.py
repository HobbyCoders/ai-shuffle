"""
Persistence helpers for query turn results.

Extracted from query_engine.py to isolate DB write logic.
"""

import asyncio
import collections
import collections.abc
import logging
from typing import Any, Deque, Optional, Callable, Set

from app.db import database
from app.db.async_database import run_sync
from app.core.config import settings

logger = logging.getLogger(__name__)


_STDERR_CAPTURE_MAXLEN = 50

# Bound on the live SDK get_context_usage() control call. This is the SINGLE
# source of truth for the timeout: both the turn-end capture below and the GET
# context-usage endpoint drive the call through ``shielded_get_context_usage``.
# A wedged CLI must never hang or delay a request or turn completion.
SDK_CONTEXT_USAGE_TIMEOUT_SECONDS = 5.0

# Strong references to detached capture tasks. asyncio only keeps a weak
# reference to a bare create_task() result, so without this the task could be
# garbage-collected mid-flight; the done-callback discards it on completion.
_context_usage_capture_tasks: "Set[asyncio.Task]" = set()


async def shielded_get_context_usage(adapter: Any) -> Optional[dict]:
    """Run ``adapter.get_context_usage()`` bounded, without cancelling it midflight.

    This is the SINGLE copy of the pattern shared by the turn-end capture and
    the GET context-usage endpoint. The SDK control call runs on a DETACHED task
    guarded by a SHIELD: on timeout we must NOT cancel the in-flight control
    request — cancelling it mid-flight leaks the entry in the SDK client's
    pending_control_responses, which can wedge later control calls. Shielding
    lets the ``wait_for`` time out while the underlying call keeps running to
    completion in the background.

    Returns the raw usage dict on success, or ``None`` when the SDK returns
    ``None`` (control call unsupported). Raises ``asyncio.TimeoutError`` when the
    call does not complete within the bound, so callers can distinguish a
    timeout from an unsupported call.
    """
    usage_task = asyncio.create_task(adapter.get_context_usage())
    # Retrieve any exception on the detached task so a failure after we've timed
    # out doesn't surface as "Task exception was never retrieved".
    usage_task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return await asyncio.wait_for(
        asyncio.shield(usage_task),
        timeout=SDK_CONTEXT_USAGE_TIMEOUT_SECONDS,
    )


async def capture_context_usage(
    session_id: str,
    log_prefix: str = "",
) -> None:
    """Capture and persist the SDK context-usage breakdown for a finished turn.

    Self-contained and best-effort: this peeks the session pool itself (so
    callers need only pass the session id), runs ``adapter.get_context_usage()``
    via ``shielded_get_context_usage``, and writes the payload to
    ``sessions.context_usage_json``. Intended to run DETACHED — see
    ``schedule_context_usage_capture`` — so it never delays a turn's terminal
    event. Any failure/timeout is logged at debug and swallowed: capturing must
    NEVER raise or delay the turn.

    Known gap: ``execute_query`` in query_engine.py holds no live adapter, so a
    turn that completes only through that path is not captured here; the GET
    endpoint's live path still serves such a session while it stays pooled.
    """
    # Lazy import to mirror the endpoint and avoid an import cycle with the pool.
    from app.core.session_pool import get_session_pool
    try:
        state = get_session_pool().peek(session_id)
        if state is None or not state.is_connected or state.adapter is None:
            return
        result = await shielded_get_context_usage(state.adapter)
        if result is None:
            logger.debug(
                f"{log_prefix}context-usage capture unavailable for {session_id}"
            )
            return
        await run_sync(
            database.set_session_context_usage,
            session_id=session_id,
            data=result,
        )
        logger.debug(f"{log_prefix}Captured context-usage for {session_id}")
    except Exception as e:
        logger.debug(
            f"{log_prefix}context-usage capture failed for {session_id}: {e}"
        )


def schedule_context_usage_capture(session_id: str, log_prefix: str = "") -> None:
    """Fire-and-forget the turn-end context-usage capture on a detached task.

    ``capture_context_usage`` self-guards (peeks the pool, checks the
    connection), so turn-end call sites just hand off the session id and move on
    — the capture runs detached and never delays the terminal event. The task is
    held in a module-level set with a done-callback that discards it, giving the
    event loop a strong reference so the detached task isn't GC'd mid-flight.
    """
    task = asyncio.create_task(
        capture_context_usage(session_id, log_prefix=log_prefix)
    )
    _context_usage_capture_tasks.add(task)
    task.add_done_callback(_context_usage_capture_tasks.discard)


def _wrap_stderr_capture(
    original_stderr: Optional[Callable],
) -> tuple[Callable[[str], None], "Deque[str]"]:
    """Wrap a stderr callback to also capture lines into a bounded ring buffer.

    Returns (capturing_callback, captured_lines) so callers can inspect
    captured stderr output for error reporting.

    ``captured_lines`` is a ``collections.deque(maxlen=50)`` rather than an
    unbounded list: the buffer is stored on ``SessionState`` and shared across
    every turn of a long-lived pooled subprocess, so an unbounded list leaked
    for the subprocess's whole lifetime. Only the lines nearest a crash are
    diagnostic, so the ring keeps the newest 50 and drops the oldest.
    """
    captured: "Deque[str]" = collections.deque(maxlen=_STDERR_CAPTURE_MAXLEN)

    def capturing_stderr(line: str):
        captured.append(line)
        if original_stderr:
            original_stderr(line)

    return capturing_stderr, captured


def _extract_connection_error(
    exc: Exception, cli_stderr_lines: "Optional[collections.abc.Iterable[str]]"
) -> str:
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


def log_captured_cli_stderr(
    session_id: str,
    cli_stderr_lines: "Optional[collections.abc.Iterable[str]]",
    context: str = "",
) -> None:
    """Record captured CLI subprocess stderr at ERROR after a mid-stream crash.

    The connect-time handlers already surface captured stderr through
    ``_extract_connection_error`` (it lands in the user-facing error message and
    an ERROR log line). But a subprocess that dies MID-STREAM — the common case
    behind a background task that silently vanishes — raises from
    ``receive_response()`` where ``cli_stderr_lines`` was previously out of
    scope. The real reason (exit code, the stack trace the CLI printed before
    dying) was therefore only ever logged at DEBUG by the stderr callback, and
    the SDK reader-task error otherwise leaked as "Task exception was never
    retrieved". Persisting the capture list on ``SessionState`` lets the
    streaming exception handlers call this so the next exit-1 records WHY.

    Best-effort and never raises — a logging helper must not mask the original
    error it is trying to explain.
    """
    try:
        if not cli_stderr_lines:
            return
        # A long-lived pooled subprocess accumulates stderr across turns; the
        # lines nearest the crash are the most diagnostic, so keep the tail.
        # ``cli_stderr_lines`` is a bounded ``deque`` (see _wrap_stderr_capture)
        # which does NOT support slice indexing, so materialise to a list first.
        recent = list(cli_stderr_lines)[-40:]
        tail = [ln.strip() for ln in recent if ln and ln.strip()]
        if not tail:
            return
        ctx = f" [{context}]" if context else ""
        logger.error(
            "[Captured CLI stderr]%s session=%s: %s",
            ctx, session_id, " | ".join(tail),
        )
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        pass


_SUBAGENT_TOOLS = {"Task", "Agent"}


def _row_for_tool_use(session_id: str, tool_msg: dict, profile_id: Optional[str],
                      subagent_children: Optional[dict] = None) -> dict:
    """Build a DB row for a tool_use entry. Task/Agent stored with role=subagent."""
    tool_name = tool_msg.get("name", "unknown")
    tool_id = tool_msg.get("tool_id")
    tool_input = tool_msg.get("input") or {}
    jsonl_uuid = tool_msg.get("uuid")

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
            "jsonl_uuid": jsonl_uuid,
        }

    return {
        "session_id": session_id,
        "role": "tool_use",
        "content": f"Using tool: {tool_name}",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "metadata": {"tool_id": tool_id},
        "profile_id": profile_id,
        "jsonl_uuid": jsonl_uuid,
    }


def _row_for_tool_result(session_id: str, tool_msg: dict, profile_id: Optional[str]) -> dict:
    return {
        "session_id": session_id,
        "role": "tool_result",
        "content": tool_msg.get("output", ""),
        "tool_name": tool_msg.get("name", "unknown"),
        "metadata": {"tool_id": tool_msg.get("tool_id")},
        "profile_id": profile_id,
        "jsonl_uuid": tool_msg.get("uuid"),
    }


def _row_for_thinking(session_id: str, tb: dict, profile_id: Optional[str], model: Optional[str]) -> dict:
    return {
        "session_id": session_id,
        "role": "thinking",
        "content": tb.get("content", ""),
        "metadata": {"model": model},
        "profile_id": profile_id,
        "jsonl_uuid": tb.get("uuid"),
    }


def _row_for_text(session_id: str, payload: dict, profile_id: Optional[str],
                  metadata: Optional[dict] = None) -> dict:
    """Build an assistant text row from an ordered turn event.

    The closing turn metadata (cost/duration/usage) is attached only to the
    last text row of the turn (passed as `metadata`); intermediate text rows
    carry just the model so the frontend renders cost/duration on the final
    assistant bubble exactly as the legacy single-row path did.
    """
    return {
        "session_id": session_id,
        "role": "assistant",
        "content": payload.get("content", ""),
        "metadata": metadata if metadata is not None else {},
        "profile_id": profile_id,
        "jsonl_uuid": payload.get("uuid"),
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
        "jsonl_uuid": sm.get("uuid"),
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
        "jsonl_uuid": pm.get("uuid"),
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
    prompt: Optional[str] = None,
    working_dir: Optional[str] = None,
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
    # Normalise optional lists
    thinking_blocks = thinking_blocks or []
    system_messages = system_messages or []
    plugin_messages = plugin_messages or []
    subagent_children = subagent_children or {}

    # 1. Update session metadata (sdk_session_id, cost, tokens, turns)
    #
    # This runs in ALL modes — including postgres_only. The `sessions` row is
    # canonical session state that the transcript store does NOT replace: it
    # holds the cost/token/turn aggregates AND, critically, the
    # `sdk_session_id` that links an app session to its SDK transcript. The
    # postgres_only READ path (GET /{session_id}) looks the session_store
    # entries up by `sessions.sdk_session_id`; if this write is skipped the
    # column stays NULL, the store lookup keys on NULL, and a session that
    # streamed and stored its turn correctly reads back EMPTY on reload (the
    # "messages disappear after sending a second message" bug). Only the
    # per-message legacy `session_messages` batch in step 3 belongs behind the
    # postgres_only gate — not this sessions-row metadata update.
    #
    # Context-window columns must store the LAST API call's per-call usage,
    # not ResultMessage aggregates. SDK's ResultMessage.usage sums every API
    # hop in the agent loop (one user turn can make many calls via tools), so
    # `metadata["tokens_in"]` and cache fields there are cumulative per turn
    # and inflate the context bar on reload.
    #
    # Each AssistantMessage in the query loop writes the per-call values into
    # metadata["last_call_*"], overwriting so the final value is the last API
    # call's usage == current context window size. We fall back to the
    # ResultMessage aggregate only when the per-call capture is missing
    # (pre-fix metadata, non-streaming paths without AssistantMessages, etc.)
    # so the update is still safe.
    try:
        # Written unconditionally at turn end. POST /move 409s while
        # has_active_run so a mid-turn move cannot let this restore the old
        # disk's resume id after a successful ownership change.
        if sdk_session_id:
            last_input_tokens = metadata.get(
                "last_call_tokens_in",
                metadata.get("tokens_in", 0),
            )
            last_cache_creation = metadata.get(
                "last_call_cache_creation_tokens",
                metadata.get("cache_creation_tokens", 0),
            )
            last_cache_read = metadata.get(
                "last_call_cache_read_tokens",
                metadata.get("cache_read_tokens", 0),
            )
            await run_sync(
                database.update_session,
                session_id=session_id,
                sdk_session_id=sdk_session_id,
                # `or 0` (not a .get default): these keys can be PRESENT with a
                # None value on the fluid path, so the default never fires and a
                # None would poison the additive SQL update.
                cost_increment=metadata.get("total_cost_usd") or 0,
                tokens_in_increment=metadata.get("tokens_in") or 0,
                tokens_out_increment=metadata.get("tokens_out") or 0,
                turn_increment=metadata.get("num_turns") or 0,
                # Persist per-turn context-window values so /live can render
                # the context bar on session reload without scanning JSONL.
                last_input_tokens=last_input_tokens,
                cache_creation_tokens=last_cache_creation,
                cache_read_tokens=last_cache_read,
            )
            logger.debug(f"{log_prefix}Updated session {session_id}, sdk_session_id={sdk_session_id}")
    except Exception as e:
        logger.error(f"{log_prefix}Failed to update session {session_id} in database: {e}")

    # 2. Build batch of messages to insert in one transaction
    full_response = "\n".join(response_text) if response_text else ""
    batch: list[dict] = []

    # Index of the last assistant-text row appended via the ordered path.
    # The turn-closing metadata (cost/duration/usage) is attached to this row
    # after the loop so the frontend renders cost on the final assistant bubble.
    last_text_batch_idx = -1

    if turn_events:
        # Ordered path — preserves stream order, skips subagent children
        # (they're embedded in their parent subagent's metadata).
        model = metadata.get("model")
        for ev in turn_events:
            kind = ev.get("kind")
            payload = ev.get("payload") or {}
            if kind == "thinking":
                batch.append(_row_for_thinking(session_id, payload, profile_id, model))
            elif kind == "text":
                # Ordered assistant text block. Intermediate blocks carry only
                # the model; the closing metadata is attached to the LAST text
                # row after the loop (see below).
                batch.append(_row_for_text(
                    session_id, payload, profile_id,
                    metadata={"model": model} if model else {},
                ))
                last_text_batch_idx = len(batch) - 1
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

    # Assistant text response — closes the turn.
    assistant_msg = None
    assistant_uuid = metadata.get("jsonl_uuid") or metadata.get("last_assistant_uuid")

    if last_text_batch_idx >= 0:
        # Ordered path already emitted the assistant text as one or more rows in
        # their true stream position (around tool cards). Don't append a second,
        # merged trailing row — that's exactly what reordered the turn on reload.
        # Instead, attach the turn-closing metadata to the LAST text row so cost/
        # duration render on the final bubble, and fold the interrupted marker in.
        final_row = batch[last_text_batch_idx]
        final_row["metadata"] = metadata
        if not final_row.get("jsonl_uuid"):
            final_row["jsonl_uuid"] = assistant_uuid
        if interrupted:
            final_row["content"] = (final_row.get("content", "") or "") + "\n[Interrupted]"
    elif full_response or interrupted or tool_messages:
        # No ordered text rows (legacy bucketed path, tool-only turn, or an
        # interrupt before any text). Emit a single closing assistant row, as
        # before, so the turn still has a final assistant bubble.
        # Capture the assistant message's JSONL uuid so rewind can map a
        # DB row back to its transcript entry for truncation.
        batch.append({
            "session_id": session_id,
            "role": "assistant",
            "content": full_response + ("\n[Interrupted]" if interrupted else ""),
            "metadata": metadata,
            "profile_id": profile_id,
            "jsonl_uuid": assistant_uuid,
        })

    # 3. (removed) Legacy session_messages batch insert. The Postgres session
    # store is the single source of truth for the transcript, so the legacy
    # write was dead weight and is gone along with the SESSION_STORE_MODE flag.

    # 3b. (removed) App-level durable write to PgSessionStore.
    #
    # An earlier iteration wrote the turn to session_store_entries here on the
    # premise that the SDK's --session-mirror was not producing durable rows
    # under postgres_only. Direct inspection of the store proved that premise
    # false: the mirror DOES write complete, rich transcript rows (entrypoint
    # "sdk-py", full usage.iterations, requestId, etc.) for every live turn.
    # The real reason postgres_only sessions read back EMPTY was that the
    # sessions-row `sdk_session_id` linkage (step 1) was gated off, so the read
    # path looked the store up by NULL. With step 1 now running in all modes,
    # the mirror's rows are found directly — an app-level write here only
    # duplicated every user/assistant message on reload, so it has been removed.
    # The SDK mirror is the single durable writer; the read transform is shared.

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
